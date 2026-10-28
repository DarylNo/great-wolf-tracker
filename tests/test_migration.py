"""Upgrading the live database: a copy of one written by 84181f2 (the build the server ran
before the redesign; see tests/fixtures/make_legacy_db.py) migrated by the current code."""

import shutil
import sqlite3
import threading
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gwt import db
from gwt.cli import EXIT_USAGE, main
from gwt.collector import parse_availability
from gwt.config import ScheduleConfig, Settings
from gwt.locking import sweep_lock
from gwt.web.app import create_app

LEGACY = Path(__file__).parent / "fixtures" / "legacy-84181f2.db"
TODAY = date(2026, 9, 22)  # the fixture's last sweep
NO_SCHEDULE = ScheduleConfig(enabled=False, timezone="America/Toronto", jobs=())
OLD_TABLES = ("snapshots", "suite_snapshots", "suite_rate_families", "suites", "runs")
NEW_INDEXES = {"idx_suite_snapshots_snapshot", "idx_suite_snapshots_code"}


@pytest.fixture
def legacy_db(tmp_path) -> Path:
    path = tmp_path / "gwl.db"
    shutil.copyfile(LEGACY, path)
    return path


def make_client(path: Path) -> TestClient:
    settings = Settings(db_path=path, host="127.0.0.1", port=8080, schedule=NO_SCHEDULE)
    return TestClient(create_app(settings, today=lambda: TODAY))


def raw_connect(path: Path) -> sqlite3.Connection:
    """Plain sqlite3: no migration, so it sees the file exactly as it is."""
    return sqlite3.connect(str(path))


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def dump(path: Path, cols: dict[str, list[str]] | None = None) -> dict[str, list[tuple]]:
    """Every row of the pre-upgrade tables, restricted to the pre-upgrade columns."""
    conn = raw_connect(path)
    try:
        cols = cols or {t: columns(conn, t) for t in OLD_TABLES}
        return {t: conn.execute(f"SELECT {', '.join(cols[t])} FROM {t} ORDER BY rowid").fetchall() for t in OLD_TABLES}
    finally:
        conn.close()


def schema(path: Path) -> list[tuple]:
    conn = raw_connect(path)
    try:
        return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    finally:
        conn.close()


