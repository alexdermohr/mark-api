# Mark – aktuelles Betriebs- und Recovery-Runbook

Stand: 09.10.2026. Dieses Runbook beschreibt den installierten technischen
Produktpfad. Eine laufende lokale Write-API und ein belegter, zulässiger
Kleinanzeigen-Live-Write sind unterschiedliche Abnahmegegenstände.

## Installation und Betrieb

Python 3.12+, eine bereits authentifizierte lokale Chrome-/Chromium-Sitzung
mit Loopback-CDP und eine bestehende sichere Datenbankdatei werden benötigt.

~~~bash
python3.12 -m pip install '.[private-web]'

# Einmalige Erstinitialisierung einer neuen Datenbank
mark-api-launch --init-db --db /sicherer/pfad/mark.sqlite --cdp-port 9222

# Weitere Starts ohne --init-db
mark-api-launch --db /sicherer/pfad/mark.sqlite --cdp-port 9222
~~~

Der normale Launcher bietet die vorhandenen Create-/Media-, Update-,
Pause-/Aktivierungs- und Delete-Capabilities über die lokale Write-API
**default-on** an. Kein zusätzlicher technischer Produkt-Opt-in ist notwendig.
Einzeloperationen bleiben an Bearer, Idempotency-Key, eindeutige ID,
frische Ownership-Reads, Confirmation, TOCTOU, unabhängigen Post-Readback
und No-Blind-Retry gebunden. Das Dashboard benutzt Same-Origin-Proxy und
einen getrennten per-process Token im URL-Fragment; Tokens nicht loggen.

Der Launcher startet keinen Browser und umgeht weder Login noch MFA oder
CAPTCHA. Für den aktuellen konkreten Account sind gesonderte erlaubte
Plattformrechte/PRO-API-Credentials und eine reale aktuelle Live-Write-Abnahme
nicht nachgewiesen. Issue #2 dokumentiert den offiziellen
Privat-/PRO-/Partner-Betriebsrahmen. Diese Anleitung ermächtigt daher keine
Testanzeigen oder Plattformmutationen allein zu Abnahmezwecken.

Diagnose nach erfolgreichem Launcher-Start:

~~~bash
curl --fail http://127.0.0.1:8765/healthz
curl --fail http://127.0.0.1:8765/readyz
curl --fail http://127.0.0.1:8765/api/sync/status
~~~

healthz ist ausschließlich HTTP-Liveness; readyz validiert SQLite und
kritische Write-Recovery-Tabellen. Der separate Sync-Status berichtet
den letzten Versuch, den letzten tatsächlichen erfolgreichen Abschluss
und gegebenenfalls Fehler beziehungsweise in_progress/unklar. Vorhandene
alte Anzeigen- oder Metrik-Snapshots beweisen keinen neuen Sync-Erfolg.
Der Product Launcher führt derzeit genau einen Startsync aus, keinen
periodischen Scheduler.

Eine rein lokale E-Mail-/SQLite-/Analytics-Integration kann mit einer
selbst bereitgestellten validen RFC822-.eml-Datei überprüft werden:

~~~bash
mark-api-local-smoke /pfad/zu/eigener-nachricht.eml
~~~

Dieser Smoke ruft weder Kleinanzeigen noch die Write-API auf.

## WAL-konsistentes, nicht überschreibendes SQLite-Backup

Die neue installierte **mark-api-backup**-CLI benutzt SQLite Online Backup
statt einer unsicheren Kopie der live genutzten Datenbankdatei. Committe
WAL-Daten werden in einem konsistenten Punkt-in-Zeit-Snapshot erfasst.

~~~bash
mkdir -m 700 -p /sicherer/backup-ordner
mark-api-backup --db /sicherer/pfad/mark.sqlite \
  --backup /sicherer/backup-ordner/mark-2026-10-09.sqlite
~~~

- Das Ziel darf *noch nicht existieren*. Auch Symlinks und defekte
  Symlinks werden nicht überschrieben. Das reale Elternverzeichnis
  muss vorhanden sein. Die Quelle darf kein Symlink/Hardlink-Alias sein.
- Vor dem Backup wird die aktuelle Mark-Datenbank read-only inklusive
  Recovery-, Idempotenz- und Sync-Journal-Schema validiert. Der private
  Stage wird nach dem SQLite-Backup unabhängig auf volle Integrität
  und Bereitschaft geprüft.
- Der vollständig geprüfte Stage wird auf 0400 gesetzt, fsynct und
  per atomarem create-only Hardlink veröffentlicht; das Verzeichnis
  wird ebenfalls fsynct. Es wird niemals in die Quelldatenbank
  geschrieben, kein Plattformrequest gestartet und kein bestehendes
  Backup überschrieben.
