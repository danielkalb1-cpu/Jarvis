"""Kleiner Datei-Cache für den Nachrichtenblock.

Hintergrund: der Workflow läuft tagsüber halbstündlich, vor allem wegen des
Verkehrs. Die Feeds und die Gewichtung durch Claude jedes Mal neu zu holen
wäre teuer und bringt kaum etwas. Deshalb wird der fertige Nachrichtenblock
zwischengespeichert und nur alle `min_interval_minutes` erneuert.

Nebeneffekt: fällt die Gewichtung einmal aus, steht immer noch der letzte
brauchbare Stand auf der Seite statt gar nichts.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def load(path: Path, max_age: timedelta, now: datetime) -> dict[str, Any] | None:
    """Gecachten Block zurückgeben, wenn er noch frisch genug ist."""
    if max_age <= timedelta(0) or not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        built = datetime.fromisoformat(raw["built_at"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        log.warning("Cache nicht lesbar (%s) – wird neu geholt.", exc)
        return None

    if now - built > max_age:
        return None

    block = _revive(raw["block"])
    block["cached_at"] = built
    return block


def save(path: Path, block: dict[str, Any], now: datetime) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"built_at": now.isoformat(), "block": _flatten(block)}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    except OSError as exc:
        log.warning("Cache konnte nicht geschrieben werden: %s", exc)


def _flatten(block: dict[str, Any]) -> dict[str, Any]:
    """datetime -> ISO-String, damit der Block als JSON ablegbar ist."""
    out = dict(block)
    for key in ("top", "region", "world"):
        out[key] = [
            {**story,
             "published": story["published"].isoformat() if story.get("published") else None}
            for story in block.get(key) or []
        ]
    out.pop("cached_at", None)
    return out


def _revive(block: dict[str, Any]) -> dict[str, Any]:
    out = dict(block)
    for key in ("top", "region", "world"):
        stories = []
        for story in block.get(key) or []:
            item = dict(story)
            value = item.get("published")
            item["published"] = _parse(value) if value else None
            stories.append(item)
        out[key] = stories
    return out


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
