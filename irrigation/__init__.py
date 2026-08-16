"""Nightly irrigation orchestrator — the single pyscript app file.

Everything that touches pyscript globals (state / service / task / log) lives in
THIS file, because pyscript only runs an app's ``__init__.py`` through the
interpreter that injects those globals. Sibling files in an app directory load
as plain Python (no globals) and are not autoloaded, so the Rachio I/O, the
Rachio runtime API, and the plan-execution engine are all inlined here rather
than split into modules.

Pure, unit-tested logic (no pyscript globals) stays in
``pyscript/modules/irrigation_lib/`` and is imported below. That package is
named ``irrigation_lib`` (not ``irrigation``) so its module name cannot collide
with this app's name (``irrigation``) — pyscript resolves a bare ``import
irrigation`` to the app, which would recurse forever.

Sections: Rachio zone I/O · Rachio runtime API · plan execution · orchestration
· services & triggers.
"""
import datetime as dt
import random
import time

# NOTE: import each submodule by its dotted path, NOT `from irrigation_lib import
# abort, ...`. pyscript does not auto-load a package's submodules for the
# `from package import submodule` form — it would look for an attribute on the
# (empty) irrigation_lib package and fail with AttributeError. `import
# irrigation_lib.X as X` loads the leaf module file directly.
import irrigation_lib.abort as abort
import irrigation_lib.blocks as blocks
import irrigation_lib.config as config
import irrigation_lib.drought as drought
import irrigation_lib.evaluate as evaluate
import irrigation_lib.plan as plan
import irrigation_lib.rachio_runtime as rachio_runtime
import irrigation_lib.report_format as report_format
import irrigation_lib.sensors as sensors
import irrigation_lib.weather as weather

CONFIG_PATH = "/config/pyscript/apps/irrigation/config.yaml"
# Diagnostic records survive a restart by being written here and re-published on
# startup. pyscript entities live only in the state machine, which HA does NOT
# restore, so a restart wipes them — and restarts are routine, since every YAML
# package change needs one. Twice now the record naming an abort existed, was
# correct, and was destroyed before it could be read. Gitignored: this is host
# state, and /config IS the repo root.
STATE_DIR = "/config/pyscript/apps/irrigation/state"
# Only records whose value is HISTORICAL. `irrigation_last_run` is "what just
# happened" and `irrigation_status` is live, both of which the next run or the
# startup handler re-establishes correctly.
PERSISTED = ("irrigation_last_nightly", "irrigation_calibration")
LOGBOOK_NAME = "Irrigation"
# Every Logbook entry is attached to an entity so the activity log can be
# FILTERED instead of scrolled. Run-level events land here; per-zone events land
# on the zone's own switch, putting a zone's watering history in its own
# more-info -> Logbook alongside its Rachio on/off events.
STATUS_ENTITY = "pyscript.irrigation_status"
CHECK_INTERVAL_S = 30
# Consecutive polls showing nothing running before a block is called externally
# stopped. Rachio steps between zones inside a block on its own, and HA's view of
# both switches lags Rachio's cloud across that hand-off, so for a poll or two
# nothing reads `on`.
#
# Six polls = three minutes. It was two, and that ended three consecutive nights:
# on 2026-08-10 one zone's block completed at 04:16:38, the watch rendered its
# verdict the same second, and stop_all cut off the next zone three seconds after
# Rachio had legitimately started it. A hand-off is a coin flip against a 60 s
# threshold.
#
# The trade is deeply asymmetric and the original comment here had the reasoning
# but not the conclusion: a LATE verdict is free, because pyscript is not
# advancing zones during a block — Rachio owns the queue and we are only
# watching. An EARLY verdict costs the whole night plus a stop that truncates a
# healthy zone. So this wants to sit far on the tolerant side; three minutes is
# still far inside the 6-hour stuck-zone backstop.
EXTERNAL_STOP_POLLS = 6
# How long to let a finished block drain before moving on. Rachio's clock starts
# when it receives the call, a beat after ours, so the last zone can still be
# closing when our sleep ends.
BLOCK_DRAIN_TIMEOUT_S = 120
# Stop testing for an external stop this close to a block's scheduled end. The
# two clocks are independent: if Rachio's runs even slightly fast, the block
# finishes normally a poll or two before our sleep does, and without this the
# watch would read its own block completing as an external stop and abort the
# rest of the night. A real stop inside the grace window costs at most this much
# over-credited water on the block's last zone — a far cheaper error.
BLOCK_END_GRACE_S = 90
# How long a block gets to actually start watering. Past this with nothing ever
# seen on, the block did not start — something stopped it before our first poll
# caught it, which is what the adverse-conditions automation did on 2026-07-28.
# Without a bound, "not yet on" and "never came on" are indistinguishable and
# the second is scored as a full success.
BLOCK_START_CONFIRM_S = 90
RACHIO_BASE = "https://api.rach.io/1/public/"
RUNTIME_CACHE_TTL_S = 6 * 3600

_manual_stop = False
api_calls = 0    # Rachio-affecting service calls (start/stop) — the real budget
state_polls = 0  # local HA state reads (free); tracked separately, see below
_runtime_cache = {"ts": 0.0, "runtimes": {}, "depths": {}}
_current_tun = None  # tunables of the in-flight run; read by _is_rain()
# Sibling globals to _current_tun, set at the same points (and at every other
# site that reads an entity/service outside an active run — preview,
# calibration, runtime refresh). Never read without first confirming the
# caller set them; pyscript's lambdas/nested defs cannot capture enclosing
# locals, so module-level predicates like _is_standby/_is_rain have no other
# way to reach the loaded config.
_current_cfg = None       # the loaded Config (spray_switches() etc.)
_current_bindings = None  # _current_cfg.bindings — entity/service ids
_run_in_progress = False  # True only while _plan_and_run() owns the run-
                           # scoped globals above; _preview/irrigation_
                           # calibrate/irrigation_refresh_runtimes check this
                           # before touching them, so they never clobber a
                           # run's bindings mid-watch.
_rain_since = None   # when the rain condition first went true (sustain clock)


def _activity(message, entity_id=None):
    """Record a normal-operation event in the HA Logbook (the activity log),
    NOT the system log. The system log (log.warning/log.error) is reserved for
    errors and failures. Use this for routine events: runs, previews, refreshes,
    manual stops.

    Every entry is attached to an entity (`entity_id`, an optional field of
    logbook.log), defaulting to STATUS_ENTITY. Unattached entries can only be
    found by scrolling the global Logbook; attached ones can be filtered to, and
    show up in that entity's own more-info dialog. Pass a zone switch to file an
    event under the zone it happened to.

    NOTE (pyscript): call logbook.log via the function-style service call, NOT
    service.call("logbook", "log", name=...). service.call's second positional
    arg is itself named `name` (the service name), so passing the logbook `name`
    data field as a kwarg raises "got multiple values for argument 'name'". The
    function-style form has no such collision.
    """
    target = entity_id if entity_id is not None else STATUS_ENTITY
    logbook.log(name=LOGBOOK_NAME, message=message, entity_id=target)


def _set_status(status, detail=None):
    """Publish what the scheduler is doing right now.

    HA logs a state CHANGE to the Logbook by itself, attached to the entity and
    carrying how long the previous state lasted — so this one call gives a
    filterable, duration-aware timeline of the night without a Logbook call per
    transition. It also answers a question nothing could before: mid-run, is the
    system waiting for the pre-dawn window or actually watering?

    States: idle / planning / waiting / watering / skipped / standby / aborted.
    A preview never touches it — a dry run changes nothing.

    NB pyscript re-creates its entities, so this reads `unknown` after a restart
    until the next run (or the startup handler) sets it.
    """
    attributes = {
        "friendly_name": "Irrigation Status",
        "updated": dt.datetime.now().isoformat(timespec="seconds"),
    }
    if detail is not None:
        attributes["detail"] = detail
    state.set(STATUS_ENTITY, value=status, new_attributes=attributes)


# ─── Rachio zone I/O (start/stop + poll-verify) ──────────────────────────────

def reset_counters():
    global api_calls, state_polls
    api_calls = 0
    state_polls = 0


def poll_zone_running(zone_switch):
    """Is this zone running? Reads the HA state machine — NOT the Rachio API.

    Counted separately from api_calls: HA's Rachio integration polls the cloud
    on its own schedule, so reading local state costs nothing against the Rachio
    budget. Keeping these out of api_calls matters now that the watch loop polls
    every CHECK_INTERVAL_S — otherwise the budget figure would balloon with
    calls that were never made.
    """
    global state_polls
    state_polls += 1
    return state.get(zone_switch) == "on"


