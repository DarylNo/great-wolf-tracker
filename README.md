# Great Wolf Tracker

Track real demand at **Great Wolf Lodge Niagara** over time, so we can pick the quietest weeks with data instead of guesses.

Great Wolf doesn't publish attendance. But its booking API reports how many rooms are still available for each suite type, and it prices dynamically, so remaining inventory and the nightly rate work as proxies for occupancy. This project polls those public signals on a schedule, stores every snapshot, and serves a dashboard that answers "which weeks and nights are busy or quiet, and when should we go".

## Signals collected

For every arrival date over the next 365 days (1-night stay, 2 adults), from the availability API (see [`docs/endpoints.md`](docs/endpoints.md)):

| Signal | Source field | Why it matters |
|---|---|---|
| **Total rooms available** | sum of `suites[].availableRoomCount` | Direct inventory: fewer rooms left = fuller lodge (read it together with lead time, see Metrics) |
| **Lowest available nightly rate** | min `totalNightlyRate` over suites that still have rooms | Great Wolf's own dynamic price, which rises with expected demand |
| Suite types sold out / total | `availableRoomCount == 0` across the 27 suite types | Shows how widely the lodge has sold out, not just the cheap suites |
| Rooms available per suite type | `suites[].availableRoomCount`, `suiteCategory` | Which rooms (standard/themed/premium/cottage) fill first |
| Rate for one fixed reference suite (Family Suite, `SFSSB`) | min `totalNightlyRate` for `SFSSB` | Apples-to-apples price over time |

Example from the live API (2026-10-28): Wed Nov 18 had 198 rooms left and 7/27 types sold out, from $379.99. Sat Nov 21 had 4 rooms left and 25/27 sold out, from $909.99.

Optional enrichment (later):
- Day pass availability (generally only sold on lower-occupancy dates)
- Google Trends weekly interest for "Great Wolf Lodge Niagara" (multi-year seasonal curve)

## How it works

1. **Collector** (`gwt/collector.py`): calls the availability JSON API the booking site itself uses (`prod.api.greatwolf.com/room-booking/v2/lodges/NIAGON/availability`) for one arrival date, then parses the response into a run-level row plus one row per suite type. The raw response is kept with every `pricingKey` (signed booking token) stripped.
2. **Storage** (`gwt/db.py`): SQLite (`data/gwl.db`). Each run appends rows; nothing is overwritten, so we can see how a date's inventory and price move as it approaches (booking-curve data).
3. **Sweep** (`python -m gwt sweep --days 365`): single-threaded with a jittered 3–5 s delay between requests. It backs off exponentially on 429/5xx, aborts after 5 consecutive failures, and stops immediately on 403. It is resumable: re-running with the same run ID skips dates already captured.
   Every invocation is logged to a `runs` table (counts, failures, aborted / blocked).
