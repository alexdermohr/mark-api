# Produktspezifikation

Stand: 05.10.2026

Status: **MVP-Kern technisch vorhanden; Product Launcher bündelt Initial-Sync, expliziten lokalen Reaction-E-Mail-Import, Dashboard, Same-Origin Write UX und Default-on Write Composition; Email-only Classification ist integriert; Gesamtprodukt, Freshness/Recovery und finale E2E-Abnahme noch nicht abgeschlossen**

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

Die Daten-/Analytics-Surface des Dashboards ist read-only und stellt Anzeigen-, Reaktions- und Analytics-Daten dar; Grafiken und Ranglisten verwenden ausschließlich explizit angeforderte Rohmetriken. Der standalone Einstieg `mark-api-dashboard` bleibt vollständig GET-only/read-only. Im normalen Product Launcher ergänzt dieselbe sichtbare Oberfläche eine getrennt abgesicherte Write UX, die ausschließlich an die bestehende lokale Write API delegiert.

Der installierte Einstieg `mark-api-launch` bildet den kohärenten Produktstart für eine bereits laufende, bereits authentifizierte lokale Chrome-/Chromium-Sitzung über loopback-CDP. Er führt genau einen frischen Owner-Inventory-Read aus und persistiert bestätigte aktuelle Anzeigen sowie `ABSENT`-Transitions für historisch bekannte, nun fehlende Anzeigen. Optional importiert er danach einen vom Nutzer ausdrücklich über `--email FILE` angegebenen lokalen Batch von Kleinanzeigen-Nachrichtenkopien mit der bestehenden idempotenten E-Mail-Importlogik in denselben Store. Erst nach erfolgreichem Initial-Read und einem gegebenenfalls angeforderten erfolgreichen Reaction-Import wird zuerst die bestehende loopback-only Mark Write API gestartet und anschließend das Dashboard an deren tatsächliche Loopback-Adresse gebunden. Der Launcher erzeugt dafür pro Prozess einen neuen lokalen Write-API-Bearer sowie einen getrennten Dashboard-Write-Token. Letzterer wird ausschließlich als URL-Fragment der lokalen Dashboard-URL übergeben, vom Browser in `sessionStorage` übernommen und unmittelbar aus der sichtbaren URL entfernt; der Backend-Bearer wird nicht an Browser-JavaScript exponiert.

Die Product-Launcher Write UX unterstützt die bereits implementierten Create-, Media-, Content-, State- und Delete-Surfaces. Ihre Same-Origin-Proxy-Schicht ist strikt route-/header-/body-allowlistet, verlangt den per-process Dashboard-Token, einen passenden `Origin` und einen CSRF-Marker und öffnet kein CORS. Sie implementiert keine eigene Plattform-Write-Semantik: Capability, persistente Write-API-Idempotenz, Ownership, Confirmation, TOCTOU, Post-Readback, Media-Persistenzevidenz und `platform_retry_authorized=false` bleiben ausschließlich beim vorhandenen Write-API-/Core-Pfad. Zusätzlich persistiert der Dashboard-Proxy vor jeder Plattformweiterleitung den normalisierten UI-Request und dessen Idempotency-Key als lokalen Pending-Fence in derselben SQLite-Datei. Bei unbekanntem Transportausgang wird nicht automatisch erneut gesendet; ein zweiter Tab oder neuer Key für dieselbe Ressource wird blockiert, und eine manuelle Wiederholung verwendet exakt denselben gespeicherten Request/Key. Nach Tab- oder Dashboard-Neustart kann die UI die Pending-Records mit dem aktuellen per-process UI-Token wieder lesen; Persistenzfehler bleiben fail-closed. Ein Pending-Fence wird erst durch einen separaten lokalen Browser-ACK für exakt Scope und Idempotency-Key geräumt, nachdem die UI einen gebundenen terminalen Backend-Response oder sicheren Clientfehler tatsächlich verarbeitet hat; bei verlorenem Response oder ACK bleibt der ursprüngliche Request gebunden. Partielles lokales Media-Staging verwirft bereits bekannte Refs über eine capability-gebundene idempotente Discard-Primitive; sie ist keine Plattformoperation. Verlorene Antworten nach bereits erfolgreichem lokalem Stage können keinen dauerhaften Handle-Leak erzeugen: ungenutzte Handles laufen nach 15 Minuten ab und werden vor dem nächsten Stage/Resolve lazy aus dem lokalen Kontingent entfernt. Ein zusätzlicher `--enable-writes`-Schalter existiert weiterhin nicht. Ein fehlgeschlagener oder unklarer Initial-Read oder ein fehlerhafter ausdrücklich angeforderter Mail-Batch bricht den Start vor beiden HTTP-Surfaces ab. Mailbox-Suche, Messaging-Gateway-Reads und periodische Aktualisierung bleiben außerhalb dieses Slices.

