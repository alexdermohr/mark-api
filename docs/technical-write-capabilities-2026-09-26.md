# Technische Write-Machbarkeit und Umsetzungsplan — 26.09.2026

Status: **historische technische Machbarkeitsdokumentation. D-008 band den Betrieb zunächst read-only; D-010 präzisiert seit 28.09.2026 den neuen privaten Web-UI-Writer.**

> Private/mobile Reverse-Engineering-Pfade und der historische `BrowserBotAdapter` bleiben PoC-Evidenz. D-010 führt stattdessen einen getrennten `PrivateWebWriter` über die normale, vom Nutzer selbst authentifizierte Weboberfläche ein. ProSellers bleibt optional hinter dem separaten Credentials-/Entitlement-/Write-Authority-Gate.

## Auftrag

Mark soll nicht nur lesen und auswerten, sondern eine eigene API bereitstellen, über die die Verwaltung eigener Kleinanzeigen-Angebote einschließlich Schreiboperationen möglich ist.

Diese Datei trennt deshalb bewusst:

1. **technische Machbarkeit** — was über vorhandene Plattformpfade tatsächlich implementierbar ist,
2. **Remote-Beleg** — was im Projekt bereits gegen einen eigenen Account praktisch bestätigt wurde,
3. **Betriebsfreigabe** — welcher Pfad in einem konkreten Kontomodell tatsächlich eingeschaltet werden darf.

Ein fehlendes Betriebs-Gate ist kein Gegenbeweis zur technischen Machbarkeit. Umgekehrt wird eine technisch vorhandene private Schnittstelle nicht als offiziell freigegeben bezeichnet.

## Zielbild

```text
Client / Mark-UI
      |
      v
Mark HTTP API
      |
      v
SafeWriteOrchestrator
      |
      +-- ProSellersAdapter        # offizielle Power/Premium-API
      +-- MobileApiAdapter         # eigener Account, HTTP-first
      +-- BrowserBotAdapter        # aktuell primär für Content-Update; später Fallback
      |
      +-- unabhängige Readback-Adapter
```

Der Mark-Core bleibt provider-neutral. Jeder Plattform-Write bleibt im aktuellen Low-Level-Core standardmäßig deaktiviert und läuft weiterhin über frischen Precondition-Read und genau einen Mutationsversuch ohne Blind-Retry. ID-gebundene Content-/Lifecycle-Writes verwenden ihren target-bound Post-Readback; Create/Delete folgen der durch D-019 supersedierten Confirmation-Semantik mit zeitlich getrennten frischen Inventarbeobachtungen und bei Create zusätzlichem target-bound Detailread.

## Evidenzklassen

- **O — offiziell dokumentiert:** aktuelle Kleinanzeigen Developer-Dokumentation.
- **R — im Projekt remote belegt:** eigener Account/Testanzeige, bereits dokumentierter PoC.
- **S — aktueller Quellcode statisch belegt:** aktuell geprüfte Open-Source-Clients/Reverse-Engineering-Referenzen.
- **H — historische CAPI-Spezifikation:** ältere, aber zum heutigen Endpoint-Familienmodell passende eBay-Classifieds/Kleinanzeigen-API-Dokumentation.
- **P — geplant:** durch diesen Projektstand als nächster Implementierungsschritt festgelegt.

## Capability-Matrix

