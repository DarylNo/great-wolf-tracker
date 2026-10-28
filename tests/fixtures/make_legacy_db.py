#!/usr/bin/env python3
"""Build tests/fixtures/legacy-84181f2.db: a database written by the code of commit 84181f2.

84181f2 is the build that ran on the live server before the dashboard redesign, so this file
has the live database's real old schema (no `meta` table, no suites.max_rooms_seen /
min_rate_seen, fewer indexes) and rows written by the old code paths (raw_json as plain
TEXT). tests/test_migration.py and the CI smoke test upgrade a copy of it with the current code.

Regenerate it (no network: the old sweep runs against a mock HTTP transport):

    mkdir /tmp/gwt-84181f2 && git archive 84181f2 | tar -x -C /tmp/gwt-84181f2
    python tests/fixtures/make_legacy_db.py /tmp/gwt-84181f2 tests/fixtures/legacy-84181f2.db

What's in it (Toronto dates; every scrape at a fixed fake time):
- 2026-09-06 weekly sweep, 14 nights from Sep 6, completed
- 2026-09-13 weekly sweep, blocked by an HTTP 403 on the 5th night (an older block, not the
  latest run, so the upgraded app must NOT be paused), then resumed from the CLI the same
  day under the same run ID and completed
- 2026-09-14 daily sweep, 7 nights, one 429 retried, completed
- 2026-09-20 weekly sweep, 14 nights, suite TWLFD renamed "Wolf Den Bunk Suite" by the API
- 2026-09-21 daily sweep aborted after 5 consecutive HTTP 500s (nothing fetched)
- 2026-09-22 daily sweep, 7 nights, completed (the latest run)
"""

from __future__ import annotations

import copy
import json
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent


def main(src: Path, out: Path) -> None:
    sys.path.insert(0, str(src))
    import gwt  # noqa: E402  (the 84181f2 checkout, not this one)

    if not Path(gwt.__file__).resolve().is_relative_to(src):
        raise SystemExit(f"imported gwt from {gwt.__file__}, not from {src}")
    import httpx
    from gwt import db
    from gwt.sweep import SweepAborted, run_sweep

    if out.exists():
        raise SystemExit(f"{out} exists; delete it first")
    template = json.loads((FIXTURES / "availability_sample.json").read_text())
    rng = random.Random(84181)
    base_rooms = {s["suiteCode"]: max(s["availableRoomCount"], 3) + 6 for s in template["suites"]}

    def payload(run_no: int, arrival: date, rename: bool) -> dict:
        p = copy.deepcopy(template)
        for s in p["suites"]:
            lead = (arrival - date(2026, 9, 6)).days
            booked = min(0.15 * run_no + 0.5 / (1 + lead / 10) + rng.uniform(-0.05, 0.05), 1.0)
            rooms = max(0, round(base_rooms[s["suiteCode"]] * (1 - booked)))
            if s["suiteCode"] == "PGRZL":
                rooms = 0  # always sold out, like the sample
            s["availableRoomCount"] = rooms
            if rename and s["suiteCode"] == "TWLFD":
                s["suiteBrand"] = "Wolf Den Bunk Suite"
            if rooms == 0:
                s["rateFamilies"] = []
            for fam in s["rateFamilies"]:
                for r in fam["rates"]:
                    price = round(r["totalNightlyRate"] * (1 + 0.04 * run_no) + rng.choice([0, 10, 20]), 2)
                    r["rate"] = r["totalRate"] = r["totalNightlyRate"] = price
        return p

    def clock_from(start_utc: str):
        t = datetime.fromisoformat(start_utc)

        def tick() -> str:
            nonlocal t
            t += timedelta(seconds=4)
            return t.astimezone(timezone.utc).replace(microsecond=0).isoformat()

        return tick

    def client(responses) -> httpx.Client:
        return httpx.Client(base_url="https://prod.api.greatwolf.com",
                            transport=httpx.MockTransport(lambda request: next(responses)(request)))

    def ok(run_no: int, rename: bool = False):
        def respond(request):
            arrival = date.fromisoformat(request.url.params["arrival"])
            return httpx.Response(200, json=payload(run_no, arrival, rename))

        return respond

    def status(code: int):
        return lambda request: httpx.Response(code, headers={"Retry-After": "1"} if code == 429 else {})

    conn = db.connect(out)
    quiet = dict(sleep=lambda s: None, rng=random.Random(0))

    run_sweep(conn, client(iter([ok(0)] * 14)), days=14, start=date(2026, 9, 6), run_id="2026-09-06",
              trigger="schedule:weekly", clock=clock_from("2026-09-06T07:17:00+00:00"), **quiet)
    try:
        run_sweep(conn, client(iter([ok(1)] * 4 + [status(403)])), days=14, start=date(2026, 9, 13),
                  run_id="2026-09-13", trigger="schedule:weekly", clock=clock_from("2026-09-13T07:17:00+00:00"),
                  **quiet)
    except SweepAborted:
        pass
    run_sweep(conn, client(iter([ok(1)] * 10)), days=14, start=date(2026, 9, 13), run_id="2026-09-13",
              trigger="cli", clock=clock_from("2026-09-13T19:05:00+00:00"), **quiet)
    run_sweep(conn, client(iter([status(429)] + [ok(1)] * 7)), days=7, start=date(2026, 9, 14),
              run_id="2026-09-14", trigger="schedule:daily", clock=clock_from("2026-09-14T09:43:00+00:00"), **quiet)
    run_sweep(conn, client(iter([ok(2, rename=True)] * 14)), days=14, start=date(2026, 9, 20), run_id="2026-09-20",
              trigger="schedule:weekly", clock=clock_from("2026-09-20T07:17:00+00:00"), **quiet)
    try:
        run_sweep(conn, client(iter([status(500)] * 5)), days=7, start=date(2026, 9, 21), run_id="2026-09-21",
                  trigger="schedule:daily", clock=clock_from("2026-09-21T09:43:00+00:00"), **quiet)
    except SweepAborted:
        pass
    run_sweep(conn, client(iter([ok(3, rename=True)] * 7)), days=7, start=date(2026, 9, 22), run_id="2026-09-22",
              trigger="schedule:daily", clock=clock_from("2026-09-22T09:43:00+00:00"), **quiet)
    conn.close()

    # Fold the WAL into the file so the fixture is a single self-contained file. It stays in
    # WAL mode, like the live database.
    raw = sqlite3.connect(out)
    raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    raw.execute("VACUUM")
    raw.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{out}{suffix}").unlink(missing_ok=True)
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KiB)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
