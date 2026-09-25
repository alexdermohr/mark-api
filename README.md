# mark-api

Öffentliche Projektdokumentation für die mit Mark besprochene Kleinanzeigen-Agent-/API-Integration.

## Status

**Ein-Anzeigen-Realtest abgeschlossen / technische MVP-Architektur festgelegt / Core-Implementierung startet** — Stand: 24.09.2026.

Mark hat nach dem Telefonat die gewünschte Funktionalität schriftlich konkretisiert. Gewünscht sind Anzeigenverwaltung, ein Dashboard sowie datenbasierte Auswertungen und Optimierungsvorschläge. Externe Text-/Bildgenerierung war ursprünglich Teil des Wunsches, ist seit 24.09.2026 aber nicht mehr MVP-priorisiert.

## Aktuell belegt

- Anzeigen **erstellen, löschen und verwalten**.
- Titel und Beschreibungstexte **aus Prompts generieren**.
- Bilder **aus Prompts generieren und automatisch für Anzeigen verwenden**.
- Dashboard für Anzeigen- und Leistungsdaten.
- Datensammlung zu **Aufrufen** und dazu, **wie viele geschrieben haben**.
- Grafiken und Top-Listen nach **Bild-Typen, Städten, Text-Typen und Titel-Typen**.
- Daraus Vorschläge für die besten bzw. erfolgversprechendsten Lösungen ableiten.
- Modellwahl ist derzeit kein Kriterium; entscheidend ist, dass die Lösung zuverlässig funktioniert.

## Wichtigstes Gate

Der technische Ein-Anzeigen-PoC ist abgeschlossen. Belegt sind Sync, in-place Update, Pause/Aktivierung, Verkäufermetriken, positiver Inbox/adId-Fall und Delete mit unabhängigen Besitzerlisten-Readbacks.

Für den MVP gilt D-006/D-007: dünne eigene Python-Schicht mit Capability-Adaptern; SQLite für den ersten Core-Slice. Produktions-/ToS-Freigabe und ein separater Create/Publish-Realtest bleiben vor schreibendem Dauerbetrieb offen.

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

Ab jetzt entlang der belegten Adaptergrenzen implementieren: frameworkfreier Python-Core, diskriminierte Read-Ergebnisse, SQLite-Snapshots, danach Management- und Mobile-API-Adapter. Browserautomation bleibt auf Fähigkeiten beschränkt, für die kein engerer belegter API-Pfad existiert. Plattformwrites sind standardmäßig deaktiviert und benötigen Pre-/Post-Readbacks auf eine explizite Anzeigen-ID.
