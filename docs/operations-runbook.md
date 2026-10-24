# Mark – aktuelles Betriebs- und Recovery-Runbook

Stand: 10.10.2026. Dieses Runbook beschreibt den installierten technischen
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

## Unabhängiger SQLite-Owner – Compose-Draft, nicht produktiv gestartet

`src/mark_api/storage_owner.py` implementiert einen ersten **separat
UID-geschützten und streng typisierten** AF_UNIX-Datenbankzugang. Vorgesehene
Storage-Besitzer-UID ist **50046**, Client-UID **50044**; beide dürfen weder
root noch dieselbe numerische UID sein. Die bereits vorhandene Datenbank
muss als Schema-v2 unter einem privaten 50046-Verzeichnis (0700) mit
Dateimodus 0600 und ohne Symlink-/Hardlink-Alias existieren. Vorhandene
WAL-/SHM-/Journal-Sidecars unterliegen derselben Inode-/UID-/Mode-Prüfung.
Der UDS-Ordner benötigt 50046:50044/2710, der Socket 50046:50044/0660.
Der Client attestiert per Kernel-`SO_PEERCRED` die echte Owner-UID. Eine
verwaiste Socket-Inode verhindert den Neustart; keine automatische Löschung,
Schema-Neuanlage oder Historienmigration.

Der veröffentlichte Broker bietet feste typisierte RPCs für Readiness,
Sync-Versuche und atomare Inventarübernahmen, paginierte Anzeigen-/Reaktions-/
Inbox-Historien, E-Mail-Batchpersistenz, Klassifikationen, gebundene
Write-Request-Claims/-Starts/-Readbacks sowie reine Dashboard-Pending-
Claim-/Leseseiten. Provenienz, originale Beobachtungszeiten, Idempotency-Key
und Fingerprint bleiben erhalten. Er enthält **keine** allgemeine
SQL-/Datei-/RPC-Methodendispatch-Schnittstelle, keinen beliebigen
`complete`-, Pending-`ack`-, Recovery-`clearance`-, Operator-Clear-,
Restore- oder Backup-Aufruf. Antworten/Transportfehler dürfen eine
unklare Persistenz niemals als sichere Ablehnung interpretieren. Ein
unbekannter ACK wird nur beobachtend aufgelöst und nie blind wiederholt.

Zusätzlich zu den E-Mail-Import- und Klassifikations-Socket-Modi kann das
read-only Dashboard über `mark-api-dashboard --owner-socket
/rpc/owner.sock --owner-uid 50046` ohne
SQLite-Pfad unter Client-UID 50044 lesen (nur attestierte Test-/Offline-
Umgebungen; die Docker-Draft-Startgate bleibt wirksam). Query, Analytics,
Dashboard-Readiness und die HTTP-Antwort 503 bei ausgefallenem Owner sind
regressionstestsicher. Der Socket-Dashboard-Modus verweigert einen
`DashboardWriteProxy` ausdrücklich **vor** der Serverbindung. Keine
UI-Write-Bestätigung oder Recovery-Clearance wird hierdurch ermöglicht.

**Shutdown-Fence:** Angenommene RPC-Threads sind seit diesem Slice nicht
mehr `daemon_threads`; ein kontrolliertes `stop()`/Socket-Close wartet
bis zur wirklichen Beendigung aller bereits angenommenen Operationen.
Ein vor der SQLite-Persistenz künstlich angehaltener `begin`-Request
reproduzierte zuvor einen vorzeitig abgeschlossenen `stop()` mit
weiterlebender Schreiboperation. Der neue Regressionstest erzwingt, dass
vor Socket-Unlink und erneuter Autoritätsvergabe zuerst diese Operation
abgeschlossen sein muss. Dies beweist keine atomare Wiederherstellung
nach `SIGKILL`, bei ererbten Alt-FDs oder gegen defekte Hardware.
In solchen Fällen bleibt der Store gesperrt bis zur Offline-Reconciliation.

**Inventar-Sync-Recovery über Neustarts:** Ein `sync_begin` unter dem
isolierten Storage-Owner prüft nun in derselben `BEGIN IMMEDIATE`-
SQLite-Transaktion vor einer neuen Journalanlage, ob **irgendein** älterer
`sync_attempts`-Eintrag noch `in_progress` ist – unabhängig von dessen
Quelle oder einem neueren abgeschlossenen Versuch. Nach Prozessverlust,
verlorenem Begin-/Stage-ACK oder verlorener In-Memory-Stage kann ein neuer
Inventar-Read deshalb nicht stillschweigend die ungeklärte Historie
übergehen. Ein noch aktiver Owner darf denselben Versuch regulär
abschließen; aus dem bloßen Zustand `in_progress` folgt **kein**
dauerhafter Absturz. Bleibt ein Versuch nach Generationenwechsel oder
verlorenem ACK unklar, sperrt er weitere Starts bis zur kontrollierten
Reconciliation. Der Executor besitzt weiterhin keine RPC zum Löschen,
Abbrechen oder Umklassifizieren alter Versuche. Der gewöhnliche
`sync_status` zeigt nur den letzten gestarteten und letzten erfolgreichen
Versuch und ist **kein** Beweis, dass keine älteren offenen Versuche
existieren. Der neue **ausschließlich lesende** Owner-RPC
`sync_recovery_status` und die Dashboard-Route `GET /api/sync/recovery`
zeigen dagegen die Anzahl sämtlicher noch `in_progress`-Einträge und
die gebundene Identität des ältesten offenen Versuchs, selbst wenn ein
späterer Versuch erfolgreich war. Das Dashboard übernimmt diesen Status
aus `/api/summary` und warnt sichtbar, ohne die alten Beobachtungen als
gültige Freigabe oder Plattformnachweis zu deklarieren.

~~~bash
curl --fail http://127.0.0.1:8765/api/sync/recovery
~~~

Dies ist eine **zeitpunktbezogene Leseansicht**, keine Quittierung und
kein Ersatz für kontrollierte Offline-Reconciliation. Für die vollständige
Untersuchung ist die gesamte Owner-Journaltabelle nur in einer separat
abgesicherten Offline-Umgebung auszuwerten. Weder die Existenz eines neuen
Browsers noch ein verlorener ACK beweisen eine vorherige erfolgreiche
oder fehlgeschlagene Synchronisierung.

**Strikter Status: weiterhin Draft, keine Produktfreigabe.** Die geplante
sechste Compose-Instanz UID 50046 besitzt exklusiv das SQLite-Main-/WAL-/
SHM-Volume. UID 50044 hat im Compose **keinen** Rohdatenbank- oder
Backup-Mount mehr: nur ein gesondertes Media-Volume, den typisierten
UID-50046-Storage-Socket und die bestehenden privaten Executor-/Witness-
Sockets. Auch der gestoppte Backup-Container ist auf UID 50046 getrennt.
Die normale Launcher-/Write-API-Laufzeit funktioniert damit aber **noch
nicht**: ihr direkter SnapshotStore, Write-Completion, Operation-/Create-/
Media-Receipts, Dashboard-Pending-Acks und unabhängige Clearances sind
noch nicht vollständig migriert. Read-only Dashboard, Analytics und Sync
verwenden bereits die geschlossenen RPCs. Die sechs direkten Entrypoints
sind deshalb weiterhin **vor** der ersten sensiblen Operation hart gesperrt.
Eine Compose-Datei allein ist weder historischer Cutover noch Runtime-Beleg.
Alt geöffnete writable FDs und `MAP_SHARED`-Mappings werden durch
`chown`, einen neu laufenden Prozess oder diesen isolierten Test **nicht
entzogen**. Erst danach darf eine echte kompromittierte UID-50044-
Negativprobe alle drei #76/#77-Exploits einschließlich gemeinsamem
SQLite/Witness-Backup und Restore widerlegen. Bis dahin keine Mutation
an installierten DBs/Volumes, keine Ersetzung des bisherigen Mounts,
kein `live`-/`backup`-Start und kein Merge.

