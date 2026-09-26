# Integrationsoptionen für mark-api

Stand: 24.09.2026

Status: **Ein-Anzeigen-Realtest abgeschlossen; Architekturentscheidung getroffen**

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

Remote-Evidenz ist für Sync, Inhaltsupdate, Pause/Aktivieren, Verkäufermetriken, positiven Inbox/adId-Fall und Delete erreicht. Die verbleibenden Punkte sind Härtung, nicht mehr Architektur-Sondierung.

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

Der Pfad wurde real gegen Anzeigen-ID `3521676801` geprüft. Ein erster unveränderter Upstream-Lauf scheiterte im XPath-Lookup, obwohl `li[data-adid]` und der sichtbare `Reservieren`-Button im Produktions-DOM vorhanden waren. Ein isolierter CSS-basierter Runtime-Overlay-Patch wurde daraufhin als fail-closed Kompatibilitätsversuch vorbereitet. Der später erfolgreiche `reserve → paused → activate → active`-Lauf verwendete diesen Patch jedoch nachweislich nicht: der Python-Importpfad zeigte weiterhin die unveränderte Upstream-Datei. Der Befund ist deshalb als intermittierende XPath/CDP-/Timing-Robustheitslücke zu werten. Der Overlay-Patch ist nicht in `mark-api` übernommen.

## Kandidat B — monkrel/kleinanzeigen-api

### Praktisch und remote belegt

Separate Python-3.13.9-Venv:

- Installation erfolgreich,
- vollständige Suite: **71/71 Tests bestanden**,
- CLI startbar,
- PKCE/Login-URL und Nicht-eingeloggt-Verhalten offline getestet,
- PKCE-Login im echten Testaccount erfolgreich; Token nur lokal mit Modus 0600,
- `my_ads()` und `get_my_ad()` lesen Testanzeige `3521676801` real,
- `pause_ad()` und `activate_ad()` wurden mit unabhängigem Web-Management-Readback real bestätigt.

Auf diesem Host zeigte `curl_cffi` einen reproduzierbaren DNS-Resolve-Timeout, während normales `curl` denselben Host erreichte. Der bereits von `curl_cffi` unterstützte Session-Parameter `doh_url` stellte den Transport ohne Quellcodeänderung her.

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

Der Request fordert `ad-status` an. Im echten Rohpayload wurden `PENDING`, `ACTIVE` und `PAUSED` tatsächlich beobachtet; das `Listing`-Dataclass übernimmt diesen Status aber nicht. Views, Verkäufer-Merkerzahl und Replies werden ebenfalls nicht modelliert.

`watchlist()` bezeichnet die vom eingeloggten Nutzer gespeicherten Anzeigen und ist nicht die Merkerzahl der eigenen Anzeige.

## Capability-Matrix

Legende: **S** statisch, **T** offline getestet, **L** lokal gestartet, **R** echter Remote-Readback, **—** nicht gefunden.

| Fähigkeit | bot-ui | underlying bot | monkrel API | Remote-Stand |
|---|---|---|---|---|
| eigene Anzeigen synchronisieren | S | **R** | **R** | beide real auf ID 3521676801 |
| bestehende Anzeige inhaltlich ändern | S über Bot | **R** | — | Bot mit stabiler ID real |
| neue Anzeige erstellen | S | S/T | S/T Payload | Kandidaten-Publish nicht separat getestet |
| pausieren | UI-Menü nur lokal | **R** | **R** | Bot einmaliger XPath-Fehler, später unverändert erfolgreich; beide mit Status-Readback |
| aktivieren | UI-Menü nur lokal | **R** | **R** | Bot-Erfolg ohne Overlay-Patch; beide mit Status-Readback |
| Views lesen | S | Roh-Manage-Ads **R** | — im Modell | real, vor Delete zuletzt 15 |
| Merkerzahl lesen | S | Roh-Manage-Ads **R** | — im Modell | real, zuletzt 0 |
| Replies lesen | S | Roh-Manage-Ads **R** | — im Modell | real, vor Delete zuletzt 1 |
| Inbox lesen | **R** | — | **R** | bot-ui und monkrel lesen denselben Conversation-Fall real |
| Conversation → Anzeige | **R** über `adId` | — | **R** über `ad_id` | beide ordnen real ID 3521676801 zu |
| Nachricht senden | S | — | S | nicht automatisch getestet |
| Delete | S über Bot | Readback **R** | **R** | Erst-DELETE über monkrel; beide Besitzerlisten danach ohne ID |
| Extend | S über Bot | S/T | S | nicht Teil des minimalen Kernnachweises |
| Dashboard | L | — | — | lokal |
| Statistik-Historie | S | — | — | lokal |
| manueller Login/MFA | S | **R Session-Reuse** | **R PKCE** | keine Credentials im Repo |
| Statusmodell | S | **R** | Rohpayload **R**, Parser-Lücke | monkrel verwirft vorhandenen Status |

## Vergleich nach Betriebsmerkmalen

### Technische Abdeckung

