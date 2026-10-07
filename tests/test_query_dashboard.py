from __future__ import annotations

import io
import json
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mark_api.analytics import ANALYTICS_METRICS, REACTION_METRICS, AnalyticsContract
from mark_api.classification_cli import main as classification_main
from mark_api.dashboard import DashboardWriteProxy, _dashboard_http_origin, create_server
from mark_api.email_import import import_kleinanzeigen_email_files
from mark_api.domain import (
    AdClassification,
    AdSnapshot,
    InboundMessageEvent,
    LifecycleState,
    ReactionSnapshot,
)
from mark_api.query import (
    MarkQueryService,
    ad_view_to_dict,
    email_reaction_view_to_dict,
    summary_to_dict,
)
from mark_api.storage import SnapshotStore


T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=5)


def email_notification(
    *,
    ad_id: str = "3333333333",
    conversation_id: str = "abc12:def34:ghi56",
    provider_message_id: str = "11111111-2222-3333-4444-555555555555",
) -> bytes:
    message = EmailMessage(policy=policy.default)
    message["From"] = "Kleinanzeigen <noreply@mail.kleinanzeigen.de>"
    message["To"] = "owner@example.invalid"
    message["Date"] = "Thu, 24 Sep 2026 12:00:00 +0000"
    message["Message-ID"] = f"<{provider_message_id}@chat.kleinanzeigen.de>"
    message["X-Conversation-ID"] = conversation_id
    message["X-Message-ID"] = provider_message_id
    reply_url = (
        "https://www.kleinanzeigen.de/m-nachrichten.html?"
        f"conversationId={conversation_id}"
    )
    message.set_content(
        "Anfrage zu deiner Anzeige\n"
        f"(Anzeigennummer: {ad_id})\n"
        "Um auf diese Nachricht zu antworten: "
        + reply_url
    )
    return message.as_bytes(policy=policy.default)


class RecordingWriteBackend:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.closed = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "recording-write/0.1"
            sys_version = ""

            def log_message(self, format: str, *args: object) -> None:
                return

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                owner.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "body": body,
                        "authorization": self.headers.get("Authorization"),
                        "idempotency_key": self.headers.get("Idempotency-Key"),
                        "content_type": self.headers.get("Content-Type"),
                        "filename": self.headers.get("X-Mark-Media-Filename"),
                        "dashboard_token": self.headers.get(
                            "X-Mark-Dashboard-Token"
                        ),
                        "dashboard_marker": self.headers.get(
                            "X-Mark-Dashboard-Write"
                        ),
                    }
                )
                if self.path == "/api/write/media/stage":
                    payload = {"media_ref": "staged-ref-1"}
                elif self.path == "/api/write/media/discard":
                    payload = {
                        "discarded": len(json.loads(body).get("media_refs", []))
                    }
                else:
                    payload = {
                        "idempotency_key": self.headers.get("Idempotency-Key"),
                        "operation_receipt": {"outcome": "confirmed"},
                        "platform_retry_authorized": False,
                    }
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "application/json; charset=utf-8",
                )
                self.send_header("Content-Length", str(len(encoded)))
                if self.path != "/api/write/media/stage":
                    self.send_header("Idempotency-Replayed", "true")
                self.end_headers()
                self.wfile.write(encoded)

            def do_POST(self) -> None:
                self._handle()

            def do_PATCH(self) -> None:
                self._handle()

            def do_DELETE(self) -> None:
                self._handle()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()

    @property
    def server_address(self) -> tuple[str, int]:
        host, port = self.server.server_address
        return str(host), int(port)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class DashboardWriteProxyHttpTests(unittest.TestCase):
    BACKEND_TOKEN = "backend-write-token-00000001"
    UI_TOKEN = "dashboard-write-token-00000001"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SnapshotStore(Path(self.tmp.name) / "mark.sqlite")
        self.backend = RecordingWriteBackend()
        self.addCleanup(self.backend.close)
        backend_host, backend_port = self.backend.server_address
        self.server = create_server(
            self.store,
            port=0,
            write_proxy=DashboardWriteProxy(
                host=backend_host,
                port=backend_port,
                bearer_token=self.BACKEND_TOKEN,
                ui_token=self.UI_TOKEN,
            ),
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"
        self.origin = self.base

    def write_headers(
        self,
        *,
        content_type: str | None = None,
        idempotency_key: str | None = None,
        origin: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Origin": self.origin if origin is None else origin,
            "X-Mark-Dashboard-Write": "1",
            "X-Mark-Dashboard-Token": self.UI_TOKEN,
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def test_proxy_rejects_dashboard_tokens_that_are_not_header_safe_ascii(self) -> None:
        for token in (
            "abcdefghijklmnop😀",
            "abcdefghijklmnop",
            "abcdefghijklmnop",
        ):
            with self.subTest(token=repr(token)):
                with self.assertRaisesRegex(ValueError, "ui_token is invalid"):
                    DashboardWriteProxy(
                        host="127.0.0.1",
                        port=1,
                        bearer_token=self.BACKEND_TOKEN,
                        ui_token=token,
                    )

        proxy = DashboardWriteProxy(
            host="127.0.0.1",
            port=1,
            bearer_token=self.BACKEND_TOKEN,
            ui_token="abcdefghijklmnop&x=y#z",
        )
        self.assertEqual(proxy.ui_token, "abcdefghijklmnop&x=y#z")

    def test_dashboard_origin_omits_default_http_port(self) -> None:
        self.assertEqual(
            _dashboard_http_origin("127.0.0.1", 80),
            "http://127.0.0.1",
        )
        self.assertEqual(
            _dashboard_http_origin("127.0.0.1", 8765),
            "http://127.0.0.1:8765",
        )

    def test_proxy_requires_dashboard_secret_and_same_origin(self) -> None:
        body = b'{"title":"Neu"}'
        missing_secret = Request(
            self.base + "/api/write/ads/2",
            data=body,
            method="PATCH",
            headers={
                "Origin": self.origin,
                "Content-Type": "application/json",
                "Idempotency-Key": "ui:test-missing-secret",
            },
        )
        with self.assertRaises(HTTPError) as missing:
            urlopen(missing_secret, timeout=2)
        self.assertEqual(missing.exception.code, 403)

        bad_origin = Request(
            self.base + "/api/write/ads/2",
            data=body,
            method="PATCH",
            headers=self.write_headers(
                content_type="application/json",
                idempotency_key="ui:test-bad-origin",
                origin="http://127.0.0.1:1",
            ),
        )
        with self.assertRaises(HTTPError) as bad:
            urlopen(bad_origin, timeout=2)
        self.assertEqual(bad.exception.code, 403)

        non_exact_origin = Request(
            self.base + "/api/write/ads/2",
            data=body,
            method="PATCH",
            headers=self.write_headers(
                content_type="application/json",
                idempotency_key="ui:test-non-exact-origin",
                origin=self.origin + "/",
            ),
        )
        with self.assertRaises(HTTPError) as non_exact:
            urlopen(non_exact_origin, timeout=2)
        self.assertEqual(non_exact.exception.code, 403)

        cross_site = Request(
            self.base + "/api/write/ads/2",
            data=body,
            method="PATCH",
            headers={
                **self.write_headers(
                    content_type="application/json",
                    idempotency_key="ui:test-cross-site",
                ),
                "Sec-Fetch-Site": "cross-site",
            },
        )
        with self.assertRaises(HTTPError) as cross_site_error:
            urlopen(cross_site, timeout=2)
        self.assertEqual(cross_site_error.exception.code, 403)

        malformed_origin = Request(
            self.base + "/api/write/ads/2",
            data=body,
            method="PATCH",
            headers=self.write_headers(
                content_type="application/json",
                idempotency_key="ui:test-malformed-origin",
                origin="http://127.0.0.1:not-a-port",
            ),
        )
        with self.assertRaises(HTTPError) as malformed:
            urlopen(malformed_origin, timeout=2)
        self.assertEqual(malformed.exception.code, 403)

        options = Request(
            self.base + "/api/write/ads/2",
            method="OPTIONS",
            headers=self.write_headers(),
        )
        with self.assertRaises(HTTPError) as preflight:
            urlopen(options, timeout=2)
        self.assertEqual(preflight.exception.code, 405)
        self.assertEqual(self.backend.requests, [])

    def test_proxy_config_assets_hide_tokens_and_route_allowlist_is_closed(self) -> None:
        for path in ("/", "/dashboard.js", "/api/dashboard/config"):
            with urlopen(self.base + path, timeout=2) as response:
                body = response.read()
            self.assertNotIn(self.BACKEND_TOKEN.encode("utf-8"), body)
            self.assertNotIn(self.UI_TOKEN.encode("utf-8"), body)

        with urlopen(self.base + "/api/dashboard/config", timeout=2) as response:
            self.assertEqual(
                json.loads(response.read()),
                {"write_ui_available": True},
            )

        blocked = Request(
            self.base + "/api/write/proxy-anything",
            data=b"{}",
            method="POST",
            headers=self.write_headers(
                content_type="application/json",
                idempotency_key="ui:test-route-allowlist",
            ),
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(blocked, timeout=2)
        self.assertEqual(error.exception.code, 404)
        self.assertEqual(self.backend.requests, [])

    def test_proxy_forwards_exact_write_contract_and_hides_ui_secret(self) -> None:
        payload = {"title": "Neu", "description": "Beschreibung"}
        request = Request(
            self.base + "/api/write/ads/2",
            data=json.dumps(payload).encode("utf-8"),
            method="PATCH",
            headers=self.write_headers(
                content_type="application/json",
                idempotency_key="ui:test-forward-1",
            ),
        )
        with urlopen(request, timeout=2) as response:
            response_payload = json.loads(response.read())
            replayed = response.headers.get("Idempotency-Replayed")

        self.assertEqual(response_payload["idempotency_key"], "ui:test-forward-1")
        self.assertEqual(replayed, "true")
        self.assertEqual(len(self.backend.requests), 1)
        captured = self.backend.requests[0]
        self.assertEqual(captured["method"], "PATCH")
        self.assertEqual(captured["path"], "/api/write/ads/2")
        self.assertEqual(json.loads(captured["body"]), payload)
        self.assertEqual(
            captured["authorization"],
            f"Bearer {self.BACKEND_TOKEN}",
        )
        self.assertEqual(captured["idempotency_key"], "ui:test-forward-1")
        self.assertIsNone(captured["dashboard_token"])
        self.assertIsNone(captured["dashboard_marker"])

        pending = self.store.dashboard_pending_writes()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].scope, "ad:2:update")
        self.assertEqual(pending[0].idempotency_key, "ui:test-forward-1")

        wrong_ack = Request(
            self.base + "/api/dashboard/pending-writes/ack",
            data=json.dumps(
                {
                    "scope": "ad:2:update",
                    "idempotency_key": "ui:wrong-key",
                }
            ).encode("utf-8"),
            method="POST",
            headers=self.write_headers(content_type="application/json"),
        )
        with self.assertRaises(HTTPError) as wrong_ack_error:
            urlopen(wrong_ack, timeout=2)
        self.assertEqual(wrong_ack_error.exception.code, 409)
        self.assertEqual(self.store.dashboard_pending_writes(), pending)

        ack = Request(
            self.base + "/api/dashboard/pending-writes/ack",
            data=json.dumps(
                {
                    "scope": "ad:2:update",
                    "idempotency_key": "ui:test-forward-1",
                }
            ).encode("utf-8"),
            method="POST",
            headers=self.write_headers(content_type="application/json"),
        )
        with urlopen(ack, timeout=2) as response:
            self.assertEqual(
                json.loads(response.read()),
                {"acknowledged": True, "finalized": False},
            )
        pending_after_ack = self.store.dashboard_pending_writes()
        self.assertEqual(len(pending_after_ack), 1)
        self.assertTrue(pending_after_ack[0].acknowledged)

        with urlopen(ack, timeout=2) as response:
            self.assertEqual(
                json.loads(response.read()),
                {"acknowledged": True, "finalized": True},
            )
        self.assertEqual(self.store.dashboard_pending_writes(), ())

    def test_proxy_media_stage_keeps_bytes_local_and_uses_no_idempotency_key(self) -> None:
        media = b"\x89PNG\r\n\x1a\nlocal-test"
        request = Request(
            self.base + "/api/write/media/stage",
            data=media,
            method="POST",
            headers={
                **self.write_headers(content_type="image/png"),
                "X-Mark-Media-Filename": "bild.png",
            },
        )
        with urlopen(request, timeout=2) as response:
            payload = json.loads(response.read())

        self.assertEqual(payload, {"media_ref": "staged-ref-1"})
        self.assertEqual(len(self.backend.requests), 1)
        captured = self.backend.requests[0]
        self.assertEqual(captured["body"], media)
        self.assertEqual(captured["content_type"], "image/png")
        self.assertEqual(captured["filename"], "bild.png")
        self.assertIsNone(captured["idempotency_key"])
        self.assertEqual(
            captured["authorization"],
            f"Bearer {self.BACKEND_TOKEN}",
        )

    def test_proxy_media_stage_blocks_existing_create_recovery(self) -> None:
        self.store.claim_dashboard_pending_write(
            scope="create-media",
            resource_key="create",
            idempotency_key="ui:pending-media-create",
            method="POST",
            path="/api/write/media/ads",
            payload_json=json.dumps(
                {
                    "category_path": ["A", "B"],
                    "title": "Alt",
                    "description": "Recovery",
                    "price_eur": 1,
                    "media_refs": ["media_existing"],
                }
            ),
            ad_id=None,
        )
        media = b"\\x89PNG\\r\\n\\x1a\\nnew-local-test"
        request = Request(
            self.base + "/api/write/media/stage",
            data=media,
            method="POST",
            headers={
                **self.write_headers(content_type="image/png"),
                "X-Mark-Media-Filename": "upload.png",
            },
        )

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)

        self.assertEqual(error.exception.code, 409)
        self.assertEqual(
            json.loads(error.exception.read()),
            {
                "error": "dashboard_pending_write_conflict",
                "platform_retry_authorized": False,
            },
        )
        self.assertEqual(self.backend.requests, [])
        self.assertEqual(len(self.store.dashboard_pending_writes()), 1)

    def test_proxy_media_discard_is_allowlisted_without_platform_idempotency(self) -> None:
        body = json.dumps({"media_refs": ["staged-ref-1"]}).encode("utf-8")
        request = Request(
            self.base + "/api/write/media/discard",
            data=body,
            method="POST",
            headers=self.write_headers(content_type="application/json"),
        )
        with urlopen(request, timeout=2) as response:
            payload = json.loads(response.read())

        self.assertEqual(payload, {"discarded": 1})
        self.assertEqual(len(self.backend.requests), 1)
        captured = self.backend.requests[0]
        self.assertEqual(captured["path"], "/api/write/media/discard")
        self.assertEqual(json.loads(captured["body"]), {"media_refs": ["staged-ref-1"]})
        self.assertIsNone(captured["idempotency_key"])
        self.assertEqual(
            captured["authorization"],
            f"Bearer {self.BACKEND_TOKEN}",
        )

    def test_proxy_persists_unknown_request_and_recovers_exactly_after_restart(self) -> None:
        self.backend.close()
        request = Request(
            self.base + "/api/write/ads/2",
            data=b'{"title":"Neu"}',
            method="PATCH",
            headers=self.write_headers(
                content_type="application/json",
                idempotency_key="ui:test-transport-unknown",
            ),
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, 502)
        self.assertEqual(
            json.loads(error.exception.read()),
            {
                "error": "write_proxy_transport_unknown",
                "platform_retry_authorized": False,
            },
        )

        pending = self.store.dashboard_pending_writes()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].scope, "ad:2:update")
        self.assertEqual(pending[0].resource_key, "ad:2")
        self.assertEqual(
            pending[0].idempotency_key,
            "ui:test-transport-unknown",
        )
        self.assertEqual(pending[0].method, "PATCH")
        self.assertEqual(pending[0].path, "/api/write/ads/2")
        self.assertEqual(
            json.loads(pending[0].payload_json or "{}"),
            {"title": "Neu"},
        )
        self.assertEqual(pending[0].ad_id, "2")

        conflict = Request(
            self.base + "/api/write/ads/2",
            data=b'{"title":"Changed"}',
            method="PATCH",
            headers=self.write_headers(
                content_type="application/json",
                idempotency_key="ui:different-key",
            ),
        )
        with self.assertRaises(HTTPError) as conflict_error:
            urlopen(conflict, timeout=2)
        self.assertEqual(conflict_error.exception.code, 409)
        self.assertEqual(
            json.loads(conflict_error.exception.read()),
            {
                "error": "dashboard_pending_write_conflict",
                "platform_retry_authorized": False,
            },
        )
        self.assertEqual(
            self.store.dashboard_pending_writes(),
            pending,
        )

        replacement_backend = RecordingWriteBackend()
        self.addCleanup(replacement_backend.close)
        backend_host, backend_port = replacement_backend.server_address
        replacement_token = "dashboard-write-token-rotated-0001"
        replacement_server = create_server(
            self.store,
            port=0,
            write_proxy=DashboardWriteProxy(
                host=backend_host,
                port=backend_port,
                bearer_token=self.BACKEND_TOKEN,
                ui_token=replacement_token,
            ),
        )
        replacement_thread = threading.Thread(
            target=replacement_server.serve_forever,
            daemon=True,
        )
        replacement_thread.start()
        self.addCleanup(replacement_thread.join, 2)
        self.addCleanup(replacement_server.server_close)
        self.addCleanup(replacement_server.shutdown)
        host, port = replacement_server.server_address
        replacement_base = f"http://{host}:{port}"

        pending_request = Request(
            replacement_base + "/api/dashboard/pending-writes",
            headers={
                "X-Mark-Dashboard-Write": "1",
                "X-Mark-Dashboard-Token": replacement_token,
            },
        )
        with urlopen(pending_request, timeout=2) as response:
            recovered = json.loads(response.read())
        self.assertEqual(
            recovered,
            {
                "pending_writes": [
                    {
                        "scope": "ad:2:update",
                        "key": "ui:test-transport-unknown",
                        "method": "PATCH",
                        "path": "/api/write/ads/2",
                        "payload": {"title": "Neu"},
                        "adId": "2",
                        "acknowledged": False,
                    }
                ]
            },
        )

        retry = Request(
            replacement_base + "/api/write/ads/2",
            data=b'{"title":"Neu"}',
            method="PATCH",
            headers={
                "Origin": replacement_base,
                "X-Mark-Dashboard-Write": "1",
                "X-Mark-Dashboard-Token": replacement_token,
                "Content-Type": "application/json",
                "Idempotency-Key": "ui:test-transport-unknown",
            },
        )
        with urlopen(retry, timeout=2) as response:
            replay_payload = json.loads(response.read())
        self.assertEqual(
            replay_payload["idempotency_key"],
            "ui:test-transport-unknown",
        )
        self.assertEqual(len(replacement_backend.requests), 1)
        self.assertEqual(
            replacement_backend.requests[0]["idempotency_key"],
            "ui:test-transport-unknown",
        )
        self.assertEqual(len(self.store.dashboard_pending_writes()), 1)

        ack = Request(
            replacement_base + "/api/dashboard/pending-writes/ack",
            data=json.dumps(
                {
                    "scope": "ad:2:update",
                    "idempotency_key": "ui:test-transport-unknown",
                }
            ).encode("utf-8"),
            method="POST",
            headers={
                "Origin": replacement_base,
                "X-Mark-Dashboard-Write": "1",
                "X-Mark-Dashboard-Token": replacement_token,
                "Content-Type": "application/json",
            },
        )
        with urlopen(ack, timeout=2) as response:
            self.assertEqual(
                json.loads(response.read()),
                {"acknowledged": True, "finalized": False},
            )
        pending_after_ack = self.store.dashboard_pending_writes()
        self.assertEqual(len(pending_after_ack), 1)
        self.assertTrue(pending_after_ack[0].acknowledged)

        with urlopen(ack, timeout=2) as response:
            self.assertEqual(
                json.loads(response.read()),
                {"acknowledged": True, "finalized": True},
            )
        self.assertEqual(self.store.dashboard_pending_writes(), ())


