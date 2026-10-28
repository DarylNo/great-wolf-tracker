"""Dashboard pages and JSON API on a demo DB, a single-snapshot DB, and an empty/missing DB."""

import json
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
    # Today's month through the month of the 365th night: 13 wall-calendar months, the last partial.
    assert "October 2026" in html and "October 2027" in html
    assert html.count('class="month"') == 13
    assert 'id="m-price" value="price" checked' in html
    assert "Far-out dates haven&#39;t been booked yet" in html or "Far-out dates haven't been booked yet" in html
    assert "Cheapest upcoming nights" in html and "Filling fastest" in html
    assert "ties go to the night with more rooms left" in html
    assert "Nights that sold rooms in the last 7 days come first, most sold first" in html
    assert 'id="scatter"' in html and 'id="scatter-data"' in html
    assert "/static/plotly.min.js" in html and "cdn." not in html
    assert not re.search(r"demand index", html, re.I)


def test_calendar_has_a_cell_for_every_one_of_the_365_nights(client):
    html = client.get("/").text
    calendar = html.split('<div class="months">')[1].split("</section>")[0]
    linked = re.findall(r'<a class="day[^"]*"\s+href="/date/(\d{4}-\d{2}-\d{2})"', calendar)
    expected = [(TODAY + timedelta(days=i)).isoformat() for i in range(365)]
    assert linked == expected
    # The 13th month (Oct 2027) is partial: the 27th is the last night, the rest are out of range.
    assert '<span class="day out"><span class="n">28</span>' in calendar


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
    selling = [n for n in filling if (n["pickup_7d"] or 0) > 0]
    assert filling[:len(selling)] == selling  # nights that sold rooms come first...
    assert [n["pickup_7d"] for n in selling] == sorted((n["pickup_7d"] for n in selling), reverse=True)
    rest = filling[len(selling):]
    assert [n["pace_pct"] for n in rest] == sorted((n["pace_pct"] for n in rest), reverse=True)  # ...then pace
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


# --------------------------------------------------------------------------- Filling fastest rule


def _n(day: int, pickup, pace, sold_out=False, has_data=True) -> dict:
    return {"date": f"2026-11-{day:02d}", "has_data": has_data, "sold_out": sold_out, "pickup_7d": pickup,
            "pace_pct": pace}


def test_filling_fastest_puts_nights_that_sold_rooms_first_then_pace():
    nights = [
        _n(1, 3, 10.0), _n(2, 0, 95.0), _n(3, None, 80.0), _n(4, 7, None), _n(5, -2, 99.0),
        _n(6, 3, 60.0), _n(7, None, None), _n(8, 9, 50.0, sold_out=True), _n(9, None, 20.0, has_data=False),
    ]
    order = [n["date"][-2:] for n in data.filling_fastest(nights)]
    # pickup > 0 by pickup (ties: faster pace first), then everything else by pace;
    # sold-out, no-data and no-signal nights are left out.
    assert order == ["04", "06", "01", "05", "02", "03"]


# --------------------------------------------------------------------------- banners: pause (B), events file (G)


def _block(path: Path) -> None:
    conn = db.connect(path)
    run = db.start_run(conn, run_id="2026-10-25", trigger="schedule:weekly", started_at="2026-10-25T07:17:00+00:00",
                       start_date="2026-10-25", days=365, skipped=0)
    db.update_run(conn, run, status="aborted", blocked=1, finished_at="2026-10-25T07:17:05+00:00",
                  message="2026-10-25: HTTP 403 - looks blocked; stopping (do not evade)")
    conn.close()


def test_pause_banner_on_plan_and_runs_until_unblocked(single_db):
    _block(single_db)
    with make_client(single_db) as c:
        for path in ("/", "/runs"):
            html = c.get(path).text
            assert "Collection paused: blocked (403) on 2026-10-25, run <code>gwt unblock</code> after reviewing." in html
        assert "Paused: scheduled sweeps skip until" in c.get("/runs").text
        health = c.get("/healthz").json()
        assert health["collection_paused"]["run_id"] == "2026-10-25"
        assert c.get("/api/runs").json()["collection_paused"]["date"] == "2026-10-25"
    conn = db.connect(single_db)
    db.acknowledge_block(conn)
    conn.close()
    with make_client(single_db) as c:
        assert "Collection paused" not in c.get("/").text
        assert c.get("/healthz").json()["collection_paused"] is None


@pytest.mark.parametrize("path", ["/", "/seasonality", "/date/2026-11-21", "/suites", "/runs", "/healthz"])
def test_bad_events_file_falls_back_with_a_banner(demo_db, tmp_path, monkeypatch, path):
    bad = tmp_path / "calendar_events.yaml"
    bad.write_text("events:\n  - {region: ON, name: X, start: 2027-01-01, end: 2027-01-01, source: s, official: true}\n")
    monkeypatch.setenv("GWT_EVENTS_FILE", str(bad))
    with make_client(demo_db) as c:
        r = c.get(path)
    assert r.status_code == 200
    if path == "/healthz":
        assert r.json()["events_problem"]["file"] == str(bad)
        return
    assert f"School calendar file <code>{bad}</code>" in r.text and "quote it" in r.text
    if path == "/":  # the bundled calendar is still used
        assert "NY: Thanksgiving recess" in r.text


