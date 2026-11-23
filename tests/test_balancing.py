import json
from datetime import date
from decimal import Decimal

from securo_kraken.balancing import FLOOR, TOLERANCE, balancing
from securo_kraken.book import Trade
from tests import securo_replay

D = Decimal
TODAY = date(2026, 11, 22)


def trade(id_, day, kind, quantity, price="100"):
    return Trade(id=id_, coin="BTC", date=day, kind=kind, quantity=D(quantity), price=D(price), fee=D(0),
                 pricing="fixed", group=None, time=0.0)


def priced(extra):
    """Give balancing trades an explicit price so they can be replayed."""
    for item in extra:
        item.price = D("100")
    return extra


def test_no_balancing_when_trades_match():
    trades = [trade("a", date(2026, 1, 1), "buy", "2")]
    extra, prev = balancing("BTC", trades, D("2"), TODAY, {})
    assert extra == []
    assert prev == {"opening": "0", "adjust": "0", "adjust_date": TODAY.isoformat()}
    assert TOLERANCE == D("0.00001") and FLOOR == D("0.000001")


def test_opening_for_missing_history():
    trades = [trade("a", date(2026, 1, 10), "buy", "2")]
    extra, prev = balancing("BTC", trades, D("5"), TODAY, {})
    assert len(extra) == 1
    opening = extra[0]
    assert (opening.id, opening.kind, opening.quantity) == ("kraken:opening:BTC", "buy", D("3"))
    assert opening.date == date(2026, 1, 9) and opening.pricing == "market" and opening.price is None
    assert opening.fee == 0 and opening.coin == "BTC" and opening.group is None
    assert opening.time == 1767916800.0  # 2026-01-09T00:00:00Z
    assert prev["opening"] == "3"
    json.dumps(prev)
    assert securo_replay.reconciles(trades + priced(extra), D("5"))


def test_opening_without_trades_is_dated_yesterday():
    extra, _ = balancing("BTC", [], D("1"), TODAY, {})
    assert extra[0].date == date(2026, 11, 21)


def test_adjust_for_surplus_dated_today_then_kept():
    trades = [trade("a", date(2026, 1, 1), "buy", "5")]
    extra, prev = balancing("BTC", trades, D("4"), TODAY, {})
    assert len(extra) == 1
    adjust = extra[0]
    assert (adjust.id, adjust.kind, adjust.quantity) == ("kraken:adjust:BTC", "sell", D("1"))
    assert adjust.date == TODAY and adjust.pricing == "avg" and adjust.price is None and adjust.fee == 0
    assert securo_replay.reconciles(trades + priced(extra), D("4"))
    later = date(2026, 12, 5)
    extra2, prev2 = balancing("BTC", trades, D("4"), later, prev)
    assert extra2[0].date == TODAY
    assert prev2 == prev
    # A changed surplus is redated.
    extra3, prev3 = balancing("BTC", trades, D("3"), later, prev)
    assert extra3[0].date == later and extra3[0].quantity == D("2")
    assert prev3["adjust_date"] == later.isoformat()


def test_shrunk_to_floor_when_no_longer_needed():
    trades = [trade("a", date(2026, 1, 10), "buy", "5")]
    prev = {"opening": "3", "adjust": "1", "adjust_date": "2026-11-01"}
    extra, new_prev = balancing("BTC", trades, D("5"), TODAY, prev)
    by_id = {item.id: item for item in extra}
    assert by_id["kraken:opening:BTC"].quantity == FLOOR
    assert by_id["kraken:opening:BTC"].kind == "buy"
    assert by_id["kraken:adjust:BTC"].quantity == FLOOR
    assert by_id["kraken:adjust:BTC"].kind == "sell"
    assert by_id["kraken:adjust:BTC"].date == date(2026, 11, 1)
    assert new_prev["opening"] == "0" and new_prev["adjust"] == "0"
    # Already at zero last time: not resent.
    again, _ = balancing("BTC", trades, D("5"), TODAY, new_prev)
    assert again == []


def test_within_tolerance_counts_as_zero():
    trades = [trade("a", date(2026, 1, 1), "buy", "2")]
    extra, _ = balancing("BTC", trades, D("2.00001"), TODAY, {})
    assert extra == []
    extra, _ = balancing("BTC", trades, D("1.99999"), TODAY, {})
    assert extra == []
    extra, _ = balancing("BTC", trades, D("2.00002"), TODAY, {})
    assert len(extra) == 1


