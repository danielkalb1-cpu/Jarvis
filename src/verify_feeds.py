#!/usr/bin/env python3
"""Prüft jede Feed-URL aus der config.yaml mit einem echten Request.

RSS-Adressen ändern sich häufig. Dieses Script ruft jeden Feed ab, schaut nach,
ob wirklich ein lesbarer Feed zurückkommt, und zeigt, wie alt die neueste
Meldung darin ist.

    python3 src/verify_feeds.py                  # alle Feeds aus der config
    python3 src/verify_feeds.py <url> [<url> …]  # einzelne Kandidaten testen

Der zweite Aufruf ist praktisch, wenn ein Feed umgezogen ist: erst die
Kandidaten durchprobieren, dann die passende URL in die config eintragen.

Exit-Code 1, wenn mindestens ein Feed nicht nutzbar ist – damit ein
CI-Lauf den Ausfall sichtbar macht.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources.net import FetchError, HttpConfig, fetch  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# Manche Redaktionen weisen Requests ohne browserähnlichen User-Agent ab.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def check(feed: dict, http: HttpConfig) -> tuple[str, bool, str]:
    name = feed.get("name", "?")
    url = feed.get("url", "")
    headers = {"Accept": "application/rss+xml, application/xml, text/xml, */*"}
    try:
        response = fetch(url, http, headers=headers)
    except FetchError as exc:
        # Zweiter Versuch mit browserähnlichem User-Agent.
        try:
            response = fetch(url, http, headers={**headers, "User-Agent": BROWSER_UA})
        except FetchError:
            return name, False, f"nicht abrufbar: {exc}"
        name = f"{name} [Browser-UA]"

    parsed = feedparser.parse(response.content)
    entries = parsed.entries or []
    if not entries:
        reason = "kein lesbarer Feed" if parsed.bozo else "Feed ist leer"
        return name, False, reason

    newest = None
    for entry in entries:
        for field in ("published_parsed", "updated_parsed"):
            value = getattr(entry, field, None)
            if value:
                import calendar
                stamp = datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
                newest = stamp if newest is None or stamp > newest else newest
                break

    if newest is None:
        age = "ohne Zeitstempel"
    else:
        hours = (datetime.now(timezone.utc) - newest).total_seconds() / 3600
        age = f"neueste Meldung vor {hours:.1f} h"

    title = (parsed.feed.get("title") or "").strip()[:40]
    return name, True, f"{len(entries):>3} Einträge · {age} · „{title}“"


def main() -> int:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    http = HttpConfig.from_dict(config.get("http"))

    given = [a for a in sys.argv[1:] if a.startswith("http")]
    if given:
        feeds = [{"name": url.split("/")[2], "url": url} for url in given]
        print(f"Prüfe {len(feeds)} übergebene URLs …\n")
    else:
        feeds = (config.get("news") or {}).get("feeds") or []
        print(f"Prüfe {len(feeds)} Feeds aus config.yaml …\n")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda f: check(f, http), feeds))

    broken = []
    for (feed, (name, ok, detail)) in zip(feeds, results):
        label = name if name != feed.get("name") else feed.get("name", "?")
        print(f"  {'OK  ' if ok else 'FEHL'}  {label:<26} {detail}")
        if given:
            print(f"          {feed['url']}")
        if not ok:
            broken.append(feed.get("name", "?"))

    print()
    if broken:
        print(f"{len(broken)} von {len(feeds)} Feeds nicht nutzbar: {', '.join(broken)}")
        print("Bitte die URL in config.yaml korrigieren oder den Eintrag entfernen.")
        return 1

    print(f"Alle {len(feeds)} Feeds in Ordnung.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
