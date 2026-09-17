import pytest
import yaml

from irrigation_lib import config


def test_band_rank_orders_low_to_high():
    assert config.band_rank("dry") == 0
    assert config.band_rank("moist") == 2
    assert config.band_rank("wet_plus") == 5


def test_state_rank_maps_enum_text():
    assert config.state_rank("Dry") == 0
    assert config.state_rank("Moist+") == 3
    assert config.state_rank("Wet+") == 5
    assert config.state_rank("Unknown") == -1
    assert config.state_rank("unavailable") == -1


def test_load_config_reads_zones_and_bands(example_config_path):
    cfg = config.load_config(example_config_path)
    # bands are copied verbatim from the example into any real config, so
    # this value is portable.
    assert cfg.bands["moist"].low == 67
    assert len(cfg.zones) == 3
    za = cfg.zones["zone_a"]
    assert za.target_range == "moist"
    assert za.geography == "front"
    assert za.adjacency == ("zone_b",)
    assert len(za.quality_sensors) == 3
    # No exact minutes: runtimes are seeded from Rachio and legitimately move
    # when a zone's characteristics change. The zone ID is the stable contract.
    assert za.runtime_minutes > 0
    assert za.rachio_zone_id == "00000000-0000-0000-0000-000000000000"


def test_adjacency_is_symmetric(example_config_path):
    """Physical neighbours are mutual: if A borders B, B borders A.

    A one-sided edit would silently weaken runoff protection in one direction
    only — the plan builder consults the PREVIOUS zone's list, so a missing
    back-reference lets the pair run consecutively half the time.
    """
    cfg = config.load_config(example_config_path)
    for key, zone in cfg.zones.items():
        for neighbour in zone.adjacency:
            assert neighbour in cfg.zones, f"{key} lists unknown zone {neighbour}"
            assert key in cfg.zones[neighbour].adjacency, (
                f"{key} lists {neighbour}, but {neighbour} does not list {key}"
            )


def test_load_config_parses_drought_profiles(example_config_path):
    cfg = config.load_config(example_config_path)
    assert cfg.drought_profiles["Level 3 - Critical"].target_offset == -1
    assert cfg.drought_profiles["Level 0 - Normal"].runtime_scale == 1.0


def test_drought_profiles_match_ma_rating_levels(example_config_path):
    # Keys must match the input_select.irrigation_drought_level options exactly.
    cfg = config.load_config(example_config_path)
    assert sorted(cfg.drought_profiles) == [
        "Level 0 - Normal",
        "Level 1 - Mild",
        "Level 2 - Significant",
        "Level 3 - Critical",
        "Level 4 - Emergency",
    ]


def test_tunables_defaults_present(example_config_path):
    cfg = config.load_config(example_config_path)
    assert cfg.tunables.cycle_minutes == 12
    assert cfg.tunables.soak_minutes == 20
    assert cfg.tunables.end_offset_minutes == 1


def test_tunables_have_rain_skip_settings(example_config_path):
    cfg = config.load_config(example_config_path)
    assert cfg.tunables.rain_skip_probability_pct == 70
    assert cfg.tunables.rain_skip_refill_fraction == 0.5


def test_disease_thresholds_retuned_for_overnight_means(example_config_path):
    # Overnight is systematically calmer and more humid than the 23:00 snapshot
    # these were originally chosen for; unchanged values would fire every night.
    cfg = config.load_config(example_config_path)
    assert cfg.tunables.warm_temp_f == 68
    assert cfg.tunables.humid_rh_pct == 90
    assert cfg.tunables.stagnant_wind_mph == 2


def test_drought_profiles_carry_rain_skip_horizon(example_config_path):
    cfg = config.load_config(example_config_path)
    assert cfg.drought_profiles["Level 0 - Normal"].rain_skip_horizon_hours == 12
    assert cfg.drought_profiles["Level 1 - Mild"].rain_skip_horizon_hours == 18
    assert cfg.drought_profiles["Level 2 - Significant"].rain_skip_horizon_hours == 24
    assert cfg.drought_profiles["Level 3 - Critical"].rain_skip_horizon_hours == 24


def test_end_anchor_sensor_maps_names_to_entities():
    assert config.end_anchor_sensor("dawn") == "sensor.sun_next_dawn"
    assert config.end_anchor_sensor("sunrise") == "sensor.sun_next_rising"


def test_end_anchor_sensor_falls_back_to_dawn():
    # Dawn is the earlier, more conservative anchor: an unrecognised or missing
    # value must shrink the window, never extend it past sunrise.
    assert config.end_anchor_sensor("typo") == "sensor.sun_next_dawn"
    assert config.end_anchor_sensor(None) == "sensor.sun_next_dawn"
    assert config.end_anchor_sensor("") == "sensor.sun_next_dawn"


