"""Securo's ledger replay, copied for tests.

Mirrors `_recompute` (app/services/asset_transaction_service.py) on the order
Securo stores reported trades in (`_reconciled_trades` in
app/services/connection_service.py re-stamps them by
`(date, kind != "buy", external_id)`), and `_reconciled_trades`' acceptance
check. Quantity and price are stored at 6 decimals, so both are replayed as
`q6` values; `fee` is used as given.
"""

from decimal import Decimal

from securo_kraken.numbers import q6

_ZERO = Decimal(0)


def _order(trade) -> tuple:
    return trade.date, trade.kind != "buy", trade.id


def replay(trades) -> dict:
    """Units, cost of the units held, realized gain and the lowest position seen."""
    qty = cost = realized = low = _ZERO
    for trade in sorted(trades, key=_order):
        q = q6(trade.quantity)
        p = q6(trade.price)
        fee = trade.fee
        if trade.kind == "buy":
            cost += q * p + fee
            qty += q
        else:
            avg = (cost / qty) if qty > 0 else _ZERO
            low = min(low, qty - q)
            sell_qty = q if q <= qty else qty
            realized += (p - avg) * sell_qty - fee
            cost -= avg * sell_qty
            qty -= sell_qty
    return {"units": qty, "cost": cost if qty > 0 else _ZERO, "realized": realized, "low": low}


def reconciles(trades, quantity: Decimal) -> bool:
    """Whether Securo would keep this set for a holding reporting `quantity`."""
    if not trades:
        return False
    largest = max([abs(quantity), *(q6(trade.quantity) for trade in trades)])
    tolerance = max(Decimal("0.0001"), largest * Decimal("0.000001"))
    position = _ZERO
    for trade in sorted(trades, key=_order):
        q = q6(trade.quantity)
        position += q if trade.kind == "buy" else -q
        if position < -tolerance:
            return False
    return abs(position - quantity) <= tolerance
