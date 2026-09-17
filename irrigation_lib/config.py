"""Config model and loader for the irrigation scheduler. Pure Python."""
from __future__ import annotations

from dataclasses import dataclass

import yaml

BAND_ORDER = ["dry", "dry_plus", "moist", "moist_plus", "wet", "wet_plus"]

STATE_RANK = {
    "Dry": 0, "Dry+": 1, "Moist": 2, "Moist+": 3, "Wet": 4, "Wet+": 5,
}


def band_rank(name: str) -> int:
    return BAND_ORDER.index(name)


def state_rank(text: str) -> int:
    if text is None:
        return -1
    return STATE_RANK.get(text.strip(), -1)


@dataclass(frozen=True)
class Band:
    low: float
    high: float


@dataclass(frozen=True)
class Tunables:
    cycle_minutes: int = 12
    soak_minutes: int = 20
    end_offset_minutes: int = 5
    window_base_cap_hours: float = 6.0
    window_disease_cap_hours: float = 3.0
    warm_temp_f: float = 68.0
    # Retuned for OVERNIGHT MEANS (the forecast window), not the 23:00 snapshot:
    # overnight is systematically calmer and more humid, so the original 85/5
    # would fire nearly every night and carry no information.
    humid_rh_pct: float = 90.0
    stagnant_wind_mph: float = 2.0
    # Rain-gauge accumulation threshold. Doubles as the spray-zone
    # corroboration: while a spray-flagged zone runs, precip_type "rain" is
    # believed only if the gauge clears this (or it is hail), because the Tempest
    # misreads that zone's overspray as "rain" while the gauge stays at 0.
    rain_last_hour_mm: float = 0.2
    # Plain rain must persist this long (the automation's 2m30s `for:`) before
    # aborting a running block.
    rain_sustain_seconds: int = 150
    # Forecast rain skip. The amount threshold is a FRACTION of the zone refill
    # depth rather than a fixed depth, so it means something and self-adjusts.
    rain_skip_probability_pct: float = 70.0
    rain_skip_refill_fraction: float = 0.5
    # Notification-collapse: submit the whole night as ONE Rachio schedule and
    # fill idle soak gaps with device pauses (one schedule-start notification).
    use_pause_collapse: bool = True
    max_pause_minutes: int = 60  # HA rachio.pause_watering ceiling; chain beyond
    max_pauses_per_schedule: int = 0  # 0 = unbounded (one schedule); >0 = fallback
    # Collapsed-schedule drop recovery: when a mid-run never-started follows
    # delivered water, Rachio dropped the schedule (a ~60-min cumulative-pause
    # cap, observed 2026-08-27). Re-issue a fresh schedule for the remaining
    # water up to this many times per run. 0 disables recovery (abort as before).
    max_schedule_retries: int = 2
    # Default refill target on the `dominant` scale when a zone sets none. Wet-band
    # low: full but short of the waterlogged Wet+ range. Overridable per zone.
    field_capacity_pct: float = 87.0
    # Active Watering Calibration (BETA): each zone learns its own dosing span
    # from real soil response instead of the Rachio-derived span. Defaults OFF —
    # opt in per yard once you've watched a few nights of probing behavior.
    self_calibration_enabled: bool = False
    # Fraction of the zone's full-refill runtime for the FIRST probe (~1/3 lands a
    # measurable rise in the linear region on night one, clear of the no_rise floor).
    probe_fraction: float = 1.0 / 3.0
    # Minimum duration per probe cycle, in minutes.
    probe_floor_minutes: float = 10.0
    # Growth rate per calibration cycle: multiplier for next probe size after a
    # sub-measurable rise.
    probe_growth: float = 1.5
    # Shrink divisor after a saturated probe: symmetric to probe_growth so an
    # over-large probe self-corrects downward instead of stalling.
    probe_shrink: float = 1.5
    # Minimum points rise to be measurable in soil moisture signal.
    measurable_rise_pts: float = 3.0
    # Hours to allow soil settling before evaluating response to watering.
    settle_hours: float = 4.0
    # How long past measure_at (run_end + settle_hours) the settle poll will keep
    # waiting for a genuine post-settle sensor report before giving up on an
    # observation. On timeout the obs is DROPPED (inconclusive) — never rejected —
    # so a missed GeoDrops check-in can't corrupt the model. Sized to clear the
    # observed worst-case ~6h missed check-in plus settle.
    settle_max_wait_hours: float = 12.0
    # EWMA smoothing factor for calibration convergence.
    calibration_ewma_alpha: float = 0.3
    # Tolerance threshold for convergence detection.
    convergence_tolerance: float = 0.10
    # Number of converged samples to declare tuning complete.
    convergence_samples: int = 3
    # Dominant level below which a zone may be probed.
    probe_headroom_ceiling: float = 85.0
    # Upper threshold to reject saturation-state reads.
    saturation_reject: float = 95.0
    # Minimum span (range of refill dosing) in points.
    span_min: float = 1.0
    # Maximum span (range of refill dosing) in points.
    span_max: float = 60.0
    # Daily rain accumulation (mm) over the settle window above which a
    # calibration observation is rejected as rain-confounded.
    rain_confounder_mm: float = 0.5
    # A zone excluded (see ZoneConfig.exclude_boolean) for at least this many
    # hours is reset to recalibrating on return (soil likely changed, e.g.
    # overseed); a shorter/accidental exclusion keeps its learned calibration.
    recalibrate_after_exclusion_hours: float = 48.0


