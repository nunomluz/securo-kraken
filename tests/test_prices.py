import json
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from securo_kraken import log, prices
from securo_kraken.client import Client
from securo_kraken.errors import BadRequest, OutOfTime, RateLimited, Transient
from tests.factories import API_KEY, API_SECRET, FakeHttp, err, ok

TICKER = "/0/public/Ticker"
TRADES = "/0/public/Trades"
PAIRS = {
    "BTC/EUR": "XXBTZEUR",
    "ETH/EUR": "XETHZEUR",
    "EUR/USD": "ZEURZUSD",
    "ADA/USD": "ADAUSD",
    "AAPLx/USD": "AAPLXUSD",
}
HOUR = 3600
T0 = 1_600_000_000.0


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make(routes):
    clock = Clock()
    http = FakeHttp(routes)
    client = Client(API_KEY, API_SECRET, http=http, clock=clock, wall=lambda: 1_700_000_000.0,
                    sleep=clock.sleep)
    return client, http


def ticker(price):
    return {"a": [price, "1", "1.000"], "b": [price, "1", "1.000"], "c": [price, "0.1"]}


def trade(price, when):
    return [price, "0.01", when, "b", "m", "", 1]


def query(call):
    return parse_qs(urlsplit(call[1]).query)


class Stub:
    """Public-only client stub: replies per pair, remaining drops per call."""

    def __init__(self, replies, remaining=100.0, step=1.0, raises=None):
        self.replies = replies
        self._remaining = remaining
        self.step = step
        self.raises = raises
        self.calls = []

    def remaining(self):
        return self._remaining

    def public(self, method, params=None):
        self.calls.append((method, dict(params or {})))
        if self.raises:
            raise self.raises
        self._remaining -= self.step
        return self.replies[params["pair"]]


def test_current_direct_inverted_and_via_usd():
    client, http = make({TICKER: [ok({
        "XXBTZEUR": ticker("50000.5"),
        "ZEURZUSD": ticker("4"),
        "ADAUSD": ticker("2"),
    })]})
    out = prices.current(client, ["BTC", "USD", "ADA"], "EUR", PAIRS)
    # USD->EUR is the inverse of EUR/USD; ADA goes ADA/USD then 1/(EUR/USD)
    assert out == {"BTC": Decimal("50000.5"), "USD": Decimal("0.25"), "ADA": Decimal("0.5")}
    assert len(http.calls) == 1
    pair_param = query(http.calls[0])["pair"][0].split(",")
    assert sorted(pair_param) == ["ADAUSD", "XXBTZEUR", "ZEURZUSD"]


def test_current_skips_coin_without_route(capsys):
    log.reset()
    client, http = make({TICKER: [ok({"XXBTZEUR": ticker("1")})]})
    out = prices.current(client, ["BTC", "ZZZ", "EUR"], "EUR", PAIRS)
    assert out == {"BTC": Decimal("1"), "EUR": Decimal(1)}
    assert "ZZZ" not in capsys.readouterr().err


def test_current_skips_coin_with_no_answer():
    client, _ = make({TICKER: [ok({})]})
    assert prices.current(client, ["BTC"], "EUR", PAIRS) == {}


def test_current_tokenized_call_carries_only_tokenized_pairs():
    client, http = make({TICKER: [
        ok({"XXBTZEUR": ticker("10"), "ZEURZUSD": ticker("2")}),
        ok({"AAPLXUSD": ticker("200")}),
    ]})
    out = prices.current(client, ["BTC", "AAPLx.T"], "EUR", {**PAIRS, "AAPLx.T/USD": "AAPLXUSD"})
    assert out == {"BTC": Decimal("10"), "AAPLx.T": Decimal("100")}
    assert "asset_class" not in query(http.calls[0])
    assert sorted(query(http.calls[0])["pair"][0].split(",")) == ["XXBTZEUR", "ZEURZUSD"]
    assert query(http.calls[1])["asset_class"] == ["tokenized_asset"]
    assert query(http.calls[1])["pair"] == ["AAPLXUSD"]


