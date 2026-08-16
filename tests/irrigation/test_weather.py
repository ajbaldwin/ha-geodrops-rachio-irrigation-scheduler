from irrigation_lib import config, weather

T = config.Tunables()


def wx(temp=60, rh=50, wind=10, dew=False, rain=0.0, precip="none"):
    return weather.WeatherReading(
        temp_f=temp, rh_pct=rh, wind_mph=wind, dew_formed=dew,
        rain_last_hour_mm=rain, precip_type=precip,
    )


def test_pressure_breakdown_none_active():
    b = weather.pressure_breakdown(wx(), T)
    assert b == {"warm": False, "humid": False, "stagnant": False, "count": 0}


def test_pressure_breakdown_all_active():
    b = weather.pressure_breakdown(wx(temp=75, rh=95, wind=1), T)
    assert b["warm"] and b["humid"] and b["stagnant"]
    assert b["count"] == 3


def test_pressure_breakdown_dew_counts_as_humid():
    b = weather.pressure_breakdown(wx(dew=True), T)
    assert b["humid"] is True
    assert b["count"] == 1


def test_pressure_signals_equals_breakdown_count():
    w = wx(temp=75, rh=50, wind=10)  # warm only
    assert weather.pressure_signals(w, T) == weather.pressure_breakdown(w, T)["count"]


def test_no_pressure_gives_base_cap():
    assert weather.pressure_signals(wx(), T) == 0
    assert weather.window_cap_minutes(wx(), T) == 6 * 60


def test_all_pressure_gives_disease_cap():
    w = wx(temp=75, rh=95, wind=1)
    assert weather.pressure_signals(w, T) == 3
    assert weather.window_cap_minutes(w, T) == 3 * 60


def test_dew_counts_as_humid_signal():
    w = wx(temp=60, rh=50, wind=10, dew=True)
    assert weather.pressure_signals(w, T) == 1


def test_partial_pressure_interpolates():
    w = wx(temp=75, rh=50, wind=10)  # only warm
    # cap = 6 - (6-3)*(1/3) = 5 hours
    assert weather.window_cap_minutes(w, T) == 5 * 60


def test_is_hail_detects_hail_types():
    assert weather.is_hail(wx(precip="hail")) is True
    assert weather.is_hail(wx(precip="rain_hail")) is True
    assert weather.is_hail(wx(precip="rain")) is False
    assert weather.is_hail(wx()) is False


def test_hail_aborts():
    assert weather.is_rain_abort(wx(precip="hail"), T) is True
    assert weather.is_rain_abort(wx(precip="rain_hail"), T) is True


def test_spray_zone_running_rejects_rain_with_no_gauge_accumulation():
    # a spray-flagged zone's spray is misread as precip_type "rain" by the
    # Tempest, but the rain GAUGE stays at 0. Humidity is not a valid
    # discriminator here: this
    # scheduler runs in the humid pre-dawn window, so RH is high whether or not
    # it is raining. Neither low nor high RH may wave the spray through.
    assert weather.is_rain_abort(wx(precip="rain", rh=50), T, True) is False
    # The 2026-08-11 false abort: RH 93.75%, gauge 0, aborted our own spray.
    assert weather.is_rain_abort(wx(precip="rain", rh=95), T, True) is False


def test_spray_zone_running_believes_rain_when_the_gauge_accumulates():
    # Genuine rain accumulates measurable depth within the sustain window;
    # drifting overspray does not. The gauge is the corroboration, not RH.
    assert weather.is_rain_abort(wx(precip="rain", rh=50, rain=0.5), T, True) is True


def test_hail_overrides_spray_rejection():
    assert weather.is_rain_abort(wx(precip="hail", rh=50), T, True) is True


def test_rain_abort_on_precip_type():
    assert weather.is_rain_abort(wx(precip="rain"), T) is True


def test_rain_abort_on_recent_rain():
    assert weather.is_rain_abort(wx(rain=0.5), T) is True


def test_no_rain_abort_when_dry():
    assert weather.is_rain_abort(wx(), T) is False


def test_rain_skip_requires_both_probability_and_amount():
    # 7.58mm refill * 0.5 fraction = 3.79mm threshold
    assert weather.is_rain_skip(85, 12.0, 7.58, T) is True


def test_rain_skip_rejects_high_probability_trace_amount():
    # The whole point of the amount gate: 85% chance of 0.2mm is not a refill.
    assert weather.is_rain_skip(85, 0.2, 7.58, T) is False


def test_rain_skip_rejects_low_probability_heavy_amount():
    assert weather.is_rain_skip(30, 20.0, 7.58, T) is False


def test_rain_skip_boundary_values_skip():
    # Thresholds are inclusive: exactly at the limit still skips.
    assert weather.is_rain_skip(70, 3.79, 7.58, T) is True


