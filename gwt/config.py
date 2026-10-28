"""Runtime settings from environment variables (all optional)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_FULL_CRON = "17 3 * * 0"  # Sundays 03:17
DEFAULT_DAILY_CRON = "43 5 * * *"  # daily 05:43
# 38 = 30 + 8: a night up to 30 days out today was up to 38 days out 6-8 days ago, so with a
# 38-day daily window every night in the next 30 days has a scrape 6-8 days earlier
# (pickup_7d, "sold in the last 7 days") on every day of the week, not only right after
# the weekly sweep.
DEFAULT_DAILY_DAYS = 38
DEFAULT_TZ = "America/Toronto"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_OFF = {"", "off", "none", "disabled", "false"}


class ConfigError(ValueError):
    pass


def parse_bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ConfigError(f"not a boolean: {value!r}")


def parse_positive_int(value: str | None, default: int, name: str) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        n = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if n < 1:
        raise ConfigError(f"{name} must be >= 1, got {n}")
    return n


DOW_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")  # crontab numbering: 0 (and 7) = Sunday


def _dow_number(token: str, field_: str) -> int:
    """One crontab day-of-week value: 0-7 (0 and 7 are Sunday) or a 3-letter name."""
    t = token.strip().lower()
    if t.isdigit():
        n = int(t)
        if 0 <= n <= 7:
            return n
    elif t in DOW_NAMES:
        return DOW_NAMES.index(t)
    raise ConfigError(f"bad day-of-week {token!r} in {field_!r} (use 0-7 or sun..sat)")


def crontab_days(field_: str) -> tuple[int, ...]:
    """Expand a crontab day-of-week field into the days it selects (0 = Sunday .. 6 = Saturday).

    Handles '*', single days, ranges (including wrap-around like 5-1 = Fri..Mon, and 7 as
    Sunday), lists, and steps ('*/2' = Sun, Tue, Thu, Sat; '1-5/2' = Mon, Wed, Fri; 'a/n'
    runs from a to Saturday). The result is explicit, so APScheduler's Monday-first
    numbering never sees a crontab number or a range that would come out reversed.
    """
    if not field_.strip():
        raise ConfigError("empty day-of-week field")
    days: set[int] = set()
    for item in field_.lower().split(","):
        base, slash, step_s = item.partition("/")
        step = 1
        if slash:
            if not step_s.isdigit() or int(step_s) < 1:
                raise ConfigError(f"bad step {step_s!r} in day-of-week {field_!r}")
            step = int(step_s)
        if base == "*":
            seq = list(range(7))
        elif "-" in base:
            a_s, _, b_s = base.partition("-")
            a, b = _dow_number(a_s, field_), _dow_number(b_s, field_)
            if a <= b:
                seq = list(range(a, b + 1))
            else:  # wrap around the end of the week, e.g. 5-1 or sat-sun
                seq = list(range(a, 7)) + list(range(0, b + 1))
        else:
            a = _dow_number(base, field_)
            seq = list(range(a, 7)) if slash else [a]
        days.update(n % 7 for n in seq[::step])
    return tuple(sorted(days))


def apscheduler_day_of_week(days: tuple[int, ...]) -> str:
    """Explicit day names for APScheduler ('*' when every day is selected)."""
    return "*" if len(days) == 7 else ",".join(DOW_NAMES[d] for d in days)


@dataclass(frozen=True)
class CronSpec:
    """A 5-field crontab expression split into APScheduler CronTrigger kwargs."""

    expression: str
    minute: str
    hour: str
    day: str
    month: str
    day_of_week: str
    days: tuple[int, ...] = ()

    @classmethod
    def parse(cls, expression: str) -> "CronSpec":
        parts = expression.split()
        if len(parts) != 5:
            raise ConfigError(f"cron expression needs 5 fields (min hour day month dow), got {expression!r}")
        minute, hour, day, month, dow = parts
        days = crontab_days(dow)
        spec = cls(expression, minute, hour, day, month, apscheduler_day_of_week(days), days)
        # Validate every field now, so a bad value is a ConfigError at startup rather than a
        # crash inside the web server's lifespan.
        from apscheduler.triggers.cron import CronTrigger

        try:
            CronTrigger(timezone="UTC", **spec.trigger_kwargs())
        except ValueError as exc:
            raise ConfigError(f"invalid cron expression {expression!r}: {exc}") from exc
        return spec

    def trigger_kwargs(self) -> dict[str, str]:
        return {
            "minute": self.minute,
            "hour": self.hour,
            "day": self.day,
            "month": self.month,
            "day_of_week": self.day_of_week,
        }


@dataclass(frozen=True)
class SweepJob:
    name: str  # "weekly" / "daily"
    cron: CronSpec
    days: int


@dataclass(frozen=True)
class ScheduleConfig:
    enabled: bool
    timezone: str
    jobs: tuple[SweepJob, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ScheduleConfig":
        env = os.environ if env is None else env
        enabled = parse_bool(env.get("GWT_SCHEDULE_ENABLED"), True)
        tz = env.get("GWT_TZ") or env.get("TZ") or DEFAULT_TZ
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"unknown timezone {tz!r}") from exc

        jobs = []
        for name, cron_var, cron_default, days_var, days_default in (
            ("weekly", "GWT_FULL_SWEEP_CRON", DEFAULT_FULL_CRON, "GWT_FULL_SWEEP_DAYS", 365),
            ("daily", "GWT_DAILY_SWEEP_CRON", DEFAULT_DAILY_CRON, "GWT_DAILY_SWEEP_DAYS", DEFAULT_DAILY_DAYS),
        ):
            raw = env.get(cron_var)
            expr = cron_default if raw is None else raw.strip()
            if expr.lower() in _OFF:
                continue
            jobs.append(
                SweepJob(
                    name=name,
                    cron=CronSpec.parse(expr),
                    days=parse_positive_int(env.get(days_var), days_default, days_var),
                )
            )
        return cls(enabled=enabled, timezone=tz, jobs=tuple(jobs))


def parse_nonnegative_int(value: str | None, default: int, name: str) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        n = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if n < 0:
        raise ConfigError(f"{name} must be >= 0, got {n}")
    return n


@dataclass(frozen=True)
class Settings:
    db_path: Path
    host: str
    port: int
    schedule: ScheduleConfig
    # final_fill counts only if the closest scrape to arrival is at most this many days out.
    final_fill_max_lead: int = 2

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        return cls(
            db_path=Path(env.get("GWT_DB") or "data/gwl.db"),
            host=env.get("GWT_HOST") or "0.0.0.0",
            port=parse_positive_int(env.get("GWT_PORT"), 8080, "GWT_PORT"),
            schedule=ScheduleConfig.from_env(env),
            final_fill_max_lead=parse_nonnegative_int(env.get("GWT_FINAL_FILL_MAX_LEAD"), 2, "GWT_FINAL_FILL_MAX_LEAD"),
        )
