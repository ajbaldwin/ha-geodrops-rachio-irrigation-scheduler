import datetime as dt

from irrigation_lib import report_format as rf


def result(**kw):
    base = dict(
        watered=["zone_a", "zone_b"],
        uncompleted={"zone_c": "offline"},
        start="02:14", end="04:55",
        per_zone_minutes={"zone_a": 24.0, "zone_b": 12.0},
        standby=False,
    )
    base.update(kw)
    return rf.RunResult(**base)


def test_display_name_prettifies_zone_keys():
    assert rf.display_name("zone_a") == "Zone A"
    assert rf.display_name("north_bed") == "North Bed"
    assert rf.display_name("corner_strip") == "Corner Strip"


def test_notification_lists_watered_and_times():
    msg = rf.format_notification(result())
    assert "Zone A" in msg and "Zone B" in msg
    assert "02:14" in msg and "04:55" in msg


def test_notification_uses_display_names_not_raw_keys():
    msg = rf.format_notification(result())
    assert "zone_a" not in msg
    assert "zone_b" not in msg
    assert "zone_c" not in msg


def test_notification_lists_uncompleted_with_reason():
    msg = rf.format_notification(result())
    assert "Zone C" in msg
    assert "offline" in msg


def test_standby_notification():
    msg = rf.format_notification(result(standby=True, watered=[], uncompleted={}))
    assert "Standby" in msg


def test_calendar_shows_disease_window_not_watering_span():
    # Nothing watered: the old code printed "Window: 04:52–04:52" because it
    # showed the watering span (zero) rather than the disease window.
    title, desc = rf.format_calendar(
        result(watered=[], per_zone_minutes={}, uncompleted={},
               start="04:52", end="04:52",
               window_start="00:52", window_end="04:52", window_hours=4.0),
        12, 20,
    )
    assert "00:52–04:52" in desc
    assert "4.0h" in desc
    assert "04:52–04:52" not in desc


def test_calendar_separates_window_from_watering_times():
    title, desc = rf.format_calendar(
        result(window_start="00:52", window_end="04:55", window_hours=4.0), 12, 20,
    )
    assert "00:52–04:55" in desc      # disease window
    assert "02:14–04:55" in desc      # actual watering


def test_notification_shows_window_when_nothing_watered():
    msg = rf.format_notification(
        result(watered=[], per_zone_minutes={}, uncompleted={},
               window_start="00:52", window_end="04:52", window_hours=4.0),
    )
    assert "No zones watered" in msg
    assert "00:52–04:52" in msg


def test_calendar_title_and_description():
    title, desc = rf.format_calendar(result(), 12, 20)
    assert "2" in title  # 2 zones watered
    assert "Zone A" in desc
    assert "24" in desc            # per-zone minutes
    assert "12 min" in desc or "12/20" in desc  # cycle/soak settings
    assert "Zone C" in desc        # uncompleted listed


def test_calendar_uses_display_names_not_raw_keys():
    title, desc = rf.format_calendar(result(), 12, 20)
    assert "zone_a" not in desc
    assert "zone_c" not in desc


def test_preview_lists_planned_with_window():
    msg = rf.format_preview(
        "L3 Survival",
        [("zone_a", 39.0), ("zone_b", 33.0)],
        {"zone_c": "offline"},
        window_note="Window: 23:44–04:44 (5.0h, pressure 1/3: stagnant), ends 1 min before dawn",
        watering_note="Watering: 72 min, starts 03:32",
    )
    assert "L3 Survival" in msg
    assert "Zone A" in msg and "39 min" in msg
    assert "Would water 2" in msg
    assert "Zone C" in msg and "offline" in msg
    # Raw config keys must not leak into operator-facing text.
    assert "zone_a" not in msg and "zone_c" not in msg
    # The window is the disease window, not the watering span.
    assert "23:44–04:44 (5.0h" in msg
    assert "Watering: 72 min, starts 03:32" in msg


