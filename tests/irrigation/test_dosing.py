from irrigation_lib import dosing


def dose(**kw):
    args = dict(
        dominant_now=71.0, refill_target=87.0, span_pts=32.0, span_source="live",
        full_refill_min=20.0, refill_depth_mm=8.0, runtime_scale=1.0,
    )
    args.update(kw)
    return dosing.dose_zone(**args)


def test_proportional_half_span_deficit_is_half_full_refill():
    # deficit 16 of span 32 -> frac 0.5
    d = dose(dominant_now=71.0, refill_target=87.0, span_pts=32.0)
    assert d.frac == 0.5
    assert d.minutes == 10.0
    assert d.effective_depth_mm == 4.0
    assert d.deficit_pts == 16.0
    assert d.source == "live"


def test_deficit_exceeds_span_clamps_to_full_refill():
    d = dose(dominant_now=40.0, refill_target=87.0, span_pts=10.0)
    assert d.frac == 1.0
    assert d.minutes == 20.0
    assert d.effective_depth_mm == 8.0


def test_nonpositive_deficit_gives_zero():
    d = dose(dominant_now=90.0, refill_target=87.0, span_pts=32.0)
    assert d.frac == 0.0
    assert d.minutes == 0.0


def test_runtime_scale_multiplies():
    d = dose(dominant_now=71.0, refill_target=87.0, span_pts=32.0, runtime_scale=0.8)
    assert d.frac == 0.5
    assert d.minutes == 8.0  # 20 * 0.5 * 0.8


def test_missing_span_falls_back_to_full_refill():
    for bad in (None, 0.0, -5.0):
        d = dose(span_pts=bad, span_source="live")
        assert d.frac == 1.0
        assert d.minutes == 20.0
        assert d.source == "fallback"


def test_config_source_is_recorded_when_span_usable():
    d = dose(span_source="config")
    assert d.source == "config"
