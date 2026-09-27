# Plan: Grid-Ansicht, Aktions-Einblendungen, Asset-System

Stand: 2026-09-27 · Status: Phase A–C abgeschlossen (Schritte 1–9b), weiter mit Phase D

Drei Erweiterungen des Display-Companions (`skills/dnd/display/`). Grundprinzip:
modular, austauschbare Teile (Bildquelle, Icon-Sets, Trigger-Logik),
Konfigurationsdateien statt fest verdrahteter Werte, **JSON über SSE** als
Schnittstelle zwischen Spiellogik (Server) und Anzeige (Browser).

**Nicht Teil des Plans:** Lokalisierung der Web-Oberfläche. Die Oberfläche
bleibt Englisch; nur DM-Erzählung und Spielinhalte sind Deutsch.

---

## 1. Querschnitt

### Konfiguration in zwei Ebenen

Das Plugin liefert Standardwerte mit; Dateien im Datenordner überschreiben
sie. Der Code-Ordner wird bei `/plugin update` ersetzt, darum liegt alles
Nutzereigene im Datenordner (`DND_CAMPAIGN_ROOT`, Standard `~/.claude/dnd`).

```
Plugin (wird bei Update ersetzt)          Datenordner (bleibt erhalten)
display/config/vfx-triggers.json    ←     config/vfx-triggers.json
display/config/vfx-iconset.json     ←     config/vfx-iconset.json
display/config/item-categories.json ←     config/item-categories.json
display/config/image-provider.json  ←     config/image-provider.json
display/config/travel-events.json   ←     config/travel-events.json
display/config/map-templates/*.json ←     config/map-templates/*.json
display/assets/placeholders/*.svg         assets/{tokens,items,maps}/, manifest.json, pending-assets.json
                                          maps/library/*.json            (Kartenaufbau, kampagnenübergreifend)
                                          campaigns/<k>/maps/*.state.json (Belegung pro Kampagne)
```

`display/config_loader.py` lädt beide Ebenen, führt sie zusammen (Objekte
rekursiv, Listen/Werte ersetzen, `null` entfernt einen Schlüssel), prüft sie
optional und lädt bei Dateiänderung neu. Eine kaputte Überschreibung wird mit
Warnung ignoriert, der Standard gilt weiter.

Einstellungen pro Kampagne stehen in den Session Flags der `state.md`
(`display_mode: auto`, `travel_event_chance: 15`).

### Frontend

Neue Teile als eigene Dateien unter `display/static/{js,css}/` (kein
Build-Schritt). `index.html` bekommt einen Verteiler
`DisplayModules.register(name, handler)`, der jede SSE-Nachricht an die
Module weiterreicht.

### Stichwort-Erkennung

`TriggerMatcher` (`display/triggers.py`) wird aus `audio.py` herausgelöst und
von Sound und Einblendungen gemeinsam genutzt. Neu: `wort*` = Wortanfang (auch in Phrasen: `greif* an`).

---

## 2. Aktions-Einblendungen (VFX)

Zwei getrennte Dateien — *welcher Text löst was aus* und *wie sieht es aus*:

```json
// vfx-triggers.json
{"version":1, "cooldown_ms":1500, "max_per_chunk":1,
 "triggers": {
   "de": {"attack":["greift an","schlägt zu","Hieb"], "steal":["stiehlt","klaut","entwendet"],
          "spell":["Zauber","wirkt +","beschwört"], "heal":["heilt","Heiltrank"]},
   "en": {"attack":["attacks","strikes","slash*"]}}}

// vfx-iconset.json
{"id":"default","effects":{
  "attack":{"icon":"vfx/sword.svg","animation":"slash","duration_ms":900,"tint":"#e74c3c"},
  "steal": {"icon":"vfx/hand.svg","animation":"swipe","duration_ms":800}}}
```

- Server: `vfx.py` mit derselben Schnittstelle wie `audio.py`
  (`set_broadcast`, `on_text`, `set_languages`); SSE `{"vfx":{"effect":"attack","actor":null}}`.
- `send.py --vfx attack[:Flerb]` löst gezielt aus und hat Vorrang.
- `vfx.js`: Einblendung mittig bzw. über der Figur, wenn eine Karte aktiv ist;
  beachtet `prefers-reduced-motion`.

---

## 3. Asset-System

### manifest.json

