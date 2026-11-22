"""Shared test builders and a fake HTTP transport. All values are made up."""

import json
from urllib.parse import urlsplit

Reply = tuple[int, object]

# Made-up but well-formed (valid base64) credentials.
API_KEY = "test-api-key-not-real"
API_SECRET = "dGVzdC1zZWNyZXQtbm90LXJlYWwtYnl0ZXMtMDEyMzQ1Njc4OQ=="


class FakeHttp:
    """Routes by URL path; replies pop in order, the last repeats.

    A reply is (status, body); a non-bytes body is JSON-encoded.
    """

    def __init__(self, routes: dict[str, list[Reply]]) -> None:
        self.routes = {key: list(replies) for key, replies in routes.items()}
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.timeouts: list[float] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float):
        self.calls.append((method, url, dict(headers), body))
        self.timeouts.append(timeout)
        path = urlsplit(url).path
        if path not in self.routes:
            raise AssertionError(f"unrouted URL path: {path}")
        replies = self.routes[path]
        status, payload = replies.pop(0) if len(replies) > 1 else replies[0]
        if not isinstance(payload, (bytes, bytearray)):
            payload = json.dumps(payload).encode()
        return status, bytes(payload)


def ok(result) -> Reply:
    return 200, {"error": [], "result": result}


def err(*errors: str) -> Reply:
    return 200, {"error": list(errors)}


def ledger_row(id: str, refid: str, time: float, type: str, subtype: str = "", asset: str = "ZEUR",
               amount: str = "0", fee: str = "0", aclass: str = "currency") -> tuple[str, dict]:
    return id, {"refid": refid, "time": time, "type": type, "subtype": subtype,
                "aclass": aclass, "asset": asset, "amount": amount, "fee": fee, "balance": "0"}


def ledger_page(rows) -> dict:
    return {"ledger": dict(rows), "count": None}