def test_fixture_has_the_old_schema():
    conn = sqlite3.connect(f"file:{LEGACY}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "meta" not in tables
        assert "max_rooms_seen" not in columns(conn, "suites")
        assert conn.execute("SELECT DISTINCT typeof(raw_json) FROM snapshots").fetchall() == [("text",)]
    finally:
        conn.close()


def test_web_startup_upgrades_without_losing_data(legacy_db):
    conn = raw_connect(legacy_db)
    old_cols = {t: columns(conn, t) for t in OLD_TABLES}
    conn.close()
    before = dump(legacy_db)
    assert sum(len(rows) for rows in before.values()) > 600

    with make_client(legacy_db) as c:  # the lifespan migrates before the first request is served
        health = c.get("/healthz").json()
        assert health["has_data"] and health["collection_paused"] is None  # the 403 isn't the latest run

    assert dump(legacy_db, old_cols) == before  # every old row and value unchanged

    conn = raw_connect(legacy_db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert "meta" in tables and NEW_INDEXES <= indexes
        assert {"max_rooms_seen", "min_rate_seen"} <= set(columns(conn, "suites"))
        # Backfills: every suite's most rooms / lowest rate ever seen, from its snapshots.
        for code, most, lowest in conn.execute("SELECT suite_code, max_rooms_seen, min_rate_seen FROM suites"):
            expected = conn.execute(
                "SELECT MAX(available_room_count), MIN(min_total_nightly_rate) FROM suite_snapshots WHERE suite_code = ?",
                (code,),
            ).fetchone()
            assert (most, lowest) == expected, code
        assert conn.execute("SELECT max_rooms_seen, min_rate_seen FROM suites WHERE suite_code = 'PGRZL'").fetchone() == (0, None)
    finally:
        conn.close()


def _routes(c: TestClient) -> list[str]:
    rooms = c.get("/api/rooms").json()["rooms"]
    slugs = [r["slug"] for r in rooms]
    codes = [code for r in rooms for code in r["codes"]]
    pages = ["/", "/?metric=rooms", "/?metric=pace", "/seasonality", "/trends", "/date/2026-09-22",
             "/date/2026-09-08", "/date/2027-01-01", "/suites", "/suites?window=30", "/suites?window=365",
             "/runs", "/runs?page=2"]
    pages += [f"/rooms/{s}" for s in slugs] + [f"/rooms/{slugs[0]}?date=2026-09-08"]
    pages += [f"/suites/{code}" for code in codes] + [f"/suites/{codes[0]}?date=2026-09-08"]
    api = ["/api/plan", "/api/seasonality", "/api/dates/2026-09-22", "/api/rooms", "/api/rooms/matrix",
           "/api/rooms/matrix?window=365", "/api/suites", "/api/runs", "/api/runs?page=2", "/healthz",
           "/static/app.js", "/static/app.css", "/static/plotly.min.js"]
    api += [f"/api/rooms/{s}" for s in slugs] + [f"/api/suites/{code}" for code in codes]
    return pages + api


def test_every_page_and_api_route_returns_200_after_upgrade(legacy_db):
    with make_client(legacy_db) as c:
        routes = _routes(c)
        assert len(routes) > 40
        failures = {path: r.status_code for path in routes if (r := c.get(path)).status_code != 200}
        assert failures == {}
        # Spot checks on the upgraded data.
        assert len(c.get("/api/plan").json()["nights"]) == 365
        names = [r["name"] for r in c.get("/api/rooms").json()["rooms"]]
        assert "Wolf Den Bunk Suite" in names and "Wolf Den Suite" not in names  # renamed code, one room
        before_rename = c.get("/api/dates/2026-09-08").json()  # scraped while it was still "Wolf Den Suite"
        assert "Wolf Den Bunk Suite" in [r["name"] for r in before_rename["rooms"]]
        runs = c.get("/api/runs").json()
        assert runs["total"] == 7 and runs["runs"][0]["started_local"].endswith("5:43 AM")


def test_old_text_raw_json_still_reads_and_new_rows_are_compressed(legacy_db, sample_payload):
    conn = db.connect(legacy_db)
    try:
        old = conn.execute("SELECT raw_json FROM snapshots ORDER BY id LIMIT 1").fetchone()[0]
        assert isinstance(old, str) and db.decode_raw_json(old)["suites"]
        db.save_snapshot(conn, "2026-09-23", "2026-09-23T09:43:04+00:00",
                         parse_availability(sample_payload, date(2026, 9, 30)))
        new = conn.execute("SELECT raw_json FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert isinstance(new, bytes) and db.decode_raw_json(new)["suites"]
    finally:
        conn.close()


def test_migration_is_idempotent(legacy_db):
    db.connect(legacy_db).close()
    first_schema, first_rows = schema(legacy_db), dump(legacy_db)
    db.connect(legacy_db).close()
    db.migrate_file(legacy_db, lock_wait=0)
    assert schema(legacy_db) == first_schema and dump(legacy_db) == first_rows


def test_failed_migration_leaves_the_old_database_as_it_was(legacy_db, monkeypatch):
    before_schema, before_rows = schema(legacy_db), dump(legacy_db)
    # Fail at the very end (after the new tables, columns and indexes were created).
    monkeypatch.setattr(db, "_BACKFILLS", db._BACKFILLS + ("UPDATE no_such_table SET x = 1",))
    with pytest.raises(sqlite3.OperationalError):
        db.connect(legacy_db)
    assert schema(legacy_db) == before_schema and dump(legacy_db) == before_rows  # all or nothing
    monkeypatch.undo()
    db.connect(legacy_db).close()  # and a later start completes it
    assert "meta" in {r[1] for r in schema(legacy_db)}


def test_unfinished_backfill_is_completed_on_the_next_connect(legacy_db):
    conn = db.connect(legacy_db)
    expected = conn.execute("SELECT max_rooms_seen, min_rate_seen FROM suites WHERE suite_code = 'SFSSB'").fetchone()
    conn.execute("UPDATE suites SET max_rooms_seen = NULL, min_rate_seen = NULL WHERE suite_code = 'SFSSB'")
    conn.commit()
    conn.close()
    conn = db.connect(legacy_db)
    try:
        assert conn.execute("SELECT max_rooms_seen, min_rate_seen FROM suites WHERE suite_code = 'SFSSB'").fetchone() == expected
    finally:
        conn.close()


def test_startup_migration_waits_for_a_running_sweep(legacy_db):
    held, release = threading.Event(), threading.Event()

    def sweep():
        with sweep_lock(legacy_db):
            held.set()
            release.wait(5)

    t = threading.Thread(target=sweep)
    t.start()
    assert held.wait(5)
    try:
        threading.Timer(0.3, release.set).start()
        assert db.migrate_file(legacy_db, lock_wait=5) == "migrated"  # waited for the lock
    finally:
        release.set()
        t.join(5)

    held.clear()
    release.clear()
    t = threading.Thread(target=sweep)
    t.start()
    assert held.wait(5)
    try:
        # A sweep that outlasts the wait: migrate anyway (SQLite's write lock serialises it).
        assert db.migrate_file(legacy_db, lock_wait=0.2) == "migrated while a sweep was running"
    finally:
        release.set()
        t.join(5)


def test_backup_before_upgrading(legacy_db, tmp_path, capsys):
    out = tmp_path / "backups" / "gwl.pre-redesign.db"
    assert main(["backup", "--db", str(legacy_db), "--out", str(out)]) == 0
    assert "integrity check: ok" in capsys.readouterr().out
    assert dump(out) == dump(legacy_db)
    assert schema(out) == schema(legacy_db)  # an exact copy: the backup doesn't migrate
    assert "meta" not in {r[1] for r in schema(out)}
    # Refusals: existing file without --force, the database itself, a missing source.
    assert main(["backup", "--db", str(legacy_db), "--out", str(out)]) == EXIT_USAGE
    assert main(["backup", "--db", str(legacy_db), "--out", str(out), "--force"]) == 0
    assert main(["backup", "--db", str(legacy_db), "--out", str(legacy_db)]) == EXIT_USAGE
    assert main(["backup", "--db", str(tmp_path / "nope.db"), "--out", str(tmp_path / "x.db")]) == EXIT_USAGE