class SeededStoreMixin:
    def make_store(self) -> SnapshotStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SnapshotStore(Path(tmp.name) / "mark.sqlite")
        store.append_ad_snapshot(
            AdSnapshot(
                ad_id="1",
                observed_at=T0,
                source="management",
                lifecycle_state=LifecycleState.ACTIVE,
                title="Dekorativer Hirsch",
                description="beflockt",
                views=15,
                watch_count=0,
                reply_count=1,
            )
        )
        store.append_ad_snapshot(
            AdSnapshot(
                ad_id="1",
                observed_at=T1,
                source="management-post-delete",
                lifecycle_state=LifecycleState.ABSENT,
            )
        )
        store.append_ad_snapshot(
            AdSnapshot(
                ad_id="2",
                observed_at=T1,
                source="management+mobile",
                lifecycle_state=LifecycleState.PAUSED,
                title="Zweite Anzeige",
                description="Beschreibung",
                views=4,
                watch_count=2,
                reply_count=0,
            )
        )
        store.append_reaction_snapshot(
            ReactionSnapshot(
                ad_id="1",
                observed_at=T0,
                source="mobile",
                conversation_count=1,
                unique_buyer_count=1,
                inbound_message_count=1,
            )
        )
        store.append_classification(
            AdClassification(
                ad_id="1",
                observed_at=T0,
                source="manual",
                image_type="detail",
                city="Berlin",
                text_type="short",
                title_type="object",
            )
        )
        store.append_classification(
            AdClassification(
                ad_id="2",
                observed_at=T1,
                source="manual",
                image_type="overview",
                city="Berlin",
                text_type="short",
                title_type="object",
            )
        )
        return store


class MarkQueryServiceTests(SeededStoreMixin, unittest.TestCase):
    def test_latest_view_preserves_last_known_content_and_metrics_after_absent(self) -> None:
        query = MarkQueryService(self.make_store())

        rows = query.latest_ads()

        self.assertEqual([item.ad_id for item in rows], ["1", "2"])
        deleted = rows[0]
        self.assertFalse(deleted.present)
        self.assertEqual(deleted.lifecycle_state, LifecycleState.ABSENT)
        self.assertEqual(deleted.observed_at, T1)
        self.assertEqual(deleted.source, "management-post-delete")
        self.assertEqual(deleted.title, "Dekorativer Hirsch")
        self.assertEqual(deleted.description, "beflockt")
        self.assertEqual(deleted.views, 15)
        self.assertEqual(deleted.watch_count, 0)
        self.assertEqual(deleted.reply_count, 1)

    def test_summary_counts_presence_and_metric_coverage_separately(self) -> None:
        query = MarkQueryService(self.make_store())

        summary = query.summary()

        self.assertEqual(summary.tracked_ads, 2)
        self.assertEqual(summary.current_ads, 1)
        self.assertEqual(summary.absent_ads, 1)
        self.assertEqual(summary.unknown_state_ads, 0)
        self.assertEqual(summary.views_total_known, 19)
        self.assertEqual(summary.views_observed_ads, 2)
        self.assertEqual(summary.watch_total_known, 2)
        self.assertEqual(summary.watch_observed_ads, 2)
        self.assertEqual(summary.replies_total_known, 1)
        self.assertEqual(summary.replies_observed_ads, 2)

    def test_email_reaction_projection_supports_email_only_ad(self) -> None:
        store = self.make_store()
        store.append_inbound_message_events(
            (
                InboundMessageEvent(
                    ad_id="3",
                    conversation_id="conversation-a",
                    provider_message_id="message-a",
                    observed_at=T0,
                    source="kleinanzeigen-email",
                ),
                InboundMessageEvent(
                    ad_id="3",
                    conversation_id="conversation-a",
                    provider_message_id="message-b",
                    observed_at=T1,
                    source="kleinanzeigen-email",
                ),
            )
        )
        query = MarkQueryService(store)

        item = query.email_reaction("3")

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.conversation_count, 1)
        self.assertEqual(item.inbound_message_count, 2)
        self.assertEqual(item.first_observed_at, T0)
        self.assertEqual(item.last_observed_at, T1)
        self.assertEqual(query.email_reactions(), (item,))
        payload = email_reaction_view_to_dict(item)
        self.assertNotIn("unique_buyer_count", payload)
        self.assertNotIn("message_text", payload)
        self.assertNotIn("present", payload)

    def test_serializers_are_json_safe_and_do_not_add_message_text(self) -> None:
        query = MarkQueryService(self.make_store())
        ad_payload = ad_view_to_dict(query.latest_ads()[0])
        summary_payload = summary_to_dict(query.summary())

        json.dumps(ad_payload)
        json.dumps(summary_payload)
        self.assertNotIn("message", ad_payload)
        self.assertEqual(ad_payload["lifecycle_state"], "absent")


