"""Provider-contract-1 records: fiat accounts, coin holdings and the trades to send.

Every amount leaves as a string from ``numbers.number``. Trades are recomputed
from scratch each sync; ``diff`` keeps only those Securo hasn't stored yet in
their current form, within the reply's trade cap.
"""

import hashlib
from datetime import date
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
LARGEST = Decimal("999999999.999999")
_ZERO = Decimal(0)
_FLOOR = number(FLOOR)


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
    if q6(value) >= LIMIT:
        log.warning("holding value too large for Securo; capped")
        value = LARGEST
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


def diff(trades_by_coin: dict[str, list[Trade]], sent: dict[str, list], *,
         today: date | None = None) -> tuple[dict[str, list[dict] | None], dict]:
    """Per coin, the trades to send (None: nothing) and the new `sent` state.

    `sent` keeps, per coin, [external_id, short digest of (date, kind, quantity, price, fee)], plus
    the date for a neutralised trade so it is not resent when the date it would get changes.

    A trade is sent when it is new or its (date, kind, quantity, price, fee) changed. A trade
    sent before and no longer produced is neutralised: resent as a floor-quantity buy at no cost
    (it can't take the replay below zero), dated the coin's first trade (else `today`). A coin
    whose changes don't fit in what is left of MAX_TRADES waits whole for a later sync (its
    `sent` unchanged). Coins not in `trades_by_coin` keep their `sent` entry as it is.
    """
    left = MAX_TRADES
    out: dict[str, list[dict] | None] = {}
    after = dict(sent)
    for coin in sorted(trades_by_coin):
        stored = {item[0]: item for item in sent.get(coin, [])}
        before = {trade_id: item[1] for trade_id, item in stored.items()}
        now: dict[str, dict] = {}
        for trade in trades_by_coin[coin]:
            if q6(trade.quantity) > 0:  # Securo needs quantity > 0; the balancing trades cover the rest
                now[trade.id] = _record(trade)
        first = min((record["date"] for record in now.values()), default=(today or date.today()).isoformat())
        gone = {trade_id: stored[trade_id][2] if len(stored[trade_id]) > 2 else first
                for trade_id in before if trade_id not in now}
        for trade_id, day in gone.items():
            now[trade_id] = {"external_id": trade_id, "kind": "buy", "date": day, "quantity": _FLOOR,
                             "price": "0", "fee": "0"}
        digests = {trade_id: _digest(record) for trade_id, record in now.items()}
        changes = [record for trade_id, record in now.items() if before.get(trade_id) != digests[trade_id]]
        if not changes:
            out[coin] = None
            continue
        if len(changes) > left:
            if len(changes) > MAX_TRADES:
                log.warning("a coin has more trades than one sync can send; its trades are left out")
            out[coin] = None
            continue
        left -= len(changes)
        out[coin] = changes
        after[coin] = [[trade_id, digest, gone[trade_id]] if trade_id in gone else [trade_id, digest]
                       for trade_id, digest in digests.items()]
    return out, after


def _record(trade: Trade) -> dict:
    if trade.price is None:
        raise ValueError("a trade has no price")
    return {"external_id": trade.id, "kind": trade.kind, "date": trade.date.isoformat(),
            "quantity": number(q6(trade.quantity)), "price": number(q6(trade.price)), "fee": number(trade.fee)}


def _digest(record: dict) -> str:
    text = "|".join(record[key] for key in ("date", "kind", "quantity", "price", "fee"))
    return hashlib.blake2b(text.encode(), digest_size=6).hexdigest()
