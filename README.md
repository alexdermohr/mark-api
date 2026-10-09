# mark-api

Öffentliche Projektdokumentation für die mit Mark besprochene Kleinanzeigen-Agent-/API-Integration.

## Status

**Core, Dashboard und Analytics implementiert / PrivateWebWriter + Runtime-Smoke real belegt / Product Launcher mit Default-on Write Composition, lokalem Reaction-Import und Same-Origin Dashboard Write UX implementiert / Write-API-Auth-/Idempotency-Runtime crash-sicher gehärtet** — Stand: 05.10.2026.

Mark hat nach dem Telefonat die gewünschte Funktionalität schriftlich konkretisiert. Der MVP-Fokus liegt auf Anzeigenverwaltung, Synchronisation, Verkäufermetriken, Inbox/Interessenten, Dashboard und datenbasierter Auswertung. Externe Text-/Bildgenerierung war ursprünglich Teil des Wunsches, ist seit 24.09.2026 aber nicht mehr MVP-priorisiert.

## Technisch belegt

- Python-3.12+-Core mit diskriminierten Read-Ergebnissen, Safe-Write-Orchestrierung und SQLite-Snapshot-Persistenz.
- `ManagementReadAdapter` für den autoritativen Besitzerbestand sowie Verkäuferstatus, Views, Merker und Replies.
- `MonkrelMobileApiAdapter` als technisch belegter PoC-Adapter für eigene Anzeigen, ID-gebundene Pause/Aktivierung, Delete und Inbox/Conversation-Zuordnung; nach Abschluss von Issue #2 ist er kein Produktpfad für private Accounts.
- `BrowserBotAdapter` bleibt als historisch belegter Fremdprozess-PoC für Sync und in-place Inhaltsupdate im Repository; er wird nicht automatisch zum Produkt-Backend.
- `PrivateWebContentWriter` bildet den davon getrennten privaten Writer-Contract für die normale Kleinanzeigen-Weboberfläche. `CdpPrivateWebPage`, `CdpCookieProvider` und `CdpPrivateWebOwnerReader` implementieren den gehärteten loopback-only Driver; der gegatete Real-Smoke auf genau einer eigenen Anzeige hat einen Content-Write mit unabhängigem Owner/Web-Post-Read bestätigt.
- `PrivateWebCreateWriter` bildet den ersten engen Create-Vertrag für private Konten: expliziter Kategoriepfad, `OFFER + FIXED`, Titel/Beschreibung/Festpreis, keine Medien, kein Draft, kein Buy-Now, keine Marketing- oder Adressfreigabe. Der aktuelle Create-Flow wurde ausschließlich read-only kartiert; ein Live-Publish wurde in diesem Slice nicht ausgeführt.
- `PrivateWebCreateMediaWriter` ergänzt davon getrennt einen lokalen Media-Publish-Contract: stabile private Kopien aus validierten Datei-Deskriptoren, exakter `FileList`-Readback, erneute Bindung desselben CDP-File-Input-Handles und höchstens ein browser-level Publish-Versuch. Der Browser-Pfad ist nicht direkt an `MarkService` gekoppelt. Für den getrennten Write-API-Port erzeugt `POST /api/write/media/stage` aus explizit hochgeladenen JPEG-/PNG-/WebP-Bildbytes runtime-eigene private `0600`-Kopien und zufällige opake `media_refs`; der normale High-Level-Builder benötigt dafür keine caller-supplied Pfad-Bindings. `PrivateWebMediaCreateService` stabilisiert die ausgewählten Handle-Quellen vor dem ersten Owner-Read erneut in private Kopien, verbraucht die Handles und führt genau diese stabilisierten Quellen über `SafeWriteOrchestrator` und die bestehende `PrivateWebMediaCreateRuntime` aus. Die HTTP-Schicht bleibt vollständig pfadfrei. Der normale High-Level-Builder komponiert bei `CREATE_MEDIA` zusätzlich einen read-only `PrivateWebPublicMediaPersistenceVerifier`: er bindet die bestätigte Anzeigen-ID an die normale öffentliche VIP-Detailseite, isoliert ausschließlich deren eigene Galerie, lädt nur streng validierte `img.kleinanzeigen.de`-Galeriebilder und verlangt gleiche Bildanzahl plus vollständiges reihenfolgeerhaltendes one-to-one Inhaltsmatching gegen genau die stabilisierten lokalen Quellen. Bytegleichheit wird nicht verlangt, weil Kleinanzeigen Galerievarianten serverseitig skaliert/rekodiert; Host-/ID-/Galerie-/Aspect-/Pixelgrenzen bleiben fail-closed; EXIF-Orientierung wird vor Dimensions- und Pixelvergleich normalisiert. Dieser Slice führt keinen realen Kleinanzeigen-Publish aus.
- `MarkService` als Application-Layer über den Capability-Adaptern.
- Loopback-Dashboard/API mit read-only Daten-/Analytics-Surface; im normalen Product Launcher zusätzlich eine eng allowlistete Same-Origin Write UX, die ausschließlich an die bestehende separate Write API delegiert.
- Der Ein-Anzeigen-Realtest vom 24.09.2026 belegt Sync, stabiles in-place Update, Pause/Aktivierung, Verkäufermetriken, positiven Inbox/adId-Fall und Delete mit unabhängigen Besitzerlisten-Readbacks.
- Am 25.09.2026 wurde der aktuell eingeloggte eigene Account read-only durch `ManagementReadAdapter -> EnrichedOwnerReader -> MarkService -> SnapshotStore` geführt: erfolgreicher leerer Besitzerbestand (`success_empty`), keine Plattformmutation.
- Die generischen Core-/Runtime-Write-Gates bleiben fail-safe standardmäßig deaktiviert. Der normale Produktstart öffnet die vorhandenen Gates jetzt ausdrücklich und automatisch für seine loopback-only Write-Komposition. Der mediafreie Create/Publish-Vertrag, der getrennte media-aware Browser-Publish-Primitive und die read-only öffentliche Media-Persistenzverifikation sind lokal implementiert und regressionsgetestet; ein kontrollierter Live-Publish-Smoke bleibt ein separates Abnahmeinstrument.

