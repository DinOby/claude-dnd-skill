# Plan: Grid-Ansicht, Aktions-Einblendungen, Asset-System

Stand: 2026-09-27 · Status: Phase A–C abgeschlossen (Schritte 1–9b), Phase D: Schritte 10–17 fertig (Testsitzung mit echten Spielern offen)

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
`scripts/assets.py seed [--category items|portraits|maps|sprites] [--limit N] [--provider X] [--dry-run]`
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

Voller Katalog ≈ 119 Bilder ≈ 4 $ mit dem Standardmodell (seit Schritt 15: `maps` = Bodenbeläge, dazu 24 `sprites`); `--dry-run` zeigt
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

Umgesetzt in Schritt 12 (`display/scene_state.py`, `scripts/travel.py`):

- Gespeichert in `<kampagne>/scene-state.json` (mit Sperrdatei); `travel.py`
  schreibt direkt und funktioniert ohne Display, danach `POST /scene {"sync": true}`.
- Zusätzlicher Übergang `travel-day` (travel → travel). Unbekannte Parameter
  und unerlaubte Wechsel werden mit Hinweis abgelehnt („erst ankommen“).
- Der Server gleicht die Karte an: `travel` → keine Karte (Kartenbefehle 409),
  `travel_event` → Ereignis-Karte aus der Vorlage des Ereignisses mit Gruppe
  und Gegnern, `temporary` (nicht in der Bibliothek, beim Ausblenden
  verworfen); `stationary` → Karte der Szene. `--scene-set` räumt die alte
  Karte ab; eine im Stillstand gezeigte Karte wird zur Karte der Szene.
- Beim Start und Kampagnenwechsel werden nur die Reisezustände durchgesetzt,
  eine geladene Karte im Stillstand bleibt.
- Ereignis-id `<ereignis>-d<tag>`, Karten-id `event-<id>`.

### Anzeigeregel (`display_mode: auto | scene | grid`, bei `auto`)

| Zustand | Anzeige |
|---|---|
| `stationary` mit Karte | Raster mit Figuren |
| `stationary` ohne Karte | Farbverlauf wie bisher |
| `travel` | Farbverlauf nach Gelände + Reise-Banner, keine Karte |
| `travel_event` | Raster mit Ereignis-Karte + Ereignis-Banner |

Umgesetzt in Schritt 13/14: Die Karte liegt oben zwischen Seitenleiste und
Einstellungsspalte, die Erzählung läuft darunter weiter (`body.grid-on`).
`display_mode` ist die Einstellung „Map View“ (Auto / Scene / Grid, pro
Browser). Reise-Hintergrund nach Gelände über die bestehenden Szenen
(`TERRAIN_SCENES`, neue Szene „The Road“ ohne Stichwörter). Banner-Texte der
Oberfläche sind Englisch, Orts- und Ereignisnamen bleiben wie angegeben.
Patches mit passender `rev` werden im Browser angewendet, sonst `GET /map`.

### Karten

```json
{"id":"kessel-schankraum","template":"tavern-small","tags":["tavern","small"],
 "cols":14,"rows":10,"cell_ft":5,
 "background":{"asset":"map:kessel-schankraum"},
 "terrain":[{"x":5,"y":4,"w":3,"h":1,"type":"table"}],
 "tokens":[{"id":"flerb","name":"Flerb","kind":"pc","x":2,"y":5,"asset":"token:flerb"}]}
```

- Teil-Updates: `{"map_patch":{"map_id":"…","move":[…],"add":[…],"remove":[…]}}`.
  Alles oder nichts: ein fehlerhafter Teil lehnt den ganzen Patch mit allen
  Gründen ab; Auffälliges (Figur auf Wand, zwei Figuren auf einem Feld) kommt
  als Warnung zurück. Jede Änderung erhöht `rev`; der Browser erhält den
  Patch mit `base`/`rev` und holt bei Lücken `GET /map` neu.
- Koordinaten für den DM in Schach-Notation (`D5`), `A1` = oben links,
  nach `Z` folgen `AA`, `AB`; in JSON 0-basiert `x`/`y`, überall auch `"at": "D5"`.
- Figuren: `kind` `pc | npc | enemy | object`, `size` 1–4 Felder; `id` aus dem
  Namen (`goblin`, `goblin-2`), Befehle akzeptieren Name oder id.
- Server: `grid_map.py` (Modell, Patches, `MapStore`), Route `/map`
  (`map | show | patch | hide`), SSE nur an den Hauptbildschirm.
- **Aufbau** (Raster, Hindernisse, Bild) liegt in `<data-root>/maps/library/` und wird
  kampagnenübergreifend wiederverwendet; **Belegung** (Figuren) pro Kampagne in
  `<kampagne>/maps/<id>.json`, die gezeigte Karte in `<kampagne>/maps/active.json`
  (ohne Kampagne im Runtime-Ordner). Umgesetzt in Schritt 10.
- **Vorlagen** in `map-templates/` mit `spawn`-Feldern für Gruppe, Gegner, NSC
  (`tavern-small`, `market-square`, `forest-road`, `forest-clearing`,
  `mountain-pass`, `bridge`, `camp`, `cave-mouth`, `dungeon-corridor`, `ruins`).
  Vorlagen verweisen als Hintergrund auf die generischen Seed-Karten
  (`"background": {"asset": "map:taverne"}`). Umgesetzt in Schritt 11:
  `display/map-templates/<id>.json`, eigene oder ersetzende in
  `<data-root>/map-templates/`; `spawn` ist `{"pc"|"enemy"|"npc": [Bereiche]}`
  (Objekte nutzen die NSC-Zone) und wird mit dem Aufbau gespeichert.
