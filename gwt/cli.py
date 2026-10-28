"""Command-line entry point: `python -m gwt sweep --days 365`."""

from __future__ import annotations

import argparse
import logging
from datetime import date

from gwt import db
from gwt.collector import make_client
from gwt.sweep import SweepAborted, run_sweep


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gwt", description="Great Wolf Lodge Niagara demand tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    sweep = sub.add_parser("sweep", help="fetch availability for upcoming arrival dates")
    sweep.add_argument("--days", type=int, default=365, help="number of arrival dates from --start (default 365)")
    sweep.add_argument("--start", type=date.fromisoformat, default=None, help="first arrival date, YYYY-MM-DD (default: today UTC)")
    sweep.add_argument("--db", default=str(db.DEFAULT_DB_PATH), help="SQLite path (default data/gwl.db)")
    sweep.add_argument(
        "--run-id",
        default=None,
        help="run identifier used for resume; re-using it skips dates already captured (default: today's UTC date)",
    )
    sweep.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "sweep":
        if args.days < 1:
            logging.error("--days must be >= 1")
            return 2
        conn = db.connect(args.db)
        try:
            with make_client() as client:
                result = run_sweep(conn, client, days=args.days, start=args.start, run_id=args.run_id)
        except SweepAborted as exc:
            logging.error("%s", exc)
            return 1
        finally:
            conn.close()
        logging.info(
            "run %s complete: fetched=%d skipped=%d failed_dates=%d",
            result.run_id,
            result.fetched,
            result.skipped,
            result.failed_dates,
        )
        return 0
    return 2