Der optionale Rootful-Docker-Test
`tests/test_storage_owner_multiuid.py` arbeitet ausschließlich mit
eigens erzeugten synthetischen Volumes unter UID 50046/50044. Er überprüft
genau diese Grenzen, darunter lost-ACK-Readback, fremden DB-FD-Zugriff,
verweigerte Raw-SQL/Clearance-Anfragen, persistente offene Fences nach
SIGKILL und blockierten Neustart mit altem Socket. Der Test führt zudem unter UID 50044 **ohne SQLite-Datenmount** echte
HTTP-Dashboard-/Analytics-Lesezugriffe über den UID-50046-Socket aus;
ein Write-Proxy ist dort verboten. Eine zweite synthetische
Gegenprobe startet die UID-50044-/50046-Container ohne Quellcode-Bindmount,
nutzt direkt das installierte Release-Wheel und führt den nichtleeren
`sync_owner_inventory`-Kompositionspfad aus. Sie prüft den realen
`SO_PEERCRED`-Peer, journalisierte Nichtleer-Beobachtung, Snapshot-Historie,
offene Idempotency-Write-Fences und das Fortbestehen der Draft-CLI-Sperre.
Dabei wird weder ein echter CDP-Browser gelesen noch auf Kleinanzeigen
geschrieben. Auch diese Tests ersetzen keine Host-Cutover-/Live-Produktabnahme
und keine Prüfung historischer offener Mappings.

## Legacy-Systemdienst ist sicherheitshalber außer Betrieb

**Die früheren systemd-Installations- und Betriebsbefehle sind nicht mehr zulässig.**
Die Vorlagen `mark-api.service` und `mark-api-backup@.service` verweigern
mit `ExecCondition=/usr/bin/false` bereits vor dem Bootstrap den Start;
`ExecStart=/usr/bin/false` bleibt als zweite Sperre erhalten. Vorhandene
alte, eventuell installierte Units sind separat auf dem Zielhost zu inventarisieren.
UID 50042 darf keine Datenbank und WAL-/SHM-Dateien besitzen. Der installierte
DB-Preflight akzeptiert nur den unabhängigen SQLite-Owner `mark-api-storage` (UID 50046); der Browser-/Write-Executor `mark-api-owner` (UID 50044) wird abgewiesen.

Ein eigener Unix-Besitzer, `chown`, Snapshot-Hash oder Docker-`pause`
entziehen keine bereits offenen fremden FDs oder writable
`MAP_SHARED`-Mappings. Die ursprüngliche SQLite-Backup-CLI bleibt für
den unabhängigen Witness-/Owner-Betrieb ohne gemeinsamen
Recovery-/Prozess-/Hostnachweis nicht produktionssicher.

## Wiederanlauf nach einem unklaren Write-/Media-Submit

Nach SIGTERM, SIGKILL, Prozessabsturz oder Browser-Timeout bleibt ein
bereits gestarteter, unklarer Write durch den SQLite-Request **in_progress**
gesperrt. Bei abgeschlossenem HTTP-202-Receipt eines unklaren Media-Submits
bleibt nun ebenfalls eine **datenbankweite Create-Recovery-Sperre** bestehen:
Ein neuer Idempotency-Key darf keine erneute Create-/Media-Create-Eingabe
auslösen. Bereits gespeicherte Antworten auf den alten Key werden unverändert
replayed. Andere reguläre Write-Capabilities bleiben technisch default-on,
soweit ihre eigenen Sicherheitsbedingungen erfüllt sind.

Der an den ursprünglichen Browser-Submit gebundene offene Media-Page-Zustand
kann während der **gleichen laufenden Sitzung** ausschließlich über
`reconcile_media_submit()` observation-only geprüft werden. Nach
positiv abgeschlossenem Page-Readback schreibt Mark für exakt den gebundenen
Write-API-Idempotency-Key eine zusätzliche SQLite-Clearance mit Fingerprint
und Verifikationsart. Die ursprüngliche Operation, ihr Receipt und ihre
HTTP-Replay-Antwort werden **niemals gelöscht oder verändert**. Scheitert
die Beobachtung, bleibt die Sperre bestehen. Scheitert lediglich die
SQLite-Quittierung nach bestätigter Browser-Beobachtung, bleibt die
operationgebundene Media-Seite im laufenden Prozess erhalten. Ein zweiter
Reconciliation-Aufruf wiederholt nur den fehlenden Persistenzschritt, nicht
den externen Submit oder die bereits abgeschlossene Browser-Beobachtung.
Bei einem Prozessabbruch vor dieser Quittierung bleibt die SQLite-Sperre
bestehen; dann ist die unabhängige Operatorprüfung erforderlich.

Für einen bereits beendeten Prozess ist die ursprüngliche Browserseite
nicht automatisch rekonstruierbar. Ein unabhängiger, autorisierter Operator
muss dann den tatsächlichen fremden Plattformzustand und die konkrete
betroffene Operation anhand frischer Account-/Owner-/Target- und bei Bedarf
Medien-Beobachtungen verifizieren. Ein fehlender lokaler Pending-Eintrag,
ein historisches Backup oder die Aussage des ursprünglichen Write-Handlers
genügen nicht. Nur wenn das Ergebnis eindeutig festgestellt ist, darf der
Operator eine **manuelle**, exakt an den Request-Fingerprint gebundene
Clearance attestieren. Sind Ausgang oder Accountrechte weiterhin unklar,
**nicht quittieren und nicht erneut senden**.

Im getrennten UID-/Witness-Betrieb darf eine manuelle Clearance **nicht**
durch einen SQLite-only Python-Aufruf erfolgen. Das würde die unabhängige
Ledger-Autorität umgehen. Es fehlt noch ein gemeinsames
operator-attestiertes Reconciliation-Protokoll, das nach frisch belegtem
externem Plattformzustand an denselben Idempotency-Key, Request-Hash und
Witness-Fence gebunden ist. Bis dahin: unbekannte Ergebnisse gesperrt
lassen; weder Clearance noch externen Submit blind wiederholen.

## Restore – derzeit nur Offline-Diagnose, keine Produktfreigabe

Ein einzelnes create-only SQLite-Backup ist kein vollständiger
Recovery-Snapshot der neuen Architektur. Produktiver Restore bleibt
gesperrt, bis atomar zusammengehörige Datenbank-/Witness-Sicherungen
und Restore/Schema-/Fence-Readback unabhängig validiert sind.

Eine isolierte Kopie zur forensischen Offline-Diagnostik darf nur in
einen bisher nicht existierenden Pfad erfolgen. Sie darf nicht an
Stelle des laufenden DB-/Witness-Paares treten. Integritäts- und
SHA-Prüfungen ersetzen keine aktuelle externe Plattform-Reconciliation;
historisch offene Fences bleiben offen. Niemals `--init-db`,
automatische Ledger-Neuanlage oder Blind-Retry.

## Offene Produkt-Abnahme

| Bereich | Tatsächlicher Nachweis |
| --- | --- |
| Paketinstallation und sieben CLI-Einstiege | installierter lokaler Smoke |
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
automatisches Betriebs-/Merge-Gate**. Die Legacy-systemd-Vorlagen sind absichtlich deaktiviert.
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
  Das weiterhin gesperrte Draft-Profil `live` enthält UID 50042
  (untrusted Frontend ohne IP-Netzwerk), UID 50045 (separater vertrauenswürdiger
  öffentlicher HTTP-Eingang), UID 50044 (Browser-/Write-Executor ohne rohe
  SQLite-Mounts), UID 50043 (unabhängiger Witness) und UID 50046
  (unabhängiger SQLite-Owner). `backup` ist ebenfalls UID 50046,
  aber bis zur gepaarten Witness-Wiederherstellung hart gestoppt.
  Alle sechs Dienste sind explizit profiliert, besitzen read-only Rootfs,
  `cap_drop: ALL`, `no-new-privileges`, begrenzte PIDs und
  `restart: no`. Neun Volumes sind ausschließlich extern mit
  `nocopy: true`; es wird nichts automatisch provisioniert oder initialisiert.
  Die fünf bisherigen Image-/Entrypoint-Grenzen und der neue Storage-
  Entrypoint sind weiterhin **Draft-only**.