def any_zone_running(zone_switches):
    # pyscript has no generator expressions; use a list comprehension.
    return any([poll_zone_running(zs) for zs in zone_switches])


def start_block(runs, zone_switches):
    """Hand one back-to-back block of zone runs to Rachio as a single schedule.

    NOT `switch.turn_on`. The Rachio integration does not treat a zone switch as
    a relay: turning one on starts that zone for the integration's OWN configured
    default (`DEFAULT_MANUAL_RUN_MINS`, 10 minutes) and stops it on its own timer,
    which is what made the first live run water 10 minutes instead of 32.

    `start_multiple_zone_schedule` carries explicit per-zone durations, so the
    plan's minutes are the ones that run. It takes the whole block at once —
    zones back to back, in the order given, the same zone appearing as many
    times as the cycle-and-soak plan revisits it (the entity list is NOT
    de-duplicated, confirmed on the controller). One call per block instead of
    one start and one stop per cycle also keeps Rachio's push notifications to a
    couple per block rather than a couple per cycle.
    """
    global api_calls
    api_calls += 1
    service.call(
        "rachio", "start_multiple_zone_schedule",
        entity_id=blocks.entity_ids(runs, zone_switches),
        duration=blocks.duration_csv(runs),
    )


def stop_zone(zone_switch):
    global api_calls
    api_calls += 1
    service.call("switch", "turn_off", entity_id=zone_switch)


def stop_all(zone_switches):
    for zs in zone_switches:
        try:
            stop_zone(zs)
        except Exception as err:
            log.warning(f"irrigation: stop_all failed for {zs}: {err}")


# ─── Rachio Public API: per-zone full-refill runtimes ────────────────────────
# Prereqs: rachio_api_key in /config/secrets.yaml and allow_all_imports: true.
# Blocking I/O runs via task.executor; results cached (runtime is config-stable).
#
# The two blocking helpers are @pyscript_compile: that compiles them to native
# CPython so task.executor accepts them. A plain pyscript function (whether in
# the app OR in a pyscript module — modules are interpreted too) is rejected with
# "pyscript functions can't be called from task.executor".

@pyscript_compile
def _read_api_key(key_name):
    import yaml

    with open("/config/secrets.yaml", encoding="utf-8") as handle:
        return (yaml.safe_load(handle) or {}).get(key_name)


@pyscript_compile
def _write_json_atomic(path, data):
    """Write JSON via a temp file + rename, so a crash mid-write cannot leave a
    truncated record that would then fail to load on the next startup."""
    import json
    import os

    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(tmp, path)


@pyscript_compile
def _read_json(path):
    """Parsed JSON, or None when the file is absent."""
    import json
    import os

    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


@pyscript_compile
def _read_config_yaml(path):
    import yaml

    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pyscript_compile
def _http_get_json(url, key):
    import json
    import urllib.request

    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def _fetch_zone_data():
    """(runtimes_minutes, refill_depths_mm) from one pass over the device payload.

    Both come from the same `device/{id}` response, so capturing depths costs no
    additional API calls.
    """
    # _current_bindings may be unset only if this is called before any config
    # load at all (should not happen — every entry point that can reach here
    # loads config first); fall back to the documented default rather than
    # crash a Rachio fetch over a missing global.
    key_name = (
        _current_bindings.rachio_api_key_secret if _current_bindings
        else "rachio_api_key"
    )
    key = task.executor(_read_api_key, key_name)
    if not key:
        log.warning(
            "irrigation: no rachio_api_key in secrets.yaml; using static values"
        )
        return {}, {}
    person = task.executor(_http_get_json, RACHIO_BASE + "person/info", key)
    devices = task.executor(
        _http_get_json, RACHIO_BASE + "person/" + person["id"], key
    )["devices"]
    runtimes = {}
    depths = {}
    for device in devices:
        payload = task.executor(
            _http_get_json, RACHIO_BASE + "device/" + device["id"], key
        )
        zones = payload.get("zones", [])
        runtimes.update(rachio_runtime.parse_runtimes(zones))
        depths.update(rachio_runtime.parse_refill_depths(zones))
    return runtimes, depths


def _refresh_zone_cache(force=False):
    """Refresh the cache when stale. True when usable live data is present."""
    now = time.time()
    if (
        not force
        and _runtime_cache["runtimes"]
        and now - _runtime_cache["ts"] < RUNTIME_CACHE_TTL_S
    ):
        return True
    try:
        runtimes, depths = _fetch_zone_data()
    except Exception as err:
        log.warning(
            f"irrigation: Rachio fetch failed ({err}); using static values"
        )
        return False
    if runtimes:
        _runtime_cache["ts"] = now
        _runtime_cache["runtimes"] = runtimes
        _runtime_cache["depths"] = depths
        return True
    return False


def get_runtimes(force=False):
    """{rachio_zone_id: runtime_minutes}; {} on failure (caller falls back)."""
    if _refresh_zone_cache(force):
        return _runtime_cache["runtimes"]
    return {}


def get_refill_depths(force=False):
    """{rachio_zone_id: refill_depth_mm}; {} on failure (caller falls back)."""
    if _refresh_zone_cache(force):
        return _runtime_cache["depths"]
    return {}


# ─── Plan execution (poll-verify + abort watching) ───────────────────────────

def _abort_now(is_standby, is_manual_stop, is_rain):
    return abort.abort_reason(is_standby(), is_manual_stop(), is_rain())


def run_plan(slots, zone_switches, is_standby, is_manual_stop, is_rain,
             is_rain_at_start):
    """Execute a plan block by block.

    A block is a maximal run of back-to-back watering slots; Rachio runs the
    whole block from one call while pyscript waits and watches. Idle soak slots
    are pyscript sleeping with nothing running. The happy path issues NO stop at
    all — the block simply ends when its last zone's minutes are up. Stops are
    reserved for aborts, which is both correct and the reason the operator gets
    a couple of Rachio notifications a night instead of dozens.
    """
    watered = []
    delivered = {}
    # What we actually asked Rachio for, block by block. Nothing else records
    # the durations that left this app, so without it the whole-minute
    # quantization is invisible after the fact.
    sent = []
    all_switches = list(zone_switches.values())
    try:
        for block in blocks.group_blocks(slots):
            reason = _abort_now(is_standby, is_manual_stop, is_rain)
            if reason:
                stop_all(all_switches)
                return {"watered": watered, "aborted_reason": reason,
                        "delivered_minutes": delivered, "blocks": sent}

            if block.kind == "idle":  # soak: nothing running
                aborted, _idle = _sleep_watching(
                    block.minutes * 60, is_standby, is_manual_stop, is_rain
                )
                if aborted:
                    stop_all(all_switches)
                    return {"watered": watered, "aborted_reason": aborted,
                            "delivered_minutes": delivered, "blocks": sent}
                continue

            runs = blocks.quantize(block.slots)
            if not runs:
                # Every slot rounded below a minute — nothing worth sending.
                continue

            # Do not start INTO precipitation. Unlike the in-block watch, this
            # gate applies no sustain: the adverse-conditions automation aborts
            # the instant watering begins, so a block started now is a block
            # stopped now.
            if is_rain_at_start():
                stop_all(all_switches)
                return {"watered": watered, "aborted_reason": "rain-at-start",
                        "delivered_minutes": delivered, "blocks": sent}

            # Poll-verify: nothing should be running before we hand over a
            # block. Unlike switch.turn_on, start_multiple_zone_schedule does
            # NOT stop current watering first, so an overlap would be ours to
            # cause. A stop here is an anomaly, not routine.
            for switch in zone_switches.values():
                if poll_zone_running(switch):
                    log.warning(
                        f"irrigation: {switch} was already running before a "
                        "block; stopping it first"
                    )
                    stop_zone(switch)

            start_block(runs, zone_switches)
            block_seconds = sum([r.minutes for r in runs]) * 60
            sent.append({
                "minutes": int(block_seconds / 60),
                "runs": [[r.zone_key, r.minutes] for r in runs],
            })
            # Watch that SOMETHING stays on rather than tracking which zone is
            # up: Rachio steps through the block itself, and everything that
            # stops us from outside — the adverse-conditions automation, the
            # stuck-zone automation, a manual stop in the app — goes through the
            # controller and stops all watering.
            aborted, elapsed = _sleep_watching(
                block_seconds, is_standby, is_manual_stop, is_rain,
                watch_switches=all_switches,
            )

            # Credit by measurement: the whole block when it ran to completion,
            # otherwise only the zones the elapsed time actually reached.
            gave = blocks.delivered(
                runs, block_seconds if aborted is None else elapsed
            )
            for zone_key, minutes in gave.items():
                delivered[zone_key] = delivered.get(zone_key, 0) + minutes
                if zone_key not in watered:
                    watered.append(zone_key)

            if aborted:
                if aborted == "external-stop":
                    log.warning(
                        f"irrigation: watering stopped externally after "
                        f"{int(elapsed)}s of a {int(block_seconds / 60)} min "
                        "block; ending the run rather than starting more zones"
                    )
                elif aborted == "never-started":
                    log.warning(
                        f"irrigation: no zone came on within "
                        f"{BLOCK_START_CONFIRM_S}s of handing Rachio a "
                        f"{int(block_seconds / 60)} min block; something "
                        "stopped it at the start. Crediting no water and "
                        "ending the run rather than starting more zones"
                    )
                stop_all(all_switches)
                return {"watered": watered, "aborted_reason": aborted,
                        "delivered_minutes": delivered, "blocks": sent}

            _await_block_end(all_switches)

        return {"watered": watered, "aborted_reason": None,
                "delivered_minutes": delivered, "blocks": sent}
    except Exception:
        # Safety net: never leave a valve open if something unexpected raised.
        stop_all(all_switches)
        raise


