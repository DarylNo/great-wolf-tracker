"""The synthetic demo database (`gwt demo-db`)."""

import sqlite3
import statistics as st
from datetime import date, datetime
from pathlib import Path

import pytest

from gwt import db
from gwt.cli import main
from gwt.demo import DEMO_SUITES, RAW_JSON_MARKER, build_demo_db, is_demo_db
from gwt.metrics import LODGE_TZ, lead_days

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=LODGE_TZ)


@pytest.fixture(scope="module")
def demo(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("demo") / "demo.db"
    result = build_demo_db(path, weeks=4, now=NOW)
    assert result["sweeps"] == 25  # 4 weekly + 21 daily
    return path


def q(path, sql, *args):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_demo_is_marked_synthetic_and_shaped_like_the_api(demo):
    assert is_demo_db(demo)
    assert q(demo, "SELECT value FROM meta WHERE key = 'synthetic_demo'") == [("1",)]
    assert q(demo, "SELECT COUNT(*), COUNT(DISTINCT suite_brand) FROM suites") == [(27, 17)]
    assert q(demo, "SELECT DISTINCT raw_json FROM snapshots") == [(RAW_JSON_MARKER,)]
    assert q(demo, "SELECT COUNT(*) FROM runs WHERE trigger = 'schedule:weekly'") == [(4,)]
    # Weekly sweeps at 03:17 and daily ones at 05:43 Toronto time, stored in UTC.
    starts = {r[0][11:16] for r in q(demo, "SELECT started_at FROM runs")}
    assert starts <= {"07:17", "08:17", "09:43", "10:43"}


def test_rooms_decline_toward_arrival(demo):
    rows = q(demo, "SELECT arrival_date, scraped_at, total_rooms_available FROM snapshots")
    by_date = {}
    for arrival, scraped_at, rooms in rows:
        by_date.setdefault(arrival, []).append((lead_days(arrival, scraped_at), rooms))
    declines = []
    for snaps in by_date.values():
        if len(snaps) >= 3:
            snaps.sort()
            declines.append(snaps[-1][1] - snaps[0][1])  # farthest-out minus closest-in
    assert len(declines) > 50
    assert st.mean(declines) > 5  # only ~4 weeks of history in this small demo
    assert sum(1 for d in declines if d >= 0) / len(declines) > 0.95


def test_weekends_fill_higher_and_cost_more(demo):
    rows = q(demo, """SELECT arrival_date, total_rooms_available, min_rate FROM snapshots
                      WHERE id IN (SELECT MAX(id) FROM snapshots GROUP BY arrival_date)""")
    weekend = [(r, p) for d, r, p in rows if date.fromisoformat(d).weekday() in (4, 5)]
    weekday = [(r, p) for d, r, p in rows if date.fromisoformat(d).weekday() not in (4, 5)]
    assert st.mean(r for r, _ in weekend) < st.mean(r for r, _ in weekday)
    assert st.mean(p for _, p in weekend if p) > st.mean(p for _, p in weekday if p)


def test_cheapest_suites_sell_out_first(demo):
    base = {s.code: s.base_rate for s in DEMO_SUITES if not s.accessible}
    rows = q(demo, "SELECT run_id, arrival_date, suite_code, available_room_count FROM suite_snapshots")
    by_snap = {}
    for run_id, arrival, code, rooms in rows:
        if code in base:
            by_snap.setdefault((run_id, arrival), []).append((code, rooms))
    gaps = []
    for suites in by_snap.values():
        sold = [base[c] for c, r in suites if r == 0]
        left = [base[c] for c, r in suites if r > 0]
        if sold and left:
            gaps.append(st.mean(left) - st.mean(sold))
    assert len(gaps) > 20
    assert st.mean(gaps) > 100  # rooms still available are pricier than the sold-out ones


def test_demo_db_cli_never_touches_the_real_db(tmp_path, monkeypatch):
    real = tmp_path / "real.db"
    monkeypatch.setenv("GWT_DB", str(real))
    assert main(["demo-db", "--out", str(real), "--weeks", "1"]) == 2
    assert not real.exists()
    monkeypatch.chdir(tmp_path)
    assert main(["demo-db", "--out", "data/gwl.db", "--weeks", "1"]) == 2
    assert not (tmp_path / "data" / "gwl.db").exists()
    assert main(["demo-db", "--out", "/app/data/gwl.db", "--weeks", "1"]) == 2


def test_demo_db_cli_refuses_to_overwrite_a_non_demo_file(tmp_path):
    other = tmp_path / "other.db"
    db.connect(other).close()  # a real-looking (non-demo) database
    assert main(["demo-db", "--out", str(other), "--weeks", "1"]) == 2
    assert main(["demo-db", "--out", str(other), "--weeks", "1", "--force"]) == 2
    assert not is_demo_db(other)


def test_demo_db_cli_writes_and_replaces_a_demo(tmp_path):
    out = tmp_path / "demo.db"
    assert main(["demo-db", "--out", str(out), "--weeks", "1"]) == 0
    assert is_demo_db(out)
    assert main(["demo-db", "--out", str(out), "--weeks", "1"]) == 2  # exists, no --force
    assert main(["demo-db", "--out", str(out), "--weeks", "1", "--force"]) == 0
    assert is_demo_db(out)
