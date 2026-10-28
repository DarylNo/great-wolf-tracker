"""Group suite codes into rooms.

The API returns ~27 suite codes, but many are accessible or bedding variants of the same
room (e.g. four "Timber Wolf Cottage" codes). Codes are grouped by their human name
(``suiteBrand``), giving ~17 rooms. A room is sold out only when every variant is at 0.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

CATEGORY_ORDER = ("standard", "themed", "premium", "cottage")


def category_rank(category: str | None) -> int:
    if category and category.lower() in CATEGORY_ORDER:
        return CATEGORY_ORDER.index(category.lower())
    return len(CATEGORY_ORDER)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "room"


@dataclass
class Variant:
    """One suite code in one snapshot (or in the suites lookup table)."""

    code: str
    name: str
    category: str | None = None
    rooms: int | None = None
    accessible: int | None = None
    beds: int | None = None
    baths: int | None = None
    max_occupancy: int | None = None
    min_rate: float | None = None
    rates: dict[str, float | None] = field(default_factory=dict)  # rate family -> cheapest rate
    sort_order: int | None = None

    @property
    def has_rooms(self) -> bool:
        return (self.rooms or 0) > 0

    @property
    def cheapest(self) -> float | None:
        values = [v for v in self.rates.values() if v is not None]
        if self.min_rate is not None:
            values.append(self.min_rate)
        return min(values) if values else None


@dataclass
class RoomGroup:
    name: str
    slug: str
    category: str | None
    variants: list[Variant]

    @property
    def codes(self) -> list[str]:
        return [v.code for v in self.variants]

    @property
    def rooms(self) -> int:
        return sum(v.rooms or 0 for v in self.variants)

    @property
    def has_accessible(self) -> bool:
        return any(v.accessible for v in self.variants)

    @property
    def accessible_rooms(self) -> int:
        return sum(v.rooms or 0 for v in self.variants if v.accessible)

    @property
    def sold_out(self) -> bool:
        return all(not v.has_rooms for v in self.variants)

    @property
    def occupancy(self) -> tuple[int, int] | None:
        occ = [v.max_occupancy for v in self.variants if v.max_occupancy is not None]
        return (min(occ), max(occ)) if occ else None

    @property
    def occupancy_label(self) -> str:
        occ = self.occupancy
        if occ is None:
            return "–"
        return str(occ[0]) if occ[0] == occ[1] else f"{occ[0]}–{occ[1]}"

    def family_rate(self, family: str) -> float | None:
        """Cheapest rate in one rate family, among variants that still have rooms."""
        values = [v.rates.get(family) for v in self.variants if v.has_rooms]
        values = [x for x in values if x is not None]
        return min(values) if values else None

    @property
    def cheapest(self) -> float | None:
        """Cheapest rate among variants that still have rooms."""
        values = [v.cheapest for v in self.variants if v.has_rooms]
        values = [x for x in values if x is not None]
        return min(values) if values else None


def group_rooms(
    variants: Iterable[Variant],
    reference_rates: Mapping[str, float | None] | None = None,
) -> list[RoomGroup]:
    """Group variants by room name and sort the rooms.

    Order: category (standard, themed, premium, cottage), then price. A sold-out room has no
    current rate, so when `reference_rates` (a stable price level per room, e.g. its lowest
    rate ever seen) is given, every room sorts by that: the order is the same on every night
    and sold-out rooms keep their place. Without it, rooms sort by their current cheapest rate
    and sold-out rooms go last within their category.
    """
    reference_rates = reference_rates or {}
    by_name: dict[str, list[Variant]] = {}
    for v in variants:
        by_name.setdefault(v.name or v.code, []).append(v)

    groups = []
    used_slugs: set[str] = set()
    for name, members in by_name.items():
        members.sort(key=lambda v: (bool(v.accessible), v.sort_order if v.sort_order is not None else 1_000, v.code))
        cats = [v.category for v in members if v.category]
        category = max(set(cats), key=cats.count) if cats else None
        slug = slugify(name)
        while slug in used_slugs:
            slug += "-2"
        used_slugs.add(slug)
        groups.append(RoomGroup(name=name, slug=slug, category=category, variants=members))

    def key(g: RoomGroup):
        rate = reference_rates.get(g.name)
        if rate is None:
            rate = g.cheapest
        return (category_rank(g.category), rate if rate is not None else math.inf, g.name.lower())

    groups.sort(key=key)
    return groups
