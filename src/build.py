#!/usr/bin/env python3
"""Baut das Morgen-Briefing zu einer fertigen statischen Seite.

Ablauf: Konfiguration laden -> Wetter, Verkehr, Nachrichten holen ->
src/template.html füllen -> docs/index.html schreiben.

Die erzeugte Seite enthält keine API-Aufrufe und kein nachladendes JavaScript.
Alle Daten stehen zum Build-Zeitpunkt schon im HTML – das Repo ist öffentlich,
und Keys dürfen nicht im Browser landen.

Beenden mit Code != 0 nur, wenn das Rendering selbst kaputt ist. Eine Quelle,
die ausfällt, hinterlässt eine Notiz im betroffenen Block.
"""

from __future__ import annotations

import argparse
import html as html_mod
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources import news as news_source          # noqa: E402
from sources import traffic as traffic_source    # noqa: E402
from sources import weather as weather_source    # noqa: E402
from sources.net import HttpConfig, Problems     # noqa: E402
import cache as news_cache                       # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "src" / "template.html"
OUTPUT = ROOT / "docs" / "index.html"
CONFIG = ROOT / "config.yaml"
NEWS_CACHE = ROOT / "cache" / "news.json"
NEWS_MODULE = ROOT / "src" / "sources" / "news.py"
SEEN_FILE = ROOT / "cache" / "seen.json"

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni",
          "Juli", "August", "September", "Oktober", "November", "Dezember"]

log = logging.getLogger("build")


# ======================================================================
# Hilfen
# ======================================================================

def esc(value: Any) -> str:
    return html_mod.escape(str(value if value is not None else ""), quote=True)


def num(value: Any, digits: int = 0, dash: str = "–") -> str:
    """Zahl deutsch formatieren; None wird zum Gedankenstrich."""
    if value is None:
        return dash
    try:
        text = f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return dash
    return text.replace(".", ",")


# ======================================================================
# Fragmente
# ======================================================================

def render_lage(text: str) -> str:
    """Die Einordnung des Tages – nur vorhanden, wenn Claude gewichtet hat."""
    if not (text or "").strip():
        return ""
    return (f'  <section class="lage" aria-label="Lage">\n'
            f'    <span class="lage__k">Lage</span>\n'
            f'    <p class="lage__t">{esc(text.strip())}</p>\n'
            f'  </section>\n')


def render_alerts(alerts: list[dict[str, str]]) -> str:
    if not alerts:
        return ""
    rows = "\n".join(
        f'    <div class="alert alert--{esc(a.get("kind", "info"))}">'
        f'<span class="alert__icon">{icon(a.get("icon"), "")}</span>'
        f'<span>{esc(a.get("text", ""))}</span></div>'
        for a in alerts
    )
    return f'  <div class="alerts">\n{rows}\n  </div>\n'


def render_weather(block: dict[str, Any], alerts_here: str = "") -> str:
    if not block.get("enabled", True):
        return note("Wetter ist in der config.yaml abgeschaltet.")

    locations = block.get("locations") or []
    if not locations:
        return note("Keine Standorte konfiguriert.")

    # Der erste Ort bekommt die volle Kachel, die weiteren eine Zeile. Zwei
    # gleich aufgebaute Kacheln kosteten je einen halben Bildschirm und
    # zeigten meist fast dieselben Zahlen.
    out = [alerts_here] if alerts_here else []
    out.append(_weather_panel(locations[0]))
    rest = [_weather_row(loc) for loc in locations[1:]]
    if rest:
        out.append('    <div class="panel wx__more">\n' + "\n".join(rest) + "\n    </div>")
    return "\n".join(out)


def _weather_row(location: dict[str, Any]) -> str:
    """Weiterer Standort als eine Zeile statt als zweite Vollkachel."""
    current = location.get("current") or {}
    today = location.get("today") or {}

    if location.get("error") and not current:
        return ('      <div class="wxrow"><span class="wxrow__n">'
                f'{esc(location["name"])}</span>'
                f'<span class="wxrow__e">nicht verfügbar</span></div>')

    facts = [f'{num(today.get("min"))}° / {num(today.get("max"))}°']
    prob = today.get("precipitation_probability")
    if prob is not None:
        facts.append(f'{num(prob)} % Regen')
    wind = current.get("wind_speed")
    if wind is not None:
        facts.append(f'{num(wind)} km/h')

    return f"""      <div class="wxrow">
        <span class="wxrow__n">{esc(location["name"])}</span>
        {icon(current.get("icon"), "wxrow__i")}
        <span class="wxrow__t">{num(current.get("temperature"), 1)}°</span>
        <span class="wxrow__f">{esc(" · ".join(facts))}</span>
      </div>"""


