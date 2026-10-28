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
   Every invocation is logged to a `runs` table (counts, failures, aborted / blocked).
4. **Scheduler** (`gwt/scheduler.py`): APScheduler runs inside the container next to the web server. By default it does a full 365-day sweep on Sundays at 03:17 and a 30-day sweep daily at 05:43 (America/Toronto). A file lock means two sweeps never overlap, including one started by hand with `docker compose exec`.
5. **Web dashboard** (`gwt/web/`): FastAPI with server-rendered Jinja templates and Plotly.js from a pinned CDN version, so there's no frontend build. It reads the database read-only and also serves a JSON API under `/api/...`. See [Web dashboard](#web-dashboard).

## Demand index

Per arrival date, normalise to 0–100:

```
demand = 100 * ( 0.6 * (1 - rooms_available / max_rooms_seen)
               + 0.3 * rate_percentile
               + 0.1 * sold_out_type_ratio )
```

- `rooms_available`: `total_rooms_available` for the date
- `max_rooms_seen`: the highest `total_rooms_available` ever observed for the same suite mix (same set of suite types). This is our best estimate of sellable capacity, since the API doesn't report it.
- `rate_percentile`: percentile (0–1) of `min_rate` within the set of dates being compared. On the overview that set is the next 365 days. On Trends and the ISO-week view it is every arrival date ever captured. A date with no available rate at all (fully sold out) counts as the top percentile.
- `sold_out_type_ratio`: `suite_types_sold_out / suite_types`

Each arrival date uses its latest snapshot. For past dates that is the scrape closest to arrival, the best estimate of how full the lodge actually got. The suite mix is approximated by the number of suite types returned. Implementation: `gwt/metrics.py`.

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
  suite_brand            TEXT,      -- human name, e.g. "Superior Family Suite"
  suite_category         TEXT,      -- standard / themed / premium / cottage
  base_occupancy         INTEGER,
  max_occupancy          INTEGER,
  is_accessible          INTEGER,   -- isAccessible: 1/0 (NULL if missing)
  num_beds               INTEGER,   -- numOfBeds as int (NULL if unparseable)
  num_baths              INTEGER,   -- numOfBaths as int (NULL if unparseable)
  sort_order             INTEGER,   -- the site's display order
  available_room_count   INTEGER,   -- 0 = sold out
  min_total_nightly_rate REAL       -- cheapest rate across all families
)

suite_rate_families(                -- cheapest rate per rate family, per suite snapshot
  suite_snapshot_id      INTEGER,   -- -> suite_snapshots.id
  family_title           TEXT,      -- "Saver", "Flexible", ...
  policy_type            TEXT,
  display_rank           INTEGER,
  min_total_nightly_rate REAL
)

suites(                             -- lookup: one row per suite type ever seen, upserted every save
  suite_code             TEXT PRIMARY KEY,
  suite_brand            TEXT,
  suite_category         TEXT,
  max_occupancy          INTEGER,
  is_accessible          INTEGER,
  first_seen             TEXT,      -- scraped_at of first sighting
  last_seen              TEXT       -- scraped_at of latest sighting
)

runs(                               -- one row per sweep invocation (a resumed run_id gets a new row)
  run_id                 TEXT,
  trigger                TEXT,      -- cli / schedule:weekly / schedule:daily
  started_at, finished_at TEXT,
  start_date             TEXT,      -- first arrival date requested
  days                   INTEGER,
  fetched, skipped, failed_dates INTEGER,
  failures               INTEGER,   -- failed requests, including retried ones
  status                 TEXT,      -- running / completed / aborted / error / interrupted
  blocked                INTEGER,   -- 1 if the run stopped on HTTP 403
  message                TEXT       -- abort / error reason
)
```

A suite whose `last_seen` is older than the latest run has dropped out of the API, and a recent `first_seen` means a new suite type. Rate families live in a child table rather than a JSON column so they can be queried directly (e.g. the Saver vs Flexible spread over time).

**Accessibility, beds and baths** come straight from each suite in the API response: `isAccessible` (boolean, stored as 1/0), and `numOfBeds` / `numOfBaths` (sent as strings like `"2"`, stored as integers). A missing or unparseable value is stored as NULL. See [`docs/endpoints.md`](docs/endpoints.md).

Older databases get the new `suite_snapshots` columns added automatically on connect.

## Running locally

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest                                  # offline tests against tests/fixtures/
python -m gwt sweep --days 7 -v         # small live run
python -m gwt sweep --days 365          # full sweep (~25-30 min at 3-5 s/request)

# Query what's stored
python -m gwt suites --date 2026-11-21               # latest snapshot: every room type for that night
python -m gwt suites --date 2026-11-21 --code SFSSB  # booking curve for one suite across runs

# Web dashboard on http://localhost:8080 (add --no-scheduler to skip the built-in schedule)
python -m gwt serve --no-scheduler
```

`suites --date` prints suite name, code, category, rooms left, cheapest rate and a SOLD OUT flag, using the most recent scrape of that date. Adding `--code` prints one row per scrape run with days before arrival, rooms left, cheapest rate, the cheapest rate in each rate family (Saver, Flexible, ...) and a sold-out flag. Both take `--db PATH`.

