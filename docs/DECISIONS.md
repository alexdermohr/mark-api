# Entscheidungen

## D-001 — Projektregistratur

**Entscheidung:** `alexdermohr/mark-api` ist die kanonische Registratur für dieses Projekt.

**Folgen:**
- Aufgaben und offene Arbeit werden als GitHub Issues hier geführt.
- Entscheidungen und Gesprächsevidenz liegen im Repository.
- Für `mark-api` werden keine neuen Bureau-Einträge angelegt.

## D-002 — Öffentliches Repository, private Rohquellen

**Entscheidung:** Das Repository ist öffentlich; die Audiodatei selbst bleibt privat außerhalb des Repositorys.

**Folgen:**
- Transkription und sachlich erforderliche Projektinformation dürfen dokumentiert werden.
- Zugangsdaten, Tokens, Telefonnummern und unnötige personenbezogene Daten werden nicht committed.

## D-003 — Noch keine technische Festlegung

**Entscheidung:** Der Integrationsweg wird erst nach Marks schriftlicher Spezifikation und einer Prüfung der tatsächlich verfügbaren Kleinanzeigen-Schnittstellen festgelegt.

**Begründung:** Das Telefonat benennt das Zielsystem, aber nicht die Agentenaktionen. Eine API-, Browser- oder Modellarchitektur wäre derzeit vorzeitig.

## D-004 — Schriftlicher Funktionsscope liegt vor

**Entscheidung:** Die schriftliche Wunschvorstellung vom 23.09.2026 ist ab jetzt die maßgebliche fachliche Konkretisierung für den Funktionsumfang.

**Belegt sind:** Anzeigen erstellen/löschen/verwalten, Titel- und Beschreibungsgenerierung per Prompt, Bildgenerierung mit automatischer Anzeigenverwendung, Dashboard, Datenauswertung zu Aufrufen und zur noch zu definierenden Kennzahl „wie viele geschrieben haben“, Grafiken/Top-Listen nach Bild-Typ/Stadt/Text-Typ/Titel-Typ sowie datenbasierte Optimierungsvorschläge.

**Nicht entschieden:** genaue Einzeloperationen unter „verwalten“, Metrikdefinitionen, Freigaberegeln, Hosting, Modellarchitektur und Kleinanzeigen-Integrationsweg.

**Folge:** Issue #1 bleibt bis zur Klärung der noch offenen Detail-Akzeptanzpunkte offen; die technische Discovery in #2 kann parallel auf Basis des jetzt belegten Funktionsscopes beginnen.


## D-005 — MVP-Fokus auf Automation und Messbarkeit

**Entscheidung:** Externe Text- und Bildgenerierung werden ab 24.09.2026 nicht als MVP-Schwerpunkt behandelt.

**Begründung:** Der Auftraggeber bewertet insbesondere zusätzliche Textgenerierung als geringen Zusatznutzen, weil Kleinanzeigen selbst Textunterstützung anbietet. Der knappe technische Hebel liegt stattdessen bei Anzeigen-CRUD, Synchronisation, Besucher-/Watchlist-Daten, Inbox/Interessenten und darauf aufbauender Analyse.

**Folgen:**
- Die ursprünglich genannten Generierungswünsche bleiben als historische Anforderung dokumentiert, sind aber für den MVP depriorisiert.
- Die technische Discovery priorisiert vorhandene Lösungen für CRUD, Messaging und Statistik.
- Noch keine endgültige Architekturwahl: `kleinanzeigen-bot-ui`/`kleinanzeigen-bot` und `monkrel/kleinanzeigen-api` werden als PoC-Kandidaten geprüft.
- Account-/ToS- und Wartungsrisiken inoffizieller Schnittstellen bleiben ein Gate.

## D-006 — Dünne eigene Schicht mit Capability-Adaptern

**Entscheidung:** Für den privaten technischen MVP wird kein geprüfter Fremdkandidat als vollständiger Anwendungskern übernommen. `mark-api` wird als dünne eigene Python-Schicht mit capability-spezifischen Adaptern aufgebaut.

**Primäre Zuordnung:**
- private/mobile API über einen austauschbaren `MobileApiAdapter` für eigene Anzeigen, Pause/Aktivierung, Delete und Inbox/Messaging,
- eigener read-only `ManagementReadAdapter` für Verkäuferstatus, Views, Merker und Replies,
- `Second-Hand-Friends/kleinanzeigen-bot` nur als isolierter externer Prozess für in-place Inhaltsupdate und erst nach separatem Real-Smoke-Test ggf. Create/Publish,
- eigenes normalisiertes SQLite-Datenmodell und eigenes kleines Dashboard statt Übernahme von `kleinanzeigen-bot-ui`.

