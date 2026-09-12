# GeoDrops + Rachio Irrigation Scheduler

A [pyscript](https://github.com/custom-components/pyscript) app for Home
Assistant that runs your Rachio sprinkler zones based on real soil-moisture
readings instead of a fixed clock.

## What it does

- Runs a nightly watering pass driven by soil-moisture data, not a calendar.
- Skips any zone that's already sufficiently moist — no wasted water on zones
  that don't need it.
- Scales how much (and how often) it waters against a configurable
  drought-level setting, from normal conditions up to emergency restrictions.
- Skips watering entirely when the forecast calls for enough rain to do the
  job for you.
- Finishes each run near dawn or sunrise — the anchor and an offset are set
  per drought level — keeping leaf-wetness time short to limit fungal/disease
  risk on turf.
- Runs zones in blocks (cycle + soak), rather than one long soak, for better
  infiltration.
- Aborts a run in progress if the Tempest weather station's rain gauge
  detects real rain starting mid-run.
- Lets you exclude any zone from both the nightly plan and calibration
  probing with a toggle helper — e.g. an overseeded zone you're watering
  separately with a lighter, more frequent schedule.
- **Active Watering Calibration (Beta, off by default):** each zone can
  learn its own dosing span from its actual soil-moisture response,
  replacing the Rachio-derived estimate. See [Enabling Active Watering
  Calibration](#enabling-active-watering-calibration-beta) before turning
  it on.

## Prerequisites

- A working Home Assistant instance.
- [pyscript](https://github.com/custom-components/pyscript) installed via
  HACS.
- Soil-moisture sensors (e.g. GeoDrops) already flowing into Home Assistant
  as sensor entities. This project does not create that data — it assumes
  the moisture sensors already exist and are updating.
- The [Rachio](https://www.home-assistant.io/integrations/rachio/) Home
  Assistant integration, configured against your controller.
- A [Tempest](https://www.home-assistant.io/integrations/weatherflow/)
  weather station (or another station exposing equivalent entities) for
  live rain/wind/temperature/humidity readings.
- A forecast `weather.*` entity (e.g. the built-in `weather.home` from
  whatever weather integration you use) for the forecast-based rain skip.
- Your Rachio controller's device name (Rachio app → Settings → Devices).
  `tunables.use_pause_collapse` defaults to **on**, which submits the whole
  night as one Rachio schedule and uses device-level pause/resume/stop —
  those calls target the controller by name, so `rachio_device_name` in
  `config.yaml` must match it. Set `use_pause_collapse: false` to keep the
  previous multi-block scheduling behavior instead, in which case this
  device name isn't used.

## Install

1. Copy `irrigation/` into `<config>/pyscript/apps/irrigation/`.
2. Copy `irrigation_lib/` into `<config>/pyscript/modules/irrigation_lib/`.
3. Install `examples/ha-package.yaml` as a Home Assistant package: enable
   packages in `configuration.yaml` if you haven't already
   (`homeassistant:` → `packages: !include_dir_named packages`), then copy
   the file into your `packages/` directory. (Or create the equivalent
   entities by hand via the Helpers UI.) This creates the drought-level
   select, standby/dew-formed booleans, the stop button, and the
   `statistics`/`template` sensors for observed- and forecast-overnight data
   that the app reads. It does **not** create
   `input_boolean.irrigation_run_active` — create that helper yourself
   (Settings → Devices & Services → Helpers → Toggle). It's the marker the
   app sets while a collapsed run is in flight so startup recovery can detect
   and self-heal an interrupted (even paused) run; required whenever
   `tunables.use_pause_collapse` is on (the default).
4. Copy `examples/config.example.yaml` to
   `<config>/pyscript/apps/irrigation/config.yaml` and edit it:
   - The `homeassistant:` section — point every entity/service binding at
     your own Home Assistant entities, including `rachio_device_name` (your
     controller's device name) and `run_active_boolean` (the helper created
     in step 3, if you kept the default entity id).
   - The `zones:` section — one entry per Rachio zone you want the
     scheduler to manage, with your own zone IDs and sensor bindings. Each
     zone may optionally set `exclude_boolean` to an `input_boolean` entity
     id — turning that helper on pulls the zone out of both the nightly
     plan and calibration probing (e.g. for an overseeded zone you're
     watering separately). Omit it and the zone can never be excluded this
     way. Create the helper yourself (Helpers UI) for any zone you want
     this on.
   - The `drought_profiles:` section (optional to tune) — each level sets when
     its watering window ends via `end_anchor` (`dawn` or `sunrise`) and an
     optional `end_offset_minutes` (positive = minutes before the anchor,
     negative = after); omit the key to inherit the global
     `tunables.end_offset_minutes`. See the comments in `config.example.yaml`.
5. Add your Rachio API key to `secrets.yaml`, under the key name given by
   `rachio_api_key_secret` in `config.yaml` (defaults to `rachio_api_key`).
6. **Restart Home Assistant.** A full restart is required the first time:
   the package's new helper entities and `statistics`/`template` sensors
   (step 3) only register on a restart, not a reload. The same restart loads
   the pyscript app. (Afterwards, for changes to the app code alone,
   `pyscript.reload` is enough — but any change to the package entities still
   needs a restart.)

## Enabling Active Watering Calibration (Beta)

**Off by default.** This feature is new, still being validated against real
yards, and changes how much water your zones actually receive — read this
before turning it on.

What it does: instead of trusting the Rachio-derived dosing span, each zone
runs small "probe" waterings, measures the resulting soil-moisture rise, and
learns its own points-per-minute efficacy from that. Over a few nights it
converges on a dosing span tuned to your actual soil, sensor placement, and
sprinkler output — which can differ substantially from Rachio's estimate.

Why it's Beta: the probe-sizing logic is new and, while unit-tested, hasn't
been observed across enough different soil types and zone geometries to
trust as a default. A zone that never registers a measurable rise, or one
whose sensor is noisy, can take longer to converge than expected while it
runs smaller-than-normal watering doses in the meantime.

To enable it:

1. Set `tunables.self_calibration_enabled: true` in `config.yaml`.
2. Watch the calibrating zone(s) for several nights before trusting the
   result. The state of each zone's calibration is queryable from the
   scheduler's status attributes (`state`, `n_obs`, `efficacy`, and
   `last_reject_reason` if a probe was rejected — e.g. `no_rise`,
   `saturated`, `rain`).
3. If you'd rather exclude a specific zone from calibration entirely while
   still probing others, give that zone `refill_span_pts` a nonzero value
   in its `zones:` entry (pins it to a fixed dosing span) or set its
   `exclude_boolean` helper on.
4. If it isn't behaving as expected, set `self_calibration_enabled: false`
   to fall back to the original Rachio-derived dosing at any time — nothing
   else about the schedule changes.

## Advanced: renaming the stop button or the schedule times

Almost everything the app touches is read from `config.yaml` at runtime and
can be renamed freely. Two things can't: the `input_button.irrigation_stop`
entity id, and the nightly (23:00) / calibration (06:00) cron schedules.
pyscript binds `@state_trigger(...)` and `@time_trigger("cron(...)")`
decorators at **module load time**, before `config.yaml` is even read, so
they can't be driven by config. If you want to rename the stop button or
change the run times, edit the relevant decorator line directly in
`irrigation/__init__.py` rather than trying to do it through configuration:
the stop button is the `@state_trigger("input_button.irrigation_stop")` line
on `_on_stop_button`, and the schedules are the `@time_trigger("cron(...)")`
lines on `irrigation_nightly` (23:00) and `irrigation_calibrate` (06:00).

## Disclaimer

This is a hobby project, provided with no warranty of any kind. It controls
physical sprinkler valves on a schedule it decides for itself. Review the
config, watch it run a few times, and use it at your own risk — you are
responsible for your own sprinklers, your own water bill, and your own lawn.
