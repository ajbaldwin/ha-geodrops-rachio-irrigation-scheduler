# Calibration peak + retention (hybrid efficacy) — design

Status: **implemented 2026-09-18** (see docs/superpowers/plans/2026-09-18-calibration-peak-retention.md). Extends
`calibration-settle-timing.md` (the freshness-gated settle poll shipped in
v0.8.4). This note adds *what* the settle poll measures; that note fixed *when*.

## Problem (Q4 from the settle-timing note, now with data)

The settle model learns `efficacy` = moisture points gained per minute of
watering, from a single "settled" reading taken at `run_end + settle_hours`.
Even with the v0.8.4 freshness gate (which correctly waits for a genuine
post-settle sample), a **fast-draining zone** defeats a single settled read:
the soil peaks soon after watering, then drains before the settle window
opens, and the sparse GeoDrops sensor (~4h cadence) catches the decay at a
different point every night.

Reconstructing Right Wall's last three probes from the recorder (2026-09-17)
made this concrete:

| Probe | min | pre | peak | eff(peak) | first-fresh ≥+4h | eff(settled) | plateau | eff(plateau) |
|---|---|---|---|---|---|---|---|---|
| 09-15 | 20 | 74.4 | 87.4 | 0.65 | 87.4 | 0.65 | 85.9 | 0.57 |
| 09-16 | 20 | 76.0 | 86.2 | 0.51 | 79.6 | 0.18 | 79.4 | 0.17 |
| 09-17 | 30 | 79.0 | 91.9 | 0.43 | 82.3 | 0.11 | 82.3 | 0.11 |

The **peak** rate is consistent (0.43–0.65); the **settled** rate is noisy
(0.11–0.65), scrambled by drainage and sensor timing. A model that learns from
the settled read cannot converge for such a zone; the stored model had drifted
to `recent=[0.015, 0.075]` — corrupt, an order of magnitude low.

## Decision: hybrid efficacy

Learn `efficacy` from the **peak** rise (stable → the zone can converge), and
separately track a **retention factor** `r = retained_rise / peak_rise`
(0–1), EWMA-smoothed across nights. Dose to a *retained* target using the
**retained rate `efficacy × r`**. This decouples convergence (stable peak
rate) from the noisy retained signal (a slowly-moving correction), so the zone
converges *and* dosing still targets lasting moisture.

`efficacy × r` equals the instantaneous settled rate, but smoothing peak and
`r` on their own EWMAs — and gating convergence on peak only — is what keeps
one night's drainage noise from permanently blocking calibration.

## Data model

**Pending observation** (`irrigation_pending_obs.json`) gains accumulator
fields, written on each poll and persisted (so accumulation survives a
restart):

- `peak: float | None` — max genuine reading seen since `run_end` (init null).
- `retained: float | None` — latest genuine reading at/after
  `run_end + settle_hours` (init null).
- `last_seen_updated: str | None` — the `last_updated` ISO of the last sample
  counted, so a stale 15-minute MQTT republish (same value, unchanged
  `last_updated`) is never double-counted.

Existing fields unchanged: `zone`, `pre_dominant`, `minutes`, `run_end_iso`.
The old `measure_at_iso` field is dropped — the finalize time is derived from
`run_end_iso + retain_hours` at poll time (see Timing).

**Efficacy store** (`irrigation_efficacy.json`) per-zone record gains:

- `retention: float | None` — the smoothed `r` (0–1), null until the first
  accepted obs. `efficacy` keeps its name but now holds the **peak** rate.

## Tunables (config.py)

- `retain_hours: float = 6.0` — when an observation is finalized (retained
  captured, obs accepted). Must exceed `settle_hours`.
- Kept: `settle_hours: float = 4.0` — floor before a sample counts as
  *retained* (peak accumulates from `run_end` regardless).
- Kept: `settle_max_wait_hours: float = 12.0` — grace past `retain_hours`
  before an obs with no genuine sample at all is expired/dropped.
- `retention_ewma_alpha` — reuse `calibration_ewma_alpha` (0.3); no new
  tunable (YAGNI).
- `retention_floor: float = 0.1` — lower clamp for `r` (see below).

No new on/off flag: the hybrid replaces the settled-only read *within* the
existing `self_calibration_enabled` beta. Revert is git.

## Timing: accumulate → finalize → expire

Each 30-minute poll, for each pending obs:

1. **Accumulate.** Read the dominant sensor's value + `last_updated`. If
   `last_updated` is newer than the obs's `last_seen_updated` (the existing
   freshness gate, per-obs now), it is a genuine new sample:
   - `peak = max(peak, value)` (from `run_end` on);
   - if `now ≥ run_end + settle_hours`, `retained = value`;
   - set `last_seen_updated = last_updated`.
   Re-persist the obs.
