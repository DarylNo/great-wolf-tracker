"""Cross-process lock so two sweeps never run at once (scheduler, CLI, `docker exec` alike)."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class SweepBusy(RuntimeError):
    """Another sweep already holds the lock."""


def lock_path_for(db_path: str | Path) -> Path:
    return Path(f"{db_path}.sweep.lock")


@contextmanager
def sweep_lock(db_path: str | Path) -> Iterator[None]:
    """Non-blocking exclusive flock next to the database file. Raises SweepBusy if held."""
    path = lock_path_for(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SweepBusy(f"another sweep is already running (lock: {path})") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