- **Platzierung:** Figuren ohne Position kommen auf das erste freie Feld ihrer
  Zone (Lesereihenfolge, größenbewusst, nicht auf Terrain außer `difficult`,
  `door`, `stairs`, `road`, `rug`, `bridge`, `shallow-water`), bei voller Zone
  auf das nächste freie Feld daneben, ohne Zonen nahe der Mitte.
  `--map-new TEMPLATE --map-id ID` legt eine Karte an — oder zeigt den
  gespeicherten Aufbau, wenn die Bibliothek die id schon hat.
  `--token-party` stellt alle Spielercharaktere auf, die noch fehlen.
- `/dm:dnd maps <list|reset|restore>`: `reset <id>`, `--tag tavern`, `--all`,
  `--keep-image`. Reset archiviert, löscht nie endgültig. Umgesetzt in 12b
  (`scripts/maps.py`): Archiv `<data-root>/maps/archive/<id>/<zeitstempel>/`
  mit Aufbau und ggf. eigenem Bild `map:<id>`; generische Seed-Bilder und die
  Belegung der Kampagnen bleiben; `restore` archiviert vorher den aktuellen Stand.

### Befehle

`push_stats.py`: `--map-set JSON|@datei`, `--map-show ID`, `--map-hide`,
`--stat-move "Flerb:D5"`, `--token-add "Goblin 2:E7:enemy"` (oder JSON),
`--token-remove NAME` (alle wiederholbar, ein Aufruf = ein Patch);
`--scene-set` folgt mit Schritt 12.

Reisen laufen über `scripts/travel.py`:

- `start --to Dornfeld --days 4 --terrain forest`
- `day` — stellt die Zeit vor und würfelt automatisch die Ereignis-Probe
  (`travel_event_chance` in `state.md`, Standard 15 % pro Reisetag; Ereignisarten
  aus `travel-events.json`, Rückfall `oracle.py event`). Bei Ereignis wird
  `travel_event` mit Karte gesetzt.
- `event [--id ID | --title T --template TPL]` (erzwingt ein Ereignis), `event-end`,
  `arrive [--location Dornfeld]`, `status`

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
12. `scene_state`-Zustandsmaschine, `travel.py` mit automatischer Ereignis-Probe
    (Trennung Bibliothek/Belegung ist schon mit Schritt 10 umgesetzt).
12b. `/dm:dnd maps list|reset|restore`.
13. `grid.js`: Raster, Figuren mit Initialen, animierte Bewegung.
14. `scene-mode.js`: Anzeigeregel, Reise- und Ereignis-Banner; nicht auf Handys.
15. Bilder auf der Karte (Entscheidung 2026-09-27 nach Durchsicht der Rasteransicht):
    - **Kein Gesamtbild pro Karte** — ein KI-Bild setzt Möbel nicht auf die Hindernis-Felder.
      Stattdessen **Bodenbelag** (der Hintergrund `map:<szene>` der Vorlage, gekachelt) und
      **Einzelbilder pro Terrain-Typ** (`sprite:table`, `sprite:chair`, `sprite:bar` …), die genau
      auf ihren Feldern liegen; Möbel gestreckt, Natur (Bäume, Felsen) und Flächen (Wand, Wasser) gekachelt.
    - Sprites **freigestellt**: erzeugt vor magentafarbener Fläche, Pillow schneidet sie heraus
      (Pillow damit Pflicht; ohne Pillow werden Sprites nicht erzeugt). Neue Bildart `sprite`.
    - Neuer Terrain-Typ `chair` (Stühle an den Tischen der Taverne).
    - **Figuren als rundes Porträt mit Farbring** nach Art: eigenes Bild → Name ohne Nummer
      (`Goblin 2` → `token:goblin`) → `archetype` → Scheibe mit Initialen.
    - Der Server ergänzt beim Senden die Bild-URLs (`images`), das Kartenmodell bleibt ohne URLs.
    - Warteliste: Figuren und unbekannte Terrain-Typen ohne Bild; Spielercharaktere mit Volk/Klasse als Hinweis.
    - Markierung der Figur am Zug (`turn_order.current`).
16. `SKILL.md`/`SKILL-commands.md`: wann `--scene-set`, `travel.py start|day|arrive`, `event-end`; `combat start` legt nur bei Bedarf eine Karte an. Testsitzung.
    Umgesetzt: Abschnitt *Scene, journeys and the battle map* in `SKILL.md` (Karte nur, wenn
    Positionen zählen; Reisetag enthält die Nacht, kein doppeltes Vorstellen der Uhr; Figuren
    heißen wie in der Initiative), Kampfabfolge mit `--stat-move`; `/dm:dnd load` liest
    `travel.py status`, `combat start` nutzt eine vorhandene Karte oder legt nur bei Bedarf eine an,
    `/dm:dnd end` bietet die Bilderzeugung an. Ereignis-Gegner werden nummeriert (`Wolf 1` …),
    die Zug-Markierung erkennt auch die id. Befehlsfolge als Skript-Testlauf gegen das Display
    geprüft; eine Sitzung mit echten Spielern steht noch aus.
17. *(Optional)* Einblendungen über der Figur der handelnden Person.
    Umgesetzt: `grid.js` setzt `VfxOverlay.anchorFor`; eine Einblendung mit Namen
    (`--vfx attack:Goblin 1`) erscheint bei sichtbarer Karte verkleinert über der Figur
    (Größe nach Figur, Beschriftung darüber), sonst wie bisher in der Mitte. Namen werden wie bei
    `--stat-move` aufgelöst (Name, id, `Goblin 2` ↔ `goblin-2`); verdeckte Figuren nie.
