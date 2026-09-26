# Architekturentscheidung Kleinanzeigen-MVP — 24.09.2026

Status: **angenommen für den MVP-Slice; Python 3.12+ und SQLite für den ersten Core-Slice festgelegt, Webframework/Deployment noch offen**

## Kontext

Der reale Ein-Anzeigen-Test mit Anzeigen-ID `3521676801` hat praktisch belegt:

- Kandidat A (`Second-Hand-Friends/kleinanzeigen-bot`): Sync/Download, in-place Inhaltsupdate mit stabiler ID, Reserve/Aktivieren.
- Kandidat B (`monkrel/kleinanzeigen-api`): eigene Anzeigen lesen, Pause/Aktivieren, Inbox/Conversation→`ad_id`, Delete.
- Management-Endpoint: Verkäuferstatus sowie `viewCount`, `watchCount`, `replies`.
- bot-ui-Stats: der reale Last-Ad-Delete-Fall lässt einen alten `.ad-stats.json`-Eintrag stehen.

Reale Robustheitslücken:

- Kandidat A: intermittierender XPath/CDP-Lookup beim Reserve-Pfad.
- Kandidat A: Detail-Sync einer `GIVE_AWAY`-Anzeige kann in der Preisextraktion mit `IndexError` scheitern.
- Kandidat B: `ad-status` ist im Rohpayload vorhanden, wird vom aktuellen `Listing`-Modell aber verworfen.
- Kandidat B: kein in-place Inhaltsupdate im geprüften Stand.
- bot-ui: lokale Aktivieren/Deaktivieren-Semantik ist nicht die Plattformsemantik.
- bot-ui: Fetchfehler/fehlende Session/echte Leere sind im Stats-Pfad nicht ausreichend diskriminiert.
- nach Delete verschwand die Anzeige sofort aus Besitzerlisten, während `get_my_ad(id)` und die öffentliche Detailseite mindestens drei Minuten weiter alte Detaildaten lieferten.

## Entscheidung

`mark-api` wird als **dünnes eigenes Orchestrierungsbackend mit getrennten Adaptern** aufgebaut.

### 1. Canonical Domain / Orchestration

`mark-api` besitzt das kanonische Domänenmodell, die Operationen und die Readback-Regeln. Externe Kandidatenmodelle werden nicht unverändert zum Produktmodell.

Kernobjekte:

- `AdSnapshot`
  - `ad_id`
  - `title`
  - `lifecycle_state`: `PENDING | ACTIVE | PAUSED | ABSENT | UNKNOWN`
  - `views: int | null`
  - `watch_count: int | null`
  - `reply_count: int | null`
  - `observed_at`
  - `source`
- `ReactionSnapshot`
  - `conversation_count`
  - `unique_buyer_count`
  - `inbound_message_count`
  - `observed_at`
- `OperationReceipt`
  - Operation
  - Ziel-`ad_id`
  - Precondition-Readback
  - Ergebnis
  - Postcondition-Readback
  - Fehler-/Unklarheitsstatus

Die fachliche Kennzahl „wie viele geschrieben haben“ wird **nicht** vorschnell auf eine dieser drei Metriken reduziert. Der Realtest mit genau einer Conversation, einem Gegenkontakt und einer eingehenden Nachricht beweist ihre technische Berechenbarkeit, unterscheidet die drei fachlichen Bedeutungen aber nicht.

### 2. Account/API-Adapter — primär monkrel

`monkrel/kleinanzeigen-api` ist der primäre Adapter für:

- Auth/PKCE,
- eigene Anzeigen lesen,
- Pause,
- Aktivieren,
- Delete,
- Conversations,
- Messages.

Begründung: ID-gebundene Pause/Aktivieren/Delete-Pfade waren im Realtest kompakt und reproduzierbar. Die MIT-Lizenz erleichtert eine direkte Adapterintegration.

Vor produktiver Nutzung:

- `ad-status` im Adapter explizit modellieren,
- DNS/Transportkonfiguration nicht hardcodieren,
- Read-Operationen auf mögliche Seiteneffekte prüfen; im E2E fiel `unread_count` nach Message-Abruf von 1 auf 0.

### 3. Management-Adapter — eigener Read-Adapter

Verkäuferstatus und Verkäufermetriken werden über einen **eigenen kleinen Management-Adapter** gelesen:

- Besitzerbestand / Presence,
- `state`,
- `viewCount`,
- `watchCount`,
- `replies`.

