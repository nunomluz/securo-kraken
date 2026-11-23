"""Coin moves -> Securo trades, priced in the order Securo replays them. Pure: no HTTP.

Securo replays a holding's trades by (date, kind != "buy", external_id): on
each day every buy comes before every sell, whatever the time of day. An
average-cost sell must go at the average Securo will compute, so prices are
settled day by day in that same order, on the values Securo stores: quantity
and price at 6 decimals, the fee at 2.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from itertools import groupby
from typing import Literal

from . import log
from .classify import Move
from .numbers import q6
from .prices import History

MICRO = Decimal("0.01")
_ZERO = Decimal(0)
_ONE = Decimal(1)
_CENT = Decimal("0.01")
_SINGULAR = Decimal("1e-20")
_QUOTED = ("market", "fixed")  # priced from the market, not from a cost basis

Position = tuple[Decimal, Decimal | None]  # (units, cost); cost None while a price is missing


@dataclass
class Trade:
    id: str
    coin: str
    date: date
    kind: Literal["buy", "sell"]
    quantity: Decimal
    price: Decimal | None
    fee: Decimal
    pricing: str
    group: str | None
    time: float
    # What the price is worked out from (the move's figures).
    fiat: str | None = None
    fiat_amount: Decimal | None = None
    fiat_fee: Decimal = _ZERO
    weight_market: bool = False


def trades_from_moves(moves: list[Move]) -> dict[str, list[Trade]]:
    """One trade per move, except rewards: one buy per coin and UTC month."""
    out: dict[str, list[Trade]] = defaultdict(list)
    rewards: dict[tuple[str, str], list[Move]] = defaultdict(list)
    for move in moves:
        if move.reward and move.kind == "buy":
            rewards[(move.coin, _utc(move.time).strftime("%Y-%m"))].append(move)
            continue
        out[move.coin].append(Trade(
            id=f"kraken:{move.id}", coin=move.coin, date=_utc(move.time).date(), kind=move.kind,
            quantity=q6(move.quantity), price=None, fee=_ZERO, pricing=move.pricing, group=move.group,
            time=move.time, fiat=move.fiat, fiat_amount=move.fiat_amount, fiat_fee=move.fiat_fee,
            weight_market=move.weight_market,
        ))
    _date_by_first_leg([trade for trades in out.values() for trade in trades])
    for (coin, month), items in rewards.items():
        first = min(items, key=lambda move: (move.time, move.id))
        quantity = sum((move.quantity for move in items), _ZERO)
        out[coin].append(Trade(
            id=f"kraken:reward:{coin}:{month}", coin=coin, date=_utc(first.time).date(), kind="buy",
            quantity=q6(quantity), price=_ZERO, fee=_ZERO, pricing="zero", group=None, time=first.time,
        ))
    return {coin: sorted(trades, key=_order) for coin, trades in out.items()}


def _date_by_first_leg(trades: list[Trade]) -> None:
    """A swap's or a payment's legs all take the earliest leg's time, so they are priced together."""
    linked: dict[tuple, list[Trade]] = defaultdict(list)
    for trade in trades:
        if trade.group is not None or (trade.weight_market and trade.pricing == "fixed"):
            linked[_share_key(trade) if trade.group is None else ("group", trade.group)].append(trade)
    for members in linked.values():
        first = min(member.time for member in members)
        for member in members:
            member.time, member.date = first, _utc(first).date()


def resolve(trades_by_coin: dict[str, list[Trade]], valuation: str, history: History,
            pairs: dict[str, str]) -> set[str]:
    """Fill every trade's price and fee in place; return the coins not fully priced yet."""
    everything = sorted((trade for trades in trades_by_coin.values() for trade in trades), key=_order)
    pricer = _Pricer(everything, valuation, history, pairs)
    positions: dict[str, Position] = {}
    for _, day in groupby(everything, key=lambda trade: trade.date):
        positions = pricer.settle(list(day), positions)
    return {trade.coin for trade in everything if trade.price is None}


def average_costs(trades: list[Trade]) -> tuple[Decimal, Decimal]:
    """(units, cost of the units held) as Securo's replay of these priced trades leaves them."""
    position: Position = (_ZERO, _ZERO)
    for trade in sorted(trades, key=_order):
        position, _ = _apply(position, trade)
    units, cost = position
    if cost is None:
        raise ValueError("a trade has no price")
    return units, cost if units > 0 else _ZERO


def value_only(trades: list[Trade], current_price: Decimal | None) -> bool:
    """Whether the coin is priced below a cent now or at any trade priced from the market.

    A current price of 0 means no price. A trade price of 0 counts when it was quoted
    (a micro price rounded away), not when it is an exact cost basis of 0 (rewards).
    """
    if current_price and current_price < MICRO:
        return True
    return any(trade.price is not None and trade.price < MICRO and (trade.price > 0 or trade.pricing in _QUOTED)
               for trade in trades)


