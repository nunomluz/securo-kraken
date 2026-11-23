import json
from datetime import date
from decimal import Decimal

import pytest

from securo_kraken import log
from securo_kraken.classify import Cash, Move, classify, compact, expand
from tests.factories import ledger_row

T = 1_700_000_000.0  # 2023-11-14 22:13:20 UTC
DAY = date(2023, 11, 14)
FAR = T + 10**7  # an edge nothing reaches
ALT = {"XXBT": "XBT", "ZEUR": "EUR", "ZUSD": "USD", "XETH": "ETH", "XXDG": "XDG"}
D = Decimal


@pytest.fixture(autouse=True)
def fresh_log():
    log.reset()
    yield
    log.reset()


def rows(*built):
    """Rows as classify gets them: the API dict plus "id"."""
    return [{**raw, "id": ledger_id} for ledger_id, raw in built]


def run(*built, edge=FAR):
    return classify(rows(*built), ALT, edge=edge)


# --- Rule 3: trades by refid ---------------------------------------------------

def test_fiat_buy_is_fixed_buy_with_fee():
    cash, moves, held = run(
        ledger_row("L1", "R1", T, "trade", asset="ZEUR", amount="-100.0", fee="0.26"),
        ledger_row("L2", "R1", T, "trade", asset="XXBT", amount="0.002"),
    )
    assert cash == []
    assert held == []
    assert moves == [Move(id="L2", coin="BTC", time=T, kind="buy", quantity=D("0.002"), pricing="fixed",
                          fiat="EUR", fiat_amount=D("100.0"), fiat_fee=D("0.26"))]


def test_fiat_sell_is_fixed_sell():
    cash, moves, _ = run(
        ledger_row("L1", "R1", T, "trade", asset="XXBT", amount="-0.01", fee="0.00001"),
        ledger_row("L2", "R1", T, "trade", asset="ZEUR", amount="350.0", fee="0.91"),
    )
    assert cash == []
    assert moves == [Move(id="L1", coin="BTC", time=T, kind="sell", quantity=D("0.01001"), pricing="fixed",
                          fiat="EUR", fiat_amount=D("350.0"), fiat_fee=D("0.91"))]


def test_fee_on_receiving_leg_reduces_quantity():
    _, moves, _ = run(
        ledger_row("L1", "R1", T, "trade", asset="ZEUR", amount="-50"),
        ledger_row("L2", "R1", T, "trade", asset="XXBT", amount="0.001", fee="0.000002"),
    )
    assert moves == [Move(id="L2", coin="BTC", time=T, kind="buy", quantity=D("0.000998"), pricing="fixed",
                          fiat="EUR", fiat_amount=D("50"), fiat_fee=D("0"))]


def test_swap_gives_avg_sell_and_carry_buy_sharing_group():
    cash, moves, _ = run(
        ledger_row("L1", "R1", T, "trade", asset="XETH", amount="-1", fee="0.0026"),
        ledger_row("L2", "R1", T, "trade", asset="DOT", amount="100"),
    )
    assert cash == []
    assert moves == [
        Move(id="L1", coin="ETH", time=T, kind="sell", quantity=D("1.0026"), pricing="avg", group="R1"),
        Move(id="L2", coin="DOT", time=T, kind="buy", quantity=D("100"), pricing="carry", group="R1"),
    ]


def test_card_purchase_deposit_spend_receive():
    cash, moves, _ = run(
        ledger_row("L1", "R1", T, "deposit", asset="ZEUR", amount="20"),
        ledger_row("L2", "R1", T + 1, "spend", asset="ZEUR", amount="-20", fee="0.3"),
        ledger_row("L3", "R1", T + 1, "receive", asset="XETH", amount="0.01"),
    )
    assert cash == [Cash("kraken:L1", "EUR", DAY, D("20"), "credit", "Deposit")]
    assert moves == [Move(id="L3", coin="ETH", time=T + 1, kind="buy", quantity=D("0.01"), pricing="fixed",
                          fiat="EUR", fiat_amount=D("20"), fiat_fee=D("0.3"))]


