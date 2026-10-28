"""Suite detail fields, rate families, the `suites` lookup table and the `gwt suites` CLI."""

import copy
import sqlite3
from argparse import Namespace
from datetime import date

import pytest

from gwt import db
from gwt.cli import cmd_suites, main
from gwt.collector import parse_availability


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


# --------------------------------------------------------------------------- parsing


def test_suite_detail_fields(sample_payload):
    snap = parse_availability(sample_payload, "2026-11-18")
    s = {r.suite_code: r for r in snap.suites}["SFSSB"]
    assert s.suite_brand == "Superior Family Suite"
    assert (s.base_occupancy, s.max_occupancy, s.sort_order) == (4, 6, 1)
    # isAccessible (bool), numOfBeds / numOfBaths (strings) -> ints.
    assert (s.is_accessible, s.num_beds, s.num_baths) == (0, 2, 1)
    by_code = {r.suite_code: r for r in snap.suites}
    assert by_code["SDLXB"].is_accessible == 1
    assert (by_code["CCTGE"].num_beds, by_code["CCTGE"].num_baths) == (5, 3)


def test_rate_families_cheapest_per_family(sample_payload):
    snap = parse_availability(sample_payload, "2026-11-18")
    by_code = {r.suite_code: r for r in snap.suites}

    sfssb = {f.title: f.min_total_nightly_rate for f in by_code["SFSSB"].rate_families}
    assert sfssb == {"Saver": 379.99, "Flexible": 419.99}

    twlfd = by_code["TWLFD"].rate_families
    assert [f.title for f in twlfd] == ["Saver", "Flexible"]
    assert twlfd[1].min_total_nightly_rate == 499.99  # cheaper of the two Flexible rates
    assert twlfd[1].policy_type == "FLEX"
    assert twlfd[1].display_rank == 2

    assert by_code["PGRZL"].rate_families == []


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"isAccessible": True, "numOfBeds": "3", "numOfBaths": "2"}, (1, 3, 2)),
        ({"isAccessible": False, "numOfBeds": " 4 ", "numOfBaths": "1"}, (0, 4, 1)),
        ({"isAccessible": False, "numOfBeds": 2, "numOfBaths": 1}, (0, 2, 1)),  # tolerate real ints
        ({"isAccessible": None, "numOfBeds": "", "numOfBaths": "two"}, (None, None, None)),  # unparseable -> NULL
        ({"isAccessible": "true", "numOfBeds": "1.5", "numOfBaths": None}, (None, None, None)),  # non-bool flag -> NULL
    ],
)
def test_accessible_beds_baths_mapping(sample_payload, fields, expected):
    sample_payload["suites"][0].update(fields)
    s = parse_availability(sample_payload, "2026-11-18").suites[0]
    assert (s.is_accessible, s.num_beds, s.num_baths) == expected


def test_missing_accessible_beds_baths_keys_give_null(sample_payload):
    for key in ("isAccessible", "numOfBeds", "numOfBaths"):
        sample_payload["suites"][0].pop(key)
    s = parse_availability(sample_payload, "2026-11-18").suites[0]
    assert (s.is_accessible, s.num_beds, s.num_baths) == (None, None, None)


def test_old_guessed_keys_are_ignored(sample_payload):
    suite = sample_payload["suites"][0]
    for key in ("isAccessible", "numOfBeds", "numOfBaths"):
        suite.pop(key)
    suite.update({"isAda": True, "numberOfBeds": 2, "bedCount": "3"})
    s = parse_availability(sample_payload, "2026-11-18").suites[0]
    assert (s.is_accessible, s.num_beds) == (None, None)


# --------------------------------------------------------------------------- storage


