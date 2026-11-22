import base64
from decimal import Decimal

import pytest

from securo_kraken.client import BUDGET_SECONDS, Client, sign
from securo_kraken.errors import NeedsAuth, OutOfTime, RateLimited, Transient
from tests.factories import API_KEY, API_SECRET, FakeHttp, err, ok

DOCS_SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
BALANCE = "/0/private/BalanceEx"
LEDGERS = "/0/private/Ledgers"


class Clock:
    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make(routes, *, wall=lambda: 1_700_000_000.0, budget=BUDGET_SECONDS, clock=None):
    clock = clock or Clock()
    http = FakeHttp(routes)
    client = Client(API_KEY, API_SECRET, http=http, clock=clock, wall=wall, sleep=clock.sleep, budget=budget)
    return client, http, clock


def test_sign_matches_kraken_docs_vector():
    assert sign(
        DOCS_SECRET,
        "/0/private/AddOrder",
        "1616492376594",
        "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25",
    ) == "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="


def test_private_posts_signed_form_with_nonce_first():
    client, http, _ = make({BALANCE: [ok({"ZEUR": {}})]})
    assert client.private("BalanceEx") == {"ZEUR": {}}
    method, url, headers, body = http.calls[0]
    assert method == "POST"
    assert url == "https://api.kraken.com/0/private/BalanceEx"
    assert body.startswith(b"nonce=")
    assert headers["API-Key"] == API_KEY
    nonce = body.decode().split("&")[0].split("=")[1]
    assert headers["API-Sign"] == sign(API_SECRET, "/0/private/BalanceEx", nonce, body.decode())
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"


def test_private_params_follow_nonce():
    client, http, _ = make({LEDGERS: [ok({"ledger": {}, "count": 0})]})
    client.private("Ledgers", {"ofs": "50"})
    assert http.calls[0][3].decode().endswith("&ofs=50")


def test_nonce_strictly_increases_with_frozen_clock():
    client, http, _ = make({BALANCE: [ok({})]})
    client.private("BalanceEx")
    client.private("BalanceEx")
    first, second = (int(c[3].decode().split("&")[0].split("=")[1]) for c in http.calls)
    assert first == 1_700_000_000_000
    assert second == first + 1


def test_public_is_get_without_key_headers():
    client, http, _ = make({"/0/public/Trades": [ok({"last": "1"})]})
    assert client.public("Trades", {"pair": "XBTEUR", "since": "5"}) == {"last": "1"}
    method, url, headers, body = http.calls[0]
    assert method == "GET" and body is None
    assert url == "https://api.kraken.com/0/public/Trades?pair=XBTEUR&since=5"
    assert "API-Key" not in headers and "API-Sign" not in headers


def test_result_floats_are_decimal():
    client, _, _ = make({BALANCE: [(200, b'{"error":[],"result":{"x":0.1}}')]})
    assert client.private("BalanceEx")["x"] == Decimal("0.1")


@pytest.mark.parametrize("message", ["EAPI:Invalid key", "EAPI:Invalid signature"])
def test_invalid_key_or_signature_is_needs_auth(message):
    client, _, _ = make({BALANCE: [err(message)]})
    with pytest.raises(NeedsAuth) as exc:
        client.private("BalanceEx")
    assert exc.value.code == "credentials_invalid"


def test_permission_denied_names_permission():
    client, _, _ = make({LEDGERS: [err("EGeneral:Permission denied")]})
    with pytest.raises(NeedsAuth) as exc:
        client.private("Ledgers")
    assert "Data: Query ledger entries" in str(exc.value)
    assert exc.value.code == "permission_missing"


def test_otp_error_is_needs_auth():
    client, _, _ = make({BALANCE: [err("EAPI:Invalid OTP")]})
    with pytest.raises(NeedsAuth) as exc:
        client.private("BalanceEx")
    assert "2FA" in str(exc.value)


def test_invalid_nonce_is_transient_with_dedicated_key_hint():
    client, _, _ = make({BALANCE: [err("EAPI:Invalid nonce")]})
    with pytest.raises(Transient) as exc:
        client.private("BalanceEx")
    assert "used only by Securo" in str(exc.value)


def test_rate_limit_error_is_rate_limited():
    client, _, _ = make({BALANCE: [err("EAPI:Rate limit exceeded")]})
    with pytest.raises(RateLimited):
        client.private("BalanceEx")


