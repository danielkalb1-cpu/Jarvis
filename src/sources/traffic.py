"""Verkehr über die TomTom Routing API.

https://developer.tomtom.com/routing-api – wir fragen pro Route einmal
`calculateRoute` mit Live-Verkehr ab und vergleichen die Fahrzeit mit der
verkehrsfreien Referenz.

Ohne Secret TOMTOM_API_KEY wird der Block sauber übersprungen, nicht
abgestürzt.
"""

from __future__ import annotations

import os
from typing import Any

from .net import FetchError, HttpConfig, Problems, fetch_json

BASE = "https://api.tomtom.com/routing/1/calculateRoute"
ENV_KEY = "TOMTOM_API_KEY"


def collect(config: dict[str, Any], http: HttpConfig, problems: Problems) -> dict[str, Any]:
    section = config.get("traffic") or {}
    if not section.get("enabled", True):
        return {"enabled": False, "routes": [], "skipped": None}

    api_key = (os.environ.get(ENV_KEY) or "").strip()
    if not api_key:
        # Kein Key -> kein Absturz, nur ein Hinweis im Block.
        return {
            "enabled": True,
            "routes": [],
            "skipped": f"Kein {ENV_KEY} gesetzt – Verkehrsdaten übersprungen.",
            "warn_minutes": int(section.get("delay_warn_minutes", 10)),
        }

    routes = [_one_route(route, api_key, http, problems)
              for route in section.get("routes", [])]

    return {
        "enabled": True,
        "routes": routes,
        "skipped": None,
        "warn_minutes": int(section.get("delay_warn_minutes", 10)),
    }


def _one_route(route: dict[str, Any], api_key: str, http: HttpConfig,
               problems: Problems) -> dict[str, Any]:
    start, end = route.get("from") or {}, route.get("to") or {}
    name = route.get("name") or f"{start.get('name', '?')} → {end.get('name', '?')}"
    block: dict[str, Any] = {
        "name": name,
        "from": start.get("name"),
        "to": end.get("name"),
        "free_flow_minutes": None,
        "live_minutes": None,
        "delay_minutes": None,
        "distance_km": None,
        "error": None,
    }

    locations = f"{start.get('lat')},{start.get('lon')}:{end.get('lat')},{end.get('lon')}"
    try:
        data = fetch_json(
            f"{BASE}/{locations}/json",
            http,
            params={
                "key": api_key,
                "traffic": "true",
                "routeType": "fastest",
                "travelMode": "car",
                # liefert zusätzlich noTrafficTravelTimeInSeconds
                "computeTravelTimeFor": "all",
            },
        )
    except FetchError as exc:
        problems.add(f"Verkehr {name}", str(exc))
        block["error"] = f"Route nicht abrufbar ({exc})."
        return block

    routes = data.get("routes") or []
    if not routes:
        problems.add(f"Verkehr {name}", "keine Route in der Antwort")
        block["error"] = "Keine Route gefunden."
        return block

    summary = routes[0].get("summary") or {}
    live_s = summary.get("travelTimeInSeconds")
    free_s = summary.get("noTrafficTravelTimeInSeconds")
    delay_s = summary.get("trafficDelayInSeconds")
    metres = summary.get("lengthInMeters")

    # noTrafficTravelTime kann fehlen – dann aus Live-Zeit minus Verzögerung ableiten.
    if free_s is None and live_s is not None and delay_s is not None:
        free_s = live_s - delay_s
    if delay_s is None and live_s is not None and free_s is not None:
        delay_s = live_s - free_s

    block["live_minutes"] = _minutes(live_s)
    block["free_flow_minutes"] = _minutes(free_s)
    block["delay_minutes"] = max(0, _minutes(delay_s) or 0) if delay_s is not None else None
    block["distance_km"] = round(metres / 1000.0, 1) if metres is not None else None
    return block


def _minutes(seconds: Any) -> int | None:
    if seconds is None:
        return None
    try:
        return int(round(float(seconds) / 60.0))
    except (TypeError, ValueError):
        return None
