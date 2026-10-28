"""Command-line entry point: `python -m gwt sweep|suites|serve|unblock|backup|demo-db`."""

from __future__ import annotations

import argparse
import logging
import os
from datetime import date
from pathlib import Path

from gwt import db
from gwt.locking import SweepBusy
from gwt.sweep import SweepAborted, SweepPaused, execute_sweep

EXIT_ABORTED = 1
EXIT_USAGE = 2
EXIT_BUSY = 3
EXIT_PAUSED = 4
EXIT_INTERRUPTED = 130  # Ctrl-C, as shells report it


def _default_db() -> str:
    return os.environ.get("GWT_DB") or str(db.DEFAULT_DB_PATH)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gwt", description="Great Wolf Lodge Niagara demand tracker")
    sub = parser.add_subparsers(dest="command", required=True)
    db_help = "SQLite path (default $GWT_DB or data/gwl.db)"

    sweep = sub.add_parser("sweep", help="fetch availability for upcoming arrival dates")
    sweep.add_argument("--days", type=int, default=365, help="number of arrival dates from --start (default 365)")
    sweep.add_argument("--start", type=date.fromisoformat, default=None,
                       help="first arrival date, YYYY-MM-DD (default: today in Toronto)")
    sweep.add_argument("--db", default=_default_db(), help=db_help)
    sweep.add_argument(
        "--run-id",
        default=None,
        help="run identifier used for resume; re-using it skips dates already captured (default: today's date in Toronto)",
    )
    sweep.add_argument("--force", action="store_true",
                       help="sweep even though collection is paused after a 403 (prefer `gwt unblock` after reviewing)")
    sweep.add_argument("-v", "--verbose", action="store_true")

    unblock = sub.add_parser("unblock", help="resume scheduled sweeps after reviewing a blocked (403) run")
    unblock.add_argument("--db", default=_default_db(), help=db_help)

    backup = sub.add_parser("backup", help="consistent copy of the database (safe while it's in use)")
    backup.add_argument("--out", required=True, help="where to write the copy")
    backup.add_argument("--db", default=_default_db(), help=db_help)
    backup.add_argument("--force", action="store_true", help="overwrite an existing file at --out")

    suites = sub.add_parser("suites", help="show stored suite availability for one arrival date")
    suites.add_argument("--date", type=date.fromisoformat, required=True, help="arrival date, YYYY-MM-DD")
    suites.add_argument("--code", default=None, help="suite code (e.g. SFSSB): show its booking curve across runs")
    suites.add_argument("--db", default=_default_db(), help=db_help)

    serve = sub.add_parser("serve", help="run the web dashboard (and the sweep scheduler unless disabled)")
    serve.add_argument("--host", default=None, help="bind address (default $GWT_HOST or 0.0.0.0)")
    serve.add_argument("--port", type=int, default=None, help="port (default $GWT_PORT or 8080)")
    serve.add_argument("--db", default=None, help=db_help)
    serve.add_argument("--no-scheduler", action="store_true", help="web only, same as GWT_SCHEDULE_ENABLED=false")

    demo = sub.add_parser("demo-db", help="write a synthetic demo database (never the real one)")
    demo.add_argument("--out", required=True, help="where to write the demo database")
    demo.add_argument("--weeks", type=int, default=60, help="weeks of weekly full sweeps to simulate (default 60)")
    demo.add_argument("--seed", type=int, default=7, help="random seed (default 7)")
    demo.add_argument("--force", action="store_true", help="replace an existing demo database at --out")
    return parser


# Paths the real database lives at; demo-db refuses to write to any of them.
REAL_DB_PATHS = ("data/gwl.db", "/app/data/gwl.db")


def real_db_paths() -> set[Path]:
    paths = {Path(p).resolve() for p in REAL_DB_PATHS}
    if os.environ.get("GWT_DB"):
        paths.add(Path(os.environ["GWT_DB"]).resolve())
    return paths