def test_current_tokenized_reply_never_overwrites_plain_prices():
    client, _ = make({TICKER: [
        ok({"XXBTZEUR": ticker("10")}),
        ok({"AAPLXEUR": ticker("200"), "XXBTZEUR": ticker("999")}),
    ]})
    out = prices.current(client, ["BTC", "AAPLx.T"], "EUR", {**PAIRS, "AAPLx.T/EUR": "AAPLXEUR"})
    assert out["BTC"] == Decimal("10")


def test_current_ticker_failure_returns_empty_without_raising(capsys):
    log.reset()
    client, _ = make({TICKER: [(500, b"")]})
    assert prices.current(client, ["BTC"], "EUR", PAIRS) == {}
    assert "current prices unavailable" in capsys.readouterr().err


def test_current_failing_tokenized_call_keeps_plain_prices():
    client, _ = make({TICKER: [ok({"XXBTZEUR": ticker("10")}), (500, b"")]})
    out = prices.current(client, ["BTC", "AAPLx.T"], "EUR", {**PAIRS, "AAPLx.T/EUR": "AAPLXEUR"})
    assert out == {"BTC": Decimal("10")}


def test_history_fetch_stops_on_transient_keeping_cache_and_missing(capsys):
    log.reset()
    history = prices.History({})
    history.get("BTC", T0, "EUR", PAIRS)
    history.get("BTC", T0 + HOUR, "EUR", PAIRS)
    history.get("BTC", T0 + 2 * HOUR, "EUR", PAIRS)

    class Flaky(Stub):
        def public(self, method, params=None):
            if len(self.calls) == 1:
                raise Transient("boom")
            self.calls.append((method, params))
            return {"XXBTZEUR": [trade("1", T0)], "last": "1"}

    stub = Flaky({})
    history.fetch(stub, "EUR", PAIRS, deadline=0)
    assert len(history.cache) == 1
    assert len(history.missing) == 2
    assert "historical prices unavailable" in capsys.readouterr().err


def test_history_malformed_or_non_positive_first_trade_is_none():
    for bad in ([["abc", "1", T0]], [["0", "1", T0]], [["-1", "1", T0]], [["5"]], ["x"], [["5", "1", "t"]]):
        history = prices.History({})
        history.get("BTC", T0, "EUR", PAIRS)
        stub = Stub({"XXBTZEUR": {"XXBTZEUR": bad, "last": "1"}})
        history.fetch(stub, "EUR", PAIRS, deadline=0)
        assert history.cache == {f"XXBTZEUR:{int(T0 // HOUR)}": "none"}, bad


def test_history_get_records_missing_then_fetch_fills_cache():
    history = prices.History({})
    assert history.get("BTC", T0, "EUR", PAIRS) is None
    assert history.missing
    stub = Stub({"XXBTZEUR": {"XXBTZEUR": [trade("8762.8", T0 + 1)], "last": "1"}})
    history.fetch(stub, "EUR", PAIRS, deadline=5)
    assert not history.missing
    assert history.get("BTC", T0, "EUR", PAIRS) == Decimal("8762.8")
    assert history.cache == {f"XXBTZEUR:{int(T0 // HOUR)}": "8762.8"}
    json.dumps(history.cache)


def test_history_multiplies_route_legs():
    history = prices.History({
        f"ADAUSD:{int(T0 // HOUR)}": "2",
        f"ZEURZUSD:{int(T0 // HOUR)}": "4",
    })
    # ADA -> USD (2) -> EUR (inverted EUR/USD: 1/4)
    assert history.get("ADA", T0, "EUR", PAIRS) == Decimal("0.5")
    assert not history.missing


def test_history_valuation_currency_is_one():
    history = prices.History({})
    assert history.get("EUR", T0, "EUR", PAIRS) == Decimal(1)
    assert not history.missing


