"""Every number the dashboard shows is defined here, in real units.

Definitions (also in the README, "Metrics"):

- ``lead_days``: arrival_date minus the scrape date, where the scrape date is ``scraped_at``
  converted to America/Toronto (the lodge's local time), not UTC.
- ``capacity_est``: the largest ``total_rooms_available`` ever seen, used as a proxy for
  sellable inventory. Provisional until at least ~60 arrival dates have been seen with
  ``lead_days >= 90`` ("capacity estimate improves after the first full sweep").
- ``pct_sold_est``: ``1 - rooms_left / capacity_est``.
- Lead buckets: 0–1, 2–3, 4–7, 8–14, 15–30, 31–60, 61–90, 91–180, 181–365 days.
- ``pace_pct``: percentile rank (0–100) of a snapshot's ``pct_sold_est`` among all snapshots
  in the same lead bucket, i.e. "vs other dates at the same days-out". ``None`` (n/a) when
  the bucket holds fewer than 20 snapshots.
- ``final_fill``: for one arrival date, the snapshot with the smallest ``lead_days``, valid only
  if that lead is <= 2 days (configurable). "How full it actually got."
- ``pickup_7d``: rooms_left at the scrape ~7 days earlier minus rooms_left now (rooms sold in
  the last week). ``None`` (n/a) without a scrape 6–8 days earlier.
- Price is ``min_rate``, the lowest available nightly rate (1 night, 2 adults, no offer code).

All functions are pure so they can be unit-tested without a database.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Iterable, Protocol, Sequence, TypeVar
from zoneinfo import ZoneInfo

LODGE_TZ = ZoneInfo("America/Toronto")

LEAD_BUCKETS: tuple[tuple[int, int], ...] = (
    (0, 1),
    (2, 3),
    (4, 7),
    (8, 14),
    (15, 30),
    (31, 60),
    (61, 90),
    (91, 180),
    (181, 365),
)
BUCKET_LABELS: tuple[str, ...] = tuple(f"{lo}–{hi}" for lo, hi in LEAD_BUCKETS)
MAX_LEAD = LEAD_BUCKETS[-1][1]

PACE_MIN_SNAPSHOTS = 20
PACE_LABEL = "vs other dates at the same days-out"

CAPACITY_FAR_LEAD = 90
CAPACITY_MIN_FAR_DATES = 60
CAPACITY_CAVEAT = "capacity estimate improves after the first full sweep"

DEFAULT_FINAL_FILL_MAX_LEAD = 2

PICKUP_TARGET_GAP = 7
PICKUP_MIN_GAP = 6
PICKUP_MAX_GAP = 8

PRICE_FOOTNOTE = (
    "Prices are the lowest available nightly rate for 1 night, 2 adults, queried with no "
    "offer code, so promotions and member discounts aren't reflected."
)


# --------------------------------------------------------------------------- lead time


def as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


@lru_cache(maxsize=262_144)
def scrape_local_date(scraped_at: str) -> date:
    """The calendar date in Toronto when a scrape happened.

    ``scraped_at`` is the collector's UTC ISO-8601 timestamp. A timestamp without an offset is
    treated as UTC (that's what the collector writes).
    """
    ts = datetime.fromisoformat(scraped_at.strip().replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(LODGE_TZ).date()


def lead_days(arrival: date | str, scraped_at: str) -> int:
    """Days before arrival at the time of the scrape (Toronto calendar days)."""
    return (as_date(arrival) - scrape_local_date(scraped_at)).days


def lead_bucket(lead: int | None) -> int | None:
    """Index into LEAD_BUCKETS, or None for a lead outside 0..365."""
    if lead is None or lead < 0 or lead > MAX_LEAD:
        return None
    for i, (lo, hi) in enumerate(LEAD_BUCKETS):
        if lo <= lead <= hi:
            return i
    return None  # pragma: no cover - buckets are contiguous


# --------------------------------------------------------------------------- capacity / % sold


def capacity_estimate(rooms_values: Iterable[int | None]) -> int | None:
    """Largest total_rooms_available ever seen (None if nothing positive was seen)."""
    best = max((v for v in rooms_values if v is not None), default=None)
    return best if best is not None and best > 0 else None


def capacity_is_provisional(far_dates_seen: int) -> bool:
    """True until enough far-out dates (lead >= 90) have been seen to trust capacity_est."""
    return far_dates_seen < CAPACITY_MIN_FAR_DATES


def pct_sold(rooms_left: int | None, capacity: int | None) -> float | None:
    """Estimated share of inventory sold, 0..1 (clamped)."""
    if rooms_left is None or not capacity:
        return None
    return min(max(1.0 - rooms_left / capacity, 0.0), 1.0)


# --------------------------------------------------------------------------- pace


def percentile_rank(sorted_values: Sequence[float], value: float) -> float | None:
    """Percentile rank 0..100 of `value` within `sorted_values` (ties count half)."""
    n = len(sorted_values)
    if n == 0:
        return None
    below = bisect_left(sorted_values, value)
    equal = bisect_right(sorted_values, value) - below
    return 100.0 * (below + 0.5 * equal) / n


class PaceTable:
    """pct_sold distributions per lead bucket, for pace percentiles."""

    def __init__(self, pairs: Iterable[tuple[int | None, float | None]], min_n: int = PACE_MIN_SNAPSHOTS):
        buckets: list[list[float]] = [[] for _ in LEAD_BUCKETS]
        for lead, pct in pairs:
            b = lead_bucket(lead)
            if b is not None and pct is not None:
                buckets[b].append(pct)
        for values in buckets:
            values.sort()
        self.buckets = buckets
        self.min_n = min_n

    def size(self, lead: int | None) -> int:
        b = lead_bucket(lead)
        return 0 if b is None else len(self.buckets[b])

    def pace(self, lead: int | None, pct: float | None) -> float | None:
        """Percentile of `pct` among snapshots at the same days-out; None if that bucket is too small."""
        b = lead_bucket(lead)
        if b is None or pct is None:
            return None
        values = self.buckets[b]
        if len(values) < self.min_n:
            return None
        return percentile_rank(values, pct)


# --------------------------------------------------------------------------- final fill / pickup


class _HasLead(Protocol):
    lead: int


class _HasScrape(Protocol):
    scrape_date: date
    rooms: int | None


L = TypeVar("L", bound=_HasLead)
S = TypeVar("S", bound=_HasScrape)


def final_fill(snapshots: Iterable[L], max_lead: int = DEFAULT_FINAL_FILL_MAX_LEAD) -> L | None:
    """The snapshot closest to arrival for one date, if it was taken within `max_lead` days.

    Ties on lead go to the later item (callers pass snapshots in scrape order).
    """
    best: L | None = None
    for snap in snapshots:
        if snap.lead is None or snap.lead < 0:
            continue
        if best is None or snap.lead <= best.lead:
            best = snap
    if best is None or best.lead > max_lead:
        return None
    return best


def pickup_7d(history: Iterable[S], current: S) -> int | None:
    """Rooms sold in the last ~7 days for one arrival date.

    `history` holds snapshots of the same arrival date (in scrape order). The comparison
    snapshot is the one taken 6–8 days before `current`, closest to 7 days; ties go to the
    more recent (smaller gap), and within one day to the later snapshot.
    """
    if current.rooms is None:
        return None
    best: S | None = None
    best_key: tuple[int, int] | None = None
    for snap in history:
        gap = (current.scrape_date - snap.scrape_date).days
        if snap.rooms is None or not PICKUP_MIN_GAP <= gap <= PICKUP_MAX_GAP:
            continue
        key = (abs(gap - PICKUP_TARGET_GAP), gap)
        if best_key is None or key <= best_key:
            best, best_key = snap, key
    if best is None:
        return None
    return best.rooms - current.rooms


# --------------------------------------------------------------------------- distributions / bands


def quantile(sorted_values: Sequence[float], p: float) -> float | None:
    """Linear-interpolated quantile (p in 0..1) of already-sorted values."""
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_values[0])
    pos = min(max(p, 0.0), 1.0) * (n - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def band_by_bucket(
    pairs: Iterable[tuple[int | None, float | None]], min_n: int = 5
) -> list[dict | None]:
    """Median and 10th–90th percentile of values per lead bucket (None where n < min_n).

    The straightforward version; pages use band_from_buckets, which gives the same numbers
    from pre-sorted per-bucket lists and can leave one date's values out without re-sorting.
    """
    buckets: list[list[float]] = [[] for _ in LEAD_BUCKETS]
    for lead, value in pairs:
        b = lead_bucket(lead)
        if b is not None and value is not None:
            buckets[b].append(float(value))
    out: list[dict | None] = []
    for (lo, hi), label, values in zip(LEAD_BUCKETS, BUCKET_LABELS, buckets):
        if len(values) < min_n:
            out.append(None)
            continue
        values.sort()
        out.append(
            {
                "bucket": label,
                "lead_lo": lo,
                "lead_hi": hi,
                "p10": quantile(values, 0.10),
                "p50": quantile(values, 0.50),
                "p90": quantile(values, 0.90),
                "n": len(values),
            }
        )
    return out


def bucket_values(pairs: Iterable[tuple[int | None, float | None]]) -> list[list[float]]:
    """Values per lead bucket, each list sorted: the input band_from_buckets works from."""
    buckets: list[list[float]] = [[] for _ in LEAD_BUCKETS]
    for lead, value in pairs:
        b = lead_bucket(lead)
        if b is not None and value is not None:
            buckets[b].append(float(value))
    for values in buckets:
        values.sort()
    return buckets


def kth_excluding(all_sorted: Sequence[float], excluded_sorted: Sequence[float], k: int) -> float:
    """The k-th smallest (0-based) value of the multiset `all_sorted` minus `excluded_sorted`.

    `excluded_sorted` must be a sub-multiset of `all_sorted` (both sorted). Binary search over
    `all_sorted`: O(log² n), no copying, so leaving one date out of a large pool is cheap.
    """
    lo, hi = 0, len(all_sorted) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        v = all_sorted[mid]
        if bisect_right(all_sorted, v) - bisect_right(excluded_sorted, v) >= k + 1:
            hi = mid
        else:
            lo = mid + 1
    return all_sorted[lo]


def quantile_excluding(all_sorted: Sequence[float], excluded_sorted: Sequence[float], p: float) -> float | None:
    """`quantile(sorted(all - excluded), p)`, computed without building the difference."""
    n = len(all_sorted) - len(excluded_sorted)
    if n <= 0:
        return None
    if n == 1:
        return float(kth_excluding(all_sorted, excluded_sorted, 0))
    pos = min(max(p, 0.0), 1.0) * (n - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    a = kth_excluding(all_sorted, excluded_sorted, lo)
    b = a if hi == lo else kth_excluding(all_sorted, excluded_sorted, hi)
    return a + (b - a) * (pos - lo)


def band_from_buckets(
    all_buckets: Sequence[Sequence[float]],
    excluded: Sequence[Sequence[float]] | None = None,
    min_n: int = 5,
) -> list[dict | None]:
    """Same result as band_by_bucket over every value in `all_buckets` except `excluded`.

    Both come from bucket_values(); `excluded` (e.g. the page's own date) must be part of
    `all_buckets`. The pool's sorted lists are built once per database version and reused.
    """
    out: list[dict | None] = []
    for i, ((lo, hi), label) in enumerate(zip(LEAD_BUCKETS, BUCKET_LABELS)):
        values = all_buckets[i]
        skip = excluded[i] if excluded is not None else ()
        n = len(values) - len(skip)
        if n < min_n:
            out.append(None)
            continue
        out.append(
            {
                "bucket": label,
                "lead_lo": lo,
                "lead_hi": hi,
                "p10": quantile_excluding(values, skip, 0.10),
                "p50": quantile_excluding(values, skip, 0.50),
                "p90": quantile_excluding(values, skip, 0.90),
                "n": n,
            }
        )
    return out


# --------------------------------------------------------------------------- colour classes


def quantile_breaks(values: Iterable[float | None], classes: int = 5) -> list[float]:
    """Inner break points that split `values` into up to `classes` quantile classes.

    Class i holds values v with breaks[i-1] <= v < breaks[i]. Breaks are actual data values
    and strictly increasing; heavy ties merge classes, so fewer classes may come back.
    """
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    if n == 0 or classes < 2:
        return []
    breaks: list[float] = []
    for i in range(1, classes):
        b = vals[min(n - 1, (i * n) // classes)]
        if b > vals[0] and (not breaks or b > breaks[-1]):
            breaks.append(b)
    return breaks


def class_index(value: float, breaks: Sequence[float]) -> int:
    """Which quantile class `value` falls in (0..len(breaks))."""
    return bisect_right(breaks, value)


def class_ranges(values: Iterable[float | None], breaks: Sequence[float]) -> list[tuple[float, float]]:
    """Actual (min, max) of the values in each class, for a legend with real numbers."""
    ranges: list[list[float] | None] = [None] * (len(breaks) + 1)
    for v in values:
        if v is None:
            continue
        i = class_index(v, breaks)
        r = ranges[i]
        if r is None:
            ranges[i] = [v, v]
        else:
            r[0], r[1] = min(r[0], v), max(r[1], v)
    return [(r[0], r[1]) for r in ranges if r is not None]


def ramp_step(cls: int, n_classes: int, steps: int = 5) -> int:
    """Spread `n_classes` classes evenly over a colour ramp of `steps` steps (0 = lightest)."""
    if n_classes <= 1:
        return steps // 2
    return round(cls * (steps - 1) / (n_classes - 1))


# --------------------------------------------------------------------------- calendar helpers


def is_weekend_night(d: date) -> bool:
    """Friday and Saturday nights are the weekend nights at a hotel."""
    return d.weekday() in (4, 5)


def week_of_year(d: date) -> int:
    """Week 1..52 counted from January 1 (the last day or two of the year fold into week 52)."""
    return min((d.timetuple().tm_yday - 1) // 7 + 1, 52)


def week_position(d: date) -> float:
    """Continuous x position of a night on a week-of-year axis (week n is centred on n)."""
    return 1 + (d.timetuple().tm_yday - 4) / 7


def week_dates(year: int, week: int) -> tuple[date, date]:
    """First and last night of week_of_year `week` in `year` (week 52 runs to Dec 31)."""
    first = date(year, 1, 1) + timedelta(days=(week - 1) * 7)
    last = date(year, 12, 31) if week == 52 else first + timedelta(days=6)
    return first, last
