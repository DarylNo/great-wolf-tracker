"""School-closure calendar: YAML seed data, holidays merge, and the closure-night rule."""

from datetime import date

import pytest

from gwt import events
from gwt.events import ClosureCalendar, EventsFileError, merge_spans, parse_events_yaml

TDSB = "https://www.tdsb.on.ca/Portals/0/docs/2026-27%20TDSB%20School%20Year%20Calendar%20Key%20Dates.pdf"
BUFFALO = "https://calendarstates.com/new-york/buffalo-public-schools/"


@pytest.fixture
def cal_2027():
    return ClosureCalendar.for_range(date(2026, 10, 1), date(2027, 9, 30))


def test_seed_yaml_dates_sources_and_official_flags():
    school, exclusions = events.load_school_events()
    assert exclusions == {}
    rows = {(e.region, e.name, e.start, e.end, e.source, e.official, e.closure) for e in school}
    on = {r for r in rows if r[0] == "ON"}
    ny = {r for r in rows if r[0] == "NY"}
    assert all(r[4] == TDSB and r[5] is True for r in on)
    assert all(r[4] == BUFFALO and r[5] is False for r in ny)
    expected_on = {
        ("Winter break", date(2026, 12, 21), date(2027, 1, 1)),
        ("March Break", date(2027, 3, 15), date(2027, 3, 19)),
        ("PA day", date(2026, 11, 20), date(2026, 11, 20)),
        ("PA day (elementary)", date(2027, 1, 15), date(2027, 1, 15)),
        ("PA day", date(2027, 2, 12), date(2027, 2, 12)),
        ("Good Friday", date(2027, 3, 26), date(2027, 3, 26)),
        ("Easter Monday", date(2027, 3, 29), date(2027, 3, 29)),
        ("Family Day", date(2027, 2, 15), date(2027, 2, 15)),
        ("Victoria Day", date(2027, 5, 24), date(2027, 5, 24)),
    }
    assert {(r[1], r[2], r[3]) for r in on} == expected_on
    expected_ny = {
        ("Thanksgiving recess", date(2026, 11, 25), date(2026, 11, 27)),
        ("Winter recess", date(2026, 12, 24), date(2027, 1, 1)),
        ("Presidents' Day", date(2027, 2, 15), date(2027, 2, 15)),
        ("Mid-winter recess", date(2027, 2, 16), date(2027, 2, 19)),
        ("Good Friday", date(2027, 3, 26), date(2027, 3, 26)),
        ("Spring recess", date(2027, 3, 30), date(2027, 4, 2)),
        ("Last student day", date(2027, 6, 24), date(2027, 6, 24)),
    }
    assert {(r[1], r[2], r[3]) for r in ny} == expected_ny
    # The last student day is informational, not a closure.
    assert {r[1] for r in ny if not r[6]} == {"Last student day"}


def test_march_break_nights_are_flagged(cal_2027):
    # ON March Break 2027-03-15..19: nights of Sun 14 .. Thu 18 have a closure the next morning.
    for day in range(14, 19):
        flags = cal_2027.night_flags(date(2027, 3, day))
        assert any(f.region == "ON" and "March Break" in f.text for f in flags), day
    # Friday 12th: the next morning is a regular Saturday.
    assert cal_2027.night_flags(date(2027, 3, 12)) == []
    # Friday 19th: the break ends with a regular weekend, so no extra night.
    assert cal_2027.night_flags(date(2027, 3, 19)) == []


def test_weekend_mornings_never_flag(cal_2027):
    # Sat 2026-12-26 is inside ON winter break, but a Saturday morning is a regular weekend.
    assert cal_2027.is_closure_day("ON", date(2026, 12, 26))
    assert cal_2027.night_flags(date(2026, 12, 25)) == []
    # Mon 2026-12-28 is a closure day, so Sunday night is an extra night.
    assert [f.region for f in cal_2027.night_flags(date(2026, 12, 27))] == ["ON", "NY"]


def test_public_holidays_are_merged(cal_2027):
    mlk = cal_2027.night_flags(date(2027, 1, 17))  # MLK Day Mon 2027-01-18 (US-NY)
    assert [f.region for f in mlk] == ["NY"]
    assert "Martin Luther King Jr. Day (public holiday)" in mlk[0].labels
    thanksgiving_ca = cal_2027.night_flags(date(2026, 10, 11))  # Thanksgiving Mon 2026-10-12 (CA-ON)
    assert "ON" in [f.region for f in thanksgiving_ca]


def test_school_entry_and_holiday_with_the_same_name_are_not_doubled(cal_2027):
    assert cal_2027.closure_labels("ON", date(2027, 5, 24)) == ["Victoria Day"]


def test_informational_events_are_tags_not_flags(cal_2027):
    assert cal_2027.info_tags(date(2027, 6, 24)) == ["NY: Last student day"]
    assert all(f.region != "NY" for f in cal_2027.night_flags(date(2027, 6, 23)))


def test_flagged_nights_and_spans(cal_2027):
    nights = cal_2027.flagged_nights(date(2027, 3, 10), date(2027, 3, 20))
    assert sorted(nights) == [date(2027, 3, d) for d in range(14, 19)]
    spans = merge_spans(nights)
    assert spans == [(date(2027, 3, 14), date(2027, 3, 18))]
    assert merge_spans([date(2027, 1, 1), date(2027, 1, 2), date(2027, 1, 5)]) == [
        (date(2027, 1, 1), date(2027, 1, 2)),
        (date(2027, 1, 5), date(2027, 1, 5)),
    ]


@pytest.mark.parametrize(
    "text, message",
    [
        ("events:\n  - {region: ON, name: X, start: 2027-01-01, end: 2027-01-01, source: s, official: true}\n", "quote"),
        ("events:\n  - {region: 'NY', name: X, start: 2027-01-01, end: 2027-01-01, source: s}\n", "missing official"),
        ("events:\n  - {region: 'NY', name: X, start: 2027-01-02, end: 2027-01-01, source: s, official: true}\n", "before"),
        ("events:\n  - {region: 'QC', name: X, start: 2027-01-01, end: 2027-01-01, source: s, official: true}\n", "unknown region"),
        ("events:\n  - {region: 'NY', name: X, start: soon, end: 2027-01-01, source: s, official: true}\n", "YYYY-MM-DD"),
    ],
)
def test_bad_yaml_is_rejected_with_a_clear_message(text, message):
    with pytest.raises(EventsFileError, match=message):
        parse_events_yaml(text)


def test_holiday_exclusions_drop_named_holidays_only():
    _, exclusions = parse_events_yaml(
        "events: []\nexclude_holidays:\n  'NY': [\"Lincoln's Birthday\", Susan B. Anthony Day]\n"
    )
    hol = events.holiday_events([2027], exclusions)
    ny = {e.start: e.name for e in hol if e.region == "NY"}
    assert date(2027, 2, 12) not in ny  # Lincoln's Birthday dropped
    assert ny[date(2027, 2, 15)] == "Washington's Birthday"  # combined entry keeps the rest


def test_events_file_override(tmp_path, monkeypatch):
    custom = tmp_path / "events.yaml"
    custom.write_text(
        "events:\n"
        "  - {region: 'ON', name: Test break, start: 2027-10-04, end: 2027-10-08, source: test, official: false}\n"
    )
    monkeypatch.setenv("GWT_EVENTS_FILE", str(custom))
    cal = events.calendar_for_range(date(2027, 9, 1), date(2027, 10, 31))
    assert any("Test break" in f.text for f in cal.night_flags(date(2027, 10, 3)))
