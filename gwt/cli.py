"""Command-line entry point: `python -m gwt sweep --days 365`."""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

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

    suites = sub.add_parser("suites", help="show stored suite availability for one arrival date")
    suites.add_argument("--date", type=date.fromisoformat, required=True, help="arrival date, YYYY-MM-DD")
    suites.add_argument("--code", default=None, help="suite code (e.g. SFSSB): show its booking curve across runs")
    suites.add_argument("--db", default=str(db.DEFAULT_DB_PATH), help="SQLite path (default data/gwl.db)")
    return parser


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
    if args.command == "suites":
        return cmd_suites(args)
    return 2