## Metrikherkunft und Datenfrische

Die read-only Endpunkte /api/ads und /api/analytics/ads?metric=... liefern zusätzlich zum unveränderten Zählerwert ein metric_evidence-Objekt. Für views, watch_count und reply_count kennzeichnet es jeweils den **tatsächlich wertliefernden** Snapshot mit observed_at, source und last_known. Ein Zähler aus einem älteren Snapshot behält seinen alten Zeitstempel auch dann, wenn eine neuere Status-Beobachtung vorliegt. Unbekannte Werte besitzen keine erfundene Provenienz; eine beobachtete 0 bleibt ein belegter Wert.

Analytics-Reaktionsmetriken verwenden Zeit und Quelle ihres neuesten tatsächlich ausgewählten Reaction-Snapshots. Aggregierte E-Mail-Metriken werden ausdrücklich der Datenquelle inbound_message_events zugeordnet; ihre Beobachtungszeit ist die jüngste importierte Einzelmeldung, nicht der Zeitpunkt einer unabhängig bestätigten Plattform-Bestandsaufnahme. Da der Last-known-Vergleich nur für Anzeigen-Snapshotfelder sinnvoll ist, lautet last_known für unabhängige Reaction-/E-Mail-Datengrundlagen null.

Dashboard-Tabellen zeigen Zeit, Quelle und aus der Betrachtungszeit berechnetes Alter pro Metrik. **Diese Zeiten sind keine Sync-Erfolgsnachweise.** Das von Anzeigen-Snapshots getrennte SQLite-`sync_attempts`-Journal wird bei `MarkService.refresh_inventory()` und vor dem ersten Browser-Runtime-Aufbau des normalen Product Launchers eröffnet. Es speichert Startzeit, Quelle und den nachgewiesenen Ausgang `success_nonempty`, `success_empty` oder `failed` mit einer begrenzten Fehlerklasse; `in_progress` ohne Abschluss bleibt nach Unterbrechung ausdrücklich **ungeklärt**. Fehler speichern keine rohen HTTP-/Browsertexte, Zugangsdaten oder Anzeigennachrichten. Bei erfolgreichem Eigentümer-Read werden Snapshot-Persistenz und Journalabschluss in derselben SQLite-Transaktion bestätigt. Ein fehlgeschlagener Read hinterlässt die historischen Anzeigen-/Zählerwerte unverändert und kann den letzten erfolgreichen Sync nicht überschreiben.

