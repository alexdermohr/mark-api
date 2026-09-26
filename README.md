# mark-api

Öffentliche Projektdokumentation für die mit Mark besprochene Kleinanzeigen-Agent-/API-Integration.

## Status

**Core, Adapter, Dashboard und Analytics implementiert / technischer Read-only-Livepfad belegt / Plattformwrites bis zur Integrationsfreigabe gesperrt** — Stand: 25.09.2026.

Mark hat nach dem Telefonat die gewünschte Funktionalität schriftlich konkretisiert. Der MVP-Fokus liegt auf Anzeigenverwaltung, Synchronisation, Verkäufermetriken, Inbox/Interessenten, Dashboard und datenbasierter Auswertung. Externe Text-/Bildgenerierung war ursprünglich Teil des Wunsches, ist seit 24.09.2026 aber nicht mehr MVP-priorisiert.

## Technisch belegt

- Python-3.12+-Core mit diskriminierten Read-Ergebnissen, Safe-Write-Orchestrierung und SQLite-Snapshot-Persistenz.
- `ManagementReadAdapter` für den autoritativen Besitzerbestand sowie Verkäuferstatus, Views, Merker und Replies.
- `MonkrelMobileApiAdapter` für eigene Anzeigen, ID-gebundene Pause/Aktivierung, Delete und Inbox/Conversation-Zuordnung.
- `BrowserBotAdapter` als isolierter externer Prozess für Sync und in-place Inhaltsupdate; Browserautomation ist nicht der bevorzugte Zustandsadapter.
- `MarkService` als Application-Layer über den Capability-Adaptern.
- Read-only Dashboard/API auf Loopback sowie Analytics-Rankings auf explizit gespeicherten Klassifikationslabels.
- Der Ein-Anzeigen-Realtest vom 24.09.2026 belegt Sync, stabiles in-place Update, Pause/Aktivierung, Verkäufermetriken, positiven Inbox/adId-Fall und Delete mit unabhängigen Besitzerlisten-Readbacks.
- Am 25.09.2026 wurde der aktuell eingeloggte eigene Account read-only durch `ManagementReadAdapter -> EnrichedOwnerReader -> MarkService -> SnapshotStore` geführt: erfolgreicher leerer Besitzerbestand (`success_empty`), keine Plattformmutation.
- Plattformwrites sind im Core standardmäßig deaktiviert. Create/Publish ist weiterhin nicht für den Dauerbetrieb freigegeben.

## Lokale Klassifikationspflege

Die Analytics-Gruppierung verwendet explizite Labels für `image_type`, `city`, `text_type` und `title_type`. Das HTTP-Dashboard bleibt absichtlich vollständig read-only. Labels werden lokal und append-only über den separaten CLI-Entrypoint gepflegt:

```bash
mark-api-classify \
  --db /pfad/zu/mark.sqlite \
  --ad-id 1234567890 \
  --city Dresden \
  --image-type overview
```

Nicht angegebene Dimensionen werden aus der letzten Klassifikation übernommen. Ein Label wird nur durch eine explizite Clear-Aktion entfernt, zum Beispiel:

```bash
mark-api-classify \
  --db /pfad/zu/mark.sqlite \
  --ad-id 1234567890 \
  --clear text-type
```

Der CLI akzeptiert nur bereits im lokalen Store bekannte Anzeigen-IDs. Er greift weder auf Kleinanzeigen noch auf andere Netzwerkdienste zu und verändert keine Plattformdaten.

## Offene fachliche Punkte

Die Reaktionsdaten werden absichtlich getrennt als `conversation_count`, `unique_buyer_count` und `inbound_message_count` gespeichert. Welche dieser Größen fachlich „wie viele geschrieben haben“ meint, ist noch nicht festgelegt.

Ebenso ist noch keine fachlich bestätigte Zielfunktion für „beste Lösung“ definiert. Die Analytics-Schicht zeigt deshalb Rohmetriken und Rankings, ohne daraus Kausalität oder Qualität abzuleiten.

