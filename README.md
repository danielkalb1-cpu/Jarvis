# Jarvis – Morgen-Briefing

Ein persönliches Morgen-Briefing als statische Seite: Wetter, Verkehr und
Nachrichten auf einem Blick, gebaut für ein Handy im Hochformat.

Ein Python-Script holt alle Daten, rendert daraus eine fertige `docs/index.html`
und committet sie zurück. Eine GitHub Action ruft das Script per Cron auf.
GitHub Pages liefert das Ergebnis aus.

**Die ausgelieferte Seite enthält kein JavaScript, das Daten nachlädt, und keine
API-Aufrufe.** Alles steht zum Build-Zeitpunkt schon im HTML. Grund: das Repo ist
öffentlich – API-Keys dürfen nicht im Browser landen. Sie liegen ausschließlich
in GitHub-Secrets und werden nur auf dem Actions-Runner gelesen.

---

## Inhalt

1. [Einmal einrichten](#einmal-einrichten)
2. [Secrets anlegen](#1-secrets-anlegen)
3. [TomTom-Key besorgen](#2-tomtom-key-besorgen-kostenlos)
4. [GitHub Pages auf `/docs` stellen](#3-github-pages-auf-docs-stellen)
5. [Zum Home-Bildschirm hinzufügen](#4-zum-home-bildschirm-hinzufügen)
6. [Orte und Routen ändern](#orte-und-routen-ändern)
7. [Lokal testen](#lokal-testen)
8. [Feeds prüfen](#feeds-prüfen) und [Quellenlage](#zur-quellenlage)
9. [Zeitplan und Kosten](#zeitplan-und-kosten)
10. [Wenn etwas nicht geht](#wenn-etwas-nicht-geht)
11. [Urheberrecht](#urheberrecht-bei-den-nachrichten)

---

## Einmal einrichten

Die vier Schritte unten einmal durchgehen, danach läuft alles von allein.
Ohne Secrets läuft der Build trotzdem: der Verkehrsblock wird dann sauber
übersprungen und die Nachrichten werden nur nach Aktualität sortiert.

### 1. Secrets anlegen

Im Repo: **Settings → Secrets and variables → Actions → New repository secret**.

| Name                | Pflicht | Wofür                                                |
| ------------------- | ------- | ---------------------------------------------------- |
| `TOMTOM_API_KEY`    | nein    | Fahrzeiten mit Live-Verkehr. Fehlt er, entfällt der Verkehrsblock. |
| `ANTHROPIC_API_KEY` | nein    | Auswahl und Gewichtung der Nachrichten. Fehlt er, wird nach Aktualität sortiert. |

Für das Wetter ist kein Key nötig – Bright Sky (DWD-Daten) ist frei abrufbar.

> Die Keys gehören **nur** hierhin. Nicht in `config.yaml`, nicht in den Code.

### 2. TomTom-Key besorgen (kostenlos)

1. Auf <https://developer.tomtom.com/> ein Konto anlegen.
2. Nach dem Anmelden auf **Dashboard → My Keys** gehen. Dort liegt bereits ein
   Standard-Key; mit **Add new key** lässt sich ein eigener anlegen.
3. Beim Anlegen das Produkt **Routing API** aktivieren.
4. Den Key kopieren und als Secret `TOMTOM_API_KEY` eintragen.

Der kostenlose Tarif umfasst ein tägliches Kontingent an Anfragen. Dieses
Briefing braucht **zwei Anfragen pro Lauf** (eine je Richtung), also rund
70 pro Werktag – das passt bequem hinein.

### 3. GitHub Pages auf `/docs` stellen

1. Im Repo auf **Settings → Pages**.
2. Bei **Source** *Deploy from a branch* auswählen.
3. Bei **Branch**: `main` wählen, daneben den Ordner **`/docs`**.
4. **Save** drücken.

Nach ein bis zwei Minuten ist die Seite erreichbar unter:

```
https://<dein-github-name>.github.io/<repo-name>/
```

Die Seite trägt `noindex` – sie wird von Suchmaschinen nicht aufgenommen.
Öffentlich erreichbar ist sie trotzdem, wer die Adresse kennt, kann sie sehen.
Es stehen dort nur Wetter, Fahrzeiten und öffentliche Schlagzeilen.

### 4. Zum Home-Bildschirm hinzufügen

Die Seite bringt ein `manifest.json` und die passenden Meta-Tags mit, verhält
sich also wie eine App.

* **iPhone (Safari):** Seite öffnen → Teilen-Symbol → *Zum Home-Bildschirm*.
* **Android (Chrome):** Seite öffnen → Menü (⋮) → *Zum Startbildschirm hinzufügen*.

---

## Orte und Routen ändern

Alles Ortsbezogene steht in `config.yaml`. Voreingestellt sind die Ortsmitten –
für die Fahrzeiten lohnt es sich, die genaue Adresse einzutragen.

**Koordinaten finden:** in Google Maps oder OpenStreetMap den Punkt lange
antippen; die angezeigten Zahlen sind `lat, lon` in dieser Reihenfolge.

```yaml
weather:
  locations:
    - name: "Augsburg"
      lat: 48.3705        # <- hier die eigene Adresse eintragen
      lon: 10.8978

traffic:
  delay_warn_minutes: 10  # ab wann die Verzögerung rot wird
  routes:
    - name: "Augsburg → Marktoberdorf"
      from: { name: "Augsburg",      lat: 48.3705, lon: 10.8978 }
      to:   { name: "Marktoberdorf", lat: 47.7767, lon: 10.6167 }
```

Weitere nützliche Stellschrauben in derselben Datei:

| Schlüssel                         | Bedeutung                                           |
| --------------------------------- | --------------------------------------------------- |
| `weather.forecast_step_hours`     | Schrittweite im Tagesverlauf (Vorgabe: 3 Stunden)   |
| `weather.alerts.*`                | ab wann Regen- und Frosthinweis oben erscheinen     |
| `news.counts`                     | Größe der drei Nachrichtenblöcke (5 / 4 / 4)        |
| `news.feeds`                      | die Feed-Liste, siehe [Feeds prüfen](#feeds-prüfen) |
| `news.llm.region_terms`           | was als „regional“ zählt                            |
| `news.llm.min_interval_minutes`   | wie oft die Nachrichten neu gewichtet werden        |
| `http.timeout_seconds` / `retries`| Geduld bei langsamen Quellen                        |

Nach dem Ändern committen – der nächste Lauf übernimmt es. Sofort sehen:
**Actions → Briefing bauen → Run workflow**. Änderungen an den Nachrichten
wirken dabei sofort: der Cache merkt sich die Konfiguration und verfällt,
sobald sie sich ändert.

---

## Lokal testen

```bash
git clone https://github.com/<dein-name>/<repo>.git
cd <repo>
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**Ohne Netz und ohne Keys** – rendert die Seite mit Beispieldaten. Gut, um am
Aussehen zu schrauben:

```bash
python3 src/build.py --demo
```

**Mit echten Daten.** Ohne gesetzte Keys werden Verkehr und Gewichtung
übersprungen, der Rest wird ganz normal geholt:

```bash
python3 src/build.py --verbose

# mit Keys:
export TOMTOM_API_KEY="…"
export ANTHROPIC_API_KEY="…"
python3 src/build.py --verbose
```

Ergebnis ansehen:

```bash
python3 -m http.server 8000 --directory docs
# dann http://localhost:8000 im Browser öffnen
```

Im Browser mit den Entwicklerwerkzeugen die Handy-Ansicht einschalten – die
Seite ist für ein Display im Hochformat gebaut.

---

## Feeds prüfen

RSS-Adressen ändern sich häufiger, als man denkt. Dieses Script ruft jeden Feed
aus der `config.yaml` tatsächlich ab und sagt, was zurückkommt:

```bash
python3 src/verify_feeds.py
```

```
  OK    tagesschau                30 Einträge · neueste Meldung vor 0.4 h · „tagesschau.de“
  FEHL  Beispielzeitung           nicht abrufbar: HTTP 404
```

Einzelne Kandidaten testen, bevor sie in die `config.yaml` wandern:

```bash
python3 src/verify_feeds.py "https://example.de/rss" "https://example.de/feed/"
```

Und wenn unklar ist, wo eine Redaktion ihren Feed inzwischen hat – dieser
Aufruf liest die Feed-Angaben aus dem `<head>` der Seite:

```bash
python3 src/verify_feeds.py --discover https://www.example.de/
```

Dasselbe läuft monatlich als Action (**Actions → Feeds prüfen**) und lässt sich
dort jederzeit von Hand starten; die beiden Sonderfälle oben gibt es dort als
Eingabefelder. Ein toter Feed lässt den Build **nicht** scheitern – er wird
übersprungen und unten auf der Seite als nicht erreichbar vermerkt.

### Zur Quellenlage

Die Liste in der `config.yaml` ist geprüft: alle 22 Feeds haben beim Einrichten
geantwortet. Fünf der ursprünglich vorgesehenen Quellen gibt es so nicht mehr:

| Quelle                              | Befund                                   | Ersatz |
| ----------------------------------- | ---------------------------------------- | ------ |
| BR24                                | 404, kein Feed mehr im HTML deklariert    | tagesschau Bayern (ARD-Regionalschiene) |
| Augsburger Allgemeine               | 404, kein Feed mehr im HTML deklariert    | Google-News-Suche „Augsburg“ |
| Allgäuer Zeitung                    | 404, kein Feed mehr im HTML deklariert    | Google-News-Suche „Allgäu“ |
| Reuters                             | öffentliche Feeds eingestellt             | Al Jazeera, France 24 |
| AP News                             | 403, auch mit Browser-Kennung             | Deutsche Welle |

Die beiden Google-News-Suchen liefern weiterhin die Meldungen von Augsburger
Allgemeine und Allgäuer Zeitung – nur eben über den Umweg des Aggregators. Als
Quelle wird auf der Seite dann das meldende Haus angezeigt, nicht Google.

---

## Zeitplan und Kosten

Der Build läuft werktags halbstündlich zwischen 03:00 und 19:30 UTC:

```yaml
- cron: '0,30 3-19 * * 1-5'
```

Der Bereich ist absichtlich großzügig gewählt, weil GitHub Cron in UTC rechnet
und sich die Ortszeit mit der Sommerzeit verschiebt: 03:00 UTC sind 04:00 Uhr in
der Winterzeit und 05:00 Uhr in der Sommerzeit. So liegt in beiden Fällen ein
Lauf sicher vor sechs Uhr morgens.

Geplante Läufe starten bei GitHub gelegentlich einige Minuten später als
eingetragen – dafür ist die halbstündliche Taktung da.

**Zu den Kosten:** die vielen Läufe sind für den Verkehr gedacht. Die
Nachrichten jedes Mal neu von Claude gewichten zu lassen, wäre teuer, deshalb
wird das Ergebnis in `cache/news.json` zwischengespeichert und nur alle
drei Stunden erneuert (`news.llm.min_interval_minutes`). Damit bleiben etwa
sechs API-Aufrufe pro Werktag übrig statt über dreißig. Wer die Nachrichten
lieber jedes Mal frisch will, setzt den Wert auf `0` – wer gar keine
KI-Gewichtung will, setzt `news.llm.enabled: false`.

**Zur Repo-Größe:** jeder Lauf mit Änderung erzeugt einen Commit mit der neu
gerenderten `docs/index.html`. Das sind etwa 30 Commits pro Werktag. Git packt
die Versionen gut, aber nach ein paar Jahren lohnt sich ein Blick auf
`git count-objects -vH`. Wer die Historie kurz halten will, kann den Zeitplan
ausdünnen (etwa `0 3-19 * * 1-5` für stündlich statt halbstündlich).

---

## Wenn etwas nicht geht

**Die Seite zeigt einen alten Stand.** Unten auf der Seite steht der
Build-Zeitpunkt. Stimmt der nicht, unter **Actions** nachsehen, ob der letzte
Lauf durchgelaufen ist. GitHub schaltet geplante Läufe in Repos ab, die
60 Tage lang keine Aktivität hatten – ein manueller Lauf weckt sie wieder.

**Im Verkehrsblock steht „Kein TOMTOM_API_KEY gesetzt“.** Das Secret fehlt oder
heißt anders. Groß-/Kleinschreibung beachten.

**Eine Quelle fehlt.** Unten auf der Seite steht, welche beim letzten Lauf nicht
erreichbar waren. Einzelne Ausfälle sind normal; bleibt eine Quelle dauerhaft
weg, `src/verify_feeds.py` laufen lassen.

**Der Workflow ist rot.** Der Build scheitert absichtlich nur dann, wenn das
Rendering selbst kaputt ist – nicht, wenn eine Quelle ausfällt. Das Log unter
**Actions** zeigt die Zeile.

**Push-Fehler im Workflow.** Wenn zwei Läufe sich überholen, versucht der
Workflow es bis zu viermal mit Rebase. `concurrency` verhindert das
normalerweise.

---

## Urheberrecht bei den Nachrichten

Aus den Feeds werden **ausschließlich Überschrift, Kurzbeschreibung, Zeitstempel
und Link** verwendet. Es werden keine Artikelseiten abgerufen und keine Absätze
übernommen. Die ein bis zwei Sätze zu jeder Meldung formuliert das Modell in
eigenen Worten; jede Meldung verlinkt auf das Original beim jeweiligen Haus.

---

## Aufbau

```
.github/workflows/build.yml         Cron-Lauf: rendern und zurückcommitten
.github/workflows/verify-feeds.yml  monatliche Prüfung der Feed-URLs
src/build.py                        holt alles, füllt das Template, schreibt docs/
src/template.html                   Gerüst und das komplette CSS (alles inline)
src/cache.py                        Zwischenspeicher für den Nachrichtenblock
src/verify_feeds.py                 prüft die Feed-Liste
src/sources/net.py                  Timeout, Retry, Sammelstelle für Ausfälle
src/sources/weather.py              Bright Sky (DWD)
src/sources/traffic.py              TomTom Routing
src/sources/news.py                 RSS einsammeln, Claude gewichten lassen
config.yaml                         Orte, Routen, Feeds, Schwellenwerte
cache/news.json                     letzter Nachrichtenstand (wird committet)
docs/                               was Pages ausliefert
```

### Wie die Robustheit gedacht ist

Jeder Abruf hat Timeout und zwei Versuche. Fällt eine Quelle aus, bekommt der
betroffene Block eine sichtbare, aber unaufdringliche Notiz – die übrigen Blöcke
rendern ganz normal weiter. Ganz unten steht, welche Quellen beim letzten Lauf
nicht erreichbar waren. Der Workflow scheitert nur, wenn das Rendering selbst
kaputt ist.