`GET /api/sync/status` und das additive Feld `sync_status` in `GET /api/summary` zeigen unabhängig voneinander den jüngsten Sync-Versuch und den zuletzt **erfolgreich abgeschlossenen** Eigentümer-Read einschließlich Zeitpunkt, Quelle und Ergebnis. Das Dashboard zeigt deren Alter separat vom Alter jedes einzelnen Zählers. Ein bestätigter Leerbestand ist ein Erfolg mit 0 Einträgen, unbekannte oder ausstehende Versuche nicht. Historische Snapshots und direkte Offline-Imports ohne eröffnete Journal-ID werden **nicht** als erfolgreiche Synchronisation umgedeutet. Diese APIs lösen selbst keinen Plattformzugriff aus; die bisherige Launcher-Synchronisation bleibt ein einmaliger Start-Read und noch kein periodischer Scheduler.

Die additive Tabelle erhält den SQLite-`user_version`-Marker 1 erst nach vollständiger Validierung der vorhandenen kritischen Recovery-/Idempotenz-Tabellen. Alte vollständige Mark-Stores (vor dem Journal, Marker 0) werden beim Start in-place additiv erweitert, ohne Daten und Write-Fences umzudeuten. Ist die Journal-Tabelle bei einer schon versionierten Datenbank verloren gegangen oder beschädigt, blockiert der normale Start fail-closed, statt die verlorene Sync-Historie durch ein leeres Journal zu ersetzen. Backup und Restore müssen künftig SQLite-Datenbank **einschließlich** `sync_attempts` und Versionsmarker bewahren; `/readyz` prüft den Zustand.

Für den angereicherten Besitzerbestand bleibt die allgemeine Anzeige-/Inhaltsquelle zusammengesetzt (beispielsweise management+mobile). Die tatsächliche Zählerquelle wird separat und unverwechselbar in `ad_snapshots.metric_source` gespeichert. Bei bereits vollständigen SQLite-Stores wird diese optionale Spalte erst **nach der Prüfung sämtlicher vorhandener Recovery-Tabellen und Idempotenz-Fences** additiv ergänzt; der Quelltext `source` bleibt stets unverändert, auch bei beliebigen Präfixen oder JSON-ähnlichen Provider-Namen. Fehlende Recovery-Fences und unerkannte historische Tabellenschemata werden weiterhin nicht automatisch repariert. Fehlt bei älteren oder direkt gespeicherten Snapshots `metric_source`, wird in `metric_evidence.source` ausschließlich der unveränderte Quelltext `source` angezeigt. Gerade bei zusammengesetzten Bezeichnungen ist damit **nicht** unabhängig belegt, welcher Teil die Zähler geliefert hat. Weder Pluszeichen noch Präfixe werden als Trennzeichen oder Herkunftsbeweise interpretiert; eine Management-Teilquelle wird nicht geraten.

## Isolierter Produktbetrieb unter eigener Unix-UID

Für den root-verwalteten Linux-Produktbetrieb gibt es
[`docs/mark-api.service`](docs/mark-api.service),
[`docs/mark-api-backup@.service`](docs/mark-api-backup@.service),
[`docs/mark-api-bootstrap.sh`](docs/mark-api-bootstrap.sh),
[`docs/mark-api-preflight.py`](docs/mark-api-preflight.py) und
[`docs/mark-api.sysusers.conf`](docs/mark-api.sysusers.conf).
Sie beschreiben einen nicht interaktiv verwendbaren, dedizierten
`mark-api`-Unix-Account mit privaten SQLite- und Runtime-Verzeichnissen;
ein root-verwalteter nativer Shell-Check prüft zuerst die Standardbibliothek
**des Bootstrap-OS-Pythons selbst**. Erst danach startet der Python-Preflight
unter `-I -S` und prüft alle First-/Third-Party-Dateien des nicht-editierbaren,
root-owned Venvs. Der normale Launcher aktiviert weiterhin alle vorhandenen produktseitigen
Write-Capabilities **default-on**. Ausführbare Installationsanweisungen,
Sicherheits- und Beweisgrenzen stehen im
[`docs/operations-runbook.md`](docs/operations-runbook.md).
Die Repository-Vorlagen wurden nicht als laufender Dienst installiert:
Insbesondere ist eine echte OS-Identitätstrennung gegen andere
gleichberechtigte lokale Prozesse erst nach Installation und unabhängiger
Liveprüfung belegt, nicht durch eine statische Unit-Datei. Die
Standard-Ports 8765/8766 können auf dem Host belegt sein; die Vorlage
verwendet daher 8875/8876 (vor Start erneut auf Kollision prüfen).

