"""Fetch and parse Great Wolf Lodge availability for a single arrival date.

The parsing functions are pure (dict in, rows out) so they can be tested offline
against fixture JSON. Network access is isolated in `fetch_availability`.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx

from gwt import __version__

LODGE_CODE = "NIAGON"
API_BASE = "https://prod.api.greatwolf.com"
AVAILABILITY_PATH = f"/room-booking/v2/lodges/{LODGE_CODE}/availability"
REF_SUITE_CODE = "SFSSB"
NUM_ADULTS = 2

DEFAULT_USER_AGENT = (
    f"great-wolf-tracker/{__version__} "
    "(personal demand research; single-threaded, one request every few seconds)"
)

# Keys that must never be persisted (signed booking tokens).
SENSITIVE_KEYS = frozenset({"pricingKey"})


def user_agent() -> str:
    return os.environ.get("GWT_USER_AGENT", DEFAULT_USER_AGENT)


def build_params(arrival: date) -> dict[str, str]:
    """Query params for a 1-night stay starting on `arrival`."""
    departure = arrival + timedelta(days=1)
    return {
        "arrival": arrival.isoformat(),
        "departure": departure.isoformat(),
        "location": LODGE_CODE,
        "numberOfAdults": str(NUM_ADULTS),
        "offerCode": "",
        "kidsAges": "",
    }


def make_client(transport: httpx.BaseTransport | None = None, timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        headers={"User-Agent": user_agent(), "Accept": "application/json"},
        timeout=timeout,
        transport=transport,
    )


def fetch_availability(client: httpx.Client, arrival: date) -> httpx.Response:
    """Single GET, no retries. Retry/backoff policy lives in the sweep loop."""
    return client.get(AVAILABILITY_PATH, params=build_params(arrival))


# --------------------------------------------------------------------------- parsing


def strip_sensitive(obj: Any) -> Any:
    """Return a deep copy of `obj` with every SENSITIVE_KEYS entry removed, at any depth."""
    if isinstance(obj, dict):
        return {k: strip_sensitive(v) for k, v in obj.items() if k not in SENSITIVE_KEYS}
    if isinstance(obj, list):
        return [strip_sensitive(v) for v in obj]
    return copy.copy(obj)


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def suite_min_nightly_rate(suite: dict[str, Any]) -> float | None:
    """Lowest totalNightlyRate across all rate families/rates of a suite (falls back to `rate`)."""
    best: float | None = None
    for family in suite.get("rateFamilies") or []:
        for rate in family.get("rates") or []:
            value = _rate_value(rate)
            if value is not None and (best is None or value < best):
                best = value
    return best


def _to_int(value: Any) -> int | None:
    """int, or None if missing/unparseable. Accepts numeric strings like "2" (numOfBeds/numOfBaths)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_to_int(value: Any) -> int | None:
    """isAccessible is a JSON boolean: true -> 1, false -> 0, anything else -> None."""
    if isinstance(value, bool):
        return int(value)
    return None


@dataclass
class RateFamilyRow:
    title: str
    policy_type: str | None
    display_rank: int | None
    min_total_nightly_rate: float | None


@dataclass
class SuiteRow:
    suite_code: str
    suite_category: str | None
    available_room_count: int
    min_total_nightly_rate: float | None
    suite_brand: str | None = None
    base_occupancy: int | None = None
    max_occupancy: int | None = None
    is_accessible: int | None = None
    num_beds: int | None = None
    num_baths: int | None = None
    sort_order: int | None = None
    rate_families: list[RateFamilyRow] = field(default_factory=list)


def _rate_value(rate: dict[str, Any]) -> float | None:
    value = _to_float(rate.get("totalNightlyRate"))
    return value if value is not None else _to_float(rate.get("rate"))


def parse_rate_families(suite: dict[str, Any]) -> list[RateFamilyRow]:
    """Cheapest nightly rate per rate family (e.g. Saver vs Flexible)."""
    rows = []
    for family in suite.get("rateFamilies") or []:
        values = [v for v in (_rate_value(r) for r in family.get("rates") or []) if v is not None]
        rows.append(
            RateFamilyRow(
                title=str(family.get("title") or ""),
                policy_type=family.get("policyType"),
                display_rank=_to_int(family.get("ratesDisplayRank")),
                min_total_nightly_rate=min(values) if values else None,
            )
        )
    return rows


@dataclass
class Snapshot:
    arrival_date: str
    total_rooms_available: int
    suite_types: int
    suite_types_sold_out: int
    min_rate: float | None
    ref_suite_rate: float | None
    error_code: str | None
    raw_json: str
    suites: list[SuiteRow] = field(default_factory=list)


def parse_availability(payload: dict[str, Any], arrival: date | str) -> Snapshot:
    """Turn one availability response into a run-level Snapshot plus per-suite rows.

    - total_rooms_available: sum of availableRoomCount across all suites
    - suite_types_sold_out: suites with availableRoomCount == 0
    - min_rate: lowest nightly rate among suites that still have rooms
    - ref_suite_rate: lowest nightly rate for the reference suite (SFSSB), if it has rooms
    """
    arrival_str = arrival.isoformat() if isinstance(arrival, date) else str(arrival)
    suites_in = payload.get("suites") or []

    rows: list[SuiteRow] = []
    for suite in suites_in:
        count = suite.get("availableRoomCount")
        try:
            count_int = int(count) if count is not None else 0
        except (TypeError, ValueError):
            count_int = 0
        rows.append(
            SuiteRow(
                suite_code=str(suite.get("suiteCode") or ""),
                suite_category=suite.get("suiteCategory"),
                available_room_count=max(count_int, 0),
                min_total_nightly_rate=suite_min_nightly_rate(suite),
                suite_brand=suite.get("suiteBrand"),
                base_occupancy=_to_int(suite.get("baseOccupancy")),
                max_occupancy=_to_int(suite.get("maxOccupancy")),
                is_accessible=_bool_to_int(suite.get("isAccessible")),
                num_beds=_to_int(suite.get("numOfBeds")),
                num_baths=_to_int(suite.get("numOfBaths")),
                sort_order=_to_int(suite.get("sortOrder")),
                rate_families=parse_rate_families(suite),
            )
        )

    available = [r for r in rows if r.available_room_count > 0]
    available_rates = [r.min_total_nightly_rate for r in available if r.min_total_nightly_rate is not None]
    ref = next((r for r in available if r.suite_code == REF_SUITE_CODE), None)

    error_code = payload.get("errorCode")
    return Snapshot(
        arrival_date=arrival_str,
        total_rooms_available=sum(r.available_room_count for r in rows),
        suite_types=len(rows),
        suite_types_sold_out=sum(1 for r in rows if r.available_room_count == 0),
        min_rate=min(available_rates) if available_rates else None,
        ref_suite_rate=ref.min_total_nightly_rate if ref else None,
        error_code=str(error_code) if error_code not in (None, "") else None,
        raw_json=json.dumps(strip_sensitive(payload), separators=(",", ":"), sort_keys=True),
        suites=rows,
    )
