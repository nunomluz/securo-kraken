"""Kraken ledger rows -> fiat cash transactions and coin moves. Pure: no HTTP.

Rules follow the spec's "Classification" (1-7), applied in order: internal
rows, rewards, trades netted per refid, deposits/withdrawals, then anything
else by sign. KFEE rows are ignored.
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from . import log
from .assets import family, is_fiat
from .numbers import parse_decimal

TRADE_TYPES = frozenset({"trade", "spend", "receive", "conversion", "sale", "adjustment"})
STAKING_TRANSFERS = frozenset({"spottostaking", "stakingfromspot", "stakingtospot", "spotfromstaking"})
FUTURES_TRANSFERS = frozenset({"spottofutures", "spotfromfutures"})
EARN_INTERNAL = frozenset({"allocation", "deallocation", "autoallocate", "migration"})
REWARD_TYPES = frozenset({"staking", "reward", "dividend", "invite bonus"})
FIAT_REWARD_TYPES = frozenset({"staking", "reward", "earn"})
KNOWN_TYPES = TRADE_TYPES | REWARD_TYPES | frozenset({
    "deposit", "withdrawal", "earn", "transfer", "margin", "rollover", "settled", "credit", "custodytransfer",
})
_ZERO = Decimal(0)


@dataclass(frozen=True)
class Cash:
    external_id: str
    currency: str
    date: date
    amount: Decimal
    kind: Literal["credit", "debit"]
    description: str


@dataclass(frozen=True)
class Move:
    id: str
    coin: str
    time: float
    kind: Literal["buy", "sell"]
    quantity: Decimal
    pricing: Literal["fixed", "zero", "market", "avg", "carry"]
    fiat: str | None = None
    fiat_amount: Decimal | None = None
    fiat_fee: Decimal = _ZERO
    group: str | None = None
    reward: bool = False
    weight_market: bool = False


@dataclass(frozen=True)
class _Row:
    id: str
    time: float
    type: str
    subtype: str
    fam: str
    amount: Decimal
    fee: Decimal

    @property
    def net(self) -> Decimal:
        """Units this row moved, net of its own fee."""
        return self.amount - self.fee

    @property
    def fiat(self) -> bool:
        return is_fiat(self.fam)


def classify(rows: list[dict], altnames: dict[str, str], *,
             edge: float) -> tuple[list[Cash], list[Move], list[dict]]:
    """Classify ledger rows; return (cash, moves, rows held back for the next sync)."""
    raw_groups: dict[str, list[dict]] = defaultdict(list)
    groups: dict[str, list[_Row]] = defaultdict(list)
    for raw in sorted(rows, key=_raw_key):
        key = str(raw.get("refid") or raw["id"])
        raw_groups[key].append(raw)
        row = _parse(raw, altnames)
        if row is not None:
            groups[key].append(row)

    out = _Out()
    held: list[dict] = []
    for key, group in groups.items():
        internal = _internal(group)
        rest = [row for row in group if row.id not in internal]
        legs = [row for row in rest if row.type in TRADE_TYPES]
        if len(legs) == 1 and legs[0].time >= edge:
            held.extend(raw_groups[key])
            continue
        for row in group:
            if row.id in internal:
                out.internal_fee(row)
            elif row.type not in TRADE_TYPES:
                out.single(row)
        if legs:
            out.trade_group(key, legs)

    cash = [item for _, _, item in sorted(out.cash, key=lambda entry: entry[:2])]
    moves = sorted(out.moves, key=lambda move: (move.time, move.id))
    return cash, moves, sorted(held, key=_raw_key)


def compact(move: Move) -> list:
    """A JSON-safe list for state."""
    return [move.id, move.coin, move.time, move.kind, _text(move.quantity), move.pricing, move.fiat,
            _text(move.fiat_amount), _text(move.fiat_fee), move.group, move.reward, move.weight_market]


def expand(item: list) -> Move:
    """The Move that ``compact`` stored."""
    (move_id, coin, time, kind, quantity, pricing, fiat,
     fiat_amount, fiat_fee, group, reward, weight_market) = item
    return Move(id=move_id, coin=coin, time=float(time), kind=kind, quantity=Decimal(quantity),
                pricing=pricing, fiat=fiat,
                fiat_amount=None if fiat_amount is None else Decimal(fiat_amount),
                fiat_fee=Decimal(fiat_fee), group=group, reward=bool(reward),
                weight_market=bool(weight_market))


class _Out:
    """Collects cash (with its sort time) and moves."""

    def __init__(self) -> None:
        self.cash: list[tuple[float, str, Cash]] = []
        self.moves: list[Move] = []

    def move(self, row: _Row, kind: str, quantity: Decimal, pricing: str, **extra) -> None:
        self.moves.append(Move(id=row.id, coin=row.fam, time=row.time, kind=kind, quantity=quantity,
                               pricing=pricing, **extra))

    def _add_cash(self, row: _Row, external_id: str, amount: Decimal, kind: str, description: str) -> None:
        cash = Cash(external_id, row.fam, _utc_date(row.time), amount, kind, description)
        self.cash.append((row.time, external_id, cash))

    def fee_cash(self, row: _Row) -> None:
        if row.fee > 0:
            self._add_cash(row, f"kraken:{row.id}:fee", row.fee, "debit", "Kraken fee")

    def cash_row(self, row: _Row, description: str) -> None:
        """A fiat row as a transaction by sign, plus its fee."""
        if row.amount:
            kind = "credit" if row.amount > 0 else "debit"
            self._add_cash(row, f"kraken:{row.id}", abs(row.amount), kind, description)
        self.fee_cash(row)

    def by_sign(self, row: _Row) -> None:
        """Crypto up -> buy at market; crypto down -> sell at average cost."""
        if row.net > 0:
            self.move(row, "buy", row.net, "market")
        elif row.net < 0:
            self.move(row, "sell", -row.net, "avg")

    def internal_fee(self, row: _Row) -> None:
        """Rule 7: an internal row records nothing but its fee."""
        if row.fee <= 0:
            return
        if row.fiat:
            self.fee_cash(row)
        else:
            self.move(row, "sell", row.fee, "avg")

    def single(self, row: _Row) -> None:
        """Rules 2, 4 and 5 for a row outside any trade group."""
        kind, subtype = row.type, row.subtype
        reward = row.amount > 0 and (
            kind in REWARD_TYPES
            or (kind == "earn" and subtype == "reward")
            or (kind == "transfer" and not subtype)
        )
        if kind not in ("deposit", "withdrawal") and not reward:
            _check_known(row)
        if row.fiat:
            if kind in ("deposit", "withdrawal"):
                description = kind.capitalize()
            elif reward and kind in FIAT_REWARD_TYPES:
                description = "Kraken rewards"
            else:
                description = _label(kind)
            self.cash_row(row, description)
        elif reward and row.net > 0:
            self.move(row, "buy", row.net, "zero", reward=True)
        else:
            self.by_sign(row)

    def trade_group(self, refid: str, legs: list[_Row]) -> None:
        """Rule 3: the trade-type legs of one refid, netted per family."""
        if len(legs) == 1:
            log.warning("lone trade leg")
            self._fallback(legs)
            return
        nets: dict[str, Decimal] = defaultdict(Decimal)
        by_family: dict[str, list[_Row]] = defaultdict(list)
        for row in legs:
            nets[row.fam] += row.net
            by_family[row.fam].append(row)
        fiat_out = [f for f, n in nets.items() if n < 0 and is_fiat(f)]
        fiat_in = [f for f, n in nets.items() if n > 0 and is_fiat(f)]
        coins_out = [f for f, n in nets.items() if n < 0 and not is_fiat(f)]
        coins_in = [f for f, n in nets.items() if n > 0 and not is_fiat(f)]

        def leg(fam: str) -> _Row:
            return max(by_family[fam], key=lambda row: abs(row.amount))

        if not coins_out and not coins_in:
            if len(fiat_out) == 1 and len(fiat_in) == 1:
                description = f"Conversion {fiat_out[0]} → {fiat_in[0]}"
                for row in legs:
                    self.cash_row(row, description)
                return
        elif len(fiat_out) == 1 and not fiat_in and not coins_out:
            paid = by_family[fiat_out[0]]
            # Never negative (a rebate, an odd row): Securo refuses a negative fee or price.
            amount = max(-sum((row.amount for row in paid), _ZERO), _ZERO)
            fee = max(sum((row.fee for row in paid), _ZERO), _ZERO)
            for fam in coins_in:
                self.move(leg(fam), "buy", nets[fam], "fixed", fiat=fiat_out[0], fiat_amount=amount,
                          fiat_fee=fee, group=refid if len(coins_in) > 1 else None,
                          weight_market=len(coins_in) > 1)
            return
        elif len(fiat_in) == 1 and not fiat_out and not coins_in:
            received = by_family[fiat_in[0]]
            amount = max(sum((row.amount for row in received), _ZERO), _ZERO)
            fee = max(sum((row.fee for row in received), _ZERO), _ZERO)
            for fam in coins_out:
                self.move(leg(fam), "sell", -nets[fam], "fixed", fiat=fiat_in[0], fiat_amount=amount,
                          fiat_fee=fee, group=refid if len(coins_out) > 1 else None,
                          weight_market=len(coins_out) > 1)
            return
        elif coins_out and coins_in and not fiat_out and not fiat_in:
            for fam in coins_out:
                self.move(leg(fam), "sell", -nets[fam], "avg", group=refid)
            for fam in coins_in:
                self.move(leg(fam), "buy", nets[fam], "carry", group=refid, weight_market=len(coins_in) > 1)
            return

        log.warning("unusual trade group")
        for fam in coins_out:
            self.move(leg(fam), "sell", -nets[fam], "avg")
        for fam in coins_in:
            self.move(leg(fam), "buy", nets[fam], "market")
        for row in legs:
            if row.fiat:
                self.cash_row(row, _label(row.type))

    def _fallback(self, legs: list[_Row]) -> None:
        for row in legs:
            if row.fiat:
                self.cash_row(row, _label(row.type))
            else:
                self.by_sign(row)


def _internal(group: list[_Row]) -> set[str]:
    """Rule 1: ids of rows that move value between members of one family."""
    ids: set[str] = set()
    for row in group:
        if row.type == "transfer" and row.subtype in STAKING_TRANSFERS:
            ids.add(row.id)
        elif row.type == "earn" and row.subtype != "reward":
            if row.subtype not in EARN_INTERNAL:
                log.unknown("earn_subtype", row.subtype)
            ids.add(row.id)
        elif row.type == "staking" and row.amount < 0:
            ids.add(row.id)
    free = [row for row in group if row.id not in ids]
    for i, first in enumerate(free):
        if first.id in ids:
            continue
        for second in free[i + 1:]:
            if second.id not in ids and _mirrored(first, second):
                ids.update((first.id, second.id))
                break
    return ids


def _mirrored(a: _Row, b: _Row) -> bool:
    return (a.type == b.type and a.subtype == b.subtype and a.fam == b.fam
            and a.amount != 0 and a.amount == -b.amount and a.fee == b.fee)


def _check_known(row: _Row) -> None:
    if row.type not in KNOWN_TYPES and not row.type.startswith("nft"):
        log.unknown("ledger_type", row.type)
    elif row.type == "transfer" and row.subtype and row.subtype not in FUTURES_TRANSFERS:
        log.unknown("transfer_subtype", row.subtype)


def _parse(raw: dict, altnames: dict[str, str]) -> _Row | None:
    fam = family(str(raw.get("asset") or ""), altnames)
    if fam is None:  # KFEE fee credits are not a coin
        return None
    amount = parse_decimal(raw.get("amount"))
    fee = parse_decimal(raw.get("fee") or "0")
    if not fam or amount is None or fee is None:
        log.warning("unreadable ledger row")
        return None
    return _Row(id=str(raw["id"]), time=float(raw["time"]), type=str(raw.get("type") or "").lower(),
                subtype=str(raw.get("subtype") or "").lower(), fam=fam, amount=amount, fee=fee)


def _raw_key(raw: dict) -> tuple[float, str]:
    return float(raw["time"]), str(raw["id"])


def _label(kind: str) -> str:
    """A ledger type as a safe description, e.g. "rollover" -> "Rollover"."""
    return re.sub(r"[^a-z0-9 ]", "", kind.lower()).strip()[:40].capitalize() or "Kraken"


def _utc_date(time: float) -> date:
    return datetime.fromtimestamp(time, UTC).date()


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
