"""Resumable walk over the Kraken ledger, newest first.

``Ledgers`` pages hold 50 rows and ``end`` is an inclusive timestamp, so the
walk resumes from the oldest time of the last page and relies on remembered ids
to skip rows that share a boundary timestamp. ``ofs`` is never sent.

Walk state lives in ``lstate`` (JSON-safe). It is only updated when ``advance``
returns, so an unexpected error leaves it exactly as it was.
"""

from . import log
from .client import LEDGER_BUDGET
from .errors import OutOfTime, RateLimited

OVERLAP = 3600  # seconds a fresh top walk reaches back below the newest row
BOUNDARY = 1.0  # ids this close to the cursor are kept for the resumed page
PAGE = 50  # rows Kraken returns per Ledgers page
STALL_STEP = 0.0001  # how far the cursor steps past a page of tied rows


def advance(client, lstate: dict, *, stop_after: float = LEDGER_BUDGET) -> tuple[list[dict], bool]:
    """Walk as far as time allows; return (new rows, caught_up)."""
    phase = lstate.get("phase") or "backfill"
    cursor = lstate.get("cursor")
    floor = lstate.get("floor")
    newest = lstate.get("newest")
    seen = dict(lstate.get("seen_at") or {})  # id -> time

    rows: list[dict] = []
    top = cursor is None  # this walk started from the top within this call
    caught_up = False

    while True:
        if client.elapsed() >= stop_after:
            break
        if cursor is None and phase == "incremental" and floor is None:
            floor = (newest if newest is not None else 0.0) - OVERLAP
        params = {"without_count": "true"}
        if cursor is not None:
            params["end"] = str(cursor)
        try:
            result = client.private("Ledgers", params)
        except (RateLimited, OutOfTime):
            break

        page = result.get("ledger") or {}
        times = []
        fresh = 0
        for ledger_id, raw in page.items():
            at = float(raw["time"])
            times.append(at)
            if newest is None or at > newest:
                newest = at
            if ledger_id in seen or (floor is not None and at < floor):
                continue
            seen[ledger_id] = at
            rows.append({**raw, "id": ledger_id})
            fresh += 1

        oldest = min(times) if times else None
        if fresh == 0 and cursor is not None and len(times) >= PAGE and oldest == max(times) == cursor:
            # A whole page of already-seen rows sharing the cursor time: more rows share
            # that time than one page holds, and `ofs` is off limits. Step past them.
            log.warning("ledger rows sharing one time were skipped")
            cursor -= STALL_STEP
            continue
        if phase == "backfill":
            finished = fresh == 0
        else:
            finished = len(times) < PAGE or oldest <= floor
        if not finished:
            cursor = oldest
            continue

        cursor = None
        floor = None
        phase = "incremental"
        if top:
            caught_up = True
            break
        top = True  # the resumed walk is done; now take a fresh look from the top

    limit = None if newest is None else newest - 2 * OVERLAP
    seen = {
        ledger_id: at for ledger_id, at in seen.items()
        if (limit is not None and at >= limit) or (cursor is not None and abs(at - cursor) <= BOUNDARY)
    }
    lstate.pop("seen", None)
    lstate.update(phase=phase, cursor=cursor, floor=floor, newest=newest, seen_at=seen)
    return rows, caught_up