# Where a run's watering window ENDS, per drought profile. Dawn is the earlier
# of the two (civil dawn precedes sunrise by roughly half an hour), so it yields
# the shorter, more conservative window and is the safe default.
END_ANCHOR_SENSORS = {
    "dawn": "sensor.sun_next_dawn",
    "sunrise": "sensor.sun_next_rising",
}


def end_anchor_sensor(name) -> str:
    """HA sensor giving the end anchor for `name` ("dawn" | "sunrise").

    Anything unrecognised, empty, or None falls back to dawn: an unknown anchor
    must shrink the window, never silently extend watering past sunrise.
    """
    return END_ANCHOR_SENSORS.get(name, END_ANCHOR_SENSORS["dawn"])


def resolved_end_offset(profile, global_offset: int) -> int:
    """The end offset in effect for a profile: its own if set, else the global.

    Tests `is not None`, not truthiness, so a per-level 0 ("finish exactly at the
    anchor") overrides a nonzero global instead of falling back to it.
    """
    if profile.end_offset_minutes is not None:
        return profile.end_offset_minutes
    return global_offset


@dataclass(frozen=True)
class DroughtProfile:
    target_offset: int
    trigger_margin: float
    runtime_scale: float
    # Which sun event the window ends before (see END_ANCHOR_SENSORS). Levels
    # 0-2 finish at sunrise, so watering ends just as drying begins; Level 3
    # finishes at dawn, before any sun, to minimise evaporative loss when water
    # is scarce. Defaulted to dawn so existing constructions stay valid.
    end_anchor: str = "dawn"
    # How long the grass may go dry is exactly what the drought level encodes,
    # so the rain-skip horizon is per profile. Defaulted so existing direct
    # constructions stay valid.
    rain_skip_horizon_hours: int = 18
    # Per-level override of Tunables.end_offset_minutes. None = inherit the global.
    # POSITIVE = minutes BEFORE end_anchor, NEGATIVE = after (end = anchor - offset).
    end_offset_minutes: int | None = None


