"""Polite, single-threaded, resumable sweep over upcoming arrival dates."""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import httpx

from gwt import db
from gwt.collector import fetch_availability, parse_availability

log = logging.getLogger("gwt.sweep")

MIN_DELAY_S = 3.0
MAX_DELAY_S = 5.0
MAX_CONSECUTIVE_FAILURES = 5
BACKOFF_BASE_S = 10.0
BACKOFF_CAP_S = 300.0


class SweepAborted(RuntimeError):
    """Raised when the sweep must stop (too many failures, or we look blocked)."""


@dataclass
class SweepResult:
    run_id: str
    fetched: int = 0
    skipped: int = 0
    failed_dates: int = 0


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_run_id() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def arrival_dates(start: date, days: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(days)]


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


def backoff_delay(consecutive_failures: int) -> float:
    """Exponential backoff: 10s, 20s, 40s, ... capped at 5 min."""
    return min(BACKOFF_BASE_S * (2 ** max(consecutive_failures - 1, 0)), BACKOFF_CAP_S)


def run_sweep(
    conn: sqlite3.Connection,
    client: httpx.Client,
    *,
    days: int = 365,
    start: date | None = None,
    run_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    clock: Callable[[], str] = utc_now_iso,
) -> SweepResult:
    """Fetch each arrival date in [start, start+days) once for this run_id.

    - Dates already stored under `run_id` are skipped (resume after a crash/abort).
    - Jittered 3-5 s delay between requests.
    - 429 / 5xx / network errors: exponential backoff, retry the same date.
    - 403: treated as "blocked" -> abort immediately (stop and reassess, don't evade).
    - Other 4xx: logged, date skipped, counts toward the failure streak.
    - 5 consecutive failures of any kind abort the run.
    """
    rng = rng or random.Random()
    start = start or datetime.now(timezone.utc).date()
    run_id = run_id or default_run_id()
    result = SweepResult(run_id=run_id)

    done = db.captured_dates(conn, run_id)
    pending = [d for d in arrival_dates(start, days) if d.isoformat() not in done]
    result.skipped = days - len(pending)
    log.info("run %s: %d dates to fetch, %d already captured", run_id, len(pending), result.skipped)

    consecutive_failures = 0
    first_request = True

    def fail(reason: str) -> None:
        nonlocal consecutive_failures
        consecutive_failures += 1
        log.warning("failure %d/%d: %s", consecutive_failures, MAX_CONSECUTIVE_FAILURES, reason)
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            raise SweepAborted(f"aborting after {consecutive_failures} consecutive failures (last: {reason})")

    for arrival in pending:
        while True:
            if not first_request:
                sleep(rng.uniform(MIN_DELAY_S, MAX_DELAY_S))
            first_request = False

            try:
                resp = fetch_availability(client, arrival)
            except httpx.TransportError as exc:
                fail(f"{arrival}: network error {exc!r}")
                sleep(backoff_delay(consecutive_failures))
                continue

            status = resp.status_code
            if status == 403:
                raise SweepAborted(f"{arrival}: HTTP 403 - looks blocked; stopping (do not evade)")
            if status == 429 or status >= 500:
                fail(f"{arrival}: HTTP {status}")
                wait = _retry_after_seconds(resp)
                sleep(max(wait or 0.0, backoff_delay(consecutive_failures)))
                continue
            if status >= 400:
                fail(f"{arrival}: HTTP {status}, skipping date")
                result.failed_dates += 1
                break

            try:
                payload = resp.json()
            except ValueError:
                fail(f"{arrival}: response was not JSON")
                result.failed_dates += 1
                break

            snap = parse_availability(payload, arrival)
            db.save_snapshot(conn, run_id, clock(), snap)
            consecutive_failures = 0
            result.fetched += 1
            log.info(
                "%s rooms=%d sold_out=%d/%d min=%s",
                arrival,
                snap.total_rooms_available,
                snap.suite_types_sold_out,
                snap.suite_types,
                snap.min_rate,
            )
            break

    return result