def _weather_panel(location: dict[str, Any]) -> str:
    current = location.get("current") or {}
    today = location.get("today") or {}
    tomorrow = location.get("tomorrow") or {}

    parts = ['    <div class="panel">']

    if current:
        parts.append(f"""      <div class="wx__top">
        <div>
          <div class="wx__place">{esc(location["name"])}</div>
          <div class="wx__temp">{num(current.get("temperature"), 1)}<sup>°C</sup></div>
          <div class="wx__feels">gefühlt {num(current.get("feels_like"), 1)} °C</div>
        </div>
        <div class="wx__cond">
          {icon(current.get("icon"), "wx__glyph")}
          <span class="wx__label">{esc(current.get("label", ""))}</span>
        </div>
      </div>""")
    else:
        parts.append(f'      <div class="wx__place">{esc(location["name"])}</div>')

    has_values = bool(current) or bool(today)
    metrics = [
        ("Regen", f'{num(today.get("precipitation_probability"))} <small>%</small>'),
        ("Menge", f'{num(today.get("precipitation_sum"), 1)} <small>mm</small>'),
        ("Wind", f'{num(current.get("wind_speed"))} <small>km/h</small>'),
        ("Wolken", f'{num(current.get("cloud_cover"))} <small>%</small>'),
    ]
    if has_values:
        cells = "".join(
            f'<div class="metric"><span class="metric__k">{esc(k)}</span>'
            f'<span class="metric__v">{v}</span></div>'
            for k, v in metrics
        )
        parts.append(f'      <div class="wx__grid">{cells}</div>')

    hours = location.get("hourly") or []
    if hours:
        cells = "".join(
            f'<div class="hour"><div class="hour__t">{esc(h["hour"])}</div>'
            f'{icon(h.get("icon"), "hour__i")}'
            f'<div class="hour__d">{num(h.get("temperature"))}°</div>'
            f'<div class="hour__p">'
            f'{(str(int(h["precipitation_probability"])) + "%") if h.get("precipitation_probability") else ""}'
            f'</div></div>'
            for h in hours
        )
        parts.append(f'      <div class="hours">{cells}</div>')

    days: list[str] = []
    if today:
        days.append(f'<span>Heute <b>{num(today.get("min"))}°</b> / <b>{num(today.get("max"))}°</b></span>')
    if tomorrow:
        days.append(f'<span>Morgen <b>{num(tomorrow.get("min"))}°</b> / <b>{num(tomorrow.get("max"))}°</b></span>')
    if days:
        parts.append(f'      <div class="wx__days">{"".join(days)}</div>')

    if location.get("error"):
        parts.append("      " + note(location["error"]).strip())

    parts.append("    </div>")
    return "\n".join(parts)


def render_traffic(block: dict[str, Any]) -> str:
    if not block.get("enabled", True):
        return note("Verkehr ist in der config.yaml abgeschaltet.")
    if block.get("skipped"):
        return note(block["skipped"])
    routes = block.get("routes") or []
    if not routes:
        return note("Keine Routen konfiguriert.")

    warn_at = int(block.get("warn_minutes", 10))
    return "\n".join(_route_panel(route, warn_at) for route in routes)


def _route_panel(route: dict[str, Any], warn_at: int) -> str:
    parts = ['    <div class="panel">',
             f"""      <div class="route__head">
        <span class="route__name">{esc(route["name"])}</span>
        <span class="route__km">{num(route.get("distance_km"), 1)} km</span>
      </div>"""]

    if route.get("error"):
        parts.append("      " + note(route["error"]).strip())
        parts.append("    </div>")
        return "\n".join(parts)

    delay = route.get("delay_minutes")
    live = route.get("live_minutes")
    free = route.get("free_flow_minutes")
    warn = delay is not None and delay >= warn_at

    if delay is None:
        chip = '<span class="delay">Verzögerung –</span>'
    elif delay <= 0:
        chip = '<span class="delay">frei</span>'
    else:
        chip = (f'<span class="delay{" delay--warn" if warn else ""}">'
                f'+{delay} min</span>')

    parts.append(f"""      <div class="route__times">
        <span class="route__live">{num(live)}<small>min</small></span>
        {chip}
        <span class="route__free">ohne Verkehr {num(free)} min</span>
      </div>""")

    # Balken: Anteil der Verzögerung an der Gesamtfahrzeit.
    if live and free and live > 0:
        share = max(0, min(100, round((live - free) / live * 100)))
        parts.append(f'      <div class="bar"><span class="bar__fill'
                     f'{" bar__fill--warn" if warn else ""}" style="width:{share}%"></span></div>')

    parts.append("    </div>")
    return "\n".join(parts)