**Begründung:** Der Realtest auf Anzeigen-ID `3521676801` zeigte:
- monkrel war für ID-gebundene Pause/Aktivierung/Delete und Inbox kompakt und reproduzierbar,
- der Browser-Bot konnte als einziger Kandidat das bestehende Inserat mit stabiler ID in-place ändern,
- Browserpfade zeigten reale XPath/CDP-/Parser-Robustheitsprobleme,
- monkrel allein hat kein belegtes in-place Update und modelliert Status/Verkäufermetriken unvollständig,
- die vollständige UI bringt unnötige AGPL-/Browserkomplexität und hat real die Last-Ad-Stats-Lücke bestätigt.

**Grenzen:**
- keine AGPL-Codeübernahme in den mark-api-Kern ohne separate Lizenzentscheidung,
- Schreiboperationen standardmäßig deaktiviert und immer an eine einzelne bekannte Anzeigen-ID gebunden,
- Create/Publish bleibt bis zu einem eigenen Remote-Smoke-Test deaktiviert,
- diese technische Entscheidung ist keine Feststellung einer vertraglichen Freigabe der inoffiziellen Schnittstellen.

Details: `docs/architecture-decision-2026-09-24.md` und `docs/poc-2026-09-24.md`.

## D-007 — Python-Core und SQLite für den ersten Slice

**Entscheidung:** Der erste Implementierungs-Slice wird als Python-3.12+-Package mit frameworkfreien Domain-/Adapter-Contracts und SQLite-Persistenz umgesetzt.

**Begründung:** Beide operativ relevanten Adapterpfade sind Python-basiert. Python vermeidet für den ersten Slice eine zusätzliche Sprachbrücke; SQLite deckt den privaten Single-Account-MVP und append-only Metrik-Snapshots ohne Infrastrukturvorgriff ab.

**Nicht entschieden:** Webframework, Dashboard-Frontend, Queue/Job-System und späteres Deploymentmodell.

**Folge:** Der erste Code-Slice implementiert Domänenmodelle, diskriminierte Read-Ergebnisse, Adapterports und Snapshot-Persistenz. Plattformwrites bleiben standardmäßig deaktiviert.

## D-008 — Produktbetrieb nur über offizielle oder nutzerbereitgestellte Eingaben

**Entscheidung:** Der erfolgreiche private/mobile/browserbasierte PoC bleibt technische Machbarkeitsevidenz, wird aber nicht als Produktpfad für private Kleinanzeigen-Accounts betrieben. Der zulässige nächste Produktpfad ist lokal und plattformseitig read-only: vom Nutzer bereitgestellte offizielle Daten und im eigenen Postfach empfangene Kleinanzeigen-E-Mail-Kopien.

**Begründung:** Issue #2 hat den Integrations-Gate abgeschlossen. Für private Accounts liegt kein freigegebener offizieller Automationspfad für die im PoC genutzten privaten/mobile/browserbasierten Mechanismen vor. Die offizielle ProSellers API ist ein anderer Betriebsmodus für berechtigte professionelle Power-/Premium-Konten und setzt provisionierte Zugangsdaten sowie eine klare API-Write-Authority voraus.

**Folgen:**
- Private/mobile Reverse-Engineering-HTTP und der historische `BrowserBotAdapter` bleiben als technische PoC-Adapter im Repository und werden nicht als automatische Produktintegration aktiviert. Der davon getrennte normale Web-UI-Writer wird ab D-010 separat gegated.
- Kleinanzeigen-E-Mail-Kopien dürfen lokal importiert werden. Der Import speichert nur Anzeigen-ID, Conversation-ID, Provider-Message-ID, Zeitstempel und Quelle; Nachrichtentext und Personennamen werden nicht persistiert. Die Header-/Body-Prüfung ist eine Format-/Konsistenzprüfung und keine kryptographische Absenderauthentifizierung; der Nutzer stellt die rohe Nachricht aus dem eigenen Postfach bereit.
- Aus E-Mail-Kopien werden getrennte read-only Projektionen für Conversation- und Inbound-Message-Zähler abgeleitet. In Analytics heißen sie source-explizit `email_conversation_count` und `email_inbound_message_count`; bestehende ReactionSnapshot-Metriken werden nicht ersetzt oder additiv vermischt. `unique_buyer_count` wird aus E-Mail-Kopien nicht erfunden.
- Ein Import offizieller PRO-Statistikdownloads wird erst implementiert, wenn ein reales Exportformat als Contract-Evidenz vorliegt.
- Der lokale E2E-Smoke darf ausschließlich nutzerbereitgestellte lokale Eingaben, eine ephemere SQLite-Datenbank und die loopback-only read-only Dashboard/API-Surface verbinden. Er konstruiert keinen Plattformadapter, aktiviert keine Plattformwrites und ist keine Freigabe für private/mobile/browserbasierte Automation.
- Ein späterer ProSellers-Adapter benötigt ein eigenes Credentials-/Entitlement-/Write-Authority-Gate und bleibt bis dahin deaktiviert.

