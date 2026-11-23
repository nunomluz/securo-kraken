"""Opening and adjustment trades that make a coin's trades add up to its reported quantity.

Sized from scratch every sync and resent under the same ids. The opening buy
(at market, dated before the first known event, and keeping that date while it
is still before every trade) covers history Kraken no longer shows; the
adjustment sell (at average cost, dated the sync day) takes away a surplus.
Neither creates a gain when it is made.
"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from .book import ADJUST_PREFIX, OPENING_PREFIX, Trade
from .numbers import number, q6

TOLERANCE = Decimal("0.00001")
FLOOR = Decimal("0.000001")
_ZERO = Decimal(0)


def balancing(coin: str, trades: list[Trade], target: Decimal, today: date,
              prev: dict) -> tuple[list[Trade], dict]:
    """The balancing trades to add for `coin` and the state to remember for the next sync."""
    position = sum((q6(t.quantity) if t.kind == "buy" else -q6(t.quantity) for t in trades), _ZERO)
    low = _lowest(trades)
    dip = max(_ZERO, -low)
    opening = dip + max(_ZERO, target - (position + dip))
    opening = _ZERO if opening <= TOLERANCE else opening
    adjust = max(_ZERO, position + opening - target)
    adjust = _ZERO if adjust <= TOLERANCE else adjust

    before_opening = _before(prev, "opening")
    before_adjust = _before(prev, "adjust")
    # The opening keeps its first date (and so its price) while that is still before every trade.
    first = min((item.date for item in trades), default=None)
    opening_date = _date(prev.get("opening_date"))
    if opening_date is None or (first is not None and opening_date >= first):
        opening_date = (first or today) - timedelta(days=1)
    opening_trade = _make(OPENING_PREFIX, coin, "buy", "market", opening, opening_date)
    kept_date = _date(prev.get("adjust_date"))
    adjust_date = today
    if kept_date is not None and before_adjust == adjust:
        # Keep the earlier date only if the ledger still never drops below zero with the sell there;
        # today is always safe (it sorts after that day's buys).
        candidate = _make(ADJUST_PREFIX, coin, "sell", "avg", adjust or FLOOR, kept_date)
        if _lowest([*trades, opening_trade, candidate]) >= -TOLERANCE:
            adjust_date = kept_date

    # A balancing trade no longer needed is sent once at FLOOR; after that `prev` says 0 and it is
    # dropped here, and payload.diff's vanished-trade path keeps it at a FLOOR buy of no cost
    # (+0.000001 units, at most 2 per coin: the opening and the adjustment).
    out: list[Trade] = []
    if opening > 0 or before_opening != 0:
        out.append(_make(OPENING_PREFIX, coin, "buy", "market", opening or FLOOR, opening_date))
    if adjust > 0 or before_adjust != 0:
        # A floor adjust (no longer needed) goes under its old date; state["adjust_date"] only
        # follows a live adjust, so a later need is dated afresh.
        day = adjust_date if adjust else kept_date or today
        out.append(_make(ADJUST_PREFIX, coin, "sell", "avg", adjust or FLOOR, day))
    state = {"opening": number(opening), "adjust": number(adjust), "adjust_date": adjust_date.isoformat(),
             "opening_date": opening_date.isoformat()}
    return out, state


def _lowest(trades: list[Trade]) -> Decimal:
    position = low = _ZERO
    for item in sorted(trades, key=lambda t: (t.date, t.kind != "buy", t.id)):
        quantity = q6(item.quantity)
        position += quantity if item.kind == "buy" else -quantity
        low = min(low, position)
    return low


def _make(prefix: str, coin: str, kind: str, pricing: str, quantity: Decimal, day: date) -> Trade:
    return Trade(
        id=f"{prefix}{coin}", coin=coin, date=day, kind=kind, quantity=quantity, price=None, fee=_ZERO,
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