def _await_block_end(zone_switches):
    """Wait for a finished block to actually close before moving on.

    Rachio's clock starts when it receives the call, a beat after ours starts,
    so the block's last zone can still be closing when our sleep ends. Stepping
    straight into the next block would make the pre-block poll-verify stop a
    zone that was about to finish on its own — cutting its tail short AND
    sending the operator a "stopped manually" notification for a run that was
    seconds from done.
    """
    waited = 0
    while waited < BLOCK_DRAIN_TIMEOUT_S:
        if not any_zone_running(zone_switches):
            return
        task.sleep(CHECK_INTERVAL_S)
        waited += CHECK_INTERVAL_S
    log.warning(
        f"irrigation: a zone was still running {waited}s after its block "
        "should have ended; stopping it"
    )
    stop_all(zone_switches)


def _sleep_watching(seconds, is_standby, is_manual_stop, is_rain, watch_switches=None):
    """Sleep in CHECK_INTERVAL_S chunks while watching for aborts.

    Returns (reason, elapsed_seconds); reason is None when the full duration
    elapsed normally.

    When `watch_switches` is given, also verify that watering is still going.
    Plenty outside this scheduler can stop it: the HA "Detect Adverse Watering
    Conditions" automation, the stuck-zone automation, a manual stop in the
    Rachio app, or a controller hiccup. Ignoring that would credit water that
    was never delivered, so an external stop ends the run and reports the time
    actually watered.

    The verdict for each poll comes from `abort.watch_step`, which holds the
    three interacting guards (start confirmation, end grace, consecutive empty
    polls) as pure, unit-tested logic — they are where the subtle failures live.
    A block that never started reports zero elapsed: nothing was delivered, so
    nothing may be credited. An external stop reports the FIRST empty poll,
    where the water actually ended, not the poll that finally confirmed it.
    """
    elapsed = 0
    seen_on = False
    misses = 0
    stopped_at = 0
    while elapsed < seconds:
        chunk = min(CHECK_INTERVAL_S, seconds - elapsed)
        task.sleep(chunk)
        elapsed += chunk
        reason = _abort_now(is_standby, is_manual_stop, is_rain)
        if reason:
            return reason, elapsed
        if watch_switches:
            verdict, seen_on, new_misses = abort.watch_step(
                any_zone_running(watch_switches), seen_on, misses,
                elapsed, seconds,
                BLOCK_START_CONFIRM_S, BLOCK_END_GRACE_S, EXTERNAL_STOP_POLLS,
            )
            if misses == 0 and new_misses == 1:
                stopped_at = elapsed
            misses = new_misses
            if verdict == "never-started":
                return verdict, 0
            if verdict:
                return verdict, stopped_at
    return None, elapsed


# ─── Orchestration ───────────────────────────────────────────────────────────

def _is_standby():
    # Either standby source disables the system: the Rachio-native switch OR the
    # input_boolean.irrigation_standby helper (the user-facing disable toggle).
    # Reads _current_bindings (module global, not a param) for the same reason
    # _is_rain() reads _current_tun: this is passed by reference as a callback
    # and pyscript cannot close over enclosing locals.
    #
    # Each read is NameError-tolerant, same pattern as the spray-switch loop in
    # _rain_condition_now(): a bad/renamed standby entity id must degrade to
    # "not in standby" rather than crash the watch loop.
    standby = False
    try:
        if state.get(_current_bindings.standby_switch) == "on":
            standby = True
    except NameError:
        pass
    try:
        if state.get(_current_bindings.standby_boolean) == "on":
            standby = True
    except NameError:
        pass
    return standby


def _is_manual_stop():
    return _manual_stop


def _is_rain():
    """Rain-abort predicate for run_plan, structured to match the HA automation
    "Irrigation - Detect Adverse Watering Conditions" so the two do not fight
    over the valves. NB the spray-zone rule diverged on 2026-08-11: this now
    corroborates with the rain gauge, while the automation still uses RH >= 92.
    Until that automation is switched to the gauge too, it can still stop a run
    on a spray-flagged zone's own spray (an `external-stop`) that this predicate
    no longer triggers.

      - hail / rain_hail aborts IMMEDIATELY (that automation trigger has no
        `for:` delay);
      - plain rain must persist `rain_sustain_seconds` (its 2m30s `for:`) before
        aborting, so a momentary misread does not kill a run;
      - while any zone flagged `spray: true` in config (config.spray_switches)
        is watering, "rain" is only believed if it is hail or the rain gauge
        shows accumulation — the Tempest misreads such a zone's overspray as
        "rain" while the gauge stays at 0, so without this the system aborts
        on its own sprinkler.

    Module-level (reading the _current_tun/_current_cfg globals) rather than a
    closure: pyscript lambdas and nested defs do NOT capture enclosing-function
    locals.
    """
    global _rain_since
    holds, hail = _rain_condition_now()
    abort_now, _rain_since = weather.rain_sustain_step(
        holds, hail, _rain_since, time.time(),
        _current_tun.rain_sustain_seconds if _current_tun else 0,
    )
    return abort_now


def _rain_condition_now():
    """(condition_holds, is_hail) for the automation's rule, right now.

    The single reading both rain gates share, so the "is it raining" question
    cannot drift between them — they differ ONLY in whether a sustain applies.
    """
    if _current_tun is None or _current_cfg is None:
        return False, False
    wx = _read_weather(_current_tun)
    # Explicit loop (no genexpr/any() closure — pyscript does not implement
    # generator expressions, and a lambda/nested-def here could not capture
    # `spray_on` from the enclosing scope anyway). Any flagged spray zone
    # corroborating with the gauge is enough to treat "rain" as its overspray.
    spray_on = False
    for sw in config.spray_switches(_current_cfg):
        try:
            if state.get(sw) == "on":
                spray_on = True
        except NameError:
            pass
    return weather.is_rain_abort(wx, _current_tun, spray_on), weather.is_hail(wx)


def _is_rain_at_start():
    """Rain gate for the moment a block is about to begin — NO sustain.

    `_is_rain()` cannot return True on its first evaluation: with no history it
    starts the sustain clock and defers. That is right for interrupting a
    running block and wrong here. The automation this mirrors makes the same
    distinction — its `rain_sustained` trigger waits 2m30s, but
    `irrigation_started` fires the instant watering begins, with no delay, and
    with any zone other than the spray-flagged one running its condition reduces
    to bare `precip`. On 2026-07-28 both facts collided: our gate deferred on a
    momentary reading, the block launched, and the automation stopped it
    seconds later.

    At the start there is nothing to protect. Waiting costs nothing; starting
    costs a Rachio call, a push notification, and a fight with an automation
    that has zero tolerance.
    """
    holds, _hail = _rain_condition_now()
    return holds