## Product Launcher

Der installierte Startpfad für den aktuellen Produktstand ist `mark-api-launch`. Er verwendet einen **bereits laufenden, vom Nutzer bereits authentifizierten** lokalen Chrome-/Chromium-Prozess mit loopback-CDP; der Launcher startet keinen Browser und automatisiert weder Login noch MFA/CAPTCHA.

Installation mit dem benötigten Private-Web-Extra:

```bash
python -m pip install -e '.[private-web]'
```

**SQLite-Erstinitialisierung:** Bei der ersten Einrichtung muss die Datenbank bewusst angelegt werden. Nur hierfür einmal `--init-db` ergänzen:

```bash
mark-api-launch --init-db --db /pfad/mark.sqlite --cdp-port 9222
```

Danach normal **ohne** `--init-db` starten. Ein vertippter Pfad darf sonst niemals stillschweigend einen leeren SQLite-Store erzeugen. Den Bootstrap-Schalter nicht dauerhaft in Dienst- oder Autostart-Konfigurationen belassen; er ist keine zusätzliche Freigabe für Plattformwrites. Beim regulären Produktstart werden vorhandene Datenbank, Integrität und Mark-Schema geprüft. Auch `mark-api-dashboard` und `mark-api-import-mail` unterstützen `--init-db` ausschließlich für eine ausdrückliche Neueinrichtung; `mark-api-classify` benötigt stets einen bestehenden Store.

**Betriebs-Readiness:** `GET /healthz` zeigt nur HTTP-Liveness. `GET /readyz` prüft die tatsächliche SQLite-Erreichbarkeit, Integrität und kritische Tabellen für Owner-Daten und Write-Recovery (HTTP 200 `{"status":"ready"}` oder HTTP 503 `{"status":"unavailable","error":"database_unavailable"}`). Bei Datenbankverlust oder Beschädigung antworten datenlesende Dashboard-APIs mit HTTP 503 `{"error":"database_unavailable"}` statt die Verbindung abzubrechen. Nach dem Start kann eine fehlende Datenbankdatei nicht automatisch neu entstehen. Erst eine gültige Wiederherstellung gibt die Datenversorgung frei; unsichere Write-Idempotenz-Fences berechtigen niemals zu einem blinden Plattform-Retry.

Beispielstart:

```bash
mark-api-launch \
  --db /pfad/mark.sqlite \
  --cdp-port 9222 \
  --email /pfad/zu/nachricht-1.eml \
  --email /pfad/zu/nachricht-2.eml
```

Beim Start eröffnet der Launcher zunächst einen persistenten Sync-Versuch und führt danach genau **einen frischen Owner-Inventory-Read** aus. Auch Browser-Runtime-Startfehler, ungültige Daten und klassifizierte Read-Fehler werden getrennt von historischen Snapshotwerten sichtbar. Nur ein nachweislich erfolgreicher Read wird in die konfigurierte SQLite-Datenbank übernommen; zuvor bekannte, nun fehlende Anzeigen werden dabei mit der bestehenden `ABSENT`-Transition persistiert. Optional kann der Nutzer mit wiederholtem `--email FILE` lokale rohe Kleinanzeigen-RFC822-/`.eml`-Nachrichtenkopien ausdrücklich in denselben Store importieren. Dieser Reaction-Import läuft nach dem erfolgreichen Inventory-Sync und **vor** der Konstruktion von Dashboard und Write API. Der vorhandene Import bleibt batch-atomar und idempotent; scheitert ein ausdrücklich angeforderter Mail-Batch, startet keine der beiden HTTP-Surfaces. Bereits bestätigte Inventory-Beobachtungen bleiben wie bei anderen späteren Startup-Fehlern persistiert.

