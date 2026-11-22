import json
from decimal import Decimal

from securo_kraken.errors import RateLimited
from securo_kraken.ledger import OVERLAP, advance
from tests.factories import ledger_page, ledger_row

T0 = 1_700_000_000.0


def make_rows(count, start=T0, step=10.0, prefix="L"):
    """Newest-first made-up rows, one every `step` seconds going back."""
    return [ledger_row(f"{prefix}{i:05d}", f"R{i:05d}", start - i * step, "deposit", amount="1")
            for i in range(count)]


class FakeLedger:
    """Serves `rows` like Kraken: newest first, 50 per page, `end` inclusive."""

    def __init__(self, rows, fail_on=()):
        self.rows = list(rows)
        self.fail_on = set(fail_on)
        self.params = []
        self.now = 0.0

    def elapsed(self):
        return self.now

    def private(self, method, params=None):
        assert method == "Ledgers"
        self.params.append(dict(params or {}))
        if len(self.params) in self.fail_on:
            raise RateLimited("Kraken rate limit reached.")
        end = float(params["end"]) if "end" in (params or {}) else None
        rows = sorted((r for r in self.rows if end is None or r[1]["time"] <= end),
                      key=lambda r: -r[1]["time"])[:50]
        # Times arrive as Decimal because the client parses JSON floats that way.
        return ledger_page((i, {**r, "time": Decimal(str(r["time"]))}) for i, r in rows)


def ids(rows):
    return [r["id"] for r in rows]


def test_backfill_walks_pages_until_nothing_new():
    rows = make_rows(107)
    api = FakeLedger(rows)
    lstate = {}
    got, caught_up = advance(api, lstate)
    assert len(got) == 107 and len(set(ids(got))) == 107
    assert caught_up and lstate["phase"] == "incremental" and lstate["cursor"] is None
    assert api.params[0] == {"without_count": "true"}
    assert api.params[1] == {"without_count": "true", "end": str(rows[49][1]["time"])}
    assert api.params[2] == {"without_count": "true", "end": str(rows[98][1]["time"])}  # page 2's oldest
    assert got[0]["asset"] == "ZEUR" and got[0]["id"] == "L00000"
    json.dumps(lstate)


def test_backfill_resumes_after_rate_limit_without_gaps_or_repeats():
    rows = make_rows(107)
    api = FakeLedger(rows, fail_on={2})
    lstate = {}
    first, caught_up = advance(api, lstate)
    assert not caught_up and len(first) == 50
    assert lstate["phase"] == "backfill" and lstate["cursor"] == rows[49][1]["time"]
    second, caught_up = advance(api, lstate)
    assert caught_up
    assert api.params[2]["end"] == str(rows[49][1]["time"])
    assert sorted(ids(first + second)) == sorted(i for i, _ in rows)
    assert len(ids(first + second)) == len(set(ids(first + second)))


def test_rows_sharing_the_boundary_timestamp_are_not_lost_or_doubled():
    # 60 rows, the 50th and 51st (page edge) share one timestamp.
    rows = make_rows(60)
    rows[50] = ledger_row("L00050", "R00050", rows[49][1]["time"], "deposit", amount="1")
    api = FakeLedger(rows)
    lstate = {}
    first, _ = advance(api, lstate, stop_after=0.0)  # no budget: nothing happens
    assert first == [] and api.params == []
    api.now = 0.0
    got, caught_up = advance(api, lstate)
    assert caught_up
    assert sorted(ids(got)) == sorted(i for i, _ in rows)
    assert len(ids(got)) == len(set(ids(got)))


def test_incremental_reads_until_overlap_floor():
    rows = make_rows(120, step=100.0)  # 11,900 s of history
    api = FakeLedger(rows)
    lstate = {}
    advance(api, lstate)
    newest = lstate["newest"]
    api.params.clear()

    fresh = [ledger_row(f"N{i}", f"RN{i}", newest + 5 + i, "deposit", amount="1") for i in range(3)]
    api.rows = fresh + rows
    got, caught_up = advance(api, lstate)
    assert caught_up and sorted(ids(got)) == ["N0", "N1", "N2"]
    assert api.params[0] == {"without_count": "true"}  # from the top, no end
    # The first page already reaches back past newest - OVERLAP, so no second call is made.
    assert len(api.params) == 1
    assert lstate["floor"] is None and lstate["newest"] == newest + 7
    assert OVERLAP == 3600


def test_incremental_without_floor_in_reach_stops_on_a_page_with_nothing_new():
    rows = make_rows(120, step=10.0)  # a page spans 490 s, well inside the overlap
    api = FakeLedger(rows)
    lstate = {}
    advance(api, lstate)
    api.params.clear()
    fresh = [ledger_row("N0", "RN0", lstate["newest"] + 5, "deposit", amount="1")]
    api.rows = fresh + rows
    got, caught_up = advance(api, lstate)
    assert caught_up and ids(got) == ["N0"]
    assert len(api.params) == 2  # page 1 had a new row and stayed above the floor; page 2 had none


def test_incremental_interrupted_then_new_rows_arrive():
    rows = make_rows(160, step=100.0)
    api = FakeLedger(rows)
    lstate = {}
    advance(api, lstate)
    newest = lstate["newest"]

    wave1 = [ledger_row(f"A{i:03d}", f"RA{i}", newest + 10 + i, "deposit", amount="1") for i in range(70)]
    api.rows = wave1 + rows
    api.params.clear()
    api.now = 0.0
    api.fail_on = {2}
    first, caught_up = advance(api, lstate)
    assert not caught_up and len(first) == 50
    assert lstate["phase"] == "incremental" and lstate["cursor"] is not None
    floor = lstate["floor"]
    assert floor == newest - OVERLAP

    wave2 = [ledger_row(f"B{i:03d}", f"RB{i}", newest + 200 + i, "deposit", amount="1") for i in range(5)]
    api.rows = wave2 + wave1 + rows
    api.params.clear()
    api.fail_on = set()
    second, caught_up = advance(api, lstate)
    assert caught_up
    assert api.params[0]["end"] == str(lstate_cursor_of(wave1, first))  # resumed first
    everything = ids(first + second)
    assert len(everything) == len(set(everything))
    assert sorted(everything) == sorted(i for i, _ in wave1 + wave2)


def lstate_cursor_of(wave, first):
    return min(float(r["time"]) for r in first)


def test_seen_is_pruned_to_window():
    rows = make_rows(10_000)
    api = FakeLedger(rows)
    lstate = {}
    got, caught_up = advance(api, lstate)
    assert caught_up and len(got) == 10_000
    # Window is 2 * OVERLAP seconds at one row per 10 s.
    assert len(lstate["seen"]) <= 2 * OVERLAP // 10 + 2
    assert len(lstate["seen"]) < 1000
    assert len(json.dumps(lstate)) < 100_000


def test_params_never_include_ofs():
    api = FakeLedger(make_rows(120))
    lstate = {}
    advance(api, lstate)
    advance(api, lstate)
    assert len(api.params) >= 4
    for params in api.params:
        assert "ofs" not in params
        assert params["without_count"] == "true"
        assert all(isinstance(v, str) for v in params.values())
