# Entscheidungen

## D-001 — Projektregistratur

**Entscheidung:** `alexdermohr/mark-api` ist die kanonische Registratur für dieses Projekt.

**Folgen:**
- Aufgaben und offene Arbeit werden als GitHub Issues hier geführt.
- Entscheidungen und Gesprächsevidenz liegen im Repository.
- Für `mark-api` werden keine neuen Bureau-Einträge angelegt.

## D-002 — Öffentliches Repository, private Rohquellen

**Entscheidung:** Das Repository ist öffentlich; die Audiodatei selbst bleibt privat außerhalb des Repositorys.

**Folgen:**
- Transkription und sachlich erforderliche Projektinformation dürfen dokumentiert werden.
- Zugangsdaten, Tokens, Telefonnummern und unnötige personenbezogene Daten werden nicht committed.

## D-003 — Noch keine technische Festlegung

**Entscheidung:** Der Integrationsweg wird erst nach Marks schriftlicher Spezifikation und einer Prüfung der tatsächlich verfügbaren Kleinanzeigen-Schnittstellen festgelegt.

**Begründung:** Das Telefonat benennt das Zielsystem, aber nicht die Agentenaktionen. Eine API-, Browser- oder Modellarchitektur wäre derzeit vorzeitig.

## D-004 — Schriftlicher Funktionsscope liegt vor

**Entscheidung:** Die schriftliche Wunschvorstellung vom 23.09.2026 ist ab jetzt die maßgebliche fachliche Konkretisierung für den Funktionsumfang.

**Belegt sind:** Anzeigen erstellen/löschen/verwalten, Titel- und Beschreibungsgenerierung per Prompt, Bildgenerierung mit automatischer Anzeigenverwendung, Dashboard, Datenauswertung zu Aufrufen und zur noch zu definierenden Kennzahl „wie viele geschrieben haben“, Grafiken/Top-Listen nach Bild-Typ/Stadt/Text-Typ/Titel-Typ sowie datenbasierte Optimierungsvorschläge.

**Nicht entschieden:** genaue Einzeloperationen unter „verwalten“, Metrikdefinitionen, Freigaberegeln, Hosting, Modellarchitektur und Kleinanzeigen-Integrationsweg.

**Folge:** Issue #1 bleibt bis zur Klärung der noch offenen Detail-Akzeptanzpunkte offen; die technische Discovery in #2 kann parallel auf Basis des jetzt belegten Funktionsscopes beginnen.


## D-005 — MVP-Fokus auf Automation und Messbarkeit

**Entscheidung:** Externe Text- und Bildgenerierung werden ab 24.09.2026 nicht als MVP-Schwerpunkt behandelt.

**Begründung:** Der Auftraggeber bewertet insbesondere zusätzliche Textgenerierung als geringen Zusatznutzen, weil Kleinanzeigen selbst Textunterstützung anbietet. Der knappe technische Hebel liegt stattdessen bei Anzeigen-CRUD, Synchronisation, Besucher-/Watchlist-Daten, Inbox/Interessenten und darauf aufbauender Analyse.

**Folgen:**
- Die ursprünglich genannten Generierungswünsche bleiben als historische Anforderung dokumentiert, sind aber für den MVP depriorisiert.
- Die technische Discovery priorisiert vorhandene Lösungen für CRUD, Messaging und Statistik.
- Noch keine endgültige Architekturwahl: `kleinanzeigen-bot-ui`/`kleinanzeigen-bot` und `monkrel/kleinanzeigen-api` werden als PoC-Kandidaten geprüft.
- Account-/ToS- und Wartungsrisiken inoffizieller Schnittstellen bleiben ein Gate.