def render_news(block: dict[str, Any], tz: ZoneInfo) -> str:
    if not block.get("enabled", True):
        return note("Nachrichten sind in der config.yaml abgeschaltet.")

    # Überschriften zu den Blöcken aus sources/news.py – die Reihenfolge
    # gibt dort BLOCKS vor.
    titles = {"top": "Wichtigstes heute", "region": "Region",
              "agrar": "Landwirtschaft", "eu": "Neues Regelwerk",
              "world": "Weltweit"}
    blocks = [(key, titles.get(key, key.title())) for key in news_source.BLOCKS]
    parts: list[str] = ['    <div class="panel">']
    any_story = False

    for key, label in blocks:
        stories = block.get(key) or []
        if not stories:
            continue
        any_story = True
        parts.append(f'      <div class="block__title">{esc(label)}</div>')
        for story in stories:
            parts.append(_story(story, tz))

    if not any_story:
        parts.append("      " + note(block.get("note")
                                     or "Derzeit keine Meldungen verfügbar.").strip())
    elif block.get("note"):
        parts.append("      " + note(block["note"]).strip())

    parts.append("    </div>")
    return "\n".join(parts)


def _story(story: dict[str, Any], tz: ZoneInfo) -> str:
    published = story.get("published")
    clock = published.astimezone(tz).strftime("%H:%M") if published else "–"

    meta = [f'<span class="chip chip--src">{esc(story.get("source", ""))}</span>',
            f'<span class="chip">{esc(clock)}</span>']
    also = story.get("also") or []
    if also:
        meta.append(f'<span class="chip chip--also">auch: {esc(", ".join(also[:3]))}</span>')

    summary = story.get("summary") or ""
    summary_html = f'\n        <p class="story__s">{esc(summary)}</p>' if summary else ""

    return f"""      <a class="story" href="{esc(story.get("link", "#"))}" target="_blank" rel="noopener noreferrer">
        <h3 class="story__h">{esc(story.get("title", ""))}</h3>{summary_html}
        <div class="story__m">{"".join(meta)}</div>
      </a>"""


def icon(name: str, css_class: str) -> str:
    """Verweis auf ein Symbol aus dem Inline-SVG-Sprite im Template."""
    return (f'<svg class="ic {css_class}" aria-hidden="true">'
            f'<use href="#i-{esc(name or "cloud")}"></use></svg>')


def note(text: str) -> str:
    return (f'    <div class="note"><span class="note__i">!</span>'
            f'<span>{esc(text)}</span></div>\n')


def render_footer(now: datetime, problems: Problems, news_block: dict[str, Any]) -> str:
    stamp = (f"{WEEKDAYS[now.weekday()]}, {now.day}. {MONTHS[now.month - 1]} "
             f"{now.year} · {now.strftime('%H:%M')} Uhr")
    lines = [f"Stand <b>{esc(stamp)}</b> ({esc(now.tzname() or '')}, Europe/Berlin)"]

    if news_block.get("item_count"):
        cached_at = news_block.get("cached_at")
        stand = (f" · Nachrichten von {cached_at.strftime('%H:%M')} Uhr"
                 if cached_at else "")
        lines.append(f'{news_block["item_count"]} Meldungen aus '
                     f'{news_block.get("feed_count", 0)} Feeds · '
                     f'Gewichtung: {esc(news_block.get("ranked_by", "–"))}{esc(stand)}')

    if problems:
        down = problems.names()
        shown = ", ".join(esc(n) for n in down[:8])
        rest = f" und {len(down) - 8} weitere" if len(down) > 8 else ""
        lines.append(f'<span class="foot__down">Nicht erreichbar:</span> {shown}{rest}')
    else:
        lines.append("Alle Quellen erreichbar.")

    lines.append("Gebaut aus statischem HTML · keine Skripte, keine Tracker")
    return "<br>\n    ".join(lines)


# ======================================================================
# Demo-Daten (für `--demo`, damit die Seite auch ohne Netz/Secrets steht)
# ======================================================================

