# A6-Integrationsstand, 23.10.2026

**Unvollständig; kein Liefer- oder Multi-UID-Abnahmenachweis.** Dieser Bericht
bezieht sich auf die uncommittete Integration im bestehenden Worktree
`kleinanzeiger-a6-customer-20261022`: HEAD `ab621837`, MERGE_HEAD `280e861`.
Vorhandene Index- und Arbeitskopieänderungen bleiben erhalten; kein Commit,
Reset, Branchwechsel, Deployment oder externer Plattform-/Provideraufruf.

## Verifizierte Verbesserung dieses Arbeitslaufs

Ein Owner-Pending-Claim kann das 32-KiB-Requestlimit einhalten, während seine
spätere Recovery-Seite wegen des größeren Antwort-Envelopes nicht mehr
übertragbar ist. Das ließ einen dauerhaft gespeicherten, über RPC nicht mehr
lesbaren Pending-Fence zu. Der Owner prüft nun vor der Persistenz die exakt
codierte einzelne Recovery-Zeile einschließlich Antwort-Envelope. Ein zu
großer Claim wird ohne neue SQLite-Zeile abgewiesen; vorhandene Fences bleiben
erhalten. Bereits historisch gespeicherte übergroße Zeilen werden weiterhin
sicher abgewiesen und weder gekürzt noch gelöscht.

Pending-Readback prüft jetzt dieselbe exakte Methoden-/Pfad-/Scope-/Ressourcen-/
Anzeigenbindung und denselben kanonischen, begrenzten JSON-Vertrag wie die
Admission. Vertauschte Identitäten, fehlende Bodies, doppelte Mitglieder,
überlaufende Zahlen, Surrogate und übertiefe Bodies werden zurückgewiesen.
Es wurde keine Completion-, ACK-, SQL- oder Clearance-RPC hinzugefügt.

Ein bestehender Sync-Migrationstest erwartete noch Schema-v1. Er erwartet
jetzt das bereits implementierte A6-Schema-v2 und prüft zusätzlich den
Fortbestand der Recovery-Clearance-Tabelle. Die produktive Schema- und
Migrationslogik wurde dabei nicht geändert.

## Ausgeführte Prüfungen

Mit den vorhandenen uv-Interpretern Python **3.12.12 und 3.13.9** jeweils:

```bash
PYTHONPATH=src:tests <python> -m unittest \
  test_storage test_storage_owner_pending_admission \
  test_storage_collation_contract test_storage_connection_lifecycle \
  test_sync_attempt_journal test_application test_analytics -q
```

**123 Tests bestanden je Interpreter.** Die vier neuen Tests führen echte
Client-Decodierung, Closed-Table-Dispatch und SQLite-Persistenz mit einem
Transport im Speicher aus. Sie prüfen Größenrandfälle, Unicode, verlorene
Antworten, Wiederöffnung, Replay-/Ressourcenbindung, erhaltene Pending-Fences
und die Sperre nach begonnenen beziehungsweise quarantänisierten Operationen.
Socket- und UID-Attestation sind in diesem ausdrücklich begrenzten Harness
ausgenommen; bestehende Sicherheits-/Integrationstests wurden nicht
abgeschaltet oder entsprechend umgebaut. Python 3.13 meldete außerdem
ResourceWarnings für vorhandene rohe SQLite-Testverbindungen in
`test_storage_collation_contract.py`; diese sind nicht als behoben ausgewiesen.

Source-Compile aller **124 Python-Dateien** unter `src` und `tests` unter
beiden Interpretern sowie `git diff --check`: bestanden. Es wurde keine
Testabhängigkeit installiert.

Die versuchten bestehenden Integrationssuiten sind **nicht bestanden**:

- `test_dashboard_full_lifecycle_e2e`: zwei Fehler beim Anlegen des lokalen
  HTTP-Sockets (`PermissionError: EPERM`), vor der Kundenreise.
- `test_storage_owner`: 77 ausgeführt, 65 Fehler. Insbesondere verweigert die
  bestehende Owner-Attestation die Sandbox-Ahnenverzeichnisse: `/` und `/tmp`
  erscheinen als UID 65534 statt root oder eigener UID. Ein separater Check
  bestätigt außerdem `EPERM` beim Binden eines temporären Unix-Sockets.
  Die Sicherheitsprüfung bleibt unverändert.