| Fähigkeit | Offizielle ProSellers API | Private/mobile HTTP | Browser | Projektstand |
|---|---|---|---|---|
| eigene Anzeigen lesen | O | R | R | vorhanden |
| Anzeige erstellen | O | S | S | technisch vorhanden; privater Remote-Smoke offen |
| Titel/Beschreibung in-place ändern | O | S/H + lokale Implementierung | R | konservativer HTTP-Client lokal vorhanden; Remote-Smoke offen |
| Preis/Attribute ändern | O | S/H | R/S | nach HTTP-Update-Grundpfad |
| Bilder hochladen | O | S/H | S | offiziell vollständig; mobile Detailfluss noch zu härten |
| Bilder löschen/sortieren | O | S/H | S | offiziell vollständig |
| pausieren | O | R | R | vorhanden |
| aktivieren/resumen | O | R | R | vorhanden |
| löschen | O | R | R | vorhanden |
| verlängern | anderes Publication-Modell | S | S | mobile Fähigkeit vorhanden |
| Conversations lesen | nicht öffentlich dokumentiert | R | Referenzpfade | vorhanden |
| Nachrichten lesen | nicht öffentlich dokumentiert | R | — | vorhanden; Read kann mark-read Side-Effect haben |
| Nachricht antworten | nicht öffentlich dokumentiert | S | — | technischer Endpoint vorhanden; Remote-Write noch nicht separat gesmoked |
| neue Conversation starten | nicht öffentlich dokumentiert | S | — | technischer Endpoint vorhanden |
| Views | kein Listing-Write-Thema | S + Management R | Management R | technisch verfügbar |
| Merkerzahl | kein Listing-Write-Thema | S + Management R | Management R | technisch verfügbar |
| Replies/Anfragen | kein Listing-Write-Thema | Management R / Inbox R | Management R | technisch verfügbar |

## Offizielle ProSellers API

Aktuelle öffentliche Quellen:

- https://developer.kleinanzeigen.de/docs/general/
- https://developer.kleinanzeigen.de/docs/general/authentication/
- https://developer.kleinanzeigen.de/docs/listing/api/
- https://developer.kleinanzeigen.de/docs/listing/subscription-packages/
- https://developer.kleinanzeigen.de/docs/general/rate_limits/

Technisch dokumentiert sind insbesondere:

- `POST /api/goods/v1/listings`
- `PUT /api/goods/v1/listings/{uuid}`
- `DELETE /api/goods/v1/listings/{uuid}`
- Publication Create/Update/Delete
- `PUT .../publications/{publicationUuid}/state?requestedState=PAUSED|RESUMED`
- Media-Ticket mit presigned Upload-URL
- Media an Listing hängen, löschen und sortieren
- Client-Credentials-Flow
- getrennte Read-/Write-Rate-Limits

Damit ist ein vollständiger offizieller Write-Adapter für API-originierte PRO-Anzeigen technisch möglich.

Wichtig: Web- und API-Anzeigen sind laut offizieller Dokumentation nicht synchron. Im PRO-API-Modus wird deshalb die API alleinige Write-Authority für API-originierte Anzeigen.

## Private/mobile HTTP-CAPI

### Aktueller Client

Der am 26.09.2026 geprüfte Stand von `monkrel/kleinanzeigen-api` enthält HTTP-Pfade für:

- Auth/PKCE + Refresh Token,
- `my_ads()`,
- `get_my_ad()`,
- `post_ad()`,
- `pause_ad()`,
- `activate_ad()`,
- `delete_ad()`,
- `extend_ad()`,
- Conversations,
- Messages,
- Reply,
- Start einer Conversation,
- Watchlist.

Referenz: https://github.com/monkrel/kleinanzeigen-api

### Aktuelle Endpoint-Kartierung

Eine aktuelle Android-App-basierte Endpoint-Kartierung nennt für den owner-scoped Pfad `api/users/{userId}/ads/{adId}` ausdrücklich die Write-Verben **create / edit / extend / delete** und trennt diese von fremden Accounts.

Referenz:
https://github.com/its-me-prash/kleinanzeigen-reader/blob/3b429914dc8b78ead54d42ae0ecb5b3106e37fb1/references/mobile-api.md

Diese Referenz ist technische Reverse-Engineering-Evidenz, keine Betriebsfreigabe.

### Historische CAPI-Spezifikation

Die ältere eBay-Classifieds-API-Spezifikation dokumentiert den owner-scoped Updatepfad explizit:

```text
PUT /users/{idName}/ads/{adId}
```

