# Anforderungen

Stand: 04.10.2026

## Belegt

| Punkt | Stand |
|---|---|
| Nutzung | privat |
| Zielplattform | Kleinanzeigen-Account |
| Vorhaben | Agent-/API-Integration mit Anzeigenverwaltung, KI-Inhalten und Auswertung |
| Modellwahl | derzeit keine Präferenz; lokal/kostenlos ist nicht gefordert |
| Hauptkriterium | die Lösung soll zuverlässig funktionieren |
| Anzeigen | erstellen, löschen, verwalten; der MVP-Verwaltungsscope ist technisch präzisiert |
| Text | Titel und Beschreibungen aus Prompts generieren; für den MVP depriorisiert |
| Bilder | aus Prompts generieren und automatisch für Anzeigen verwenden; für den MVP depriorisiert |
| Dashboard | gewünscht und als read-only Dashboard/API umgesetzt |
| Daten | Aufrufe sowie getrennte `conversation_count`, `unique_buyer_count` und `inbound_message_count`; welche davon „wie viele geschrieben haben“ bedeutet, bleibt offen |
| Auswertung | Kennzahlen, Grafiken und Top-Listen |
| Vergleichsdimensionen | Bild-Typen, Städte, Text-Typen, Titel-Typen |
| Ergebnis | Vorschläge für die besten bzw. erfolgversprechendsten Lösungen; die dafür maßgebliche Zielmetrik bleibt offen |

Primärquelle für diese Konkretisierung: `docs/requirements-source-2026-09-23.md`.

Die normalisierte Produktspezifikation liegt in `docs/product-spec.md`. Technische und betriebliche Entscheidungen werden in `docs/DECISIONS.md` fortgeschrieben.

## Aktueller MVP-Verwaltungsscope

„Anzeigen verwalten“ ist für den vorhandenen Core inzwischen präzisiert als:

- bestehende eigene Anzeigen synchronisieren,
- Titel und Beschreibung einer bestehenden Anzeige in-place aktualisieren,
- pausieren beziehungsweise reservieren,
- aktivieren,
- löschen,
- Besitzerstatus sowie Views, Merker und Replies lesen,
- Inbox/Conversation einer Anzeigen-ID zuordnen.

Create/Publish bleibt ein separat gegateter Write-Pfad und ist kein implizit freigegebener Standardbetrieb.

Schreibende Operationen bleiben fail-closed: Writes sind standardmäßig deaktiviert, Ziel-IDs werden frisch owner-verifiziert, es gibt genau einen Mutationsversuch ohne Blind-Retry und einen unabhängigen Post-Readback. Delete verlangt zusätzlich eine explizite ID-gebundene Freigabe. Die konkreten Runtime- und Write-Sicherheitsentscheidungen sind in D-010 bis D-018 dokumentiert.

## Expliziter Analytics-Contract

Der technische Contract löst die offenen Produktentscheidungen nicht durch Defaults. `reaction_metric` ist entweder nicht gesetzt oder explizit genau eine der drei getrennten Reaktionsmetriken. `objective_metric` ist entweder nicht gesetzt oder explizit eine vorhandene Analytics-Rohmetrik. Ohne konfigurierte Zielmetrik gibt es keine implizite Optimierungs- oder „beste Lösung“-Semantik; deskriptive Rankings verlangen weiterhin eine explizit angeforderte Metrik.

Fehlende Messwerte werden ausgelassen, beobachtete Nullwerte bleiben echte Nullwerte. Rankings und Charts bleiben deskriptiv und begründen weder Kausalität noch automatisch eine Qualitätsaussage.

## Verbleibende fachliche Entscheidungen

Issue #1 enthält nur noch zwei fachliche Restentscheidungen:

1. Welche der getrennten Rohmetriken `conversation_count`, `unique_buyer_count` oder `inbound_message_count` soll fachlich „wie viele geschrieben haben“ bedeuten?
2. Welche explizite `objective_metric` oder später ausdrücklich definierte Zielfunktion bestimmt, wann eine Variante als „beste Lösung“ gilt?

Keine dieser Entscheidungen wird technisch vorweggenommen. Insbesondere wird weder die erste verfügbare Reaktionsmetrik noch eine bevorzugte Optimierungsmetrik automatisch gewählt.

Frühere Discovery-Fragen zu Verwaltungsscope, Integrationsarchitektur, Lese-/Schreibsicherheit und Runtime-Komposition sind durch die dokumentierten Entscheidungen und implementierten Contracts geklärt oder als separate Betriebs-/Freigabegates abgegrenzt; sie sind keine offenen Detail-Akzeptanzpunkte von Issue #1 mehr.

## Gate

**Issue #1 bleibt offen, bis ein Mensch die Reaktionsmetrik und das Optimierungsziel ausdrücklich festlegt.**

Bis dahin bleiben beide Analytics-Felder default-off. Reale Plattformwrites werden nicht aus Dokumentations- oder Regressionstestgründen ausgelöst und benötigen weiterhin ihre separaten technischen und menschlichen Freigaben.
