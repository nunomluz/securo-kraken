"""Provider-contract-1 records: fiat accounts, coin holdings and the trades to send.

Every amount leaves as a string from ``numbers.number``. Trades are recomputed
from scratch each sync; ``diff`` keeps only those Securo hasn't stored yet in
their current form, within the reply's trade cap.
"""

from decimal import Decimal

from . import log
from .balancing import FLOOR
from .book import Trade
from .classify import Cash
from .numbers import number, q6

CONTRACT = 1
LOGO = "kraken"
INSTITUTION = "Kraken"
ACCOUNT_NAME = "Kraken"
PAYEE = "Kraken"
MAX_TRADES = 20_000
MAX_TRANSACTIONS = 20_000
LIMIT = Decimal(10) ** 9  # Securo's units column holds 9 digits before the point
_ZERO = Decimal(0)
_FLOOR = number(FLOOR)

# A sent trade as kept in state: [external_id, date, kind, quantity, price, fee].
Sent = list[str]


def account(ccy: str, balance: Decimal, cash: list[Cash]) -> dict:
    unique: dict[str, Cash] = {}
    for item in cash:
        unique.setdefault(item.external_id, item)
    return {
        "external_id": f"kraken:{ccy}",
        "name": f"Kraken {ccy}",
        "type": "investment",
        "balance": number(balance),
        "currency": ccy,
        "institution_name": INSTITUTION,
        "institution_logo": LOGO,
        "transactions": [
            {
                "external_id": item.external_id,
                "description": item.description,
                "amount": number(item.amount),
                "date": item.date.isoformat(),
                "type": item.kind,
                "currency": ccy,
                "payee": PAYEE,
            }
            for item in unique.values()
        ],
    }


def holding(coin: str, *, valuation: str, quantity: Decimal, value: Decimal, price: Decimal | None,
            cost: Decimal, split: dict[str, str], trades: list[dict] | None, value_only: bool,
            tokenized: bool, source: str | None = None) -> dict:
    """One coin. `value_only` (priced below a cent) keeps the quantity and sends no trades."""
    units = q6(quantity)
    metadata: dict[str, str] = dict(split)
    if source:
        metadata["price_source"] = source
    record = {
        "external_id": f"kraken:{coin}",
        "name": coin,
        "ticker": coin[:32],
        "type": "stock" if tokenized else "crypto",
        "currency": valuation,
        "account_name": ACCOUNT_NAME,
        "quantity": number(units),
        "current_value": number(value),
        "purchase_price": number(max(cost, _ZERO)),
        "metadata": metadata,
        "trades": None if value_only else trades,
    }
    if price is not None and price > 0:
        record["unit_price"] = number(price)
    if too_large(quantity):
        log.warning("a coin's quantity is too large for Securo; it is sent with its value only")
        record["quantity"] = None
        record["trades"] = None
    return record


def too_large(quantity: Decimal) -> bool:
    return q6(quantity) >= LIMIT


def diff(trades_by_coin: dict[str, list[Trade]], sent: dict[str, list]) -> tuple[dict[str, list[dict] | None], dict]:
    """Per coin, the trades to send (None: nothing) and the new `sent` state.

    A trade is sent when it is new or its (date, kind, quantity, price, fee) changed. A trade
    sent before and no longer produced is resent at the floor quantity. A coin whose changes
    don't fit in what is left of MAX_TRADES waits whole for a later sync (its `sent` unchanged).
    Coins not in `trades_by_coin` keep their `sent` entry as it is.
    """
    left = MAX_TRADES
    out: dict[str, list[dict] | None] = {}
    after = dict(sent)
    for coin in sorted(trades_by_coin):
        before = {item[0]: item for item in sent.get(coin, [])}
        now: dict[str, Sent] = {}
        for trade in trades_by_coin[coin]:
            if q6(trade.quantity) > 0:  # Securo needs quantity > 0; the balancing trades cover the rest
                now[trade.id] = _sent(trade)
        for trade_id, item in before.items():
            if trade_id not in now:
                now[trade_id] = [trade_id, item[1], item[2], _FLOOR, item[4], item[5]]
        changes = [item for trade_id, item in now.items() if before.get(trade_id) != item]
        if not changes:
            out[coin] = None
            continue
        if len(changes) > left:
            if len(changes) > MAX_TRADES:
                log.warning("a coin has more trades than one sync can send; its trades are left out")
            out[coin] = None
            continue
        left -= len(changes)
        out[coin] = [_record(item) for item in changes]
        after[coin] = list(now.values())
    return out, after


def _sent(trade: Trade) -> Sent:
    if trade.price is None:
        raise ValueError("a trade has no price")
    return [trade.id, trade.date.isoformat(), trade.kind, number(q6(trade.quantity)), number(q6(trade.price)),
            number(trade.fee)]


def _record(item: Sent) -> dict:
    trade_id, day, kind, quantity, price, fee = item
    return {"external_id": trade_id, "kind": kind, "date": day, "quantity": quantity, "price": price, "fee": fee}
