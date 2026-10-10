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

Die Quell-Inode-Attestation benötigt Linux mit lesbarem `/proc/self/fd`.
Das Backup vorzugsweise als **eigenständigen CLI-Prozess** ausführen:
im selben Prozess vorbestehende SQLite-Hauptdateideskriptoren fremder Datenbanken
oder eine nicht eindeutig bestimmbare Quellbindung führen zum sicheren Abbruch.
Auch die tatsächlichen `-wal`-Dateideskriptoren und neu eingebundenen
`-shm`-Mappings werden über die Linux-Inode-Identität geprüft. Zusätzlich
werden Größe, Änderungszeit und Linux-`ctime` der Hauptdatei und der
vorhandenen WAL vor und nach dem Snapshot verglichen. SQLite kann beim
normalen Öffnen eines bestehenden WAL `-shm`-Metadaten und je nach
SQLite-Version auch dessen `ctime` aktualisieren. Für vorhandene WAL-Dateien
wird deshalb vor und nach dem Öffnen der vollständige SHA-256 verglichen,
bevor die WAL-Metadatenbasis auf den Post-Open-Zustand gesetzt wird.
Ab dann müssen WAL-/SHM-Inhalte und Metadaten bis zur Veröffentlichung stabil
bleiben. Andernfalls wird die Sicherung sicher abgebrochen.

Sind anfänglich **weder `-wal` noch `-shm` vorhanden**, unterscheidet
die CLI anhand des validierten SQLite-Hauptdateiheaders:
Bei **WAL-Modus** nutzt sie `immutable=1`, damit später eingeschleuste
WAL-Frames nicht gelesen werden; eine neu auftauchende Sidecar-Datei
blockiert das Backup. Bei **Rollback-Journalmodus** öffnet sie SQLite
dagegen normal read-only mit SQLite-Lesesperren, damit parallele
Produkt-Writes keinen inkonsistenten Snapshot verursachen.
Bei bereits vorhandenen vollständigen WAL-/SHM-Sidecars bleibt der
normale WAL-konsistente SQLite-Read einschließlich committeter Frames aktiv.
Fehlen beide Sidecars, werden zusätzlich alle serialisierten Snapshot-Seiten
mit den roh aus dem geprüften Hauptdatei-Deskriptor gelesenen Seiten
verglichen; lediglich der von SQLite beim Backup veränderbare
Schema-Cookie im Header ist ausgenommen. Ein zwar valider, aber während
des Öffnens veralteter SQLite-Snapshot führt damit zum Abbruch statt zum
Verlust offener Write-Recovery-Einträge. Dieser Abgleich ist keine
kontinuierliche Kernel-Isolation gegen einen aktiv böswilligen Prozess mit
denselben Dateirechten und manipulierbaren Prüfzeitpunkten.
Eine allein vorhandene WAL ohne ihre SHM-Datei blockiert das Backup
fail-closed; der Betreiber muss die SQLite-Wiederherstellung klären.
Ein vorhandenes SQLite-Rollback-Journal (`-journal`), insbesondere nach
einem abgestürzten Writer, blockiert jedes Backup bis zur zulässigen
Offline-Recovery; `immutable=1` darf ein solches Journal nicht umgehen.
Fehlende procfs-Verifikation führt niemals zum unsicheren Pfad-Fallback.

Der SQLite-Online-Snapshot wird zuerst ausschließlich im privaten
Prozessspeicher angelegt, einschließlich vollständiger Integritätsprüfung
und Recovery-Zähler. Erst danach werden seine serialisierten Bytes über
einen unbenannten Linux-`O_TMPFILE`-Deskriptor im vorhandenen
Backup-Zielverzeichnis geschrieben. Ein durch andere Prozesse austauschbarer
temporärer SQLite-Pfad oder ein SQLite-Journal für die Stage existiert
nicht. SHA-256, Stage-Inode und Zielverzeichnis werden vor und nach der
atomaren create-only-Veröffentlichung geprüft.

