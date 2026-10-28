"""In-process sweep scheduler (APScheduler) that runs alongside the web server."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from gwt.config import ScheduleConfig, SweepJob
from gwt.locking import SweepBusy
from gwt.sweep import SweepAborted, SweepInterrupted, SweepPaused, execute_sweep

log = logging.getLogger("gwt.scheduler")

# In-process guard on top of the cross-process file lock.
_in_process = threading.Lock()


def run_scheduled_sweep(
    db_path: Path,
    job: SweepJob,
    runner: Callable = execute_sweep,
    stop: threading.Event | None = None,
) -> str:
    """Body of a scheduled job. Never raises: returns an outcome string for logging/tests."""
    if stop is not None and stop.is_set():  # a job queued behind another while shutting down
        log.info("%s sweep not started: shutting down", job.name)
        return "stopping"
    if not _in_process.acquire(blocking=False):
        log.warning("%s sweep skipped: another sweep is running in this process", job.name)
        return "busy"
    try:
        result = runner(db_path, days=job.days, trigger=f"schedule:{job.name}", stop=stop)
        log.info("%s sweep done: fetched=%d skipped=%d", job.name, result.fetched, result.skipped)
        return "completed"
    except SweepPaused as exc:
        log.error("=" * 72)
        log.error("COLLECTION PAUSED: %s sweep skipped. %s", job.name, exc)
        log.error("Last blocked run %s: %s", exc.pause.get("run_id"), exc.pause.get("message"))
        log.error("=" * 72)
        return "paused"
    except SweepBusy as exc:
        log.warning("%s sweep skipped: %s", job.name, exc)
        return "busy"
    except SweepInterrupted as exc:
        log.warning("%s sweep interrupted: %s", job.name, exc)
        return "interrupted"
    except SweepAborted as exc:
        if exc.blocked:
            log.error("%s sweep BLOCKED (HTTP 403): %s. Scheduled sweeps are paused until `gwt unblock`.",
                      job.name, exc)
            return "blocked"
        log.error("%s sweep ended (%s): %s", job.name, exc.status, exc)
        return exc.status
    except Exception:
        log.exception("%s sweep crashed", job.name)
        return "error"
    finally:
        _in_process.release()


def build_scheduler(
    config: ScheduleConfig, db_path: Path, stop: threading.Event | None = None
) -> BackgroundScheduler | None:
    """Create (not start) the scheduler, or None when scheduling is disabled."""
    if not config.enabled or not config.jobs:
        return None
    scheduler = BackgroundScheduler(
        timezone=config.timezone,
        # One worker thread + max_instances=1: jobs queue behind each other, never overlap.
        executors={"default": {"type": "threadpool", "max_workers": 1}},
        job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 3600},
    )
    for job in config.jobs:
        scheduler.add_job(
            run_scheduled_sweep,
            CronTrigger(timezone=config.timezone, **job.cron.trigger_kwargs()),
            args=[db_path, job],
            kwargs={"stop": stop},
            id=f"sweep-{job.name}",
            name=f"{job.name} sweep ({job.days} days, cron '{job.cron.expression}')",
        )
    return scheduler


def describe(scheduler: BackgroundScheduler | None) -> list[dict]:
    if scheduler is None:
        return []
    out = []
    for job in scheduler.get_jobs():
        nxt = getattr(job, "next_run_time", None)
        out.append({"id": job.id, "name": job.name, "next_run": nxt.isoformat() if nxt else None})
    return out