def test_dust_sweep_several_outs_one_in():
    cash, moves, _ = run(
        ledger_row("L1", "R1", T, "conversion", asset="XXDG", amount="-10"),
        ledger_row("L2", "R1", T, "conversion", asset="ADA", amount="-3"),
        ledger_row("L3", "R1", T, "conversion", asset="XETH", amount="0.001"),
    )
    assert cash == []
    assert moves == [
        Move(id="L1", coin="DOGE", time=T, kind="sell", quantity=D("10"), pricing="avg", group="R1"),
        Move(id="L2", coin="ADA", time=T, kind="sell", quantity=D("3"), pricing="avg", group="R1"),
        Move(id="L3", coin="ETH", time=T, kind="buy", quantity=D("0.001"), pricing="carry", group="R1"),
    ]


def test_several_coins_in_are_split_by_market_value():
    _, moves, _ = run(
        ledger_row("L1", "R1", T, "trade", asset="ZEUR", amount="-30", fee="0.1"),
        ledger_row("L2", "R1", T, "trade", asset="XXBT", amount="0.0001"),
        ledger_row("L3", "R1", T, "trade", asset="XETH", amount="0.002"),
        ledger_row("L4", "R2", T + 5, "trade", asset="ADA", amount="-4"),
        ledger_row("L5", "R2", T + 5, "trade", asset="DOT", amount="1"),
        ledger_row("L6", "R2", T + 5, "trade", asset="XETH", amount="0.0005"),
        ledger_row("L7", "R3", T + 9, "trade", asset="ADA", amount="-2"),
        ledger_row("L8", "R3", T + 9, "trade", asset="DOT", amount="-1"),
        ledger_row("L9", "R3", T + 9, "trade", asset="ZEUR", amount="12", fee="0.03"),
    )
    assert moves == [
        Move(id="L2", coin="BTC", time=T, kind="buy", quantity=D("0.0001"), pricing="fixed",
             fiat="EUR", fiat_amount=D("30"), fiat_fee=D("0.1"), group="R1", weight_market=True),
        Move(id="L3", coin="ETH", time=T, kind="buy", quantity=D("0.002"), pricing="fixed",
             fiat="EUR", fiat_amount=D("30"), fiat_fee=D("0.1"), group="R1", weight_market=True),
        Move(id="L4", coin="ADA", time=T + 5, kind="sell", quantity=D("4"), pricing="avg", group="R2"),
        Move(id="L5", coin="DOT", time=T + 5, kind="buy", quantity=D("1"), pricing="carry", group="R2",
             weight_market=True),
        Move(id="L6", coin="ETH", time=T + 5, kind="buy", quantity=D("0.0005"), pricing="carry", group="R2",
             weight_market=True),
        Move(id="L7", coin="ADA", time=T + 9, kind="sell", quantity=D("2"), pricing="fixed",
             fiat="EUR", fiat_amount=D("12"), fiat_fee=D("0.03"), group="R3", weight_market=True),
        Move(id="L8", coin="DOT", time=T + 9, kind="sell", quantity=D("1"), pricing="fixed",
             fiat="EUR", fiat_amount=D("12"), fiat_fee=D("0.03"), group="R3", weight_market=True),
    ]


def test_several_legs_of_one_coin_net_and_take_largest_leg_id():
    _, moves, _ = run(
        ledger_row("L1", "R1", T, "trade", asset="ZEUR", amount="-60"),
        ledger_row("L2", "R1", T, "trade", asset="XXBT", amount="0.001"),
        ledger_row("L3", "R1", T, "trade", asset="XXBT", amount="0.003"),
    )
    assert moves == [Move(id="L3", coin="BTC", time=T, kind="buy", quantity=D("0.004"), pricing="fixed",
                          fiat="EUR", fiat_amount=D("60"), fiat_fee=D("0"))]


