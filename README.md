# mark-api

Öffentliche Projektdokumentation für die mit Mark besprochene Kleinanzeigen-Agent-/API-Integration.

## Status

**Funktionaler Scope konkretisiert / technische Discovery offen** — Stand: 23.09.2026.

Mark hat nach dem Telefonat die gewünschte Funktionalität schriftlich konkretisiert. Gewünscht sind Anzeigenverwaltung, KI-generierte Titel/Beschreibungen/Bilder, ein Dashboard sowie datenbasierte Auswertungen und Optimierungsvorschläge.

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

Der funktionale Zielumfang ist jetzt ausreichend konkret für die technische Discovery. Noch offen ist, **welcher zulässige Kleinanzeigen-Integrationsweg** die benötigten Lese- und Schreibaktionen tatsächlich ermöglicht und welche Kennzahlen verfügbar sind.

Bis das belegt ist, wird keine API-, Browser-, Hosting- oder Modellarchitektur dauerhaft festgelegt.

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

## Datenschutz / Öffentlichkeit

Das Repository ist öffentlich. Zugangsdaten, Tokens, Telefonnummern und sonstige nicht erforderliche personenbezogene Daten werden nicht committed. Private Originalmedien und nicht zur Veröffentlichung bestimmte Quelldateien bleiben außerhalb des Repositorys. Ausgewählte Transkriptionen und Anforderungsauszüge werden nur dann öffentlich dokumentiert, wenn sie für das Projekt erforderlich sind und keine unnötigen sensiblen oder personenbezogenen Inhalte enthalten.

## Arbeitsregel

Zuerst den real verfügbaren Integrationsweg und die verfügbaren Daten belegen. Danach genau einen kleinen End-to-End-Fall umsetzen. Analyse-, Ranking- und Optimierungsfunktionen werden auf real verfügbaren und sauber definierten Metriken aufgebaut.