Wheel-Bau, frische Installation, Browser-/Media-Lifecycle, volle Suite,
Multi-UID-/FD-/MAP_SHARED-Proof und CI wurden in diesem Arbeitslauf nicht
nachgewiesen. Synthetische Dispatch-Ergebnisse ersetzen diese Prüfungen nicht.

### Nachtrag: lokale Dashboard-Reconciliation

Mit vorhandenen Cache-Paketen, ohne Netzwerk oder Systeminstallation,
ausgeführt unter Python **3.12.12 und 3.13.9** (uv editable):

```bash
UV_CACHE_DIR=/tmp/kleinanzeiger-a6-replay-uv uv run --offline --no-project \
  --python 3.13 --with pytest --with-editable . python -m pytest -q \
  tests/test_dashboard_local_reconciliation.py \
  tests/test_storage_owner_pending_admission.py tests/test_storage.py \
  tests/test_storage_connection_lifecycle.py tests/test_application.py
```

**96 Tests und 95 Subtests bestanden je Interpreter**, derselbe Aufruf
zusätzlich mit `--python 3.12`. Der neue ergänzende Dispatch-Harness ersetzt
nur den HTTP-Transport durch Speicherobjekte; Write-Authentifizierung,
Orchestrator, Operations-Receipts und SQLite bleiben real. Er prüft CRUD,
Restart vor und zwischen den ACK-Phasen, Replay ohne weiteren Backend-Aufruf,
Payload-/Route-/Ressourcen-/Key-Kollisionen, manipulierte Antworten, fehlende
oder mehrdeutige Receipts, begonnene/unbekannte/202-Ergebnisse sowie
Mock-/Principal-/Owner-/Container-Sperren. Die ursprünglichen beiden echten
HTTP-Lifecycle-Tests behalten sämtliche Assertions und zählen jetzt zusätzlich
Backend-Aufrufe. Sie wurden nicht in Dispatch-Tests umgewandelt.

Unter Python 3.13 außerdem **64 Launcher-/Shutdown-Tests und 24 Subtests
bestanden**, mit explizitem Ausschluss der beiden socketabhängigen
Launcher-Tests. Darunter ist die Prüfung, dass Witness-Konfiguration den
lokalen Reconciliation-Pfad trotz bekanntem Principal deaktiviert.

Der abschließende ungekürzte Aufruf von `test_dashboard_full_lifecycle_e2e`,
`test_query_dashboard`, `test_dashboard_owner` und `test_storage_owner*`
ist **nicht grün: 155 fehlgeschlagen, 36 bestanden, 2 übersprungen**.
86 Fehler entstehen bei der vom Shell-Sandbox verweigerten Socket-Erzeugung
(`EPERM`), 69 bei der unveränderten Inode-/Ahnen-UID-Attestation. Insbesondere
ist die echte HTTP-Kundenreise hier weiterhin nicht nachgewiesen. Ein lokaler
`systemd-run --user` scheitert ebenfalls am verweigerten Bus-Zugriff. Der
entsprechende Durable-Job-Werkzeugaufruf wurde nicht ausgeführt, weil er
Approval benötigt und die Sitzung die Approval-Policy `never` hat.
Keine Attestation wurde gelockert. `git diff --check` und Source-Compile
der geänderten Python-Dateien bestanden. Kein Commit, Staging oder Push.

**A6 bleibt unvollständig; unabhängiger UID-, Backup-/Restore-, installierter
Browser-/Media- und echter HTTP-Proof bleiben offen.**

## Konkrete technische Restarbeiten

1. **Trusted Completion Actor:** Der untrusted Owner-RPC-Client kann nur
   claim/begin/read, unbegonnene Claims abbrechen oder begonnene als festes
   UNKNOWN quarantänisieren. Es fehlt ein separat geschützter Akteur, der
   tatsächliche externe Ergebnisse unabhängig prüft und unveränderliche,
   an Generation, Key, Fingerprint, Aktion und Antwort gebundene Receipts
   persistiert. Ein Caller-JSON, Browser-ACK oder Inventar-Sync ist keine
   solche Evidenz. Verlorene Completion-Antworten müssen ausschließlich
   beobachtend auflösbar sein.
