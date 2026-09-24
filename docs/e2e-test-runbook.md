# E2E-Test-Runbook Kleinanzeigen

Stand: 24.09.2026

Status: **vorbereitet; wartet auf freigegebene Testsitzung und genau eine Testanzeige**

Dieses Runbook operationalisiert den einzigen noch offenen Plattformtest. Es ersetzt keine Architekturentscheidung.

## Voraussetzungen

Vor dem Start müssen ausdrücklich vorhanden sein:

- eine eigene/dedizierte oder ausdrücklich freigegebene Kleinanzeigen-Testsitzung,
- genau eine als Test bestimmte Anzeige,
- bekannte Anzeigen-ID,
- ein kontrollierter Gegenkontakt für genau eine Testnachricht,
- ein separates Browserprofil/Sessionverzeichnis,
- keine produktiven Mark-Anzeigen im Testscope.

Secrets bleiben ausschließlich lokal außerhalb von Git, Issues und Testprotokollen.

## Kandidaten

A. `bkd3sign/kleinanzeigen-bot-ui` + `Second-Hand-Friends/kleinanzeigen-bot`

B. `monkrel/kleinanzeigen-api`

Beide Kandidaten müssen vor jedem Schritt dieselbe Anzeigen-ID lesen, soweit die jeweilige Fähigkeit vorhanden ist.

## Evidenzregel

Für jeden Schritt werden festgehalten:

| Feld | Inhalt |
|---|---|
| Zeitpunkt | lokaler Zeitstempel des Abrufs |
| Kandidat | A oder B |
| Anzeigen-ID | Plattform-ID |
| Aktion | tatsächlich ausgeführter Aufruf/UI-Pfad |
| Zustand vorher | letzter unabhängiger Readback |
| Zustand nachher | neuer unabhängiger Readback |
| gleiche ID? | ja/nein |
| Plattformwirkung | belegt / nicht belegt / unklar |
| Fehler | exakter fachlicher Fehler, keine Secrets |
| lokale Nebenwirkung | YAML, Bilder, Stats, Sessiondateien usw. |

**Nicht zulässig als Erfolgsbeleg:** nur lokales Speichern, UI-Toast, Job-Status ohne Plattform-Readback, `[]` ohne geklärten Fetchstatus oder ein Nullwert bei fehlendem Feld.

## Schritt 0 — Baseline

1. Testsitzung manuell herstellen.
2. Automatische KI-Antworten und Out-of-Office deaktiviert lassen.
3. Testanzeige in der Verkäuferansicht öffnen.
4. Anzeigen-ID, Titel, Beschreibung, Preis und Plattformstatus notieren.
5. Beide Kandidaten lesen die Anzeige.
6. Stimmen ID und wesentliche Felder nicht überein: **STOP** und Ursache klären.

Erwartete Baseline:

- eine einzige Testanzeige im Scope,
- stabile Anzeigen-ID,
- kein Kandidat hat bereits eine Mutation ausgelöst.

## Schritt 1 — Sync

### Kandidat A

- Download/Sync nur für die Testanzeige bzw. den kleinstmöglichen Scope ausführen.
- entstandene YAML-/JSON-Datei erfassen.
- gespeicherte Plattform-ID prüfen.
- Bilder/Assets auf unnötige Duplikate prüfen.
- lokalen `active`-Wert getrennt vom Plattformstatus dokumentieren.

### Kandidat B

- `my_ads()` bzw. `get_my_ad(<id>)` verwenden.
- ID, Titel, Beschreibung und Preis erfassen.
- fehlende Plattformfelder explizit als fehlend notieren.

### Readback

Beide Kandidaten erneut lesen.

Erfolg:

- identische Plattform-ID,
- kein Duplikat beim zweiten Sync,
- wesentliche Inhaltsfelder stimmen mit der Verkäuferansicht überein.

## Schritt 2 — Inhaltsupdate

Eine kleine reversible Änderung verwenden, zum Beispiel einen eindeutig markierten Zusatz in der Beschreibung.

### Kandidat A

- lokalen Inhalt ändern,
- echten Bot-`update`-Pfad ausführen,
- Job-Erfolg allein nicht akzeptieren,
- anschließend Plattforminhalt erneut lesen.

Erfolg nur wenn:

- Anzeigen-ID unverändert,
- Änderung auf Kleinanzeigen sichtbar,
- unabhängiger Readback bestätigt den neuen Inhalt.

### Kandidat B

Im geprüften Stand existiert kein belegter in-place Update-Aufruf.

- nicht mit `post_ad()` ersetzen,
- Fähigkeit als **nicht vorhanden** protokollieren,
- keine zweite Anzeige erzeugen.

## Schritt 3 — Pause/Reserve

### Kandidat A

Nicht den lokalen UI-Schalter „Deaktivieren“ als Plattformtest verwenden.

Stattdessen den echten Underlying-Bot-Pfad `reserve` verwenden.

Readback:

- Manage-Ads-Status muss `paused` liefern,
- gleiche Anzeigen-ID.

### Kandidat B

`pause_ad(<id>)` ausführen.

Readback mit mindestens einem unabhängigen zweiten Pfad.

Wenn ein Kandidat bereits pausiert hat, vor Prüfung des zweiten Kandidaten wieder aktivieren. So bekommt jeder Kandidat einen eigenen nichtdestruktiven Aktionsnachweis.

## Schritt 4 — Aktivieren

Analog zu Pause:

- Kandidat A: `activate`,
- Kandidat B: `activate_ad(<id>)`.

