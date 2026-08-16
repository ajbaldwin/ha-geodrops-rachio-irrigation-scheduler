"""Parse the Rachio Public API device-zones payload into per-zone runtimes.

Pure Python: no pyscript globals, no HTTP. The adapter (`rachio_api.py`) does
the fetching and hands the raw `zones` array here.
"""
from __future__ import annotations


def parse_runtimes(zones_json: list) -> dict:
    """Map each Rachio zone `id` -> full-refill runtime in minutes.

    Uses the zone `runtime` field (seconds). Zones without an `id` or without a
    numeric `runtime` are skipped, so a partial/garbled payload degrades to
    fewer entries rather than raising.
    """
    result = {}
    for zone in zones_json:
        zone_id = zone.get("id")
        runtime = zone.get("runtime")
        if not zone_id or runtime is None:
            continue
        try:
            result[zone_id] = float(runtime) / 60.0
        except (TypeError, ValueError):
            continue
    return result


def parse_refill_depths(zones_json: list) -> dict:
    """Map each Rachio zone `id` -> refill depth in millimetres.

    Uses the zone `depthOfWater` field (inches): the water that must enter the
    root zone to bring it from the management-allowed-depletion point back to
    field capacity. Verified against the underlying agronomy —
    `depthOfWater == availableWater * rootZoneDepth * managementAllowedDepletion`
    for every zone on this controller.

    This is the quantity forecast rain must substitute for, so the rain-skip
    threshold is expressed as a fraction of it rather than a fixed depth.

    A separate function from `parse_runtimes` on purpose: that one and its tests
    stay untouched. Zones without an `id` or without a numeric `depthOfWater`
    are skipped, so a partial payload degrades to fewer entries rather than
    raising.
    """
    result = {}
    for zone in zones_json:
        zone_id = zone.get("id")
        depth_in = zone.get("depthOfWater")
        if not zone_id or depth_in is None:
            continue
        try:
            result[zone_id] = float(depth_in) * 25.4
        except (TypeError, ValueError):
            continue
    return result
