"""Plan slots -> Rachio custom-schedule blocks. Pure Python.

`rachio.start_multiple_zone_schedule` runs a list of zones back to back, in the
order given, each for its own whole number of minutes. That is exactly the shape
of a maximal run of consecutive watering slots in a plan, so each such run is
handed to Rachio as ONE call ("a block") rather than started and stopped zone by
zone. Idle soak slots separate blocks and are executed by pyscript sleeping with
nothing running.
"""
from __future__ import annotations

from dataclasses import dataclass

# Slack for float dust: 0.7-scaled runtimes land on values like 42.699999999999996,
# which a bare floor() would charge a whole minute for.
_EPS = 1e-9

# A run credited less than this delivered nothing worth recording; without a
# floor, a poll landing a hair past a run boundary would list the next zone in
# the recap as watered for ~0 minutes.
_MIN_CREDIT_MINUTES = 0.05  # 3 seconds


@dataclass(frozen=True)
class ZoneRun:
    zone_key: str
    minutes: int  # whole minutes, as Rachio's `int(duration) * 60` requires


@dataclass(frozen=True)
class Block:
    kind: str            # "zones" (one Rachio call) | "idle" (sleep, nothing on)
    slots: list          # plan.Slot list; empty for an idle block
    minutes: float       # planned length of the block


def group_blocks(slots: list) -> list:
    """Split a plan's slots into maximal watering blocks separated by idle."""
    result = []
    current = []
    for slot in slots:
        if slot.zone_key is None:
            if current:
                result.append(_zone_block(current))
                current = []
            result.append(Block(kind="idle", slots=[], minutes=slot.minutes))
        else:
            current.append(slot)
    if current:
        result.append(_zone_block(current))
    return result


def quantize(slots: list) -> list:
    """Whole-minute runs for one block's slots, allocated by cumulative floor.

    Rachio takes an integer number of minutes per zone, but scaled drought
    levels produce fractional slots. Rounding each slot on its own would let the
    error accumulate and could round the block UP — past the window's end
    anchor. Allocating against the running total instead keeps the whole block
    within one minute of plan and never longer than it, because each run is the
    difference of two floors of the cumulative time.

    Runs that come out at zero minutes are dropped: `int(0.4) * 60` would put a
    zero-duration zone into the schedule.
    """
    runs = []
    cumulative = 0.0
    allocated = 0
    for slot in slots:
        cumulative += slot.minutes
        minutes = int(cumulative + _EPS) - allocated
        if minutes > 0:
            runs.append(ZoneRun(zone_key=slot.zone_key, minutes=minutes))
            allocated += minutes
    return runs


def entity_ids(runs: list, switch_by_zone: dict) -> list:
    """Zone switches, one entry per run — repeats intended.

    The Rachio service does NOT de-duplicate its entity list (confirmed on the
    controller), which is what lets one call express a cycle-and-soak block
    where the same zone recurs several times.
    """
    return [switch_by_zone[r.zone_key] for r in runs]


def duration_csv(runs: list) -> str:
    """Minutes for each run, positionally paired with `entity_ids`.

    The service validates this with `cv.ensure_list_csv`, then walks it in step
    with the entity list.
    """
    return ",".join([str(r.minutes) for r in runs])


def delivered(runs: list, elapsed_seconds: float) -> dict:
    """{zone_key: minutes} actually watered by a block that ran `elapsed`.

    Rachio walks the block itself, so when a block ends early the scheduler
    knows only how long the block ran, not which zone was on. The run order
    recovers that: time fills the runs in sequence, the last one reached gets
    whatever is left, and zones never reached are absent rather than zero.
    """
    result = {}
    remaining = max(0.0, elapsed_seconds / 60.0)
    for run in runs:
        if remaining < _MIN_CREDIT_MINUTES:
            break
        gave = min(run.minutes, remaining)
        result[run.zone_key] = result.get(run.zone_key, 0) + gave
        remaining -= gave
    return result


def _zone_block(slots: list) -> Block:
    # pyscript has no generator expressions; use a list comprehension.
    return Block(
        kind="zones",
        slots=list(slots),
        minutes=sum([s.minutes for s in slots]),
    )
