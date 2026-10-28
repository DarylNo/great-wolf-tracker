"""The dashboard's metric definitions (gwt.metrics)."""

from dataclasses import dataclass
from datetime import date

import pytest

from gwt import metrics as m


@dataclass
class Snap:
    lead: int | None = None
    scrape_date: date | None = None
    rooms: int | None = None
    tag: str = ""


# --------------------------------------------------------------------------- lead_days (Toronto)


def test_scrape_date_is_toronto_not_utc():
    # 03:30 UTC on Nov 2 is 22:30 EST on Nov 1 in Toronto.
    assert m.scrape_local_date("2026-11-02T03:30:00+00:00") == date(2026, 11, 1)
    assert m.lead_days("2026-11-10", "2026-11-02T03:30:00+00:00") == 9  # UTC date would give 8


@pytest.mark.parametrize(
    "scraped_at, toronto_date",
    [
        # Summer, EDT (UTC-4): 04:30 UTC is 00:30 the same day...
        ("2026-06-01T04:30:00+00:00", date(2026, 6, 1)),
        # ...winter, EST (UTC-5): the same clock time is 23:30 the day before.
        ("2026-12-01T04:30:00+00:00", date(2026, 11, 30)),
        # Spring-forward night 2026-03-08 (02:00 EST = 07:00 UTC).
        ("2026-03-08T04:59:59+00:00", date(2026, 3, 7)),  # 23:59:59 EST
        ("2026-03-08T05:00:00+00:00", date(2026, 3, 8)),  # 00:00 EST
        ("2026-03-09T03:59:59+00:00", date(2026, 3, 8)),  # 23:59:59 EDT
        ("2026-03-09T04:00:00+00:00", date(2026, 3, 9)),  # 00:00 EDT
        # Fall-back night 2026-11-01 (02:00 EDT = 06:00 UTC).
        ("2026-11-01T03:59:59+00:00", date(2026, 10, 31)),  # 23:59:59 EDT
        ("2026-11-01T04:00:00+00:00", date(2026, 11, 1)),  # 00:00 EDT
        ("2026-11-01T06:30:00+00:00", date(2026, 11, 1)),  # 01:30 EST (the repeated hour)
        ("2026-11-02T04:59:59+00:00", date(2026, 11, 1)),  # 23:59:59 EST
        ("2026-11-02T05:00:00+00:00", date(2026, 11, 2)),  # 00:00 EST
        # The scheduled sweeps (03:17 / 05:43 local) land on the local day in both seasons.
        ("2026-07-05T07:17:00+00:00", date(2026, 7, 5)),
        ("2026-12-06T08:17:00+00:00", date(2026, 12, 6)),
    ],
)
def test_scrape_local_date_across_dst(scraped_at, toronto_date):
    assert m.scrape_local_date(scraped_at) == toronto_date


def test_scrape_local_date_accepts_z_and_naive_utc():
    assert m.scrape_local_date("2026-11-02T03:30:00Z") == date(2026, 11, 1)
    assert m.scrape_local_date("2026-11-02T03:30:00") == date(2026, 11, 1)  # naive = UTC


def test_lead_days_accepts_date_or_string():
    assert m.lead_days(date(2026, 11, 21), "2026-10-28T07:17:00+00:00") == 24
    assert m.lead_days("2026-11-21", "2026-10-28T07:17:00+00:00") == 24


@pytest.mark.parametrize(
    "lead, bucket",
    [(0, 0), (1, 0), (2, 1), (3, 1), (4, 2), (7, 2), (8, 3), (14, 3), (15, 4), (30, 4), (31, 5), (60, 5),
     (61, 6), (90, 6), (91, 7), (180, 7), (181, 8), (365, 8), (366, None), (-1, None), (None, None)],
)
def test_lead_bucket_boundaries(lead, bucket):
    assert m.lead_bucket(lead) == bucket


def test_bucket_labels():
    assert m.BUCKET_LABELS == ("0–1", "2–3", "4–7", "8–14", "15–30", "31–60", "61–90", "91–180", "181–365")


# --------------------------------------------------------------------------- capacity / % sold