**Betriebsanforderungen:** Linux mit lesbarem procfs, ein Ziel-Dateisystem
mit `O_TMPFILE` und ausreichend freier Arbeitsspeicher für den vollständigen
SQLite-Snapshot **plus** seine serialisierte Kopie. Der Spitzenbedarf kann
deutlich größer sein als die Datenbankdatei. Fehlt eine Voraussetzung,
wird keine bestätigte Sicherung veröffentlicht; es gibt keinen unsicheren
Pfad-Fallback. Insbesondere für große Datenbanken vor dem Backup den
tatsächlichen RAM-Bedarf und freien Zielspeicher prüfen. Voraussetzung
bleibt ein vertrauenswürdiger lokaler Betrieb; privilegierte oder
kompromittierte Prozesse mit direktem Zugriff auf Dateideskriptoren
sind durch ein Backup-Receipt nicht ausgeschlossen.

~~~bash
mkdir -m 700 -p /sicherer/backup-ordner
mark-api-backup --db /sicherer/pfad/mark.sqlite \
  --backup /sicherer/backup-ordner/mark-2026-10-09.sqlite
~~~

- Das Ziel darf *noch nicht existieren*. Auch Symlinks und defekte
  Symlinks werden nicht überschrieben. Das reale Elternverzeichnis
  muss vorhanden sein. Die Quelle darf kein Symlink/Hardlink-Alias sein.
  Namen der SQLite-Quell-Sidecars (`-wal`, `-shm`, `-journal`) sind
  als Backup-Ziel ebenfalls gesperrt.
- Vor dem Backup wird die bestehende Mark-Quelldatenbank read-only
  einschließlich Recovery-, Idempotenz- und Sync-Journal-Schema validiert.
  SQLite Online Backup kopiert in die private In-Memory-Datenbank;
  dort werden Integrität und alle Receipt-Zähler vor der Serialisierung
  geprüft. Eine zweite dateibasierte SQLite-Stage-Verbindung entfällt.
- Die serialisierten Bytes besitzen einen unveränderlichen SHA-256-
  Referenzwert aus dem geprüften RAM-Snapshot. Jeder spätere Stage-Hash
  muss diesem ursprünglichen Wert entsprechen; ein späterer Hash wird
  niemals als neue Vertrauensbasis übernommen. Geschrieben wird nur
  über den offenen `O_TMPFILE`-Inode ohne vorherigen Dateinamen im
  Zielverzeichnis. Bei Speichermangel, fehlender Serialisierung oder
  nicht unterstütztem `O_TMPFILE` wird fail-closed abgebrochen.
- Der anonyme Stage-Inode wird auf 0400 gesetzt und mit fsync sowie
  Hash- und Metadatenkontrollen geprüft. Nach atomarer create-only-
  Hardlink-Veröffentlichung folgen ein erneuter Hash-/Inode-Readback und
  Verzeichnis-fsync. Es wird niemals in die Quelldatenbank geschrieben,
  kein Plattformrequest gestartet und kein bestehendes Backup überschrieben.
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

## Isolierter Linux-Systemdienst: tatsächliche Vertrauensgrenze

**Vorlagen, keine laufende Installation:** `docs/mark-api.service`,
`docs/mark-api-backup@.service`, `docs/mark-api-bootstrap.sh`,
`docs/mark-api-preflight.py` und `docs/mark-api.sysusers.conf`
beschreiben einen root-verwalteten, dedizierten `mark-api`-Unix-Nutzer ohne
Login-Shell. Diese Dateien im Repository oder ein erfolgreicher Unit-Test
beweisen **keine** tatsächlich eingerichtete, sichere Laufzeitidentität.

Ein Benutzerprozess, der dieselbe Unix-UID und Schreibrechte wie die
SQLite-Dateien besitzt und böswillig über bereits writable `MAP_SHARED`
gemappte Main-/WAL-/SHM-Seiten schreibt, kann die Inhalts-/Zeitpunktprüfungen
der Backup-CLI umgehen und alte, scheinbar gültige Snapshots erzwingen.
Weder `lstat`, Datei-Hashes, ein SQLite-Leselock noch die neue Rohseiten-
Vergleichsfunktion sind dafür eine kontinuierliche Isolation. Der harte
Betriebsrand ist deshalb: **Das Dienstkonto darf ausschließlich die
vertrauenswürdige Mark-Laufzeit und ihre Backup-Aufgabe ausführen.** Alle
anderen Desktop-/Browser-/Automation-Prozesse bleiben unter getrennten
Unprivilegierten-UIDs; ausschließlich root darf die Unit oder ihre
Programmdateien ändern. Direkte UID-Impersonation, eine kompromittierte
Mark-Laufzeit oder root sind damit nicht als abgewehrte Angreifer
modelliert. Gegen diese Bedrohungen braucht es zusätzliche, extern
verifizierbare Integrität und/oder unabhängig isolierte Storage-Snapshots.

