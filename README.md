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

Beim Start führt der Launcher genau **einen frischen Owner-Inventory-Read** aus. Nur ein nachweislich erfolgreicher Read wird in die konfigurierte SQLite-Datenbank übernommen; zuvor bekannte, nun fehlende Anzeigen werden dabei mit der bestehenden `ABSENT`-Transition persistiert. Optional kann der Nutzer mit wiederholtem `--email FILE` lokale rohe Kleinanzeigen-RFC822-/`.eml`-Nachrichtenkopien ausdrücklich in denselben Store importieren. Dieser Reaction-Import läuft nach dem erfolgreichen Inventory-Sync und **vor** der Konstruktion von Dashboard und Write API. Der vorhandene Import bleibt batch-atomar und idempotent; scheitert ein ausdrücklich angeforderter Mail-Batch, startet keine der beiden HTTP-Surfaces. Bereits bestätigte Inventory-Beobachtungen bleiben wie bei anderen späteren Startup-Fehlern persistiert.

Der Launcher sucht weder ein Postfach noch ein Verzeichnis ab, beobachtet keine Dateien periodisch und ruft keine private/mobile Messaging-API auf. E-Mail-Evidenz erzeugt keine Besitzeridentität und keinen `AdSnapshot`: eine nur aus Mail bekannte Anzeigen-ID bleibt analytische Reaction-Evidenz. Sichtbar werden ausschließlich die bereits source-expliziten Größen `email_conversation_count` und `email_inbound_message_count`; `unique_buyer_count` wird daraus nicht erfunden und `reaction_metric` bleibt ohne ausdrückliche Nutzerwahl ungesetzt.

Erst danach startet die bestehende Mark Write API auf `127.0.0.1:8766` und anschließend das Dashboard auf `127.0.0.1:8765` (jeweils oder auf dem explizit gewählten lokalen Port). Diese Reihenfolge ist absichtlich fest: das Dashboard erhält im Product Launcher ausschließlich einen eng gebundenen Proxy auf die **tatsächliche** lokale Write-API-Adresse. Der Launcher erzeugt weiterhin pro Prozess einen neuen Write-API-Bearer. Zusätzlich erzeugt er einen getrennten per-process Dashboard-Write-Token und gibt eine Dashboard-URL mit diesem Token ausschließlich im URL-Fragment `#write_token=...` aus. Browser-JavaScript übernimmt ihn in `sessionStorage` und entfernt das Fragment sofort aus der sichtbaren URL; der Backend-Bearer wird nicht an Browser-JavaScript weitergegeben. Noch offene Plattformrequests werden dagegen vor der Proxy-Weiterleitung serverseitig in derselben SQLite-Datenbank als Pending-Recovery-Records gebunden; weder Dashboard-Token noch Backend-Bearer werden dort gespeichert. Die direkte Write-API-URL und ihr Bearer bleiben für den fortgeschrittenen lokalen Operator verfügbar und sind weiterhin als lokales Write-Secret-Material zu behandeln.

Die Product-Launcher-Oberfläche kann damit Create, optionales lokales Media-Staging/Media-Create, Titel/Beschreibung, Pause, Aktivierung und Delete direkt im Dashboard auslösen. Das Dashboard implementiert dabei **keine zweite Write-Engine**: es akzeptiert nur dieselben vorhandenen `/api/write/...`-Routen sowie das lokale, idempotente Cleanup bereits ausgestellter Media-Refs, verlangt den per-process Dashboard-Token, einen Same-Origin-`Origin` und den CSRF-Marker `X-Mark-Dashboard-Write: 1`, leitet nur eng ausgewählte Header/Bodies an die separate Write API weiter und injiziert dort serverseitig den bestehenden Bearer. `OPTIONS`/CORS wird absichtlich nicht geöffnet. Der standalone Einstieg `mark-api-dashboard` konstruiert keinen Write-Proxy und bleibt vollständig GET-only/read-only.

