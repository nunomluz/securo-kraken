import pytest

from securo_kraken import assets, log

ALTNAMES = {
    "XXBT": "XBT",
    "ZEUR": "EUR",
    "XETH": "ETH",
    "XXDG": "XDG",
    "ETH2.S": "ETH2.S",
    "USDT": "USDT",
}


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("XXBT", "BTC"),
        ("XBT.M", "BTC"),
        ("ZEUR", "EUR"),
        ("EUR.HOLD", "EUR"),
        ("EUR.M", "EUR"),
        ("ETH2.S", "ETH"),
        ("ETH2", "ETH"),
        ("DOT28.S", "DOT"),
        ("USDT.F", "USDT"),
        ("ETH.B", "ETH"),
        ("FLOWH.S", "FLOW"),
        ("XXDG", "DOGE"),
        ("LUNA2", "LUNA2"),
        ("KFEE", None),
    ],
)
def test_family(code, expected):
    assert assets.family(code, ALTNAMES) == expected


def test_family_unknown_suffix_is_unchanged_and_logged_once(capsys):
    log.reset()
    assert assets.family("AAPLx.T", ALTNAMES) == "AAPLx.T"
    assert assets.family("AAPLx.T", ALTNAMES) == "AAPLx.T"
    assert capsys.readouterr().err == "kraken: unknown asset_suffix: T\n"


def test_is_fiat():
    assert assets.is_fiat("EUR")
    assert not assets.is_fiat("USDT")


def test_valuations_and_aliases():
    assert assets.VALUATIONS == ("EUR", "USD", "GBP", "CAD", "CHF", "AUD", "JPY")
    assert assets.ALIASES["XBT"] == "BTC"


def test_altnames_from():
    result = {"XXBT": {"altname": "XBT"}, "ZEUR": {"altname": "EUR"}, "BAD": {}}
    assert assets.altnames_from(result) == {"XXBT": "XBT", "ZEUR": "EUR"}


def test_index_pairs_keeps_every_status():
    result = {
        "XXBTZEUR": {"base": "XXBT", "quote": "ZEUR", "status": "online"},
        "XETHZEUR": {"base": "XETH", "quote": "ZEUR", "status": "cancel_only"},
        "ODD": {"base": "XXBT"},
    }
    assert assets.index_pairs(result, ALTNAMES) == {
        "BTC/EUR": "XXBTZEUR",
        "ETH/EUR": "XETHZEUR",
    }


def test_route_direct():
    assert assets.route("BTC", "EUR", {"BTC/EUR": "XXBTZEUR"}) == [("XXBTZEUR", False)]


def test_route_inverted():
    assert assets.route("USD", "EUR", {"EUR/USD": "ZEURZUSD"}) == [("ZEURZUSD", True)]


def test_route_same_currency_is_empty():
    assert assets.route("EUR", "EUR", {}) == []


def test_route_via_usd():
    pairs = {"FOO/USD": "FOOUSD", "EUR/USD": "ZEURZUSD"}
    assert assets.route("FOO", "EUR", pairs) == [("FOOUSD", False), ("ZEURZUSD", True)]


def test_route_via_btc():
    pairs = {"FOO/BTC": "FOOXBT", "BTC/EUR": "XXBTZEUR"}
    assert assets.route("FOO", "EUR", pairs) == [("FOOXBT", False), ("XXBTZEUR", False)]


def test_route_none():
    assert assets.route("FOO", "EUR", {"BAR/USD": "BARUSD"}) is None
