"""Cross-process lock so two sweeps never run at once (scheduler, CLI, `docker exec` alike)."""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class SweepBusy(RuntimeError):
    """Another sweep already holds the lock."""


def lock_path_for(db_path: str | Path) -> Path:
    return Path(f"{db_path}.sweep.lock")


@contextmanager
def sweep_lock(db_path: str | Path, wait: float = 0.0) -> Iterator[None]:
    """Exclusive flock next to the database file.

    Non-blocking by default; with `wait` > 0 it retries for up to that many seconds.
    Raises SweepBusy if the lock is still held.
    """
    path = lock_path_for(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + max(wait, 0.0)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise SweepBusy(f"another sweep is already running (lock: {path})") from exc
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