- **Unabhängige Host-CDP-Grenze für UID 50042:** Das Frontend läuft
  mit Docker-`network_mode: none` und hat weder IPv4-/IPv6-Hostroute
  noch ein veröffentlichtes TCP-Portmapping. Auch kompromittierter
  Frontend-Python-Code kann daher kein TCP zu CDP-/Owner-Ports, Gateway
  oder LAN herstellen. Er kann nur die fest gebundenen Unix-Sockets nutzen.
  Ein unabhängiger *trusted* Ingress unter UID 50045 bindet
  **127.0.0.1:8875** im Host-Namespace und verbindet sich mit dem
  UID-50042-Socket `/run/mark-api-ingress/frontend.sock` über
  `SO_PEERCRED`, mit Inode-/Modus-/UID-/GID-Prüfung. Er besitzt keine
  Executor-, Daten-, Witness- oder Backup-Mounts.
  Das netzlose Frontend verbindet sich ausschließlich per UID-geprüftem
  Unix-Socket mit dem UID-50044-Dashboard-Relay, nicht mit TCP.
  Das Backend bindet weiterhin nur **127.0.0.1:18875/18876**.
  UID 50044 benötigt Chrome/CDP auf 127.0.0.1:9222.
  **Kernel-TCP-Grenze für UID 50045:** Obwohl der Ingress seinen TCP-Port
  im Host-Netzwerk bindet, führt sein unveränderbarer root-eigener
  Container-Entrypoint ab diesem Draft einen minimalen Python-3.12-Start
  mit `-I -S` aus. Bevor irgendein Mark-Modul importiert, ein TCP-Socket
  geöffnet oder der Relay per `execv` gestartet wird, legt
  `/etc/mark-api/ingress-landlock.py` eine Linux-Landlock-Regel ab ABI 4
  an: `handled_access_net = BIND_TCP | CONNECT_TCP`,
  **nur `BIND_TCP:8875` erlaubt, `CONNECT_TCP` für alle Ports verweigert**.
  Diese irreversible Prozess-/Nachfahrenregel gilt für IPv4 und IPv6,
  unabhängig von Chrome-Rebinding oder Startup-Proben. `AF_UNIX` für den
  UID-geprüften Frontend-Socket bleibt funktionsfähig.
  Fehlende ABI-/Seccomp-Unterstützung, falsche UID oder Fehler beim
  Anwenden führen zum sicheren Startabbruch; der direkte Docker-Draft-
  Entrypoint bleibt darüber hinaus vorab gesperrt.
  Eine echte Rootful-Docker-Hostnetz-Gegenprobe
  (`tests/test_ingress_landlock.py`) zeigte zunächst funktionierendes
  synthetisches TCP, nach Landlock und `execv` verweigertes IPv4-/IPv6-
  TCP-CONNECT, erlaubtes ausschließliches Port-Binden und funktionierendes
  `AF_UNIX`. **Keine echte Chrome-Verbindung** wurde dabei aufgebaut.
  Landlock-Netzwerkregeln prüfen **nur die TCP-Portnummer**, nicht die
  Bindadresse: Auch ein kompromittierter Ingress könnte nach Schließen des
  regulären Listeners versuchen, Port 8875 an eine Nicht-Loopback-Adresse
  zu binden. Das eigentliche Produkt verwendet `127.0.0.1`; eine
  unabhängige Host-Firewall-/Port- und Prozess-Attestation ist trotzdem
  weiter notwendig. Landlock blockiert **nicht UDP** und schützt nur den
  eingeschränkten Ingress-Prozessbaum. Andere vorher bereits laufende
  Prozesse derselben Host-UID oder ein privilegierter Docker-/Root-Operator
  sind durch diese Prozessregel nicht geschützt.
  Darum bleiben UID-50045-Exklusivität, sicherer OS-Cutover, sämtliche
  Prozess-/Socket-FDs sowie tatsächliche finale Produktimage-Negativtests
  gesonderte Gates. Ein fremder UID-Prozess kann den
  Frontend-Ingress-Socket nicht öffnen. Alle sechs Entrypoints sind Draft-
  gesperrt. Der ältere Bridge-basierte TCP-Frontend-Pfad gehört nicht
  zum installierbaren Compose-Entwurf und ist kein Release-Nachweis.
- **Teilnachweis:** Der real ausgeführte ausschließlich synthetische
  Rootful-Docker-Mehr-UID-Test `tests/test_dashboard_relay_netless_multiuid.py`
  beweist die tatsächliche UDS/TCP-Weiterleitung über UID 50045 → 50042 →
  50044 **auch mit bereits im Ingress installierter Landlock-TCP-Policy**:
  Noch im eingeschränkten UID-50045-Prozess wird der direkte Versuch zum
  synthetischen Host-Owner-TCP-Port mit `PermissionError` abgewiesen,
  während `GET /readyz` und die erwartete 404-Antwort über den gesamten
  UID-geprüften Unix-Socket-HTTP-Pfad funktionieren. Zusätzlich sind Host-
  TCP-Ziele aus `network:none` unerreichbar, fremde Socket-UIDs werden
  verweigert und Neustarts nach verwaistem Socket bleiben blockiert.
  Dieser E2E-Test erzeugt keine Chrome-Verbindung und nutzt weiterhin ein
  vorhandenes synthetisches Docker-Testimage, kein attestiertes Releaseimage.
  Das ist weder ein finales Produktimage noch eine Attestation historischer
  Host-UID-/FD-/Mapping-Rechte oder der Autorität eines kompromittierten
  UID-50044-Executors. Auch UID 50045 muss im echten Hostprozess- und
  Portbestand exklusiv verifiziert werden. Keine Live-Freigabe.
  Default-on-Writes bleiben unverändert an Write-API, Ownership,
  Confirmation, Idempotenz, Witness und No-Blind-Retry gebunden.
  Frontend-Token/User-Intention, Storage-Mappings, gemeinsames
  SQLite/Witness-Backup/Restore und Plattformrechte sind weiterhin offen.
  Geheimnisse bleiben im UID-50044-Tmpfs `/run/mark-api/launcher.log`,
  nicht im öffentlichen Frontend oder dessen Docker-STDOUT.

**Kritische Host-UID-Grenze:** Der vorhandene Docker-Daemon ist rootful
und hat kein User-Namespace-Remapping. Container-UIDs entsprechen damit
numerisch Host-UIDs. Auf dem aktuell geprüften Host ist **UID 999 schon
für Caddy sowie Redis-/MongoDB-/PostgreSQL-Prozesse in Benutzung**.
Die synthetische Container-UID 999 darf daher nicht übernommen werden!
Die neue Konfiguration verwendet numerische UID/GID **50042** für das
unprivilegierte Frontend, **50043** für den Witness, **50044** für den
Browser-/Write-Executor, **50045** für den ausschließlich
Loopback-UDS-basierten Ingress und **50046** für den SQLite-Owner. Sie liegen außerhalb systemds üblichem
DynamicUser-Bereich. Das ist **keine automatische UID-Exklusivität**:
Vor einer echten Installation muss root alle fünf Identitäten systemweit
eindeutig den nicht-interaktiven Diensten `mark-api`,
`mark-api-witness`, `mark-api-owner`, `mark-api-ingress` und
`mark-api-storage` zuweisen und nachweisen,
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
# Im sauberen, unabhängig geprüften PR-Checkout mit exakt aktuellem HEAD.
# Aufrufende Shell und Loader müssen bereits vertrauenswürdig sein:
# LD_PRELOAD/LD_AUDIT wirken vor Eintritt ins Skript und auch vor env -i.
# Nur aus einer unabhängig kontrollierten Operator-Startumgebung ausführen.
HEAD_SHA=$(/usr/bin/git rev-parse HEAD)
# Setuptools 80.9.0: das offizielle Wheel vorab aus unabhängig
# geprüfter Herkunft separat beziehen. Nie aus User-Site importieren.
# Der Build kopiert es und prüft dessen Bytes nach dem Kopieren.
SETUPTOOLS_WHEEL=/pfad/zum/verifizierten/setuptools-80.9.0-py3-none-any.whl
printf '%s  %s\n' \
  062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922 \
  "$SETUPTOOLS_WHEEL" | /usr/bin/sha256sum --check
# Lokale, aktuell freigegebene uv-Toolchain (Versionswechsel neu attestieren).
printf '%s  %s\n' \
  534bcd0cf0a0d00a0977363abfc93cd1300914d03e16a77d8d5133a3fd99dacb \
  /home/alex/.local/bin/uv | /usr/bin/sha256sum --check
# MARK_UV darf keinen fremden Pfad wählen; das Skript kopiert und prüft
# die Buildtool-Bytes nochmals nach dem Kopieren in ein privates Verzeichnis.
/usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent \
  MARK_UV=/home/alex/.local/bin/uv \
  /bin/sh docs/mark-api-container-build.sh "$HEAD_SHA" "$SETUPTOOLS_WHEEL"
# Das Tag dient nur zur Identifikation der gerade gebauten lokalen Datei.
# Für jede tatsächliche Container-Aktion gilt anschließend ausschließlich die
# vollständige unveränderliche Image-ID; das Release-Label wird erneut geprüft.
IMAGE_ID=$(/usr/bin/docker --host unix:///var/run/docker.sock image inspect \
  --format '{{.Id}}' "mark-api:pr75-${HEAD_SHA%????????????????????????????}")