Schlüssel `art:slug` (Umlaute umgeschrieben: ä→ae, ß→ss).

```json
{"version":1,
 "entries":{
   "item:flammenschwert-der-asche":{"file":"items/flammenschwert-der-asche.png",
       "name":"Flammenschwert der Asche","category":"weapon","source":"gemini","prompt":"…","created":"…"},
   "token:flerb":{"file":"tokens/flerb.png","name":"Flerb","category":"pc"},
   "map:kessel-schankraum":{"file":"maps/kessel-schankraum.png","category":"map"}},
 "aliases":{}}
```

- Route `/assets/<pfad>` (mit Schutz gegen Pfad-Tricks), Kampagne kann globale Bilder überschreiben.
- `AssetResolver.url(key, category)` → Bild oder Platzhalter der Kategorie
  (`weapon, armor, potion, scroll, ring, wondrous, gear, pc, npc, enemy, map`).
- Kategorie: explizit → SRD → `item-categories.json` → `gear`.
- Inventar-Einträge dürfen Text oder `{"name":…, "category":…}` sein.

### Warteliste pending-assets.json

Einträge bei `inventory-add`, neuen Figuren und Karten fester Orte ohne Bild.
Keine Duplikate, **nie Generierung während des Spiels**. Temporäre
Ereignis-Karten kommen nicht auf die Liste.

```json
{"version":1,"queue":[{"key":"item:flammenschwert-der-asche","kind":"item",
  "name":"Flammenschwert der Asche","category":"weapon","campaign":"ashveil",
  "hint":"Klinge glüht wie Kohle","prompt":null,
  "status":"pending","attempts":0,"last_error":null,"first_seen":"…"}]}
```

`status`: `pending | done | failed | skipped`.

### /dm:dnd assets <status|generate|seed|add|skip|retry>

`generate`: Claude ergänzt Beschreibungen aus dem Kampagnenkontext, dann
erzeugt `scripts/assets.py generate [--limit N] [--provider X] [--dry-run]`
die Bilder. Läuft auch ohne Claude und im Hintergrund.

### Standardkatalog vorab erzeugen (seed)

Die Warteliste erfährt von Inhalten erst, wenn sie im Spiel auftauchen.
Kampagnenunabhängige Standardinhalte sind vorher bekannt und werden mit
`scripts/assets.py seed [--category items|portraits|maps] [--limit N] [--provider X] [--dry-run]`
(`/dm:dnd assets seed`) einmal erzeugt und von allen Kampagnen genutzt:

- **items** — Standardausrüstung aus dem SRD (Langschwert, Heiltrank,
  Lederrüstung, Kurzbogen …), deutsche Namen, englischer SRD-Name als Alias.
- **portraits** — Archetypen ohne konkrete NSC-Namen (menschlicher Krieger,
  Elfen-Magierin, Zwergen-Kleriker, Wirtin, Stadtwache, Goblin, Ork …).
- **maps** — Hintergründe für wiederkehrende Szenenarten (Taverne, Waldweg,
  Verlies-Korridor, Höhle …), je mit der passenden Kartenvorlage (`template`).

Der Katalog `display/config/asset-seed.json` gehört zur Plugin-Konfiguration,
nicht zur Kampagne (eigene Einträge in `<data-root>/config/asset-seed.json`;
Einträge sind nach Slug geschlüsselt, `null` entfernt einen). Die englischen
Bildbeschreibungen stehen im Katalog, Claude muss nichts ergänzen.

```json
{"items": {"langschwert": {"name": "Langschwert", "category": "weapon", "srd": "Longsword",
                           "aliases": ["Longsword"], "prompt": "a straight double-edged steel longsword …"}},
 "maps":  {"taverne": {"name": "Taverne", "category": "map", "template": "tavern-small", "prompt": "…"}}}
```

Ergebnis im **selben Manifest** wie kampagnenspezifische Bilder, mit
generischem Schlüssel (`item:langschwert`, `token:goblin`, `map:taverne`) und
`"generic": true`, `"seed": "items"`. Vorrang:

- `seed` ersetzt nie ein vorhandenes Bild und überspringt Schlüssel, die auf
  der Warteliste `pending` sind (die bekommen ihr eigenes Bild).
