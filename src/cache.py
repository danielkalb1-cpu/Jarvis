"""Kleiner Datei-Cache für den Nachrichtenblock.

Hintergrund: der Workflow läuft tagsüber halbstündlich, vor allem wegen des
Verkehrs. Die Feeds und die Gewichtung durch Claude jedes Mal neu zu holen
wäre teuer und bringt kaum etwas. Deshalb wird der fertige Nachrichtenblock
zwischengespeichert und nur alle `min_interval_minutes` erneuert.

Nebeneffekt: fällt die Gewichtung einmal aus, steht immer noch der letzte
brauchbare Stand auf der Seite statt gar nichts.

Wird die Nachrichten-Konfiguration geändert (Feeds, Blockgrößen, Modell)
oder der Code, der die Meldungen einsammelt, verfällt der Cache sofort –
sonst würde eine Änderung bis zu drei Stunden lang nicht sichtbar.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sources.news import BLOCKS

log = logging.getLogger(__name__)


def fingerprint(news_config: dict[str, Any], *sources: Path) -> str:
    """Fingerabdruck aus Konfiguration und dem Code, der die Meldungen holt.

    Der Code gehört dazu, weil er die Form des Ergebnisses bestimmt: wird die
    Auswahl oder die Aufbereitung geändert, wäre der alte Stand sonst noch
    stundenlang zu sehen. Schlimmstenfalls wird einmal zu viel geholt.
    """
    digest = hashlib.sha256()
    digest.update(json.dumps(news_config or {}, sort_keys=True,
                             ensure_ascii=False, default=str).encode("utf-8"))
    for path in sources:
        try:
            digest.update(path.read_bytes())
        except OSError:
            pass
    return digest.hexdigest()[:16]


def load(path: Path, max_age: timedelta, now: datetime,
         expected: str | None = None) -> dict[str, Any] | None:
    """Gecachten Block zurückgeben, wenn er noch frisch und passend ist."""
    if max_age <= timedelta(0) or not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        built = datetime.fromisoformat(raw["built_at"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        log.warning("Cache nicht lesbar (%s) – wird neu geholt.", exc)
        return None

    if expected is not None and raw.get("config") != expected:
        log.info("Konfiguration hat sich geändert – Cache verworfen.")
        return None

    if now - built > max_age:
        return None

    block = _revive(raw["block"])
    block["cached_at"] = built
    return block


def save(path: Path, block: dict[str, Any], now: datetime,
         config: str | None = None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"built_at": now.isoformat(), "config": config,
                   "block": _flatten(block)}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    except OSError as exc:
        log.warning("Cache konnte nicht geschrieben werden: %s", exc)


def _flatten(block: dict[str, Any]) -> dict[str, Any]:
    """datetime -> ISO-String, damit der Block als JSON ablegbar ist."""
    out = dict(block)
    for key in BLOCKS:
        out[key] = [
            {**story,
             "published": story["published"].isoformat() if story.get("published") else None}
            for story in block.get(key) or []
        ]
    out.pop("cached_at", None)
    return out


def _revive(block: dict[str, Any]) -> dict[str, Any]:
    out = dict(block)
    for key in BLOCKS:
        stories = []
        for story in block.get(key) or []:
            item = dict(story)
            value = item.get("published")
            item["published"] = _parse(value) if value else None
            stories.append(item)
        out[key] = stories
    return out


# ----------------------------------------------------------------------
# Gesehene Meldungen – für Blöcke, die nur Neues zeigen sollen
# ----------------------------------------------------------------------

SEEN_KEEP_DAYS = 90


def load_seen(path: Path) -> dict[str, str]:
    """Link -> Datum, an dem er zum ersten Mal auftauchte."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        log.warning("Gesehenes nicht lesbar (%s) – wird neu aufgebaut.", exc)
        return {}


def save_seen(path: Path, seen: dict[str, str], now: datetime) -> None:
    """Ablegen und dabei alte Einträge ausmisten."""
    cutoff = (now - timedelta(days=SEEN_KEEP_DAYS)).date().isoformat()
    pruned = {link: day for link, day in seen.items() if day >= cutoff}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pruned, ensure_ascii=False, indent=1, sort_keys=True),
                        encoding="utf-8")
    except OSError as exc:
        log.warning("Gesehenes konnte nicht geschrieben werden: %s", exc)


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