/bin/sh docs/mark-api-container-control.sh verify "$IMAGE_ID" "$HEAD_SHA"
~~~

Das 40-stellige Commit-SHA ist vor dem Bau aus GitHub unabhängig zu prüfen.
Für die Freigabe ist die **vollständige Image-ID**, nicht ein veränderliches
Tag, zu verwenden. Paketversionen, signierte Ubuntu-Paketherkunft und
das vollständige Image-/Wheel-/Source-Binding sind am tatsächlich gebauten
Artefakt erneut zu kontrollieren. Falls der getrennte Docker-Daemon oder
die Buildabhängigkeiten nicht vertrauenswürdig/verfügbar sind, nicht
auf einen ungescannten Benutzer-Python-Importpfad ausweichen.

**Vor einem kontrollierten Start müssen zusätzlich** alle folgenden
Bedingungen nachgewiesen sein: dedizierte, reservierte Host-UID/GID
**50042/50043/50044/50045/50046**, keine fremden Prozesse, alten DB-/WAL-/SHM-FDs
oder writable MAP_SHARED-Inodes; kein Docker-UserNS-Remap oder
Rootless-Daemon; eine offline attestierte vollständige SQLite-v2-DB samt
`0600`-Sidecars unter **50046**; neun externe, vorab attestierte Volumes
`mark-api-data-v1`, `mark-api-backups-v1`,
`mark-api-media-v1`, `mark-api-storage-socket-v1`,
`mark-api-fence-ledger-v1`, `mark-api-fence-runtime-socket-v1`,
`mark-api-fence-operator-socket-v1`, `mark-api-executor-socket-v1`,
`mark-api-ingress-socket-v1`.
Für jedes dieser Volumes prüft die vorbereitete Betreiberkontrolle zusätzlich
Docker-Driver `local` und **leere Treiberoptionen**. Zwei unterschiedliche
Volume-Namen können sonst über `driver_opts` denselben Datenpfad binden und
dem Executor wieder rohe SQLite-Zugriffe eröffnen. Auch diese Prüfung
ersetzt weder die Root-attestierte Inode-/Mountpoint-Exklusivität noch die
Revokation bereits offener historischer FDs/Mappings.
Ledger-Verzeichnis: **50043:50043 / 0700**;
Witness-Runtime-Socket-Verzeichnis: **50043:50044 / 2710**;
Operator-Socket-Verzeichnis: **50043:50043 / 0700**;
Executor-Dashboard-Socket-Verzeichnis: **50044:50042 / 2710**;
Frontend-Ingress-Socket-Verzeichnis: **50042:50045 / 2710**;
Storage-RPC-Socket-Verzeichnis: **50046:50044 / 2710**;
Media-Verzeichnis: **50044:50044 / 0700**;
SQLite- und Backup-Verzeichnisse: **50046:50046 / 0700**.
UID 50042 darf weder Daten/Sidecars noch Ledger- oder Operator-Socket
öffnen. Nur ein kontrollierter **einmaliger Offline-Cutover** nach
historischer Fence-Inventur darf das Ledger neu erzeugen; `live` nie.
Erforderlich sind außerdem ein überprüftes ausschließliches Host-Loopback
CDP-Binding, Abwehr möglicher Host-Gateway-CDP-Wege, freie Ports
`8875/18875/18876/9222`, keine parallelen Writer, ein aus DB und Witness
zusammengehöriger Restore sowie frische SHA-gebundene Sicherheitsreviews.
**Keine automatische Datenbankinitialisierung, Ledger-Neuanlage oder
chown bestehender Daten!** Ein kontrollierter Restore benötigt einen
zusammengehörigen, überprüften Ledger-/SQLite-Cutover; die jetzige
Backup-CLI sichert nur SQLite und ist **keine** vollständige
Witness-Backup-/Restore-Lösung.

**Stand PR #78 (Draft):** Die geprüfte Kontrollschnittstelle sperrt
`live` mit `unmediated host CDP bypass blocks product live deployment`
und `backup` mit
`independent witness ledger backup is not implemented`.
**Zusätzlich** verweigern alle sechs Entrypoints (Executor, Frontend,
Ingress, Witness, SQLite-Owner und Backup) den direkten Compose-Start vor jeder sensiblen
Operation, auch mit `--no-deps`. Das Image-Default ist
`ENTRYPOINT ["/usr/bin/false"]`. Socket-Healthchecks verwenden echte
AF_UNIX-Connects mit SO_PEERCRED statt nur `test -S`. Verwaiste Socket-
Inodes werden nicht automatisch entfernt; sie verlangen eine gesicherte
Offline-Prozess-/Inode-Prüfung.
Das ist eine Sicherheitsblockade, keine künstliche Benutzerfreigabe.
`verify` attestiert weiterhin nur Image-Identität/Revision/Compose,
**nicht** die sichere Installation. Die Legacy-systemd-Units sind mit `ExecCondition=/usr/bin/false`
**vor** dem Bootstrap und zusätzlich `ExecStart=/usr/bin/false` gesperrt.

**Zusätzliche Draft-Image-Sperre:** Das gebaute Image enthält das
root-eigene, für Laufzeit-UIDs nicht entfernbare
`/etc/mark-api/UNATTESTED_DRAFT`. Alle elf vorhandenen direkten
Python-CLI-Einstiege prüfen es vor Argumentverarbeitung, DB-Zugriff,
CDP oder Betrieb und verweigern den Start. Ein explizites
`docker run --entrypoint python` hebt die Sperre daher nicht auf.
Das ist ein Schutz vor unbeabsichtigtem Start des **neuen** Images,
kein Schutz gegen einen privilegierten Docker-Administrator.

**Alte Artefakte bleiben separat riskant:** Images und Container aus
früheren Commits besitzen diese Sperren nicht. Ein alter Compose-Stand
kann sie weiterhin starten. Vor einer Hostinstallation müssen Root bzw.
der autorisierte Betreiber die tatsächlich vorhandenen alten
Mark-Images, Container, Compose-/systemd-Startpfade und offenen Prozesse
inventarisieren, kontrolliert außer Betrieb nehmen und die
alten DB-/WAL-/SHM-Inodes inklusive alter schreibbarer FDs und
`MAP_SHARED`-Zuordnungen sichern bzw. ausschließen. Das bestehende
Legacy-Konto `mark-api` darf nicht aufgrund seines Namens als
neuer unprivilegierter UID-50042-Frontend-Account vorausgesetzt werden.
Ein bloßer `sysusers`-Eintrag migriert keine vorhandenen Rechte.
Bei Unsicherheit bleibt der Start gesperrt.

**Absturz mit verwaistem Frontend-Socket:** Der Executor prüft das
vorhandene `dashboard.sock`-Inode bereits *vor* Aufbau einer
Witness-Sitzung oder SQLite-/CDP-Zugriff und verweigert den Start.
Er entfernt den Pfad nicht automatisch; erst die unabhängig
geprüfte Offline-Prozess-/Inode-Reconciliation darf einen
verwaisten Socket entfernen. Der spätere eigentliche Socket-Bind
prüft nochmals, damit ein konkurrierender Pfadersatz nicht
stillschweigend akzeptiert wird.
Erst nach unabhängigem Host-/CDP-/Recovery-/Backup-Nachweis darf ein
kontrollierter neues Release diese Stops ersetzen.

Erst nach Schließung dieser P1 und sämtlicher übrigen Gates darf der
Operator die **einzige unterstützte**
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
Image-IDs werden verweigert. Der **vorbereitete, weiterhin durch die Host-/Recovery-Sperren unerreichbare**
Startzweig verlangt root-verwaltete numerische Host-Identitäten
**50042 (Frontend), 50043 (Witness), 50044 (Executor), 50045 (Ingress),
50046 (SQLite-Owner)**
und alle neun bereits existierenden, geprüften Volumes. Die vorgesehene
Reihenfolge ist Witness → SQLite-Owner → Executor → netzloses Frontend → Host-Loopback-Ingress,
jeweils mit numerischer UID-, Image-, Netzwerk- und
Health-Attestation. Dies ist noch kein tatsächlich installierter Dienst.
Heute wird keiner dieser Live-Starts ausgeführt. Selbst nach späterer
Aktivierung ist ein grüner Socket-Healthcheck **keine** unabhängige
Protokoll-, Storage- oder CDP-Abnahme.
**Direktes `docker compose up/run` umgeht die Kontrollschnittstelle,
aber alle sechs Entrypoints und der Image-Default verweigern im Draft
jeden Produktstart.** Ein manipulierter
Entrypoint oder ein privilegierter Root-/Docker-Daemon-Operator liegt
außerhalb dieser Grenze. Direkter Compose-Aufruf ist kein freigegebener
Betriebsweg. Auch der Guard
beweist keine Mapping-Isolation, Volume-Eigentümerschaft oder Kontoberechtigung;
diese separaten Gates bleiben Pflicht. Ohne sie wird kein `live` oder
`backup` ausgeführt. Ein Image-Bau, ein erfolgreicher Guard-`verify` oder
eine grüne `docker compose config` sind keine erfolgreiche Host-Installation.
Keine Wiederholung einer unklaren Backup- oder Plattformmutation ohne Readback.