## D-009 — ProSellers-Netzwerkpfad nur nach expliziter lokaler Admission

**Entscheidung:** Ein zukünftiger offizieller ProSellers-Netzwerkclient darf erst hinter einer lokalen fail-closed Admission konstruiert werden. Die Admission ist nur positiv, wenn der Account ausdrücklich professionell ist, das Paket Power oder Premium ist, das konkrete API-Entitlement bestätigt wurde, nichtleere provisionierte `client_id`/`client_secret` vorliegen und die Write-Authority `api_originated_only` ist.

**Begründung:** Die am 28.09.2026 erneut gelesene offizielle Kleinanzeigen-Dokumentation beschreibt den OAuth-Client-Credentials-Flow mit `client_id`, `client_secret`, `grant_type=client_credentials` und Audience `consumer-goods-api`; die ProSellers API ist dort ausdrücklich nur für professionelle Power-/Premium-Nutzer verfügbar. Das Vorhandensein eines Paketnamens oder beliebiger Credential-Strings beweist aber weder ein konkretes Entitlement noch die geplante Write-Authority.

**Folgen:**
- `mark_api.prosellers` ist vollständig lokal und netzwerkfrei; der Slice führt weder Token- noch Goods-API-Requests aus.
- Private Accounts, Basic, fehlendes/unklares Entitlement, fehlende Credentials und jede andere Write-Authority werden deterministisch abgewiesen.
- Admission-Diagnostik enthält nur Reason-Codes; Credentials erscheinen nicht in `repr`, Decision-Payloads oder Exceptions.
- API-originierte Listings dürfen später nur unter expliziter API-Write-Authority geschrieben werden; Mischbetrieb mit manuellen Web-Writes für dieselben API-originierte Listings wird nicht freigegeben.
- OAuth- und Goods-Transport bleiben ein separater Slice und müssen die Admission erneut als Konstruktor-/Factory-Gate erzwingen.

## D-010 — Privater Writer über die normale nutzer-authentifizierte Weboberfläche

**Entscheidung:** Für eigene private Kleinanzeigen-Accounts wird ein eigener `PrivateWebWriter` als Produkt-Writepfad aufgebaut. Er steuert ausschließlich die normale Kleinanzeigen-Weboberfläche in einer vom Nutzer selbst authentifizierten Browser-Sitzung. D-010 supersediert D-008 nur insoweit, als D-008 sämtliche Browserautomation pauschal dem PoC zuordnete; private/mobile Reverse-Engineering-HTTP sowie der historische `BrowserBotAdapter` bleiben weiterhin PoC und sind keine automatischen Fallbacks.

**Begründung:** Der Auftraggeber hat am 28.09.2026 klargestellt, dass echte Schreiboperationen auch ohne PRO-Konto zentraler Projektauftrag sind. Die normale Weboberfläche stellt diese Operationen dem privaten Kontoinhaber selbst bereit. Der neue Pfad bindet deshalb die vorhandenen Core-Sicherheitsregeln an eine nutzerkontrollierte Browser-Sitzung, ohne Passwörter, MFA oder Anti-Automation-Challenges zu automatisieren oder zu umgehen.

**Folgen:**
- jede Mutation betrifft genau eine explizite, frisch owner-verifizierte Anzeigen-ID des eigenen Kontos,
- Login, MFA, CAPTCHA und sonstige Sicherheitschallenges sind harte Stop-Zustände; `mark-api` speichert keine Passwörter oder MFA-Codes,
- der erste Content-Writer öffnet genau den Editor der Ziel-ID, prüft vor jeder Änderung `ready` + exakte ID, ändert nur angeforderte Felder und prüft vor einem einzigen Submit nochmals ID sowie den vollständigen erwarteten Editorzustand,
- Browser-/Providerfehler werden in Receipts nur als sanitizierte Fehlerklasse/Stage geführt; kein Cookie, Token, URL-Fragment oder Anzeigeninhalt wird als Fehlertext persistiert,
- der bestehende `SafeWriteOrchestrator` bleibt alleinige Write-Semantik: frischer Owner-Pre-Read, genau ein Mutationsversuch, kein Blind-Retry und Post-Readback,
- der Contract selbst ist browserdriver-neutral. Ein konkreter persistenter Browserdriver und ein eigener, reversibler Live-Smoke sind separate Gates vor Aktivierung,
- Create, Bilder, Pause/Aktivieren und Delete werden erst nach dem Content-Update-Grundpfad einzeln erweitert; Delete behält zusätzlich seine explizite ID-Freigabe,
- ProSellers bleibt ein separates optionales Backend für tatsächlich berechtigte Power/Premium-Konten.

## D-011 — Mark Write API bleibt getrennt, loopback-only und crash-idempotent