- Ein spezifisches Bild mit demselben Schlüssel ersetzt das generische:
  `generate` eines Wartelisten-Eintrags oder `add`. Ein generisches Bild zählt
  im Spiel als vorhanden (kommt nicht automatisch auf die Warteliste); soll es
  ersetzt werden: `assets.py prompt KEY "…" --add`, dann `generate`.
- Fehlschläge werden nicht gespeichert; der nächste `seed`-Lauf versucht es erneut.

Voller Katalog ≈ 95 Bilder ≈ 3,20 $ mit dem Standardmodell; `--dry-run` zeigt
vorher Anzahl und Prompts.

### Bilddienst-Schnittstelle

```python
@dataclass
class ImageRequest:  prompt: str; kind: str; width: int; height: int
                     negative_prompt: str = ""; seed: int | None = None; transparent: bool = False
@dataclass
class ImageResult:   data: bytes; mime: str; meta: dict

class ImageProvider(Protocol):
    name: str
    def available(self) -> tuple[bool, str]: ...
    def generate(self, req: ImageRequest) -> ImageResult: ...
```

`image-provider.json` wählt den aktiven Dienst (auch pro Bildart) und legt
zentrale Stilvorgaben fest. Dienste sind einzelne Dateien unter
`display/image_providers/` (eigene Dienste: `<data-root>/providers/`).
Umgesetzt sind `dummy` (Tests, ohne API) und `gemini`. Freigestellte Icons
werden nicht benötigt.

**Modellwahl (Stand 2026-09, bei Schritt 9 geprüft):** `gemini-2.5-flash-image`
ist inzwischen als veraltet markiert. Standard ist `gemini-3.1-flash-lite-image`
(~0,034 $ pro 1K-Bild, kein Gratis-Kontingent für Bilder) über den
`interactions`-Endpunkt; `gemini-3.1-flash-image` (~0,045–0,067 $) und der
klassische `generateContent`-Weg sind per Konfiguration wählbar. Bilder werden
mit Pillow (falls installiert) auf 512 px verkleinert.

**Gemini-Key einrichten:** aistudio.google.com → API-Key erstellen →
Abrechnung im Projekt aktivieren (Budget-Alarm setzen) → `setx GEMINI_API_KEY "…"`
(oder `DND_IMAGE_KEY`, oder Datei `~/.config/claude-dnd/image.key` bzw.
`tts.key`). Derselbe Key aktiviert auch die Sprachausgabe. Prüfen mit
`python skills/dnd/scripts/assets.py providers`.

---

## 4. Grid und Szenenzustände

Nur auf dem großen Display, **nie auf Handys** (Handys verbinden sich mit
`/stream?character=…` und ignorieren Karte und Szenenzustand).

### Szenenzustand

```json
{"scene_state": {
  "mode": "travel_event",          // stationary | travel | travel_event
  "location": null,
  "map_id": "event-wolfsrudel-1",
  "travel": {"from":"Ashveil","to":"Dornfeld","via":"Königsstraße","day":2,"days_total":4,"terrain":"forest"},
  "event":  {"id":"wolfsrudel-1","title":"Wolfsrudel am Waldrand","started":"…","map_is_temporary":true}
}}
```

Erlaubte Wechsel (der Server lehnt andere ab):

```
stationary ──travel-start──▶ travel ──event-start──▶ travel_event
     ▲                        │  ▲                        │
     │                        │  └──────event-end─────────┘
     └──────travel-end────────┘
stationary ──scene-set──▶ stationary   (Ortswechsel ohne Reise)
```

Kampf ist kein eigener Zustand; er findet auf der Karte der aktuellen Szene statt.

### Anzeigeregel (`display_mode: auto | scene | grid`, bei `auto`)

| Zustand | Anzeige |
|---|---|
| `stationary` mit Karte | Raster mit Figuren |
| `stationary` ohne Karte | Farbverlauf wie bisher |
| `travel` | Farbverlauf nach Gelände + Reise-Banner, keine Karte |
| `travel_event` | Raster mit Ereignis-Karte + Ereignis-Banner |

### Karten

```json
{"id":"kessel-schankraum","template":"tavern-small","tags":["tavern","small"],
 "cols":14,"rows":10,"cell_ft":5,
 "background":{"asset":"map:kessel-schankraum"},
 "terrain":[{"x":5,"y":4,"w":3,"h":1,"type":"table"}],
 "tokens":[{"id":"flerb","name":"Flerb","kind":"pc","x":2,"y":5,"asset":"token:flerb"}]}
```