def test_rain_skip_scales_with_refill_depth():
    # 4mm covers >half of a 6.35mm refill but not an 8.89mm one.
    assert weather.is_rain_skip(90, 4.0, 6.35, T) is True
    assert weather.is_rain_skip(90, 4.0, 8.89, T) is False


def test_rain_skip_fails_open_on_missing_data():
    assert weather.is_rain_skip(None, 12.0, 7.58, T) is False
    assert weather.is_rain_skip(85, None, 7.58, T) is False
    assert weather.is_rain_skip(85, 12.0, None, T) is False


def test_rain_skip_fails_open_on_unusable_refill_depth():
    # No zones planned, or depths unavailable -> no threshold to compare against.
    assert weather.is_rain_skip(99, 50.0, 0.0, T) is False


# ─── rain_sustain_step ───────────────────────────────────────────────────────
# The sustain clock, lifted out of the app file. It is deliberately unable to
# abort on first sight — plain rain must persist before a RUNNING block is
# killed, mirroring the automation's 2m30s `for:`. That is correct mid-run and
# was wrong at the start decision, where it meant a run could launch straight
# into precipitation the automation would stop instantly (2026-07-28).

SUSTAIN = 150.0


def test_a_cleared_condition_resets_the_clock():
    assert weather.rain_sustain_step(False, False, 1000.0, 1200.0, SUSTAIN) == (False, None)


def test_hail_aborts_immediately_without_waiting():
    abort_now, _since = weather.rain_sustain_step(True, True, None, 1000.0, SUSTAIN)
    assert abort_now is True


def test_the_first_sighting_starts_the_clock_and_defers():
    # The behaviour that let a run start into rain: never True on first call.
    abort_now, since = weather.rain_sustain_step(True, False, None, 1000.0, SUSTAIN)
    assert abort_now is False
    assert since == 1000.0


def test_rain_shorter_than_the_sustain_does_not_abort():
    abort_now, since = weather.rain_sustain_step(True, False, 1000.0, 1100.0, SUSTAIN)
    assert abort_now is False
    assert since == 1000.0


def test_rain_lasting_the_sustain_aborts():
    abort_now, _since = weather.rain_sustain_step(True, False, 1000.0, 1150.0, SUSTAIN)
    assert abort_now is True


def test_a_blip_that_clears_forfeits_its_accumulated_time():
    # 2026-07-28: precip at the block start, gone before the next poll, so the
    # clock never reached the threshold and the rain gate never fired at all.
    _abort1, since = weather.rain_sustain_step(True, False, None, 1000.0, SUSTAIN)
    _abort2, since = weather.rain_sustain_step(False, False, since, 1030.0, SUSTAIN)
    assert since is None

    abort_now, since = weather.rain_sustain_step(True, False, since, 1060.0, SUSTAIN)
    assert abort_now is False
    assert since == 1060.0


# ─── pressure_agreement ──────────────────────────────────────────────────────
# Calibration compares the FORECAST overnight means against the OBSERVED ones
# for the same 00:00-06:00 window. What matters is not the numeric error but
# whether each step-function signal agreed: a forecast RH of 89.5 against an
# observed 90.5 is a one-point miss that flips `humid` and moves the disease
# window by a full hour.

def bd(warm, humid, stagnant):
    return {"warm": warm, "humid": humid, "stagnant": stagnant,
            "count": int(warm) + int(humid) + int(stagnant)}


def test_identical_breakdowns_agree_on_every_signal():
    a = weather.pressure_agreement(bd(True, False, False), bd(True, False, False))
    assert a["mismatches"] == []
    assert a["signals"] == {"warm": "agree", "humid": "agree", "stagnant": "agree"}


def test_a_signal_the_forecast_missed_is_reported_as_forecast_low():
    # The direction that matters: forecast said no pressure, reality had it, so
    # the disease window was sized too generously.
    a = weather.pressure_agreement(bd(True, False, False), bd(True, True, False))
    assert a["mismatches"] == ["humid"]
    assert a["signals"]["humid"] == "forecast-low"


def test_a_signal_the_forecast_overcalled_is_reported_as_forecast_high():
    a = weather.pressure_agreement(bd(True, True, False), bd(True, False, False))
    assert a["signals"]["humid"] == "forecast-high"


def test_both_counts_are_carried_for_the_window_impact():
    a = weather.pressure_agreement(bd(True, False, False), bd(True, True, True))
    assert a["forecast_count"] == 1
    assert a["observed_count"] == 3


def test_every_signal_can_disagree_at_once():
    a = weather.pressure_agreement(bd(False, False, False), bd(True, True, True))
    assert a["mismatches"] == ["warm", "humid", "stagnant"]


def test_a_missing_signal_is_treated_as_absent_rather_than_crashing():
    a = weather.pressure_agreement({"count": 0}, bd(True, False, False))
    assert a["signals"]["warm"] == "forecast-low"
