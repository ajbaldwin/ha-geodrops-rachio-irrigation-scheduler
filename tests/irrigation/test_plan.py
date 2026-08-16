import pytest

from irrigation_lib import config, plan

T = config.Tunables()  # cycle 12, soak 20


def test_single_zone_multi_cycle_inserts_idle_soak():
    slots = plan.build_sequence(
        zones_priority=["a"],
        minutes_by_zone={"a": 24.0},
        geo={"a": "front"}, adjacency={"a": ()},
        cycle_min=12, soak_min=20,
    )
    # water 12, idle 20, water 12
    assert slots == [
        plan.Slot("a", 12), plan.Slot(None, 20), plan.Slot("a", 12),
    ]
    assert plan.span_of(slots) == 44


def test_two_adjacent_front_zones_interleave_then_soak():
    slots = plan.build_sequence(
        zones_priority=["a", "b"],
        minutes_by_zone={"a": 24.0, "b": 12.0},
        geo={"a": "front", "b": "front"},
        adjacency={"a": ("b",), "b": ("a",)},
        cycle_min=12, soak_min=20,
    )
    # a(12), b(12), idle(8) until a ready at 32, a(12)
    assert slots == [
        plan.Slot("a", 12), plan.Slot("b", 12),
        plan.Slot(None, 8), plan.Slot("a", 12),
    ]
    assert plan.span_of(slots) == 44


def test_front_back_alternate_no_idle():
    slots = plan.build_sequence(
        zones_priority=["a", "b"],
        minutes_by_zone={"a": 24.0, "b": 24.0},
        geo={"a": "front", "b": "back"},
        adjacency={"a": (), "b": ()},
        cycle_min=12, soak_min=20,
    )
    # a,b fill each other's soak: a(0-12) b(12-24) a ready@32? 24<32 -> a not ready,
    # b last_end 24, b ready@44. At t=24 neither ready -> idle to 32, a(32-44), b ready44 -> b(44-56)
    keys = [s.zone_key for s in slots]
    assert keys == ["a", "b", None, "a", "b"]


def test_dispersion_prefers_opposite_geography():
    slots = plan.build_sequence(
        zones_priority=["a", "b", "c"],
        minutes_by_zone={"a": 12.0, "b": 12.0, "c": 12.0},
        geo={"a": "front", "b": "front", "c": "back"},
        adjacency={"a": ("b",), "b": ("a",), "c": ()},
        cycle_min=12, soak_min=20,
    )
    # first a (priority), then prefer opposite geo -> c before b
    assert [s.zone_key for s in slots][:2] == ["a", "c"]


def test_build_plan_trims_lowest_priority_to_fit_cap():
    result = plan.build_plan(
        zones_priority=["a", "b"],
        minutes_by_zone={"a": 24.0, "b": 24.0},
        geo={"a": "front", "b": "front"},
        adjacency={"a": ("b",), "b": ("a",)},
        cap_minutes=45.0,
        t=T,
    )
    # full two-zone plan exceeds 45; dropping b leaves a=44 which fits
    assert result.watered == ["a"]
    assert result.dropped == ["b"]
    assert result.span_minutes <= 45


def test_build_plan_keeps_all_when_it_fits():
    result = plan.build_plan(
        zones_priority=["a", "b"],
        minutes_by_zone={"a": 12.0, "b": 12.0},
        geo={"a": "front", "b": "back"},
        adjacency={"a": (), "b": ()},
        cap_minutes=120.0,
        t=T,
    )
    assert result.watered == ["a", "b"]
    assert result.dropped == []


def test_fractional_remaining_terminates():
    slots = plan.build_sequence(
        zones_priority=["a"],
        minutes_by_zone={"a": 8.4},
        geo={"a": "front"}, adjacency={"a": ()},
        cycle_min=12, soak_min=20,
    )
    assert slots == [plan.Slot("a", 8.4)]
    assert plan.span_of(slots) == pytest.approx(8.4)


def test_fractional_multi_cycle_terminates():
    slots = plan.build_sequence(
        zones_priority=["a"],
        minutes_by_zone={"a": 20.4},
        geo={"a": "front"}, adjacency={"a": ()},
        cycle_min=12, soak_min=20,
    )
    assert [s.zone_key for s in slots] == ["a", None, "a"]
    assert slots[-1].minutes == pytest.approx(8.4)
    assert plan.span_of(slots) == pytest.approx(40.4)


def test_cycles_minutes_scales():
    assert plan.cycles_minutes(24.0, 0.7) == pytest.approx(16.8)
    assert plan.cycles_minutes(12.0, 1.0) == 12.0


def test_build_plan_trims_multiple_zones():
    result = plan.build_plan(
        zones_priority=["a", "b", "c"],
        minutes_by_zone={"a": 24.0, "b": 24.0, "c": 24.0},
        geo={"a": "front", "b": "front", "c": "front"},
        adjacency={"a": ("b", "c"), "b": ("a", "c"), "c": ("a", "b")},
        cap_minutes=45.0,
        t=config.Tunables(),
    )
    assert result.watered == ["a"]
    assert result.dropped == ["b", "c"]
    assert result.span_minutes <= 45
