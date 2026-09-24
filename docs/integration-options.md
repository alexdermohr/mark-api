# Integrationsoptionen für mark-api

Stand: 24.09.2026

Status: **credential-frei praktisch verifiziert; Architekturentscheidung noch gesperrt**

Ausführliche Prüfprotokolle, Testresultate und Codepfade: `docs/poc-2026-09-24.md`.

## Ziel

Für Marks Kleinanzeigen-Account soll der kleinste belastbare Integrationsweg gefunden werden für:

- Anzeigen erstellen, synchronisieren, ändern und löschen,
- Pause/Aktivierung,
- Views, Merker und Replies,
- Inbox/Conversations mit Anzeigenzuordnung,
- darauf aufbauende Reaktionsmetriken und Dashboard-Auswertung.

Externe Text- und Bildgenerierung sind kein MVP-Schwerpunkt.

## Evidenzregel

Eine Funktion gilt hier nicht allein wegen README, UI-Button oder Methodennamen als unterstützt.

Wir unterscheiden:

- **statisch belegt:** konkreter Codepfad vorhanden,
- **offline getestet:** automatisierter Test ohne echten Account,
- **lokal gestartet:** Anwendung/Route praktisch gestartet,
- **remote belegt:** echte Plattformwirkung plus Readback.

Der letzte Punkt ist noch offen.

## Frisch bestätigte Kandidaten

| Kandidat | aktueller Remote-HEAD am 24.09.2026 | Lizenz |
|---|---|---|
| `bkd3sign/kleinanzeigen-bot-ui` | `845988fd1e79c6ce5a96f7b284b1f10e695b1159` | AGPL-3.0 |
| `Second-Hand-Friends/kleinanzeigen-bot` | `7f5c59b9e068d83dab74ff925a1b02d9f036be1c` | AGPL-3.0-or-later |
| `monkrel/kleinanzeigen-api` | `efb2d82bf449c38a49558b8c71df8d888effbfd9` | MIT |

Alle drei lokalen Checkouts waren bei der Verifikation clean und entsprachen per `git ls-remote origin HEAD` dem jeweiligen Remote-HEAD.

## Kandidat A — kleinanzeigen-bot-ui

### Praktisch belegt

- Produktionsbuild erfolgreich.
- Anwendung mit leerem, separatem `BOT_DIR` ohne Kleinanzeigen-Zugangsdaten startbar.
- Health-Endpoint meldet korrekt `setup_required: true`.
- Login-, Setup-, Dashboard-, Ads- und Messages-Routen werden lokal ausgeliefert.
- geschützte Messages-/Stats-APIs liefern ohne Setup 401 statt scheinbar leerer Plattformdaten.
- beim ersten Start wird im Datenverzeichnis eine deaktivierte Default-`schedules.yaml` angelegt.

### Anzeigenverwaltung

Das UI besitzt echte Bot-Routen für Publish, Update, Delete, Download und Extend.

Wichtige Einschränkung:

**Der UI-Menüpunkt „Aktivieren/Deaktivieren“ ist im geprüften Stand nur lokal.**

Er ändert das YAML-Feld `active` und verschiebt den Anzeigenordner ins/aus dem Archiv. Er pausiert die Anzeige nicht nachweislich auf Kleinanzeigen.

### Statistik

Der UI-Stats-Pfad liest `m-meine-anzeigen-verwalten.json` und modelliert:

- `viewCount`,
- `watchCount`,
- `replies`,
- `state`,
- Aktivierungs- und Ablaufdatum.

Das passt fachlich sehr gut zu Marks Analytics-Ziel.

Aktuelle Schwäche: fehlende Session, HTTP-Fehler, Exceptions und echte leere Anzeigenlisten können alle zu `[]` werden; fehlende Zähler werden zu `0`. Das muss vor produktiver Statistiknutzung getrennt werden.

Zusätzlich bleibt nach dem Löschen der letzten Anzeige derzeit potenziell ein verwaister `.ad-stats.json`-Datensatz bestehen.

### Messaging

Conversation-Daten enthalten unter anderem:

- `adId`,
- Käufer-/Verkäufer-ID,
- Richtung,
- ungelesene Nachrichten,
- Zeitstempel,
- Anzeigentitel und Anzeigenstatus.