def test_suite_snapshot_columns_and_rate_family_rows(conn, sample_payload):
    db.save_snapshot(conn, "r1", "2026-10-28T03:17:00+00:00", parse_availability(sample_payload, "2026-11-18"))

    row = conn.execute(
        "SELECT suite_brand, base_occupancy, max_occupancy, is_accessible, num_beds, num_baths "
        "FROM suite_snapshots WHERE suite_code = 'SFSSB'"
    ).fetchone()
    assert row == ("Superior Family Suite", 4, 6, 0, 2, 1)
    assert conn.execute("SELECT is_accessible FROM suites WHERE suite_code = 'SDLXB'").fetchone() == (1,)

    fams = conn.execute(
        """SELECT ss.suite_code, rf.family_title, rf.min_total_nightly_rate
           FROM suite_rate_families rf JOIN suite_snapshots ss ON ss.id = rf.suite_snapshot_id
           ORDER BY ss.suite_code, rf.display_rank"""
    ).fetchall()
    assert ("TWLFD", "Flexible", 499.99) in fams
    assert ("SFSSB", "Saver", 379.99) in fams
    # SFSSB 2 + SDLXB 1 + TWLFD 2 + PGRZL 0 + CCTGE 1
    assert len(fams) == 6


def test_suites_lookup_upsert_first_and_last_seen(conn, sample_payload):
    db.save_snapshot(conn, "r1", "2026-10-28T03:17:00+00:00", parse_availability(sample_payload, "2026-11-18"))

    later = copy.deepcopy(sample_payload)
    later["suites"] = [s for s in later["suites"] if s["suiteCode"] != "CCTGE"]  # cottage disappears
    later["suites"].append(
        {"suiteCode": "NEWST", "suiteBrand": "New Suite", "suiteCategory": "themed",
         "availableRoomCount": 3, "maxOccupancy": 5, "rateFamilies": []}
    )
    later["suites"][0]["suiteBrand"] = "Superior Family Suite (renamed)"
    db.save_snapshot(conn, "r2", "2026-11-04T03:17:00+00:00", parse_availability(later, "2026-11-18"))

    rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT suite_code, suite_brand, max_occupancy, first_seen, last_seen FROM suites"
    )}
    assert len(rows) == 6
    assert rows["SFSSB"] == ("Superior Family Suite (renamed)", 6, "2026-10-28T03:17:00+00:00", "2026-11-04T03:17:00+00:00")
    assert rows["CCTGE"][2:] == ("2026-10-28T03:17:00+00:00", "2026-10-28T03:17:00+00:00")  # not seen in r2
    assert rows["NEWST"][2:] == ("2026-11-04T03:17:00+00:00", "2026-11-04T03:17:00+00:00")


def test_migrates_old_suite_snapshots_table(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        """CREATE TABLE suite_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
           scraped_at TEXT NOT NULL, arrival_date TEXT NOT NULL, suite_code TEXT NOT NULL,
           suite_category TEXT, available_room_count INTEGER, min_total_nightly_rate REAL)"""
    )
    old.commit()
    old.close()

    conn = db.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(suite_snapshots)")}
    assert {"suite_brand", "base_occupancy", "max_occupancy", "is_accessible", "num_beds", "num_baths"} <= cols
    conn.close()


def test_suites_table_gets_max_rooms_and_min_rate_backfilled(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """CREATE TABLE suite_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
               scraped_at TEXT NOT NULL, arrival_date TEXT NOT NULL, suite_code TEXT NOT NULL,
               suite_category TEXT, available_room_count INTEGER, min_total_nightly_rate REAL);
           CREATE TABLE suites (suite_code TEXT PRIMARY KEY, suite_brand TEXT, suite_category TEXT,
               max_occupancy INTEGER, is_accessible INTEGER, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
           INSERT INTO suites VALUES ('SFSSB', 'Family Suite', 'standard', 6, 0, 't1', 't2');
           INSERT INTO suite_snapshots (run_id, scraped_at, arrival_date, suite_code, available_room_count, min_total_nightly_rate)
               VALUES ('r1', 't1', '2026-11-18', 'SFSSB', 40, 399.99), ('r2', 't2', '2026-11-18', 'SFSSB', 12, 449.99);"""
    )
    old.commit()
    old.close()
    conn = db.connect(path)
    assert conn.execute("SELECT max_rooms_seen, min_rate_seen FROM suites").fetchone() == (40, 399.99)
    conn.close()


def test_suites_table_tracks_max_rooms_and_min_rate(conn, sample_payload):
    db.save_snapshot(conn, "r1", "2026-10-28T03:17:00+00:00", parse_availability(sample_payload, "2026-11-18"))
    fewer = copy.deepcopy(sample_payload)
    fewer["suites"][0]["availableRoomCount"] = 3
    fewer["suites"][0]["rateFamilies"][0]["rates"][0]["totalNightlyRate"] = 299.99
    db.save_snapshot(conn, "r2", "2026-11-04T03:17:00+00:00", parse_availability(fewer, "2026-11-18"))
    assert conn.execute("SELECT max_rooms_seen, min_rate_seen FROM suites WHERE suite_code = 'SFSSB'").fetchone() == (12, 299.99)


