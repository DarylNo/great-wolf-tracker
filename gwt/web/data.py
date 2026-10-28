"""Read-only data layer behind the dashboard pages and the JSON API.

Every number comes from the definitions in gwt.metrics. A per-database `Stats` object
(every snapshot with its Toronto lead time, the capacity estimate, the pace table and the
final fills) is cached and rebuilt only when new snapshots arrive (MAX(snapshots.id) changes);
while a sweep is writing, at most once a minute. Builders return plain dicts so the same data
serves the HTML templates and /api routes.

Suite codes are always shown under their catalogue room (the `suites` table's latest name for
the code), so a suite renamed by Great Wolf keeps one row, one slug and its whole history.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from typing import Callable, Iterable

from gwt import metrics as M
from gwt.events import calendar_for_range, merge_spans
from gwt.rooms import Variant, category_rank, group_rooms, slugify
from gwt.web import fmt

HORIZON_DAYS = 365
FILLING_WINDOW_DAYS = 60
LIST_LENGTH = 12
RAMP_STEPS = 5
REF_SUITE_CODE = "SFSSB"
REF_SUITE_LABEL = "Family Suite (SFSSB)"
MATRIX_WINDOWS = (30, 60, 90, 365)
MATRIX_DEFAULT_WINDOW = 90
SCATTER_WINDOWS = (30, 60, 90, 180, 365)
MONTH_LONG = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
              "October", "November", "December")

LEAD_CAPTION = (
    "Far-out dates haven't been booked yet, so rooms left mostly reflects how far away they are. "
    "Price reflects Great Wolf's own demand forecast."
)
CHEAPEST_RULE = (
    "Lowest available nightly rate first; ties go to the night with more rooms left. "
    "Next 12 months, sold-out nights left out."
)
FILLING_RULE = (
    "Next 60 days. Nights that sold rooms in the last 7 days come first, most sold first; the rest "
    "follow by pace (vs other dates at the same days-out). Sold-out nights left out."
)
STATS_REBUILD_INTERVAL_S = 60.0  # while a sweep is writing, rebuild Stats at most this often


# --------------------------------------------------------------------------- stats cache


@dataclass(slots=True)
class Snap:
    id: int
    run_id: str
    arrival: date
    scraped_at: str
    scrape_date: date
    lead: int
    rooms: int
    min_rate: float | None
    ref_rate: float | None
    suite_types: int
    sold_types: int
    pct: float | None = None
    pace: float | None = None


class Stats:
    """Everything derived from the snapshots table, for one version of the database."""

    def __init__(self, conn: sqlite3.Connection, version, final_fill_max_lead: int):
        self.version = version
        self.final_fill_max_lead = final_fill_max_lead
        parsed: dict[str, date] = {}
        snaps: list[Snap] = []
        rows = conn.execute(
            """SELECT id, run_id, arrival_date, scraped_at, total_rooms_available, min_rate, ref_suite_rate,
                      suite_types, suite_types_sold_out
               FROM snapshots WHERE COALESCE(suite_types, 0) > 0 ORDER BY id"""
        )
        for r in rows:
            arrival = parsed.get(r[2])
            if arrival is None:
                arrival = parsed[r[2]] = date.fromisoformat(r[2])
            scrape_date = M.scrape_local_date(r[3])
            snaps.append(
                Snap(r[0], r[1], arrival, r[3], scrape_date, (arrival - scrape_date).days, r[4] or 0, r[5], r[6],
                     r[7] or 0, r[8] or 0)
            )
        self.snaps = snaps
        self.capacity = M.capacity_estimate(s.rooms for s in snaps)
        self.far_dates = len({s.arrival for s in snaps if s.lead >= M.CAPACITY_FAR_LEAD})
        self.provisional = M.capacity_is_provisional(self.far_dates)
        for s in snaps:
            s.pct = M.pct_sold(s.rooms, self.capacity)
        self.pace_table = M.PaceTable((s.lead, s.pct) for s in snaps)
        for s in snaps:
            s.pace = self.pace_table.pace(s.lead, s.pct)
        self.by_date: dict[date, list[Snap]] = {}
        for s in snaps:
            self.by_date.setdefault(s.arrival, []).append(s)
        self.latest = {d: lst[-1] for d, lst in self.by_date.items()}
        self.final: dict[date, Snap] = {}
        for d, lst in self.by_date.items():
            f = M.final_fill(lst, final_fill_max_lead)
            if f is not None:
                self.final[d] = f
        self.last_scraped_at = snaps[-1].scraped_at if snaps else None
        self.built_at = time.monotonic()
        self._memo: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._bands: dict[str, list[list[float]]] = {}

    def pickup(self, s: Snap) -> int | None:
        return M.pickup_7d(self.by_date.get(s.arrival, []), s)

    def band_buckets(self, field_name: str) -> list[list[float]]:
        """Every snapshot's `rooms` or `min_rate`, sorted per lead bucket (built once per version)."""
        with self._lock:
            cached = self._bands.get(field_name)
        if cached is None:
            cached = M.bucket_values((s.lead, getattr(s, field_name)) for s in self.snaps)
            with self._lock:
                self._bands[field_name] = cached
        return cached

    MEMO_LIMIT = 24  # per-room histories are large (one row per snapshot); keep only recent ones

    def memo(self, key, fn: Callable):
        with self._lock:
            if key in self._memo:
                self._memo.move_to_end(key)
                return self._memo[key]
        value = fn()
        with self._lock:
            self._memo[key] = value
            while len(self._memo) > self.MEMO_LIMIT:
                self._memo.popitem(last=False)
        return value


