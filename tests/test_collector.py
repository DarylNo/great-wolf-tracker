import json
from datetime import date

import httpx

from gwt.collector import (
    AVAILABILITY_PATH,
    build_params,
    fetch_availability,
    make_client,
    parse_availability,
    strip_sensitive,
)


def test_build_params_one_night():
    params = build_params(date(2026, 12, 31))
    assert params == {
        "arrival": "2026-12-31",
        "departure": "2027-01-01",
        "location": "NIAGON",
        "numberOfAdults": "2",
        "offerCode": "",
        "kidsAges": "",
    }
    assert "uuid" not in params


def test_parse_run_level_row(sample_payload):
    snap = parse_availability(sample_payload, date(2026, 11, 18))
    assert snap.arrival_date == "2026-11-18"
    assert snap.total_rooms_available == 12 + 0 + 5 + 0 + 2
    assert snap.suite_types == 5
    assert snap.suite_types_sold_out == 2
    # Sold-out SDLXB has a cheaper rate (299.99) but must not count toward min_rate.
    assert snap.min_rate == 379.99
    # Reference suite: lowest of its Saver/Flexible rates.
    assert snap.ref_suite_rate == 379.99
    assert snap.error_code is None


def test_parse_suite_rows(sample_payload):
    snap = parse_availability(sample_payload, "2026-11-18")
    by_code = {s.suite_code: s for s in snap.suites}
    assert list(by_code) == ["SFSSB", "SDLXB", "TWLFD", "PGRZL", "CCTGE"]
    assert by_code["SFSSB"].suite_category == "standard"
    assert by_code["SFSSB"].available_room_count == 12
    assert by_code["SFSSB"].min_total_nightly_rate == 379.99
    assert by_code["SDLXB"].available_room_count == 0
    assert by_code["SDLXB"].min_total_nightly_rate == 299.99
    assert by_code["PGRZL"].min_total_nightly_rate is None  # no rateFamilies
    assert by_code["CCTGE"].suite_category == "cottage"


def test_raw_json_has_no_pricing_keys(sample_payload):
    snap = parse_availability(sample_payload, "2026-11-18")
    assert "pricingKey" not in snap.raw_json
    assert "eyJ" not in snap.raw_json
    raw = json.loads(snap.raw_json)
    # Everything else is kept for re-parsing.
    assert raw["suites"][0]["rateFamilies"][0]["rates"][0]["totalNightlyRate"] == 379.99
    assert raw["isProperty"] is True


def test_strip_sensitive_does_not_mutate_input(sample_payload):
    before = json.dumps(sample_payload, sort_keys=True)
    stripped = strip_sensitive(sample_payload)
    assert json.dumps(sample_payload, sort_keys=True) == before
    assert "pricingKey" not in json.dumps(stripped)


def test_ref_suite_sold_out_gives_none(sample_payload):
    sample_payload["suites"][0]["availableRoomCount"] = 0
    snap = parse_availability(sample_payload, "2026-11-21")
    assert snap.ref_suite_rate is None
    assert snap.suite_types_sold_out == 3
    assert snap.min_rate == 459.99


def test_fully_sold_out_and_error_code():
    payload = {
        "suites": [
            {"suiteCode": "SFSSB", "suiteCategory": "standard", "availableRoomCount": 0, "rateFamilies": []},
            {"suiteCode": "TWLFD", "suiteCategory": "themed", "availableRoomCount": 0, "rateFamilies": []},
        ],
        "errorCode": "NO_AVAILABILITY",
    }
    snap = parse_availability(payload, "2026-12-26")
    assert snap.total_rooms_available == 0
    assert snap.suite_types_sold_out == 2
    assert snap.min_rate is None
    assert snap.ref_suite_rate is None
    assert snap.error_code == "NO_AVAILABILITY"


def test_empty_payload():
    snap = parse_availability({}, "2026-12-26")
    assert snap.suite_types == 0
    assert snap.total_rooms_available == 0
    assert snap.min_rate is None


def test_fetch_sends_expected_request(sample_payload):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = request.url
        seen["ua"] = request.headers["User-Agent"]
        seen["cookie"] = request.headers.get("Cookie")
        return httpx.Response(200, json=sample_payload)

    with make_client(transport=httpx.MockTransport(handler)) as client:
        resp = fetch_availability(client, date(2026, 11, 18))

    assert resp.status_code == 200
    assert seen["url"].host == "prod.api.greatwolf.com"
    assert seen["url"].path == AVAILABILITY_PATH
    assert seen["url"].params["arrival"] == "2026-11-18"
    assert seen["url"].params["departure"] == "2026-11-19"
    assert seen["ua"].startswith("great-wolf-tracker/")
    assert seen["cookie"] is None
