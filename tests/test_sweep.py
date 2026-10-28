import random
from datetime import date

import httpx
import pytest

from gwt import db
from gwt.cli import main
from gwt.sweep import MAX_CONSECUTIVE_FAILURES, SweepAborted, backoff_delay, run_sweep

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
    assert "pricingKey" not in row[5]


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


def test_cli_parses_and_rejects_bad_days(tmp_path):
    assert main(["sweep", "--days", "0", "--db", str(tmp_path / "x.db")]) == 2
