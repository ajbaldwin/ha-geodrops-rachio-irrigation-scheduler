from irrigation_lib import abort


def test_none_when_clear():
    assert abort.abort_reason(False, False, False) is None


def test_manual_wins():
    assert abort.abort_reason(True, True, True) == "manual-abort"


def test_rain_over_standby():
    assert abort.abort_reason(True, False, True) == "rain-abort"


def test_standby_alone():
    assert abort.abort_reason(True, False, False) == "standby"


# ─── watch_step ──────────────────────────────────────────────────────────────
# One poll of the in-block watch. Extracted from the app file and tested here
# because the three guards INTERACT, and their interaction is what went wrong:
# on 2026-07-28 the adverse-conditions automation stopped a block within seconds
# of its start, before the first poll ever saw the zone on. "Seen on first" then
# suppressed the external-stop verdict for the entire 12-minute block, which was
# scored as a full success and credited water that never fell.

CONFIRM, GRACE, POLLS = 90, 90, 2
BLOCK = 720  # a 12-minute block


def step(running, seen_on, misses, elapsed, total=BLOCK):
    return abort.watch_step(running, seen_on, misses, elapsed, total,
                            CONFIRM, GRACE, POLLS)


def test_watering_seen_clears_any_accumulated_misses():
    assert step(True, False, 1, 60) == (None, True, 0)


def test_a_slow_start_is_not_yet_a_failure():
    assert step(False, False, 0, 60) == (None, False, 0)


def test_a_block_that_never_started_is_caught_once_the_window_passes():
    verdict, _seen, _misses = step(False, False, 0, CONFIRM)
    assert verdict == "never-started"


def test_a_block_that_never_started_is_caught_even_on_a_short_block():
    # A 3-minute block is mostly inside the end grace, but a start that never
    # happened must still be reported rather than scored as a completion.
    verdict, _seen, _misses = step(False, False, 0, CONFIRM, total=180)
    assert verdict == "never-started"


def test_one_empty_poll_mid_block_is_not_yet_an_external_stop():
    assert step(False, True, 0, 300) == (None, True, 1)


def test_two_consecutive_empty_polls_mid_block_are_an_external_stop():
    verdict, _seen, misses = step(False, True, 1, 300)
    assert verdict == "external-stop"
    assert misses == 2


def test_watering_ending_inside_the_end_grace_is_a_normal_finish():
    # Rachio's clock and ours are independent; a block finishing a poll early is
    # completion, not a stop.
    assert step(False, True, 1, BLOCK - GRACE) == (None, True, 1)


def test_the_end_grace_does_not_excuse_a_stop_before_it():
    verdict, _seen, _misses = step(False, True, 1, BLOCK - GRACE - 1)
    assert verdict == "external-stop"


# ─── the 2026-08-10 zone hand-off ────────────────────────────────────────────
# Three nights ended early because a NORMAL transition between two zones inside
# a block looked like an external stop. The valve closes, the next opens, and
# HA's view of both lags Rachio's cloud — so for a poll or two nothing reads
# `on`. With a two-poll threshold that is a verdict; the run ended, and the stop
# then cut short the zone that had just legitimately started.

HANDOFF_POLLS = 6  # matches EXTERNAL_STOP_POLLS in the app


def handoff_step(running, seen_on, misses, elapsed, total=BLOCK):
    return abort.watch_step(running, seen_on, misses, elapsed, total,
                            CONFIRM, GRACE, HANDOFF_POLLS)


def test_a_handoff_gap_shorter_than_the_threshold_never_renders_a_verdict():
    misses, elapsed = 0, 300
    for _poll in range(HANDOFF_POLLS - 1):
        verdict, _seen, misses = handoff_step(False, True, misses, elapsed)
        assert verdict is None, f"aborted after only {misses} empty poll(s)"
        elapsed += 30
    assert misses == HANDOFF_POLLS - 1


def test_watering_resuming_after_a_gap_clears_the_count_entirely():
    # The next zone finally shows up: the run must carry on as if nothing
    # happened, with no residue that could trip a later verdict early.
    _verdict, _seen, misses = handoff_step(False, True, 0, 300)
    _verdict, _seen, misses = handoff_step(False, True, misses, 330)
    verdict, seen_on, misses = handoff_step(True, True, misses, 360)

    assert verdict is None
    assert seen_on is True
    assert misses == 0


def test_a_genuine_stop_is_still_caught_once_the_threshold_is_reached():
    misses, elapsed = 0, 300
    verdict = None
    for _poll in range(HANDOFF_POLLS):
        verdict, _seen, misses = handoff_step(False, True, misses, elapsed)
        elapsed += 30

    assert verdict == "external-stop"