mit demselben Write-Payloadmodell wie Create.

Referenz:
https://github.com/tejado/ebk-client/blob/8468b6b6cf3997b3fadeb672d64497c5b63ef728/docs/pages/users.html

Zusammen mit der aktuellen Endpoint-Kartierung ist damit die technische Hypothese stark genug, den HTTP-in-place-Updatepfad zu implementieren. **Noch nicht behauptet wird**, dass der konkrete 2026er Request bereits remote mit unserem Account bestätigt ist.

### Lokales HTTP-Update-Primitive

`MonkrelPrivateHttpContentClient` implementiert jetzt lokal den fehlenden `update_ad()`-Vertrag:

- frischer owner-scoped GET der konkreten Anzeige,
- exakte ID-Bindung,
- Rekonstruktion des bestehenden Write-Payloads über den aktuell von monkrel verwendeten Create-XML-Builder,
- genau ein owner-scoped `PUT /api/users/{uid}/ads/{adId}`,
- `max_retries=1` als harte Voraussetzung für den dedizierten Write-Client,
- No-op-Rejection,
- fail-closed bei nicht verlustfrei rekonstruierbaren Mehrfachattributen, Bildern ohne eindeutigen XXL-Link oder noch nicht modellierten Commerce-/Media-Sonderzuständen,
- kein Token-, Cookie- oder Response-Body-Logging im Mark-Core.

Damit ist die lokale Transportlücke geschlossen. Der Browser bleibt für echte Inhaltsupdates weiterhin operativ primär, bis der Remote-Smoke den HTTP-Pfad auf einer eigenen Anzeige bestätigt.

### Runtime-Komposition

`MonkrelPrivateHttpRuntimeClient` schließt jetzt auch die Laufzeitlücke zwischen dem bestehenden `MonkrelMobileApiAdapter` und dem neuen HTTP-Content-Writer:

- `my_ads`, Pause, Aktivieren, Delete, Conversations und Messages werden explizit an den normalen upstream Client delegiert,
- `update_ad` wird ausschließlich an den separaten `MonkrelPrivateHttpContentClient` delegiert,
- der Content-Write-Client kann dadurch unabhängig mit `max_retries=1` betrieben werden,
- unbekannte upstream-Methoden werden **nicht** über ein generisches Proxy/`__getattr__` exponiert,
- `build_monkrel_private_http_adapter(...)` liefert direkt einen für Mark nutzbaren `MonkrelMobileApiAdapter` mit dieser Trennung.

Die Runtime-Komposition ist lokal regressionsgetestet. Sie ändert nichts am Remote-Smoke-Gate.

## Im Projekt bereits remote belegt

Der bestehende PoC hat gegen eigene Testdaten bereits praktisch bestätigt:

- eigene Anzeige lesen,
- stabiler in-place Inhaltsupdate über den Browser-Bot,
- Pause und Aktivieren über Mobile/API mit unabhängigem Management-Readback,
- Delete mit Besitzerlisten-Readback,
- Management-Status und Verkäufermetriken,
- Inbox/Conversation mit konkreter Anzeigen-ID,
- Delete-Staleness: Besitzerlisten sind authoritative; Detail-GET/Public-URL können nachlaufen.

Diese Befunde bleiben gültige Regressionsevidenz. Der damalige HTTP-first-Plan ordnete sie neu ein; D-010 supersediert inzwischen nur den aktiven Produktpfad, nicht die historische Evidenz.

## Historische Architekturentscheidung ab 26.09.2026

1. **HTTP-first als damaliges Zielbild.** Für private/eigene Accounts sollte die Mobile-CAPI als primärer technischer Write-Kandidat weiter ausgebaut werden.
2. **Browser bis zum Remote-Beleg primär.** Für bestehende Inhaltsupdates blieb der `BrowserBotAdapter` operativ primär.
3. **Offizielle API parallel anschließbar.** Ein ProSellersAdapter sollte denselben Mark-Domainvertrag bedienen.
4. **Keine Providersemantik im Mark-API-Vertrag.**
5. **Fail closed.** Kein Blind-Retry bei unklarem Write-Ergebnis.
6. **Kein Live-Write im damaligen Slice.**

