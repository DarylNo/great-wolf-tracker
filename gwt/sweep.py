"""Polite, single-threaded, resumable sweep over upcoming arrival dates."""

from __future__ import annotations

import email.utils
import logging
import random
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx

from gwt import db
from gwt.collector import fetch_availability, make_client, parse_availability
from gwt.locking import sweep_lock
from gwt.metrics import LODGE_TZ

log = logging.getLogger("gwt.sweep")

MIN_DELAY_S = 3.0
MAX_DELAY_S = 5.0
MAX_CONSECUTIVE_FAILURES = 5
BACKOFF_BASE_S = 10.0
BACKOFF_CAP_S = 300.0
RETRY_AFTER_CAP_S = 600.0  # longer than this and the run ends as "rate_limited" instead of waiting


class SweepAborted(RuntimeError):
    """The sweep must stop: too many failures, blocked (403), or rate-limited for too long."""

    def __init__(self, message: str, *, blocked: bool = False, status: str = "aborted"):
        super().__init__(message)
        self.blocked = blocked
        self.status = status


class SweepInterrupted(RuntimeError):
    """The sweep was asked to stop (e.g. the container is shutting down)."""


class SweepPaused(RuntimeError):
    """Collection is paused after a blocked (403) run until `gwt unblock`."""

    def __init__(self, pause: dict):
        super().__init__(db.pause_message(pause))
        self.pause = pause


@dataclass
class SweepResult:
    run_id: str
    fetched: int = 0
    skipped: int = 0
    failed_dates: int = 0
    failures: int = 0


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def local_today(now: datetime | None = None) -> date:
    """Today's date at the lodge (America/Toronto), not UTC."""
    return (now or datetime.now(timezone.utc)).astimezone(LODGE_TZ).date()


def default_run_id(now: datetime | None = None) -> str:
    return local_today(now).isoformat()


def arrival_dates(start: date, days: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(days)]