def _read_weather(tun):
    # Weather only sizes the disease window; a missing/unavailable sensor must
    # degrade to a default, never crash the run. pyscript's state.get raises
    # NameError when an entity does not exist at all (distinct from a present-
    # but-"unavailable" value that float() rejects), so catch NameError too and
    # warn once per missing entity (a wrong/absent entity id is a misconfig).
    def num(entity, default=0.0):
        try:
            return float(state.get(entity))
        except NameError:
            log.warning(f"irrigation: weather entity {entity!r} not found; using {default}")
            return default
        except (ValueError, TypeError):
            return default

    def text(entity, default=""):
        try:
            return state.get(entity)
        except NameError:
            log.warning(f"irrigation: weather entity {entity!r} not found; using {default!r}")
            return default

    wx_ids = _current_bindings.weather
    return weather.WeatherReading(
        temp_f=num(wx_ids.temperature),
        rh_pct=num(wx_ids.humidity),
        # Averaged local wind smooths momentary lulls that would otherwise
        # falsely trip the "stagnant" disease signal on a once-per-night read.
        wind_mph=num(wx_ids.wind),
        dew_formed=text(_current_bindings.dew_formed_boolean) == "on",
        rain_last_hour_mm=num(wx_ids.rain_last_hour),
        precip_type=text(wx_ids.precip_type) or "none",
    )


def _forecast_num(entity):
    """Read a numeric forecast sensor. Returns None when unusable.

    Forecast data is an optimisation, never a prerequisite: a missing entity
    (state.get raises NameError — gotcha #10) or an `unknown` / `unavailable`
    value returns None so the caller falls back. Warnings go to the SYSTEM log
    because an absent forecast sensor is a misconfiguration or outage, not
    routine operation.
    """
    try:
        raw = state.get(entity)
    except NameError:
        log.warning(f"irrigation: forecast entity {entity!r} not found")
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning(f"irrigation: forecast entity {entity!r} unusable ({raw!r})")
        return None


def _read_forecast_weather():
    """A WeatherReading describing the OVERNIGHT hours, for the window cap only.

    Returns None if any component is unavailable, so the caller falls back to
    instantaneous readings.

    dew_formed is deliberately False: `input_boolean.dew_formed` is a CURRENT
    observation, and using it to describe 00:00-05:00 is exactly the staleness
    this replaces. Sustained overnight RH at or above humid_rh_pct already
    captures dew conditions.

    The precipitation fields are zeroed: this reading sizes the disease window
    only. Rain ABORT during a run stays instantaneous (_read_weather) and still
    mirrors the HA adverse-conditions automation.
    """
    d = _current_bindings.derived.forecast_overnight
    temp_f = _forecast_num(d["temp"])
    rh_pct = _forecast_num(d["humidity"])
    wind_mph = _forecast_num(d["wind"])
    if temp_f is None or rh_pct is None or wind_mph is None:
        return None
    return weather.WeatherReading(
        temp_f=temp_f, rh_pct=rh_pct, wind_mph=wind_mph,
        dew_formed=False, rain_last_hour_mm=0.0, precip_type="none",
    )


def _read_observed_overnight():
    """WeatherReading from the OBSERVED overnight means, or None if unusable.

    Deliberately mirrors _read_forecast_overnight: precipitation fields zeroed
    and dew_formed False, so the two readings differ only in whether the numbers
    were predicted or measured. Anything else would compare two models rather
    than one model against reality.
    """
    d = _current_bindings.derived.observed_overnight
    temp_f = _forecast_num(d["temp"])
    rh_pct = _forecast_num(d["humidity"])
    wind_mph = _forecast_num(d["wind"])
    if temp_f is None or rh_pct is None or wind_mph is None:
        return None
    return weather.WeatherReading(
        temp_f=temp_f, rh_pct=rh_pct, wind_mph=wind_mph,
        dew_formed=False, rain_last_hour_mm=0.0, precip_type="none",
    )


def _read_forecast_precip(horizon_hours):
    """(probability_pct, amount_mm) for the given horizon; (None, None) if
    unavailable. The horizon comes from the active drought profile, and Task 1
    publishes sensors for 12/18/24 h."""
    d = _current_bindings.derived
    prob = _forecast_num(f"{d.precipitation_chance_prefix}{horizon_hours}_hour")
    amount = _forecast_num(f"{d.precipitation_amount_prefix}{horizon_hours}_hour")
    return prob, amount


def _read_zone_signals(zone):
    return sensors.ZoneSignals(
        dominant=state.get(zone.dominant_sensor),
        state=state.get(zone.state_sensor),
        # pyscript does not implement generator expressions (ast_generatorexp);
        # a list comprehension is supported and equivalent here.
        qualities=tuple([state.get(q) for q in zone.quality_sensors]),
    )


def _dawn_time():
    return dt.datetime.fromisoformat(state.get(_current_bindings.sun.dawn))


def _end_anchor_time(anchor):
    """When the watering window must finish, for the given anchor.

    `anchor` is a drought profile's `end_anchor` ("dawn" | "sunrise"). Falls back
    to dawn when that anchor's sensor does not exist (state.get raises NameError
    for a nonexistent entity — gotcha #10), because dawn is the earlier of the
    two: a lookup failure must shorten the window, never extend watering past
    sunrise.
    """
    entity = (
        _current_bindings.sun.sunrise if anchor == "sunrise"
        else _current_bindings.sun.dawn
    )
    try:
        return dt.datetime.fromisoformat(state.get(entity))
    except NameError:
        log.warning(
            f"irrigation: end-anchor entity {entity!r} not found; using dawn"
        )
        return _dawn_time()


def _plan_context(cfg):
    """Evaluate zones and build the full plan — no execution. Shared by the
    real run and the preview so both see identical decisions."""
    tun = cfg.tunables
    # pyscript's state.get raises NameError when the entity does not exist at all
    # (vs. returning "unavailable" for a present-but-unknown entity). Treat a
    # missing helper the same as an unknown value: fall back to Level 3 - Critical
    # and warn to the system log — a missing helper is a misconfiguration, not
    # routine operation. Deliberately NOT Level 4 - Emergency: that never waters,
    # so a typo in the helper's options would silently let the lawn die. Level 3
    # is the least-water profile that still waters.
    try:
        level = state.get(cfg.bindings.drought_level_select)
    except NameError:
        level = None
    profile = cfg.drought_profiles.get(level)
    if profile is None:
        log.warning(
            f"irrigation: drought level {level!r} missing or unknown; "
            "defaulting to Level 3 - Critical"
        )
        profile = cfg.drought_profiles["Level 3 - Critical"]
        level = "Level 3 - Critical"
    rng = random.Random()

    evals, targets, uncompleted = [], {}, {}
    for key, zone in cfg.zones.items():
        reading = sensors.read_zone(zone, _read_zone_signals(zone))
        if not reading.online:
            uncompleted[key] = reading.offline_reason
            continue
        tgt = drought.effective_target(zone, cfg.bands, profile)
        targets[key] = tgt
        evals.append(evaluate.evaluate_zone(reading, tgt))

    priority = [e.key for e in evaluate.sort_by_priority(evals, rng)]

    # Per-zone full-refill runtime: prefer a live Rachio pull (keyed by
    # rachio_zone_id), else the static runtime_minutes from config.yaml.
    # Prefer the live Rachio runtime (keyed by rachio_zone_id), else the static
    # runtime_minutes from config.yaml. Record which source each zone used: the
    # fallback is otherwise SILENT, so a mid-season Rachio outage would quietly
    # plan on stale values with nothing to show for it.
    api_runtimes = get_runtimes()
    minutes = {}
    runtime_sources = {}
    for k in priority:
        zone_cfg = cfg.zones[k]
        live = api_runtimes.get(zone_cfg.rachio_zone_id)
        if live:
            base = live
            runtime_sources[k] = "live"
        else:
            base = zone_cfg.runtime_minutes
            runtime_sources[k] = "static"
        minutes[k] = plan.cycles_minutes(base, targets[k].runtime_scale)
    geo = {k: cfg.zones[k].geography for k in priority}
    adjacency = {k: cfg.zones[k].adjacency for k in priority}

    # The window cap describes the hours the grass will be WET, so it is sized
    # from the overnight forecast, not from conditions at plan time. Rain abort
    # during a run stays instantaneous (see _is_rain).
    wx = _read_weather(tun)
    forecast_wx = _read_forecast_weather()
    if forecast_wx is None:
        cap_source = "instant"
        log.warning(
            "irrigation: overnight forecast unavailable; sizing the window from "
            "current conditions"
        )
        cap = weather.window_cap_minutes(wx, tun)
    else:
        cap_source = "forecast"
        cap = weather.window_cap_minutes(forecast_wx, tun)
    # The end anchor is per drought profile: Levels 0-2 finish at sunrise so
    # watering ends just as drying begins; Level 3 finishes at dawn, before any
    # sun, to minimise evaporative loss when water is scarce.
    end = _end_anchor_time(profile.end_anchor) - dt.timedelta(
        minutes=tun.end_offset_minutes
    )

    # Clamp the disease-window cap to the time actually left before the end
    # anchor, so a plan can never be scheduled to start in the past or run past
    # dawn. Matters most for a mid-window restart (self-heal), where only part of
    # the window remains; it also keeps the nightly plan honest when the trigger
    # fires later than the theoretical window open.
    remaining = (end - dt.datetime.now(end.tzinfo)).total_seconds() / 60.0
    if remaining < cap:
        cap = max(0.0, remaining)

    the_plan = plan.build_plan(priority, minutes, geo, adjacency, cap, tun)
    for z in the_plan.dropped:
        uncompleted[z] = "insufficient window"

    start = end - dt.timedelta(minutes=the_plan.span_minutes)
    earliest_start = end - dt.timedelta(minutes=cap)
    return {
        "cfg": cfg, "tun": tun, "level": level, "priority": priority,
        "minutes": minutes, "uncompleted": uncompleted, "the_plan": the_plan,
        "start": start, "end": end,
        "wx": wx, "cap_minutes": cap, "earliest_start": earliest_start,
        "runtime_sources": runtime_sources,
        "forecast_wx": forecast_wx, "cap_source": cap_source,
        "horizon_hours": profile.rain_skip_horizon_hours,
        "end_anchor": profile.end_anchor,
        "refill_depths": get_refill_depths(),
    }


