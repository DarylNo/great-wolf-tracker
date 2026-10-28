"""Runtime settings from environment variables (all optional)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_FULL_CRON = "17 3 * * 0"  # Sundays 03:17
DEFAULT_DAILY_CRON = "43 5 * * *"  # daily 05:43
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


_DOW_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _crontab_dow_to_names(field_: str) -> str:
    """Standard crontab day-of-week numbers (0/7 = Sunday) -> names.

    APScheduler 3's CronTrigger numbers days from Monday = 0, so passing crontab
    numbers straight through would shift every weekly job by a day. Step values
    (the number after '/') are left alone.
    """

    def repl(m: re.Match) -> str:
        if m.group(1):  # preceded by '/', it's a step
            return m.group(0)
        n = int(m.group(2))
        if not 0 <= n <= 7:
            raise ConfigError(f"day-of-week out of range: {n}")
        return _DOW_NAMES[n]

    return re.sub(r"(/)?(\d+)", repl, field_)


@dataclass(frozen=True)
class CronSpec:
    """A 5-field crontab expression split into APScheduler CronTrigger kwargs."""

    expression: str
    minute: str
    hour: str
    day: str
    month: str
    day_of_week: str

    @classmethod
    def parse(cls, expression: str) -> "CronSpec":
        parts = expression.split()
        if len(parts) != 5:
            raise ConfigError(f"cron expression needs 5 fields (min hour day month dow), got {expression!r}")
        minute, hour, day, month, dow = parts
        return cls(expression, minute, hour, day, month, _crontab_dow_to_names(dow.lower()))

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
            ("daily", "GWT_DAILY_SWEEP_CRON", DEFAULT_DAILY_CRON, "GWT_DAILY_SWEEP_DAYS", 30),
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