### 2.5 Datensammlung und Auswertung

Gewünscht und technisch abgebildet sind mindestens:

- Aufrufe,
- getrennte Rohmetriken für Konversationen, eindeutige Interessenten und eingehende Nachrichten,
- daraus berechnete deskriptive Kennzahlen,
- Grafiken,
- Top-Listen.

Der Datenvertrag hält `conversation_count`, `unique_buyer_count` und `inbound_message_count` getrennt. `reaction_metric` ist standardmäßig nicht gesetzt und darf nur explizit auf eine dieser drei Rohmetriken gebunden werden. Keine davon ist technisch oder fachlich als Default bevorzugt.

Zusätzlich bleiben E-Mail-basierte Projektionen als eigene, source-explizite Metriken getrennt und werden nicht mit den ReactionSnapshot-Werten vermischt. Der normale Produktstart kann diese Evidenz aus ausdrücklich angegebenen lokalen `.eml`-Dateien importieren. Eine so lokal belegte Email-only-Anzeigen-ID darf explizite Klassifikationslabels tragen und an E-Mail-Metrik-Gruppenrankings teilnehmen; dadurch wird sie weder zu einer Owner-/Bestandsidentität noch erhält sie Views-, `ReactionSnapshot`- oder Write-Semantik.

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
7. Die schreibfähige HTTP-Surface bleibt als separate loopback-only Write API erhalten. Der Product Launcher bindet die sichtbare Dashboard Write UX ausschließlich über einen Same-Origin-Proxy daran; der Browser erhält nicht den Backend-Bearer, sondern einen getrennten per-process Dashboard-Token. Beide Tokens werden nicht in SQLite persistiert. Standalone `mark-api-dashboard` bleibt GET-only/read-only.
8. Die Analytics-Entscheidungen `reaction_metric` und `objective_metric` bleiben unabhängig von den Write-Gates und standardmäßig ungesetzt.
9. `mark-api-launch` verwendet eine bereits authentifizierte loopback-CDP-Sitzung, führt genau einen fail-closed Initial-Inventory-Sync aus und kann danach ausdrücklich angegebene lokale `.eml`-Reaction-Evidenz batch-atomar importieren. Erst anschließend startet er die default-on Write API und danach das daran gebundene Dashboard. Die Dashboard Write UX delegiert ausschließlich an diese Write API, führt keine automatischen Retries aus und eröffnet weder CORS noch einen privaten/mobile Messaging-Readpfad. Der Launcher startet keinen Browser und automatisiert keinen Login. Automatisierte Tests dieses Slices führen keine Kleinanzeigen-Plattformwrites aus.
10. E-Mail-Evidenz bleibt source-explizit: sie erzeugt keine Owner-Identität, keinen `AdSnapshot` und keinen `unique_buyer_count`; `reaction_metric` bleibt default-off. Lokale Klassifikation darf an eine durch Owner-Snapshot **oder** importiertes E-Mail-Event belegte ID gebunden werden. Email-only-IDs nehmen nur an Gruppenrankings von Metriken teil, deren eigene Ranking-Population sie enthält; insbesondere nicht an Views-/Owner-/`ReactionSnapshot`-Gruppen.
11. Periodische Synchronisation sowie die weitergehende Freshness-/Recovery-UX bleiben spätere Produktslices. Ein Media-Submit-`UNKNOWN` autorisiert weiterhin keinen Write-Retry; beim Launcher-Shutdown ist höchstens die bestehende observation-only Media-Reconciliation vor erneutem Cleanup zulässig.
12. Die Write API serialisiert ihre Prozessautorität zusätzlich über einen exklusiven OS-Lock auf der SQLite-Datei. Persistente Idempotency-Claims sind runtime-owner-gebunden und besitzen einen separaten Execution-Start-Barrier. Nur ein gleichartiger Claim eines beendeten Runtimes ohne gesetzten Barrier darf übernommen werden; gesetzter Barrier und Legacy-Claims ohne beweisbaren Owner bleiben über Neustarts fail-closed.

