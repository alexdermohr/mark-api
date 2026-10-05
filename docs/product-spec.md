# Produktspezifikation

Stand: 04.10.2026

Status: **MVP-Kern technisch vorhanden; Product Launcher bündelt Initial-Sync, Dashboard und Default-on Write Composition; Gesamtprodukt, Freshness/Recovery und finale E2E-Abnahme noch nicht abgeschlossen**

Quelle: `docs/requirements-source-2026-09-23.md`

## 1. Ziel

Für Marks Kleinanzeigen-Account soll eine Lösung entstehen, die Anzeigen verwaltet und Leistungsdaten in einem Dashboard auswertet. Aus den gesammelten Daten sollen Vergleiche, Ranglisten und Optimierungsvorschläge entstehen.

Die ursprünglich genannten Wünsche nach externer Titel-/Beschreibungsgenerierung und Bildgenerierung bleiben dokumentiert, sind nach Projektentscheidung vom 24.09.2026 jedoch **nicht MVP-priorisiert**. Der MVP konzentriert sich auf Anzeigenverwaltung, Synchronisation, Messaging und messbare Leistungsdaten.

## 2. Belegter Funktionsumfang

### 2.1 Anzeigen

Der gewünschte Oberbegriff „Anzeigen verwalten“ ist für den vorhandenen MVP-Core inzwischen technisch präzisiert als:

- bestehende eigene Anzeigen synchronisieren,
- Titel und Beschreibung einer bestehenden Anzeige in-place aktualisieren,
- pausieren beziehungsweise reservieren,
- aktivieren,
- löschen,
- Besitzerstatus sowie Views, Merker und Replies lesen,
- Inbox/Conversation einer Anzeigen-ID zuordnen.

Create/Publish ist zusätzlich als eigener, stärker gegateter Write-Pfad implementiert. Daraus folgt keine automatische Produktionsfreigabe.

### 2.2 Textgenerierung — bestätigt, aber depriorisiert

- Titel aus Prompts generieren.
- Beschreibungstexte aus Prompts generieren.
- Generierte Inhalte für Anzeigen verwenden.

Diese ursprünglich bestätigte Anforderung ist **kein MVP-Schwerpunkt**.

### 2.3 Bildgenerierung — bestätigt, aber depriorisiert

- Bilder aus Prompts generieren.
- Generierte Bilder automatisch für Anzeigen verwenden.

Diese ursprünglich bestätigte Anforderung ist **kein MVP-Schwerpunkt**. Der vorhandene Media-Create-Pfad betrifft dagegen die sichere Verwendung explizit zugelassener lokaler Medien und ersetzt keine generative Bildfunktion.

### 2.4 Dashboard und Product Launcher

Ein read-only Dashboard samt lokaler API stellt Anzeigen-, Reaktions- und Analytics-Daten dar. Grafiken und Ranglisten verwenden ausschließlich explizit angeforderte Rohmetriken.

Der installierte Einstieg `mark-api-launch` bildet den kohärenten Produktstart für eine bereits laufende, bereits authentifizierte lokale Chrome-/Chromium-Sitzung über loopback-CDP. Er führt genau einen frischen Owner-Inventory-Read aus und persistiert bestätigte aktuelle Anzeigen sowie `ABSENT`-Transitions für historisch bekannte, nun fehlende Anzeigen. Erst nach diesem erfolgreichen Initial-Read werden das read-only Dashboard und die bestehende loopback-only Mark Write API als gemeinsame Produkt-Runtime bereitgestellt. Der Launcher erzeugt dafür pro Prozess einen neuen lokalen Bearer, gewährt die bereits implementierten Write-Capabilities und öffnet API-, Core- und Media-Gate explizit; hierfür gibt es keinen zusätzlichen `--enable-writes`-Schalter. Ein fehlgeschlagener oder unklarer Initial-Read bricht den Start vorher ab. Periodische Aktualisierung bleibt ein separater späterer Slice.

### 2.5 Datensammlung und Auswertung

Gewünscht und technisch abgebildet sind mindestens:

- Aufrufe,
- getrennte Rohmetriken für Konversationen, eindeutige Interessenten und eingehende Nachrichten,
- daraus berechnete deskriptive Kennzahlen,
- Grafiken,
- Top-Listen.

Der Datenvertrag hält `conversation_count`, `unique_buyer_count` und `inbound_message_count` getrennt. `reaction_metric` ist standardmäßig nicht gesetzt und darf nur explizit auf eine dieser drei Rohmetriken gebunden werden. Keine davon ist technisch oder fachlich als Default bevorzugt.

Zusätzlich bleiben E-Mail-basierte Projektionen als eigene, source-explizite Metriken getrennt und werden nicht mit den ReactionSnapshot-Werten vermischt.

### 2.6 Vergleichsdimensionen

