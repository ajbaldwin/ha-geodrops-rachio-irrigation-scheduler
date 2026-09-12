"""Deficit-proportional per-zone dose. Pure Python (no pyscript globals)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DoseResult:
    minutes: float
    frac: float
    effective_depth_mm: float
    deficit_pts: float
    span_pts: float
    source: str  # "live" | "config" | "fallback"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def dose_zone(
    dominant_now: float,
    refill_target: float,
    span_pts,               # float | None; None/<=0 -> fallback (frac=1)
    span_source: str,       # "live" | "config" — where span_pts came from
    full_refill_min: float,
    refill_depth_mm: float,
    runtime_scale: float,
) -> DoseResult:
    """Minutes and delivered depth to refill this zone from `dominant_now` up to
    `refill_target`, never more than one full Rachio refill.

    `span_pts` is the `dominant` points a full refill buys. When it is missing or
    non-positive there is no usable calibration, so the dose degrades to a full
    refill (frac=1) and the recorded source is "fallback" regardless of
    `span_source`. Otherwise frac is the clamped deficit fraction and the source
    is whatever produced the span.
    """
    deficit_pts = refill_target - dominant_now
    if span_pts is None or span_pts <= 0:
        frac = 1.0
        span_val = 0.0
        source = "fallback"
    else:
        frac = _clamp(deficit_pts / span_pts, 0.0, 1.0)
        span_val = float(span_pts)
        source = span_source
    minutes = full_refill_min * frac * runtime_scale
    effective_depth_mm = frac * refill_depth_mm
    return DoseResult(
        minutes=minutes,
        frac=frac,
        effective_depth_mm=effective_depth_mm,
        deficit_pts=deficit_pts,
        span_pts=span_val,
        source=source,
    )
