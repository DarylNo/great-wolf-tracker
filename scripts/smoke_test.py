#!/usr/bin/env python3
"""Smoke-test a built image on a database written by the pre-redesign build (84181f2).

usage: python scripts/smoke_test.py IMAGE

Starts IMAGE with the scheduler off on a copy of tests/fixtures/legacy-84181f2.db (the live
server's old schema), waits for /healthz, requests every page and API route expecting 200,
then checks inside the container that the database was migrated: new table, columns and
indexes, correct backfills, no rows lost, and that `gwt backup` works. Finally it stops the
container and expects a clean exit. Makes no calls to Great Wolf: no sweep runs.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "legacy-84181f2.db"
NAME = "gwt-smoke"
PORT = 18080
TABLES = ("snapshots", "suite_snapshots", "suite_rate_families", "suites", "runs")

# Runs inside the container (argv[1] = row counts before the upgrade, as JSON).
CHECK_MIGRATED = r"""
import json, sqlite3, sys
expected = json.loads(sys.argv[1])
conn = sqlite3.connect("/app/data/gwl.db")
counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in expected}
assert counts == expected, f"row counts changed: {counts} != {expected}"
names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
missing = {"meta", "idx_suite_snapshots_snapshot", "idx_suite_snapshots_code"} - names
assert not missing, f"missing after migration: {missing}"
cols = {r[1] for r in conn.execute("PRAGMA table_info(suites)")}
assert {"max_rooms_seen", "min_rate_seen"} <= cols, cols
for code, most, lowest in conn.execute("SELECT suite_code, max_rooms_seen, min_rate_seen FROM suites"):
    want = conn.execute("SELECT MAX(available_room_count), MIN(min_total_nightly_rate) "
                        "FROM suite_snapshots WHERE suite_code = ?", (code,)).fetchone()
    assert (most, lowest) == want, (code, most, lowest, want)
assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
print("migration ok:", counts)
"""


def sh(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, text=True, capture_output=capture)


def get(base: str, path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(base + path, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def routes(base: str) -> list[str]:
    status, body = get(base, "/api/rooms")
    assert status == 200, f"/api/rooms -> {status}"
    rooms = json.loads(body)["rooms"]
    slugs = [r["slug"] for r in rooms]
    codes = [code for r in rooms for code in r["codes"]]
    assert slugs and codes, "no rooms in the upgraded database"
    pages = ["/", "/?metric=rooms", "/?metric=pace", "/seasonality", "/trends", "/date/2026-09-22",
             "/date/2026-09-08", "/suites", "/suites?window=30", "/suites?window=365", "/runs", "/runs?page=2"]
    pages += [f"/rooms/{s}" for s in slugs] + [f"/rooms/{slugs[0]}?date=2026-09-08"]
    pages += [f"/suites/{c}" for c in codes] + [f"/suites/{codes[0]}?date=2026-09-08"]
    api = ["/api/plan", "/api/seasonality", "/api/dates/2026-09-22", "/api/rooms", "/api/rooms/matrix",
           "/api/rooms/matrix?window=365", "/api/suites", "/api/runs", "/healthz", "/static/app.js",
           "/static/app.css", "/static/plotly.min.js"]
    api += [f"/api/rooms/{s}" for s in slugs] + [f"/api/suites/{c}" for c in codes]
    return pages + api


def check_routes(base: str) -> None:
    paths = routes(base)
    failures = {}
    for path in paths:
        status, _ = get(base, path)
        if status != 200:
            failures[path] = status
    if failures:
        raise SystemExit(f"non-200 responses: {failures}")
    print(f"{len(paths)} pages and API routes returned 200")


def wait_healthy(base: str, seconds: int = 90) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            status, body = get(base, "/healthz")
            if status == 200:
                return json.loads(body)
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(1)
    raise SystemExit(f"/healthz didn't answer within {seconds}s")


def main(image: str) -> None:
    src = sqlite3.connect(f"file:{FIXTURE}?mode=ro", uri=True)
    before = {t: src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    src.close()

    data = Path(tempfile.mkdtemp(prefix="gwt-smoke-"))
    shutil.copyfile(FIXTURE, data / "gwl.db")
    data.chmod(0o777)  # the container runs as uid 1000
    (data / "gwl.db").chmod(0o666)
    base = f"http://127.0.0.1:{PORT}"
    sh("docker", "rm", "-f", NAME, check=False)
    sh("docker", "run", "-d", "--name", NAME, "-p", f"127.0.0.1:{PORT}:8080",
       "-e", "GWT_SCHEDULE_ENABLED=false", "-v", f"{data}:/app/data", image)
    try:
        health = wait_healthy(base)
        print("healthz:", json.dumps(health))
        assert health["has_data"] and health["collection_paused"] is None, health
        check_routes(base)
        print(sh("docker", "exec", NAME, "python", "-c", CHECK_MIGRATED, json.dumps(before)).stdout.strip())
        print(sh("docker", "exec", NAME, "python", "-m", "gwt", "backup", "--out", "/app/data/backup.db").stdout.strip())
        stop = time.monotonic()
        sh("docker", "stop", "-t", "45", NAME)
        code = sh("docker", "inspect", "-f", "{{.State.ExitCode}}", NAME).stdout.strip()
        logs = sh("docker", "logs", NAME)
        print(f"stopped in {time.monotonic() - stop:.1f}s, exit code {code}")
        # uvicorn shuts down cleanly on SIGTERM, then re-raises it: 143 = 128 + SIGTERM.
        # 137 would mean Docker had to SIGKILL it.
        assert code in ("0", "143"), f"container exited with {code}"
        assert "Application shutdown complete" in logs.stdout + logs.stderr, "no graceful shutdown in the logs"
    except BaseException:
        logs = sh("docker", "logs", NAME, check=False)
        print(logs.stdout[-5000:], logs.stderr[-5000:], sep="\n", file=sys.stderr)
        raise
    finally:
        sh("docker", "rm", "-f", NAME, check=False)
    print("smoke test passed")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
