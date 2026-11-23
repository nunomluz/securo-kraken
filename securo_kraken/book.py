"""Coin moves -> Securo trades, priced in the order Securo replays them. Pure: no HTTP.

Securo replays a holding's trades by (date, kind != "buy", external_id): on
each day every buy comes before every sell, whatever the time of day. An
average-cost sell must go at the average Securo will compute, so prices are
settled day by day in that same order, on the 6-decimal values Securo stores.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import groupby
from typing import Literal

from . import log
from .classify import Move
from .numbers import q6
from .prices import History

MICRO = Decimal("0.01")
MAX_ROUNDS = 20
_ZERO = Decimal(0)
_ONE = Decimal(1)

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
    for (coin, month), items in rewards.items():
        first = min(items, key=lambda move: (move.time, move.id))
        quantity = sum((move.quantity for move in items), _ZERO)
        out[coin].append(Trade(
            id=f"kraken:reward:{coin}:{month}", coin=coin, date=_utc(first.time).date(), kind="buy",
            quantity=q6(quantity), price=_ZERO, fee=_ZERO, pricing="zero", group=None, time=first.time,
        ))
    return {coin: sorted(trades, key=_order) for coin, trades in out.items()}


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
    """Whether the coin is priced below a cent now or at any priced trade."""
    if current_price is not None and current_price < MICRO:
        return True
    return any(trade.price is not None and trade.price != 0 and trade.price < MICRO for trade in trades)


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
        """Price the day's trades until nothing changes; return the positions at its end."""
        # None means a price is missing. Average-cost sells start at a provisional 0 so a
        # same-day swap and swap back (each side's carry needing the other's average) can settle.
        for trade in day:
            trade.price, trade.fee = (_ZERO if trade.pricing == "avg" else None), _ZERO
        seen = None
        for _ in range(MAX_ROUNDS):
            for trade in day:
                if trade.pricing != "avg":
                    trade.price, trade.fee = self._price(trade)
            end = dict(start)
            for trade in day:  # buys, then sells: Securo's order
                end[trade.coin], avg = _apply(end.get(trade.coin, (_ZERO, _ZERO)), trade)
                if trade.kind == "sell" and trade.pricing == "avg":
                    trade.price = None if avg is None else q6(avg)
            now = [(trade.price, trade.fee) for trade in day]
            if now == seen:
                return end
            seen = now
        log.warning("trade prices did not settle")
        return end

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
            return _per_unit(amount, trade.quantity), trade.fiat_fee * rate * part / whole
        if trade.pricing == "carry":
            sources = self.sources.get(trade.group, []) if trade.group is not None else []
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
            cost += quantity * q6(trade.price) + trade.fee
        else:
            cost = None
        return (units + quantity, cost), None
    avg = None if cost is None else (cost / units if units > 0 else _ZERO)
    sold = quantity if quantity <= units else units
    return (units - sold, None if cost is None else cost - avg * sold), avg


def _share_key(trade: Trade) -> tuple:
    """Trades that split one figure: a swap's received coins, or one fiat payment's coins."""
    if trade.pricing == "carry":
        return "carry", trade.group
    return "fixed", trade.kind, trade.group, trade.time, trade.fiat, trade.fiat_amount, trade.fiat_fee


def _per_unit(amount: Decimal, quantity: Decimal) -> Decimal:
    return q6(amount / quantity) if quantity else _ZERO


def _order(trade: Trade) -> tuple:
    return trade.date, trade.kind != "buy", trade.id


def _utc(time: float) -> datetime:
    return datetime.fromtimestamp(time, UTC)