# --------------------------------------------------------------------------- Runs page


def test_runs_page_paginates_and_shows_toronto_time(tmp_path):
    path = tmp_path / "runs.db"
    conn = db.connect(path)
    for i in range(120):
        day = date(2026, 6, 1) + timedelta(days=i)
        run = db.start_run(conn, run_id=day.isoformat(), trigger="schedule:daily",
                           started_at=f"{day.isoformat()}T09:43:00+00:00", start_date=day.isoformat(), days=38, skipped=0)
        db.update_run(conn, run, status="completed", fetched=38, finished_at=f"{day.isoformat()}T09:46:00+00:00")
    conn.close()
    with make_client(path) as c:
        first = c.get("/runs").text
        assert first.count("<tr>") - 1 == 50  # header row + 50 runs
        assert "Page 1 of 3 · 120 runs" in first and 'href="?page=2"' in first
        newest = (date(2026, 6, 1) + timedelta(days=119))
        # 09:43 UTC is 05:43 in Toronto (EDT); the UTC value stays in the tooltip.
        assert f"{data.fmt.date_long(newest)} 5:43 AM" in first
        assert f'title="{newest.isoformat()}T09:43:00+00:00 UTC"' in first
        last = c.get("/runs?page=3").text
        assert last.count("<tr>") - 1 == 20 and 'href="?page=2"' in last and "?page=4" not in last
        assert c.get("/runs?page=99").status_code == 200  # clamps to the last page
        api = c.get("/api/runs?page=2").json()
        assert api["page"] == 2 and api["total"] == 120 and len(api["runs"]) == 50
        assert api["runs"][0]["started_local"].endswith("5:43 AM")


def test_runs_badges_for_new_statuses(single_db):
    conn = db.connect(single_db)
    for status in ("rate_limited", "interrupted"):
        run = db.start_run(conn, run_id="x", trigger="cli", started_at="2026-10-28T10:00:00+00:00",
                           start_date="2026-10-28", days=5, skipped=0)
        db.update_run(conn, run, status=status, finished_at="2026-10-28T10:01:00+00:00")
    conn.close()
    with make_client(single_db) as c:
        html = c.get("/runs").text
    assert '<span class="badge bad">rate-limited</span>' in html
    assert '<span class="badge muted">interrupted</span>' in html


# --------------------------------------------------------------------------- capacity wording


def test_no_capacity_estimate_reads_as_text_not_none(tmp_path, sample_payload):
    for s in sample_payload["suites"]:
        s["availableRoomCount"] = 0  # every scrape sold out: no capacity estimate possible
    path = tmp_path / "soldout.db"
    conn = db.connect(path)
    db.save_snapshot(conn, "2026-10-28", "2026-10-28T09:43:00+00:00",
                     parse_availability(sample_payload, TODAY + timedelta(days=3)))
    conn.close()
    with make_client(path) as c:
        for page in ("/", "/seasonality"):
            html = c.get(page).text
            assert "None" not in html
        assert "capacity estimate not available yet" in c.get("/").text


# --------------------------------------------------------------------------- room / suite ?date=


def test_room_page_date_param_and_picker_agree(client):
    # A past night with snapshots (not in the upcoming list) is added to the picker and plotted.
    past = (TODAY - timedelta(days=3)).isoformat()
    html = client.get(f"/rooms/family-suite?date={past}").text
    assert f'<option value="{past}" selected' in html
    body = client.get(f"/api/rooms/family-suite?date={past}").json()
    assert body["curve_date"] == past and past in body["curve_dates"] and body["curve"]["rooms"]
    # A night with no snapshots of this room: fall back to the first upcoming night, say so.
    html = client.get("/rooms/family-suite?date=2031-01-01").text
    assert "No snapshots of this room for Wed, Jan 1, 2031; showing" in html
    selected = re.findall(r'<option value="([^"]+)" selected', html)
    assert selected == [TODAY.isoformat()]
    body = client.get("/api/suites/SFSSB?date=2031-01-01").json()
    assert body["curve_date"] == TODAY.isoformat() and body["curve_date"] in body["curve_dates"]
    assert "No snapshots of this suite code" in body["curve_note"]
    assert client.get(f"/api/suites/SFSSB?date={TODAY.isoformat()}").json()["curve_note"] is None


# --------------------------------------------------------------------------- renamed suite codes


