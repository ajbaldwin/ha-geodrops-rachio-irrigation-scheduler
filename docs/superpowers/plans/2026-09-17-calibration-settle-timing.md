# Calibration Settle-Timing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the calibration settle pass from wrongly rejecting good probes when the GeoDrops moisture sensor reports late or misses a check-in, by measuring on a frequent poll gated on sensor freshness with an inconclusive deadline instead of a fixed 9am read.

**Architecture:** Replace `_settle_and_learn`'s fixed `cron(0 9 * * *)` with a 30-minute poll. A new pure timing function (`calibration.settle_decision`) decides, per pending observation, whether to **wait** (not ripe, or no genuine post-settle sample yet), **measure** (ripe *and* the dominant sensor has changed since `measure_at`), or **expire** (ripe, still no fresh sample by the deadline → drop the obs, never reject). Freshness comes from the dominant sensor's HA `last_updated` (bumps only on a value change → tracks real device check-ins), read in the app layer via `state.get("<entity>.last_updated")` and handed to the pure function. Accept/reject logic is unchanged.

**Tech Stack:** Python 3.13 (pure lib) + pyscript (app layer). pytest for the lib.

**Spec:** `docs/design/calibration-settle-timing.md` (this repo), with these resolutions from the 2026-09-17 handoff session:
- **Open Q1 (GeoDrops read-now):** NOT feasible. `ha-geodrops-integration` is a one-way BigQuery→MQTT daemon (no command topic / button / service); a forced re-sync only re-publishes the latest BigQuery row, and the device's ~4h cadence is upstream of HA. **Option D is dropped — the fix is scheduler-only.**
- **Open Q2 (pyscript `last_updated`):** confirmed. `state.get("sensor.x.last_updated")` returns a tz-aware **UTC datetime**. Use `last_updated`, **not `last_reported`** (the latter bumps on every 15-min MQTT republish and would defeat the gate). Freshness is read in the app, NOT surfaced through `sensors.read_zone` (keeps `read_zone` focused on value online/offline; the timing decision is its own pure function).
- **Open Q3 (deadline):** `settle_max_wait_hours = 12.0` (≈16h after run end; clears the observed 6h missed-checkin gap plus settle).
- **Open Q4 (settle model):** keep `settle_hours` as the *minimum redistribution time*; the freshness gate makes the effective rule "first genuine report ≥ settle_hours after run end". No separate change.
- **Open Q5 (inconclusive isolation):** an expired obs is **dropped** — removed from pending, with **no** change to `efficacy`, `state`, `recent`, `miss_streak`, or `last_reject_reason`. The zone re-probes on its next eligible night.
- **Poll cadence:** 30 min.

## Global Constraints

- **Pure lib only in `irrigation_lib/`** — no pyscript globals (`state`/`service`/`task`/`log`), no `open`/file I/O, no generator expressions, no `with` referencing enclosing locals. These run under pyscript's interpreter. (Copied verbatim from existing `config.py`/`calibration.py` header conventions.)
- **App layer (`irrigation/__init__.py`) is `py_compile`-only** — pyscript primitives; not unit-testable. Verify with `python -m py_compile`.
- **This touches LIVE-watering calibration.** Accept/reject must stay isolated from planning/preview. An inconclusive/expired outcome must never reject, never penalise `miss_streak`, never touch convergence.
- **All datetimes are tz-aware.** `now = dt.datetime.now().astimezone()` (local), `measure_at` is local-aware (written with `.astimezone()` in `_append_pending_obs`), `last_updated` is UTC-aware. Cross-zone aware comparisons are valid; never compare aware to naive.
- **Windows test command:** `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/` (the installed pytest-homeassistant-custom-component autoloads and imports Unix-only `fcntl`).
- **No release in this work.** Scheduler tags are batched and cut only when the user asks (release-batching-preference). The user merges PRs.

---

### Task 1: Pure timing decision + deadline tunable

Adds the freshness/deadline decision as a pure, unit-tested function and the tunable that sizes the deadline. No app or behavior wiring yet.

