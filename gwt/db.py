"""SQLite storage. Snapshot tables are append-only: every run adds rows, nothing is overwritten.

The `suites` lookup table is the one exception: it is upserted on every save so it always
reflects the latest known description of each suite type plus when it was first/last seen.
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path

from gwt.collector import Snapshot
from gwt.metrics import lead_days, scrape_local_date

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
    raw_json                TEXT,               -- pricingKey stripped; zlib BLOB (older rows: plain TEXT)
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
    last_seen               TEXT    NOT NULL,   -- scraped_at of latest sighting
    max_rooms_seen          INTEGER,            -- most rooms ever available (inventory proxy)
    min_rate_seen           REAL                -- lowest nightly rate ever seen
);

-- Key/value facts about the database itself (e.g. synthetic_demo = 1 for `gwt demo-db`).
CREATE TABLE IF NOT EXISTS meta (
    key                     TEXT PRIMARY KEY,
    value                   TEXT
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
CREATE INDEX IF NOT EXISTS idx_suite_snapshots_snapshot ON suite_snapshots (arrival_date, run_id);
CREATE INDEX IF NOT EXISTS idx_suite_snapshots_code ON suite_snapshots (suite_code, arrival_date);
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
    "suites": {
        "max_rooms_seen": "INTEGER",
        "min_rate_seen": "REAL",
    },
}

# Idempotent backfills, re-run on every connect: they only touch rows still NULL, so they
# finish an interrupted earlier migration and cost nothing once done.
_BACKFILLS = (
    """UPDATE suites SET max_rooms_seen = (
           SELECT MAX(available_room_count) FROM suite_snapshots ss WHERE ss.suite_code = suites.suite_code)
       WHERE max_rooms_seen IS NULL""",
    """UPDATE suites SET min_rate_seen = (
           SELECT MIN(min_total_nightly_rate) FROM suite_snapshots ss WHERE ss.suite_code = suites.suite_code)
       WHERE min_rate_seen IS NULL""",
)


def _statements(script: str) -> list[str]:
    """Split a SQL script into complete statements (comments may contain ';')."""
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf)
            buf = ""
    return out


SCHEMA_STATEMENTS = _statements(SCHEMA)


def migrate(conn: sqlite3.Connection) -> None:
    """Create/upgrade the schema in one IMMEDIATE transaction: all of it applies, or none."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)
        for table, cols in _ADDED_COLUMNS.items():
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
        for sql in _BACKFILLS:
            conn.execute(sql)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Read-write connection; creates the file and schema if needed, and migrates it."""
    path = Path(path)
    conn_target = str(path)
    if conn_target != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(conn_target, timeout=30)
    if conn_target != ":memory:":
        # WAL lets the web dashboard read while a sweep is writing.
        conn.execute("PRAGMA journal_mode=WAL")
    migrate(conn)
    return conn


def migrate_file(path: str | Path, lock_wait: float = 10.0) -> str:
    """Migrate a database file before serving it (web startup).

    Takes the sweep lock (waiting at most `lock_wait` seconds) so a migration never runs
    alongside a sweep from this machine. If a sweep holds the lock longer, that sweep has
    already migrated the file on connect (same code), and the migration below is a no-op
    that SQLite's own write lock serialises with it, so startup never deadlocks.
    """
    from gwt.locking import SweepBusy, sweep_lock

    try:
        with sweep_lock(path, wait=lock_wait):
            connect(path).close()
            return "migrated"
    except SweepBusy:
        connect(path).close()
        return "migrated while a sweep was running"


# --------------------------------------------------------------------------- raw JSON


def encode_raw_json(text: str | None) -> bytes | None:
    """raw_json is stored zlib-compressed (a BLOB); older rows are plain TEXT."""
    return None if text is None else zlib.compress(text.encode("utf-8"), 6)


def decode_raw_json(value: bytes | str | None) -> dict | None:
    """Read raw_json from either format (compressed BLOB or older plain TEXT)."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = zlib.decompress(bytes(value)).decode("utf-8")
    return json.loads(value)


# --------------------------------------------------------------------------- backup


