"""Calibration functions: efficacy↔span, probe sizing, saturation cap. Pure Python (no pyscript globals)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


def efficacy_to_span(efficacy: float, full_refill_min: float) -> float:
    """Convert efficacy to span in points.

    Returns the points range that a full refill buys at the given efficacy.
    """
    return efficacy * full_refill_min


def probe_minutes(full_refill_min: float, prior_minutes, last_rise, t) -> float:
    """Calculate probe window size for next calibration cycle.

    `t` is a Tunables object with .probe_fraction, .probe_floor_minutes,
    .probe_growth, and .measurable_rise_pts attributes.

    First probe (prior_minutes is None): uses fraction of window or floor, whichever is larger.
    Subsequent probes: grows on small rise, holds steady on measurable rise.
    """
    if prior_minutes is None:
        candidate = t.probe_fraction * full_refill_min
        return max(candidate, t.probe_floor_minutes)
    elif last_rise is not None and last_rise < t.measurable_rise_pts:
        return prior_minutes * t.probe_growth
    else:
        return prior_minutes


def cap_for_saturation(minutes: float, dominant_now: float, efficacy, t) -> float:
    """Cap probe minutes to prevent saturation.

    If efficacy is known (not None and > 0), calculates the maximum watering
    that keeps soil moisture below the saturation rejection threshold.
    Otherwise returns minutes unchanged.

    `t` is a Tunables object with .saturation_reject attribute.
    """
    if efficacy and efficacy > 0:
        headroom = t.saturation_reject - dominant_now
        max_min = headroom / efficacy
        return max(0.0, min(minutes, max_min))
    else:
        return minutes


@dataclass(frozen=True)
class Observation:
    """A calibration observation: pre/post watering soil moisture and metadata."""
    zone: str
    pre_dominant: float
    minutes: float
    settled_dominant: float
    qcn_training: bool
    rained: bool
    sensor_ok: bool


def classify(obs: Observation, t) -> str:
    """Classify observation quality; returns "ok" or a reject reason.

    Priority order (first match wins):
    1. not obs.sensor_ok → "unavailable"
    2. obs.qcn_training → "training"
    3. obs.rained → "rain"
    4. obs.settled_dominant >= t.saturation_reject → "saturated"
    5. (obs.settled_dominant - obs.pre_dominant) <= 0 → "no_rise"
    6. else → "ok"
    """
    if not obs.sensor_ok:
        return "unavailable"
    if obs.qcn_training:
        return "training"
    if obs.rained:
        return "rain"
    if obs.settled_dominant >= t.saturation_reject:
        return "saturated"
    if (obs.settled_dominant - obs.pre_dominant) <= 0:
        return "no_rise"
    return "ok"


def update_efficacy(prev, obs: Observation, t) -> float:
    """Update efficacy with EWMA from a new observation.

    efficacy_obs = (settled - pre) / minutes
    If prev is None: return efficacy_obs (bootstrap)
    Else: blend with EWMA using t.calibration_ewma_alpha
    """
    efficacy_obs = (obs.settled_dominant - obs.pre_dominant) / obs.minutes
    if prev is None:
        return efficacy_obs
    return t.calibration_ewma_alpha * efficacy_obs + (1 - t.calibration_ewma_alpha) * prev


def converged(recent, t) -> bool:
    """Check if recent efficacy samples have converged.

    recent: list of efficacy values
    Returns True if:
    - length >= t.convergence_samples AND
    - mean != 0 AND
    - (max - min) <= convergence_tolerance * mean
    """
    if len(recent) < t.convergence_samples:
        return False

    # Compute mean explicitly (no generator expressions per pyscript)
    total = 0.0
    for val in recent:
        total = total + val
    mean = total / len(recent)

    if mean == 0:
        return False

    # Find max and min
    max_val = recent[0]
    min_val = recent[0]
    for i in range(len(recent)):
        if recent[i] > max_val:
            max_val = recent[i]
        if recent[i] < min_val:
            min_val = recent[i]

    tolerance_band = t.convergence_tolerance * mean
    return (max_val - min_val) <= tolerance_band


def next_state(state: str, converged_flag: bool, training: bool, miss_streak: int, t) -> str:
    """State machine: compute next calibration state.

    Transitions:
    - if training: always transition to "recalibrating"
    - elif state in ("calibrating", "recalibrating") and converged_flag: -> "converged"
    - elif state == "converged" and miss_streak >= t.convergence_samples: -> "calibrating"
    - else: return state unchanged

    `t` is a Tunables object with .convergence_samples attribute.
    """
    if training:
        return "recalibrating"

    if state in ("calibrating", "recalibrating") and converged_flag:
        return "converged"

    if state == "converged" and miss_streak >= t.convergence_samples:
        return "calibrating"

    return state


def exclusion_return(rec, now, threshold_hours: float) -> dict:
    """Resolve a zone's return from exclusion, given its efficacy record.

    If the record carries an `excluded_since` stamp and the zone was excluded for
    at least `threshold_hours`, reset it to recalibrating (its learned efficacy is
    stale — the soil likely changed, e.g. overseed). A shorter (accidental)
    exclusion keeps the existing calibration. Either way the stamp is cleared. A
    record with no stamp is returned unchanged. `now` is a datetime whose tz-
    awareness matches how `excluded_since` was written.
    """
    out = dict(rec)
    stamp = out.pop("excluded_since", None)
    if stamp is None:
        return out
    try:
        since = dt.datetime.fromisoformat(stamp)
        hours = (now - since).total_seconds() / 3600.0
    except (TypeError, ValueError):
        return out  # malformed stamp: clear it, keep the calibration
    if hours >= threshold_hours:
        out["state"] = "recalibrating"
        out["efficacy"] = None
        out["span_pts"] = 0
        out["recent"] = []
        out["miss_streak"] = 0
    return out


def apply_reject(zrec: dict, reason: str, minutes: float, rise: float, t) -> dict:
    """Return a copy of zrec updated for a rejected calibration observation.

    Both dose-size rejects resize the probe so it self-corrects toward the clean
    measurable band:
    - "no_rise": the probe was too small — record its size and (non-positive)
      rise so probe_minutes grows the next probe.
    - "saturated": the probe was too large — record the shrunk size (÷probe_shrink)
      with last_rise=None so probe_minutes holds there next time.
    Confounded reasons (rain/unavailable/training) never tested efficacy, so they
    leave the size untouched. Always stamps last_reject_reason.
    """
    out = dict(zrec)
    out["last_reject_reason"] = reason
    if reason == "no_rise":
        out["prior_minutes"] = minutes
        out["last_rise"] = rise
    elif reason == "saturated":
        out["prior_minutes"] = minutes / t.probe_shrink
        out["last_rise"] = None
    return out


def should_probe(state: str, dominant_now: float, pinned: bool, t) -> bool:
    """Determine whether to probe (collect calibration observation) now.

    Returns True iff:
    - state in ("calibrating", "recalibrating") AND
    - not pinned AND
    - dominant_now < t.probe_headroom_ceiling

    `t` is a Tunables object with .probe_headroom_ceiling attribute.
    """
    if state not in ("calibrating", "recalibrating"):
        return False

    if pinned:
        return False

    if dominant_now >= t.probe_headroom_ceiling:
        return False

    return True
