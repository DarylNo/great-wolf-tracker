"""SQLite storage. Snapshot tables are append-only: every run adds rows, nothing is overwritten.

The `suites` lookup table is the one exception: it is upserted on every save so it always
reflects the latest known description of each suite type plus when it was first/last seen.
"""

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
    min_total_nightly_rate  REAL,
    suite_brand             TEXT,
    base_occupancy          INTEGER,
    max_occupancy           INTEGER,
    is_accessible           INTEGER,            -- isAccessible: 1/0, NULL if missing
    num_beds                INTEGER,            -- numOfBeds (string in API), NULL if unparseable
    sort_order              INTEGER,
    num_baths               INTEGER             -- numOfBaths (string in API), NULL if unparseable
);

-- Cheapest rate per rate family (Saver, Flexible, ...) for each suite snapshot.
CREATE TABLE IF NOT EXISTS suite_rate_families (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    suite_snapshot_id       INTEGER NOT NULL REFERENCES suite_snapshots (id),
    family_title            TEXT    NOT NULL,
    policy_type             TEXT,
    display_rank            INTEGER,
    min_total_nightly_rate  REAL
);

-- Latest description of every suite type ever seen.
CREATE TABLE IF NOT EXISTS suites (
    suite_code              TEXT PRIMARY KEY,
    suite_brand             TEXT,
    suite_category          TEXT,
    max_occupancy           INTEGER,
    is_accessible           INTEGER,
    first_seen              TEXT    NOT NULL,   -- scraped_at of first sighting
    last_seen               TEXT    NOT NULL    -- scraped_at of latest sighting
);

-- One row per sweep invocation (a resumed run_id gets a new row).
CREATE TABLE IF NOT EXISTS runs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT    NOT NULL,
    trigger                 TEXT,               -- cli / schedule:weekly / schedule:daily
    started_at              TEXT    NOT NULL,
    finished_at             TEXT,
    start_date              TEXT,               -- first arrival date requested
    days                    INTEGER,
    fetched                 INTEGER NOT NULL DEFAULT 0,
    skipped                 INTEGER NOT NULL DEFAULT 0,   -- already captured under this run_id
    failed_dates            INTEGER NOT NULL DEFAULT 0,
    failures                INTEGER NOT NULL DEFAULT 0,   -- failed requests incl. retried ones
    status                  TEXT    NOT NULL,   -- running / completed / aborted / error / interrupted
    blocked                 INTEGER NOT NULL DEFAULT 0,   -- 1 if stopped by HTTP 403
    message                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_snapshots_arrival ON snapshots (arrival_date, scraped_at);
CREATE INDEX IF NOT EXISTS idx_suite_snapshots_arrival ON suite_snapshots (arrival_date, suite_code, scraped_at);
CREATE INDEX IF NOT EXISTS idx_suite_rate_families_parent ON suite_rate_families (suite_snapshot_id);
"""

# Columns added after the first schema version; applied to older databases on connect.
_ADDED_COLUMNS = {
    "suite_snapshots": {
        "suite_brand": "TEXT",
        "base_occupancy": "INTEGER",
        "max_occupancy": "INTEGER",
        "is_accessible": "INTEGER",
        "num_beds": "INTEGER",
        "sort_order": "INTEGER",
        "num_baths": "INTEGER",
    },
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _ADDED_COLUMNS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    conn.commit()


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Read-write connection; creates the file and schema if needed."""
    path = Path(path)
    conn_target = str(path)
    if conn_target != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(conn_target, timeout=30)
    if conn_target != ":memory:":
        # WAL lets the web dashboard read while a sweep is writing.
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def connect_readonly(path: str | Path) -> sqlite3.Connection | None:
    """Read-only connection for the web app, or None if the database doesn't exist yet."""
    path = Path(path)
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------- runs


def start_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    trigger: str,
    started_at: str,
    start_date: str,
    days: int,
    skipped: int,
) -> int:
    with conn:
        cur = conn.execute(
            """INSERT INTO runs (run_id, trigger, started_at, start_date, days, skipped, status)
               VALUES (?, ?, ?, ?, ?, ?, 'running')""",
            (run_id, trigger, started_at, start_date, days, skipped),
        )
    return int(cur.lastrowid)


_RUN_FIELDS = {"finished_at", "fetched", "failed_dates", "failures", "status", "blocked", "message"}


def update_run(conn: sqlite3.Connection, run_row_id: int, **fields) -> None:
    unknown = set(fields) - _RUN_FIELDS
    if unknown:
        raise ValueError(f"unknown run fields: {unknown}")
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with conn:
        conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", (*fields.values(), run_row_id))


def mark_interrupted(conn: sqlite3.Connection, finished_at: str) -> int:
    """Close out 'running' rows left by a crashed/killed sweep. Call only while holding the sweep lock."""
    with conn:
        cur = conn.execute(
            """UPDATE runs SET status = 'interrupted', finished_at = ?,
                   message = COALESCE(message, 'process ended before the run finished')
               WHERE status = 'running'""",
            (finished_at,),
        )
    return cur.rowcount


