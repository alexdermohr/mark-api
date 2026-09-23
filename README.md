# mark-api

Öffentliche Projektdokumentation für die mit Mark besprochene Agent-/API-Integration.

## Status

**Discovery / Anforderungen ausstehend** — Stand: 23.09.2026.

Im Telefonat wurde nur der Rahmen geklärt. Die konkrete Funktion des Agenten ist noch nicht beschrieben. Mark will die genaue Wunschvorstellung schriftlich per WhatsApp nachreichen.

## Aktuell belegt

- Ziel ist eine Agent-/API-Integration für Marks **Kleinanzeigen-Account** zur privaten Nutzung.
- Die Wahl zwischen lokalen, kostenlosen oder anderen Modellen ist für Mark derzeit kein Kriterium.
- Seine explizite Priorität: **„Ich brauche nur, dass das alles funktioniert.“**
- Welche Aktionen der Agent konkret ausführen soll, ist noch offen.
- Der nächste harte Input ist Marks detaillierte schriftliche Anforderung.

## Projektregistratur

Dieses Repository ist die **kanonische Registratur für `mark-api`**.

- Aufgaben und offene Punkte: **GitHub Issues dieses Repositories**
- Entscheidungen, Gesprächsstände und Evidenz: **`docs/`**
- Implementierung: dieses Repository
- **Für dieses Projekt keine Registrierung im Bureau.**

## Datenschutz / Öffentlichkeit

Das Repository ist öffentlich. Deshalb werden hier keine Zugangsdaten, Tokens, Telefonnummern oder sonstige nicht erforderliche personenbezogene Daten abgelegt. Die private Audiodatei bleibt außerhalb des Repositories; dokumentiert wird nur die Transkription und daraus abgeleitete Projektinformation.

## Quellen

- Private Gesprächsaufnahme `mark.m4a`, 23.09.2026, Google Drive (nicht im Repository)
- Automatische Rohtranskription: [docs/call-2026-09-23-raw.md](docs/call-2026-09-23-raw.md)
- Bereinigter Gesprächsstand: [docs/call-2026-09-23.md](docs/call-2026-09-23.md)
- Anforderungen und Lücken: [docs/requirements.md](docs/requirements.md)
- Entscheidungen: [docs/DECISIONS.md](docs/DECISIONS.md)

## Arbeitsregel

Keine Architektur festlegen, bevor die konkreten gewünschten Agentenaktionen und der tatsächlich verfügbare Kleinanzeigen-Integrationsweg geklärt sind. Danach wird der kleinste ausreichende End-to-End-Pfad umgesetzt.