_stats_cache: dict[str, Stats] = {}
_stats_lock = threading.Lock()


def has_data(conn: sqlite3.Connection | None) -> bool:
    if conn is None:
        return False
    try:
        return conn.execute("SELECT 1 FROM snapshots WHERE COALESCE(suite_types, 0) > 0 LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:  # tables not created yet
        return False


def sweep_running(conn: sqlite3.Connection) -> bool:
    """True while the latest run is still writing (status 'running')."""
    try:
        row = conn.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row) and row[0] == "running"


_build_locks: dict[str, threading.Lock] = {}


def get_stats(
    conn: sqlite3.Connection,
    key: str,
    final_fill_max_lead: int = M.DEFAULT_FINAL_FILL_MAX_LEAD,
    clock: Callable[[], float] = time.monotonic,
) -> Stats:
    """The cached Stats for this database, rebuilt when new snapshots arrive.

    During a sweep MAX(snapshots.id) changes every few seconds; rebuilding on every request
    then would mean a full rebuild per page view. So while the latest run is 'running', a Stats
    up to STATS_REBUILD_INTERVAL_S old is served as is (pages lag the sweep by up to a minute).
    """
    version = (conn.execute("SELECT MAX(id) FROM snapshots").fetchone()[0], final_fill_max_lead)

    def usable(s: Stats | None) -> bool:
        if s is None or s.version[1] != final_fill_max_lead:
            return False
        if s.version == version:
            return True
        return clock() - s.built_at < STATS_REBUILD_INTERVAL_S and sweep_running(conn)

    with _stats_lock:
        cached = _stats_cache.get(key)
        build_lock = _build_locks.setdefault(key, threading.Lock())
    if usable(cached):
        return cached
    with build_lock:  # one rebuild at a time; requests that waited reuse its result
        with _stats_lock:
            cached = _stats_cache.get(key)
        if usable(cached):
            return cached
        stats = Stats(conn, version, final_fill_max_lead)
        stats.built_at = clock()
        with _stats_lock:
            _stats_cache[key] = stats
        return stats


def is_demo(conn: sqlite3.Connection | None) -> bool:
    if conn is None:
        return False
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'synthetic_demo'").fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row) and row[0] == "1"


