# Anforderungen

Stand: 23.09.2026

## Belegt

| Punkt | Stand |
|---|---|
| Nutzung | privat |
| Zielplattform | Kleinanzeigen-Account |
| Vorhaben | Agent-/API-Integration mit Anzeigenverwaltung, KI-Inhalten und Auswertung |
| Modellwahl | derzeit keine Präferenz; lokal/kostenlos ist nicht gefordert |
| Hauptkriterium | die Lösung soll zuverlässig funktionieren |
| Anzeigen | erstellen, löschen, verwalten |
| Text | Titel und Beschreibungen aus Prompts generieren |
| Bilder | aus Prompts generieren und automatisch für Anzeigen verwenden |
| Dashboard | gewünscht |
| Daten | Aufrufe und Anzahl der Personen, die geschrieben haben |
| Auswertung | Kennzahlen, Grafiken und Top-Listen |
| Vergleichsdimensionen | Bild-Typen, Städte, Text-Typen, Titel-Typen |
| Ergebnis | Vorschläge für die besten bzw. erfolgversprechendsten Lösungen |

Primärquelle für diese Konkretisierung: docs/requirements-source-2026-09-23.md.

Die normalisierte Produktspezifikation liegt in docs/product-spec.md.

## Plausibel, aber noch nicht belegt

- „Verwalten“ kann Bearbeiten, Pausieren, Duplizieren oder erneutes Einstellen umfassen; welche Operationen wirklich gemeint sind, ist offen.
- Für Empfehlungen werden vergleichbare historische oder experimentelle Daten benötigt.
- Für einige Funktionen könnte eine offizielle API, ein anderer zulässiger Integrationsweg oder Browser-/UI-Automation erforderlich sein.
- Der geeignete technische Weg hängt von den tatsächlich verfügbaren Kleinanzeigen-Schnittstellen und erlaubten Aktionen ab.

Diese Punkte sind keine bestätigten Detailanforderungen.

## Fehlt vor Implementierungsbeginn

1. Welche Einzelaktionen umfasst „verwalten“?
2. Welche Ereignisse lösen Aktionen aus?
3. Welche schreibenden Aktionen benötigen menschliche Freigabe?
4. Welche Kleinanzeigen-Zugänge bzw. offiziellen Schnittstellen stehen zur Verfügung?
5. Sind Aufrufe und Interessenten-/Nachrichtenmetriken technisch abrufbar?
6. Was bedeutet „wie viele geschrieben haben“ exakt?
7. Wie werden Bild-, Text- und Titel-Typen klassifiziert?
8. Wo soll die Lösung laufen?
9. Welche Kosten sind akzeptabel?
10. Welche Fehler-, Logging- und Wiederanlaufregeln werden erwartet?
11. Welche Daten dürfen gespeichert werden und wie lange?
12. Woran wird objektiv festgestellt, dass eine Variante „besser“ ist?

## Gate

**Keine API-, Browser-, Hosting- oder Modellarchitektur als dauerhaft gesetzt behandeln, bevor der Kleinanzeigen-Integrationsweg und die benötigten Lese-/Schreiboperationen praktisch belegt sind.**

Danach wird der kleinste ausreichende End-to-End-Pfad umgesetzt und an einem realen, begrenzten Fall belegt.