**UID-50046-Inventarsynchronisation (separates, noch nicht freigegebenes
Produktinkrement):** `mark-api-inventory-sync` liest ausschließlich den bereits
angemeldeten Besitzerbestand über einen bestehenden lokalen CDP-Reader und
schreibt die Beobachtung per UID-geprüftem, typisiertem
`StorageOwnerClient`-Socket. Der SQLite-Pfad wird dem Prozess nicht übergeben.
Vor der Browserabfrage entsteht ein dauerhafter Sync-Versuch. Erfolgreiche
leere Bestände, echte Read-Fehler und unklare Storage-ACKs bleiben unterscheidbar;
bei verlorenem Commit-ACK erfolgt kein automatischer Replay. Der CLI-Fehler
meldet daher JSON mit `kind`, bekannter `attempt_id`, optionalem
`read_status`, `cleanup_failed` und stets
`platform_retry_authorized: false`. `read_failed` bezeichnet einen
klassifizierten Browserlesefehler; `storage_unknown` einen nicht
bestätigten Store-Abschluss. Ein anschließender lokaler Cleanupfehler
überschreibt die ursprüngliche Kategorie nicht. Ein kompromittierter
Executor kann Beobachtungsdaten behaupten: Sie erzeugen weder
Write-Receipts noch Recovery-Clearances oder externe Write-Autorität.

Die nur für **explizite, autorisierte und isolierte Integrationsprüfungen**
vorgesehene CLI-Signatur lautet:

~~~text
mark-api-inventory-sync --storage-socket /run/mark-api-storage/storage.sock --storage-owner-uid 50046 --cdp-port 9222
~~~

Der Container-Draft-Gate verweigert auch diesen installierten Python-Einstieg.
Die Aufnahme als Paket-Entrypoint erlaubt **keinen** echten Docker-Cutover,
kein Testen fremder Kleinanzeigenkonten und keinen Ersatz für die vollständige
default-on Write-/Recovery-Migration.

**Frontend-Trust-Grenze:** Ein kompromittiertes UID-50042-Frontend
kann den Dashboard-Token und Origin-Header sehen und eigene typisierte
Write-API-Anfragen im Rahmen dieser Sitzung auslösen. Seine Trennung
verhindert direkten CDP-/DB-/Witness-Zugriff, ist aber **keine**
unabhängige Benutzerintention-Prüfung. Bearer, Confirmation, Ownership,
Idempotenz und No-Blind-Retry bleiben weiterhin verbindlich.

**Angreifermodell und Merge-Gate:** Der aktuelle Draft-Witness
persistiert begonnene Operationen außerhalb der Runtime-UID und kann
historisch offene Fences nach einem SQLite-Rollback im ehrlichen
Write-Pfad verweigern. Das ist **kein** Schutz gegen einen kompromittierten
Host-Root-/Docker-Daemon oder gegen direkte Browser-/CDP-Writes der
kompromittierten Runtime-UID. Ebenso ist die Echtheit einer von der Runtime
behaupteten externen Completion nicht unabhängig verifiziert.
Das gesonderte Ledger wird weder von der heutigen Backup-CLI mitsamt
SQLite atomar gesichert noch von einem Release-Restore verifiziert.
Die drei `MAP_SHARED`-P2, der CDP-Bypass und die Recovery-Cutover-Grenze
bleiben bis zu tatsächlichen Multi-UID-/Prozess-/SQLite-/WAL-/SHM-Nachweisen
und SHA-gebundenen unabhängigen Sicherheits-/Captain-Gates
**weiterhin blockierend**. Kein PR-Merge und keine reale
Kleinanzeigenmutation zu Testzwecken ohne nachgewiesene Berechtigung.

## Offline-Paar-Restore-Preflight (A6: KEINE Produkt- oder Restore-Freigabe)

Das neue optionale `mark-api-pair-restore-check` kontrolliert zwei bereits
**stillgelegte**, private, vom unabhängigen Operator besessene SQLite-v2-
und Witness-v2-Kopien gegen ein **extern verankertes** JSON-Manifest.
Voraussetzung: physisch unabhängiger Host-Cutover, kein vorbestehendes
writable FD/MAP_SHARED, vollständiger gestoppter Storage-/Witness-Writer,
beide Quellen in einem privaten Operator-Verzeichnis (`0700`) als
einzelne UID-eigene reguläre Dateien (`0600`). Beide unveränderten
Quellbilder brauchen schon **vor dem SQLite-Parser** den SQLite-Header
für den DELETE-Journal-Modus (`01 01`); ein WAL-Header (`02 02`) ist
auch bei fehlenden `-wal`-/`-shm`-Dateien unzulässig. Keine Sidecars,
Symlinks oder offenen Witness-Owner-Leases.
Jeder lexikalische Vorfahre des privaten Staging-Verzeichnisses
muss zudem ein echtes Verzeichnis unter Root- oder derselben Operator-UID
sein, ohne unsichere Gruppen-/Fremdschreibrechte oder Symlink-Komponente.
Nur ein **root-besessenes Sticky-Verzeichnis** (etwa `/tmp` mit `1777`)
ist als Ausnahme zulässig; ein fremd- oder nutzerbesessenes, allgemein
beschreibbares Vorfahrenverzeichnis ist kein sicherer Lock-/Image-Anker.
Die Prüfung verwendet dieselbe Vorgängerpfad-Richtlinie wie der
Witness-Owner. Der unabhängige Root-/Host-Cutover bleibt trotzdem eine
separate notwendige Sicherheitsgrenze.
`witness.sqlite.lock` muss als reguläre, UID-eigene Datei mit Modus
`0600` **bereits vor dem Scan vorhanden** sein. Die Prüfung hält
während des gesamten Scans ein exklusives `flock` und verweigert
fehlende/unsichere Locks; sie erzeugt oder repariert diese nicht.

Das kanonische JSON-Manifest hat die Felder `schema_version=5`,
`generation` (32 lowercase hex), `sqlite_sha256`, `witness_sha256`
und `fences`. `fences` enthält genau `sqlite_started`,
`sqlite_sync_open`, `dashboard_pending`, `ambiguous_creates`,
`ambiguous_final_creates`, `witness_started`, `witness_unresolved`,
`witness_chain_tip`, `sqlite_quarantined`, `sqlite_media_unresolved`,
`sqlite_unverified_replay`.
Auch das SQLite-Store-Schema muss **exakt** der aktuell im Code
aus `SnapshotStore._initialize()` erzeugten Version-2-Referenz
entsprechen. Das Read-only-Preflight verifiziert die vollständige Menge
aller 22 `sqlite_master`-Objekte samt Typ, Zuordnung und
SHA-256 der unveränderten DDL-Definitionen. Hinzugefügte oder versteckte
Trigger, abweichende `CHECK`-Constraints und gefälschte Indizes werden
verweigert, auch wenn die geforderten Spalten weiter vorhanden sind.
Die Digest-Referenz wird **nicht aus dem Kandidaten** gelernt.
Ein legitimer, historisch additiv migrierter v2-Bestand mit
abweichendem DDL-Layout besteht diese konservative Prüfung ebenfalls
**nicht automatisch**: er erfordert eine gesonderte, autorisierte
Offline-Schema-Migration und erneute unabhängige Paarattestierung,
keinesfalls ein automatisches Reparieren oder Freigeben durch den
Checker.