def backup(path: str | Path, out: str | Path) -> dict:
    """Consistent copy of a live database (safe with WAL) using SQLite's backup API.

    The source is opened with plain sqlite3 (no migration), so a backup taken before an
    upgrade is an exact copy of the old schema.
    """
    path, out = Path(path), Path(out)
    if not path.exists():
        raise FileNotFoundError(f"no database at {path}")
    out.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(path), timeout=30)
    dst = sqlite3.connect(str(out))
    try:
        src.backup(dst)
        check = dst.execute("PRAGMA quick_check").fetchone()[0]
        tables = {r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        counts = {t: dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("snapshots", "suite_snapshots", "runs") if t in tables}
    finally:
        dst.close()
        src.close()
    return {"quick_check": check, "bytes": out.stat().st_size, **counts}


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


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def collection_pause(conn: sqlite3.Connection) -> dict | None:
    """Collection is paused from a blocked (HTTP 403) run until `gwt unblock` acknowledges it, or
    until a run started after it completes. A later run that aborts, errors or is interrupted
    (e.g. a failed `gwt sweep --force`) does not lift the pause."""
    try:
        acknowledged = int(get_meta(conn).get("unblocked_run_id") or 0)
    except ValueError:
        acknowledged = 0
    try:
        block = conn.execute(
            """SELECT id, run_id, started_at, finished_at, message FROM runs
               WHERE blocked = 1 AND id > ? ORDER BY id DESC LIMIT 1""",
            (acknowledged,),
        ).fetchone()
        if block is None:
            return None
        completed_after = conn.execute(
            "SELECT 1 FROM runs WHERE id > ? AND status = 'completed' LIMIT 1", (block[0],)
        ).fetchone()
    except sqlite3.OperationalError:  # no runs table yet
        return None
    if completed_after:
        return None
    return {
        "run_row": block[0],
        "run_id": block[1],
        "started_at": block[2],
        "finished_at": block[3],
        "message": block[4],
        "date": scrape_local_date(block[2]).isoformat(),
    }


def pause_message(pause: dict) -> str:
    return (f"Collection paused: blocked (403) on {pause['date']}, "
            "run `gwt unblock` after reviewing.")


def acknowledge_block(conn: sqlite3.Connection) -> dict | None:
    """`gwt unblock`: let sweeps run again after a reviewed 403. Returns the cleared pause."""
    pause = collection_pause(conn)
    if pause is not None:
        set_meta(conn, "unblocked_run_id", str(pause["run_row"]))
    return pause


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
                encode_raw_json(snap.raw_json),
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
                           is_accessible, first_seen, last_seen, max_rooms_seen, min_rate_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (suite_code) DO UPDATE SET
                           suite_brand    = COALESCE(excluded.suite_brand, suites.suite_brand),
                           suite_category = COALESCE(excluded.suite_category, suites.suite_category),
                           max_occupancy  = COALESCE(excluded.max_occupancy, suites.max_occupancy),
                           is_accessible  = COALESCE(excluded.is_accessible, suites.is_accessible),
                           first_seen     = MIN(suites.first_seen, excluded.first_seen),
                           last_seen      = MAX(suites.last_seen, excluded.last_seen),
                           max_rooms_seen = MAX(COALESCE(suites.max_rooms_seen, excluded.max_rooms_seen),
                                                COALESCE(excluded.max_rooms_seen, suites.max_rooms_seen)),
                           min_rate_seen  = MIN(COALESCE(suites.min_rate_seen, excluded.min_rate_seen),
                                                COALESCE(excluded.min_rate_seen, suites.min_rate_seen))""",
                    (
                        s.suite_code,
                        s.suite_brand,
                        s.suite_category,
                        s.max_occupancy,
                        s.is_accessible,
                        scraped_at,
                        scraped_at,
                        s.available_room_count,
                        s.min_total_nightly_rate,
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
    """Booking curve: one row per scrape run for this suite and arrival date, oldest first.

    `days_out` is lead_days: arrival minus the scrape date in Toronto (see gwt.metrics).
    """
    cur = conn.execute(
        """SELECT ss.run_id, ss.scraped_at, ss.available_room_count, ss.min_total_nightly_rate
           FROM suite_snapshots ss
           WHERE ss.arrival_date = ? AND ss.suite_code = ?
           ORDER BY ss.scraped_at, ss.id""",
        (arrival_date, suite_code),
    )
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for row in rows:
        row["days_out"] = lead_days(arrival_date, row["scraped_at"])
    return rows


def get_meta(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        return {k: v for k, v in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.OperationalError:  # older/readonly db without the table yet
        return {}


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
