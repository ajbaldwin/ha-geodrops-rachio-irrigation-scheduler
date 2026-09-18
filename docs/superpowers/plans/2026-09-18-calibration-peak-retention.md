# Calibration Peak + Retention (Hybrid Efficacy) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Learn calibration `efficacy` from the peak moisture rise (stable → convergent) plus a smoothed retention factor `r`, and dose to a retained target via `efficacy × r`, so a fast-draining zone can calibrate.

**Architecture:** The 30-minute settle poll accumulates the running peak and latest retained reading into the persisted pending obs (freshness-gated), then finalizes at `run_end + retain_hours`: efficacy from the peak, `r = retained_rise/peak_rise` (EWMA-smoothed), and a stored **retained span** (`efficacy × r × base`) so dosing needs no change. Pure logic lives in `calibration.py` (unit-tested); the accumulate/finalize wiring is in the app's `_settle_and_learn` / `_append_pending_obs` (`py_compile` only).

**Tech Stack:** Python 3.13 (pure lib) + pyscript (app). pytest for the lib.

**Spec:** `docs/design/calibration-peak-retention.md`

## Global Constraints

- **`irrigation_lib/` is PURE Python** run under pyscript's interpreter: no pyscript globals (`state`/`service`/`task`/`log`), no file I/O, no generator expressions. `calibration.py` already has `import datetime as dt` — reuse it.
- **App layer (`irrigation/__init__.py`) is `py_compile`-only** — pyscript primitives; verify with `python -m py_compile irrigation/__init__.py`. Not unit-testable.
- **All datetimes tz-aware**; only aware/aware comparisons. `now`/`run_end`/`last_updated`/`last_seen` are aware.
- **Live-watering calibration**: accept/reject stays isolated from planning/preview. An `expired`/inconclusive obs mutates nothing and never rejects.
- **`efficacy` now means the PEAK rate.** `retention` (`r`, 0–1) is a new per-zone field. `span_pts` stored at finalize is the **retained span** = `efficacy × r × base`, clamped `[span_min, span_max]` — this is why dosing (`dosing.dose_zone`, `_plan_context`) needs no change.
- **No new on/off flag** — hybrid replaces the settled-only read within the existing `self_calibration_enabled` beta.
- **Windows test command:** `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/`.
- **No release** in this work (batched per release-batching-preference). User merges PRs.

---

### Task 1: Pure lib — tunables, decision, accumulator, retention helpers

Rewrites `settle_decision` for the accumulate model and adds the pure helpers, plus two tunables. All unit-tested. This replaces the settle-timing `settle_decision` signature, so its existing tests are updated here too.

**Files:**
- Modify: `irrigation_lib/config.py` (add two `Tunables` fields)
- Modify: `irrigation_lib/calibration.py` (rewrite `settle_decision`; add `accumulate_sample`, `retention_factor`, `retained_span`, `ewma`)
- Test: `tests/irrigation/test_calibration.py` (replace settle_decision tests; add new)
- Test: `tests/irrigation/test_config.py` (assert new defaults)

**Interfaces:**
- Consumes: existing `Tunables` (`settle_hours`, `settle_max_wait_hours`, `span_min`, `span_max`, `calibration_ewma_alpha`).
- Produces:
  - `Tunables.retain_hours: float = 6.0`, `Tunables.retention_floor: float = 0.1`
  - `settle_decision(now, run_end, retain_hours, max_wait_hours, has_sample) -> str` in `{"accumulate","finalize","expired"}`
  - `accumulate_sample(peak, retained, last_seen, value, last_updated, now, run_end, settle_hours) -> (peak, retained, last_seen, changed)` — `peak`/`retained` floats-or-None; `last_seen`/`last_updated`/`now`/`run_end` aware datetimes-or-None; `changed` bool
  - `retention_factor(pre, peak, retained, floor) -> float | None`
  - `retained_span(efficacy, retention, base, span_min, span_max) -> float`
  - `ewma(prev, value, alpha) -> float`

