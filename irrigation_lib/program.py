"""Whole-night collapse: plan slots -> one Rachio schedule + device pauses.

blocks.py hands Rachio one call per maximal watering run and sleeps through idle
soak gaps, so a night yields several schedule-start notifications. This module
flattens the WHOLE night into a single ordered program: every watering slot
feeds one `start_multiple_zone_schedule` call, and each idle soak gap becomes a
device pause that keeps that one schedule alive. One schedule -> one notification.

Pure Python (no pyscript names). plan.py is untouched; this consumes its slots.
"""
from __future__ import annotations

from dataclasses import dataclass

from irrigation_lib.blocks import ZoneRun

# Slack for float dust from drought-scaled runtimes (mirrors blocks.py).
_EPS = 1e-9


@dataclass(frozen=True)
class Step:
    kind: str             # "water" | "pause"
    zone_key: str | None  # set for "water", None for "pause"
    minutes: int          # whole minutes


def plan_program(slots: list) -> list:
    """Flatten a plan's slots into an ordered water/pause Step list.

    Watering slots are quantized by cumulative floor across the WHOLE night (as
    blocks.quantize does per block) so total watering stays within a minute of
    plan and never over. Idle slots become pause Steps. A water slot that rounds
    to zero is dropped; the pauses that would have flanked it merge so the soak
    stays one gap. Leading and trailing pauses are dropped (nothing to keep the
    schedule alive for).
    """
    steps = []
    cumulative = 0.0
    allocated = 0
    for slot in slots:
        if slot.zone_key is None:
            pause_min = int(round(slot.minutes))
            if pause_min > 0:
                steps.append(Step("pause", None, pause_min))
        else:
            cumulative += slot.minutes
            minutes = int(cumulative + _EPS) - allocated
            if minutes > 0:
                steps.append(Step("water", slot.zone_key, minutes))
                allocated += minutes
    return _normalize(steps)


def _normalize(steps: list) -> list:
    merged = []
    for s in steps:
        if s.kind == "pause" and merged and merged[-1].kind == "pause":
            merged[-1] = Step("pause", None, merged[-1].minutes + s.minutes)
        else:
            merged.append(s)
    while merged and merged[0].kind == "pause":
        merged = merged[1:]
    while merged and merged[-1].kind == "pause":
        merged = merged[:-1]
    return merged


def program_runs(steps: list) -> list:
    """The water Steps as ZoneRuns, in order — the single schedule's entity list."""
    runs = []
    for s in steps:
        if s.kind == "water":
            runs.append(ZoneRun(zone_key=s.zone_key, minutes=s.minutes))
    return runs


@dataclass(frozen=True)
class Segment:
    steps: list      # water/pause Steps executed under ONE schedule call
    gap_after: int   # idle minutes AFTER this segment (0 = none / last segment)


def segment_program(steps: list, max_pauses: int) -> list:
    """Split a program into per-schedule segments.

    Each segment is submitted as its own `start_multiple_zone_schedule`; the
    pauses INSIDE a segment are device pauses that keep it alive. When a segment
    is full (`max_pauses` pauses already), the next pause instead ends the
    segment and becomes a between-segment idle gap (schedule ends, pyscript
    sleeps) — the bounded-collapse fallback. `max_pauses <= 0` is unbounded: one
    segment, full collapse. Input is assumed normalized (no leading/trailing or
    adjacent pauses), so a break-triggering pause always has water after it.
    """
    unbounded = max_pauses <= 0
    segments = []
    current = []
    pauses_in_current = 0
    for s in steps:
        if s.kind == "pause":
            if not unbounded and pauses_in_current >= max_pauses:
                segments.append(Segment(steps=current, gap_after=s.minutes))
                current = []
                pauses_in_current = 0
                continue
            current.append(s)
            pauses_in_current += 1
        else:
            current.append(s)
    if current:
        segments.append(Segment(steps=current, gap_after=0))
    return segments