Für jede Plattformaktion erzeugt die Browser-UX genau einen Idempotency-Key. Vor der Weiterleitung an die Write API legt der Dashboard-Proxy den normalisierten Request samt Key atomar als Pending-Record in derselben SQLite-Datei an. Kann dieser Claim nicht sicher persistiert werden, wird der Plattformrequest nicht weitergeleitet. Bei einem Transportfehler oder sonst unbekanntem Proxy-Ausgang erfolgt **kein automatischer Retry**; der ursprüngliche Request bleibt serverseitig an denselben Key gebunden und eine manuelle Wiederholung verwendet exakt denselben Pfad und Payload. Ein zweiter Tab oder ein neuer Key für dieselbe Create-/Anzeigen-Ressource wird durch den persistenten Fence abgewiesen. Nach Tab-Schließen oder Dashboard-Neustart lädt die UI die noch offenen Records über eine authentifizierte Same-Origin-Surface erneut; ein rotierter per-process Dashboard-Token ändert den gespeicherten Plattform-Key nicht. Ein Backend-Response löscht den Pending-Record nicht schon vor der Browserzustellung: erst nachdem der Browser einen gebundenen terminalen Response oder einen sicheren Clientfehler verarbeitet hat, bestätigt er lokal exakt Scope und Idempotency-Key. Gehen Response oder dieser ACK verloren, bleibt der Fence erhalten und derselbe persistierte Request/Key kann sicher erneut gelesen beziehungsweise replayt werden. Media-Staging bleibt ein lokaler, nicht-plattformschreibender Schritt; vor dem Staging wird der ausgewählte Batch gegen Anzahl und Größenlimits geprüft. Scheitert ein späteres Einzel-Staging, werden bereits bekannte opake Refs über die capability-gebundene lokale Discard-Primitive wieder freigegeben. Geht die Antwort eines bereits lokal erfolgreichen Stage verloren und bleibt dessen Ref deshalb unbekannt, läuft der ungenutzte Handle nach 15 Minuten ab und wird vor dem nächsten Stage/Resolve lazy aus dem lokalen Kontingent entfernt. Erst der nachgelagerte Media-Create besitzt den persistenten Plattform-Idempotency-Claim. Persistente Idempotenz, ID-/Ownership-Bindung, Confirmation, TOCTOU-Prüfung, Post-Readback und `platform_retry_authorized=false` bleiben ausschließlich Verantwortung der bestehenden Write API und des darunterliegenden Core. Ein Media-Submit-`UNKNOWN` kann beim Shutdown weiterhin ausschließlich über die bestehende observation-only Reconciliation geklärt werden; ein zweiter Plattform-Submit wird dadurch nicht autorisiert.

CDP, Dashboard und Write API bleiben auf Loopback begrenzt. Ein zusätzlicher Write-Opt-in ist im normalen Produktpfad nicht erforderlich. Schlägt Initial-Read oder expliziter Mail-Import fehl, startet keine HTTP-Surface. Kann die Write API nicht starten, wird das Dashboard nicht konstruiert; scheitert die spätere Dashboard-Konstruktion, wird die bereits gestartete Write-Runtime geschlossen. Der Launcher führt weiterhin **keine periodische Synchronisation** aus; Freshness/Recovery folgen separat.

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

Der Import erkennt nur belegte historische Schema-Stufen, prüft Integrität, Spalten und Recovery-Schlüssel und verweigert offene Write-API-Requests, Create-Checkpoints sowie mehrdeutige Write-Receipts. Bereits abgeschlossene Idempotenzbelege werden mit ihren Schlüsseln und Antworten bewahrt. SQLite erstellt eine eigenständige private Sicherung; die historischen Datensätze samt IDs werden in einer neuen Datenbank übernommen und deren Readiness geprüft. Die alte Datei sowie bestehende Backup- oder Zielpfade werden niemals überschrieben. Bei Fehlschlag kann ein Backup zurückbleiben; vor jedem weiteren Versuch den tatsächlichen Dateistand prüfen.

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
  --ad-id 1234567890 \
  --clear text-type
```

Der CLI akzeptiert nur lokal bereits belegte analytische Anzeigen-IDs: entweder aus einem Owner-/Bestandssnapshot oder aus einem importierten Kleinanzeigen-E-Mail-Event. Eine nur per E-Mail belegte ID darf damit klassifiziert und für source-explizite E-Mail-Gruppenrankings verwendet werden, wird dadurch aber **nicht** zu einer eigenen/aktuellen Anzeige: `tracked_ad_ids()`, Bestandszusammenfassung und Write-Autorität bleiben unverändert. Der CLI greift weder auf Kleinanzeigen noch auf andere Netzwerkdienste zu und verändert keine Plattformdaten.

## Lokaler Import von Kleinanzeigen-Nachrichtenkopien

Issue #2 erlaubt für private Accounts lokale Auswertung von offiziellen Kleinanzeigen-E-Mail-Kopien, die im eigenen Postfach ankommen. `mark-api` greift dafür **nicht** auf Gmail oder Kleinanzeigen zu. Der Import verarbeitet ausschließlich lokal bereitgestellte rohe RFC822-/`.eml`-Dateien:

```bash
mark-api-import-mail \
  --db /pfad/zu/mark.sqlite \
  /pfad/zu/nachricht-1.eml \
  /pfad/zu/nachricht-2.eml
