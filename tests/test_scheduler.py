"""Schedule config parsing, scheduler construction, and the no-overlap guarantees."""

import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger

from gwt.config import ConfigError, CronSpec, ScheduleConfig, Settings, SweepJob
from gwt.locking import SweepBusy, sweep_lock
from gwt.scheduler import build_scheduler, describe, run_scheduled_sweep
from gwt.sweep import SweepAborted, SweepInterrupted, SweepPaused, SweepResult

TZ = ZoneInfo("America/Toronto")


def test_defaults():
    cfg = ScheduleConfig.from_env({})
    assert cfg.enabled is True
    assert cfg.timezone == "America/Toronto"
    assert [(j.name, j.cron.expression, j.days) for j in cfg.jobs] == [
        ("weekly", "17 3 * * 0", 365),
        ("daily", "43 5 * * *", 38),
        ("intraday", "13 15 * * *", 14),
    ]
    assert [j.own_run for j in cfg.jobs] == [False, False, True]


def test_env_overrides():
    cfg = ScheduleConfig.from_env({
        "GWT_SCHEDULE_ENABLED": "false",
        "GWT_TZ": "UTC",
        "GWT_FULL_SWEEP_CRON": "0 2 * * 6",
        "GWT_FULL_SWEEP_DAYS": "200",
        "GWT_DAILY_SWEEP_CRON": "off",
        "GWT_INTRADAY_SWEEP_CRON": "off",
    })
    assert cfg.enabled is False
    assert cfg.timezone == "UTC"
    assert [(j.name, j.cron.expression, j.days) for j in cfg.jobs] == [("weekly", "0 2 * * 6", 200)]


@pytest.mark.parametrize("value, expected", [("true", True), ("1", True), ("YES", True), ("off", False), ("0", False), ("", True)])
def test_enabled_flag_values(value, expected):
    assert ScheduleConfig.from_env({"GWT_SCHEDULE_ENABLED": value}).enabled is expected


@pytest.mark.parametrize(
    "env",
    [
        {"GWT_SCHEDULE_ENABLED": "maybe"},
        {"GWT_TZ": "Mars/Olympus_Mons"},
        {"GWT_FULL_SWEEP_CRON": "17 3 * *"},
        {"GWT_FULL_SWEEP_DAYS": "0"},
        {"GWT_DAILY_SWEEP_DAYS": "lots"},
        {"GWT_FULL_SWEEP_CRON": "0 3 * * 9"},
    ],
)
def test_invalid_config_raises(env):
    with pytest.raises(ConfigError):
        ScheduleConfig.from_env(env)


def fire_days(dow: str) -> list[str]:
    """Weekdays a '0 3 * * <dow>' job actually fires on over two weeks (Toronto)."""
    spec = CronSpec.parse(f"0 3 * * {dow}")
    trig = CronTrigger(timezone=TZ, **spec.trigger_kwargs())
    t = datetime(2026, 11, 1, 0, 0, tzinfo=TZ)  # a Sunday
    days = []
    for _ in range(14):
        t = trig.get_next_fire_time(None, t)
        if t is None or t >= datetime(2026, 11, 15, tzinfo=TZ):
            break
        days.append(t.strftime("%a"))
        t = t.replace(hour=4)
    return sorted(set(days), key=["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].index)


@pytest.mark.parametrize(
    "dow, days",
    [
        ("0", ["Sun"]),
        ("7", ["Sun"]),
        ("1-5", ["Mon", "Tue", "Wed", "Thu", "Fri"]),
        ("0-6", ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]),  # used to crash: reversed APScheduler range
        ("0-4", ["Sun", "Mon", "Tue", "Wed", "Thu"]),
        ("*/2", ["Sun", "Tue", "Thu", "Sat"]),  # crontab steps from Sunday, not Monday
        ("1-5/2", ["Mon", "Wed", "Fri"]),
        ("5-1", ["Sun", "Mon", "Fri", "Sat"]),  # wrap-around
        ("5-7", ["Sun", "Fri", "Sat"]),
        ("sat-sun", ["Sun", "Sat"]),
        ("1,3,5", ["Mon", "Wed", "Fri"]),
        ("6/1", ["Sat"]),
        ("*", ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]),
    ],
)
def test_crontab_day_of_week_fires_on_the_right_days(dow, days):
    assert fire_days(dow) == days


@pytest.mark.parametrize("dow", ["8", "1-9", "*/0", "*/x", "fooday", "1-", "1,,2", "mon-"])
def test_bad_day_of_week_is_a_config_error(dow):
    with pytest.raises(ConfigError):
        CronSpec.parse(f"0 3 * * {dow}")


@pytest.mark.parametrize("expr", ["61 3 * * 0", "0 25 * * 0", "0 3 32 * *", "0 3 * 13 *"])
def test_other_bad_cron_fields_are_config_errors(expr):
    with pytest.raises(ConfigError):
        CronSpec.parse(expr)


