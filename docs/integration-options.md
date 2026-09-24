# Integrationsoptionen für mark-api

Stand: 24.09.2026

## Ergebnis der erweiterten Recherche

Die frühere Schlussfolgerung „für den privaten Account ist kein praktikabler technischer Weg sichtbar“ war zu eng. Neben der offiziellen ProSellers-API existieren aktuelle Open-Source-Projekte, die große Teile des benötigten Funktionsumfangs für normale Kleinanzeigen-Accounts technisch bereits umsetzen.

Das ändert die technische Machbarkeit, nicht automatisch die Nutzungsbedingungen. Die inoffiziellen Pfade müssen deshalb als technische Kandidaten mit Account-/ToS- und Wartungsrisiko behandelt werden.

## Kandidat A — kleinanzeigen-bot-ui + kleinanzeigen-bot

Quellen:

- https://github.com/bkd3sign/kleinanzeigen-bot-ui
- https://github.com/Second-Hand-Friends/kleinanzeigen-bot

### Belegte Fähigkeiten

Das UI-Projekt dokumentiert aktuell:

- Anzeigen erstellen, bearbeiten, duplizieren und löschen,
- Bulk-Aktionen für publish/delete/update/activate/deactivate,
- Bilder hochladen, sortieren und verwalten,
- Anzeigen aus dem eigenen Profil synchronisieren,
- Live-Besucherzahlen, Watchlist-Zahlen und Ablaufdatum,
- Inbox mit Konversationsliste,
- Nachrichten lesen und senden,
- Dashboard und Charts,
- geplante/scheduled Bot-Jobs,
- Docker- sowie Linux/LXC-Betrieb.

Für die Live-Statistik verwendet das Projekt nach eigener Dokumentation die Kleinanzeigen-Management-Schnittstelle `m-meine-anzeigen-verwalten.json`. Dort werden u. a. `viewCount`, `watchCount`, Status und Ablaufdatum verarbeitet.

Das zugrunde liegende `Second-Hand-Friends/kleinanzeigen-bot` unterstützt publish, update, delete, republish, download und extend über Browser-Automation.

### Passung zu unserem Ziel

| Bedarf | Abdeckung |
|---|---|
| Anzeigen erstellen | ja |
| Anzeigen ändern/verwalten | ja |
| Anzeigen löschen | ja |
| Bilder verwalten | ja |
| Aufrufe/Besucher | ja, Management-Schnittstelle |
| Merker/Watchlist | ja |
| Nachrichten/Chats | ja |
| Dashboard | ja |
| Scheduling | ja |
| eigene zusätzliche Top-Listen/Analysen | darauf aufbaubar |

### Risiken

- Browser-Automation ist gegenüber UI-Änderungen fragiler als eine offizielle API.
- Das Upstream-Projekt weist selbst darauf hin, dass seine Nutzung gegen jeweils geltende Kleinanzeigen-Nutzungsbedingungen verstoßen kann.
- Login, MFA und Captcha können manuelle Eingriffe erfordern.
- Das UI speichert laut eigener README Kleinanzeigen-Zugangsdaten in `config.yaml` im Klartext; für einen produktiven Einsatz muss die Secret-Behandlung vor Übernahme verbessert werden.
- Nicht auf Anti-Bot-Umgehung als Betriebsgrundlage bauen.

### Einordnung

**Technisch stärkster aktueller PoC-Kandidat**, weil Anzeigenverwaltung, Statistik, Messaging und Dashboard bereits in einem System zusammengeführt sind. Noch keine Produktionsentscheidung.

---

## Kandidat B — monkrel/kleinanzeigen-api

Quelle:

- https://github.com/monkrel/kleinanzeigen-api

Das Projekt spricht direkt die private mobile JSON API an, die von der Kleinanzeigen-App verwendet wird.

### Belegte angemeldete Funktionen

- Konversationen auflisten,
- Nachrichten einer Konversation lesen,
- Antworten senden,
- eigene Anzeigen auflisten,
- Anzeige pausieren,
- Anzeige aktivieren,
- Anzeige löschen,
- Anzeige verlängern,
- neue Anzeige veröffentlichen,
- Watchlist lesen.

Damit ist dieser Pfad insbesondere für **CRUD + Messaging** technisch interessant und vermeidet für viele Operationen eine vollständige Browsersteuerung.

### Risiken