2. **Produktkomposition:** `build_product_launcher` erstellt weiterhin einen
   `SnapshotStore`; `create_server(StorageOwnerClient, write_proxy=...)`
   verweigert vor dem HTTP-Bind. Write-API, Orchestrator und Media-Runtime
   benötigen außerdem persistente Receipts/Checkpoints, exklusive Laufzeit-
   Leases und geschützte Media-Handles. Diese Methoden einfach ungeprüft als
   RPC zu exportieren würde die A6-Grenze aufheben.
3. **Bedienbare unabhängige Pending-Recovery:** Für den direkten lokalen
   `SnapshotStore` existiert jetzt ein ausdrücklich konfigurierter Same-UID-
   Pfad für lesendes Receipt-Replay und zweiphasigen ACK. Er prüft die exakte
   Pending-Identität, den Backend-Principal, den normalisierten Request-
   Fingerprint, den abgeschlossenen HTTP-200-Journaleintrag und dessen
   kanonisches Receipt gegen genau ein persistiertes Operations-Receipt.
   Replay kontaktiert den Backend-HTTP-Server nicht erneut. Beide ACK-Phasen
   prüfen das Ergebnis neu. Unbekannte/in-progress/202-Ergebnisse, Kollisionen
   und veränderte Antworten bleiben gesperrt. Unkonfigurierte Mock-Proxys,
   Witness-Konfigurationen und Container-Drafts erhalten diese lokale
   Autorität nicht. `StorageOwnerClient` mit Write-Proxy wird weiterhin vor
   dem HTTP-Bind verweigert; es gibt keine neue Owner-Completion-/ACK-/
   Clearance-RPC. Die unabhängige A6-Recovery einschließlich Browser- und
   Medienreise bleibt offen. Der lokale Pfad ersetzt keinen Multi-UID-Proof.
4. **Tatsächliche Installation und Cutover:** Der normale lokale Start ist
   Same-UID/direct-SQLite, auch mit optionalem FenceWitness kein Beleg für
   isolierten Rohdatenbankzugriff. Die Multi-UID-Containerstarts bleiben
   Drafts mit technischen Sperren. Frischer, separat UID-geschützter Store,
   Entzug alter SQLite-/WAL-/SHM-FDs und MAP_SHARED-/Namespace-Aliase,
   abgestimmter Witness-/Backup-/Restore-Zustand und die installierte
   UI-Write-/Media-/Recovery-Reise müssen unabhängig geprüft werden.

D-028/D-029 bleiben verbindlich: Default-on Read/Write und vollständiger
Produktumfang, optional `--read-only`, Generierung zuerst als Entwurf. Eine
allgemeine externe Plattformfreigabe ist kein Entwicklungsblocker. Die oben
genannten offenen Punkte sind technische Integrations- und Sicherheitslücken.


## Neue verifizierte Produkt-/Recovery-Abnahme, 23.10.2026

Die obigen CLI-Sandbox-Einschränkungen wurden durch authentisierte
Grabowski-Systemd-Testjobs umgangen, **ohne Sicherheitsattestierungen
zu lockern**. Das ist keine Host-A6-Freigabe:

- Echtes lokales HTTP CRUD + Restart/Replay/2-Phasen-ACK: Python
  3.12 und 3.13 jeweils 13 Tests/32 Subtests PASS
  (Jobs grabowski-job-836c951c76fe, grabowski-job-66e80ff73d4c).
- Dashboard/Owner/Query/Launcher: Python 3.13
  172 Tests/176 Subtests PASS (grabowski-job-73745f21353d).
- Vollständige Projektquelltests: Python 3.13 **1.825 PASS,
  21 SKIP, 2 fork()-DeprecationWarnings**, 1.263 Subtests
  (grabowski-job-f9976c2c3706); SKIP ist kein Multi-UID-A6-Beweis.
- Die vier aktuellen Binary-Blank-ID-Indexe sind mit exakten DDL-
  SHA-256-Digests in der unabhängigen Offline-Pair-Schemaprüfung gepinnt.
  Die neue Sicherheitsregression entfernt jeden Index einzeln und
  verlangt eine harte Ablehnung des Restore. A6 Pair-/Backup-/Restore:
  226 PASS/91 Subtests; A6-Subset 241 PASS/15 SKIP
  (Jobs grabowski-job-4cdbc5eb3f18, grabowski-job-3ff9b571f4e6).