Die Vorlage bewahrt die normale `mark-api-launch`-Komposition mit
**default-on** Create-/Media-/Update-/Pause-/Delete-Write-Funktionen und
sämtlichen existierenden Bestätigungs-, Ownership-, Idempotenz- und
Recovery-Fences. Sie deaktiviert keine Produkt-Write-API. Der Browser
läuft als **separater bereits authentifizierter Nutzerprozess** mit
loopback-only CDP; das Dienstkonto greift über den lokalen TCP-Port zu,
nicht durch Lesen des Browserprofils. Zum Hochladen von Bildern dient der
vorhandene pfadfreie Media-Staging-Endpunkt. Das Dashboard und die
Write-API bleiben loopback-only.

### Einrichtung ausschließlich mit geprüftem Produkt-Release

1. Vor Ausführung echte Backup-/Restore-Evidenz der vorhandenen Mark-Datenbank,
   aktuelle offene Pending-Write-Fences und eine zulässige, bereits
   authentifizierte CDP-Sitzung prüfen. Bestehende Daten **niemals**
   mit `--init-db` neu anlegen oder blind durch ein Altbackup ersetzen.
   Die Vorlage setzt ein root-owned, ausschließlich aus einem geprüften
   Release **nicht-editierbar** installiertes `/opt/mark-api/venv` mit
   Python 3.12+ und `mark-api[private-web]` voraus. Die komplette venv
   einschließlich aller transitiven Abhängigkeiten, .pth-Dateien und
   Console-Scripts muss root-owned sein, weder gruppen- noch weltweit
   schreibbar, ohne nach Benutzer-Home oder andere untrusted Pfade zeigende
   Imports/Symlinks. **Kein bestehendes user-owned Venv durch bloßes `chown`
   umwidmen:** Bereits gehaltene writable Mappings könnten sonst weiter
   auf seine Inodes wirken. Stattdessen ein neues, leeres root-owned Venv
   aus geprüftem Release anlegen und dessen Pakete direkt dort installieren.
   Der tatsächlich gestartete `/opt/mark-api/venv/bin/python` und seine
   vorhandenen Python-Interpreter-Aliasse müssen Symlinks auf **dieselbe**
   root-owned `/usr/bin/python3.X`-Datei sein, deren `X` dem einzigen
   installierten Mark-Paketverzeichnis `lib/python3.X/site-packages` entspricht.
   Kopierte Interpreter im Venv oder Links auf andere Minor-Versionen
   werden abgelehnt, weil sie ungeprüfte Standardbibliotheken laden könnten.
   Symlinks auf ein Benutzer-Home sind unzulässig.
   Der Preflight lehnt zusätzlich .pth-Pfaderweiterungen außerhalb der
   vollständig gescannten Venv, ungescannte Paket-Symlinks nach /usr und
   `include-system-site-packages = true` ab. In `pyvenv.cfg` müssen
   `include-system-site-packages = false` und exakt `home = /usr/bin`
   enthalten. Andere Python-Basis-Pfade, auch scheinbar geschützte unter
   `/usr/local`, werden abgewiesen. Der Preflight prüft zusätzlich den
   vollständig erreichbaren OS-Standardbibliotheksbaum unter
   `/usr/lib/pythonX.Y` (einschließlich Symlink-Zielen) und gegebenenfalls
   `/usr/lib/pythonXY.zip` auf root-owned, nicht schreibbare Inhalte. Ein
   OS-übliches Symlink auf eine externe **reguläre Datei** (etwa
   `/etc/pythonX.Y/sitecustomize.py`) ist nur bei geschütztem Ziel und
   geschützter gesamter Elternkette zulässig; externe Verzeichnis-Symlinks
   bleiben gesperrt.
   UTF-8-BOM in `.pth`-Dateien werden entsprechend CPythons `site.py`
   vor der Importzeilenprüfung entfernt. Das normale Mark-Venv setzt somit
   eine sicher installierte passende OS-Python-Version unter `/usr/bin`
   voraus; das nur im Benutzer-Home vorhandene Python 3.12 genügt nicht
   für die isolierte Produktions-Unit. Diese Vorgabe ersetzt keine Prüfung
   der tatsächlichen Host- und Dienst-Identität.
   Die OS-System-Python-Version darf älter sein. Ihr Importbaum wird **vor**
   dem ersten Python-Aufruf durch das root-verwaltete native POSIX-Bootstrap-
   Skript geprüft; nur danach startet es den isolierten Preflight mit `-I -S`.
   Das eingesetzte `/bin/sh`, `/usr/bin/find`, `/usr/bin/readlink`, deren
   nativer Loader und die root-verwaltete systemd-Unit sind ausdrücklich
   Teil der zugrunde gelegten vertrauenswürdigen OS-Basis. Gegen deren
   Kompromittierung schützt kein in derselben Unit ausgeführter Preflight.
