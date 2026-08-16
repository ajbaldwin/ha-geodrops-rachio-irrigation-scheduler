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
- Times each run to finish before sunrise, keeping leaf-wetness time short to
  limit fungal/disease risk on turf.
- Runs zones in blocks (cycle + soak), rather than one long soak, for better
  infiltration.
- Aborts a run in progress if the Tempest weather station's rain gauge
  detects real rain starting mid-run.

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

## Install

1. Copy `irrigation/` into `<config>/pyscript/apps/irrigation/`.
2. Copy `irrigation_lib/` into `<config>/pyscript/modules/irrigation_lib/`.
3. Include `examples/ha-package.yaml` as a Home Assistant package (add it
   under `homeassistant: packages:`, or use the Helpers UI to create the
   equivalent entities by hand). This creates the drought-level select,
   standby/dew-formed booleans, the stop button, and the derived
   forecast/observed-overnight sensors the app reads.
4. Copy `examples/config.example.yaml` to
   `<config>/pyscript/apps/irrigation/config.yaml` and edit it:
   - The `homeassistant:` section — point every entity/service binding at
     your own Home Assistant entities.
   - The `zones:` section — one entry per Rachio zone you want the
     scheduler to manage, with your own zone IDs and sensor bindings.
5. Add your Rachio API key to `secrets.yaml`, under the key name given by
   `rachio_api_key_secret` in `config.yaml` (defaults to `rachio_api_key`).
6. Call the `pyscript.reload` service (or restart Home Assistant) to load
   the app.

## Advanced: renaming the stop button or the schedule times

Almost everything the app touches is read from `config.yaml` at runtime and
can be renamed freely. Two things can't: the `input_button.irrigation_stop`
entity id, and the nightly (23:00) / calibration (06:00) cron schedules.
pyscript binds `@state_trigger(...)` and `@time_trigger("cron(...)")`
decorators at **module load time**, before `config.yaml` is even read, so
they can't be driven by config. If you want to rename the stop button or
change the run times, edit the relevant decorator line directly in
`irrigation/__init__.py` — it's clearly commented — rather than trying to do
it through configuration.

## Disclaimer

This is a hobby project, provided with no warranty of any kind. It controls
physical sprinkler valves on a schedule it decides for itself. Review the
config, watch it run a few times, and use it at your own risk — you are
responsible for your own sprinklers, your own water bill, and your own lawn.