Erfolg:

- gleiche Anzeigen-ID,
- Plattformstatus wieder aktiv,
- unabhängiger Readback.

## Schritt 5 — Views / Merker / Replies

Mindestens erfassen:

- `viewCount`,
- `watchCount`,
- `replies`,
- Plattformstatus,
- Abrufzeit.

Vergleich mit der sichtbaren Verkäuferansicht.

Regeln:

- fehlend ≠ 0,
- Fetchfehler ≠ 0,
- Käufer-`watchlist()` von monkrel ≠ Verkäufer-Merkerzahl,
- bei UI-`[]` zuerst klären, ob Abruf erfolgreich leer oder fehlgeschlagen war.

Ohne eindeutige Semantik kein Messwert in die spätere Historie übernehmen.

## Schritt 6 — Inbox-Zuordnung

Vom kontrollierten Gegenkontakt genau eine eindeutige Nachricht zur Testanzeige senden.

Dann beide Kandidaten prüfen.

Erfassen:

- Conversation-ID,
- `adId` / `ad_id`,
- Richtung,
- unread/unread count,
- Gegenkontakt-ID soweit verfügbar,
- Nachrichtentext,
- Zeitstempel.

Erfolg:

- Conversation erscheint,
- Anzeigen-ID entspricht exakt der Testanzeige,
- Zuordnung bleibt beim zweiten Abruf stabil.

Keine automatische Antwort senden.

## Schritt 7 — Reaktionsmetriken

Noch keine einzelne KPI auswählen.

Aus demselben Testdatensatz getrennt berechnen:

- `conversation_count`,
- `unique_buyer_count`,
- `inbound_message_count`.

Erst danach fachlich entscheiden, welche Kennzahl „wie viele geschrieben haben“ im Produkt meint.

## Schritt 8 — Delete

Delete ist der letzte Schritt.

Vorher:

1. Beide Kandidaten lesen unmittelbar vorher dieselbe aktive Testanzeige.
2. Alle nichtdestruktiven Tests sind abgeschlossen.
3. Ein Kandidat wird für den tatsächlichen Erst-DELETE gewählt.
4. Auswahl und Grund werden **vor** dem DELETE protokolliert.

Nur der gewählte Kandidat führt DELETE aus.

Danach:

- Kandidat A liest Abwesenheit,
- Kandidat B liest Abwesenheit,
- Verkäuferansicht bestätigt Löschung.

Nicht behaupten, beide Kandidaten hätten erfolgreich gelöscht.

### Zusätzliche UI-Nachprüfung

Nach dem DELETE prüfen:

- lokaler Anzeigenbestand,
- liegen Bilder/Assets unnötig weiter herum?,
- bleibt ein `.ad-stats.json`-Datensatz zurück?,
- zeigt Dashboard Phantomdaten?,
- wurde ein Fehler als leere Liste maskiert?

## Stop-Bedingungen

Sofort stoppen, wenn:

- Anzeigen-ID unerwartet wechselt,
- mehr als die Testanzeige betroffen wäre,
- Captcha/MFA eine Umgehung statt manueller Aktion verlangen würde,
- Kandidat einen unklaren Batch-Scope ausführen will,
- ein Fehlerzustand nicht sicher von „0“ oder „leer“ unterscheidbar ist,
- der tatsächliche Remote-Zustand nicht unabhängig gelesen werden kann.

Nicht durch wiederholte Mutationen „probieren“, ob es vielleicht funktioniert.

## Ergebnisprotokoll

Nach Abschluss muss eine Tabelle dieser Form ausgefüllt sein:

| Schritt | Kandidat A | Kandidat B | unabhängiger Readback | Ergebnis |
|---|---|---|---|---|
| Baseline/Sync | | | | |
| Update | | nicht vorhanden, falls unverändert | | |
| Pause | | | | |
| Aktivieren | | | | |
| Views/Merker/Replies | | | Verkäuferansicht | |
| Inbox/adId | | | Gegenkontakt + zweiter Abruf | |
| Delete | Erst-DELETE oder nur Readback | Erst-DELETE oder nur Readback | Verkäuferansicht | |

Zusätzlich festhalten:

- Anzeigen-ID,
- Zeitstempel jedes Messwerts,
- konkrete lokale Dateien/Pfade,
- aufgetretene Fehler,
- manuelle Login-/MFA-Schritte,
- Account-/ToS-Beobachtungen,
- lokale Nebenwirkungen.

## Architektur-Gate danach

Erst nach dem vollständigen Readback-Ablauf Gegner erneut auf folgende Annahmen ansetzen:

1. Ein erfolgreicher Einzeltest bedeutet ausreichende Stabilität.
2. Das UI spart langfristig mehr Aufwand als seine Komplexität kostet.
3. Browser-Automation ist robuster als private/mobile API oder umgekehrt.
4. Fehlende Felder sind Plattformlücken statt Parser-/Endpoint-Lücken.
5. Eine Anzeige ist für die Kernabläufe repräsentativ genug.

Danach erst zwischen folgenden Pfaden entscheiden:

- UI weitgehend übernehmen,
- nur UI-/Stats-/Messaging-Komponenten übernehmen,
- monkrel als Backend-Unterbau ergänzen,
- Komponenten kombinieren,
- eigenes dünnes Backend über vorhandenen Komponenten.

## Nächste Aktion

Testsitzung + genau eine Testanzeige bereitstellen. Danach dieses Runbook ohne Scope-Erweiterung abarbeiten.
