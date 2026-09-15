"""Nachrichten: RSS einsammeln, von Claude gewichten lassen.

Urheberrecht: aus den Feeds werden ausschließlich Überschrift, Kurzbeschreibung,
Zeitstempel und Link verwendet. Es werden keine Artikelseiten abgerufen und keine
Absätze übernommen. Die Zusammenfassung im Briefing formuliert das Modell in
eigenen Worten; verlinkt wird immer auf das Original.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser

from .net import FetchError, HttpConfig, Problems, fetch

ENV_KEY = "ANTHROPIC_API_KEY"

# Reihenfolge der Blöcke im Briefing. "agrar" ist zweigeteilt: ein Teil
# deutschsprachig, ein Teil international.
BLOCKS = ("top", "region", "agrar", "eu", "world")

# Blöcke, in denen nur Meldungen stehen, die heute zum ersten Mal
# auftauchen. Regulatorik bewegt sich langsam – ohne das stünden dort
# wochenlang dieselben Einträge.
ONLY_NEW = ("eu",)
# Der Modellaufruf passiert nur einmal am Tag – dann darf er auch das ganze
# Material sehen. 400 Überschriften sind rund 17.000 Token Eingabe, also
# wenige Cent.
MAX_HEADLINES_FOR_MODEL = 400

# Die Lage steht ganz oben und wird im Vorbeigehen gelesen. Mehr als das
# füllt den ganzen ersten Bildschirm.
LAGE_MAX_CHARS = 380
SUMMARY_INPUT_CHARS = 300      # Kurzbeschreibung aus dem Feed, gekürzt
TAG_RE = re.compile(r"<[^>]+>")

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 1. Feeds einsammeln
# ----------------------------------------------------------------------

def collect(config: dict[str, Any], http: HttpConfig, problems: Problems,
            now: datetime, seen: dict[str, str] | None = None) -> dict[str, Any]:
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
    items = _relevant_for_scope(items, (section.get("llm") or {}))
    seen = seen or {}
    items = _only_new(items, seen, now)
    items.sort(key=lambda i: i["published"] or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)

    counts = section.get("counts") or {}
    agrar = counts.get("agrar") or {}
    agrar_split = {
        "deutsch": int(agrar.get("deutsch", 3)),
        "international": int(agrar.get("international", 2)),
    }
    wanted = {
        "top": int(counts.get("top", 5)),
        "region": int(counts.get("region", 4)),
        "world": int(counts.get("world", 4)),
        "agrar": agrar_split["deutsch"] + agrar_split["international"],
        "eu": int(counts.get("eu", 3)),
    }

    if not items:
        return {
            "enabled": True, **{b: [] for b in BLOCKS}, "lage": "", "seen_update": {},
            "note": "Keine Meldungen eingesammelt – alle Feeds waren nicht erreichbar.",
            "feed_count": len(feeds), "item_count": 0, "ranked_by": "keine",
        }

    llm = section.get("llm") or {}
    selection, ranked_by, note = _rank(items, wanted, agrar_split, llm, problems)

    return {
        "enabled": True,
        "lage": selection.get("lage", ""),
        "seen_update": _newly_shown(selection, seen, now),
        **{block: selection[block] for block in BLOCKS},
        "note": note,
        "feed_count": len(feeds),
        "item_count": len(items),
        "ranked_by": ranked_by,
    }


def _relevant_for_scope(items: list[dict[str, Any]],
                        llm: dict[str, Any]) -> list[dict[str, Any]]:
    """Regelwerks-Meldungen müssen nach Regelwerk klingen.

    Der Filter saß bisher nur in der Ersatzlogik. Im ersten Lauf mit Modell
    landeten deshalb wieder zwei Moldau-Meldungen im Block: aus dem richtigen
    Ressort, aber eben kein EU-Regelwerk. Jetzt greift er, bevor überhaupt
    jemand auswählt – Modell wie Ersatzlogik.
    """
    terms = [t.lower() for t in (llm.get("eu_terms") or [])]
    if not terms:
        return items

    kept = []
    for item in items:
        if item["scope"] != "eu":
            kept.append(item)
            continue
        haystack = f"{item['title']} {item['teaser']}".lower()
        if any(term in haystack for term in terms):
            kept.append(item)
    return kept


def _only_new(items: list[dict[str, Any]], seen: dict[str, str],
              now: datetime) -> list[dict[str, Any]]:
    """Aus ONLY_NEW-Ressorts nur behalten, was heute zum ersten Mal auftaucht."""
    today = now.date().isoformat()
    kept: list[dict[str, Any]] = []

    for item in items:
        if item["scope"] not in ONLY_NEW:
            kept.append(item)
            continue
        first = seen.get(item["link"])
        # noch nie dagewesen, oder heute zum ersten Mal -> zeigen
        if first is None or first == today:
            kept.append(item)

    return kept


def _newly_shown(selection: dict[str, list[dict[str, Any]]], seen: dict[str, str],
                 now: datetime) -> dict[str, str]:
    """Vermerken, was tatsächlich auf der Seite gelandet ist.

    Absichtlich nicht schon beim Einsammeln: eine Meldung, die die Auswahl
    aussortiert, darf nicht als gesehen gelten – sonst wäre sie für immer
    verbrannt, obwohl sie nie jemand zu Gesicht bekommen hat.
    """
    today = now.date().isoformat()
    return {
        story["link"]: today
        for block in ONLY_NEW
        for story in selection.get(block) or []
        if story["link"] not in seen
    }


def _read_feed(feed: dict[str, Any], http: HttpConfig, problems: Problems,
               now: datetime, max_age: timedelta, limit: int) -> list[dict[str, Any]]:
    name = feed.get("name") or feed.get("url", "?")
    url = feed.get("url")
    scope = feed.get("scope", "national")
    lang = feed.get("lang", "de")
    aggregator = bool(feed.get("aggregator"))
    # Fachpresse erscheint seltener als Tageszeitungen – daher optional eine
    # eigene Altersgrenze je Feed.
    if feed.get("max_age_hours"):
        max_age = timedelta(hours=float(feed["max_age_hours"]))
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
        # Nur http(s) übernehmen – die Links landen als href auf der Seite.
        if not title or not link.lower().startswith(("http://", "https://")):
            continue

        published = _entry_time(entry)
        if published and now - published > max_age:
            continue

        # Aggregator-Feeds (z. B. Google News) nennen im <source>-Tag das Haus,
        # von dem die Meldung stammt. Das ist die ehrlichere Quellenangabe als
        # der Name des Aggregators – und der Titel trägt den Namen dann doppelt.
        # Nur bei ausdrücklich als Aggregator markierten Feeds auswerten:
        # andere Redaktionen stellen dort auch schon mal einen Bildnachweis hinein.
        publisher = _publisher(entry) if aggregator else None
        if publisher:
            title = _strip_suffix(title, publisher)

        out.append({
            "title": title,
            # Nur die Kurzbeschreibung aus dem Feed, zusätzlich gekappt.
            "teaser": _clean(getattr(entry, "summary", ""))[:SUMMARY_INPUT_CHARS],
            "link": link,
            "source": publisher or name,
            "scope": scope,
            "lang": lang,
            "published": published,
        })
        if len(out) >= limit:
            break
    return out


def _publisher(entry: Any) -> str | None:
    source = getattr(entry, "source", None)
    if isinstance(source, dict):
        title = _clean(source.get("title") or "")
        return title[:40] or None
    return None


def _strip_suffix(title: str, publisher: str) -> str:
    """„Meldung - Augsburger Allgemeine“ -> „Meldung“."""
    for separator in (" - ", " – ", " | "):
        tail = f"{separator}{publisher}"
        if title.endswith(tail):
            return title[: -len(tail)].strip()
    return title


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
    """Tags raus, Entities auflösen, Leerraum normalisieren.

    Zweimal auflösen: manche Feeds kodieren doppelt (&amp;#x27;), sonst steht
    auf der Seite später „&#x27;“ statt eines Apostrophs. Ausgegeben wird
    ohnehin wieder escaped.
    """
    cleaned = TAG_RE.sub(" ", text or "")
    for _ in range(2):
        unescaped = html.unescape(cleaned)
        if unescaped == cleaned:
            break
        cleaned = unescaped
    return re.sub(r"\s+", " ", cleaned).strip()


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

def _shortlist(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Auswahl fürs Modell, ohne kleine Ressorts abzuschneiden.

    Stumpf die neuesten N zu nehmen würde Landwirtschaft und Region
    verdrängen, weil die großen Häuser viel mehr Meldungen liefern. Daher
    erst aus jedem Ressort eine Grundmenge, dann nach Aktualität auffüllen.
    """
    if len(items) <= limit:
        return items

    scopes: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        scopes.setdefault(item["scope"], []).append(item)

    share = max(1, limit // max(1, len(scopes)))
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pool in scopes.values():
        for item in pool[:share]:
            seen.add(item["link"])
            picked.append(item)

    for item in items:
        if len(picked) >= limit:
            break
        if item["link"] not in seen:
            picked.append(item)

    picked.sort(key=lambda i: i["published"] or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True)
    return picked[:limit]


def _rank(items: list[dict[str, Any]], wanted: dict[str, int],
          agrar_split: dict[str, int], llm: dict[str, Any],
          problems: Problems) -> tuple[dict[str, list[dict[str, Any]]], str, str | None]:
    if llm.get("enabled", True):
        api_key = (os.environ.get(ENV_KEY) or "").strip()
        if not api_key:
            return (_fallback(items, wanted, agrar_split, llm),
                    "Aktualität",
                    f"Kein {ENV_KEY} gesetzt – Meldungen nach Aktualität sortiert.")
        try:
            raw = _ask_claude(items, wanted, agrar_split, llm, api_key)
            selection = _apply(raw, items, wanted)
            if selection is not None:
                return selection, "Claude", None
            log.warning("Gewichtung: Antwort nicht als JSON lesbar (%d Zeichen). "
                        "Anfang: %.200s", len(raw), raw.replace("\n", " "))
            problems.add("Nachrichten-Gewichtung", "Antwort war kein verwertbares JSON")
            return (_fallback(items, wanted, agrar_split, llm), "Aktualität",
                    "Gewichtung lieferte kein gültiges JSON – nach Aktualität sortiert.")
        except Exception as exc:  # noqa: BLE001 – Build darf nie kippen
            problems.add("Nachrichten-Gewichtung", _short(exc))
            return (_fallback(items, wanted, agrar_split, llm), "Aktualität",
                    f"Gewichtung nicht verfügbar ({_short(exc)}) – nach Aktualität sortiert.")

    return _fallback(items, wanted, agrar_split, llm), "Aktualität", None


def _ask_claude(items: list[dict[str, Any]], wanted: dict[str, int],
                agrar_split: dict[str, int], llm: dict[str, Any],
                api_key: str) -> str:
    import anthropic

    region_terms = llm.get("region_terms") or ["Augsburg", "Allgäu"]
    payload = [
        {
            "id": index,
            "titel": item["title"],
            "teaser": item["teaser"][:200],
            "quelle": item["source"],
            "bereich": item["scope"],
            "sprache": item["lang"],
            "zeit": item["published"].isoformat() if item["published"] else None,
        }
        for index, item in enumerate(_shortlist(items, MAX_HEADLINES_FOR_MODEL))
    ]

    system = (
        "Du bist Redakteur für ein persönliches Morgen-Briefing. Es wird um "
        "Viertel vor sechs mit einem Auge überflogen, von jemandem im Allgäu, "
        "der werktags nach Augsburg pendelt und beruflich mit Landwirtschaft "
        "und Landmaschinen zu tun hat.\n\n"

        "Du bekommst alle Überschriften, die heute früh eingesammelt wurden. "
        "Geh sie vollständig durch, bevor du auswählst.\n\n"

        "So gewichtest du:\n"
        "1. Tragweite – was ändert sich für viele Menschen, und wie stark?\n"
        "2. Mehrfachberichterstattung: Melden mehrere unabhängige Häuser "
        "dieselbe Sache, ist sie wichtiger. Fasse solche Meldungen zu EINER "
        "zusammen und trage die übrigen Häuser in \"also\" nach.\n"
        f"3. Bezug zu ihm: {', '.join(region_terms)}, Landwirtschaft, "
        "Pendeln zwischen Allgäu und Augsburg.\n"
        "4. Aktualität – bei gleichem Gewicht das Neuere.\n\n"

        "Aussortieren: Sport-Ergebnisse, Promi- und Boulevardmeldungen, "
        "Ratgeber, Produkttests, reine Ankündigungen, Live-Ticker ohne neuen "
        "Stand. Lieber ein Block mit vier guten Meldungen als fünf mit einer "
        "schwachen.\n\n"

        "Zu jeder ausgewählten Meldung schreibst du ein bis zwei vollständige "
        "deutsche Sätze: was ist passiert, und was folgt daraus. In eigenen "
        "Worten – übernimm keine Formulierung aus Titel oder Teaser wörtlich. "
        "Erfinde nichts, was nicht in Titel oder Teaser steht; wenn der Teaser "
        "dünn ist, schreib lieber weniger. Keine Floskeln wie \"es bleibt "
        "abzuwarten\".\n\n"

        "Zusätzlich \"lage\": zwei, höchstens drei kurze Sätze, die den Tag "
        "einordnen – was heute zählt, worauf er sich einstellen sollte. "
        "ZUSAMMEN HÖCHSTENS 350 ZEICHEN. Das wird um Viertel vor sechs mit "
        "einem Auge gelesen; lieber ein Gedanke zu wenig als ein Satz zu viel. "
        "Zähle nicht die Schlagzeilen auf, die ohnehin darunter stehen, "
        "sondern sag, was sie zusammen bedeuten. Ruhiger, sachlicher Ton, "
        "ohne Anrede und ohne Aufzählung. Wenn wenig los ist, sag genau das; "
        "erfundene Dringlichkeit ist schlimmer als ein ruhiger Tag.\n\n"

        "Antworte ausschließlich mit JSON in genau dieser Form, ohne weiteren "
        "Text:\n"
        '{"lage":"...","top":[{"id":0,"summary":"...","also":["Quelle A"]}],'
        '"region":[...],"agrar":[...],"eu":[...],"world":[...]}\n\n'
        f'"top" = {wanted["top"]} wichtigste Meldungen insgesamt, '
        f'"region" = {wanted["region"]} mit Bezug zur Region, '
        f'"agrar" = {wanted["agrar"]} aus der Landwirtschaft, davon '
        f'{agrar_split["deutsch"]} deutschsprachige (sprache "de") und '
        f'{agrar_split["international"]} internationale (sprache "en"), erst '
        "die deutschen. "
        f'"eu" = bis zu {wanted["eu"]} Meldungen aus dem Bereich "eu" zu neuem '
        "EU-Regelwerk mit Bezug zu Landwirtschaft, Landmaschinen oder "
        "Anbaugeräten. Nimm dort NUR Meldungen aus dem Bereich \"eu\" und "
        "lass den Block lieber leer, als ihn mit allgemeiner Agrarpolitik zu "
        "füllen. "
        f'"world" = {wanted["world"]} internationale Meldungen. '
        '"also" listet weitere Häuser, die dieselbe Sache melden (leer lassen, '
        "wenn keine). Keine ID doppelt über alle Blöcke hinweg."
    )

    # Großzügiges Zeitlimit: 400 Überschriften mit hohem Aufwand und
    # adaptivem Denken brauchen deutlich länger als eine kurze Anfrage.
    # Der Workflow selbst bricht nach 15 Minuten ab.
    client = anthropic.Anthropic(api_key=api_key, timeout=600.0, max_retries=2)
    request: dict[str, Any] = {
        "model": llm.get("model", "claude-sonnet-5"),
        "max_tokens": int(llm.get("max_tokens", 8000)),
        "system": system,
        "messages": [{
            "role": "user",
            "content": "Hier die eingesammelten Überschriften:\n"
                       + json.dumps(payload, ensure_ascii=False),
        }],
    }

    # Der Aufruf passiert einmal am Tag, also darf er gründlich sein:
    # adaptives Denken und hoher Aufwand. Kostet ein paar Cent mehr und
    # entscheidet über die Qualität des ganzen Briefings.
    #
    # Gestreamt, nicht als einzelne Antwort: bei dieser Menge Eingabe und
    # 16.000 möglichen Ausgabe-Token läuft eine gewöhnliche Anfrage sonst
    # ins Zeitlimit, noch bevor das Modell fertig ist.
    def ask(**extra: Any) -> Any:
        with client.messages.stream(**request, **extra) as stream:
            return stream.get_final_message()

    try:
        response = ask(thinking={"type": "adaptive"},
                       output_config={"effort": llm.get("effort", "high")})
    except Exception:
        # Ältere SDK- oder Modellstände kennen diese Parameter nicht – dann eben ohne.
        response = ask()

    stop = getattr(response, "stop_reason", None)
    usage = getattr(response, "usage", None)
    log.info("Gewichtung: stop_reason=%s, Eingabe %s / Ausgabe %s Token",
             stop, getattr(usage, "input_tokens", "?"),
             getattr(usage, "output_tokens", "?"))

    text = "".join(block.text for block in response.content
                   if getattr(block, "type", None) == "text")

    if stop == "max_tokens":
        # max_tokens deckt auch die Denk-Token ab. Reicht das Budget nicht,
        # bleibt von der eigentlichen Antwort nichts oder nur ein Bruchstück.
        raise RuntimeError(
            f"Antwort abgeschnitten – max_tokens ({request['max_tokens']}) "
            "war zu knapp für Denken und Ausgabe zusammen. In der config "
            "news.llm.max_tokens erhöhen oder effort senken.")
    if not text.strip():
        raise RuntimeError(f"Antwort enthielt keinen Text (stop_reason={stop}).")

    return text


def _apply(raw: str, items: list[dict[str, Any]],
           wanted: dict[str, int]) -> dict[str, Any] | None:
    data = _loose_json(raw)
    if not isinstance(data, dict):
        return None

    used: set[int] = set()
    selection: dict[str, list[dict[str, Any]]] = {block: [] for block in BLOCKS}

    for block in BLOCKS:
        # .get(): wer in der config eine Blockgröße herausnimmt, soll damit
        # den Block abschalten, nicht den Build zum Absturz bringen.
        limit = wanted.get(block, 0)
        if limit <= 0:
            continue
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

    if not any(selection.get(b) for b in BLOCKS):
        return None

    lage = re.sub(r"\s+", " ", TAG_RE.sub(" ", str(data.get("lage") or ""))).strip()
    selection["lage"] = _trim_sentences(lage, LAGE_MAX_CHARS)
    return selection


def _trim_sentences(text: str, limit: int) -> str:
    """Auf ganze Sätze kürzen statt mitten im Wort abzuschneiden.

    Das Modell hält sich meist an die Vorgabe; hält es sie nicht ein, soll
    trotzdem nichts Angefangenes auf der Seite stehen.
    """
    if len(text) <= limit:
        return text
    cut = max(text.rfind(m, 0, limit + 1) for m in (". ", "! ", "? "))
    if cut > limit // 2:
        return text[:cut + 1]
    return text[:limit].rsplit(" ", 1)[0] + " …"


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


MAX_PER_SOURCE_PER_BLOCK = 2


def _fallback(items: list[dict[str, Any]], wanted: dict[str, int],
              agrar_split: dict[str, int],
              llm: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Simple Sortierung nach Aktualität, wenn die Gewichtung ausfällt.

    Ohne Modell gibt es keine eigene Zusammenfassung – dann steht nur die
    Überschrift mit Quelle und Link da, was urheberrechtlich unbedenklich ist.
    """
    terms = [t.lower() for t in (llm.get("region_terms") or [])]

    def mentions_region(item: dict[str, Any]) -> bool:
        haystack = f"{item['title']} {item['teaser']}".lower()
        return any(term in haystack for term in terms)

    def is_regional(item: dict[str, Any]) -> bool:
        return item["scope"] == "regional" or mentions_region(item)

    used: set[str] = set()
    selection: dict[str, list[dict[str, Any]]] = {block: [] for block in BLOCKS}

    def take(block: str, pool: list[dict[str, Any]], limit: int) -> None:
        # Ohne Gewichtung sonst schnell viermal dasselbe Haus in einem Block.
        per_source: dict[str, int] = {}
        for allow_repeats in (False, True):
            for item in pool:
                if len(selection[block]) >= limit:
                    return
                if item["link"] in used:
                    continue
                source = item["source"]
                if not allow_repeats and per_source.get(source, 0) >= MAX_PER_SOURCE_PER_BLOCK:
                    continue
                per_source[source] = per_source.get(source, 0) + 1
                used.add(item["link"])
                selection[block].append({**item, "summary": "", "also": []})

    # Innerhalb der Region zuerst das, was Augsburg oder Allgäu namentlich nennt.
    regional = [i for i in items if is_regional(i)]
    regional.sort(key=mentions_region, reverse=True)

    agrar = [i for i in items if i["scope"] == "agrar"]

    take("region", regional, wanted.get("region", 0))
    # Erst die deutschsprachigen, dann die internationalen – die Reihenfolge
    # im Block bleibt dadurch dieselbe wie bei der Gewichtung durch Claude.
    take("agrar", [i for i in agrar if i["lang"] == "de"], agrar_split["deutsch"])
    take("agrar", [i for i in agrar if i["lang"] != "de"],
         agrar_split["deutsch"] + agrar_split["international"])
    # Falls eine der beiden Seiten zu wenig hergab, mit dem Rest auffüllen.
    take("agrar", agrar, wanted.get("agrar", 0))
    # Der Regelwerks-Filter greift schon beim Einsammeln (_relevant_for_scope).
    take("eu", [i for i in items if i["scope"] == "eu"], wanted.get("eu", 0))
    take("world", [i for i in items if i["scope"] == "world"], wanted.get("world", 0))
    take("top", items, wanted.get("top", 0))
    return {**{block: selection[block] for block in BLOCKS}, "lage": ""}


def _short(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text.splitlines()[0][:120]