Der Launcher sucht weder ein Postfach noch ein Verzeichnis ab, beobachtet keine Dateien periodisch und ruft keine private/mobile Messaging-API auf. E-Mail-Evidenz erzeugt keine Besitzeridentität und keinen `AdSnapshot`: eine nur aus Mail bekannte Anzeigen-ID bleibt analytische Reaction-Evidenz. Sichtbar werden ausschließlich die bereits source-expliziten Größen `email_conversation_count` und `email_inbound_message_count`; `unique_buyer_count` wird daraus nicht erfunden und `reaction_metric` bleibt ohne ausdrückliche Nutzerwahl ungesetzt.

Erst danach startet die bestehende Mark Write API auf `127.0.0.1:8766` und anschließend das Dashboard auf `127.0.0.1:8765` (jeweils oder auf dem explizit gewählten lokalen Port). Diese Reihenfolge ist absichtlich fest: das Dashboard erhält im Product Launcher ausschließlich einen eng gebundenen Proxy auf die **tatsächliche** lokale Write-API-Adresse. Der Launcher erzeugt weiterhin pro Prozess einen neuen Write-API-Bearer. Zusätzlich erzeugt er einen getrennten per-process Dashboard-Write-Token und gibt eine Dashboard-URL mit diesem Token ausschließlich im URL-Fragment `#write_token=...` aus. Browser-JavaScript übernimmt ihn in `sessionStorage` und entfernt das Fragment sofort aus der sichtbaren URL; der Backend-Bearer wird nicht an Browser-JavaScript weitergegeben. Noch offene Plattformrequests werden dagegen vor der Proxy-Weiterleitung serverseitig in derselben SQLite-Datenbank als Pending-Recovery-Records gebunden; weder Dashboard-Token noch Backend-Bearer werden dort gespeichert. Die direkte Write-API-URL und ihr Bearer bleiben für den fortgeschrittenen lokalen Operator verfügbar und sind weiterhin als lokales Write-Secret-Material zu behandeln.

Die Product-Launcher-Oberfläche kann damit Create, optionales lokales Media-Staging/Media-Create, Titel/Beschreibung, Pause, Aktivierung und Delete direkt im Dashboard auslösen. Das Dashboard implementiert dabei **keine zweite Write-Engine**: es akzeptiert nur dieselben vorhandenen `/api/write/...`-Routen sowie das lokale, idempotente Cleanup bereits ausgestellter Media-Refs, verlangt den per-process Dashboard-Token, einen Same-Origin-`Origin` und den CSRF-Marker `X-Mark-Dashboard-Write: 1`, leitet nur eng ausgewählte Header/Bodies an die separate Write API weiter und injiziert dort serverseitig den bestehenden Bearer. `OPTIONS`/CORS wird absichtlich nicht geöffnet. Der standalone Einstieg `mark-api-dashboard` konstruiert keinen Write-Proxy und bleibt vollständig GET-only/read-only.