@dataclass(frozen=True)
class ZoneConfig:
    key: str
    rachio_switch: str
    dominant_sensor: str
    state_sensor: str
    quality_sensors: tuple
    target_range: str
    geography: str
    adjacency: tuple
    # full-refill runtime (Rachio-computed, seeded static); defaults keep direct
    # ZoneConfig construction in tests simple — load_config always supplies it.
    runtime_minutes: float = 0.0
    rachio_zone_id: str = ""  # Rachio zone UUID, for a future live-runtime refresh
    # Full-refill depth in mm (Rachio `depthOfWater`), seeded statically here and
    # preferred from the live pull when available — mirrors runtime_minutes.
    refill_depth_mm: float = 0.0
    # Deficit-proportional dosing. refill_target_pct: the `dominant` value to
    # refill TO; None inherits Tunables.field_capacity_pct. refill_span_pts: the
    # `dominant` points a full refill buys; 0.0 = derive from Rachio (live) else
    # full-refill fallback. Same "0/None = unset" idiom as runtime_minutes.
    refill_target_pct: float | None = None
    refill_span_pts: float = 0.0
    # True for zones whose spray the Tempest can misread as rain (the rain gate
    # corroborates such a zone with the gauge, not RH). Defaulted so existing
    # constructions and configs without the key stay valid.
    spray: bool = False
    # Entity id of a boolean that, when "on", EXCLUDES this zone from the nightly
    # plan AND calibration probing (e.g. an overseeded zone watered separately).
    # Empty = never excluded. Missing/unavailable entity fails safe to included.
    exclude_boolean: str = ""


@dataclass(frozen=True)
class WeatherEntities:
    temperature: str = "sensor.tempest_sensor_temperature"
    humidity: str = "sensor.tempest_sensor_humidity"
    wind: str = "sensor.tempest_sensor_wind_speed_average"
    rain_last_hour: str = "sensor.tempest_rain_last_hour"
    precip_type: str = "sensor.tempest_sensor_precipitation_type"
    # Daily rain accumulation (resets at midnight). Read at the mid-morning
    # settle pass to detect rain over the overnight run->settle window (a real
    # windowed signal, unlike rain_last_hour), for calibration confounder
    # rejection. Override for a non-Tempest gauge.
    rain_today: str = "sensor.tempest_precipitation_today"


@dataclass(frozen=True)
class SunAnchors:
    dawn: str = "sensor.sun_next_dawn"
    sunrise: str = "sensor.sun_next_rising"


@dataclass(frozen=True)
class DerivedSensors:
    forecast_overnight: dict = None
    observed_overnight: dict = None
    precipitation_chance_prefix: str = "sensor.precipitation_chance_"
    precipitation_amount_prefix: str = "sensor.precipitation_amount_"


@dataclass(frozen=True)
class HABindings:
    notify_service: str = "notify.PLACEHOLDER"
    calendar_entity: str = "calendar.irrigation"
    rachio_api_key_secret: str = "rachio_api_key"
    drought_level_select: str = "input_select.irrigation_drought_level"
    standby_boolean: str = "input_boolean.irrigation_standby"
    standby_switch: str = "switch.sprinkler_standby"
    dew_formed_boolean: str = "input_boolean.dew_formed"
    rachio_device_name: str = "PLACEHOLDER"
    run_active_boolean: str = "input_boolean.irrigation_run_active"
    weather: WeatherEntities = WeatherEntities()
    sun: SunAnchors = SunAnchors()
    derived: DerivedSensors = DerivedSensors()


@dataclass(frozen=True)
class Config:
    bands: dict
    tunables: Tunables
    zones: dict
    drought_profiles: dict
    bindings: HABindings = HABindings()


_FORECAST_DEFAULT = {"temp": "sensor.forecast_overnight_temp",
                     "humidity": "sensor.forecast_overnight_humidity",
                     "wind": "sensor.forecast_overnight_wind"}
_OBSERVED_DEFAULT = {"temp": "sensor.observed_overnight_temp",
                     "humidity": "sensor.observed_overnight_humidity",
                     "wind": "sensor.observed_overnight_wind"}


def _merge(defaults: dict, override) -> dict:
    out = dict(defaults)
    if override:
        for k, v in override.items():
            out[k] = v
    return out