def test_unusual_trade_group_falls_back_and_warns(capsys):
    cash, moves, _ = run(
        ledger_row("L1", "R1", T, "trade", asset="XETH", amount="-1"),
        ledger_row("L2", "R1", T, "trade", asset="DOT", amount="50"),
        ledger_row("L3", "R1", T, "trade", asset="ZEUR", amount="7", fee="0.01"),
    )
    assert moves == [
        Move(id="L1", coin="ETH", time=T, kind="sell", quantity=D("1"), pricing="avg"),
        Move(id="L2", coin="DOT", time=T, kind="buy", quantity=D("50"), pricing="market"),
    ]
    assert cash == [
        Cash("kraken:L3", "EUR", DAY, D("7"), "credit", "Trade"),
        Cash("kraken:L3:fee", "EUR", DAY, D("0.01"), "debit", "Kraken fee"),
    ]
    assert "kraken: unusual trade group" in capsys.readouterr().err


# --- Rule 1: internal ------------------------------------------------------------

def test_earn_rows_with_different_refids_are_internal():
    cash, moves, held = run(
        ledger_row("L1", "RA", T, "transfer", "spottostaking", asset="XETH", amount="-1"),
        ledger_row("L2", "RB", T + 60, "transfer", "stakingfromspot", asset="ETH2.S", amount="1"),
        edge=T,
    )
    assert (cash, moves, held) == ([], [], [])


def test_legacy_staking_unstake_pair_is_internal():
    cash, moves, _ = run(
        ledger_row("L1", "RA", T, "transfer", "stakingtospot", asset="DOT.S", amount="-5"),
        ledger_row("L2", "RB", T, "transfer", "spotfromstaking", asset="DOT", amount="5"),
    )
    assert (cash, moves) == ([], [])


def test_earn_allocation_and_migration_internal_and_unknown_subtype_logged(capsys):
    cash, moves, _ = run(
        ledger_row("L1", "RA", T, "earn", "allocation", asset="XETH", amount="-2"),
        ledger_row("L2", "RB", T, "earn", "allocation", asset="ETH.B", amount="2"),
        ledger_row("L3", "RC", T, "earn", "migration", asset="DOT.S", amount="-3"),
        ledger_row("L4", "RD", T, "earn", "migration", asset="DOT.B", amount="3"),
        ledger_row("L5", "RE", T, "earn", "deallocation", asset="EUR.M", amount="-10"),
        ledger_row("L6", "RF", T, "earn", "autoallocate", asset="ZEUR", amount="10"),
        ledger_row("L7", "RG", T, "earn", "brandnewthing", asset="ADA", amount="4"),
    )
    assert (cash, moves) == ([], [])
    err = capsys.readouterr().err
    assert "BRANDNEWTHING" in err
    assert "ALLOCATION" not in err and "MIGRATION" not in err


def test_negative_staking_is_internal():
    cash, moves, _ = run(ledger_row("L1", "R1", T, "staking", asset="ETH2", amount="-0.5"))
    assert (cash, moves) == ([], [])


def test_mirrored_pair_in_one_refid_is_internal():
    # Empty-subtype transfers would be airdrops, but a mirrored pair cancels out.
    cash, moves, _ = run(
        ledger_row("L1", "R1", T, "transfer", asset="XETH", amount="-1.5"),
        ledger_row("L2", "R1", T, "transfer", asset="ETH2.S", amount="1.5"),
    )
    assert (cash, moves) == ([], [])


def test_fee_on_internal_row_is_avg_sell_or_fee_debit():
    cash, moves, _ = run(
        ledger_row("L1", "R1", T, "transfer", "spottostaking", asset="DOT", amount="-5", fee="0.01"),
        ledger_row("L2", "R2", T, "earn", "allocation", asset="ZEUR", amount="-10", fee="0.05"),
    )
    assert moves == [Move(id="L1", coin="DOT", time=T, kind="sell", quantity=D("0.01"), pricing="avg")]
    assert cash == [Cash("kraken:L2:fee", "EUR", DAY, D("0.05"), "debit", "Kraken fee")]