- private, nicht offiziell freigegebene App-API,
- Upstream warnt ausdrücklich, dass automatisierter eingeloggter Zugriff gegen Kleinanzeigen-Nutzungsbedingungen verstößt und zu Account-Sperren führen kann,
- App-Credentials bzw. private API-Oberflächen können von Kleinanzeigen geändert oder rotiert werden,
- junges/kleines Projekt; deutlich weniger belastbare Betriebshistorie als der größere kleinanzeigen-bot,
- deshalb eher Referenz/PoC als unkritische Produktionsbasis.

---

## Kandidat C — öffentliche/halböffentliche Lesewege

### View Counter

Eine aktuelle Reverse-Engineering-Referenz dokumentiert:

- `GET /s-vac-inc-get.json?adId=...` liefert einen Besucherzähler,
- die private CAPI besitzt ebenfalls Counter-Endpunkte.

Quelle:

- https://github.com/its-me-prash/kleinanzeigen-reader/blob/main/references/mobile-api.md

Für unsere eigene Statistik ist der öffentliche `s-vac-inc-get`-Weg **nicht ideal**, weil andere Implementierungen darauf hinweisen, dass der Aufruf den Zähler selbst erhöhen kann. Für unverfälschte eigene Kennzahlen ist die Management-Ansicht aus Kandidat A der bessere Prüfpfad.

### Kleinanzeigen Agent

- https://kleinanzeigen-agent.de/

Der Dienst bietet eine REST-/MCP-/A2A-Lese-API für Suchen, Anzeigen, Verkäufer und teilweise Views. Das ist für Marktvergleich und externe Vergleichsdaten interessant, ersetzt aber nach aktuellem Stand nicht die angemeldete Anzeigenverwaltung oder Inbox.

---

## Offizieller Pfad — ProSellers API

Die offizielle Consumer-Goods-/ProSellers-API bleibt der sauberste dokumentierte Schreibpfad, ist aber nur für professionelle Nutzer mit passenden Power-/Premium-Angeboten belegt.

Sie bleibt deshalb ein **bedingter** Kandidat, falls sich der Account-/Tarifrahmen ändert.

---

## Geänderter MVP-Fokus

Externe Text- und Bildgenerierung sind nach Projektentscheidung vom 24.09.2026 **kein MVP-Schwerpunkt**. Insbesondere Textgenerierung erzeugt keinen ausreichenden Zusatznutzen, wenn Kleinanzeigen selbst bereits Textunterstützung anbietet.

Der MVP soll stattdessen diese Kernfragen beantworten:

1. Können vorhandene eigene Anzeigen zuverlässig eingelesen und synchronisiert werden?
2. Kann eine Testanzeige erstellt, geändert, pausiert/aktiviert und gelöscht werden?
3. Können Besucher-/Watchlist-Werte ohne Verfälschung aus der Management-Sicht übernommen werden?
4. Können Konversationen gelesen und einer Anzeige zugeordnet werden?
5. Kann daraus pro Anzeige mindestens `views`, `watchers` und `conversation_count` gespeichert werden?
6. Lassen sich daraus erste Rankings nach Stadt und vom Nutzer vergebenen Variantenmerkmalen bilden?

## Empfohlener PoC

Noch keine vollständige Eigenentwicklung.

1. `bkd3sign/kleinanzeigen-bot-ui` isoliert als Referenz/PoC starten.
2. Mit genau einer Testanzeige prüfen:
   - Download/Sync,
   - Update,
   - Pause/Aktivierung,
   - Besucher/Watchlist,
   - Inbox-Zuordnung.
3. Parallel `monkrel/kleinanzeigen-api` nur für dieselben begrenzten eigenen Account-Operationen evaluieren.
4. Ergebnisse in einer Capability-Matrix vergleichen:
   - Funktionsabdeckung,
   - Stabilität,
   - notwendige manuelle Login-/MFA-Schritte,
   - Account-/ToS-Risiko,
   - Wartbarkeit.
5. Erst danach entscheiden, ob wir:
   - eines der Projekte forken/integrieren,
   - nur einzelne Clients/Endpunkte übernehmen,
   - oder eine eigene dünne Schicht bauen.

## Nicht tun

- keine großflächige Scraping-/Polling-Infrastruktur,
- keine Umgehung von Zugriffsschutz, Captchas oder Rate-Limits als Architekturprinzip,
- keine fremden Accounts oder privaten Daten,
- keine Secrets im öffentlichen `mark-api`-Repository.
