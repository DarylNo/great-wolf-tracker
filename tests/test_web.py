"""Dashboard pages and JSON API on a demo DB, a single-snapshot DB, and an empty/missing DB."""

import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gwt import db
from gwt.collector import parse_availability
from gwt.config import ScheduleConfig, Settings
from gwt.demo import build_demo_db
from gwt.metrics import LODGE_TZ
from gwt.web import data
from gwt.web.app import create_app

TODAY = date(2026, 10, 28)
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=LODGE_TZ)
NO_SCHEDULE = ScheduleConfig(enabled=False, timezone="America/Toronto", jobs=())


def make_client(db_path: Path) -> TestClient:
    settings = Settings(db_path=db_path, host="127.0.0.1", port=8080, schedule=NO_SCHEDULE)
    return TestClient(create_app(settings, today=lambda: TODAY))


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("demo") / "demo.db"
    build_demo_db(path, weeks=5, now=NOW)
    return path


@pytest.fixture(scope="module")
def client(demo_db):
    with make_client(demo_db) as c:
        yield c


@pytest.fixture
def single_db(tmp_path, sample_payload) -> Path:
    """Exactly one snapshot: one arrival date, one scrape."""
    path = tmp_path / "one.db"
    conn = db.connect(path)
    db.save_snapshot(conn, "2026-10-28", "2026-10-28T09:43:00+00:00",
                     parse_availability(sample_payload, TODAY + timedelta(days=3)))
    conn.close()
    return path


def keys_deep(obj) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | set().union(*(keys_deep(v) for v in obj.values())) if obj else set()
    if isinstance(obj, list):
        return set().union(*(keys_deep(v) for v in obj)) if obj else set()
    return set()


# --------------------------------------------------------------------------- Plan (/)


