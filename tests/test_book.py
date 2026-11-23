from datetime import date
from decimal import Decimal

import pytest

from securo_kraken import log
from securo_kraken.book import MICRO, Trade, average_costs, exact_costs, resolve, trades_from_moves, value_only
from securo_kraken.classify import Move
from securo_kraken.numbers import q6
from securo_kraken.prices import History
from tests.securo_replay import reconciles, replay

D = Decimal
HOUR = 3600
DAY = 86400
DAY0 = 1_699_920_000.0  # 2023-11-14 00:00 UTC
DAY1 = DAY0 + DAY
PAIRS = {
    "BTC/EUR": "XXBTZEUR",
    "ETH/EUR": "XETHZEUR",
    "SOL/EUR": "SOLEUR",
    "DOT/EUR": "DOTEUR",
    "EUR/USD": "ZEURZUSD",
}


@pytest.fixture(autouse=True)
def fresh_log():
    log.reset()
    yield
    log.reset()


def history(*entries):
    """(pair key, time, price) -> a History with those hourly prices cached."""
    return History({f"{pair}:{int(time // HOUR)}": price for pair, time, price in entries})


def buy(id, coin, time, quantity, pricing, **extra):
    return Move(id=id, coin=coin, time=time, kind="buy", quantity=D(quantity), pricing=pricing, **extra)


def sell(id, coin, time, quantity, pricing, **extra):
    return Move(id=id, coin=coin, time=time, kind="sell", quantity=D(quantity), pricing=pricing, **extra)


def fiat_buy(id, coin, time, quantity, amount, fee="0", fiat="EUR", **extra):
    return buy(id, coin, time, quantity, "fixed", fiat=fiat, fiat_amount=D(amount), fiat_fee=D(fee), **extra)


def book(moves, hist=None, valuation="EUR"):
    trades = trades_from_moves(moves)
    unresolved = resolve(trades, valuation, hist or history(), PAIRS)
    return trades, unresolved


def by_id(trades, coin, ledger_id):
    return next(trade for trade in trades[coin] if trade.id == f"kraken:{ledger_id}")


def test_fiat_buy_then_avg_withdrawal_realizes_zero():
    trades, unresolved = book([
        fiat_buy("L1", "BTC", DAY0 + 10 * HOUR, "2", "100", fee="1"),
        sell("L2", "BTC", DAY1, "0.5", "avg"),
    ])

    assert unresolved == set()
    bought, withdrawn = trades["BTC"]
    assert (bought.id, bought.date, bought.kind) == ("kraken:L1", date(2023, 11, 14), "buy")
    assert (bought.price, bought.fee) == (D("50"), D("1"))
    assert (withdrawn.id, withdrawn.date, withdrawn.price, withdrawn.fee) == ("kraken:L2", date(2023, 11, 15),
                                                                              D("50.5"), D("0"))
    result = replay(trades["BTC"])
    assert result["realized"] == 0
    assert (result["units"], result["cost"]) == (D("1.5"), D("75.75"))
    assert average_costs(trades["BTC"]) == (D("1.5"), D("75.75"))
    assert reconciles(trades["BTC"], D("1.5"))


def test_fiat_sell_priced_from_fiat_received():
    trades, unresolved = book([
        fiat_buy("L1", "BTC", DAY0, "1", "100"),
        sell("L2", "BTC", DAY1, "0.5", "fixed", fiat="EUR", fiat_amount=D("40"), fiat_fee=D("0.1")),
    ])

    assert unresolved == set()
    sold = by_id(trades, "BTC", "L2")
    assert (sold.price, sold.fee) == (D("80"), D("0.1"))
    assert replay(trades["BTC"])["realized"] == D("-10.1")


