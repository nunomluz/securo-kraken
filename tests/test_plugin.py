import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from securo_kraken import client as client_module
from securo_kraken import log, payload
from securo_kraken import plugin as plugin_module
from securo_kraken.errors import NeedsAuth, Transient
from securo_kraken.plugin import Kraken, plugin
from tests import securo_replay
from tests.factories import API_KEY, API_SECRET, FakeKraken, ledger_row

D = Decimal
DAY = 86400
WALL = 1_764_000_000.0  # 2025-11-24 16:00 UTC
TODAY = date(2025, 11, 24)
T0 = WALL - 40 * DAY  # 2025-10-15 16:00 UTC
TICKER = {"XXBTZEUR": "60000", "XETHZEUR": "3500", "ZEURZUSD": "1.1"}
HISTORY = {"XXBTZEUR": "50000", "XETHZEUR": "3000", "ZEURZUSD": "1.25"}


@pytest.fixture(autouse=True)
def fresh_log():
    log.reset()


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def ctx(currency=None):
    config = {} if currency is None else {"currency": currency}
    return SimpleNamespace(secrets={"api_key": API_KEY, "api_secret": API_SECRET}, config=config)


def run(kraken, state=None, currency=None, wall=WALL):
    clock = Clock()
    p = Kraken(http=kraken, clock=clock, wall=lambda: wall, sleep=clock.sleep, today=lambda: TODAY)
    return p.sync({"state": state}, ctx(currency))


def by_id(records):
    return {record["external_id"]: record for record in records}


def holdings(result):
    return by_id(result["data"]["holdings"])


def accounts(result):
    return by_id(result["data"]["accounts"])


def as_trades(records):
    return [SimpleNamespace(id=r["external_id"], date=date.fromisoformat(r["date"]), kind=r["kind"],
                            quantity=D(r["quantity"]), price=D(r["price"]), fee=D(r["fee"])) for r in records]


def ledger_of(*results, coin):
    """The coin's trades as Securo keeps them across syncs: overlaid by external id, reported wins."""
    kept = {}
    for result in results:
        record = by_id(result["data"].get("holdings") or []).get(f"kraken:{coin}") or {}
        kept.update((trade["external_id"], trade) for trade in record.get("trades") or [])
    return as_trades(kept.values())


def assert_reconciles(trades, quantity):
    """Securo keeps the set (it replays to the quantity, never below zero) and the sells realize nothing."""
    assert securo_replay.reconciles(trades, D(quantity))
    assert securo_replay.replay(trades)["low"] >= 0
    assert abs(securo_replay.replay(trades)["realized"]) <= D("0.01")


def buy(prefix, time, coin, quantity, fiat_amount, fee="0", fiat="ZEUR"):
    return [ledger_row(f"{prefix}A", f"R{prefix}", time, "trade", asset=fiat, amount=f"-{fiat_amount}", fee=fee),
            ledger_row(f"{prefix}B", f"R{prefix}", time, "trade", asset=coin, amount=quantity)]


def base_rows():
    return [
        ledger_row("LDEP", "RDEP", T0, "deposit", asset="ZEUR", amount="1000.0000"),
        *buy("LBTC", T0 + 100, "XXBT", "0.0100000000", "500.0000", fee="1.3000"),
        *buy("LETH", T0 + 200, "XETH", "0.1000000000", "300.0000", fee="0.7800"),
        ledger_row("LALL1", "RALL1", T0 + 300, "earn", "allocation", asset="XETH", amount="-0.0500000000"),
        ledger_row("LALL2", "RALL2", T0 + 301, "earn", "allocation", asset="ETH.B", amount="0.0500000000"),
        ledger_row("LREW1", "RREW1", T0 + 2 * DAY, "earn", "reward", asset="ETH.B", amount="0.0010000000"),
        ledger_row("LREW2", "RREW2", T0 + 3 * DAY, "staking", asset="ETH.B", amount="0.0020000000"),
    ]


BASE_BALANCES = {"ZEUR": "197.9200", "XXBT": "0.0100000000", "XETH": "0.0500000000", "ETH.B": "0.0530000000"}


def base_kraken(**over):
    args = dict(balances=BASE_BALANCES, rows=base_rows(), ticker=TICKER, history=HISTORY)
    args.update(over)
    return FakeKraken(**args)