Options: `--start YYYY-MM-DD` (first arrival date, default today UTC), `--db PATH` (default `$GWT_DB` or `data/gwl.db`), `--run-id ID` (re-use one to resume an interrupted sweep; default is today's UTC date, so re-running the same day resumes automatically). Set `GWT_USER_AGENT` to override the User-Agent.

Exit codes: `0` finished, `1` aborted (5 consecutive failures or a 403), `2` bad arguments, `3` another sweep is already running.

## Run on the home server (docker compose)

One container serves the dashboard on port 8080 and runs the sweep schedule itself, so no host cron is needed.

```bash
git clone https://github.com/DarylNo/great-wolf-tracker.git /opt/great-wolf-tracker
cd /opt/great-wolf-tracker
mkdir -p data && sudo chown 1000:1000 data     # container runs as uid 1000
docker compose up -d --build
docker compose logs -f                          # shows the next scheduled run times
```

Open `http://<server>:8080`. The first scheduled sweep is the next 05:43 (30 days) or Sunday 03:17 (365 days). To start one now:

```bash
docker compose exec gwt python -m gwt sweep --days 30
```

That still works while the scheduler is running: a file lock next to the database stops two sweeps from overlapping, and the second one exits with code 3.

**Unraid:** this works as-is on Unraid. Map `/app/data` to appdata (e.g. `/mnt/user/appdata/great-wolf-tracker:/app/data`), make that folder writable by uid 1000, and map container port 8080. The compose file has the appdata path as a comment.

**No authentication.** The dashboard is meant for the LAN only. Don't port-forward it or put it behind a public reverse proxy without adding auth first.

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `GWT_SCHEDULE_ENABLED` | `true` | `false` = web only; sweeps only via `exec` / CLI |
| `GWT_TZ` | `America/Toronto` | Timezone for the schedule (DST-aware) |
| `GWT_FULL_SWEEP_CRON` | `17 3 * * 0` | Full sweep, standard 5-field crontab (0 or 7 = Sunday); `off` disables |
| `GWT_FULL_SWEEP_DAYS` | `365` | Days covered by the full sweep |
| `GWT_DAILY_SWEEP_CRON` | `43 5 * * *` | Short sweep; `off` disables |
| `GWT_DAILY_SWEEP_DAYS` | `30` | Days covered by the short sweep |
| `GWT_PORT` | `8080` | Web port inside the container (change the compose port mapping to match) |
| `GWT_DB` | `/app/data/gwl.db` | SQLite path |
| `GWT_USER_AGENT` | `great-wolf-tracker/<version> (...)` | User-Agent for API requests |

Notes:
- Both scheduled jobs use the default run ID (today's UTC date). On Sundays the daily sweep skips dates the weekly sweep has already captured.
- Jobs that are missed while the container is down still run if it comes back within an hour. Otherwise they wait for the next slot.
- The image has a `HEALTHCHECK` against `/healthz`.
- Check the **Runs** page after the first few sweeps. **A run marked BLOCKED (403) means stop and reassess** (see Ground rules). Set `GWT_SCHEDULE_ENABLED=false` rather than letting it retry every day.
- The database is a single file (`data/gwl.db`, plus `-wal`/`-shm` files while running); include the data folder in backups.

## Web dashboard

| Page | What it shows |
|---|---|
| `/` Overview | Calendar heatmap (week × weekday) of the next 365 arrival dates coloured by demand index. Hover shows rooms left, suite types sold out and min rate; click a day to open it. Also lists the quietest and busiest upcoming dates, and an ISO week × year heatmap that fills in as history builds |
| `/date/YYYY-MM-DD` | Room-type table (suite, category, max occupancy, accessible, rooms left, cheapest rate per rate family, sold-out badge) plus a booking-curve chart of total rooms left and min rate at each scrape |
| `/suites` | Every suite type with first/last seen. Flags suites missing from the latest run, and new ones |
| `/suites/{code}` | One suite's rooms left and rate across all upcoming dates, plus its booking curve for a chosen arrival date (`?date=`) |
| `/runs` | Each sweep: run ID, trigger, start/end, dates fetched/skipped/failed, failed requests, status (completed / aborted / BLOCKED (403) / interrupted), and the next scheduled runs |
| `/trends` | Average demand per ISO week of the year, one line per year: the seasonality curve |

Every page shows a "No data yet" message until the first sweep has stored something.

JSON API (for Home Assistant etc.):

| Route | Returns |
|---|---|
| `/api/overview` | Upcoming dates with demand index and components, plus the quietest / busiest lists |
| `/api/dates/{YYYY-MM-DD}` | Latest snapshot, per-suite rows with rate families, booking curve (404 if never scraped) |
| `/api/suites`, `/api/suites/{code}?date=` | Suite list; one suite's upcoming availability and booking curve |
| `/api/trends` | ISO week × year average demand matrix |
| `/api/runs` | Run log plus scheduler status and next run times |
| `/healthz` | `{"status": "ok", ...}` with db / last completed run / scheduler state |

For example, a Home Assistant `rest` sensor on `/api/overview` with a `value_template` that picks the first entry of `quietest` gives the quietest upcoming date.

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
- [ ] Docker image + built-in schedule on home server (image, compose file and scheduler done; deploy pending)
- [x] Built-in web dashboard (calendar + ISO-week heatmaps, date detail, room types, runs, trends) and JSON API
- [ ] Day pass check
- [ ] Google Trends + school calendar overlays on the dashboard
- [ ] Tune demand index after ~3 months of data
- [ ] Auth, if the dashboard ever leaves the LAN

## Stack

Python 3.12 · httpx · SQLite · FastAPI + Jinja2 + uvicorn · APScheduler · Plotly.js (pinned CDN) · Docker