def test_same_day_sell_before_buy_priced_in_securo_order():
    trades, unresolved = book([
        fiat_buy("L0", "BTC", DAY0 + 12 * HOUR, "1", "100"),
        sell("L1", "BTC", DAY1 + 10 * HOUR, "1.5", "avg"),
        fiat_buy("L2", "BTC", DAY1 + 15 * HOUR, "1", "130"),
    ])

    assert unresolved == set()
    assert [trade.id for trade in trades["BTC"]] == ["kraken:L0", "kraken:L2", "kraken:L1"]
    # Securo replays the day's buys first: 2 BTC for 230 -> 115 each.
    assert by_id(trades, "BTC", "L1").price == D("115")
    result = replay(trades["BTC"])
    assert result["realized"] == 0
    assert result["low"] >= 0
    assert (result["units"], result["cost"]) == (D("0.5"), D("57.5"))


def test_swap_carries_cost():
    trades, unresolved = book([
        fiat_buy("L1", "ETH", DAY0, "2", "100"),
        sell("L2", "ETH", DAY1, "2", "avg", group="R1"),
        buy("L3", "BTC", DAY1, "0.5", "carry", group="R1"),
    ])

    assert unresolved == set()
    assert by_id(trades, "ETH", "L2").price == D("50")
    assert by_id(trades, "BTC", "L3").price == D("200")
    eth = replay(trades["ETH"])
    assert (eth["realized"], eth["units"]) == (0, 0)
    btc = replay(trades["BTC"])
    assert (btc["units"], btc["cost"]) == (D("0.5"), D("100"))


def test_same_day_chain_a_to_b_to_c_converges():
    trades, unresolved = book([
        fiat_buy("L1", "ETH", DAY0, "2", "100"),
        fiat_buy("L2", "SOL", DAY0, "10", "300"),
        sell("L3", "ETH", DAY1 + 10 * HOUR, "2", "avg", group="R1"),
        buy("L4", "SOL", DAY1 + 10 * HOUR, "10", "carry", group="R1"),
        sell("L5", "SOL", DAY1 + 11 * HOUR, "10", "avg", group="R2"),
        buy("L6", "BTC", DAY1 + 11 * HOUR, "0.5", "carry", group="R2"),
    ])

    assert unresolved == set()
    assert by_id(trades, "SOL", "L4").price == D("10")
    # SOL held 20 for 400 once the day's buys are in -> sold at 20 each.
    assert by_id(trades, "SOL", "L5").price == D("20")
    assert by_id(trades, "BTC", "L6").price == D("400")
    assert replay(trades["ETH"])["realized"] == 0
    sol = replay(trades["SOL"])
    assert (sol["realized"], sol["units"], sol["cost"]) == (0, D("10"), D("200"))
    btc = replay(trades["BTC"])
    assert (btc["units"], btc["cost"]) == (D("0.5"), D("200"))


def test_same_day_swap_and_swap_back_settles(capsys):
    trades, unresolved = book([
        fiat_buy("L1", "ETH", DAY0, "2", "100"),
        fiat_buy("L2", "SOL", DAY0, "10", "300"),
        sell("L3", "ETH", DAY1 + HOUR, "1", "avg", group="R1"),
        buy("L4", "SOL", DAY1 + HOUR, "3", "carry", group="R1"),
        sell("L5", "SOL", DAY1 + 2 * HOUR, "4", "avg", group="R2"),
        buy("L6", "ETH", DAY1 + 2 * HOUR, "0.7", "carry", group="R2"),
    ])

    assert unresolved == set()
    eth_out, sol_in = by_id(trades, "ETH", "L3"), by_id(trades, "SOL", "L4")
    sol_out, eth_in = by_id(trades, "SOL", "L5"), by_id(trades, "ETH", "L6")
    assert sol_in.price == q6(eth_out.quantity * eth_out.price / sol_in.quantity)
    assert eth_in.price == q6(sol_out.quantity * sol_out.price / eth_in.quantity)
    for coin in ("ETH", "SOL"):
        assert abs(replay(trades[coin])["realized"]) < D("0.00001")
    assert capsys.readouterr().err == ""