2. Vor dem ersten Start einmalig ein dediziertes, nicht interaktives
   Systemkonto und geschützte Verzeichnisse vorbereiten:

   ~~~bash
   sudo install -D -o root -g root -m 0644 docs/mark-api.sysusers.conf /etc/sysusers.d/mark-api.conf
   sudo systemd-sysusers /etc/sysusers.d/mark-api.conf
   sudo install -d -o mark-api -g mark-api -m 0700 /var/lib/mark-api /var/lib/mark-api-backups
   /opt/mark-api/venv/bin/python --version
   ~~~

   Nur bei **wirklich neuer**, nachweislich noch nicht vorhandener Datenbank
   mit dem bereits geprüften installierten Paket initialisieren, ohne
   Browser- oder Plattformzugriff:

   ~~~bash
   sudo -u mark-api /opt/mark-api/venv/bin/python -c "from mark_api.storage import SnapshotStore; SnapshotStore('/var/lib/mark-api/mark.sqlite', create_if_missing=True)"
   ~~~

   Bei bestehenden Mark-Stores stattdessen nur nach nachvollzogenem
   Offline-Stop, unveränderter SHA-256-Quellprüfung und streng geprüftem
   Backup/Restore den neuen Owner-Pfad vorbereiten. Keine automatische
   Migration/Ownership-Änderung laufender Daten.
   Die Systemd-Vorlagen verwenden ausdrücklich **kein** `StateDirectory=`,
   da systemd ein bereits bestehendes Verzeichnis andernfalls automatisch
   rekursiv umberechtigen könnte. Die Datenverzeichnisse müssen daher wie
   oben **vorher** existieren, ohne dass der Dienststart sie neu erzeugt
   oder fremde Daten automatisch übernimmt. `ReadWritePaths=` erlaubt nur
   die benötigten bereits existierenden Pfade trotz `ProtectSystem=strict`.
   `ExecCondition=` überprüft zuvor mit dem root-verwalteten nativen
   `/usr/bin/find`, ob `/etc`, `/etc/mark-api` und das Bootstrap-Skript
   root-owned, nicht gruppen-/weltweit beschreibbar und ohne Symlink auf
   die erste ausführbare Scriptdatei verweisen. Ein Verstoß verhindert den
   Dienststart, bevor ein Byte aus diesem Skript ausgeführt wird.
   `ExecStartPre=` beginnt danach mit `/bin/sh /etc/mark-api/bootstrap.sh`. Diese
   root-verwaltete, schreibfreie native Stufe prüft zunächst den **eigenen**
   `/usr/bin/python3`-Interpreter samt tatsächlich zugehörigem
   `/usr/lib/pythonX.Y`-Importbaum, Symlink-Zielen einschließlich ihrer
   Elternverzeichnisse und optionalem Stdlib-ZIP auf root-Eigentum sowie
   Nicht-Schreibbarkeit. Erst danach wird das getrennte root-owned
   `/etc/mark-api/preflight.py` unter `/usr/bin/python3 -I -S` ausgeführt.
   Dieser zweite schreibfreie Bootstrap prüft den vollständigen Installationsbaum
   einschließlich Python-Code aller Mark-/Drittanbieter-Module, Binärmodule,
   Skripte, Paket-Pfaddeklarationen und Symlink-Ziele auf root-Eigentum und
   Nicht-Schreibbarkeit. Zugleich verifiziert er Dienst-UID/-GID, Nicht-Login-
   Shell, `0700`-Verzeichnisse und `0600`-Datenbank/Sidecars. Unverifizierte
   Installationen, schreibbare Fremdmodule und untrusted `.pth`-Imports
   blockieren den Start. Der eigentliche Produktprozess benutzt anschließend
   den isolierten Venv-Aufruf `python -I -m mark_api.launcher` und aktiviert
   weiterhin sämtliche normalen Writes **default-on**. Ein Fehler startet
   weder Launcher noch Backup und löst keinerlei Plattformaktion aus.
   **Diese Prüfung erkennt keine fremden, bereits unter der kompromittierten
   Dienst-UID laufenden Prozesse oder vorbestehende mmap-Schreibzugriffe.**