# --------------------------------------------------------------------------- CLI


def _seed_two_runs(path, payload):
    conn = db.connect(path)
    db.save_snapshot(conn, "2026-10-28", "2026-10-28T03:17:00+00:00", parse_availability(payload, "2026-11-18"))
    fuller = copy.deepcopy(payload)
    fuller["suites"][0]["availableRoomCount"] = 0
    fuller["suites"][0]["rateFamilies"][0]["rates"][0]["totalNightlyRate"] = 529.99
    db.save_snapshot(conn, "2026-11-04", "2026-11-04T03:17:00+00:00", parse_availability(fuller, "2026-11-18"))
    conn.close()


def run_cmd(**kwargs):
    lines = []
    code = cmd_suites(Namespace(**kwargs), out=lines.append)
    return code, "\n".join(lines)


def test_cli_suites_latest_snapshot(tmp_path, sample_payload):
    path = tmp_path / "gwl.db"
    _seed_two_runs(path, sample_payload)

    code, text = run_cmd(db=str(path), date=date(2026, 11, 18), code=None)
    assert code == 0
    assert "scraped 2026-11-04T03:17:00+00:00" in text  # latest run, not the first
    assert "7 rooms left, 3/5 suite types sold out" in text
    lines = text.splitlines()
    assert lines[1].split() == ["Suite", "Code", "Category", "Rooms", "left", "Cheapest", "Sold", "out"]
    sfssb = next(line for line in lines if "SFSSB" in line)
    assert "Superior Family Suite" in sfssb and "SOLD OUT" in sfssb and "$419.99" in sfssb
    cottage = next(line for line in lines if "CCTGE" in line)
    assert "cottage" in cottage and "$1,199.99" in cottage and "SOLD OUT" not in cottage
    # Rows follow the site's sortOrder.
    codes = [next(c for c in ("SFSSB", "SDLXB", "TWLFD", "PGRZL", "CCTGE") if c in line) for line in lines[3:]]
    assert codes == ["SFSSB", "SDLXB", "TWLFD", "PGRZL", "CCTGE"]


def test_cli_suites_booking_curve(tmp_path, sample_payload):
    path = tmp_path / "gwl.db"
    _seed_two_runs(path, sample_payload)

    code, text = run_cmd(db=str(path), date=date(2026, 11, 18), code="sfssb")
    assert code == 0
    lines = text.splitlines()
    assert "Booking curve for SFSSB, arrival 2026-11-18 (2 snapshots)" == lines[0]
    assert "Saver" in lines[1] and "Flexible" in lines[1]
    first, second = lines[3], lines[4]
    # Days out is lead_days: 03:17 UTC is the previous evening in Toronto (Oct 27 / Nov 3).
    assert first.split()[:4] == ["2026-10-28T03:17:00+00:00", "22", "12", "$379.99"]
    assert "$419.99" in first and "yes" not in first
    assert second.split()[:4] == ["2026-11-04T03:17:00+00:00", "15", "0", "$419.99"]
    assert "$529.99" in second and second.rstrip().endswith("yes")


def test_cli_suites_no_data(tmp_path, sample_payload):
    path = tmp_path / "gwl.db"
    _seed_two_runs(path, sample_payload)
    assert run_cmd(db=str(path), date=date(2027, 1, 1), code=None)[0] == 1
    assert run_cmd(db=str(path), date=date(2026, 11, 18), code="XXXXX")[0] == 1
    assert run_cmd(db=str(tmp_path / "missing.db"), date=date(2026, 11, 18), code=None)[0] == 1
    assert not (tmp_path / "missing.db").exists()


def test_cli_suites_via_main(tmp_path, sample_payload, capsys):
    path = tmp_path / "gwl.db"
    _seed_two_runs(path, sample_payload)
    assert main(["suites", "--date", "2026-11-18", "--db", str(path)]) == 0
    assert "Superior Family Suite" in capsys.readouterr().out
