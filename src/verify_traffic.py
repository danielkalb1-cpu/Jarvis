#!/usr/bin/env python3
"""Prüft den TomTom-Key und die Routen aus der config.yaml.

    export TOMTOM_API_KEY="…"
    python3 src/verify_traffic.py

Sagt für jede Route, ob sie abrufbar ist und was zurückkommt. Exit-Code 1,
wenn etwas nicht stimmt – damit ein CI-Lauf den Ausfall sichtbar macht.

Der Key selbst wird nie ausgegeben, nur seine Länge.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources.net import HttpConfig, Problems  # noqa: E402
from sources import traffic as traffic_source  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    http = HttpConfig.from_dict(config.get("http"))

    key = (os.environ.get(traffic_source.ENV_KEY) or "").strip()
    if not key:
        print(f"{traffic_source.ENV_KEY} ist nicht gesetzt.\n")
        print("Lokal testen:")
        print(f'  export {traffic_source.ENV_KEY}="dein-key"')
        print("  python3 src/verify_traffic.py\n")
        print("Für die Action: Repo → Settings → Secrets and variables →")
        print(f"Actions → New repository secret → Name {traffic_source.ENV_KEY}.")
        return 1

    print(f"{traffic_source.ENV_KEY} gefunden ({len(key)} Zeichen).\n")

    problems = Problems()
    result = traffic_source.collect(config, http, problems)
    routes = result.get("routes") or []
    if not routes:
        print("Keine Routen in der config.yaml unter traffic.routes.")
        return 1

    broken = 0
    for route in routes:
        print(f"  {route['name']}")
        if route.get("error"):
            print(f"    FEHL  {route['error']}")
            broken += 1
            continue
        print(f"    OK    {route['live_minutes']} min mit Verkehr, "
              f"{route['free_flow_minutes']} min ohne, "
              f"Verzögerung {route['delay_minutes']} min, "
              f"{route['distance_km']} km")

    print()
    if broken:
        print(f"{broken} von {len(routes)} Routen nicht abrufbar.")
        return 1

    print(f"Alle {len(routes)} Routen in Ordnung – der Key funktioniert.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
