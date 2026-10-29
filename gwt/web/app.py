"""FastAPI app: dashboard pages, JSON API under /api, /healthz, and static assets."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import threading
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime
from importlib import metadata, resources
from pathlib import Path
from typing import Callable, Iterator
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware

from gwt import __version__, db, events
from gwt.collector import booking_url
from gwt import metrics as M
from gwt.config import Settings
from gwt.scheduler import build_scheduler, describe
from gwt.web import data, fmt

log = logging.getLogger("gwt.web")

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
MIGRATION_LOCK_WAIT = 10.0  # seconds web startup waits for a running sweep's lock before migrating anyway
RUNS_PER_PAGE = 50


def plotly_js_path() -> Path | None:
    """plotly.min.js bundled with the `plotly` pip package, served locally (works offline)."""
    try:
        path = Path(str(resources.files("plotly") / "package_data" / "plotly.min.js"))
    except ModuleNotFoundError:
        return None
    return path if path.exists() else None


def _asset_version() -> str:
    h = hashlib.sha256()
    for p in sorted(STATIC_DIR.glob("*")):
        h.update(p.read_bytes())
    try:
        h.update(metadata.version("plotly").encode())
    except metadata.PackageNotFoundError:
        pass
    return h.hexdigest()[:10]


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"not a date: {value!r} (use YYYY-MM-DD)")


def _iso_label(value: str | None) -> str:
    return fmt.date_long(date.fromisoformat(value)) if value else "–"


PLAN_NIGHT_FIELDS = (
    "date", "weekday", "weekend", "has_data", "rooms_left", "pct_sold", "min_rate", "ref_rate", "sold_out",
    "lead_days", "scraped_on", "pace_pct", "pickup_7d", "room_types_sold_out", "room_types", "school_closure",
)


def _api_night(n: dict) -> dict:
    """Every field on every night (null where a night has no data yet)."""
    return {k: n.get(k) for k in PLAN_NIGHT_FIELDS}


def create_app(
    settings: Settings | None = None,
    *,
    enable_scheduler: bool | None = None,
    today: Callable[[], date] | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    run_scheduler = settings.schedule.enabled if enable_scheduler is None else enable_scheduler
    tz = ZoneInfo(settings.schedule.timezone)
    today_fn = today or (lambda: datetime.now(tz).date())
    stats_key = str(settings.db_path.resolve())

    stop = threading.Event()  # set on shutdown: a running sweep stops between dates

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Migrate before serving: the server doesn't accept requests until this returns.
        if settings.db_path.exists():
            outcome = await asyncio.to_thread(db.migrate_file, settings.db_path, MIGRATION_LOCK_WAIT)
            log.info("database %s: %s", settings.db_path, outcome)
        scheduler = build_scheduler(settings.schedule, settings.db_path, stop=stop) if run_scheduler else None
        if scheduler:
            scheduler.start()
            for job in describe(scheduler):
                log.info("scheduled: %s, next run %s", job["name"], job["next_run"])
        else:
            log.info("scheduler disabled")
        app.state.scheduler = scheduler
        try:
            yield
        finally:
            stop.set()
            if scheduler:
                # The running sweep (if any) sees `stop`, records itself as interrupted and
                # returns; wait for that so the container's stop grace period covers it.
                await asyncio.to_thread(scheduler.shutdown, True)

    app = FastAPI(title="Great Wolf Tracker", version=__version__, lifespan=lifespan)
    app.state.scheduler = None
    app.state.settings = settings
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    env = templates.env
    env.filters.update(
        money=fmt.money, money0=fmt.money0, pct=fmt.pct, pace_rank=fmt.pace_rank, pace=fmt.pace, yesno=fmt.yesno,
        signed=fmt.signed, datelong=_iso_label, localtime=data.local_time, booking_url=booking_url,
    )
    env.globals.update(
        version=__version__,
        asset_version=_asset_version(),
        pace_label=M.PACE_LABEL,
        price_footnote=M.PRICE_FOOTNOTE,
        ref_label=data.REF_SUITE_LABEL,
        lead_caption=data.LEAD_CAPTION,
        plan_rules={"cheapest": data.CHEAPEST_RULE, "filling": data.FILLING_RULE},
    )
    plotly_path = plotly_js_path()

    @contextmanager
    def open_db() -> Iterator[sqlite3.Connection | None]:
        conn = db.connect_readonly(settings.db_path)
        try:
            yield conn
        finally:
            if conn is not None:
                conn.close()

    def stats_for(conn: sqlite3.Connection) -> data.Stats:
        return data.get_stats(conn, stats_key, settings.final_fill_max_lead)

    def page(request: Request, template: str, conn, status_code: int = 200, **ctx) -> HTMLResponse:
        pause = db.collection_pause(conn) if conn is not None else None
        base = {
            "active": template,
            "demo": data.is_demo(conn),
            "today": today_fn(),
            "pause": pause,
            "pause_message": db.pause_message(pause) if pause else None,
            "events_problem": events.events_problem(),
        }
        return templates.TemplateResponse(request, template, {**base, **ctx}, status_code=status_code)

    # ------------------------------------------------------------------ HTML pages

    @app.get("/", response_class=HTMLResponse)
    def plan_page(request: Request, metric: str = "price"):
        metric = metric if metric in ("price", "rooms", "pace") else "price"
        with open_db() as conn:
            if not data.has_data(conn):
                return page(request, "plan.html", conn, has_data=False, metric=metric)
            stats = stats_for(conn)
            return page(request, "plan.html", conn, has_data=True, metric=metric,
                        plan=data.plan(conn, stats, today_fn()), status=data.status(stats))

    @app.get("/seasonality", response_class=HTMLResponse)
    def seasonality_page(request: Request):
        with open_db() as conn:
            if not data.has_data(conn):
                return page(request, "seasonality.html", conn, has_data=False)
            stats = stats_for(conn)
            return page(request, "seasonality.html", conn, has_data=True,
                        season=data.seasonality(stats, today_fn()), status=data.status(stats))

    @app.get("/trends")
    def trends_redirect():
        return RedirectResponse("/seasonality", status_code=301)

    @app.get("/date/{arrival}", response_class=HTMLResponse)
    def date_page(request: Request, arrival: str):
        d = _parse_date(arrival)
        with open_db() as conn:
            if not data.has_data(conn):
                return page(request, "date.html", conn, has_data=False, arrival=d, detail=None)
            stats = stats_for(conn)
            detail = data.date_detail(conn, stats, d, today_fn())
            return page(request, "date.html", conn, has_data=True, arrival=d, detail=detail,
                        status=data.status(stats))

    @app.get("/suites", response_class=HTMLResponse)
    def suites_page(request: Request, window: int = data.MATRIX_DEFAULT_WINDOW):
        window = window if window in data.MATRIX_WINDOWS else data.MATRIX_DEFAULT_WINDOW
        with open_db() as conn:
            if not data.has_data(conn):
                return page(request, "suites.html", conn, has_data=False, window=window)
            stats = stats_for(conn)
            return page(request, "suites.html", conn, has_data=True, window=window,
                        rooms=data.rooms_overview(conn, stats),
                        matrix=data.room_matrix(conn, stats, today_fn(), window))

    @app.get("/rooms/{slug}", response_class=HTMLResponse)
    def room_page(request: Request, slug: str, date: str | None = None):
        curve_date = _parse_date(date) if date else None
        with open_db() as conn:
            if not data.has_data(conn):
                return page(request, "room.html", conn, has_data=False, slug=slug, detail=None)
            detail = data.room_detail(conn, stats_for(conn), slug, today_fn(), curve_date)
            return page(request, "room.html", conn, status_code=200 if detail else 404, has_data=True,
                        slug=slug, detail=detail)

    @app.get("/suites/{code}", response_class=HTMLResponse)
    def suite_page(request: Request, code: str, date: str | None = None):
        curve_date = _parse_date(date) if date else None
        code = code.upper()
        with open_db() as conn:
            if not data.has_data(conn):
                return page(request, "suite.html", conn, has_data=False, code=code, detail=None)
            detail = data.suite_detail(conn, stats_for(conn), code, today_fn(), curve_date)
            return page(request, "suite.html", conn, status_code=200 if detail else 404, has_data=True,
                        code=code, detail=detail)

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request, page_no: int = Query(1, alias="page", ge=1)):
        with open_db() as conn:
            total = data.runs_count(conn) if conn is not None else 0
            pages = max(1, -(-total // RUNS_PER_PAGE))
            page_no = min(page_no, pages)
            runs = data.runs(conn, limit=RUNS_PER_PAGE, offset=(page_no - 1) * RUNS_PER_PAGE) if conn is not None else []
            return page(request, "runs.html", conn, has_data=bool(runs), runs=runs, schedule=settings.schedule,
                        jobs=describe(app.state.scheduler), page_no=page_no, pages=pages, total_runs=total)

    # ------------------------------------------------------------------ JSON API

    @app.get("/api/plan")
    def api_plan():
        with open_db() as conn:
            if not data.has_data(conn):
                return {"has_data": False, "nights": []}
            stats = stats_for(conn)
            p = data.plan(conn, stats, today_fn())
            return {
                "has_data": True,
                "start": p["start"],
                "end": p["end"],
                "status": data.status(stats),
                "definitions": {"price": "min_rate: lowest available nightly rate", "pace": M.PACE_LABEL,
                                "price_note": M.PRICE_FOOTNOTE},
                "nights": [_api_night(n) for n in p["nights"]],
                "cheapest_nights": {"rule": data.CHEAPEST_RULE, "nights": [_api_night(n) for n in p["cheapest"]]},
                "filling_fastest": {"rule": data.FILLING_RULE, "nights": [_api_night(n) for n in p["filling"]]},
            }

    @app.get("/api/seasonality")
    def api_seasonality():
        with open_db() as conn:
            if not data.has_data(conn):
                return {"has_data": False, "final_fill_nights": 0, "panels": {}, "forecast": []}
            stats = stats_for(conn)
            return {"has_data": True, "status": data.status(stats), **data.seasonality(stats, today_fn())}

    @app.get("/api/dates/{arrival}")
    def api_date(arrival: str):
        d = _parse_date(arrival)
        with open_db() as conn:
            detail = data.date_detail(conn, stats_for(conn), d, today_fn()) if data.has_data(conn) else None
            if detail is None:
                return JSONResponse({"has_data": False, "arrival_date": d.isoformat()}, status_code=404)
            return {"has_data": True, **detail}

    @app.get("/api/rooms")
    def api_rooms():
        with open_db() as conn:
            if not data.has_data(conn):
                return {"has_data": False, "rooms": []}
            return {"has_data": True, "rooms": data.rooms_overview(conn, stats_for(conn))}

    @app.get("/api/rooms/matrix")
    def api_room_matrix(window: int = data.MATRIX_DEFAULT_WINDOW):
        window = window if window in data.MATRIX_WINDOWS else data.MATRIX_DEFAULT_WINDOW
        with open_db() as conn:
            if not data.has_data(conn):
                return {"has_data": False, "window": window, "dates": [], "rooms": []}
            m = data.room_matrix(conn, stats_for(conn), today_fn(), window)
            return {
                "has_data": True,
                "window": window,
                "dates": [c["date"] for c in m["columns"]],
                "rooms": [
                    {"name": r["name"], "slug": r["slug"], "category": r["category"], "most_rooms_seen": r["capacity"],
                     "rooms_left": [c.get("rooms_left") for c in r["cells"]],
                     "cheapest_rate": [c.get("rate") for c in r["cells"]]}
                    for r in m["rows"]
                ],
            }

    @app.get("/api/rooms/{slug}")
    def api_room(slug: str, date: str | None = None):
        curve_date = _parse_date(date) if date else None
        with open_db() as conn:
            detail = data.room_detail(conn, stats_for(conn), slug, today_fn(), curve_date) if data.has_data(conn) else None
            if detail is None:
                return JSONResponse({"has_data": data.has_data(conn), "slug": slug}, status_code=404)
            return {"has_data": True, **detail}

    @app.get("/api/suites")
    def api_suites():
        with open_db() as conn:
            if not data.has_data(conn):
                return {"has_data": False, "suites": []}
            rooms = data.rooms_overview(conn, stats_for(conn))
            return {"has_data": True, "suites": [
                {**v, "name": r["name"], "room_slug": r["slug"], "category": r["category"]}
                for r in rooms for v in r["variants"]
            ]}

    @app.get("/api/suites/{code}")
    def api_suite(code: str, date: str | None = None):
        curve_date = _parse_date(date) if date else None
        with open_db() as conn:
            detail = (data.suite_detail(conn, stats_for(conn), code.upper(), today_fn(), curve_date)
                      if data.has_data(conn) else None)
            if detail is None:
                return JSONResponse({"has_data": data.has_data(conn), "suite_code": code.upper()}, status_code=404)
            return {"has_data": True, **detail}

    @app.get("/api/runs")
    def api_runs(page_no: int = Query(1, alias="page", ge=1)):
        with open_db() as conn:
            total = data.runs_count(conn) if conn is not None else 0
            runs = data.runs(conn, limit=RUNS_PER_PAGE, offset=(page_no - 1) * RUNS_PER_PAGE) if conn is not None else []
            return {"runs": runs, "page": page_no, "per_page": RUNS_PER_PAGE, "total": total,
                    "collection_paused": db.collection_pause(conn) if conn is not None else None,
                    "scheduler": {"enabled": app.state.scheduler is not None,
                                  "timezone": settings.schedule.timezone,
                                  "jobs": describe(app.state.scheduler)}}

    @app.get("/healthz")
    def healthz():
        with open_db() as conn:
            return {
                "status": "ok",
                "version": __version__,
                "db_exists": conn is not None,
                "has_data": data.has_data(conn),
                "synthetic_demo": data.is_demo(conn),
                "last_completed_run": data.last_completed_run(conn),
                "collection_paused": db.collection_pause(conn) if conn is not None else None,
                "events_problem": events.events_problem(),
                "scheduler_running": bool(app.state.scheduler and app.state.scheduler.running),
            }

    # ------------------------------------------------------------------ static assets

    @app.get("/static/plotly.min.js")
    def plotly_js():
        if plotly_path is None:
            raise HTTPException(status_code=404, detail="plotly package not installed")
        return FileResponse(plotly_path, media_type="text/javascript",
                            headers={"Cache-Control": "public, max-age=604800"})

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app