**Files:**
- Modify: `irrigation_lib/config.py` (add one field to `Tunables`)
- Modify: `irrigation_lib/calibration.py` (add `settle_decision`)
- Test: `tests/irrigation/test_calibration.py` (add cases)
- Test: `tests/irrigation/test_config.py` (assert the new default)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `Tunables.settle_max_wait_hours: float = 12.0`
  - `calibration.settle_decision(now, measure_at, last_updated, max_wait_hours) -> str`
    - `now`: tz-aware datetime (current poll time)
    - `measure_at`: tz-aware datetime (`run_end + settle_hours`)
    - `last_updated`: tz-aware datetime **or None** (dominant sensor's HA last_updated; None when missing/unreadable)
    - `max_wait_hours`: float
    - returns one of `"wait"`, `"measure"`, `"expired"`

- [ ] **Step 1: Write the failing tests**

Add to `tests/irrigation/test_calibration.py`:

```python
import datetime as dt


def _t(h):
    # tz-aware helper: a fixed base datetime plus h hours (UTC).
    base = dt.datetime(2026, 9, 17, 0, 0, tzinfo=dt.timezone.utc)
    return base + dt.timedelta(hours=h)


def test_settle_decision_waits_before_ripe():
    # now < measure_at -> not ripe yet, regardless of freshness
    assert calibration.settle_decision(_t(3), _t(4), _t(5), 12.0) == "wait"


def test_settle_decision_measures_when_ripe_and_fresh():
    # ripe (now >= measure_at) and a genuine post-settle sample (last_updated >= measure_at)
    assert calibration.settle_decision(_t(5), _t(4), _t(4), 12.0) == "measure"     # boundary: last_updated == measure_at
    assert calibration.settle_decision(_t(6), _t(4), _t(5), 12.0) == "measure"


def test_settle_decision_waits_when_ripe_but_stale():
    # ripe but the only sample predates measure_at, still before the deadline
    assert calibration.settle_decision(_t(6), _t(4), _t(1), 12.0) == "wait"
    # sensor last_updated unreadable -> treated as not fresh
    assert calibration.settle_decision(_t(6), _t(4), None, 12.0) == "wait"


def test_settle_decision_expires_at_deadline_without_fresh_sample():
    # deadline = measure_at + max_wait = _t(4) + 12h = _t(16)
    assert calibration.settle_decision(_t(16), _t(4), _t(1), 12.0) == "expired"   # boundary: now == deadline
    assert calibration.settle_decision(_t(20), _t(4), None, 12.0) == "expired"


def test_settle_decision_measures_even_past_deadline_if_fresh():
    # a fresh sample always wins over the deadline (we can still learn)
    assert calibration.settle_decision(_t(20), _t(4), _t(18), 12.0) == "measure"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/irrigation/test_calibration.py -k settle_decision -v`
Expected: FAIL with `AttributeError: module 'irrigation_lib.calibration' has no attribute 'settle_decision'`

- [ ] **Step 3: Implement `settle_decision`**

Add to `irrigation_lib/calibration.py` (uses the module's existing `import datetime as dt`):

```python
def settle_decision(now, measure_at, last_updated, max_wait_hours) -> str:
    """Decide how to handle one pending calibration observation at this poll.

    All datetimes are tz-aware (aware/aware comparisons only). `last_updated` is
    the dominant sensor's HA last_updated (bumps on a value change, so it tracks
    genuine device check-ins) or None when it cannot be read.

    Returns:
      "wait"     - not ripe yet (now < measure_at), OR ripe but no genuine
                   post-settle sample (last_updated < measure_at or None) and the
                   deadline has not passed. Keep the obs pending.
      "measure"  - ripe AND a genuine post-settle sample exists
                   (last_updated >= measure_at). Read + classify it.
      "expired"  - ripe, still no fresh sample, and now >= measure_at + max_wait.
                   Drop the obs (never reject, never touch the model).

    A fresh sample always yields "measure", even past the deadline: if we can
    learn cleanly we should, regardless of how long it took to arrive.
    """
    if now < measure_at:
        return "wait"
    if last_updated is not None and last_updated >= measure_at:
        return "measure"
    deadline = measure_at + dt.timedelta(hours=max_wait_hours)
    if now >= deadline:
        return "expired"
    return "wait"
```

- [ ] **Step 4: Add the tunable**

In `irrigation_lib/config.py`, in the `Tunables` dataclass, immediately after `settle_hours` (currently `settle_hours: float = 4.0`), add:

```python
    # How long past measure_at (run_end + settle_hours) the settle poll will keep
    # waiting for a genuine post-settle sensor report before giving up on an
    # observation. On timeout the obs is DROPPED (inconclusive) — never rejected —
    # so a missed GeoDrops check-in can't corrupt the model. Sized to clear the
    # observed worst-case ~6h missed check-in plus settle.
    settle_max_wait_hours: float = 12.0
```

- [ ] **Step 5: Assert the tunable default**

Add to `tests/irrigation/test_config.py` (match the file's existing style for a Tunables default check):

```python
def test_settle_max_wait_hours_default():
    from irrigation_lib.config import Tunables
    assert Tunables().settle_max_wait_hours == 12.0
```

- [ ] **Step 6: Run the full lib suite**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/`
Expected: PASS (all prior tests + the new ones).

- [ ] **Step 7: Commit**

```bash
git add irrigation_lib/calibration.py irrigation_lib/config.py tests/irrigation/test_calibration.py tests/irrigation/test_config.py
git commit -m "feat(calibration): settle_decision timing gate + settle_max_wait_hours tunable"
```

---

### Task 2: Wire the freshness-gated poll into `_settle_and_learn`

Replaces the fixed 9am cron with a 30-minute poll and routes each pending obs through `settle_decision`. Accept/reject bodies are unchanged; only ripeness/freshness gating and Rachio-call laziness change. App layer — `py_compile` only.

**Files:**
- Modify: `irrigation/__init__.py` — the `_settle_and_learn` function (currently `@time_trigger("cron(0 9 * * *)")` at ~line 2275) and add a small `last_updated` helper near `_read_zone_signals` (~line 1179).

**Interfaces:**
- Consumes: `calibration.settle_decision(now, measure_at, last_updated, max_wait_hours)`, `Tunables.settle_max_wait_hours` (Task 1).
- Produces: no new callable surface (internal wiring only).

- [ ] **Step 1: Add a freshness helper**

In `irrigation/__init__.py`, directly after `_read_zone_signals` (ends ~line 1186), add:

```python
def _sensor_last_updated(entity):
    """The HA `last_updated` (tz-aware UTC datetime) of a state entity, or None.

    pyscript exposes it as a virtual attribute via state.get("<entity>.last_updated").
    We use last_updated (bumps only when the value changes) — NOT last_reported
    (bumps on every MQTT republish, which would defeat the freshness gate). Any
    failure (missing entity -> NameError, or unexpected type) yields None, which
    settle_decision treats as "not fresh".
    """
    try:
        return state.get(entity + ".last_updated")
    except Exception:
        return None
```

- [ ] **Step 2: Change the trigger to a 30-minute poll**

Replace the decorator on `_settle_and_learn`:

```python
@time_trigger("cron(0 9 * * *)")
```

with:

```python
@time_trigger("cron(*/30 * * * *)")
```

- [ ] **Step 3: Make Rachio-runtime + rain reads lazy, and route via `settle_decision`**

The current body (from `now = dt.datetime.now().astimezone()` through the per-rec loop) eagerly calls `get_runtimes()` and `_rained_since_run(...)` once per poll whenever pending is non-empty. With a 30-min poll a calibrating zone would call Rachio every 30 min for hours while it waits for a fresh sample. Load both lazily, only when at least one obs actually reaches `"measure"`.

Replace the block that currently reads (lines ~2291–2303):

```python
    api_runtimes = get_runtimes()
    now = dt.datetime.now().astimezone()
    rained = _rained_since_run(cfg.bindings, tun)
    remaining = []
    learned = 0
    for rec in pending:
        try:
            measure_at = dt.datetime.fromisoformat(rec["measure_at_iso"])
        except (KeyError, ValueError):
            continue
        if now < measure_at:
            remaining.append(rec)
            continue
```

with:

```python
    now = dt.datetime.now().astimezone()
    api_runtimes = None      # lazy: only fetched once an obs is ready to measure
    rained = None            # lazy: same
    remaining = []
    learned = 0
    dropped = 0
    for rec in pending:
        try:
            measure_at = dt.datetime.fromisoformat(rec["measure_at_iso"])
        except (KeyError, ValueError):
            continue
        zone = rec.get("zone")
        zone_cfg = cfg.zones.get(zone)
        if zone_cfg is None:
            continue  # obs for a zone no longer configured: drop it
        last_updated = _sensor_last_updated(zone_cfg.dominant_sensor)
        decision = calibration.settle_decision(
            now, measure_at, last_updated, tun.settle_max_wait_hours)
        if decision == "wait":
            remaining.append(rec)
            continue
        if decision == "expired":
            dropped += 1
            continue  # inconclusive: drop, never reject, no model change
        # decision == "measure": fall through to the accept/reject path below
        if api_runtimes is None:
            api_runtimes = get_runtimes()
            rained = _rained_since_run(cfg.bindings, tun)
```

- [ ] **Step 4: Reconcile the existing measure body**

The original loop body (after the ripeness check) re-fetched `zone = rec.get("zone")`, `zone_cfg = cfg.zones.get(zone)`, and returned early if `zone_cfg is None`. Those are now done above the decision. In the remaining measure body (currently lines ~2304–2363), **delete** the now-duplicated lines:

```python
            zone = rec.get("zone")
            zone_cfg = cfg.zones.get(zone)
            if zone_cfg is None:
                continue
```

Keep everything from `pre = rec.get("pre_dominant")` onward exactly as-is (the `try/except` wrapper, `_read_zone_signals`, `classify`, accept/`apply_reject`/training branches, `store[zone] = zrec`). The `try:` that wrapped the body stays; the measure body remains inside it.

- [ ] **Step 5: Update the trailing log line**

Replace (lines ~2367–2370):

```python
    task.executor(_write_json_atomic, PENDING_OBS_PATH, remaining)
    _write_efficacy_store(store)
    if learned:
        log.info(f"irrigation: settle-and-learn updated {learned} zone(s)")
```

with:

```python
    task.executor(_write_json_atomic, PENDING_OBS_PATH, remaining)
    _write_efficacy_store(store)
    if learned or dropped:
        log.info(
            f"irrigation: settle-and-learn updated {learned} zone(s), "
            f"dropped {dropped} inconclusive")
```

- [ ] **Step 6: Verify it compiles**

Run: `python -m py_compile irrigation/__init__.py`
Expected: no output (success).

- [ ] **Step 7: Full lib suite still green (no regressions)**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/`
Expected: PASS.

- [ ] **Step 8: Read-through review of `_settle_and_learn`**

Re-read the whole function end to end and confirm:
- Early returns (config load fail; `not self_calibration_enabled`; empty pending) are unchanged and still precede any Rachio call.
- No path calls `get_runtimes()` / `_rained_since_run` unless an obs reached `"measure"`.
- `expired` never appends to `remaining`, never writes `store`, never touches `miss_streak`.
- `wait` always re-appends to `remaining`.
- All datetime comparisons are aware/aware.

- [ ] **Step 9: Commit**

```bash
git add irrigation/__init__.py
git commit -m "feat(calibration): 30-min freshness-gated settle poll with inconclusive deadline"
```

---

### Task 3: Update the design note to record resolutions and status

Marks the design doc as implemented and folds in the Q1/Q2/Q3/Q4/Q5 resolutions so the doc matches what shipped.

**Files:**
- Modify: `docs/design/calibration-settle-timing.md`

**Interfaces:** none (docs).

- [ ] **Step 1: Flip status and record resolutions**

At the top of `docs/design/calibration-settle-timing.md`, change the Status line from `brainstormed 2026-09-17, not implemented.` to `implemented 2026-09-17 (see docs/superpowers/plans/2026-09-17-calibration-settle-timing.md).`

Then replace the "Open questions / feasibility" section's items with their resolutions:
- Q1 → **Resolved: no HA-triggerable read-now.** `ha-geodrops-integration` is one-way BigQuery→MQTT; Option D dropped; fix is scheduler-only.
- Q2 → **Resolved:** `state.get("<entity>.last_updated")` → tz-aware UTC datetime; use `last_updated` not `last_reported`; read in the app (`_sensor_last_updated`), not `sensors.read_zone`.
- Q3 → **Resolved:** `settle_max_wait_hours = 12.0`.
- Q4 → **Resolved:** `settle_hours` kept as minimum redistribution time; effective rule = first genuine report ≥ settle_hours.
- Q5 → **Resolved:** expired obs is dropped; no `miss_streak`/convergence/efficacy change.

Add a one-line note that the poll cadence chosen was 30 min (`cron(*/30 * * * *)`).

- [ ] **Step 2: Commit**

```bash
git add docs/design/calibration-settle-timing.md
git commit -m "docs(calibration): mark settle-timing design implemented; record Q1-Q5 resolutions"
```

---

## Branch / PR handling

- The design note currently lives only on branch `docs/calibration-settle-timing` (PR #4, open). Base this feature branch **off that branch** (`feat/calibration-settle-timing` from `docs/calibration-settle-timing`) so the doc is present to update, and let the feature PR **supersede PR #4** (close #4 as superseded, or the user merges #4 first and the feature PR rebases onto main). The assistant does not merge PRs — the user does.
- No release is cut here (batched per release-batching-preference). After merge, main carries the change untagged until the user asks for a scheduler release. No wrapper change is needed (no new state surfaced).

## Post-merge validation (operator, on the box)

After the scheduler is next deployed (config pull / re-vendor) with self-calibration on:
- Confirm `_settle_and_learn` fires on the half-hour (pyscript log), returns immediately when no obs pend.
- On a calibrating zone, confirm a probe's obs is measured shortly after the first genuine post-settle GeoDrops report (not at a fixed 9am), and that a night with a missed check-in logs `dropped N inconclusive` rather than a `no_rise` reject in `state/irrigation_efficacy.json`.