Diese Reihenfolge bleibt historische technische Evidenz, ist aber seit D-010 **nicht mehr der aktive private Produktplan**.

## Fortschreibung 28.09.2026 — aktiver privater Writer

Der aktive private Writepfad ist jetzt:

```text
MarkService / SafeWriteOrchestrator
        |
        +-- PrivateWebContentRuntime
        |       |
        |       +-- content_reader_for(ad_id)
        |       |       -> ManagementReadAdapter
        |       |       -> CdpPrivateWebOwnerReader (target-bound)
        |       |
        |       +-- create_writer  -> PrivateWebCreateWriter
        |       +-- content_writer -> PrivateWebContentWriter
        |       +-- state_writer   -> PrivateWebStateWriter
        |       +-- delete_writer  -> PrivateWebDeleteWriter
        |               |
        |               +-- jeweils frische CdpPrivateWebPage pro Write
        |               +-- normale Kleinanzeigen-Weboberfläche
        |                   in eigener nutzer-authentifizierter Sitzung
        |
        +-- ProSellersApiWriter (optional, nur nach Admission)
```

Private/mobile HTTP und der historische BrowserBot werden nicht automatisch als Fallback reaktiviert. Der browserdriver-neutrale Contract, der persistente loopback-only CDP-Driver und der gegatete Real-Smoke sind inzwischen belegt. Die Runtime-Komposition bleibt bewusst enger als ein Browser-Lifecycle: sie konsumiert nur einen bereits gestarteten, vom Nutzer authentifizierten CDP-Worker, erzeugt target-bound Content-Reader und frische Page-Adapter pro Writer-Aufruf und schließt nur ihre eigenen CDP-Verbindungen. Browser-Start/-Stop, Login und Reauthentifizierung bleiben außerhalb des Core.

Die optionale Runtime-Dependency ist als `.[private-web]` deklariert. Der Runtime-Builder prüft `websocket-client` vor Browserzugriff fail-closed; er installiert keine Pakete selbst. `MarkService` nimmt optional eine `content_reader_factory(ad_id)` entgegen. Diese Factory wird lazy erst hinter dem zentralen `writes_enabled`-Gate konstruiert und für Pre-/Post-Read desselben Writes wiederverwendet.

Der Real-Smoke auf einer eigenen, frisch owner-verifizierten Anzeige bestätigte einen einmaligen Titel-Write durch den unabhängigen Owner/Web-Post-Read, obwohl die lokale Submit-Bestätigung unbestimmt blieb. Ein separater Restore-Versuch blieb `AMBIGUOUS`; entsprechend wurde er nicht wiederholt. Damit ist gerade die gewünschte No-Retry/Reconciliation-Semantik real belegt, ohne aus dem unbestimmten Restore eine unbelegte Fehlerursache abzuleiten.

### Fortschreibung 30.09.2026 — read-only Delete-UI-Evidenz

Für den privaten Delete-Pfad wurde die aktuell eingeloggte normale Management-Weboberfläche ausschließlich **read-only** untersucht. Es wurde weder der Delete-Button noch dessen Bestätigung aktiviert und keine Kleinanzeigen-Anzeige mutiert.

Die aktuelle eigene Anzeigenzeile zeigte einen sichtbaren, enabled `BUTTON type=button` mit dem Accessibility-/Textnamen `Löschen`. Der geladene UI-Code belegt für genau diesen Control folgende zweistufige Semantik:

