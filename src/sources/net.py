"""Gemeinsame HTTP-Helfer: Timeout, Retry, und niemals eine Exception nach außen.

Jeder Abruf im Briefing geht hier durch. Grundregel: eine kaputte Quelle darf
den Build nicht kippen – sie liefert stattdessen einen Fehlertext, der später
sichtbar, aber unaufdringlich im betroffenen Block landet.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger(__name__)


@dataclass
class HttpConfig:
    timeout_seconds: float = 10.0
    retries: int = 2
    backoff_seconds: float = 1.5
    user_agent: str = "jarvis-morning-briefing/1.0"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "HttpConfig":
        data = data or {}
        return cls(
            timeout_seconds=float(data.get("timeout_seconds", 10.0)),
            retries=int(data.get("retries", 2)),
            backoff_seconds=float(data.get("backoff_seconds", 1.5)),
            user_agent=str(data.get("user_agent", "jarvis-morning-briefing/1.0")),
        )


@dataclass
class Problems:
    """Sammelstelle für alles, was beim Lauf schiefgegangen ist."""

    items: list[tuple[str, str]] = field(default_factory=list)

    def add(self, source: str, message: str) -> None:
        log.warning("Quelle nicht erreichbar: %s – %s", source, message)
        self.items.append((source, message))

    def names(self) -> list[str]:
        seen: list[str] = []
        for source, _ in self.items:
            if source not in seen:
                seen.append(source)
        return seen

    def __bool__(self) -> bool:
        return bool(self.items)


class FetchError(Exception):
    """Abruf endgültig fehlgeschlagen (nach allen Versuchen)."""


def fetch(
    url: str,
    cfg: HttpConfig,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    """GET mit Timeout und Retry. Wirft FetchError, wenn alle Versuche scheitern."""
    attempts = max(1, cfg.retries)
    all_headers = {"User-Agent": cfg.user_agent, "Accept-Encoding": "gzip, deflate"}
    if headers:
        all_headers.update(headers)

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers=all_headers,
                timeout=cfg.timeout_seconds,
            )
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001 – bewusst breit, Build darf nie kippen
            last_error = exc
            if attempt < attempts:
                time.sleep(cfg.backoff_seconds * attempt)

    raise FetchError(_short(last_error)) from last_error


def fetch_json(url: str, cfg: HttpConfig, **kwargs: Any) -> Any:
    response = fetch(url, cfg, **kwargs)
    try:
        return response.json()
    except ValueError as exc:
        raise FetchError("Antwort war kein gültiges JSON") from exc


def _short(exc: Exception | None) -> str:
    """Fehlermeldung auf etwas kürzen, das in eine Fußzeile passt."""
    if exc is None:
        return "unbekannter Fehler"
    if isinstance(exc, requests.exceptions.Timeout):
        return "Zeitüberschreitung"
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS-Fehler"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "keine Verbindung"
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    text = str(exc).strip() or exc.__class__.__name__
    return text[:120]
