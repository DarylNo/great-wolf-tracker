"""FastAPI app: HTML dashboard pages, JSON API under /api, and /healthz."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterator
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from gwt import __version__, db
from gwt.config import Settings
from gwt.scheduler import build_scheduler, describe
from gwt.web import data

log = logging.getLogger("gwt.web")

TEMPLATES_DIR = Path(__file__).parent / "templates"
PLOTLY_JS = "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"


def _money(value) -> str:
    return "-" if value is None else f"${value:,.2f}"


def _yesno(value) -> str:
    return "?" if value is None else ("yes" if value else "no")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"not a date: {value!r} (use YYYY-MM-DD)")


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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.db_path.exists():
            db.connect(settings.db_path).close()  # apply schema migrations once
        scheduler = build_scheduler(settings.schedule, settings.db_path) if run_scheduler else None
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
            if scheduler:
                scheduler.shutdown(wait=False)

    app = FastAPI(title="Great Wolf Tracker", version=__version__, lifespan=lifespan)
    app.state.scheduler = None
    app.state.settings = settings

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["money"] = _money
    templates.env.filters["yesno"] = _yesno
    templates.env.globals["plotly_js"] = PLOTLY_JS
    templates.env.globals["version"] = __version__

    @contextmanager
    def open_db() -> Iterator[sqlite3.Connection | None]:
        conn = db.connect_readonly(settings.db_path)
        try:
            yield conn
        finally:
            if conn is not None:
                conn.close()

    def page(request: Request, template: str, status_code: int = 200, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, template, {"active": template, **ctx}, status_code=status_code)

    # ------------------------------------------------------------------ HTML pages

    @app.get("/", response_class=HTMLResponse)
    def overview_page(request: Request):
        with open_db() as conn:
            if not data.has_data(conn):
                return page(request, "overview.html", has_data=False)
            ov = data.overview(conn, today_fn())
            return page(request, "overview.html", has_data=True, ov=ov, hist=data.history(conn))

    @app.get("/date/{arrival}", response_class=HTMLResponse)
    def date_page(request: Request, arrival: str):
        d = _parse_date(arrival)
        with open_db() as conn:
            if not data.has_data(conn):
                return page(request, "date.html", has_data=False, arrival=d.isoformat(), detail=None)
            detail = data.date_detail(conn, d, today_fn())
            return page(request, "date.html", has_data=True, arrival=d.isoformat(), detail=detail)

    @app.get("/suites", response_class=HTMLResponse)
    def suites_page(request: Request):
        with open_db() as conn:
            if not data.has_data(conn):
                return page(request, "suites.html", has_data=False)
            return page(request, "suites.html", has_data=True, suites=data.suites_list(conn))

    @app.get("/suites/{code}", response_class=HTMLResponse)
    def suite_page(request: Request, code: str, date: str | None = None):
        curve_date = _parse_date(date) if date else None
        code = code.upper()
        with open_db() as conn:
            if not data.has_data(conn):
                return page(request, "suite.html", has_data=False, code=code, detail=None)
            detail = data.suite_detail(conn, code, today_fn(), curve_date)
            if detail is None:
                return page(request, "suite.html", status_code=404, has_data=True, code=code, detail=None)
            return page(request, "suite.html", has_data=True, code=code, detail=detail)

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        with open_db() as conn:
            runs = data.runs(conn) if conn is not None else []
            return page(
                request,
                "runs.html",
                has_data=bool(runs),
                runs=runs,
                schedule=settings.schedule,
                jobs=describe(app.state.scheduler),
            )

    @app.get("/trends", response_class=HTMLResponse)
    def trends_page(request: Request):
        with open_db() as conn:
            if not data.has_data(conn):
                return page(request, "trends.html", has_data=False)
            return page(request, "trends.html", has_data=True, hist=data.history(conn))

    # ------------------------------------------------------------------ JSON API

    @app.get("/api/overview")
    def api_overview():
        with open_db() as conn:
            if not data.has_data(conn):
                return {"has_data": False, "dates": []}
            ov = data.overview(conn, today_fn())
            return {"has_data": True, "start": ov["start"], "end": ov["end"], "dates": ov["dates"],
                    "quietest": ov["quietest"], "busiest": ov["busiest"]}

    @app.get("/api/trends")
    def api_trends():
        with open_db() as conn:
            if not data.has_data(conn):
                return {"has_data": False, "years": [], "weeks": [], "z": []}
            return {"has_data": True, **data.history(conn)}

    @app.get("/api/dates/{arrival}")
    def api_date(arrival: str):
        d = _parse_date(arrival)
        with open_db() as conn:
            detail = data.date_detail(conn, d, today_fn()) if data.has_data(conn) else None
            if detail is None:
                return JSONResponse({"has_data": False, "arrival_date": d.isoformat()}, status_code=404)
            return {"has_data": True, "arrival_date": d.isoformat(), **detail}

    @app.get("/api/suites")
    def api_suites():
        with open_db() as conn:
            if not data.has_data(conn):
                return {"has_data": False, "suites": []}
            return {"has_data": True, "suites": data.suites_list(conn)}

    @app.get("/api/suites/{code}")
    def api_suite(code: str, date: str | None = None):
        curve_date = _parse_date(date) if date else None
        with open_db() as conn:
            detail = data.suite_detail(conn, code.upper(), today_fn(), curve_date) if data.has_data(conn) else None
            if detail is None:
                return JSONResponse({"has_data": data.has_data(conn), "suite_code": code.upper()}, status_code=404)
            return {"has_data": True, **detail}

    @app.get("/api/runs")
    def api_runs():
        with open_db() as conn:
            runs = data.runs(conn) if conn is not None else []
            return {"runs": runs, "scheduler": {"enabled": app.state.scheduler is not None,
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
                "last_completed_run": data.last_completed_run(conn),
                "scheduler_running": bool(app.state.scheduler and app.state.scheduler.running),
            }

    return app
