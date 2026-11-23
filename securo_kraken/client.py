"""Read-only HTTP client for the Kraken REST API.

Private calls are signed POSTs, public calls are plain GETs. Kraken reports
errors as a JSON ``error`` list (usually with HTTP 200) and sends no
rate-limit headers, so private calls are paced against an estimated counter.
Error messages are fixed text: they never include credentials, signatures,
URLs or response bodies.
"""

import base64
import binascii
import hashlib
import hmac
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from decimal import Decimal

from .errors import BadRequest, NeedsAuth, OutOfTime, RateLimited, Transient

HOST = "api.kraken.com"
BUDGET_SECONDS = 40
LEDGER_BUDGET = 30
HTTP_TIMEOUT = 15
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
PERMISSIONS = {"BalanceEx": "Funds: Query", "Ledgers": "Data: Query ledger entries"}

COUNTER_MAX = 15.0
COUNTER_DECAY = 0.33  # per second, starter tier
COSTS = {"Ledgers": 2.0}
PUBLIC_GAP = 1.0
_MAX_CREDENTIAL = 512
_KEY = re.compile(r"[A-Za-z0-9+/=_-]+")
PRIVATE_METHODS = frozenset({"BalanceEx", "Ledgers"})  # read-only, and nothing else
PUBLIC_METHODS = frozenset({"Assets", "AssetPairs", "Ticker", "Trades"})