# --- Rule 2: rewards -------------------------------------------------------------

def test_rewards_are_zero_cost():
    _, moves, _ = run(
        ledger_row("L1", "R1", T, "staking", asset="DOT.S", amount="0.1"),
        ledger_row("L2", "R2", T + 1, "earn", "reward", asset="ETH.B", amount="0.01"),
        ledger_row("L3", "R3", T + 2, "reward", asset="ADA", amount="1"),
        ledger_row("L4", "R4", T + 3, "dividend", asset="XXBT", amount="0.0001"),
        ledger_row("L5", "R5", T + 4, "invite bonus", asset="XXDG", amount="25"),
        ledger_row("L6", "R6", T + 5, "transfer", "", asset="FLOWH", amount="7"),
        ledger_row("L7", "R7", T + 6, "staking", asset="ETH2.S", amount="0.02", fee="0.002"),
    )
    expected = [("L1", "DOT", "0.1"), ("L2", "ETH", "0.01"), ("L3", "ADA", "1"), ("L4", "BTC", "0.0001"),
                ("L5", "DOGE", "25"), ("L6", "FLOW", "7"), ("L7", "ETH", "0.018")]
    assert moves == [Move(id=i, coin=c, time=T + n, kind="buy", quantity=D(q), pricing="zero", reward=True)
                     for n, (i, c, q) in enumerate(expected)]


# --- Rules 4 and 5: deposits, withdrawals, by sign ---------------------------------

def test_crypto_deposit_market_and_withdrawal_avg_with_fee():
    cash, moves, _ = run(
        ledger_row("L1", "R1", T, "deposit", asset="XXBT", amount="0.5"),
        ledger_row("L2", "R2", T + 1, "withdrawal", asset="XXBT", amount="-0.2", fee="0.0005"),
    )
    assert cash == []
    assert moves == [
        Move(id="L1", coin="BTC", time=T, kind="buy", quantity=D("0.5"), pricing="market"),
        Move(id="L2", coin="BTC", time=T + 1, kind="sell", quantity=D("0.2005"), pricing="avg"),
    ]


def test_futures_transfer_in_market_out_avg():
    cash, moves, _ = run(
        ledger_row("L1", "R1", T, "transfer", "spotfromfutures", asset="XXBT", amount="0.1"),
        ledger_row("L2", "R2", T + 1, "transfer", "spottofutures", asset="XXBT", amount="-0.05"),
        ledger_row("L3", "R3", T + 2, "transfer", "spottofutures", asset="ZEUR", amount="-40"),
    )
    assert moves == [
        Move(id="L1", coin="BTC", time=T, kind="buy", quantity=D("0.1"), pricing="market"),
        Move(id="L2", coin="BTC", time=T + 1, kind="sell", quantity=D("0.05"), pricing="avg"),
    ]
    assert cash == [Cash("kraken:L3", "EUR", DAY, D("40"), "debit", "Transfer")]


def test_fiat_deposit_withdrawal_fee_and_rewards_cash():
    cash, moves, _ = run(
        ledger_row("L1", "R1", T, "deposit", asset="ZEUR", amount="1000"),
        ledger_row("L2", "R2", T + 1, "withdrawal", asset="ZEUR", amount="-200", fee="0.09"),
        ledger_row("L3", "R3", T + 2, "staking", asset="ZUSD", amount="1.5"),
        ledger_row("L4", "R4", T + 3, "earn", "reward", asset="EUR.M", amount="0.3"),
        ledger_row("L5", "R5", T + 4, "rollover", asset="ZEUR", amount="0", fee="0.02"),
        ledger_row("L6", "R6", T + 5, "margin", asset="ZEUR", amount="-5"),
    )
    assert moves == []
    assert cash == [
        Cash("kraken:L1", "EUR", DAY, D("1000"), "credit", "Deposit"),
        Cash("kraken:L2", "EUR", DAY, D("200"), "debit", "Withdrawal"),
        Cash("kraken:L2:fee", "EUR", DAY, D("0.09"), "debit", "Kraken fee"),
        Cash("kraken:L3", "USD", DAY, D("1.5"), "credit", "Kraken rewards"),
        Cash("kraken:L4", "EUR", DAY, D("0.3"), "credit", "Kraken rewards"),
        Cash("kraken:L5:fee", "EUR", DAY, D("0.02"), "debit", "Kraken fee"),
        Cash("kraken:L6", "EUR", DAY, D("5"), "debit", "Margin"),
    ]