def test_plan_page(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "When should we go?" in html
    assert "Synthetic demo data" in html  # demo banner
    assert "October 2026" in html and "September 2027" in html  # 12 wall-calendar months
    assert html.count('class="month"') == 12
    assert 'id="m-price" value="price" checked' in html
    assert "Far-out dates haven&#39;t been booked yet" in html or "Far-out dates haven't been booked yet" in html
    assert "Cheapest upcoming nights" in html and "Filling fastest" in html
    assert "ties go to the night with more rooms left" in html
    assert "most rooms sold in the last 7 days first" in html
    assert 'id="scatter"' in html and 'id="scatter-data"' in html
    assert "/static/plotly.min.js" in html and "cdn." not in html
    assert not re.search(r"demand index", html, re.I)


def test_plan_metric_toggle_param(client):
    html = client.get("/?metric=rooms").text
    assert 'id="m-rooms" value="rooms" checked' in html
    assert 'id="m-price" value="price" checked' not in html
    assert 'id="m-price" value="price" checked' in client.get("/?metric=bogus").text


def test_plan_calendar_cells(client):
    html = client.get("/").text
    plan = client.get("/api/plan").json()
    sold = [n["date"] for n in plan["nights"] if n["has_data"] and n["sold_out"]]
    assert sold, "the demo should have sold-out nights"
    cell = re.search(rf'<a class="day ([^"]*)"\s+href="/date/{sold[0]}"', html)
    assert cell and " so" in cell.group(1)
    today_cell = re.search(rf'<a class="day ([^"]*)"\s+href="/date/{TODAY.isoformat()}"', html)
    assert today_cell and "today" in today_cell.group(1)
    # NY Thanksgiving recess starts Wed Nov 25, so the night of Tue Nov 24 is flagged.
    ev = re.search(r'<a class="day ([^"]*)"\s+href="/date/2026-11-24" data-tip="([^"]*)"', html)
    assert ev and " ev" in ev.group(1)
    assert "No school next day" in ev.group(2) and "NY: Thanksgiving recess" in ev.group(2)
    # Tooltip fields.
    for field in ("rooms left", "lowest rate", "room types sold out", "before arrival at last scrape", "pace vs other dates at the same days-out"):
        assert field in ev.group(2)


def test_api_plan(client):
    body = client.get("/api/plan").json()
    assert body["has_data"] is True
    assert body["start"] == TODAY.isoformat()
    assert len(body["nights"]) == 365
    assert not any("demand" in k for k in keys_deep(body))
    first = body["nights"][0]
    assert {"date", "rooms_left", "pct_sold", "min_rate", "ref_rate", "lead_days", "pace_pct", "pickup_7d",
            "room_types_sold_out", "room_types", "school_closure"} <= set(first)
    assert first["room_types"] == 17
    assert body["status"]["capacity_est"] > 300
    assert body["definitions"]["pace"] == "vs other dates at the same days-out"
    cheapest = body["cheapest_nights"]["nights"]
    assert len(cheapest) == 12
    order = [(n["min_rate"], -n["rooms_left"]) for n in cheapest]
    assert order == sorted(order)
    assert all(not n["sold_out"] for n in cheapest)
    filling = body["filling_fastest"]["nights"]
    assert filling and all(n["date"] < (TODAY + timedelta(days=60)).isoformat() for n in filling)
    with_pickup = [n["pickup_7d"] for n in filling if n["pickup_7d"] is not None]
    assert with_pickup == sorted(with_pickup, reverse=True)
    paced = [n for n in body["nights"] if n["pace_pct"] is not None]
    assert paced and all(0 <= n["pace_pct"] <= 100 for n in paced)
    missing = [n for n in body["nights"] if not n["has_data"]]
    assert all(set(n) == set(first) and n["rooms_left"] is None for n in missing)  # same shape, nulls


def test_plan_colour_legends_use_real_values(demo_db, client):
    conn = db.connect_readonly(demo_db)
    try:
        stats = data.get_stats(conn, str(demo_db.resolve()))
        plan = data.plan(conn, stats, TODAY)
    finally:
        conn.close()
    price = plan["scales"]["price"]
    rooms = plan["scales"]["rooms"]
    assert [b["step"] for b in price] == sorted(b["step"] for b in price)  # light -> dark
    assert all(b["label"].startswith("$") for b in price)
    lows = [float(b["label"].split("–")[0].replace("$", "").replace(",", "")) for b in price]
    assert lows == sorted(lows)  # pricier = darker
    room_lows = [int(b["label"].split("–")[0]) for b in rooms]
    assert room_lows == sorted(room_lows, reverse=True)  # fewer rooms = darker
    for n in plan["nights"]:
        if n["has_data"] and not n["sold_out"]:
            assert 0 <= n["steps"]["price"] <= 4 and 0 <= n["steps"]["rooms"] <= 4


# --------------------------------------------------------------------------- Seasonality


def test_seasonality_page_and_redirect(client):
    r = client.get("/seasonality")
    assert r.status_code == 200
    html = r.text
    assert "How full each week ends up" in html
    assert "Forecast from current prices" in html
    assert "Great Wolf raises prices when it expects to be busy" in html
    assert 'id="ff-weekend"' in html and 'id="ff-weekday"' in html and 'id="forecast"' in html
    redirect = client.get("/trends", follow_redirects=False)
    assert redirect.status_code == 301 and redirect.headers["location"] == "/seasonality"


def test_api_seasonality(client):
    body = client.get("/api/seasonality").json()
    assert body["has_data"] and body["final_fill_nights"] > 20
    assert set(body["panels"]) == {"weekend", "weekday"}
    for panel in body["panels"].values():
        for series in panel["series"]:
            assert len(series["values"]) == 52
            assert all(v is None or 0 <= v <= 100 for v in series["values"])
    assert len(body["forecast"]) == 52
    assert any(w["weekday"] for w in body["forecast"]) and any(w["weekend"] for w in body["forecast"])
    titles = {s["title"] for s in body["forecast_spans"]}
    assert "ON: March Break" in titles and "ON: Winter break" in titles
    assert not any("demand" in k for k in keys_deep(body))


# --------------------------------------------------------------------------- Date page


def test_date_page(client):
    r = client.get("/date/2026-11-21")
    assert r.status_code == 200
    html = r.text
    assert "Sat, Nov 21, 2026" in html
    for tile in ("Rooms left", "Lowest rate", "Reference rate", "Room types fully sold", "Days until arrival", "Pace"):
        assert tile in html
    assert "of 17" in html
    assert html.count("<details") >= 17
    assert "Timber Wolf Cottage" in html and "SFSSA" in html and "CTWAA" in html
    assert 'id="curve-rooms"' in html and 'id="curve-rate"' in html
    assert "Show as a table" in html


def test_date_page_closure_tags(client):
    html = client.get("/date/2027-03-14").text
    assert "No school next day · ON: March Break" in html
    assert "No school next day" not in client.get("/date/2027-03-12").text


def test_api_date(client):
    body = client.get("/api/dates/2026-11-21").json()
    assert body["has_data"] and body["room_types"] == 17
    assert len(body["rooms"]) == 17
    assert sum(len(r["variants"]) for r in body["rooms"]) == 27
    assert body["families"] == ["Saver", "Flexible"]
    family = next(r for r in body["rooms"] if r["name"] == "Family Suite")
    assert family["has_accessible"] and family["occupancy"] == "6"
    assert family["rooms_left"] == sum(v["rooms_left"] for v in family["variants"])
    curve = body["curve"]
    assert len(curve["rooms_band"]) == 9 and len(curve["rate_band"]) == 9
    leads = [p["lead"] for p in curve["rooms"]]
    assert leads == sorted(leads, reverse=True) and all(0 <= x <= 365 for x in leads)
    assert body["days_until"] == 24
    assert not any("demand" in k for k in keys_deep(body))


def test_date_without_snapshots_and_bad_dates(client):
    r = client.get("/date/2030-01-01")
    assert r.status_code == 200 and "No snapshots for this night yet" in r.text
    assert client.get("/date/not-a-date").status_code == 404
    assert client.get("/api/dates/2026-13-40").status_code == 404
    assert client.get("/api/dates/2030-01-01").status_code == 404


# --------------------------------------------------------------------------- Room types


def test_rooms_page_and_matrix(client):
    html = client.get("/suites").text
    assert "Which rooms sell out first" in html
    assert "27 suite codes, grouped into 17 rooms" in html
    rows = re.findall(r'<th class="room" scope="row">', html)
    assert len(rows) == 17
    first_row = html.split('<th class="room" scope="row">')[1].split("</tr>")[0]
    assert first_row.count('<td class="c ') == 90  # default window
    assert 'class="on"' in html and ">90 days<" in html
    assert "Sold out (×)" in html and 'class="we' in html
    html30 = client.get("/suites?window=30").text
    assert html30.split('<th class="room" scope="row">')[1].split("</tr>")[0].count('<td class="c ') == 30
    bogus = client.get("/suites?window=7").text
    assert bogus.split('<th class="room" scope="row">')[1].split("</tr>")[0].count('<td class="c ') == 90


def test_room_group_and_code_pages(client):
    r = client.get("/rooms/timber-wolf-cottage")
    assert r.status_code == 200
    assert all(code in r.text for code in ("CTW2B", "CTW3B", "CTW4B", "CTWAA"))
    assert 'id="up-rooms"' in r.text and 'id="curve-rooms"' in r.text
    r = client.get("/rooms/family-suite?date=2026-11-21")
    assert '<option value="2026-11-21" selected' in r.text
    r = client.get("/suites/sfssb")
    assert r.status_code == 200 and "Family Suite" in r.text and "/rooms/family-suite" in r.text
    assert client.get("/rooms/nope").status_code == 404
    assert client.get("/suites/NOPE").status_code == 404


def test_api_rooms(client):
    rooms = client.get("/api/rooms").json()["rooms"]
    assert len(rooms) == 17 and sum(len(r["codes"]) for r in rooms) == 27
    cats = [r["category"] for r in rooms]
    assert cats == sorted(cats, key=["standard", "themed", "premium", "cottage"].index)
    matrix = client.get("/api/rooms/matrix?window=30").json()
    assert len(matrix["dates"]) == 30 and len(matrix["rooms"]) == 17
    assert all(len(r["rooms_left"]) == 30 for r in matrix["rooms"])
    room = client.get("/api/rooms/timber-wolf-cottage").json()
    assert room["codes"] == ["CTW2B", "CTW3B", "CTW4B", "CTWAA"]
    assert room["upcoming"] and room["curve"]["rooms"]
    suites = client.get("/api/suites").json()["suites"]
    assert len(suites) == 27 and {"code", "name", "room_slug"} <= set(suites[0])
    assert client.get("/api/suites/SFSSB").json()["room_slug"] == "family-suite"
    assert client.get("/api/rooms/nope").status_code == 404


# --------------------------------------------------------------------------- Runs / health / static


def test_runs_page_and_health(client):
    r = client.get("/runs")
    assert r.status_code == 200 and "schedule:weekly" in r.text and "completed" in r.text
    health = client.get("/healthz").json()
    assert health["status"] == "ok" and health["has_data"] and health["synthetic_demo"] is True


def test_static_assets_are_local(client):
    r = client.get("/static/plotly.min.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert len(r.content) > 1_000_000
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


# --------------------------------------------------------------------------- single snapshot

PAGES = ["/", "/seasonality", f"/date/{(TODAY + timedelta(days=3)).isoformat()}", "/suites",
         "/rooms/superior-family-suite", "/suites/SFSSB", "/runs"]


@pytest.mark.parametrize("path", PAGES)
def test_pages_with_a_single_snapshot(single_db, path):
    with make_client(single_db) as c:
        r = c.get(path)
    assert r.status_code == 200, r.text[:500]


def test_single_snapshot_content(single_db):
    with make_client(single_db) as c:
        plan = c.get("/").text
        assert "capacity estimate improves after the first full sweep" in plan
        body = c.get("/api/plan").json()
        night = next(n for n in body["nights"] if n["has_data"])
        assert night["pace_pct"] is None and night["pickup_7d"] is None  # too little data
        assert body["status"]["capacity_provisional"] is True
        season = c.get("/seasonality").text
        assert "No final-fill data yet" in season  # its one scrape is 3 days out
        detail = c.get(f"/api/dates/{(TODAY + timedelta(days=3)).isoformat()}").json()
        assert len(detail["curve"]["rooms"]) == 1
        assert all(b is None for b in detail["curve"]["rooms_band"])  # no other dates to compare
        date_page = c.get(f"/date/{(TODAY + timedelta(days=3)).isoformat()}").text
        assert "n/a: 1 snapshots at this days-out (needs 20)" in date_page


# --------------------------------------------------------------------------- empty / missing db

EMPTY_PAGES = ["/", "/seasonality", "/date/2026-11-01", "/suites", "/rooms/family-suite", "/suites/SFSSB", "/runs"]


@pytest.mark.parametrize("path", EMPTY_PAGES)
def test_pages_with_missing_db(tmp_path, path):
    with make_client(tmp_path / "missing.db") as c:
        r = c.get(path)
    assert r.status_code == 200
    assert "No data yet" in r.text
    assert not (tmp_path / "missing.db").exists()  # the web app never creates the db


@pytest.mark.parametrize("path", EMPTY_PAGES)
def test_pages_with_empty_db(tmp_path, path):
    db.connect(tmp_path / "empty.db").close()
    with make_client(tmp_path / "empty.db") as c:
        r = c.get(path)
    assert r.status_code == 200
    assert "No data yet" in r.text


def test_api_with_empty_db(tmp_path):
    with make_client(tmp_path / "missing.db") as c:
        assert c.get("/api/plan").json() == {"has_data": False, "nights": []}
        assert c.get("/api/rooms").json() == {"has_data": False, "rooms": []}
        assert c.get("/api/suites").json() == {"has_data": False, "suites": []}
        assert c.get("/api/seasonality").json()["has_data"] is False
        assert c.get("/api/rooms/matrix").json()["has_data"] is False
        assert c.get("/api/runs").json()["runs"] == []
        assert c.get("/api/dates/2026-11-01").status_code == 404
        health = c.get("/healthz").json()
        assert health["status"] == "ok" and health["db_exists"] is False and health["synthetic_demo"] is False


def test_readonly_connection_cannot_write(demo_db):
    import sqlite3

    conn = db.connect_readonly(demo_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM snapshots")
    conn.close()


def test_matrix_step_bins():
    assert data.matrix_step(100, 100) == 0
    assert data.matrix_step(81, 100) == 0
    assert data.matrix_step(80, 100) == 1
    assert data.matrix_step(41, 100) == 2
    assert data.matrix_step(20, 100) == 4
    assert data.matrix_step(1, 100) == 4
    assert data.matrix_step(3, None) == 2
