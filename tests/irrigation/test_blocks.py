"""Turning a plan's slot list into Rachio custom-schedule blocks.

A "block" is a maximal run of back-to-back watering slots, which is what
`rachio.start_multiple_zone_schedule` can execute in a single call. Idle soak
slots separate blocks and are executed by pyscript sleeping with nothing on.
"""
import pytest

from irrigation_lib import blocks, config, plan
from irrigation_lib.plan import Slot

T = config.Tunables()  # cycle 12, soak 20


# ─── group_blocks ────────────────────────────────────────────────────────────

def test_consecutive_slots_become_one_zone_block():
    slots = [Slot("a", 12), Slot("b", 12), Slot("a", 12)]

    result = blocks.group_blocks(slots)

    assert result == [
        blocks.Block(kind="zones", slots=[Slot("a", 12), Slot("b", 12), Slot("a", 12)],
                     minutes=36),
    ]


def test_idle_slot_splits_the_blocks_around_it():
    slots = [Slot("a", 12), Slot(None, 20), Slot("a", 12)]

    result = blocks.group_blocks(slots)

    assert result == [
        blocks.Block(kind="zones", slots=[Slot("a", 12)], minutes=12),
        blocks.Block(kind="idle", slots=[], minutes=20),
        blocks.Block(kind="zones", slots=[Slot("a", 12)], minutes=12),
    ]


def test_a_zone_repeated_within_a_block_is_preserved_not_merged():
    # Cycle-and-soak IS zone repetition: the same zone recurs in one block and
    # each recurrence must survive as its own run.
    slots = [Slot("a", 12), Slot("b", 12), Slot("a", 12), Slot("b", 12)]

    result = blocks.group_blocks(slots)

    assert [s.zone_key for s in result[0].slots] == ["a", "b", "a", "b"]


def test_empty_plan_produces_no_blocks():
    assert blocks.group_blocks([]) == []


# ─── quantize ────────────────────────────────────────────────────────────────
# Rachio takes whole minutes per zone (`int(duration) * 60`), but scaled drought
# levels produce fractional slots (55 * 0.7 = 38.5). Minutes are allocated by
# cumulative floor so the error never accumulates and never rounds UP: the
# quantized block can only finish earlier than planned, never past the window's
# end anchor.

def test_whole_minute_slots_pass_through_unchanged():
    runs = blocks.quantize([Slot("a", 12), Slot("b", 12), Slot("a", 11)])

    assert runs == [
        blocks.ZoneRun("a", 12), blocks.ZoneRun("b", 12), blocks.ZoneRun("a", 11),
    ]


def test_fractional_slots_allocate_by_cumulative_floor():
    # cumulative 2.5, 14.5, 20.5 -> floors 2, 14, 20 -> runs 2, 12, 6
    runs = blocks.quantize([Slot("a", 2.5), Slot("b", 12), Slot("a", 6.0)])

    assert runs == [
        blocks.ZoneRun("a", 2), blocks.ZoneRun("b", 12), blocks.ZoneRun("a", 6),
    ]


def test_sub_minute_slot_is_dropped_rather_than_sent_as_zero():
    # int(0.4) * 60 == 0 would put a zero-duration zone in the schedule.
    runs = blocks.quantize([Slot("a", 12), Slot("b", 0.4)])

    assert runs == [blocks.ZoneRun("a", 12)]


def test_dropped_fractions_can_still_add_up_to_a_whole_minute():
    # No slot reaches a minute alone, but together they do — the cumulative
    # allocation credits that minute rather than discarding all three.
    runs = blocks.quantize([Slot("a", 0.4), Slot("b", 0.4), Slot("c", 0.4)])

    assert runs == [blocks.ZoneRun("c", 1)]


def test_repeated_zones_keep_their_separate_runs_and_order():
    runs = blocks.quantize([Slot("a", 12), Slot("b", 12), Slot("a", 12)])

    assert [(r.zone_key, r.minutes) for r in runs] == [("a", 12), ("b", 12), ("a", 12)]


def test_every_run_is_a_positive_whole_number_of_minutes():
    runs = blocks.quantize([Slot("a", 2.5), Slot("b", 0.4), Slot("c", 8.9)])

    for run in runs:
        assert isinstance(run.minutes, int)
        assert run.minutes >= 1


def test_quantized_total_never_exceeds_the_planned_total():
    # The guarantee the window depends on: quantization can pull the finish
    # earlier, never later. Exercised against every drought runtime_scale.
    raw = [55.0, 12.0, 52.0, 12.0, 47.0, 71.0, 61.0]
    for scale in (1.0, 0.9, 0.8, 0.7, 0.5):
        slots = [Slot(f"z{i}", m * scale) for i, m in enumerate(raw)]
        planned = sum(s.minutes for s in slots)

        total = sum(r.minutes for r in blocks.quantize(slots))

        assert total <= planned + 1e-9, f"scale {scale} rounded up"
        assert total > planned - 1, f"scale {scale} lost more than a minute"


def test_float_dust_does_not_cost_a_minute():
    # 0.7 scaling yields values like 8.899999999999999; a naive floor would
    # drop these to 8 and shed a minute per slot across a long block.
    slots = [Slot("a", 71 * 0.7), Slot("b", 61 * 0.7)]  # 49.7, 42.699999999999996

    runs = blocks.quantize(slots)

    assert [r.minutes for r in runs] == [49, 43]


# ─── service payload ─────────────────────────────────────────────────────────
# Built here rather than in the app file so the whole payload is under test and
# `start_block` is a single service.call with no logic in it.