Conversation lesen und Nachricht senden sind als konkrete Gateway-HTTP-Pfade implementiert.

Damit ist die technische Grundlage für `Conversation → adId → Anzeige` vorhanden; der reale Nachrichtentest steht noch aus.

## Kandidat A2 — underlying kleinanzeigen-bot

Der Bot ist nicht nur Implementierungsdetail des UI, sondern ein eigenständiger wichtiger Kandidat für einzelne Remote-Aktionen.

### Praktische Verifikation

Isolierte Python-3.12.12-Venv, Host-Python unverändert.

Nach Ergänzung zweier im aktuellen Packaging nicht deklarierter Testabhängigkeiten:

- Smoke: **19/19 bestanden**.
- Unit seriell: **1630 bestanden, 4 übersprungen, 2 fehlgeschlagen**.
- Die zwei Fehler sind dieselbe `psutil`-/`/proc`-Race in Browserdiagnostik.

Packaging-Lücken im geprüften Stand:

- Runtime-Code importiert `requests`, Runtime-Dependencies deklarieren es nicht.
- Smoke-Test importiert `ruyaml`, Dev-/Test-Dependencies deklarieren es nicht.

### Remote-Semantik

Neben publish/update/delete/download/extend existieren inzwischen explizite Befehle:

- `reserve`
- `activate`

`reserve_flow.py` klickt im Manage-Ads-Bereich „Reservieren“ bzw. „Aktivieren“ und liest anschließend die Management-API erneut. Erfolg wird nur gemeldet, wenn der Plattformstatus `paused` bzw. `active` zurückkommt.

Das ist der derzeit konkretste Codepfad für echten Pause/Aktivieren-Readback. Er muss mit der Testanzeige noch praktisch gegen Kleinanzeigen ausgeführt werden.

## Kandidat B — monkrel/kleinanzeigen-api

### Praktisch belegt

Separate Python-3.13.9-Venv:

- Installation erfolgreich,
- vollständige Suite: **71/71 Tests bestanden**,
- CLI startbar,
- PKCE/Login-URL und Nicht-eingeloggt-Verhalten offline getestet.

### Statisch vorhandene Account-Funktionen

Konkrete private/mobile API-Pfade existieren für:

- eigene Anzeigen lesen,
- einzelne eigene Anzeige lesen,
- Pause,
- Aktivieren,
- Delete,
- Extend,
- neue Anzeige posten,
- Conversations lesen,
- Nachrichten lesen,
- Antworten senden.

Conversations enthalten `ad_id`.

### Harte Lücken für Marks MVP

**Kein in-place Inhaltsupdate gefunden.**

`post_ad()` erstellt eine neue Anzeige und ersetzt keinen Update-Test mit stabiler Anzeigen-ID.

**Verkäuferstatistiken fehlen im aktuellen Modell.**

Der Request fordert zwar `ad-status` an, das `Listing`-Dataclass übernimmt ihn aber nicht. Views, Verkäufer-Merkerzahl und Replies werden ebenfalls nicht modelliert.

`watchlist()` bezeichnet die vom eingeloggten Nutzer gespeicherten Anzeigen und ist nicht die Merkerzahl der eigenen Anzeige.

## Capability-Matrix

Legende: **S** statisch, **T** offline getestet, **L** lokal gestartet, **R** echter Remote-Readback, **—** nicht gefunden.

| Fähigkeit | bot-ui | underlying bot | monkrel API | Remote belegt? |
|---|---|---|---|---|
| eigene Anzeigen synchronisieren | S | S/T | S | nein |
| bestehende Anzeige inhaltlich ändern | S über Bot | S/T | — | nein |
| neue Anzeige erstellen | S | S/T | S/T Payload | nein |
| pausieren | UI-Menü nur lokal | S/T | S | nein |
| aktivieren | UI-Menü nur lokal | S/T | S | nein |
| Views lesen | S | Roh-Manage-Ads | — | nein |
| Merkerzahl lesen | S | Roh-Manage-Ads | — | nein |
| Replies lesen | S | Roh-Manage-Ads | — | nein |
| Inbox lesen | S | — | S/T Parser | nein |
| Conversation → Anzeige | S über `adId` | — | S/T über `ad_id` | nein |
| Nachricht senden | S | — | S | nein |
| Delete | S über Bot | S/T | S | nein |
| Extend | S über Bot | S/T | S | nein |
| Dashboard | L | — | — | lokal |
| Statistik-Historie | S | — | — | lokal |
| manueller Login/MFA | S | S/T | PKCE S/T | nein |

