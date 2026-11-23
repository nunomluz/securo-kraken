"""Current and historical Kraken prices in the valuation currency."""

from decimal import Decimal

from . import log
from .assets import route
from .errors import OutOfTime, RateLimited

HOUR = 3600
LATE_SECONDS = 7 * 86400
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


def current(client, coins: list[str], valuation: str, pairs: dict[str, str]) -> dict[str, Decimal]:
    """Last price of each coin in the valuation currency; unpriceable coins are left out."""
    routes: dict[str, list[tuple[str, bool]]] = {}
    for coin in coins:
        legs = route(coin, valuation, pairs)
        if legs is None:
            log.warning("no price route for a coin")
            continue
        routes[coin] = legs
    plain = sorted({p for coin, legs in routes.items() if not coin.endswith(TOKENIZED_SUFFIX) for p, _ in legs})
    tokenized = sorted({p for coin, legs in routes.items() if coin.endswith(TOKENIZED_SUFFIX) for p, _ in legs})
    last = _ticker(client, plain)
    if tokenized:
        last.update(_ticker(client, tokenized, {"asset_class": "tokenized_asset"}))
    out: dict[str, Decimal] = {}
    for coin, legs in routes.items():
        if any(p not in last for p, _ in legs):
            log.warning("no current price for a coin")
            continue
        out[coin] = _legs_product(legs, [last[p] for p, _ in legs])
    return out


class History:
    """Hourly historical prices, cached as "<pair>:<hour>" -> price string."""

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
            return self.fallback.get(family)
        return _legs_product(legs, values)

    def fetch(self, client, valuation: str, pairs: dict[str, str], *, deadline: float) -> None:
        """Fetch missing (pair, hour) prices until the budget floor `deadline` is reached."""
        for key, (pair, time) in list(self.missing.items()):
            if client.remaining() <= deadline:
                return
            try:
                result = client.public("Trades", {"pair": pair, "since": str(int(time)), "count": "1"})
            except (RateLimited, OutOfTime):
                return
            trades = next((v for k, v in result.items() if k != "last" and isinstance(v, list)), [])
            if not trades:
                self.cache[key] = NONE
            else:
                first = trades[0]
                self.cache[key] = str(first[0])
                if float(first[2]) - time > LATE_SECONDS:
                    log.warning("price taken after the event")
            del self.missing[key]