## Wichtigstes Gate

Der technische PoC ist abgeschlossen; die technische Machbarkeit ist **nicht** mit einer Freigabe für automatisierten Dauerbetrieb gleichzusetzen.

Die aktuell veröffentlichten Kleinanzeigen-Nutzungsbedingungen untersagen ohne ausdrückliche schriftliche Zustimmung den Einsatz von Crawlern, Scrapern oder anderen automatisierten Mechanismen, um auf die Kleinanzeigen-Dienste zuzugreifen und Inhalte zu sammeln. Die offizielle Professional-Sellers-API ist laut Entwicklerdokumentation nur für professionelle Nutzer mit Power- oder Premium-Angebot verfügbar und nicht mit manuell im Web erstellten Anzeigen synchron.

Daher gilt bis zur Klärung von [Issue #2](https://github.com/alexdermohr/mark-api/issues/2):

- keine synthetischen oder testartig erkennbaren Anzeigen/Nachrichten auf dem aktuellen Account,
- keine Publish/Delete/Pause/Aktivieren-Zyklen nur zu Testzwecken,
- keine Ableitung einer produktiven Freigabe aus dem erfolgreichen technischen PoC,
- schreibender Dauerbetrieb bleibt gesperrt,
- ein dauerhafter Integrationsweg benötigt entweder einen belegten offiziellen API-Pfad für den konkreten Account oder eine ausdrückliche Freigabe/Partnerlösung von Kleinanzeigen.

## Projektregistratur

Dieses Repository ist die **kanonische Registratur für mark-api**.

- Aufgaben und offene Punkte: **GitHub Issues dieses Repositories**
- Entscheidungen, Gesprächsstände und Evidenz: **docs/**
- Implementierung: dieses Repository
- **Für dieses Projekt keine Registrierung im Bureau.**

## Dokumentation

- Schriftliche Wunschvorstellung: [docs/requirements-source-2026-09-23.md](docs/requirements-source-2026-09-23.md)
- Produktspezifikation: [docs/product-spec.md](docs/product-spec.md)
- Anforderungen und offene Punkte: [docs/requirements.md](docs/requirements.md)
- Telefonat, Rohtranskription: [docs/call-2026-09-23-raw.md](docs/call-2026-09-23-raw.md)
- Telefonat, bereinigter Stand: [docs/call-2026-09-23.md](docs/call-2026-09-23.md)
- Ausgangskontext: [docs/context.md](docs/context.md)
- Entscheidungen: [docs/DECISIONS.md](docs/DECISIONS.md)
- Architekturentscheidung: [docs/architecture-decision-2026-09-24.md](docs/architecture-decision-2026-09-24.md)
- Realtest/PoC: [docs/poc-2026-09-24.md](docs/poc-2026-09-24.md)
- Integrationsoptionen: [docs/integration-options.md](docs/integration-options.md)

## Datenschutz / Öffentlichkeit

Das Repository ist öffentlich. Zugangsdaten, Tokens, Telefonnummern und sonstige nicht erforderliche personenbezogene Daten werden nicht committed. Private Originalmedien und nicht zur Veröffentlichung bestimmte Quelldateien bleiben außerhalb des Repositorys. Ausgewählte Transkriptionen und Anforderungsauszüge werden nur dann öffentlich dokumentiert, wenn sie für das Projekt erforderlich sind und keine unnötigen sensiblen oder personenbezogenen Inhalte enthalten.

## Arbeitsregel

Der Core wird entlang der bereits belegten Adaptergrenzen weiter gehärtet. Plattformwrites bleiben fail-closed: standardmäßig deaktiviert, an genau eine bekannte Anzeigen-ID gebunden, mit frischem Precondition-Read, genau einer Mutation, ohne Blind-Retry und mit unabhängigem Post-Readback. Delete verlangt zusätzlich eine explizite Freigabe für die konkrete Anzeigen-ID. Create/Publish bleibt bis zu einem separaten technischen und zulässigen Betriebs-Gate deaktiviert.