**Entscheidung:** Die schreibfähige Mark-HTTP-Surface wird nicht in das read-only Dashboard eingebaut. Sie läuft als separater loopback-only Server und exponiert im ersten Slice ausschließlich die bereits gehärteten ID-gebundenen `MarkService`-Operationen Content-Update, Pause, Activate und Delete. Create, Media-Publish und Reply bleiben separate spätere Gates.

**Begründung:** Eine Erweiterung des Dashboard-Handlers um Writes würde eine bereits belegte Read-only-Sicherheitsgrenze aufweichen. Reine In-Memory-Idempotenz wäre ebenfalls unzureichend: Nach einem Prozessabbruch zwischen möglichem Plattforminput und HTTP-Response könnte ein Neustart denselben externen Request erneut ausführen. Die SQLite-Ledger bindet deshalb den Idempotency-Key vor dem Service-Aufruf an einen Request-Fingerprint.

**Folgen:**
- der Write-Server bindet ausschließlich an `127.0.0.1`,
- Bearer-Authentisierung, explizite Capability und ein eigener `writes_enabled`-Gate werden vor jedem `MarkService`-Aufruf geprüft; der Core behält zusätzlich seinen unabhängigen Write-Gate,
- Laufzeit-Tokens werden nicht persistiert und in Konfigurations-`repr` verborgen,
- jede Mutation benötigt einen syntaktisch validierten `Idempotency-Key`,
- gleicher Key + gleicher abgeschlossener Request gibt nur die gespeicherte Response zurück und ruft keinen Writer erneut auf,
- gleicher Key + anderer Request wird fail-closed abgewiesen,
- ein persistiertes `in_progress` nach Crash bleibt ein harter Retry-Blocker und verlangt Reconciliation statt Wiederholung,
- Delete benötigt zusätzlich eine explizite, zur Pfad-ID identische `confirm_ad_id` sowie eine Approval-Referenz,
- Operation-Receipts werden sanitisiert über HTTP exponiert; ein `AMBIGUOUS`-Outcome erteilt ausdrücklich keine Retry-Autorisierung,
- dieser Slice konstruiert keine Browser-/PrivateWeb-Runtime und führt keinen Kleinanzeigen-Plattformwrite aus.
## D-012 — Create wird als eigener Mark-Write-API-Contract exponiert

**Entscheidung:** Die lokale loopback-only Mark Write API erweitert D-011 um `POST /api/write/ads`. Exponiert wird ausschließlich der bereits gehärtete enge `AdCreateRequest` mit Kategoriepfad, Titel, Beschreibung und ganzzahligem EUR-Festpreis. Media-Publish und Reply bleiben weiterhin separate Gates.

**Begründung:** Der Core-Createpfad besitzt bereits eine strengere Reconciliation als ID-gebundene Writes: zwei unabhängige Inventar-Pre-/Post-Reads müssen exakt dieselbe einzelne neue Anzeigen-ID erkennen, anschließend muss ein target-bound Content-Read Titel und Beschreibung bestätigen. Diese Semantik kann über dieselbe crash-idempotente HTTP-Ledger exponiert werden, ohne einen Browser zu starten oder einen Plattformwrite im Implementierungs-Slice auszuführen.

**Folgen:**
- Create benötigt eine eigene `CREATE`-Capability sowie Bearer-Authentisierung und den API-`writes_enabled`-Gate vor jeder Body-/Service-Ausführung,
- der HTTP-Payload wird strikt auf `category_path`, `title`, `description` und `price_eur` begrenzt und zuerst durch `AdCreateRequest` normalisiert,
- die normalisierte Create-Semantik fließt in den persistenten Idempotency-Fingerprint; derselbe semantische Request mit demselben Key wird nach Neustart nur replayt,
- `CreateOperationReceipt` trägt `authorization_by` und `authorization_reference`; beide Felder werden additiv und rückwärtskompatibel in SQLite migriert und persistiert,
- der HTTP-Layer akzeptiert nur einen exakt an Operation, Principal und Idempotency-Referenz gebundenen Create-Receipt,
- `created_ad_id` ist ausschließlich für `CONFIRMED` zulässig; `AMBIGUOUS` und `PRECONDITION_FAILED` exponieren bewusst keine nur vermutete neue Anzeigen-ID,
- `AMBIGUOUS` und Ausführungsfehler erteilen weiterhin keine Retry-Autorisierung,
- Media-Daten sind im Create-HTTP-Vertrag nicht zulässig; ein späterer Media-Publish braucht weiterhin einen eigenen Contract, eigene Freigabe und Reconciliation.

## D-013 — Media-Publish bleibt ein separater, noch nicht domain-bestätigter PrivateWeb-Contract (historischer Zwischenstand)

