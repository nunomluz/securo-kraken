import json
from datetime import date
from decimal import Decimal

import pytest

from securo_kraken import log, payload
from securo_kraken.book import Trade
from securo_kraken.classify import Cash

D = Decimal


@pytest.fixture(autouse=True)
def fresh_log():
    log.reset()


def trade(id_, kind="buy", quantity="1", price="100", fee="0", day=date(2026, 1, 5), coin="BTC"):
    return Trade(id=id_, coin=coin, date=day, kind=kind, quantity=D(quantity), price=D(price), fee=D(fee),
                 pricing="fixed", group=None, time=0.0)


def ids(records):
    return [record["external_id"] for record in records]


def test_constants():
    assert payload.CONTRACT == 1 and payload.LOGO == "kraken" and payload.MAX_TRADES == 20_000


def test_diff_new_changed_unchanged():
    first = {"BTC": [trade("kraken:L1"), trade("kraken:L2", price="200")], "ETH": [trade("kraken:L3", coin="ETH")]}
    out, sent = payload.diff(first, {})
    assert ids(out["BTC"]) == ["kraken:L1", "kraken:L2"] and ids(out["ETH"]) == ["kraken:L3"]
    assert out["BTC"][0] == {"external_id": "kraken:L1", "kind": "buy", "date": "2026-01-05",
                             "quantity": "1", "price": "100", "fee": "0"}
    json.dumps(sent)

    second = {"BTC": [trade("kraken:L1"), trade("kraken:L2", price="250"), trade("kraken:L4", "sell")],
              "ETH": [trade("kraken:L3", coin="ETH")]}
    out, sent = payload.diff(second, sent)
    assert ids(out["BTC"]) == ["kraken:L2", "kraken:L4"]  # changed and new only
    assert out["BTC"][0]["price"] == "250"
    assert out["ETH"] is None  # nothing changed: Securo's ledger is left alone

    out, _ = payload.diff(second, sent)
    assert out == {"BTC": None, "ETH": None}


def test_diff_zero_quantity_trades_are_never_sent():
    out, sent = payload.diff({"BTC": [trade("kraken:L1", quantity="0.0000004"), trade("kraken:L2")]}, {})
    assert ids(out["BTC"]) == ["kraken:L2"]


def test_diff_vanished_trade_resent_at_floor():
    """A trade no longer produced is neutralised: a floor buy at no cost, which can't go below zero."""
    first = [trade("kraken:L1", day=date(2026, 1, 3)), trade("kraken:L2", "sell", "0.5", "120", "1.5")]
    _, sent = payload.diff({"BTC": first}, {})
    out, sent = payload.diff({"BTC": [trade("kraken:L1", day=date(2026, 1, 3))]}, sent, today=date(2026, 2, 1))
    assert out["BTC"] == [{"external_id": "kraken:L2", "kind": "buy", "date": "2026-01-03",
                           "quantity": "0.000001", "price": "0", "fee": "0"}]
    out, _ = payload.diff({"BTC": [trade("kraken:L1", day=date(2026, 1, 3))]}, sent)
    assert out["BTC"] is None  # already at the floor: not resent again
    out, _ = payload.diff({"BTC": []}, {"BTC": [["kraken:L9", "x"]]}, today=date(2026, 2, 1))
    assert out["BTC"][0]["date"] == "2026-02-01"  # nothing left to date it by


def test_diff_sent_state_is_compact():
    _, sent = payload.diff({"BTC": [trade("kraken:L1", quantity="0.12345678", price="61234.56789")]}, {})
    (entry,) = sent["BTC"]
    assert entry[0] == "kraken:L1" and len(entry) == 2 and len(entry[1]) <= 12


def test_diff_leaves_other_coins_sent_alone():
    _, sent = payload.diff({"BTC": [trade("kraken:L1")], "PEPE": [trade("kraken:L9", coin="PEPE")]}, {})
    _, after = payload.diff({"BTC": [trade("kraken:L1")]}, sent)
    assert after["PEPE"] == sent["PEPE"]


def test_diff_trade_budget_defers_whole_coins(monkeypatch, capsys):
    monkeypatch.setattr(payload, "MAX_TRADES", 3)
    trades = {
        "ADA": [trade(f"kraken:A{i}", coin="ADA") for i in range(2)],
        "BTC": [trade(f"kraken:B{i}") for i in range(2)],
        "ETH": [trade("kraken:E0", coin="ETH")],
        "XRP": [trade(f"kraken:X{i}", coin="XRP") for i in range(4)],
    }
    out, sent = payload.diff(trades, {})
    assert ids(out["ADA"]) == ["kraken:A0", "kraken:A1"]
    assert out["BTC"] is None and "BTC" not in sent  # would not fit: deferred whole
    assert ids(out["ETH"]) == ["kraken:E0"]
    assert out["XRP"] is None and "XRP" not in sent  # more than one reply can ever hold
    assert "more trades than one sync can send" in capsys.readouterr().err

    out, sent = payload.diff(trades, sent)
    assert out["ADA"] is None and out["ETH"] is None
    assert ids(out["BTC"]) == ["kraken:B0", "kraken:B1"] and "BTC" in sent