Die Witness-DDL-Prüfung verlangt die vollständigen, unveränderten
Tabellen-, UNIQUE-Partial-Index- und RAISE-Abbruch-Trigger-Semantiken
einschließlich exakter SQL-Literalwerte und Token-Grenzen. Zusätzlich
wird jedes `sqlite_`-Schemaobjekt geprüft; einzig der echte interne
Session-Primärschlüsselindex ist in dieser Schema-Version erlaubt.
Jedes Witness-Event muss zusätzlich auf eine vorhandene
Ursprungs-`sessions`-Zeile verweisen. Ein hashkonsistentes
`started`-Event nach Löschung dieser Session ist **nicht**
wiederherstellbar: die unabhängige Operator-Clearance verlangt eine
versiegelte Ursprungssession. Die Prüfung lehnt auch verwaiste
`opened`- und `sealed`-Events ab.

Alle Witness-Session-/Event-Zeitpunkte müssen die von `FenceLedger._now()`
erzeugte kanonische UTC-ISO-Form mit maximal 64 Zeichen tragen. Auch
eine neu gehashte Eventkette mit einem überlangen oder unlesbaren
`started_at` ist nicht wiederherstellbar: der unabhängige
`FenceWitnessClient.pending_page()` lehnt sie ab.

Auch eine kryptografisch konsistente Witness-Hashkette wird als
**nicht wiederherstellbar** verworfen, wenn die dauerhaften Event-Sequenzen
nicht vom tatsächlichen Witness-Protokoll erzeugbar sind: `opened` vor
`started`, `started` vor etwaigem `completed`, `sealed` nach dem
letzten Runtime-Event der Ursprungssession und zwingend **vor**
`operator_cleared`. Nachträglich umsortierte und neu gehashte
Clearances sind kein Recovery-Beweis. Historische `runtime_cleared`-
Beobachtungen ersetzen weiterhin keine unabhängige Operator-Clearance.
Für SQLite-Write-Starts und Witness-Operationen gilt exakt die
gemeinsame Witness-/StorageOwner-Key-Grammatik `[A-Za-z0-9][A-Za-z0-9._:-]{0,159}`: Unicode, Leerzeichen,
unzulässige Sonderzeichen und überlange Keys lassen sich nicht durch
einen übereinstimmend manipulierten Paarstand legitimieren.

Die bisherigen Schema-v1/v2/v3/v4-Manifeste bleiben nach dieser
sicherheitsrelevanten Erweiterung grundsätzlich ungültig.
Das vollständige `fences`-Inventar wird zudem **typbewahrend**
als kanonische JSON-Bytefolge verglichen: `false` ist kein numerisches
`0`, `true` keine Zeilen-ID `1` und `1.0` keine Integer-ID
`1`. Auch ein extern mit dem manipulierten Manifest-Digest gepinnter
solcher Kandidat wird verworfen. Damit bleiben SQLite-/Witness-IDs,
Bestätigungs-Bools und Hashketten-Sequenzen semantisch getrennt.
Zusätzlich werden sämtliche persistierten Write-Request-Zeitstempel
(requested_at, execution_started_at und completed_at) vor der
Restore-Prüfung wie beim produktiven Datenbank-Readback als
zeitzonenbehaftete ISO-Zeitpunkte validiert. Auch niemals gestartete
Requests dürfen nicht mit unlesbaren Zeitfeldern restauriert werden.

Vor dem Inventar werden alle SQLite-Recovery-Clearances mit ihrer
tatsächlich ausgeführten Ursprungsoperation (exakter Key und SHA,
gesetzter execution_started_at) abgeglichen. Verwaiste, falsch
gehashte und niemals gestartete Write-Clearances werden ebenso
verworfen wie Executor-Clearances für UNKNOWN-Antworten oder
Clearances auf gewöhnliche erfolgreich abgeschlossene Writes.
Damit darf eine gefälschte Clearance keine Create-, Media- oder
Quarantäne-Fence verbergen. Auch `evidence_reference` muss genau der
getrimmten kanonischen Speicherung aus `record_write_recovery_clearance()`
entsprechen; führende/abschließende Leerzeichen oder Tabs werden verworfen.
Das ist eine **Offline-Diagnose**, keine unabhängige Verifikation der
externen Plattformantwort.

Dashboard-Pending-Writes müssen vollständig durch den typisierten
StorageOwner-Decoder **und** dessen tatsächlichen
`_dashboard_pending_contract(method,path)` einschließlich
Scope, Resource, Ad-ID und Payload-Präsenz sowie kanonischer
`_validate_pending_wire_body` lesbar sein; nicht zulässige
Routenidentitäten, Key-Grammatik und Payloads verhindern die
Offline-Paardiagnose.
Eine historische Witness-Runtime-Clearance benötigt vorher eine
create_media-Completion mit Outcome ambiguous; ein bestätigter
oder mit precondition_failed abgeschlossener Submit legitimiert sie nicht.

Sämtliche Write-Request-Zeilen müssen durch den realen StorageOwner-
Record-Decoder lesbar sein; auch Status, Owner, Ergebnisübergang und
kanonische JSON-Antwort sind Teil der Prüfung. Bei **jeder ungestarteten**
Operation (also `execution_started_at=NULL`) muss der ursprüngliche
`claim_owner` erhalten sein: der aktuelle StorageOwner erstellt Claims
ausschließlich mit gültigem Owner, und ein `in_progress`-Claim ohne
Owner ist nach Neustart nicht einmal durch den zulässigen
`allow_abandoned_takeover`-Pfad übernehmbar. Solche historischen oder
manipulierten Claims dürfen im Offline-Paarrestore nicht als
fence-frei attestiert werden. Eine als `completed`
persistierte Operation **ohne** `execution_started_at` hat keinen
Witness-Start: Sie darf ausschließlich den vom StorageOwner selbst
konstruierten HTTP-409-Abbruch `write_claim_cancelled_before_begin`
enthalten, mit exakt passendem Idempotency-Key und
`platform_retry_authorized=false`. Auch kanonische HTTP-200-/500-
Antworten oder zusätzliche JSON-Felder sind hierfür unzulässig;
ein Restore-Preflight darf sie nicht als unproblematisch durchwinken. Ein vorliegender
Witness-Completion-Digest muss genau zur dauerhaft gespeicherten
SQLite-Antwort passen. Fehlt die Witness-Completion nach einem Crash
zwischen SQLite-Persistenz und Witness-Append, bleibt die Antwort als
`sqlite_unverified_replay` im generationengebundenen Manifest sichtbar
und ist **niemals** eine erfolgreiche Replay-Bestätigung. Vor einer
Operator-Recovery bleibt der ursprüngliche Witness-Fence offen.
Nach einer tatsächlich an dieselbe Ursprungsoperation (Key/SHA/Session)
gebundenen unabhängigen Operator-Clearance **in Witness und SQLite**
darf dieses Fence geschlossen sein; das unbestätigte Replay bleibt
weiterhin im Manifest sichtbar. Die SQLite-Clearance muss dabei
`operator_verified_postread` sein: ein `media_submit_reconciled`-
Executor-Eintrag ist keine unabhängige Operator-Evidenz und kann eine
fehlende Witness-Completion auch dann nicht freigeben, wenn der Witness
seinerseits operatorseitig geklärt wurde. Solange der Witness-Fence
offen ist, darf auf SQLite-Seite **überhaupt keine** Clearance für diese
nicht verifizierte Antwort vorliegen. Fehlt die Witness-Completion, wird jede einseitige Clearance einer
lokal ungelösten SQLite-Recovery-Operation abgewiesen. Auch **mit**
persistierter Witness-Completion darf eine Executor-Clearance
`media_submit_reconciled` keine unabhängige
SQLite-`operator_verified_postread`-Clearance ersetzen.
Ist die Witness-Completion hingegen exakt vorhanden und der Witness
operatorseitig geklärt, kann ein **weiterhin offener** SQLite-Fence
ohne unabhängige SQLite-Operator-Clearance im Offline-Inventar sichtbar bleiben. Das ist
ausschließlich ein ungeklärter Diagnosezustand: niemals eine
Recovery-Fence-Freigabe, sichere HTTP-Replay-Autorität oder Host-Cutover-Erlaubnis.

**Media-JSON und historische Executor-Clearances:** Sowohl der
produktive `SnapshotStore.create_recovery_pending()` als auch der
Offline-Preflight erkennen Media-Responses ausschließlich anhand des
Top-Level-JSON-Mitglieds `media_persistence_confirmed`
(`json_type(..., '$.media_persistence_confirmed')`), **nicht** an einem
Texttreffer in der gesamten Response. Dieser Text darf etwa in einem
vom Client gesetzten Idempotency-Key oder Fehlerdetail vorkommen,
ohne dadurch einen falschen Media-Recovery-Fence zu erzeugen.