def demo_data(now: datetime) -> tuple[dict, dict, dict]:
    def hour(h: int, t: float, p: int, icon: str) -> dict:
        return {"hour": f"{h:02d}", "temperature": t, "precipitation": 0.2 if p > 40 else 0.0,
                "precipitation_probability": p, "wind_speed": 12, "icon": icon, "label": "Demo"}

    weather = {
        "enabled": True,
        "alerts": [
            {"kind": "rain", "icon": "rain", "text": "Augsburg: Regen ab dem späten Vormittag – "
                                                  "70 % Regenwahrscheinlichkeit, 2,4 mm erwartet. Schirm einpacken."},
            {"kind": "frost", "icon": "snow", "text": "Marktoberdorf: Bodenfrost möglich, Tiefstwert 2 °C."},
        ],
        "locations": [
            {"name": "Augsburg", "error": None,
             "current": {"temperature": 8.4, "feels_like": 5.5, "wind_speed": 11,
                         "cloud_cover": 75, "icon": "partly-day", "label": "teils bewölkt"},
             "hourly": [hour(6, 8, 10, "cloud"), hour(9, 10, 20, "partly-day"), hour(12, 13, 70, "rain"),
                        hour(15, 14, 65, "rain"), hour(18, 12, 20, "partly-day"), hour(21, 9, 10, "moon")],
             "today": {"min": 7.0, "max": 14.0, "precipitation_probability": 70,
                       "precipitation_sum": 2.4},
             "tomorrow": {"min": 5.0, "max": 16.0, "precipitation_probability": 10,
                          "precipitation_sum": 0.0}},
            {"name": "Marktoberdorf", "error": None,
             "current": {"temperature": 6.1, "feels_like": 3.2, "wind_speed": 8,
                         "cloud_cover": 90, "icon": "cloud", "label": "bewölkt"},
             "hourly": [hour(6, 5, 20, "cloud"), hour(9, 8, 30, "cloud"), hour(12, 11, 60, "rain"),
                        hour(15, 12, 55, "rain"), hour(18, 10, 30, "cloud"), hour(21, 7, 10, "moon")],
             "today": {"min": 2.0, "max": 12.0, "precipitation_probability": 60,
                       "precipitation_sum": 1.8},
             "tomorrow": {"min": 3.0, "max": 14.0, "precipitation_probability": 20,
                          "precipitation_sum": 0.2}},
        ],
    }

    traffic = {
        "enabled": True, "skipped": None, "warn_minutes": 10,
        "routes": [
            {"name": "Augsburg → Marktoberdorf", "distance_km": 96.4, "live_minutes": 83,
             "free_flow_minutes": 69, "delay_minutes": 14, "error": None},
            {"name": "Marktoberdorf → Augsburg", "distance_km": 95.1, "live_minutes": 71,
             "free_flow_minutes": 68, "delay_minutes": 3, "error": None},
        ],
    }

    def story(title: str, source: str, summary: str, hours_ago: int,
              also: list[str] | None = None) -> dict:
        return {"title": title, "source": source, "summary": summary,
                "link": "https://example.org/", "also": also or [],
                "published": now - timedelta(hours=hours_ago), "teaser": "", "scope": "national"}

    news = {
        "enabled": True, "note": "Demo-Daten – keine echten Meldungen.",
        "lage": "Ruhiger Vormittag, ab elf zieht Regen auf – wenn du nach Augsburg "
                "und zurück willst, leg die Rückfahrt vor den Nachmittag. "
                "Politisch dominiert die Netzentgelt-Reform, für dich relevanter ist "
                "die geänderte Düngeverordnung, die ab Januar die roten Gebiete "
                "betrifft. Sonst nichts, was den Tag umwirft.",
        "feed_count": 20, "item_count": 128, "ranked_by": "Demo",
        "top": [
            story("Bundestag beschließt Reform der Netzentgelte", "tagesschau",
                  "Das Parlament hat eine Neuverteilung der Stromnetzkosten verabschiedet. "
                  "Haushalte in Regionen mit vielen Windrädern sollen dadurch entlastet werden.",
                  2, ["Zeit Online", "FAZ", "Spiegel"]),
            story("EZB lässt Leitzins unverändert", "Handelsblatt",
                  "Die Notenbank belässt den Zins auf dem bisherigen Niveau und verweist auf "
                  "die zuletzt wieder anziehende Kerninflation.", 3, ["Süddeutsche Zeitung", "n-tv"]),
            story("Tarifrunde im öffentlichen Dienst vertagt", "Deutschlandfunk",
                  "Die Verhandlungen sind ohne Ergebnis unterbrochen worden. Beide Seiten "
                  "kündigten für kommende Woche einen neuen Anlauf an.", 5),
            story("Studie: Bahnverkehr erneut unpünktlicher", "Zeit Online",
                  "Eine Auswertung der Fahrplandaten zeigt eine weiter sinkende Pünktlichkeit "
                  "im Fernverkehr, besonders auf den Nord-Süd-Strecken.", 7),
            story("Neue Regeln für Solaranlagen auf Dächern", "Welt",
                  "Ab dem kommenden Quartal gelten vereinfachte Anmeldeverfahren für kleine "
                  "Photovoltaikanlagen.", 8),
        ],
        "region": [
            story("A8 bei Augsburg nach Unfall wieder frei", "Augsburger Allgemeine",
                  "Nach einem Auffahrunfall am frühen Morgen war die Autobahn zeitweise gesperrt. "
                  "Seit etwa einer Stunde rollt der Verkehr wieder.", 1),
            story("Klinikverbund im Ostallgäu plant Umbau", "Allgäuer Zeitung",
                  "Der Trägerverbund will Standorte bündeln; über Details soll der Kreistag "
                  "im Herbst entscheiden.", 4),
            story("Augsburger Straßenbahnlinie wird verlängert", "BR24",
                  "Die Verlängerung soll die westlichen Stadtteile besser anbinden. "
                  "Der Bau beginnt im Frühjahr.", 6),
            story("Marktoberdorf beschließt neuen Haushalt", "Merkur",
                  "Der Stadtrat hat den Etat mit Schwerpunkt auf Schulsanierungen beschlossen.", 9),
        ],
        "agrar": [
            story("Milchpreis zieht im September weiter an", "agrarheute",
                  "Die Molkereien zahlen im Schnitt zwei Cent mehr je Kilogramm als im Vormonat. "
                  "Als Grund gilt die knappere Anlieferung im Süden.", 4),
            story("Bundesrat billigt Änderung bei der Düngeverordnung", "top agrar",
                  "Die Länder haben der Neufassung zugestimmt; für rote Gebiete gelten ab "
                  "kommendem Jahr veränderte Aufzeichnungspflichten.", 6, ["Proplanta"]),
            story("Maisernte im Allgäu läuft früher als üblich", "Allgäuer Zeitung",
                  "Wegen der trockenen Wochen im August beginnt der Häckselbetrieb in "
                  "vielen Betrieben gut zwei Wochen vor dem Durchschnitt.", 7),
            story("EU farm ministers revisit protein crop targets", "Agriland",
                  "Ministers discussed raising domestic protein production to cut reliance "
                  "on imported feed, with no decision expected before December.", 5),
            story("US corn harvest ahead of five-year average", "Successful Farming",
                  "Field reports put harvest progress several points above the usual pace "
                  "for mid-September, helped by dry weather across the Corn Belt.", 9),
        ],
        "eu": [
            story("Neue Typgenehmigungsfristen für Anbaugeräte beschlossen", "agrarheute",
                  "Die EU hat die Übergangsfristen für die Typgenehmigung angehängter "
                  "Arbeitsgeräte verlängert; betroffen sind Geräte über drei Tonnen.", 20,
                  ["Profi"]),
            story("Kommission legt Entwurf zur Maschinenverordnung vor", "CEMA",
                  "Der Entwurf betrifft unter anderem Nachrüstsätze und die Einstufung "
                  "von Software-Updates als wesentliche Veränderung.", 30),
        ],
        "world": [
            story("UN-Vollversammlung startet in New York", "Reuters",
                  "Die Generaldebatte beginnt mit Reden zahlreicher Staats- und Regierungschefs.",
                  3, ["BBC News", "AP News"]),
            story("Schwere Überschwemmungen in Südostasien", "BBC News",
                  "Nach tagelangem Starkregen mussten zehntausende Menschen ihre Häuser verlassen.", 5),
            story("Handelsgespräche vor neuer Runde", "AP News",
                  "Beide Seiten deuten Bewegung an, ein Abschluss gilt aber weiter als offen.", 6),
            story("Rekordhitze im Süden Australiens gemessen", "The Guardian",
                  "Der Wetterdienst meldet für mehrere Regionen die höchsten je gemessenen "
                  "Septemberwerte.", 10),
        ],
    }
    return weather, traffic, news