- der erste `Löschen`-Handler delegiert die konkrete Anzeige an `handleDeleteSingle(ad)`,
- dieser Handler setzt `adIdsToDelete=[ad.id]` und öffnet nur das Single-Delete-Modal,
- das Modal trägt den Titel `Anzeige löschen` und die Frage `Bist du sicher, dass du die Anzeige löschen möchtest?`,
- der bestätigende Control ist `#delete-celebration-sbmt` mit dem sichtbaren Text `Ja, Anzeige löschen`,
- daneben existiert der explizite Cancel-Control `Abbrechen`,
- erst der Confirm-Handler ruft die eigentliche Delete-Funktion für die gebundene `adId` auf.

Darauf baut der neue `PrivateWebDeleteWriter` konservativ auf: exakte eigene `ad_id`/Row-Bindung, browser-level Input ohne DOM-`.click()`, erneute Prüfung des expliziten Bestätigungsmodals, genau ein Confirm-Mutationsversuch und kein Blind-Retry. Jeder Fehler nach möglichem Browser-Input wird als unbekannter Effekt behandelt. Erfolg wird weiterhin **nicht** aus Modal-/UI-Signalen behauptet, sondern nur über die bestehenden autoritativen Besitzer-/Management-Readbacks des `SafeWriteOrchestrator` bestätigt.

Der Delete-Pfad ist in diesem Stand lokal regressionsgetestet; ein Live-Delete wurde bewusst nicht ausgeführt.

### Fortschreibung 30.09.2026 — read-only Create-UI-Evidenz und lokaler Create-Vertrag

Der normale private Create-Flow wurde mit dem bereits nutzer-authentifizierten Webprofil ausschließlich **read-only** kartiert. Es wurde keine Anzeige veröffentlicht, kein Entwurf gespeichert und kein Bild hochgeladen.

Belegt ist ein zweistufiger Ablauf:

- `/p-anzeige-aufgeben.html` führt über einen expliziten Kategoriebaum; der aktuelle Pfad wird durch echte Browseraktivierungen der sichtbaren Kategorien aufgebaut,
- `Weiter` ist ein `POST` auf `/p-anzeige-aufgeben-schritt2.html` mit CSRF und der gewählten Kategorie-/Attributbindung,
- das Hauptformular bietet getrennte Controls für `Vorschau`, `Entwurf speichern` und `Anzeige aufgeben`,
- der Publish-Control `Anzeige aufgeben` ist `type=button` und läuft zuerst durch `renderIfVerificationRequired(...)`; derselbe erste Browser-Input kann daher entweder Telefonverifikation öffnen oder unmittelbar den Publish-Handler erreichen,
- der aktuelle private Default ist `OFFER` + `FIXED`; `buyNowEligible=false`, `posterType=PRIVATE`, Marketing aus, vollständige Adresse aus, Straße deaktiviert, kein Draft/`adId` und keine hochgeladenen Dateien.

Darauf basiert der neue enge `PrivateWebCreateWriter`:

- expliziter, nichtleerer Kategoriepfad mit eindeutigen Labels,
- nur `OFFER + FIXED`, Titel, Beschreibung und ganzzahliger EUR-Festpreis,
- keine Medien, kein Draft, kein Buy-Now, keine Marketing- oder Adressfreigabe,
- unerwartete kategoriespezifische editierbare Controls blockieren fail-closed,
- Kategorien und `Weiter` werden ausschließlich per browser-level, hit-tested Input bedient; kein DOM-`.click()`,
- unmittelbar vor Publish werden Formvertrag und alle drei gesetzten Werte erneut geprüft,
- exakt ein Browser-Input auf `Anzeige aufgeben`; ab dem ersten möglichen Publish-Input gilt jeder unklare Ausgang als `SubmitUnknown` und wird nicht wiederholt.