- [ ] **Step 1: Write the failing tests**

Add to `tests/irrigation/test_calibration.py` (and DELETE the old `test_settle_decision_*` tests from the settle-timing work — the signature changed):

```python
import datetime as dt

def _t(h):
    return dt.datetime(2026, 9, 17, 6, 0, tzinfo=dt.timezone.utc) + dt.timedelta(hours=h)

# --- settle_decision (accumulate model) ---
def test_settle_decision_accumulates_before_finalize():
    # run_end=_t(0), retain_hours=6 -> finalize at _t(6)
    assert calibration.settle_decision(_t(3), _t(0), 6.0, 12.0, True) == "accumulate"
    assert calibration.settle_decision(_t(3), _t(0), 6.0, 12.0, False) == "accumulate"

def test_settle_decision_finalizes_when_ripe_with_sample():
    assert calibration.settle_decision(_t(6), _t(0), 6.0, 12.0, True) == "finalize"   # boundary
    assert calibration.settle_decision(_t(9), _t(0), 6.0, 12.0, True) == "finalize"

def test_settle_decision_waits_past_finalize_without_sample_then_expires():
    # past finalize (_t6) but within grace (+12h -> _t18): still accumulate, hoping for a sample
    assert calibration.settle_decision(_t(10), _t(0), 6.0, 12.0, False) == "accumulate"
    # at/after finalize+max_wait (_t18) with no sample ever -> expired
    assert calibration.settle_decision(_t(18), _t(0), 6.0, 12.0, False) == "expired"   # boundary
    assert calibration.settle_decision(_t(20), _t(0), 6.0, 12.0, False) == "expired"

def test_settle_decision_sample_finalizes_even_past_grace():
    assert calibration.settle_decision(_t(20), _t(0), 6.0, 12.0, True) == "finalize"

# --- accumulate_sample ---
def test_accumulate_first_sample_sets_peak_not_retained_before_settle():
    # now=_t(1) < run_end+settle(4) -> retained stays None, peak set
    peak, ret, seen, ch = calibration.accumulate_sample(
        None, None, None, 85.0, _t(1), _t(1), _t(0), 4.0)
    assert (peak, ret, ch) == (85.0, None, True) and seen == _t(1)

def test_accumulate_running_max_and_retained_after_settle():
    # second sample lower value but after settle -> peak holds, retained updates
    peak, ret, seen, ch = calibration.accumulate_sample(
        85.0, None, _t(1), 82.0, _t(5), _t(5), _t(0), 4.0)
    assert (peak, ret, ch) == (85.0, 82.0, True) and seen == _t(5)
    # a higher later sample raises peak and updates retained
    peak, ret, seen, ch = calibration.accumulate_sample(
        85.0, 82.0, _t(5), 88.0, _t(6), _t(6), _t(0), 4.0)
    assert (peak, ret, ch) == (88.0, 88.0, True)

def test_accumulate_ignores_stale_republish_and_missing():
    # last_updated not newer than last_seen -> no change (stale MQTT republish)
    assert calibration.accumulate_sample(85.0, 82.0, _t(5), 99.0, _t(5), _t(6), _t(0), 4.0) == (85.0, 82.0, _t(5), False)
    # None value/last_updated -> no change
    assert calibration.accumulate_sample(85.0, 82.0, _t(5), None, _t(6), _t(6), _t(0), 4.0) == (85.0, 82.0, _t(5), False)

# --- retention_factor ---
def test_retention_factor_and_clamps():
    assert calibration.retention_factor(80.0, 92.0, 84.0, 0.1) == pytest.approx((84-80)/(92-80))  # 0.333
    assert calibration.retention_factor(80.0, 92.0, 120.0, 0.1) == 1.0        # clamp high
    assert calibration.retention_factor(80.0, 92.0, 80.0, 0.1) == 0.1         # fully drained -> floor
    assert calibration.retention_factor(80.0, 80.0, 84.0, 0.1) is None        # peak<=pre -> None
    assert calibration.retention_factor(80.0, 92.0, None, 0.1) is None        # no retained -> None

# --- retained_span ---
def test_retained_span_applies_r_and_clamps():
    assert calibration.retained_span(0.5, 0.4, 60.0, 1.0, 60.0) == pytest.approx(12.0)   # .5*.4*60
    assert calibration.retained_span(0.5, None, 60.0, 1.0, 60.0) == pytest.approx(30.0)  # r None -> 1.0
    assert calibration.retained_span(2.0, 1.0, 60.0, 1.0, 60.0) == 60.0                  # clamp high
    assert calibration.retained_span(0.001, 0.1, 60.0, 1.0, 60.0) == 1.0                 # clamp low

# --- ewma ---
def test_ewma():
    assert calibration.ewma(None, 0.4, 0.3) == 0.4               # bootstrap
    assert calibration.ewma(0.5, 0.4, 0.3) == pytest.approx(0.3*0.4 + 0.7*0.5)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/irrigation/test_calibration.py -k "settle_decision or accumulate or retention or retained_span or ewma" -v`