def test_usd_buy_converted_with_historical_eurusd():
    hist = history(("ZEURZUSD", DAY0 + 10 * HOUR, "1.25"), ("ZEURZUSD", DAY0 + 11 * HOUR, "1.10"))
    trades, unresolved = book([
        fiat_buy("L1", "BTC", DAY0 + 10 * HOUR + 60, "0.5000004", "500", fee="2", fiat="USD"),
    ], hist)

    assert unresolved == set()
    (bought,) = trades["BTC"]
    assert bought.quantity == D("0.500000")
    assert bought.price == D("800")  # 500 USD at 1.25 USD per EUR = 400 EUR for 0.5
    assert bought.fee == D("1.6")


def test_rewards_grouped_monthly_dated_earliest_price_zero():
    november = DAY0 - 13 * DAY + 6 * HOUR  # 2023-11-01 06:00 UTC
    rewards = [buy(f"S{day:02}", "ETH", november + day * DAY, "0.0000004", "zero", reward=True)
               for day in range(30)]
    december = buy("S99", "ETH", november + 31 * DAY, "0.0000004", "zero", reward=True)
    trades, unresolved = book([*reversed(rewards), december])

    assert unresolved == set()
    month, next_month = trades["ETH"]
    assert month.id == "kraken:reward:ETH:2023-11"
    assert (month.date, month.time, month.kind) == (date(2023, 11, 1), november, "buy")
    assert month.quantity == D("0.000012")
    assert (month.price, month.fee, month.pricing) == (D("0"), D("0"), "zero")
    assert next_month.id == "kraken:reward:ETH:2023-12"
    assert next_month.date == date(2023, 12, 2)


def test_missing_history_reports_coin_and_leaves_price_none():
    hist = history()
    trades, unresolved = book([
        buy("L1", "SOL", DAY0 + 10 * HOUR, "10", "market"),
        fiat_buy("L2", "ETH", DAY0 + 11 * HOUR, "1", "100"),
        sell("L3", "SOL", DAY1, "10", "avg", group="R1"),
        buy("L4", "BTC", DAY1, "0.5", "carry", group="R1"),
        sell("L5", "ETH", DAY1, "0.5", "avg"),
    ], hist)

    assert unresolved == {"SOL", "BTC"}
    assert by_id(trades, "SOL", "L1").price is None
    assert by_id(trades, "SOL", "L3").price is None
    assert by_id(trades, "BTC", "L4").price is None
    assert by_id(trades, "ETH", "L2").price == D("100")
    assert by_id(trades, "ETH", "L5").price == D("100")
    assert f"SOLEUR:{int((DAY0 + 10 * HOUR) // HOUR)}" in hist.missing


def test_fixed_split_by_market_value_for_multi_coin_group():
    at = DAY0 + 9 * HOUR
    hist = history(("XXBTZEUR", at, "30000"), ("XETHZEUR", at, "1500"))
    trades, unresolved = book([
        fiat_buy("L1", "BTC", at, "0.01", "300", fee="3", weight_market=True),
        fiat_buy("L2", "ETH", at, "0.1", "300", fee="3", weight_market=True),
    ], hist)

    assert unresolved == set()
    # Market values 300 and 150 -> the 300 EUR paid splits 2:1.
    assert (trades["BTC"][0].price, trades["BTC"][0].fee) == (D("20000"), D("2"))
    assert (trades["ETH"][0].price, trades["ETH"][0].fee) == (D("1000"), D("1"))