def test_drought_profiles_carry_end_anchor(example_config_path):
    cfg = config.load_config(example_config_path)
    assert cfg.drought_profiles["Level 0 - Normal"].end_anchor == "sunrise"
    assert cfg.drought_profiles["Level 1 - Mild"].end_anchor == "sunrise"
    assert cfg.drought_profiles["Level 2 - Significant"].end_anchor == "sunrise"
    assert cfg.drought_profiles["Level 3 - Critical"].end_anchor == "dawn"


def test_drought_profile_end_anchor_defaults_to_dawn():
    p = config.DroughtProfile(target_offset=0, trigger_margin=0, runtime_scale=1.0)
    assert p.end_anchor == "dawn"


def test_drought_profile_horizon_defaults_to_18():
    p = config.DroughtProfile(target_offset=0, trigger_margin=0, runtime_scale=1.0)
    assert p.rain_skip_horizon_hours == 18


def test_zones_carry_static_refill_depth(example_config_path):
    """Every zone parses a usable static depth.

    Deliberately no exact millimetres: depths are seeded per-install (from
    Rachio's computed depthOfWater) and legitimately vary between installs
    and even between zones on the same install. What must hold is that the
    field parses per zone, stays positive, and has not collapsed to the 0.0
    default.
    """
    cfg = config.load_config(example_config_path)
    depths = {k: z.refill_depth_mm for k, z in cfg.zones.items()}

    assert len(depths) == 3
    for key, depth in depths.items():
        assert depth > 0, f"{key} fell back to the 0.0 default"
    assert len(set(depths.values())) > 1, "depths look broadcast, not per-zone"


def test_zone_refill_depth_defaults_to_zero():
    z = config.ZoneConfig(
        key="z", rachio_switch="s", dominant_sensor="d", state_sensor="st",
        quality_sensors=("q1", "q2", "q3"), target_range="moist",
        geography="front", adjacency=(),
    )
    assert z.refill_depth_mm == 0.0


def test_zone_spray_flag_parses(example_config_path):
    cfg = config.load_config(example_config_path)
    # zone_c has spray: true, so it loads as True
    assert cfg.zones["zone_c"].spray is True
    # zone_a does not have spray set, so it defaults to False
    assert cfg.zones["zone_a"].spray is False


def test_spray_switches_lists_flagged_zone_switches():
    z_spray = config.ZoneConfig(key="lw", rachio_switch="switch.lw",
        dominant_sensor="s", state_sensor="s", quality_sensors=(),
        target_range="moist", geography="back", adjacency=(), spray=True)
    z_plain = config.ZoneConfig(key="fy", rachio_switch="switch.fy",
        dominant_sensor="s", state_sensor="s", quality_sensors=(),
        target_range="moist", geography="front", adjacency=())
    cfg = config.Config(bands={}, tunables=config.Tunables(),
        zones={"lw": z_spray, "fy": z_plain}, drought_profiles={})
    assert config.spray_switches(cfg) == ["switch.lw"]


def test_bindings_defaults_when_section_absent():
    b = config.parse_bindings({})
    assert b.notify_service == "notify.PLACEHOLDER"
    assert b.rachio_api_key_secret == "rachio_api_key"
    assert b.weather.rain_last_hour == "sensor.tempest_rain_last_hour"
    assert b.sun.dawn == "sensor.sun_next_dawn"


def test_bindings_override_is_shallow_merged():
    raw = {"homeassistant": {"notify_service": "notify.me",
                             "weather": {"humidity": "sensor.my_rh"}}}
    b = config.parse_bindings(raw)
    assert b.notify_service == "notify.me"
    assert b.weather.humidity == "sensor.my_rh"
    # unspecified weather keys keep their default
    assert b.weather.temperature == "sensor.tempest_sensor_temperature"


def test_bindings_rain_today_default_and_override():
    assert config.parse_bindings({}).weather.rain_today == "sensor.tempest_precipitation_today"
    raw = {"homeassistant": {"weather": {"rain_today": "sensor.my_daily_rain"}}}
    b = config.parse_bindings(raw)
    assert b.weather.rain_today == "sensor.my_daily_rain"
    assert b.weather.rain_last_hour == "sensor.tempest_rain_last_hour"  # sibling default kept


def test_rain_confounder_mm_default():
    assert config.parse_config(_raw()).tunables.rain_confounder_mm == 0.5


def test_bindings_default_device_name_is_placeholder():
    assert config.HABindings().rachio_device_name == "PLACEHOLDER"


def test_bindings_default_run_active_boolean():
    assert config.HABindings().run_active_boolean == "input_boolean.irrigation_run_active"


def test_parse_bindings_reads_device_and_marker():
    raw = {"homeassistant": {
        "rachio_device_name": "Main House",
        "run_active_boolean": "input_boolean.custom_marker",
    }}
    b = config.parse_bindings(raw)
    assert b.rachio_device_name == "Main House"
    assert b.run_active_boolean == "input_boolean.custom_marker"


def test_tunables_pause_collapse_defaults():
    t = config.Tunables()
    assert t.use_pause_collapse is True
    assert t.max_pause_minutes == 60
    assert t.max_pauses_per_schedule == 0


