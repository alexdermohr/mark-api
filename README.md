# mark-api

Öffentliche Projektdokumentation für die mit Mark besprochene Kleinanzeigen-Agent-/API-Integration.

## Status

**Core, Dashboard und Analytics implementiert / PrivateWebWriter + Runtime-Smoke real belegt / lokale Mark Write API mit opakem Media-Ref-Adapter implementiert** — Stand: 02.10.2026.

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
- Read-only Dashboard/API auf Loopback sowie Analytics-Rankings auf explizit gespeicherten Klassifikationslabels.
- Der Ein-Anzeigen-Realtest vom 24.09.2026 belegt Sync, stabiles in-place Update, Pause/Aktivierung, Verkäufermetriken, positiven Inbox/adId-Fall und Delete mit unabhängigen Besitzerlisten-Readbacks.
- Am 25.09.2026 wurde der aktuell eingeloggte eigene Account read-only durch `ManagementReadAdapter -> EnrichedOwnerReader -> MarkService -> SnapshotStore` geführt: erfolgreicher leerer Besitzerbestand (`success_empty`), keine Plattformmutation.
- Plattformwrites sind im Core standardmäßig deaktiviert. Der mediafreie Create/Publish-Vertrag, der getrennte media-aware Browser-Publish-Primitive und die read-only öffentliche Media-Persistenzverifikation sind lokal implementiert und regressionsgetestet; ein kontrollierter Live-Publish-Smoke und die spätere Produkt-/Betriebsfreigabe bleiben weiterhin offen.

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

Slice D führt eine **separate** loopback-only Write-Surface ein; das bestehende Dashboard bleibt vollständig read-only. Der normale Create-Vertrag bleibt mediafrei, zusätzlich ist ein getrennt gegateter Media-Create-Vertrag exponiert:

- `POST /api/write/ads` für den engen mediafreien Create-Vertrag aus `category_path`, `title`, `description` und `price_eur`,
- `POST /api/write/media/stage` für genau einen lokal ausgewählten JPEG-/PNG-/WebP-Bildkörper; Antwort ist ein intern erzeugter opaker `media_ref`,
- `POST /api/write/media/ads` für dieselben Create-Felder plus die vom Produktclient intern gehaltenen `media_refs`,
- `PATCH /api/write/ads/{id}` für Titel/Beschreibung,
- `POST /api/write/ads/{id}/pause`,
- `POST /api/write/ads/{id}/activate`,
- `DELETE /api/write/ads/{id}`; die authentifizierte, exakt ID-gebundene Nutzeroperation ist die Freigabe, die Audit-/Idempotenz-Referenz wird intern erzeugt.

Der Media-Create-Pfad benötigt die eigene Capability `CREATE_MEDIA`; `CREATE` allein autorisiert ihn nicht. `media_refs` sind ausschließlich opake, eindeutige ASCII-Handles der Form `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`. Die Create-HTTP-Schicht akzeptiert weiterhin keine Dateipfade. Der getrennte Staging-Endpunkt nimmt ausschließlich den vom Nutzer ausgewählten bounded Bildkörper plus sicheren Basename an, speichert ihn privat und gibt nur das Handle zurück. Wird `CREATE_MEDIA` aktiviert, erzeugt der normale High-Level-Builder den Handle-Store und Media-Service automatisch; explizite `media_bindings` bleiben nur ein Low-Level-/Test-Seam. Reply bleibt ein separates, nicht exponiertes Gate.

Die Write-Surface bindet ausschließlich an `127.0.0.1` und verlangt vor jedem Service-Aufruf einen Bearer-Token, eine passende Capability und `writes_enabled=true`. Der Core behält seinen eigenen unabhängigen `writes_enabled`-Gate, sodass die HTTP-Surface allein keine Plattformwrites freischaltet. Tokens werden nur zur Laufzeit injiziert und weder persistiert noch in `repr` ausgegeben.