- Frisches lokales Wheel, SHA-256
  bd52b5304b5b341983e918f331f51f9231af9d2e64e74572d73fa0388f6db46c,
  wurde unter Python 3.12.12 **und** 3.13.9 separat installiert:
  offizielle installierte NetNS-only Offline-Wheel-Akzeptanz jeweils
  PASS (Jobs grabowski-job-b44c457263d7 und
  grabowski-job-75c5c645a7b6). Die Fixture erfordert jetzt
  ausdrücklich den vertrauenswürdigen lokalen Backend-Principal und
  prüft den tatsächlichen sicheren zweiphasigen Dashboard-Pending-ACK,
  statt eine unwirksame Mock-Konfiguration als Erfolg zu deklarieren.
  Weitere Gates: vollständiges CRUD/Restart im installierten Wheel,
  Fake-Provider Text/Bild ohne Kosten, E-Mail/CSV/SQLite und
  lokale Backup-/Restore-Sicherheit. Kein echter Plattformwrite.
  Der eigene Wheel-Receipt beschränkt den Backup-Scope ausdrücklich auf
  create-only-offline-not-a6.

**A6 weiterhin NO-GO:** keine unabhängige hostile-UID Write-/Media-
Completion-Autorität, kein vollständig verifizierter Multi-UID-Host-Cutover
mit alter FD-/MAP_SHARED-Revocation, kein reales A6-Backup/Restore.
Kein untrusted SQL-/Completion-/Clearance-RPC wird freigeschaltet.

## Nachtrag: verifizierter installierter Media-Write-/ACK-Pfad

- Drei neue reale lokale HTTP-Tests in
  `tests/test_dashboard_media_full_lifecycle_e2e.py` laufen unter
  Python 3.12/3.13 erfolgreich. Sie durchlaufen private PNG-Media-Staging-
  Handles (0600), Dashboard-Write-Proxy, SQL-Receipts, bestätigten
  `create-media`-Status 200, HTTP-Replay nach Serverneustart ohne zweiten
  synthetischen Plattformwrite und die zwei getrennten ACK-Phasen.
- Der synthetische Media-Dienst legt vor der finalen Medienbestätigung
  ein SQLite-Create-Checkpoint an und persistiert danach das exakte finale
  Operations-Receipt. Ein unbestätigter Media-Write (202) bleibt bei
  Restart und ACK blockiert. Ein nachträglich manipuliertes persistentes
  Media-Receipt blockiert ebenfalls beide Pfade, ohne den Pending-Fence
  zu löschen.
- Gezielt: Python 3.12/3.13 jeweils **3 PASS** (Jobs
  `grabowski-job-a5ee424b5657`,
  `grabowski-job-6d8c7144f830`).
  Das tatsächlich installierte Wheel wurde für beide Versionen in einer
  ausgehenden-netzlosen User-/Network-Namespace gesondert getestet:
  beide **PASS** (Jobs `grabowski-job-b250e6d43323`,
  `grabowski-job-267ccfda570e`). Der neue Receipt-Indikator lautet
  `installed_synthetic_media_write_end_to_end=true`; Wheel-Digest
  `bd52b5304b5b341983e918f331f51f9231af9d2e64e74572d73fa0388f6db46c`.
- Mit den drei Ergänzungen: Python-3.12-Vollsuite **1.828 PASS,
  21 SKIP, 2 fork()-Warnungen, 1.263 Subtests PASS**
  (`grabowski-job-d120133b13b6`). Die parallel gestartete 3.13-Suite
  zeigte 33 Fail durch absichtlich exklusive CDP-Kernel-Flocks
  (`target_busy`) gegen den zeitgleichen 3.12-Lauf. Eine neue
  **isolierte** 3.13-Vollsuite ergab anschließend **1.828 PASS,
  21 SKIP, 2 fork()-Warnungen, 1.263 Subtests PASS**
  (`grabowski-job-4e4a22329cb3`). Der Fehllauf wurde durch
  gleichzeitige CDP-Exklusiv-Tests auf demselben Kernel verursacht,
  nicht durch den neuen Medienpfad.

