"""Abort-reason precedence. Pure Python.

Partial-run accounting used to live here as a per-cycle helper. Rachio now runs
a whole block from one call, so crediting is per block and lives in
`blocks.delivered`.
"""
from __future__ import annotations


def abort_reason(standby: bool, manual_stop: bool, rain: bool):
    if manual_stop:
        return "manual-abort"
    if rain:
        return "rain-abort"
    if standby:
        return "standby"
    return None


def watch_step(running, seen_on, misses, elapsed_seconds, total_seconds,
               confirm_seconds, grace_seconds, stop_polls):
    """One poll of the in-block watch: (verdict, seen_on, misses).

    `verdict` is None while the block looks healthy, otherwise the reason the
    run should end. Three guards, in this order, because they interact:

    1. **Never started.** Watering that has not been observed within
       `confirm_seconds` of the block being handed to Rachio did not start.
       This is checked FIRST and is not excused by the end grace, because
       otherwise a block short enough to sit inside the grace could never be
       reported at all. Without this the "seen on first" rule below has no
       timeout: "not yet on" and "never came on" look identical, and the second
       gets scored as a full success — crediting water that never fell.

    2. **End grace.** Rachio's clock and ours are independent, so a block
       finishing a poll or two before our sleep does is completion, not a stop.
       Inside `grace_seconds` of the scheduled end, an empty poll is ignored.

    3. **External stop.** Watering that WAS seen and then went away for
       `stop_polls` consecutive polls was stopped by something outside this
       scheduler. One empty poll is not proof: Rachio steps between zones
       inside a block on its own, and HA can briefly show both switches off
       across that hand-off.
    """
    if running:
        return None, True, 0
    if not seen_on:
        if elapsed_seconds >= confirm_seconds:
            return "never-started", False, misses
        return None, False, misses
    if (total_seconds - elapsed_seconds) <= grace_seconds:
        return None, seen_on, misses
    misses = misses + 1
    if misses >= stop_polls:
        return "external-stop", seen_on, misses
    return None, seen_on, misses