3. Root-verwaltete Units installieren, aber erst nach verifiziertem
   CDP-Endpunkt und sicherem DB-Eigentum starten:

   ~~~bash
   sudo install -d -o root -g root -m 0755 /etc/mark-api
   sudo install -o root -g root -m 0644 docs/mark-api-bootstrap.sh /etc/mark-api/bootstrap.sh
   sudo install -o root -g root -m 0644 docs/mark-api-preflight.py /etc/mark-api/preflight.py
   sudo install -o root -g root -m 0644 docs/mark-api.service /etc/systemd/system/mark-api.service
   sudo install -o root -g root -m 0644 'docs/mark-api-backup@.service' '/etc/systemd/system/mark-api-backup@.service'
   sudo systemctl daemon-reload
   sudo systemctl cat mark-api.service
   sudo stat -c '%U:%G %a %n' /etc/mark-api /etc/mark-api/bootstrap.sh /etc/mark-api/preflight.py /var/lib/mark-api /var/lib/mark-api/mark.sqlite /var/lib/mark-api-backups
   ~~~

   Erwartet sind `root:root`, `0755` für `/etc/mark-api`, `0644` für
   beide Preflight-Skripte sowie `mark-api:mark-api`, `0700` auf beiden
   Datenverzeichnissen und `0600` auf der Quelldatei. Vor Livebetrieb prüfen, dass eine andere
   nicht-root UID keinen Dateizugriff erhält und kein Fremdprozess unter der
   Dienst-UID läuft. Der Browser und die lokalen CLI-Tools unter dem
   Desktopkonto dürfen **nicht** mehr direkt in diese Datenbank schreiben.
   Das Starten der Unit ohne authentifiziertes Loopback-CDP liefert
   keine produktive Abnahme.
4. Die Vorlage verwendet `--dashboard-port 8875 --write-port 8876`,
   da die Standardports `8765/8766` auf dem überprüften Host von der
   Audioverwaltung belegt waren. Ports unmittelbar vor Inbetriebnahme
   erneut prüfen. Nach sicherem Start mit `sudo systemctl start mark-api.service`
   erscheinen Dashboard-URL samt Fragment und prozesslokaler Bearer
   **nicht im Journal**, sondern ausschließlich in
   `/run/mark-api/launcher.log` innerhalb des `0700`-RuntimeDirectory.
   Datei und Token nur über einen autorisierten Operator lesen, niemals
   in öffentlich zugängliche Logs oder PR-Kommentare kopieren.