def test_bad_cron_fails_settings_not_server_startup():
    with pytest.raises(ConfigError):
        Settings.from_env({"GWT_DAILY_SWEEP_CRON": "43 5 * * 0-9"})


def test_weekly_default_fires_on_sunday_0317_toronto():
    cfg = ScheduleConfig.from_env({})
    weekly = cfg.jobs[0]
    trig = CronTrigger(timezone=TZ, **weekly.cron.trigger_kwargs())
    nxt = trig.get_next_fire_time(None, datetime(2026, 10, 28, 12, 0, tzinfo=TZ))  # a Wednesday
    assert nxt == datetime(2026, 11, 1, 3, 17, tzinfo=TZ)
    assert nxt.strftime("%A") == "Sunday"

    daily = CronTrigger(timezone=TZ, **cfg.jobs[1].cron.trigger_kwargs())
    assert daily.get_next_fire_time(None, datetime(2026, 10, 28, 12, 0, tzinfo=TZ)) == datetime(2026, 10, 29, 5, 43, tzinfo=TZ)


def test_settings_from_env():
    s = Settings.from_env({"GWT_DB": "/app/data/gwl.db", "GWT_PORT": "9000"})
    assert str(s.db_path) == "/app/data/gwl.db" and s.port == 9000 and s.host == "0.0.0.0"
    assert Settings.from_env({}).port == 8080


def test_final_fill_max_lead_setting():
    assert Settings.from_env({}).final_fill_max_lead == 2
    assert Settings.from_env({"GWT_FINAL_FILL_MAX_LEAD": "0"}).final_fill_max_lead == 0
    assert Settings.from_env({"GWT_FINAL_FILL_MAX_LEAD": "5"}).final_fill_max_lead == 5
    for bad in ("-1", "two"):
        with pytest.raises(ConfigError):
            Settings.from_env({"GWT_FINAL_FILL_MAX_LEAD": bad})


def test_build_scheduler(tmp_path):
    assert build_scheduler(ScheduleConfig.from_env({"GWT_SCHEDULE_ENABLED": "false"}), tmp_path / "x.db") is None
    sched = build_scheduler(ScheduleConfig.from_env({}), tmp_path / "x.db")
    sched.start(paused=True)
    try:
        jobs = {j["id"]: j for j in describe(sched)}
        assert set(jobs) == {"sweep-weekly", "sweep-daily", "sweep-intraday"}
        for job in sched.get_jobs():
            assert job.max_instances == 1 and job.coalesce is True
    finally:
        sched.shutdown(wait=False)


# --------------------------------------------------------------------------- no overlap

JOB = SweepJob(name="daily", cron=CronSpec.parse("43 5 * * *"), days=30)


def test_scheduled_sweep_outcomes(tmp_path):
    calls = []
    stop = threading.Event()

    def ok(db_path, *, days, trigger, stop):
        calls.append((days, trigger, stop))
        return SweepResult(run_id="r", fetched=30)

    assert run_scheduled_sweep(tmp_path / "x.db", JOB, runner=ok, stop=stop) == "completed"
    assert calls == [(30, "schedule:daily", stop)]  # the shutdown event reaches the sweep

    def blocked(*a, **k):
        raise SweepAborted("403", blocked=True)

    def aborted(*a, **k):
        raise SweepAborted("5 failures")

    def rate_limited(*a, **k):
        raise SweepAborted("Retry-After 3600s", status="rate_limited")

    def interrupted(*a, **k):
        raise SweepInterrupted("stop requested")

    def paused(*a, **k):
        raise SweepPaused({"run_id": "2026-10-25", "message": "HTTP 403", "date": "2026-10-25", "run_row": 9})

    def crash(*a, **k):
        raise RuntimeError("boom")

    assert run_scheduled_sweep(tmp_path / "x.db", JOB, runner=blocked) == "blocked"
    assert run_scheduled_sweep(tmp_path / "x.db", JOB, runner=aborted) == "aborted"
    assert run_scheduled_sweep(tmp_path / "x.db", JOB, runner=rate_limited) == "rate_limited"
    assert run_scheduled_sweep(tmp_path / "x.db", JOB, runner=interrupted) == "interrupted"
    assert run_scheduled_sweep(tmp_path / "x.db", JOB, runner=paused) == "paused"
    assert run_scheduled_sweep(tmp_path / "x.db", JOB, runner=crash) == "error"


