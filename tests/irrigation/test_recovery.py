from irrigation_lib import recovery
from irrigation_lib.program import Step


# ─── verdict ─────────────────────────────────────────────────────────────────
# The decision after a segment aborts. delivered_since_issue is minutes watered
# under the CURRENT schedule; it separates "Rachio dropped a working schedule"
# from "Rachio will not water". Cap default in production is 2.

def v(reason, delivered, is_recovery, retries, cap=2):
    return recovery.verdict(reason, delivered, is_recovery, retries, cap)


def test_real_stops_never_recover():
    for reason in ("rain-abort", "standby", "manual-abort", "external-stop"):
        assert v(reason, 30, False, 0) == recovery.CONTINUE_ABORT


def test_initial_schedule_that_never_watered_is_genuine_abort():
    # never-started on the FIRST schedule with nothing delivered = Rachio never
    # watered at all. This is today's behavior, unchanged.
    assert v("never-started", 0, False, 0) == recovery.CONTINUE_ABORT


def test_delivered_then_dropped_recovers():
    # Water fell, then the schedule vanished: the budget cap. Re-issue.
    assert v("never-started", 36, False, 0) == recovery.RECOVER


def test_second_drop_still_recovers_under_cap():
    assert v("never-started", 12, True, 1) == recovery.RECOVER


def test_cap_reached_gives_up():
    assert v("never-started", 12, True, 2) == recovery.GIVE_UP


def test_fresh_schedule_that_never_starts_gives_up():
    # A re-issued schedule with nothing delivered = Rachio refusing NOW. Don't
    # loop even though retries remain.
    assert v("never-started", 0, True, 1) == recovery.GIVE_UP


def test_cap_zero_disables_recovery():
    assert v("never-started", 36, False, 0, cap=0) == recovery.GIVE_UP


# ─── remaining_after ─────────────────────────────────────────────────────────

def test_remaining_reincludes_the_failed_water_step():
    steps = [
        Step("water", "z", 12), Step("pause", None, 20),
        Step("water", "z", 12), Step("pause", None, 20),
        Step("water", "z", 12),
    ]
    # Dropped at index 4 (the 3rd water step): remainder is just that step.
    assert recovery.remaining_after(steps, 4) == [Step("water", "z", 12)]


def test_remaining_from_mid_program_keeps_the_tail():
    steps = [
        Step("water", "z", 12), Step("pause", None, 20),
        Step("water", "z", 12), Step("pause", None, 20),
        Step("water", "z", 8),
    ]
    assert recovery.remaining_after(steps, 2) == steps[2:]


# ─── startup_action ──────────────────────────────────────────────────────────
# The decision _on_startup makes after a restart when NO Rachio valve is open.
# A "waiting marker" (written just before the pre-dawn sleep, cleared the instant
# the wait ends) says a run was planned and waiting. `window_end` is the ISO time
# the watering window closes (dawn − end_offset). now is the current ISO time.

def sa(marker, now_iso):
    return recovery.startup_action(marker, now_iso)


def test_no_marker_ignores():
    # No run was waiting: startup does nothing (today's behavior).
    assert sa(None, "2026-08-30T05:00:00") == recovery.IGNORE


def test_marker_before_window_end_re_arms():
    # Restart landed inside the window: re-plan and water.
    marker = {"window_end": "2026-08-30T06:00:00"}
    assert sa(marker, "2026-08-30T02:29:00") == recovery.RE_ARM


def test_marker_after_window_end_is_missed():
    # Restart finished after the window closed: record missed, water nothing.
    marker = {"window_end": "2026-08-30T06:00:00"}
    assert sa(marker, "2026-08-30T06:50:00") == recovery.MISSED


def test_marker_exactly_at_window_end_is_missed():
    # At the boundary there is no window left to water in.
    marker = {"window_end": "2026-08-30T06:00:00"}
    assert sa(marker, "2026-08-30T06:00:00") == recovery.MISSED


def test_malformed_marker_ignores():
    # A marker without a usable window_end must never crash startup; treat it as
    # nothing-to-do so the safety-stop path still runs.
    assert sa({}, "2026-08-30T05:00:00") == recovery.IGNORE
    assert sa({"window_end": "not-a-time"}, "2026-08-30T05:00:00") == recovery.IGNORE
