"""Per-zone watering evaluation and priority ordering. Pure Python."""
from __future__ import annotations

import random
from dataclasses import dataclass

from irrigation_lib.drought import EffectiveTarget
from irrigation_lib.sensors import ZoneReading


@dataclass(frozen=True)
class ZoneEvaluation:
    key: str
    needs_water: bool
    index_deficit: int
    dominant_deficit: float


def evaluate_zone(reading: ZoneReading, target: EffectiveTarget) -> ZoneEvaluation:
    if not reading.online:
        return ZoneEvaluation(reading.key, False, 0, 0.0)
    needs = reading.dominant < target.floor
    return ZoneEvaluation(
        key=reading.key,
        needs_water=needs,
        index_deficit=target.rank - reading.index_rank,
        dominant_deficit=target.floor - reading.dominant,
    )


def revalidate_zone(online, dominant_now, dosing_source, floor, ceiling):
    """Re-decide, at window start, whether a PLANNED zone should still water.

    The nightly plan is built hours before the pre-dawn window; rain that lands
    in the gap can raise a zone's moisture without being visible at plan time
    (GeoDrops sensors report on a slow cadence). Given a FRESH reading, return a
    drop reason, or None to keep the zone:

    - sensor offline now -> None (fail-open; cannot prove the soil is wet, and
      the plan already qualified the sensor).
    - probe zone -> "saturated" if dominant_now >= ceiling
      (probe_headroom_ceiling); a probe into saturated soil is rejected anyway.
    - deficit zone -> "above-floor" if dominant_now >= floor; the trigger reason
      (dominant below floor) is gone.

    Boundary is inclusive (>=): a reading exactly at the line drops.
    """
    if not online:
        return None
    if dosing_source == "probe":
        if dominant_now >= ceiling:
            return "saturated"
        return None
    if dominant_now >= floor:
        return "above-floor"
    return None


def sort_by_priority(evals: list, rng: random.Random) -> list:
    """Zones needing water, ordered by index deficit, then dominant-% deficit,
    then random.

    NOTE (pyscript): do NOT use `sorted(..., key=lambda e: ...)` here. pyscript
    lambdas (and nested defs) do NOT capture enclosing-function locals, so
    referencing `rng` inside the key raises
    `NameError: name 'rng' is not defined`. Comprehensions DO see enclosing
    locals, so the filter below is fine. Decorate in an explicit loop (which can
    see `rng`) with an all-numeric tuple, sort that, then map back by index — the
    trailing index also guarantees the sort never has to compare ZoneEvaluation
    objects, which are not orderable.
    """
    needing = [e for e in evals if e.needs_water]
    decorated = []
    for i in range(len(needing)):
        e = needing[i]
        decorated.append((-e.index_deficit, -e.dominant_deficit, rng.random(), i))
    result = []
    for row in sorted(decorated):
        result.append(needing[row[3]])
    return result