def test_carry_split_by_market_value_for_dust_sweep():
    at = DAY1 + 8 * HOUR
    hist = history(("XXBTZEUR", at, "60000"), ("DOTEUR", at, "10"))
    trades, unresolved = book([
        fiat_buy("L1", "ETH", DAY0, "1", "90"),
        fiat_buy("L2", "SOL", DAY0, "3", "30"),
        sell("L3", "ETH", at, "1", "avg", group="R9"),
        sell("L4", "SOL", at, "3", "avg", group="R9"),
        buy("L5", "BTC", at, "0.001", "carry", group="R9", weight_market=True),
        buy("L6", "DOT", at, "2", "carry", group="R9", weight_market=True),
    ], hist)

    assert unresolved == set()
    # 120 EUR carried; market values 60 and 20 -> 90 and 30.
    assert by_id(trades, "BTC", "L5").price == D("90000")
    assert by_id(trades, "DOT", "L6").price == D("15")
    assert replay(trades["ETH"])["realized"] == 0
    assert replay(trades["SOL"])["realized"] == 0


def test_fixed_split_legs_at_different_times_share_one_payment():
    at = DAY0 + 9 * HOUR
    hist = history(("XXBTZEUR", at, "30000"), ("XETHZEUR", at, "1500"))
    trades, unresolved = book([
        fiat_buy("L1", "BTC", at, "0.01", "300", fee="3", group="R5", weight_market=True),
        fiat_buy("L2", "ETH", at + 1, "0.1", "300", fee="3", group="R5", weight_market=True),
    ], hist)

    assert unresolved == set()
    btc, eth = trades["BTC"][0], trades["ETH"][0]
    assert btc.quantity * btc.price + eth.quantity * eth.price == D("300")
    assert btc.fee + eth.fee == D("3")


def test_same_day_full_cycle_conserves_carried_cost(capsys):
    # All ETH -> SOL and all SOL -> ETH on one day: the averages depend on each other.
    trades, unresolved = book([
        fiat_buy("L1", "ETH", DAY0, "2", "100"),
        fiat_buy("L2", "SOL", DAY0, "10", "300"),
        sell("L3", "ETH", DAY1 + HOUR, "2", "avg", group="R1"),
        buy("L4", "SOL", DAY1 + HOUR, "10", "carry", group="R1"),
        sell("L5", "SOL", DAY1 + 2 * HOUR, "20", "avg", group="R2"),
        buy("L6", "ETH", DAY1 + 2 * HOUR, "2", "carry", group="R2"),
    ])

    assert unresolved == set()
    # ETH: 4 units for 100 + 2 x carry; SOL: 20 units for 300 + 10 x carry -> 200 and 35.
    assert [by_id(trades, coin, ledger_id).price for coin, ledger_id in
            (("ETH", "L3"), ("SOL", "L4"), ("SOL", "L5"), ("ETH", "L6"))] == [D("200"), D("40"), D("35"), D("350")]
    eth, sol = replay(trades["ETH"]), replay(trades["SOL"])
    assert eth["units"] == D("2")
    assert abs(eth["cost"] - D("400")) <= D("0.000001") * eth["units"]
    assert sol["units"] == 0
    for result, sold in ((eth, D("2")), (sol, D("20"))):
        assert abs(result["realized"]) <= D("0.0000005") * sold
    assert capsys.readouterr().err == ""


def test_converted_fee_rounded_to_cents_like_securo():
    hist = history(("ZEURZUSD", DAY0, "1.25"))
    trades, unresolved = book([
        fiat_buy("L1", "BTC", DAY0, "1", "500", fee="2.01", fiat="USD"),
        sell("L2", "BTC", DAY1, "1", "avg"),
    ], hist)

    assert unresolved == set()
    assert by_id(trades, "BTC", "L1").fee == D("1.61")  # 2.01 USD = 1.608 EUR
    assert by_id(trades, "BTC", "L2").price == D("401.61")
    assert replay(trades["BTC"])["realized"] == 0


def test_swap_across_midnight_dated_by_its_first_leg():
    received = DAY1 + DAY - 0.5  # 2023-11-15 23:59:59.5 UTC
    trades, unresolved = book([
        fiat_buy("L1", "ETH", DAY0, "2", "100"),
        sell("L3", "ETH", received + 1, "2", "avg", group="R1"),
        buy("L4", "BTC", received, "0.5", "carry", group="R1"),
    ])

    assert unresolved == set()
    spent, got = by_id(trades, "ETH", "L3"), by_id(trades, "BTC", "L4")
    assert (spent.date, spent.time) == (got.date, got.time) == (date(2023, 11, 15), received)
    assert (spent.price, got.price) == (D("50"), D("200"))


