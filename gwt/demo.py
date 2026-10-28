"""Synthetic demo database (`python -m gwt demo-db --out PATH`).

Clearly synthetic but shaped like the real thing, so the dashboard can be developed and
reviewed without live data:

- ~60 weekly full sweeps (Sundays 03:17, 365 days) plus daily 30-day sweeps (05:43),
  scraped_at in UTC exactly like the collector writes it;
- 27 suite codes grouped into 17 rooms (accessible / bedding variants share a name);
- rooms decline toward arrival; Fri/Sat nights, summer, and school-break / holiday nights
  fill earlier and higher; cheaper suites sell out first, accessible variants last;
- prices rise with expected demand and as rooms fill.

The database is marked with meta.synthetic_demo = 1 and the dashboard shows a banner for it.
Raw API JSON is not generated (raw_json holds a small marker instead).
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable

from gwt import db
from gwt.events import ClosureCalendar
from gwt.metrics import LODGE_TZ

RAW_JSON_MARKER = '{"synthetic_demo":true}'


@dataclass(frozen=True)
class DemoSuite:
    code: str
    name: str
    category: str
    capacity: int
    accessible: bool
    beds: int
    baths: int
    max_occupancy: int
    base_rate: float  # Saver rate on a quiet night


def _suites() -> list[DemoSuite]:
    s = DemoSuite
    return [
        s("SFSSB", "Family Suite", "standard", 96, False, 2, 1, 6, 279),
        s("SFSSA", "Family Suite", "standard", 8, True, 2, 1, 6, 279),
        s("SQSSB", "Queen Sofa Suite", "standard", 38, False, 2, 1, 6, 299),
        s("SQSSA", "Queen Sofa Suite", "standard", 4, True, 2, 1, 4, 299),
        s("SDFSB", "Deluxe Family Suite", "standard", 30, False, 2, 1, 6, 319),
        s("SDQSB", "Double Queen Suite", "standard", 24, False, 2, 1, 4, 269),
        s("SFPSB", "Fireplace Suite", "standard", 18, False, 1, 1, 4, 309),
        s("TKKSB", "KidKamp Suite", "themed", 34, False, 3, 1, 6, 339),
        s("TKKSA", "KidKamp Suite", "themed", 3, True, 3, 1, 6, 339),
        s("TWDSB", "Wolf Den Suite", "themed", 30, False, 3, 1, 6, 349),
        s("TWDSA", "Wolf Den Suite", "themed", 3, True, 3, 1, 6, 349),
        s("TDKSB", "Deluxe KidCabin Suite", "themed", 20, False, 3, 1, 8, 379),
        s("TDKSA", "Deluxe KidCabin Suite", "themed", 2, True, 3, 1, 6, 379),
        s("TKCSB", "King Cabin Suite", "themed", 15, False, 2, 1, 6, 359),
        s("TKCSA", "King Cabin Suite", "themed", 2, True, 2, 1, 6, 359),
        s("PLFSB", "Loft Fireplace Suite", "premium", 12, False, 3, 2, 8, 429),
        s("PLFSA", "Loft Fireplace Suite", "premium", 2, True, 3, 2, 8, 429),
        s("PMBSB", "Majestic Bear Suite", "premium", 8, False, 3, 2, 8, 489),
        s("PGBSB", "Grizzly Bear Suite", "premium", 6, False, 4, 2, 10, 549),
        s("PGRSB", "Grand Bear Suite", "premium", 5, False, 4, 2, 10, 599),
        s("PNLSB", "Northern Lights Suite", "premium", 4, False, 4, 2, 10, 649),
        s("PPRSB", "Presidential Suite", "premium", 2, False, 4, 3, 12, 899),
        s("CTW2B", "Timber Wolf Cottage", "cottage", 3, False, 4, 2, 10, 699),
        s("CTW3B", "Timber Wolf Cottage", "cottage", 3, False, 5, 2, 12, 799),
        s("CTW4B", "Timber Wolf Cottage", "cottage", 2, False, 6, 3, 14, 899),
        s("CTWAA", "Timber Wolf Cottage", "cottage", 1, True, 4, 2, 10, 699),
        s("CLKSB", "Lakeside Cottage", "cottage", 2, False, 5, 3, 12, 849),
    ]


DEMO_SUITES = _suites()

_MASK = (1 << 64) - 1


def _noise(*keys: int) -> float:
    """Deterministic pseudo-random number in [0, 1) from integer keys (splitmix64-style)."""
    x = 0x9E3779B97F4A7C15
    for k in keys:
        x = (x ^ (k & _MASK)) * 0xBF58476D1CE4E5B9 & _MASK
        x ^= x >> 31
        x = x * 0x94D049BB133111EB & _MASK
        x ^= x >> 29
    return (x >> 11) / float(1 << 53)


@dataclass
class _DateModel:
    demand: float  # 0..1, expected demand for the night
    final: float  # share of rooms sold by arrival
    tau: float  # booking-curve half-point in days; busier nights fill earlier
    price: float  # base price multiplier


def _date_model(d: date, closure: dict[date, set[str]], seed: int) -> _DateModel:
    doy = d.timetuple().tm_yday
    summer = 0.5 + 0.5 * math.cos(2 * math.pi * (doy - 205) / 365.25)  # peaks late July
    demand = 0.12 + 0.32 * summer
    wd = d.weekday()
    demand += {5: 0.50, 4: 0.33, 6: 0.03}.get(wd, 0.0)
    if (d.month == 12 and d.day >= 20) or (d.month == 1 and d.day <= 2):
        demand += 0.30  # holiday season
    if d.month == 3 and 10 <= d.day <= 22:
        demand += 0.12  # typical March-break weeks, even without a calendar entry
    if d.month == 10 and d.day >= 24 and wd in (4, 5):
        demand += 0.30  # Halloween weekends sell out
    regions = closure.get(d, set())
    if "ON" in regions:
        demand += 0.28
    if "NY" in regions:
        demand += 0.12
    demand += (_noise(seed, d.toordinal(), 1) - 0.5) * 0.10
    demand = min(max(demand, 0.05), 1.0)
    final = min(max(0.22 + 0.88 * demand + (_noise(seed, d.toordinal(), 2) - 0.5) * 0.08, 0.2), 1.0)
    tau = 30 + 95 * demand
    price = 0.95 + 1.15 * demand + (_noise(seed, d.toordinal(), 3) - 0.5) * 0.08
    return _DateModel(demand, final, tau, price)


def _sweeps(now: datetime, weeks: int, full_days: int, daily_days: int) -> list[tuple[datetime, str, int]]:
    """(local start time, trigger, days) for every sweep up to `now`, oldest first."""
    today = now.date()
    last_sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    first_sunday = last_sunday - timedelta(weeks=weeks - 1)
    sweeps = []
    d = first_sunday
    while d <= today:
        if d.weekday() == 6:
            start = datetime.combine(d, time(3, 17), LODGE_TZ)
            if start <= now:
                sweeps.append((start, "schedule:weekly", full_days))
        else:
            start = datetime.combine(d, time(5, 43), LODGE_TZ)
            if start <= now:
                sweeps.append((start, "schedule:daily", daily_days))
        d += timedelta(days=1)
    return sweeps


def is_demo_db(path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        return db.get_meta(conn).get("synthetic_demo") == "1"
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def build_demo_db(
    out: Path,
    *,
    weeks: int = 60,
    now: datetime | None = None,
    seed: int = 7,
    full_days: int = 365,
    daily_days: int = 30,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Write a fresh synthetic database at `out` (which must not exist). Returns counts."""
    out = Path(out)
    if out.exists():
        raise FileExistsError(f"{out} already exists")
    now = (now or datetime.now(LODGE_TZ)).astimezone(LODGE_TZ)
    sweeps = _sweeps(now, weeks, full_days, daily_days)
    if not sweeps:
        raise ValueError("no sweeps fall before `now`; increase weeks")

    first_day = sweeps[0][0].date()
    last_day = sweeps[-1][0].date() + timedelta(days=full_days)
    cal = ClosureCalendar.for_range(first_day, last_day)
    closure: dict[date, set[str]] = {}
    for night, flags in cal.flagged_nights(first_day, last_day).items():
        closure[night] = {f.region for f in flags}

    suites = DEMO_SUITES
    order = sorted(range(len(suites)), key=lambda i: suites[i].base_rate)
    k = [0.0] * len(suites)
    for rank, i in enumerate(order):
        k[i] = 3.0 - 2.6 * rank / (len(suites) - 1)  # cheapest fill fastest
        if suites[i].accessible:
            k[i] *= 0.55  # accessible rooms are held back and sell last
    ref_index = next(i for i, s in enumerate(suites) if s.code == "SFSSB")

    models: dict[date, _DateModel] = {}
    conn = db.connect(out)
    conn.execute("PRAGMA synchronous=OFF")
    snapshot_rows, suite_rows, family_rows, run_rows = [], [], [], []
    seen: dict[str, dict] = {
        s.code: {"first": None, "last": None, "max_rooms": 0, "min_rate": None} for s in suites
    }
    snap_id = suite_id = 0

    def flush() -> None:
        conn.executemany(
            """INSERT INTO snapshots (id, run_id, scraped_at, arrival_date, total_rooms_available,
                   suite_types, suite_types_sold_out, min_rate, ref_suite_rate, error_code, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            snapshot_rows,
        )
        conn.executemany(
            """INSERT INTO suite_snapshots (id, run_id, scraped_at, arrival_date, suite_code, suite_category,
                   available_room_count, min_total_nightly_rate, suite_brand, base_occupancy, max_occupancy,
                   is_accessible, num_beds, num_baths, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 4, ?, ?, ?, ?, ?)""",
            suite_rows,
        )
        conn.executemany(
            """INSERT INTO suite_rate_families (suite_snapshot_id, family_title, policy_type, display_rank,
                   min_total_nightly_rate) VALUES (?, ?, ?, ?, ?)""",
            family_rows,
        )
        snapshot_rows.clear()
        suite_rows.clear()
        family_rows.clear()

    for n_sweep, (start, trigger, days) in enumerate(sweeps):
        start_utc = start.astimezone(timezone.utc)
        run_id = start_utc.date().isoformat()
        scraped = start_utc
        for i in range(days):
            arrival = start.date() + timedelta(days=i)
            scraped = start_utc + timedelta(seconds=round(i * 4.1))
            scraped_at = scraped.isoformat(timespec="seconds")
            lead = i
            m = models.get(arrival)
            if m is None:
                m = models[arrival] = _date_model(arrival, closure, seed)
            booked = m.final / (1 + (lead / m.tau) ** 1.6) if lead > 0 else m.final
            o = arrival.toordinal()
            snap_id += 1
            total = sold = 0
            min_rate = ref_rate = None
            price_mult = m.price + 0.45 * booked * booked
            for si, s in enumerate(suites):
                expected = s.capacity * (1 - booked) ** k[si]
                # Small per-date and per-scrape wobble, shrinking near sell-out so the
                # cheapest-first order stays visible on busy nights.
                wobble = (_noise(seed, o, si, 7) - 0.5) * 2.0 + (_noise(seed, o, si, lead) - 0.5) * 1.0
                left = expected + min(1.0, 0.3 * expected) * wobble
                rooms = 0 if left < 0.6 else min(s.capacity, max(0, round(left)))
                suite_id += 1
                rate = None
                if rooms > 0:
                    saver = math.floor(s.base_rate * price_mult * (1 + (_noise(seed, o, si, 9) - 0.5) * 0.04)) + 0.99
                    flexible = math.floor(saver * 1.12 + 15) + 0.99
                    rate = saver
                    family_rows.append((suite_id, "Saver", "NONREF", 1, saver))
                    family_rows.append((suite_id, "Flexible", "FLEX", 2, flexible))
                    if min_rate is None or saver < min_rate:
                        min_rate = saver
                    if si == ref_index:
                        ref_rate = saver
                else:
                    sold += 1
                total += rooms
                suite_rows.append(
                    (
                        suite_id, run_id, scraped_at, arrival.isoformat(), s.code, s.category, rooms, rate,
                        s.name, s.max_occupancy, int(s.accessible), s.beds, s.baths, si + 1,
                    )
                )
                info = seen[s.code]
                if info["first"] is None:
                    info["first"] = scraped_at
                info["last"] = scraped_at
                info["max_rooms"] = max(info["max_rooms"], rooms)
                if rate is not None and (info["min_rate"] is None or rate < info["min_rate"]):
                    info["min_rate"] = rate
            snapshot_rows.append(
                (snap_id, run_id, scraped_at, arrival.isoformat(), total, len(suites), sold, min_rate, ref_rate,
                 RAW_JSON_MARKER)
            )
        run_rows.append(
            (run_id, trigger, start_utc.isoformat(timespec="seconds"), scraped.isoformat(timespec="seconds"),
             start.date().isoformat(), days, days)
        )
        if len(suite_rows) > 60_000:
            flush()
        if progress and (n_sweep % 50 == 0 or n_sweep == len(sweeps) - 1):
            progress(f"sweep {n_sweep + 1}/{len(sweeps)} ({run_id}, {trigger})")
    flush()

    conn.executemany(
        """INSERT INTO runs (run_id, trigger, started_at, finished_at, start_date, days, fetched, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'completed')""",
        run_rows,
    )
    conn.executemany(
        """INSERT INTO suites (suite_code, suite_brand, suite_category, max_occupancy, is_accessible,
               first_seen, last_seen, max_rooms_seen, min_rate_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (s.code, s.name, s.category, s.max_occupancy, int(s.accessible), seen[s.code]["first"],
             seen[s.code]["last"], seen[s.code]["max_rooms"], seen[s.code]["min_rate"])
            for s in suites
        ],
    )
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [
            ("synthetic_demo", "1"),
            ("generated_at", datetime.now(timezone.utc).isoformat(timespec="seconds")),
            ("demo_now", now.isoformat(timespec="seconds")),
            ("demo_weeks", str(weeks)),
            ("demo_seed", str(seed)),
        ],
    )
    conn.commit()
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("ANALYZE")
    conn.commit()
    conn.close()
    return {"sweeps": len(sweeps), "snapshots": snap_id, "suite_snapshots": suite_id}
