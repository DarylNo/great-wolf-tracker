"""Dashboard pages and JSON API against a temp db seeded from the fixture."""

import copy
import random
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gwt import db
from gwt.collector import parse_availability
from gwt.config import ScheduleConfig, Settings
from gwt.sweep import SweepAborted, run_sweep
from gwt.web.app import create_app

TODAY = date(2026, 11, 1)
NO_SCHEDULE = ScheduleConfig(enabled=False, timezone="America/Toronto", jobs=())


def make_client(db_path: Path) -> TestClient:
    settings = Settings(db_path=db_path, host="127.0.0.1", port=8080, schedule=NO_SCHEDULE)
    return TestClient(create_app(settings, today=lambda: TODAY))


def _variant(payload: dict, sfssb_rooms: int, saver: float) -> dict:
    p = copy.deepcopy(payload)
    p["suites"][0]["availableRoomCount"] = sfssb_rooms
    p["suites"][0]["rateFamilies"][0]["rates"][0]["totalNightlyRate"] = saver
    return p


@pytest.fixture
def seeded_db(tmp_path, sample_payload) -> Path:
    path = tmp_path / "gwl.db"
    conn = db.connect(path)

    # Two weekly sweeps over the first 10 upcoming days via the real sweep loop (logs `runs` too).
    def handler_for(run: int):
        def handler(request):
            d = date.fromisoformat(request.url.params["arrival"])
            offset = (d - TODAY).days
            rooms = max(12 - offset - 4 * run, 0)
            return httpx.Response(200, json=_variant(sample_payload, rooms, 379.99 + 10 * offset + 50 * run))
        return handler

    for run, (run_id, ts) in enumerate([("2026-10-25", "2026-10-25T03:17:00+00:00"),
                                        ("2026-11-01", "2026-11-01T03:17:00+00:00")]):
        client = httpx.Client(base_url="https://prod.api.greatwolf.com", transport=httpx.MockTransport(handler_for(run)))
        run_sweep(conn, client, days=10, start=TODAY, run_id=run_id, trigger="schedule:weekly",
                  sleep=lambda s: None, rng=random.Random(0), clock=lambda ts=ts: ts)

    # A blocked run for the Runs page.
    blocked = httpx.Client(base_url="https://prod.api.greatwolf.com",
                           transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    with pytest.raises(SweepAborted):
        run_sweep(conn, blocked, days=3, start=TODAY, run_id="2026-11-02", trigger="cli",
                  sleep=lambda s: None, clock=lambda: "2026-11-02T10:00:00+00:00")

    # Past history across two years for the ISO-week / trends views.
    for year in (2025, 2026):
        for d in (date(year, 7, 14), date(year, 7, 15), date(year, 3, 10)):
            snap = parse_availability(_variant(sample_payload, 2 if d.month == 7 else 10, 500.0), d)
            db.save_snapshot(conn, f"{year}-hist", f"{d.isoformat()}T05:43:00+00:00", snap)
    conn.close()
    return path


@pytest.fixture
def client(seeded_db):
    with make_client(seeded_db) as c:
        yield c


# --------------------------------------------------------------------------- pages


def test_overview_page(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Overview" in html and 'id="cal"' in html and 'id="iso"' in html
    assert "plotly.js-dist-min@2.35.2" in html
    assert "Quietest upcoming dates" in html
    assert "/date/2026-11-10" in html  # emptiest seeded date shows in the quietest list
    assert "No data yet" not in html


def test_date_page(client):
    r = client.get("/date/2026-11-01")
    assert r.status_code == 200
    html = r.text
    assert "Arrival 2026-11-01" in html
    assert "Superior Family Suite" in html and "Grizzly Bear Suite" in html
    assert "Saver" in html and "Flexible" in html  # one column per rate family
    assert "Sold out" in html  # SFSSB is sold out in the latest run for this date
    assert 'id="curve"' in html
    assert "2026-10-25T03:17:00+00:00" in html and "2026-11-01T03:17:00+00:00" in html  # curve points


def test_date_page_without_snapshots(client):
    r = client.get("/date/2027-06-01")
    assert r.status_code == 200
    assert "No snapshots for this date yet" in r.text


def test_bad_date_is_404(client):
    assert client.get("/date/not-a-date").status_code == 404
    assert client.get("/api/dates/2026-13-40").status_code == 404


def test_suites_pages(client):
    r = client.get("/suites")
    assert r.status_code == 200
    assert "Superior Family Suite" in r.text and "Cottage" in r.text and "First seen" in r.text

    r = client.get("/suites/sfssb")
    assert r.status_code == 200
    assert "Superior Family Suite" in r.text
    assert 'id="upcoming"' in r.text and 'id="curve"' in r.text
    assert '<option value="2026-11-01" selected' in r.text

    r = client.get("/suites/SFSSB?date=2026-11-05")
    assert '<option value="2026-11-05" selected' in r.text

    assert client.get("/suites/NOPE").status_code == 404


def test_runs_page(client):
    r = client.get("/runs")
    assert r.status_code == 200
    assert "schedule:weekly" in r.text and "completed" in r.text
    assert "BLOCKED (403)" in r.text
    assert "Disabled" in r.text  # scheduler off in tests


def test_trends_page(client):
    r = client.get("/trends")
    assert r.status_code == 200
    assert 'id="trend"' in r.text and "Trends" in r.text


# --------------------------------------------------------------------------- API


def test_api_overview(client):
    body = client.get("/api/overview").json()
    assert body["has_data"] is True
    assert body["start"] == "2026-11-01"
    assert len(body["dates"]) == 10
    first = body["dates"][0]
    assert {"arrival_date", "demand", "total_rooms_available", "suite_types_sold_out", "min_rate"} <= set(first)
    assert all(0 <= d["demand"] <= 100 for d in body["dates"])
    # Fewer rooms left -> higher demand.
    by_date = {d["arrival_date"]: d for d in body["dates"]}
    assert by_date["2026-11-10"]["demand"] > by_date["2026-11-01"]["demand"]


def test_api_trends(client):
    body = client.get("/api/trends").json()
    assert body["years"] == [2025, 2026]
    assert len(body["weeks"]) == 53
    jul_week = date(2025, 7, 14).isocalendar().week
    assert body["z"][0][jul_week - 1] is not None


def test_api_date(client):
    body = client.get("/api/dates/2026-11-10").json()
    assert body["snapshot"]["run_id"] == "2026-11-01"
    assert "raw_json" not in body["snapshot"]
    assert len(body["suites"]) == 5 and len(body["curve"]) == 2
    assert [c["total_rooms_available"] for c in body["curve"]] == [10, 7]  # 12+5+2 minus SFSSB drawdown
    sfssb = body["suites"][0]
    assert sfssb["suite_code"] == "SFSSB" and sfssb["sold_out"] is True  # 0 rooms left on 11-10
    assert set(sfssb["rates"]) == {"Saver", "Flexible"}
    assert client.get("/api/dates/2027-06-01").status_code == 404


def test_api_suites(client):
    body = client.get("/api/suites").json()
    codes = {s["suite_code"] for s in body["suites"]}
    assert codes == {"SFSSB", "SDLXB", "TWLFD", "PGRZL", "CCTGE"}
    one = client.get("/api/suites/SFSSB").json()
    assert one["suite"]["suite_brand"] == "Superior Family Suite"
    assert len(one["upcoming"]) == 10 and len(one["curve"]) == 2
    assert client.get("/api/suites/NOPE").status_code == 404


def test_api_runs_and_healthz(client):
    runs = client.get("/api/runs").json()
    assert [r["status"] for r in runs["runs"]] == ["aborted", "completed", "completed"]
    assert runs["runs"][0]["blocked"] == 1
    assert runs["scheduler"]["enabled"] is False

    health = client.get("/healthz").json()
    assert health["status"] == "ok" and health["has_data"] is True
    assert health["last_completed_run"]["run_id"] == "2026-11-01"


# --------------------------------------------------------------------------- empty states

PAGES = ["/", "/date/2026-11-01", "/suites", "/suites/SFSSB", "/runs", "/trends"]


@pytest.mark.parametrize("path", PAGES)
def test_pages_with_missing_db(tmp_path, path):
    with make_client(tmp_path / "missing.db") as c:
        r = c.get(path)
    assert r.status_code == 200
    assert "No data yet" in r.text
    assert not (tmp_path / "missing.db").exists()  # the web app never creates the db


@pytest.mark.parametrize("path", PAGES)
def test_pages_with_empty_db(tmp_path, path):
    db.connect(tmp_path / "empty.db").close()
    with make_client(tmp_path / "empty.db") as c:
        r = c.get(path)
    assert r.status_code == 200
    assert "No data yet" in r.text


def test_api_with_empty_db(tmp_path):
    with make_client(tmp_path / "missing.db") as c:
        assert c.get("/api/overview").json() == {"has_data": False, "dates": []}
        assert c.get("/api/suites").json() == {"has_data": False, "suites": []}
        assert c.get("/api/trends").json()["has_data"] is False
        assert c.get("/api/runs").json()["runs"] == []
        health = c.get("/healthz").json()
        assert health["status"] == "ok" and health["db_exists"] is False


def test_readonly_connection_cannot_write(seeded_db):
    import sqlite3

    conn = db.connect_readonly(seeded_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM snapshots")
    conn.close()


def test_upcoming_window_excludes_past(client):
    dates = [d["arrival_date"] for d in client.get("/api/overview").json()["dates"]]
    assert min(dates) >= TODAY.isoformat()
    assert max(dates) <= (TODAY + timedelta(days=364)).isoformat()