class EmailOnlyClassificationIntegrationTests(unittest.TestCase):
    def test_email_import_classification_and_group_dashboard(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "mark.sqlite"
        email_path = Path(tmp.name) / "reaction.eml"
        email_path.write_bytes(email_notification())
        store = SnapshotStore(db_path)

        report = import_kleinanzeigen_email_files(store, (email_path,))
        self.assertEqual(report.inserted_events, 1)
        self.assertEqual(store.tracked_ad_ids(), ())

        output = io.StringIO()
        with redirect_stdout(output):
            result = classification_main(
                [
                    "--db",
                    str(db_path),
                    "--ad-id",
                    "3333333333",
                    "--city",
                    "Dresden",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["city"], "Dresden")
        self.assertEqual(store.tracked_ad_ids(), ())

        server = create_server(store, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        thread.start()
        host, port = server.server_address

        with urlopen(
            "http://"
            f"{host}:{port}/api/analytics/groups"
            "?dimension=city&metric=email_inbound_message_count",
            timeout=2,
        ) as response:
            groups = json.loads(response.read())
        with urlopen(
            f"http://{host}:{port}/api/summary",
            timeout=2,
        ) as response:
            summary = json.loads(response.read())

        self.assertEqual(
            groups,
            [
                {
                    "label": "Dresden",
                    "sample_size": 1,
                    "metric_sum": 1,
                    "metric_mean": 1.0,
                }
            ],
        )
        self.assertEqual(summary["tracked_ads"], 0)
        self.assertEqual(summary["current_ads"], 0)


class DashboardHttpTests(SeededStoreMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.make_store()
        self.server = create_server(self.store, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def get(self, path: str):
        with urlopen(self.base + path, timeout=2) as response:
            return (
                response.status,
                dict(response.headers.items()),
                response.read(),
            )

    def test_health_summary_and_ads_endpoints(self) -> None:
        status, headers, body = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok"})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

        _, _, summary_body = self.get("/api/summary")
        summary = json.loads(summary_body)
        self.assertEqual(summary["tracked_ads"], 2)
        self.assertEqual(summary["current_ads"], 1)
        self.assertEqual(summary["absent_ads"], 1)

        _, _, ads_body = self.get("/api/ads")
        ads = json.loads(ads_body)
        self.assertEqual(len(ads), 2)
        self.assertEqual(ads[0]["ad_id"], "1")
        self.assertEqual(ads[0]["lifecycle_state"], "absent")
        self.assertEqual(ads[0]["views"], 15)

    def test_history_and_reactions_endpoints(self) -> None:
        _, _, history_body = self.get("/api/ads/1/history")
        history = json.loads(history_body)
        self.assertEqual(
            [item["lifecycle_state"] for item in history],
            ["active", "absent"],
        )

        _, _, reactions_body = self.get("/api/ads/1/reactions")
        reactions = json.loads(reactions_body)
        self.assertEqual(len(reactions), 1)
        self.assertEqual(reactions[0]["conversation_count"], 1)
        self.assertNotIn("text", reactions[0])

    def test_analytics_contract_is_neutral_and_ui_has_no_metric_default(self) -> None:
        _, _, contract_body = self.get("/api/analytics/contract")
        contract = json.loads(contract_body)

        self.assertIsNone(contract["reaction_metric"])
        self.assertIsNone(contract["objective_metric"])
        self.assertEqual(
            contract["allowed_reaction_metrics"],
            list(REACTION_METRICS),
        )
        self.assertEqual(
            contract["allowed_objective_metrics"],
            list(ANALYTICS_METRICS),
        )

        _, _, js_body = self.get("/dashboard.js")
        script = js_body.decode("utf-8")
        self.assertIn('setOptions(metricSelect, metricsPayload.metrics, "Metrik auswählen …")', script)
        self.assertIn('getJson("/api/analytics/contract")', script)

    def test_dashboard_exposes_explicit_configured_contract(self) -> None:
        server = create_server(
            self.store,
            port=0,
            analytics_contract=AnalyticsContract(
                reaction_metric="unique_buyer_count",
                objective_metric="inbound_message_count",
            ),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address

        with urlopen(
            f"http://{host}:{port}/api/analytics/contract",
            timeout=2,
        ) as response:
            contract = json.loads(response.read())

        self.assertEqual(contract["reaction_metric"], "unique_buyer_count")
        self.assertEqual(contract["objective_metric"], "inbound_message_count")

    def test_analytics_contract_and_rankings_endpoints(self) -> None:
        _, _, metrics_body = self.get("/api/analytics/metrics")
        _, _, dimensions_body = self.get("/api/analytics/dimensions")
        metrics = json.loads(metrics_body)["metrics"]
        dimensions = json.loads(dimensions_body)["dimensions"]

        self.assertEqual(
            metrics,
            [
                "views",
                "watch_count",
                "reply_count",
                "conversation_count",
                "unique_buyer_count",
                "inbound_message_count",
                "email_conversation_count",
                "email_inbound_message_count",
            ],
        )
        self.assertEqual(
            dimensions,
            ["image_type", "city", "text_type", "title_type"],
        )

        _, _, ads_body = self.get("/api/analytics/ads?metric=views")
        ranking = json.loads(ads_body)
        self.assertEqual([item["ad_id"] for item in ranking], ["1", "2"])
        self.assertEqual(ranking[0]["value"], 15)
        self.assertFalse(ranking[0]["present"])
        self.assertEqual(ranking[0]["lifecycle_state"], "absent")

        _, _, groups_body = self.get(
            "/api/analytics/groups?dimension=city&metric=views"
        )
        groups = json.loads(groups_body)
        self.assertEqual(
            groups,
            [
                {
                    "label": "Berlin",
                    "sample_size": 2,
                    "metric_sum": 19,
                    "metric_mean": 9.5,
                }
            ],
        )
        encoded = json.dumps(groups).lower()
        self.assertNotIn("winner", encoded)
        self.assertNotIn("recommend", encoded)

    def test_email_reactions_endpoints_and_explicit_analytics_metric(self) -> None:
        self.store.append_inbound_message_events(
            (
                InboundMessageEvent(
                    ad_id="1",
                    conversation_id="email-conversation-a",
                    provider_message_id="email-message-a",
                    observed_at=T0,
                    source="kleinanzeigen-email",
                ),
                InboundMessageEvent(
                    ad_id="1",
                    conversation_id="email-conversation-a",
                    provider_message_id="email-message-b",
                    observed_at=T1,
                    source="kleinanzeigen-email",
                ),
                InboundMessageEvent(
                    ad_id="3",
                    conversation_id="email-conversation-b",
                    provider_message_id="email-message-c",
                    observed_at=T1,
                    source="kleinanzeigen-email",
                ),
            )
        )

        _, _, all_body = self.get("/api/email-reactions")
        all_rows = json.loads(all_body)
        self.assertEqual([item["ad_id"] for item in all_rows], ["1", "3"])

        _, _, item_body = self.get("/api/ads/3/email-reactions")
        item = json.loads(item_body)
        self.assertEqual(item["conversation_count"], 1)
        self.assertEqual(item["inbound_message_count"], 1)
        self.assertNotIn("unique_buyer_count", item)

        _, _, email_ranking_body = self.get(
            "/api/analytics/ads?metric=email_inbound_message_count"
        )
        email_ranking = json.loads(email_ranking_body)
        self.assertEqual(
            [(row["ad_id"], row["value"]) for row in email_ranking],
            [("1", 2), ("3", 1)],
        )
        email_only = email_ranking[1]
        self.assertIsNone(email_only["present"])
        self.assertIsNone(email_only["lifecycle_state"])

        self.store.append_classification(
            AdClassification(
                ad_id="3",
                observed_at=T1 + timedelta(seconds=1),
                source="manual-cli",
                city="Dresden",
            )
        )
        _, _, email_groups_body = self.get(
            "/api/analytics/groups"
            "?dimension=city&metric=email_inbound_message_count"
        )
        email_groups = json.loads(email_groups_body)
        self.assertEqual(
            email_groups,
            [
                {
                    "label": "Berlin",
                    "sample_size": 1,
                    "metric_sum": 2,
                    "metric_mean": 2.0,
                },
                {
                    "label": "Dresden",
                    "sample_size": 1,
                    "metric_sum": 1,
                    "metric_mean": 1.0,
                },
            ],
        )

        _, _, summary_body = self.get("/api/summary")
        self.assertEqual(json.loads(summary_body)["tracked_ads"], 2)

        _, _, mobile_ranking_body = self.get(
            "/api/analytics/ads?metric=inbound_message_count"
        )
        mobile_ranking = json.loads(mobile_ranking_body)
        self.assertEqual(
            [(row["ad_id"], row["value"]) for row in mobile_ranking],
            [("1", 1)],
        )

    def test_reaction_ranking_excludes_ads_without_reaction_history(self) -> None:
        _, _, body = self.get(
            "/api/analytics/ads?metric=inbound_message_count"
        )
        ranking = json.loads(body)

        self.assertEqual(
            [(item["ad_id"], item["value"]) for item in ranking],
            [("1", 1)],
        )

    def test_invalid_analytics_metric_and_dimension_are_400(self) -> None:
        with self.assertRaises(HTTPError) as bad_metric:
            urlopen(
                self.base + "/api/analytics/ads?metric=engagement",
                timeout=2,
            )
        self.assertEqual(bad_metric.exception.code, 400)
        metric_payload = json.loads(bad_metric.exception.read())
        self.assertEqual(metric_payload["error"], "invalid_metric")
        self.assertIn("views", metric_payload["allowed_metrics"])

        with self.assertRaises(HTTPError) as missing_metric:
            urlopen(self.base + "/api/analytics/ads", timeout=2)
        self.assertEqual(missing_metric.exception.code, 400)
        self.assertEqual(
            json.loads(missing_metric.exception.read())["error"],
            "invalid_metric",
        )

        with self.assertRaises(HTTPError) as bad_dimension:
            urlopen(
                self.base
                + "/api/analytics/groups?dimension=category&metric=views",
                timeout=2,
            )
        self.assertEqual(bad_dimension.exception.code, 400)
        dimension_payload = json.loads(bad_dimension.exception.read())
        self.assertEqual(dimension_payload["error"], "invalid_dimension")
        self.assertIn("city", dimension_payload["allowed_dimensions"])
    def test_reaction_only_history_is_available_without_ad_history(self) -> None:
        self.store.append_reaction_snapshot(
            ReactionSnapshot(
                ad_id="3",
                observed_at=T1,
                source="mobile",
                conversation_count=2,
                unique_buyer_count=2,
                inbound_message_count=3,
            )
        )

        status, _, body = self.get("/api/ads/3/reactions")

        self.assertEqual(status, 200)
        reactions = json.loads(body)
        self.assertEqual(len(reactions), 1)
        self.assertEqual(reactions[0]["ad_id"], "3")
        self.assertEqual(reactions[0]["conversation_count"], 2)
        self.assertEqual(reactions[0]["unique_buyer_count"], 2)
        self.assertEqual(reactions[0]["inbound_message_count"], 3)

        with self.assertRaises(HTTPError) as missing:
            urlopen(self.base + "/api/ads/999/reactions", timeout=2)
        self.assertEqual(missing.exception.code, 404)
        self.assertEqual(
            json.loads(missing.exception.read()),
            {"error": "ad_not_found"},
        )

    def test_unknown_and_invalid_ad_ids_are_explicit(self) -> None:
        with self.assertRaises(HTTPError) as missing:
            urlopen(self.base + "/api/ads/999/history", timeout=2)
        self.assertEqual(missing.exception.code, 404)
        self.assertEqual(
            json.loads(missing.exception.read()),
            {"error": "ad_not_found"},
        )

        with self.assertRaises(HTTPError) as invalid:
            urlopen(self.base + "/api/ads/not-an-id/history", timeout=2)
        self.assertEqual(invalid.exception.code, 400)
        self.assertEqual(
            json.loads(invalid.exception.read()),
            {"error": "invalid_ad_id"},
        )

    def test_dashboard_assets_are_local_and_javascript_is_not_escaped(self) -> None:
        _, _, html_body = self.get("/")
        html = html_body.decode("utf-8")
        self.assertIn("Mark Dashboard", html)
        self.assertIn('src="/dashboard.js"', html)
        self.assertIn('id="metric-select"', html)
        self.assertIn('id="groups-body"', html)
        self.assertIn('id="groups-chart"', html)
        self.assertIn('id="ranking-chart"', html)
        self.assertIn('id="write-panel"', html)
        self.assertIn('id="create-form"', html)
        self.assertIn('id="manage-form"', html)
        self.assertIn('role="list"', html)
        self.assertNotIn("https://", html)
        self.assertNotIn("http://", html)

        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        self.assertIn("summary.views_total_known", javascript)
        self.assertIn("/api/analytics/groups", javascript)
        self.assertIn('item.present === null ? "—"', javascript)
        self.assertIn("renderBarChart(", javascript)
        self.assertIn('"groups-chart",', javascript)
        self.assertIn('"ranking-chart",', javascript)
        self.assertIn("label.textContent = String(labelOf(entry.item));", javascript)
        self.assertIn('row.setAttribute("role", "listitem");', javascript)
        self.assertIn("container.replaceChildren();", javascript)
        self.assertIn("rawValue === null || rawValue === undefined", javascript)
        self.assertIn('document.createElement("progress")', javascript)
        self.assertIn("progress.max = max === 0 ? 1 : max;", javascript)
        self.assertIn("progress.value = entry.value;", javascript)
        self.assertIn("formatValue(entry.value, entry.item)", javascript)
        self.assertIn("(n=${group.sample_size})", javascript)
        self.assertNotIn(".style.width", javascript)
        self.assertNotIn("innerHTML", javascript)
        self.assertIn("let analyticsRequestGeneration = 0;", javascript)
        self.assertIn('const WRITE_TOKEN_STORAGE_KEY = "mark-dashboard-write-token";', javascript)
        self.assertIn('"X-Mark-Dashboard-Token": writeToken', javascript)
        self.assertIn("const pendingWrites = new Map();", javascript)
        self.assertIn("write_proxy_transport_unknown", javascript)
        self.assertNotIn("Authorization", javascript)
        self.assertIn(
            "const generation = ++analyticsRequestGeneration;",
            javascript,
        )
        guard = "if (generation !== analyticsRequestGeneration) return;"
        self.assertIn(guard, javascript)
        self.assertLess(
            javascript.index(guard),
            javascript.index("renderGroups(groups);"),
        )
        self.assertNotIn(chr(92) + chr(96), javascript)

        _, _, css_body = self.get("/dashboard.css")
        css = css_body.decode("utf-8")
        self.assertIn(".cards", css)
        self.assertIn(".chart-row", css)
        self.assertIn(".chart-progress", css)
        self.assertIn("appearance: none;", css)
        self.assertIn(".chart-progress::-webkit-progress-bar", css)
        self.assertIn(".chart-progress::-webkit-progress-value", css)
        self.assertIn(".chart-progress::-moz-progress-bar", css)
        self.assertIn(
            ".chart-row { grid-template-columns: minmax(70px, 110px) minmax(0, 1fr); }",
            css,
        )
        self.assertIn("grid-column: 2;", css)
        self.assertIn("overflow-wrap: anywhere;", css)

    def test_bar_chart_runtime_filters_values_and_preserves_semantics(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");

class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.attributes = {};
    this.hidden = false;
    this.textContent = "";
    this.className = "";
    this.max = undefined;
    this.value = undefined;
  }

  replaceChildren(...children) {
    this.children = children;
  }

  append(...children) {
    this.children.push(...children);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
}

const elements = new Map();
const chart = new Element("div");
elements.set("chart", chart);
elements.set("groups-chart", new Element("div"));
elements.set("groups-body", new Element("tbody"));
elements.set("groups-empty", new Element("p"));

globalThis.document = {
  getElementById(id) {
    return elements.get(id);
  },
  createElement(tagName) {
    return new Element(tagName);
  },
};

renderBarChart(
  "chart",
  [
    {label: "null", value: null},
    {label: "negative", value: -1},
    {label: "zero", value: 0},
    {label: "four", value: 4},
    {label: "invalid", value: "not-a-number"},
  ],
  (item) => item.value,
  (item) => item.label,
  (value, item) => `${value}:${item.label}`,
);
assert.equal(chart.hidden, false);
assert.deepEqual(
  chart.children.map((row) => row.children[0].textContent),
  ["zero", "four"],
);
assert.deepEqual(
  chart.children.map((row) => row.children[2].textContent),
  ["0:zero", "4:four"],
);
assert.equal(chart.children[0].children[1].max, 4);
assert.equal(chart.children[0].children[1].value, 0);
assert.equal(chart.children[1].children[1].value, 4);

renderBarChart(
  "chart",
  [{label: "a", value: 0}, {label: "b", value: 0}],
  (item) => item.value,
  (item) => item.label,
  (value) => String(value),
);
assert.equal(chart.children[0].children[1].max, 1);
assert.equal(chart.children[1].children[1].max, 1);

renderBarChart(
  "chart",
  [{label: "missing", value: undefined}, {label: "bad", value: -2}],
  (item) => item.value,
  (item) => item.label,
  (value) => String(value),
);
assert.equal(chart.hidden, true);
assert.equal(chart.children.length, 0);

renderGroups([
  {label: "tiny", sample_size: 1, metric_sum: 12, metric_mean: 12},
]);
const groupChart = elements.get("groups-chart");
assert.equal(groupChart.children.length, 1);
assert.equal(groupChart.children[0].attributes.role, "listitem");
assert.equal(groupChart.children[0].children[0].textContent, "tiny");
assert.equal(groupChart.children[0].children[2].textContent, "12.00 (n=1)");
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_restores_pending_from_server_and_blocks_on_recovery_failure(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
const sessionStorageState = new Map();
globalThis.window = {
  location: {
    hash: "#write_token=dashboard-write-token-new-0001",
    pathname: "/",
    search: "",
  },
  history: {
    replaceState() {
      window.location.hash = "";
    },
  },
  sessionStorage: {
    getItem(key) {
      return sessionStorageState.has(key) ? sessionStorageState.get(key) : null;
    },
    setItem(key, value) {
      sessionStorageState.set(key, String(value));
    },
    removeItem(key) {
      sessionStorageState.delete(key);
    },
  },
};
writeUiAvailable = true;
sessionStorageState.set(
  WRITE_TOKEN_STORAGE_KEY,
  "dashboard-write-token-old-0001",
);
const original = {
  scope: "create",
  key: "ui:restart-bound-key",
  method: "POST",
  path: "/api/write/ads",
  payload: {
    category_path: ["A", "B"],
    title: "first",
    description: "one",
    price_eur: 1,
  },
  adId: null,
};
let recoveryFails = false;
globalThis.fetch = async (path, options) => {
  assert.equal(path, "/api/dashboard/pending-writes");
  assert.equal(
    options.headers["X-Mark-Dashboard-Token"],
    "dashboard-write-token-new-0001",
  );
  if (recoveryFails) {
    return {
      status: 500,
      ok: false,
      async text() {
        return JSON.stringify({error: "dashboard_pending_store_error"});
      },
    };
  }
  return {
    status: 200,
    ok: true,
    async text() {
      return JSON.stringify({pending_writes: [original]});
    },
  };
};

(async () => {
  consumeWriteToken();
  assert.equal(writeToken, "dashboard-write-token-new-0001");
  await refreshPendingWrites();
  assert.equal(pendingRecoveryBlocked, false);
  assert.deepEqual(pendingWrites.get("create"), original);
  assert.equal(writeUiReady(), true);

  recoveryFails = true;
  await refreshPendingWrites();
  assert.equal(pendingRecoveryBlocked, true);
  assert.equal(pendingWrites.size, 0);
  assert.equal(writeUiReady(), false);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_finalizes_observed_ack_tombstone_without_platform_retry(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
writeUiAvailable = true;
writeToken = "abcdefghijklmnop";
const tombstone = {
  scope: "create",
  key: "ui:acked-create",
  method: "POST",
  path: "/api/write/ads",
  payload: {
    category_path: ["A", "B"],
    title: "first",
    description: "one",
    price_eur: 1,
  },
  adId: null,
  acknowledged: true,
};
const calls = [];
globalThis.fetch = async (path, options) => {
  calls.push({path, options});
  if (path === "/api/dashboard/pending-writes") {
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({pending_writes: [tombstone]});
      },
    };
  }
  if (path === "/api/dashboard/pending-writes/ack") {
    assert.deepEqual(
      JSON.parse(options.body),
      {scope: tombstone.scope, idempotency_key: tombstone.key},
    );
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({acknowledged: true, finalized: true});
      },
    };
  }
  throw new Error("unexpected platform request: " + path);
};

(async () => {
  await refreshPendingWrites();
  assert.equal(pendingRecoveryBlocked, false);
  assert.equal(pendingWrites.size, 0);
  assert.equal(writeUiReady(), true);
  assert.deepEqual(
    calls.map((item) => item.path),
    ["/api/dashboard/pending-writes", "/api/dashboard/pending-writes/ack"],
  );
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_restores_more_than_64_pending_entries(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
const records = Array.from({length: 65}, (_unused, index) => {
  const adId = String(index + 1);
  return {
    scope: `ad:${adId}:pause`,
    key: `ui:pending-${adId}`,
    method: "POST",
    path: `/api/write/ads/${adId}/pause`,
    payload: null,
    adId,
  };
});
globalThis.fetch = async (path, options) => {
  assert.equal(path, "/api/dashboard/pending-writes");
  assert.equal(
    options.headers["X-Mark-Dashboard-Token"],
    "dashboard-write-token-00000001",
  );
  return {
    status: 200,
    ok: true,
    async text() {
      return JSON.stringify({pending_writes: records});
    },
  };
};

(async () => {
  await refreshPendingWrites();
  assert.equal(pendingRecoveryBlocked, false);
  assert.equal(pendingWrites.size, 65);
  assert.equal(writeUiReady(), true);
  assert.deepEqual(pendingWrites.get("ad:65:pause"), records[64]);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_clears_bound_terminal_202_receipt(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
const writeStatus = {textContent: "", className: ""};
globalThis.document = {
  getElementById(id) {
    if (id === "write-status") return writeStatus;
    throw new Error("unexpected element: " + id);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
let loadCalls = 0;
load = async () => { loadCalls += 1; };
const calls = [];
globalThis.fetch = async (path, options) => {
  calls.push({path, options});
  if (path === "/api/dashboard/pending-writes/ack") {
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({acknowledged: true});
      },
    };
  }
  return {
    status: 202,
    ok: true,
    async text() {
      return JSON.stringify({
        idempotency_key: options.headers["Idempotency-Key"],
        operation_receipt: {outcome: "ambiguous"},
        platform_retry_authorized: false,
      });
    },
  };
};

(async () => {
  await runPlatformWrite(
    "ad:2:pause",
    "POST",
    "/api/write/ads/2/pause",
    null,
    {adId: "2"},
  );
  assert.deepEqual(
    calls.map((item) => item.path),
    ["/api/write/ads/2/pause", "/api/dashboard/pending-writes/ack"],
  );
  assert.equal(pendingWrites.has("ad:2:pause"), false);
  assert.equal(pendingForAd("2"), null);
  assert.equal(loadCalls, 1);
  assert.match(writeStatus.textContent, /Ausgang unklar/i);
  assert.match(writeStatus.textContent, /Kein automatischer Plattform-Retry/i);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_retires_confirmed_create_draft_without_media(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
const writeStatus = {textContent: "", className: ""};
const fields = new Map([
  ["create-category", {value: "A > B"}],
  ["create-title", {value: "Confirmed title"}],
  ["create-description", {value: "Confirmed description"}],
  ["create-price", {value: "1"}],
  ["create-media", {files: [], value: ""}],
]);
globalThis.document = {
  getElementById(id) {
    if (id === "write-status") return writeStatus;
    if (fields.has(id)) return fields.get(id);
    throw new Error("unexpected element: " + id);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
load = async () => {};
let createCalls = 0;
let ackCalls = 0;
globalThis.fetch = async (path, options) => {
  if (path === "/api/write/ads") {
    createCalls += 1;
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({
          idempotency_key: options.headers["Idempotency-Key"],
          operation_receipt: {outcome: "confirmed"},
          platform_retry_authorized: false,
        });
      },
    };
  }
  if (path === "/api/dashboard/pending-writes/ack") {
    ackCalls += 1;
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({acknowledged: true});
      },
    };
  }
  throw new Error("unexpected path: " + path);
};

(async () => {
  await submitCreate({preventDefault() {}});
  assert.equal(createCalls, 1);
  assert.equal(ackCalls, 1);
  assert.equal(pendingWrites.size, 0);
  assert.equal(fields.get("create-category").value, "");
  assert.equal(fields.get("create-title").value, "");
  assert.equal(fields.get("create-description").value, "");
  assert.equal(fields.get("create-price").value, "");

  await submitCreate({preventDefault() {}});
  assert.equal(createCalls, 1);
  assert.equal(ackCalls, 1);
  assert.match(writeStatus.textContent, /Kategoriepfad/i);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_retires_ambiguous_create_draft_without_media(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
const writeStatus = {textContent: "", className: ""};
const fields = new Map([
  ["create-category", {value: "A > B"}],
  ["create-title", {value: "Ambiguous title"}],
  ["create-description", {value: "Ambiguous description"}],
  ["create-price", {value: "1"}],
  ["create-media", {files: [], value: ""}],
]);
globalThis.document = {
  getElementById(id) {
    if (id === "write-status") return writeStatus;
    if (fields.has(id)) return fields.get(id);
    throw new Error("unexpected element: " + id);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
let loadCalls = 0;
load = async () => { loadCalls += 1; };
let createCalls = 0;
let ackCalls = 0;
globalThis.fetch = async (path, options) => {
  if (path === "/api/write/ads") {
    createCalls += 1;
    return {
      status: 202,
      ok: true,
      async text() {
        return JSON.stringify({
          idempotency_key: options.headers["Idempotency-Key"],
          operation_receipt: {outcome: "ambiguous"},
          platform_retry_authorized: false,
        });
      },
    };
  }
  if (path === "/api/dashboard/pending-writes/ack") {
    ackCalls += 1;
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({acknowledged: true});
      },
    };
  }
  throw new Error("unexpected path: " + path);
};

(async () => {
  await submitCreate({preventDefault() {}});
  assert.equal(createCalls, 1);
  assert.equal(ackCalls, 1);
  assert.equal(loadCalls, 1);
  assert.equal(pendingWrites.size, 0);
  assert.equal(createInFlight, false);
  assert.equal(fields.get("create-category").value, "");
  assert.equal(fields.get("create-title").value, "");
  assert.equal(fields.get("create-description").value, "");
  assert.equal(fields.get("create-price").value, "");

  await submitCreate({preventDefault() {}});
  assert.equal(createCalls, 1);
  assert.equal(ackCalls, 1);
  assert.match(writeStatus.textContent, /Kategoriepfad/i);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_retires_content_confirmed_media_create_draft(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
const writeStatus = {textContent: "", className: ""};
const mediaInput = {
  files: [{name: "one.jpg", type: "image/jpeg", size: 10}],
  _value: "C:\\fakepath\\one.jpg",
  get value() { return this._value; },
  set value(next) {
    this._value = next;
    if (next === "") this.files = [];
  },
};
const fields = new Map([
  ["create-category", {value: "A > B"}],
  ["create-title", {value: "Ambiguous media title"}],
  ["create-description", {value: "Ambiguous media description"}],
  ["create-price", {value: "1"}],
  ["create-media", mediaInput],
]);
globalThis.document = {
  getElementById(id) {
    if (id === "write-status") return writeStatus;
    if (fields.has(id)) return fields.get(id);
    throw new Error("unexpected element: " + id);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
load = async () => {};
let stageCalls = 0;
let createCalls = 0;
let ackCalls = 0;
globalThis.fetch = async (path, options) => {
  if (path === "/api/write/media/stage") {
    stageCalls += 1;
    return {
      status: 201,
      ok: true,
      async text() {
        return JSON.stringify({media_ref: "media_ambiguous"});
      },
    };
  }
  if (path === "/api/write/media/ads") {
    createCalls += 1;
    return {
      status: 202,
      ok: true,
      async text() {
        return JSON.stringify({
          idempotency_key: options.headers["Idempotency-Key"],
          operation_receipt: {outcome: "confirmed"},
          platform_retry_authorized: false,
        });
      },
    };
  }
  if (path === "/api/dashboard/pending-writes/ack") {
    ackCalls += 1;
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({acknowledged: true});
      },
    };
  }
  throw new Error("unexpected path: " + path);
};

(async () => {
  await submitCreate({preventDefault() {}});
  assert.equal(stageCalls, 1);
  assert.equal(createCalls, 1);
  assert.equal(ackCalls, 1);
  assert.equal(pendingWrites.size, 0);
  assert.equal(createInFlight, false);
  assert.equal(mediaInput.value, "");
  assert.equal(mediaInput.files.length, 0);
  assert.equal(fields.get("create-category").value, "");

  await submitCreate({preventDefault() {}});
  assert.equal(stageCalls, 1);
  assert.equal(createCalls, 1);
  assert.equal(ackCalls, 1);
  assert.match(writeStatus.textContent, /Kategoriepfad/i);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_preserves_new_draft_while_prior_media_create_finishes(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
const writeStatus = {textContent: "", className: ""};
const oldFile = {name: "old.jpg", type: "image/jpeg", size: 10};
const newFile = {name: "new.jpg", type: "image/jpeg", size: 11};
const mediaInput = {
  files: [oldFile],
  _value: "C:\\fakepath\\old.jpg",
  get value() { return this._value; },
  set value(next) {
    this._value = next;
    if (next === "") this.files = [];
  },
};
const fields = new Map([
  ["create-category", {value: "A > B"}],
  ["create-title", {value: "First title"}],
  ["create-description", {value: "First description"}],
  ["create-price", {value: "1"}],
  ["create-media", mediaInput],
]);
globalThis.document = {
  getElementById(id) {
    if (id === "write-status") return writeStatus;
    if (fields.has(id)) return fields.get(id);
    throw new Error("unexpected element: " + id);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
load = async () => {};

let resolveStarted;
const started = new Promise((resolve) => { resolveStarted = resolve; });
let unblockCreate;
const createGate = new Promise((resolve) => { unblockCreate = resolve; });
let stageCalls = 0;
let createCalls = 0;
let ackCalls = 0;
globalThis.fetch = async (path, options) => {
  if (path === "/api/write/media/stage") {
    stageCalls += 1;
    return {
      status: 201,
      ok: true,
      async text() {
        return JSON.stringify({media_ref: "media_old"});
      },
    };
  }
  if (path === "/api/write/media/ads") {
    createCalls += 1;
    resolveStarted();
    await createGate;
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({
          idempotency_key: options.headers["Idempotency-Key"],
          operation_receipt: {outcome: "confirmed"},
          platform_retry_authorized: false,
        });
      },
    };
  }
  if (path === "/api/dashboard/pending-writes/ack") {
    ackCalls += 1;
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({acknowledged: true});
      },
    };
  }
  throw new Error("unexpected path: " + path);
};

(async () => {
  const submitting = submitCreate({preventDefault() {}});
  await started;

  fields.get("create-category").value = "C > D";
  fields.get("create-title").value = "Next title";
  fields.get("create-description").value = "Next description";
  fields.get("create-price").value = "2";
  mediaInput.files = [newFile];
  mediaInput._value = "C:\\fakepath\\new.jpg";

  unblockCreate();
  await submitting;

  assert.equal(stageCalls, 1);
  assert.equal(createCalls, 1);
  assert.equal(ackCalls, 1);
  assert.equal(pendingWrites.size, 0);
  assert.equal(createInFlight, false);
  assert.equal(fields.get("create-category").value, "C > D");
  assert.equal(fields.get("create-title").value, "Next title");
  assert.equal(fields.get("create-description").value, "Next description");
  assert.equal(fields.get("create-price").value, "2");
  assert.equal(mediaInput.value, "C:\\fakepath\\new.jpg");
  assert.equal(mediaInput.files.length, 1);
  assert.equal(mediaInput.files[0], newFile);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_preserves_new_draft_when_replaying_pending_create(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
const writeStatus = {textContent: "", className: ""};
const fields = new Map([
  ["create-category", {value: "C > D"}],
  ["create-title", {value: "Next title"}],
  ["create-description", {value: "Next description"}],
  ["create-price", {value: "2"}],
  ["create-media", {files: [], value: ""}],
]);
globalThis.document = {
  getElementById(id) {
    if (id === "write-status") return writeStatus;
    if (fields.has(id)) return fields.get(id);
    throw new Error("unexpected element: " + id);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
load = async () => {};
pendingWrites.set("create", {
  scope: "create",
  key: "ui:pending-old",
  method: "POST",
  path: "/api/write/ads",
  payload: {
    category_path: ["A", "B"],
    title: "First title",
    description: "First description",
    price_eur: 1,
  },
  adId: null,
});

let createCalls = 0;
let ackCalls = 0;
globalThis.fetch = async (path, options) => {
  if (path === "/api/write/ads") {
    createCalls += 1;
    assert.equal(options.headers["Idempotency-Key"], "ui:pending-old");
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({
          idempotency_key: "ui:pending-old",
          operation_receipt: {outcome: "confirmed"},
          platform_retry_authorized: false,
        });
      },
    };
  }
  if (path === "/api/dashboard/pending-writes/ack") {
    ackCalls += 1;
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({acknowledged: true});
      },
    };
  }
  throw new Error("unexpected path: " + path);
};

(async () => {
  await submitCreate({preventDefault() {}});

  assert.equal(createCalls, 1);
  assert.equal(ackCalls, 1);
  assert.equal(pendingWrites.has("create"), false);
  assert.equal(fields.get("create-category").value, "C > D");
  assert.equal(fields.get("create-title").value, "Next title");
  assert.equal(fields.get("create-description").value, "Next description");
  assert.equal(fields.get("create-price").value, "2");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_retires_create_draft_before_terminal_ack(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
const writeStatus = {textContent: "", className: ""};
const fields = new Map([
  ["create-category", {value: "A > B"}],
  ["create-title", {value: "Confirmed content title"}],
  ["create-description", {value: "Confirmed content description"}],
  ["create-price", {value: "1"}],
  ["create-media", {files: [], value: ""}],
]);
globalThis.document = {
  getElementById(id) {
    if (id === "write-status") return writeStatus;
    if (fields.has(id)) return fields.get(id);
    throw new Error("unexpected element: " + id);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
load = async () => {};

let ackFails = true;
let createCalls = 0;
let ackCalls = 0;
const keys = [];
globalThis.fetch = async (path, options) => {
  if (path === "/api/write/ads") {
    createCalls += 1;
    keys.push(options.headers["Idempotency-Key"]);
    return {
      status: 202,
      ok: true,
      async text() {
        return JSON.stringify({
          idempotency_key: options.headers["Idempotency-Key"],
          operation_receipt: {outcome: "confirmed"},
          platform_retry_authorized: false,
        });
      },
    };
  }
  if (path === "/api/dashboard/pending-writes/ack") {
    ackCalls += 1;
    assert.equal(fields.get("create-title").value, "");
    assert.equal(fields.get("create-category").value, "");
    if (ackFails) {
      return {
        status: 500,
        ok: false,
        async text() {
          return JSON.stringify({error: "dashboard_pending_store_error"});
        },
      };
    }
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({acknowledged: true});
      },
    };
  }
  throw new Error("unexpected path: " + path);
};

(async () => {
  await submitCreate({preventDefault() {}});
  assert.equal(createCalls, 1);
  assert.equal(ackCalls, 1);
  assert.equal(pendingWrites.has("create"), true);
  assert.equal(fields.get("create-title").value, "");
  const pendingKey = pendingWrites.get("create").key;
  assert.equal(keys[0], pendingKey);
  assert.match(writeStatus.textContent, /Recovery-ACK ist fehlgeschlagen/i);

  ackFails = false;
  await submitCreate({preventDefault() {}});
  assert.equal(createCalls, 2);
  assert.equal(ackCalls, 2);
  assert.equal(keys[1], pendingKey);
  assert.equal(pendingWrites.has("create"), false);
  assert.equal(fields.get("create-category").value, "");
  assert.equal(fields.get("create-title").value, "");
  assert.equal(fields.get("create-description").value, "");
  assert.equal(fields.get("create-price").value, "");
  assert.equal(createInFlight, false);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )


    def test_write_runtime_retires_create_after_keyed_execution_error(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
const writeStatus = {textContent: "", className: ""};
const fields = new Map([
  ["create-category", {value: "A > B"}],
  ["create-title", {value: "Possibly created title"}],
  ["create-description", {value: "Possibly created description"}],
  ["create-price", {value: "1"}],
  ["create-media", {files: [], value: ""}],
]);
globalThis.document = {
  getElementById(id) {
    if (id === "write-status") return writeStatus;
    if (fields.has(id)) return fields.get(id);
    throw new Error("unexpected element: " + id);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
load = async () => {};

let createCalls = 0;
let ackCalls = 0;
globalThis.fetch = async (path, options) => {
  if (path === "/api/write/ads") {
    createCalls += 1;
    return {
      status: 500,
      ok: false,
      async text() {
        return JSON.stringify({
          error: "write_execution_error",
          idempotency_key: options.headers["Idempotency-Key"],
          platform_retry_authorized: false,
        });
      },
    };
  }
  if (path === "/api/dashboard/pending-writes/ack") {
    ackCalls += 1;
    assert.equal(fields.get("create-title").value, "");
    assert.equal(fields.get("create-category").value, "");
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({acknowledged: true});
      },
    };
  }
  throw new Error("unexpected path: " + path);
};

(async () => {
  await submitCreate({preventDefault() {}});
  assert.equal(createCalls, 1);
  assert.equal(ackCalls, 1);
  assert.equal(pendingWrites.has("create"), false);
  assert.equal(fields.get("create-title").value, "");
  assert.equal(fields.get("create-description").value, "");
  assert.equal(fields.get("create-price").value, "");

  await submitCreate({preventDefault() {}});
  assert.equal(createCalls, 1);
  assert.equal(ackCalls, 1);
  assert.match(writeStatus.textContent, /Kategoriepfad/i);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_preserves_new_draft_during_ambiguous_media_create_recovery(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
const writeStatus = {textContent: "", className: ""};
const mediaInput = {
  files: [{name: "new-selection.jpg", type: "image/jpeg", size: 10}],
  _value: "C:\\fakepath\\new-selection.jpg",
  get value() { return this._value; },
  set value(next) {
    this._value = next;
    if (next === "") this.files = [];
  },
};
const fields = new Map([
  ["create-category", {value: "A > B"}],
  ["create-title", {value: "Visible draft"}],
  ["create-description", {value: "Visible description"}],
  ["create-price", {value: "1"}],
  ["create-media", mediaInput],
]);
globalThis.document = {
  getElementById(id) {
    if (id === "write-status") return writeStatus;
    if (fields.has(id)) return fields.get(id);
    throw new Error("unexpected element: " + id);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
load = async () => {};
const pending = {
  scope: "create-media",
  key: "ui:ambiguous-media-recovery",
  method: "POST",
  path: "/api/write/media/ads",
  payload: {
    category_path: ["A", "B"],
    title: "Persisted title",
    description: "Persisted description",
    price_eur: 1,
    media_refs: ["media_persisted"],
  },
  adId: null,
};
pendingWrites.set(pending.scope, pending);
let stageCalls = 0;
let createCalls = 0;
let ackCalls = 0;
globalThis.fetch = async (path, options) => {
  if (path === "/api/write/media/stage") {
    stageCalls += 1;
    throw new Error("recovery must not restage media");
  }
  if (path === "/api/write/media/ads") {
    createCalls += 1;
    assert.equal(options.headers["Idempotency-Key"], pending.key);
    assert.deepEqual(JSON.parse(options.body), pending.payload);
    return {
      status: 202,
      ok: true,
      async text() {
        return JSON.stringify({
          idempotency_key: pending.key,
          operation_receipt: {outcome: "ambiguous"},
          platform_retry_authorized: false,
        });
      },
    };
  }
  if (path === "/api/dashboard/pending-writes/ack") {
    ackCalls += 1;
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({acknowledged: true});
      },
    };
  }
  throw new Error("unexpected path: " + path);
};

(async () => {
  await submitCreate({preventDefault() {}});
  assert.equal(stageCalls, 0);
  assert.equal(createCalls, 1);
  assert.equal(ackCalls, 1);
  assert.equal(pendingWrites.size, 0);
  assert.equal(createInFlight, false);
  assert.equal(mediaInput.value, "C:\\fakepath\\new-selection.jpg");
  assert.equal(mediaInput.files.length, 1);
  assert.equal(mediaInput.files[0].name, "new-selection.jpg");
  assert.equal(fields.get("create-category").value, "A > B");
  assert.equal(fields.get("create-title").value, "Visible draft");
  assert.equal(fields.get("create-description").value, "Visible description");
  assert.equal(fields.get("create-price").value, "1");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_acks_deterministic_validation_failure_on_recovery(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
const writeStatus = {textContent: "", className: ""};
globalThis.document = {
  getElementById(id) {
    if (id === "write-status") return writeStatus;
    throw new Error("unexpected element: " + id);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
const original = {
  scope: "ad:2:update",
  key: "ui:invalid-update-recovery",
  method: "PATCH",
  path: "/api/write/ads/2",
  payload: {title: null},
  adId: "2",
};
pendingWrites.set(original.scope, original);

const calls = [];
globalThis.fetch = async (path, options) => {
  calls.push({path, options});
  if (path === "/api/dashboard/pending-writes/ack") {
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({acknowledged: true});
      },
    };
  }
  assert.equal(path, original.path);
  assert.equal(options.method, original.method);
  assert.equal(options.headers["Idempotency-Key"], original.key);
  assert.deepEqual(JSON.parse(options.body), original.payload);
  return {
    status: 400,
    ok: false,
    async text() {
      return JSON.stringify({
        error: "title_must_be_string",
        platform_retry_authorized: false,
      });
    },
  };
};

(async () => {
  await runPlatformWrite(
    original.scope,
    "PATCH",
    "/api/write/ads/999",
    {title: "must not replace persisted request"},
    {adId: "999"},
  );

  assert.deepEqual(
    calls.map((item) => item.path),
    [original.path, "/api/dashboard/pending-writes/ack"],
  );
  assert.equal(pendingWrites.has(original.scope), false);
  assert.match(writeStatus.textContent, /title_must_be_string/i);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_reuses_same_idempotency_request_after_transport_unknown(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
const writeStatus = {textContent: "", className: ""};
globalThis.document = {
  getElementById(id) {
    if (id === "write-status") return writeStatus;
    throw new Error("unexpected element: " + id);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
let loadCalls = 0;
load = async () => { loadCalls += 1; };
const calls = [];
globalThis.fetch = async (path, options) => {
  calls.push({path, options});
  if (path === "/api/dashboard/pending-writes/ack") {
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({acknowledged: true});
      },
    };
  }
  if (calls.length === 1) {
    throw new Error("transport interrupted");
  }
  if (calls.length === 2) {
    return {
      status: 403,
      ok: false,
      async text() {
        return JSON.stringify({
          error: "write_session_required",
          platform_retry_authorized: false,
        });
      },
    };
  }
  const pending = pendingWrites.get("create");
  return {
    status: 200,
    ok: true,
    async text() {
      return JSON.stringify({
        idempotency_key: pending.key,
        operation_receipt: {outcome: "confirmed"},
        platform_retry_authorized: false,
      });
    },
  };
};

(async () => {
  const original = {
    category_path: ["A", "B"],
    title: "first",
    description: "one",
    price_eur: 1,
  };
  await runPlatformWrite("create", "POST", "/api/write/ads", original);
  const pending = pendingWrites.get("create");
  assert.ok(pending);
  const firstKey = pending.key;
  assert.match(writeStatus.textContent, /kein automatischer Retry/i);

  await runPlatformWrite(
    "create",
    "POST",
    "/api/write/ads/999",
    {...original, title: "changed"},
  );
  assert.equal(calls.length, 2);
  assert.equal(calls[1].path, "/api/write/ads");
  assert.equal(calls[1].options.headers["Idempotency-Key"], firstKey);
  assert.deepEqual(JSON.parse(calls[1].options.body), original);
  assert.equal(pendingWrites.has("create"), true);
  assert.equal(loadCalls, 0);

  await runPlatformWrite(
    "create",
    "POST",
    "/api/write/ads/777",
    {...original, title: "changed again"},
  );
  assert.equal(calls.length, 4);
  assert.equal(calls[2].path, "/api/write/ads");
  assert.equal(calls[3].path, "/api/dashboard/pending-writes/ack");
  assert.equal(calls[2].options.headers["Idempotency-Key"], firstKey);
  assert.deepEqual(JSON.parse(calls[2].options.body), original);
  assert.equal(pendingWrites.has("create"), false);
  assert.equal(loadCalls, 1);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_uses_ascii_safe_media_stage_filenames(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
const calls = [];
globalThis.fetch = async (path, options) => {
  calls.push({path, options});
  return {
    status: 201,
    ok: true,
    async text() {
      return JSON.stringify({media_ref: "media_" + calls.length});
    },
  };
};

const files = [
  {name: "图片.jpg", type: "image/jpeg", size: 10},
  {name: "urlaub-😀.png", type: "image/png", size: 10},
  {name: "überraschung.webp", type: "image/webp", size: 10},
];

(async () => {
  const refs = await stageSelectedMedia(files);
  assert.deepEqual(refs, ["media_1", "media_2", "media_3"]);
  assert.deepEqual(
    calls.map((item) => item.options.headers["X-Mark-Media-Filename"]),
    ["upload.jpg", "upload.png", "upload.webp"],
  );
  for (const call of calls) {
    const value = call.options.headers["X-Mark-Media-Filename"];
    assert.match(value, /^[\x20-\x7e]+$/);
  }
  assert.deepEqual(
    calls.map((item) => item.options.headers["Content-Type"]),
    ["image/jpeg", "image/png", "image/webp"],
  );
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_discards_known_refs_after_partial_media_staging_failure(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
const calls = [];
globalThis.fetch = async (path, options) => {
  calls.push({path, options});
  if (path === "/api/write/media/stage" && calls.filter((item) => item.path === path).length === 1) {
    return {
      status: 201,
      ok: true,
      async text() { return JSON.stringify({media_ref: "media_first"}); },
    };
  }
  if (path === "/api/write/media/stage") {
    return {
      status: 400,
      ok: false,
      async text() { return JSON.stringify({error: "unsupported media image"}); },
    };
  }
  if (path === "/api/write/media/discard") {
    const refs = JSON.parse(options.body).media_refs;
    return {
      status: 200,
      ok: true,
      async text() { return JSON.stringify({discarded: refs.length}); },
    };
  }
  throw new Error("unexpected path: " + path);
};
const files = [
  {name: "one.jpg", type: "image/jpeg", size: 10},
  {name: "two.jpg", type: "image/jpeg", size: 10},
];

(async () => {
  await assert.rejects(stageSelectedMedia(files), /unsupported media image/i);
  assert.deepEqual(
    calls.map((item) => item.path),
    [
      "/api/write/media/stage",
      "/api/write/media/stage",
      "/api/write/media/discard",
    ],
  );
  assert.deepEqual(
    JSON.parse(calls[2].options.body),
    {media_refs: ["media_first"]},
  );
  assert.equal(calls[2].options.headers["Idempotency-Key"], undefined);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_validates_complete_create_before_media_staging(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
const writeStatus = {textContent: "", className: ""};
const fields = new Map([
  ["create-category", {value: ""}],
  ["create-title", {value: ""}],
  ["create-description", {value: ""}],
  ["create-price", {value: ""}],
  ["create-media", {files: []}],
]);
globalThis.document = {
  getElementById(id) {
    if (id === "write-status") return writeStatus;
    if (fields.has(id)) return fields.get(id);
    throw new Error("unexpected element: " + id);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
const calls = [];
globalThis.fetch = async (path, options) => {
  calls.push({path, options});
  throw new Error("fetch must not run for invalid create data");
};

function setCreate({
  category = "A > B",
  title = "Valid title",
  description = "Valid description",
  price = "1",
  files = [],
} = {}) {
  fields.get("create-category").value = category;
  fields.get("create-title").value = title;
  fields.get("create-description").value = description;
  fields.get("create-price").value = price;
  fields.get("create-media").files = files;
}

setCreate({
  category: " A > B ",
  title: "😀".repeat(32) + "x",
  description: "line 1\r\nline 2\rline 3",
  price: "99999999",
});
const normalized = createPayload();
assert.deepEqual(normalized.category_path, ["A", "B"]);
assert.equal(normalized.title, "😀".repeat(32) + "x");
assert.equal(normalized.description, "line 1\nline 2\nline 3");
assert.equal(normalized.price_eur, 99999999);

setCreate({title: "   "});
assert.throws(() => createPayload(), /Titel darf nicht leer/i);
setCreate({title: " surrounded "});
assert.throws(() => createPayload(), /umgebenden Whitespaces/i);
setCreate({title: "line\nbreak"});
assert.throws(() => createPayload(), /Zeilenumbrüche/i);
setCreate({title: "line\rbreak"});
assert.throws(() => createPayload(), /Zeilenumbrüche/i);
setCreate({title: "😀".repeat(33)});
assert.throws(() => createPayload(), /65 UTF-16/i);

setCreate({category: "A"});
assert.throws(() => createPayload(), /zwischen 2 und 6/i);
setCreate({category: "A>B>C>D>E>F>G"});
assert.throws(() => createPayload(), /zwischen 2 und 6/i);
setCreate({category: "A >   > B"});
assert.throws(() => createPayload(), /nicht leer/i);
setCreate({category: "x".repeat(121) + " > B"});
assert.throws(() => createPayload(), /120 Zeichen/i);
setCreate({category: " A > B > A "});
assert.throws(() => createPayload(), /doppelten Labels/i);

setCreate({title: "\uD800"});
assert.throws(() => createPayload(), /Unicode-Surrogates/i);
setCreate({description: "   "});
assert.throws(() => createPayload(), /Beschreibung darf nicht leer/i);
setCreate({description: "x".repeat(4001)});
assert.throws(() => createPayload(), /4000 UTF-16/i);
setCreate({price: "100000000"});
assert.throws(() => createPayload(), /99999999/i);

setCreate({
  title: "   ",
  files: [{name: "one.jpg", type: "image/jpeg", size: 10}],
});
(async () => {
  await submitCreate({preventDefault() {}});
  assert.equal(calls.length, 0);
  assert.match(writeStatus.textContent, /Titel darf nicht leer/i);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_blocks_concurrent_media_create_staging(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
const writeStatus = {textContent: "", className: ""};
const fields = new Map([
  ["create-category", {value: "A > B"}],
  ["create-title", {value: "Valid title"}],
  ["create-description", {value: "Valid description"}],
  ["create-price", {value: "1"}],
  ["create-media", {
    files: [{name: "one.jpg", type: "image/jpeg", size: 10}],
  }],
]);
globalThis.document = {
  getElementById(id) {
    if (id === "write-status") return writeStatus;
    if (fields.has(id)) return fields.get(id);
    throw new Error("unexpected element: " + id);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
load = async () => {};

let releaseFirstStage;
let markFirstStageStarted;
const firstStageStarted = new Promise((resolve) => {
  markFirstStageStarted = resolve;
});
const firstStageGate = new Promise((resolve) => {
  releaseFirstStage = resolve;
});
let stageCalls = 0;
let mediaCreateCalls = 0;
let ackCalls = 0;

globalThis.fetch = async (path, options) => {
  if (path === "/api/write/media/stage") {
    stageCalls += 1;
    if (stageCalls === 1) {
      markFirstStageStarted();
      await firstStageGate;
    }
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({media_ref: "media_" + stageCalls});
      },
    };
  }
  if (path === "/api/write/media/ads") {
    mediaCreateCalls += 1;
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({
          idempotency_key: options.headers["Idempotency-Key"],
          operation_receipt: {outcome: "confirmed"},
          platform_retry_authorized: false,
        });
      },
    };
  }
  if (path === "/api/dashboard/pending-writes/ack") {
    ackCalls += 1;
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({acknowledged: true});
      },
    };
  }
  throw new Error("unexpected path: " + path);
};

(async () => {
  const first = submitCreate({preventDefault() {}});
  await firstStageStarted;
  assert.equal(stageCalls, 1);
  assert.equal(createInFlight, true);

  fields.get("create-media").files = [];
  await submitCreate({preventDefault() {}});
  assert.equal(stageCalls, 1);
  assert.equal(mediaCreateCalls, 0);
  assert.equal(ackCalls, 0);
  assert.match(writeStatus.textContent, /läuft bereits/i);

  releaseFirstStage();
  await first;
  assert.equal(stageCalls, 1);
  assert.equal(mediaCreateCalls, 1);
  assert.equal(ackCalls, 1);
  assert.equal(createInFlight, false);

  assert.equal(fields.get("create-category").value, "A > B");
  assert.equal(fields.get("create-title").value, "Valid title");
  assert.equal(fields.get("create-description").value, "Valid description");
  assert.equal(fields.get("create-price").value, "1");
  assert.equal(fields.get("create-media").files.length, 0);
  assert.equal(stageCalls, 1);
  assert.equal(mediaCreateCalls, 1);
  assert.equal(ackCalls, 1);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_discards_media_when_cross_tab_create_loses_claim(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");
const writeStatus = {textContent: "", className: ""};
const fields = new Map([
  ["create-category", {value: "A > B"}],
  ["create-title", {value: "Valid title"}],
  ["create-description", {value: "Valid description"}],
  ["create-price", {value: "1"}],
  ["create-media", {
    files: [{name: "loser.jpg", type: "image/jpeg", size: 10}],
  }],
]);
globalThis.document = {
  getElementById(id) {
    if (id === "write-status") return writeStatus;
    if (fields.has(id)) return fields.get(id);
    throw new Error("unexpected element: " + id);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
const winner = {
  scope: "create-media",
  key: "ui:winner",
  method: "POST",
  path: "/api/write/media/ads",
  payload: {
    category_path: ["A", "B"],
    title: "Winner",
    description: "Winner description",
    price_eur: 1,
    media_refs: ["media_winner"],
  },
  adId: null,
};
let loadCalls = 0;
load = async () => {
  loadCalls += 1;
  pendingWrites.clear();
  pendingWrites.set(winner.scope, winner);
};

const calls = [];
globalThis.fetch = async (path, options) => {
  calls.push({path, options});
  if (path === "/api/write/media/stage") {
    return {
      status: 201,
      ok: true,
      async text() {
        return JSON.stringify({media_ref: "media_loser"});
      },
    };
  }
  if (path === "/api/write/media/ads") {
    return {
      status: 409,
      ok: false,
      async text() {
        return JSON.stringify({
          error: "dashboard_pending_write_conflict",
          platform_retry_authorized: false,
        });
      },
    };
  }
  if (path === "/api/write/media/discard") {
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({discarded: 1});
      },
    };
  }
  throw new Error("unexpected path: " + path);
};

(async () => {
  await submitCreate({preventDefault() {}});

  assert.deepEqual(
    calls.map((item) => item.path),
    [
      "/api/write/media/stage",
      "/api/write/media/ads",
      "/api/write/media/discard",
    ],
  );
  assert.deepEqual(
    JSON.parse(calls[1].options.body).media_refs,
    ["media_loser"],
  );
  assert.equal(
    calls[1].options.headers["Idempotency-Key"].startsWith("ui:"),
    true,
  );
  assert.deepEqual(
    JSON.parse(calls[2].options.body),
    {media_refs: ["media_loser"]},
  );
  assert.equal(calls[2].options.headers["Idempotency-Key"], undefined);
  assert.equal(loadCalls, 1);
  assert.equal(pendingWrites.get("create-media"), winner);
  assert.equal(createInFlight, false);
  assert.match(writeStatus.textContent, /nicht weitergeleitet/i);
  assert.match(writeStatus.textContent, /verworfen/i);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_omits_unknown_unchanged_content_fields(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");

class Element {
  constructor() {
    this.value = "";
    this.textContent = "";
    this.className = "";
  }
  scrollIntoView() {}
}

const elements = new Map([
  ["manage-ad-id", new Element()],
  ["manage-title", new Element()],
  ["manage-description", new Element()],
  ["manage-state", new Element()],
  ["manage-form", new Element()],
  ["write-status", new Element()],
]);
globalThis.document = {
  getElementById(id) {
    if (!elements.has(id)) throw new Error("unexpected element: " + id);
    return elements.get(id);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
load = async () => {};

const calls = [];
globalThis.fetch = async (path, options) => {
  calls.push({path, options});
  if (path === "/api/dashboard/pending-writes/ack") {
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({acknowledged: true});
      },
    };
  }
  return {
    status: 200,
    ok: true,
    async text() {
      return JSON.stringify({
        idempotency_key: options.headers["Idempotency-Key"],
        operation_receipt: {outcome: "confirmed"},
        platform_retry_authorized: false,
      });
    },
  };
};

function select(ad) {
  latestAdsById = new Map([[ad.ad_id, ad]]);
  populateManageForm(ad);
}

const unknownDescription = {
  ad_id: "2",
  title: "Old title",
  description: null,
  lifecycle_state: "active",
  present: true,
};

(async () => {
  select(unknownDescription);
  assert.equal(elements.get("manage-description").value, "");
  await saveManagedAd();
  assert.equal(calls.length, 0);
  assert.match(elements.get("write-status").textContent, /Keine Content-Änderungen/i);

  elements.get("manage-title").value = "New title";
  await saveManagedAd();
  assert.equal(calls.length, 2);
  assert.equal(calls[0].path, "/api/write/ads/2");
  assert.equal(calls[0].options.method, "PATCH");
  assert.deepEqual(JSON.parse(calls[0].options.body), {title: "New title"});
  assert.equal(calls[1].path, "/api/dashboard/pending-writes/ack");

  calls.length = 0;
  select({...unknownDescription, title: "New title"});
  elements.get("manage-description").value = "Explicit description";
  await saveManagedAd();
  assert.equal(calls.length, 2);
  assert.deepEqual(
    JSON.parse(calls[0].options.body),
    {description: "Explicit description"},
  );

  calls.length = 0;
  select({...unknownDescription, title: "New title"});
  const persisted = {
    scope: "ad:2:update",
    key: "ui:persisted-update",
    method: "PATCH",
    path: "/api/write/ads/2",
    payload: {description: "Persisted description"},
    adId: "2",
  };
  pendingWrites.set(persisted.scope, persisted);
  await saveManagedAd();
  assert.equal(calls.length, 2);
  assert.equal(calls[0].path, persisted.path);
  assert.equal(calls[0].options.method, persisted.method);
  assert.equal(calls[0].options.headers["Idempotency-Key"], persisted.key);
  assert.deepEqual(JSON.parse(calls[0].options.body), persisted.payload);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_rebases_managed_content_after_completed_update(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");

class Element {
  constructor() {
    this.value = "";
    this.textContent = "";
    this.className = "";
  }
  scrollIntoView() {}
}

const elements = new Map([
  ["manage-ad-id", new Element()],
  ["manage-title", new Element()],
  ["manage-description", new Element()],
  ["manage-state", new Element()],
  ["manage-form", new Element()],
  ["write-status", new Element()],
]);
globalThis.document = {
  getElementById(id) {
    if (!elements.has(id)) throw new Error("unexpected element: " + id);
    return elements.get(id);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";

const calls = [];
let editDuringPatch = null;
load = async () => {
  throw new Error("refresh failed");
};
globalThis.fetch = async (path, options) => {
  calls.push({path, options});
  if (path === "/api/dashboard/pending-writes/ack") {
    const submitted = JSON.parse(calls[calls.length - 2].options.body);
    if (Object.prototype.hasOwnProperty.call(submitted, "title")) {
      assert.equal(managedAdBaseline.title, submitted.title);
    }
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({acknowledged: true});
      },
    };
  }
  if (editDuringPatch !== null) {
    elements.get("manage-title").value = editDuringPatch;
    editDuringPatch = null;
  }
  return {
    status: 200,
    ok: true,
    async text() {
      return JSON.stringify({
        idempotency_key: options.headers["Idempotency-Key"],
        operation_receipt: {outcome: "confirmed"},
        platform_retry_authorized: false,
      });
    },
  };
};

const original = {
  ad_id: "2",
  title: "Old title",
  description: "Description",
  lifecycle_state: "active",
  present: true,
};

(async () => {
  latestAdsById = new Map([[original.ad_id, original]]);
  populateManageForm(original);
  elements.get("manage-title").value = "New title";

  await saveManagedAd();
  assert.equal(calls.length, 2);
  assert.deepEqual(JSON.parse(calls[0].options.body), {title: "New title"});
  assert.equal(managedAdBaseline.title, "New title");
  assert.equal(elements.get("manage-title").value, "New title");
  assert.match(
    elements.get("write-status").textContent,
    /Dashboard-Aktualisierung fehlgeschlagen/i,
  );

  calls.length = 0;
  await saveManagedAd();
  assert.equal(calls.length, 0);
  assert.match(elements.get("write-status").textContent, /Keine Content-Änderungen/i);

  elements.get("manage-title").value = "Saved server title";
  editDuringPatch = "Later local edit";
  await saveManagedAd();
  assert.equal(managedAdBaseline.title, "Saved server title");
  assert.equal(elements.get("manage-title").value, "Later local edit");

  calls.length = 0;
  await saveManagedAd();
  assert.equal(calls.length, 2);
  assert.deepEqual(JSON.parse(calls[0].options.body), {title: "Later local edit"});
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_fences_ambiguous_content_update_after_ack(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");

class Element {
  constructor() {
    this.value = "";
    this.textContent = "";
    this.className = "";
  }
  scrollIntoView() {}
}

const elements = new Map([
  ["manage-ad-id", new Element()],
  ["manage-title", new Element()],
  ["manage-description", new Element()],
  ["manage-state", new Element()],
  ["manage-form", new Element()],
  ["write-status", new Element()],
]);
globalThis.document = {
  getElementById(id) {
    if (!elements.has(id)) throw new Error("unexpected element: " + id);
    return elements.get(id);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
load = async () => {};

const calls = [];
globalThis.fetch = async (path, options) => {
  calls.push({path, options});
  if (path === "/api/dashboard/pending-writes/ack") {
    assert.equal(managedAdBaseline, null);
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({acknowledged: true});
      },
    };
  }
  return {
    status: 202,
    ok: true,
    async text() {
      return JSON.stringify({
        idempotency_key: options.headers["Idempotency-Key"],
        operation_receipt: {outcome: "ambiguous"},
        platform_retry_authorized: false,
      });
    },
  };
};

const original = {
  ad_id: "2",
  title: "Old title",
  description: "Description",
  lifecycle_state: "active",
  present: true,
};

(async () => {
  latestAdsById = new Map([[original.ad_id, original]]);
  populateManageForm(original);
  elements.get("manage-title").value = "Attempted title";

  await saveManagedAd();
  assert.equal(calls.length, 2);
  assert.equal(calls[0].path, "/api/write/ads/2");
  assert.equal(calls[0].options.method, "PATCH");
  assert.deepEqual(JSON.parse(calls[0].options.body), {title: "Attempted title"});
  assert.equal(calls[1].path, "/api/dashboard/pending-writes/ack");
  assert.equal(pendingForAd("2"), null);
  assert.equal(managedAdBaseline, null);
  assert.equal(elements.get("manage-title").value, "Attempted title");
  assert.match(elements.get("write-status").textContent, /nicht mit einem neuen Idempotency-Key/i);

  calls.length = 0;
  await saveManagedAd();
  assert.equal(calls.length, 0);
  assert.match(
    elements.get("write-status").textContent,
    /Ausgangswerte sind nicht mehr eindeutig gebunden/i,
  );

  populateManageForm(original);
  elements.get("manage-title").value = "Explicit retry title";
  await saveManagedAd();
  assert.equal(calls.length, 2);
  assert.deepEqual(
    JSON.parse(calls[0].options.body),
    {title: "Explicit retry title"},
  );
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_write_runtime_exposes_absent_pending_recovery_without_new_writes(self) -> None:
        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        definitions, marker, _ = javascript.partition(
            'byId("reload").addEventListener',
        )
        self.assertTrue(marker)

        harness = definitions + r"""
const assert = require("node:assert/strict");

class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.hidden = false;
    this.textContent = "";
    this.className = "";
    this.value = "";
    this.type = "";
  }
  replaceChildren(...children) {
    this.children = children;
  }
  append(...children) {
    this.children.push(...children);
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  addEventListener(name, handler) {
    this.listeners[name] = handler;
  }
  scrollIntoView() {}
}

const elements = new Map([
  ["ads-body", new Element("tbody")],
  ["empty", new Element("p")],
  ["manage-ad-id", new Element("input")],
  ["manage-title", new Element("input")],
  ["manage-description", new Element("textarea")],
  ["manage-state", new Element("span")],
  ["manage-form", new Element("form")],
  ["write-status", new Element("p")],
]);
globalThis.document = {
  getElementById(id) {
    if (!elements.has(id)) throw new Error("unexpected element: " + id);
    return elements.get(id);
  },
  createElement(tagName) {
    return new Element(tagName);
  },
  querySelectorAll() {
    return [];
  },
};
writeUiAvailable = true;
writeToken = "dashboard-write-token-00000001";
let loadCalls = 0;
load = async () => { loadCalls += 1; };

const original = {
  scope: "ad:2:delete",
  key: "ui:pending-delete-key",
  method: "DELETE",
  path: "/api/write/ads/2",
  payload: null,
  adId: "2",
};
pendingWrites.set(original.scope, original);

const absent = {
  ad_id: "2",
  title: "Deleted ad",
  description: "Original description",
  lifecycle_state: "absent",
  present: false,
  views: 1,
  watch_count: 2,
  reply_count: 3,
  observed_at: "2026-10-05T18:00:00Z",
};
renderAds([absent]);
const row = elements.get("ads-body").children[0];
const actionCell = row.children[7];
assert.equal(actionCell.children.length, 1);
const recoveryButton = actionCell.children[0];
assert.equal(recoveryButton.textContent, "Recovery");
recoveryButton.listeners.click();
assert.equal(elements.get("manage-ad-id").value, "2");
assert.equal(elements.get("manage-title").value, "Deleted ad");
assert.equal(elements.get("manage-description").value, "Original description");

const calls = [];
globalThis.fetch = async (path, options) => {
  calls.push({path, options});
  if (path === "/api/dashboard/pending-writes/ack") {
    return {
      status: 200,
      ok: true,
      async text() {
        return JSON.stringify({acknowledged: true});
      },
    };
  }
  return {
    status: 200,
    ok: true,
    async text() {
      return JSON.stringify({
        idempotency_key: original.key,
        operation_receipt: {outcome: "confirmed"},
        platform_retry_authorized: false,
      });
    },
  };
};

(async () => {
  await runAdAction(
    "update",
    "PATCH",
    "",
    {title: "must not replace pending delete"},
  );
  assert.equal(calls.length, 0);
  assert.equal(pendingWrites.get(original.scope), original);

  await runAdAction("delete", "DELETE", "");
  assert.equal(calls.length, 2);
  assert.equal(calls[0].path, original.path);
  assert.equal(calls[0].options.method, original.method);
  assert.equal(calls[0].options.headers["Idempotency-Key"], original.key);
  assert.equal(calls[0].options.body, null);
  assert.equal(calls[1].path, "/api/dashboard/pending-writes/ack");
  assert.equal(pendingWrites.size, 0);
  assert.equal(loadCalls, 1);

  await runAdAction(
    "update",
    "PATCH",
    "",
    {title: "must stay blocked after recovery"},
  );
  assert.equal(calls.length, 2);
  assert.match(elements.get("write-status").textContent, /keine neuen Writes/i);

  renderAds([{
    ...absent,
    ad_id: "3",
    title: "Absent without pending",
  }]);
  const absentWithoutPending = elements.get("ads-body").children[0];
  assert.equal(absentWithoutPending.children[7].children.length, 0);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"node stderr:\n{completed.stderr}\nnode stdout:\n{completed.stdout}",
        )

    def test_non_get_methods_are_405_and_no_write_route_exists(self) -> None:
        _, _, config_body = self.get("/api/dashboard/config")
        self.assertEqual(
            json.loads(config_body),
            {"write_ui_available": False},
        )
        for path in (
            "/api/ads/1",
            "/api/analytics/ads?metric=views",
        ):
            request = Request(
                self.base + path,
                data=b"{}",
                method="POST",
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=2)
            self.assertEqual(error.exception.code, 405)
            self.assertEqual(error.exception.headers["Allow"], "GET")
            self.assertEqual(
                json.loads(error.exception.read()),
                {"error": "method_not_allowed"},
            )

    def test_unknown_route_is_404(self) -> None:
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base + "/api/write/delete", timeout=2)
        self.assertEqual(error.exception.code, 404)

    def test_server_refuses_non_loopback_bind(self) -> None:
        with self.assertRaises(ValueError):
            create_server(self.store, host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()