## Vergleich nach Betriebsmerkmalen

### Technische Abdeckung

- **bot-ui + Bot:** breiteste Gesamtabdeckung, insbesondere Analytics + UI + bestehendes Inhaltsupdate.
- **monkrel:** schlank für API-Aktionen und Messaging, aber ohne Inhaltsupdate und Verkäuferstatistiken nicht allein MVP-komplett.

### Fehlertransparenz

- UI-Stats aktuell problematisch, weil Fehler und echte Leere zusammenfallen können.
- Underlying Bot kann bei ownership-kritischen Manage-Ads-Abrufen mit `strict=True` fail-closed arbeiten.
- monkrel nutzt überwiegend explizite Exceptions, reale Netzfehler müssen aber noch am Testaccount bewertet werden.

### Browserabhängigkeit

- bot-ui/underlying Bot: für mehrere Kernaktionen Browser-Automation.
- monkrel: viele Aktionen per privater/mobile HTTP-API ohne Browser nach Authentifizierung.

### Wartungsrisiko

- Browserpfade können durch Web-UI-Änderungen brechen.
- private/mobile API kann durch App-/Backend-Änderungen brechen.
- beide inoffiziellen Pfade tragen Account-/ToS-Risiko.

### Lizenzfolgen

- UI und underlying Bot sind AGPL-lizenziert.
- monkrel ist MIT-lizenziert.

Vor Übernahme von Code muss deshalb konkret unterschieden werden zwischen bloßem externen Aufruf/Adapter und tatsächlicher Codeübernahme bzw. abgeleitetem Werk. Dieser PoC trifft keine Rechtsberatung oder endgültige Lizenzentscheidung.

## Offizieller ProSellers-Pfad

Die offizielle API bleibt technisch relevant, ist für den dokumentierten privaten Account aber weiterhin kein vollständig belegter MVP-Pfad. Zusätzlich sind die für Marks Dashboard entscheidenden Views-/Inbox-Funktionen dort in der bislang geprüften öffentlichen Dokumentation nicht als entsprechendes Komplettpaket belegt.

## Was jetzt nicht mehr sinnvoll ist

Keine weitere breite Tool-/Marktrecherche vor dem echten Test.

Die offenen Fragen sind jetzt konkret und experimentell:

1. Kann dieselbe Testanzeige mit stabiler ID synchronisiert und geändert werden?
2. Funktioniert Reserve/Pause und Aktivierung tatsächlich aktuell auf Kleinanzeigen?
3. Stimmen `viewCount`, `watchCount`, `replies` mit der sichtbaren Verkäuferansicht überein?
4. Kommt eine kontrollierte Testnachricht mit korrekter `adId` bei beiden Clients an?
5. Welche Fehlerbilder entstehen bei Sessionablauf und Plattformänderungen?
6. Welche Lösung ist nach diesem realen Ablauf wartbarer?

## Nächstes Gate

Benötigt werden:

- eine explizit freigegebene Kleinanzeigen-Testsitzung,
- genau eine Testanzeige,
- eine bekannte Anzeigen-ID,
- für die Inbox-Prüfung ein kontrollierter Gegenkontakt.

Dann exakt:

`Sync → Update → Pause → Aktivieren → Views/Watchlist → Inbox-Zuordnung → Löschen`

Keine Architekturentscheidung vorher.

## Mögliche Ergebnisse nach dem E2E-Test

Noch gleichberechtigt offen:

1. UI weitgehend übernehmen/forken.
2. Nur UI-/Stats-/Messaging-Komponenten nutzen.
3. monkrel als schlanken API-Unterbau verwenden und gezielt ergänzen.
4. Browser-Bot und API-Client kombinieren.
5. Eigenes dünnes Backend nur dann, wenn Adapter um vorhandene Komponenten nachweislich nicht ausreichen.

Die Auswahl erfolgt erst anhand der realen Readbacks aus dem Ein-Anzeigen-Test.
