"""Schedule config parsing, scheduler construction, and the no-overlap guarantees."""

import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger

from gwt.config import ConfigError, CronSpec, ScheduleConfig, Settings, SweepJob
from gwt.locking import SweepBusy, sweep_lock
from gwt.scheduler import build_scheduler, describe, run_scheduled_sweep
from gwt.sweep import SweepAborted, SweepResult

TZ = ZoneInfo("America/Toronto")


def test_defaults():
    cfg = ScheduleConfig.from_env({})
    assert cfg.enabled is True
    assert cfg.timezone == "America/Toronto"
    assert [(j.name, j.cron.expression, j.days) for j in cfg.jobs] == [
        ("weekly", "17 3 * * 0", 365),
        ("daily", "43 5 * * *", 30),
    ]


def test_env_overrides():
    cfg = ScheduleConfig.from_env({
        "GWT_SCHEDULE_ENABLED": "false",
        "GWT_TZ": "UTC",
        "GWT_FULL_SWEEP_CRON": "0 2 * * 6",
        "GWT_FULL_SWEEP_DAYS": "200",
        "GWT_DAILY_SWEEP_CRON": "off",
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


@pytest.mark.parametrize(
    "dow, names",
    [("0", "sun"), ("7", "sun"), ("1-5", "mon-fri"), ("0,6", "sun,sat"), ("*/2", "*/2"), ("*", "*"), ("sun", "sun")],
)
def test_crontab_day_of_week_translation(dow, names):
    assert CronSpec.parse(f"0 0 * * {dow}").day_of_week == names


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


def test_build_scheduler(tmp_path):
    assert build_scheduler(ScheduleConfig.from_env({"GWT_SCHEDULE_ENABLED": "false"}), tmp_path / "x.db") is None
    sched = build_scheduler(ScheduleConfig.from_env({}), tmp_path / "x.db")
    sched.start(paused=True)
    try:
        jobs = {j["id"]: j for j in describe(sched)}
        assert set(jobs) == {"sweep-weekly", "sweep-daily"}
        for job in sched.get_jobs():
            assert job.max_instances == 1 and job.coalesce is True
    finally:
        sched.shutdown(wait=False)


# --------------------------------------------------------------------------- no overlap

JOB = SweepJob(name="daily", cron=CronSpec.parse("43 5 * * *"), days=30)


def test_scheduled_sweep_outcomes(tmp_path):
    calls = []

    def ok(db_path, *, days, trigger):
        calls.append((days, trigger))
        return SweepResult(run_id="r", fetched=30)

    assert run_scheduled_sweep(tmp_path / "x.db", JOB, runner=ok) == "completed"
    assert calls == [(30, "schedule:daily")]

    def blocked(*a, **k):
        raise SweepAborted("403", blocked=True)

    def aborted(*a, **k):
        raise SweepAborted("5 failures")

    def crash(*a, **k):
        raise RuntimeError("boom")

    assert run_scheduled_sweep(tmp_path / "x.db", JOB, runner=blocked) == "blocked"
    assert run_scheduled_sweep(tmp_path / "x.db", JOB, runner=aborted) == "aborted"
    assert run_scheduled_sweep(tmp_path / "x.db", JOB, runner=crash) == "error"


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