class _Pricer:
    """Prices one day's trades given every coin's position at the start of that day."""

    def __init__(self, trades: list[Trade], valuation: str, history: History, pairs: dict[str, str]) -> None:
        self.valuation = valuation
        self.history = history
        self.pairs = pairs
        self.shared: dict[tuple, list[Trade]] = defaultdict(list)  # trades splitting one group's figure
        self.sources: dict[str, list[Trade]] = defaultdict(list)  # a swap's average-cost sells
        for trade in trades:
            if trade.weight_market and trade.pricing in ("fixed", "carry"):
                self.shared[_share_key(trade)].append(trade)
            if trade.kind == "sell" and trade.pricing == "avg" and trade.group is not None:
                self.sources[trade.group].append(trade)

    def settle(self, day: list[Trade], start: dict[str, Position]) -> dict[str, Position]:
        """Price the day's trades; return the positions at its end."""
        for trade in day:
            if trade.pricing not in ("avg", "carry"):
                trade.price, trade.fee = self._price(trade)
        averages = self._averages(day, start)
        for trade in day:  # the swaps' sources, at the day's exact averages
            if id(trade) in averages:
                avg = averages[id(trade)]
                trade.price = None if avg is None else q6(avg)
        for trade in day:
            if trade.pricing == "carry":
                trade.price, trade.fee = self._price(trade)
        end = dict(start)
        for trade in day:  # buys, then sells: Securo's order, on what Securo stores
            end[trade.coin], avg = _apply(end.get(trade.coin, (_ZERO, _ZERO)), trade)
            if trade.kind == "sell" and trade.pricing == "avg":
                trade.price = None if avg is None else q6(avg)
        return end

    def _averages(self, day: list[Trade], start: dict[str, Position]) -> dict[int, Decimal | None]:
        """The exact average each of the day's average-cost sells goes at (None: missing).

        A carry buy makes its coin's average depend on its sources' averages that day:
        units_c * avg_c = known cost_c + sum(share * q_source * avg_source) -- linear, solved
        exactly. A sell from an empty position goes at 0, as in Securo.
        """
        units: dict[str, Decimal] = {}
        known: dict[str, Decimal | None] = {}
        for trade in day:
            units[trade.coin], known[trade.coin] = start.get(trade.coin, (_ZERO, _ZERO))
        for trade in day:
            if trade.kind == "buy":
                units[trade.coin] += q6(trade.quantity)
        left, live = dict(units), set()
        for trade in day:
            if trade.kind == "sell":
                if left[trade.coin] > 0:
                    live.add(id(trade))
                left[trade.coin] -= min(q6(trade.quantity), left[trade.coin])
        links: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        needs: dict[str, set[str]] = defaultdict(set)
        for trade in day:
            if trade.kind != "buy":
                continue
            cost = known[trade.coin]
            if trade.pricing != "carry":
                known[trade.coin] = None if cost is None or trade.price is None else (
                    cost + q6(trade.quantity) * q6(trade.price) + _cents(trade.fee))
                continue
            share = self._share(trade)
            if share is None:
                known[trade.coin] = None
                continue
            for source in self._sources(trade):
                needs[trade.coin].add(source.coin)
                if id(source) in live:
                    links[trade.coin][source.coin] += q6(source.quantity) * share[0] / share[1]
        missing = {coin for coin, cost in known.items() if cost is None}
        while grown := {coin for coin, sources in needs.items() if sources & missing} - missing:
            missing |= grown
        averages = _solve(units, known, links, missing)
        return {id(trade): None if trade.coin in missing else averages[trade.coin] if id(trade) in live else _ZERO
                for trade in day if trade.kind == "sell" and trade.pricing == "avg"}

    def _sources(self, trade: Trade) -> list[Trade]:
        return self.sources.get(trade.group, []) if trade.group is not None else []

    def _price(self, trade: Trade) -> tuple[Decimal | None, Decimal]:
        """(price, fee) of a trade not priced at average cost."""
        if trade.pricing == "market":
            price = self._rate(trade.coin, trade.time)
            return (None if price is None else q6(price)), _ZERO
        if trade.pricing == "fixed":
            rate = self._rate(trade.fiat, trade.time)
            share = self._share(trade)
            if rate is None or share is None or trade.fiat_amount is None:
                return None, _ZERO
            part, whole = share
            amount = trade.fiat_amount * rate * part / whole
            return _per_unit(amount, trade.quantity), _cents(trade.fiat_fee * rate * part / whole)
        if trade.pricing == "carry":
            sources = self._sources(trade)
            share = self._share(trade)
            if share is None or any(source.price is None for source in sources):
                return None, _ZERO
            carried = sum((source.quantity * source.price for source in sources), _ZERO)
            part, whole = share
            return _per_unit(carried * part / whole, trade.quantity), _ZERO
        return _ZERO, _ZERO  # "zero": rewards

    def _share(self, trade: Trade) -> tuple[Decimal, Decimal] | None:
        """(part, whole) of the group's figure that is this trade's, by market value."""
        if not trade.weight_market:
            return _ONE, _ONE
        members = self.shared[_share_key(trade)]
        values = [self._market_value(member) for member in members]  # all looked up: missing ones get fetched
        if any(value is None for value in values):
            return None
        whole = sum(values, _ZERO)
        if whole == 0:
            return _ONE, Decimal(len(members))
        return next(value for member, value in zip(members, values) if member is trade), whole

    def _market_value(self, trade: Trade) -> Decimal | None:
        price = self._rate(trade.coin, trade.time)
        return None if price is None else trade.quantity * price

    def _rate(self, family: str | None, time: float) -> Decimal | None:
        """One unit of `family` in the valuation currency at `time`."""
        if family is None:
            return None
        return self.history.get(family, time, self.valuation, self.pairs)