# ======================================================================
# Hauptlauf
# ======================================================================

def _news_with_cache(config: dict[str, Any], http: HttpConfig, problems: Problems,
                     now: datetime) -> dict[str, Any]:
    """Nachrichten holen – oder den noch frischen Stand aus dem Cache nehmen."""
    news_config = config.get("news") or {}
    llm = news_config.get("llm") or {}
    once_per_day = bool(llm.get("once_per_day", True))
    max_age = timedelta(minutes=float(llm.get("min_interval_minutes", 0) or 0))
    stamp = news_cache.fingerprint(news_config, NEWS_MODULE)

    cached = news_cache.load(NEWS_CACHE, max_age, now, stamp, once_per_day)
    if cached is not None:
        age = int((now - cached["cached_at"]).total_seconds() // 60)
        log.info("Nachrichten aus dem Cache (%d min alt, Stand von heute früh).", age)
        return cached

    seen = news_cache.load_seen(SEEN_FILE)
    block = news_source.collect(config, http, problems,
                                now.astimezone(timezone.utc), seen)
    # Erst vermerken, wenn der Abruf wirklich etwas gebracht hat – sonst
    # gilt eine Meldung als gesehen, die nie auf der Seite stand.
    update = block.get("seen_update") or {}
    if update and any(block.get(key) for key in news_source.BLOCKS):
        news_cache.save_seen(SEEN_FILE, {**seen, **update}, now)
    # Nur brauchbare Ergebnisse ablegen – ein Totalausfall soll den letzten
    # guten Stand nicht überschreiben.
    if any(block.get(key) for key in news_source.BLOCKS):
        news_cache.save(NEWS_CACHE, block, now, stamp)
    else:
        stale = news_cache.load(NEWS_CACHE, timedelta(days=2), now, stamp)
        if stale is not None:
            log.info("Keine frischen Meldungen – letzter Stand aus dem Cache.")
            stale["note"] = "Keine frischen Meldungen abrufbar – letzter bekannter Stand."
            return stale
    return block


def build(demo: bool = False) -> int:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    general = config.get("general") or {}
    tz = ZoneInfo(general.get("timezone", "Europe/Berlin"))
    now = datetime.now(tz)

    problems = Problems()
    http = HttpConfig.from_dict(config.get("http"))

    if demo:
        log.info("Demo-Modus: keine Abrufe, Beispieldaten werden gerendert.")
        weather_block, traffic_block, news_block = demo_data(now)
    else:
        weather_block = weather_source.collect(config, http, problems, now)
        traffic_block = traffic_source.collect(config, http, problems)
        news_block = _news_with_cache(config, http, problems, now)

    stamp = (f"{WEEKDAYS[now.weekday()]} · {now.strftime('%d.%m.%Y')} · "
             f"{now.strftime('%H:%M')} Uhr")

    lage_html = render_lage(news_block.get("lage") or "")
    alerts_html = render_alerts(weather_block.get("alerts") or [])

    page = TEMPLATE.read_text(encoding="utf-8")
    for key, value in {
        "TITLE": esc(general.get("title", "JARVIS")),
        "SUBTITLE": esc(general.get("subtitle", "Morgen-Briefing")),
        "STAMP": esc(stamp),
        "LAGE": lage_html,
        # Gibt es eine Lage, fasst sie das Wetter ohnehin in Worte – die
        # genauen Hinweise stehen dann im Wetterblock statt doppelt oben.
        "ALERTS": "" if lage_html else alerts_html,
        "WEATHER": render_weather(weather_block, alerts_html if lage_html else ""),
        "TRAFFIC": render_traffic(traffic_block),
        "NEWS": render_news(news_block, tz),
        "FOOTER": render_footer(now, problems, news_block),
    }.items():
        page = page.replace("{{" + key + "}}", value)

    leftovers = re.findall(r"\{\{[A-Z_]+\}\}", page)
    if leftovers:
        raise RuntimeError(f"Ungefüllte Platzhalter im Template: {', '.join(sorted(set(leftovers)))}")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(page, encoding="utf-8")

    log.info("Geschrieben: %s (%d Bytes)", OUTPUT, OUTPUT.stat().st_size)
    if problems:
        log.info("Nicht erreichbar: %s", ", ".join(problems.names()))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Morgen-Briefing bauen")
    parser.add_argument("--demo", action="store_true",
                        help="Beispieldaten rendern statt echte Quellen abzurufen")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )
    return build(demo=args.demo)


if __name__ == "__main__":
    raise SystemExit(main())