def cmd_demo_db(args: argparse.Namespace) -> int:
    from gwt.demo import build_demo_db, is_demo_db

    out = Path(args.out).resolve()
    if out in real_db_paths():
        logging.error("refusing to write the demo database to the real database path %s", out)
        return EXIT_USAGE
    if args.weeks < 1:
        logging.error("--weeks must be >= 1")
        return EXIT_USAGE
    if out.exists():
        if not args.force:
            logging.error("%s already exists (use --force to replace an earlier demo database)", out)
            return EXIT_USAGE
        if not is_demo_db(out):
            logging.error("%s is not a demo database; refusing to overwrite it", out)
            return EXIT_USAGE
        for suffix in ("", "-wal", "-shm"):
            Path(f"{out}{suffix}").unlink(missing_ok=True)
    result = build_demo_db(out, weeks=args.weeks, seed=args.seed, progress=logging.info)
    logging.info(
        "demo database written to %s: %d sweeps, %d snapshots, %d suite rows (synthetic data)",
        out, result["sweeps"], result["snapshots"], result["suite_snapshots"],
    )
    return 0


def cmd_unblock(args: argparse.Namespace, out=print) -> int:
    path = Path(args.db)
    if not path.exists():
        out(f"No database at {path}.")
        return EXIT_USAGE
    conn = db.connect(path)
    try:
        cleared = db.acknowledge_block(conn)
    finally:
        conn.close()
    if cleared is None:
        out("Collection is not paused; nothing to unblock.")
        return 0
    out(f"Unblocked. The 403 on {cleared['date']} (run {cleared['run_id']}) was: {cleared['message']}")
    out("Scheduled sweeps will run again at their next time.")
    return 0


def cmd_backup(args: argparse.Namespace, out=print) -> int:
    src, dst = Path(args.db), Path(args.out)
    if dst.resolve() == src.resolve():
        out("--out must be a different file from the database.")
        return EXIT_USAGE
    if dst.exists():
        if not args.force:
            out(f"{dst} already exists (use --force to overwrite it).")
            return EXIT_USAGE
        for suffix in ("", "-wal", "-shm"):
            Path(f"{dst}{suffix}").unlink(missing_ok=True)
    try:
        info = db.backup(src, dst)
    except FileNotFoundError as exc:
        out(str(exc))
        return EXIT_USAGE
    counts = ", ".join(f"{k} {v}" for k, v in info.items() if k not in ("quick_check", "bytes"))
    out(f"Backed up {src} to {dst} ({info['bytes'] / 1e6:.1f} MB; {counts}); integrity check: {info['quick_check']}")
    return 0 if info["quick_check"] == "ok" else EXIT_ABORTED


def _fmt_rate(value: float | None) -> str:
    return "-" if value is None else f"${value:,.2f}"


def format_table(headers: list[str], rows: list[list[str]], right_align: set[int] = frozenset()) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, row)]

    def line(cells: list[str]) -> str:
        return "  ".join(
            c.rjust(w) if i in right_align else c.ljust(w) for i, (c, w) in enumerate(zip(cells, widths))
        ).rstrip()

    out = [line(headers), line(["-" * w for w in widths])]
    out.extend(line(r) for r in rows)
    return "\n".join(out)