```

Der Parser bindet Kleinanzeigen-Absender, `X-Conversation-ID`, `X-Message-ID`/standardisierte `Message-ID`, Anzeigen-ID und Zeitzone gegeneinander und bricht bei Widersprüchen fail-closed ab. Diese Prüfung validiert die Struktur und Konsistenz der nutzerbereitgestellten Rohmail; sie ist **keine kryptographische Absenderauthentifizierung**. Der Import setzt voraus, dass die rohe Nachricht aus dem eigenen Postfach bereitgestellt wird. Persistiert werden nur Anzeigen-ID, Conversation-ID, Provider-Message-ID, Zeitstempel und Quelle. **Nachrichtentext und Personennamen werden nicht gespeichert.** Exakte Wiederholungsimporte sind idempotent; dieselbe Provider-Message-ID mit abweichenden Daten blockiert den gesamten Batch.

Die daraus ableitbaren Größen bleiben bewusst von `ReactionSnapshot` getrennt. Die read-only Query-Surface `/api/email-reactions` bzw. `/api/ads/{id}/email-reactions` exponiert die importierten Zähler; Analytics führt sie source-explizit als `email_conversation_count` und `email_inbound_message_count`. Die bestehenden Metriken `conversation_count`, `inbound_message_count` und `unique_buyer_count` bleiben unverändert ReactionSnapshot-basiert. Aus E-Mail-Kopien wird insbesondere kein `unique_buyer_count` erfunden und es findet keine additive Doppelzählung zwischen Quellen statt.

Derselbe Import kann im normalen Produktstart direkt mit wiederholtem `mark-api-launch --email FILE` ausgeführt werden. Das ersetzt den separaten `mark-api-import-mail`-CLI nicht; es integriert dieselbe lokale, netzwerkfreie und idempotente Importlogik lediglich in den kohärenten Startup-Pfad, sodass die Reaction-Daten beim ersten Dashboard-Read bereits vorhanden sind. Danach kann `mark-api-classify` dieselbe Email-only-ID lokal labeln. Solche Labels nehmen nur an Rankings teil, deren gewählte Metrik diese ID tatsächlich enthält; insbesondere können Email-only-IDs nach `email_conversation_count` oder `email_inbound_message_count` gruppiert werden, ohne in Views-, Owner- oder `ReactionSnapshot`-Gruppen aufzutauchen.

## Lokaler E2E-Smoke

Der zulässige lokale Datenpfad kann ohne Plattformzugriff end-to-end geprüft werden:

```bash
mark-api-local-smoke \
  /pfad/zu/nachricht-1.eml \
  /pfad/zu/nachricht-2.eml