2. **Decide** via the pure `settle_decision` (renamed/extended — see below),
   which returns:
   - `"accumulate"` — `now < run_end + retain_hours`: keep the obs pending.
   - `"finalize"` — `now ≥ run_end + retain_hours` and `peak` is set: compute
     and accept (below), remove from pending.
   - `"expired"` — `now ≥ run_end + retain_hours + settle_max_wait_hours` and
     `peak` is still null (no genuine sample ever arrived): drop the obs, no
     model change (unchanged inconclusive semantics).

Peak accumulates from `run_end` because the spike often lands before
`settle_hours` (09-17 peaked at ~3h < 4h).

## Efficacy, retention, convergence

At **finalize**, with `pre = pre_dominant`, `min = minutes`:

- `peak_rise = peak - pre`; `efficacy_obs = peak_rise / min`.
- `classify` keys off the **peak**: `no_rise` when `peak_rise ≤ 0`;
  `saturated` when `peak ≥ saturation_reject` (95); `rain`/`training`/
  `unavailable` as today. (Peak is the correct signal for saturation — it is
  the maximum the soil reached.)
- On accept (`"ok"`):
  - `efficacy` (peak rate): EWMA-blended as today via `update_efficacy`.
  - `recent` / `converged` / `miss_streak`: computed on **peak** efficacy —
    unchanged logic, stable input.
  - `retained_rise = retained - pre`; `r_obs = retained_rise / peak_rise`,
    clamped to `[retention_floor, 1.0]`. If `retained` is null (no post-
    settle sample before finalize) `r_obs` is skipped and `retention` is left
    as-is (peak still learned).
  - `retention`: EWMA-blended `r` (bootstrap to `r_obs` when null).

`r` never affects convergence or `miss_streak` — a noisy retention reading
can't stall calibration.

## Dosing

Converged dosing targets *retained* moisture using the **retained rate
`efficacy × retention`**: a deficit of `D` points needs
`D / (efficacy × retention)` minutes. Wherever converged dosing currently
derives runtime from `efficacy` (via `efficacy_to_span` / the span path in
`dosing.py`), substitute the retained rate. When `retention` is null
(pre-convergence) it defaults to `1.0` — harmless, because efficacy-based
dosing only applies once the zone has converged, by which point `r` has
several smoothed samples; calibration nights use fixed probe doses, not the
efficacy path. *(The exact dosing seam is confirmed in the implementation
plan against `dosing.py`.)*

`cap_for_saturation` continues to use `efficacy` (peak) to keep a probe's
**peak** below `saturation_reject` — correct, since peak is what saturates.

## Testing

Pure lib (`calibration.py`, unit-tested):

- `settle_decision` (extended): accumulate / finalize / expire across the
  timing boundaries and the freshness (`last_seen_updated`) gate.
- An accumulate helper: peak = running max; retained set only at/after
  `settle_hours`; stale republish (same `last_updated`) ignored.
- Efficacy + retention at finalize: peak rate, `r` computation, the
  `[retention_floor, 1.0]` clamp, the `retained ≤ pre` (fully drained) and
  `retained is null` edges.
- Convergence unaffected: still keyed on peak-efficacy `recent`.

App layer (`irrigation/__init__.py`, `py_compile` + read-through): the poll
now writes accumulator fields back to the pending obs each cycle; finalize
runs the accept branch; dosing reads `retention`.

## Safety notes

- Live-watering calibration: accept/reject stays isolated from planning/
  preview; app layer is `py_compile`-only; lib logic is unit-tested.
- The accumulator only ever *raises* `peak` and advances `retained`/
  `last_seen_updated`; a bad single read cannot lower a learned value, and an
  expired obs still mutates nothing.
- Under-dose vs saturation trade-off is resolved by `efficacy × r`: dosing
  aims at the retained target, and `cap_for_saturation` still bounds the peak.

## Open items (resolve in the plan)

- The retained span is stored in `span_pts` at finalize (`efficacy × r × base`,
  clamped), so `dosing.dose_zone` and `_plan_context` are unchanged — the
  existing learned-span path (the `eff["span_pts"]` branch in
  `irrigation/__init__.py`) reads it directly.
- Whether `retained` should be the last sample before finalize or a small
  median of post-`settle_hours` samples (extra state vs. noise); default to
  last-sample, revisit if `r` proves jumpy.
- `retain_hours = 6.0` sizing against more zones once data exists.
