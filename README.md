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
3. **Sweep** (`python -m gwt sweep --days 365`): single-threaded with a jittered 3–5 s delay between requests, starting from today's date in Toronto. It backs off exponentially on 429/5xx and honours `Retry-After` up to 10 minutes (a longer one ends the run as **rate-limited** rather than holding the sweep open), aborts after 5 consecutive failures, and stops immediately on 403, which also **pauses collection** until you run `gwt unblock` (see [Blocked runs](#blocked-runs-403)). It is resumable: re-running with the same run ID skips dates already captured.
   Every invocation is logged to a `runs` table (counts, failures, aborted / blocked / rate-limited / interrupted).
4. **Scheduler** (`gwt/scheduler.py`): APScheduler runs inside the container next to the web server. By default it does a full 365-day sweep on Sundays at 03:17, a 38-day sweep daily at 05:43, and a short 14-day **intraday** sweep daily at 15:13 that re-scrapes the nearest nights to catch same-day price moves (America/Toronto). A file lock means two sweeps never overlap, including one started by hand with `docker compose exec`. When the container stops, a running sweep stops between dates, records itself as interrupted, and the container exits cleanly.
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
| `pace_pct` | Percentile rank (0–100) of a snapshot's `pct_sold_est` among **all daily snapshots in the same lead bucket**, labelled "vs other dates at the same days-out". n/a while that bucket has fewer than 20 snapshots |
| `final_fill` | For one arrival date, the snapshot with the smallest `lead_days`, valid only if that lead is ≤ 2 days (`GWT_FINAL_FILL_MAX_LEAD`): "how full it actually got" |
| `pickup_7d` | `rooms_left` at the scrape about 7 days earlier minus `rooms_left` now (rooms sold in the last week). n/a without a scrape 6–8 days earlier; the closest to 7 days counts |
| Price | `min_rate`, the lowest available nightly rate. The reference suite rate (Family Suite, `SFSSB`) is shown as secondary information. With a room tier picked on the Plan page, the cheapest rate among rooms of that category or above |
| Price move | A night's lowest rate at its latest scan minus the scan before, when the two are at most 24 h apart |

**One snapshot per night per day.** The intraday sweep scrapes the nearest nights twice a day. Pace, the comparison bands and the booking curves use only each night's **latest scrape of each Toronto day**, so those nights aren't counted twice at the same days-out. Every scrape is still stored, the "latest" figures everywhere are the most recent scrape, and price moves compare the last two scrapes whatever day they fall on.

**Read rooms left together with lead time.** Far-out dates haven't been booked yet, so a high rooms-left count a year out mostly reflects how far away the date is. That's why the calendar defaults to price, which reflects Great Wolf's own demand forecast (its booking site reports to Duetto, a hotel dynamic-pricing system), and why pace compares a date only with other dates at the same days-out.

**Price footnote:** rates are queried for 1 night, 2 adults, with no offer code, so promotions and member discounts aren't reflected.

## Data model

```
snapshots(                          -- one row per arrival date per run
  run_id                 TEXT,      -- groups one sweep (default: today's date in Toronto); used for resume
  scraped_at             TEXT,      -- UTC timestamp
  arrival_date           TEXT,      -- YYYY-MM-DD
  total_rooms_available  INTEGER,   -- sum of availableRoomCount
  suite_types            INTEGER,   -- suite types returned (27 at Niagara)
  suite_types_sold_out   INTEGER,   -- availableRoomCount == 0
  min_rate               REAL,      -- lowest totalNightlyRate among suites with rooms
  ref_suite_rate         REAL,      -- lowest totalNightlyRate for SFSSB (NULL if sold out)
  error_code             TEXT,      -- response errorCode
  raw_json               TEXT       -- response with pricingKey stripped, for re-parsing; stored
                                    -- zlib-compressed (BLOB), rows from older builds are plain TEXT
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
  trigger                TEXT,      -- cli / schedule:weekly / schedule:daily / schedule:intraday
  started_at, finished_at TEXT,
  start_date             TEXT,      -- first arrival date requested
  days                   INTEGER,
  fetched, skipped, failed_dates INTEGER,
  failures               INTEGER,   -- failed requests, including retried ones
  status                 TEXT,      -- running / completed / aborted / rate_limited / error / interrupted
  blocked                INTEGER,   -- 1 if the run stopped on HTTP 403
  message                TEXT       -- abort / error reason
)

meta(key TEXT PRIMARY KEY, value TEXT)  -- facts about the database: synthetic_demo = 1 for gwt demo-db,
                                        -- unblocked_run_id = the last 403 that `gwt unblock` cleared
```