def holding(**over):
    args = dict(valuation="EUR", quantity=D("1.5"), value=D("90000"), price=D("60000"), cost=D("45000.123"),
                split={"XXBT": "1", "XBT.M": "0.5"}, trades=[{"external_id": "kraken:L1"}], value_only=False,
                tokenized=False)
    args.update(over)
    return payload.holding("BTC", **args)


def test_holding_fields():
    record = holding(source="XXBTZEUR")
    assert record == {
        "external_id": "kraken:BTC", "name": "BTC", "ticker": "BTC", "type": "crypto", "currency": "EUR",
        "account_name": "Kraken", "quantity": "1.5", "current_value": "90000", "unit_price": "60000",
        "purchase_price": "45000.123", "trades": [{"external_id": "kraken:L1"}],
        "metadata": {"XXBT": "1", "XBT.M": "0.5", "price_source": "XXBTZEUR"},
    }
    assert holding(tokenized=True)["type"] == "stock"
    assert "unit_price" not in holding(price=None, value=D(0))


def test_holding_value_only_by_size_and_by_price(capsys):
    by_price = holding(value_only=True)
    assert by_price["quantity"] == "1.5" and by_price["trades"] is None
    assert by_price["purchase_price"] == "45000.123"

    by_size = holding(quantity=D("1000000000"))
    assert by_size["quantity"] is None and by_size["trades"] is None
    assert by_size["current_value"] == "90000"
    assert holding(quantity=D("999999999.9999994"))["quantity"] == "999999999.999999"
    err = capsys.readouterr().err
    assert err.count("too large") == 1
    assert "1000000000" not in err and "BTC" not in err


def test_holding_value_too_large_is_capped(capsys):
    record = holding(value=D("1234567890.5"))
    assert record["current_value"] == "999999999.999999"
    assert holding(value=D("999999999.999999"))["current_value"] == "999999999.999999"
    err = capsys.readouterr().err
    assert err.count("holding value too large for Securo; capped") == 1 and "1234" not in err


def test_account_fields_and_transactions():
    cash = [Cash("kraken:L1", "EUR", date(2026, 1, 2), D("1000.5"), "credit", "Deposit"),
            Cash("kraken:L1:fee", "EUR", date(2026, 1, 2), D("0.25"), "debit", "Kraken fee"),
            Cash("kraken:L1", "EUR", date(2026, 1, 2), D("1000.5"), "credit", "Deposit")]
    record = payload.account("EUR", D("1200.10"), cash)
    assert {k: v for k, v in record.items() if k != "transactions"} == {
        "external_id": "kraken:EUR", "name": "Kraken EUR", "type": "investment", "balance": "1200.1",
        "currency": "EUR", "institution_name": "Kraken", "institution_logo": "kraken",
    }
    assert record["transactions"] == [
        {"external_id": "kraken:L1", "description": "Deposit", "amount": "1000.5", "date": "2026-01-02",
         "type": "credit", "currency": "EUR", "payee": "Kraken"},
        {"external_id": "kraken:L1:fee", "description": "Kraken fee", "amount": "0.25", "date": "2026-01-02",
         "type": "debit", "currency": "EUR", "payee": "Kraken"},
    ]
    assert payload.account("USD", D(0), [])["transactions"] == []


def test_amounts_are_strings():
    cash = [Cash("kraken:L1", "EUR", date(2026, 1, 2), D("0.123456789"), "credit", "Deposit")]
    record = payload.account("EUR", D("1E+2"), cash)
    out, _ = payload.diff({"BTC": [trade("kraken:L1", quantity="0.12345678", price="1E+3", fee="0.1")]}, {})
    found = [record["balance"], record["transactions"][0]["amount"]]
    found += [out["BTC"][0][key] for key in ("quantity", "price", "fee")]
    found += [holding(value=D("1E+1"), cost=D("2.5E-3"))[key]
              for key in ("quantity", "current_value", "unit_price", "purchase_price")]
    assert all(isinstance(item, str) and "E" not in item and "e" not in item for item in found)
    assert found[:5] == ["100", "0.12345679", "0.123457", "1000", "0.1"]
