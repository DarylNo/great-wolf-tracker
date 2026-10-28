"""Demand index (see README "Demand index")."""

from __future__ import annotations

from typing import Iterable, Mapping

W_INVENTORY = 0.6
W_RATE = 0.3
W_SOLD_OUT = 0.1


def percentile_ranks(values: list[float]) -> list[float]:
    """Percentile rank 0..1 of each value within `values` (ties share their average rank)."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2
        for k in range(i, j + 1):
            ranks[order[k]] = avg / (n - 1)
        i = j + 1
    return ranks


def add_demand(rows: Iterable[Mapping], max_rooms_by_mix: Mapping[int, int]) -> list[dict]:
    """Return copies of snapshot rows with `demand` (0-100) and its components added.

    - inventory term: 1 - rooms_available / max rooms ever seen for the same suite mix
      (suite mix approximated by the number of suite types returned)
    - rate term: percentile of min_rate among the rows passed in; a date with no
      available rate at all (fully sold out) counts as the top percentile
    - sold-out term: suite_types_sold_out / suite_types
    Rows with no suites (error responses) get demand None.
    """
    out = [dict(r) for r in rows]
    priced = [(i, r["min_rate"]) for i, r in enumerate(out) if r.get("min_rate") is not None]
    pct = dict(zip((i for i, _ in priced), percentile_ranks([v for _, v in priced])))

    for i, r in enumerate(out):
        types = r.get("suite_types") or 0
        rooms = r.get("total_rooms_available") or 0
        if types == 0:
            r.update(demand=None, inventory_term=None, rate_term=None, sold_out_term=None)
            continue
        cap = max_rooms_by_mix.get(types) or 0
        inventory = (1.0 - rooms / cap) if cap > 0 else (1.0 if rooms == 0 else 0.0)
        inventory = min(max(inventory, 0.0), 1.0)
        rate = pct.get(i, 1.0 if rooms == 0 else 0.5)
        sold = (r.get("suite_types_sold_out") or 0) / types
        demand = 100 * (W_INVENTORY * inventory + W_RATE * rate + W_SOLD_OUT * sold)
        r.update(
            demand=round(demand, 1),
            inventory_term=round(inventory, 3),
            rate_term=round(rate, 3),
            sold_out_term=round(sold, 3),
        )
    return out