**Fortschreibung:** D-014 ersetzt ausschließlich die damalige Aussage, dass die lokale Write API mediafrei bleibt. Die hier festgehaltene Einschränkung bleibt bestehen: Der vorhandene `CreateOperationReceipt` bestätigt keine serverseitige Medienpersistenz.

**Entscheidung:** Für einen Create mit expliziten lokalen Medien wird ein eigener `PrivateWebCreateMediaWriter` oberhalb des bereits gehärteten Media-Stagings eingeführt. Der normale `PrivateWebCreateWriter`, `AdCreateRequest`, `MarkService` und die lokale Mark Write API bleiben mediafrei. Der neue Pfad darf nach exakter Form- und `FileList`-Revalidierung genau einen browser-level Publish-Versuch auf demselben gebundenen File-Input-Handle auslösen.

**Begründung:** Der vorhandene Create-Contract ist absichtlich eng und bereits separat über Inventar- und Content-Readbacks reconciled. Media-Staging besitzt eigene lokale Stabilitäts- und No-Blind-Retry-Invarianten. Eine Vermischung würde den bestätigten mediafreien Contract rückwirkend aufweichen. Gleichzeitig enthält der aktuelle autoritative `AdSnapshot` keine Medienidentität; deshalb kann ein erfolgreicher Listing-Readback derzeit nicht beweisen, dass genau die erwarteten Medien serverseitig hochgeladen und persistiert wurden.

**Folgen:**
- lokale Media-Dateien werden vor dem ersten Browserzugriff aus validierten Deskriptoren in private stabile Kopien überführt,
- der bestehende Create-Writer darf die Form für den Media-Pfad nur vorbereiten und unmittelbar revalidieren; sein normaler mediafreier Submit bleibt unverändert,
- Media-Staging muss die erwartete `FileList` exakt read-backen; der Publish-Pfad bindet danach weiterhin dasselbe CDP-Objekthandle und prüft Dateiname und Größe erneut,
- der Media-Publish ist one-shot: sobald der browser-level Publish-Input beginnt, ist ein Fehler `AMBIGUOUS`/unknown und autorisiert keinen Blind-Retry,
- der neue Contract wird noch nicht an `MarkService`, `SafeWriteOrchestrator` oder `/api/write` angeschlossen; insbesondere gibt es kein `CONFIRMED`-Media-Receipt,
- ein späterer höherer Media-Write-Contract benötigt zuerst einen autoritativen media-aware Post-Read beziehungsweise eine andere belastbare Medien-Reconciliation,
- dieser Implementierungs-Slice führt keinen realen Kleinanzeigen-Publish oder sonstigen Plattformwrite aus.

## D-014 — Media-Create erhält eine eigene opake Write-API-Surface ohne Dateipfadautorität

**Entscheidung:** Die lokale loopback-only Mark Write API exponiert zusätzlich `POST /api/write/media/ads` als getrennten Media-Create-Vertrag. Dieser Pfad benötigt die eigene Capability `CREATE_MEDIA`; die bestehende `CREATE`-Capability autorisiert ihn nicht. Der HTTP-Payload verwendet die normalen `AdCreateRequest`-Felder plus eine nicht leere, reihenfolgeerhaltende Liste eindeutiger opaker `media_refs` mit der ASCII-Grammatik `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`.

**Begründung:** Die HTTP-Schicht soll weder absolute oder relative Dateipfade noch Bytes annehmen und damit keine allgemeine lokale Datei-Leseautorität erhalten. Die Referenzauflösung bleibt deshalb hinter einem separaten Media-Service-Port. `write_api.py` kennt weder `PrivateWebMediaSource` noch eine Dateisystemauflösung. Wenn `CREATE_MEDIA` konfiguriert ist, aber kein Media-Service bereitsteht, schlägt der Serveraufbau fail-closed fehl.

**Folgen:**
- der normale `POST /api/write/ads`-Create bleibt unverändert mediafrei,
- die normalisierten `media_refs` sind Bestandteil des persistenten Idempotency-Fingerprints; derselbe Key mit anderen Refs ergibt einen Konflikt statt einer erneuten Ausführung,
- die Write API gibt `media_refs` nicht zurück und setzt weiterhin `platform_retry_authorized=false`,
- D-014 lieferte für `CONFIRMED` wie für `AMBIGUOUS` zunächst HTTP 202 mit `media_persistence_confirmed=false`; diese Response-Aussage wird durch D-018 fortgeschrieben: nur ein zusätzlicher exakter autoritativer Media-Post-Read hebt einen content-bestätigten Media-Create auf HTTP 200/`media_persistence_confirmed=true`, sonst bleibt er HTTP 202; `PRECONDITION_FAILED` bleibt HTTP 409,
- dieser Slice stellt noch keinen Resolver von `media_refs` zu lokalen `PrivateWebMediaSource`-Objekten und keine konkrete PrivateWeb-Media-Service-Komposition bereit,
- ein späterer Resolver muss Refs an vorher explizit zugelassene und stabilisierte Media-Artefakte binden; beliebiges Server-Filesystem-Lesen bleibt außerhalb des HTTP-Contracts,
- dieser Slice führt keinen realen Kleinanzeigen-Plattformwrite aus.