@pytest.mark.parametrize(
    "reply",
    [err("EService:Unavailable"), err("EGeneral:Temporary lockout"), err("EAPI:Bad request"), (503, b"x"), (200, b"nope")],
)
def test_eservice_and_lockout_and_5xx_are_transient(reply):
    client, _, _ = make({BALANCE: [reply]})
    with pytest.raises(Transient):
        client.private("BalanceEx")


def test_unknown_error_category_is_sanitised_and_short():
    client, _, _ = make({BALANCE: [err("EOrder:Some thing 123:more" + "x" * 80)]})
    with pytest.raises(Transient) as exc:
        client.private("BalanceEx")
    assert "EOrder:Somethingx" not in str(exc.value)
    assert "(EOrder:Something)" in str(exc.value)


def test_transport_failure_is_transient():
    clock = Clock()

    def boom(*args):
        raise OSError("secret detail")

    client = Client(API_KEY, API_SECRET, http=boom, clock=clock, wall=lambda: 1.0, sleep=clock.sleep)
    with pytest.raises(Transient) as exc:
        client.private("BalanceEx")
    assert "OSError" in str(exc.value) and "secret detail" not in str(exc.value)


def test_pacing_sleeps_before_exceeding_starter_counter():
    client, http, clock = make({LEDGERS: [ok({})]})
    for _ in range(7):
        client.private("Ledgers")
    assert clock.sleeps == []
    client.private("Ledgers")
    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] >= 3.0
    assert len(http.calls) == 8


def test_pacing_raises_rate_limited_when_wait_exceeds_budget():
    client, http, _ = make({LEDGERS: [ok({})]}, budget=5)
    for _ in range(7):
        client.private("Ledgers")
    with pytest.raises(RateLimited):
        client.private("Ledgers")
    assert len(http.calls) == 7


def test_counter_decays_with_time():
    client, _, clock = make({LEDGERS: [ok({})]})
    for _ in range(7):
        client.private("Ledgers")
    clock.now += 10
    client.private("Ledgers")
    assert clock.sleeps == []


def test_public_calls_spaced_one_second():
    client, _, clock = make({"/0/public/Trades": [ok({})]})
    client.public("Trades")
    client.public("Trades")
    assert len(clock.sleeps) == 1 and clock.sleeps[0] == pytest.approx(1.0)


def test_budget_exhausted_raises_out_of_time():
    client, http, clock = make({BALANCE: [ok({})]}, budget=5)
    clock.now += 6
    with pytest.raises(OutOfTime):
        client.private("BalanceEx")
    assert http.calls == []


def test_request_timeout_capped_by_remaining_budget():
    client, http, clock = make({BALANCE: [ok({})]}, budget=5)
    client.private("BalanceEx")
    assert http.timeouts[0] == 5
    clock.now += 2
    assert client.remaining() == 3
    assert client.elapsed() == 2
    client.private("BalanceEx")
    assert http.timeouts[1] == 3


def test_messages_never_contain_secrets():
    cases = [
        err("EAPI:Invalid key"),
        err("EAPI:Invalid signature"),
        err("EGeneral:Permission denied"),
        err("EAPI:Invalid OTP"),
        err("EAPI:Invalid nonce"),
        err("EAPI:Rate limit exceeded"),
        err("EService:Unavailable"),
        err("EGeneral:Temporary lockout"),
        err("EFoo:Bar?x=1"),
        (500, b"body"),
        (200, b"nope"),
    ]
    for reply in cases:
        client, http, _ = make({LEDGERS: [reply]})
        with pytest.raises((NeedsAuth, Transient, RateLimited)) as exc:
            client.private("Ledgers")
        text = str(exc.value)
        assert API_KEY not in text and API_SECRET not in text and "?" not in text
        assert http.calls[0][2]["API-Sign"] not in text


@pytest.mark.parametrize("secret", ["", "   ", "not base64!!"])
def test_empty_or_non_base64_secret_is_needs_auth(secret):
    with pytest.raises(NeedsAuth):
        Client(API_KEY, secret, http=FakeHttp({}))


def test_empty_key_is_needs_auth():
    with pytest.raises(NeedsAuth):
        Client("", API_SECRET, http=FakeHttp({}))


def test_sign_secret_is_base64():
    assert base64.b64decode(API_SECRET)