def _retry_after_seconds(resp: httpx.Response, now: datetime | None = None) -> float | None:
    """Retry-After as seconds: either delta-seconds or an HTTP date."""
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    value = value.strip()
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((when - (now or datetime.now(timezone.utc))).total_seconds(), 0.0)


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
    trigger: str = "cli",
    sleep: Callable[[float], None] | None = None,
    rng: random.Random | None = None,
    clock: Callable[[], str] = utc_now_iso,
    now: Callable[[], datetime] | None = None,
    stop: threading.Event | None = None,
) -> SweepResult:
    """Fetch each arrival date in [start, start+days) once for this run_id.

    - start and run_id default to today's date in Toronto.
    - Dates already stored under `run_id` are skipped (resume after a crash/abort).
    - Jittered 3-5 s delay between requests.
    - 429 / 5xx / network errors: exponential backoff, retry the same date; a Retry-After
      over 10 minutes ends the run as "rate_limited" instead of waiting with the lock held.
    - 403: treated as "blocked" -> abort immediately (stop and reassess, don't evade).
    - Other 4xx: logged, date skipped, counts toward the failure streak.
    - 5 consecutive failures of any kind abort the run.
    - `stop` (set on shutdown) is checked between dates and wakes any wait, so the run is
      recorded as "interrupted" instead of being killed mid-flight.

    Every invocation is logged to the `runs` table (status, counters, abort reason).
    """
    rng = rng or random.Random()
    stop = stop or threading.Event()
    today = local_today(now() if now else None)
    start = start or today
    run_id = run_id or today.isoformat()
    result = SweepResult(run_id=run_id)

    done = db.captured_dates(conn, run_id)
    pending = [d for d in arrival_dates(start, days) if d.isoformat() not in done]
    result.skipped = days - len(pending)
    log.info("run %s: %d dates to fetch, %d already captured", run_id, len(pending), result.skipped)

    run_row = db.start_run(
        conn,
        run_id=run_id,
        trigger=trigger,
        started_at=clock(),
        start_date=start.isoformat(),
        days=days,
        skipped=result.skipped,
    )

    consecutive_failures = 0
    first_request = True

    def pause(seconds: float) -> None:
        if sleep is not None:
            sleep(seconds)
        elif stop.wait(seconds):
            raise SweepInterrupted("stop requested while waiting")
        if stop.is_set():
            raise SweepInterrupted("stop requested")

    def fail(reason: str) -> None:
        nonlocal consecutive_failures
        consecutive_failures += 1
        result.failures += 1
        log.warning("failure %d/%d: %s", consecutive_failures, MAX_CONSECUTIVE_FAILURES, reason)
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            raise SweepAborted(f"aborting after {consecutive_failures} consecutive failures (last: {reason})")

    def progress(**extra) -> None:
        db.update_run(
            conn,
            run_row,
            fetched=result.fetched,
            failed_dates=result.failed_dates,
            failures=result.failures,
            **extra,
        )

    try:
        for arrival in pending:
            if stop.is_set():
                raise SweepInterrupted(f"stop requested before {arrival}")
            while True:
                if not first_request:
                    pause(rng.uniform(MIN_DELAY_S, MAX_DELAY_S))
                first_request = False

                try:
                    resp = fetch_availability(client, arrival)
                except httpx.TransportError as exc:
                    fail(f"{arrival}: network error {exc!r}")
                    pause(backoff_delay(consecutive_failures))
                    continue

                status = resp.status_code
                if status == 403:
                    result.failures += 1
                    raise SweepAborted(
                        f"{arrival}: HTTP 403 - looks blocked; stopping (do not evade)", blocked=True
                    )
                if status == 429 or status >= 500:
                    wait = _retry_after_seconds(resp)
                    if wait is not None and wait > RETRY_AFTER_CAP_S:
                        result.failures += 1
                        raise SweepAborted(
                            f"{arrival}: HTTP {status} asks to retry after {wait:.0f}s (over the "
                            f"{RETRY_AFTER_CAP_S / 60:.0f}-minute cap); ending the run",
                            status="rate_limited",
                        )
                    fail(f"{arrival}: HTTP {status}")
                    pause(max(wait or 0.0, backoff_delay(consecutive_failures)))
                    continue
                if status >= 400:
                    result.failed_dates += 1
                    fail(f"{arrival}: HTTP {status}, skipping date")
                    break

                try:
                    payload = resp.json()
                except ValueError:
                    result.failed_dates += 1
                    fail(f"{arrival}: response was not JSON")
                    break

                snap = parse_availability(payload, arrival)
                db.save_snapshot(conn, run_id, clock(), snap)
                consecutive_failures = 0
                result.fetched += 1
                progress()
                log.info(
                    "%s rooms=%d sold_out=%d/%d min=%s",
                    arrival,
                    snap.total_rooms_available,
                    snap.suite_types_sold_out,
                    snap.suite_types,
                    snap.min_rate,
                )
                break
    except SweepAborted as exc:
        progress(status=exc.status, blocked=int(exc.blocked), message=str(exc), finished_at=clock())
        raise
    except SweepInterrupted as exc:
        progress(status="interrupted", message=f"stopped by shutdown ({exc})", finished_at=clock())
        raise
    except KeyboardInterrupt:
        progress(status="interrupted", message="interrupted (Ctrl-C)", finished_at=clock())
        raise
    except BaseException as exc:  # crash
        progress(status="error", message=f"{type(exc).__name__}: {exc}", finished_at=clock())
        raise

    progress(status="completed", finished_at=clock())
    return result


def execute_sweep(
    db_path: str | Path,
    *,
    days: int,
    start: date | None = None,
    run_id: str | None = None,
    trigger: str = "cli",
    force: bool = False,
    stop: threading.Event | None = None,
) -> SweepResult:
    """Lock, connect, run one sweep. Shared by the CLI and the in-container scheduler.

    Raises gwt.locking.SweepBusy if another sweep holds the lock, SweepPaused if collection
    is paused after a 403 (unless `force`), SweepAborted on abort, SweepInterrupted on stop.
    """
    with sweep_lock(db_path):
        conn = db.connect(db_path)
        try:
            stale = db.mark_interrupted(conn, utc_now_iso())
            if stale:
                log.warning("marked %d stale 'running' run(s) as interrupted", stale)
            pause = db.collection_pause(conn)
            if pause and not force:
                raise SweepPaused(pause)
            with make_client() as client:
                return run_sweep(conn, client, days=days, start=start, run_id=run_id, trigger=trigger, stop=stop)
        finally:
            conn.close()