## D-015 — Opaque Media-Refs werden pro Create-Versuch stabilisiert und an die bestehende Media-Runtime gebunden

**Fortschreibung:** Diese Entscheidung ersetzt ausschließlich die D-014-Aussage, dass noch kein Resolver und keine konkrete PrivateWeb-Media-Service-Komposition existieren. Die D-013/D-014-Grenze bleibt unverändert: `CreateOperationReceipt.CONFIRMED` bestätigt weiterhin keine serverseitige Medienpersistenz.

**Entscheidung:** `PrivateWebMediaRefResolver` ist eine kleine immutable Kopie explizit zugelassener `media_ref -> PrivateWebMediaSource`-Bindings ohne CRUD- oder Dateipfadauflösung aus HTTP-Input. `PrivateWebMediaCreateService` löst die angeforderten Refs genau einmal innerhalb seines serialisierten Create-Versuchs auf und kopiert die ausgewählten Quellen mit der bestehenden Deskriptor-/TOCTOU-Prüfung in private, service-eigene Dateien **vor dem ersten Owner-Pre-Read**. Diese Kopien bleiben bis zum Abschluss aller Post-Reads am Leben; der Browserwriter bleibt ausschließlich `PrivateWebMediaCreateRuntime.bind_create_writer()`.

**Begründung:** Eine späte Übersetzung `ref -> Pfad` würde erlauben, dass sich die Bytes während der Pre-Read-Phase ändern. Die per-Attempt-Stabilisierung bindet den autorisierten/idempotenten Versuch stattdessen vor jeder Browsermutation an konkrete validierte Bytes, ohne eine langlebige allgemeine Registry einzuführen.

**Folgen:**
- dieselbe ASCII-Grammatik wie in der Write API; unbekannte, ungültige, leere oder doppelte Ref-Tupel werden vor Owner-/Browser-Reads als lokales `PRECONDITION_FAILED` ohne Writer-Aufruf klassifiziert,
- Ref-Reihenfolge bleibt erhalten; Änderungen an Originaldateien nach Service-Eintritt können die für diesen Versuch gewählten Bytes nicht mehr verändern,
- `PrivateWebMediaCreateService` serialisiert seinen vollständigen Pre-/Write-/Post-Read-Zyklus; für Requests derselben `LoopbackWriteApiServer`-Instanz serialisiert zusätzlich ein gemeinsamer Create-Lock mediafreien und media-aware Create nach dem persistenten Idempotency-Claim, während Update, Pause, Activate und Delete unabhängig bleiben,
- die bestehende Runtime behält ihre one-shot-, UNKNOWN- und Reconciliation-Fences; der Service erzeugt keine zusätzliche Retry-Autorität,
- `write_api.py` bleibt path-frei; persistente HTTP-Idempotenz bleibt vorgelagert und ein abgeschlossener Replay ruft den Media-Service nicht erneut auf,
- ohne media-aware autoritativen Post-Read bleibt `media_persistence_confirmed=false`,
- dieser Slice führt keinen realen Kleinanzeigen-Plattformwrite aus.
## D-016 — PrivateWeb Write API wird als explizites loopback-only Runtime-Bundle komponiert

**Fortschreibung:** D-016 ändert keine Write-Semantik aus D-010 bis D-015. Insbesondere bleibt media_persistence_confirmed=false, solange kein media-aware autoritativer Post-Read existiert, und ein Browser-/Media-UNKNOWN erteilt keine Retry-Autorisierung.

**Entscheidung:** build_private_web_write_api_runtime(...) ist die konkrete Produktionskomposition für einen bereits laufenden, vom Nutzer selbst authentifizierten CDP-Worker. Das resultierende PrivateWebWriteApiRuntime besitzt den PrivateWebContentRuntime, optional den PrivateWebMediaCreateRuntime, den daraus komponierten MarkService, optional PrivateWebMediaCreateService und genau einen LoopbackWriteApiServer. Eine unabhängige Create/Delete-Confirmation-Runtime wird nicht aus einer zweiten Instanz derselben Management-Quelle konstruiert. Sowohl der High-Level-Builder über `confirmation_runtime=` als auch die Low-Level-Komposition dürfen eine vom Caller bereits separat etablierte `PrivateWebInventoryRuntime` nur dann explizit übernehmen, wenn deren tatsächliche Unabhängigkeit außerhalb dieser Builder begründet ist; kein Builder erzeugt eine solche zweite Quelle automatisch. Der Browserprozess selbst bleibt außerhalb des Bundles und wird weder gestartet noch authentifiziert oder beendet.

