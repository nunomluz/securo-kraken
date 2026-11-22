"""Read-only Kraken probe for designing the Securo plugin.

Prints the *shape* of your Kraken balances and ledger: which asset codes,
entry types and subtypes appear, and how entries group together. It never
prints amounts, balances, ledger ids, refids or exact dates (months only).

It only calls GetApiKeyInfo, BalanceEx and Ledgers. Use a key with only
"Funds: Query" and "Data: Query ledger entries".

Usage (PowerShell):
    $env:KRAKEN_API_KEY = "<api key>"
    $env:KRAKEN_API_SECRET = "<private key>"
    python tools/kraken_probe.py > probe.txt
Read probe.txt before sharing it.
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal

API = "https://api.kraken.com"
KEY = os.environ.get("KRAKEN_API_KEY", "")
SECRET = os.environ.get("KRAKEN_API_SECRET", "")
PAGE_PAUSE = 6  # seconds between ledger pages (ledger calls cost 2; starter tier decays 0.33/s)

_last_nonce = 0


def _nonce() -> str:
    global _last_nonce
    _last_nonce = max(int(time.time() * 1000), _last_nonce + 1)
    return str(_last_nonce)


def private(method: str, **params) -> dict:
    path = f"/0/private/{method}"
    for _ in range(6):
        data = {"nonce": _nonce(), **params}
        body = urllib.parse.urlencode(data)
        digest = hashlib.sha256((data["nonce"] + body).encode()).digest()
        sign = hmac.new(base64.b64decode(SECRET), path.encode() + digest, hashlib.sha512).digest()
        request = urllib.request.Request(
            API + path,
            data=body.encode(),
            headers={
                "API-Key": KEY,
                "API-Sign": base64.b64encode(sign).decode(),
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "securo-kraken-probe",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            reply = json.load(response)
        errors = reply.get("error") or []
        if any("Rate limit" in e for e in errors):
            print("# rate limited, waiting 20 s", file=sys.stderr)
            time.sleep(20)
            continue
        if errors:
            raise RuntimeError(f"{method}: {errors}")
        return reply["result"]
    raise RuntimeError(f"{method}: still rate limited")


def sign_of(value: str) -> str:
    d = Decimal(value or "0")
    return "+" if d > 0 else "-" if d < 0 else "0"


def month(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def main() -> None:
    if not KEY or not SECRET:
        sys.exit("Set KRAKEN_API_KEY and KRAKEN_API_SECRET first.")

    print("== GetApiKeyInfo (field names and permission names only)")
    try:
        info = private("GetApiKeyInfo")
        print("fields:", sorted(info))
        perms = info.get("permissions")
        print("permissions:", json.dumps(perms, sort_keys=True) if not isinstance(perms, (int, float, str)) else type(perms).__name__)
    except Exception as exc:  # shape only; not essential
        print("unavailable:", exc)

    print("\n== BalanceEx keys (zero / nonzero only)")
    balances = private("BalanceEx")
    for code in sorted(balances):
        entry = balances[code]
        fields = sorted(entry) if isinstance(entry, dict) else type(entry).__name__
        nonzero = Decimal(entry.get("balance", "0")) != 0 if isinstance(entry, dict) else "?"
        print(f"{code:20} {'nonzero' if nonzero else 'zero':8} fields={fields}")

    print("\n== Ledgers (walking newest to oldest)", file=sys.stderr)
    entries: dict[str, dict] = {}
    first = private("Ledgers")
    reported_count = first.get("count")
    page = first.get("ledger", {})
    pages = 1
    newest_first = None
    while page:
        new = {k: v for k, v in page.items() if k not in entries}
        if newest_first is None and len(page) > 1:
            times = [v["time"] for v in page.values()]
            newest_first = times == sorted(times, reverse=True)
        if not new:
            break
        entries.update(new)
        oldest_id = min(new, key=lambda k: new[k]["time"])
        print(f"# page {pages}: {len(new)} new, {len(entries)} total", file=sys.stderr)
        time.sleep(PAGE_PAUSE)
        page = private("Ledgers", end=oldest_id).get("ledger", {})
        pages += 1

    print("\n== Ledger overview")
    print("reported count:", reported_count, "| collected:", len(entries), "| pages:", pages)
    print("pages newest-first:", newest_first)
    if entries:
        times = [e["time"] for e in entries.values()]
        print("first month:", month(min(times)), "| last month:", month(max(times)))
    print("entry fields:", sorted({f for e in entries.values() for f in e}))

    print("\n== Entries by type/subtype/aclass -> asset codes")
    by_kind: dict[tuple, Counter] = defaultdict(Counter)
    for e in entries.values():
        by_kind[(e.get("type"), e.get("subtype") or "-", e.get("aclass"))][e.get("asset")] += 1
    for kind, assets in sorted(by_kind.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"{sum(assets.values()):6}  {'/'.join(map(str, kind))}: {dict(assets)}")

    print("\n== Groups by refid: leg pattern -> count")
    groups: dict[str, list] = defaultdict(list)
    missing_refid = 0
    for lid, e in entries.items():
        ref = e.get("refid")
        if not ref:
            missing_refid += 1
            ref = f"(none){lid}"
        groups[ref].append(e)
    patterns = Counter()
    spread = 0
    for legs in groups.values():
        times = [leg["time"] for leg in legs]
        if max(times) - min(times) > 1:
            spread += 1
        pattern = " + ".join(
            sorted(
                f"{leg.get('type')}/{leg.get('subtype') or '-'} {leg.get('asset')} {sign_of(leg.get('amount'))}"
                + (" fee" if Decimal(leg.get("fee") or "0") != 0 else "")
                for leg in legs
            )
        )
        patterns[pattern] += 1
    print("groups:", len(groups), "| entries without refid:", missing_refid,
          "| groups whose legs differ in time by >1 s:", spread)
    for pattern, count in patterns.most_common():
        print(f"{count:6}  {pattern}")

    print("\n== Dust: entries with 0 < |amount| < 0.000001, by type/asset")
    dust = Counter(
        (e.get("type"), e.get("asset"))
        for e in entries.values()
        if 0 < abs(Decimal(e.get("amount") or "0")) < Decimal("0.000001")
    )
    for (kind, asset), count in dust.most_common():
        print(f"{count:6}  {kind} {asset}")


if __name__ == "__main__":
    main()