def _pressure_pair(ctx):
    """(forecast-based, instantaneous) disease-pressure breakdowns.

    Report BOTH models: the forecast one drives the cap, the instantaneous one
    is the old behaviour. Side by side they are the calibration instrument for
    the retuned overnight thresholds — which is why the nightly RUN publishes
    them too, not just the preview. Comparing them required firing a preview by
    hand at 23:00 every night, which nobody was going to do.
    """
    pb_instant = weather.pressure_breakdown(ctx["wx"], ctx["tun"])
    if ctx["forecast_wx"] is None:
        return pb_instant, pb_instant
    return weather.pressure_breakdown(ctx["forecast_wx"], ctx["tun"]), pb_instant


def _publish_record(entity, value, attributes):
    """Publish a diagnostic state AND persist it, so a restart cannot erase it.

    File I/O goes through task.executor because it blocks, and lives in a
    @pyscript_compile helper because `open` is not a builtin in pyscript's
    interpreter (gotcha #6). A persistence failure must never take down the run
    that produced the record, so it warns and continues — the in-memory state is
    still published either way.
    """
    state.set("pyscript." + entity, value=value, new_attributes=attributes)
    try:
        task.executor(
            _write_json_atomic, STATE_DIR + "/" + entity + ".json",
            {"value": value, "attributes": attributes},
        )
    except Exception as err:
        log.warning(f"irrigation: could not persist {entity} ({err})")


def _restore_records():
    """Re-publish persisted diagnostic states after a restart.

    Without this the morning-after question ("what happened last night?") is
    unanswerable whenever HA restarted in between, which is exactly when it gets
    asked. A corrupt or unreadable file must not stop startup — the safety-stop
    that follows matters more than a diagnostic.
    """
    for entity in PERSISTED:
        try:
            data = task.executor(_read_json, STATE_DIR + "/" + entity + ".json")
        except Exception as err:
            log.warning(f"irrigation: could not restore {entity} ({err})")
            continue
        if not data:
            continue
        state.set("pyscript." + entity, value=data["value"],
                  new_attributes=data["attributes"])


def _publish_last_run(stamp, trigger, ctx=None, result=None, outcome=None,
                      skipped=None):
    """Full, untruncated record of what the nightly run actually did.

    The run used to leave behind one Logbook line and a recap; everything
    diagnostic lived in `pyscript.irrigation_preview`, written only by the
    preview service. So answering "did last night work?" meant cross-reading the
    Logbook, the recap, the system log and the Rachio app — and two open items
    (threshold calibration, quantized-vs-planned minutes) were simply
    unanswerable after the fact. This state is that record.
    """
    attributes = {
        "friendly_name": "Irrigation Last Run",
        "updated": stamp,
        # Which trigger produced this record. Without it the state is ambiguous:
        # a manual run and the nightly one look identical, and you cannot tell
        # which night you are reading.
        "trigger": trigger,
    }
    if skipped is not None:
        attributes["skipped"] = skipped
    if ctx is not None:
        pb, pb_instant = _pressure_pair(ctx)
        the_plan = ctx["the_plan"]
        attributes.update({
            "drought_level": ctx["level"],
            "window_cap_hours": round(ctx["cap_minutes"] / 60.0, 2),
            "window_cap_source": ctx["cap_source"],
            "end_anchor": ctx["end_anchor"],
            "pressure_forecast": pb,
            "pressure_instant": pb_instant,
            "planned_zones": the_plan.watered,
            # Fractional, straight from the plan: compare against
            # delivered_minutes below to see the whole-minute quantization.
            "planned_minutes": {k: round(ctx["minutes"][k], 2)
                                for k in the_plan.watered},
            "planned_span_minutes": the_plan.span_minutes,
            "runtime_sources": ctx["runtime_sources"],
            "horizon_hours": ctx["horizon_hours"],
        })
    if result is not None:
        attributes.update({
            "start": result.start, "end": result.end,
            "window_start": result.window_start, "window_end": result.window_end,
            "watered": result.watered,
            "delivered_minutes": result.per_zone_minutes,
            "uncompleted": result.uncompleted,
        })
    if outcome is not None:
        # What was actually asked of Rachio, block by block — the only record of
        # the durations that left this app.
        attributes.update({
            "aborted_reason": outcome["aborted_reason"],
            "block_count": len(outcome.get("blocks", [])),
            "blocks": outcome.get("blocks", []),
        })
    attributes["rachio_calls"] = api_calls
    attributes["state_polls"] = state_polls
    value = len(result.watered) if result is not None else 0
    state.set("pyscript.irrigation_last_run", value=value,
              new_attributes=attributes)
    # The unattended run keeps its OWN copy. `last_run` is literally the last
    # one, so a manual re-run overwrites it — which is exactly what happened
    # while diagnosing the 2026-08-09 abort: the run_now erased the night we
    # were trying to read. A self-heal counts as the night's run; a run_now
    # does not.
    if trigger == "nightly" or trigger == "startup-heal":
        nightly = dict(attributes)
        nightly["friendly_name"] = "Irrigation Last Nightly Run"
        _publish_record("irrigation_last_nightly", value, nightly)


def _rain_skip_check(ctx):
    """(should_skip, detail) for the plan in `ctx`.

    The amount threshold is a fraction of the MEAN refill depth across the zones
    actually planned tonight — it answers "would this rain substitute for
    tonight's run?" rather than favouring the smallest or largest zone. With no
    zones planned there is nothing to skip, so it returns False.

    Fails open: any unavailable forecast value yields False (see
    weather.is_rain_skip).
    """
    cfg = ctx["cfg"]
    planned = ctx["the_plan"].watered
    if not planned:
        return False, {}

    api_depths = ctx["refill_depths"]
    depths = []
    for key in planned:
        zone_cfg = cfg.zones[key]
        # Live Rachio depth preferred, static config.yaml seed as fallback —
        # the same precedence used for runtimes.
        depth = api_depths.get(zone_cfg.rachio_zone_id) or zone_cfg.refill_depth_mm
        if depth > 0:
            depths.append(depth)
    if not depths:
        return False, {}
    mean_depth = sum(depths) / len(depths)

    horizon = ctx["horizon_hours"]
    prob, amount = _read_forecast_precip(horizon)
    skip = weather.is_rain_skip(prob, amount, mean_depth, ctx["tun"])
    detail = {
        "horizon_hours": horizon,
        "probability_pct": prob,
        "amount_mm": amount,
        "mean_refill_depth_mm": round(mean_depth, 2),
        "threshold_mm": round(ctx["tun"].rain_skip_refill_fraction * mean_depth, 2),
    }
    return skip, detail


