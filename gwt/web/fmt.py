"""Display formatting shared by templates, tooltips and the JSON API."""

from __future__ import annotations

from datetime import date

WEEKDAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAY_LONG = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTH_SHORT = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def money(value: float | None) -> str:
    return "–" if value is None else f"${value:,.2f}"


def money0(value: float | None) -> str:
    """Whole dollars, for compact places (calendar cells, legends)."""
    return "–" if value is None else f"${value:,.0f}"


def compact_money(value: float | None) -> str:
    """Whole dollars without a thousands separator: fits a small calendar cell."""
    return "–" if value is None else f"${value:.0f}"


def pct(value: float | None) -> str:
    """0..1 -> '47%'."""
    return "–" if value is None else f"{value * 100:.0f}%"


def ordinal(n: float | int | None) -> str:
    if n is None:
        return "n/a"
    k = int(round(n))
    if 10 <= k % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(k % 10, "th")
    return f"{k}{suffix}"


def pace_rank(value: float | None) -> str:
    """A pace percentile as an ordinal, shown as 1st..99th (0 and 100 read oddly)."""
    return "n/a" if value is None else ordinal(min(99, max(1, round(value))))


def pace(value: float | None) -> str:
    return "n/a" if value is None else f"{pace_rank(value)} percentile"


def signed(n: int | None) -> str:
    return "n/a" if n is None else f"{n:+d}"


def date_long(d: date) -> str:
    return f"{WEEKDAY_SHORT[d.weekday()]}, {MONTH_SHORT[d.month - 1]} {d.day}, {d.year}"


def date_short(d: date) -> str:
    return f"{MONTH_SHORT[d.month - 1]} {d.day}"


def date_medium(d: date) -> str:
    return f"{WEEKDAY_SHORT[d.weekday()]} {MONTH_SHORT[d.month - 1]} {d.day}"


def yesno(value) -> str:
    return "?" if value is None else ("yes" if value else "no")