Der Adapter muss ein diskriminiertes Ergebnis liefern, mindestens:

- `success_nonempty`
- `success_empty`
- `unauthenticated`
- `http_error`
- `transport_error`
- `parse_error`

Fehlende Zähler bleiben `null`; sie werden nicht automatisch zu `0`.

Der bot-ui-Stats-Code wird dafür **nicht kopiert**, weil der reale Last-Ad-Test seine Early-Return-/Phantomdaten-Lücke bestätigt hat.

### 4. Browser-Update-Adapter — isolierter Prozess

Solange kein stabiler API-Pfad für das in-place Inhaltsupdate belegt ist, wird `Second-Hand-Friends/kleinanzeigen-bot` nur als **isolierter externer Browser-Adapter** verwendet für:

- Sync/Download, soweit benötigt,
- bestehende Anzeige in-place aktualisieren.

Die Prozessgrenze erhält:

- explizite Anzeigen-ID,
- temporären/isolierten Workspace,
- begrenzte Aktion,
- zwingenden Post-Readback über einen unabhängigen Adapter.

Kein AGPL-Quellcode wird in `mark-api` kopiert. Vor Distribution/Hosting muss die konkrete Lizenzkonstellation separat geprüft werden.

Reserve/Aktivieren sollen im Produkt primär über den API-Adapter laufen; der Browserpfad bleibt kein bevorzugter Zustandsadapter.

### 5. UI

`bkd3sign/kleinanzeigen-bot-ui` wird **nicht als Gesamtsystem übernommen**.

Es dient nur als Referenz für UX-/Datenbedürfnisse. Dashboard und API-Oberfläche werden gegen das eigene Domänenmodell gebaut.

Gründe:

- lokale vs. Remote-Aktivierungssemantik,
- real bestätigte Stats-Phantomdaten nach Last-Ad-Delete,
- Error-vs-empty-Lücke,
- unnötige Kopplung an den kompletten AGPL-Stack für einen kleineren benötigten Funktionsumfang.

## Readback-Regeln

### Lifecycle

Für `ACTIVE`/`PAUSED` gilt der Besitzer-/Managementstatus als maßgeblicher Readback.

### Delete

Ein Delete gilt für den operativen Bestand als bestätigt, wenn die Ziel-ID in **beiden** Besitzerlisten nicht mehr vorhanden ist:

- Management-/Web-Besitzerbestand,
- `my_ads()` des API-Adapters.

Nicht authoritative für Delete:

- `get_my_ad(id)`
- öffentliche Detail-URL mit HTTP 200

Im Realtest blieben beide Oberflächen mindestens drei Minuten mit alten Detaildaten erreichbar, obwohl die Besitzerlisten die Anzeige bereits entfernt hatten.

### Inbox

Conversation→Anzeige muss immer über die konkrete `ad_id` erfolgen.

Message-Fetches sind bis zur Klärung als **potenziell zustandsverändernd** zu behandeln, weil im Realtest `unread_count` nach dem Nachrichtenabruf von 1 auf 0 wechselte.

## Schreibsicherheit

Alle Plattform-Schreiboperationen sind im mark-api-Core standardmäßig deaktiviert.

Für jeden Write gelten:

1. explizite einzelne `ad_id`; keine impliziten Batch-Scopes,
2. frischer Precondition-Readback,
3. erwarteter Vorzustand,
4. genau ein Mutationsaufruf,
5. bei unklarem Outcome kein Blind-Retry,
6. unabhängiger Postcondition-Readback,
7. Audit-Eintrag ohne Secrets oder Nachrichtentext.

Delete verlangt zusätzlich eine explizite Freigabe für die konkrete Anzeigen-ID.

Create/Publish bleibt zunächst deaktiviert, weil im Realtest nur die manuell erstellte Testanzeige verwendet wurde. Ein Kandidaten-Publish wurde nicht remote belegt. Vor Freischaltung ist deshalb ein separater Real-Smoke-Test erforderlich.

## Authentifizierung und Secrets

- Login/MFA bleibt manuell.
- Browserprofile, Cookies und Tokens liegen lokal außerhalb des Repositorys.
- Passwörter sind kein Teil des mark-api-Konfigurationsmodells.
- Logs enthalten keine Cookies, Tokens, Telefonnummern oder Nachrichtentexte.
- Gegenkontakt-IDs werden für Metriken pseudonymisiert, soweit Klar-IDs fachlich nicht erforderlich sind.