def test_drought_profile_end_offset_defaults_to_none():
    p = config.DroughtProfile(target_offset=0, trigger_margin=0, runtime_scale=1.0)
    assert p.end_offset_minutes is None


def test_resolved_end_offset_uses_global_when_unset():
    p = config.DroughtProfile(target_offset=0, trigger_margin=0, runtime_scale=1.0)
    assert config.resolved_end_offset(p, 1) == 1


def test_resolved_end_offset_uses_profile_when_set_positive():
    p = config.DroughtProfile(target_offset=0, trigger_margin=0, runtime_scale=1.0,
                              end_offset_minutes=15)
    assert config.resolved_end_offset(p, 1) == 15


def test_resolved_end_offset_uses_profile_when_negative():
    p = config.DroughtProfile(target_offset=0, trigger_margin=0, runtime_scale=1.0,
                              end_offset_minutes=-15)
    assert config.resolved_end_offset(p, 1) == -15


def test_resolved_end_offset_zero_overrides_global():
    # 0 means "at the anchor" and must win over a nonzero global — not fall back.
    p = config.DroughtProfile(target_offset=0, trigger_margin=0, runtime_scale=1.0,
                              end_offset_minutes=0)
    assert config.resolved_end_offset(p, 5) == 0


def test_parse_config_reads_per_level_end_offset(example_config_path):
    raw = yaml.safe_load(open(example_config_path, encoding="utf-8"))
    raw["drought_profiles"]["Level 0 - Normal"]["end_offset_minutes"] = -15
    cfg = config.parse_config(raw)
    assert cfg.drought_profiles["Level 0 - Normal"].end_offset_minutes == -15


def test_max_schedule_retries_defaults_to_two():
    from irrigation_lib.config import Tunables
    assert Tunables().max_schedule_retries == 2


def test_max_schedule_retries_is_overridable():
    from irrigation_lib.config import Tunables
    assert Tunables(max_schedule_retries=0).max_schedule_retries == 0


def test_settle_max_wait_hours_default():
    from irrigation_lib.config import Tunables
    assert Tunables().settle_max_wait_hours == 12.0


def _raw():
    return {
        "bands": {"moist": {"low": 67, "high": 76}},
        "tunables": {"field_capacity_pct": 90.0},
        "drought_profiles": {
            "L0": {"target_offset": 0, "trigger_margin": 0, "runtime_scale": 1.0}
        },
        "zones": {
            "zone_a": {
                "rachio_switch": "switch.a", "dominant_sensor": "sensor.a_dom",
                "state_sensor": "sensor.a_state",
                "quality_sensors": ["sensor.a_q1", "sensor.a_q2", "sensor.a_q3"],
                "target_range": "moist", "geography": "front", "adjacency": [],
                "runtime_minutes": 20.0, "refill_target_pct": 85.0,
                "refill_span_pts": 12.0,
            },
            "zone_b": {  # omits the new zone keys -> defaults
                "rachio_switch": "switch.b", "dominant_sensor": "sensor.b_dom",
                "state_sensor": "sensor.b_state",
                "quality_sensors": ["sensor.b_q1", "sensor.b_q2", "sensor.b_q3"],
                "target_range": "moist", "geography": "back", "adjacency": [],
                "runtime_minutes": 20.0,
            },
        },
    }


def test_field_capacity_pct_parsed():
    c = config.parse_config(_raw())
    assert c.tunables.field_capacity_pct == 90.0


def test_field_capacity_pct_defaults_to_87():
    raw = _raw()
    raw["tunables"] = {}
    c = config.parse_config(raw)
    assert c.tunables.field_capacity_pct == 87.0


def test_zone_refill_target_and_span_parsed_with_defaults():
    c = config.parse_config(_raw())
    assert c.zones["zone_a"].refill_target_pct == 85.0
    assert c.zones["zone_a"].refill_span_pts == 12.0
    assert c.zones["zone_b"].refill_target_pct is None
    assert c.zones["zone_b"].refill_span_pts == 0.0


def test_self_calibration_tunables_defaults():
    c = config.parse_config(_raw())
    t = c.tunables
    assert t.self_calibration_enabled is False  # Beta: Active Watering Calibration, opt-in
    assert t.probe_fraction == pytest.approx(1 / 3)
    assert t.probe_shrink == 1.5
    assert t.probe_floor_minutes == 10.0
    assert t.convergence_tolerance == 0.10
    assert t.span_max == 60.0


def test_recalibrate_after_exclusion_hours_default():
    assert config.parse_config(_raw()).tunables.recalibrate_after_exclusion_hours == 48.0


def test_zone_exclude_boolean_default_and_override():
    raw = _raw()
    raw["zones"]["zone_a"]["exclude_boolean"] = "input_boolean.irrigation_exclude_zone_a"
    c = config.parse_config(raw)
    assert c.zones["zone_a"].exclude_boolean == "input_boolean.irrigation_exclude_zone_a"
    assert c.zones["zone_b"].exclude_boolean == ""  # default when key omitted
