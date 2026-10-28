import json
import random
import threading
import time
import zlib
from datetime import date, datetime, timezone

import httpx
import pytest

from gwt import db
from gwt import sweep as sweep_mod
from gwt.cli import EXIT_PAUSED, main
from gwt.sweep import (
    MAX_CONSECUTIVE_FAILURES,
    RETRY_AFTER_CAP_S,
    SweepAborted,
    SweepInterrupted,
    SweepPaused,
    _retry_after_seconds,
    backoff_delay,
    execute_sweep,
    run_sweep,
)

START = date(2026, 11, 18)


class Recorder:
    """Fake sleep that records requested delays instead of sleeping."""

    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def client_for(handler) -> httpx.Client:
    return httpx.Client(base_url="https://prod.api.greatwolf.com", transport=httpx.MockTransport(handler))


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def test_sweep_stores_rows_and_jitters(conn, sample_payload):
    requested = []

    def handler(request):
        requested.append(request.url.params["arrival"])
        return httpx.Response(200, json=sample_payload)

    sleep = Recorder()
    result = run_sweep(
        conn, client_for(handler), days=3, start=START, run_id="r1", sleep=sleep, rng=random.Random(1)
    )

    assert result.fetched == 3 and result.skipped == 0
    assert requested == ["2026-11-18", "2026-11-19", "2026-11-20"]
    # One delay between each pair of requests, all within 3-5 s.
    assert len(sleep.calls) == 2
    assert all(3.0 <= s <= 5.0 for s in sleep.calls)

    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM suite_snapshots").fetchone()[0] == 15
    row = conn.execute(
        "SELECT total_rooms_available, suite_types, suite_types_sold_out, min_rate, ref_suite_rate, raw_json "
        "FROM snapshots WHERE arrival_date = '2026-11-18'"
    ).fetchone()
    assert row[:5] == (19, 5, 2, 379.99, 379.99)
    raw = db.decode_raw_json(row[5])
    assert raw["suites"] and "pricingKey" not in json.dumps(raw)


def test_sweep_resumes_within_run(conn, sample_payload):
    calls = []

    def handler(request):
        calls.append(request.url.params["arrival"])
        return httpx.Response(200, json=sample_payload)

    run_sweep(conn, client_for(handler), days=2, start=START, run_id="r1", sleep=Recorder())
    calls.clear()
    result = run_sweep(conn, client_for(handler), days=4, start=START, run_id="r1", sleep=Recorder())

    assert result.skipped == 2 and result.fetched == 2
    assert calls == ["2026-11-20", "2026-11-21"]

    # A different run_id re-captures everything (append-only history).
    calls.clear()
    run_sweep(conn, client_for(handler), days=2, start=START, run_id="r2", sleep=Recorder())
    assert calls == ["2026-11-18", "2026-11-19"]
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 6


def test_backoff_then_success(conn, sample_payload):
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(503),
            httpx.Response(200, json=sample_payload),
        ]
    )
    sleep = Recorder()
    result = run_sweep(conn, client_for(lambda r: next(responses)), days=1, start=START, run_id="r", sleep=sleep)

    assert result.fetched == 1
    backoffs = [s for s in sleep.calls if s > 5.0]
    assert backoffs == [backoff_delay(1), backoff_delay(2)]  # 10s, 20s (Retry-After 2 < backoff)


def test_backoff_is_exponential_and_capped():
    assert [backoff_delay(n) for n in range(1, 5)] == [10.0, 20.0, 40.0, 80.0]
    assert backoff_delay(20) == 300.0


def test_aborts_after_consecutive_failures(conn):
    count = {"n": 0}

    def handler(request):
        count["n"] += 1
        return httpx.Response(500)

    with pytest.raises(SweepAborted):
        run_sweep(conn, client_for(handler), days=10, start=START, run_id="r", sleep=Recorder())
    assert count["n"] == MAX_CONSECUTIVE_FAILURES
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0


def test_network_errors_count_as_failures(conn):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(SweepAborted):
        run_sweep(conn, client_for(handler), days=3, start=START, run_id="r", sleep=Recorder())


def test_success_resets_failure_streak(conn, sample_payload):
    # 4 failures, success, 4 failures, success: never reaches 5 in a row.
    seq = [500] * 4 + [200] + [500] * 4 + [200]
    it = iter(seq)

    def handler(request):
        status = next(it)
        return httpx.Response(status, json=sample_payload) if status == 200 else httpx.Response(status)

    result = run_sweep(conn, client_for(handler), days=2, start=START, run_id="r", sleep=Recorder())
    assert result.fetched == 2