- Teil-Updates: `{"map_patch":{"map_id":"…","move":[…],"add":[…],"remove":[…]}}`.
- Koordinaten für den DM in Schach-Notation (`D5`).
- **Aufbau** (Raster, Hindernisse, Bild) liegt in `maps/library/` und wird
  kampagnenübergreifend wiederverwendet; **Belegung** (Figuren) pro Kampagne.
- **Vorlagen** in `map-templates/` mit `spawn`-Feldern für Gruppe, Gegner, NSC
  (`tavern-small`, `market-square`, `forest-road`, `forest-clearing`,
  `mountain-pass`, `bridge`, `camp`, `cave-mouth`, `dungeon-corridor`, `ruins`).
  Vorlagen verweisen als Hintergrund auf die generischen Seed-Karten
  (`"background": {"asset": "map:taverne"}`).
- `/dm:dnd maps <list|reset|restore>`: `reset <id>`, `--tag tavern`, `--all`,
  `--keep-image`. Reset archiviert, löscht nie endgültig.

### Befehle

`push_stats.py`/`send.py`: `--scene-set`, `--stat-move "Flerb:D5"`,
`--token-add`, `--token-remove`.

Reisen laufen über `scripts/travel.py`:

- `start --to Dornfeld --days 4 --terrain forest`
- `day` — stellt die Zeit vor und würfelt automatisch die Ereignis-Probe
  (`travel_event_chance` in `state.md`, Standard 15 % pro Reisetag; Ereignisarten
  aus `travel-events.json`, Rückfall `oracle.py event`). Bei Ereignis wird
  `travel_event` mit Karte gesetzt.
- `event --force`, `event-end`, `arrive --location Dornfeld`

---

## 5. Umsetzungsschritte

Jeder Schritt ein eigener PR mit Tests.

**Phase A – Grundlagen**
1. `config_loader.py` (zwei Ebenen, Zusammenführen, Prüfung, Neuladen).
2. `triggers.py` aus `audio.py` herauslösen, ohne Verhaltensänderung; `*`-Muster.
3. Frontend-Verteiler und `/static`-Route, ohne sichtbare Änderung.

**Phase B – Aktions-Einblendungen**
4. `vfx.py`, VFX-Konfigurationen, `send.py --vfx` (ohne Anzeige).
5. `vfx.js` mit Standard-Icons, Schalter, `prefers-reduced-motion`.

**Phase C – Assets**
6. Ordner, Manifest, `/assets`-Route, `AssetResolver`, Platzhalter, Inventar mit Icons.
7. Kategorie-Erkennung und Warteliste (Inventar).
8. Bilddienst-Schnittstelle, `dummy`-Dienst, `assets.py`, `/dm:dnd assets`.
9. Gemini-Dienst, Stilvorgaben, Nachbearbeitung.
9b. Standardkatalog `asset-seed.json` und `assets.py seed` (SRD-Ausrüstung,
    Archetyp-Portraits, Szenen-Hintergründe), generische Schlüssel mit Vorrang
    für spezifische Bilder, `prompt --add`.

**Phase D – Grid und Szenenzustände**
10. Kartenmodell, Teil-Updates, Schach-Koordinaten, `--stat-move`, `--token-*`, Speicherung.
11. Kartenvorlagen und Platzierung auf `spawn`-Feldern; Hintergrund aus der Seed-Karte der Vorlage.
12. `scene_state`-Zustandsmaschine, Bibliothek/Belegung getrennt, `travel.py` mit automatischer Ereignis-Probe.
12b. `/dm:dnd maps list|reset|restore`.
13. `grid.js`: Raster, Figuren mit Initialen, animierte Bewegung.
14. `scene-mode.js`: Anzeigeregel, Reise- und Ereignis-Banner; nicht auf Handys.
15. Figuren über `AssetResolver`, Markierung der Figur am Zug, Kartenbilder, Warteliste für Figuren/Karten.
    Figuren ohne eigenes Bild können über `archetype` (z. B. `token:zwergischer-kleriker`) auf ein generisches Portrait zurückfallen.
16. `SKILL.md`/`SKILL-commands.md`: wann `--scene-set`, `travel.py start|day|arrive`, `event-end`; `combat start` legt nur bei Bedarf eine Karte an. Testsitzung.
17. *(Optional)* Einblendungen über der Figur der handelnden Person.
