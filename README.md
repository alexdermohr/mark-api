# mark-api

Öffentliche Projektdokumentation für die mit Mark besprochene Kleinanzeigen-Agent-/API-Integration.

## Status

**Core, Adapter, Dashboard und Analytics implementiert / technischer PoC abgeschlossen / zulässiger lokaler E-Mail-Import und lokaler E2E-Smoke implementiert / inoffizielle Plattformautomation nicht als Produktpfad freigegeben** — Stand: 28.09.2026.

Mark hat nach dem Telefonat die gewünschte Funktionalität schriftlich konkretisiert. Der MVP-Fokus liegt auf Anzeigenverwaltung, Synchronisation, Verkäufermetriken, Inbox/Interessenten, Dashboard und datenbasierter Auswertung. Externe Text-/Bildgenerierung war ursprünglich Teil des Wunsches, ist seit 24.09.2026 aber nicht mehr MVP-priorisiert.

## Technisch belegt

- Python-3.12+-Core mit diskriminierten Read-Ergebnissen, Safe-Write-Orchestrierung und SQLite-Snapshot-Persistenz.
- `ManagementReadAdapter` für den autoritativen Besitzerbestand sowie Verkäuferstatus, Views, Merker und Replies.
- `MonkrelMobileApiAdapter` als technisch belegter PoC-Adapter für eigene Anzeigen, ID-gebundene Pause/Aktivierung, Delete und Inbox/Conversation-Zuordnung; nach Abschluss von Issue #2 ist er kein Produktpfad für private Accounts.
- `BrowserBotAdapter` als technisch belegter PoC für Sync und in-place Inhaltsupdate; nach Abschluss von Issue #2 ist Browserautomation kein Produktpfad für private Accounts.
- `MarkService` als Application-Layer über den Capability-Adaptern.
- Read-only Dashboard/API auf Loopback sowie Analytics-Rankings auf explizit gespeicherten Klassifikationslabels.
- Der Ein-Anzeigen-Realtest vom 24.09.2026 belegt Sync, stabiles in-place Update, Pause/Aktivierung, Verkäufermetriken, positiven Inbox/adId-Fall und Delete mit unabhängigen Besitzerlisten-Readbacks.
- Am 25.09.2026 wurde der aktuell eingeloggte eigene Account read-only durch `ManagementReadAdapter -> EnrichedOwnerReader -> MarkService -> SnapshotStore` geführt: erfolgreicher leerer Besitzerbestand (`success_empty`), keine Plattformmutation.
- Plattformwrites sind im Core standardmäßig deaktiviert. Create/Publish ist weiterhin nicht für den Dauerbetrieb freigegeben.

## Lokale Klassifikationspflege

Die Analytics-Gruppierung verwendet explizite Labels für `image_type`, `city`, `text_type` und `title_type`. Das HTTP-Dashboard bleibt absichtlich vollständig read-only. Labels werden lokal und append-only über den separaten CLI-Entrypoint gepflegt:

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

Der CLI akzeptiert nur bereits im lokalen Store bekannte Anzeigen-IDs. Er greift weder auf Kleinanzeigen noch auf andere Netzwerkdienste zu und verändert keine Plattformdaten.

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

## Offene fachliche Punkte

Die Reaktionsdaten werden absichtlich getrennt als `conversation_count`, `unique_buyer_count` und `inbound_message_count` gespeichert. Welche dieser Größen fachlich „wie viele geschrieben haben“ meint, ist noch nicht festgelegt.

Ebenso ist noch keine fachlich bestätigte Zielfunktion für „beste Lösung“ definiert. Die Analytics-Schicht zeigt deshalb Rohmetriken und Rankings, ohne daraus Kausalität oder Qualität abzuleiten.

## Zulässige Betriebswege

[Issue #2](https://github.com/alexdermohr/mark-api/issues/2) ist abgeschlossen und trennt den erfolgreichen technischen PoC vom zulässigen Produktbetrieb:

- Für private Accounts wird keine inoffizielle Browser-/Mobile-/Private-HTTP-Automation als Produktpfad betrieben.
- Lokale Analytics dürfen aus vom Nutzer bereitgestellten offiziellen Daten entstehen, insbesondere Kleinanzeigen-E-Mail-Kopien und — sobald das konkrete Dateiformat belegt ist — offiziellen PRO-Statistikdownloads.
- Die offizielle ProSellers API bleibt ein optionaler zukünftiger Pfad ausschließlich für berechtigte professionelle Power-/Premium-Konten mit provisionierten Zugangsdaten. Für diesen Modus müssen API-Herkunft und Write-Authority der betroffenen Anzeigen separat gebunden werden.
- Die vorhandenen privaten/mobile/browserbasierten Adapter bleiben als technische PoC-Evidenz im Repository, begründen aber keine Betriebsfreigabe.
- Plattformwrites bleiben für den aktuellen privaten Produktpfad aus.

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

Der aktuelle Produktpfad für private Accounts ist lokal/read-only gegenüber der Plattform: user-provided offizielle Dateien und E-Mail-Kopien werden lokal verarbeitet. Die inoffiziellen PoC-Adapter bleiben technisch fail-closed erhalten, werden aber nicht als Produktintegration aktiviert. Ein späterer offizieller ProSellers-Writepfad benötigt eine neue, credentials- und entitlement-gebundene Freigabe mit eindeutig gebundener Write-Authority.
