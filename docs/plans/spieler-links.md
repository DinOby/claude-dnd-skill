# Plan: Persönliche Spieler-Links

Stand: 13.11.2026 · Status: **nur Plan, noch kein Code** · Fragen des Hosts beantwortet (13.11.2026)

## Ziel

Jeder Spieler bekommt einen eigenen Link mit eigenem Schlüssel, und dieser Schlüssel gehört fest zu seiner Figur. Geheimnachrichten, geheime Aktionen und Würfelanfragen gehen nur an Geräte mit dem passenden Schlüssel. Kein Spieler kann sich eine andere Figur aussuchen. Nur der Host (localhost) erzeugt Links, widerruft sie und ändert die Zuordnung.

## Ist-Zustand und warum er nicht reicht

- Alle Spieler teilen sich **einen** Spieler-Schlüssel (`.player_token`). Er öffnet die Spieler-Schnittstellen, sagt aber nicht, *wer* da kommt.
- Die Figur nennt das Gerät selbst: `/stream?as=…` („Playing as“) oder `?char=…` (Handy). Der Server glaubt das.
- Aktionen, Geheim-Aktionen und Würfe nennen die Figur im Body (`"character": "…"`). Der Server prüft nur, ob es die Figur gibt.
- Folge in Sitzung 4: Ein Gerät stand versehentlich auf einer fremden Figur und hat deren Geheimnachrichten bekommen. Absichtlich ginge das genauso. Die Warnung `/play-as/conflicts` (S4) macht das nur sichtbar, verhindert es aber nicht.

## Entwurf

### Schlüssel und Ablage
- Pro Spieler ein Zufallsschlüssel (32 Byte, `secrets.token_urlsafe`). Gespeichert wird nur ein **Hash** (SHA-256) in `<data-root>/.runtime/player-links.json`: `{id, character, key_hash, created, revoked, last_seen}`. Der Schlüssel selbst wird nur einmal beim Erzeugen angezeigt.
- Link-Form: `http://<tailscale-adresse>:5001/#k=<schlüssel>`. Der Schlüssel steht im **Fragment**, also hinter dem `#`. Fragmente schickt der Browser nie an den Server, deshalb landet der Schlüssel weder im Server-Log noch im Referer. Die Seite liest ihn einmal aus, legt ihn in `localStorage` ab und entfernt ihn aus der Adresszeile.

### Server: Die Figur kommt immer vom Schlüssel
- Jede Spieler-Anfrage schickt `X-DND-Player-Key`. Der Server sucht den Hash und kennt damit die Figur.
- `stage`, `secret`, `dice`, `ready` und `skip` nehmen die Figur **vom Schlüssel**. Steht im Body eine andere Figur, gibt es 403, und nichts wird geschrieben.
- **Stream:** `EventSource` kann keine eigenen Header setzen. Deshalb holt die Seite zuerst mit Header `POST /stream-ticket` ein Einmal-Ticket (30 s gültig) und verbindet dann `/stream?ticket=…`. Der Server bindet den Stream an die Figur des Schlüssels; `?as=` und `?char=` werden für Spieler ignoriert.
- Würfelanfragen an Figur A gehen nur an Streams von A. Ein Wurf auf eine Anfrage von A mit dem Schlüssel von B ergibt 403.
- **Widerruf:** Der Schlüssel ist sofort ungültig, offene Streams dieses Schlüssels werden geschlossen.

### Host-Verwaltung (nur localhost + Host-Schlüssel, wie `_host_ok`)
- `POST /player-links` `{character}` liefert den Link (einmalig mit Schlüssel)
- `GET /player-links` liefert die Liste **ohne** Schlüssel (Figur, erstellt, zuletzt gesehen, widerrufen)
- `DELETE /player-links/<id>` widerruft
- `PATCH /player-links/<id>` `{character}` ändert die Zuordnung (offene Streams werden neu gebunden)
- Kommandozeile für den DM: `display/player_links.py create|list|revoke|reassign`

