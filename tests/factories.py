"""Shared test builders and a fake HTTP transport. All values are made up."""

import json
from bisect import bisect_left
from urllib.parse import parse_qsl, urlsplit

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


ASSETS = {
    "XXBT": {"altname": "XBT"}, "XETH": {"altname": "ETH"}, "ETH2": {"altname": "ETH2"},
    "ETH2.S": {"altname": "ETH2.S"}, "SOL": {"altname": "SOL"}, "ZEUR": {"altname": "EUR"},
    "ZUSD": {"altname": "USD"}, "KFEE": {"altname": "FEE"},
}
ASSET_PAIRS = {
    "XXBTZEUR": {"base": "XXBT", "quote": "ZEUR"}, "XXBTZUSD": {"base": "XXBT", "quote": "ZUSD"},
    "XETHZEUR": {"base": "XETH", "quote": "ZEUR"}, "SOLEUR": {"base": "SOL", "quote": "ZEUR"},
    "ZEURZUSD": {"base": "ZEUR", "quote": "ZUSD"},
}


class FakeKraken:
    """A made-up Kraken: balances, a ledger served newest first (50 a page, `end` inclusive),
    names, pairs, last prices (`ticker`) and historical prices (`history`, one per pair).

    `fail` maps a method name to an error string returned instead of a result; `limit` maps a
    method name to how many calls succeed before Kraken answers with its rate-limit error.
    """

    def __init__(self, *, balances=None, rows=(), ticker=None, history=None, fail=None, limit=None) -> None:
        self.balances = dict(balances or {})
        self.rows = dict(rows)
        self.ticker = dict(ticker or {})
        self.history = dict(history or {})
        self.fail = dict(fail or {})
        self.limit = dict(limit or {})
        self.calls: list[tuple[str, dict]] = []
        self._newest_first: list | None = None

    def add(self, *rows) -> None:
        self.rows.update(rows)
        self._newest_first = None

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float):
        parts = urlsplit(url)
        name = parts.path.rsplit("/", 1)[-1]
        params = dict(parse_qsl(body.decode() if body else parts.query))
        self.calls.append((name, params))
        if name in self.fail:
            return 200, json.dumps({"error": [self.fail[name]]}).encode()
        if name in self.limit and self.count(name) > self.limit[name]:
            return 200, json.dumps({"error": ["EAPI:Rate limit exceeded"]}).encode()
        result = getattr(self, "_" + name)(params)
        return 200, json.dumps({"error": [], "result": result}).encode()

    def count(self, name: str) -> int:
        return sum(1 for called, _ in self.calls if called == name)

    def _BalanceEx(self, params):
        return {code: {"balance": amount, "hold_trade": "0"} for code, amount in self.balances.items()}

    def _Ledgers(self, params):
        if self._newest_first is None:
            self._newest_first = sorted(self.rows.items(), key=lambda item: (-item[1]["time"], item[0]))
        end = float(params["end"]) if "end" in params else None
        start = 0 if end is None else bisect_left(self._newest_first, -end, key=lambda item: -item[1]["time"])
        return ledger_page(self._newest_first[start:start + 50])

    def _Assets(self, params):
        return ASSETS

    def _AssetPairs(self, params):
        return ASSET_PAIRS

    def _Ticker(self, params):
        wanted = params["pair"].split(",")
        return {key: {"c": [self.ticker[key], "1"]} for key in wanted if key in self.ticker}

    def _Trades(self, params):
        pair = params["pair"]
        trades = [[self.history[pair], "1", float(params["since"]) + 1, "b", "m", "", 1]] if pair in self.history else []
        return {pair: trades, "last": params["since"]}