## Erster Implementierungs-Slice

Für den ersten Core-Slice wird **Python 3.12+** verwendet.

Begründung:

- beide operativ relevanten Kandidaten sind Python-basiert,
- der Browser-Bot wird als Python-CLI-Prozess angebunden,
- der Mobile-API-Adapter kann ohne Sprachbrücke gekapselt werden,
- Domain-/Result-Contracts und SQLite-Persistenz benötigen noch kein Webframework.

Für den lokalen privaten MVP wird zunächst **SQLite** als persistente Ablage für normalisierte Snapshots verwendet. Das ist keine Festlegung auf das spätere Deploymentmodell.

Noch offen bleiben:

- Webframework,
- Dashboard-Frontend,
- Queue/Job-System,
- Deployment/Hosting.

## Produktions-/Vertragsgate

Diese Entscheidung ist eine technische MVP-Architektur. Sie stellt **nicht** fest, dass private/mobile API oder Browserautomation von Kleinanzeigen vertraglich freigegeben sind.

Vor produktivem Dauerbetrieb bleiben deshalb separat zu entscheiden:

- ausdrückliche Freigabe oder bewusste Risikoentscheidung für den Account,
- Rate-/Polling-Grenzen,
- Umgang mit Plattformänderungen,
- der separate Create/Publish-Realtest.

## Nicht gewählt

### Gesamtes bot-ui + Bot übernehmen

Nicht gewählt, weil der Realtest gerade in Stats- und Remote-State-Semantik zusätzliche Härtung erfordern würde und damit die vermeintliche Einfachheit reduziert.

### monkrel-only

Nicht gewählt, weil in-place Inhaltsupdate und Verkäufermetriken im geprüften Stand fehlen.

### Browser-Bot als alleiniger Kern

Nicht gewählt, weil reale Browser-/DOM-Robustheitsfehler auftraten und die API-Pfade für mehrere ID-gebundene Mutationen schlanker waren.

## Noch nicht entschieden

- konkretes Webframework,
- Dashboard-Frontend,
- Deploymentmodell,
- Queue/Job-System.

Diese Punkte werden erst gewählt, wenn der frameworkfreie Core-Slice und die ersten Adapter stabil sind.

## Implementierungsreihenfolge

1. Domain-/Result-Contracts und Adapterports.
2. monkrel-Adapter für Read/Pause/Activate/Delete/Inbox.
3. Management-Read-Adapter mit diskriminierten Fehlerzuständen.
4. persistierte Metrik-Snapshots ohne Null-Fälschung.
5. isolierter Browser-Update-Adapter mit unabhängigen Post-Readbacks.
6. API-/Dashboard-Oberfläche.
7. Härtung aus Issue #5.

## Konsequenz

Der Realtest beendet die Architektur-Sondierung. Weitere breite Toolrecherche ist nicht Teil des nächsten Schritts; ab jetzt wird entlang dieser Adaptergrenzen implementiert und gegen die dokumentierten Realtest-Regressionsfälle geprüft.

## Fortschreibung 26.09.2026 — HTTP-first Write-Pfad

Die Entscheidung wird für den nächsten Slice präzisiert:

- Ziel bleibt eine **eigene schreibfähige Mark-API**.
- Für private/eigene Anzeigen wird die Mobile-CAPI als primärer technischer Write-Kandidat ausgebaut.
- Für bestehende Inhaltsupdates bleibt der remote belegte Browser-Update-Adapter **operativ primär**, solange der private HTTP-in-place-Updatepfad nicht remote bestätigt ist. Nach erfolgreichem HTTP-Beleg wird er zum Fallback; das Zielbild bleibt HTTP-first.
- `MonkrelMobileApiAdapter` erhält den `AdContentUpdater`-Vertrag und delegiert Partial-Updates an einen HTTP-fähigen Client.
- Der konkrete 2026er private in-place-Request bleibt bis zum separaten Remote-Smoke fail-closed; dieser Commit führt keinen Plattform-Write aus.
- Für Power/Premium bleibt ein offizieller ProSellersAdapter parallel möglich und soll denselben Domainvertrag erfüllen.
- Danach wird die externe Mark-HTTP-Schicht mit Write-Endpunkten gegen den bestehenden `SafeWriteOrchestrator` gebaut.

Die technische Begründung und Capability-Matrix stehen in `docs/technical-write-capabilities-2026-09-26.md`.
