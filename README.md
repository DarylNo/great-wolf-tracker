# Great Wolf Tracker

Track real demand at **Great Wolf Lodge Niagara** over time, so we can pick the quietest weeks with data instead of guesses.

Great Wolf doesn't publish attendance. But its booking API reports how many rooms are still available for each suite type, and it prices dynamically, so remaining inventory (and secondarily, nightly rate) works as a proxy for occupancy. This project polls those public signals on a schedule, stores every snapshot, and charts a week-by-week demand heatmap.

## Signals collected

For every arrival date over the next 365 days (1-night stay, 2 adults), from the availability API (see [`docs/endpoints.md`](docs/endpoints.md)):

| Signal | Source field | Why it matters |
|---|---|---|
| **Total rooms available** (primary) | sum of `suites[].availableRoomCount` | Direct inventory: fewer rooms left = fuller lodge |
| Suite types sold out / total | `availableRoomCount == 0` across the 27 suite types | Shows how widely the lodge has sold out, not just the cheap suites |
| Rooms available per suite type | `suites[].availableRoomCount`, `suiteCategory` | Which categories (standard/themed/premium/cottage) fill first |
| Lowest available nightly rate (secondary) | min `totalNightlyRate` over suites that still have rooms | Dynamic pricing rises with expected occupancy |
| Rate for one fixed reference suite (Family Suite, `SFSSB`) | min `totalNightlyRate` for `SFSSB` | Apples-to-apples price over time |

Example from the live API (2026-10-28): Wed Nov 18 had 198 rooms left and 7/27 types sold out, from $379.99. Sat Nov 21 had 4 rooms left and 25/27 sold out, from $909.99.

Optional enrichment (later):
- Day pass availability (generally only sold on lower-occupancy dates)
- Google Trends weekly interest for "Great Wolf Lodge Niagara" (multi-year seasonal curve)
- Ontario + New York school break calendars overlaid on the heatmap
- Ontario and US holiday flags

## How it works

