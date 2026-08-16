"""Disease-aware window cap and rain-abort decision. Pure Python."""
from __future__ import annotations

from dataclasses import dataclass

from irrigation_lib.config import Tunables


@dataclass(frozen=True)
class WeatherReading:
    temp_f: float
    rh_pct: float
    wind_mph: float
    dew_formed: bool
    rain_last_hour_mm: float
    precip_type: str


def pressure_breakdown(w: WeatherReading, t: Tunables) -> dict:
    """Which disease-pressure signals are active, for diagnostics/reporting.

    Returns {"warm", "humid", "stagnant": bool, "count": int}.
    """
    warm = w.temp_f > t.warm_temp_f
    humid = w.rh_pct > t.humid_rh_pct or w.dew_formed
    stagnant = w.wind_mph < t.stagnant_wind_mph
    return {
        "warm": warm, "humid": humid, "stagnant": stagnant,
        "count": int(warm) + int(humid) + int(stagnant),
    }


def pressure_signals(w: WeatherReading, t: Tunables) -> int:
    return pressure_breakdown(w, t)["count"]


def window_cap_minutes(w: WeatherReading, t: Tunables) -> float:
    signals = pressure_signals(w, t)
    base = t.window_base_cap_hours
    disease = t.window_disease_cap_hours
    hours = base - (base - disease) * (signals / 3.0)
    return hours * 60.0


_HAIL_TYPES = ("rain_hail", "hail")


def is_hail(w: WeatherReading) -> bool:
    """Hail (or mixed rain/hail), which always warrants an immediate abort."""
    return (w.precip_type or "").strip().lower() in _HAIL_TYPES


def is_rain_abort(w: WeatherReading, t: Tunables, spray_zone_on: bool = False) -> bool:
    """Genuine precipitation warranting an abort.

        precip and (hail or not spray_zone or gauge accumulated)

    `spray_zone_on` is the zone whose spray the Tempest misreads as
    precip_type "rain" (a zone flagged `spray: true`). While it runs, "rain" is
    only believed if it is hail or the rain GAUGE shows accumulation — otherwise
    the system would abort its own run on its own sprinkler spray.

    The corroboration is the gauge, not humidity. Humidity was tried and failed
    (2026-08-11: RH 93.75%, gauge 0, the run aborted on its own spray): this
    scheduler waters in the humid pre-dawn window, where RH is high whether or
    not it is raining, so RH cannot tell real rain from overspray. Measured
    accumulation can — genuine rain adds depth within the sustain window; drift
    from a sprinkler does not.

    NOTE: this answers "is precipitation genuine right now"; the *sustain* delay
    matching the automation's 2m30s `for:` is applied by the caller, which needs
    to track time across polls.
    """
    hail = is_hail(w)
    gauge = w.rain_last_hour_mm > t.rain_last_hour_mm
    precip = (
        hail
        or (w.precip_type or "").strip().lower() == "rain"
        or gauge
    )
    if not precip:
        return False
    return hail or (not spray_zone_on) or gauge


def is_rain_skip(prob_pct, amount_mm, refill_depth_mm, t: Tunables) -> bool:
    """Is forecast rain enough to make tonight's watering unnecessary?

    Requires BOTH a confident forecast and a meaningful amount: probability
    alone cannot distinguish a 70% chance of a 0.2mm trace from a 70% chance of
    15mm, and only the latter refills the root zone.

    The amount threshold is a fraction of `refill_depth_mm` — the water that must
    enter the soil to refill the root zone (Rachio's `depthOfWater`) — so it is
    proportional to what a run would actually deliver rather than a fixed depth.

    Fails OPEN: any missing input, or a non-positive refill depth (no zones
    planned, or depths unavailable), returns False so watering proceeds. A
    forecast outage must never stop irrigation.
    """
    if prob_pct is None or amount_mm is None or refill_depth_mm is None:
        return False
    if refill_depth_mm <= 0:
        return False
    threshold_mm = t.rain_skip_refill_fraction * refill_depth_mm
    return prob_pct >= t.rain_skip_probability_pct and amount_mm >= threshold_mm


def rain_sustain_step(condition_holds, hail, rain_since, now, sustain_seconds):
    """One poll of the mid-run rain clock: (abort_now, rain_since).

    Mirrors the automation's `rain_sustained` trigger: plain rain must persist
    `sustain_seconds` before a RUNNING block is killed, so a momentary misread
    cannot cost a night. Hail skips the wait, as the automation's `hail_now`
    trigger does. A condition that clears forfeits its accumulated time.

    Note what this CANNOT do: abort on first sight. There is no history on the
    first call, so it always defers. That is right for interrupting watering
    already in progress and wrong for deciding whether to begin — the automation
    has a separate, undelayed `irrigation_started` trigger for exactly that, and
    starting into rain the automation will stop immediately is how a block came
    to be launched and killed within seconds on 2026-07-28. Callers gate the
    START on the bare condition instead (`is_rain_abort`), and use this only
    while a block is running.
    """
    if not condition_holds:
        return False, None
    if hail:
        return True, rain_since
    if rain_since is None:
        return False, now
    return (now - rain_since) >= sustain_seconds, rain_since


PRESSURE_SIGNAL_NAMES = ("warm", "humid", "stagnant")


def pressure_agreement(forecast, observed):
    """Per-signal comparison of two pressure breakdowns, for calibration.

    The overnight thresholds (`humid_rh_pct`, `stagnant_wind_mph`) are reasoned
    from turf pathology rather than this site's data, so they need checking
    against reality. The valid comparison is the FORECAST mean for 00:00-06:00
    against the OBSERVED mean for the same window — never against an
    instantaneous reading, which samples one moment of the distribution the mean
    averages over, and near its extreme: humidity peaks and wind bottoms out
    just before dawn, so a 3am sample reads as more pressured than the night
    even when the forecast is perfect.

    Reports agreement per SIGNAL, not numeric error, because the thresholds are
    step functions: a forecast RH of 89.5 against an observed 90.5 is a
    one-point miss that flips `humid` and moves the disease window by an hour.
    `forecast-low` means the forecast missed pressure that was really there —
    the direction that sizes the window too generously.
    """
    signals = {}
    mismatches = []
    for name in PRESSURE_SIGNAL_NAMES:
        predicted = bool(forecast.get(name))
        actual = bool(observed.get(name))
        if predicted == actual:
            signals[name] = "agree"
        elif actual:
            signals[name] = "forecast-low"
        else:
            signals[name] = "forecast-high"
        if predicted != actual:
            mismatches.append(name)
    return {
        "signals": signals,
        "mismatches": mismatches,
        "forecast_count": forecast.get("count"),
        "observed_count": observed.get("count"),
    }
