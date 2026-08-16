import pytest

from irrigation_lib import rachio_runtime

# Trimmed sample of a Rachio /device zones payload.
ZONES = [
    {"id": "zone_a", "name": "Zone A", "runtime": 3659},
    {"id": "zone_b", "name": "Zone B", "runtime": 3125},
    {"id": "nulls", "name": "Null Runtime", "runtime": None},
    {"id": "missing", "name": "No Runtime Key"},
    {"name": "No Id", "runtime": 600},
]


def test_parse_runtimes_converts_seconds_to_minutes():
    out = rachio_runtime.parse_runtimes(ZONES)
    assert out["zone_a"] == 3659 / 60
    assert out["zone_b"] == 3125 / 60


def test_parse_runtimes_skips_none_or_missing_runtime():
    out = rachio_runtime.parse_runtimes(ZONES)
    assert "nulls" not in out
    assert "missing" not in out


def test_parse_runtimes_skips_zone_without_id():
    out = rachio_runtime.parse_runtimes(ZONES)
    assert "No Id" not in out.values()
    assert len(out) == 2


def test_parse_runtimes_empty_input():
    assert rachio_runtime.parse_runtimes([]) == {}


def test_parse_runtimes_skips_non_numeric_runtime():
    out = rachio_runtime.parse_runtimes([{"id": "x", "runtime": "oops"}])
    assert out == {}


def test_parse_refill_depths_converts_inches_to_mm():
    zones = [{"id": "z1", "depthOfWater": 0.25}]
    assert rachio_runtime.parse_refill_depths(zones)["z1"] == pytest.approx(6.35)


def test_parse_refill_depths_reads_all_zones():
    zones = [
        {"id": "zone_a", "depthOfWater": 0.25},
        {"id": "zone_b", "depthOfWater": 0.35},
    ]
    result = rachio_runtime.parse_refill_depths(zones)
    assert result["zone_a"] == pytest.approx(6.35)
    assert result["zone_b"] == pytest.approx(8.89)


def test_parse_refill_depths_skips_missing_and_bad_values():
    zones = [
        {"id": "ok", "depthOfWater": 0.30},
        {"id": "no_depth"},
        {"depthOfWater": 0.30},              # no id
        {"id": "bad", "depthOfWater": "n/a"},
        {"id": "null", "depthOfWater": None},
    ]
    result = rachio_runtime.parse_refill_depths(zones)
    assert list(result) == ["ok"]


def test_parse_refill_depths_empty_payload():
    assert rachio_runtime.parse_refill_depths([]) == {}
