import random

from irrigation_lib import drought, evaluate, sensors


def reading(key, dominant, index_rank, online=True):
    return sensors.ZoneReading(
        key=key, online=online, offline_reason=None,
        dominant=dominant, index_rank=index_rank, state_text="x",
    )


def target(floor=67, rank=2):
    return drought.EffectiveTarget(
        band_name="moist", floor=floor, rank=rank, runtime_scale=1.0,
    )


def test_needs_water_when_below_floor():
    e = evaluate.evaluate_zone(reading("z", 60.0, 0), target())
    assert e.needs_water is True
    assert e.index_deficit == 2
    assert e.dominant_deficit == 7.0


def test_no_water_at_or_above_floor():
    e = evaluate.evaluate_zone(reading("z", 67.0, 2), target())
    assert e.needs_water is False


def test_offline_never_needs_water():
    r = sensors.ZoneReading("z", False, "unavailable", None, None, None)
    e = evaluate.evaluate_zone(r, target())
    assert e.needs_water is False


def test_priority_ranks_bigger_index_deficit_first():
    # Both dry (dominant 50); zone A target moist+ (rank 3), zone B target moist (rank 2)
    a = evaluate.evaluate_zone(reading("a", 50.0, 0), target(floor=76, rank=3))
    b = evaluate.evaluate_zone(reading("b", 50.0, 0), target(floor=67, rank=2))
    ordered = evaluate.sort_by_priority([b, a], random.Random(1))
    assert [e.key for e in ordered] == ["a", "b"]


def test_priority_tiebreak_by_dominant_deficit():
    # Equal index deficit (both rank 2, current 0); a is further below floor
    a = evaluate.evaluate_zone(reading("a", 50.0, 0), target(floor=67, rank=2))
    b = evaluate.evaluate_zone(reading("b", 60.0, 0), target(floor=67, rank=2))
    ordered = evaluate.sort_by_priority([b, a], random.Random(1))
    assert [e.key for e in ordered] == ["a", "b"]


def test_sort_excludes_non_needing():
    a = evaluate.evaluate_zone(reading("a", 50.0, 0), target())
    b = evaluate.evaluate_zone(reading("b", 70.0, 2), target())  # above floor
    ordered = evaluate.sort_by_priority([a, b], random.Random(1))
    assert [e.key for e in ordered] == ["a"]


# --- window-start moisture re-check (revalidate_zone) ---

def test_revalidate_probe_drops_when_at_or_above_ceiling():
    assert evaluate.revalidate_zone(
        online=True, dominant_now=92.5, dosing_source="probe",
        floor=65.0, ceiling=85.0) == "saturated"


def test_revalidate_probe_boundary_equal_ceiling_drops():
    assert evaluate.revalidate_zone(
        online=True, dominant_now=85.0, dosing_source="probe",
        floor=65.0, ceiling=85.0) == "saturated"


def test_revalidate_probe_keeps_below_ceiling():
    assert evaluate.revalidate_zone(
        online=True, dominant_now=83.8, dosing_source="probe",
        floor=65.0, ceiling=85.0) is None


def test_revalidate_deficit_drops_when_at_or_above_floor():
    assert evaluate.revalidate_zone(
        online=True, dominant_now=70.0, dosing_source="deficit",
        floor=65.0, ceiling=85.0) == "above-floor"


def test_revalidate_deficit_boundary_equal_floor_drops():
    assert evaluate.revalidate_zone(
        online=True, dominant_now=65.0, dosing_source="deficit",
        floor=65.0, ceiling=85.0) == "above-floor"


def test_revalidate_deficit_keeps_below_floor():
    assert evaluate.revalidate_zone(
        online=True, dominant_now=60.0, dosing_source="deficit",
        floor=65.0, ceiling=85.0) is None


def test_revalidate_offline_keeps_fail_open():
    # Even a reading that would otherwise drop is kept when the sensor is offline
    # at re-check time — we cannot prove the soil is wet.
    assert evaluate.revalidate_zone(
        online=False, dominant_now=99.0, dosing_source="probe",
        floor=65.0, ceiling=85.0) is None
