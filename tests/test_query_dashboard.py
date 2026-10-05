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
from mark_api.dashboard import DashboardWriteProxy, create_server
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

    def test_proxy_backend_transport_failure_is_unknown_and_not_retry_authorized(self) -> None:
        backend_host, backend_port = self.backend.server_address
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
  assert.equal(calls.length, 1);
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
  if (calls.length === 1) {
    throw new Error("transport interrupted");
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