def _plan_and_run(wait, trigger):
    """Plan and water. `wait=True` (nightly) sleeps until the pre-dawn window;
    `wait=False` (run-now) executes immediately."""
    global _manual_stop, _current_tun, _current_cfg, _current_bindings
    global _rain_since, _run_in_progress
    _manual_stop = False
    _rain_since = None  # fresh sustain clock; a stale one could abort instantly
    reset_counters()
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    _set_status("planning")
    cfg = config.parse_config(task.executor(_read_config_yaml, CONFIG_PATH))
    tun = cfg.tunables
    # _is_rain()/_is_standby() etc. read these globals rather than closing over
    # locals — pyscript lambdas/nested defs cannot see enclosing-function
    # locals, and several of these are also called as bare-reference callbacks.
    _current_tun = tun
    _current_cfg = cfg
    _current_bindings = cfg.bindings
    _run_in_progress = True
    try:
        if _is_standby():
            # Record the standby note at the END of the potential watering window,
            # not at the 23:00 trigger time. Otherwise the standby calendar entry
            # lands on the previous DAY relative to every normal recap (which is
            # written pre-dawn), making the two impossible to compare.
            event_time = None
            if wait:
                try:
                    event_time = _dawn_time() - dt.timedelta(
                        minutes=tun.end_offset_minutes
                    )
                except Exception as err:
                    log.warning(
                        f"irrigation: dawn unavailable for standby note ({err}); "
                        "recording at the current time"
                    )
            standby_result = report_format.RunResult(
                [], {}, "", "", {}, standby=True
            )
            _publish_last_run(stamp, trigger, result=standby_result, skipped="standby")
            _set_status("standby")
            _activity("Standby — nothing watered tonight")
            _report(standby_result, tun, event_time=event_time)
            return

        ctx = _plan_context(cfg)
        the_plan, start = ctx["the_plan"], ctx["start"]
        uncompleted, priority = ctx["uncompleted"], ctx["priority"]

        skip, skip_detail = _rain_skip_check(ctx)
        if skip:
            summary = (
                f"rain forecast ({int(skip_detail['probability_pct'])}%, "
                f"{skip_detail['amount_mm']}mm / {skip_detail['horizon_hours']}h)"
            )
            if not wait:
                # run_now expresses intent to water; the forecast informs, not vetoes.
                log.warning(f"irrigation: {summary} — watering anyway (manual run)")
            else:
                for z in priority:
                    uncompleted[z] = "rain-forecast"
                result = report_format.RunResult(
                    watered=[], uncompleted=uncompleted,
                    start="", end="", per_zone_minutes={}, standby=False,
                    window_start=ctx["earliest_start"].astimezone().strftime("%H:%M"),
                    window_end=ctx["end"].astimezone().strftime("%H:%M"),
                    window_hours=round(ctx["cap_minutes"] / 60.0, 2),
                )
                _publish_last_run(stamp, trigger, ctx=ctx, result=result,
                                  skipped="rain-forecast")
                _set_status("skipped", detail=summary)
                _activity(f"Skipped: {summary}")
                _report(result, tun, event_time=ctx["end"])
                return

        if wait:
            wait_s = (start - dt.datetime.now(start.tzinfo)).total_seconds()
            if wait_s > 0:
                start_str = start.astimezone().strftime("%H:%M")
                _set_status("waiting", detail=f"watering starts {start_str}")
                _activity(
                    f"Plan ready — {len(the_plan.watered)} zone(s), "
                    f"{int(the_plan.span_minutes)} min span, starting {start_str}"
                )
                task.sleep(wait_s)

            # The plan was built at 23:00 but watering starts hours later, and the
            # forecast refreshes every 15 minutes. Without this, a forecast that
            # turns bad after planning is ignored entirely — the in-run abort only
            # detects rain already falling.
            skip_now, skip_now_detail = _rain_skip_check(ctx)
            if skip_now:
                summary = (
                    f"rain forecast ({int(skip_now_detail['probability_pct'])}%, "
                    f"{skip_now_detail['amount_mm']}mm / "
                    f"{skip_now_detail['horizon_hours']}h)"
                )
                for z in priority:
                    uncompleted[z] = "rain-forecast"
                result = report_format.RunResult(
                    watered=[], uncompleted=uncompleted,
                    start="", end="", per_zone_minutes={}, standby=False,
                    window_start=ctx["earliest_start"].astimezone().strftime("%H:%M"),
                    window_end=ctx["end"].astimezone().strftime("%H:%M"),
                    window_hours=round(ctx["cap_minutes"] / 60.0, 2),
                )
                _publish_last_run(stamp, trigger, ctx=ctx, result=result,
                                  skipped="rain-forecast-at-window-start")
                _set_status("skipped", detail=summary)
                _activity(f"Skipped at window start: {summary}")
                _report(result, tun, event_time=ctx["end"])
                return

        zone_switches = {k: cfg.zones[k].rachio_switch for k in the_plan.watered}
        # When watering ACTUALLY begins, which is not the planned start. For a
        # nightly run the two coincide, because the plan's start is what we slept
        # until — but run_now does not sleep, so reporting the planned pre-dawn
        # start put the recap and the calendar entry 15 minutes adrift of the run
        # they describe. RunResult.start is documented as "when watering actually
        # began"; now it is.
        began = dt.datetime.now().astimezone()
        _set_status("watering", detail=f"{len(the_plan.watered)} zone(s)")
        outcome = run_plan(
            the_plan.slots, zone_switches,
            _is_standby, _is_manual_stop, _is_rain, _is_rain_at_start,
        )

        watered = outcome["watered"]
        delivered = outcome.get("delivered_minutes", {})
        if outcome["aborted_reason"]:
            for z in priority:
                if z not in watered and z not in uncompleted:
                    uncompleted[z] = outcome["aborted_reason"]

        end_anchor = ctx["end"]
        # Timezone-aware: `start` carries tzinfo, and the calendar entry compares
        # the two. A naive now() here would raise on that comparison.
        finished = dt.datetime.now().astimezone()
        result = report_format.RunResult(
            watered=watered, uncompleted=uncompleted,
            start=began.strftime("%H:%M"),
            end=finished.strftime("%H:%M"),
            per_zone_minutes={k: delivered.get(k, 0) for k in watered}, standby=False,
            # The disease window that was available, reported separately from the
            # actual watering times (which collapse to a point when nothing ran).
            window_start=ctx["earliest_start"].astimezone().strftime("%H:%M"),
            window_end=end_anchor.astimezone().strftime("%H:%M"),
            window_hours=round(ctx["cap_minutes"] / 60.0, 2),
        )
        # Record what happened BEFORE announcing it. The recap talks to notify and
        # calendar — external services whose failure modes this app does not own —
        # and when it went first, one rejected calendar event destroyed the entire
        # diagnostic trail: no irrigation_last_run, no per-zone entries, status
        # stranded on "watering", no "Run complete". The diagnostics exist for
        # exactly the nights that go wrong, so nothing fragile runs ahead of them.
        _publish_last_run(stamp, trigger, ctx=ctx, result=result, outcome=outcome)
        _log_zone_outcomes(cfg, watered, delivered, uncompleted)
        aborted_reason = outcome["aborted_reason"]
        if aborted_reason:
            _set_status("aborted", detail=aborted_reason)
        else:
            _set_status("idle")
        _activity(
            f"Run complete — rachio_calls={api_calls}, state_polls={state_polls}"
        )
        # Anchor the recap to the end of the window so nightly entries always land
        # on the same date, whatever time the run actually finished. When water
        # actually ran, the entry SPANS the watering instead (see _report).
        _report(
            result, tun, event_time=end_anchor if wait else None,
            started_at=began if watered else None,
            ended_at=finished if watered else None,
        )
    finally:
        _run_in_progress = False