Jede mutierende Anfrage benötigt einen `Idempotency-Key`. Vor dem Service-Aufruf wird ein Request-Fingerprint atomar in derselben SQLite-Datenbank als `in_progress` gespeichert; beim Media-Create gehören die normalisierten `media_refs` zum Fingerprint. Ein identischer bereits abgeschlossener Request replayt ausschließlich die gespeicherte HTTP-Response; ein abweichender Request mit demselben Key wird abgewiesen. Bleibt nach einem Prozessabbruch ein `in_progress`-Eintrag zurück, wird **kein** erneuter Plattformversuch autorisiert. Damit überlebt die No-Blind-Retry-Regel auch einen HTTP-Prozessneustart.

Die HTTP-Antwort exponiert den sanitisierten `OperationReceipt` beziehungsweise `CreateOperationReceipt` und setzt immer `platform_retry_authorized=false`. Für den mediafreien Create sowie die ID-gebundenen Writes bleibt `CONFIRMED` = 200, `PRECONDITION_FAILED` = 409 und `AMBIGUOUS` = 202. Beim Media-Create bleibt `CreateOperationReceipt.CONFIRMED` die Anzeigen-/Content-Bestätigung; der High-Level-PrivateWeb-Builder komponiert standardmäßig den öffentlichen read-only `PrivateWebPublicMediaPersistenceVerifier`; ein explizit injizierter `PrivateWebMediaPersistenceVerifier` bleibt als Low-Level-/Test-Seam zulässig. Der Verifier prüft exakt die vor dem ersten Owner-Pre-Read stabilisierten Media-Quellen gegen die target-bound VIP-Galerie. Nur wenn Create/Content **und** dieser Media-Post-Read den erwarteten Satz exakt bestätigen, liefert der Media-Pfad HTTP 200 mit `media_persistence_confirmed=true` und `media_post_read_status=confirmed`. Echte vollständige Abweichung ergibt `MISMATCH`; HTTP-, Transport-, Parser-, Decoder- oder Timeout-Unsicherheit bleibt `UNKNOWN`. Beides hält einen ansonsten content-bestätigten Media-Create bei HTTP 202/`media_persistence_confirmed=false`; `PRECONDITION_FAILED` bleibt HTTP 409. Der Media-Post-Read erzeugt keine zusätzliche Retry-Autorität und hebt einen bestehenden Submit-UNKNOWN-Fence nicht auf. Die konkrete PrivateWeb-Komposition serialisiert sämtliche `MarkService`-Writes, Media-Create und Media-Reconciliation über ein gemeinsames Operations-Lock und drainiert akzeptierte HTTP-Handler vor Runtime-Cleanup. Solange `PrivateWebMediaCreateRuntime.reconciliation_required` wahr ist, verweigert dieselbe Komposition jeden weiteren Core- oder Media-Write noch vor dem Service-Delegate; erst erfolgreiche `reconcile_media_submit()`-Beobachtung hebt diesen Fence wieder auf. `build_private_web_write_api_runtime(...)` besitzt `PrivateWebContentRuntime`, optional `PrivateWebMediaCreateRuntime`, `MarkService`, optional `PrivateWebMediaCreateService`, bei `CREATE_MEDIA` standardmäßig den runtime-eigenen `PrivateWebMediaHandleStore` und einen loopback-only `LoopbackWriteApiServer`; eine unabhängige Confirmation-Runtime wird **nicht** automatisch erzeugt, weil im aktiven Produktpfad keine zweite unabhängige Inventarquelle existiert. Eine separat etablierte `PrivateWebInventoryRuntime` kann weiterhin optional über `confirmation_runtime=` injiziert werden, ist für den normalen Create-/Delete-Pfad aber nicht erforderlich: ohne sie verwendet das Produkt zeitlich getrennte frische Owner-Inventarbeobachtungen; Create verlangt zusätzlich den target-spezifischen Content-/Detail-Read, Delete eine zweite frische Abwesenheitsbeobachtung nach exakt einem Submit. HTTP-`WriteApiAccess.writes_enabled`, `core_writes_enabled` und `media_writes_enabled` bleiben unabhängige default-off Gates. Der Browserprozess bleibt caller-owned; dieser Slice führt keinen realen Kleinanzeigen-Plattformwrite aus.

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