def test_a_renamed_suite_stays_one_room_everywhere(tmp_path, sample_payload):
    """Great Wolf renames TWLFD between two scrapes: the old snapshot rows still carry the old
    name, but every page groups the code under its current catalogue name."""
    path = tmp_path / "renamed.db"
    conn = db.connect(path)
    early = TODAY + timedelta(days=3)
    db.save_snapshot(conn, "2026-10-27", "2026-10-27T09:43:00+00:00", parse_availability(sample_payload, early))
    renamed = json.loads(json.dumps(sample_payload))
    for s in renamed["suites"]:
        if s["suiteCode"] == "TWLFD":
            s["suiteBrand"] = "Wolf Den Bunk Suite"
    db.save_snapshot(conn, "2026-10-28", "2026-10-28T09:43:00+00:00",
                     parse_availability(renamed, TODAY + timedelta(days=4)))
    conn.close()
    with make_client(path) as c:
        # The early night's latest snapshot says "Wolf Den Suite"; it links to the current room.
        detail = c.get(f"/api/dates/{early.isoformat()}").json()
        names = [r["name"] for r in detail["rooms"]]
        assert "Wolf Den Bunk Suite" in names and "Wolf Den Suite" not in names
        slug = next(r["slug"] for r in detail["rooms"] if r["name"] == "Wolf Den Bunk Suite")
        assert c.get(f"/rooms/{slug}").status_code == 200
        html = c.get(f"/date/{early.isoformat()}").text
        assert f'href="/rooms/{slug}?date={early.isoformat()}"' in html
        # The matrix row has data for both nights, not "no data" for the old-name night.
        matrix = c.get("/api/rooms/matrix?window=30").json()
        row = next(r for r in matrix["rooms"] if r["name"] == "Wolf Den Bunk Suite")
        assert row["rooms_left"][3] == 5 and row["rooms_left"][4] == 5
        assert not any(r["name"] == "Wolf Den Suite" for r in matrix["rooms"])
        night = next(n for n in c.get("/api/plan").json()["nights"] if n["date"] == early.isoformat())
        assert night["room_types"] == 5


# --------------------------------------------------------------------------- seasonality week labels


def test_week_labels_cover_the_whole_year():
    assert data.week_label(2026, 1) == "Jan 1–Jan 7"
    assert data.week_label(2026, 52) == "Dec 24–Dec 31"  # 8 nights: the 365th folds into week 52
    assert data.week_label(2028, 52) == "Dec 23–Dec 31"  # leap year: 9 nights
    assert data.week_label(2028, 9) == "Feb 26–Mar 3"


def test_api_seasonality_has_week_labels(client):
    body = client.get("/api/seasonality").json()
    for y in body["years"]:
        labels = body["week_labels"][str(y)]
        assert len(labels) == 52 and labels[-1].endswith("Dec 31")


# --------------------------------------------------------------------------- Stats rebuild throttle (H)


def test_stats_rebuild_is_throttled_while_a_sweep_runs(single_db, sample_payload):
    clock = {"t": 1000.0}
    key = f"throttle:{single_db}"
    conn = db.connect(single_db)
    first = data.get_stats(conn, key, clock=lambda: clock["t"])
    run = db.start_run(conn, run_id="2026-10-29", trigger="schedule:daily", started_at="2026-10-29T09:43:00+00:00",
                       start_date="2026-10-29", days=38, skipped=0)
    db.save_snapshot(conn, "2026-10-29", "2026-10-29T09:43:05+00:00",
                     parse_availability(sample_payload, TODAY + timedelta(days=5)))
    clock["t"] += 30
    assert data.get_stats(conn, key, clock=lambda: clock["t"]) is first  # sweep running, < 60 s old
    clock["t"] += 31
    second = data.get_stats(conn, key, clock=lambda: clock["t"])
    assert second is not first and len(second.snaps) == 2  # > 60 s: rebuilt
    db.save_snapshot(conn, "2026-10-29", "2026-10-29T09:43:09+00:00",
                     parse_availability(sample_payload, TODAY + timedelta(days=6)))
    db.update_run(conn, run, status="completed", finished_at="2026-10-29T09:44:00+00:00")
    third = data.get_stats(conn, key, clock=lambda: clock["t"])
    assert third is not second and len(third.snaps) == 3  # sweep done: rebuilt at once
    assert data.get_stats(conn, key, clock=lambda: clock["t"]) is third  # unchanged: cached
    conn.close()


def test_date_page_bands_match_a_brute_force_recompute(demo_db):
    conn = db.connect_readonly(demo_db)
    try:
        stats = data.get_stats(conn, str(demo_db.resolve()))
        night = date(2026, 11, 21)
        detail = data.date_detail(conn, stats, night, TODAY)
        others = [s for s in stats.snaps if s.arrival != night]
        assert detail["curve"]["rooms_band"] == data.M.band_by_bucket((s.lead, s.rooms) for s in others)
        assert detail["curve"]["rate_band"] == data.M.band_by_bucket((s.lead, s.min_rate) for s in others)
        room = data.room_detail(conn, stats, "family-suite", TODAY, night)
        history = data.cached_code_history(conn, stats, room["codes"]).obs
        rest = [o for o in history if o.arrival != night]
        assert room["curve"]["rooms_band"] == data.M.band_by_bucket((o.lead, o.rooms) for o in rest)
        assert room["curve"]["rate_band"] == data.M.band_by_bucket((o.lead, o.min_rate) for o in rest)
    finally:
        conn.close()