**Begründung:** Die zuvor einzeln gehärteten Contracts waren nur testweise zusammensteckbar. Eine zentrale Komposition macht Ownership, Gate-Trennung und Shutdown-Semantik explizit, ohne einen CLI-Konfigurationskanal für Tokens/Dateipfade einzuführen oder Plattformwrites automatisch zu aktivieren.

**Folgen:**
- der HTTP-Server bindet weiterhin ausschließlich an 127.0.0.1; start() startet nur seinen lokalen Serverthread,
- WriteApiAccess.writes_enabled, core_writes_enabled und media_writes_enabled bleiben unabhängige Freigabegates und sind standardmäßig false; ein geöffnetes HTTP-Gate öffnet weder Core- noch Media-Writes,
- CREATE_MEDIA darf nur mit gemeinsam vorhandener PrivateWebMediaCreateRuntime und PrivateWebMediaRefResolver komponiert werden; der High-Level-Builder verlangt dafür nicht leere explizite media_ref -> PrivateWebMediaSource-Bindings und kopiert sie über den bestehenden Resolver immutable,
- Media-Bindings ohne CREATE_MEDIA, CREATE_MEDIA ohne vollständige Media-Komposition sowie media_writes_enabled=true ohne Media-Komposition scheitern vor Browserzugriff,
- der High-Level-Builder fabriziert keine unabhängige Bestätigung aus einer zweiten Instanz derselben Management-Quelle: eine vom Caller separat etablierte `PrivateWebInventoryRuntime` kann explizit über `confirmation_runtime=` injiziert werden; ohne diese explizite Runtime stoppt Create vor dem Writer mit `confirmation_reader_not_independent`, und Delete kann ohne zweite Abwesenheitsquelle nicht `CONFIRMED` werden,
- der Builder bereinigt von ihm bereits erzeugte Runtimes bei partieller Konstruktion; eine fehlgeschlagene Low-Level-Komposition übernimmt dagegen keine caller-owned Runtimes,
- bleibt nach einem Media-Submit `PrivateWebMediaCreateRuntime.reconciliation_required` wahr, blockiert die gemeinsame PrivateWeb-Komposition sämtliche weiteren Core- und Media-Writes unter demselben Operations-Lock noch vor ihrem Service-Delegate; nur erfolgreiche `reconcile_media_submit()`-Beobachtung hebt diesen Fence wieder auf,
- Shutdown quiesziert zuerst HTTP. Meldet PrivateWebMediaCreateRuntime.close() einen unresolved Submit, wird PrivateWebSubmitUnknownError unverändert weitergereicht; Content- und Media-Runtime bleiben für die explizite observation-only reconcile_media_submit() erhalten, während der HTTP-Server nicht neu gestartet werden darf,
- erst nach erfolgreicher Reconciliation kann ein erneutes close() die restlichen lokalen Ressourcen deterministisch schließen,
- dieser Slice führt keinen realen Kleinanzeigen-Plattformwrite aus und fügt keinen Auth-, MFA-, CAPTCHA- oder Security-Challenge-Bypass hinzu.

## D-017 — Analytics-Ziel und Reaktionsmetrik sind explizite, default-off Produktentscheidungen

**Entscheidung:** Die drei Reaktionsrohmetriken `conversation_count`, `unique_buyer_count` und `inbound_message_count` bleiben getrennt. `AnalyticsContract.reaction_metric` darf genau eine davon explizit auswählen, hat aber keinen Default. Unabhängig davon bindet `AnalyticsContract.objective_metric` eine vorhandene Analytics-Rohmetrik als Optimierungsziel; auch diese Auswahl ist standardmäßig nicht gesetzt. Die beiden Felder dürfen unterschiedliche Metriken benennen und werden nicht voneinander abgeleitet.

**Begründung:** Issue #1 belegt den Wunsch nach „wie viele geschrieben haben“ und nach einer „besten Lösung“, enthält aber keine autorisierte fachliche Auswahl der Einheit beziehungsweise Zielfunktion. Eine technische Defaultmetrik würde eine Produktentscheidung erfinden. Gleichzeitig sollen vorhandene Rohdaten und deskriptive Vergleiche weiterhin nutzbar bleiben.

