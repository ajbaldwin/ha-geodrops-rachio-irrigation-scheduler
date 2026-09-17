# Calibration settle timing — design note (unbuilt)

Status: **implemented 2026-09-17** (see `docs/superpowers/plans/2026-09-17-calibration-settle-timing.md`). Captures the problem, the
real hardware behaviour, the options considered, and resolutions to all feasibility questions.

## Background: how a calibration probe is accepted/rejected today

Self-calibration learns each zone's `efficacy` = moisture points gained per
minute of watering, by watering a measured amount and reading the moisture rise
once the soil has settled.

1. A nightly **run** that waters a calibrating zone appends a **pending
   observation** (`_append_pending_obs`) recording `pre_dominant`, `minutes`,
   `run_end_iso`, and `measure_at_iso = run_end + settle_hours` (default 4h).
2. `_settle_and_learn` runs on a **fixed `cron(0 9 * * *)`** (9am). For each
   pending obs where `now >= measure_at`, it reads the **current** dominant
   moisture *at pass time* as the "settled" value, then `classify` → **accept**
   (learn `efficacy = (settled - pre) / minutes`, advance convergence) or
   **`apply_reject`** (`no_rise` / `saturated` / `rain`). Obs not yet ripe are
   kept for a later pass.

Accept/reject lives **only** here — never in preview/`_plan_context` (those only
*plan* probes via `should_probe`). A machine restart never triggers it, and it
is clock-gated by `measure_at`, so startup cannot prematurely accept/reject.

## Problem 1 — fixed 9am reads the "settled" value late

`measure_at` only gates *ripeness*; the reading is taken at 9am regardless of
when the run finished. A run finishing at 02:00 (settle 06:00) is measured at
09:00 — **3h late**. In that window the soil keeps drying (ET/drainage), so
`settled - pre` is understated → **efficacy biased low, real rises misread as
`no_rise`**. A run finishing after ~05:00 isn't ripe by 9am and waits until the
*next* 9am — ~24h late.

## Problem 2 (the bigger one) — GeoDrops sensors are sparse and lossy

The dominant-moisture sensors report on the **device's** cadence, not HA's.
Observed on the live box (`sensor.outside_front_stairs_moisture_sensor_dominant_moisture`,
2026-09-17 UTC):

```
00:40 → 84.2   [no report ~04:40]   06:40 → 81.1   08:40 → 80.8
09:40 → 80.1   11:40 → 78.5   12:40 → 78.4
```

- Nominal ~4h polling, "check in earlier if justified", and **missed check-ins
  are common** — note the **6h gap** 00:40→06:40.
- **No device sample-timestamp attribute** — the entity carries only
  `state_class` / `unit_of_measurement` / `device_class` / `friendly_name`. The
  only freshness signal available is HA's **`last_updated`**, which tracks
  reports acceptably here because the value drifts every poll.

Today the settle pass reads the **current** value via `state.get` and treats
`sensor_ok = reading.online` as sufficient — but "online" is true even when the
value is **hours stale**. So if a run finishes at 01:00 (settle 05:00) and the
next report is 06:40, any read before 06:40 returns the **00:40 pre-watering
value** → `settled ≈ pre` → a genuine soak is classified **`no_rise`** and the
good probe is **wrongly rejected**. This corrupts the model — worse than being
merely late.

## Options considered

- **A — Frequent poll, measure at first-ripe.** Replace `cron(0 9 * * *)` with a
  ~30-min poll; process each obs at the first pass after it ripens. Bounds
  lateness to the interval, stays restart-robust (persisted pending-obs + cron),
  improves rain-confounder timing. *Vehicle for the fix, but insufficient alone —
  see the freshness gate below.*
- **B — Read the settled value from history at exactly `measure_at`.** Ruled
  **out** for sparse sensors: at `measure_at` there is often no sample, so a
  history lookup returns the stale pre-water value — exactly the bug.
- **C — Per-run scheduled measurement (`task.sleep` until `measure_at`).** Exact
  timing, but lost on restart (pyscript is stateless across restarts — the
  persisted-obs + cron is precisely the restart-robust workaround). Needs the
  poll as a fallback anyway.
- **D — Upstream early check-in (ideal accuracy).** If the GeoDrops integration
  can be asked to **read on demand**, fire it at `run_end + settle_hours` for a
  timely, accurate settled sample. Lives in the `ha-geodrops-integration` layer
  (BigQuery→MQTT / device API), not the scheduler — a separate, larger change.

## Recommendation

Build the **freshness-gated measurement on a frequent poll**, regardless of D:

1. **Poll cadence** (Option A): trigger `_settle_and_learn` every ~30 min instead
   of once at 9am.
2. **Freshness gate:** accept the settled reading only when the sensor has a
   report whose **`last_updated >= measure_at`** (i.e. `run_end + settle_hours`) —
   a genuine post-settle sample, not a stale pre-water value. Otherwise keep the
   obs pending. Missed check-ins fall out naturally (wait for the next real
   sample).
3. **Deadline:** if no fresh post-settle report arrives within
   `settle_hours + max_wait` (size `max_wait` past a typical gap — the observed
   worst case was ~6h, so consider +8–12h), classify **inconclusive** and
   **defer or drop the obs — never reject it as `no_rise`.** This is a **new
   classify outcome**; today `classify` distinguishes only online/offline, not
   stale/timed-out.

Then, if the integration supports it, layer **D** (early check-in trigger) on top
to shorten the wait and cut missed-checkin dependence.

## Open questions / feasibility (resolved)

1. **Is there an HA-triggerable "read now" for a GeoDrops sensor?**
   Resolved: no HA-triggerable read-now. `ha-geodrops-integration` is one-way BigQuery→MQTT; Option D dropped; fix is scheduler-only.

2. **pyscript access to `last_updated`:**
   Resolved: `state.get("<entity>.last_updated")` → tz-aware UTC datetime; use `last_updated` not `last_reported`; read in the app via `_sensor_last_updated`, not `sensors.read_zone`.

3. **Deadline sizing (`max_wait`):**
   Resolved: `settle_max_wait_hours = 12.0`.

4. **`settle_hours` vs sensor cadence:**
   Resolved: `settle_hours` kept as minimum redistribution time; effective rule = first genuine report ≥ `settle_hours`.

5. **New inconclusive/stale outcome routing:**
   Resolved: expired obs is dropped; no `miss_streak`/convergence/efficacy change.

**Poll cadence:** chosen as 30 min (`cron(*/30 * * * *)`).

## Safety notes (carry forward)

- This touches **live-watering calibration** — spec-first, and the app layer is
  `py_compile`-only (pyscript primitives); lib logic (`calibration.py`,
  `sensors.py`) is unit-testable.
- Accept/reject must remain isolated from planning/preview; the read-only target
  computation added in v0.8.3 (`_compute_target_floors`) is the model to follow
  for "safe to run anytime" code.