def test_history_unrouted_coin_uses_fallback_and_warns_once(capsys):
    log.reset()
    history = prices.History({})
    history.fallback = {"ZZZ": Decimal("3")}
    assert history.get("ZZZ", T0, "EUR", PAIRS) == Decimal("3")
    assert history.get("ZZZ", T0 + 99999, "EUR", PAIRS) == Decimal("3")
    assert history.get("YYY", T0, "EUR", PAIRS) is None
    assert capsys.readouterr().err.count("no price route for a coin") == 1
    assert not history.missing


def test_history_same_hour_reuses_cache():
    history = prices.History({})
    history.get("BTC", T0, "EUR", PAIRS)
    history.get("BTC", T0 + 60, "EUR", PAIRS)
    history.get("BTC", T0 + 120, "EUR", PAIRS)
    stub = Stub({"XXBTZEUR": {"XXBTZEUR": [trade("1", T0)], "last": "1"}})
    history.fetch(stub, "EUR", PAIRS, deadline=5)
    assert len(stub.calls) == 1
    assert history.get("BTC", T0 + 1000, "EUR", PAIRS) == Decimal(1)
    history.get("BTC", T0, "EUR", PAIRS)
    history.fetch(stub, "EUR", PAIRS, deadline=5)
    assert len(stub.calls) == 1


def test_history_shared_pair_hour_fetched_once_across_coins():
    history = prices.History({})
    history.get("ADA", T0, "EUR", PAIRS)
    history.get("USD", T0, "EUR", PAIRS)  # also needs EUR/USD in the same hour
    stub = Stub({
        "ADAUSD": {"ADAUSD": [trade("2", T0)], "last": "1"},
        "ZEURZUSD": {"ZEURZUSD": [trade("4", T0)], "last": "1"},
    })
    history.fetch(stub, "EUR", PAIRS, deadline=5)
    assert sorted(c[1]["pair"] for c in stub.calls) == ["ADAUSD", "ZEURZUSD"]


def test_history_stops_on_deadline_keeping_missing():
    history = prices.History({})
    for i in range(4):
        history.get("BTC", T0 + i * HOUR, "EUR", PAIRS)
    reply = {"XXBTZEUR": [trade("1", T0)], "last": "1"}
    stub = Stub({"XBTEUR": reply, "XXBTZEUR": reply}, remaining=10.0, step=3.0)
    history.fetch(stub, "EUR", PAIRS, deadline=5.0)
    # remaining 10 -> fetch, 7 -> fetch, 4 <= 5 -> stop
    assert len(stub.calls) == 2
    assert len(history.cache) == 2
    assert len(history.missing) == 2


def test_history_stops_quietly_on_rate_limit_and_out_of_time():
    for exc in (RateLimited("x"), OutOfTime("x")):
        history = prices.History({})
        history.get("BTC", T0, "EUR", PAIRS)
        stub = Stub({}, raises=exc)
        history.fetch(stub, "EUR", PAIRS, deadline=0)
        assert len(history.missing) == 1
        assert history.cache == {}


def test_history_no_trades_falls_back_to_current():
    history = prices.History({})
    history.fallback = {"BTC": Decimal("42")}
    history.get("BTC", T0, "EUR", PAIRS)
    stub = Stub({"XXBTZEUR": {"XXBTZEUR": [], "last": "1"}})
    history.fetch(stub, "EUR", PAIRS, deadline=0)
    assert history.cache == {f"XXBTZEUR:{int(T0 // HOUR)}": "none"}
    assert history.get("BTC", T0, "EUR", PAIRS) == Decimal("42")
    assert not history.missing


def test_history_price_taken_after_the_event_warns(capsys):
    log.reset()
    history = prices.History({})
    history.get("BTC", T0, "EUR", PAIRS)
    stub = Stub({"XXBTZEUR": {"XXBTZEUR": [trade("7", T0 + 8 * 86400)], "last": "1"}})
    history.fetch(stub, "EUR", PAIRS, deadline=0)
    assert history.get("BTC", T0, "EUR", PAIRS) == Decimal(7)
    assert "price taken after the event" in capsys.readouterr().err


