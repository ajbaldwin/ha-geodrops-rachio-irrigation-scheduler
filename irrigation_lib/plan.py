"""Cycle-and-soak interleaving scheduler. Pure Python."""
from __future__ import annotations

from dataclasses import dataclass

from irrigation_lib.config import Tunables

_EPS = 1e-9


@dataclass(frozen=True)
class Slot:
    zone_key: str | None  # None = idle soak
    minutes: float  # int for whole cycles; float for a final partial cycle


@dataclass(frozen=True)
class Plan:
    slots: list[Slot]
    span_minutes: int
    watered: list[str]
    dropped: list[str]


def cycles_minutes(runtime_min: float, scale: float) -> float:
    return runtime_min * scale


def span_of(slots: list) -> int:
    # pyscript has no generator expressions; use a list comprehension.
    return sum([s.minutes for s in slots])


def build_sequence(
    zones_priority: list,
    minutes_by_zone: dict,
    geo: dict,
    adjacency: dict,
    cycle_min: int,
    soak_min: int,
) -> list:
    remaining = {z: minutes_by_zone[z] for z in zones_priority}
    last_end = {z: None for z in zones_priority}
    priority_index = {z: i for i, z in enumerate(zones_priority)}
    clock = 0
    prev_zone = None
    prev_geo = None
    slots = []

    # NOTE (pyscript): the readiness test is inlined into the comprehension
    # below rather than living in a nested `def ready(z)`. pyscript nested
    # functions and lambdas do NOT capture enclosing-function locals, so a
    # helper referencing `remaining` / `last_end` / `clock` / `soak_min` raises
    # NameError. Comprehensions DO see enclosing locals, so this form is safe.
    while any([remaining[z] > _EPS for z in zones_priority]):
        ready_zones = [
            z for z in zones_priority
            if remaining[z] > _EPS
            and (last_end[z] is None or clock >= last_end[z] + soak_min)
        ]
        if ready_zones:
            if prev_zone is None:
                pool = ready_zones
            else:
                dispersed = [
                    z for z in ready_zones
                    if geo[z] != prev_geo and z not in adjacency.get(prev_zone, ())
                ]
                pool = dispersed or ready_zones
            # Explicit loop, not min(..., key=lambda ...): the lambda would not
            # see `priority_index` under pyscript (see note above).
            chosen = pool[0]
            for z in pool:
                if priority_index[z] < priority_index[chosen]:
                    chosen = z
            take = min(cycle_min, remaining[chosen])
            slots.append(Slot(chosen, take))
            clock += take
            last_end[chosen] = clock
            remaining[chosen] -= take
            prev_zone = chosen
            prev_geo = geo[chosen]
        else:
            pending = [z for z in zones_priority if remaining[z] > _EPS]
            next_ready = min([last_end[z] + soak_min for z in pending])
            slots.append(Slot(None, next_ready - clock))
            clock = next_ready
            prev_zone = None
            prev_geo = None

    return slots


def build_plan(
    zones_priority: list,
    minutes_by_zone: dict,
    geo: dict,
    adjacency: dict,
    cap_minutes: float,
    t: Tunables,
) -> Plan:
    kept = list(zones_priority)
    while kept:
        slots = build_sequence(
            kept, minutes_by_zone, geo, adjacency,
            t.cycle_minutes, t.soak_minutes,
        )
        span = span_of(slots)
        if span <= cap_minutes:
            dropped = [z for z in zones_priority if z not in kept]
            return Plan(slots=slots, span_minutes=span, watered=kept, dropped=dropped)
        kept = kept[:-1]
    return Plan(slots=[], span_minutes=0, watered=[], dropped=list(zones_priority))