1. **Collector** (`gwt/collector.py`): calls the availability JSON API the booking site itself uses (`prod.api.greatwolf.com/room-booking/v2/lodges/NIAGON/availability`) for one arrival date, then parses the response into a run-level row plus one row per suite type. The raw response is kept with every `pricingKey` (signed booking token) stripped.
2. **Storage** (`gwt/db.py`): SQLite (`data/gwl.db`). Each run appends rows; nothing is overwritten, so we can see how a date's inventory and price move as it approaches (booking-curve data).
3. **Sweep** (`python -m gwt sweep --days 365`): single-threaded with a jittered 3–5 s delay between requests. It backs off exponentially on 429/5xx, aborts after 5 consecutive failures, and stops immediately on 403. It is resumable: re-running with the same run ID skips dates already captured.
4. **Scheduler**: runs weekly (daily for the next 30 days) as a Docker container under cron on the home server. See [Running on the home server](#running-on-the-home-server).
5. **Report** (not built yet): generates a static HTML heatmap (ISO week × year, colour = demand index) plus a booking-curve chart per date.

## Demand index

Per arrival date, normalise to 0–100:

```
demand = 100 * ( 0.6 * (1 - rooms_available / max_rooms_seen)
               + 0.3 * rate_percentile
               + 0.1 * sold_out_type_ratio )
```

- `rooms_available`: `total_rooms_available` for the date
- `max_rooms_seen`: the highest `total_rooms_available` ever observed for the same suite mix (same set of suite types). This is our best estimate of sellable capacity, since the API doesn't report it.
- `rate_percentile`: percentile of `min_rate` across all dates in the same run (0–1)
- `sold_out_type_ratio`: `suite_types_sold_out / suite_types`

Weights are a starting point; tune once we have a few months of data.

## Data model

```
snapshots(                          -- one row per arrival date per run
  run_id                 TEXT,      -- groups one sweep (default: UTC date); used for resume
  scraped_at             TEXT,      -- UTC timestamp
  arrival_date           TEXT,      -- YYYY-MM-DD
  total_rooms_available  INTEGER,   -- sum of availableRoomCount
  suite_types            INTEGER,   -- suite types returned (27 at Niagara)
  suite_types_sold_out   INTEGER,   -- availableRoomCount == 0
  min_rate               REAL,      -- lowest totalNightlyRate among suites with rooms
  ref_suite_rate         REAL,      -- lowest totalNightlyRate for SFSSB (NULL if sold out)
  error_code             TEXT,      -- response errorCode
  raw_json               TEXT       -- response with pricingKey stripped, for re-parsing
)

suite_snapshots(                    -- one row per suite type per arrival date per run
  run_id                 TEXT,
  scraped_at             TEXT,
  arrival_date           TEXT,
  suite_code             TEXT,      -- e.g. SFSSB
  suite_category         TEXT,      -- standard / themed / premium / cottage
  available_room_count   INTEGER,   -- 0 = sold out
  min_total_nightly_rate REAL
)
```

## Running locally

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest                                  # offline tests against tests/fixtures/
python -m gwt sweep --days 7 -v         # small live run
python -m gwt sweep --days 365          # full sweep (~25-30 min at 3-5 s/request)
```

Options: `--start YYYY-MM-DD` (first arrival date, default today UTC), `--db PATH` (default `data/gwl.db`), `--run-id ID` (re-use one to resume an interrupted sweep; default is today's UTC date, so re-running the same day resumes automatically). Set `GWT_USER_AGENT` to override the User-Agent.

Exit codes: `0` finished, `1` aborted (5 consecutive failures or a 403), `2` bad arguments.

## Running on the home server

Build the image and create a data directory owned by the container user (uid 1000):

```bash
git clone https://github.com/DarylNo/great-wolf-tracker.git /opt/great-wolf-tracker
cd /opt/great-wolf-tracker
docker build -t great-wolf-tracker .
sudo mkdir -p /srv/gwt/data && sudo chown 1000:1000 /srv/gwt/data

# one-off test run
docker run --rm -v /srv/gwt/data:/app/data great-wolf-tracker sweep --days 3 -v
```

Then add the schedule with `crontab -e`:

```cron
# Weekly full sweep: Sundays 03:17
17 3 * * 0  flock -n /tmp/gwt.lock docker run --rm -v /srv/gwt/data:/app/data great-wolf-tracker sweep --days 365 >> /srv/gwt/sweep.log 2>&1

# Daily sweep of the next 30 days: 05:43 (on Sundays it skips dates the weekly run already captured)
43 5 * * *  flock -n /tmp/gwt.lock docker run --rm -v /srv/gwt/data:/app/data great-wolf-tracker sweep --days 30 >> /srv/gwt/sweep.log 2>&1
```

- `flock -n` stops two sweeps from overlapping.
- Both jobs use the default run ID (today's UTC date), so they never fetch the same date twice in a day.
- If a run aborts, check `/srv/gwt/sweep.log`. Re-running the same command on the same UTC day resumes where it stopped. **A 403 means stop and reassess** (see Ground rules); don't just re-schedule it.
- The database is a single file (`/srv/gwt/data/gwl.db`); include it in the home server's backups.

## Ground rules

- Low and polite: one request every few seconds, single-threaded, identifiable User-Agent, back off on errors / 429.
- Public pages only; no logins, no bookings, no promo-code probing.
- Never store `pricingKey` booking tokens.
- Review greatwolf.com terms of use before the first scheduled run.
- If automated requests get blocked, stop and reassess rather than evade.

## Roadmap

- [x] Endpoint discovery (network inspection of the booking flow): see [`docs/endpoints.md`](docs/endpoints.md)
- [ ] Collector for rate + availability, one date (implemented; awaiting first live run on home server)
- [ ] Full 365-day sweep + SQLite storage (implemented; awaiting first live run on home server)
- [ ] Docker image + weekly schedule on home server (Dockerfile and cron docs done; deploy pending)
- [ ] HTML heatmap report
- [ ] Day pass check
- [ ] Google Trends + school calendar overlays
- [ ] Tune demand index after ~3 months of data

## Stack

Python 3.12 · httpx · SQLite · pandas · Plotly (static HTML) · Docker