def test_dip_from_sell_before_any_buy():
    trades = [trade("s", date(2026, 1, 1), "sell", "2"), trade("b", date(2026, 1, 5), "buy", "3")]
    extra, prev = balancing("BTC", trades, D("1"), TODAY, {})
    # 2 opening covers the dip; the adjust then takes the 2 surplus it leaves.
    assert [(i.id, i.quantity) for i in extra] == [("kraken:opening:BTC", D("2")), ("kraken:adjust:BTC", D("2"))]
    assert extra[0].date == date(2025, 12, 31)
    assert securo_replay.reconciles(trades + priced(extra), D("1"))
    assert securo_replay.replay(trades + priced(extra))["low"] == 0


def test_same_day_buy_covers_sell():
    day = date(2026, 1, 1)
    trades = [trade("a-sell", day, "sell", "2"), trade("z-buy", day, "buy", "2")]
    extra, _ = balancing("BTC", trades, D("0"), TODAY, {})
    assert extra == []


def test_dip_and_shortfall_combine():
    trades = [trade("s", date(2026, 1, 1), "sell", "2"), trade("b", date(2026, 1, 5), "buy", "3")]
    extra, _ = balancing("BTC", trades, D("4"), TODAY, {})
    assert extra[0].quantity == D("3")
    assert securo_replay.reconciles(trades + priced(extra), D("4"))


def test_sold_out_coin_reconciles_to_zero():
    trades = [trade("b", date(2026, 1, 1), "buy", "3"), trade("s", date(2026, 2, 1), "sell", "1")]
    extra, _ = balancing("BTC", trades, D("0"), TODAY, {})
    assert [(i.id, i.quantity) for i in extra] == [("kraken:adjust:BTC", D("2"))]
    assert securo_replay.reconciles(trades + priced(extra), D("0"))


def test_kept_date_is_dropped_when_it_would_overdraw():
    trades = [trade("a", date(2026, 1, 1), "buy", "5"), trade("b", date(2026, 11, 10), "sell", "5"),
              trade("c", date(2026, 11, 15), "buy", "1")]
    prev = {"opening": "0", "adjust": "1", "adjust_date": "2026-11-01"}
    extra, state = balancing("BTC", trades, D("0"), TODAY, prev)
    assert [(i.id, i.quantity, i.date) for i in extra] == [("kraken:adjust:BTC", D("1"), TODAY)]
    assert state["adjust_date"] == TODAY.isoformat()
    assert securo_replay.reconciles(trades + priced(extra), D("0"))


def test_kept_date_stays_when_the_ledger_still_replays():
    trades = [trade("a", date(2026, 1, 1), "buy", "5"), trade("b", date(2026, 11, 10), "buy", "1")]
    prev = {"opening": "0", "adjust": "1", "adjust_date": "2026-11-01"}
    extra, state = balancing("BTC", trades, D("5"), TODAY, prev)
    assert extra[0].date == date(2026, 11, 1) and state["adjust_date"] == "2026-11-01"


def test_trades_dated_today():
    trades = [trade("a", date(2026, 1, 1), "buy", "5"), trade("b", TODAY, "sell", "5"), trade("c", TODAY, "buy", "1")]
    extra, _ = balancing("BTC", trades, D("0"), TODAY, {})
    assert [(i.id, i.quantity, i.date) for i in extra] == [("kraken:adjust:BTC", D("1"), TODAY)]
    assert securo_replay.reconciles(trades + priced(extra), D("0"))


def test_adjust_is_sized_from_the_zeroed_opening():
    trades = [trade("s", date(2026, 1, 1), "sell", "0.000005"), trade("b", date(2026, 1, 5), "buy", "1")]
    extra, state = balancing("BTC", trades, D("0.999987"), TODAY, {})
    assert extra == []
    assert state["opening"] == "0" and state["adjust"] == "0"


def test_unparseable_previous_date_is_ignored():
    trades = [trade("a", date(2026, 1, 1), "buy", "5")]
    prev = {"opening": "0", "adjust": "1", "adjust_date": "not-a-date"}
    extra, state = balancing("BTC", trades, D("4"), TODAY, prev)
    assert extra[0].date == TODAY and state["adjust_date"] == TODAY.isoformat()
    prev = {"opening": "0", "adjust": "1", "adjust_date": "not-a-date"}
    extra, _ = balancing("BTC", trades, D("5"), TODAY, prev)
    assert extra[0].date == TODAY and extra[0].quantity == FLOOR