**Nicht als A6-Freigabe missverstehen:** Das lokale E2E-Szenario verwendet
eine synthetische Media-Receipt-Quelle und dieselbe vertrauenswürdige
SQLite-UID. Separat geschützte UID, verifizierte Completion-Evidenz,
bestehende FDs/MAP_SHARED-Revocation, WAL/SHM und echte A6-Host-/Restore-
Abnahme bleiben offen; keine generischen SQL-/Completion-/Clearance-RPCs
und keine Aufhebung der Container-Hard-Stops.

## Nachtrag: Native SQLite-Page-Reads des unabhängigen Owners, 23.10.2026

Ein neuer Angriffstest `test_storage_owner_paged_history_corruption.py`
reproduzierte auf dem aktuellen A6-Checkout **10 Fails** (2 Subtests
bestanden): Die typisierten Owner-seitigen paginierten Reaktions-/Inbox-
Reader und `classification_latest` setzten ungültiges natives SQLite-TEXT
(`observed_at`) mit `ValueError` um, akzeptierten teilweise native BLOB-
`source`-Felder stillschweigend und ließen fremde BLOB-`ad_id`-Zeilen
über `WHERE ad_id=?` unsichtbar werden.

Die vier Owner-Backing-Reader
`reaction_history_page`, `inbound_message_page`,
`inbound_message_ad_ids_page` und `latest_classification_bounded`
verwenden jetzt die **bestehenden indexierten, BINARY-geprüften
Native-/Blank-ID-Guards** in derselben SQLite-Lesetransaktion
und die vorhandenen streng typisierten Beobachtungsdecoder.
Korrupte persistierte Werte werden als `sqlite3.DatabaseError`
statt Python-`ValueError`/stiller Datenunterdrückung behandelt.
Die begrenzten Page-Kursoren, eingrenzenden SQL-Selektoren, die
Connection-Schließung vor Resultatdekodierung sowie das
`max_records`-API-Limit bleiben erhalten. Keine Completion-,
SQL-, Arbitrary-Clearance- oder andere Owner-RPC wurden geöffnet.

**RED:** `grabowski-job-0e869aa5cf37`, 10 Fails/2 PASS.
**GREEN:** Unter Python 3.12 und 3.13 jeweils 131 PASS,
189 Subtests PASS (Jobs `grabowski-job-507878b47cac` und
`grabowski-job-34b1a77d6aa6`).
Der frisch für diese Quelländerung gebaute Wheel-Digest
`a8bc6c60052b4aded7513405cc82e53236e9d0bb351af02eea2eef25390f3358`
hat auf beiden Versionen die vollständige getrennt installierte,
netzisolierte Testreise einschließlich der drei neuen Owner-
Korruptionsregressionen bestanden (Jobs `grabowski-job-7215eb2d17a0`,
`grabowski-job-4a5329ef6e9d`), mit
`installed_owner_paged_history_fail_closed=true`.

**No-Go bleibt unverändert:** Der indexed SQLite-Proof sagt nichts über
die unabhängige feindliche Dienst-UID, bereits existierende
FD-/MAP_SHARED-Aliase, WAL-Rollback oder den fehlenden vertrauenswürdigen
Browser-/Write-Completion-Akteur. Die Multi-UID-/Host-Cutover-Hard-Stops
bleiben aktiv.

**Abschließende vollständige Quellenmatrix nach diesem Owner-P2-Fix:**
Python **3.12 und 3.13** jeweils **1.831 PASS, 21 SKIP,
1.272 Subtests PASS, 2 `os.fork()`-DeprecationWarnings**, ohne
Fehlschläge. Die beiden Gesamtsuiten liefen absichtlich nacheinander,
weil CDP-Zielreservierungen auf demselben Host exklusiv bleiben müssen.
Revisionsgebundene erfolgreich finalisierte Jobs:
`grabowski-job-07091f2357dc` (3.12) und
`grabowski-job-056a45cb0ef8` (3.13). Die 21 SKIPs sind keine
hostile-UID-/MAP_SHARED-/Restore-Abnahme. WIP-PR #199 bleibt Draft.
