"""Flattening a plan's slot list into ONE Rachio schedule + device pauses.

The whole night becomes a single ordered program: water steps feed one
`start_multiple_zone_schedule` call; idle soak slots become pause steps the
executor turns into device pauses that keep that one schedule alive.
"""
from irrigation_lib import program
from irrigation_lib.blocks import ZoneRun
from irrigation_lib.plan import Slot


def test_watering_slots_become_water_steps_in_order():
    steps = program.plan_program([Slot("a", 12), Slot("b", 12), Slot("a", 12)])
    assert steps == [
        program.Step("water", "a", 12),
        program.Step("water", "b", 12),
        program.Step("water", "a", 12),
    ]


def test_idle_slot_becomes_a_pause_step_between_water():
    steps = program.plan_program([Slot("a", 12), Slot(None, 20), Slot("a", 12)])
    assert steps == [
        program.Step("water", "a", 12),
        program.Step("pause", None, 20),
        program.Step("water", "a", 12),
    ]


def test_whole_night_quantization_never_exceeds_plan():
    # Two 6.5-min slots: cumulative floor gives 6 then 7 (total 13), not 7+7.
    steps = program.plan_program([Slot("a", 6.5), Slot("b", 6.5)])
    assert [s.minutes for s in steps] == [6, 7]


def test_sub_minute_water_slot_is_dropped():
    steps = program.plan_program([Slot("a", 0.4)])
    assert steps == []


def test_leading_and_trailing_pauses_are_dropped():
    steps = program.plan_program([Slot(None, 20), Slot("a", 12), Slot(None, 20)])
    assert steps == [program.Step("water", "a", 12)]


def test_adjacent_pauses_merge_when_a_water_slot_drops_out():
    # A zero-minute water slot between two idles must not split the soak into
    # two device pauses; they merge into one gap.
    steps = program.plan_program([Slot("a", 12), Slot(None, 20), Slot("b", 0.3), Slot(None, 20), Slot("a", 12)])
    assert steps == [
        program.Step("water", "a", 12),
        program.Step("pause", None, 40),
        program.Step("water", "a", 12),
    ]


def test_program_runs_extracts_water_steps_as_zone_runs():
    steps = [program.Step("water", "a", 12), program.Step("pause", None, 20),
             program.Step("water", "b", 8)]
    assert program.program_runs(steps) == [ZoneRun("a", 12), ZoneRun("b", 8)]


def _steps():
    return [
        program.Step("water", "a", 12), program.Step("pause", None, 20),
        program.Step("water", "b", 12), program.Step("pause", None, 20),
        program.Step("water", "a", 12),
    ]


def test_unbounded_segmentation_is_one_full_collapse_segment():
    segs = program.segment_program(_steps(), 0)
    assert len(segs) == 1
    assert segs[0].steps == _steps()
    assert segs[0].gap_after == 0


def test_one_pause_per_schedule_splits_at_each_gap():
    segs = program.segment_program(_steps(), 1)
    assert [len([x for x in s.steps if x.kind == "pause"]) for s in segs] == [1, 0]
    assert segs[0].gap_after == 20          # the split pause becomes an idle gap
    assert segs[0].steps[-1].kind == "water"
    assert segs[1].steps == [program.Step("water", "a", 12)]
    assert segs[1].gap_after == 0


def test_two_pause_budget_keeps_both_gaps_in_one_segment():
    segs = program.segment_program(_steps(), 2)
    assert len(segs) == 1
    assert segs[0].gap_after == 0
