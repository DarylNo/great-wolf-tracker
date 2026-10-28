"""Read-only queries behind the dashboard pages and the JSON API.

Every function takes a read-only sqlite3 connection (row_factory = sqlite3.Row) and
returns plain dicts/lists so the same data serves both HTML templates and /api routes.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean

from gwt.metrics import add_demand

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_LATEST_PER_DATE = """
    SELECT s.id, s.run_id, s.scraped_at, s.arrival_date, s.total_rooms_available, s.suite_types,
           s.suite_types_sold_out, s.min_rate, s.ref_suite_rate, s.error_code
    FROM snapshots s
    JOIN (SELECT arrival_date, MAX(id) AS max_id FROM snapshots GROUP BY arrival_date) l
      ON s.id = l.max_id
"""


def _dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def has_data(conn: sqlite3.Connection | None) -> bool:
    if conn is None:
        return False
    try:
        return conn.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:  # tables not created yet
        return False


def max_rooms_by_mix(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute(
        "SELECT suite_types, MAX(total_rooms_available) FROM snapshots GROUP BY suite_types"
    ).fetchall()
    return {r[0]: r[1] for r in rows if r[0] is not None}


def latest_per_date(conn: sqlite3.Connection, start: date | None = None, end: date | None = None) -> list[dict]:
    """Latest snapshot for each arrival date (optionally within [start, end]), with demand."""
    sql = _LATEST_PER_DATE
    params: list[str] = []
    clauses = []
    if start:
        clauses.append("s.arrival_date >= ?")
        params.append(start.isoformat())
    if end:
        clauses.append("s.arrival_date <= ?")
        params.append(end.isoformat())
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY s.arrival_date"
    rows = _dicts(conn.execute(sql, params))
    return add_demand(rows, max_rooms_by_mix(conn))


# --------------------------------------------------------------------------- overview


def overview(conn: sqlite3.Connection, today: date, days: int = 365) -> dict:
    end = today + timedelta(days=days - 1)
    rows = latest_per_date(conn, today, end)
    by_date = {r["arrival_date"]: r for r in rows}

    first_monday = today - timedelta(days=today.weekday())
    n_weeks = (end - first_monday).days // 7 + 1
    weeks = [(first_monday + timedelta(weeks=w)).isoformat() for w in range(n_weeks)]
    z = [[None] * n_weeks for _ in range(7)]
    custom = [[None] * n_weeks for _ in range(7)]
    for w in range(n_weeks):
        for wd in range(7):
            d = first_monday + timedelta(weeks=w, days=wd)
            if d < today or d > end:
                continue
            r = by_date.get(d.isoformat())
            if r is None:
                custom[wd][w] = [d.isoformat(), "no data", "", "", ""]
                continue
            z[wd][w] = r["demand"]
            custom[wd][w] = [
                d.isoformat(),
                r["total_rooms_available"],
                r["suite_types_sold_out"],
                r["suite_types"],
                "sold out" if r["min_rate"] is None else f"${r['min_rate']:,.2f}",
            ]

    scored = [r for r in rows if r["demand"] is not None]
    quietest = sorted(scored, key=lambda r: r["demand"])[:10]
    busiest = sorted(scored, key=lambda r: r["demand"], reverse=True)[:10]
    return {
        "start": today.isoformat(),
        "end": end.isoformat(),
        "dates": rows,
        "calendar": {"weeks": weeks, "weekdays": WEEKDAYS, "z": z, "customdata": custom},
        "quietest": quietest,
        "busiest": busiest,
    }


# --------------------------------------------------------------------------- history / trends


def history(conn: sqlite3.Connection) -> dict:
    """Average demand by ISO week x year over every arrival date ever captured.

    Each arrival date contributes its latest snapshot (closest to arrival, so the best
    estimate of how full the lodge actually got).
    """
    rows = latest_per_date(conn)
    buckets: dict[tuple[int, int], list[float]] = defaultdict(list)
    for r in rows:
        if r["demand"] is None:
            continue
        iso = date.fromisoformat(r["arrival_date"]).isocalendar()
        buckets[(iso.year, iso.week)].append(r["demand"])

    years = sorted({y for y, _ in buckets})
    weeks = list(range(1, 54))
    z = [[round(mean(buckets[(y, w)]), 1) if (y, w) in buckets else None for w in weeks] for y in years]
    counts = [[len(buckets.get((y, w), [])) for w in weeks] for y in years]
    return {"years": years, "weeks": weeks, "z": z, "counts": counts}


# --------------------------------------------------------------------------- date detail


def date_detail(conn: sqlite3.Connection, arrival: date, today: date, window_days: int = 365) -> dict | None:
    d = arrival.isoformat()
    latest = conn.execute(
        "SELECT * FROM snapshots WHERE arrival_date = ? ORDER BY id DESC LIMIT 1", (d,)
    ).fetchone()
    if latest is None:
        return None
    latest = dict(latest)
    latest.pop("raw_json", None)
    # Same comparison set as the page that links here: the overview window for upcoming
    # dates, all history for past ones (the rate term is a percentile within that set).
    window_end = today + timedelta(days=window_days - 1)
    if today <= arrival <= window_end:
        scored = latest_per_date(conn, today, window_end)
    else:
        scored = latest_per_date(conn)
    latest["demand"] = next((r["demand"] for r in scored if r["arrival_date"] == d), None)

    suites = _dicts(
        conn.execute(
            """SELECT ss.id, ss.suite_code, COALESCE(ss.suite_brand, ss.suite_code) AS suite_name,
                      ss.suite_category, ss.max_occupancy, ss.is_accessible, ss.num_beds,
                      ss.available_room_count, ss.min_total_nightly_rate
               FROM suite_snapshots ss
               WHERE ss.run_id = ? AND ss.arrival_date = ?
               ORDER BY ss.sort_order IS NULL, ss.sort_order, ss.suite_code""",
            (latest["run_id"], d),
        )
    )
    families: list[str] = []
    fam_rows = conn.execute(
        """SELECT rf.suite_snapshot_id, rf.family_title, rf.min_total_nightly_rate
           FROM suite_rate_families rf
           JOIN suite_snapshots ss ON ss.id = rf.suite_snapshot_id
           WHERE ss.run_id = ? AND ss.arrival_date = ?
           ORDER BY rf.display_rank IS NULL, rf.display_rank, rf.family_title""",
        (latest["run_id"], d),
    ).fetchall()
    per_suite: dict[int, dict[str, float | None]] = defaultdict(dict)
    for fr in fam_rows:
        if fr["family_title"] not in families:
            families.append(fr["family_title"])
        per_suite[fr["suite_snapshot_id"]][fr["family_title"]] = fr["min_total_nightly_rate"]
    for s in suites:
        s["rates"] = per_suite.get(s.pop("id"), {})
        s["sold_out"] = s["available_room_count"] == 0

    curve = _dicts(
        conn.execute(
            """SELECT run_id, scraped_at, total_rooms_available, suite_types_sold_out, suite_types, min_rate,
                      CAST(julianday(arrival_date) - julianday(substr(scraped_at, 1, 10)) AS INTEGER) AS days_out
               FROM snapshots WHERE arrival_date = ? ORDER BY id""",
            (d,),
        )
    )
    return {"snapshot": latest, "suites": suites, "families": families, "curve": curve}


# --------------------------------------------------------------------------- suites


def suites_list(conn: sqlite3.Connection) -> list[dict]:
    latest_run = conn.execute("SELECT run_id FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    cutoff = None
    if latest_run:
        cutoff = conn.execute(
            "SELECT MIN(scraped_at) FROM snapshots WHERE run_id = ?", (latest_run[0],)
        ).fetchone()[0]
    rows = _dicts(
        conn.execute(
            """SELECT suite_code, suite_brand, suite_category, max_occupancy, is_accessible, first_seen, last_seen
               FROM suites ORDER BY suite_category, suite_brand, suite_code"""
        )
    )
    has_earlier = cutoff is not None and conn.execute(
        "SELECT 1 FROM snapshots WHERE scraped_at < ? LIMIT 1", (cutoff,)
    ).fetchone() is not None
    for r in rows:
        r["in_latest_run"] = cutoff is not None and r["last_seen"] >= cutoff
        # Only meaningful once there is an earlier run to compare against.
        r["new_in_latest_run"] = has_earlier and r["first_seen"] >= cutoff
    return rows


def suite_detail(conn: sqlite3.Connection, code: str, today: date, curve_date: date | None = None) -> dict | None:
    suite = conn.execute("SELECT * FROM suites WHERE suite_code = ?", (code,)).fetchone()
    if suite is None:
        return None
    upcoming = _dicts(
        conn.execute(
            """SELECT ss.arrival_date, ss.available_room_count, ss.min_total_nightly_rate, ss.scraped_at
               FROM suite_snapshots ss
               JOIN (SELECT arrival_date, MAX(id) AS max_id FROM suite_snapshots
                     WHERE suite_code = ? AND arrival_date >= ? GROUP BY arrival_date) l
                 ON ss.id = l.max_id
               ORDER BY ss.arrival_date""",
            (code, today.isoformat()),
        )
    )
    curve_dates = [
        r[0]
        for r in conn.execute(
            """SELECT arrival_date FROM suite_snapshots WHERE suite_code = ? AND arrival_date >= ?
               GROUP BY arrival_date ORDER BY arrival_date""",
            (code, today.isoformat()),
        )
    ]
    chosen = curve_date.isoformat() if curve_date else (curve_dates[0] if curve_dates else None)
    curve = []
    if chosen:
        curve = _dicts(
            conn.execute(
                """SELECT scraped_at, available_room_count, min_total_nightly_rate,
                          CAST(julianday(arrival_date) - julianday(substr(scraped_at, 1, 10)) AS INTEGER) AS days_out
                   FROM suite_snapshots WHERE suite_code = ? AND arrival_date = ? ORDER BY id""",
                (code, chosen),
            )
        )
    return {
        "suite": dict(suite),
        "upcoming": upcoming,
        "curve_date": chosen,
        "curve_dates": curve_dates,
        "curve": curve,
    }


# --------------------------------------------------------------------------- runs


def runs(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    try:
        return _dicts(conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)))
    except sqlite3.OperationalError:
        return []


def last_completed_run(conn: sqlite3.Connection | None) -> dict | None:
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT run_id, finished_at, fetched FROM runs WHERE status = 'completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None
