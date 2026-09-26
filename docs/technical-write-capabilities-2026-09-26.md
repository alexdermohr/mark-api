# Technische Write-Machbarkeit und Umsetzungsplan — 26.09.2026

Status: **technisch machbar; Umsetzung ist aktiver Projektplan. Vertrags-/Produktfreigabe bleibt ein separates Gate.**

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
      +-- BrowserBotAdapter        # nur Fallback, solange HTTP-Lücke besteht
      |
      +-- unabhängige Readback-Adapter
```

Der Mark-Core bleibt provider-neutral. Jeder Plattform-Write bleibt standardmäßig deaktiviert und läuft weiterhin über frischen Precondition-Read, genau einen Mutationsversuch und unabhängigen Post-Readback.

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
| Titel/Beschreibung in-place ändern | O | S/H | R | HTTP-Pfad ist nächster Beweis-Slice |
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

Zusammen mit der aktuellen Endpoint-Kartierung ist damit die technische Hypothese stark genug, den HTTP-in-place-Updatepfad als nächsten Implementierungsslice festzulegen. **Noch nicht behauptet wird**, dass der konkrete 2026er Request bereits remote mit unserem Account bestätigt ist.

## Im Projekt bereits remote belegt

Der bestehende PoC hat gegen eigene Testdaten bereits praktisch bestätigt:

- eigene Anzeige lesen,
- stabiler in-place Inhaltsupdate über den Browser-Bot,
- Pause und Aktivieren über Mobile/API mit unabhängigem Management-Readback,
- Delete mit Besitzerlisten-Readback,
- Management-Status und Verkäufermetriken,
- Inbox/Conversation mit konkreter Anzeigen-ID,
- Delete-Staleness: Besitzerlisten sind authoritative; Detail-GET/Public-URL können nachlaufen.

Diese Befunde bleiben gültige Regressionsevidenz. Der neue HTTP-first-Plan ersetzt sie nicht, sondern reduziert die Browserabhängigkeit.

## Aktive Architekturentscheidung ab 26.09.2026

1. **HTTP-first für Writes.** Für private/eigene Accounts wird die Mobile-CAPI als primärer technischer Write-Kandidat weiter ausgebaut.
2. **Browser nur Fallback.** Der vorhandene BrowserBotAdapter bleibt solange verfügbar, bis der entsprechende HTTP-Write remote bestätigt ist; er ist nicht mehr das Zielbild.
3. **Offizielle API parallel anschließbar.** Ein ProSellersAdapter soll denselben Mark-Domainvertrag bedienen.
4. **Keine Providersemantik im Mark-API-Vertrag.** Mark exponiert eigene Operationen; UUID/adId/Publication-Details bleiben im Adapter.
5. **Fail closed.** Nicht belegte Fähigkeiten bleiben deaktiviert; kein Blind-Retry bei unklarem Write-Ergebnis.
6. **Kein Live-Write in diesem Slice.** Zuerst Adaptervertrag und lokale Regressionstests, danach separater explizit gegateter Remote-Smoke.

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

### Slice B — nächster Remote-Beweis

Auf genau einer eigenen, ausdrücklich geeigneten Anzeige:

1. frischer Owner-Read,
2. vollständigen aktuellen Update-Request rekonstruieren,
3. genau eine reversible Inhaltsänderung per HTTP,
4. unabhängiger Management-Readback,
5. gleiche Anzeigen-ID + erwarteter neuer Inhalt,
6. bei Ambiguität kein Retry.

Erst nach diesem Beleg wird der Browser-Content-Updatepfad zum Fallback degradiert.

### Slice C — Create/Media/Reply

- privaten Create-Flow kontrolliert belegen,
- Bilderpfad vollständig kartieren und testen,
- Reply nur in einer bestehenden natürlichen Conversation testen; keine künstliche Testnachricht erzeugen.

### Slice D — Mark Write API

- REST-Schicht gegen den bestehenden `SafeWriteOrchestrator`,
- Authentisierung/Autorisierung für die Mark-API,
- Idempotency-/Operation-Receipt-Exposition,
- Writes weiterhin standardmäßig aus und capability-gated.

## Nicht-Ziele

- keine Umgehung von MFA/Captcha,
- keine Mutationen fremder Anzeigen oder fremder Accounts,
- keine versteckten Batch-Writes,
- keine automatische Wiederholung eines unklaren Plattformwrites,
- keine Secrets, Tokens oder App-Credentials im Repository,
- keine Behauptung einer Vertragsfreigabe allein aus technischer Machbarkeit.

## Konsequenz

Die Frage, **ob** eine schreibfähige Mark-API technisch möglich ist, ist mit **ja** beantwortet.

Die offene technische Arbeit ist jetzt konkret: den privaten HTTP-in-place-Updatepfad remote belegen, anschließend Create/Media/Reply vervollständigen und darauf die eigene Mark-Write-API setzen. Das ist der aktive Umsetzungsplan.