def test_capacity_estimate_is_max_ever_seen():
    assert m.capacity_estimate([None, 10, 250, 3]) == 250
    assert m.capacity_estimate([]) is None
    assert m.capacity_estimate([0, None]) is None


def test_capacity_caveat_until_60_far_dates():
    assert m.capacity_is_provisional(0)
    assert m.capacity_is_provisional(59)
    assert not m.capacity_is_provisional(60)
    assert m.CAPACITY_CAVEAT == "capacity estimate improves after the first full sweep"


@pytest.mark.parametrize(
    "rooms, cap, expected",
    [(100, 400, 0.75), (400, 400, 0.0), (0, 400, 1.0), (500, 400, 0.0), (None, 400, None), (10, None, None), (10, 0, None)],
)
def test_pct_sold(rooms, cap, expected):
    assert m.pct_sold(rooms, cap) == expected


# --------------------------------------------------------------------------- pace


def test_percentile_rank_counts_ties_half():
    assert m.percentile_rank([1, 2, 3, 4], 3) == 62.5
    assert m.percentile_rank([1, 2, 3, 4], 0) == 0
    assert m.percentile_rank([1, 2, 3, 4], 5) == 100
    assert m.percentile_rank([1, 1, 1, 1], 1) == 50
    assert m.percentile_rank([], 1) is None


def test_pace_is_percentile_within_the_same_lead_bucket():
    # 20 snapshots 15-30 days out, evenly spread 0.0 .. 0.95 sold; a busier bucket 0-1 days out.
    pairs = [(20, i / 20) for i in range(20)] + [(1, 0.9 + i / 1000) for i in range(25)]
    table = m.PaceTable(pairs)
    assert table.size(20) == 20 and table.size(0) == 25
    assert table.pace(16, 0.95) == pytest.approx(97.5)  # top of its own bucket
    assert table.pace(29, 0.0) == pytest.approx(2.5)
    # The same 0.9 sold is low for 0-1 days out but high for 15-30 days out.
    assert table.pace(0, 0.9) < 10 < 90 < table.pace(20, 0.9)
    assert table.pace(400, 0.5) is None  # outside every bucket
    assert table.pace(20, None) is None


def test_pace_is_na_below_20_snapshots():
    table = m.PaceTable([(40, i / 19) for i in range(19)])
    assert table.size(40) == 19
    assert table.pace(40, 0.5) is None
    assert m.PaceTable([(40, i / 20) for i in range(20)]).pace(40, 0.5) is not None


# --------------------------------------------------------------------------- final fill / pickup


def test_final_fill_takes_the_closest_scrape_within_two_days():
    snaps = [Snap(lead=30), Snap(lead=9), Snap(lead=2), Snap(lead=1, tag="last")]
    assert m.final_fill(snaps).tag == "last"
    assert m.final_fill([Snap(lead=30), Snap(lead=9), Snap(lead=3)]) is None  # closest is 3 days out
    assert m.final_fill([Snap(lead=3, tag="x")], max_lead=3).tag == "x"  # configurable
    assert m.final_fill([Snap(lead=0, tag="a"), Snap(lead=0, tag="b")]).tag == "b"  # ties: later scrape
    assert m.final_fill([Snap(lead=-1)]) is None
    assert m.final_fill([]) is None


def test_pickup_7d():
    now = Snap(scrape_date=date(2026, 11, 10), rooms=100)
    week_ago = Snap(scrape_date=date(2026, 11, 3), rooms=130)
    assert m.pickup_7d([week_ago, now], now) == 30
    # 8 and 6 days earlier are equally close to 7: the more recent (6 days) wins.
    six = Snap(scrape_date=date(2026, 11, 4), rooms=125)
    eight = Snap(scrape_date=date(2026, 11, 2), rooms=135)
    assert m.pickup_7d([eight, six, now], now) == 25
    # Two scrapes on the same day: the later one counts.
    later = Snap(scrape_date=date(2026, 11, 3), rooms=128)
    assert m.pickup_7d([week_ago, later, now], now) == 28
    # Nothing 6-8 days earlier -> n/a.
    assert m.pickup_7d([Snap(scrape_date=date(2026, 11, 1), rooms=150), now], now) is None  # 9 days
    assert m.pickup_7d([Snap(scrape_date=date(2026, 11, 5), rooms=150), now], now) is None  # 5 days
    assert m.pickup_7d([week_ago], Snap(scrape_date=date(2026, 11, 10), rooms=None)) is None
    # Rooms can come back (cancellations): pickup goes negative.
    assert m.pickup_7d([Snap(scrape_date=date(2026, 11, 3), rooms=90)], now) == -10