Die maßgeblichen Architektur- und Sicherheitsentscheidungen sind in D-006 bis D-027 dokumentiert. Ein implementierter technischer Pfad ist keine automatische Freigabe für reale Plattformwrites außerhalb einer vom Nutzer ausgelösten Produktoperation.

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
10. read-only Daten-/Analytics-Dashboard plus im Product Launcher Same-Origin Write UX für die bestehenden Create-/Media-/Content-/State-/Delete-Surfaces, Grafiken, Top-Listen und lokale Runtime-Smokes,
11. installierter `mark-api-launch`-Startpfad mit genau einem bestätigten Startup-Inventory-Sync vor Dashboard und Write API,
12. Default-on Produktkomposition der bestehenden Create-, Media-, Content-, State- und Delete-Surfaces mit prozesslokalem Bearer und unveränderten Idempotency-/Confirmation-/No-Blind-Retry-Grenzen.
13. Interne Auth-/Idempotency-Härtung mit exklusiver SQLite-Write-Runtime, runtime-owner-gebundenen Claims, persistiertem Execution-Start-Barrier und fail-closed Legacy-Migration.
14. Im normalen Launcher optionaler, ausdrücklich dateigebundener Reaction-Import lokaler Kleinanzeigen-E-Mail-Kopien vor den HTTP-Surfaces; idempotent, ohne Mailbox-/Messaging-Netzwerkzugriff und ohne erfundene Owner- oder Buyer-Fakten.
15. Explizite lokale Klassifikation von Email-only-IDs mit source-expliziten E-Mail-Gruppenrankings, ohne `AdSnapshot`, Owner-/Presence-Folgerung oder Ausweitung auf nicht belegte Metriken.
16. Same-Origin Dashboard Write UX mit getrenntem UI-Token, serverseitiger Bearer-Injektion, strikter Proxy-Allowlist und exakt gebundener UI-Idempotenz ohne automatische Wiederholung bei unbekanntem Transportausgang.

Text- und Bildgenerierung bleiben dokumentiert, aber für diesen MVP depriorisiert.

Reale sichtbare Testanzeigen, Testnachrichten oder zyklische Plattformwrites werden nicht als normale Regressionstests verwendet.

## 7. Nächste Produktphase

Der Product Launcher schließt jetzt den kohärenten Startpfad aus Initial-Sync, read-only Daten-/Analytics-Dashboard, Same-Origin Write UX und default-on Write-Komposition. Die bestehenden Authentizitäts-, Ownership-, Confirmation-, TOCTOU-, Readback- und No-Blind-Retry-Sicherheiten bleiben erhalten; D-024 härtet zusätzlich die persistente Idempotenz über Prozessabbruch und Neustart.

Die interne Auth-/Idempotency-Härtung, der explizite lokale Reaction-Data-Startup-Pfad, Email-only Classification und Dashboard Write UX sind jetzt Bestandteil des Produktpfads. Als nächster eigener Produktslice folgt Freshness/Time; danach Recovery UX, SQLite Lifecycle, Shutdown, Packaging, Full E2E und finaler Produktaudit.

Die beiden fachlichen Analytics-Entscheidungen bleiben weiterhin offen: `reaction_metric` für „wie viele geschrieben haben“ und `objective_metric` beziehungsweise eine ausdrücklich definierte Zielfunktion für „beste Lösung“. Ohne diese Festlegungen darf keine objective-gebundene Empfehlung als fachlich gewollt behauptet werden.