Expected: FAIL (new functions/signature not present).

- [ ] **Step 3: Add the tunables**

In `irrigation_lib/config.py`, `Tunables`, immediately after `settle_max_wait_hours`:

```python
    # When a calibration observation is finalized (retained reading captured,
    # obs accepted). Must exceed settle_hours: peak accumulates from run_end, but
    # the retained reading is only taken from settle_hours on, and finalize waits
    # until the soil has settled (retain_hours).
    retain_hours: float = 6.0
    # Lower clamp for the retention factor r (retained_rise / peak_rise). Keeps a
    # fully-drained night from zeroing the dosing span.
    retention_floor: float = 0.1
```

- [ ] **Step 4: Rewrite `settle_decision` and add helpers**

In `irrigation_lib/calibration.py`, REPLACE the existing `settle_decision` (from the settle-timing work) with the accumulate-model version, and add the helpers (reuse the module's `import datetime as dt`):

```python
def settle_decision(now, run_end, retain_hours, max_wait_hours, has_sample) -> str:
    """Decide how to handle one pending calibration obs at this poll.

    Accumulate model: the poll folds each genuine reading into the obs (peak +
    retained) until finalize. All datetimes tz-aware.

    Returns:
      "accumulate" - before finalize (now < run_end + retain_hours), OR past it
                     with no genuine sample yet but still within the grace
                     window. Keep the obs pending and keep collecting.
      "finalize"   - now >= run_end + retain_hours AND at least one genuine
                     sample was captured (has_sample). Compute + accept.
      "expired"    - past finalize + max_wait_hours with no sample ever. Drop
                     the obs (never reject, never touch the model).

    A captured sample always finalizes once ripe, even past the grace window.
    """
    finalize_at = run_end + dt.timedelta(hours=retain_hours)
    if now < finalize_at:
        return "accumulate"
    if has_sample:
        return "finalize"
    if now >= finalize_at + dt.timedelta(hours=max_wait_hours):
        return "expired"
    return "accumulate"


def accumulate_sample(peak, retained, last_seen, value, last_updated,
                      now, run_end, settle_hours):
    """Fold one candidate sensor reading into an obs accumulator.

    Counts only a genuinely new report (last_updated strictly newer than
    last_seen — a stale MQTT republish carries the same last_updated). peak is
    the running max from run_end on; retained is the latest reading at/after
    run_end + settle_hours. Returns (peak, retained, last_seen, changed).
    """
    if value is None or last_updated is None:
        return peak, retained, last_seen, False
    if last_seen is not None and last_updated <= last_seen:
        return peak, retained, last_seen, False
    new_peak = value if peak is None else max(peak, value)
    new_ret = retained
    if now >= run_end + dt.timedelta(hours=settle_hours):
        new_ret = value
    return new_peak, new_ret, last_updated, True


def retention_factor(pre, peak, retained, floor):
    """r = (retained - pre) / (peak - pre), clamped to [floor, 1.0].

    None when it cannot be computed: no retained sample, or peak did not rise
    above pre (peak_rise <= 0).
    """
    if retained is None:
        return None
    peak_rise = peak - pre
    if peak_rise <= 0:
        return None
    r = (retained - pre) / peak_rise
    return max(floor, min(1.0, r))


def retained_span(efficacy, retention, base, span_min, span_max):
    """Dominant points a full refill RETAINS = efficacy(peak) * r * base, clamped.

    retention None (pre-retention) is treated as 1.0 (peak span).
    """
    r = 1.0 if retention is None else retention
    span = efficacy * r * base
    return max(span_min, min(span_max, span))


def ewma(prev, value, alpha):
    """Exponentially-weighted blend; bootstraps to value when prev is None."""
    if prev is None:
        return value
    return alpha * value + (1 - alpha) * prev
```

- [ ] **Step 5: Add the config default tests**

Add to `tests/irrigation/test_config.py`:

```python
def test_retain_and_retention_floor_defaults():
    from irrigation_lib.config import Tunables
    t = Tunables()
    assert t.retain_hours == 6.0
    assert t.retention_floor == 0.1
    assert t.retain_hours > t.settle_hours   # finalize must be after the retained floor
```

- [ ] **Step 6: Run the full lib suite**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/`
Expected: PASS (new tests + all prior; the old settle_decision tests are gone).

- [ ] **Step 7: Commit**

```bash
git add irrigation_lib/calibration.py irrigation_lib/config.py tests/irrigation/test_calibration.py tests/irrigation/test_config.py
git commit -m "feat(calibration): peak/retention pure helpers + accumulate-model settle_decision"
```

---

### Task 2: App wiring — accumulate on the poll, finalize with peak + retention

Extends the pending-obs schema and rewires `_settle_and_learn` to accumulate each poll and finalize with peak efficacy + retained span. App layer — `py_compile` only.

**Files:**
- Modify: `irrigation/__init__.py` — the `_append_pending_obs` producer block (~line 1917-1928) and `_settle_and_learn` (~line 2290+).

**Interfaces:**
- Consumes: `calibration.settle_decision`, `calibration.accumulate_sample`, `calibration.retention_factor`, `calibration.retained_span`, `calibration.ewma` (Task 1); existing `_sensor_last_updated`, `_read_zone_signals`, `sensors.read_zone`, `calibration.classify`, `calibration.update_efficacy`, `calibration.converged`, `calibration.next_state`.
- Produces: no new callable surface.

- [ ] **Step 1: Extend the pending-obs producer**

In `_append_pending_obs`'s caller (the `if tun.self_calibration_enabled and watered:` block, ~line 1917), change the measure time to `retain_hours` and add accumulator fields. Replace:

```python
            measure_at = finished + dt.timedelta(hours=tun.settle_hours)
            pend = []
            for k in watered:
                pend.append({
                    "zone": k,
                    "pre_dominant": ctx["dominant_by_zone"].get(k),
                    "minutes": delivered.get(k, 0),
                    "run_end_iso": finished.isoformat(),
                    "measure_at_iso": measure_at.isoformat(),
                })
```

with:

```python
            measure_at = finished + dt.timedelta(hours=tun.retain_hours)
            pend = []
            for k in watered:
                pend.append({
                    "zone": k,
                    "pre_dominant": ctx["dominant_by_zone"].get(k),
                    "minutes": delivered.get(k, 0),
                    "run_end_iso": finished.isoformat(),
                    "measure_at_iso": measure_at.isoformat(),
                    # Accumulator (peak + retained) filled by the settle poll.
                    "peak": None,
                    "retained": None,
                    "last_seen_updated": None,
                })
```

- [ ] **Step 2: Rewrite the `_settle_and_learn` per-obs loop**

Locate `_settle_and_learn` (the `@time_trigger("cron(*/30 * * * *)")` function). Keep the head unchanged through `now = dt.datetime.now().astimezone()` and the lazy `api_runtimes = None` / `rained = None` / `remaining = []` / `learned = 0` / `dropped = 0` initialisation. Replace the per-rec body (from `for rec in pending:` through the end of the loop) with:

```python
    for rec in pending:
        try:
            run_end = dt.datetime.fromisoformat(rec["run_end_iso"])
        except (KeyError, ValueError):
            continue
        zone = rec.get("zone")
        zone_cfg = cfg.zones.get(zone)
        if zone_cfg is None:
            continue  # obs for a zone no longer configured: drop it
        # --- accumulate this poll's reading into the obs (freshness-gated) ---
        signals = _read_zone_signals(zone_cfg)
        reading = sensors.read_zone(zone_cfg, signals)
        value = reading.dominant if reading.online else None
        last_updated = _sensor_last_updated(zone_cfg.dominant_sensor)
        try:
            last_seen = (dt.datetime.fromisoformat(rec["last_seen_updated"])
                         if rec.get("last_seen_updated") else None)
        except ValueError:
            last_seen = None
        peak, retained, last_seen, _ch = calibration.accumulate_sample(
            rec.get("peak"), rec.get("retained"), last_seen,
            value, last_updated, now, run_end, tun.settle_hours)
        rec["peak"] = peak
        rec["retained"] = retained
        rec["last_seen_updated"] = last_seen.isoformat() if last_seen else None
        # --- decide ---
        decision = calibration.settle_decision(
            now, run_end, tun.retain_hours, tun.settle_max_wait_hours,
            peak is not None)
        if decision == "accumulate":
            remaining.append(rec)
            continue
        if decision == "expired":
            dropped += 1
            continue  # inconclusive: drop, never reject, no model change
        # decision == "finalize"
        if api_runtimes is None:
            api_runtimes = get_runtimes()
            rained = _rained_since_run(cfg.bindings, tun)
        try:
            pre = rec.get("pre_dominant")
            minutes = rec.get("minutes")
            if pre is None or not minutes:
                continue
            quals = [(q or "").strip() for q in signals.qualities]
            qcn_training = (len(quals) == 3 and quals[0] == "Training"
                            and quals[1] == "Training" and quals[2] == "Training")
            # settled_dominant = PEAK: classify's no_rise (rise<=0) and saturated
            # (>=95) both key off the max the soil reached.
            obs = calibration.Observation(
                zone=zone, pre_dominant=pre, minutes=minutes,
                settled_dominant=peak, qcn_training=qcn_training,
                rained=rained, sensor_ok=True,
            )
            reason = calibration.classify(obs, tun)
            zrec = store.get(zone) or {}
            if reason == "ok":
                prev = zrec.get("efficacy")
                eff = calibration.update_efficacy(prev, obs, tun)   # peak rate
                eff_obs = (peak - pre) / minutes
                recent = (zrec.get("recent") or []) + [eff_obs]
                if len(recent) > tun.convergence_samples:
                    recent = recent[-tun.convergence_samples:]
                r_obs = calibration.retention_factor(pre, peak, retained, tun.retention_floor)
                prev_r = zrec.get("retention")
                r = prev_r if r_obs is None else calibration.ewma(prev_r, r_obs, tun.calibration_ewma_alpha)
                base = api_runtimes.get(zone_cfg.rachio_zone_id) or zone_cfg.runtime_minutes
                span = calibration.retained_span(eff, r, base, tun.span_min, tun.span_max)
                miss = zrec.get("miss_streak") or 0
                if prev and prev > 0 and abs(eff_obs - prev) / prev > tun.convergence_tolerance:
                    miss = miss + 1
                else:
                    miss = 0
                conv = calibration.converged(recent, tun)
                state_name = calibration.next_state(
                    zrec.get("state", "calibrating"), conv, False, miss, tun)
                zrec = {
                    "state": state_name, "efficacy": eff, "retention": r,
                    "span_pts": span, "recent": recent,
                    "n_obs": (zrec.get("n_obs") or 0) + 1,
                    "prior_minutes": minutes, "last_rise": (peak - pre),
                    "miss_streak": miss,
                    "last_updated": now.isoformat(), "last_reject_reason": None,
                }
                learned += 1
            elif reason == "training":
                zrec["state"] = calibration.next_state(
                    zrec.get("state", "calibrating"), False, True, 0, tun)
                zrec["efficacy"] = None
                zrec["span_pts"] = 0
                zrec["recent"] = []
                zrec["miss_streak"] = 0
                zrec["last_reject_reason"] = reason
            else:
                zrec = calibration.apply_reject(zrec, reason, minutes, peak - pre, tun)
            store[zone] = zrec
        except Exception as err:
            log.warning(f"irrigation: settle-and-learn skipped a record ({err})")
            continue
    task.executor(_write_json_atomic, PENDING_OBS_PATH, remaining)
    _write_efficacy_store(store)
    if learned or dropped:
        log.info(
            f"irrigation: settle-and-learn updated {learned} zone(s), "
            f"dropped {dropped} inconclusive")
```

- [ ] **Step 3: Verify it compiles**

Run: `python -m py_compile irrigation/__init__.py`
Expected: no output (exit 0).

- [ ] **Step 4: Full lib suite still green (no regression)**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/`
Expected: PASS.

- [ ] **Step 5: Read-through review of `_settle_and_learn`**

Re-read the whole function and confirm:
- Every poll re-persists the accumulator: `accumulate` appends the mutated `rec` to `remaining`; `finalize`/`expired` do not.
- `get_runtimes()`/`_rained_since_run` run only when an obs reaches `finalize`.
- Finalize uses `peak` as the settled value everywhere (classify, update_efficacy, eff_obs, last_rise, apply_reject rise); `retained` feeds only `retention_factor`.
- `span_pts` stored is the retained span; `retention` persisted.
- `expired` and `accumulate` never mutate `store`.
- All datetime comparisons aware/aware.

- [ ] **Step 6: Commit**

```bash
git add irrigation/__init__.py
git commit -m "feat(calibration): accumulate peak+retained on the settle poll; finalize with retained span"
```

---

### Task 3: Mark the design implemented

**Files:**
- Modify: `docs/design/calibration-peak-retention.md`

**Interfaces:** none.

- [ ] **Step 1: Update status + resolve the dosing open item**

At the top, change `Status: **approved 2026-09-18, not yet implemented.**` to `Status: **implemented 2026-09-18** (see docs/superpowers/plans/2026-09-18-calibration-peak-retention.md).`

In "Open items", replace the dosing-seam bullet with the resolution: the retained span is stored in `span_pts` at finalize (`efficacy × r × base`, clamped), so `dosing.dose_zone` and `_plan_context` are unchanged — the learned-span path (`irrigation/__init__.py`, the `eff["span_pts"]` branch) already reads it. Keep the remaining two open items (retained last-sample-vs-median; `retain_hours` sizing).

- [ ] **Step 2: Commit**

```bash
git add docs/design/calibration-peak-retention.md
git commit -m "docs(calibration): mark peak-retention design implemented; resolve dosing seam"
```

---

## Post-merge / deploy notes

- Scheduler-only. After merge + a scheduler release, re-vendor into the wrapper (brain bump) and cut a wrapper release — same pipeline as v0.9.9. No wrapper Python change (no new surfaced state; `retention`/retained-span live inside the efficacy record the wrapper already reads).
- On the box, Right Wall (freshly reset) will re-probe and now finalize on peak + retention.
- The wrapper's Calibration State label reads `n_obs` from the last_nightly snapshot; unchanged by this work.