def test_403_aborts_immediately(conn):
    count = {"n": 0}

    def handler(request):
        count["n"] += 1
        return httpx.Response(403)

    with pytest.raises(SweepAborted, match="403"):
        run_sweep(conn, client_for(handler), days=5, start=START, run_id="r", sleep=Recorder())
    assert count["n"] == 1


def test_other_4xx_skips_date(conn, sample_payload):
    def handler(request):
        if request.url.params["arrival"] == "2026-11-18":
            return httpx.Response(400)
        return httpx.Response(200, json=sample_payload)

    result = run_sweep(conn, client_for(handler), days=2, start=START, run_id="r", sleep=Recorder())
    assert result.failed_dates == 1 and result.fetched == 1


def test_runs_table_logs_completed_run(conn, sample_payload):
    stamps = iter(f"2026-10-28T03:17:{i:02d}+00:00" for i in range(60))
    run_sweep(
        conn,
        client_for(lambda r: httpx.Response(200, json=sample_payload)),
        days=3,
        start=START,
        run_id="r1",
        trigger="schedule:weekly",
        sleep=Recorder(),
        clock=lambda: next(stamps),
    )
    row = conn.execute(
        "SELECT run_id, trigger, started_at, finished_at, days, fetched, skipped, failed_dates, status, blocked FROM runs"
    ).fetchone()
    assert row[:2] == ("r1", "schedule:weekly")
    assert row[2] == "2026-10-28T03:17:00+00:00" and row[3] is not None
    assert row[4:] == (3, 3, 0, 0, "completed", 0)


def test_runs_table_logs_abort_and_block(conn):
    with pytest.raises(SweepAborted):
        run_sweep(conn, client_for(lambda r: httpx.Response(500)), days=3, start=START, run_id="a", sleep=Recorder())
    with pytest.raises(SweepAborted):
        run_sweep(conn, client_for(lambda r: httpx.Response(403)), days=3, start=START, run_id="b", sleep=Recorder())
    rows = conn.execute("SELECT run_id, status, blocked, failures, message FROM runs ORDER BY id").fetchall()
    assert rows[0][:4] == ("a", "aborted", 0, MAX_CONSECUTIVE_FAILURES)
    assert rows[1][:4] == ("b", "aborted", 1, 1)
    assert "403" in rows[1][4]


def test_resumed_run_gets_new_runs_row_and_counts_skips(conn, sample_payload):
    ok = client_for(lambda r: httpx.Response(200, json=sample_payload))
    run_sweep(conn, ok, days=2, start=START, run_id="r1", sleep=Recorder())
    run_sweep(conn, ok, days=3, start=START, run_id="r1", sleep=Recorder())
    rows = conn.execute("SELECT fetched, skipped FROM runs ORDER BY id").fetchall()
    assert rows == [(2, 0), (1, 2)]


def test_mark_interrupted(conn):
    db.start_run(conn, run_id="x", trigger="cli", started_at="t0", start_date="2026-11-18", days=5, skipped=0)
    assert db.mark_interrupted(conn, "t1") == 1
    assert conn.execute("SELECT status, finished_at FROM runs").fetchone() == ("interrupted", "t1")


def test_cli_parses_and_rejects_bad_days(tmp_path):
    assert main(["sweep", "--days", "0", "--db", str(tmp_path / "x.db")]) == 2


# --------------------------------------------------------------------------- raw_json storage


def test_raw_json_is_stored_compressed_and_old_text_rows_still_decode(conn, sample_payload):
    ok = client_for(lambda r: httpx.Response(200, json=sample_payload))
    run_sweep(conn, ok, days=1, start=START, run_id="r1", sleep=Recorder())
    stored = conn.execute("SELECT raw_json, typeof(raw_json) FROM snapshots").fetchone()
    assert stored[1] == "blob"
    text = zlib.decompress(stored[0]).decode()
    assert len(stored[0]) < len(text) / 3  # the JSON compresses well
    assert db.decode_raw_json(stored[0]) == json.loads(text)
    # Rows written before compression are plain TEXT and read the same way.
    assert db.decode_raw_json('{"suites": []}') == {"suites": []}
    assert db.decode_raw_json(None) is None


# --------------------------------------------------------------------------- Toronto dates (D)


