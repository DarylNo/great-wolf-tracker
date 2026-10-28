"""School-closure calendar: the nights families can stay without missing school.

Rule: a night is flagged for a region when the NEXT MORNING is a school-closure day for
that region (a break, PA day or public holiday) and is not a regular weekend day (Sat/Sun).

Closure days come from `gwt/calendar_events.yaml` (school boards; override the file with
GWT_EVENTS_FILE) plus public holidays from the `holidays` package for Ontario (CA-ON) and
New York (US-NY), computed for whichever years are being shown. Where a school-board entry
covers a day, it's the authority for that region and day: the package's holiday names for
that day are not added on top (so "Presidents' Day" isn't doubled as "Washington's Birthday").

A GWT_EVENTS_FILE that can't be read or parsed doesn't take the dashboard down: it's logged,
the bundled calendar is used instead, and `events_problem()` reports it for a page banner.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import yaml

log = logging.getLogger("gwt.events")

DEFAULT_EVENTS_FILE = Path(__file__).with_name("calendar_events.yaml")

# region code -> (holidays country, subdivision, display name)
REGIONS: dict[str, tuple[str, str, str]] = {
    "ON": ("CA", "ON", "Ontario"),
    "NY": ("US", "NY", "New York"),
}
HOLIDAYS_SOURCE = "python-holidays package"


class EventsFileError(ValueError):
    pass


@dataclass(frozen=True)
class CalendarEvent:
    region: str
    name: str
    start: date
    end: date
    source: str
    official: bool
    closure: bool = True
    kind: str = "school"  # "school" (from the YAML) or "holiday" (from python-holidays)

    def covers(self, d: date) -> bool:
        return self.start <= d <= self.end

    @property
    def label(self) -> str:
        suffix = " (public holiday)" if self.kind == "holiday" else ""
        return f"{self.region}: {self.name}{suffix}"


@dataclass(frozen=True)
class NightFlag:
    """Why a night is flagged: the region and the closure(s) on the next morning."""

    region: str
    labels: tuple[str, ...]

    @property
    def text(self) -> str:
        return f"{self.region}: " + "; ".join(self.labels)


def events_file() -> Path:
    override = os.environ.get("GWT_EVENTS_FILE")
    return Path(override) if override else DEFAULT_EVENTS_FILE


def _as_date(value, field: str, index: int) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise EventsFileError(f"event #{index}: {field} must be YYYY-MM-DD, got {value!r}") from exc


def parse_events_yaml(text: str) -> tuple[list[CalendarEvent], dict[str, set[str]]]:
    """Parse the YAML document into events plus the optional per-region holiday exclusions."""
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        where = getattr(exc, "problem_mark", None)
        at = f" (line {where.line + 1}, column {where.column + 1})" if where is not None else ""
        reason = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        raise EventsFileError(f"not valid YAML{at}: {reason}") from exc
    if not isinstance(doc, dict):
        raise EventsFileError("top level must be a mapping with an 'events' list")
    events = []
    for i, raw in enumerate(doc.get("events") or [], start=1):
        if not isinstance(raw, dict):
            raise EventsFileError(f"event #{i} must be a mapping")
        missing = [k for k in ("region", "name", "start", "end", "source", "official") if k not in raw]
        if missing:
            raise EventsFileError(f"event #{i} is missing {', '.join(missing)}")
        if isinstance(raw["region"], bool):
            raise EventsFileError(f'event #{i}: region was read as a boolean; quote it, e.g. region: "ON"')
        region = str(raw["region"]).upper()
        if region not in REGIONS:
            raise EventsFileError(f"event #{i}: unknown region {region!r} (use {', '.join(REGIONS)})")
        start, end = _as_date(raw["start"], "start", i), _as_date(raw["end"], "end", i)
        if end < start:
            raise EventsFileError(f"event #{i}: end {end} is before start {start}")
        events.append(
            CalendarEvent(
                region=region,
                name=str(raw["name"]),
                start=start,
                end=end,
                source=str(raw["source"]),
                official=bool(raw["official"]),
                closure=bool(raw.get("closure", True)),
            )
        )
    exclusions = {}
    raw_exclusions = doc.get("exclude_holidays") or {}
    if not isinstance(raw_exclusions, dict):
        raise EventsFileError("exclude_holidays must map a region to a list of holiday names")
    for region, names in raw_exclusions.items():
        if isinstance(region, bool):
            raise EventsFileError('exclude_holidays: a region key was read as a boolean; quote it, e.g. "ON":')
        if isinstance(names, str) or not isinstance(names, (list, type(None))):
            raise EventsFileError(f"exclude_holidays: {region} must be a list of holiday names")
        exclusions[str(region).upper()] = {str(n) for n in (names or [])}
    return events, exclusions


_cache_lock = threading.Lock()
# (path, mtime) -> parsed (events, exclusions), or the error message if the file is unusable
_file_cache: dict[tuple[str, float], tuple[list[CalendarEvent], dict[str, set[str]]] | str] = {}


def load_school_events(path: Path | None = None) -> tuple[list[CalendarEvent], dict[str, set[str]]]:
    """Parse one events file (cached until its mtime changes). Raises EventsFileError."""
    path = path or events_file()
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError as exc:
        raise EventsFileError(f"events file not found: {path}") from exc
    except OSError as exc:
        raise EventsFileError(f"can't read {path}: {exc.strerror or exc}") from exc
    key = (str(path), mtime)
    with _cache_lock:
        cached = _file_cache.get(key)
        if cached is None:
            try:
                cached = parse_events_yaml(path.read_text(encoding="utf-8"))
            except EventsFileError as exc:
                cached = str(exc)
            except (OSError, UnicodeDecodeError) as exc:
                cached = f"can't read {path}: {getattr(exc, 'strerror', None) or exc}"
            if isinstance(cached, str):
                log.error("events file %s is unusable: %s", path, cached)
            if len(_file_cache) >= 8:
                _file_cache.clear()
            _file_cache[key] = cached
    if isinstance(cached, str):
        raise EventsFileError(cached)
    return cached


def school_events(
    path: Path | None = None,
) -> tuple[list[CalendarEvent], dict[str, set[str]], dict[str, str] | None]:
    """Events from `path` (default: GWT_EVENTS_FILE or the bundled file), falling back to the
    bundled file when a custom one is unusable. Returns (events, exclusions, problem)."""
    path = path or events_file()
    try:
        school, exclusions = load_school_events(path)
        return school, exclusions, None
    except EventsFileError as exc:
        if path == DEFAULT_EVENTS_FILE:
            raise  # the bundled calendar itself is broken: a packaging bug, not a config problem
        problem = {"file": str(path), "error": str(exc)}
    school, exclusions = load_school_events(DEFAULT_EVENTS_FILE)
    return school, exclusions, problem


def events_problem() -> dict[str, str] | None:
    """{'file', 'error'} when GWT_EVENTS_FILE is set but unusable (the bundled calendar is
    being shown instead), else None. Cheap: parse results are cached by file mtime."""
    path = events_file()
    if path == DEFAULT_EVENTS_FILE:
        return None
    try:
        load_school_events(path)
    except EventsFileError as exc:
        return {"file": str(path), "error": str(exc)}
    return None


def holiday_events(years: Iterable[int], exclusions: dict[str, set[str]] | None = None) -> list[CalendarEvent]:
    """Public holidays for each region from the `holidays` package (one-day events)."""
    import holidays  # imported lazily: it's slow to import and only needed here

    exclusions = exclusions or {}
    years = sorted(set(years))
    out = []
    for region, (country, subdiv, _name) in REGIONS.items():
        skip = exclusions.get(region, set())
        for day, names in sorted(holidays.country_holidays(country, subdiv=subdiv, years=years).items()):
            kept = [n.strip() for n in str(names).split(";") if n.strip() and n.strip() not in skip]
            if kept:
                out.append(
                    CalendarEvent(
                        region=region,
                        name="; ".join(kept),
                        start=day,
                        end=day,
                        source=HOLIDAYS_SOURCE,
                        official=True,
                        kind="holiday",
                    )
                )
    return out


class ClosureCalendar:
    """Closure days per region, and the nights they flag."""

    def __init__(self, events: Iterable[CalendarEvent], problem: dict[str, str] | None = None):
        self.events = list(events)
        self.problem = problem  # set when a custom events file was unusable (see school_events)
        self._closures: dict[str, dict[date, list[str]]] = {r: {} for r in REGIONS}
        self._info: dict[date, list[str]] = {}
        school_days: set[tuple[str, date]] = set()
        # School-board entries first. A day a board lists is labelled by the board only: the
        # package's holiday names for that region/day are skipped, so a board's "Presidents'
        # Day" isn't repeated as "Washington's Birthday (public holiday)".
        for ev in sorted(self.events, key=lambda e: e.kind != "school"):
            d = ev.start
            while d <= ev.end:
                if not ev.closure:
                    self._info.setdefault(d, []).append(ev.label)
                elif ev.kind == "school":
                    school_days.add((ev.region, d))
                    names = self._closures.setdefault(ev.region, {}).setdefault(d, [])
                    if ev.name not in names:
                        names.append(ev.name)
                elif (ev.region, d) not in school_days:
                    self._closures.setdefault(ev.region, {}).setdefault(d, []).append(ev.name + " (public holiday)")
                d += timedelta(days=1)

    @classmethod
    def for_years(cls, years: Iterable[int], path: Path | None = None) -> "ClosureCalendar":
        school, exclusions, problem = school_events(path)
        return cls([*school, *holiday_events(years, exclusions)], problem)

    @classmethod
    def for_range(cls, start: date, end: date, path: Path | None = None) -> "ClosureCalendar":
        # +1 year: the night of Dec 31 depends on Jan 1 of the next year.
        return cls.for_years(range(start.year, end.year + 2), path)

    def closure_labels(self, region: str, d: date) -> list[str]:
        return list(self._closures.get(region, {}).get(d, []))

    def is_closure_day(self, region: str, d: date) -> bool:
        return d in self._closures.get(region, {})

    def night_flags(self, night: date) -> list[NightFlag]:
        """Regions whose schools are closed the next morning (a weekday), with the reasons."""
        morning = night + timedelta(days=1)
        if morning.weekday() >= 5:  # Saturday/Sunday mornings are a regular weekend
            return []
        flags = []
        for region in REGIONS:
            labels = self._closures.get(region, {}).get(morning)
            if labels:
                flags.append(NightFlag(region, tuple(labels)))
        return flags

    def flagged_nights(self, start: date, end: date) -> dict[date, list[NightFlag]]:
        out = {}
        d = start
        while d <= end:
            flags = self.night_flags(d)
            if flags:
                out[d] = flags
            d += timedelta(days=1)
        return out

    def info_tags(self, d: date) -> list[str]:
        """Informational events (closure: false) on this date, e.g. 'NY: Last student day'."""
        return list(self._info.get(d, []))


_range_cache: dict[tuple, ClosureCalendar] = {}


def calendar_for_range(start: date, end: date) -> ClosureCalendar:
    """Cached ClosureCalendar covering [start, end] (refreshed when the YAML file changes)."""
    path = events_file()
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        mtime = -1.0
    key = (str(path), mtime, start.year, end.year)
    with _cache_lock:
        cal = _range_cache.get(key)
    if cal is None:
        cal = ClosureCalendar.for_range(start, end, path)
        with _cache_lock:
            if len(_range_cache) > 16:
                _range_cache.clear()
            _range_cache[key] = cal
    return cal


def merge_spans(nights: Iterable[date]) -> list[tuple[date, date]]:
    """Collapse a set of nights into (first, last) runs of consecutive dates."""
    spans: list[list[date]] = []
    for d in sorted(set(nights)):
        if spans and (d - spans[-1][1]).days == 1:
            spans[-1][1] = d
        else:
            spans.append([d, d])
    return [(a, b) for a, b in spans]
