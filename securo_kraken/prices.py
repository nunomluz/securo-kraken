"""Current and historical Kraken prices in the valuation currency."""

from decimal import Decimal

from . import log
from .assets import route
from .errors import BadRequest, OutOfTime, RateLimited, Transient

HOUR = 3600
LATE_SECONDS = 7 * 86400
RECENT_SECONDS = 86400  # an event this recent may still get its first trade: no "none" yet
TOKENIZED_SUFFIX = ".T"
NONE = "none"


def _legs_product(legs: list[tuple[str, bool]], values: list[Decimal]) -> Decimal:
    total = Decimal(1)
    for (_, invert), price in zip(legs, values):
        total *= (1 / price) if invert else price
    return total


def _ticker(client, pair_keys: list[str], extra: dict[str, str] | None = None) -> dict[str, Decimal]:
    if not pair_keys:
        return {}
    result = client.public("Ticker", {"pair": ",".join(pair_keys), **(extra or {})})
    out: dict[str, Decimal] = {}
    for key, info in result.items():
        try:
            price = Decimal(str(info["c"][0]))
        except (KeyError, IndexError, TypeError, ArithmeticError):
            continue
        if price > 0:
            out[key] = price
    return out


def _ticker_one_by_one(client, pair_keys: list[str], extra: dict[str, str] | None) -> dict[str, Decimal]:
    """Kraken refused a batch (one bad pair spoils it): ask pair by pair, skipping refused ones."""
    out: dict[str, Decimal] = {}
    for key in pair_keys:
        try:
            out.update(_ticker(client, [key], extra))
        except BadRequest:
            log.warning("a price pair was refused")
        except (Transient, RateLimited, OutOfTime):
            log.warning("current prices unavailable")
            break
    return out


def current(client, coins: list[str], valuation: str, pairs: dict[str, str]) -> dict[str, Decimal]:
    """Last price of each coin in the valuation currency; unpriceable coins are left out."""
    routes: dict[str, list[tuple[str, bool]]] = {}
    for coin in coins:
        legs = route(coin, valuation, pairs)
        if legs is None:
            log.warning("no price route for a coin")
            continue
        routes[coin] = legs
    tokenized = {legs[0][0] for coin, legs in routes.items() if coin.endswith(TOKENIZED_SUFFIX) and legs}
    plain = {p for coin, legs in routes.items() for i, (p, _) in enumerate(legs)
             if not (i == 0 and coin.endswith(TOKENIZED_SUFFIX))}
    tokenized -= plain
    last: dict[str, Decimal] = {}
    for keys, extra in ((plain, None), (tokenized, {"asset_class": "tokenized_asset"})):
        try:
            got = _ticker(client, sorted(keys), extra)
        except BadRequest:
            got = _ticker_one_by_one(client, sorted(keys), extra)
        except (Transient, RateLimited, OutOfTime):
            log.warning("current prices unavailable")
            continue
        for key, price in got.items():
            last.setdefault(key, price)
    out: dict[str, Decimal] = {}
    for coin, legs in routes.items():
        if any(p not in last for p, _ in legs):
            log.warning("no current price for a coin")
            continue
        out[coin] = _legs_product(legs, [last[p] for p, _ in legs])
    return out


def _first_trade(trades: list) -> tuple[str, float] | None:
    """(price string, trade time) of a usable first trade, else None."""
    try:
        first = trades[0]
        price = Decimal(str(first[0]))
        when = float(first[2])
    except (IndexError, KeyError, TypeError, ValueError, ArithmeticError):
        return None
    if not price.is_finite() or price <= 0:
        return None
    return str(first[0]), when


class History:
    """Hourly historical prices, cached as "<pair>:<hour>" -> price string.

    A pair with no trade after the event is cached as "none". The fallback price
    then used for a coin and hour is kept as "<coin>:<hour>:fallback", so the lot
    keeps the price it got first instead of following the market.
    """

    def __init__(self, cache: dict[str, str]) -> None:
        self.cache = cache
        self.fallback: dict[str, Decimal] = {}
        self.missing: dict[str, tuple[str, float]] = {}

    def get(self, family: str, time: float, valuation: str, pairs: dict[str, str]) -> Decimal | None:
        legs = route(family, valuation, pairs)
        if legs is None:
            log.warning("no price route for a coin")
            return self.fallback.get(family)
        hour = int(time // HOUR)
        frozen = self.cache.get(_fallback_key(family, hour))
        if frozen is not None:
            return Decimal(frozen)
        values: list[Decimal] = []
        need: list[tuple[str, str]] = []
        unavailable = False
        for pair, _ in legs:
            key = f"{pair}:{hour}"
            cached = self.cache.get(key)
            if cached is None:
                need.append((key, pair))
            elif cached == NONE:
                unavailable = True
            else:
                values.append(Decimal(cached))
        if need:
            for key, pair in need:
                self.missing.setdefault(key, (pair, time))
            return None
        if unavailable:
            price = self.fallback.get(family)
            if price is not None and price > 0:
                log.warning("price taken after the event")
                self.cache[_fallback_key(family, hour)] = str(price)
            return price
        return _legs_product(legs, values)

    def fetch(self, client, valuation: str, pairs: dict[str, str], *, deadline: float,
              now: float | None = None) -> None:
        """Fetch missing (pair, hour) prices until the budget floor `deadline` is reached.

        A pair with no trade yet after an event less than a day before `now` stays missing.
        """
        for key, (pair, time) in list(self.missing.items()):
            if client.remaining() <= deadline:
                return
            try:
                result = client.public("Trades", {"pair": pair, "since": str(int(time)), "count": "1"})
            except BadRequest:  # this pair only: the others are still asked
                log.warning("a price pair was refused")
                self.cache[key] = NONE
                del self.missing[key]
                continue
            except (RateLimited, OutOfTime, Transient):
                log.warning("historical prices unavailable")
                return
            trades = next((v for k, v in result.items() if k != "last" and isinstance(v, list)), [])
            parsed = _first_trade(trades)
            if parsed is None:
                if now is not None and now - time < RECENT_SECONDS:
                    continue  # retried next sync; holdings wait for it
                self.cache[key] = NONE
            else:
                price, when = parsed
                self.cache[key] = price
                if when - time > LATE_SECONDS:
                    log.warning("price taken after the event")
            del self.missing[key]


def _fallback_key(family: str, hour: int) -> str:
    return f"{family}:{hour}:fallback"