5. Eine explizit benannte, eindeutige Backup-Instanz ausführen, etwa
   `sudo systemctl start mark-api-backup@20261009T1700.service`. Die
   Ausgabe enthält nur den sanitisierten Receipt. Wiederverwendung
   derselben Instanz darf die existierende Datei **nicht** überschreiben;
   bei unklarem Lauf-/Publikationsstatus Zielpfad und Hash zuerst prüfen,
   kein blinder Retry. Der Dienst/Backup-Prozess benutzt dieselbe
   **vertrauenswürdige** UID, während normale gleichberechtigte
   SQLite-Anwendungen weiterhin über SQLite-Koordinierung laufen können.

**Merge-/Produktabnahme:** Ein Unit-Template, statische Tests oder eine grüne
CI ersetzen nicht die unabhängige Live-Prüfung der tatsächlich laufenden
Prozess-UID, systemd-Sandbox, Besitzer-/Gruppen-/Modusrechte des Datenbank-
und WAL-/SHM-Verzeichnisses, anderer Prozesse unter der Dienst-UID, des
Installationspfads und des erfolgreichen Backups einer realen, vollständigen
Write-/Sync-Recovery-Datenbank. Die offenen mmap-basierten Review-Befunde
bleiben solange blockierend, bis eine für den realen Produktpfad wirksame
Vertrauensgrenze nachgewiesen und der konkrete PR-Head unabhängig
nachgeprüft ist.

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

## Alternative A6-Installation: geprüftes Ubuntu-24.04-Containerimage

Dieser alternative Installationsweg ist für Hosts ohne vertrauenswürdige
System-Python-3.12-Installation (aktuell Pop!_OS 22.04) vorgesehen und **kein
automatisches Betriebs-/Merge-Gate**. Die systemd-Vorlagen oben bleiben bestehen.
Die bereits ausgeführten Wegwerfcontainer haben eine root-eigene Ubuntu-24.04-
Installation mit dedizierter UID, vollständigem Preflight sowie Backup und Restore
mit allen drei offenen Recovery-Fences synthetisch nachgewiesen. Ein dauerhafter
Hostdienst, realer CDP-Zugang und Schutz vor vorbestehenden fremden
`MAP_SHARED`-Mappings sind dadurch nicht nachgewiesen.

`docs/mark-api-container.Dockerfile`, `mark-api-container-start.sh`,
`mark-api-container-backup.sh`, `mark-api-container-build.sh`,
`mark-api-container-control.sh` und `mark-api-container.compose.yaml`
definieren eine opt-in-Produktkomposition:

- Das Image wird nur aus einem *sauberen exakt 40-stellig gebundenen Git-HEAD*
  erzeugt. Das Buildskript erstellt per `git archive` in einem privaten
  temporären Kontext ein nicht-editierbares Wheel und überträgt ausschließlich
  dieses Wheel sowie die geprüften Bootstrap-/Startskripte an den lokalen
  Docker-Daemon. Das Buildskript deaktiviert Git-Replacement-Objekte,
  verweigert vorhandene `refs/replace` und Git-Grafts und akzeptiert keine
  umgeleiteten Git-Objektdatenbanken aus der Shell-Umgebung. Andernfalls
  könnte die Archivquelle trotz des attestierten Commit-SHA verändert sein.
  Das Ubuntu-24.04-Basisimage ist über SHA-256 adressiert.
  Python 3.12 stammt aus den Ubuntu-Paketen. Paketdateien werden root-eigen
  installiert; die Containerlaufzeit startet als Nicht-Root-`mark-api`.
- Die *normalen* Create-/Media-/Update-/Pause-/Activate-/Delete-Funktionen
  bleiben ohne Write-Opt-in im Produkt-Launcher aktiv. Der Start führt erst
  den nativen und den Python-Preflight aus und ruft anschließend
  `mark_api.launcher` ohne `--init-db` auf. Bestätigungen, Bearer,
  Ownership-, Idempotenz- und Recovery-Fences bleiben erhalten.
- Das Produktprofil ist ausdrücklich `live`, das Backup `backup`.
  Ohne diese expliziten Compose-Profile entsteht kein Dienst. Beide verwenden
  ein nur lesbares Root-Dateisystem, keine Linux-Capabilities, `no-new-privileges`
  und begrenzte PIDs. Volumes sind `external: true`; Compose erstellt
  sie nicht stillschweigend und weist bestehenden Daten keinen neuen Owner zu.
  Backups haben `network_mode: none` und verwenden einen zulässigen einzelnen
  Instanznamen als create-only-Ziel.