Für jede Plattformaktion erzeugt die Browser-UX genau einen Idempotency-Key. Vor der Weiterleitung an die Write API legt der Dashboard-Proxy den normalisierten Request samt Key atomar als Pending-Record in derselben SQLite-Datei an. Kann dieser Claim nicht sicher persistiert werden, wird der Plattformrequest nicht weitergeleitet. Bei einem Transportfehler oder sonst unbekanntem Proxy-Ausgang erfolgt **kein automatischer Retry**; der ursprüngliche Request bleibt serverseitig an denselben Key gebunden und eine manuelle Wiederholung verwendet exakt denselben Pfad und Payload. Ein zweiter Tab oder ein neuer Key für dieselbe Create-/Anzeigen-Ressource wird durch den persistenten Fence abgewiesen. Nach Tab-Schließen oder Dashboard-Neustart lädt die UI die noch offenen Records über eine authentifizierte Same-Origin-Surface erneut; ein rotierter per-process Dashboard-Token ändert den gespeicherten Plattform-Key nicht. Ein Backend-Response löscht den Pending-Record nicht schon vor der Browserzustellung: erst nachdem der Browser einen gebundenen terminalen Response oder einen sicheren Clientfehler verarbeitet hat, bestätigt er lokal exakt Scope und Idempotency-Key. Gehen Response oder dieser ACK verloren, bleibt der Fence erhalten und derselbe persistierte Request/Key kann sicher erneut gelesen beziehungsweise replayt werden. Media-Staging bleibt ein lokaler, nicht-plattformschreibender Schritt; vor dem Staging wird der ausgewählte Batch gegen Anzahl und Größenlimits geprüft. Scheitert ein späteres Einzel-Staging, werden bereits bekannte opake Refs über die capability-gebundene lokale Discard-Primitive wieder freigegeben. Geht die Antwort eines bereits lokal erfolgreichen Stage verloren und bleibt dessen Ref deshalb unbekannt, läuft der ungenutzte Handle nach 15 Minuten ab und wird vor dem nächsten Stage/Resolve lazy aus dem lokalen Kontingent entfernt. Erst der nachgelagerte Media-Create besitzt den persistenten Plattform-Idempotency-Claim. Persistente Idempotenz, ID-/Ownership-Bindung, Confirmation, TOCTOU-Prüfung, Post-Readback und `platform_retry_authorized=false` bleiben ausschließlich Verantwortung der bestehenden Write API und des darunterliegenden Core. Ein Media-Submit-`UNKNOWN` kann beim Shutdown weiterhin ausschließlich über die bestehende observation-only Reconciliation geklärt werden; ein zweiter Plattform-Submit wird dadurch nicht autorisiert.

CDP, Dashboard und Write API bleiben auf Loopback begrenzt. Ein zusätzlicher Write-Opt-in ist im normalen Produktpfad nicht erforderlich. Schlägt Initial-Read oder expliziter Mail-Import fehl, startet keine HTTP-Surface. Kann die Write API nicht starten, wird das Dashboard nicht konstruiert; scheitert die spätere Dashboard-Konstruktion, wird die bereits gestartete Write-Runtime geschlossen. Der Launcher führt weiterhin **keine periodische Synchronisation** aus; Freshness/Recovery folgen separat.

## Backup und Wiederanlauf des Gesamtprodukts (A6)

Das installierte Tool **mark-api-backup** sichert eine bestehende, vollständig
validierte SQLite-Produktdatenbank einschließlich WAL, Sync-Journal und
persistenter Write-Idempotenz-/Dashboard-Pending-Fences. Es erzeugt
ausschließlich einen neuen, geschützten Backup-Pfad und überschreibt
keine vorhandenen Daten. Kein Plattformrequest wird ausgelöst.

~~~bash
mark-api-backup --db /sicherer/pfad/mark.sqlite \
  --backup /sicherer/backup-ordner/mark-2026-10-09.sqlite
~~~

Backup-Receipt, aktuelle Start-/Readiness-Prüfungen, sichere Offline-Restores
**nur auf neue Pfade**, Reconciliation ungeklärter Plattformwrites sowie die
verifizierten und weiterhin offenen Gesamtprodukt-Gates stehen im
[Betriebs- und Wiederanlaufhandbuch](docs/operations-runbook.md).
Eine lokale Backup-/Restore-Abnahme ist keine Berechtigung zur
Kleinanzeigen-Plattformautomation und kein Ersatz für einen echten,
autorisierten Create/Update/Delete/Media-End-to-End-Test. Die normale
Write-Komposition des Product Launchers bleibt default-on; Authentisierung,
Confirmation, Ownership und No-Blind-Retry bleiben unverändert.

## Historische SQLite-Datenbanken sicher übernehmen

Eine ältere Mark-Datenbank kann 3, 4, 5, 6, 7 oder 8 der heute 9 Tabellen enthalten. Beim normalen Start und auch mit `--init-db` werden fehlende Tabellen **nicht** automatisch nachgebaut: Die alten Dateien besitzen keinen verlässlichen Schema-Versionsmarker. Eine historisch noch nicht angelegte Recovery-Tabelle lässt sich deshalb nicht sicher von einem nach einem Plattformwrite verlorenen Idempotenz-Fence unterscheiden.

