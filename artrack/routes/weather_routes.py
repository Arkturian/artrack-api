"""
Weather endpoint for the Tscheppa voice guide (and any other consumer).

GET /weather?lat=&lon= — current conditions from open-meteo (keyless, free),
translated into a German condition + emoji + one ready-to-inject `text` line,
analogous to guide-api /progress/summary. Public (no auth): weather is not
sensitive and the voice guide fetches it at session start.

Server-side cache: 10 min TTL keyed by rounded coordinates (~100 m grid) —
weather changes slowly and this keeps us polite towards open-meteo. On an
upstream error we serve the stale cache entry (better a 20-min-old reading
than none mid-hike).
"""
import time
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)
router = APIRouter()

# Tscheppaschlucht center — default when the caller sends no coordinates.
DEFAULT_LAT = 46.49778
DEFAULT_LON = 14.26374
DEFAULT_LOCATION = "Tscheppaschlucht"

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_CACHE_TTL = 600  # 10 min
_CACHE: dict = {}  # (lat3, lon3) -> (fetched_epoch, payload_dict)

# WMO weather interpretation codes -> (German condition, emoji)
_WMO: dict = {
    0: ("klar", "☀️"),
    1: ("überwiegend klar", "🌤️"),
    2: ("teils bewölkt", "⛅"),
    3: ("bewölkt", "☁️"),
    45: ("Nebel", "🌫️"),
    48: ("Nebel mit Reifbildung", "🌫️"),
    51: ("leichter Nieselregen", "🌦️"),
    53: ("Nieselregen", "🌦️"),
    55: ("starker Nieselregen", "🌧️"),
    56: ("gefrierender Nieselregen", "🌧️"),
    57: ("gefrierender Nieselregen", "🌧️"),
    61: ("leichter Regen", "🌦️"),
    63: ("Regen", "🌧️"),
    65: ("starker Regen", "🌧️"),
    66: ("gefrierender Regen", "🌧️"),
    67: ("gefrierender Regen", "🌧️"),
    71: ("leichter Schneefall", "🌨️"),
    73: ("Schneefall", "❄️"),
    75: ("starker Schneefall", "❄️"),
    77: ("Schneegriesel", "❄️"),
    80: ("leichte Regenschauer", "🌦️"),
    81: ("Regenschauer", "🌦️"),
    82: ("heftige Regenschauer", "🌧️"),
    85: ("Schneeschauer", "🌨️"),
    86: ("starke Schneeschauer", "🌨️"),
    95: ("Gewitter", "⛈️"),
    96: ("Gewitter mit Hagel", "⛈️"),
    99: ("Gewitter mit starkem Hagel", "⛈️"),
}


def _wind_text(kmh: float) -> str:
    if kmh < 5:
        return "kaum Wind"
    if kmh < 15:
        return "leichter Wind"
    if kmh < 30:
        return "mäßiger Wind"
    if kmh < 50:
        return "starker Wind"
    return "stürmischer Wind"


def _precip_text(mm: float) -> str:
    if mm <= 0:
        return "kein Niederschlag"
    if mm < 0.5:
        return "kaum Niederschlag"
    return f"{mm:g} mm Niederschlag"


def _build_payload(lat: float, lon: float, location: str, cur: dict) -> dict:
    code = int(cur.get("weather_code", -1))
    condition, emoji = _WMO.get(code, ("unbekannt", "🌡️"))
    temp = cur.get("temperature_2m")
    precip = cur.get("precipitation", 0.0) or 0.0
    wind = cur.get("wind_speed_10m", 0.0) or 0.0
    text = (
        f"Aktuell in der {location} {emoji} {condition}, "
        f"rund {round(temp)} °C, {_precip_text(precip)}, {_wind_text(wind)}."
        if temp is not None
        else f"Aktuell in der {location} {emoji} {condition}."
    )
    return {
        "location": location,
        "lat": lat,
        "lon": lon,
        "observed_at": cur.get("time"),
        "temperature_c": temp,
        "precipitation_mm": precip,
        "wind_kmh": wind,
        "weather_code": code,
        "condition": condition,
        "emoji": emoji,
        "text": text,
    }


_WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def _forecast_day_text(day_label: str, weekday: str, condition: str, tmin, tmax, precip_mm, precip_prob) -> str:
    """One ready-to-speak sentence per forecast day (the voice guide reads it verbatim)."""
    temp_part = f"{round(tmin)} bis {round(tmax)} Grad" if tmin is not None and tmax is not None else ""
    if (precip_mm or 0) <= 0 and (precip_prob or 0) < 20:
        rain_part = "kein Regen"
    elif (precip_mm or 0) <= 0:
        rain_part = f"{precip_prob} Prozent Regenwahrscheinlichkeit"
    else:
        rain_part = f"{precip_mm:g} mm Regen ({precip_prob} Prozent Wahrscheinlichkeit)"
    lead = f"{day_label}, {weekday}" if day_label else weekday
    parts = [p for p in (condition, temp_part, rain_part) if p]
    return f"{lead}: {', '.join(parts)}."


def _build_forecast(daily: dict) -> list:
    """Map open-meteo daily arrays into per-day dicts with German text."""
    from datetime import date as _date
    out = []
    dates = daily.get("time") or []
    for i, ds in enumerate(dates):
        try:
            d = _date.fromisoformat(ds)
        except (TypeError, ValueError):
            continue
        offset = (d - _date.today()).days
        if offset < 1:
            continue  # today is covered by the current-conditions block
        day_label = "Morgen" if offset == 1 else ("Übermorgen" if offset == 2 else "")
        weekday = _WEEKDAYS_DE[d.weekday()]
        code = int((daily.get("weather_code") or [None] * len(dates))[i] or -1)
        condition, emoji = _WMO.get(code, ("unbekannt", "🌡️"))
        tmin = (daily.get("temperature_2m_min") or [None] * len(dates))[i]
        tmax = (daily.get("temperature_2m_max") or [None] * len(dates))[i]
        precip = (daily.get("precipitation_sum") or [0] * len(dates))[i] or 0.0
        prob = (daily.get("precipitation_probability_max") or [0] * len(dates))[i] or 0
        out.append({
            "date": ds,
            "weekday": weekday,
            "temp_min_c": tmin,
            "temp_max_c": tmax,
            "precipitation_mm": precip,
            "precipitation_probability": prob,
            "weather_code": code,
            "condition": condition,
            "emoji": emoji,
            "text": _forecast_day_text(day_label, weekday, condition, tmin, tmax, precip, prob),
        })
    return out


@router.get("")
async def get_weather(
    lat: float = Query(DEFAULT_LAT, description="Latitude (default: Tscheppaschlucht center)"),
    lon: float = Query(DEFAULT_LON, description="Longitude (default: Tscheppaschlucht center)"),
    location: Optional[str] = Query(
        None,
        description="Display name used in the `text` line; defaults to 'Tscheppaschlucht' "
        "for the default coordinates, else 'Umgebung'",
    ),
    forecast_days: int = Query(
        0, ge=0, le=7,
        description="Additionally return `forecast[]` for the next N days (starting tomorrow). "
        "0 (default) keeps the response byte-identical to before — current conditions only.",
    ),
):
    """Current weather with German condition text + ready-to-inject `text` line.

    With ?forecast_days=N the response additionally carries `forecast[]` — one
    entry per upcoming day (starting tomorrow) with min/max temperature,
    precipitation, condition and a ready-to-speak German `text` sentence
    (\"Soll ich morgen kommen?\" — the voice guide reads it verbatim)."""
    if location is None:
        is_default = abs(lat - DEFAULT_LAT) < 0.01 and abs(lon - DEFAULT_LON) < 0.01
        location = DEFAULT_LOCATION if is_default else "Umgebung"

    key = (round(lat, 3), round(lon, 3), forecast_days)
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,precipitation,weather_code,wind_speed_10m",
        "timezone": "Europe/Vienna",
    }
    if forecast_days > 0:
        # +1: open-meteo counts today as day 1, our forecast[] starts tomorrow
        params["daily"] = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max"
        params["forecast_days"] = forecast_days + 1

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            cur = data.get("current") or {}
    except Exception as e:
        if cached:
            # Stale beats nothing mid-hike; the payload still carries observed_at.
            logger.warning(f"open-meteo failed ({e}), serving stale cache for {key}")
            return cached[1]
        raise HTTPException(status_code=502, detail=f"Weather source unreachable: {e}")

    payload = _build_payload(lat, lon, location, cur)
    if forecast_days > 0:
        payload["forecast"] = _build_forecast(data.get("daily") or {})
    _CACHE[key] = (now, payload)
    if len(_CACHE) > 256:  # bound the cache (grid keys are effectively finite anyway)
        _CACHE.pop(next(iter(_CACHE)))
    return payload
