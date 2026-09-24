# Produktspezifikation

Stand: 23.09.2026

Status: **funktionaler Scope konkretisiert; Integrationsweg und Detail-Akzeptanz noch offen**

Quelle: docs/requirements-source-2026-09-23.md

## 1. Ziel

Für Marks Kleinanzeigen-Account soll eine Lösung entstehen, die Anzeigen verwaltet und Leistungsdaten in einem Dashboard auswertet. Aus den gesammelten Daten sollen Vergleiche, Ranglisten und Optimierungsvorschläge entstehen.

Die ursprünglich genannten Wünsche nach externer Titel-/Beschreibungsgenerierung und Bildgenerierung bleiben dokumentiert, sind nach Projektentscheidung vom 24.09.2026 jedoch **nicht MVP-priorisiert**. Der MVP konzentriert sich auf Anzeigen-CRUD, Synchronisation, Messaging und messbare Leistungsdaten.

## 2. Belegter Funktionsumfang

### 2.1 Anzeigen
- Anzeigen erstellen.
- Anzeigen löschen.
- Anzeigen verwalten.

„Verwalten“ ist als gewünschter Oberbegriff belegt; die darunter fallenden Einzeloperationen müssen noch präzisiert werden.

### 2.2 Textgenerierung — bestätigt, aber depriorisiert
- Titel aus Prompts generieren.
- Beschreibungstexte aus Prompts generieren.
- Generierte Inhalte für Anzeigen verwenden.

Diese ursprünglich bestätigte Anforderung ist **kein MVP-Schwerpunkt**.

### 2.3 Bildgenerierung — bestätigt, aber depriorisiert
- Bilder aus Prompts generieren.
- Generierte Bilder automatisch für Anzeigen verwenden.

Diese ursprünglich bestätigte Anforderung ist **kein MVP-Schwerpunkt**.

### 2.4 Dashboard
Ein Dashboard soll die relevanten Anzeigen- und Leistungsdaten sichtbar machen.

### 2.5 Datensammlung und Auswertung
Gewünscht sind mindestens:
- Aufrufe,
- die Kennzahl „wie viele geschrieben haben“; ob damit Chats, eindeutige Personen oder Nachrichten gemeint sind, ist noch offen,
- daraus berechnete Kennzahlen,
- Grafiken,
- Top-Listen.

### 2.6 Vergleichsdimensionen
Top-Listen bzw. Vergleiche sollen insbesondere nach folgenden Merkmalen möglich sein:
- Bild-Typ,
- Stadt,
- Text-Typ,
- Titel-Typ.

### 2.7 Empfehlungen
Aus den Daten sollen Vorschläge für die besten bzw. erfolgversprechendsten Lösungen abgeleitet werden.

## 3. Funktionale Anforderungen

- **FR-01:** Anzeigen erstellen.
- **FR-02:** Anzeigen löschen.
- **FR-03:** Anzeigen verwalten.
- **FR-04 (depriorisiert):** Titel anhand eines Prompts generieren.
- **FR-05 (depriorisiert):** Beschreibungstexte anhand eines Prompts generieren.
- **FR-06 (depriorisiert):** Bilder anhand eines Prompts generieren.
- **FR-07 (depriorisiert):** Generierte Bilder einer Anzeige automatisch zur Verwendung zuführen.
- **FR-08:** Aufrufzahlen erfassen, soweit der Integrationsweg diese Daten bereitstellt.
- **FR-09:** Die vom Auftraggeber gewünschte Kennzahl „wie viele geschrieben haben“ erfassen, sobald ihre Einheit (z. B. Chats, eindeutige Personen oder Nachrichten) definiert und technisch verfügbar ist.
- **FR-10:** Kennzahlen und Grafiken im Dashboard visualisieren.
- **FR-11:** Top-Listen nach Bild-Typ, Stadt, Text-Typ und Titel-Typ erzeugen.
- **FR-12:** Aus beobachteten Ergebnissen Optimierungsvorschläge ableiten.

## 4. Noch nicht belegte Detailanforderungen

Folgende Punkte dürfen nicht als bereits entschieden behandelt werden:

1. Welche Einzelaktionen umfasst „Anzeigen verwalten“ genau?
2. Welche Pflichtfelder muss eine Anzeige enthalten?
3. Welche Aktionen laufen automatisch und welche benötigen Freigabe?
4. Was bedeutet „wie viele geschrieben haben“ exakt: Chats, eindeutige Interessenten oder Nachrichten?
5. Wie werden Bild-, Text- und Titel-Typen klassifiziert?
6. Welche Zeiträume und Vergleichsgruppen gelten im Dashboard?
7. Wo soll die Anwendung laufen?
8. Welche Kosten- und Betriebsgrenzen gelten?
9. Welche Daten dürfen wie lange gespeichert werden?
10. Welche Mindestqualität bzw. Zielwerte definieren „beste Lösung“?

## 5. Technische Gates

Vor einer dauerhaften Architekturentscheidung muss belegt werden:

1. Welcher zulässige Integrationsweg für Kleinanzeigen die benötigten Lese- und Schreibaktionen ermöglicht.
2. Ob Aufrufe und Nachrichten-/Interessentenmetriken technisch abrufbar sind.
3. Welche Authentifizierung, Limits und Nutzungsbedingungen gelten.
4. Welche Aktionen tatsächlich automatisierbar sind und wo menschliche Freigaben erforderlich sind.

## 6. Vorgeschlagener MVP

Der MVP ist ein **Planungsvorschlag**, keine bereits bestätigte Detailanforderung.

Ziel ist ein realer, begrenzter End-to-End-Pfad rund um **Verwaltung und Messbarkeit**:

1. eine vorhandene eigene Anzeige einlesen/synchronisieren,
2. eine Testanzeige erstellen,
3. diese ändern sowie pausieren/aktivieren,
4. Besucher- und Watchlist-Zahlen erfassen,
5. zugehörige Konversationen lesen und als definierte Reaktionsmetrik speichern,
6. die Testanzeige wieder löschen,
7. die Messwerte in einem einfachen Dashboard anzeigen.

Text- und Bildgenerierung werden erst wieder aufgenommen, wenn dafür ein konkreter Zusatznutzen gegenüber Kleinanzeigen selbst belegt ist.

Technische Kandidaten und PoC-Plan: `docs/integration-options.md`.

## 7. Erfolgskriterium für die nächste Phase

Die Discovery-Phase ist technisch abgeschlossen, wenn für jede benötigte Lese- und Schreibaktion ein zulässiger, reproduzierbarer Integrationsweg belegt oder eine konkrete Nicht-Verfügbarkeit dokumentiert ist.