def test_fiat_to_fiat_conversion_two_cash_transactions():
    cash, moves, _ = run(
        ledger_row("L1", "R1", T, "trade", asset="ZEUR", amount="-100", fee="0.2"),
        ledger_row("L2", "R1", T, "trade", asset="ZUSD", amount="108"),
    )
    assert moves == []
    assert cash == [
        Cash("kraken:L1", "EUR", DAY, D("100"), "debit", "Conversion EUR → USD"),
        Cash("kraken:L1:fee", "EUR", DAY, D("0.2"), "debit", "Kraken fee"),
        Cash("kraken:L2", "USD", DAY, D("108"), "credit", "Conversion EUR → USD"),
    ]


# --- Incomplete groups -----------------------------------------------------------

def test_lone_leg_at_edge_held_back_and_final_when_older(capsys):
    built = [
        ledger_row("L1", "R1", T - 7200, "receive", asset="XETH", amount="1"),
        ledger_row("L2", "R2", T - 7100, "spend", asset="ADA", amount="-5"),
        ledger_row("L3", "R3", T, "receive", asset="XXBT", amount="0.1"),
        ledger_row("L4", "R4", T + 10, "deposit", asset="ZEUR", amount="15"),
        ledger_row("L5", "R4", T + 10, "spend", asset="ZEUR", amount="-15"),
    ]
    cash, moves, held = run(*built, edge=T)
    assert moves == [
        Move(id="L1", coin="ETH", time=T - 7200, kind="buy", quantity=D("1"), pricing="market"),
        Move(id="L2", coin="ADA", time=T - 7100, kind="sell", quantity=D("5"), pricing="avg"),
    ]
    assert cash == []  # the deposit waits with its refid
    assert held == rows(*built[2:])
    assert "kraken: lone trade leg" in capsys.readouterr().err

    # Once older than the edge, the lone legs are final.
    cash, moves, held = run(*built, edge=T + 3600)
    assert held == []
    assert Move(id="L3", coin="BTC", time=T, kind="buy", quantity=D("0.1"), pricing="market") in moves
    assert cash == [
        Cash("kraken:L4", "EUR", DAY, D("15"), "credit", "Deposit"),
        Cash("kraken:L5", "EUR", DAY, D("15"), "debit", "Spend"),
    ]


# --- Rule 6 and unknowns -------------------------------------------------------

def test_kfee_ignored():
    cash, moves, held = run(
        ledger_row("L1", "R1", T, "trade", asset="ZEUR", amount="-10"),
        ledger_row("L2", "R1", T, "trade", asset="XXBT", amount="0.0002"),
        ledger_row("L3", "R1", T, "trade", asset="KFEE", amount="0", fee="2.5"),
        ledger_row("L4", "R2", T, "deposit", asset="KFEE", amount="100"),
        edge=T,
    )
    assert cash == []
    assert held == []
    assert moves == [Move(id="L2", coin="BTC", time=T, kind="buy", quantity=D("0.0002"), pricing="fixed",
                          fiat="EUR", fiat_amount=D("10"), fiat_fee=D("0"))]


