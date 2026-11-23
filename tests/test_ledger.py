import json
from decimal import Decimal

from securo_kraken import log
from securo_kraken.errors import OutOfTime, RateLimited
from securo_kraken.ledger import OVERLAP, advance
from tests.factories import ledger_page, ledger_row

T0 = 1_700_000_000.0


def make_rows(count, start=T0, step=10.0, prefix="L"):
    """Newest-first made-up rows, one every `step` seconds going back."""
    return [ledger_row(f"{prefix}{i:05d}", f"R{i:05d}", start - i * step, "deposit", amount="1")
            for i in range(count)]


class FakeLedger:
    """Serves `rows` like Kraken: newest first, 50 per page, `end` inclusive."""

    def __init__(self, rows, fail_on=(), error=RateLimited, tick=0.0):
        self.rows = list(rows)
        self.fail_on = set(fail_on)
        self.error = error
        self.tick = tick  # seconds "spent" by every call
        self.params = []
        self.now = 0.0

    def elapsed(self):
        return self.now

    def private(self, method, params=None):
        assert method == "Ledgers"
        self.params.append(dict(params or {}))
        self.now += self.tick
        if len(self.params) in self.fail_on:
            raise self.error("Kraken stopped the walk.")
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
    got, caught_up = advance(api, lstate)
    assert caught_up
    assert sorted(ids(got)) == sorted(i for i, _ in rows)
    assert len(ids(got)) == len(set(ids(got)))


def test_incremental_reads_until_overlap_floor():
    rows = make_rows(400, step=10.0)  # 3,990 s of history, a page spans 490 s
    api = FakeLedger(rows)
    lstate = {}
    advance(api, lstate)
    newest = lstate["newest"]
    floor = newest - OVERLAP
    api.params.clear()

    api.rows = [ledger_row("N0", "RN0", newest + 5, "deposit", amount="1")] + rows
    got, caught_up = advance(api, lstate)
    assert caught_up and ids(got) == ["N0"]
    assert api.params[0] == {"without_count": "true"}  # from the top, no end
    assert len(api.params) > 2  # crosses several pages even though they hold nothing new
    # The last page requested is the one that reached the floor: the one before it did not.
    assert float(api.params[-1]["end"]) > floor >= float(api.params[-1]["end"]) - 490
    assert lstate["floor"] is None and lstate["newest"] == newest + 5


def test_incremental_returns_a_late_row_inside_the_window_on_a_later_page():
    rows = make_rows(400, step=10.0)
    api = FakeLedger(rows)
    lstate = {}
    advance(api, lstate)
    newest = lstate["newest"]

    fresh = ledger_row("N0", "RN0", newest + 5, "deposit", amount="1")
    late = ledger_row("LATE", "RLATE", newest - 1500, "deposit", amount="1")  # 4th page
    api.rows = [fresh, late] + rows
    got, caught_up = advance(api, lstate)
    assert caught_up and sorted(ids(got)) == ["LATE", "N0"]


def test_incremental_stops_on_a_short_page():
    rows = make_rows(30)
    api = FakeLedger(rows)
    lstate = {}
    advance(api, lstate)
    api.params.clear()
    api.rows = [ledger_row("N0", "RN0", lstate["newest"] + 5, "deposit", amount="1")] + rows
    got, caught_up = advance(api, lstate)
    assert caught_up and ids(got) == ["N0"] and len(api.params) == 1


def test_incremental_interrupted_then_new_rows_arrive():
    rows = make_rows(160, step=100.0)
    api = FakeLedger(rows)
    lstate = {}
    advance(api, lstate)
    newest = lstate["newest"]

    wave1 = [ledger_row(f"A{i:03d}", f"RA{i}", newest + 10 + i, "deposit", amount="1") for i in range(70)]
    api.rows = wave1 + rows
    api.params.clear()
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
    assert api.params[0]["end"] == str(min(float(r["time"]) for r in first))  # resumed first
    everything = ids(first + second)
    assert len(everything) == len(set(everything))
    assert sorted(everything) == sorted(i for i, _ in wave1 + wave2)


def test_seen_is_pruned_to_window():
    rows = make_rows(10_000)
    api = FakeLedger(rows)
    lstate = {}
    got, caught_up = advance(api, lstate)
    assert caught_up and len(got) == 10_000
    # Window is 2 * OVERLAP seconds at one row per 10 s.
    assert "seen" not in lstate
    assert len(lstate["seen_at"]) <= 2 * OVERLAP // 10 + 2
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


def test_a_full_page_of_tied_rows_does_not_end_the_backfill_early(capsys):
    log.reset()
    tied = T0 - 100
    rows = (make_rows(3, step=10.0)
            + [ledger_row(f"T{i:03d}", f"RT{i}", tied, "deposit", amount="1") for i in range(55)]
            + make_rows(10, start=T0 - 500, prefix="O"))
    api = FakeLedger(rows)
    lstate = {}
    got, caught_up = advance(api, lstate)
    assert caught_up
    assert {f"O{i:05d}" for i in range(10)} <= set(ids(got))  # the older rows were reached
    assert len(ids(got)) == len(set(ids(got)))
    assert "kraken: ledger rows sharing one time were skipped" in capsys.readouterr().err
    assert all("ofs" not in p for p in api.params)


def test_resuming_keeps_ids_near_the_cursor_even_when_far_below_the_window():
    rows = make_rows(160, step=100.0)  # cursor ends 9,800 s below newest, past 2 * OVERLAP
    rows[99] = ledger_row("L00099", "R00099", rows[98][1]["time"], "deposit", amount="1")  # tie at the edge
    api = FakeLedger(rows, fail_on={3})
    lstate = {}
    first, caught_up = advance(api, lstate)
    assert not caught_up and lstate["cursor"] == rows[98][1]["time"]
    assert lstate["newest"] - lstate["cursor"] > 2 * OVERLAP
    second, caught_up = advance(api, lstate)
    assert caught_up
    both = ids(first + second)
    assert len(both) == len(set(both))
    assert sorted(both) == sorted(i for i, _ in rows)


def test_out_of_time_keeps_the_cursor_and_returns_what_was_read():
    rows = make_rows(120)
    api = FakeLedger(rows, fail_on={2}, error=OutOfTime)
    lstate = {}
    got, caught_up = advance(api, lstate)
    assert not caught_up and len(got) == 50
    assert lstate["cursor"] == rows[49][1]["time"]


def test_budget_stops_the_walk_before_the_next_call():
    rows = make_rows(120)
    api = FakeLedger(rows, tick=20.0)
    lstate = {}
    got, caught_up = advance(api, lstate, stop_after=30.0)
    assert not caught_up and len(api.params) == 2 and len(got) == 99
    assert lstate["phase"] == "backfill" and lstate["cursor"] is not None
    api.now = 100.0
    again, caught_up = advance(api, lstate, stop_after=30.0)
    assert again == [] and not caught_up and len(api.params) == 2