**Folgen:**
- unbekannte `reaction_metric`- oder `objective_metric`-Werte werden fail-closed abgewiesen,
- objective-gebundene Rankings sind ohne konfigurierte Zielmetrik nicht ausführbar,
- die bestehenden Rohmetrik-Rankings bleiben erhalten, verlangen aber weiterhin eine explizite Metrik,
- fehlende Messwerte werden aus Rankings ausgeschlossen; ein tatsächlich beobachteter Wert `0` bleibt erhalten,
- das Dashboard exponiert `GET /api/analytics/contract` und wählt ohne explizite Konfiguration keine erste Metrik implizit aus,
- `mark-api-dashboard` kann die beiden Entscheidungen zur Laufzeit mit `--reaction-metric` beziehungsweise `--objective-metric` erhalten; das Repository speichert oder setzt dafür keinen Produktdefault,
- Rankings bleiben deskriptiv. Aus ihnen wird weder Kausalität noch Qualität abgeleitet,
- Issue #1 bleibt offen, bis ein Mensch die fachliche Reaktionsmetrik und das Optimierungsziel tatsächlich festlegt.

## D-018 — Autoritativer Media-Post-Read bestätigt exakte serverseitige Medienpersistenz

**Fortschreibung:** D-018 erweitert D-013 bis D-016 ausschließlich um eine zusätzliche serverseitige Media-Evidenzschicht. Die bestehenden one-shot-/UNKNOWN-/No-Blind-Retry-Regeln bleiben unverändert; insbesondere ersetzt ein bestätigter Media-Post-Read keinen weiterhin unresolved Browser-Submit-Fence.

**Entscheidung:** `PrivateWebMediaCreateService` darf optional einen vom Caller bereitgestellten `PrivateWebMediaPersistenceVerifier` verwenden. Der Verifier erhält nur die bestätigte Anzeigen-ID und genau die privaten, vor dem ersten Owner-Pre-Read stabilisierten `PrivateWebMediaSource`-Kopien des laufenden Create-Versuchs. Der Verifier besitzt die plattformspezifische Identitätslogik und liefert eine path-freie `PrivateWebMediaPersistenceSnapshot` mit einem expliziten `exact_match`. Der synchrone Port erhält ein explizites `timeout_seconds`-Budget; die plattformspezifische Implementierung muss ihre Transport-/Read-I/O innerhalb dieses Budgets terminieren und Ablauf als Read-Fehler zurückgeben. Lokale Browser-`FileList`- oder Dateistaging-Evidenz zählt nicht als serverseitige Persistenzbestätigung.

**Folgen:**
- `CreateOperationReceipt` erhält additive Media-Evidenz: `media_post_read_status` und `media_persistence_confirmed`; bestehende Positionsargumente bleiben unverändert,
- `media_persistence_confirmed=true` ist nur zulässig, wenn der Create-/Content-Receipt `CONFIRMED` ist und der autoritative Media-Post-Read für dieselbe Anzeigen-ID `CONFIRMED`/`exact_match=true` liefert,
- fehlt der Verifier, meldet der Media-Post-Read Mismatch oder ist das Read-Ergebnis ungültig/unsicher, bleibt `media_persistence_confirmed=false`; daraus entsteht weder ein Plattform-Retry noch ein zweiter Reconciliation-Fence,
- ein unresolved Browser-Submit behält unabhängig von serverseitiger Content-/Media-Bestätigung den bestehenden Submit-UNKNOWN-Fence; `reconcile_media_submit()` bleibt observation-only und wiederholt keinen Browser-Input,
- bevor bei einem bereits bestätigten und tatsächlich ausgeführten Create der externe Media-Verifier aufgerufen wird, persistiert SQLite einen append-only `before_media_post_read`-Checkpoint mit der bereits bekannten Anzeigen-/Content-Evidenz; ein Prozessabbruch oder propagierender `BaseException` während des Verifiers kann damit den Nachweis des Plattformwrites nicht mehr verlieren,
- bei normalem Abschluss bleibt weiterhin genau ein finales angereichertes `create_operation_receipts`-Receipt; der separate Checkpoint ist ausschließlich Crash-/Audit-Evidenz und erzeugt keine Retry-Autorität,
- die finale `completed_at`-Zeit eines Media-Create-Receipts wird erst nach der Media-Post-Read-Klassifikation gesetzt, damit persistierte Audit-Reihenfolge und Operationsdauer den zusätzlichen autoritativen Read einschließen,
- SQLite persistiert die beiden Media-Evidenzfelder additiv; bestehende Datenbanken migrieren `media_persistence_confirmed` mit Default 0,
- die Write API liefert für einen content-bestätigten Media-Create nur dann HTTP 200, wenn zusätzlich `media_persistence_confirmed=true` ist; ansonsten bleibt der Media-Pfad HTTP 202, `PRECONDITION_FAILED` bleibt HTTP 409 und `platform_retry_authorized` bleibt immer false,
- persistente Idempotency-Replays geben die zuvor gespeicherte Media-Response unverändert zurück und führen weder Service noch Verifier erneut aus,
- HTTP-Antwort und persistente Media-Evidenz enthalten keine `media_refs`, lokalen Pfade, Bytes, Browser-`FileList`-Zustände oder provider-spezifischen Media-IDs,
- dieser Slice führt keinen realen Kleinanzeigen-Plattformwrite aus.
