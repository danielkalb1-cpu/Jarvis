"""Wetter über Bright Sky (DWD-Daten, kein API-Key nötig).

https://brightsky.dev/docs/ – wir nutzen zwei Endpunkte:
  /current_weather  aktuelle Messwerte
  /weather          stündliche Vorhersage (heute + morgen)
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from .net import FetchError, HttpConfig, Problems

BASE = "https://api.brightsky.dev"

# Bright-Sky-"icon" -> ID im SVG-Sprite (siehe src/template.html) + Klartext
ICONS: dict[str, tuple[str, str]] = {
    "clear-day": ("sun", "klar"),
    "clear-night": ("moon", "klar"),
    "partly-cloudy-day": ("partly-day", "teils bewölkt"),
    "partly-cloudy-night": ("partly-night", "teils bewölkt"),
    "cloudy": ("cloud", "bewölkt"),
    "fog": ("fog", "Nebel"),
    "wind": ("wind", "windig"),
    "rain": ("rain", "Regen"),
    "sleet": ("sleet", "Schneeregen"),
    "snow": ("snow", "Schnee"),
    "hail": ("hail", "Hagel"),
    "thunderstorm": ("thunder", "Gewitter"),
}
FALLBACK_ICON = "cloud"


def collect(config: dict[str, Any], http: HttpConfig, problems: Problems,
            now: datetime) -> dict[str, Any]:
    """Liefert für jeden Standort einen aufbereiteten Block plus Warnhinweise."""
    section = config.get("weather") or {}
    if not section.get("enabled", True):
        return {"enabled": False, "locations": [], "alerts": []}

    locations: list[dict[str, Any]] = []
    for place in section.get("locations", []):
        locations.append(_one_location(place, section, http, problems, now))

    return {
        "enabled": True,
        "locations": locations,
        "alerts": _alerts(locations, section.get("alerts") or {}),
    }


def _comma(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


def _one_location(place: dict[str, Any], section: dict[str, Any], http: HttpConfig,
                  problems: Problems, now: datetime) -> dict[str, Any]:
    name = place.get("name", "?")
    lat, lon = place.get("lat"), place.get("lon")
    block: dict[str, Any] = {
        "name": name,
        "current": None,
        "hourly": [],
        "today": None,
        "rest_of_day": None,
        "tomorrow": None,
        "error": None,
    }

    tz = str(now.tzinfo) if now.tzinfo else "Europe/Berlin"
    params_common = {"lat": lat, "lon": lon, "tz": tz}

    # --- aktuelle Werte -------------------------------------------------
    try:
        from .net import fetch_json

        data = fetch_json(f"{BASE}/current_weather", http, params=params_common)
        block["current"] = _current(data.get("weather") or {})
    except FetchError as exc:
        problems.add(f"Wetter {name}", str(exc))
        block["error"] = f"Aktuelle Werte nicht verfügbar ({exc})."

    # --- Vorhersage heute + morgen --------------------------------------
    try:
        from .net import fetch_json

        today = now.date()
        data = fetch_json(
            f"{BASE}/weather",
            http,
            params={
                **params_common,
                "date": today.isoformat(),
                "last_date": (today + timedelta(days=1)).isoformat(),
            },
        )
        records = data.get("weather") or []
        parsed = [_record(r) for r in records]
        parsed = [r for r in parsed if r["time"] is not None]

        today_rows = [r for r in parsed if r["time"].date() == today]
        tomorrow_rows = [r for r in parsed if r["time"].date() == today + timedelta(days=1)]

        block["hourly"] = _course(
            today_rows,
            now,
            step=int(section.get("forecast_step_hours", 3)),
            until_hour=int(section.get("forecast_until_hour", 21)),
        )
        # Bright Sky liefert für manche Stationen keine aktuellen Wind- oder
        # Bewölkungswerte. Dann die Stunde aus der Vorhersage nehmen, die
        # jetzt am nächsten liegt – sonst steht auf der Seite nur ein Strich,
        # und die gefühlte Temperatur wird ohne Wind zu warm gerechnet.
        _backfill_current(block.get("current"), parsed, now)

        block["today"] = _day_summary(today_rows)
        # Für die Hinweiszeile zählt nur, was noch kommt – nicht, was
        # heute schon gefallen ist.
        block["rest_of_day"] = _day_summary(
            [r for r in today_rows if r["time"] >= now])
        block["tomorrow"] = _day_summary(tomorrow_rows)
    except FetchError as exc:
        problems.add(f"Wetter {name} (Vorhersage)", str(exc))
        existing = block["error"]
        note = f"Vorhersage nicht verfügbar ({exc})."
        block["error"] = f"{existing} {note}".strip() if existing else note

    return block


def _backfill_current(current: dict[str, Any] | None, rows: list[dict[str, Any]],
                      now: datetime) -> None:
    """Fehlende aktuelle Messwerte aus der nächstgelegenen Vorhersagestunde."""
    if not current or not rows:
        return

    nearest = min(rows, key=lambda r: abs(r["time"] - now))
    filled = False
    for field in ("wind_speed", "wind_gust_speed", "cloud_cover", "relative_humidity"):
        if current.get(field) is None and nearest.get(field) is not None:
            current[field] = nearest[field]
            filled = True

    if filled:
        # Die gefühlte Temperatur hängt am Wind – also neu rechnen.
        current["feels_like"] = apparent_temperature(
            current.get("temperature"),
            current.get("relative_humidity"),
            current.get("wind_speed"),
        )


def _record(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "time": _parse_time(raw.get("timestamp")),
        "temperature": raw.get("temperature"),
        "precipitation": raw.get("precipitation"),
        "precipitation_probability": raw.get("precipitation_probability"),
        "wind_speed": raw.get("wind_speed"),
        "wind_gust_speed": raw.get("wind_gust_speed"),
        "cloud_cover": raw.get("cloud_cover"),
        "relative_humidity": raw.get("relative_humidity"),
        "icon": raw.get("icon"),
        "condition": raw.get("condition"),
    }


def _current(raw: dict[str, Any]) -> dict[str, Any]:
    temp = raw.get("temperature")
    wind = raw.get("wind_speed")
    humidity = raw.get("relative_humidity")
    symbol, label = ICONS.get(raw.get("icon") or "", (FALLBACK_ICON, raw.get("condition") or "–"))
    return {
        "temperature": temp,
        "feels_like": apparent_temperature(temp, humidity, wind),
        "precipitation": raw.get("precipitation_60") or raw.get("precipitation_30")
        or raw.get("precipitation_10"),
        "wind_speed": wind,
        "wind_gust_speed": raw.get("wind_gust_speed"),
        "wind_direction": raw.get("wind_direction"),
        "cloud_cover": raw.get("cloud_cover"),
        "relative_humidity": humidity,
        "icon": symbol,
        "label": label,
        "time": _parse_time(raw.get("timestamp")),
    }


def _course(rows: list[dict[str, Any]], now: datetime, step: int,
            until_hour: int) -> list[dict[str, Any]]:
    """Tagesverlauf in N-Stunden-Schritten ab jetzt bis `until_hour`."""
    out: list[dict[str, Any]] = []
    for row in rows:
        hour = row["time"].hour
        if row["time"] < now.replace(minute=0, second=0, microsecond=0):
            continue
        if hour > until_hour:
            continue
        if hour % max(1, step) != 0:
            continue
        symbol, label = ICONS.get(row.get("icon") or "", (FALLBACK_ICON, row.get("condition") or "–"))
        out.append({
            "hour": f"{hour:02d}",
            "temperature": row["temperature"],
            "precipitation": row["precipitation"],
            "precipitation_probability": row["precipitation_probability"],
            "wind_speed": row["wind_speed"],
            "icon": symbol,
            "label": label,
        })
    return out


def _day_summary(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    temps = [r["temperature"] for r in rows if r["temperature"] is not None]
    probs = [r["precipitation_probability"] for r in rows
             if r["precipitation_probability"] is not None]
    rain = [r["precipitation"] for r in rows if r["precipitation"] is not None]
    gusts = [r["wind_gust_speed"] for r in rows if r["wind_gust_speed"] is not None]
    return {
        "min": min(temps) if temps else None,
        "max": max(temps) if temps else None,
        "precipitation_probability": max(probs) if probs else None,
        "precipitation_sum": round(sum(rain), 1) if rain else 0.0,
        "wind_gust_max": max(gusts) if gusts else None,
    }


def _alerts(locations: list[dict[str, Any]], thresholds: dict[str, Any]) -> list[dict[str, str]]:
    """Kurze Hinweissätze für ganz oben – nur wenn wirklich etwas ansteht."""
    prob_limit = float(thresholds.get("rain_probability_pct", 40))
    amount_limit = float(thresholds.get("rain_amount_mm", 0.5))
    frost_limit = float(thresholds.get("frost_temp_c", 0.0))
    ground_frost_limit = float(thresholds.get("ground_frost_temp_c", 3.0))

    alerts: list[dict[str, str]] = []
    for block in locations:
        name = block["name"]
        today = block.get("today") or {}
        ahead = block.get("rest_of_day") or {}

        prob = ahead.get("precipitation_probability")
        amount = ahead.get("precipitation_sum") or 0.0
        if (prob is not None and prob >= prob_limit) or amount >= amount_limit:
            # Nur die Angabe nennen, die den Hinweis ausgelöst hat – sonst
            # steht da „1 % Regenwahrscheinlichkeit“ neben 5 mm Regen.
            parts = []
            if prob is not None and prob >= prob_limit:
                parts.append(f"{int(prob)} % Wahrscheinlichkeit")
            if amount >= amount_limit:
                parts.append(f"{_comma(amount)} mm erwartet")
            detail = f" – {', '.join(parts)}" if parts else ""
            alerts.append({
                "kind": "rain",
                "icon": "rain",
                "text": f"{name}: Regen im weiteren Tagesverlauf{detail}. Schirm einpacken.",
            })

        low = today.get("min")
        if low is not None:
            if low <= frost_limit:
                alerts.append({
                    "kind": "frost",
                    "icon": "snow",
                    "text": f"{name}: Frost, Tiefstwert {low:.0f} °C. Scheiben werden kratzig.",
                })
            elif low <= ground_frost_limit:
                alerts.append({
                    "kind": "frost",
                    "icon": "snow",
                    "text": f"{name}: Bodenfrost möglich, Tiefstwert {low:.0f} °C.",
                })
    return alerts


def apparent_temperature(temp_c: float | None, humidity_pct: float | None,
                         wind_kmh: float | None) -> float | None:
    """Gefühlte Temperatur nach der Apparent-Temperature-Formel (BOM).

    Bright Sky liefert keinen eigenen Wert dafür, also rechnen wir ihn aus
    Temperatur, Luftfeuchte und Wind selbst aus.
    """
    if temp_c is None:
        return None
    humidity = 50.0 if humidity_pct is None else float(humidity_pct)
    wind_ms = 0.0 if wind_kmh is None else float(wind_kmh) / 3.6
    vapour = (humidity / 100.0) * 6.105 * math.exp(17.27 * temp_c / (237.7 + temp_c))
    return round(temp_c + 0.33 * vapour - 0.70 * wind_ms - 4.00, 1)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