def captured_dates(conn: sqlite3.Connection, run_id: str) -> set[str]:
    rows = conn.execute("SELECT arrival_date FROM snapshots WHERE run_id = ?", (run_id,))
    return {r[0] for r in rows}


def save_snapshot(conn: sqlite3.Connection, run_id: str, scraped_at: str, snap: Snapshot) -> None:
    """Insert one snapshot, its suite rows and rate-family rows, and upsert `suites`, atomically."""
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
        for s in snap.suites:
            cur = conn.execute(
                """INSERT INTO suite_snapshots (run_id, scraped_at, arrival_date, suite_code,
                       suite_category, available_room_count, min_total_nightly_rate, suite_brand,
                       base_occupancy, max_occupancy, is_accessible, num_beds, num_baths, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    scraped_at,
                    snap.arrival_date,
                    s.suite_code,
                    s.suite_category,
                    s.available_room_count,
                    s.min_total_nightly_rate,
                    s.suite_brand,
                    s.base_occupancy,
                    s.max_occupancy,
                    s.is_accessible,
                    s.num_beds,
                    s.num_baths,
                    s.sort_order,
                ),
            )
            conn.executemany(
                """INSERT INTO suite_rate_families (suite_snapshot_id, family_title, policy_type,
                       display_rank, min_total_nightly_rate)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (cur.lastrowid, f.title, f.policy_type, f.display_rank, f.min_total_nightly_rate)
                    for f in s.rate_families
                ],
            )
            if s.suite_code:
                conn.execute(
                    """INSERT INTO suites (suite_code, suite_brand, suite_category, max_occupancy,
                           is_accessible, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (suite_code) DO UPDATE SET
                           suite_brand    = COALESCE(excluded.suite_brand, suites.suite_brand),
                           suite_category = COALESCE(excluded.suite_category, suites.suite_category),
                           max_occupancy  = COALESCE(excluded.max_occupancy, suites.max_occupancy),
                           is_accessible  = COALESCE(excluded.is_accessible, suites.is_accessible),
                           first_seen     = MIN(suites.first_seen, excluded.first_seen),
                           last_seen      = MAX(suites.last_seen, excluded.last_seen)""",
                    (
                        s.suite_code,
                        s.suite_brand,
                        s.suite_category,
                        s.max_occupancy,
                        s.is_accessible,
                        scraped_at,
                        scraped_at,
                    ),
                )


# --------------------------------------------------------------------------- queries


def latest_suites_for_date(conn: sqlite3.Connection, arrival_date: str) -> tuple[str | None, list[dict]]:
    """Suite rows from the most recent snapshot of `arrival_date`. Returns (scraped_at, rows)."""
    latest = conn.execute(
        "SELECT run_id, scraped_at FROM snapshots WHERE arrival_date = ? ORDER BY scraped_at DESC, id DESC LIMIT 1",
        (arrival_date,),
    ).fetchone()
    if latest is None:
        return None, []
    run_id, scraped_at = latest
    cur = conn.execute(
        """SELECT suite_code, COALESCE(suite_brand, suite_code) AS suite_name, suite_category,
                  available_room_count, min_total_nightly_rate
           FROM suite_snapshots
           WHERE run_id = ? AND arrival_date = ?
           ORDER BY sort_order IS NULL, sort_order, suite_code""",
        (run_id, arrival_date),
    )
    cols = [c[0] for c in cur.description]
    return scraped_at, [dict(zip(cols, r)) for r in cur.fetchall()]


def suite_history(conn: sqlite3.Connection, arrival_date: str, suite_code: str) -> list[dict]:
    """Booking curve: one row per scrape run for this suite and arrival date, oldest first."""
    cur = conn.execute(
        """SELECT ss.run_id, ss.scraped_at,
                  CAST(julianday(ss.arrival_date) - julianday(substr(ss.scraped_at, 1, 10)) AS INTEGER)
                      AS days_out,
                  ss.available_room_count, ss.min_total_nightly_rate
           FROM suite_snapshots ss
           WHERE ss.arrival_date = ? AND ss.suite_code = ?
           ORDER BY ss.scraped_at, ss.id""",
        (arrival_date, suite_code),
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def rate_families_for(conn: sqlite3.Connection, arrival_date: str, suite_code: str, run_id: str) -> list[dict]:
    cur = conn.execute(
        """SELECT rf.family_title, rf.policy_type, rf.min_total_nightly_rate
           FROM suite_rate_families rf
           JOIN suite_snapshots ss ON ss.id = rf.suite_snapshot_id
           WHERE ss.arrival_date = ? AND ss.suite_code = ? AND ss.run_id = ?
           ORDER BY rf.display_rank IS NULL, rf.display_rank, rf.family_title""",
        (arrival_date, suite_code, run_id),
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