def _preview():
    """Report the plan that WOULD run — no valves opened, no calendar written.

    Three surfaces (title 'Irrigation preview' on the notification carries the
    label, so message bodies never repeat it):
      - notify + Logbook: short human summary (Logbook truncates long text);
      - pyscript.irrigation_preview state (Developer Tools -> States): the FULL,
        untruncated breakdown incl. the dynamic-window characteristics.
    """
    global _current_cfg, _current_bindings
    if _run_in_progress:
        msg = "Preview skipped — an irrigation run is already in progress."
        service.call(
            "notify", _current_bindings.notify_service.split(".", 1)[-1],
            message=msg, title="Irrigation Preview",
        )
        _activity("Preview skipped: run in progress")
        return
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    cfg = config.parse_config(task.executor(_read_config_yaml, CONFIG_PATH))
    # Preview runs outside _plan_and_run, so it must load these globals itself —
    # every entity read below (_is_standby, _plan_context's weather/forecast/sun
    # reads, the notify call) goes through _current_bindings, not a local.
    _current_cfg = cfg
    _current_bindings = cfg.bindings
    if _is_standby():
        msg = "System in Standby — a run would water nothing."
        service.call(
            "notify", _current_bindings.notify_service.split(".", 1)[-1],
            message=msg, title="Irrigation Preview",
        )
        _activity("Preview: " + msg)
        state.set(
            "pyscript.irrigation_preview", value="standby",
            new_attributes={"updated": stamp, "standby": True, "message": msg},
        )
        return

    ctx = _plan_context(cfg)
    the_plan = ctx["the_plan"]
    wx = ctx["wx"]
    pb, pb_instant = _pressure_pair(ctx)
    would_skip, skip_detail = _rain_skip_check(ctx)
    cap_hours = round(ctx["cap_minutes"] / 60.0, 2)
    active = [name for name, on in
              (("warm", pb["warm"]), ("humid", pb["humid"]), ("stagnant", pb["stagnant"]))
              if on]
    active_str = ", ".join(active) if active else "none"
    start_str = ctx["start"].astimezone().strftime("%H:%M")
    end_str = ctx["end"].astimezone().strftime("%H:%M")
    earliest_str = ctx["earliest_start"].astimezone().strftime("%H:%M")
    span = int(the_plan.span_minutes)
    offset = ctx["tun"].end_offset_minutes
    # The window is the disease window (earliest_start..end, cap hours long,
    # ending just before dawn) — independent of how many zones water. The
    # watering (span) is packed within it.
    window_note = (
        f"Window: {earliest_str}–{end_str} ({cap_hours}h, "
        f"pressure {pb['count']}/3: {active_str}), "
        f"ends {offset} min before {ctx['end_anchor']}"
    )
    planned = [(z, ctx["minutes"][z]) for z in the_plan.watered]
    # Span is wall-clock (water + idle soaks); water_minutes is what is actually
    # delivered. They differ a lot for a lone zone, which must idle-soak 20 min
    # between its own cycles — reporting the span as "Watering" read as if it
    # contradicted the per-zone minutes.
    water_minutes = 0
    for _z, _m in planned:
        water_minutes += _m
    water_minutes = int(water_minutes)
    watering_note = (
        f"Runs {start_str}–{end_str} — {span} min span "
        f"({water_minutes} min water + soaks)"
    )

    msg = report_format.format_preview(
        ctx["level"], planned, ctx["uncompleted"],
        window_note=window_note, watering_note=watering_note,
    )
    # Make a silent fallback loud: if any planned zone used the static
    # config.yaml runtime, the Rachio pull failed for it and the plan is on
    # possibly-stale values.
    runtime_sources = ctx["runtime_sources"]
    static_zones = [k for k in the_plan.watered if runtime_sources.get(k) == "static"]
    if static_zones:
        msg = msg + (
            "\nNOTE: static fallback runtimes (Rachio pull failed) for: "
            + ", ".join(static_zones)
        )
    if would_skip:
        msg = msg + (
            f"\nWOULD SKIP: rain forecast "
            f"({int(skip_detail['probability_pct'])}%, {skip_detail['amount_mm']}mm"
            f" / {skip_detail['horizon_hours']}h)"
        )
    service.call(
        "notify", _current_bindings.notify_service.split(".", 1)[-1],
        message=msg, title="Irrigation Preview",
    )
    _activity(
        f"Preview: would water {len(the_plan.watered)} zone(s), {water_minutes} min "
        f"water over a {span} min span, in window {earliest_str}-{end_str} "
        f"({cap_hours}h, pressure {pb['count']}/3)"
    )

    fwx = ctx["forecast_wx"]
    if fwx is None:
        forecast_weather = {}
    else:
        forecast_weather = {
            "overnight_temp_f": fwx.temp_f,
            "overnight_rh_pct": fwx.rh_pct,
            "overnight_wind_mph": fwx.wind_mph,
        }

    # Full, untruncated breakdown — Developer Tools -> States.
    state.set(
        "pyscript.irrigation_preview",
        value=len(the_plan.watered),
        new_attributes={
            "updated": stamp,
            "drought_level": ctx["level"],
            "window_cap_hours": cap_hours,
            "window_cap_source": ctx["cap_source"],
            "end_anchor": ctx["end_anchor"],
            "pressure_forecast": pb,
            "pressure_instant": pb_instant,
            "pressure_count": pb["count"],
            "pressure_active": active,
            "earliest_start": earliest_str,
            "start": start_str,
            "end": end_str,
            "span_minutes": span,          # wall-clock incl. idle soaks
            "water_minutes": water_minutes,  # actually delivered
            # Per planned zone: "live" (Rachio API) or "static" (config.yaml).
            "runtime_sources": runtime_sources,
            "runtimes_all_live": len(static_zones) == 0,
            "weather": {
                "temp_f": wx.temp_f, "rh_pct": wx.rh_pct, "wind_mph": wx.wind_mph,
                "dew_formed": wx.dew_formed, "precip_type": wx.precip_type,
                "rain_last_hour_mm": wx.rain_last_hour_mm,
            },
            "planned_minutes": {z: int(m) for z, m in planned},
            "priority": ctx["priority"],
            "skipped": ctx["uncompleted"],
            "message": msg,
            "forecast_available": fwx is not None,
            "forecast_weather": forecast_weather,
            "rain_skip": would_skip,
            "rain_skip_detail": skip_detail,
        },
    )


def _log_zone_outcomes(cfg, watered, delivered, uncompleted):
    """File each zone's result under the ZONE, not just the run.

    This is what makes "what has this zone been doing?" a filter rather than a
    scroll: the entry lands in switch.<zone>'s own Logbook, next to the Rachio
    on/off events for the same night. Zones that did not water are logged too —
    the reason a zone was skipped is exactly what you go looking for later.
    """
    for zone_key in watered:
        zone = cfg.zones.get(zone_key)
        if zone is None:
            continue
        _activity(
            f"{report_format.display_name(zone_key)} watered "
            f"{round(delivered.get(zone_key, 0))} min",
            entity_id=zone.rachio_switch,
        )
    for zone_key, reason in uncompleted.items():
        zone = cfg.zones.get(zone_key)
        if zone is None:
            continue
        _activity(
            f"{report_format.display_name(zone_key)} not watered ({reason})",
            entity_id=zone.rachio_switch,
        )


def _report(result, tun, event_time=None, started_at=None, ended_at=None):
    """Notify + write the calendar entry.

    When water actually ran, `started_at`/`ended_at` make the entry SPAN the
    watering, so the calendar shows at a glance how long the system was out
    there. Otherwise — standby notes, rain skips, nothing watered — there is no
    interval to draw, and the entry is a 1-minute marker at `event_time`: the
    end-of-window anchor for nightly runs, so every nightly entry lands at the
    same point on the same date, or now for a run-now.

    Never zero-duration. HA's Local Calendar enforces a minimum of one second
    (`Expected minimum event duration of 0:00:01`), so a point-in-time entry is
    rejected 100% of the time on this backend. The old code asked for one
    anyway and fell back after catching the failure, which put a guaranteed
    warning in the system log every single night — in a design where the system
    log is supposed to carry errors ONLY, and noise in it means something is
    wrong.

    NOTE: a long window can start before midnight (the window opens up to 6 h
    before a ~04:55 dawn), in which case a spanning entry begins on the previous
    day. That is accurate rather than tidy, and only affects which day the entry
    sorts under.
    """
    msg = report_format.format_notification(result)
    service.call(
        "notify", _current_bindings.notify_service.split(".", 1)[-1],
        message=msg, title="Irrigation Recap",
    )
    title, desc = report_format.format_calendar(
        result, tun.cycle_minutes, tun.soak_minutes
    )
    fallback = event_time if event_time is not None else dt.datetime.now()
    start_dt, end_dt = report_format.calendar_span(started_at, ended_at, fallback)
    # A calendar problem must cost the calendar entry and NOTHING else. This
    # call is the least important thing the run does and the most external:
    # it reaches a service whose validation this app does not control.
    try:
        service.call(
            "calendar", "create_event", entity_id=_current_bindings.calendar_entity,
            summary=title, description=desc,
            start_date_time=start_dt.isoformat(), end_date_time=end_dt.isoformat(),
        )
    except Exception as err:
        log.warning(f"irrigation: calendar entry failed ({err}); recap was sent")


# ─── Services & triggers ─────────────────────────────────────────────────────

