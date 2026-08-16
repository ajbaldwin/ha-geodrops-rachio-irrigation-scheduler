"""Apply a drought profile to a zone's target. Pure Python."""
from __future__ import annotations

from dataclasses import dataclass

from irrigation_lib.config import BAND_ORDER, Band, DroughtProfile, ZoneConfig, band_rank


@dataclass(frozen=True)
class EffectiveTarget:
    band_name: str
    floor: float
    rank: int
    runtime_scale: float


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def effective_target(
    zone: ZoneConfig, bands: dict[str, Band], profile: DroughtProfile
) -> EffectiveTarget:
    rank = _clamp(band_rank(zone.target_range) + profile.target_offset, 0, 5)
    band_name = BAND_ORDER[rank]
    floor = bands[band_name].low - profile.trigger_margin
    return EffectiveTarget(
        band_name=band_name, floor=floor, rank=rank,
        runtime_scale=profile.runtime_scale,
    )
