"""Opening and adjustment trades that make a coin's trades add up to its reported quantity.

Sized from scratch every sync and resent under the same ids. The opening buy
(at market, dated before the first known event) covers history Kraken no
longer shows; the adjustment sell (at average cost, dated the sync day) takes
away a surplus. Neither creates a gain when it is made.
"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from .book import Trade
from .numbers import number, q6

TOLERANCE = Decimal("0.00001")
FLOOR = Decimal("0.000001")
_ZERO = Decimal(0)


def balancing(coin: str, trades: list[Trade], target: Decimal, today: date,
              prev: dict) -> tuple[list[Trade], dict]:
    """The balancing trades to add for `coin` and the state to remember for the next sync."""
    position = low = _ZERO
    for item in sorted(trades, key=lambda t: (t.date, t.kind != "buy", t.id)):
        quantity = q6(item.quantity)
        position += quantity if item.kind == "buy" else -quantity
        low = min(low, position)
    dip = max(_ZERO, -low)
    opening = dip + max(_ZERO, target - (position + dip))
    adjust = max(_ZERO, position + opening - target)
    opening = _ZERO if opening <= TOLERANCE else opening
    adjust = _ZERO if adjust <= TOLERANCE else adjust

    before_opening = _before(prev, "opening")
    before_adjust = _before(prev, "adjust")
    opening_date = (min(item.date for item in trades) if trades else today) - timedelta(days=1)
    kept_date = _date(prev.get("adjust_date"))
    keeps = kept_date is not None and before_adjust == adjust
    adjust_date = kept_date if keeps else today

    out: list[Trade] = []
    if opening > 0 or before_opening != 0:
        out.append(_make("opening", coin, "buy", "market", opening or FLOOR, opening_date))
    if adjust > 0 or before_adjust != 0:
        out.append(_make("adjust", coin, "sell", "avg", adjust or FLOOR, adjust_date if adjust else kept_date or today))
    state = {"opening": number(opening), "adjust": number(adjust), "adjust_date": adjust_date.isoformat()}
    return out, state


def _make(name: str, coin: str, kind: str, pricing: str, quantity: Decimal, day: date) -> Trade:
    return Trade(
        id=f"kraken:{name}:{coin}", coin=coin, date=day, kind=kind, quantity=quantity, price=None, fee=_ZERO,
        pricing=pricing, group=None, time=datetime.combine(day, time(), UTC).timestamp(),
    )


def _before(prev: dict, key: str) -> Decimal:
    try:
        return Decimal(str(prev.get(key, "0")))
    except ArithmeticError:
        return _ZERO


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