- Der Launcher benötigt `network_mode: host`, um eine separat autorisierte,
  bereits angemeldete Browser-CDP-Sitzung auf **127.0.0.1:9222** zu erreichen.
  Das ist eine bewusste Abschwächung ausschließlich der **Netzwerk-Namespace**-
  Trennung, nicht der Dateirechte oder Write-Fences. Dashboard/Write-API
  binden nur Loopback `8875/8876`. Die sensible Dashboard-URL und der
  prozessgebundene Bearer stehen nur in `/run/mark-api/launcher.log` auf
  einem privaten temporären `0700`-Dateisystem, nicht im Docker-STDOUT.
  Andere Host-Loopback-Dienste bleiben durch Host-Networking erreichbar;
  diese Trust-Boundary vor der Produktivnutzung ausdrücklich prüfen.

**Kritische Host-UID-Grenze:** Der vorhandene Docker-Daemon ist rootful
und hat kein User-Namespace-Remapping. Container-UIDs entsprechen damit
numerisch Host-UIDs. Auf dem aktuell geprüften Host ist **UID 999 schon
für Caddy sowie Redis-/MongoDB-/PostgreSQL-Prozesse in Benutzung**.
Die synthetische Container-UID 999 darf daher nicht übernommen werden!
Die alternative Image-/Compose-Konfiguration wählt stattdessen bewusst
`50042:50042` außerhalb von systemds üblichem DynamicUser-Bereich.
Das ist **keine automatische UID-Exklusivität**: Vor einer echten
Produktinstallation muss root diese UID/GID systemweit eindeutig dem
nicht-interaktiven `mark-api`-Dienst zuweisen/reservieren und überprüfen,
dass keine fremden Host- oder Containerprozesse dieselbe numerische UID
besitzen, vorher besessen haben oder schreibbare Mappings der betroffenen
DB-, WAL- oder SHM-Inodes halten. Die Registrierung, die aktive Prozess-
/Mapping-Inventur und die Volume-Provisionierung sind separate,
ausdrücklich nachzuweisende Root-Operationen; nicht durch einen
passenden `getent`-Eintrag allein erledigt. Insbesondere weder Docker
`userns-remap` global auf dem gemeinsam genutzten Host aktivieren noch
bestehende Docker-Volumes blind übernehmen/umberechtigen.

**Image-Bau (kein Host-Deployment):**

~~~bash
# Im sauberen, unabhängig geprüften PR-Checkout mit exakt aktuellem HEAD:
HEAD_SHA=$(git rev-parse HEAD)
MARK_UV=/home/alex/.local/bin/uv sh docs/mark-api-container-build.sh "$HEAD_SHA"
# Das Tag dient nur zur Identifikation der gerade gebauten lokalen Datei.
# Für jede tatsächliche Container-Aktion gilt anschließend ausschließlich die
# vollständige unveränderliche Image-ID; das Release-Label wird erneut geprüft.
IMAGE_ID=$(docker --host unix:///var/run/docker.sock image inspect \
  --format '{{.Id}}' "mark-api:pr75-${HEAD_SHA%????????????????????????????}")
sh docs/mark-api-container-control.sh verify "$IMAGE_ID" "$HEAD_SHA"
~~~

Das 40-stellige Commit-SHA ist vor dem Bau aus GitHub unabhängig zu prüfen.
Für die Freigabe ist die **vollständige Image-ID**, nicht ein veränderliches
Tag, zu verwenden. Paketversionen, signierte Ubuntu-Paketherkunft und
das vollständige Image-/Wheel-/Source-Binding sind am tatsächlich gebauten
Artefakt erneut zu kontrollieren. Falls der getrennte Docker-Daemon oder
die Buildabhängigkeiten nicht vertrauenswürdig/verfügbar sind, nicht
auf einen ungescannten Benutzer-Python-Importpfad ausweichen.