def test_module_exposes_plugin():
    assert isinstance(plugin, Kraken)


def test_first_sync_full_history_sends_accounts_holdings_trades():
    kraken = base_kraken()
    result = run(kraken)
    data = result["data"]
    assert data["contract"] == 1
    eur = accounts(result)["kraken:EUR"]
    assert eur["balance"] == "197.92" and eur["name"] == "Kraken EUR" and eur["type"] == "investment"
    assert eur["transactions"] == [{"external_id": "kraken:LDEP", "description": "Deposit", "amount": "1000",
                                    "date": "2025-10-15", "type": "credit", "currency": "EUR", "payee": "Kraken"}]
    found = holdings(result)
    assert set(found) == {"kraken:BTC", "kraken:ETH"}
    btc, eth = found["kraken:BTC"], found["kraken:ETH"]
    assert btc["trades"] == [{"external_id": "kraken:LBTCB", "kind": "buy", "date": "2025-10-15",
                              "quantity": "0.01", "price": "50000", "fee": "1.3"}]
    assert (btc["quantity"], btc["current_value"], btc["unit_price"], btc["purchase_price"]) == (
        "0.01", "600", "60000", "501.3")
    assert btc["currency"] == "EUR" and btc["account_name"] == "Kraken" and btc["type"] == "crypto"
    assert sorted(t["external_id"] for t in eth["trades"]) == ["kraken:LETHB", "kraken:reward:ETH:2025-10"]
    assert (eth["quantity"], eth["current_value"], eth["purchase_price"]) == ("0.103", "360.5", "300.78")
    for record in (btc, eth):
        trades = as_trades(record["trades"])
        assert securo_replay.reconciles(trades, D(record["quantity"]))
        assert securo_replay.replay(trades)["cost"] == D(record["purchase_price"])
    assert kraken.count("Trades") == 0  # bought with the valuation currency: no history needed
    assert result["state"]["v"] == 1 and result["state"]["valuation"] == "EUR"
    json.dumps(result)


def test_backfill_interrupted_sends_cash_and_no_holdings_key():
    deposits = [ledger_row(f"LD{i:03d}", f"RD{i:03d}", T0 + DAY + 60 * i, "deposit", amount="10") for i in range(70)]
    kraken = base_kraken(rows=[*buy("LBTC", T0, "XXBT", "0.01", "500"), *deposits],
                         balances={"ZEUR": "200", "XXBT": "0.01"}, limit={"Ledgers": 1})
    first = run(kraken)
    assert "holdings" not in first["data"]
    sent_first = {t["external_id"] for t in accounts(first)["kraken:EUR"]["transactions"]}
    assert len(sent_first) == 49  # the row at the page's oldest time waits: its group may go on
    assert [row["id"] for row in first["state"]["held"]] == ["LD020"]
    assert first["state"]["ledger"]["cursor"] is not None

    kraken.limit = {}
    second = run(kraken, first["state"], wall=WALL + 3600)
    sent_second = {t["external_id"] for t in accounts(second)["kraken:EUR"]["transactions"]}
    assert sent_first.isdisjoint(sent_second)
    assert sent_first | sent_second == {f"kraken:LD{i:03d}" for i in range(70)}
    btc = holdings(second)["kraken:BTC"]
    assert [t["external_id"] for t in btc["trades"]] == ["kraken:LBTCB"]
    assert_reconciles(ledger_of(first, second, coin="BTC"), btc["quantity"])


