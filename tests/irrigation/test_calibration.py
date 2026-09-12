import pytest

from irrigation_lib import calibration
from irrigation_lib.config import Tunables


def test_efficacy_to_span():
    assert calibration.efficacy_to_span(0.5, 40.0) == 20.0


def test_first_probe_uses_fraction_or_floor():
    t = Tunables()  # fraction 1/3, floor 10
    assert calibration.probe_minutes(24.0, None, None, t) == 10.0            # 1/3*24=8 -> floor 10
    assert calibration.probe_minutes(71.0, None, None, t) == pytest.approx(71 / 3)   # 1/3*71≈23.7
    assert calibration.probe_minutes(100.0, None, None, t) == pytest.approx(100 / 3)


def test_probe_grows_on_small_rise():
    t = Tunables()  # growth 1.5, measurable 3.0
    assert calibration.probe_minutes(40.0, 10.0, 1.0, t) == 15.0      # rise<3 -> grow
    assert calibration.probe_minutes(40.0, 10.0, 5.0, t) == 10.0      # rise>=3 -> hold


def test_saturation_cap():
    t = Tunables()  # saturation_reject 95
    assert calibration.cap_for_saturation(20.0, 90.0, 0.5, t) == 10.0  # room 5 / eff .5 = 10
    assert calibration.cap_for_saturation(20.0, 50.0, None, t) == 20.0  # unknown eff, unchanged


def obs(pre=60, mins=30, settled=72, training=False, rained=False, ok=True):
    return calibration.Observation("z", pre, mins, settled, training, rained, ok)


def test_classify_priority():
    t = Tunables()
    assert calibration.classify(obs(ok=False), t) == "unavailable"
    assert calibration.classify(obs(training=True), t) == "training"
    assert calibration.classify(obs(rained=True), t) == "rain"
    assert calibration.classify(obs(settled=96), t) == "saturated"
    assert calibration.classify(obs(settled=60), t) == "no_rise"
    assert calibration.classify(obs(), t) == "ok"


def test_update_and_converged():
    t = Tunables()
    e = calibration.update_efficacy(None, obs(60, 30, 72), t)  # 12/30 = 0.4
    assert e == 0.4
    assert calibration.converged([0.40, 0.42, 0.38], t) is True
    assert calibration.converged([0.40, 0.50, 0.38], t) is False
    assert calibration.converged([0.40, 0.42], t) is False


def test_next_state():
    t = Tunables()  # convergence_samples=3
    assert calibration.next_state("calibrating", False, True, 0, t) == "recalibrating"   # training wins
    assert calibration.next_state("converged", True, True, 0, t) == "recalibrating"       # training wins from converged too
    assert calibration.next_state("calibrating", True, False, 0, t) == "converged"
    assert calibration.next_state("recalibrating", True, False, 0, t) == "converged"
    assert calibration.next_state("converged", False, False, 3, t) == "calibrating"        # miss streak
    assert calibration.next_state("converged", False, False, 1, t) == "converged"          # streak too low
    assert calibration.next_state("calibrating", False, False, 0, t) == "calibrating"      # unchanged


def test_should_probe():
    t = Tunables()  # probe_headroom_ceiling=85
    assert calibration.should_probe("calibrating", 70.0, False, t) is True
    assert calibration.should_probe("recalibrating", 70.0, False, t) is True
    assert calibration.should_probe("converged", 70.0, False, t) is False   # not calibrating
    assert calibration.should_probe("calibrating", 70.0, True, t) is False  # pinned
    assert calibration.should_probe("calibrating", 90.0, False, t) is False # no headroom


import datetime as _dt


def test_exclusion_return_resets_after_threshold():
    now = _dt.datetime(2026, 9, 20, 12, 0, 0)
    rec = {"state": "converged", "efficacy": 0.5, "span_pts": 20.0,
           "recent": [0.5, 0.5, 0.5], "miss_streak": 0, "n_obs": 5,
           "excluded_since": _dt.datetime(2026, 9, 17, 12, 0, 0).isoformat()}  # 72h
    out = calibration.exclusion_return(rec, now, 48.0)
    assert out["state"] == "recalibrating"
    assert out["efficacy"] is None
    assert out["span_pts"] == 0
    assert out["recent"] == []
    assert "excluded_since" not in out
    assert out["n_obs"] == 5  # history count preserved


def test_exclusion_return_keeps_below_threshold():
    now = _dt.datetime(2026, 9, 20, 12, 0, 0)
    rec = {"state": "converged", "efficacy": 0.5, "span_pts": 20.0,
           "excluded_since": _dt.datetime(2026, 9, 20, 0, 0, 0).isoformat()}  # 12h
    out = calibration.exclusion_return(rec, now, 48.0)
    assert out["state"] == "converged"
    assert out["efficacy"] == 0.5
    assert out["span_pts"] == 20.0
    assert "excluded_since" not in out  # stamp cleared


def test_exclusion_return_no_stamp_is_noop():
    now = _dt.datetime(2026, 9, 20, 12, 0, 0)
    rec = {"state": "converged", "efficacy": 0.5}
    out = calibration.exclusion_return(rec, now, 48.0)
    assert out == {"state": "converged", "efficacy": 0.5}


def test_exclusion_return_malformed_stamp_clears_keeps():
    now = _dt.datetime(2026, 9, 20, 12, 0, 0)
    rec = {"state": "converged", "efficacy": 0.5, "excluded_since": "not-a-date"}
    out = calibration.exclusion_return(rec, now, 48.0)
    assert out["state"] == "converged"
    assert out["efficacy"] == 0.5
    assert "excluded_since" not in out


def test_apply_reject_no_rise_records_growth_fields():
    t = Tunables()  # growth 1.5, measurable 3.0, floor 10
    out = calibration.apply_reject({"state": "calibrating"}, "no_rise", 10.0, -0.5, t)
    assert out["last_reject_reason"] == "no_rise"
    assert out["prior_minutes"] == 10.0
    assert out["last_rise"] == -0.5
    # the recorded fields must let the next probe grow, not restart at the floor
    assert calibration.probe_minutes(40.0, out["prior_minutes"], out["last_rise"], t) == 15.0


def test_apply_reject_saturated_shrinks_and_holds():
    t = Tunables()  # shrink 1.5
    out = calibration.apply_reject({"state": "calibrating"}, "saturated", 30.0, 12.0, t)
    assert out["last_reject_reason"] == "saturated"
    assert out["prior_minutes"] == pytest.approx(30.0 / t.probe_shrink)  # 20.0
    assert out["last_rise"] is None
    # next probe holds at the shrunk size (does not grow or restart at floor)
    nxt = calibration.probe_minutes(71.0, out["prior_minutes"], out["last_rise"], t)
    assert nxt == pytest.approx(30.0 / t.probe_shrink)


def test_apply_reject_confounded_reasons_do_not_resize():
    for reason in ("rain", "unavailable", "training"):
        out = calibration.apply_reject({"state": "calibrating"}, reason, 10.0, 2.0, t=Tunables())
        assert out["last_reject_reason"] == reason
        assert "prior_minutes" not in out
        assert "last_rise" not in out


def test_apply_reject_does_not_mutate_input():
    rec = {"state": "calibrating"}
    calibration.apply_reject(rec, "no_rise", 10.0, -0.5, Tunables())
    assert rec == {"state": "calibrating"}