def _apply(position: Position, trade: Trade) -> tuple[Position, Decimal | None]:
    """Securo's replay of one trade: the position after it and, for a sell, the average it went at."""
    units, cost = position
    quantity = q6(trade.quantity)
    if trade.kind == "buy":
        if cost is not None and trade.price is not None:
            cost += quantity * q6(trade.price) + _cents(trade.fee)
        else:
            cost = None
        return (units + quantity, cost), None
    avg = None if cost is None else (cost / units if units > 0 else _ZERO)
    sold = quantity if quantity <= units else units
    return (units - sold, None if cost is None else cost - avg * sold), avg


def _solve(units: dict[str, Decimal], known: dict[str, Decimal | None], links: dict[str, dict[str, Decimal]],
           missing: set[str]) -> dict[str, Decimal]:
    """Each priced coin's exact average after the day's buys (0 when nothing is held)."""
    held = [coin for coin in units if coin not in missing and units[coin] > 0]
    averages = {coin: _ZERO for coin in units if coin not in missing}
    averages.update((coin, known[coin] / units[coin]) for coin in held if not links.get(coin))
    coupled = [coin for coin in held if links.get(coin)]
    coupled += [coin for coin in held if coin not in coupled and any(coin in links[c] for c in coupled)]
    index = {coin: i for i, coin in enumerate(coupled)}
    rows = []
    for coin in coupled:
        row = [_ZERO] * len(coupled) + [known[coin]]
        row[index[coin]] = units[coin]
        for source, weight in links.get(coin, {}).items():
            if source in index:
                row[index[source]] -= weight
            # else: the source holds nothing, so its sells go at 0
        rows.append(row)
    solution = _gauss(rows)
    if solution is None:
        log.warning("trade prices did not settle")
        solution = [known[coin] / units[coin] for coin in coupled]
    averages.update(zip(coupled, solution))
    return averages


def _gauss(rows: list[list[Decimal]]) -> list[Decimal] | None:
    """Solve an augmented linear system (Gauss-Jordan, partial pivoting); None if singular."""
    n = len(rows)
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < _SINGULAR:
            return None
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for r in range(n):
            if r != col and rows[r][col]:
                factor = rows[r][col] / rows[col][col]
                rows[r] = [a - factor * b for a, b in zip(rows[r], rows[col])]
    return [rows[i][n] / rows[i][i] for i in range(n)]


def _share_key(trade: Trade) -> tuple:
    """Trades that split one figure: a swap's received coins, or one fiat payment's coins."""
    if trade.pricing == "carry":
        return "carry", trade.group
    if trade.group is not None:
        return "fixed", trade.kind, trade.group
    return "fixed", trade.kind, trade.time, trade.fiat, trade.fiat_amount, trade.fiat_fee


def _cents(fee: Decimal) -> Decimal:
    """A fee as Securo stores it: 2 decimals, half up."""
    return fee.quantize(_CENT, rounding=ROUND_HALF_UP)


def _per_unit(amount: Decimal, quantity: Decimal) -> Decimal:
    return q6(amount / quantity) if quantity else _ZERO


def _order(trade: Trade) -> tuple:
    return trade.date, trade.kind != "buy", trade.id


def _utc(time: float) -> datetime:
    return datetime.fromtimestamp(time, UTC)