# (method, url, headers, body, timeout seconds) -> (status, body)
Http = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def urllib_http(method: str, url: str, headers: dict[str, str], body: bytes | None,
                timeout: float = HTTP_TIMEOUT) -> tuple[int, bytes]:
    """One request without following redirects."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        try:
            with _opener.open(request, timeout=timeout) as resp:
                data = resp.read(MAX_RESPONSE_BYTES + 1)
                status = resp.status
        except urllib.error.HTTPError as exc:
            status, data = exc.code, exc.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise Transient(f"Couldn't reach Kraken ({type(exc).__name__}).") from None
    if len(data) > MAX_RESPONSE_BYTES:
        raise Transient("Kraken returned a response that is too large.")
    return status, data


def sign(secret_b64: str, path: str, nonce: str, body: str) -> str:
    """API-Sign = base64(HMAC-SHA512(b64decode(secret), path + SHA256(nonce + body)))."""
    digest = hashlib.sha256((nonce + body).encode()).digest()
    mac = hmac.new(base64.b64decode(secret_b64), path.encode() + digest, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


class Client:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        http: Http = urllib_http,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        budget: float = BUDGET_SECONDS,
    ) -> None:
        key = (api_key or "").strip()
        secret = (api_secret or "").strip()
        if not key or not secret:
            raise NeedsAuth("Enter both the Kraken API key and private key.")
        if len(key) > _MAX_CREDENTIAL or len(secret) > _MAX_CREDENTIAL:
            raise NeedsAuth("The Kraken API key or private key is too long.")
        if not _KEY.fullmatch(key):
            raise NeedsAuth("The Kraken API key contains characters that are not allowed.")
        try:
            base64.b64decode(secret, validate=True)
        except (binascii.Error, ValueError):
            raise NeedsAuth("The Kraken private key is not valid. Paste it exactly as Kraken showed it.") from None
        self._key = key
        self._secret = secret
        self._http = http
        self._clock = clock
        self._wall = wall
        self._sleep = sleep
        self._start = clock()
        self._deadline = self._start + budget
        self._nonce = 0
        self._counter = 0.0
        self._counter_at = self._start
        self._last_public: float | None = None

    def remaining(self) -> float:
        return self._deadline - self._clock()

    def elapsed(self) -> float:
        return self._clock() - self._start

    # -- public API -----------------------------------------------------

    def private(self, method: str, params: dict[str, str] | None = None) -> dict:
        _check_method(method, PRIVATE_METHODS)
        self._pace_private(COSTS.get(method, 1.0))
        path = f"/0/private/{method}"
        self._nonce = max(int(self._wall() * 1000), self._nonce + 1)
        nonce = str(self._nonce)
        body = urllib.parse.urlencode({"nonce": nonce, **(params or {})})
        headers = {
            "API-Key": self._key,
            "API-Sign": sign(self._secret, path, nonce, body),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        status, data = self._request("POST", f"https://{HOST}{path}", headers, body.encode())
        return self._handle(status, data, method)

    def public(self, method: str, params: dict[str, str] | None = None) -> dict:
        _check_method(method, PUBLIC_METHODS)
        self._pace_public()
        url = f"https://{HOST}/0/public/{method}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        status, data = self._request("GET", url, {"Accept": "application/json"}, None)
        return self._handle(status, data, method)

    # -- internals ------------------------------------------------------

    def _decay(self) -> None:
        now = self._clock()
        self._counter = max(0.0, self._counter - (now - self._counter_at) * COUNTER_DECAY)
        self._counter_at = now

    def _pace_private(self, cost: float) -> None:
        self._decay()
        over = self._counter + cost - COUNTER_MAX
        if over > 0:
            wait = over / COUNTER_DECAY
            if wait > self.remaining() - 2:
                raise RateLimited("Kraken rate limit reached.")
            self._sleep(wait)
            self._decay()
        self._counter += cost

    def _pace_public(self) -> None:
        now = self._clock()
        if self._last_public is not None:
            wait = PUBLIC_GAP - (now - self._last_public)
            if wait > 0:
                if wait > self.remaining() - 2:
                    raise RateLimited("Kraken rate limit reached.")
                self._sleep(wait)
        self._last_public = self._clock()

    def _request(self, method: str, url: str, headers: dict[str, str],
                 body: bytes | None) -> tuple[int, bytes]:
        remaining = self.remaining()
        if remaining <= 1:
            raise OutOfTime("The sync time budget ran out.")
        # A slow request must not run past the budget (Securo kills the call at 60 s).
        timeout = min(HTTP_TIMEOUT, remaining)
        try:
            return self._http(method, url, headers, body, timeout)
        except (OSError, http.client.HTTPException) as exc:
            raise Transient(f"Couldn't reach Kraken ({type(exc).__name__}).") from None
        except ValueError:
            raise Transient("Couldn't reach Kraken (ValueError).") from None

    @staticmethod
    def _handle(status: int, data: bytes, method: str) -> dict:
        if status != 200:
            raise Transient(f"Kraken answered HTTP {status}.")
        try:
            payload = json.loads(data, parse_float=Decimal)
        except ValueError:
            raise Transient("Kraken returned an unreadable response.") from None
        if not isinstance(payload, dict):
            raise Transient("Kraken returned an unreadable response.")
        errors = payload.get("error")
        if errors:
            if not isinstance(errors, list):
                errors = [errors]
            raise _map_errors([str(e) for e in errors], method)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise Transient("Kraken returned an unreadable response.")
        return result


def _check_method(method: str, allowed: frozenset[str]) -> None:
    """Only the read methods the plugin needs can be called; anything else is a programming error."""
    if method not in allowed:
        raise ValueError("Kraken method is not allowed.")


def _map_errors(errors: list[str], method: str) -> Exception:
    text = " | ".join(errors)
    lower = text.lower()
    if "Invalid key" in text or "Invalid signature" in text:
        return NeedsAuth("Kraken rejected the API key or private key.")
    if "Permission denied" in text:
        needed = PERMISSIONS.get(method)
        detail = f'The API key is missing the "{needed}" permission.' if needed else "The API key isn't allowed to read this."
        return NeedsAuth(f"{detail} Edit the key in Kraken and enable it.", code="permission_missing")
    if "otp" in lower:
        return NeedsAuth("This API key asks for a 2FA code. Create a key without 2FA for Securo.")
    if "Rate limit exceeded" in text:
        return RateLimited("Kraken rate limit reached.")
    if "Invalid nonce" in text:
        return Transient("Kraken refused the request order (nonce). Use an API key used only by Securo.")
    if "EService:" in text or "Temporary lockout" in text:
        return Transient("Kraken is temporarily unavailable. Try again later.")
    category = re.sub(r"[^A-Za-z:]", "", ":".join(errors[0].split(":")[:2]))[:40]
    if "EQuery:" in text or "EGeneral:Invalid arguments" in text:
        return BadRequest(f"Kraken returned an error ({category}).")
    return Transient(f"Kraken returned an error ({category}).")
