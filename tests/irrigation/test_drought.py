from irrigation_lib import config, drought

ZONE = config.ZoneConfig(
    key="z", rachio_switch="s", dominant_sensor="d", state_sensor="st",
    quality_sensors=("q1", "q2", "q3"), target_range="moist",
    geography="front", adjacency=(),
)
BANDS = {
    "dry": config.Band(0, 62), "dry_plus": config.Band(62, 67),
    "moist": config.Band(67, 76), "moist_plus": config.Band(76, 87),
    "wet": config.Band(87, 95), "wet_plus": config.Band(95, 100),
}


def test_emergency_profile_never_waters():
    # Level 4 - Emergency: offset -2 puts a `moist` zone in the `dry` band, whose
    # low is 0, so the floor is 0 and `dominant < floor` can never be true.
    from irrigation_lib import evaluate, sensors

    p = config.DroughtProfile(target_offset=-2, trigger_margin=0, runtime_scale=0.5)
    t = drought.effective_target(ZONE, BANDS, p)
    assert t.band_name == "dry"
    assert t.floor == 0

    for dominant in (0.0, 1.0, 25.0, 55.0, 67.0, 100.0):
        reading = sensors.ZoneReading(
            key="z", online=True, offline_reason=None,
            dominant=dominant, index_rank=0, state_text="Dry",
        )
        assert evaluate.evaluate_zone(reading, t).needs_water is False


def test_health_profile_uses_configured_target():
    p = config.DroughtProfile(target_offset=0, trigger_margin=0, runtime_scale=1.0)
    t = drought.effective_target(ZONE, BANDS, p)
    assert t.band_name == "moist"
    assert t.rank == 2
    assert t.floor == 67
    assert t.runtime_scale == 1.0


def test_survival_profile_lowers_band_and_floor():
    p = config.DroughtProfile(target_offset=-1, trigger_margin=6, runtime_scale=0.7)
    t = drought.effective_target(ZONE, BANDS, p)
    assert t.band_name == "dry_plus"   # moist(2) + (-1) = dry_plus(1)
    assert t.rank == 1
    assert t.floor == 62 - 6           # band floor minus trigger margin
    assert t.runtime_scale == 0.7


def test_offset_clamps_at_zero():
    dry_zone = config.ZoneConfig(
        key="z", rachio_switch="s", dominant_sensor="d", state_sensor="st",
        quality_sensors=("q1", "q2", "q3"), target_range="dry",
        geography="front", adjacency=(),
    )
    p = config.DroughtProfile(target_offset=-1, trigger_margin=0, runtime_scale=1.0)
    t = drought.effective_target(dry_zone, BANDS, p)
    assert t.rank == 0
    assert t.band_name == "dry"


def test_offset_clamps_at_five():
    wet_zone = config.ZoneConfig(
        key="z", rachio_switch="s", dominant_sensor="d", state_sensor="st",
        quality_sensors=("q1", "q2", "q3"), target_range="wet_plus",
        geography="front", adjacency=(),
    )
    p = config.DroughtProfile(target_offset=2, trigger_margin=0, runtime_scale=1.0)
    t = drought.effective_target(wet_zone, BANDS, p)
    assert t.rank == 5
    assert t.band_name == "wet_plus"