def test_group_cut_by_an_interrupted_walk_is_kept_whole():
    """A dust sweep (two coins out, one in) whose legs straddle where the walk stopped."""
    filler = [ledger_row(f"LD{i:03d}", f"RD{i:03d}", T0 + DAY + 60 * i, "deposit", amount="10") for i in range(48)]
    sweep = [ledger_row("LSW1", "RSW", T0, "trade", asset="XETH", amount="-0.1"),
             ledger_row("LSW2", "RSW", T0, "trade", asset="SOL", amount="-1"),
             ledger_row("LSW3", "RSW", T0, "trade", asset="XXBT", amount="0.01")]
    kraken = base_kraken(rows=[*buy("LETH", T0 - DAY, "XETH", "0.1", "300"), *buy("LSOL", T0 - DAY, "SOL", "1", "100"),
                               *filler, *sweep],
                         balances={"ZEUR": "80", "XXBT": "0.01"}, limit={"Ledgers": 1})
    first = run(kraken)  # the first page ends inside the sweep: LSW1 and LSW2 read, LSW3 not
    assert {row["id"] for row in first["state"]["held"]} == {"LSW1", "LSW2"}
    kraken.limit = {}
    second = run(kraken, first["state"], wall=WALL + 3600)
    btc = holdings(second)["kraken:BTC"]
    assert btc["trades"] == [{"external_id": "kraken:LSW3", "kind": "buy", "date": "2025-10-15",
                              "quantity": "0.01", "price": "40000", "fee": "0"}]  # 300 + 100 carried over
    for coin, quantity in (("BTC", "0.01"), ("ETH", "0"), ("SOL", "0")):
        assert_reconciles(ledger_of(second, coin=coin), quantity)


def test_second_sync_sends_only_new_or_changed_trades():
    kraken = base_kraken()
    first = run(kraken)
    kraken.add(ledger_row("LDEP2", "RDEP2", T0 + 9 * DAY, "deposit", asset="ZEUR", amount="600.0000"),
               *buy("LNEW", T0 + 10 * DAY, "XXBT", "0.0100000000", "550.0000"))
    kraken.balances = {**BASE_BALANCES, "ZEUR": "247.9200", "XXBT": "0.0200000000"}
    second = run(kraken, first["state"], wall=WALL + 3600)
    found = holdings(second)
    assert found["kraken:BTC"]["trades"] == [{"external_id": "kraken:LNEWB", "kind": "buy", "date": "2025-10-25",
                                              "quantity": "0.01", "price": "55000", "fee": "0"}]
    assert found["kraken:ETH"]["trades"] is None
    assert_reconciles(ledger_of(first, second, coin="BTC"), "0.02")
    eur = accounts(second)["kraken:EUR"]
    assert eur["balance"] == "247.92"
    assert [t["external_id"] for t in eur["transactions"]] == ["kraken:LDEP2"]
    third = run(kraken, second["state"], wall=WALL + 7200)
    assert all(h["trades"] is None for h in third["data"]["holdings"])


def test_split_leg_across_syncs_becomes_one_buy():
    spend, receive = buy("LSPL", WALL - 600, "XXBT", "0.0100000000", "500.0000", fee="1.3000")
    kraken = base_kraken(rows=[ledger_row("LDEP", "RDEP", T0, "deposit", amount="1000"), spend],
                         balances={"ZEUR": "498.7"})
    first = run(kraken)
    assert "holdings" not in first["data"]  # one leg read so far: it waits, nothing is guessed
    assert [t["external_id"] for t in accounts(first)["kraken:EUR"]["transactions"]] == ["kraken:LDEP"]
    assert [row["id"] for row in first["state"]["held"]] == ["LSPLA"]
    json.dumps(first["state"])

    kraken.add(receive)
    kraken.balances = {"ZEUR": "498.7", "XXBT": "0.01"}
    second = run(kraken, first["state"], wall=WALL + 3600)
    assert holdings(second)["kraken:BTC"]["trades"] == [
        {"external_id": "kraken:LSPLB", "kind": "buy", "date": "2025-11-24",
         "quantity": "0.01", "price": "50000", "fee": "1.3"}]
    assert accounts(second)["kraken:EUR"]["transactions"] == []
    assert second["state"]["held"] == []
    assert kraken.count("Trades") == 0  # no market-priced guess for the lone leg
    assert_reconciles(ledger_of(first, second, coin="BTC"), "0.01")


def test_lone_leg_older_than_an_hour_is_final():
    spend = buy("LSPL", WALL - 600, "XXBT", "0.01", "500")[0]
    kraken = base_kraken(rows=[ledger_row("LDEP", "RDEP", T0, "deposit", amount="1000"), spend],
                         balances={"ZEUR": "500"})
    first = run(kraken)
    assert first["state"]["held"] and "holdings" not in first["data"]
    later = run(kraken, first["state"], wall=WALL + 7200)
    assert later["state"]["held"] == [] and later["data"]["holdings"] == []
    assert [t["external_id"] for t in accounts(later)["kraken:EUR"]["transactions"]] == ["kraken:LSPLA"]