def test_default_start_and_run_id_use_the_toronto_date(conn, sample_payload):
    """At 21:00 EDT on Oct 27 it's already Oct 28 in UTC; the sweep must still start on Oct 27."""
    requested = []

    def handler(request):
        requested.append(request.url.params["arrival"])
        return httpx.Response(200, json=sample_payload)

    evening = datetime(2026, 10, 28, 1, 0, tzinfo=timezone.utc)  # Tue Oct 27, 21:00 EDT
    result = run_sweep(conn, client_for(handler), days=2, sleep=Recorder(), now=lambda: evening)
    assert result.run_id == "2026-10-27"
    assert requested == ["2026-10-27", "2026-10-28"]
    assert sweep_mod.default_run_id(evening) == "2026-10-27"
    assert sweep_mod.local_today(datetime(2026, 10, 28, 4, 30, tzinfo=timezone.utc)).isoformat() == "2026-10-28"


# --------------------------------------------------------------------------- Retry-After cap (J)


def test_retry_after_over_the_cap_ends_the_run_rate_limited(conn):
    sleep = Recorder()
    with pytest.raises(SweepAborted, match="retry after 3600s") as exc:
        run_sweep(conn, client_for(lambda r: httpx.Response(429, headers={"Retry-After": "3600"})),
                  days=3, start=START, run_id="r", sleep=sleep)
    assert exc.value.status == "rate_limited" and not exc.value.blocked
    assert not any(s > RETRY_AFTER_CAP_S for s in sleep.calls)  # never waited it out with the lock held
    row = conn.execute("SELECT status, blocked, failures, finished_at FROM runs").fetchone()
    assert row[:3] == ("rate_limited", 0, 1) and row[3] is not None


def test_retry_after_within_the_cap_is_honoured(conn, sample_payload):
    responses = iter([httpx.Response(503, headers={"Retry-After": "600"}), httpx.Response(200, json=sample_payload)])
    sleep = Recorder()
    result = run_sweep(conn, client_for(lambda r: next(responses)), days=1, start=START, run_id="r", sleep=sleep)
    assert result.fetched == 1 and sleep.calls[0] == 600.0  # then the usual 3-5 s jitter before the retry


def test_retry_after_http_date():
    now = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    resp = httpx.Response(429, headers={"Retry-After": "Wed, 28 Oct 2026 12:05:00 GMT"})
    assert _retry_after_seconds(resp, now) == 300.0
    assert _retry_after_seconds(httpx.Response(429, headers={"Retry-After": "soon"}), now) is None
    assert _retry_after_seconds(httpx.Response(429), now) is None


# --------------------------------------------------------------------------- graceful stop (J)


def test_stop_between_dates_records_the_run_as_interrupted(conn, sample_payload):
    stop = threading.Event()
    requested = []

    def handler(request):
        requested.append(request.url.params["arrival"])
        stop.set()  # e.g. `docker compose stop` while the first date is in flight
        return httpx.Response(200, json=sample_payload)

    with pytest.raises(SweepInterrupted):
        run_sweep(conn, client_for(handler), days=5, start=START, run_id="r", sleep=Recorder(), stop=stop)
    assert requested == ["2026-11-18"]  # the in-flight date is saved, no further dates are fetched
    row = conn.execute("SELECT status, fetched, message, finished_at FROM runs").fetchone()
    assert row[0] == "interrupted" and row[1] == 1 and "shutdown" in row[2] and row[3] is not None
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1


def test_stop_wakes_a_backoff_wait(conn):
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    began = time.monotonic()
    with pytest.raises(SweepInterrupted):
        # 503 -> a 10 s backoff wait; the stop event must cut it short (no fake sleep here).
        run_sweep(conn, client_for(lambda r: httpx.Response(503)), days=1, start=START, run_id="r", stop=stop)
    assert time.monotonic() - began < 5
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "interrupted"


def test_ctrl_c_records_the_run_as_interrupted(conn):
    def handler(request):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_sweep(conn, client_for(handler), days=2, start=START, run_id="r", sleep=Recorder())
    assert conn.execute("SELECT status, message FROM runs").fetchone() == ("interrupted", "interrupted (Ctrl-C)")


# --------------------------------------------------------------------------- pause after 403 (B)


@pytest.fixture
def mock_api(monkeypatch, sample_payload):
    """execute_sweep/CLI sweeps talk to a mock API, never the real one."""
    state = {"status": 200, "calls": 0}

    def handler(request):
        state["calls"] += 1
        if state["status"] != 200:
            return httpx.Response(state["status"])
        return httpx.Response(200, json=sample_payload)

    monkeypatch.setattr(sweep_mod, "make_client", lambda: client_for(handler))
    monkeypatch.setattr(sweep_mod, "MIN_DELAY_S", 0.0)
    monkeypatch.setattr(sweep_mod, "MAX_DELAY_S", 0.0)
    return state