- **bot-ui + Bot:** breiteste Gesamtabdeckung; Sync und stabiles Inhaltsupdate sind real belegt. Beim Reserve-Lookup trat ein realer intermittierender XPath/CDP-Fehler auf; ein späterer unveränderter Wiederholungslauf funktionierte, sodass ein vorbereiteter CSS-Overlay-Patch für den Erfolg nicht benötigt wurde.
- **monkrel:** Pause/Aktivieren und eigene Anzeigen sind real schlank per API belegt. Ohne Inhaltsupdate und Verkäuferstatistik-Modell ist es allein weiterhin nicht MVP-komplett.

### Fehlertransparenz

- UI-Stats bleiben problematisch, weil Fehler und echte Leere zusammenfallen können.
- Underlying Bot arbeitet bei ownership-kritischen Manage-Ads-Abrufen mit `strict=True` fail-closed; der reale DOM-Lookup zeigte aber Wartungsbedarf.
- monkrel nutzt überwiegend explizite Exceptions. Im Realtest trat ein host-lokaler `curl_cffi`-DNS-Fehler auf, der über einen unterstützten DoH-Sessionparameter isoliert umgangen werden konnte.

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

Keine weitere breite Tool-/Marktrecherche. Der Realtest hat die entscheidenden Unterschiede sichtbar gemacht und die Architektur-Sondierung beendet.

Abschlussbefunde:

1. kontrollierte Testnachricht → Conversation mit korrekter `ad_id=3521676801`,
2. `conversation_count=1`, `unique_buyer_count=1`, `inbound_message_count=1`,
3. Erst-DELETE vorab auf Kandidat B festgelegt und genau einmal ausgeführt,
4. `my_ads()` und Management-Besitzerbestand danach ohne Test-ID,
5. `get_my_ad(id)` und öffentliche Detailseite liefern mindestens drei Minuten weiter alte Detaildaten,
6. unveränderter bot-ui-`fetchAdStats()` lässt nach erfolgreichem leeren Last-Ad-Readback den alten Stats-Eintrag stehen,
7. Kandidat A zeigte beim Pre-Delete-Neusync zusätzlich einen `GIVE_AWAY`-Preisparser-`IndexError`.

## Gate-Stand

Der definierte E2E-Ablauf ist abgeschlossen:

`Sync → Update → Pause/Reserve → Aktivieren → Views/Watchlist/Replies → Inbox/adId → Delete → Besitzerlisten-Abwesenheit → UI-Stats-Nachprüfung`

Die Detail-/Public-URL-Staleness nach Delete ist als eigene Semantik dokumentiert und blockiert die Architekturentscheidung nicht; für Delete gilt der Besitzerbestand als authoritative Readback.

## Architekturentscheidung

Gewählt ist ein **dünnes eigenes Orchestrierungsbackend mit getrennten Adaptern**:

1. monkrel als primärer MIT-Adapter für Auth, eigene Anzeigen, Pause/Aktivieren, Delete und Messaging,
2. eigener Management-Read-Adapter für Besitzerbestand, Status, Views, Merker und Replies mit diskriminierten Fehlerzuständen,
3. `Second-Hand-Friends/kleinanzeigen-bot` als isolierter externer Browser-Adapter nur für Sync/in-place Inhaltsupdate, solange kein stabiler API-Updatepfad belegt ist,
4. kein vollständiges `kleinanzeigen-bot-ui`; Dashboard/API werden gegen das eigene Domänenmodell gebaut.

Nicht gewählt:

- bot-ui + Bot als Gesamtsystem: reale Remote-/Stats-Semantikfehler und unnötige Kopplung,
- monkrel-only: kein belegtes in-place Inhaltsupdate und keine Verkäufermetriken im Modell,
- Browser-Bot-only: reale DOM/CDP- und Preisparser-Robustheitsprobleme.

Authoritative Delete-Semantik: Abwesenheit aus den Besitzerlisten. `get_my_ad(id)` und öffentliche Detail-HTTP-200 sind nach dem realen Delete nachweislich stale.

Details und Adapter-Contracts: `docs/architecture-decision-2026-09-24.md`.

## Update 26.09.2026 — Write-Machbarkeit ist Umsetzungsziel

Die frühere Aussage „kein in-place Inhaltsupdate im aktuellen monkrel-Modell gefunden“ beschreibt nur den damals geprüften Upstream-Client, nicht die technische Plattformfähigkeit.

Aktuelle technische Evidenz zeigt:

- die offizielle ProSellers API besitzt vollständige Listing-/Publication-/Media-Writes,
- aktuelle Mobile-CAPI-Kartierung nennt owner-scoped create/edit/extend/delete,
- die historische API-Spezifikation dokumentiert `PUT /users/{idName}/ads/{adId}`,
- unser eigener PoC hat Pause/Aktivieren/Delete und Browser-in-place-Update bereits remote belegt.

Daraus folgt die neue aktive Richtung: **HTTP-first als Zielarchitektur für eigene Anzeigen. Für bestehende Inhaltsupdates bleibt der remote belegte Browserpfad operativ primär, bis der jeweilige HTTP-Pfad remote bestätigt ist; erst danach wird er zum Fallback.**

Vollständige Evidenzmatrix, Grenzen und Implementierungsreihenfolge: `docs/technical-write-capabilities-2026-09-26.md`.
