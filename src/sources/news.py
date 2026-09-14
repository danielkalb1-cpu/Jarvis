"""Nachrichten: RSS einsammeln, von Claude gewichten lassen.

Urheberrecht: aus den Feeds werden ausschließlich Überschrift, Kurzbeschreibung,
Zeitstempel und Link verwendet. Es werden keine Artikelseiten abgerufen und keine
Absätze übernommen. Die Zusammenfassung im Briefing formuliert das Modell in
eigenen Worten; verlinkt wird immer auf das Original.
"""

from __future__ import annotations

import html
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser

from .net import FetchError, HttpConfig, Problems, fetch

ENV_KEY = "ANTHROPIC_API_KEY"
MAX_HEADLINES_FOR_MODEL = 220
SUMMARY_INPUT_CHARS = 300      # Kurzbeschreibung aus dem Feed, gekürzt
TAG_RE = re.compile(r"<[^>]+>")


# ----------------------------------------------------------------------
# 1. Feeds einsammeln
# ----------------------------------------------------------------------

def collect(config: dict[str, Any], http: HttpConfig, problems: Problems,
            now: datetime) -> dict[str, Any]:
    section = config.get("news") or {}
    if not section.get("enabled", True):
        return {"enabled": False, "top": [], "region": [], "world": [], "note": None}

    feeds = section.get("feeds") or []
    max_age = timedelta(hours=float(section.get("max_age_hours", 24)))
    per_feed = int(section.get("max_items_per_feed", 12))

    items: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(
            lambda feed: _read_feed(feed, http, problems, now, max_age, per_feed),
            feeds,
        )
        for chunk in results:
            items.extend(chunk)

    items = _dedupe(items)
    items.sort(key=lambda i: i["published"] or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)

    counts = section.get("counts") or {}
    wanted = {
        "top": int(counts.get("top", 5)),
        "region": int(counts.get("region", 4)),
        "world": int(counts.get("world", 4)),
    }

    if not items:
        return {
            "enabled": True, "top": [], "region": [], "world": [],
            "note": "Keine Meldungen eingesammelt – alle Feeds waren nicht erreichbar.",
            "feed_count": len(feeds), "item_count": 0, "ranked_by": "keine",
        }

    llm = section.get("llm") or {}
    selection, ranked_by, note = _rank(items, wanted, llm, problems)

    return {
        "enabled": True,
        "top": selection["top"],
        "region": selection["region"],
        "world": selection["world"],
        "note": note,
        "feed_count": len(feeds),
        "item_count": len(items),
        "ranked_by": ranked_by,
    }


def _read_feed(feed: dict[str, Any], http: HttpConfig, problems: Problems,
               now: datetime, max_age: timedelta, limit: int) -> list[dict[str, Any]]:
    name = feed.get("name") or feed.get("url", "?")
    url = feed.get("url")
    scope = feed.get("scope", "national")
    if not url:
        return []

    try:
        response = fetch(url, http, headers={"Accept": "application/rss+xml, application/xml, text/xml, */*"})
    except FetchError as exc:
        problems.add(f"Feed {name}", str(exc))
        return []

    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        problems.add(f"Feed {name}", "Antwort war kein lesbarer Feed")
        return []

    out: list[dict[str, Any]] = []
    for entry in parsed.entries[: limit * 2]:
        title = _clean(getattr(entry, "title", ""))
        link = (getattr(entry, "link", "") or "").strip()
        if not title or not link:
            continue

        published = _entry_time(entry)
        if published and now - published > max_age:
            continue

        out.append({
            "title": title,
            # Nur die Kurzbeschreibung aus dem Feed, zusätzlich gekappt.
            "teaser": _clean(getattr(entry, "summary", ""))[:SUMMARY_INPUT_CHARS],
            "link": link,
            "source": name,
            "scope": scope,
            "published": published,
        })
        if len(out) >= limit:
            break
    return out


def _entry_time(entry: Any) -> datetime | None:
    import calendar

    for field in ("published_parsed", "updated_parsed"):
        value = getattr(entry, field, None)
        if value:
            try:
                return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", text or ""))).strip()


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_links: set[str] = set()
    seen_titles: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        link = item["link"].split("?")[0].rstrip("/")
        title_key = re.sub(r"[^a-z0-9äöüß ]", "", item["title"].lower())[:90]
        if link in seen_links or title_key in seen_titles:
            continue
        seen_links.add(link)
        seen_titles.add(title_key)
        out.append(item)
    return out


# ----------------------------------------------------------------------
# 2. Gewichtung – erst Claude, sonst Aktualität
# ----------------------------------------------------------------------

def _rank(items: list[dict[str, Any]], wanted: dict[str, int], llm: dict[str, Any],
          problems: Problems) -> tuple[dict[str, list[dict[str, Any]]], str, str | None]:
    if llm.get("enabled", True):
        api_key = (os.environ.get(ENV_KEY) or "").strip()
        if not api_key:
            return (_fallback(items, wanted, llm),
                    "Aktualität",
                    f"Kein {ENV_KEY} gesetzt – Meldungen nach Aktualität sortiert.")
        try:
            raw = _ask_claude(items, wanted, llm, api_key)
            selection = _apply(raw, items, wanted)
            if selection is not None:
                return selection, "Claude", None
            problems.add("Nachrichten-Gewichtung", "Antwort war kein verwertbares JSON")
            return (_fallback(items, wanted, llm), "Aktualität",
                    "Gewichtung lieferte kein gültiges JSON – nach Aktualität sortiert.")
        except Exception as exc:  # noqa: BLE001 – Build darf nie kippen
            problems.add("Nachrichten-Gewichtung", _short(exc))
            return (_fallback(items, wanted, llm), "Aktualität",
                    f"Gewichtung nicht verfügbar ({_short(exc)}) – nach Aktualität sortiert.")

    return _fallback(items, wanted, llm), "Aktualität", None