```

Der Smoke erzeugt dafür ausschließlich eine **ephemere SQLite-Datenbank**, importiert die angegebenen lokalen RFC822-Dateien, startet den bestehenden Dashboard-Server auf `127.0.0.1` mit einem temporären Port und liest anschließend `/healthz`, `/api/email-reactions` sowie das Ranking `email_inbound_message_count`. Für die Loopback-Probes werden HTTP-Proxies deaktiviert. Danach wird zusätzlich geprüft, dass ein `POST` auf die read-only API mit `405 Allow: GET` abgewiesen wird und dass `/api/write/delete` nicht existiert (`404`).

Der Smoke konstruiert **keinen** Kleinanzeigen-, Browser-, Mobile- oder Private-HTTP-Adapter, kontaktiert Kleinanzeigen nicht und aktiviert keine Plattformwrites. Die ephemere Datenbank wird nach dem Lauf verworfen. Er belegt damit nur die lokale Kette `user-provided .eml -> SQLite -> Query/Analytics -> Loopback-Dashboard`; er ist keine Freigabe für Plattformautomation und ersetzt keine noch fehlende Contract-Evidenz für das konkrete PRO-Statistikformat.

## Offizielles ProSellers-Admission-Gate

Der optionale offizielle ProSellers-Pfad bleibt standardmäßig gesperrt. `mark_api.prosellers` führt ausschließlich eine **lokale** Zulassungsprüfung durch; das Modul fordert keinen Token an und sendet keinen Request an Kleinanzeigen.

Ein zukünftiger offizieller API-Client darf nur konstruiert werden, wenn gleichzeitig alle folgenden Voraussetzungen erfüllt sind:

- Kontotyp `professional`,
- PRO-Paket `power` oder `premium`,
- das konkrete API-Entitlement wurde ausdrücklich bestätigt,
- tatsächlich provisionierte `client_id` und `client_secret` liegen nichtleer vor,
- die Write-Authority ist explizit `api_originated_only`: API-originierte Anzeigen werden nicht mit manuellen Web-Writes vermischt.

Die Entscheidung liefert nur stabile Reason-Codes wie `plan_not_api_eligible` oder `api_entitlement_not_confirmed`. Credential-Werte werden weder normalisiert noch persistiert und sind aus `repr`, Decision-Payloads und Admission-Exceptions ausgeschlossen. Power/Premium oder vorhandene Strings allein gelten ausdrücklich **nicht** als Entitlement-Beweis.

Dieser Slice implementiert nur das Gate. OAuth-Tokenabruf (`client_credentials`, Audience `consumer-goods-api`) und Requests an die Goods API bleiben separate spätere Arbeit und dürfen erst hinter diesem Gate ergänzt werden.

## Privater Web-UI-Writer

Der zentrale private Writer-Pfad wird als eigener `PrivateWebWriter` gegen die **normale Kleinanzeigen-Weboberfläche** aufgebaut. Er ist ausdrücklich nicht der historische `BrowserBotAdapter` und kein Fallback auf private/mobile Reverse-Engineering-HTTP-APIs.

Der erste Contract `PrivateWebPage -> PrivateWebContentWriter` ist eng begrenzt:

- jede Operation erhält genau eine explizite eigene `ad_id`,
- vor einer Feldänderung muss der geöffnete Editor `ready` sein und exakt dieselbe Anzeigen-ID zeigen,
- `login_required`, `mfa_required`, `captcha_required`, `security_challenge` und unbekannte Zustände blockieren ohne Submit,
- nur angeforderte und tatsächlich abweichende Titel-/Beschreibungsfelder werden lokal im Editor geändert,
- unmittelbar vor dem Speichern werden Anzeigen-ID **und beide Editorwerte** erneut geprüft,
- es gibt höchstens einen Submit; Browser-/Providerfehler werden ohne Inhalte, URLs, Cookies oder Secrets auf eine Stage-Klasse reduziert,
- der Plattform-Outcome wird weiterhin nur durch den bestehenden `SafeWriteOrchestrator` mit frischem Owner-Pre-Read und Post-Read bestätigt; ein unklarer Submit wird nicht wiederholt.

Passwörter, MFA-Codes und Cookies sind kein `mark-api`-Konfigurationsmodell. Der Nutzer authentifiziert die persistente Browser-Sitzung selbst. Der konkrete CDP-Driver und ein gegateter Real-Smoke sind inzwischen belegt. Die Runtime-Komposition konsumiert ausschließlich einen **bereits laufenden** loopback-only CDP-Worker; Browser-Start/-Stop und Reauthentifizierung bleiben außerhalb des Core.

Für diese Runtime muss das deklarierte optionale Extra installiert sein:

```bash
python -m pip install -e '.[private-web]'
```

`build_private_web_content_runtime(cdp_port=...)` liefert fünf für den Application-Layer bestimmte Flächen: `create_writer`, `content_writer`, `state_writer`, `delete_writer` und die target-bound Factory `content_reader_for(ad_id)`. Jede Create-, Content-, Lifecycle- oder Delete-Writer-Operation erhält eine frische `CdpPrivateWebPage`; jeder Content-Read ist an genau die angeforderte eigene Anzeigen-ID gebunden. Create/Delete benötigen im normalen PrivateWeb-Pfad keine künstlich duplizierte zweite Management-Runtime: Create bindet den Inventarübergang durch frische Vor-/Nachbeobachtungen und bestätigt die resultierende ID zusätzlich über den target-bound Content-/Detail-Read. Delete bindet die Ziel-ID vor dem genau einmaligen Submit und verlangt danach zwei frische erfolgreiche Abwesenheitsbeobachtungen; unklare Reads bleiben `AMBIGUOUS`. Eine separat etablierte `PrivateWebInventoryRuntime` kann weiterhin optional über `confirmation_runtime=` injiziert werden, wird aber nicht als Voraussetzung oder automatisch als angeblich unabhängige Quelle konstruiert. Private/mobile Reverse-Engineering-HTTP bleibt dabei ausgeschlossen.

## Lokale Mark Write API

Slice D führt eine **separate** loopback-only Write-Surface ein. Diese Write API bleibt auch nach Einführung der Dashboard Write UX die alleinige HTTP-Ausführungs-, Auth-, Capability- und Idempotenzautorität; der Product Launcher stellt lediglich einen Same-Origin-Proxy darauf bereit. Der standalone Dashboard-Server bleibt read-only. Der normale Create-Vertrag bleibt mediafrei, zusätzlich ist ein getrennt gegateter Media-Create-Vertrag exponiert:

- `POST /api/write/ads` für den engen mediafreien Create-Vertrag aus `category_path`, `title`, `description` und `price_eur`,
- `POST /api/write/media/stage` für genau einen lokal ausgewählten JPEG-/PNG-/WebP-Bildkörper; Antwort ist ein intern erzeugter opaker `media_ref`,
- `POST /api/write/media/ads` für dieselben Create-Felder plus die vom Produktclient intern gehaltenen `media_refs`,
- `PATCH /api/write/ads/{id}` für Titel/Beschreibung,
- `POST /api/write/ads/{id}/pause`,
- `POST /api/write/ads/{id}/activate`,
- `DELETE /api/write/ads/{id}`; die authentifizierte, exakt ID-gebundene Nutzeroperation ist die Freigabe, die Audit-/Idempotenz-Referenz wird intern erzeugt.

Der Media-Create-Pfad benötigt die eigene Capability `CREATE_MEDIA`; `CREATE` allein autorisiert ihn nicht. `media_refs` sind ausschließlich opake, eindeutige ASCII-Handles der Form `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`. Die Create-HTTP-Schicht akzeptiert weiterhin keine Dateipfade. Der getrennte Staging-Endpunkt nimmt ausschließlich den vom Nutzer ausgewählten bounded Bildkörper plus sicheren Basename an, speichert ihn privat und gibt nur das Handle zurück. Wird `CREATE_MEDIA` aktiviert, erzeugt der normale High-Level-Builder den Handle-Store und Media-Service automatisch; explizite `media_bindings` bleiben nur ein Low-Level-/Test-Seam. Reply bleibt ein separates, nicht exponiertes Gate.

Die Write-Surface bindet ausschließlich an `127.0.0.1` und verlangt vor jedem Service-Aufruf einen Bearer-Token, eine passende Capability und `writes_enabled=true`. Der Core behält seinen eigenen unabhängigen `writes_enabled`-Gate, sodass die HTTP-Surface allein keine Plattformwrites freischaltet. Tokens werden nur zur Laufzeit injiziert und weder persistiert noch in `repr` ausgegeben.

Jede mutierende Anfrage benötigt einen `Idempotency-Key`. Vor dem Service-Aufruf wird ein Request-Fingerprint atomar in derselben SQLite-Datenbank als `in_progress` gespeichert; beim Media-Create gehören die normalisierten `media_refs` zum Fingerprint. Der Write-API-Server hält zusätzlich einen exklusiven OS-Lock auf derselben SQLite-Datei bis Socket, Serve-Schleife und alle bereits akzeptierten Handler vollständig quiesziert sind, sodass nicht zwei Write-Runtimes dieselbe Claim-Domain gleichzeitig ausführen können. Claims tragen einen internen Runtime-Owner und einen separaten `execution_started_at`-Barrier: Ein identischer Claim eines beendeten Prozesses darf nur übernommen werden, solange nachweislich noch **keine** Write-Ausführung begonnen hatte. In der konkreten PrivateWeb-Produktkomposition wird vor dieser Persistenz zuerst das gemeinsame Operations-Lock erworben; Claims von Handlern, die hinter einem anderen Write oder einer Media-Reconciliation warten, bleiben dadurch nachweislich ungestartet. Erst unter gehaltenem Lock wird unmittelbar vor dem Service-Delegate der Start owner-gebunden persistiert; ab diesem Moment bleibt jeder Restart fail-closed und autorisiert keinen Blind-Retry. Legacy-`in_progress`-Rows ohne beweisbaren Owner bleiben ebenfalls strikt gesperrt. Ein identischer bereits abgeschlossener Request replayt ausschließlich die gespeicherte HTTP-Response; ein abweichender Request mit demselben Key wird abgewiesen.

Die HTTP-Antwort exponiert den sanitisierten `OperationReceipt` beziehungsweise `CreateOperationReceipt` und setzt immer `platform_retry_authorized=false`. Für den mediafreien Create sowie die ID-gebundenen Writes bleibt `CONFIRMED` = 200, `PRECONDITION_FAILED` = 409 und `AMBIGUOUS` = 202. Beim Media-Create bleibt `CreateOperationReceipt.CONFIRMED` die Anzeigen-/Content-Bestätigung; der High-Level-PrivateWeb-Builder komponiert standardmäßig den öffentlichen read-only `PrivateWebPublicMediaPersistenceVerifier`; ein explizit injizierter `PrivateWebMediaPersistenceVerifier` bleibt als Low-Level-/Test-Seam zulässig. Der Verifier prüft exakt die vor dem ersten Owner-Pre-Read stabilisierten Media-Quellen gegen die target-bound VIP-Galerie. Nur wenn Create/Content **und** dieser Media-Post-Read den erwarteten Satz exakt bestätigen, liefert der Media-Pfad HTTP 200 mit `media_persistence_confirmed=true` und `media_post_read_status=confirmed`. Eine vollständig lesbare Abweichung wird innerhalb des Verifier-Zeitbudgets bis zum Ende des **nutzbaren** Beobachtungsfensters weiter gepollt; ein weiterer Poll startet nur, wenn das Restbudget mindestens den gemessenen Aufwand der letzten vollständigen Abweichungsbeobachtung plus kleine Reserve tragen kann. Erst die letzte vollständige Abweichung am Ende dieses Fensters ergibt `MISMATCH`. Ein vorzeitig erschöpfter interner Safety-Attempt-Cap bleibt ebenso wie HTTP-, Transport-, Parser-, Decoder- oder ein **vor** der gemeinsamen absoluten Deadline auftretender Timeout `UNKNOWN`; verbraucht dagegen ein bereits mit ausreichendem Budget zugelassener letzter Poll die gemeinsame Deadline oder erschöpft der normale Public-Web-Fetch sein exakt aus diesem Restbudget abgeleitetes Fetch-Timeout, ersetzt diese bloß unvollständige Endbeobachtung nicht den zuvor vollständig belegten Mismatch. Beides hält einen ansonsten content-bestätigten Media-Create bei HTTP 202/`media_persistence_confirmed=false`; `PRECONDITION_FAILED` bleibt HTTP 409. Der Media-Post-Read erzeugt keine zusätzliche Retry-Autorität und hebt einen bestehenden Submit-UNKNOWN-Fence nicht auf. Die konkrete PrivateWeb-Komposition serialisiert sämtliche `MarkService`-Writes, Media-Create und Media-Reconciliation über ein gemeinsames Operations-Lock und drainiert akzeptierte HTTP-Handler vor Runtime-Cleanup. Solange `PrivateWebMediaCreateRuntime.reconciliation_required` wahr ist, verweigert dieselbe Komposition jeden weiteren Core- oder Media-Write noch vor dem Service-Delegate; erst erfolgreiche `reconcile_media_submit()`-Beobachtung hebt diesen Fence wieder auf. `build_private_web_write_api_runtime(...)` besitzt `PrivateWebContentRuntime`, optional `PrivateWebMediaCreateRuntime`, `MarkService`, optional `PrivateWebMediaCreateService`, bei `CREATE_MEDIA` standardmäßig den runtime-eigenen `PrivateWebMediaHandleStore` und einen loopback-only `LoopbackWriteApiServer`; eine unabhängige Confirmation-Runtime wird **nicht** automatisch erzeugt, weil im aktiven Produktpfad keine zweite unabhängige Inventarquelle existiert. Eine separat etablierte `PrivateWebInventoryRuntime` kann weiterhin optional über `confirmation_runtime=` injiziert werden, ist für den normalen Create-/Delete-Pfad aber nicht erforderlich: ohne sie verwendet das Produkt zeitlich getrennte frische Owner-Inventarbeobachtungen; Create verlangt zusätzlich den target-spezifischen Content-/Detail-Read, Delete eine zweite frische Abwesenheitsbeobachtung nach exakt einem Submit. HTTP-`WriteApiAccess.writes_enabled`, `core_writes_enabled` und `media_writes_enabled` bleiben für generische/Low-Level-Caller unabhängige default-off Gates. `mark-api-launch` setzt sie im normalen Produktpfad gemäß D-023 ausdrücklich auf `true`, erzeugt einen prozesslokalen Bearer und komponiert alle vorhandenen Write-Capabilities ohne zusätzlichen Opt-in. Der Browserprozess bleibt caller-owned; die automatisierten Regressionstests führen keinen realen Kleinanzeigen-Plattformwrite aus.

## Produktinternes Media-Staging

Wenn `CREATE_MEDIA` vorhanden ist, stellt die lokale Write-Runtime zusätzlich `POST /api/write/media/stage` bereit. Die Produktoberfläche kann damit ausgewählte JPEG-, PNG- oder WebP-Dateien an die loopback-only Runtime übergeben und erhält einen zufällig erzeugten opaken `media_ref`. Die Runtime kopiert die Datei in einen privaten `0600`-Stagingbereich; weder Dateipfad noch Bytes werden im späteren Create-Payload benötigt. Der Handle wird nach erfolgreicher Stabilisierung für den Media-Create verbraucht und die Stagingkopie entfernt. Authentifizierung und `CREATE_MEDIA` bleiben erforderlich; das lokale Staging selbst ist kein Plattformwrite und hängt deshalb nicht am Plattform-`writes_enabled`-Gate. Der eigentliche `POST /api/write/media/ads`-Publish bleibt an Write-/Core-/Media-Gates, Idempotenz und alle bestehenden No-Blind-Retry-Regeln gebunden.
Unverbrauchte Staging-Handles sind auf 32 Einträge und 100 MiB Gesamtgröße begrenzt. Staging-Bodies werden jeweils nur einzeln gepuffert; Write-API-Body-Reads haben eine 10-Sekunden-Deadline, damit stockende lokale Clients den Runtime-Shutdown nicht unbegrenzt blockieren.

Das lokale Staging allein bestätigt weiterhin **keine** serverseitige Medienpersistenz. Im normalen PrivateWeb-Produktpfad folgt nach einem content-bestätigten Create deshalb der getrennte öffentliche VIP-Galerie-Post-Read aus D-021. Nur dessen vollständiges target-bound one-to-one Inhaltsmatching setzt `media_persistence_confirmed=true`; jede unsichere Beobachtung bleibt fail-closed.

## Bestätigte Writes und lokale Anzeigenansicht

Die SQLite-Persistenz speichert bei einem tatsächlich ausgeführten, vollständig gebundenen `CONFIRMED`-Receipt zusätzlich die vorhandene Post-Read-Beobachtung der Zielanzeige in derselben Transaktion. Content- und Lifecycle-Updates übernehmen den exakten Post-Snapshot; Create übernimmt den target-bound Content-Post-Snapshot. Ein bestätigtes Delete erzeugt nur für seine explizite Ziel-ID eine `ABSENT`-Beobachtung mit Quelle `confirmed-write:delete`. Unbeteiligte Anzeigen und die bisherige Historie bleiben unverändert. Ein Teil-Read wird nie als vollständiger Inventarbestand behandelt.

Nach erneutem Laden liest das weiterhin read-only Dashboard diese persistierten Beobachtungen ohne zusätzlichen Plattformzugriff. `AMBIGUOUS`, `PRECONDITION_FAILED` und unvollständige alte Receipts aktualisieren die Anzeigenprojektion nicht. Ein Idempotency-Replay schreibt weder ein weiteres Receipt noch einen weiteren Snapshot. Scheitert die gemeinsame Transaktion, meldet die Write API keinen Erfolg und autorisiert keine Wiederholung des Plattformversuchs.

Die Anzeigenhistorie und die daraus gelesene aktuelle Ansicht sind nach dem tatsächlichen, zeitzonenbewussten `observed_at` sortiert; nur bei demselben Zeitpunkt entscheidet die Einfügereihenfolge. Ein verspätet gespeicherter älterer Readback bleibt damit als Historie erhalten, verdrängt aber keinen neueren Zustand. Das gilt auch bei konkurrierenden Requests beziehungsweise getrennten Store-Instanzen.

Die lokale Anzeigenansicht bestätigt weder Medienpersistenz noch Datenfrische außerhalb der übernommenen Beobachtung. Alte Receipts werden nicht nachträglich rückprojiziert; reine Crash-Checkpoints vor dem Media-Post-Read bleiben Recovery-Evidenz. Die übrigen Integrations-, Frische- und Recovery-Aufgaben aus Issue #57 sind dadurch nicht abgeschlossen.

## Offene fachliche Punkte

Die Reaktionsdaten werden absichtlich getrennt als `conversation_count`, `unique_buyer_count` und `inbound_message_count` gespeichert. `AnalyticsContract.reaction_metric` kann explizit genau eine dieser Größen auswählen, ist aber standardmäßig `None`. Damit wird keine davon stillschweigend zur Bedeutung von „wie viele geschrieben haben“ erklärt.

Ebenso ist noch keine fachlich bestätigte Zielfunktion für „beste Lösung“ definiert. `AnalyticsContract.objective_metric` kann explizit eine vorhandene Analytics-Rohmetrik binden und ist standardmäßig ebenfalls `None`. Objective-gebundene Rankings brechen ohne diese Konfiguration fail-closed ab; die bestehenden deskriptiven Ranking-Endpunkte verlangen weiterhin einen expliziten `metric`-Parameter. Fehlende Werte werden ausgelassen, beobachtete Nullwerte bleiben erhalten. Keine Rangfolge begründet Kausalität oder Qualität.

Das read-only Dashboard exponiert den aktuellen Contract unter `GET /api/analytics/contract`. Ohne explizite Zielmetrik zeigt die Metrikauswahl zunächst „Metrik auswählen …“ und lädt kein Ranking. Für eine bewusst gesetzte Laufzeitkonfiguration akzeptiert `mark-api-dashboard` optional `--reaction-metric` und `--objective-metric`; das Repository definiert dafür keinen fachlichen Default.

## Zulässige Betriebswege

[Issue #2](https://github.com/alexdermohr/mark-api/issues/2) ist abgeschlossen und trennt den erfolgreichen technischen PoC vom zulässigen Produktbetrieb:

- Private/mobile Reverse-Engineering-HTTP und der historische `BrowserBotAdapter` bleiben technische PoC-Evidenz und werden nicht als automatische Produkt-Fallbacks aktiviert.
- Für private Konten ist der getrennte lokale `PrivateWebWriter` der vorgesehene Write-Backendpfad: normale Weboberfläche, eigene nutzer-authentifizierte Sitzung, exakte eigene Anzeigen-ID und Challenge-Stop ohne Auth-/CAPTCHA-Umgehung.
- Lokale Analytics dürfen weiterhin aus vom Nutzer bereitgestellten offiziellen Daten entstehen, insbesondere Kleinanzeigen-E-Mail-Kopien und — sobald das konkrete Dateiformat belegt ist — offiziellen PRO-Statistikdownloads.
- Die offizielle ProSellers API bleibt ein optionaler paralleler Pfad ausschließlich für berechtigte professionelle Power-/Premium-Konten mit provisionierten Zugangsdaten. Für diesen Modus müssen API-Herkunft und Write-Authority der betroffenen Anzeigen separat gebunden werden.
- Plattformwrites bleiben im Core standardmäßig deaktiviert. Der private Web-Writer darf nur explizit mit einem bereits authentifizierten, loopback-only Browser-Worker und der target-bound Runtime-Komposition aktiviert werden; unklare Plattformergebnisse werden weiterhin nicht wiederholt.

## Projektregistratur

Dieses Repository ist die **kanonische Registratur für mark-api**.

- Aufgaben und offene Punkte: **GitHub Issues dieses Repositories**
- Entscheidungen, Gesprächsstände und Evidenz: **docs/**
- Implementierung: dieses Repository
- **Für dieses Projekt keine Registrierung im Bureau.**

## Dokumentation

- Schriftliche Wunschvorstellung: [docs/requirements-source-2026-09-23.md](docs/requirements-source-2026-09-23.md)
- Produktspezifikation: [docs/product-spec.md](docs/product-spec.md)
- Anforderungen und offene Punkte: [docs/requirements.md](docs/requirements.md)
- Telefonat, Rohtranskription: [docs/call-2026-09-23-raw.md](docs/call-2026-09-23-raw.md)
- Telefonat, bereinigter Stand: [docs/call-2026-09-23.md](docs/call-2026-09-23.md)
- Ausgangskontext: [docs/context.md](docs/context.md)
- Entscheidungen: [docs/DECISIONS.md](docs/DECISIONS.md)
- Architekturentscheidung: [docs/architecture-decision-2026-09-24.md](docs/architecture-decision-2026-09-24.md)
- Realtest/PoC: [docs/poc-2026-09-24.md](docs/poc-2026-09-24.md)
- Integrationsoptionen: [docs/integration-options.md](docs/integration-options.md)

## Datenschutz / Öffentlichkeit

Das Repository ist öffentlich. Zugangsdaten, Tokens, Telefonnummern und sonstige nicht erforderliche personenbezogene Daten werden nicht committed. Private Originalmedien und nicht zur Veröffentlichung bestimmte Quelldateien bleiben außerhalb des Repositorys. Ausgewählte Transkriptionen und Anforderungsauszüge werden nur dann öffentlich dokumentiert, wenn sie für das Projekt erforderlich sind und keine unnötigen sensiblen oder personenbezogenen Inhalte enthalten.

## Arbeitsregel

Der private Produktpfad kombiniert den bestehenden lokalen/read-only Datenpfad mit dem gegateten `PrivateWebWriter` und seiner expliziten CDP-Runtime für eigene Anzeigen über die normale nutzer-authentifizierte Weboberfläche. Der Core besitzt keinen Browser-Lifecycle; die Runtime konsumiert nur einen bereits laufenden loopback-only Worker. Private/mobile Reverse-Engineering-APIs und der historische BrowserBot bleiben PoC-Evidenz und werden nicht automatisch aktiviert. Login/MFA/CAPTCHA/Sicherheitschecks werden nicht umgangen. ProSellers bleibt ein optionales zweites Backend hinter seinem Credentials-/Entitlement-/Write-Authority-Gate.