def test_trades_query_uses_since_seconds_and_count_1():
    client, http = make({TRADES: [ok({"XXBTZEUR": [trade("1.5", T0 + 2)], "last": "1600000009679790589"})]})
    history = prices.History({})
    history.get("BTC", T0 + 0.9, "EUR", PAIRS)
    history.fetch(client, "EUR", PAIRS, deadline=5)
    assert query(http.calls[0]) == {"pair": ["XXBTZEUR"], "since": [str(int(T0 + 0.9))], "count": ["1"]}
    assert http.calls[0][0] == "GET"
    assert history.get("BTC", T0 + 0.9, "EUR", PAIRS) == Decimal("1.5")


def test_history_recent_event_without_trades_stays_missing():
    now = T0 + 86400 - 1  # the event is less than a day old
    history = prices.History({})
    history.get("BTC", T0, "EUR", PAIRS)
    stub = Stub({"XXBTZEUR": {"XXBTZEUR": [], "last": "1"}})
    history.fetch(stub, "EUR", PAIRS, deadline=0, now=now)
    assert history.cache == {} and list(history.missing) == [f"XXBTZEUR:{int(T0 // HOUR)}"]
    history.fetch(stub, "EUR", PAIRS, deadline=0, now=T0 + 86400 + 1)  # a day old: Kraken has nothing
    assert history.cache == {f"XXBTZEUR:{int(T0 // HOUR)}": "none"} and not history.missing


def test_history_fallback_price_is_frozen_once_used(capsys):
    log.reset()
    history = prices.History({f"XXBTZEUR:{int(T0 // HOUR)}": "none"})
    history.fallback = {"BTC": Decimal("42")}
    assert history.get("BTC", T0, "EUR", PAIRS) == Decimal("42")
    assert "price taken after the event" in capsys.readouterr().err
    history.fallback = {"BTC": Decimal("50")}  # the price now has moved: the lot keeps the first one
    assert history.get("BTC", T0 + 60, "EUR", PAIRS) == Decimal("42")
    assert prices.History(json.loads(json.dumps(history.cache))).get("BTC", T0, "EUR", PAIRS) == Decimal("42")


def test_history_zero_fallback_is_not_frozen():
    history = prices.History({f"XXBTZEUR:{int(T0 // HOUR)}": "none"})
    history.fallback = {"BTC": Decimal(0)}
    assert history.get("BTC", T0, "EUR", PAIRS) == 0
    history.fallback = {"BTC": Decimal("50")}
    assert history.get("BTC", T0, "EUR", PAIRS) == Decimal("50")


def test_history_bad_pair_is_none_and_the_others_are_still_fetched(capsys):
    log.reset()
    history = prices.History({})
    history.get("ADA", T0, "EUR", PAIRS)  # ADAUSD, then EUR/USD
    history.get("BTC", T0, "EUR", PAIRS)

    class OneBad(Stub):
        def public(self, method, params=None):
            if params["pair"] == "ADAUSD":
                self.calls.append((method, dict(params)))
                raise BadRequest("refused")
            return super().public(method, params)

    stub = OneBad({"ZEURZUSD": {"ZEURZUSD": [trade("4", T0)], "last": "1"},
                   "XXBTZEUR": {"XXBTZEUR": [trade("9", T0)], "last": "1"}})
    history.fetch(stub, "EUR", PAIRS, deadline=0)
    hour = int(T0 // HOUR)
    assert history.cache == {f"ADAUSD:{hour}": "none", f"ZEURZUSD:{hour}": "4", f"XXBTZEUR:{hour}": "9"}
    assert not history.missing
    assert "historical prices unavailable" not in capsys.readouterr().err


def test_current_retries_pairs_one_by_one_when_the_batch_is_refused():
    client, http = make({TICKER: [err("EQuery:Unknown asset pair"), err("EQuery:Unknown asset pair"),
                                  ok({"XXBTZEUR": ticker("10")})]})
    out = prices.current(client, ["BTC", "ETH"], "EUR", PAIRS)
    assert out == {"BTC": Decimal("10")}
    assert [query(call)["pair"] for call in http.calls] == [["XETHZEUR,XXBTZEUR"], ["XETHZEUR"], ["XXBTZEUR"]]