**Rooms vs suite codes.** The API's ~27 suite codes include accessible and bedding variants of the same room (e.g. four "Timber Wolf Cottage" codes), so the dashboard groups codes by `suite_brand` into ~17 rooms (`gwt/rooms.py`). A room's rooms left is the sum over its variants, and it's sold out only when every variant is at 0.

**Accessibility, beds and baths** come straight from each suite in the API response: `isAccessible` (boolean, stored as 1/0), and `numOfBeds` / `numOfBaths` (sent as strings like `"2"`, stored as integers). A missing or unparseable value is stored as NULL. See [`docs/endpoints.md`](docs/endpoints.md).

**Upgrades migrate the database automatically**, in one transaction (all of it applies or none of it does): new tables, columns and indexes, then backfills (the `suites` max/min columns) that only touch rows still empty, so an interrupted upgrade is simply finished on the next start. The web server migrates before it serves its first request, waiting up to 10 s for a running sweep's lock; every CLI command migrates on connect too. Back up first anyway: see [Upgrading](#upgrading).

**Size.** With the default schedule the database grows by roughly 410 MB a year (about 36,000 arrival-date snapshots, ~5,000 of them from the intraday sweep; each with ~27 suite rows, their rate families and indexes). `raw_json` is stored zlib-compressed, about 2 KB instead of 17 KB per snapshot, which saves ~450 MB a year; rows written by older builds stay as plain text and are read the same way.

## School closures

A night is marked when the **next morning is a school-closure day** (a break, PA day or holiday) for Ontario or New York **and isn't a regular weekend**: the extra nights when families can stay without missing school. They show as a dot on calendar cells (with the names in the tooltip), as tags on the night's page, and as shaded bands on the Seasonality charts.

Closure days come from two places:

- [`gwt/calendar_events.yaml`](gwt/calendar_events.yaml): school-board dates, each with `region`, `name`, `start`, `end` (inclusive), `source` and `official`. Seeded with the TDSB 2026-27 key dates (official) and the Buffalo Public Schools 2026-27 no-school days from a third-party compilation (`official: false`), every one listed explicitly, holidays included, with sources cited in the file. Entries with `closure: false` (e.g. Buffalo's last student day) are shown as tags but never mark a night.
- Public holidays for Ontario (CA-ON) and New York (US-NY) from the [`holidays`](https://pypi.org/project/holidays/) package, computed for whichever years are on screen. The YAML's `exclude_holidays` drops New York's Lincoln's Birthday and Susan B. Anthony Day, which are school days in Buffalo (Election Day and Veterans Day do close schools and stay). On a day a school-board entry covers, the board's name is the only label shown, so "Presidents' Day" isn't repeated as "Washington's Birthday".

For 2026-27 that marks these New York nights from November to June: Nov 2, Nov 10, Nov 24–26, Dec 23–24, Dec 27–31, Jan 17, Feb 14–18, Mar 25, Mar 29–Apr 1, May 30 and Jun 17 (`tests/test_events.py` checks the exact list).

**Adding next year's dates:** append entries for the new school year to the YAML when the boards publish their calendars, with the source URL (`official: false` for third-party compilations). Keep the quotes around `"ON"` (unquoted `ON` means "true" in YAML). Don't guess dates: the file has TODOs for the Ontario summer break and the 2027-28 year. Either edit the file and rebuild the image, or copy it next to the database (e.g. `/app/data/calendar_events.yaml`) and point `GWT_EVENTS_FILE` at it, so it can be edited without a rebuild. If that file can't be read or parsed, the dashboard keeps working on the bundled calendar, logs the error, and shows a banner naming the file and the problem until it's fixed.

## Running locally

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -c requirements.lock -e '.[dev]'   # the exact versions the Docker image uses
pytest                                  # offline tests (fixtures + a small synthetic demo database)
python -m pyflakes gwt tests scripts
python -m gwt sweep --days 7 -v         # small live run
python -m gwt sweep --days 365          # full sweep (~25-30 min at 3-5 s/request)
python -m gwt backup --out backups/gwl.db  # consistent copy, safe while the database is in use
python -m gwt unblock                   # resume after reviewing a blocked (403) run

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

Sweep options: `--start YYYY-MM-DD` (first arrival date, default today in Toronto), `--db PATH` (default `$GWT_DB` or `data/gwl.db`), `--run-id ID` (re-use one to resume an interrupted sweep; default is today's date in Toronto, so re-running the same day resumes automatically, including in the evening after the UTC date has already changed), `--force` (sweep even while collection is paused after a 403; prefer `gwt unblock` once you've reviewed it). Set `GWT_USER_AGENT` to override the User-Agent.

Exit codes: `0` finished, `1` aborted (5 consecutive failures, a 403, or rate-limited), `2` bad arguments, `3` another sweep is already running, `4` collection is paused after a 403 (see [Blocked runs](#blocked-runs-403)), `130` interrupted with Ctrl-C (recorded as interrupted; re-run the same day to resume).

`backup --out PATH [--db PATH] [--force]` copies the database with SQLite's backup API, so it's consistent even while a sweep is writing, then checks the copy (`PRAGMA quick_check`) and prints its row counts. It never overwrites a file unless given `--force`, and never migrates: a backup taken before an upgrade is an exact copy of the old database.

### Demo database

`python -m gwt demo-db --out PATH [--weeks 60] [--seed 7] [--force]` writes clearly synthetic but realistic history: ~60 weekly full sweeps plus daily 38-day sweeps (scrape times in UTC like the collector), 27 suite codes grouped into 17 rooms, rooms declining towards arrival, Fri/Sat nights, summer and school-break / holiday nights filling earlier, prices rising with expected demand, and the cheapest suites selling out first. The full default takes ~25 s and ~300 MB. The database is marked `synthetic_demo` and the dashboard shows a banner for it.

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

Open `http://<server>:8080` (or whichever host port you mapped). The first scheduled sweep is the next 05:43 (38 days), 15:13 (14 days) or Sunday 03:17 (365 days). To start one now:

```bash
docker compose exec gwt python -m gwt sweep --days 38
```

That still works while the scheduler is running: a file lock next to the database stops two sweeps from overlapping, and the second one exits with code 3.

**Stopping.** `docker compose stop` (or a rebuild) lets a running sweep stop at the next date, or wake from its wait between requests, and record itself as `interrupted`; the compose file gives it 45 s (`stop_grace_period`) before Docker would kill it. Re-running the same day resumes it. If you run the container from an **Unraid Docker UI template** instead of compose, add `--stop-timeout 45` under **Extra Parameters** (Docker's default is 10 s).

**Unraid:** keep the data folder on a pool path, not the user-share FUSE path: map `/mnt/cache/appdata/great-wolf-tracker:/app/data` (use your pool's name if it isn't `cache`), or set the `appdata` share to **Exclusive access** (Unraid 6.12+, Shares → appdata → Primary storage only), which makes `/mnt/user/appdata` a direct path to the pool. Through the plain `/mnt/user/...` FUSE path (shfs), SQLite's WAL shared memory and the sweep's file lock aren't reliable, which risks a corrupt database or two sweeps at once. If you're already on `/mnt/user/appdata/great-wolf-tracker` and the share lives on the cache pool, the files are already at `/mnt/cache/appdata/great-wolf-tracker`: stop the container, change the volume path, start it again. Make the folder writable by uid 1000 and map container port 8080.

**No authentication.** The dashboard is meant for the LAN only. Don't port-forward it or put it behind a public reverse proxy without adding auth first.

### Upgrading

Take a backup, then rebuild. The new version migrates the database on startup (see [Data model](#data-model)); the backup is your way back if anything looks wrong.

1. **Back up** (safe while the container runs):
   ```bash
   docker compose exec gwt python -m gwt backup --out /app/data/gwl.pre-redesign.db
   ```
2. **Pull and rebuild:**
   ```bash
   git pull && docker compose up -d --build
   ```
3. **Check `/healthz`** on the host port you mapped (the left side of `ports:` in the compose file or the template's port; it may not be 8080, e.g. `8090:8080` means 8090):
   ```bash
   curl -s http://localhost:<port>/healthz      # e.g. curl -s http://localhost:8090/healthz
   ```
   Expect `"status": "ok"`, `"db_exists": true` and `"has_data": true`; `docker compose logs gwt` shows `database /app/data/gwl.db: migrated`. `"collection_paused"` should be `null`, **unless the database's last run before the upgrade was a 403**: then the dashboard shows "Collection paused" straight after the upgrade. That's expected (older builds didn't pause, the new one honours the recorded block); review it and run `gwt unblock` (see [Blocked runs](#blocked-runs-403)).
4. **Run a small sweep** and check it on the Runs page:
   ```bash
   docker compose exec gwt python -m gwt sweep --days 3
   ```

**Upgrading from a build without `gwt backup`** (e.g. the one built from commit 84181f2): step 1 doesn't exist there, so copy the files with the container stopped instead.

1. Stop the container so nothing writes to the database:
   ```bash
   docker compose stop
   ```
2. In the data folder, copy `gwl.db` together with any `gwl.db-wal` / `gwl.db-shm` next to it, keeping the set's names matched (SQLite only reads a `-wal` file named after its database):
   ```bash
   cd /mnt/cache/appdata/great-wolf-tracker      # your data folder
   for s in "" -wal -shm; do [ -e "gwl.db$s" ] && cp -p "gwl.db$s" "gwl.pre-redesign.db$s"; done; ls -l gwl.*
   ```
   Confirm the copy opens, with `sqlite3` if the host has it:
   ```bash
   sqlite3 gwl.pre-redesign.db "PRAGMA quick_check; SELECT COUNT(*) FROM snapshots;"
   ```
   or with Docker (works on Unraid, which has no `sqlite3`):
   ```bash
   docker run --rm -v "$PWD":/data python:3.12-slim python -c "import sqlite3; c = sqlite3.connect('/data/gwl.pre-redesign.db'); print(c.execute('PRAGMA quick_check').fetchone()[0], c.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 'snapshots')"
   ```
   It should print `ok` and the number of snapshots.
3. Then pull, rebuild and start, and carry on with steps 3 and 4 above:
   ```bash
   git pull && docker compose up -d --build
   ```

To roll back, stop the container, move the `gwl.db*` files aside, copy `gwl.pre-redesign.db` back as `gwl.db` (with its `-wal`/`-shm` if any, renamed to match), and check out the previous commit before rebuilding.

### Blocked runs (403)

An HTTP 403 means Great Wolf may be blocking automated requests, so the sweep stops at once (Ground rules: stop and reassess, don't evade) and the run is recorded as **BLOCKED (403)**. From then on **collection is paused**: scheduled sweeps skip with a `COLLECTION PAUSED` error in the logs, a manual `gwt sweep` refuses with exit code 4, and the Plan and Runs pages show "Collection paused: blocked (403) on DATE, run `gwt unblock` after reviewing." No setting needs changing.

Once you've looked into it (the run's message, the logs, the site's terms), resume with:

```bash
docker compose exec gwt python -m gwt unblock
```

`gwt sweep --force` sweeps once despite the pause. The pause lifts only when you run `gwt unblock` or when a run started after the block **completes**; a forced run that aborts, errors or is interrupted leaves collection paused. A new 403 pauses again.

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `GWT_SCHEDULE_ENABLED` | `true` | `false` = web only; sweeps only via `exec` / CLI |
| `GWT_TZ` | `America/Toronto` | Timezone for the schedule (DST-aware) |
| `GWT_FULL_SWEEP_CRON` | `17 3 * * 0` | Full sweep, standard 5-field crontab: day-of-week takes 0–7 (0 and 7 = Sunday), names (`mon`), ranges incl. wrap-around (`5-1` = Fri–Mon), lists and steps (`*/2` = Sun, Tue, Thu, Sat); anything else stops startup with an error. `off` disables |
| `GWT_FULL_SWEEP_DAYS` | `365` | Days covered by the full sweep |
| `GWT_DAILY_SWEEP_CRON` | `43 5 * * *` | Short sweep; `off` disables |
| `GWT_DAILY_SWEEP_DAYS` | `38` | Days covered by the short sweep. 38 = 30 + 8: every night in the next 30 days then has a scrape 6–8 days earlier on any day of the week, which "sold in the last 7 days" needs |
| `GWT_INTRADAY_SWEEP_CRON` | `13 15 * * *` | Afternoon re-scrape of the nearest nights, for same-day price moves; `off` disables |
| `GWT_INTRADAY_SWEEP_DAYS` | `14` | Days covered by the intraday sweep (~1 minute of requests at 14) |
| `GWT_PORT` | `8080` | Web port inside the container (change the compose port mapping to match) |
| `GWT_DB` | `/app/data/gwl.db` | SQLite path |
| `GWT_USER_AGENT` | `great-wolf-tracker/<version> (...)` | User-Agent for API requests |
| `GWT_EVENTS_FILE` | packaged `calendar_events.yaml` | School-closure calendar to use instead of the packaged one |
| `GWT_FINAL_FILL_MAX_LEAD` | `2` | A night's final fill counts only if its closest scrape was at most this many days before arrival |

Notes:
- The weekly and daily jobs share the default run ID (today's date in Toronto), so on Sundays the daily sweep skips dates the weekly sweep has already captured. The intraday job has a run ID of its own (`YYYY-MM-DD-intraday`): it re-scrapes nights the morning sweeps already captured, and an interrupted one resumes the same afternoon.
- Jobs that are missed while the container is down still run if it comes back within an hour. Otherwise they wait for the next slot.
- The image has a `HEALTHCHECK` against `/healthz`, which also reports `collection_paused` and any `events_problem`.
- Check the **Runs** page after the first few sweeps. **A run marked BLOCKED (403) pauses collection** until you run `gwt unblock` (see [Blocked runs](#blocked-runs-403)); there's no need to touch `GWT_SCHEDULE_ENABLED`. A **rate-limited** run (Retry-After over 10 minutes) simply ends; the next scheduled run carries on.
- The database is a single file (`data/gwl.db`, plus `-wal`/`-shm` files while running). For backups use `gwt backup` (consistent while running) or copy the whole data folder with the container stopped.
- Dependencies: `pyproject.toml` gives version ranges with upper bounds; `requirements.lock` pins the exact versions the Docker image installs (`pip check` in the build fails if they stop matching). After changing a dependency, regenerate it with the command at the top of the file (`uv pip compile ...`; pip-tools' `pip-compile` works too) and commit both.
- `python scripts/smoke_test.py IMAGE` (needs Docker) starts a built image on a copy of `tests/fixtures/legacy-84181f2.db`, a database written by the pre-redesign build, waits for `/healthz`, requests every page and API route expecting 200, checks the migration inside the container, and stops it. CI runs it after `docker build`; it never calls Great Wolf.

## Web dashboard

Charts and calendars use the full window width (24 px padding, 16 px on a phone); prose stays at ~75 characters per line.

| Page | What it shows |
|---|---|
| **Plan** `/` | A wall calendar of the next 365 nights: today's month through the month of the 365th night, so 13 months with the last one partial (Sun–Sat, 6 months per row on a wide screen down to 1 on a phone). Each night is coloured by **Price** (default), **Rooms left** or **Pace**, with a legend in real values (quantile breaks over the nights shown, colourblind-safe blue ramp), distinct sold-out and no-data styles, today outlined, and a dot on no-school-next-day nights. The tooltip shows rooms left, % sold, lowest and reference rate, room types sold out, lead days, pace and school closures. Below: a price vs pace scatter (rooms left on a toggle; the note says how many nights have no pace yet) with day-window and weekday filters and the cheap-and-quiet quadrant shaded, then **Cheapest upcoming nights** and **Filling fastest** (nights that sold rooms in the last 7 days first, most sold first, then the rest by pace), each with its rule stated and a **Book ↗** link per night, and **Price moves since the previous scan** (next 38 nights whose lowest rate changed between their last two scans within 24 h: biggest drops first, then reopened, rises, sold out). **Prices for** (Any room / Themed and up / Premium and up / Cottages) switches every price on the page to the cheapest room of that category or above (`?tier=`); rooms left and pace stay lodge-wide |
| **Seasonality** `/seasonality` (`/trends` redirects) | "How full each week ends up": average final-fill % sold by week of the year, one line per year, Fri–Sat and Sun–Thu nights in two panels, school breaks shaded (fills in one night per day). "Forecast from current prices": average lowest rate per week for the next 52 weeks, Sun–Thu vs Fri–Sat, breaks shaded and named |
| **Night** `/date/YYYY-MM-DD` | A **Book this night on greatwolf.com** link (upcoming nights only; opens the public booking page for 1 night, 2 adults). Tiles for rooms left (% sold), lowest rate (with the previous scan's rate when it changed within 24 h), reference rate, room types fully sold (of ~17), days until arrival, pace, sold in the last 7 days, and school-closure tags. Then the rooms table (codes grouped into rooms, expandable into variants with accessibility, beds, baths and rates) and booking curves for rooms left and lowest rate: this night against the median and 10th–90th percentile of every other night at the same days-out |
| **Room types** `/suites` | A room × date matrix (30/60/90/365-day window, default 90) coloured by rooms left as a share of the most that room has had, sold-out cells dark with ×, weekend columns marked: which rooms sell out first. Then every room with its codes, first/last seen and lowest rate seen |
| Room `/rooms/{slug}` | One grouped room: rooms left and cheapest rate across upcoming nights, and its booking curve for a chosen night (`?date=`; a past night with data is added to the picker, a night without data falls back to the first upcoming one with a note) |
| Suite code `/suites/{code}` | The same for a single suite code |
| **Runs** `/runs` | Each sweep, newest first, 50 per page: run ID, trigger, start/end in Toronto time (UTC in the tooltip), dates fetched/skipped/failed, failed requests, status (completed / aborted / BLOCKED (403) / rate-limited / interrupted), and the next scheduled runs |

Every chart has a "Show as a table" view, and every page shows a "No data yet" message until the first sweep has stored something. Suite codes always appear under their current name in the `suites` table, so a suite Great Wolf renames stays one room with its whole history. Banners appear on top of the pages for synthetic demo data, for paused collection (Plan and Runs), and for an unusable `GWT_EVENTS_FILE` (every page).

JSON API (for Home Assistant etc.):

| Route | Returns |
|---|---|
| `/api/plan?tier=` | Every upcoming night with the metrics above (nulls where there's no data yet; `min_rate` in the chosen tier, `lodge_min_rate` for any room), plus the cheapest-nights, filling-fastest and price-moves lists with their rules |
| `/api/seasonality` | Final fill by week of year per year (both night types), the 52-week price forecast, and closure spans |
| `/api/dates/{YYYY-MM-DD}` | One night: tiles, rooms with variants, booking curves and comparison bands (404 if never scraped) |
| `/api/rooms`, `/api/rooms/{slug}?date=` | Grouped rooms; one room's upcoming availability and booking curve |
| `/api/rooms/matrix?window=` | Rooms left and cheapest rate per room for the next 30/60/90/365 nights |
| `/api/suites`, `/api/suites/{code}?date=` | Suite codes; one code's upcoming availability and booking curve |
| `/api/runs?page=` | Run log (50 per page, with Toronto local times), whether collection is paused, scheduler status and next run times |
| `/healthz` | `{"status": "ok", ...}` with db / demo flag / last completed run / collection paused / events-file problem / scheduler state |

For example, a Home Assistant `rest` sensor on `/api/plan` with a `value_template` that picks the first entry of `cheapest_nights.nights` gives the cheapest upcoming night.

## Ground rules

- Low and polite: one request every few seconds, single-threaded, identifiable User-Agent, back off on errors / 429.
- Public pages only; no logins, no bookings, no promo-code probing.
- Never store `pricingKey` booking tokens.
- Review greatwolf.com terms of use before the first scheduled run.
- If automated requests get blocked, stop and reassess rather than evade. A 403 pauses collection automatically until `gwt unblock`.

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
