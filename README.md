# Great Wolf Tracker

Track real demand at **Great Wolf Lodge Niagara** over time, so we can pick the quietest weeks with data instead of guesses.

Great Wolf doesn't publish attendance. But its booking engine prices dynamically and hides room types as they sell out, so nightly rate and availability work as a proxy for occupancy. This project polls those public signals on a schedule, stores every snapshot, and charts a week-by-week demand heatmap.

## Signals collected

For every arrival date over the next 365 days (1-night stay, 2 adults + 2 kids):

| Signal | Why it matters |
|---|---|
| Lowest available nightly rate | Dynamic pricing rises with expected occupancy |
| Rate for one fixed reference suite (Family Suite) | Apples-to-apples price over time |
| Number of suite types available / sold out | Cheap suites selling out = lodge filling up |
| Day pass availability | Day passes are generally only sold on lower-occupancy dates |

Optional enrichment (later):
- Google Trends weekly interest for "Great Wolf Lodge Niagara" (multi-year seasonal curve)
- Ontario + New York school break calendars overlaid on the heatmap
- Ontario and US holiday flags

## How it works

1. **Collector** — Python script that queries the booking pages / underlying availability API for each date. Discover the actual endpoints by inspecting network requests on greatwolf.com/niagara before writing scrapers; prefer the JSON API the site itself calls over HTML parsing.
2. **Storage** — SQLite (`data/gwl.db`). Each run appends rows; nothing is overwritten, so we can see how a date's price moves as it approaches (booking-curve data).
3. **Scheduler** — runs weekly (daily in the final 30 days before a date) as a Docker container / cron on the home server.
4. **Report** — generates a static HTML heatmap: ISO week × year, colour = demand index, plus a booking-curve chart per date.

## Demand index

Per arrival date, normalise to 0–100:

```
demand = 0.6 * rate_percentile + 0.3 * sold_out_ratio + 0.1 * (day_pass_blocked ? 1 : 0)
```

Weights are a starting point; tune once we have a few months of data.

## Data model (draft)

```
snapshots(
  scraped_at      TEXT,   -- UTC timestamp of the run
  arrival_date    TEXT,   -- YYYY-MM-DD
  min_rate        REAL,
  ref_suite_rate  REAL,
  suites_avail    INTEGER,
  suites_total    INTEGER,
  day_pass_open   INTEGER,  -- 1/0/NULL if unknown
  raw_json        TEXT      -- response kept for re-parsing
)
```

## Ground rules

- Low and polite: one request every few seconds, single-threaded, identifiable User-Agent, back off on errors / 429.
- Public pages only; no logins, no bookings, no promo-code probing.
- Review greatwolf.com terms of use before the first scheduled run.
- If automated requests get blocked, stop and reassess rather than evade.

## Roadmap

- [ ] Endpoint discovery (network inspection of the booking flow)
- [ ] Collector for rate + availability, one date
- [ ] Full 365-day sweep + SQLite storage
- [ ] Day pass check
- [ ] Docker image + weekly schedule on home server
- [ ] HTML heatmap report
- [ ] Google Trends + school calendar overlays
- [ ] Tune demand index after ~3 months of data

## Stack

Python 3.12 · httpx · SQLite · pandas · Plotly (static HTML) · Docker
