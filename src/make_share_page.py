#!/usr/bin/env python3
"""Erzeugt aus docs/index.html die Fassung für den Teilen-Link (Artifact).

Hintergrund: docs/index.html ist eine vollständige HTML-Seite. Die
Artifact-Plattform setzt den Seitenrahmen selbst, erwartet also nur Titel,
Stil und Inhalt. Außerdem kann der Betrachter dort das Farbschema
ausdrücklich setzen – die Seite folgt von sich aus nur dem Betriebssystem.

    python3 src/make_share_page.py [ziel.html]
    python3 src/make_share_page.py --source <datei|url> [ziel.html]

Vorgabe für das Ziel ist build/share.html. Die Datei wird anschließend als
Artifact veröffentlicht; das Skript selbst veröffentlicht nichts.

Ohne --source wird der aktuellste Stand vom Branch geholt. Mit --source
lässt sich eine Datei oder eine URL angeben – praktisch, wenn keine
Arbeitskopie da ist, etwa:

    --source https://raw.githubusercontent.com/<user>/<repo>/main/docs/index.html
"""

from __future__ import annotations

import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "index.html"
DEFAULT_TARGET = ROOT / "build" / "share.html"
TITLE = "Jarvis Morgen-Briefing"


def read_source(source: str) -> str:
    """Seite aus einer Datei oder von einer URL lesen."""
    if source.startswith(("http://", "https://")):
        print(f"Quelle: {source}")
        with urllib.request.urlopen(source, timeout=30) as response:
            return response.read().decode("utf-8")
    print(f"Quelle: {source}")
    return Path(source).read_text(encoding="utf-8")


def newest_page() -> str:
    """Die aktuellste gerenderte Seite holen.

    Der Workflow committet docs/index.html halbstündlich zurück. In einer
    frischen Arbeitskopie kann die lokale Datei also älter sein als das, was
    auf dem Branch liegt – daher erst vom Remote versuchen.
    """
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        if branch:
            subprocess.run(["git", "fetch", "--quiet", "origin"],
                           cwd=ROOT, capture_output=True, timeout=120)
            page = subprocess.run(
                ["git", "show", f"{branch}:docs/index.html"],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            if page.returncode == 0 and page.stdout.strip():
                print(f"Quelle: {branch}:docs/index.html")
                return page.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Remote-Stand nicht verfügbar ({exc}) – nehme die lokale Datei.")

    print(f"Quelle: {SOURCE}")
    return SOURCE.read_text(encoding="utf-8")


def to_share_page(src: str) -> str:
    style = re.search(r"<style>(.*?)</style>", src, re.S).group(1)
    body = re.search(r"<body>(.*?)</body>", src, re.S).group(1)

    # Helle Palette aus dem Media-Query lösen und zusätzlich an die
    # ausdrückliche Auswahl des Betrachters hängen.
    block = re.search(
        r"@media \(prefers-color-scheme: light\)\{\s*(:root\{.*?\})\s*\}", style, re.S)
    if block:
        tokens = re.search(r":root\{(.*?)\}", block.group(1), re.S).group(1)
        style = style.replace(block.group(0), (
            "/* Hell als Einstellung des Systems – außer der Betrachter hat\n"
            "   ausdrücklich Dunkel gewählt. */\n"
            "@media (prefers-color-scheme: light){\n"
            f'  :root:not([data-theme="dark"]){{{tokens}}}\n'
            "}\n"
            "/* Hell ausdrücklich gewählt – schlägt auch ein dunkles System. */\n"
            f':root[data-theme="light"]{{{tokens}}}\n'
        ))

    return f"<title>{TITLE}</title>\n<style>{style}</style>\n{body.strip()}\n"


def main() -> int:
    args = sys.argv[1:]
    source = None
    if "--source" in args:
        index = args.index("--source")
        source = args[index + 1]
        del args[index:index + 2]

    target = Path(args[0]) if args else DEFAULT_TARGET
    page = to_share_page(read_source(source) if source else newest_page())

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")

    stand = re.search(r"Stand <b>([^<]*)", page)
    print(f"Geschrieben: {target} ({len(page)} Bytes)")
    print(f"Stand der Seite: {stand.group(1) if stand else 'unbekannt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