def test_entity_ids_repeat_a_zone_once_per_run_in_order():
    runs = [blocks.ZoneRun("a", 12), blocks.ZoneRun("b", 12), blocks.ZoneRun("a", 11)]
    switches = {"a": "switch.zone_a", "b": "switch.zone_b"}

    assert blocks.entity_ids(runs, switches) == [
        "switch.zone_a", "switch.zone_b", "switch.zone_a",
    ]


def test_duration_csv_pairs_positionally_with_the_entity_ids():
    runs = [blocks.ZoneRun("a", 12), blocks.ZoneRun("b", 12), blocks.ZoneRun("a", 11)]

    assert blocks.duration_csv(runs) == "12,12,11"


def test_duration_csv_of_a_single_run_has_no_separator():
    assert blocks.duration_csv([blocks.ZoneRun("a", 12)]) == "12"


# ─── delivered ───────────────────────────────────────────────────────────────
# Rachio advances through a block on its own, so when a block is cut short the
# scheduler knows only how long the block ran. Which zones that time reached is
# recovered from the run order — water is credited by measurement, never by
# assumption. Elapsed is in seconds, as the watch loop reports it.

RUNS = [blocks.ZoneRun("a", 12), blocks.ZoneRun("b", 12), blocks.ZoneRun("a", 11)]


def test_a_block_that_finished_credits_every_run():
    assert blocks.delivered(RUNS, 35 * 60) == {"a": 23, "b": 12}


def test_a_zone_reached_twice_sums_its_runs():
    # "a" ran 12 in the first run and 11 in the third.
    assert blocks.delivered(RUNS, 35 * 60)["a"] == 23


def test_a_block_cut_mid_run_credits_only_the_time_that_zone_got():
    # Stopped 6 minutes into the second run: a got its full 12, b got 6.
    assert blocks.delivered(RUNS, 18 * 60) == {"a": 12, "b": 6}


def test_zones_never_reached_are_absent_rather_than_zero():
    result = blocks.delivered(RUNS, 5 * 60)

    assert result == {"a": 5}
    assert "b" not in result


def test_a_block_stopped_before_anything_ran_credits_nothing():
    assert blocks.delivered(RUNS, 0) == {}


def test_overshoot_from_the_polling_interval_does_not_over_credit():
    # The watch polls on an interval, so elapsed can exceed the block length.
    assert blocks.delivered(RUNS, 40 * 60) == {"a": 23, "b": 12}


def test_elapsed_exactly_on_a_run_boundary_credits_that_run_and_no_more():
    assert blocks.delivered(RUNS, 12 * 60) == {"a": 12}


def test_a_sliver_past_a_boundary_does_not_credit_the_next_zone():
    # A poll landing a fraction of a second past a run boundary must not put a
    # zone in the recap as watered when it delivered nothing.
    assert blocks.delivered(RUNS, 12 * 60 + 0.001) == {"a": 12}


# ─── composition ─────────────────────────────────────────────────────────────
# The units above are correct in isolation; these check the property the
# watering window actually depends on, against whole plans from the real planner.
#
# The graph below is synthetic but exercises every code path a real multi-zone
# yard does: two geography groups, in-group adjacency chains, and one adjacency
# edge that crosses the group boundary (zone_g <-> zone_b) so adjacency exclusion
# and geography grouping are both driven, not just one of them.

SITE_RUNTIMES = {
    "zone_a": 55, "zone_b": 52, "zone_c": 47,
    "zone_d": 71, "zone_e": 61, "zone_f": 71, "zone_g": 47,
}
SITE_GEO = {
    "zone_a": "north", "zone_b": "north", "zone_c": "north",
    "zone_d": "south", "zone_e": "south", "zone_f": "south",
    "zone_g": "south",
}
SITE_ADJACENCY = {
    "zone_a": ("zone_c",),
    "zone_b": ("zone_g", "zone_c"),
    "zone_c": ("zone_b", "zone_a"),
    "zone_d": ("zone_f",),
    "zone_e": ("zone_f",),
    "zone_f": ("zone_e", "zone_d"),
    "zone_g": ("zone_b",),
}


def _site_plan(zone_keys, scale):
    return plan.build_sequence(
        zones_priority=zone_keys,
        minutes_by_zone={z: SITE_RUNTIMES[z] * scale for z in zone_keys},
        geo=SITE_GEO, adjacency=SITE_ADJACENCY,
        cycle_min=T.cycle_minutes, soak_min=T.soak_minutes,
    )


@pytest.mark.parametrize("scale", [1.0, 0.9, 0.8, 0.7, 0.5])
@pytest.mark.parametrize("zone_keys", [
    ["zone_d", "zone_f", "zone_e", "zone_g"],   # a representative multi-zone run
    list(SITE_RUNTIMES),                         # all seven
    ["zone_d", "zone_b"],
    ["zone_g"],
])
def test_quantized_blocks_never_outlast_the_plan_they_came_from(zone_keys, scale):
    slots = _site_plan(zone_keys, scale)

    total = 0.0
    for block in blocks.group_blocks(slots):
        if block.kind == "idle":
            total += block.minutes
        else:
            total += sum(r.minutes for r in blocks.quantize(block.slots))

    assert total <= plan.span_of(slots) + 1e-9


def test_a_real_cycle_and_soak_block_repeats_zones_within_one_call():
    # The property that makes one Rachio call per block possible at all.
    slots = _site_plan(["zone_d", "zone_f", "zone_e", "zone_g"], 1.0)

    first = blocks.group_blocks(slots)[0]
    runs = blocks.quantize(first.slots)
    switches = {z: f"switch.{z}" for z in SITE_RUNTIMES}
    ids = blocks.entity_ids(runs, switches)

    assert len(ids) > len(set(ids)), "expected repeated zones in one block"
    assert len(blocks.duration_csv(runs).split(",")) == len(ids)
