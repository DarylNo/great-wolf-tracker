"""School-closure calendar: YAML seed data, holidays merge, and the closure-night rule."""

import os
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
    assert exclusions == {"NY": {"Lincoln's Birthday", "Susan B. Anthony Day"}}
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
        ("Labor Day", date(2026, 9, 7), date(2026, 9, 7)),
        ("Columbus Day", date(2026, 10, 12), date(2026, 10, 12)),
        ("Election Day", date(2026, 11, 3), date(2026, 11, 3)),
        ("Veterans Day", date(2026, 11, 11), date(2026, 11, 11)),
        ("Thanksgiving recess", date(2026, 11, 25), date(2026, 11, 27)),
        ("Winter recess", date(2026, 12, 24), date(2027, 1, 1)),
        ("Martin Luther King Jr. Day", date(2027, 1, 18), date(2027, 1, 18)),
        ("Presidents' Day", date(2027, 2, 15), date(2027, 2, 15)),
        ("Mid-winter recess", date(2027, 2, 16), date(2027, 2, 19)),
        ("Good Friday", date(2027, 3, 26), date(2027, 3, 26)),
        ("Spring recess", date(2027, 3, 30), date(2027, 4, 2)),
        ("Memorial Day", date(2027, 5, 31), date(2027, 5, 31)),
        ("Juneteenth (observed)", date(2027, 6, 18), date(2027, 6, 18)),
        ("Last student day", date(2027, 6, 24), date(2027, 6, 24)),
    }
    assert {(r[1], r[2], r[3]) for r in ny} == expected_ny
    # The last student day is informational, not a closure.
    assert {r[1] for r in ny if not r[6]} == {"Last student day"}
    # The Buffalo one-day closures the review listed are all present as explicit entries.
    one_day = {r[2] for r in ny if r[2] == r[3] and r[6]}
    assert {date(2026, 9, 7), date(2026, 10, 12), date(2026, 11, 3), date(2026, 11, 11), date(2027, 1, 18),
            date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31), date(2027, 6, 18)} <= one_day


def test_ny_flagged_nights_nov_2026_to_jun_2027(cal_2027):
    """Every New York night flagged in the 2026-27 school year after October, and why.

    Lincoln's Birthday (Fri Feb 12) and Susan B. Anthony Day are excluded, so Thu Feb 11 is not
    an extra night; Election Day and Veterans Day still close Buffalo schools."""
    flagged = {
        night: next(f for f in flags if f.region == "NY").labels
        for night, flags in cal_2027.flagged_nights(date(2026, 11, 1), date(2027, 6, 30)).items()
        if any(f.region == "NY" for f in flags)
    }
    expected = {
        date(2026, 11, 2): ("Election Day",),
        date(2026, 11, 10): ("Veterans Day",),
        date(2026, 11, 24): ("Thanksgiving recess",),
        date(2026, 11, 25): ("Thanksgiving recess",),
        date(2026, 11, 26): ("Thanksgiving recess",),
        date(2026, 12, 23): ("Winter recess",),
        date(2026, 12, 24): ("Winter recess",),
        date(2026, 12, 27): ("Winter recess",),
        date(2026, 12, 28): ("Winter recess",),
        date(2026, 12, 29): ("Winter recess",),
        date(2026, 12, 30): ("Winter recess",),
        date(2026, 12, 31): ("Winter recess",),
        date(2027, 1, 17): ("Martin Luther King Jr. Day",),
        date(2027, 2, 14): ("Presidents' Day",),
        date(2027, 2, 15): ("Mid-winter recess",),
        date(2027, 2, 16): ("Mid-winter recess",),
        date(2027, 2, 17): ("Mid-winter recess",),
        date(2027, 2, 18): ("Mid-winter recess",),
        date(2027, 3, 25): ("Good Friday",),
        date(2027, 3, 29): ("Spring recess",),
        date(2027, 3, 30): ("Spring recess",),
        date(2027, 3, 31): ("Spring recess",),
        date(2027, 4, 1): ("Spring recess",),
        date(2027, 5, 30): ("Memorial Day",),
        date(2027, 6, 17): ("Juneteenth (observed)",),
    }
    assert flagged == expected
    assert date(2027, 2, 11) not in flagged
    assert not cal_2027.is_closure_day("NY", date(2027, 2, 12))


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
    # Thanksgiving Mon 2026-10-12 (CA-ON) isn't in the TDSB list: it comes from the package.
    thanksgiving_ca = cal_2027.night_flags(date(2026, 10, 11))
    on = [f for f in thanksgiving_ca if f.region == "ON"]
    assert on and on[0].labels == ("Thanksgiving Day (public holiday)",)
    # Columbus Day Mon 2027-10-11 (US-NY) is past the listed Buffalo year: package only.
    columbus = cal_2027.night_flags(date(2027, 10, 10))
    assert [f.labels for f in columbus if f.region == "NY"] == [("Columbus Day (public holiday)",)]


def test_school_entry_and_holiday_with_the_same_name_are_not_doubled(cal_2027):
    assert cal_2027.closure_labels("ON", date(2027, 5, 24)) == ["Victoria Day"]


def test_school_entry_is_the_only_label_on_its_days(cal_2027):
    # The board's "Presidents' Day" isn't repeated as the package's "Washington's Birthday".
    assert cal_2027.closure_labels("NY", date(2027, 2, 15)) == ["Presidents' Day"]
    # Inside a break, the break names the day (no "Christmas Day (public holiday)" alongside).
    assert cal_2027.closure_labels("ON", date(2026, 12, 25)) == ["Winter break"]
    assert cal_2027.closure_labels("NY", date(2026, 12, 25)) == ["Winter recess"]


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
    assert cal.problem is None and events.events_problem() is None


@pytest.mark.parametrize(
    "content, message",
    [
        (None, "not found"),
        ("events: [unclosed\n", "not valid YAML"),
        ("events:\n  - {region: ON, name: X, start: 2027-01-01, end: 2027-01-01, source: s, official: true}\n",
         "quote"),
        (b"\xff\xfe\x00bad", "can't read"),
    ],
)
def test_unusable_events_file_falls_back_to_the_bundled_calendar(tmp_path, monkeypatch, caplog, content, message):
    custom = tmp_path / "events.yaml"
    if isinstance(content, bytes):
        custom.write_bytes(content)
    elif content is not None:
        custom.write_text(content)
    monkeypatch.setenv("GWT_EVENTS_FILE", str(custom))
    with caplog.at_level("ERROR", logger="gwt.events"):
        cal = events.calendar_for_range(date(2026, 10, 1), date(2027, 9, 30))
    # The bundled calendar is in use: March Break is still flagged.
    assert any("March Break" in f.text for f in cal.night_flags(date(2027, 3, 15)))
    problem = events.events_problem()
    assert problem["file"] == str(custom) and message in problem["error"]
    assert cal.problem == problem
    if content is not None:
        assert message in caplog.text  # logged once when the bad file is first read
    # Fixing the file clears the problem (cache is keyed by mtime).
    custom.write_text("events: []\n")
    os.utime(custom, (1_900_000_000, 1_900_000_000))
    assert events.events_problem() is None