**Vor einem kontrollierten Start müssen zusätzlich** alle folgenden
Bedingungen nachgewiesen sein: Dedizierte und reservierte UID/GID
`50042:50042`; keine fremden Prozesse oder offenen beschreibbaren
Mappings unter dieser Identität; geprüfte neue oder offline sicher
übertragene Datenbank mit vollständigen Recovery-Tabellen und
`0600`-Datei/Sidecars in einem privaten `0700`-Volume; ausschließlich
vertrauenswürdig neu provisioniertes `mark-api-data-v1` und
`mark-api-backups-v1` mit passenden Rechten, ohne andere Container-Mounts;
gültige bereits angemeldete und zulässige CDP-Sitzung; freie Ports
`8875/8876`; kein paralleler Writer und frische Release-/Review-Evidenz.
**Keine automatische Datenbankinitialisierung oder chown bestehender
Datenbanken!** Die tatsächliche Datenübernahme braucht ihren eigenen
Stop-/Hash-/Snapshot-/Mapping-/Recovery-Nachweis.

Erst nach diesen Gates darf der Operator die **einzige unterstützte**
Image-/Startkontrolle `docs/mark-api-container-control.sh` verwenden:

~~~bash
# HEAD_SHA und IMAGE_ID müssen aus dem obigen geprüften Build stammen.
# Prüft exakt 64 hexadezimale Image-ID-Bytes, das tatsächliche lokale
# Docker-Image, dessen Git-Revisionslabel und den sauberen Quell-HEAD.
sh docs/mark-api-container-control.sh verify "$IMAGE_ID" "$HEAD_SHA"

# Nur nach zusätzlich vollständig belegter OS-/Storage-/CDP-Freigabe:
sh docs/mark-api-container-control.sh live "$IMAGE_ID" "$HEAD_SHA"

# Getrennt und nur mit bekannt unbenutzter, eindeutiger Backup-Instanz:
sh docs/mark-api-container-control.sh backup "$IMAGE_ID" "$HEAD_SHA" EINDEUTIGE_ID
~~~

Der Guard übergibt der Compose-Konfiguration ausschließlich die vollständig
geprüfte unveränderliche `sha256:...`-Image-ID und kontrolliert das
`org.opencontainers.image.revision`-Label **unmittelbar vor dem Aufruf**.
Mutable Tags wie `latest` oder `mark-api:pr75-...` sowie unvollständige
Image-IDs werden verweigert. Der Start verlangt außerdem die explizite
root-verwaltete Registrierung von Host-UID/-GID `50042:50042` als
`mark-api` und zwei bereits existierende externe Volumes.
**Direktes `docker compose up/run` mit manuell gesetztem `MARK_API_IMAGE`
umgeht diesen Guard und ist kein freigegebener Betriebsweg.** Auch der Guard
beweist keine Mapping-Isolation, Volume-Eigentümerschaft oder Kontoberechtigung;
diese separaten Gates bleiben Pflicht. Ohne sie wird kein `live` oder
`backup` ausgeführt. Ein Image-Bau, ein erfolgreicher Guard-`verify` oder
eine grüne `docker compose config` sind keine erfolgreiche Host-Installation.
Keine Wiederholung einer unklaren Backup- oder Plattformmutation ohne Readback.

**Angreifermodell und Merge-Gate:** Eine verlässlich exklusive UID mit
frischen privaten DB-/WAL-/SHM-Inodes verhindert den gewöhnlichen
unprivilegierten *fremden* Same-UID-Prozesszugriff, sobald die
tatsächliche Host- und Docker-Konfiguration das belegt. Das schützt
weder gegen root-/Docker-Gruppen-Kompromittierung noch gegen böswilligen
Code, der bereits als der vertraute Mark-Dienst ausgeführt wird. Falls
auch dieser Angreifer abgewehrt werden muss, ist eine unabhängig
durchgesetzte Storage-Snapshot-/Integrity-Grenze erforderlich.
Die drei bestehenden `MAP_SHARED`-P2 sind bis zu einer neuen unabhängigen
Live-Attestation und entsprechendem head-bound High-Critical-/Captain-Gate
**weiterhin blockierend**. Keine reale Kleinanzeigenmutation zu Testzwecken
ohne nachgewiesene Kontoberechtigung und zulässigen Integrationsweg.