def test_non_terminating_average_realizes_within_rounding():
    trades, unresolved = book([
        fiat_buy("L1", "BTC", DAY0, "3", "100"),
        sell("L2", "BTC", DAY1, "1", "avg"),
    ])

    assert unresolved == set()
    assert by_id(trades, "BTC", "L2").price == D("33.333333")
    assert abs(replay(trades["BTC"])["realized"]) <= D("0.0000005")


def _priced(price, pricing="market"):
    return Trade(id="kraken:L1", coin="SHIB", date=date(2023, 11, 14), kind="buy", quantity=D("1000"),
                 price=price, fee=D("0"), pricing=pricing, group=None, time=DAY0)


def test_value_only_below_one_cent():
    assert MICRO == D("0.01")
    assert value_only([_priced(D("0.005"))], D("1"))
    assert value_only([_priced(D("1"))], D("0.009"))
    assert not value_only([_priced(D("0"), "zero"), _priced(D("0.01"))], D("0.01"))
    assert not value_only([_priced(None)], None)


def test_value_only_when_market_price_rounds_to_zero():
    assert value_only([_priced(D("0"))], D("1"))
    assert value_only([_priced(D("0"), "fixed")], D("1"))


def test_value_only_ignores_zero_current_price_and_zero_cost_basis():
    assert not value_only([_priced(D("1"))], D("0"))
    # Rewards swapped or withdrawn carry a cost of exactly 0: no precision is lost.
    assert not value_only([_priced(D("0"), "avg"), _priced(D("0"), "carry")], D("1"))


def test_exact_costs_unrounded_for_value_only_coins():
    hist = history(("DOTEUR", DAY1 + 5 * HOUR, "3.123456789"))
    moves = [
        fiat_buy("L1", "PEPE", DAY0 + 10 * HOUR, "1000000", "0.33", fee="0.01"),  # 0.00000033 a unit: q6 -> 0
        fiat_buy("L2", "PEPE", DAY1, "2000000", "0.62"),
        sell("L3", "PEPE", DAY1 + 2 * HOUR, "1500000", "avg"),  # takes away half the cost: 0.48
        sell("L4", "PEPE", DAY1 + 3 * HOUR, "500000", "avg", group="G"),  # a third of what is left: 0.16
        buy("L5", "BTC", DAY1 + 3 * HOUR, "0.0001", "carry", group="G"),
        buy("L6", "BTC", DAY1 + 4 * HOUR, "0.00001", "zero", reward=True),
        buy("L7", "DOT", DAY1 + 5 * HOUR, "1", "market"),
    ]
    trades, unresolved = book(moves, hist)
    assert unresolved == set()
    exact = exact_costs(trades, "EUR", hist, PAIRS)
    assert exact == {"PEPE": D("0.32"), "BTC": D("0.16"), "DOT": D("3.123456789")}
    assert average_costs(trades["PEPE"])[1] != D("0.32")  # Securo's 6-decimal prices lose it


def test_exact_costs_split_a_payment_by_market_value_and_zero_when_sold_out():
    hist = history(("XXBTZEUR", DAY0, "30000"), ("XETHZEUR", DAY0, "3000"))
    moves = [
        fiat_buy("L1", "BTC", DAY0, "0.01", "600", fee="3", group="R", weight_market=True),
        fiat_buy("L2", "ETH", DAY0, "0.1", "600", fee="3", group="R", weight_market=True),
        sell("L3", "ETH", DAY1, "0.1", "avg"),
    ]
    trades, _ = book(moves, hist)
    assert exact_costs(trades, "EUR", hist, PAIRS) == {"BTC": D("301.5"), "ETH": D(0)}