def local_time(scraped_at: str | None) -> str:
    """A UTC scrape timestamp as Toronto local time, e.g. 'Wed, Oct 28, 2026 3:17 AM'."""
    if not scraped_at:
        return "–"
    ts = datetime.fromisoformat(scraped_at.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    t = ts.astimezone(M.LODGE_TZ)
    hour = t.hour % 12 or 12
    return f"{fmt.date_long(t.date())} {hour}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def daterange(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# --------------------------------------------------------------------------- room catalogue


@dataclass
class CatalogVariant:
    code: str
    name: str
    category: str | None
    max_occupancy: int | None
    accessible: int | None
    first_seen: str
    last_seen: str
    max_rooms: int | None
    min_rate: float | None


@dataclass
class CatalogRoom:
    name: str
    slug: str
    category: str | None
    variants: list[CatalogVariant] = field(default_factory=list)

    @property
    def codes(self) -> list[str]:
        return [v.code for v in self.variants]

    @property
    def first_seen(self) -> str:
        return min(v.first_seen for v in self.variants)

    @property
    def last_seen(self) -> str:
        return max(v.last_seen for v in self.variants)

    @property
    def capacity(self) -> int:
        return sum(v.max_rooms or 0 for v in self.variants)

    @property
    def min_rate(self) -> float | None:
        rates = [v.min_rate for v in self.variants if v.min_rate is not None]
        return min(rates) if rates else None

    @property
    def has_accessible(self) -> bool:
        return any(v.accessible for v in self.variants)

    @property
    def occupancy_label(self) -> str:
        occ = [v.max_occupancy for v in self.variants if v.max_occupancy is not None]
        if not occ:
            return "–"
        return str(min(occ)) if min(occ) == max(occ) else f"{min(occ)}–{max(occ)}"


class Catalogue:
    def __init__(self, rooms: list[CatalogRoom]):
        self.rooms = rooms
        self.by_slug = {r.slug: r for r in rooms}
        self.by_name = {r.name: r for r in rooms}
        self.by_code = {v.code: r for r in rooms for v in r.variants}

    def slug_for(self, name: str) -> str:
        room = self.by_name.get(name)
        return room.slug if room else slugify(name)

    def room_name(self, code: str | None, snapshot_name: str | None) -> str:
        """The catalogue room a code belongs to, whatever name the snapshot row carried."""
        room = self.by_code.get(code) if code else None
        if room is not None:
            return room.name
        return snapshot_name or code or "?"

    def reference_rates(self) -> dict[str, float | None]:
        """Each room's usual price level (lowest rate ever seen), for a stable sort order."""
        return {r.name: r.min_rate for r in self.rooms}


def catalogue(conn: sqlite3.Connection, stats: Stats) -> Catalogue:
    def build() -> Catalogue:
        rows = conn.execute(
            """SELECT suite_code, COALESCE(suite_brand, suite_code), suite_category, max_occupancy, is_accessible,
                      first_seen, last_seen, max_rooms_seen, min_rate_seen FROM suites"""
        ).fetchall()
        by_name: dict[str, list[CatalogVariant]] = {}
        for r in rows:
            by_name.setdefault(r[1], []).append(CatalogVariant(*r))
        rooms, used = [], set()
        for name in sorted(by_name):
            variants = sorted(by_name[name], key=lambda v: (bool(v.accessible), v.code))
            cats = [v.category for v in variants if v.category]
            slug = slugify(name)
            while slug in used:
                slug += "-2"
            used.add(slug)
            rooms.append(CatalogRoom(name, slug, max(set(cats), key=cats.count) if cats else None, variants))
        rooms.sort(key=lambda r: (category_rank(r.category), r.min_rate if r.min_rate is not None else float("inf"),
                                  r.name.lower()))
        return Catalogue(rooms)

    return stats.memo("catalogue", build)


def latest_room_summary(conn: sqlite3.Connection, stats: Stats, start: date, end: date) -> dict[date, dict]:
    """Per arrival date (latest snapshot): rooms left and cheapest rate per catalogue room, and
    sold-out counts."""

    def build() -> dict[date, dict]:
        cat = catalogue(conn, stats)
        rows = conn.execute(
            """SELECT ss.arrival_date, ss.suite_code, ss.suite_brand, ss.available_room_count,
                      ss.min_total_nightly_rate
               FROM snapshots s
               JOIN suite_snapshots ss ON ss.arrival_date = s.arrival_date AND ss.run_id = s.run_id
               WHERE s.id IN (SELECT MAX(id) FROM snapshots
                              WHERE arrival_date BETWEEN ? AND ? AND COALESCE(suite_types, 0) > 0
                              GROUP BY arrival_date)""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        per_date: dict[date, dict[str, list]] = {}
        for arrival, code, brand, rooms, rate in rows:
            name = cat.room_name(code, brand)
            agg = per_date.setdefault(date.fromisoformat(arrival), {}).setdefault(name, [0, None])
            rooms = rooms or 0
            agg[0] += rooms
            if rooms > 0 and rate is not None and (agg[1] is None or rate < agg[1]):
                agg[1] = rate
        out = {}
        for d, rooms in per_date.items():
            out[d] = {
                "rooms": {name: (v[0], v[1]) for name, v in rooms.items()},
                "sold": sum(1 for v in rooms.values() if v[0] == 0),
                "total": len(rooms),
            }
        return out

    return stats.memo(("room_summary", start, end), build)


# --------------------------------------------------------------------------- colour scales


@dataclass
class Scale:
    breaks: list[float]
    n: int
    reverse: bool
    legend: list[dict]

    def step(self, value: float | None) -> int | None:
        if value is None:
            return None
        cls = M.class_index(value, self.breaks)
        if self.reverse:
            cls = self.n - 1 - cls
        return M.ramp_step(cls, self.n, RAMP_STEPS)


def make_scale(values: list[float], label: Callable[[float, float], str], reverse: bool = False) -> Scale:
    """Quantile classes over the values on screen, legend in real units, light -> dark."""
    breaks = M.quantile_breaks(values, RAMP_STEPS)
    ranges = M.class_ranges(values, breaks)
    n = len(ranges)
    legend = [
        {"step": M.ramp_step(n - 1 - i if reverse else i, n, RAMP_STEPS), "label": label(lo, hi)}
        for i, (lo, hi) in enumerate(ranges)
    ]
    legend.sort(key=lambda x: x["step"])  # always light -> dark
    return Scale(breaks, n, reverse, legend)


def _range(fmt_one: Callable[[float], str]) -> Callable[[float, float], str]:
    return lambda lo, hi: fmt_one(lo) if fmt_one(lo) == fmt_one(hi) else f"{fmt_one(lo)}–{fmt_one(hi)}"


# --------------------------------------------------------------------------- nights (Plan page)


def _night(stats: Stats, d: date, flags, summary: dict | None) -> dict:
    s = stats.latest.get(d)
    night = {
        "date": d.isoformat(),
        "weekday": fmt.WEEKDAY_SHORT[d.weekday()],
        "weekend": M.is_weekend_night(d),
        "has_data": s is not None,
        "school_closure": [f.text for f in flags],
        "closure": bool(flags),
    }
    if s is None:
        return night
    night.update(
        rooms_left=s.rooms,
        pct_sold=s.pct,
        min_rate=s.min_rate,
        ref_rate=s.ref_rate,
        sold_out=s.rooms == 0,
        lead_days=s.lead,
        scraped_on=s.scrape_date.isoformat(),
        pace_pct=s.pace,
        pickup_7d=stats.pickup(s),
        room_types_sold_out=summary["sold"] if summary else None,
        room_types=summary["total"] if summary else None,
    )
    return night


def night_tip(n: dict, today: date) -> str:
    """Tooltip text: first line is the title, then 'value<TAB>label' lines (values lead)."""
    d = date.fromisoformat(n["date"])
    lines = [fmt.date_long(d) + (" · today" if d == today else "")]
    if not n["has_data"]:
        lines.append("No data yet\t")
    else:
        if n["sold_out"]:
            lines.append("Sold out\t")
        lines.append(f"{n['rooms_left']}\trooms left · {fmt.pct(n['pct_sold'])} sold (est.)")
        lines.append(f"{fmt.money(n['min_rate'])}\tlowest rate" if n["min_rate"] is not None else "–\tno rate (sold out)")
        if n["ref_rate"] is not None:
            lines.append(f"{fmt.money(n['ref_rate'])}\t{REF_SUITE_LABEL}")
        if n.get("room_types") is not None:
            lines.append(f"{n['room_types_sold_out']} of {n['room_types']}\troom types sold out")
        scraped = fmt.date_short(date.fromisoformat(n["scraped_on"]))
        lines.append(f"{n['lead_days']} days\tbefore arrival at last scrape ({scraped})")
        lines.append(f"{fmt.pace(n['pace_pct'])}\tpace {M.PACE_LABEL}")
        if n["pickup_7d"] is not None:
            lines.append(f"{fmt.signed(n['pickup_7d'])}\trooms sold in the last 7 days")
    for text in n["school_closure"]:
        lines.append(f"No school next day\t{text}")
    return "\n".join(lines)


def _month_grids(today: date, nights: dict[date, dict], end: date) -> list[dict]:
    """Wall-calendar months from today's month through the month of `end`: 13 months unless
    today is the 1st, so every one of the 365 nights has a cell (the last month is partial)."""
    months = []
    y, m = today.year, today.month
    while date(y, m, 1) <= end:
        first = date(y, m, 1)
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        cells: list[dict | None] = [None] * ((first.weekday() + 1) % 7)  # Sunday-first
        for d in daterange(first, nxt - timedelta(days=1)):
            state = "past" if d < today else ("out" if d > end else "in")
            cells.append({"date": d.isoformat(), "day": d.day, "state": state, "today": d == today,
                          "night": nights.get(d) if state == "in" else None})
        months.append({"title": f"{MONTH_LONG[m - 1]} {y}", "cells": cells})
        y, m = nxt.year, nxt.month
    return months


def filling_fastest(window: list[dict], limit: int = LIST_LENGTH) -> list[dict]:
    """Nights that sold rooms in the last 7 days (pickup > 0) first, most sold first; then the
    rest (no pickup yet, or none sold / rooms released) by pace, fastest first. Sold-out
    nights and nights with neither signal are left out."""
    open_nights = [n for n in window if n["has_data"] and not n["sold_out"]]
    selling = sorted((n for n in open_nights if (n["pickup_7d"] or 0) > 0),
                     key=lambda n: (-n["pickup_7d"], -(n["pace_pct"] if n["pace_pct"] is not None else -1), n["date"]))
    rest = sorted((n for n in open_nights if (n["pickup_7d"] or 0) <= 0 and n["pace_pct"] is not None),
                  key=lambda n: (-n["pace_pct"], n["date"]))
    return (selling + rest)[:limit]


def plan(conn: sqlite3.Connection, stats: Stats, today: date) -> dict:
    end = today + timedelta(days=HORIZON_DAYS - 1)
    cal = calendar_for_range(today, end)
    summary = latest_room_summary(conn, stats, today, end)
    nights = [_night(stats, d, cal.night_flags(d), summary.get(d)) for d in daterange(today, end)]

    live = [n for n in nights if n["has_data"] and not n["sold_out"]]
    scales = {
        "price": make_scale([n["min_rate"] for n in live if n["min_rate"] is not None], _range(fmt.money0)),
        "rooms": make_scale([n["rooms_left"] for n in live], _range(lambda v: f"{v:.0f}"), reverse=True),
        "pace": make_scale([n["pace_pct"] for n in live if n["pace_pct"] is not None],
                           _range(lambda v: f"{v:.0f}")),
    }
    by_date = {}
    for n in nights:
        n["tip"] = night_tip(n, today)
        if n["has_data"]:
            n["steps"] = {
                "price": scales["price"].step(n["min_rate"]),
                "rooms": scales["rooms"].step(n["rooms_left"]),
                "pace": scales["pace"].step(n["pace_pct"]),
            }
            n["cell"] = {
                "price": fmt.compact_money(n["min_rate"]),
                "rooms": str(n["rooms_left"]),
                "pace": fmt.pace_rank(n["pace_pct"]),
            }
        by_date[date.fromisoformat(n["date"])] = n

    priced = [n for n in nights if n["has_data"] and n["min_rate"] is not None and not n["sold_out"]]
    cheapest = sorted(priced, key=lambda n: (n["min_rate"], -n["rooms_left"], n["date"]))[:LIST_LENGTH]
    filling = filling_fastest(nights[:FILLING_WINDOW_DAYS])

    scatter = [
        {
            "d": n["date"],
            "label": fmt.date_long(date.fromisoformat(n["date"])),
            "we": n["weekend"],
            "price": n["min_rate"],
            "rooms": n["rooms_left"],
            "pace": None if n["pace_pct"] is None else round(n["pace_pct"], 1),
            "ev": n["school_closure"],
            "i": i,
        }
        for i, n in enumerate(nights)
        if n["has_data"]
    ]
    sold_out_nights = sum(1 for n in nights if n["has_data"] and n["sold_out"])
    return {
        "start": today.isoformat(),
        "end": end.isoformat(),
        "nights": nights,
        "months": _month_grids(today, by_date, end),
        "scales": {k: s.legend for k, s in scales.items()},
        "cheapest": cheapest,
        "filling": filling,
        "scatter": scatter,
        "sold_out_nights": sold_out_nights,
        "nights_with_data": sum(1 for n in nights if n["has_data"]),
        "closure_nights": sum(1 for n in nights if n["closure"]),
    }


def status(stats: Stats) -> dict:
    return {
        "capacity_est": stats.capacity,
        # For sentences: "capacity estimate 412 rooms" / "... not available yet".
        "capacity_text": (f"{stats.capacity:,} rooms" if stats.capacity
                          else "not available yet (no scrape has seen a room available)"),
        "capacity_provisional": stats.provisional,
        "capacity_caveat": M.CAPACITY_CAVEAT if stats.provisional else None,
        "far_dates_seen": stats.far_dates,
        "snapshots": len(stats.snaps),
        "last_scraped_at": stats.last_scraped_at,
        "last_scrape_local": local_time(stats.last_scraped_at),
        "pace_min_snapshots": M.PACE_MIN_SNAPSHOTS,
        "final_fill_max_lead": stats.final_fill_max_lead,
    }


# --------------------------------------------------------------------------- seasonality


def _closure_spans(start: date, end: date) -> list[dict]:
    """Runs of consecutive no-school-next-day nights, each titled by its longest-running event."""
    cal = calendar_for_range(start, end)
    flagged = cal.flagged_nights(start, end)
    spans = []
    for a, b in merge_spans(flagged):
        nights_per_event: dict[str, int] = {}
        for d in daterange(a, b):
            for f in flagged.get(d, []):
                for label in f.labels:
                    key = f"{f.region}: {label}"
                    nights_per_event[key] = nights_per_event.get(key, 0) + 1
        labels = list(nights_per_event)
        title = max(labels, key=lambda k: nights_per_event[k]).replace(" (public holiday)", "")
        spans.append({"start": a.isoformat(), "end": b.isoformat(), "nights": (b - a).days + 1,
                      "title": title, "labels": labels})
    return spans


def week_label(year: int, week: int) -> str:
    """'Dec 24–Dec 31' for week 52 of 2026: the nights week_of_year puts in that week."""
    first, last = M.week_dates(year, week)
    return f"{fmt.date_short(first)}–{fmt.date_short(last)}"


def seasonality(stats: Stats, today: date) -> dict:
    finals = stats.final
    years = sorted({d.year for d in finals})
    panels = {}
    for key, title, pick in (("weekend", "Fri–Sat nights", True), ("weekday", "Sun–Thu nights", False)):
        series = []
        for y in years:
            buckets: dict[int, list[float]] = {}
            for d, s in finals.items():
                if d.year == y and M.is_weekend_night(d) == pick and s.pct is not None:
                    buckets.setdefault(M.week_of_year(d), []).append(s.pct)
            values = [round(mean(buckets[w]) * 100, 1) if w in buckets else None for w in range(1, 53)]
            counts = [len(buckets.get(w, [])) for w in range(1, 53)]
            if any(v is not None for v in values):
                series.append({"year": y, "values": values, "counts": counts})
        panels[key] = {"title": title, "series": series}

    week_spans = []
    if years:
        for span in _closure_spans(date(years[0], 1, 1), date(years[-1], 12, 31)):
            a, b = date.fromisoformat(span["start"]), date.fromisoformat(span["end"])
            if a.year != b.year:  # split a span that crosses New Year
                parts = [(a, date(a.year, 12, 31)), (date(b.year, 1, 1), b)]
            else:
                parts = [(a, b)]
            for pa, pb in parts:
                week_spans.append({"year": pa.year, "x0": M.week_position(pa) - 1 / 14,
                                   "x1": M.week_position(pb) + 1 / 14, "labels": span["labels"]})

    sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    end = today + timedelta(days=HORIZON_DAYS - 1)
    weeks = []
    for k in range(52):
        ws = sunday + timedelta(weeks=k)
        row = {"week_start": ws.isoformat(), "label": fmt.date_short(ws)}
        for key, pick in (("weekday", False), ("weekend", True)):
            rates, sold, n = [], 0, 0
            for d in daterange(ws, ws + timedelta(days=6)):
                if d < today or d > end or M.is_weekend_night(d) != pick:
                    continue
                s = stats.latest.get(d)
                if s is None:
                    continue
                n += 1
                if s.rooms == 0:
                    sold += 1
                elif s.min_rate is not None:
                    rates.append(s.min_rate)
            row[key] = round(mean(rates), 2) if rates else None
            row[f"{key}_nights"] = n
            row[f"{key}_sold_out"] = sold
        weeks.append(row)
    forecast_spans = _closure_spans(sunday, sunday + timedelta(weeks=52))
    return {
        "final_fill_nights": len(finals),
        "years": years,
        "panels": panels,
        "week_labels": {str(y): [week_label(y, w) for w in range(1, 53)] for y in years},
        "week_spans": week_spans,
        "forecast": weeks,
        "forecast_spans": forecast_spans,
        "month_ticks": [round(M.week_position(date(2001, m, 1)), 3) for m in range(1, 13)],
    }


# --------------------------------------------------------------------------- date page


def snapshot_variants(
    conn: sqlite3.Connection, run_id: str, arrival: date, cat: Catalogue | None = None
) -> tuple[list[Variant], list[str]]:
    """Suite rows of one snapshot as Variants, named by their catalogue room when `cat` is given."""
    rows = conn.execute(
        """SELECT id, suite_code, suite_brand, suite_category, available_room_count,
                  min_total_nightly_rate, is_accessible, num_beds, num_baths, max_occupancy, sort_order
           FROM suite_snapshots WHERE arrival_date = ? AND run_id = ?""",
        (arrival.isoformat(), run_id),
    ).fetchall()
    variants: dict[int, Variant] = {}
    for r in rows:
        name = cat.room_name(r[1], r[2]) if cat is not None else (r[2] or r[1])
        variants[r[0]] = Variant(code=r[1], name=name, category=r[3], rooms=r[4], min_rate=r[5], accessible=r[6],
                                 beds=r[7], baths=r[8], max_occupancy=r[9], sort_order=r[10])
    rank: dict[str, int] = {}
    if variants:
        marks = ",".join("?" * len(variants))
        for sid, title, display_rank, rate in conn.execute(
            f"""SELECT suite_snapshot_id, family_title, display_rank, min_total_nightly_rate
                FROM suite_rate_families WHERE suite_snapshot_id IN ({marks})""",
            list(variants),
        ):
            v = variants[sid]
            if rate is not None and (v.rates.get(title) is None or rate < v.rates[title]):
                v.rates[title] = rate
            rank[title] = min(rank.get(title, 1_000), display_rank if display_rank is not None else 1_000)
    families = sorted(rank, key=lambda t: (rank[t], t))
    return list(variants.values()), families


def _curve_points(snaps: Iterable, value: Callable) -> list[dict]:
    return [
        {"lead": s.lead, "value": value(s), "scraped_on": s.scrape_date.isoformat(),
         "label": fmt.date_short(s.scrape_date), "bucket": M.lead_bucket(s.lead)}
        for s in sorted(snaps, key=lambda s: (-s.lead, s.scraped_at))
        if 0 <= s.lead <= M.MAX_LEAD
    ]


def date_detail(conn: sqlite3.Connection, stats: Stats, arrival: date, today: date) -> dict | None:
    snaps = stats.by_date.get(arrival)
    if not snaps:
        return None
    s = snaps[-1]
    cat = catalogue(conn, stats)
    variants, families = snapshot_variants(conn, s.run_id, arrival, cat)
    groups = group_rooms(variants, cat.reference_rates())
    cal = calendar_for_range(arrival, arrival + timedelta(days=1))
    flags = cal.night_flags(arrival)
    rooms_rows = []
    for g in groups:
        rooms_rows.append(
            {
                "name": g.name,
                "slug": cat.slug_for(g.name),
                "category": g.category,
                "occupancy": g.occupancy_label,
                "rooms_left": g.rooms,
                "has_accessible": g.has_accessible,
                "accessible_rooms_left": g.accessible_rooms,
                "rates": {f: g.family_rate(f) for f in families},
                "cheapest": g.cheapest,
                "sold_out": g.sold_out,
                "variants": [
                    {
                        "code": v.code,
                        "accessible": v.accessible,
                        "beds": v.beds,
                        "baths": v.baths,
                        "max_occupancy": v.max_occupancy,
                        "rooms_left": v.rooms,
                        "rates": {f: (v.rates.get(f) if v.has_rooms else None) for f in families},
                        "sold_out": not v.has_rooms,
                    }
                    for v in g.variants
                ],
            }
        )
    # Bands over every other night: the pool's sorted per-bucket lists are built once per
    # Stats; only this night's own few values are taken out per request.
    own_rooms = M.bucket_values((x.lead, x.rooms) for x in snaps)
    own_rates = M.bucket_values((x.lead, x.min_rate) for x in snaps)
    return {
        "arrival_date": arrival.isoformat(),
        "label": fmt.date_long(arrival),
        "run_id": s.run_id,
        "scraped_at": s.scraped_at,
        "scraped_local": local_time(s.scraped_at),
        "rooms_left": s.rooms,
        "pct_sold": s.pct,
        "min_rate": s.min_rate,
        "ref_rate": s.ref_rate,
        "room_types_sold_out": sum(1 for g in groups if g.sold_out),
        "room_types": len(groups),
        "days_until": (arrival - today).days,
        "lead_days": s.lead,
        "pace_pct": s.pace,
        "pace_bucket_size": stats.pace_table.size(s.lead),
        "pickup_7d": stats.pickup(s),
        "sold_out": s.rooms == 0,
        "school_closure": [f.text for f in flags],
        "info_tags": cal.info_tags(arrival),
        "families": families,
        "rooms": rooms_rows,
        "curve": {
            "rooms": _curve_points(snaps, lambda x: x.rooms),
            "rate": _curve_points(snaps, lambda x: x.min_rate),
            "rooms_band": M.band_from_buckets(stats.band_buckets("rooms"), own_rooms),
            "rate_band": M.band_from_buckets(stats.band_buckets("min_rate"), own_rates),
        },
    }


# --------------------------------------------------------------------------- room types


def rooms_overview(conn: sqlite3.Connection, stats: Stats) -> list[dict]:
    cat = catalogue(conn, stats)
    cutoff = None
    if stats.snaps:
        latest_run = stats.snaps[-1].run_id
        cutoff = min(s.scraped_at for s in stats.snaps if s.run_id == latest_run)
    has_earlier = cutoff is not None and any(s.scraped_at < cutoff for s in stats.snaps)
    out = []
    for room in cat.rooms:
        out.append(
            {
                "name": room.name,
                "slug": room.slug,
                "category": room.category,
                "codes": room.codes,
                "occupancy": room.occupancy_label,
                "has_accessible": room.has_accessible,
                "capacity_seen": room.capacity,
                "lowest_rate_seen": room.min_rate,
                "first_seen": room.first_seen,
                "last_seen": room.last_seen,
                "in_latest_run": cutoff is not None and room.last_seen >= cutoff,
                "new_in_latest_run": has_earlier and room.first_seen >= cutoff,
                "variants": [
                    {"code": v.code, "accessible": v.accessible, "max_occupancy": v.max_occupancy,
                     "first_seen": v.first_seen, "last_seen": v.last_seen, "max_rooms_seen": v.max_rooms,
                     "in_latest_run": cutoff is not None and v.last_seen >= cutoff}
                    for v in room.variants
                ],
            }
        )
    return out


def room_matrix(conn: sqlite3.Connection, stats: Stats, today: date, window: int) -> dict:
    """Rooms x arrival dates: rooms left per room (latest scrape of each date)."""
    end = today + timedelta(days=window - 1)
    summary = latest_room_summary(conn, stats, today, end)
    cat = catalogue(conn, stats)
    dates = list(daterange(today, end))
    columns, months = [], []
    for d in dates:
        columns.append({"date": d.isoformat(), "day": d.day, "weekday": fmt.WEEKDAY_SHORT[d.weekday()][0],
                        "weekend": M.is_weekend_night(d), "week_end": d.weekday() == 5})
        label = f"{fmt.MONTH_SHORT[d.month - 1]} {d.year}"
        if months and months[-1]["label"] == label:
            months[-1]["span"] += 1
        else:
            months.append({"label": label, "span": 1})
    for m in months:  # a sliver of a month at the edge has no room for its label
        if m["span"] < 4:
            m["label"] = ""
    rows = []
    for room in cat.rooms:
        cap = room.capacity
        cells = []
        for d in dates:
            info = summary.get(d)
            if info is None or room.name not in info["rooms"]:
                cells.append({"state": "nd", "tip": f"{room.name}\n{fmt.date_long(d)}\nNo data\t",
                              "aria": f"{room.name}, {fmt.date_medium(d)}: no data"})
                continue
            left, rate = info["rooms"][room.name]
            if left == 0:
                state, step = "so", None
            else:
                state, step = "ok", matrix_step(left, cap)
            tip = [room.name, fmt.date_long(d)]
            tip.append("Sold out\t" if left == 0 else f"{left}\trooms left (most seen: {cap})")
            if rate is not None:
                tip.append(f"{fmt.money(rate)}\tcheapest rate")
            aria = f"{room.name}, {fmt.date_medium(d)}: " + ("sold out" if left == 0 else f"{left} left")
            cells.append({"state": state, "step": step, "rooms_left": left, "rate": rate, "tip": "\n".join(tip),
                          "aria": aria})
        rows.append({"name": room.name, "slug": room.slug, "category": room.category, "capacity": cap, "cells": cells})
    return {"window": window, "windows": MATRIX_WINDOWS, "columns": columns, "months": months, "rows": rows,
            "legend": MATRIX_LEGEND}


# Matrix colour: rooms left as a share of the most that room type has ever had available,
# in fifths; darker = fewer left. Sold out (0) has its own colour.
MATRIX_LEGEND = [
    {"step": 0, "label": "over 80%"},
    {"step": 1, "label": "61–80%"},
    {"step": 2, "label": "41–60%"},
    {"step": 3, "label": "21–40%"},
    {"step": 4, "label": "20% or less"},
]


def matrix_step(rooms_left: int, capacity: int | None) -> int:
    """0 (lightest, plenty left) .. 4 (darkest, almost gone) for a room with rooms left."""
    if not capacity:
        return RAMP_STEPS // 2
    share = min(max(rooms_left / capacity, 0.0), 1.0)
    return min(max(RAMP_STEPS - math.ceil(share * RAMP_STEPS), 0), RAMP_STEPS - 1)


@dataclass(slots=True)
class Obs:
    arrival: date
    scraped_at: str
    scrape_date: date
    lead: int
    rooms: int
    min_rate: float | None


def code_history(conn: sqlite3.Connection, codes: list[str]) -> list[Obs]:
    """One observation per snapshot for a set of codes: rooms summed, cheapest rate among codes with rooms."""
    if not codes:
        return []
    marks = ",".join("?" * len(codes))
    agg: dict[tuple[str, str], list] = {}
    for run_id, arrival, scraped_at, rooms, rate in conn.execute(
        f"""SELECT run_id, arrival_date, scraped_at, available_room_count, min_total_nightly_rate
            FROM suite_snapshots WHERE suite_code IN ({marks})""",
        codes,
    ):
        a = agg.setdefault((run_id, arrival), [scraped_at, 0, None])
        rooms = rooms or 0
        a[1] += rooms
        if rooms > 0 and rate is not None and (a[2] is None or rate < a[2]):
            a[2] = rate
        if scraped_at < a[0]:
            a[0] = scraped_at
    parsed: dict[str, date] = {}
    out = []
    for (_run, arrival), (scraped_at, rooms, rate) in agg.items():
        d = parsed.get(arrival) or parsed.setdefault(arrival, date.fromisoformat(arrival))
        sd = M.scrape_local_date(scraped_at)
        out.append(Obs(d, scraped_at, sd, (d - sd).days, rooms, rate))
    out.sort(key=lambda o: o.scraped_at)
    return out


def _history_detail(history: list[Obs], bands: dict[str, list[list[float]]], today: date,
                    curve_date: date | None, what: str) -> dict:
    """Upcoming nights plus the booking curve of one arrival date (`curve_date`, default the
    first upcoming night). The date picker always lists the date being plotted: a requested
    date with snapshots outside the upcoming list (e.g. a past night) is added to it, and one
    with no snapshots of this room falls back to the default with a note."""
    latest: dict[date, Obs] = {}
    for o in history:
        latest[o.arrival] = o
    upcoming = [
        {"date": d.isoformat(), "rooms_left": o.rooms, "cheapest_rate": o.min_rate, "weekend": M.is_weekend_night(d)}
        for d, o in sorted(latest.items())
        if today <= d < today + timedelta(days=HORIZON_DAYS)
    ]
    curve_dates = [u["date"] for u in upcoming]
    note = None
    if curve_date is not None and curve_date.isoformat() not in curve_dates:
        if curve_date in latest:
            curve_dates = sorted(curve_dates + [curve_date.isoformat()])
        else:
            note = f"No snapshots of this {what} for {fmt.date_long(curve_date)}"
            curve_date = None
    if curve_date is None and curve_dates:
        curve_date = date.fromisoformat(curve_dates[0])
        if note:
            note += f"; showing {fmt.date_long(curve_date)} instead."
    elif note:
        note += "."
    mine = [o for o in history if o.arrival == curve_date] if curve_date else []
    return {
        "upcoming": upcoming,
        "curve_date": curve_date.isoformat() if curve_date else None,
        "curve_dates": curve_dates,
        "curve_note": note,
        "curve": {
            "rooms": _curve_points(mine, lambda x: x.rooms),
            "rate": _curve_points(mine, lambda x: x.min_rate),
            "rooms_band": M.band_from_buckets(bands["rooms"], M.bucket_values((o.lead, o.rooms) for o in mine)),
            "rate_band": M.band_from_buckets(bands["rate"], M.bucket_values((o.lead, o.min_rate) for o in mine)),
        },
    }


@dataclass
class CodeHistory:
    obs: list[Obs]
    bands: dict[str, list[list[float]]]  # sorted per-bucket rooms/rate values over all of `obs`


def cached_code_history(conn: sqlite3.Connection, stats: Stats, codes: list[str]) -> CodeHistory:
    """A code set's whole history plus its band pool, built together (so the pool always holds
    exactly the observations a page takes its own date out of) and memoized per Stats."""

    def build() -> CodeHistory:
        obs = code_history(conn, codes)
        return CodeHistory(obs, {"rooms": M.bucket_values((o.lead, o.rooms) for o in obs),
                                 "rate": M.bucket_values((o.lead, o.min_rate) for o in obs)})

    return stats.memo(("history", tuple(sorted(codes))), build)


def room_detail(conn: sqlite3.Connection, stats: Stats, slug: str, today: date, curve_date: date | None) -> dict | None:
    cat = catalogue(conn, stats)
    room = cat.by_slug.get(slug)
    if room is None:
        return None
    history = cached_code_history(conn, stats, room.codes)
    detail = _history_detail(history.obs, history.bands, today, curve_date, "room")
    detail.update(
        name=room.name,
        slug=room.slug,
        category=room.category,
        codes=room.codes,
        occupancy=room.occupancy_label,
        has_accessible=room.has_accessible,
        capacity_seen=room.capacity,
        lowest_rate_seen=room.min_rate,
        first_seen=room.first_seen,
        last_seen=room.last_seen,
        variants=[
            {"code": v.code, "accessible": v.accessible, "max_occupancy": v.max_occupancy,
             "max_rooms_seen": v.max_rooms, "first_seen": v.first_seen, "last_seen": v.last_seen}
            for v in room.variants
        ],
    )
    return detail


def suite_detail(conn: sqlite3.Connection, stats: Stats, code: str, today: date, curve_date: date | None) -> dict | None:
    cat = catalogue(conn, stats)
    room = cat.by_code.get(code)
    if room is None:
        return None
    variant = next(v for v in room.variants if v.code == code)
    history = cached_code_history(conn, stats, [code])
    detail = _history_detail(history.obs, history.bands, today, curve_date, "suite code")
    detail.update(
        code=code,
        name=room.name,
        room_slug=room.slug,
        category=variant.category,
        max_occupancy=variant.max_occupancy,
        accessible=variant.accessible,
        max_rooms_seen=variant.max_rooms,
        lowest_rate_seen=variant.min_rate,
        first_seen=variant.first_seen,
        last_seen=variant.last_seen,
    )
    return detail


# --------------------------------------------------------------------------- runs


def runs(conn: sqlite3.Connection, limit: int = 50, offset: int = 0) -> list[dict]:
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))]
    except sqlite3.OperationalError:
        return []
    for r in rows:  # Toronto local time for display; the stored UTC values stay as they are
        r["started_local"] = local_time(r.get("started_at"))
        r["finished_local"] = local_time(r.get("finished_at")) if r.get("finished_at") else None
    return rows


def runs_count(conn: sqlite3.Connection) -> int:
    try:
        return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


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