def test_preview_no_title_duplication_and_window_note():
    msg = rf.format_preview(
        "L1", [("a", 12.0)], {},
        window_note="Window: 00:30–05:30 (5.0h, pressure 1/3: stagnant), ends 1 min before dawn",
        watering_note="Watering: 12 min, starts 05:18",
    )
    assert not msg.startswith("Irrigation")  # title carries that; body must not repeat
    assert "Window: 00:30–05:30 (5.0h" in msg and "stagnant" in msg


def test_preview_nothing_planned_still_shows_window():
    msg = rf.format_preview(
        "L0 Health", [], {},
        window_note="Window: 22:00–04:00 (6.0h, pressure 0/3: none), ends 1 min before dawn",
    )
    assert "nothing" in msg.lower()
    # Even with no zones, the window is the real 6h disease window, not 0 min.
    assert "22:00–04:00 (6.0h" in msg


def test_preview_skipped_section():
    msg = rf.format_preview(
        "L1", [("a", 12.0)], {"b": "insufficient window"}
    )
    assert "Skipped" in msg
    assert "insufficient window" in msg


def test_calendar_standby_note():
    title, desc = rf.format_calendar(result(standby=True, watered=[]), 12, 20)
    assert title == "Irrigation Standby"


# ─── calendar_span ───────────────────────────────────────────────────────────
# HA's calendar.create_event rejects a pair of datetimes in DIFFERENT timezones
# ("Expected all values to have the same timezone"). The run's start is derived
# from sensor.sun_next_dawn, which HA serves in UTC, while the finish time is
# local — aware/aware comparisons across zones work fine in Python, so nothing
# catches this until the service call fails and takes the whole report with it.

UTC = dt.timezone.utc
EAST = dt.timezone(dt.timedelta(hours=-4))


def test_span_of_a_real_run_puts_both_ends_in_one_timezone():
    started = dt.datetime(2026, 7, 28, 4, 0, tzinfo=UTC)     # as sun_next_dawn gives it
    ended = dt.datetime(2026, 7, 28, 5, 11, tzinfo=EAST)     # as datetime.now() gives it

    start, end = rf.calendar_span(started, ended, fallback=ended)

    assert start.utcoffset() == end.utcoffset()


def test_span_of_a_real_run_preserves_the_actual_instants():
    started = dt.datetime(2026, 7, 28, 4, 0, tzinfo=UTC)
    ended = dt.datetime(2026, 7, 28, 5, 11, tzinfo=EAST)

    start, end = rf.calendar_span(started, ended, fallback=ended)

    assert start == started and end == ended


def test_no_watering_falls_back_to_a_one_minute_marker():
    anchor = dt.datetime(2026, 7, 28, 4, 55, tzinfo=UTC)

    start, end = rf.calendar_span(None, None, fallback=anchor)

    assert end - start == dt.timedelta(minutes=1)
    assert start.utcoffset() == end.utcoffset()


def test_a_zero_length_interval_becomes_a_marker_not_a_rejected_event():
    # Local Calendar enforces a minimum duration of one second, so start == end
    # must never be sent.
    when = dt.datetime(2026, 7, 28, 4, 55, tzinfo=UTC)

    start, end = rf.calendar_span(when, when, fallback=when)

    assert end > start


def test_an_end_before_the_start_is_not_trusted_as_a_span():
    started = dt.datetime(2026, 7, 28, 5, 0, tzinfo=UTC)
    ended = dt.datetime(2026, 7, 28, 4, 0, tzinfo=UTC)

    start, end = rf.calendar_span(started, ended, fallback=started)

    assert end - start == dt.timedelta(minutes=1)


def test_a_naive_fallback_still_yields_one_consistent_timezone():
    naive = dt.datetime(2026, 7, 28, 4, 55)  # run-now passes datetime.now()

    start, end = rf.calendar_span(None, None, fallback=naive)

    assert start.utcoffset() is not None
    assert start.utcoffset() == end.utcoffset()