def _ask_claude(items: list[dict[str, Any]], wanted: dict[str, int],
                llm: dict[str, Any], api_key: str) -> str:
    import anthropic

    region_terms = llm.get("region_terms") or ["Augsburg", "Allgäu"]
    payload = [
        {
            "id": index,
            "titel": item["title"],
            "teaser": item["teaser"][:200],
            "quelle": item["source"],
            "bereich": item["scope"],
            "zeit": item["published"].isoformat() if item["published"] else None,
        }
        for index, item in enumerate(items[:MAX_HEADLINES_FOR_MODEL])
    ]

    system = (
        "Du bist Redakteur für ein persönliches Morgen-Briefing. Du bekommst RSS-Überschriften "
        "vieler Redaktionen und wählst daraus die wichtigsten aus.\n\n"
        "Gewichte nach:\n"
        "1. Bedeutung der Meldung.\n"
        "2. Mehrfachberichterstattung: berichten mehrere unabhängige Häuser über dieselbe Sache, "
        "ist sie wichtiger. Nimm die Sache dann nur EINMAL auf.\n"
        f"3. Regionalbezug zu: {', '.join(region_terms)}.\n\n"
        "Schreibe zu jeder ausgewählten Meldung eine eigene Zusammenfassung in ein bis zwei "
        "vollständigen deutschen Sätzen. Formuliere in eigenen Worten und übernimm keine "
        "Formulierungen aus Titel oder Teaser wörtlich. Erfinde nichts dazu, was nicht in "
        "Titel oder Teaser steht.\n\n"
        "Antworte ausschließlich mit JSON in genau dieser Form, ohne weiteren Text:\n"
        '{"top":[{"id":0,"summary":"...","also":["Quelle A","Quelle B"]}],'
        '"region":[...],"world":[...]}\n\n'
        f'"top" = {wanted["top"]} wichtigste Meldungen insgesamt, '
        f'"region" = {wanted["region"]} Meldungen mit Bezug zur Region, '
        f'"world" = {wanted["world"]} internationale Meldungen. '
        '"also" listet weitere Häuser, die dieselbe Sache melden (leer lassen, wenn keine). '
        "Keine ID doppelt über alle drei Blöcke hinweg."
    )

    client = anthropic.Anthropic(api_key=api_key, timeout=120.0, max_retries=2)
    request: dict[str, Any] = {
        "model": llm.get("model", "claude-sonnet-4-6"),
        "max_tokens": int(llm.get("max_tokens", 8000)),
        "system": system,
        "messages": [{
            "role": "user",
            "content": "Hier die eingesammelten Überschriften:\n"
                       + json.dumps(payload, ensure_ascii=False),
        }],
    }

    try:
        response = client.messages.create(
            **request,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
        )
    except Exception:
        # Ältere SDK- oder Modellstände kennen diese Parameter nicht – dann eben ohne.
        response = client.messages.create(**request)

    return "".join(block.text for block in response.content
                   if getattr(block, "type", None) == "text")


def _apply(raw: str, items: list[dict[str, Any]],
           wanted: dict[str, int]) -> dict[str, list[dict[str, Any]]] | None:
    data = _loose_json(raw)
    if not isinstance(data, dict):
        return None

    used: set[int] = set()
    selection: dict[str, list[dict[str, Any]]] = {"top": [], "region": [], "world": []}

    for block, limit in (("top", wanted["top"]), ("region", wanted["region"]),
                         ("world", wanted["world"])):
        entries = data.get(block)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if len(selection[block]) >= limit:
                break
            if not isinstance(entry, dict):
                continue
            try:
                index = int(entry.get("id"))
            except (TypeError, ValueError):
                continue
            if index in used or not 0 <= index < len(items):
                continue
            used.add(index)
            also = entry.get("also")
            selection[block].append({
                **items[index],
                "summary": str(entry.get("summary") or "").strip()[:400],
                "also": [str(a) for a in also][:6] if isinstance(also, list) else [],
            })

    if not any(selection.values()):
        return None
    return selection


def _loose_json(raw: str) -> Any:
    """Tolerantes Parsen: Markdown-Fences abstreifen, notfalls das erste {...} nehmen."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _fallback(items: list[dict[str, Any]], wanted: dict[str, int],
              llm: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Simple Sortierung nach Aktualität, wenn die Gewichtung ausfällt.

    Ohne Modell gibt es keine eigene Zusammenfassung – dann steht nur die
    Überschrift mit Quelle und Link da, was urheberrechtlich unbedenklich ist.
    """
    terms = [t.lower() for t in (llm.get("region_terms") or [])]

    def is_regional(item: dict[str, Any]) -> bool:
        if item["scope"] == "regional":
            return True
        haystack = f"{item['title']} {item['teaser']}".lower()
        return any(term in haystack for term in terms)

    used: set[str] = set()
    selection: dict[str, list[dict[str, Any]]] = {"top": [], "region": [], "world": []}

    def take(block: str, pool: list[dict[str, Any]], limit: int) -> None:
        for item in pool:
            if len(selection[block]) >= limit:
                return
            if item["link"] in used:
                continue
            used.add(item["link"])
            selection[block].append({**item, "summary": "", "also": []})

    take("region", [i for i in items if is_regional(i)], wanted["region"])
    take("world", [i for i in items if i["scope"] == "world"], wanted["world"])
    take("top", items, wanted["top"])
    # Reihenfolge angleichen: "top" steht oben, auch wenn es zuletzt gefüllt wurde.
    return {"top": selection["top"], "region": selection["region"], "world": selection["world"]}


def _short(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text.splitlines()[0][:120]