- Der JSON-Receipt enthält SHA-256 und reine Mengenangaben,
  darunter pending_api_writes, pending_dashboard_writes und
  open_sync_attempts. Keine Nachrichtentexte oder Zugangsdaten.
- Scheitert die Veröffentlichung spät, kann der Backup-Zielpfad
  bereits bestehen. Zuerst dessen Zustand und Hash prüfen und
  **nicht blind wiederholen**.
- Es ist ein konsistenter Zeitpunkt, keine Zusicherung, dass keine
  externen Plattformaktionen während oder nach dem Snapshot liefen.
  Offene Pending-Write-Fences werden mitgesichert, nicht quittiert.

Backups enthalten schützenswerte Nutzerdaten. Gesicherte private
Aufbewahrung/Verschlüsselung, Dateizugriffsrechte und gegebenenfalls
Medien-/Browserprofile sind getrennte Betreiberaufgaben. Persistente
SQLite-Daten allein stellen keine CDP-Sitzung und keine opaken
temporären Media-Staging-Handles wieder her.

## Restore – ausschließlich in neue Datei

1. Mark, Dashboard, Write-API und andere SQLite-Nutzer stoppen.
   Zuvor offene oder unklare externe Write-Ausgänge separat
   dokumentieren, ohne sie durch einen Retry zu verändern.
2. SHA-256-Receipt, Backupzeitpunkt und alle möglichen späteren
   Plattformwirkungen prüfen. Ein altes Backup ist kein Nachweis
   eines aktuellen Besitzerzustands.
3. Backup nur in einen **nicht existierenden** neuen Datenbankpfad
   kopieren; weder Original noch Backup überschreiben. Die neue
   Kopie auf 0600 setzen und ihren SHA-256 mit dem Receipt vergleichen.
4. Den neuen Pfad vor produktiver Verwendung lokal prüfen:

~~~bash
python3.12 -c 'import sys; from mark_api.storage import SnapshotStore; s=SnapshotStore(sys.argv[1], create_if_missing=False); assert s.is_ready()' /neuer/pfad/restored-mark.sqlite
~~~

5. Letzten Sync, Anzeigenhistorie, offene Write-API-Requests und
   Dashboard-Pending-Writes prüfen. Ein Sync in_progress bleibt
   ungeklärt. Ein verlorenes versioniertes Sync-Journal oder
   fehlende Write-Recovery-Tabellen blockieren den Neustart
   fail-closed. Niemals Idempotency-/Recovery-Fences löschen
   oder durch einen neuen Plattform-Key umgehen.
6. Erst nach zulässiger tatsächlicher Owner-/Plattform-Reconciliation
   den Launcher mit dem **neuen** DB-Pfad starten. Es gibt keinen
   automatisch ausgelösten Submit-Retry.

Für historische unvollständige Mark-Datenbanken ist ausschließlich der
gesonderte bestätigungspflichtige Offline-Import
**mark-api-migrate-legacy** vorgesehen. Das Backup-Tool ersetzt keine
Recovery-Attestation. Fehlende historische Recovery-Tabellen beweisen
nicht, dass früher nie Plattformwrites durchgeführt wurden.

## Offene Produkt-Abnahme

| Bereich | Tatsächlicher Nachweis |
| --- | --- |
| Paketinstallation und sechs bisherige CLI-Einstiege | installierter lokaler Smoke |
| Default-on Write-Wiring, Auth, Idempotenz und Confirmation | umfangreiche lokale Tests, kein aktueller Live-Write |
| E-Mail → SQLite → Dashboard/Analytics | installierter lokaler Smoke |
| Per-Metrik-Provenienz und separate Sync-Erfolge | lokale Tests und PR #73/#74 CI |
| WAL-Backup/Restore mit pending Write-Fences | synthetische lokale Abnahme |
| Aktueller Kundendatenbestand / Endnutzergerät | nicht abgenommen |
| Explizit erlaubter PrivateWeb-/PRO-Live-Betrieb | für aktuellen Account nicht belegt |
| Reales Create/Update/Delete/Media End-to-End | nicht abgenommen |
| Scheduler, Dauerbetrieb, Monitoring | nicht abgenommen |

Die fachliche Wahl der Interessentenmetrik und der Optimierungszielfunktion
bleibt in Issue #1 offen. Historische Real-Smokes sind kein Beweis
für den heutigen vollständigen Live-Betrieb.