Eine bereits persistierte `media_submit_reconciled`-SQLite-Clearance
stammt nur aus Executor-Evidenz und kann unter dem aktuellen eindeutigen
Clearance-Key nicht zusätzlich mit `operator_verified_postread`
überschrieben oder ergänzt werden. Das Runtime-
`SnapshotStore.create_recovery_pending()` behandelt diese lokale
Executor-Zeile historisch als geklärt (ohne unabhängigen Witness);
nach einer späteren Witness-Operator-Clearance wäre daher ein neuer
Create zulässig, obwohl die SQLite-Media-Wirkung nicht unabhängig
reconciliert sein muss. **Der aktuelle Offline-Paar-Preflight verweigert
diese Kombination ausdrücklich**, unabhängig davon, ob eine passende
Witness-Completion existiert. Sie darf **weder** als zulässiger
`sqlite_media_unresolved`-Restore **noch** als unabhängige
Replay-/Write-/Host-Cutover-Freigabe ausgegeben werden. Diese
historisch zulässige lokale Recovery kann erst nach nachweislich
gemeinsam durchgesetzter StorageOwner-/Witness-Laufzeitautorität
produktiv integriert werden. Der bestehende No-Witness-Lokalmodus
wird durch diesen rein diagnostischen Sicherheits-Stop nicht verändert.

**Abgeschlossene Antworten und der offene Media-Checkpoint-Ausnahmefall:**
Für einen begonnenen, dauerhaft abgeschlossenen Write **ohne** offenen
`sqlite_quarantined`-, `sqlite_media_unresolved`- **oder**
`before_media_post_read`-Checkpoint-Fence gibt es im
`SnapshotStore.record_write_recovery_clearance()` keinen zusätzlichen
SQLite-Operator-Clearance-Pfad. Das gilt für kanonische Nicht-Media-
Antworten (HTTP 200, 409, 500), vollständig bestätigte Media-
Persistenz sowie `precondition_failed`-Receipts ohne offenen
Create-Checkpoint. Der HTTP-Status und das bloße Vorkommen von
`media_persistence_confirmed` begründen weder eine Clearance noch
eine erneute Plattform-Write-Autorisierung.

**Wichtige Ausnahme: Kern-Create bestätigt, Media nicht bestätigt.**
Ein abgeschlossenes Create kann `operation_receipt.outcome=confirmed`
und eine gültige `created_ad_id` haben, aber zugleich
`media_persistence_confirmed=false` und den Media-Post-Read-Status
`not_read`, `verifier_unavailable`, `mismatch` oder `unknown`.
Auch wenn diese HTTP-Antwort **nicht** in `sqlite_media_unresolved`
auftaucht, hält der zuvor persistierte
`before_media_post_read`-Checkpoint in `ambiguous_creates` die
Create-Recovery-Sperre ausdrücklich offen. Nur nach unabhängigem,
autorisiertem Post-Read-Nachweis zur **exakt begonnenen**
Ursprungsoperation (Idempotency-Key und Request-SHA) darf der
StorageOwner `operator_verified_postread` persistieren;
`media_submit_reconciled` aus dem Executor wird dafür verweigert.
Der Witness benötigt zusätzlich seine **eigene**, genau an
Key/SHA/Session gebundene Operator-Clearance. Nach einer einseitigen
SQLite-Clearance bei noch offenem Witness lehnt der Offline-Preflight
die Paar-Generation ab. Eine **nur** im Witness persistierte
Operator-Clearance ist lediglich diagnostizierbar: der SQLite-Checkpoint
bleibt in `ambiguous_creates`, `create_recovery_pending()` bleibt
`true`, und eine SQLite-Antwort ohne Witness-Completion bleibt in
`sqlite_unverified_replay`. Ein solcher Zwischenstand ist weder
Fence-Closure noch sicherer Paar-Restore, Replay, Host-Cutover oder
Plattform-Schreibrecht. Erst beide unabhängigen, passenden Clearances
können diesen Create-Checkpoint schließen.

Owner-HTTP-202-`write_outcome_unverified` und abgeschlossene
ambivalente Media-Replies mit eigenem lokalen
`sqlite_quarantined`- bzw. `sqlite_media_unresolved`-Fence verlangen
zum **Schließen** des Fences stets die exakt an dieselbe ursprüngliche
Operation gebundene SQLite-`operator_verified_postread`-Clearance und
die unabhängig belegte Witness-Operator-Clearance.

**Witness-only ist nicht immer ein zulässiger Offline-Zwischenstand.**
Ist bei einem solchen lokalen 202-/Media-Recovery-Fence die
Witness-Completion **nicht** dauerhaft vorhanden, lehnt der
Paar-Preflight eine bloße Witness-Operator-Clearance **auch als reine
Offline-Diagnose** ab. Liegt dagegen eine vollständig passende,
hashgebundene Witness-Completion bereits vor, kann derselbe
Witness-only-Zustand bei weiterhin offenem SQLite-Fence als
`sqlite_quarantined` bzw. `sqlite_media_unresolved` **diagnostiziert**
werden; das ist ausdrücklich keine Fence-Schließung oder Wiederfreigabe.
Diese Differenz wird durch die negativen Tests
`test_quarantined_202_without_sqlite_clearance_cannot_use_nonmedia_exception`
und `test_missing_completion_with_operator_clearance_is_not_safe_replay`
sowie durch
`test_witness_only_ambiguous_media_with_completion_is_diagnostic`
festgelegt.

Eine **abgeschlossene Nicht-Media-Antwort ohne offenen lokalen
Recovery-Fence** hat dagegen keinen künstlich zu erzwingenden
SQLite-Clearance-Pfad: Fehlt ihre Witness-Completion, darf sie nach
unabhängiger Witness-Operator-Clearance lediglich als offene
`sqlite_unverified_replay`-Diagnose erhalten bleiben, niemals als
bestätigtes Replay oder Plattform-Ergebnis. Ebenso ist der oben
gesondert behandelte `before_media_post_read`-Checkpoint bei
bestätigtem Kern-Create und unbestätigter Media-Antwort **nicht**
automatisch ein `sqlite_media_unresolved`-Reply-Fence: Witness-only
darf hier den offenen `ambiguous_creates`-Checkpoint sowie das
unverifizierte Replay sichtbar erhalten, aber nie autorisieren.

In keinem dieser Zustände darf der lokale Executor eine echte
unabhängige Post-Read-Evidenz durch `media_submit_reconciled`
ersetzen. Reine SQLite- oder Witness-Nachweise allein erteilen keine
Host-Cutover-, Restore-, Replay- oder erneute externe Write-Erlaubnis.

Ist ein Witness-Completion-Digest inkonsistent oder fehlt der
SQLite-Gegenpart, wird der Restore-Kandidat verworfen. Fehlt auch
die Witness-Completion, wird bei einer einseitigen SQLite-Clearance
ohne Witness-Operator-Clearance die Offline-Paardiagnose verweigert.
Eine anschließend unabhängig Witness-operatorgeklärte historische
Executor-Media-Clearance bleibt derzeit **nicht attestierbar**:
Der Offline-Preflight lehnt den Paarrestore-Kandidaten ab, damit die
im produktiven lokalen Runtime-Pfad fälschlich entfallende Fence
nicht beim Host-Cutover verschwinden kann.
Ist dagegen eine exakt passende Witness-Completion vorhanden, kann
ein noch ungelöster SQLite-Fence bei Witness-only-Operator-Clearance
als **offen** diagnostiziert werden. Seine Inventarisierung
bedeutet weder Closure noch Restore-/Write-Autorisierung. Auch zwei unabhängig
hashkonsistente Witness-`started`-Events ohne dazwischenliegende
Operator-Clearance sind unmöglich: der echte `FenceLedger` verweigert
jeden weiteren Start, solange irgendein vorheriger Write offen ist. Manifest-Schema v5 ist
verpflichtend. Historische v1-Claims mit Witness-Outcomes
`confirmed` oder `precondition_failed` dürfen nach v1→v2-Migration
als untrusted Event erhalten bleiben; auch sie beweisen **keinen**
externen Erfolg und werden durch Witness-Replay weiterhin als
`ambiguous` behandelt. Für eine `runtime_cleared` verlangt der aktuelle v5-Preflight
eine zuvor attestierte `ambiguous`-Completion. Abweichende tatsächliche
v1-Ledger-Ereignisse können nach expliziter v1→v2-Migration noch
existieren; deren Herkunft ist damit nicht unabhängig attestiert.
Solche Restore-Kandidaten bleiben bis zu gesonderter historischer
Provenienz-/Sicherheitsabnahme gesperrt.