Top-Listen beziehungsweise Vergleiche sind insbesondere nach folgenden expliziten Merkmalen möglich:

- Bild-Typ,
- Stadt,
- Text-Typ,
- Titel-Typ.

### 2.7 Empfehlungen

Aus den Daten sollen Vorschläge für die besten beziehungsweise erfolgversprechendsten Lösungen abgeleitet werden.

Dafür ist eine explizite `objective_metric` erforderlich. Der Contract setzt keine Zielmetrik als Default; ohne konfigurierte Zielgröße bleibt die Optimierungssemantik neutral und fail-closed. Rohmetriken und explizit angeforderte deskriptive Rankings bleiben davon getrennt und begründen weder Kausalität noch automatisch eine Qualitätsaussage.

## 3. Funktionale Anforderungen

- **FR-01:** Anzeigen erstellen, jedoch nur über den separat gegateten Create-Pfad.
- **FR-02:** Anzeigen löschen; Delete bleibt zusätzlich ID-gebunden freigabepflichtig.
- **FR-03:** Anzeigen gemäß Abschnitt 2.1 verwalten.
- **FR-04 (depriorisiert):** Titel anhand eines Prompts generieren.
- **FR-05 (depriorisiert):** Beschreibungstexte anhand eines Prompts generieren.
- **FR-06 (depriorisiert):** Bilder anhand eines Prompts generieren.
- **FR-07 (depriorisiert):** Generierte Bilder einer Anzeige automatisch zur Verwendung zuführen.
- **FR-08:** Aufrufzahlen erfassen, soweit der gewählte Datenpfad diese bereitstellt.
- **FR-09:** `conversation_count`, `unique_buyer_count` und `inbound_message_count` getrennt erfassen; die fachliche Bedeutung von „wie viele geschrieben haben“ nur über eine explizite `reaction_metric` festlegen.
- **FR-10:** Kennzahlen und Grafiken im Dashboard visualisieren.
- **FR-11:** Top-Listen nach Bild-Typ, Stadt, Text-Typ und Titel-Typ nur gegen eine explizit ausgewählte Rohmetrik erzeugen.
- **FR-12:** Optimierungsvorschläge nur gegen eine explizit konfigurierte `objective_metric` ableiten; ohne Zielmetrik keine „beste Lösung“ behaupten.

## 4. Verbleibende fachliche Detailentscheidungen

Issue #1 enthält nur noch zwei fachliche Restentscheidungen:

1. Welche der getrennten Rohmetriken `conversation_count`, `unique_buyer_count` oder `inbound_message_count` soll fachlich „wie viele geschrieben haben“ bedeuten?
2. Welche explizite `objective_metric` oder später ausdrücklich definierte Zielfunktion bestimmt objektiv, wann eine Variante als „beste Lösung“ gilt?

Diese beiden Entscheidungen bleiben bewusst menschliche Produktentscheidungen. Das Repository setzt dafür keinen Default.

Frühere offene Punkte zu Verwaltungsscope, Integrationsarchitektur, technischen Freigaberegeln, Runtime-Komposition und Fehler-/Retry-Semantik sind inzwischen durch die implementierten Contracts und die Entscheidungen in `docs/DECISIONS.md` konkretisiert oder als separate Betriebs-/Freigabegates abgegrenzt. Sie sind keine offenen Detail-Akzeptanzpunkte von Issue #1 mehr.

## 5. Technische Gates und Betriebsgrenzen

Der aktuelle Core und die Runtime-Surfaces erzwingen insbesondere:

1. Die generischen Core-/Runtime-Surfaces bleiben fail-safe default-off. Der normale Produktstart `mark-api-launch` öffnet die bestehenden Capability-, API-, Core- und Media-Gates dagegen ausdrücklich und automatisch für seine lokale, loopback-only Write-Komposition; es gibt keinen zusätzlichen Write-Opt-in im Produktpfad.
2. ID-gebundene Writes verwenden einen frischen Owner-Pre-Read, genau einen Mutationsversuch und einen frischen target-bound Post-Readback; Create/Delete folgen zusätzlich dem in D-019 festgelegten Confirmation-Vertrag ohne künstlich duplizierte Management-Runtime.
3. Ein unklarer oder `AMBIGUOUS` Ausgang autorisiert keinen Blind-Retry.
4. Bei Delete ist die authentifizierte, exakt pfad-ID-gebundene Nutzeroperation selbst die Freigabe; die stabile Idempotency-ID liefert intern die Audit-/Authorization-Referenz.
5. Create/Publish und Media-Create besitzen eigene, strengere Reconciliation- und Persistenzevidenz.
6. Login, MFA, CAPTCHA und sonstige Sicherheitschallenges werden nicht automatisiert oder umgangen.
7. Die schreibfähige HTTP-Surface bleibt vom read-only Dashboard getrennt und loopback-only; der Launcher erzeugt ihren Bearer pro Prozess neu und persistiert ihn nicht in SQLite. Der einmalig ausgegebene Bearer ist während der Prozesslaufzeit lokales Write-Secret-Material und darf nicht in geteilte Logs oder Support-Artefakte übernommen werden.
8. Die Analytics-Entscheidungen `reaction_metric` und `objective_metric` bleiben unabhängig von den Write-Gates und standardmäßig ungesetzt.
9. `mark-api-launch` verwendet eine bereits authentifizierte loopback-CDP-Sitzung, führt genau einen fail-closed Initial-Inventory-Sync aus und startet erst danach Dashboard und default-on Write API. Er startet keinen Browser und automatisiert keinen Login. Automatisierte Tests dieses Slices führen keine Kleinanzeigen-Plattformwrites aus.
10. Periodische Synchronisation sowie die weitergehende Freshness-/Recovery-UX bleiben spätere Produktslices. Ein Media-Submit-`UNKNOWN` autorisiert weiterhin keinen Write-Retry; beim Launcher-Shutdown ist höchstens die bestehende observation-only Media-Reconciliation vor erneutem Cleanup zulässig.
11. Die Write API serialisiert ihre Prozessautorität zusätzlich über einen exklusiven OS-Lock auf der SQLite-Datei. Persistente Idempotency-Claims sind runtime-owner-gebunden und besitzen einen separaten Execution-Start-Barrier. Nur ein gleichartiger Claim eines beendeten Runtimes ohne gesetzten Barrier darf übernommen werden; gesetzter Barrier und Legacy-Claims ohne beweisbaren Owner bleiben über Neustarts fail-closed.

Die maßgeblichen Architektur- und Sicherheitsentscheidungen sind in D-006 bis D-024 dokumentiert. Ein implementierter technischer Pfad ist keine automatische Freigabe für reale Plattformwrites außerhalb einer vom Nutzer ausgelösten Produktoperation.

## 6. Aktueller MVP-Stand

Der technische MVP umfasst inzwischen:

1. Synchronisation und read-only Besitzer-/Bestandsdaten,
2. ID-gebundenes Content-Update,
3. Pause/Reservierung und Aktivierung,
4. Delete mit intern gebundener Nutzerfreigabe und D-019-Confirmation,
5. separat gegatetes Create/Publish einschließlich optionalem Media-Pfad,
6. Views, Merker, Replies und getrennte Reaktionsmetriken,
7. Inbox-/Conversation-Zuordnung und source-explizite E-Mail-Projektionen,
8. append-only SQLite-Snapshots und crash-idempotente lokale Write-Receipts,
9. Analytics-Rohmetriken und Vergleiche nach Bild-Typ, Stadt, Text-Typ und Titel-Typ,
10. read-only Dashboard, Grafiken, Top-Listen und lokale Runtime-Smokes,
11. installierter `mark-api-launch`-Startpfad mit genau einem bestätigten Startup-Inventory-Sync vor Dashboard und Write API,
12. Default-on Produktkomposition der bestehenden Create-, Media-, Content-, State- und Delete-Surfaces mit prozesslokalem Bearer und unveränderten Idempotency-/Confirmation-/No-Blind-Retry-Grenzen.
13. Interne Auth-/Idempotency-Härtung mit exklusiver SQLite-Write-Runtime, runtime-owner-gebundenen Claims, persistiertem Execution-Start-Barrier und fail-closed Legacy-Migration.

Text- und Bildgenerierung bleiben dokumentiert, aber für diesen MVP depriorisiert.

Reale sichtbare Testanzeigen, Testnachrichten oder zyklische Plattformwrites werden nicht als normale Regressionstests verwendet.

## 7. Nächste Produktphase

Der Product Launcher schließt jetzt den kohärenten Startpfad aus Initial-Sync, read-only Dashboard und default-on Write-Komposition. Die bestehenden Authentizitäts-, Ownership-, Confirmation-, TOCTOU-, Readback- und No-Blind-Retry-Sicherheiten bleiben erhalten; D-024 härtet zusätzlich die persistente Idempotenz über Prozessabbruch und Neustart.

Die interne Auth-/Idempotency-Härtung ist jetzt Bestandteil des Produktpfads. Als nächster eigener Produktslice folgt Reaction Data; danach Email-only Classification, Dashboard Write UX, Freshness/Time, Recovery UX, SQLite Lifecycle, Shutdown, Packaging, Full E2E und finaler Produktaudit.

Die beiden fachlichen Analytics-Entscheidungen bleiben weiterhin offen: `reaction_metric` für „wie viele geschrieben haben“ und `objective_metric` beziehungsweise eine ausdrücklich definierte Zielfunktion für „beste Lösung“. Ohne diese Festlegungen darf keine objective-gebundene Empfehlung als fachlich gewollt behauptet werden.