def parse_bindings(raw: dict) -> HABindings:
    ha = (raw or {}).get("homeassistant", {}) or {}
    w = ha.get("weather", {}) or {}
    s = ha.get("sun", {}) or {}
    d = ha.get("derived", {}) or {}
    weather = WeatherEntities(**_merge({
        "temperature": WeatherEntities().temperature,
        "humidity": WeatherEntities().humidity,
        "wind": WeatherEntities().wind,
        "rain_last_hour": WeatherEntities().rain_last_hour,
        "precip_type": WeatherEntities().precip_type,
        "rain_today": WeatherEntities().rain_today,
    }, w))
    sun = SunAnchors(**_merge({"dawn": SunAnchors().dawn,
                               "sunrise": SunAnchors().sunrise}, s))
    derived = DerivedSensors(
        forecast_overnight=_merge(_FORECAST_DEFAULT, d.get("forecast_overnight")),
        observed_overnight=_merge(_OBSERVED_DEFAULT, d.get("observed_overnight")),
        precipitation_chance_prefix=d.get("precipitation_chance_prefix",
            DerivedSensors().precipitation_chance_prefix),
        precipitation_amount_prefix=d.get("precipitation_amount_prefix",
            DerivedSensors().precipitation_amount_prefix),
    )
    return HABindings(
        notify_service=ha.get("notify_service", HABindings().notify_service),
        calendar_entity=ha.get("calendar_entity", HABindings().calendar_entity),
        rachio_api_key_secret=ha.get("rachio_api_key_secret", HABindings().rachio_api_key_secret),
        drought_level_select=ha.get("drought_level_select", HABindings().drought_level_select),
        standby_boolean=ha.get("standby_boolean", HABindings().standby_boolean),
        standby_switch=ha.get("standby_switch", HABindings().standby_switch),
        dew_formed_boolean=ha.get("dew_formed_boolean", HABindings().dew_formed_boolean),
        rachio_device_name=ha.get("rachio_device_name", HABindings().rachio_device_name),
        run_active_boolean=ha.get("run_active_boolean", HABindings().run_active_boolean),
        weather=weather, sun=sun, derived=derived,
    )


def parse_config(raw: dict) -> Config:
    # NOTE (pyscript): explicit loops only — no `with`, no comprehensions that
    # reference enclosing-function locals. pyscript scopes those differently from
    # CPython and raises NameError. This function runs in pyscript's interpreter,
    # so it must not touch `open`/file I/O either — the caller passes a parsed
    # dict (the app reads the file via a @pyscript_compile helper).
    bands = {}
    for name, vals in raw["bands"].items():
        bands[name] = Band(**vals)

    tunables = Tunables(**raw.get("tunables", {}))

    profiles = {}
    for name, vals in raw["drought_profiles"].items():
        profiles[name] = DroughtProfile(**vals)

    zones = {}
    for key, z in raw["zones"].items():
        zones[key] = ZoneConfig(
            key=key,
            rachio_switch=z["rachio_switch"],
            dominant_sensor=z["dominant_sensor"],
            state_sensor=z["state_sensor"],
            quality_sensors=tuple(z["quality_sensors"]),
            target_range=z["target_range"],
            geography=z["geography"],
            adjacency=tuple(z.get("adjacency", [])),
            runtime_minutes=float(z["runtime_minutes"]),
            rachio_zone_id=z.get("rachio_zone_id", ""),
            refill_depth_mm=float(z.get("refill_depth_mm", 0.0)),
            refill_target_pct=(
                None if z.get("refill_target_pct") is None
                else float(z["refill_target_pct"])
            ),
            refill_span_pts=float(z.get("refill_span_pts", 0.0)),
            exclude_boolean=z.get("exclude_boolean", ""),
            spray=bool(z.get("spray", False)),
        )
    return Config(bands=bands, tunables=tunables, zones=zones, drought_profiles=profiles, bindings=parse_bindings(raw))


def load_config(path: str) -> Config:
    # CPython-only convenience (used by the test suite). pyscript's interpreter
    # does not expose `open`, so the app reads + yaml-parses the file in a
    # @pyscript_compile helper and calls parse_config() directly instead.
    handle = open(path, "r", encoding="utf-8")
    try:
        raw = yaml.safe_load(handle)
    finally:
        handle.close()
    return parse_config(raw)


def spray_switches(cfg) -> list:
    """rachio_switch of every zone flagged spray=True."""
    out = []
    for zone in cfg.zones.values():
        if zone.spray:
            out.append(zone.rachio_switch)
    return out