Create benötigt wegen der erst nach dem Write bekannten Anzeigen-ID einen eigenen `CreateOperationReceipt`. Gemäß D-019 liest `SafeWriteOrchestrator.create()` die autoritative Owner-/Management-Inventarquelle vor dem Write zweimal zu getrennten Beobachtungszeitpunkten; eine optional separat etablierte `PrivateWebInventoryRuntime` kann weiterhin als zweite Quelle injiziert werden, ist aber keine Produktvoraussetzung. Nach dem einzigen Publish-Versuch müssen die beiden Post-Read-Inventarbeobachtungen gegenüber den jeweiligen Pre-States exakt dieselbe einzelne neue ID sehen und den angeforderten Titel tragen; anschließend muss der getrennte target-bound Private-Web-Reader für genau diese ID Titel und Beschreibung exakt bestätigen. Nur dann ist der Create `CONFIRMED`. Andernfalls bleibt er `AMBIGUOUS`; eine erkannte `WriteNotAttemptedError` bleibt ohne Post-Read `PRECONDITION_FAILED`. Für Delete gilt derselbe Grundsatz ohne Scheinsicherheit durch eine duplizierte Runtime: erfolgreicher target-bound Pre-Read, genau ein Delete-Submit und danach zwei frische erfolgreiche Inventarbeobachtungen ohne die Ziel-ID; sonst bleibt das Ergebnis `AMBIGUOUS` und der Plattformwrite wird nicht wiederholt.

### Fortschreibung 30.09.2026 — read-only PrivateWeb Runtime-Smoke

Nach dem Merge des isolierten Media-Staging-Slices ist der nächste Issue-#5-Härtungsschritt bewusst **kein** weiterer Plattform-Write. Neben dem bestehenden D-010-Content-Runtime-Bundle existiert jetzt ein eigener **Inventory-only Runtime-Builder** ohne Page-/Writer-Surfaces; damit kann der freigegebene Owner-Read lokal end-to-end geprüft werden:

- Quelle ist ausschließlich der autoritative Owner-/Management-Read der bereits nutzer-authentifizierten PrivateWeb-Runtime,
- der Smoke startet und authentifiziert keinen Browser und ruft keinen Create-/Content-/Lifecycle-/Delete-Writer auf,
- nur erfolgreiche Inventar-Reads werden in eine **ephemere** SQLite-Datenbank geschrieben; Read-Fehler und doppelte IDs stoppen fail-closed vor dem Dashboard,
- das Dashboard bindet ausschließlich an `127.0.0.1` auf einem ephemeren Port,
- Health, Summary, Anzeigenprojektion und Analytics werden gegen genau dieses Inventar rückgelesen,
- HTTP-Write-Methoden bleiben `405 Method Not Allowed`, eine Mark-Write-Route ist im Smoke nicht vorhanden,
- der Report führt `platform_writes_enabled=false` fest; die Runtime wird auch bei Fehlern geschlossen.

Dieser Slice ist lokal regressionsgetestet und führt **keinen** Kleinanzeigen-Plattformwrite aus. Ein späterer Lauf gegen eine vorhandene nutzer-authentifizierte CDP-Sitzung bleibt read-only und ersetzt keine separate Freigabe für Create/Media-Publish oder andere Plattformmutationen.

## Geplante Mark-HTTP-API

Das Ziel ist eine eigene schreibfähige Oberfläche, z. B.:

```text
GET    /ads
GET    /ads/{id}
POST   /ads
PATCH  /ads/{id}
DELETE /ads/{id}

POST   /ads/{id}/pause
POST   /ads/{id}/activate
POST   /ads/{id}/extend

GET    /ads/{id}/metrics

GET    /conversations
GET    /conversations/{id}/messages
POST   /conversations/{id}/messages
```

Die konkrete Webframework-Wahl bleibt nachrangig gegenüber stabilen Domain-/Adapterverträgen.

## Umsetzungsreihenfolge

### Slice A — jetzt

- `MonkrelMobileApiAdapter` als `AdContentUpdater` vorbereiten.
- Exakten `ad_id` und Partial-Update `title`/`description` an einen HTTP-fähigen Client delegieren.
- lokale Tests für Partial-Update, ID-Bindung und Fail-closed Eingabe.
- diese technische Machbarkeit und den Plan im Repo festhalten.