def test_sold_out_coin_listed_every_sync():
    kraken = base_kraken(rows=buy("LBTC", T0, "XXBT", "0.0100000000", "500.0000", fee="1.3000"),
                         balances={"XXBT": "0.01"})
    first = run(kraken)
    kraken.add(ledger_row("LOUT", "ROUT", T0 + DAY, "withdrawal", asset="XXBT", amount="-0.0095", fee="0.0005"))
    kraken.balances = {}
    second = run(kraken, first["state"], wall=WALL + 3600)
    btc = holdings(second)["kraken:BTC"]
    assert (btc["quantity"], btc["current_value"], btc["purchase_price"]) == ("0", "0", "0")
    assert btc["trades"] == [{"external_id": "kraken:LOUT", "kind": "sell", "date": "2025-10-16",
                              "quantity": "0.01", "price": "50130", "fee": "0"}]
    all_trades = ledger_of(first, second, coin="BTC")
    assert_reconciles(all_trades, "0")
    assert securo_replay.replay(all_trades)["realized"] == 0
    third = run(kraken, second["state"], wall=WALL + 7200)
    btc = holdings(third)["kraken:BTC"]
    assert btc["quantity"] == "0" and btc["trades"] is None


def test_valuation_change_needs_auth():
    kraken = base_kraken()
    first = run(kraken)
    with pytest.raises(NeedsAuth) as raised:
        run(kraken, first["state"], currency="USD")
    assert raised.value.code == "currency_changed"
    assert "Add a new connection to value assets in another currency." in str(raised.value)
    with pytest.raises(NeedsAuth) as raised:
        run(FakeKraken(), currency="BTC")
    assert raised.value.code == "currency_invalid"


def test_blank_currency_defaults_to_eur():
    result = run(base_kraken(), currency=" ")
    assert result["state"]["valuation"] == "EUR"
    assert {h["currency"] for h in result["data"]["holdings"]} == {"EUR"}
    assert run(base_kraken(), currency="usd")["state"]["valuation"] == "USD"


def test_missing_history_defers_holdings_until_priced():
    rows = [ledger_row("LDEP", "RDEP", T0, "deposit", asset="ZUSD", amount="1000"),
            *buy("LBTC", T0 + 100, "XXBT", "0.01", "600", fee="1", fiat="ZUSD")]
    kraken = base_kraken(rows=rows, balances={"ZUSD": "399", "XXBT": "0.01"}, fail={"Trades": "EService:Unavailable"})
    first = run(kraken)
    assert "holdings" not in first["data"]
    assert set(accounts(first)) == {"kraken:USD"}
    kraken.fail = {}
    second = run(kraken, first["state"], wall=WALL + 3600)
    assert holdings(second)["kraken:BTC"]["trades"] == [
        {"external_id": "kraken:LBTCB", "kind": "buy", "date": "2025-10-15",
         "quantity": "0.01", "price": "48000", "fee": "0.8"}]  # 600 USD / 1.25, fee 1 USD / 1.25
    assert accounts(second)["kraken:USD"]["balance"] == "399"


def test_rate_limit_on_balances_keeps_state_and_sends_nothing():
    kraken = base_kraken()
    first = run(kraken)
    kraken.fail = {"BalanceEx": "EAPI:Rate limit exceeded"}
    again = run(kraken, first["state"], wall=WALL + 3600)
    assert again == {"state": first["state"], "data": None}


def test_transient_errors_escape():
    with pytest.raises(Transient):
        run(base_kraken(fail={"Ledgers": "EService:Unavailable"}))


def test_names_and_pairs_cached_for_a_day():
    kraken = base_kraken()
    first = run(kraken)
    second = run(kraken, first["state"], wall=WALL + 3600)
    assert kraken.count("Assets") == 1 and kraken.count("AssetPairs") == 1
    kraken.balances = {**BASE_BALANCES, "NEWC": "2"}  # a code first seen: names are refreshed
    third = run(kraken, second["state"], wall=WALL + 7200)
    assert kraken.count("Assets") == 2 and kraken.count("AssetPairs") == 2
    fourth = run(kraken, third["state"], wall=WALL + 3 * 3600)  # still unknown, but no longer new
    assert kraken.count("Assets") == 2
    run(kraken, fourth["state"], wall=WALL + 2 * DAY)  # a day old
    assert kraken.count("Assets") == 3