4. **Scheduler** (`gwt/scheduler.py`): APScheduler runs inside the container next to the web server. By default it does a full 365-day sweep on Sundays at 03:17 and a 30-day sweep daily at 05:43 (America/Toronto). A file lock means two sweeps never overlap, including one started by hand with `docker compose exec`.
5. **Metrics** (`gwt/metrics.py`): every number on the dashboard is defined there, with real units (see below).
6. **School closures** (`gwt/events.py`, `gwt/calendar_events.yaml`): marks the nights families can stay without missing school.
7. **Web dashboard** (`gwt/web/`): FastAPI with server-rendered Jinja templates. Charts use Plotly.js served locally from the `plotly` pip package, so it works offline on the LAN and needs no frontend build. It reads the database read-only and also serves a JSON API under `/api/...`. See [Web dashboard](#web-dashboard).

## Metrics

There is deliberately no composite "demand index". An earlier weighted index misranked nights (a Friday with 30 more rooms left at the same price scored busier than the Thursday before it), so the dashboard shows each measure in its own units instead.

| Metric | Definition |
|---|---|
| `lead_days` | Arrival date minus the scrape date, where the scrape date is `scraped_at` converted to **America/Toronto** (not UTC) |
| `capacity_est` | The largest `total_rooms_available` ever seen: a proxy for sellable inventory. Until ~60 arrival dates have been seen at `lead_days ≥ 90`, the dashboard adds the caveat "capacity estimate improves after the first full sweep" |
| `pct_sold_est` | `1 − rooms_left / capacity_est` |
| Lead buckets | 0–1, 2–3, 4–7, 8–14, 15–30, 31–60, 61–90, 91–180, 181–365 days |
| `pace_pct` | Percentile rank (0–100) of a snapshot's `pct_sold_est` among **all snapshots in the same lead bucket**, labelled "vs other dates at the same days-out". n/a while that bucket has fewer than 20 snapshots |
| `final_fill` | For one arrival date, the snapshot with the smallest `lead_days`, valid only if that lead is ≤ 2 days (`GWT_FINAL_FILL_MAX_LEAD`): "how full it actually got" |
| `pickup_7d` | `rooms_left` at the scrape about 7 days earlier minus `rooms_left` now (rooms sold in the last week). n/a without a scrape 6–8 days earlier; the closest to 7 days counts |
| Price | `min_rate`, the lowest available nightly rate. The reference suite rate (Family Suite, `SFSSB`) is shown as secondary information |

**Read rooms left together with lead time.** Far-out dates haven't been booked yet, so a high rooms-left count a year out mostly reflects how far away the date is. That's why the calendar defaults to price, which reflects Great Wolf's own demand forecast (its booking site reports to Duetto, a hotel dynamic-pricing system), and why pace compares a date only with other dates at the same days-out.

**Price footnote:** rates are queried for 1 night, 2 adults, with no offer code, so promotions and member discounts aren't reflected.

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

suites(                             -- lookup: one row per suite code ever seen, upserted every save
  suite_code             TEXT PRIMARY KEY,
  suite_brand            TEXT,
  suite_category         TEXT,
  max_occupancy          INTEGER,
  is_accessible          INTEGER,
  first_seen             TEXT,      -- scraped_at of first sighting
  last_seen              TEXT,      -- scraped_at of latest sighting
  max_rooms_seen         INTEGER,   -- most rooms ever available (per-room inventory proxy)
  min_rate_seen          REAL       -- lowest rate ever seen (each room's usual price level)
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

meta(key TEXT PRIMARY KEY, value TEXT)  -- facts about the database (synthetic_demo = 1 for gwt demo-db)
```

**Rooms vs suite codes.** The API's ~27 suite codes include accessible and bedding variants of the same room (e.g. four "Timber Wolf Cottage" codes), so the dashboard groups codes by `suite_brand` into ~17 rooms (`gwt/rooms.py`). A room's rooms left is the sum over its variants, and it's sold out only when every variant is at 0.

**Accessibility, beds and baths** come straight from each suite in the API response: `isAccessible` (boolean, stored as 1/0), and `numOfBeds` / `numOfBaths` (sent as strings like `"2"`, stored as integers). A missing or unparseable value is stored as NULL. See [`docs/endpoints.md`](docs/endpoints.md).

Older databases get new columns (and the `suites` max/min backfill) added automatically on connect.

## School closures

A night is marked when the **next morning is a school-closure day** (a break, PA day or holiday) for Ontario or New York **and isn't a regular weekend**: the extra nights when families can stay without missing school. They show as a dot on calendar cells (with the names in the tooltip), as tags on the night's page, and as shaded bands on the Seasonality charts.

Closure days come from two places:

- [`gwt/calendar_events.yaml`](gwt/calendar_events.yaml): school-board dates, each with `region`, `name`, `start`, `end` (inclusive), `source` and `official`. Seeded with the TDSB 2026-27 key dates (official) and a Buffalo Public Schools 2026-27 compilation (unofficial, third-party), with sources cited in the file. Entries with `closure: false` (e.g. Buffalo's last student day) are shown as tags but never mark a night.
- Public holidays for Ontario (CA-ON) and New York (US-NY) from the [`holidays`](https://pypi.org/project/holidays/) package, computed for whichever years are on screen. The package includes some New York commemorations (e.g. Lincoln's Birthday) that may not close schools; the YAML has a commented-out `exclude_holidays` block to drop them.

**Adding next year's dates:** append entries for the new school year to the YAML when the boards publish their calendars, with the source URL (`official: false` for third-party compilations). Keep the quotes around `"ON"` (unquoted `ON` means "true" in YAML). Don't guess dates: the file has TODOs for the Ontario summer break and the 2027-28 year. Either edit the file and rebuild the image, or copy it next to the database (e.g. `/app/data/calendar_events.yaml`) and point `GWT_EVENTS_FILE` at it, so it can be edited without a rebuild.

## Running locally

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest                                  # offline tests (fixtures + a small synthetic demo database)
python -m gwt sweep --days 7 -v         # small live run
python -m gwt sweep --days 365          # full sweep (~25-30 min at 3-5 s/request)

# Query what's stored
python -m gwt suites --date 2026-11-21               # latest snapshot: every suite code for that night
python -m gwt suites --date 2026-11-21 --code SFSSB  # booking curve for one suite across runs

# Web dashboard on http://localhost:8080 (add --no-scheduler to skip the built-in schedule)
python -m gwt serve --no-scheduler

# Synthetic demo data to explore the dashboard without live data
python -m gwt demo-db --out /tmp/gwt-demo.db
python -m gwt serve --no-scheduler --db /tmp/gwt-demo.db
```

`suites --date` prints suite name, code, category, rooms left, cheapest rate and a SOLD OUT flag, using the most recent scrape of that date. Adding `--code` prints one row per scrape run with days before arrival (`lead_days`), rooms left, cheapest rate, the cheapest rate in each rate family (Saver, Flexible, ...) and a sold-out flag. Both take `--db PATH`.

Sweep options: `--start YYYY-MM-DD` (first arrival date, default today UTC), `--db PATH` (default `$GWT_DB` or `data/gwl.db`), `--run-id ID` (re-use one to resume an interrupted sweep; default is today's UTC date, so re-running the same day resumes automatically). Set `GWT_USER_AGENT` to override the User-Agent.

Exit codes: `0` finished, `1` aborted (5 consecutive failures or a 403), `2` bad arguments, `3` another sweep is already running.

### Demo database

`python -m gwt demo-db --out PATH [--weeks 60] [--seed 7] [--force]` writes clearly synthetic but realistic history: ~60 weekly full sweeps plus daily 30-day sweeps (scrape times in UTC like the collector), 27 suite codes grouped into 17 rooms, rooms declining towards arrival, Fri/Sat nights, summer and school-break / holiday nights filling earlier, prices rising with expected demand, and the cheapest suites selling out first. The full default takes ~25 s and ~290 MB. The database is marked `synthetic_demo` and the dashboard shows a banner for it.

It **never writes to the real database**: it refuses `$GWT_DB`, `data/gwl.db` and `/app/data/gwl.db`, and won't replace an existing file unless it's an earlier demo database and `--force` is given.

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
| `GWT_EVENTS_FILE` | packaged `calendar_events.yaml` | School-closure calendar to use instead of the packaged one |
| `GWT_FINAL_FILL_MAX_LEAD` | `2` | A night's final fill counts only if its closest scrape was at most this many days before arrival |

Notes:
- Both scheduled jobs use the default run ID (today's UTC date). On Sundays the daily sweep skips dates the weekly sweep has already captured.
- Jobs that are missed while the container is down still run if it comes back within an hour. Otherwise they wait for the next slot.
- The image has a `HEALTHCHECK` against `/healthz`.
- Check the **Runs** page after the first few sweeps. **A run marked BLOCKED (403) means stop and reassess** (see Ground rules). Set `GWT_SCHEDULE_ENABLED=false` rather than letting it retry every day.
- The database is a single file (`data/gwl.db`, plus `-wal`/`-shm` files while running); include the data folder in backups.

## Web dashboard

Charts and calendars use the full window width (24 px padding, 16 px on a phone); prose stays at ~75 characters per line.

| Page | What it shows |
|---|---|
| **Plan** `/` | A 12-month wall calendar (Sun–Sat, 6 months per row on a wide screen down to 1 on a phone). Each night is coloured by **Price** (default), **Rooms left** or **Pace**, with a legend in real values (quantile breaks over the 12 months shown, colourblind-safe blue ramp), distinct sold-out and no-data styles, today outlined, and a dot on no-school-next-day nights. The tooltip shows rooms left, % sold, lowest and reference rate, room types sold out, lead days, pace and school closures. Below: a price vs rooms-left (or pace) scatter with day-window and weekday filters and the cheap-and-quiet quadrant shaded, then **Cheapest upcoming nights** and **Filling fastest** lists, each with its rule stated |
| **Seasonality** `/seasonality` (`/trends` redirects) | "How full each week ends up": average final-fill % sold by week of the year, one line per year, Fri–Sat and Sun–Thu nights in two panels, school breaks shaded (fills in one night per day). "Forecast from current prices": average lowest rate per week for the next 52 weeks, Sun–Thu vs Fri–Sat, breaks shaded and named |
| **Night** `/date/YYYY-MM-DD` | Tiles for rooms left (% sold), lowest rate, reference rate, room types fully sold (of ~17), days until arrival, pace, sold in the last 7 days, and school-closure tags. Then the rooms table (codes grouped into rooms, expandable into variants with accessibility, beds, baths and rates) and booking curves for rooms left and lowest rate: this night against the median and 10th–90th percentile of every other night at the same days-out |
| **Room types** `/suites` | A room × date matrix (30/60/90/365-day window, default 90) coloured by rooms left as a share of the most that room has had, sold-out cells dark with ×, weekend columns marked: which rooms sell out first. Then every room with its codes, first/last seen and lowest rate seen |
| Room `/rooms/{slug}` | One grouped room: rooms left and cheapest rate across upcoming nights, and its booking curve for a chosen night (`?date=`) |
| Suite code `/suites/{code}` | The same for a single suite code |
| **Runs** `/runs` | Each sweep: run ID, trigger, start/end, dates fetched/skipped/failed, failed requests, status (completed / aborted / BLOCKED (403) / interrupted), and the next scheduled runs |

Every chart has a "Show as a table" view, and every page shows a "No data yet" message until the first sweep has stored something.

JSON API (for Home Assistant etc.):

| Route | Returns |
|---|---|
| `/api/plan` | Every upcoming night with the metrics above (nulls where there's no data yet), plus the cheapest-nights and filling-fastest lists with their rules |
| `/api/seasonality` | Final fill by week of year per year (both night types), the 52-week price forecast, and closure spans |
| `/api/dates/{YYYY-MM-DD}` | One night: tiles, rooms with variants, booking curves and comparison bands (404 if never scraped) |
| `/api/rooms`, `/api/rooms/{slug}?date=` | Grouped rooms; one room's upcoming availability and booking curve |
| `/api/rooms/matrix?window=` | Rooms left and cheapest rate per room for the next 30/60/90/365 nights |
| `/api/suites`, `/api/suites/{code}?date=` | Suite codes; one code's upcoming availability and booking curve |
| `/api/runs` | Run log plus scheduler status and next run times |
| `/healthz` | `{"status": "ok", ...}` with db / demo flag / last completed run / scheduler state |

For example, a Home Assistant `rest` sensor on `/api/plan` with a `value_template` that picks the first entry of `cheapest_nights.nights` gives the cheapest upcoming night.

## Ground rules

- Low and polite: one request every few seconds, single-threaded, identifiable User-Agent, back off on errors / 429.
- Public pages only; no logins, no bookings, no promo-code probing.
- Never store `pricingKey` booking tokens.
- Review greatwolf.com terms of use before the first scheduled run.
- If automated requests get blocked, stop and reassess rather than evade.

## Roadmap

- [x] Endpoint discovery (network inspection of the booking flow): see [`docs/endpoints.md`](docs/endpoints.md)
- [x] Collector for rate + availability (live CLI sweep verified on the home server)
- [ ] Full 365-day sweep + SQLite storage (implemented; awaiting the first scheduled full sweep)
- [x] Docker image + built-in schedule, running on the home server (Unraid)
- [x] Built-in web dashboard and JSON API: Plan calendar, Seasonality, night detail, room types, runs
- [x] School break / PA day / public holiday overlay (Ontario + New York)
- [ ] Add Ontario summer break and the 2027-28 school calendars when published
- [ ] Day pass check
- [ ] Google Trends overlay
- [ ] Revisit the capacity estimate and pace buckets after ~3 months of data
- [ ] Auth, if the dashboard ever leaves the LAN

## Stack

Python 3.12 · httpx · SQLite · FastAPI + Jinja2 + uvicorn · APScheduler · Plotly.js (bundled with the `plotly` package, served locally) · holidays · PyYAML · Docker
