"""SQLite storage. Append-only: every run adds rows, nothing is overwritten."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from gwt.collector import Snapshot

DEFAULT_DB_PATH = Path("data/gwl.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT    NOT NULL,   -- groups one sweep; used for resume
    scraped_at              TEXT    NOT NULL,   -- UTC ISO-8601 timestamp
    arrival_date            TEXT    NOT NULL,   -- YYYY-MM-DD
    total_rooms_available   INTEGER,
    suite_types             INTEGER,
    suite_types_sold_out    INTEGER,
    min_rate                REAL,
    ref_suite_rate          REAL,               -- SFSSB
    error_code              TEXT,
    raw_json                TEXT,               -- pricingKey stripped
    UNIQUE (run_id, arrival_date)
);

CREATE TABLE IF NOT EXISTS suite_snapshots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT    NOT NULL,
    scraped_at              TEXT    NOT NULL,
    arrival_date            TEXT    NOT NULL,
    suite_code              TEXT    NOT NULL,
    suite_category          TEXT,
    available_room_count    INTEGER,
    min_total_nightly_rate  REAL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_arrival ON snapshots (arrival_date, scraped_at);
CREATE INDEX IF NOT EXISTS idx_suite_snapshots_arrival ON suite_snapshots (arrival_date, suite_code, scraped_at);
"""


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    return conn


def captured_dates(conn: sqlite3.Connection, run_id: str) -> set[str]:
    rows = conn.execute("SELECT arrival_date FROM snapshots WHERE run_id = ?", (run_id,))
    return {r[0] for r in rows}


def save_snapshot(conn: sqlite3.Connection, run_id: str, scraped_at: str, snap: Snapshot) -> None:
    """Insert one snapshot and its suite rows atomically."""
    with conn:
        conn.execute(
            """INSERT INTO snapshots (run_id, scraped_at, arrival_date, total_rooms_available,
                   suite_types, suite_types_sold_out, min_rate, ref_suite_rate, error_code, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                scraped_at,
                snap.arrival_date,
                snap.total_rooms_available,
                snap.suite_types,
                snap.suite_types_sold_out,
                snap.min_rate,
                snap.ref_suite_rate,
                snap.error_code,
                snap.raw_json,
            ),
        )
        conn.executemany(
            """INSERT INTO suite_snapshots (run_id, scraped_at, arrival_date, suite_code,
                   suite_category, available_room_count, min_total_nightly_rate)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    run_id,
                    scraped_at,
                    snap.arrival_date,
                    s.suite_code,
                    s.suite_category,
                    s.available_room_count,
                    s.min_total_nightly_rate,
                )
                for s in snap.suites
            ],
        )