### Slice B — HTTP-Primitive lokal vorhanden; nächster Schritt Remote-Beweis

Der konservative `MonkrelPrivateHttpContentClient` ist lokal implementiert und getestet. Er führt in diesem Slice keinen Plattform-Write aus.

Die Runtime-Facade ist ebenfalls implementiert. Der anschließende Remote-Preflight am 26.09.2026 blieb fail-closed:

- kein dedizierter Mobile-Refresh-Token liegt an einem bekannten sicheren Laufzeitpfad vor,
- das persistente Verkäufer-Browserprofil ist vorhanden, die Kleinanzeigen-Websession aber abgelaufen,
- die sichere Grabowski-Browser-Surface erlaubt Navigation, aber keinen generischen Credential-Submit auf öffentlichen Origins,
- es wurde weder ein Auth-Bypass noch ein Kleinanzeigen-Plattformwrite ausgeführt.

Für den ersten echten HTTP-Write ist deshalb weiterhin zuerst eine reguläre Reauthentifizierung erforderlich.

Auf genau einer eigenen, ausdrücklich geeigneten Anzeige:

1. frischer Owner-Read,
2. vollständigen aktuellen Update-Request rekonstruieren,
3. genau eine reversible Inhaltsänderung per HTTP,
4. unabhängiger Management-Readback,
5. gleiche Anzeigen-ID + erwarteter neuer Inhalt,
6. bei Ambiguität kein Retry.

Erst nach diesem Beleg wird der Browser-Content-Updatepfad zum Fallback degradiert.

### Slice C — Create/Media/Reply

- privater Create-Vertrag lokal implementiert und gegen die aktuelle UI read-only gebunden; ein kontrollierter Live-Publish-Smoke bleibt separat offen,
- Media-Staging bleibt ein separater fail-closed Vertrag: stabile private Kopie aus validiertem Descriptor, genau ein browser-level File-Input und exakter FileList-Readback ohne Blind-Retry,
- darauf aufbauend ist jetzt ein eigener lokaler `PrivateWebCreateMediaWriter` implementiert: der bestehende mediafreie Create-Pfad bereitet nur die Form vor, danach werden dieselbe gebundene File-Input-Instanz und Dateiname/Größe erneut geprüft und höchstens ein browser-level Publish-Versuch ausgelöst,
- der Media-Publish-Pfad ist inzwischen über `PrivateWebMediaCreateService` und die separate `POST /api/write/media/ads`-Surface in die Produktkomposition eingebunden; die vor dem ersten Owner-Read stabilisierten lokalen Quellen bleiben bis nach allen Post-Reads verfügbar,
- D-021 ergänzt die fehlende serverseitige Persistenzevidenz über einen normalen read-only VIP-Detailseiten-Post-Read: exakt target-bound Canonical-URL, genau eine Anzeigen-Galerie, strikt whitelisted `img.kleinanzeigen.de`-Objekte, gleiche Bildanzahl und vollständiges reihenfolgeerhaltendes one-to-one Inhaltsmatching nach serverseitiger Skalierung/Rekodierung,
- Bytegleichheit ist ausdrücklich **kein** Kriterium; EXIF-normalisierte, bounded Pillow-Decodierung plus enge Aspect-/RGB-Grenzen bestätigen nur konservativ vergleichbare Inhalte. Echte vollständige Abweichung wird `MISMATCH`, jede Read-/Parser-/Decoderunsicherheit `UNKNOWN`,
- der High-Level-Builder komponiert diesen Verifier bei `CREATE_MEDIA` standardmäßig; explizite Verifier-Injektion bleibt ein Low-Level-/Test-Seam,
- der read-only PrivateWeb Runtime-Smoke verbindet weiterhin Owner-Read, SQLite und Dashboard/Analytics ohne Writer-Aufruf,
- ein kontrollierter Live-Media-Publish-Smoke bleibt separat offen; dieser Slice selbst führt keinen Live-Publish aus,
- Reply nur in einer bestehenden natürlichen Conversation testen; keine künstliche Testnachricht erzeugen.