# --------------------------------------------------------------------------- bands / colour classes


def test_quantile_interpolates():
    assert m.quantile([1, 2, 3, 4, 5], 0.5) == 3
    assert m.quantile([1, 2, 3, 4, 5], 0.1) == pytest.approx(1.4)
    assert m.quantile([7], 0.9) == 7
    assert m.quantile([], 0.5) is None


def test_band_by_bucket():
    pairs = [(20, v) for v in range(1, 11)] + [(200, 5)] * 3
    band = m.band_by_bucket(pairs, min_n=5)
    b = band[m.lead_bucket(20)]
    assert (b["lead_lo"], b["lead_hi"], b["n"]) == (15, 30, 10)
    assert b["p50"] == pytest.approx(5.5)
    assert b["p10"] == pytest.approx(1.9) and b["p90"] == pytest.approx(9.1)
    assert band[m.lead_bucket(200)] is None  # only 3 values
    assert band[0] is None


def test_quantile_breaks_and_classes():
    values = list(range(1, 11))
    breaks = m.quantile_breaks(values, 5)
    assert breaks == [3, 5, 7, 9]
    assert [m.class_index(v, breaks) for v in (1, 2, 3, 8, 9, 10)] == [0, 0, 1, 3, 4, 4]
    assert m.class_ranges(values, breaks) == [(1, 2), (3, 4), (5, 6), (7, 8), (9, 10)]


def test_quantile_breaks_with_ties_and_edge_cases():
    assert m.quantile_breaks([5, 5, 5], 5) == []  # one class
    assert m.class_ranges([5, 5, 5], []) == [(5, 5)]
    assert m.quantile_breaks([], 5) == []
    assert m.quantile_breaks([None, 3], 5) == []
    tied = [1, 1, 1, 1, 1, 1, 1, 1, 2, 10]
    breaks = m.quantile_breaks(tied, 5)
    assert breaks == sorted(set(breaks)) and all(b > 1 for b in breaks)
    assert len(m.class_ranges(tied, breaks)) == len(breaks) + 1  # every class has members


def test_prices_get_real_value_legend_ranges():
    prices = [399.99, 419.99, 449.99, 499.99, 549.99, 649.99, 749.99, 899.99, 949.99, 999.99]
    breaks = m.quantile_breaks(prices, 5)
    ranges = m.class_ranges(prices, breaks)
    assert ranges[0] == (399.99, 419.99) and ranges[-1] == (949.99, 999.99)
    assert all(lo <= hi for lo, hi in ranges)
    assert all(ranges[i][1] < ranges[i + 1][0] for i in range(len(ranges) - 1))


@pytest.mark.parametrize("cls, n, step", [(0, 5, 0), (4, 5, 4), (2, 5, 2), (0, 1, 2), (0, 2, 0), (1, 2, 4), (1, 3, 2)])
def test_ramp_step(cls, n, step):
    assert m.ramp_step(cls, n) == step


def test_calendar_helpers():
    assert m.week_of_year(date(2026, 1, 1)) == 1
    assert m.week_of_year(date(2026, 1, 7)) == 1
    assert m.week_of_year(date(2026, 1, 8)) == 2
    assert m.week_of_year(date(2026, 12, 31)) == 52
    assert m.is_weekend_night(date(2026, 11, 20))  # Friday
    assert m.is_weekend_night(date(2026, 11, 21))  # Saturday
    assert not m.is_weekend_night(date(2026, 11, 22))  # Sunday
    assert m.week_position(date(2026, 1, 4)) == 1.0
