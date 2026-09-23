# Anforderungen

Stand: 23.09.2026

## Belegt

| Punkt | Stand |
|---|---|
| Nutzung | privat |
| Zielplattform | Kleinanzeigen-Account |
| Vorhaben | Agent-/API-Integration |
| Modellwahl | derzeit keine Präferenz; lokal/kostenlos ist nicht gefordert |
| Hauptkriterium | die Lösung soll zuverlässig funktionieren |
| Konkrete Agentenaktionen | **noch nicht beschrieben** |
| Nächster Input | detaillierte schriftliche Wunschvorstellung von Mark per WhatsApp |

## Plausibel, aber noch nicht belegt

- Der Agent soll Aufgaben innerhalb oder rund um den Kleinanzeigen-Account automatisieren.
- Dafür könnte eine offizielle API, ein anderer zulässiger Integrationsweg oder eine Browser-/UI-Automation nötig sein.
- Der richtige technische Weg hängt vollständig davon ab, welche Lese- und Schreibaktionen tatsächlich verlangt werden.

Diese Punkte sind noch keine Anforderungen.

## Fehlt vor Implementierungsbeginn

1. Welche konkreten Aktionen soll der Agent ausführen?
2. Welche Ereignisse lösen Aktionen aus?
3. Was darf nur gelesen, was darf verändert oder versendet werden?
4. Muss vor schreibenden Aktionen eine menschliche Freigabe erfolgen?
5. Welche Kleinanzeigen-Zugänge bzw. offiziellen API-Möglichkeiten stehen zur Verfügung?
6. Welche weiteren Systeme oder Modelle sollen angebunden werden?
7. Wo soll die Lösung laufen?
8. Welche Kosten sind akzeptabel?
9. Welche Fehler-, Logging- und Wiederanlaufregeln werden erwartet?
10. Welche Daten dürfen gespeichert werden und wie lange?
11. Woran wird objektiv festgestellt, dass der Auftrag erfüllt ist?

## Gate

**Keine Architektur und keine Automationsmethode als dauerhaft gesetzt behandeln, bevor mindestens die Punkte 1, 3, 5 und 11 beantwortet sind.**

Danach: kleinsten ausreichenden End-to-End-Pfad wählen und zuerst an einem realen, begrenzten Fall belegen.
