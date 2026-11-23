"""Securo entry point: sync a Kraken spot account (read-only).

Each sync reads the balances, walks the ledger within the time budget and sends
the fiat cash activity it found. Holdings (with their trades) are sent only
once the whole ledger has been read and every price they need is known; until
then the `holdings` key is left out so Securo archives nothing.
"""

import copy
import json
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from . import assets, book, classify, ledger, log, payload, prices
from .balancing import balancing
from .client import Client, Http, urllib_http
from .errors import NeedsAuth, OutOfTime, RateLimited
from .numbers import number, parse_decimal, q6

VERSION = 1
DEFAULT_VALUATION = "EUR"
CACHE_SECONDS = 24 * 3600
TICKER_RESERVE = 5.0  # seconds the historical price lookups leave for the current prices
STATE_WARN_BYTES = 10 * 1024 * 1024  # Securo caps state and data together at 16 MB
_ZERO = Decimal(0)
_ROW_FIELDS = ("id", "refid", "time", "type", "subtype", "aclass", "asset", "amount", "fee")


class Kraken:
    def __init__(
        self,
        *,
        http: Http | None = None,
        clock: Callable[[], float] | None = None,
        wall: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._http = http
        self._clock = clock
        self._wall = wall or time.time
        self._sleep = sleep
        self._today = today

    def sync(self, params: dict, context) -> dict:
        log.reset()
        original = params.get("state") or {}
        state = copy.deepcopy(original)
        state["v"] = VERSION
        secrets = getattr(context, "secrets", None) or {}
        config = getattr(context, "config", None) or {}
        valuation = _valuation(config.get("currency"), state.get("valuation"))
        client = self._client(secrets)
        try:
            balances = client.private("BalanceEx")
        except (RateLimited, OutOfTime):
            return {"state": original, "data": None}
        refreshed = self._refresh(client, state, list(balances))
        if refreshed is None:
            return {"state": original, "data": None}
        state["valuation"] = valuation

        lstate = state.setdefault("ledger", {})
        rows, caught_up = ledger.advance(client, lstate)
        if not refreshed:
            self._refresh(client, state, [str(row.get("asset") or "") for row in rows])
        altnames = state["altnames"]
        edge = max(lstate.get("newest") or 0.0, self._wall()) - ledger.OVERLAP if caught_up else float("-inf")
        waiting = {row["id"]: row for row in state.get("held", [])}
        waiting.update((row["id"], _stored_row(row)) for row in rows)
        # Rows at the time where an interrupted walk stopped may have legs still unread: they wait.
        cut = lstate.get("cursor") if not caught_up else None
        later = [row for row in waiting.values() if cut is not None and float(row["time"]) <= cut]
        ready = [row for row in waiting.values() if cut is None or float(row["time"]) > cut]
        cash, moves, held = classify.classify(ready, altnames, edge=edge)
        held = sorted([*held, *later], key=lambda row: (float(row["time"]), row["id"]))
        state["held"] = held
        _add_moves(state, moves)

        totals, splits = _balances(balances, altnames)
        fiats = set(state.get("fiats", []))
        fiats |= {fam for fam, total in totals.items() if assets.is_fiat(fam) and total}
        fiats |= {item.currency for item in cash} | {move.fiat for move in moves if move.fiat}
        coins = set(state.get("coins", []))
        coins |= {fam for fam, total in totals.items() if not assets.is_fiat(fam) and total}
        coins |= {move.coin for move in moves}
        state["fiats"], state["coins"] = sorted(fiats), sorted(coins)

        pending = [*(_cash_from_state(item) for item in state.pop("cash", [])), *cash]
        if len(pending) > payload.MAX_TRANSACTIONS:
            state["cash"] = [_cash_to_state(item) for item in pending[payload.MAX_TRANSACTIONS:]]
            pending = pending[:payload.MAX_TRANSACTIONS]
        by_currency: dict[str, list] = defaultdict(list)
        for item in pending:
            by_currency[item.currency].append(item)
        data: dict = {"contract": payload.CONTRACT}
        if fiats:
            data["accounts"] = [payload.account(ccy, totals.get(ccy, _ZERO), by_currency.get(ccy, []))
                                for ccy in sorted(fiats)]
        if caught_up and not held:
            found = self._holdings(client, state, totals, splits)
            if found is not None:
                data["holdings"] = found
        if len(json.dumps(state)) > STATE_WARN_BYTES:
            log.warning("plugin state is getting large")
        return {"state": state, "data": data if len(data) > 1 else None}

    def _client(self, secrets: dict) -> Client:
        kwargs: dict = {"http": self._http or urllib_http, "wall": self._wall}
        if self._clock is not None:
            kwargs["clock"] = self._clock
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        return Client(str(secrets.get("api_key") or ""), str(secrets.get("api_secret") or ""), **kwargs)

    def _refresh(self, client: Client, state: dict, codes: list[str]) -> bool | None:
        """Refresh names and pairs when a day old or a code shows up that isn't known.

        True: refreshed now; False: the cache is used; None: nothing is known and Kraken
        couldn't be asked within the budget.
        """
        altnames = state.get("altnames")
        stale = altnames is None or self._wall() - state.get("altnames_at", 0) >= CACHE_SECONDS
        unknown = set(state.get("unknown", []))
        new = altnames is not None and any(
            code and not _known(code, altnames) and code not in unknown for code in codes)
        if not stale and not new:
            return False
        try:
            fresh = assets.altnames_from(client.public("Assets"))
            pairs = assets.index_pairs(client.public("AssetPairs"), fresh)
        except (RateLimited, OutOfTime):
            return False if altnames is not None else None
        now = self._wall()
        state.update(altnames=fresh, altnames_at=now, pairs=pairs, pairs_at=now)
        state["unknown"] = sorted(code for code in set(codes) | unknown if code and not _known(code, fresh))
        return True

    def _holdings(self, client: Client, state: dict, totals: dict[str, Decimal],
                  splits: dict[str, dict[str, str]]) -> list[dict] | None:
        """Every coin ever seen, or None while a price is still missing."""
        valuation, pairs = state["valuation"], state["pairs"]
        coins = state["coins"]
        moves = [classify.expand(item) for item in state.get("moves", [])]
        # Trades too small to send (q6 quantity 0) are left out everywhere, so costs match Securo's replay.
        trades = {coin: [trade for trade in items if q6(trade.quantity) > 0]
                  for coin, items in book.trades_from_moves(moves).items()}
        today = self._today() if self._today else datetime.fromtimestamp(self._wall(), UTC).date()
        before = state.get("balancing", {})
        sizes: dict[str, dict] = {}
        for coin in coins:
            target = q6(max(totals.get(coin, _ZERO), _ZERO))
            extra, sizes[coin] = balancing(coin, trades.get(coin, []), target, today, before.get(coin, {}))
            trades[coin] = [*trades.get(coin, []), *extra]

        last: dict[str, str] = state.setdefault("last_value", {})
        fiats = sorted({move.fiat for move in moves if move.fiat and move.fiat != valuation})
        families = set(coins) | set(fiats)
        history = prices.History(state.setdefault("prices", {}))
        history.fallback = _fallback(families, {}, last)
        book.resolve(trades, valuation, history, pairs)
        if history.missing:
            history.fetch(client, valuation, pairs, deadline=TICKER_RESERVE)
        held = [coin for coin in coins if totals.get(coin, _ZERO) > 0]
        current = prices.current(client, held + fiats, valuation, pairs)
        last.update((family, str(price)) for family, price in current.items())
        history.fallback = _Fallback(_fallback(families, current, last))
        history.missing.clear()
        unresolved = book.resolve(trades, valuation, history, pairs)
        if unresolved or history.missing:
            log.warning("some prices are not known yet; holdings wait for a later sync")
            return None

        sent_before = state.get("sent", {})
        sticky = set(state.get("value_only", []))
        for coin in coins:
            if sent_before.get(coin):
                sticky.discard(coin)  # a coin whose ledger Securo stores keeps it
            elif len(trades[coin]) > payload.MAX_TRADES:
                log.warning("too many trades for one coin; sent its value only")
                sticky.add(coin)
            elif book.value_only(trades[coin], current.get(coin)):
                sticky.add(coin)
        tradable = {coin: trades[coin] for coin in coins
                    if coin not in sticky and not payload.too_large(totals.get(coin, _ZERO))}
        to_send, sent = payload.diff(tradable, sent_before, today=today)
        exact = book.exact_costs(trades, valuation, history, pairs)
        records = []
        for coin in coins:
            quantity = max(totals.get(coin, _ZERO), _ZERO)
            price, source = _price(coin, current, last, valuation, pairs) if quantity else (None, None)
            cost = exact[coin] if coin not in tradable else book.average_costs(trades[coin])[1]
            records.append(payload.holding(
                coin, valuation=valuation, quantity=quantity, value=q6(quantity) * (price or _ZERO), price=price,
                cost=cost, split=splits.get(coin, {}), trades=to_send.get(coin), value_only=coin in sticky,
                tokenized=coin.endswith(prices.TOKENIZED_SUFFIX), source=source))
        state.update(sent=sent, balancing=sizes, value_only=sorted(sticky))
        return records


plugin = Kraken()


def _valuation(value: object, fixed: str | None) -> str:
    currency = str(value or "").strip().upper() or DEFAULT_VALUATION
    if currency not in assets.VALUATIONS:
        raise NeedsAuth("Value assets in EUR, USD, GBP, CAD, CHF, AUD or JPY.", code="currency_invalid")
    if fixed and fixed != currency:
        raise NeedsAuth("Add a new connection to value assets in another currency.", code="currency_changed")
    return currency


def _known(code: str, altnames: dict[str, str]) -> bool:
    stem = code.partition(".")[0]
    names = altnames.keys() | set(altnames.values())
    return code == "KFEE" or stem in names or stem.rstrip("0123456789") in names


def _balances(result: dict, altnames: dict[str, str]) -> tuple[dict[str, Decimal], dict[str, dict[str, str]]]:
    """Per family: the sum of `balance` over its Kraken codes, and each code's part."""
    totals: dict[str, Decimal] = defaultdict(Decimal)
    splits: dict[str, dict[str, str]] = defaultdict(dict)
    for code, info in result.items():
        fam = assets.family(code, altnames)
        if fam is None:
            continue
        amount = parse_decimal(info.get("balance")) if isinstance(info, dict) else None
        if amount is None:
            log.warning("unreadable balance")
            continue
        totals[fam] += amount
        splits[fam][code] = number(amount)
    return totals, splits


def _price(coin: str, current: dict[str, Decimal], last: dict[str, str], valuation: str,
           pairs: dict[str, str]) -> tuple[Decimal | None, str]:
    """The coin's price now and where it came from: Kraken pairs, the last price seen, or none."""
    if coin in current:
        legs = assets.route(coin, valuation, pairs) or []
        return current[coin], "+".join(pair for pair, _ in legs)
    if coin in last:
        log.warning("a coin is valued at its last known price")
        return Decimal(last[coin]), "last"
    log.warning("no price found for a coin; valued at 0")
    return None, "none"


def _fallback(families: set[str], current: dict[str, Decimal], last: dict[str, str]) -> dict[str, Decimal]:
    """Price used when Kraken has no historical price: now, else the last seen, else 0."""
    return {fam: current.get(fam) or Decimal(last.get(fam, "0")) for fam in families}


class _Fallback(dict):
    """The fallback prices, saying so whenever a price of 0 is actually used."""

    def get(self, key, default=None):
        value = super().get(key, default)
        if value is not None and not value:
            log.warning("no price found for a coin; valued at 0")
        return value


def _stored_row(row: dict) -> dict:
    """A ledger row as kept in state: the fields classify reads, JSON-safe."""
    return {key: str(row[key]) if isinstance(row.get(key), Decimal) else row.get(key) for key in _ROW_FIELDS}


def _add_moves(state: dict, moves: list[classify.Move]) -> None:
    """Keep new moves; reward buys are summed into one move per coin and UTC month as they arrive."""
    stored = state.setdefault("moves", [])
    index = {item[0]: i for i, item in enumerate(stored)}
    for move in moves:
        if move.reward and move.kind == "buy":
            month = datetime.fromtimestamp(move.time, UTC).strftime("%Y-%m")
            key = f"reward:{move.coin}:{month}"
            if key in index:
                old = classify.expand(stored[index[key]])
                move = replace(old, quantity=old.quantity + move.quantity, time=min(old.time, move.time))
                stored[index[key]] = classify.compact(move)
                continue
            move = replace(move, id=key)
        if move.id not in index:
            index[move.id] = len(stored)
            stored.append(classify.compact(move))


def _cash_to_state(item: classify.Cash) -> list:
    return [item.external_id, item.currency, item.date.isoformat(), number(item.amount), item.kind, item.description]


def _cash_from_state(item: list) -> classify.Cash:
    external_id, currency, day, amount, kind, description = item
    return classify.Cash(external_id, currency, date.fromisoformat(day), Decimal(amount), kind, description)