def test_intraday_job_uses_a_run_id_of_its_own(tmp_path, monkeypatch):
    """The afternoon sweep must not share the day's run ID, or it would skip every date the
    morning sweep already captured."""
    from gwt import scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "default_run_id", lambda: "2026-10-28")
    calls = []

    def runner(db_path, **kwargs):
        calls.append(kwargs)
        return SweepResult(run_id=kwargs.get("run_id") or "2026-10-28", fetched=14)

    intraday = SweepJob(name="intraday", cron=CronSpec.parse("13 15 * * *"), days=14, own_run=True)
    assert run_scheduled_sweep(tmp_path / "x.db", intraday, runner=runner) == "completed"
    assert run_scheduled_sweep(tmp_path / "x.db", JOB, runner=runner) == "completed"
    assert calls[0]["run_id"] == "2026-10-28-intraday" and calls[0]["days"] == 14
    assert calls[0]["trigger"] == "schedule:intraday"
    assert "run_id" not in calls[1]  # daily/weekly keep the shared default run ID


def test_paused_scheduled_sweep_logs_loudly(tmp_path, caplog):
    def paused(*a, **k):
        raise SweepPaused({"run_id": "2026-10-25", "message": "HTTP 403", "date": "2026-10-25", "run_row": 9})

    with caplog.at_level("ERROR", logger="gwt.scheduler"):
        assert run_scheduled_sweep(tmp_path / "x.db", JOB, runner=paused) == "paused"
    text = caplog.text
    assert "COLLECTION PAUSED" in text and "gwt unblock" in text and "2026-10-25" in text


def test_queued_job_does_not_start_after_shutdown(tmp_path):
    stop = threading.Event()
    stop.set()

    def never(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("sweep started during shutdown")

    assert run_scheduled_sweep(tmp_path / "x.db", JOB, runner=never, stop=stop) == "stopping"


def test_file_lock_wait(tmp_path):
    db_path = tmp_path / "gwl.db"
    held, release = threading.Event(), threading.Event()

    def holder():
        with sweep_lock(db_path):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(5)
    try:
        with pytest.raises(SweepBusy):
            with sweep_lock(db_path, wait=0.3):  # bounded wait, then gives up
                pass
        threading.Timer(0.3, release.set).start()
        with sweep_lock(db_path, wait=5):  # acquired once the holder lets go
            pass
    finally:
        release.set()
        t.join(5)


def test_scheduled_sweep_skips_when_another_is_running_in_process(tmp_path):
    started, release = threading.Event(), threading.Event()

    def slow(*a, **k):
        started.set()
        release.wait(5)
        return SweepResult(run_id="r")

    t = threading.Thread(target=run_scheduled_sweep, args=(tmp_path / "x.db", JOB), kwargs={"runner": slow})
    t.start()
    assert started.wait(5)
    try:
        assert run_scheduled_sweep(tmp_path / "x.db", JOB, runner=slow) == "busy"
    finally:
        release.set()
        t.join(5)


def test_file_lock_blocks_second_sweep(tmp_path):
    db_path = tmp_path / "gwl.db"
    with sweep_lock(db_path):
        with pytest.raises(SweepBusy):
            with sweep_lock(db_path):
                pass
    with sweep_lock(db_path):  # released again
        pass


def test_cli_sweep_reports_busy(tmp_path):
    from gwt.cli import EXIT_BUSY, main

    db_path = tmp_path / "gwl.db"
    with sweep_lock(db_path):
        assert main(["sweep", "--days", "1", "--db", str(db_path)]) == EXIT_BUSY


def test_container_stop_interrupts_a_running_scheduled_sweep(tmp_path, monkeypatch, sample_payload):
    """`docker compose stop` -> uvicorn shutdown -> lifespan sets the stop event; the running
    sweep stops waiting, records itself as 'interrupted', and the scheduler waits for it."""
    import httpx
    from fastapi.testclient import TestClient

    from gwt import db
    from gwt import sweep as sweep_mod
    from gwt.web.app import create_app

    fetched = threading.Event()

    def handler(request):
        fetched.set()
        return httpx.Response(200, json=sample_payload)

    monkeypatch.setattr(sweep_mod, "make_client", lambda: httpx.Client(
        base_url="https://prod.api.greatwolf.com", transport=httpx.MockTransport(handler)))
    path = tmp_path / "gwl.db"
    db.connect(path).close()
    schedule = ScheduleConfig.from_env({"GWT_DAILY_SWEEP_CRON": "off", "GWT_INTRADAY_SWEEP_CRON": "off"})
    app = create_app(Settings(db_path=path, host="127.0.0.1", port=8080, schedule=schedule))
    with TestClient(app):
        app.state.scheduler.get_job("sweep-weekly").modify(next_run_time=datetime.now(TZ))
        assert fetched.wait(10)  # first date saved; the sweep is now in its 3-5 s pause
    conn = db.connect(path)
    try:
        status, fetched_n, message = conn.execute("SELECT status, fetched, message FROM runs").fetchone()
    finally:
        conn.close()
    assert status == "interrupted" and fetched_n >= 1 and "shutdown" in message