### Oberfläche
- Spieler-Seiten: Die Auswahl „Playing as“ entfällt. Stattdessen steht fest „Du spielst: Mordos Feuerzunge“. Die Party-Eingabe hat nur diesen einen Namen.
- Host-Seite (localhost): bleibt wie heute (alle Tabs, „Everybody“), dazu ein Bereich „Spieler-Links“ zum Erzeugen, Kopieren und Widerrufen.
- Handys: **keine persönlichen Links** (Entscheidung des Hosts, 13.11.2026). Gespielt wird nur am PC. `?char=` ohne Schlüssel bringt nichts mehr; die Handy-Ansicht bleibt höchstens als reine Leseansicht ohne Eingaben, Würfe und Geheimnachrichten.

### Übergang
- Schalter `player_links: an|aus` in den Session Flags. Bei `an` öffnet der gemeinsame Spieler-Schlüssel nur noch die öffentliche Ansicht (Text, Karte), keine Eingaben, Würfe oder Geheimnachrichten.
- Die Gerätefreigabe (`_device_ok`) bleibt als zweite Schicht bestehen.

## Bedrohungen und Antworten

| Versuch | Ergebnis |
|---|---|
| Spieler ändert `?as=`, `localStorage` oder den Body auf fremde Figur | wird ignoriert bzw. 403; Figur kommt nur vom Schlüssel |
| Spieler rät einen Schlüssel | praktisch unmöglich (256 Bit) |
| Link wird weitergegeben oder geleakt | Host widerruft, neuer Link |
| Spieler ruft Verwaltungs-Routen auf | 403 (nicht localhost / kein Host-Schlüssel) |
| Schlüssel in Logs | steht im Fragment und kommt nie beim Server an; gespeichert nur als Hash |

## Tests (müssen vor dem Einschalten grün sein)

1. **Absichtlich fremde Geheimnachricht:** Spieler B verbindet mit `?as=A` und seinem eigenen Schlüssel. Eine Nachricht `/private` an A erreicht B nicht; B's Queue bleibt leer.
2. Geheim-Aktion mit Schlüssel B und `"character": "A"` ergibt 403, nichts in der Warteschlange.
3. Würfelanfrage an A erscheint nicht im Stream von B; ein Wurf von B auf A's `request_id` ergibt 403.
4. Party-Aktion mit Schlüssel B und Figur A ergibt 403; mit eigener Figur 204.
5. Widerrufener Schlüssel: 403 überall, offener Stream wird geschlossen.
6. Umzuordnen schaltet sofort um: alte Figur bekommt nichts mehr, neue schon.
7. Verwaltungs-Routen: von außen (nicht loopback) und mit Spieler-Schlüssel 403; die Liste enthält nie Schlüssel; in der Datei steht nur der Hash.
8. Stream-Ticket: nur einmal gültig, verfällt nach 30 s, gehört zu genau einem Schlüssel.
9. Gemeinsamer Spieler-Schlüssel bei `player_links: an`: nur Lesen, keine Eingaben, keine Geheimnachrichten.
10. Host auf localhost sieht weiterhin alles und kann für alle eingeben.

## Arbeitspakete

1. Ablage und Kommandozeile (`player_links.py`), Hash und Widerruf, mit Tests 7.
2. Server: Schlüssel-Auflösung, Stream-Ticket, Figur vom Schlüssel, mit Tests 1 bis 6 und 8.
3. Oberfläche: Link-Aufnahme aus dem Fragment, „Du spielst: …“, Host-Bereich, Playing-as entfernen.
4. Übergang und Schalter, mit Tests 9 und 10; Probelauf mit drei Geräten über Tailscale.

## Entscheidungen des Hosts (13.11.2026)

- **Nur PCs, keine Handys.** Persönliche Links gibt es nur für Rechner.
- **Verteilung:** Der Host schickt jedem Spieler seinen Link selbst per privater Nachricht. Das System verschickt nichts; `player_links.py create` zeigt den Link einmal im Terminal des Hosts an.
- **Ein Gerät pro Spieler.** Ein Schlüssel bindet sich beim ersten Verbinden an genau ein Gerät (Geräte-Kennung aus der bestehenden Gerätefreigabe). Ein zweites Gerät mit demselben Link bekommt 403, und der Host sieht eine Warnung. Der Host kann einen Link **jederzeit widerrufen und neu ausstellen** (`player_links.py revoke` + `create`, oder `reissue` in einem Schritt); der alte Schlüssel ist sofort ungültig, offene Streams werden geschlossen.
- Zusätzlicher Test 11: Zweites Gerät mit demselben Schlüssel ergibt 403; nach `reissue` funktioniert nur noch der neue Link.