### Slice D — Mark Write API

Der erste lokale Write-API-Contract ist implementiert, ohne Plattformzugriff:

- separate loopback-only REST-Schicht; das bestehende Dashboard bleibt read-only,
- ID-gebundene Content-/Pause-/Activate-/Delete-Routen gegen `MarkService` sowie den engen Create-Contract über `POST /api/write/ads`,
- Bearer-Authentisierung + explizite Capabilities + eigener default-off Write-Gate vor dem Service-Aufruf,
- persistente SQLite-Idempotency-Ledger: Claim vor dem Service-Aufruf, exakter Response-Replay nach Abschluss, Konflikt bei anderem Request und harter Retry-Block bei verbliebenem `in_progress`,
- sanitizierte `OperationReceipt`-/`CreateOperationReceipt`-Exposition mit `platform_retry_authorized=false`; Create-Authorization wird bis in den SQLite-Audit persistiert,
- bei Delete ist gemäß D-019 die authentifizierte, exakt pfad-ID-gebundene Nutzeroperation selbst die Freigabe; die stabile Idempotency-ID liefert intern die Audit-/Authorization-Referenz, sodass keine caller-supplied ID-Bestätigung oder Approval-Referenz erforderlich ist,
- der normale `POST /api/write/ads`-Create akzeptiert weiterhin nur Kategoriepfad, Titel, Beschreibung und ganzzahligen EUR-Festpreis; Media-Create läuft getrennt über `POST /api/write/media/stage` + `POST /api/write/media/ads` mit eigener `CREATE_MEDIA`-Capability und serverseitigem Media-Post-Read; Reply bleibt außerhalb der HTTP-Surface.

## Nicht-Ziele

- keine Umgehung von MFA/Captcha,
- keine Mutationen fremder Anzeigen oder fremder Accounts,
- keine versteckten Batch-Writes,
- keine automatische Wiederholung eines unklaren Plattformwrites,
- keine Secrets, Tokens oder App-Credentials im Repository,
- keine Behauptung einer Vertragsfreigabe allein aus technischer Machbarkeit.

## Konsequenz

Die Frage, **ob** eine schreibfähige Mark-API technisch möglich ist, ist mit **ja** beantwortet.

Die aktive technische Arbeit folgt D-010: `PrivateWebWriter`, persistenter CDP-Driver und der ownergebundene in-place Content-Smoke sind belegt; die Runtime-Komposition verbindet diese Flächen ohne Browser-Lifecycle im Core mit `MarkService`. Lifecycle (`ACTIVE`/`PAUSED`) und Delete sind über denselben fail-closed Web-UI-Pfad implementiert. Create ist als enger lokaler Contract samt eigener Reconciliation-Semantik implementiert und gegen die aktuelle UI read-only gebunden; ein Live-Publish wurde ausdrücklich nicht ausgeführt. Media-Staging, one-shot Media-Publish, Write-API-Komposition und der serverseitige read-only VIP-Galerie-Post-Read sind separat fail-closed implementiert. Damit kann ein content-bestätigter Media-Create im Produktpfad zusätzlich die vollständige öffentlich persistierte Galerie bestätigen, ohne die Owner-Snapshot-Domäne um provider-spezifische Bild-IDs zu erweitern; ein kontrollierter Live-Publish-Smoke bleibt dennoch separat offen. Zusätzlich verbindet der neue read-only PrivateWeb Runtime-Smoke den autoritativen Owner-Read mit ephemerer SQLite-Persistenz und dem loopback-only Dashboard/Analytics, ohne einen Writer aufzurufen. ProSellers bleibt das optionale offizielle zweite Backend; die privaten/mobile Reverse-Engineering-Pfade bleiben historische PoC-Evidenz.