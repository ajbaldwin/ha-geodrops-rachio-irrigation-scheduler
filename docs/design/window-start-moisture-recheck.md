# Window-start moisture re-check — design note

Status: **spec** (2026-09-18). Captures the problem, the real hardware run that
exposed it, the options considered, and the chosen design.

## Background: plan-time is hours before watering-time

A nightly run is planned when the trigger fires (~23:00) but does not water until
the pre-dawn window (often 05:00–06:00). `_plan_and_run` builds the plan, then
`task.sleep(wait_s)` sleeps until the window start, then opens valves.

Every moisture-derived decision is frozen at plan time:

- **Which zones water** — a *deficit* zone triggers because its dominant moisture
  is below its drought target floor (`evaluate_zone`).
- **Whether a zone gets a calibration probe** — `should_probe` returns True only
  while the zone is calibrating/recalibrating and `dominant_now <
  probe_headroom_ceiling` (default 85).

Both read `dominant_by_zone`, captured once in `_plan_context`. Nothing re-reads
moisture after the multi-hour sleep.

There is already a precedent for re-checking a plan-time decision at window
start: after the sleep, `_plan_and_run` re-runs `_rain_skip_check` because "the
plan was built at 23:00 but watering starts hours later, and the forecast
refreshes every 15 minutes." The **moisture** decision has no equivalent.

## Problem — rain that lands in the gap makes the plan water wet soil

GeoDrops dominant-moisture sensors report on the device's own cadence (hours
between reports), so a moisture change during the plan→window gap is often not
visible at plan time but is visible by window start.

### Hardware evidence — night of 2026-09-17 → 06:06 2026-09-18 (Right Wall)

~5 mm of rain fell 20:00–22:35. The dominant sensor lagged it:

| Time (EDT) | Right Wall dominant | Note |
| --- | --- | --- |
| 20:35 | 83.8 | last report before the plan |
| **23:00** | *(still 83.8 — stale)* | plan built; 83.8 < 85 ceiling → **probe planned** |
| 23:40 | 96.1 | rain finally registers |
| 06:06 | ~92.5 | **probe executes on saturated soil** |

The plan saw 83.8 (just under the 85 probe ceiling) and scheduled a 20-min
calibration probe. It executed at 06:06 when the zone was actually ~92.5 —
saturated, above both the 85 probe ceiling and the 65 floor.

Result: 20 min watered on already-wet soil, plus a needless Rachio cycle. The
calibration **model was not corrupted** — `classify` correctly rejected the
observation as `saturated` (`n_obs` stayed 0). The only cost is the wasted
watering.

The rain-forecast re-check did *not* fire that night, correctly: by 23:00 the
forecast had collapsed (95%→35%, 4.57 mm→1.02 mm, below the 1.27 mm threshold).
The rain had already fallen. The gap the forecast guard cannot close is *actual*
water that landed and only shows up in the soil sensor later.

## Chosen design — re-read live dominant at window start, drop stale-wet zones

Mirror the rain re-check. Nightly only (`run_now` does not sleep, so has no
staleness). Inside the existing `if wait:` block, after the rain re-check passes
and before valves open:

1. For each zone in `the_plan.watered`, re-read live dominant moisture with the
   same `sensors.read_zone(zone, _read_zone_signals(zone))` call `_plan_context`
   uses.
2. Decide keep/drop per zone with a **pure helper** (`revalidate_zone`, lives in
   a lib module so it is Windows-unit-testable):
   - **probe zone** (`dosing_sources[k] == "probe"`) → drop if fresh
     `dominant >= probe_headroom_ceiling`.
   - **deficit zone** → drop if fresh `dominant >= floor` (the trigger reason is
     gone).
   - **sensor offline at re-check** → keep (fail-open: cannot prove it is wet;
     matches plan-time, which already qualified the sensor).
   - moisture rose but is still below the line → keep, water the full planned
     dose. Re-dosing is deliberately out of scope.
3. If any zone drops: mark it `uncompleted` with reason `"moisture-risen"`, then
   rebuild the plan from the survivors:
   `plan.build_plan(survivors, filtered_minutes, geo, adjacency, cap, tun)`
   (`build_plan` is pure and takes exactly these inputs — this is a re-call, not
   a re-plan; doses and probe sizes are unchanged). If **no** survivors remain,
   take the existing no-water path (publish `last_nightly`, status `skipped`,
   report, return) exactly like a rain skip.

### The keep/drop rule (pure)

```
revalidate_zone(online, dominant_now, dosing_source, floor, ceiling) -> str | None
    returns a drop reason, or None to keep
    not online                              -> None            (fail-open)
    dosing_source == "probe":
        dominant_now >= ceiling             -> "saturated"
        else                                -> None
    else (deficit):
        dominant_now >= floor               -> "above-floor"
        else                                -> None
```

The app collapses any non-None reason into the single `uncompleted` label
`"moisture-risen"` for the recap; the specific reason is logged for diagnostics.

## Supporting changes

- `_plan_context` must expose per-zone floors in its returned `ctx` (today
  `targets` is local). Add a `floors` map (`{key: targets[key].floor}`) to the
  return so the window-start check has each zone's floor without re-deriving the
  drought profile. Everything else it needs (`dosing_sources`, `cfg.zones` for
  `geography`/`adjacency`/`dominant_sensor`, `tun.probe_headroom_ceiling`) is
  already available.
- `last_nightly` gains a `window_start_dropped` field (`{zone: {reason, dominant}}`)
  so a re-check that fired is visible after the fact. Empty/absent when nothing
  dropped.

## Testing

- **Unit (pure `revalidate_zone`)**: probe above/below ceiling; deficit
  above/below floor; offline fail-open; boundary equality (`==` drops).
- The rebuild path is already exercised by existing `build_plan` tests.
- App wiring (`_plan_and_run`) stays `py_compile`-only, per repo practice for
  pyscript-primitive code.

## Out of scope

- Re-dosing a zone whose moisture rose but stayed below the line (partial rain).
- Any change to how calibration observations are accepted/rejected — the settle
  poll already handles saturation correctly.
- `run_now` (no sleep, no staleness).