def test_state_and_data_are_json_serialisable_and_under_16mb_for_10k_rows(monkeypatch):
    """10k trades and 20k hourly rewards in one sync: the reply and the state stay well inside 16 MiB."""
    monkeypatch.setattr(client_module, "COUNTER_MAX", 1e9)  # no pacing waits: the whole ledger in one sync
    rows = []
    for i in range(10_000):
        rows += buy(f"LB{i:05d}", T0 - i * 3600, "XXBT", "0.0010000000", "50.0000", fee="0.1300")
    rows += [ledger_row(f"LR{i:05d}", f"RR{i:05d}", T0 - i * 3600 - 1800, "staking", asset="XBT.M",
                        amount="0.0000010000") for i in range(20_000)]
    kraken = base_kraken(rows=rows, balances={"ZEUR": "1000", "XXBT": "10", "XBT.M": "0.02"})
    result = run(kraken)
    btc = holdings(result)["kraken:BTC"]
    assert len(btc["trades"]) > 10_000  # the buys and one reward trade per month
    assert_reconciles(as_trades(btc["trades"]), btc["quantity"])
    assert len(result["state"]["moves"]) < 10_100  # rewards kept as one move per coin and month
    assert all(len(entry) == 2 for entry in result["state"]["sent"]["BTC"])
    assert len(json.dumps({"ok": True, "result": result})) < 16 * 1024 * 1024


def test_large_state_is_reported(monkeypatch, capsys):
    monkeypatch.setattr(plugin_module, "STATE_WARN_BYTES", 100)
    run(base_kraken())
    assert "plugin state is getting large" in capsys.readouterr().err


def test_micro_priced_coin_is_value_only_with_exact_cost():
    rows = [*buy("LP1", T0, "PEPE", "1000000", "0.33", fee="0.01"),  # 0.00000034 a unit with the fee
            *buy("LP2", T0 + DAY, "PEPE", "2000000", "0.62"),
            ledger_row("LOUT", "ROUT", T0 + 2 * DAY, "withdrawal", asset="PEPE", amount="-1500000")]
    kraken = base_kraken(rows=rows, balances={"PEPE": "1500000"}, ticker={"PEPEEUR": "0.0000003"})
    pepe = holdings(run(kraken))["kraken:PEPE"]
    assert pepe["trades"] is None and pepe["quantity"] == "1500000"
    assert (pepe["purchase_price"], pepe["current_value"]) == ("0.48", "0.45")  # (0.34 + 0.62) / 2


def test_exact_cost_takes_a_same_day_adjustment_after_the_buy():
    rows = buy("LP1", WALL - 2 * 3600, "PEPE", "2000000", "0.62")  # 14:00 on the sync day
    found = []
    for balance in ("0", "1000000", "1500000"):
        kraken = base_kraken(rows=rows, balances={"PEPE": balance}, ticker={"PEPEEUR": "0.0000003"})
        found.append(holdings(run(kraken))["kraken:PEPE"]["purchase_price"])
    assert found == ["0", "0.31", "0.465"]


def test_value_only_is_sticky():
    kraken = base_kraken(rows=[], balances={"SOL": "100"}, ticker={"SOLEUR": "0.005"}, history={"SOLEUR": "0.02"})
    first = run(kraken)
    sol = holdings(first)["kraken:SOL"]
    assert sol["trades"] is None and sol["purchase_price"] == "2" and first["state"]["value_only"] == ["SOL"]
    kraken.ticker = {"SOLEUR": "0.03"}  # back above a cent: still value only, never a half-stored ledger
    sol = holdings(run(kraken, first["state"], wall=WALL + 3600))["kraken:SOL"]
    assert sol["trades"] is None and sol["purchase_price"] == "2" and sol["current_value"] == "3"


def test_coin_with_a_ledger_never_switches_to_value_only():
    kraken = base_kraken()
    first = run(kraken)
    kraken.ticker = {**TICKER, "XXBTZEUR": "0.005"}
    kraken.add(*buy("LNEW", T0 + 10 * DAY, "XXBT", "1.0000000000", "0.0040"))
    kraken.balances = {**BASE_BALANCES, "XXBT": "1.0100000000"}
    second = run(kraken, first["state"], wall=WALL + 3600)
    btc = holdings(second)["kraken:BTC"]
    assert [t["external_id"] for t in btc["trades"]] == ["kraken:LNEWB"]
    assert "BTC" not in second["state"]["value_only"]
    assert_reconciles(ledger_of(first, second, coin="BTC"), "1.01")