def test_403_pauses_collection_until_unblock(tmp_path, mock_api, capsys):
    path = tmp_path / "gwl.db"
    mock_api["status"] = 403
    with pytest.raises(SweepAborted):
        execute_sweep(path, days=3, start=START, run_id="blocked-run")
    conn = db.connect(path)
    pause = db.collection_pause(conn)
    conn.close()
    assert pause["run_id"] == "blocked-run" and "403" in pause["message"]
    assert db.pause_message(pause).startswith(f"Collection paused: blocked (403) on {pause['date']}")

    # Scheduled and manual sweeps refuse while paused, without calling the API.
    mock_api["status"], mock_api["calls"] = 200, 0
    with pytest.raises(SweepPaused):
        execute_sweep(path, days=2, start=START)
    assert main(["sweep", "--days", "2", "--db", str(path)]) == EXIT_PAUSED
    assert mock_api["calls"] == 0
    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1  # refusals add no run rows
    conn.close()

    # `gwt unblock` clears it; the next sweep runs.
    assert main(["unblock", "--db", str(path)]) == 0
    assert "Unblocked" in capsys.readouterr().out
    assert main(["unblock", "--db", str(path)]) == 0
    assert "not paused" in capsys.readouterr().out
    result = execute_sweep(path, days=2, start=START, run_id="after")
    assert result.fetched == 2 and mock_api["calls"] == 2


def test_force_sweeps_while_paused(tmp_path, mock_api):
    path = tmp_path / "gwl.db"
    mock_api["status"] = 403
    with pytest.raises(SweepAborted):
        execute_sweep(path, days=1, start=START, run_id="b")
    mock_api["status"] = 200
    assert main(["sweep", "--days", "2", "--start", START.isoformat(), "--force", "--db", str(path)]) == 0
    conn = db.connect(path)
    try:
        assert db.collection_pause(conn) is None  # the forced run completed, so it's no longer the latest
        assert [r[0] for r in conn.execute("SELECT status FROM runs ORDER BY id")] == ["aborted", "completed"]
    finally:
        conn.close()


def test_a_new_block_after_unblock_pauses_again(tmp_path, mock_api):
    path = tmp_path / "gwl.db"
    mock_api["status"] = 403
    with pytest.raises(SweepAborted):
        execute_sweep(path, days=1, start=START, run_id="b1")
    assert main(["unblock", "--db", str(path)]) == 0
    with pytest.raises(SweepAborted):
        execute_sweep(path, days=1, start=START, run_id="b2")
    conn = db.connect(path)
    try:
        assert db.collection_pause(conn)["run_id"] == "b2"
    finally:
        conn.close()


def test_a_failed_forced_sweep_does_not_lift_the_pause(tmp_path, mock_api, monkeypatch):
    path = tmp_path / "gwl.db"
    mock_api["status"] = 403
    with pytest.raises(SweepAborted):
        execute_sweep(path, days=1, start=START, run_id="b")
    # --force, but the API now returns 500s: the run aborts after 5 failures.
    mock_api["status"] = 500
    monkeypatch.setattr(sweep_mod, "backoff_delay", lambda n: 0.0)
    with pytest.raises(SweepAborted):
        execute_sweep(path, days=2, start=START, run_id="forced", force=True)
    # --force interrupted before its first request.
    stop = threading.Event()
    stop.set()
    with pytest.raises(SweepInterrupted):
        execute_sweep(path, days=2, start=START, run_id="forced2", force=True, stop=stop)
    conn = db.connect(path)
    try:
        statuses = [r[0] for r in conn.execute("SELECT status FROM runs ORDER BY id")]
        assert statuses == ["aborted", "aborted", "interrupted"]
        assert db.collection_pause(conn)["run_id"] == "b"  # still paused: nothing completed
    finally:
        conn.close()
    # Scheduled sweeps still refuse.
    mock_api["status"], mock_api["calls"] = 200, 0
    with pytest.raises(SweepPaused):
        execute_sweep(path, days=2, start=START)
    assert mock_api["calls"] == 0
    # Only unblock (or a forced run that completes) lifts it.
    assert main(["unblock", "--db", str(path)]) == 0
    assert execute_sweep(path, days=1, start=START, run_id="after").fetched == 1