def cmd_suites(args: argparse.Namespace, out=print) -> int:
    path = Path(args.db)
    if not path.exists():
        out(f"No database at {path}. Run `python -m gwt sweep` first.")
        return 1
    conn = db.connect(path)
    try:
        arrival = args.date.isoformat()
        if args.code:
            code = args.code.upper()
            history = db.suite_history(conn, arrival, code)
            if not history:
                out(f"No snapshots of suite {code} for arrival {arrival}.")
                return 1
            family_titles: list[str] = []
            families_by_run = {}
            for h in history:
                fams = db.rate_families_for(conn, arrival, code, h["run_id"])
                families_by_run[h["run_id"]] = {f["family_title"]: f["min_total_nightly_rate"] for f in fams}
                for f in fams:
                    if f["family_title"] not in family_titles:
                        family_titles.append(f["family_title"])
            headers = ["Scraped (UTC)", "Days out", "Rooms left", "Cheapest", *family_titles, "Sold out"]
            rows = [
                [
                    h["scraped_at"],
                    str(h["days_out"]),
                    str(h["available_room_count"]),
                    _fmt_rate(h["min_total_nightly_rate"]),
                    *[_fmt_rate(families_by_run[h["run_id"]].get(t)) for t in family_titles],
                    "yes" if h["available_room_count"] == 0 else "",
                ]
                for h in history
            ]
            out(f"Booking curve for {code}, arrival {arrival} ({len(history)} snapshots)")
            out(format_table(headers, rows, right_align=set(range(1, 4 + len(family_titles)))))
            return 0

        scraped_at, suites = db.latest_suites_for_date(conn, arrival)
        if not suites:
            out(f"No snapshots for arrival {arrival}.")
            return 1
        rows = [
            [
                s["suite_name"],
                s["suite_code"],
                s["suite_category"] or "-",
                str(s["available_room_count"]),
                _fmt_rate(s["min_total_nightly_rate"]),
                "SOLD OUT" if s["available_room_count"] == 0 else "",
            ]
            for s in suites
        ]
        total = sum(s["available_room_count"] for s in suites)
        sold = sum(1 for s in suites if s["available_room_count"] == 0)
        out(f"Arrival {arrival}, scraped {scraped_at}: {total} rooms left, {sold}/{len(suites)} suite types sold out")
        out(format_table(["Suite", "Code", "Category", "Rooms left", "Cheapest", "Sold out"], rows, right_align={3, 4}))
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "sweep":
        if args.days < 1:
            logging.error("--days must be >= 1")
            return EXIT_USAGE
        try:
            result = execute_sweep(args.db, days=args.days, start=args.start, run_id=args.run_id, trigger="cli",
                                   force=args.force)
        except SweepPaused as exc:
            logging.error("%s Last blocked run %s: %s", exc, exc.pause["run_id"], exc.pause["message"])
            logging.error("Refusing to sweep. Review, then run `python -m gwt unblock` (or pass --force).")
            return EXIT_PAUSED
        except SweepBusy as exc:
            logging.error("%s", exc)
            return EXIT_BUSY
        except SweepAborted as exc:
            logging.error("%s", exc)
            return EXIT_ABORTED
        except KeyboardInterrupt:
            logging.error("interrupted; recorded as 'interrupted' (sweeping again with the same run ID, "
                          "by default today's date, resumes where it stopped)")
            return EXIT_INTERRUPTED
        logging.info(
            "run %s complete: fetched=%d skipped=%d failed_dates=%d",
            result.run_id,
            result.fetched,
            result.skipped,
            result.failed_dates,
        )
        return 0
    if args.command == "suites":
        return cmd_suites(args)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "demo-db":
        return cmd_demo_db(args)
    if args.command == "unblock":
        return cmd_unblock(args)
    if args.command == "backup":
        return cmd_backup(args)
    return EXIT_USAGE


def cmd_serve(args: argparse.Namespace) -> int:
    import dataclasses

    import uvicorn

    from gwt.config import ConfigError, Settings
    from gwt.web.app import create_app

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        logging.error("bad configuration: %s", exc)
        return EXIT_USAGE
    overrides = {}
    if args.db:
        overrides["db_path"] = Path(args.db)
    if args.host:
        overrides["host"] = args.host
    if args.port:
        overrides["port"] = args.port
    settings = dataclasses.replace(settings, **overrides)

    app = create_app(settings, enable_scheduler=False if args.no_scheduler else None)
    logging.info("serving on http://%s:%d (db %s)", settings.host, settings.port, settings.db_path)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info", access_log=False)
    return 0