def test_size_mode_sends_value_and_exact_cost_until_the_quantity_fits():
    kraken = base_kraken(rows=[], balances={"SOL": "2000000000"}, ticker={"SOLEUR": "0.01"},
                         history={"SOLEUR": "0.01"})
    first = run(kraken)
    sol = holdings(first)["kraken:SOL"]
    assert sol["quantity"] is None and sol["trades"] is None
    assert (sol["purchase_price"], sol["current_value"]) == ("20000000", "20000000")
    kraken.balances = {"SOL": "1000"}
    sol = holdings(run(kraken, first["state"], wall=WALL + 3600))["kraken:SOL"]
    assert sol["quantity"] == "1000" and [t["external_id"] for t in sol["trades"]] == ["kraken:opening:SOL"]


def test_coin_with_too_many_trades_is_sent_value_only(monkeypatch, capsys):
    monkeypatch.setattr(payload, "MAX_TRADES", 1)
    result = run(base_kraken())
    eth, btc = holdings(result)["kraken:ETH"], holdings(result)["kraken:BTC"]
    assert eth["trades"] is None and eth["purchase_price"] == "300.78" and eth["quantity"] == "0.103"
    assert [t["external_id"] for t in btc["trades"]] == ["kraken:LBTCB"]
    assert result["state"]["value_only"] == ["ETH"]
    assert "too many trades for one coin; sent its value only" in capsys.readouterr().err


def test_cost_basis_leaves_out_trades_too_small_to_send():
    rows = [*buy("LBTC", T0, "XXBT", "0.0100000000", "500.0000"),
            *buy("LDUST", T0 + DAY, "XXBT", "0.0000004000", "0.0200", fee="0.5000")]
    btc = holdings(run(base_kraken(rows=rows, balances={"XXBT": "0.0100004000"})))["kraken:BTC"]
    assert [t["external_id"] for t in btc["trades"]] == ["kraken:LBTCB"]
    assert btc["purchase_price"] == "500"
    assert securo_replay.replay(as_trades(btc["trades"]))["cost"] == D(btc["purchase_price"])


def test_coin_without_any_price_is_valued_at_zero_and_logged(capsys):
    result = run(base_kraken(rows=[], balances={"NEWC": "2"}))
    newc = holdings(result)["kraken:NEWC"]
    assert newc["current_value"] == "0" and "unit_price" not in newc
    assert "no price found for a coin; valued at 0" in capsys.readouterr().err


def test_earn_balance_split_in_metadata():
    kraken = base_kraken(rows=[], balances={"XETH": "1.0000000000", "ETH.B": "0.5000000000", "ETH2.S": "0.2500000000"})
    eth = holdings(run(kraken))["kraken:ETH"]
    assert eth["quantity"] == "1.75"
    assert eth["metadata"] == {"XETH": "1", "ETH.B": "0.5", "ETH2.S": "0.25", "price_source": "XETHZEUR"}
    assert eth["trades"] == [{"external_id": "kraken:opening:ETH", "kind": "buy", "date": "2025-11-23",
                              "quantity": "1.75", "price": "3000", "fee": "0"}]


def test_fiat_account_balance_sums_family():
    kraken = base_kraken(rows=[], balances={"ZEUR": "100.0000", "EUR.M": "50.0000", "EUR.HOLD": "25.0000"})
    result = run(kraken)
    assert accounts(result)["kraken:EUR"]["balance"] == "175"
    assert result["data"]["holdings"] == []  # caught up and no coin ever seen


def test_fiat_account_listed_after_balance_reaches_zero():
    kraken = base_kraken(rows=[], balances={"ZUSD": "10.0000"})
    first = run(kraken)
    assert accounts(first)["kraken:USD"]["balance"] == "10"
    kraken.balances = {}
    second = run(kraken, first["state"], wall=WALL + 3600)
    usd = accounts(second)["kraken:USD"]
    assert usd["balance"] == "0" and usd["transactions"] == []