@time_trigger("cron(0 6 * * *)")
def irrigation_calibrate():
    """Score last night's forecast against what actually happened.

    Runs at 06:00 for a reason: `sensor.observed_overnight_*` are rolling
    6-hour means, so at 06:00 they cover 00:00-06:00 — exactly the window
    `sensor.forecast_overnight_*` averages (`dt.hour < 6`). Same window, same
    statistic, so the two are actually comparable. Reading them at any other
    hour silently compares different periods.

    The forecast side is NOT recomputed here: by 06:00 the forecast sensors
    describe the hours before TOMORROW's dawn. It is read back from the record
    the nightly run left behind.

    Publishes the comparison and logs a one-line verdict, so a week of Logbook
    entries answers the threshold question on its own. Nothing is auto-tuned —
    these thresholds encode turf pathology, not a fitted parameter, and a loop
    quietly adjusting them would change watering with nothing to catch it.
    """
    global _current_cfg, _current_bindings
    if _run_in_progress:
        log.warning(
            "irrigation: calibration skipped — an irrigation run is in "
            "progress and owns the shared config globals"
        )
        return
    cfg = config.parse_config(task.executor(_read_config_yaml, CONFIG_PATH))
    # Runs on its own cron, outside _plan_and_run, so it must load these
    # globals itself before _read_observed_overnight() can use them.
    _current_cfg = cfg
    _current_bindings = cfg.bindings
    tun = cfg.tunables
    observed_wx = _read_observed_overnight()
    if observed_wx is None:
        log.warning(
            "irrigation: overnight observed means unavailable; skipping "
            "calibration for last night"
        )
        return
    try:
        forecast_pb = state.getattr("pyscript.irrigation_last_nightly").get(
            "pressure_forecast"
        )
    except NameError:
        forecast_pb = None
    if not forecast_pb:
        _activity("Calibration skipped — no nightly run to compare against")
        return

    observed_pb = weather.pressure_breakdown(observed_wx, tun)
    agreement = weather.pressure_agreement(forecast_pb, observed_pb)
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    _publish_record(
        "irrigation_calibration",
        len(agreement["mismatches"]),
        {
            "friendly_name": "Irrigation Forecast Calibration",
            "updated": stamp,
            "window": "00:00-06:00",
            "pressure_forecast": forecast_pb,
            "pressure_observed": observed_pb,
            "observed_temp_f": observed_wx.temp_f,
            "observed_rh_pct": observed_wx.rh_pct,
            "observed_wind_mph": observed_wx.wind_mph,
            "signals": agreement["signals"],
            "mismatches": agreement["mismatches"],
            "thresholds": {
                "warm_temp_f": tun.warm_temp_f,
                "humid_rh_pct": tun.humid_rh_pct,
                "stagnant_wind_mph": tun.stagnant_wind_mph,
            },
        },
    )
    if agreement["mismatches"]:
        detail = ", ".join([
            f"{name} {agreement['signals'][name]}"
            for name in agreement["mismatches"]
        ])
    else:
        detail = "all signals agree"
    _activity(
        f"Calibration: forecast {agreement['forecast_count']}/3 vs observed "
        f"{agreement['observed_count']}/3 — {detail} "
        f"(RH {observed_wx.rh_pct}, wind {observed_wx.wind_mph}, "
        f"temp {observed_wx.temp_f})",
        entity_id="pyscript.irrigation_calibration",
    )


@time_trigger("cron(0 23 * * *)")
def irrigation_nightly():
    task.unique("irrigation_run")
    _plan_and_run(wait=True, trigger="nightly")


@time_trigger("startup")
def _on_startup():
    """Safety net after any HA restart or pyscript reload.

    A run interrupted mid-watering leaves a Rachio valve OPEN: pyscript is killed
    before it can stop the zone, the run is NOT resumed (the system is stateless),
    and no recap is sent. Without this, the only backstop is the 6-hour stuck-zone
    automation. On startup we poll the managed zones and close anything still
    running, noting it in the Logbook. A short sleep first lets the Rachio
    integration load its switch entities before we poll them.

    If we DID find an open valve, we then self-heal: re-plan and finish the run.
    Gating the self-heal on "an orphan was found" is what keeps it safe without
    persisted state — an open valve proves the run was interrupted mid-watering
    rather than completed, so we cannot double-water a zone that already
    finished. (GeoDrops moisture lags watering by a while, so a naive
    "re-evaluate on every restart" WOULD re-water a just-finished zone.) The
    trade-off: the zone that was interrupted re-waters from scratch, so it gets
    its partial cycle plus a full one. A restart during the pre-dawn wait, with
    nothing yet open, is indistinguishable from a completed run and is therefore
    skipped — that night is simply missed, costing no water.
    """
    task.sleep(30)
    # pyscript re-creates its entities, so the status would read `unknown` until
    # the next run. Publish it now so a filtered Logbook and any dashboard card
    # have something to point at from the moment HA comes back.
    _set_status("idle")
    # Bring back last night's record and the calibration series. HA does not
    # restore pyscript entities, so without this a restart erases the evidence
    # for the very night someone is about to ask about.
    _restore_records()
    # Time gate: only clean up during the overnight run window (~22:00-07:00).
    # A daytime restart must not stop a syringe / pet-cleanup run that shares a
    # managed zone. (Only managed zones are ever polled — see below.)
    now = dt.datetime.now()
    if not (now.hour >= 22 or now.hour <= 7):
        return
    try:
        cfg = config.parse_config(task.executor(_read_config_yaml, CONFIG_PATH))
    except Exception as err:
        log.warning(f"irrigation: startup safety check skipped; config load failed ({err})")
        return
    running = []
    for zone in cfg.zones.values():
        try:
            if poll_zone_running(zone.rachio_switch):
                running.append(zone.rachio_switch)
        except Exception:
            pass
    if not running:
        # Nothing was open. Either no run was in flight, or one had already
        # finished — we cannot tell the two apart without persisted state, so we
        # deliberately do NOT self-heal here (see the docstring's note on
        # double-watering).
        return

    stop_all(running)
    _activity(
        f"Startup safety: closed {len(running)} zone(s) left open by an "
        f"interrupted run ({', '.join(running)}); no recap was sent for that run"
    )

    # Self-heal. An orphaned open valve is proof the run was interrupted
    # MID-WATERING rather than completed, which is what makes re-planning safe:
    # a completed run could not have left a valve open, so we cannot be
    # re-watering zones that already finished. Re-evaluate from live moisture and
    # finish whatever still needs water in the time left before dawn (the cap is
    # clamped to the remaining window in _plan_context).
    _activity("Startup: re-planning the interrupted run from live moisture")
    task.unique("irrigation_run")
    _plan_and_run(wait=True, trigger="startup-heal")


@service
def irrigation_run_now():
    """Run the full plan immediately (no pre-dawn wait). Waters for real."""
    task.unique("irrigation_run")
    _plan_and_run(wait=False, trigger="run_now")


@service
def irrigation_preview():
    """Dry run: report the plan that would execute, without watering."""
    _preview()


@service
def irrigation_stop():
    global _manual_stop
    _manual_stop = True
    _activity("Manual stop requested")


@service
def irrigation_refresh_runtimes():
    """Force a live Rachio runtime fetch and record the result in the HA Logbook
    (activity log) plus a status state — checkable without the system log:
      1. Logbook entry named "Irrigation" (Settings -> Logbook / activity log);
      2. state  pyscript.irrigation_runtimes  (Developer Tools -> States) — value
         is the zone count; source/live/updated/runtimes_minutes are attributes.
    A live pull yields a non-empty dict keyed by rachio_zone_id; an empty dict
    means the fetch failed and the scheduler is on static config.yaml runtimes.
    """
    global _current_cfg, _current_bindings
    if _run_in_progress:
        log.warning(
            "irrigation: runtime refresh skipped — an irrigation run is in "
            "progress and owns the shared config globals"
        )
        return
    # Standalone service, outside _plan_and_run — load config here so
    # get_runtimes()'s Rachio fetch has _current_bindings.rachio_api_key_secret
    # to look up, same as every other entry point that can reach _fetch_zone_data.
    cfg = config.parse_config(task.executor(_read_config_yaml, CONFIG_PATH))
    _current_cfg = cfg
    _current_bindings = cfg.bindings
    runtimes = get_runtimes(force=True)
    depths = get_refill_depths() if runtimes else {}
    live = bool(runtimes)
    source = "live Rachio" if live else "FAILED — using static config.yaml runtimes"
    stamp = dt.datetime.now().isoformat(timespec="seconds")

    state.set(
        "pyscript.irrigation_runtimes",
        value=len(runtimes),
        new_attributes={
            "source": source,
            "live": live,
            "updated": stamp,
            "runtimes_minutes": runtimes,
            "refill_depths_mm": depths,
        },
    )
    _activity(f"Rachio runtime refresh: {source}; {len(runtimes)} zones {runtimes}")


@state_trigger("input_button.irrigation_stop")
def _on_stop_button():
    global _manual_stop
    _manual_stop = True
    _activity("Stop button pressed")