Nach unabhängiger Klärung der **alten** Plattformwrites ist ein einmaliger, vollständig lokaler Offline-Import möglich:

1. Alle Mark-Prozesse und anderen SQLite-Benutzer der Originaldatei beenden. Vorher offene/unklare Plattformoperationen anhand vorhandener Owner- und Recovery-Evidenz abschließen. Bei Ungewissheit **nicht** migrieren; eine vertrauenswürdige Sicherung wiederherstellen oder den Einzelfall klären.
2. Einmalige Recovery-Bestätigung nur dann erteilen, wenn keine ungeklärten alten Plattformwrites mehr bestehen. Neue und getrennte Backup- und Zieldateien angeben:

```bash
mark-api-migrate-legacy \
  --db /pfad/alter-mark.sqlite \
  --backup /pfad/archiv/alter-mark.backup.sqlite \
  --output-db /pfad/neuer-mark.sqlite \
  --confirm-no-unresolved-writes
```

3. Die ausgegebene Schema-Stufe, Tabellen-/Zeilenzahlen und den Backup-SHA-256 prüfen und ausschließlich die neue Datenbank beim normalen Produktstart verwenden:

```bash
mark-api-launch --db /pfad/neuer-mark.sqlite --cdp-port 9222
```

Der Import erkennt nur belegte historische Schema-Stufen, prüft Integrität, Spalten und Recovery-Schlüssel und verweigert offene Write-API-Requests, Create-Checkpoints sowie mehrdeutige Write-Receipts. Bereits abgeschlossene Idempotenzbelege werden mit ihren Schlüsseln und Antworten bewahrt. SQLite erstellt eine eigenständige private Sicherung; die historischen Datensätze samt IDs werden in einer neuen Datenbank übernommen und deren Readiness geprüft. Die alte Datei sowie bestehende Backup- oder Zielpfade werden niemals überschrieben. Während des Imports hält SQLite eine `BEGIN IMMEDIATE`-Schreibsperre auf der Quelle bis einschließlich der dauerhaften Zielveröffentlichung (auch im WAL-Modus); dazu muss die Quelldatei für SQLite im Read-Write-Modus geöffnet werden können, obwohl die Migration keine Quelldaten verändert. Die neue Sicherung samt Verzeichniseintrag wird vor dem Zieldateilink synchronisiert; danach auch der Zielverzeichniseintrag. Die Sperre ersetzt **nicht** das vorherige Beenden fremder Prozesse, die Dateien direkt umbenennen oder austauschen können. Bei Fehlschlag kann ein Backup und bei spätem Sync-Fehler sogar ein bereits veröffentlichter Zielpfad zurückbleiben; vor jedem weiteren Versuch den tatsächlichen Dateistand prüfen und niemals blind wiederholen.

**Keine Plattformaktion wird während des Imports ausgelöst oder automatisch wiederholt.** Die historische Tabellenform ist ausdrücklich *kein* Beweis für das Ausbleiben früherer Writes; die Bestätigung der geklärten Recovery-Lage ist eine Sicherheitsgrenze. Für die reguläre, vollständig migrierte Produktdatenbank bleiben die vorgesehenen Write-Funktionen ohne zusätzlichen Opt-in default-on.
## Lokale Klassifikationspflege

Die Analytics-Gruppierung verwendet explizite Labels für `image_type`, `city`, `text_type` und `title_type`. Die Klassifikationspflege bleibt bewusst eine lokale, append-only CLI-Funktion; die neue Product-Launcher Write UX betrifft ausschließlich die bereits vorhandenen Anzeigen-Write-Routen und erteilt Email-only-IDs keine Write-Autorität. Labels werden über den separaten CLI-Entrypoint gepflegt:

```bash
mark-api-classify \
  --db /pfad/zu/mark.sqlite \
  --ad-id 1234567890 \
  --city Dresden \
  --image-type overview
```

Nicht angegebene Dimensionen werden aus der letzten Klassifikation übernommen. Ein Label wird nur durch eine explizite Clear-Aktion entfernt, zum Beispiel:

```bash
mark-api-classify \
  --db /pfad/zu/mark.sqlite \