Alle Identitäten sind sortiert und bei der Paarsicherung durch die
**unabhängige** Backup-Autorität zu attestieren. Der Checker verifiziert
vollständige File-Digests, SQLite-Integrität/Schemata, die Witness-
Hashkette, Operation-/Key-/SHA-Übereinstimmung und den Erhalt aller
im extern verankerten Manifest enthaltenen Pending-Fences. Jede persistierte finale `create_operation_receipts`-Zeile muss
vor jeder Checkpoint-Suppression dem tatsächlichen
`CreateOperationReceipt`-Vertrag genügen: insbesondere kanonische
zeitzonenbewusste Start-/Endzeiten in korrekter Reihenfolge, gültige
Outcome-/Media-Kombinationen, typisierte Statusfelder und passende
Snapshot-Anzeigenidentitäten. Ein künstlich geänderter Final-Receipt,
der lediglich die für den SQL-Join verwendeten Spalten erfüllt,
darf einen offenen Create-Checkpoint nicht verbergen.
Auch ein für sich gültiger Final-Receipt ist keine Fortsetzung des
gleichen Checkpoints, wenn die bei Create-Persistenz unveränderlichen
Kernfelder abweichen: `pre_read_status`,
`confirmation_pre_read_status`, `post_read_status`,
`confirmation_post_read_status`, `content_post_read_status` oder
`writer_error`. Die Offline-Inspektion behält den ursprünglichen
Checkpoint dann als `ambiguous_creates` bei, statt einen
recoverypflichtigen Create fälschlich als abgeschlossen auszugeben.
Gleiches gilt, wenn ein an sich gültiger Media-Final-Receipt ein
**früheres** `completed_at` als der ursprüngliche vor dem
Media-Postread persistierte Checkpoint trägt. Der echte Runtime-
Übergang setzt den Endzeitpunkt mit `max(previous, clock())`;
der Checker vergleicht dafür tatsächliche timezone-aware
Zeitpunkte in Python, nicht fehleranfällige ISO-Textreihenfolge oder
auf Millisekunden gerundete SQLite-Zeitstempel. Legitime unterschiedliche
Offsets dürfen die Recovery-Fence dagegen nicht künstlich offenhalten.

Auch **jede persistierte ursprüngliche** `create_operation_checkpoints`-Zeile
wird unabhängig von späteren finalen Receipts auf den von
`append_create_operation_checkpoint()` zugelassenen Zustand geprüft:
`before_media_post_read`, bestätigter ausgeführter Create mit Anzeigen-ID,
zeitzonenbewusste und korrekt geordnete Zeitpunkte, gültige Pflichtstatus
und die unveränderten Snapshot-Anzeigenidentitäten. Eine nachträglich
manipulierte Ursprungszeile darf daher auch bei passend erscheinendem
finalen Receipt keine verschwundene Recovery-Fence ergeben.

Ein vor dem
Media-Postread dauerhaft geschriebenes Create-Checkpoint trägt
`outcome=confirmed` und bleibt bei Prozessabsturz **ohne** passendes
abschließendes Media-Receipt offen. Auch ein fremdes Receipt oder ein
Media-Receipt ohne nachgewiesene lokale Media-Persistenz kann diesen
Checkpoint nicht aus dem Offline-Pending-Inventar entfernen. Ein
zugehöriges bestätigtes Receipt bedeutet dabei **keine** unabhängige
Bestätigung des tatsächlichen Kleinanzeigen-Zustands.
Auch ein **bereits abschließend persistiertes** Media-Create-Receipt mit
`outcome=ambiguous`, `writer_invoked=1` und vorhandenem Media-Postread
bleibt ein eigenständiges offenes Fence, selbst **ohne** Checkpoint.
Das Inventar `ambiguous_final_creates` speichert die betroffenen
Receipt-Zeilen-IDs; nur eine exakt an den ursprünglichen begonnenen
Write-Request samt SHA gebundene unabhängige Recovery-Clearance kann
dieses Teilinventar ausnehmen. Der Offline-Checker vergibt niemals
solche Clearances. `ambiguous_create_count` zählt beide offenen
Create-Fence-Arten. Auch ein begonnener und danach durch den
unabhängigen Owner mit dem kanonischen HTTP-202-Status
`write_outcome_unverified` abgeschlossener Write-Request bleibt als
`sqlite_quarantined` ein eigener Recovery-Fence, **selbst ohne**
Create-Checkpoint oder Media-Receipt. Nur eine an Key **und** Request-SHA
gebundene persistierte Recovery-Clearance nimmt ihn aus diesem Teilinventar.
`sqlite_quarantined_count` zählt solche Operationen; der konservative
`ambiguous_create_count` umfasst auch diese potentiell bereits auf
Kleinanzeigen ausgeführten Aufträge. Ein fehlerhaftes UNKNOWN-Envelope
wird nicht als gültige Entwarnung akzeptiert.
Unabhängig davon wird jede bereits HTTP-abgeschlossene Media-Write-
Antwort mit ungelöstem Outcome aus `write_api_requests` als eigenes
`sqlite_media_unresolved`-Fence attestiert. Dies gilt insbesondere für
HTTP 500 nach Browser-Submit, wenn ein `operation_receipt` fehlt,
sowie für einen persistierten aufgerufenen `ambiguous`-Receipt.
Nur eine tatsächlich an denselben Write-Key und Request-SHA gebundene
unabhängige Recovery-Clearance nimmt die Operation aus dem Inventar.
Ungültige oder untypisierte Media-Response-Envelopes werden verweigert;
`sqlite_media_unresolved_count` und `ambiguous_create_count`
zählen diese offenen Vorgänge konservativ. Es wird kein HTTP-Status
zur unabhängigen Bestätigung eines Kleinanzeigen-Ergebnisses erhoben.

**Vor jedem Öffnen durch SQLite** werden beide bereits per FD gepinnten
Datei-Digests gegen die extern im Manifest verankerten Hashes verglichen.
Ein abweichender SHA führt unmittelbar zum Abbruch, noch vor
`integrity_check` oder sonstigem Schema-Parsing. Bei passenden Hashes
werden die **exakt per Datei-Descriptor gepinnten Bytes** (nicht
ein erneut geöffneter, umbenennbarer Pfad) größenbegrenzt in eine
In-Memory-SQLite-Datenbank deserialisiert. Beide Verbindungen erzwingen
`query_only=ON` und `trusted_schema=OFF`; der ursprüngliche
Witness-DELETE-Journalmodus wird separat aus dem gepinnten
SQLite-Dateiheader geprüft. Dateien über 512 MiB sind für diesen
begrenzten Prüfpfad nicht freigegeben; es erfolgt kein unsicherer
Dateipfad-Fallback. Symlink-Ahnen und zwischenzeitlich vertauschte
Quellpfade werden nicht als unabhängige Provenienz akzeptiert.
Der ungebundene Diagnoseaufruf
`inspect_offline_pair()` ohne erwartete Hashes bleibt ausdrücklich
**keine** vertrauenswürdige Restore-Aufnahmeprüfung.

Der Aufruf erfolgt **erst nach** Bereitstellung aller vertrauenswürdig
erzeugten Kopien und des Digest-Ankers außerhalb der DB-/Witness-Volumes:

```sh
mark-api-pair-restore-check --sqlite /privat/mark.sqlite --witness /privat/witness.sqlite --manifest /privat/manifest.json --expected-manifest-sha256 VORHER_EXTERN_ATTESTIERTER_SHA256
```

**Sicherheitsgrenze:** `inspect_offline_pair()` ist ein nicht
attestiertes Diagnoseinventar, **kein** Manifest-Signer. Wird der
erwartete SHA erst aus einer möglicherweise kompromittierten Backup-
Datei berechnet, beweist die Prüfung keine Quellintegrität.
Der neue Checker erzeugt, publiziert und restauriert keine Sicherung,
löscht keine Write-Fences und heilt keine beschädigten Bestände.
Er kann die historische MAP_SHARED-/WAL-Rollback-Angriffsklasse
gegen den **vor** dem Backup manipulierten Quellstand nicht allein
widerlegen. Auch mit Checker-PASS fehlen das unabhängige sichere
Paarbackup-Generationenprotokoll, ein root-attestierter Host-Cutover,
Crash-/Restore-Negativbelege und externe Plattformfreigaben.
`mark-api-container-backup.sh`, `live` und Draft-Entrypoints
bleiben gesperrt; PR #78 und Issue #76 bleiben NO-GO.