def test_unknown_type_by_sign_and_logged_by_name_only(capsys):
    cash, moves, _ = run(
        ledger_row("LQX7A", "RQX7A", T, "mysterytype", asset="XXBT", amount="0.123"),
        ledger_row("LQX7B", "RQX7B", T, "mysterytype", asset="ZEUR", amount="-45.67"),
    )
    assert moves == [Move(id="LQX7A", coin="BTC", time=T, kind="buy", quantity=D("0.123"), pricing="market")]
    assert cash == [Cash("kraken:LQX7B", "EUR", DAY, D("45.67"), "debit", "Mysterytype")]
    err = capsys.readouterr().err
    assert "MYSTERYTYPE" in err
    for leak in ("123", "45", "67", "LQX7", "RQX7", "BTC", "XXBT", "EUR"):
        assert leak not in err


# --- Shape of the output -----------------------------------------------------------

def test_output_sorted_by_time_then_id_with_utc_dates_and_decimal_inputs():
    late = 1_700_006_400.0  # 2023-11-15 00:00:00 UTC
    cash, moves, _ = classify([
        {**ledger_row("L9", "R9", late, "deposit", asset="ZEUR", amount="5")[1], "id": "L9", "time": D(str(late))},
        {**ledger_row("L2", "R2", T, "deposit", asset="XXBT", amount=D("0.3"))[1], "id": "L2"},
        {**ledger_row("L1", "R1", T, "deposit", asset="XETH", amount="2")[1], "id": "L1"},
        {**ledger_row("L0", "R0", T - 1, "deposit", asset="ZEUR", amount="1")[1], "id": "L0"},
    ], ALT, edge=FAR)
    assert [m.id for m in moves] == ["L1", "L2"]
    assert all(isinstance(m.time, float) for m in moves)
    assert cash == [
        Cash("kraken:L0", "EUR", DAY, D("1"), "credit", "Deposit"),
        Cash("kraken:L9", "EUR", date(2023, 11, 15), D("5"), "credit", "Deposit"),
    ]


def test_compact_roundtrip():
    moves = [
        Move(id="L1", coin="BTC", time=T + 0.5, kind="buy", quantity=D("0.00000123"), pricing="fixed",
             fiat="EUR", fiat_amount=D("100.25"), fiat_fee=D("0.26"), weight_market=True),
        Move(id="L2", coin="ETH", time=T, kind="sell", quantity=D("1.0026"), pricing="avg", group="R1"),
        Move(id="L3", coin="DOT", time=T, kind="buy", quantity=D("0.1"), pricing="zero", reward=True),
    ]
    for move in moves:
        item = json.loads(json.dumps(compact(move)))
        assert isinstance(item, list)
        assert expand(item) == move


def test_trade_group_never_carries_a_negative_fee_or_amount():
    _, buys, _ = run(
        ledger_row("L1", "R1", T, "trade", asset="ZEUR", amount="-100.0", fee="-0.26"),  # a fee rebate
        ledger_row("L2", "R1", T, "trade", asset="XXBT", amount="0.002"),
        ledger_row("L3", "R2", T + 1, "trade", asset="ZEUR", amount="1.0", fee="5.0"),  # net -4, amount +1
        ledger_row("L4", "R2", T + 1, "trade", asset="XETH", amount="0.01"),
    )
    _, sells, _ = run(
        ledger_row("L5", "R5", T, "trade", asset="XXBT", amount="-0.002"),
        ledger_row("L6", "R5", T, "trade", asset="ZEUR", amount="100.0", fee="-0.26"),
        ledger_row("L7", "R7", T + 1, "trade", asset="XETH", amount="-0.01"),
        ledger_row("L8", "R7", T + 1, "trade", asset="ZEUR", amount="-1.0", fee="-5.0"),  # net +4, amount -1
    )
    assert [(m.id, m.fiat_amount, m.fiat_fee) for m in buys + sells] == [
        ("L2", D("100.0"), D(0)), ("L4", D(0), D("5.0")), ("L5", D("100.0"), D(0)), ("L7", D(0), D(0))]
