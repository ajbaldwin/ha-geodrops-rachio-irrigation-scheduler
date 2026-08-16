"""Notification and calendar text builders. Pure Python."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

# Never zero: HA's Local Calendar enforces a minimum event duration of one
# second, so a point-in-time entry is rejected outright.
_MARKER = dt.timedelta(minutes=1)


@dataclass(frozen=True)
class RunResult:
    watered: list[str]
    uncompleted: dict[str, str]  # zone -> reason
    start: str  # when watering actually began (== end when nothing watered)
    end: str    # when the run finished
    per_zone_minutes: dict[str, float]
    standby: bool
    # The disease window the run was allowed to use: a 4-6 h span ending just
    # before dawn, independent of how many zones watered. Kept separate from
    # start/end so a no-water night reports the real window instead of a
    # zero-length one. Defaulted so existing constructions stay valid.
    window_start: str = ""
    window_end: str = ""
    window_hours: float = 0.0


def display_name(zone_key: str) -> str:
    """Operator-facing name for a zone: "zone_a" -> "Zone A".

    Zone keys are config identifiers; every human-readable surface (phone
    notification, calendar entry, Logbook) shows this instead. Title-casing the
    underscored key reproduces the zone names configured in Rachio for all
    managed zones, so no separate mapping has to be kept in sync.

    Raw keys are deliberately retained in the `pyscript.irrigation_*` state
    attributes, where they are diagnostic and must match `config.yaml`.
    """
    return zone_key.replace("_", " ").title()


def _window_line(r: RunResult) -> str:
    """"Window: 00:52–04:52 (4.0h)", or "" when the window is unknown."""
    if not (r.window_start and r.window_end):
        return ""
    return f"Window: {r.window_start}–{r.window_end} ({r.window_hours}h)"


def format_notification(r: RunResult) -> str:
    if r.standby:
        return "Irrigation Standby — system paused, no zones watered."
    lines = []
    if r.watered:
        names = [display_name(z) for z in r.watered]
        lines.append(f"Watered {len(r.watered)}: {', '.join(names)}")
        lines.append(f"Watered {r.start}–{r.end}")
    else:
        lines.append("No zones watered.")
    window = _window_line(r)
    if window:
        lines.append(window)
    if r.uncompleted:
        parts = [f"{display_name(z)} ({reason})" for z, reason in r.uncompleted.items()]
        lines.append(f"Not completed: {', '.join(parts)}")
    return "\n".join(lines)


def format_preview(
    drought_level: str,
    planned: list,
    skipped: dict,
    window_note: str = "",
    watering_note: str = "",
) -> str:
    """Human-readable dry-run summary of the plan that WOULD run (no watering).

    `planned` is an ordered list of (zone, minutes) that would water; `skipped`
    maps zone -> reason (offline / insufficient window).

    `window_note` describes the DISEASE WINDOW — a fixed 4-6 h span ending just
    before dawn, independent of how many zones water. `watering_note` describes
    the watering packed WITHIN that window (total span + when it starts). Keeping
    them separate avoids the old bug where an empty plan showed a 0-minute
    "window". The leading line omits an "Irrigation preview" prefix — the
    notification title already carries it.
    """
    # Plan first (the actionable part), then skipped, then the window + drought
    # context last — the watering plan reads at the top of the message.
    lines = []
    if planned:
        lines.append(f"Would water {len(planned)}:")
        for zone, minutes in planned:
            lines.append(f"- {display_name(zone)}: {int(minutes)} min")
        if watering_note:
            lines.append(watering_note)
    else:
        lines.append("Would water nothing (no zones below target, or none fit).")
    if skipped:
        lines.append("Skipped:")
        for zone, reason in skipped.items():
            lines.append(f"- {display_name(zone)}: {reason}")
    if window_note:
        lines.append(window_note)
    lines.append(f"Drought: {drought_level}")
    return "\n".join(lines)


def format_calendar(r: RunResult, tun_cycle: int, tun_soak: int) -> tuple[str, str]:
    if r.standby:
        return ("Irrigation Standby", "System in standby — no watering performed.")
    total = sum(r.per_zone_minutes.values())
    title = f"Irrigation — {len(r.watered)} zones, {int(total)} min"
    lines = []
    window = _window_line(r)
    if window:
        lines.append(window)
    if r.watered:
        # Actual watering times — only meaningful when something ran. With
        # nothing watered these collapse to a zero-length "window".
        lines.append(f"Watered: {r.start}–{r.end}")
    lines.append(f"Cycle/soak: {tun_cycle}/{tun_soak} min")
    for z in r.watered:
        lines.append(f"- {display_name(z)}: {int(r.per_zone_minutes.get(z, 0))} min")
    if r.uncompleted:
        lines.append("Not completed:")
        for z, reason in r.uncompleted.items():
            lines.append(f"- {display_name(z)}: {reason}")
    return (title, "\n".join(lines))


def calendar_span(started_at, ended_at, fallback):
    """(start, end) for the recap's calendar entry, in ONE timezone.

    Both ends MUST carry the same UTC offset. HA's calendar.create_event
    validates that and rejects the pair otherwise ("Expected all values to have
    the same timezone") — and the two ends arrive from sources that disagree:
    the run's start descends from `sensor.sun_next_dawn`, which HA serves in
    UTC, while the finish time is local. Python compares aware datetimes across
    zones without complaint, so nothing catches the mismatch until the service
    call fails.

    A real watering interval spans start to finish. Anything else — nothing
    watered, or an end that does not follow its start — becomes a one-minute
    marker at `fallback`, because there is no interval to draw and a
    zero-length event would be rejected.
    """
    if started_at is not None and ended_at is not None and ended_at > started_at:
        return started_at.astimezone(), ended_at.astimezone()
    base = fallback.astimezone()
    return base, base + _MARKER
