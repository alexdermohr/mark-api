from __future__ import annotations

import io
import json
import signal
import sqlite3
import tempfile
import threading
from time import monotonic
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

from mark_api.analytics import AnalyticsContract
from mark_api.dashboard import DashboardWriteProxy
from mark_api.domain import AdSnapshot, LifecycleState
from mark_api.email_import import EmailImportReport
from mark_api.launcher import (
    ProductLauncherError,
    build_product_launcher,
    main,
)
from mark_api.private_web import PrivateWebSubmitUnknownError
from mark_api.results import ReadResult, ReadStatus
from mark_api.storage import SnapshotStore
from mark_api.write_api import WriteCapability


NOW = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)


def reaction_email(
    *,
    ad_id: str = "3333333333",
    conversation_id: str = "abc12:def34:ghi56",
    provider_message_id: str = "11111111-2222-3333-4444-555555555555",
) -> bytes:
    message = EmailMessage(policy=policy.default)
    message["From"] = "Kleinanzeigen <noreply@mail.kleinanzeigen.de>"
    message["To"] = "owner@example.invalid"
    message["Date"] = "Mon, 5 Oct 2026 06:30:00 +0000"
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


class InventoryRuntime:
    def __init__(self, result, *, close_failures: int = 0) -> None:
        self.result = result
        self.read_calls = 0
        self.close_calls = 0
        self.closed = False
        self.close_failures = close_failures

    def read_inventory(self):
        self.read_calls += 1
        return self.result

    def close(self) -> None:
        self.close_calls += 1
        if self.close_failures:
            self.close_failures -= 1
            raise OSError("close failed")
        self.closed = True


class DashboardServer:
    def __init__(self, *, close_failures: int = 0) -> None:
        self.server_address = ("127.0.0.1", 18765)
        self.serve_calls = 0
        self.shutdown_calls = 0
        self.close_calls = 0
        self.closed = False
        self.close_failures = close_failures

    def serve_forever(self) -> None:
        self.serve_calls += 1

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def server_close(self) -> None:
        self.close_calls += 1
        if self.close_failures:
            self.close_failures -= 1
            raise OSError("close failed")
        self.closed = True


class BlockingDashboardServer(DashboardServer):
    def __init__(self, *, shutdown_failures: int = 0) -> None:
        super().__init__()
        self.shutdown_failures = shutdown_failures
        self.started = threading.Event()
        self.release = threading.Event()

    def serve_forever(self) -> None:
        self.serve_calls += 1
        self.started.set()
        self.release.wait()

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.shutdown_failures:
            self.shutdown_failures -= 1
            raise OSError("shutdown failed")
        self.release.set()


class WriteRuntime:
    def __init__(self) -> None:
        self.server_address = ("127.0.0.1", 18766)
        self.start_calls = 0
        self.close_calls = 0
        self.reconcile_calls = 0
        self.closed = False
        self.start_error: Exception | None = None
        self.close_error: Exception | None = None
        self.pending_unknown = False

    def start(self) -> tuple[str, int]:
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        return self.server_address

    def reconcile_media_submit(self) -> None:
        self.reconcile_calls += 1
        self.pending_unknown = False

    def close(self) -> None:
        self.close_calls += 1
        if self.pending_unknown:
            raise PrivateWebSubmitUnknownError("create_media_submit_settle")
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


class ProductLauncherTests(unittest.TestCase):
    TOKEN = "fake-launcher-test-token-0001"
    DASHBOARD_TOKEN = "fake-dashboard-write-token-0001"

    def setUp(self) -> None:
        self.write_runtime = WriteRuntime()
        write_patch = patch(
            "mark_api.launcher.build_private_web_write_api_runtime",
            return_value=self.write_runtime,
        )
        self.write_factory = write_patch.start()
        self.addCleanup(write_patch.stop)

        token_patch = patch(
            "mark_api.launcher._default_write_token",
            return_value=self.TOKEN,
        )
        token_patch.start()
        self.addCleanup(token_patch.stop)

        dashboard_token_patch = patch(
            "mark_api.launcher._default_dashboard_write_token",
            return_value=self.DASHBOARD_TOKEN,
        )
        dashboard_token_patch.start()
        self.addCleanup(dashboard_token_patch.stop)

    def make_db(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "mark.sqlite"
        # Normal product startup opens an explicitly initialized store.
        SnapshotStore(path)
        return tmp, path

    def test_wrong_database_path_fails_before_inventory_or_write_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "typo" / "mark.sqlite"
            inventory_created = False

            def runtime_factory(**kwargs):
                nonlocal inventory_created
                inventory_created = True
                return InventoryRuntime(ReadResult.success_empty(()))

            with self.assertRaisesRegex(
                ProductLauncherError, "local snapshot store startup failed"
            ):
                build_product_launcher(
                    db_path=missing,
                    cdp_port=9222,
                    runtime_factory=runtime_factory,
                )
            self.assertFalse(inventory_created)
            self.assertEqual(self.write_runtime.start_calls, 0)
            self.assertFalse(missing.exists())
            self.assertFalse(missing.parent.exists())

    def test_missing_write_request_table_prevents_launch_and_recreation(self) -> None:
        _tmp, db = self.make_db()
        with sqlite3.connect(db) as connection:
            connection.execute("DROP TABLE write_api_requests")
        inventory_called = False

        def runtime_factory(**kwargs):
            nonlocal inventory_called
            inventory_called = True
            return InventoryRuntime(ReadResult.success_empty(()))

        for initialize_db in (False, True):
            with self.subTest(initialize_db=initialize_db):
                with self.assertRaisesRegex(
                    ProductLauncherError, "local snapshot store startup failed"
                ):
                    build_product_launcher(
                        db_path=db,
                        cdp_port=9222,
                        initialize_db=initialize_db,
                        runtime_factory=runtime_factory,
                    )
        self.assertFalse(inventory_called)
        self.assertEqual(self.write_runtime.start_calls, 0)
        with sqlite3.connect(db) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='write_api_requests'"
                ).fetchone()
            )

    def test_corrupted_database_fails_before_inventory_or_writes(self) -> None:
        _tmp, db = self.make_db()
        db.write_bytes(b"corrupted local sqlite store")
        inventory_called = False

        def runtime_factory(**kwargs):
            nonlocal inventory_called
            inventory_called = True
            return InventoryRuntime(ReadResult.success_empty(()))

        with self.assertRaisesRegex(
            ProductLauncherError, "local snapshot store startup failed"
        ):
            build_product_launcher(
                db_path=db,
                cdp_port=9222,
                runtime_factory=runtime_factory,
            )
        self.assertFalse(inventory_called)
        self.assertEqual(self.write_runtime.start_calls, 0)

    def test_explicit_initialization_creates_store_then_normal_restart_opens_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "new" / "mark.sqlite"
            inventory = InventoryRuntime(ReadResult.success_empty(()))
            launcher = build_product_launcher(
                db_path=db,
                cdp_port=9222,
                initialize_db=True,
                runtime_factory=lambda **kwargs: inventory,
                dashboard_factory=lambda *args, **kwargs: DashboardServer(),
            )
            launcher.close()
            self.assertTrue(db.is_file())
            self.assertTrue(SnapshotStore(db, create_if_missing=False).is_ready())
            reopened = build_product_launcher(
                db_path=db,
                cdp_port=9222,
                runtime_factory=lambda **kwargs: InventoryRuntime(
                    ReadResult.success_empty(())
                ),
                dashboard_factory=lambda *args, **kwargs: DashboardServer(),
            )
            reopened.close()
            self.assertEqual(self.write_runtime.start_calls, 2)

    def test_main_forwarding_initialization_is_explicit(self) -> None:
        with patch(
            "mark_api.launcher.build_product_launcher",
            side_effect=ProductLauncherError("local snapshot store startup failed"),
        ) as builder, patch("sys.stderr", io.StringIO()):
            status = main(
                [
                    "--db",
                    "/tmp/nonexistent-a4-mark.sqlite",
                    "--cdp-port",
                    "9222",
                    "--init-db",
                ]
            )
        self.assertEqual(status, 2)
        self.assertIs(builder.call_args.kwargs["initialize_db"], True)

    def test_default_write_runtime_is_composed_started_and_exposed(self) -> None:
        _tmp, db = self.make_db()
        inventory = InventoryRuntime(ReadResult.success_empty(()))
        server = DashboardServer()
        clock = lambda: NOW

        dashboard_calls = []

        def dashboard_factory(*args, **kwargs):
            dashboard_calls.append((args, kwargs))
            return server

        launcher = build_product_launcher(
            db_path=db,
            cdp_port=9222,
            dashboard_port=0,
            write_port=0,
            timeout_seconds=3.5,
            analytics_contract=AnalyticsContract(),
            runtime_factory=lambda **kwargs: inventory,
            dashboard_factory=dashboard_factory,
            clock=clock,
        )
        self.addCleanup(launcher.close)

        self.assertEqual(self.write_runtime.start_calls, 1)
        self.assertEqual(
            launcher.write_server_address,
            ("127.0.0.1", 18766),
        )
        self.assertEqual(launcher.write_bearer_token, self.TOKEN)

        kwargs = self.write_factory.call_args.kwargs
        self.assertEqual(kwargs["cdp_port"], 9222)
        self.assertIsInstance(kwargs["store"], SnapshotStore)
        self.assertEqual(kwargs["port"], 0)
        self.assertEqual(kwargs["timeout_seconds"], 3.5)
        self.assertIs(kwargs["clock"], clock)
        self.assertTrue(kwargs["core_writes_enabled"])
        self.assertTrue(kwargs["media_writes_enabled"])

        access = kwargs["access"]
        self.assertEqual(access.principal, "mark-api-launch")
        self.assertEqual(access.bearer_token, self.TOKEN)
        self.assertEqual(access.capabilities, frozenset(WriteCapability))
        self.assertTrue(access.writes_enabled)

        self.assertEqual(len(dashboard_calls), 1)
        dashboard_kwargs = dashboard_calls[0][1]
        proxy = dashboard_kwargs["write_proxy"]
        self.assertIsInstance(proxy, DashboardWriteProxy)
        self.assertEqual(proxy.host, "127.0.0.1")
        self.assertEqual(proxy.port, 18766)
        self.assertEqual(proxy.bearer_token, self.TOKEN)
        self.assertEqual(proxy.ui_token, self.DASHBOARD_TOKEN)
        self.assertEqual(proxy.timeout_seconds, 42.0)
        self.assertEqual(
            launcher.dashboard_url,
            "http://127.0.0.1:18765/"
            f"#write_token={self.DASHBOARD_TOKEN}",
        )

    def test_dashboard_url_percent_encodes_write_token_fragment(self) -> None:
        _tmp, db = self.make_db()
        inventory = InventoryRuntime(ReadResult.success_empty(()))
        server = DashboardServer()
        dashboard_token = "abcdefghijklmnop&x=y#z"
        captured = {}

        def dashboard_factory(*args, **kwargs):
            captured["write_proxy"] = kwargs["write_proxy"]
            return server

        launcher = build_product_launcher(
            db_path=db,
            cdp_port=9222,
            dashboard_port=0,
            write_port=0,
            timeout_seconds=3.5,
            analytics_contract=AnalyticsContract(),
            runtime_factory=lambda **kwargs: inventory,
            dashboard_factory=dashboard_factory,
            dashboard_token_factory=lambda: dashboard_token,
            clock=lambda: NOW,
        )
        self.addCleanup(launcher.close)

        self.assertEqual(captured["write_proxy"].ui_token, dashboard_token)
        self.assertEqual(
            launcher.dashboard_url,
            "http://127.0.0.1:18765/"
            "#write_token=abcdefghijklmnop%26x%3Dy%23z",
        )

    def test_build_syncs_inventory_marks_missing_tracked_ads_absent(self) -> None:
        _tmp, db = self.make_db()
        store = SnapshotStore(db)
        store.append_ad_snapshot(
            AdSnapshot(
                ad_id="1111111111",
                observed_at=NOW - timedelta(minutes=5),
                source="old",
                lifecycle_state=LifecycleState.ACTIVE,
                views=9,
            )
        )
        current = AdSnapshot(
            ad_id="2222222222",
            observed_at=NOW,
            source="management",
            lifecycle_state=LifecycleState.ACTIVE,
            views=12,
        )
        inventory = InventoryRuntime(
            ReadResult.success_nonempty((current,))
        )
        server = DashboardServer()
        captured = {}

        def runtime_factory(**kwargs):
            captured["runtime"] = kwargs
            return inventory

        def dashboard_factory(store_arg, **kwargs):
            captured["dashboard"] = kwargs
            captured["store"] = store_arg
            return server

        launcher = build_product_launcher(
            db_path=db,
            cdp_port=9222,
            dashboard_port=0,
            timeout_seconds=3.5,
            analytics_contract=AnalyticsContract(),
            runtime_factory=runtime_factory,
            dashboard_factory=dashboard_factory,
            clock=lambda: NOW,
        )
        self.addCleanup(launcher.close)

        self.assertEqual(inventory.read_calls, 1)
        self.assertEqual(
            captured["runtime"],
            {"cdp_port": 9222, "timeout_seconds": 3.5},
        )
        self.assertEqual(captured["dashboard"]["host"], "127.0.0.1")
        self.assertEqual(captured["dashboard"]["port"], 0)
        self.assertIsInstance(
            captured["dashboard"]["analytics_contract"],
            AnalyticsContract,
        )
        self.assertEqual(launcher.startup_inventory_count, 1)
        self.assertEqual(launcher.startup_persisted_count, 2)
        self.assertEqual(launcher.server_address, ("127.0.0.1", 18765))

        persisted = SnapshotStore(db)
        self.assertEqual(
            persisted.latest_ad_snapshot("1111111111").lifecycle_state,
            LifecycleState.ABSENT,
        )
        self.assertEqual(
            persisted.latest_ad_snapshot("2222222222"),
            current,
        )
        self.assertEqual(persisted.sync_status()["state"], "success_nonempty")
        self.assertEqual(persisted.latest_sync_attempt().snapshot_count, 1)

        launcher.serve_forever()
        self.assertEqual(server.serve_calls, 1)

    def test_startup_email_reactions_are_imported_before_http_surfaces(self) -> None:
        tmp, db = self.make_db()
        email_path = Path(tmp.name) / "reaction.eml"
        email_path.write_bytes(reaction_email())
        inventory = InventoryRuntime(ReadResult.success_empty(()))
        server = DashboardServer()
        observed_at_dashboard_build: list[tuple[int, int]] = []

        def dashboard_factory(store, **kwargs):
            observed_at_dashboard_build.append(
                store.inbound_message_counts("3333333333")
            )
            return server

        launcher = build_product_launcher(
            db_path=db,
            cdp_port=9222,
            email_paths=(email_path,),
            runtime_factory=lambda **kwargs: inventory,
            dashboard_factory=dashboard_factory,
            clock=lambda: NOW,
        )
        self.addCleanup(launcher.close)

        report = launcher.startup_email_import_report
        self.assertEqual(report.parsed_files, 1)
        self.assertEqual(report.inserted_events, 1)
        self.assertEqual(report.duplicate_events, 0)
        self.assertEqual(report.ad_ids, ("3333333333",))
        self.assertEqual(observed_at_dashboard_build, [(1, 1)])

        persisted = SnapshotStore(db)
        self.assertEqual(
            persisted.inbound_message_counts("3333333333"),
            (1, 1),
        )
        self.assertNotIn("3333333333", persisted.tracked_ad_ids())
        self.assertEqual(self.write_runtime.start_calls, 1)

    def test_startup_email_reactions_are_served_by_real_dashboard(self) -> None:
        tmp, db = self.make_db()
        email_path = Path(tmp.name) / "reaction.eml"
        email_path.write_bytes(reaction_email())
        inventory = InventoryRuntime(ReadResult.success_empty(()))
        launcher = build_product_launcher(
            db_path=db,
            cdp_port=9222,
            dashboard_port=0,
            email_paths=(email_path,),
            runtime_factory=lambda **kwargs: inventory,
            clock=lambda: NOW,
        )
        thread = threading.Thread(target=launcher.serve_forever)
        self.addCleanup(lambda: launcher.close())
        self.addCleanup(lambda: thread.join(timeout=1))
        thread.start()

        host, port = launcher.server_address
        opener = build_opener(ProxyHandler({}))
        with opener.open(
            f"http://{host}:{port}/api/email-reactions",
            timeout=2,
        ) as response:
            rows = json.loads(response.read())
        with opener.open(
            "http://"
            f"{host}:{port}/api/analytics/ads"
            "?metric=email_inbound_message_count",
            timeout=2,
        ) as response:
            ranking = json.loads(response.read())

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ad_id"], "3333333333")
        self.assertEqual(rows[0]["conversation_count"], 1)
        self.assertEqual(rows[0]["inbound_message_count"], 1)
        self.assertNotIn("unique_buyer_count", rows[0])
        self.assertEqual(
            [(item["ad_id"], item["value"]) for item in ranking],
            [("3333333333", 1)],
        )

        launcher.close()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())

    def test_startup_email_reimport_is_idempotent(self) -> None:
        tmp, db = self.make_db()
        email_path = Path(tmp.name) / "reaction.eml"
        email_path.write_bytes(reaction_email())

        first_write = WriteRuntime()
        first = build_product_launcher(
            db_path=db,
            cdp_port=9222,
            email_paths=(email_path,),
            runtime_factory=lambda **kwargs: InventoryRuntime(
                ReadResult.success_empty(())
            ),
            write_runtime_factory=lambda **kwargs: first_write,
            dashboard_factory=lambda *args, **kwargs: DashboardServer(),
            clock=lambda: NOW,
        )
        first_report = first.startup_email_import_report
        first.close()

        second_write = WriteRuntime()
        second = build_product_launcher(
            db_path=db,
            cdp_port=9222,
            email_paths=(email_path,),
            runtime_factory=lambda **kwargs: InventoryRuntime(
                ReadResult.success_empty(())
            ),
            write_runtime_factory=lambda **kwargs: second_write,
            dashboard_factory=lambda *args, **kwargs: DashboardServer(),
            clock=lambda: NOW,
        )
        self.addCleanup(second.close)
        second_report = second.startup_email_import_report

        self.assertEqual(first_report.inserted_events, 1)
        self.assertEqual(first_report.duplicate_events, 0)
        self.assertEqual(second_report.inserted_events, 0)
        self.assertEqual(second_report.duplicate_events, 1)
        self.assertEqual(
            SnapshotStore(db).inbound_message_counts("3333333333"),
            (1, 1),
        )

    def test_invalid_startup_email_batch_fails_before_http_surfaces(self) -> None:
        tmp, db = self.make_db()
        email_path = Path(tmp.name) / "invalid.eml"
        email_path.write_bytes(b"not a Kleinanzeigen notification")
        current = AdSnapshot(
            ad_id="2222222222",
            observed_at=NOW,
            source="current",
            lifecycle_state=LifecycleState.ACTIVE,
        )
        inventory = InventoryRuntime(ReadResult.success_nonempty((current,)))
        dashboard_calls = 0

        def dashboard_factory(*args, **kwargs):
            nonlocal dashboard_calls
            dashboard_calls += 1
            return DashboardServer()

        with self.assertRaisesRegex(
            ProductLauncherError,
            "local reaction email import failed",
        ):
            build_product_launcher(
                db_path=db,
                cdp_port=9222,
                email_paths=(email_path,),
                runtime_factory=lambda **kwargs: inventory,
                dashboard_factory=dashboard_factory,
                clock=lambda: NOW,
            )

        self.assertTrue(inventory.closed)
        self.assertEqual(dashboard_calls, 0)
        self.write_factory.assert_not_called()
        persisted = SnapshotStore(db)
        self.assertEqual(
            persisted.latest_ad_snapshot("2222222222"),
            current,
        )
        self.assertEqual(
            persisted.inbound_message_counts("3333333333"),
            (0, 0),
        )

    def test_successful_empty_inventory_marks_history_absent(self) -> None:
        _tmp, db = self.make_db()
        store = SnapshotStore(db)
        store.append_ad_snapshot(
            AdSnapshot(
                ad_id="1111111111",
                observed_at=NOW - timedelta(minutes=1),
                source="old",
                lifecycle_state=LifecycleState.ACTIVE,
            )
        )
        inventory = InventoryRuntime(ReadResult.success_empty(()))
        server = DashboardServer()

        launcher = build_product_launcher(
            db_path=db,
            cdp_port=9222,
            runtime_factory=lambda **kwargs: inventory,
            dashboard_factory=lambda *args, **kwargs: server,
            clock=lambda: NOW,
        )
        self.addCleanup(launcher.close)

        self.assertEqual(launcher.startup_inventory_count, 0)
        self.assertEqual(launcher.startup_persisted_count, 1)
        self.assertEqual(
            SnapshotStore(db).latest_ad_snapshot(
                "1111111111"
            ).lifecycle_state,
            LifecycleState.ABSENT,
        )
        self.assertEqual(SnapshotStore(db).sync_status()["state"], "success_empty")

    def test_runtime_construction_failure_records_no_success_and_no_write(self) -> None:
        _tmp, db = self.make_db()

        def broken_runtime(**kwargs):
            raise OSError("private browser configuration")

        with self.assertRaisesRegex(ProductLauncherError, "runtime startup failed"):
            build_product_launcher(
                db_path=db, cdp_port=9222,
                runtime_factory=broken_runtime, clock=lambda: NOW,
            )
        self.write_factory.assert_not_called()
        status = SnapshotStore(db).sync_status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["latest_attempt"]["error_kind"], "runtime_unavailable")
        self.assertIsNone(status["last_successful_attempt"])
        self.assertNotIn("private browser", str(status))

    def test_failed_inventory_stops_before_dashboard_and_writes_nothing(self) -> None:
        _tmp, db = self.make_db()
        store = SnapshotStore(db)
        original = AdSnapshot(
            ad_id="1111111111",
            observed_at=NOW,
            source="old",
            lifecycle_state=LifecycleState.ACTIVE,
        )
        store.append_ad_snapshot(original)
        inventory = InventoryRuntime(
            ReadResult.failure(
                ReadStatus.HTTP_ERROR,
                error="private details",
                http_status=500,
            )
        )
        dashboard_calls = 0

        def dashboard_factory(*args, **kwargs):
            nonlocal dashboard_calls
            dashboard_calls += 1
            return DashboardServer()

        with self.assertRaisesRegex(
            ProductLauncherError,
            "private Web inventory read failed: http_error",
        ):
            build_product_launcher(
                db_path=db,
                cdp_port=9222,
                runtime_factory=lambda **kwargs: inventory,
                dashboard_factory=dashboard_factory,
                clock=lambda: NOW,
            )

        self.assertTrue(inventory.closed)
        self.assertEqual(dashboard_calls, 0)
        self.write_factory.assert_not_called()
        self.assertEqual(SnapshotStore(db).ad_history("1111111111"), (original,))
        status = SnapshotStore(db).sync_status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["latest_attempt"]["error_kind"], "http_error")
        self.assertIsNone(status["last_successful_attempt"])

    def test_duplicate_inventory_fails_before_dashboard(self) -> None:
        _tmp, db = self.make_db()
        first = AdSnapshot(
            ad_id="1111111111",
            observed_at=NOW,
            source="one",
            lifecycle_state=LifecycleState.ACTIVE,
        )
        second = AdSnapshot(
            ad_id="1111111111",
            observed_at=NOW,
            source="two",
            lifecycle_state=LifecycleState.PAUSED,
        )
        inventory = InventoryRuntime(
            ReadResult.success_nonempty((first, second))
        )

        with self.assertRaisesRegex(
            ProductLauncherError,
            "duplicate ad IDs",
        ):
            build_product_launcher(
                db_path=db,
                cdp_port=9222,
                runtime_factory=lambda **kwargs: inventory,
                dashboard_factory=lambda *args, **kwargs: self.fail(
                    "dashboard must not be built"
                ),
                clock=lambda: NOW,
            )

        self.assertTrue(inventory.closed)
        self.write_factory.assert_not_called()
        status = SnapshotStore(db).sync_status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["latest_attempt"]["error_kind"], "invalid_inventory")

    def test_dashboard_construction_failure_closes_inventory_runtime(self) -> None:
        _tmp, db = self.make_db()
        inventory = InventoryRuntime(ReadResult.success_empty(()))

        def dashboard_factory(*args, **kwargs):
            raise OSError("port in use")

        with self.assertRaisesRegex(
            ProductLauncherError,
            "product launcher startup failed",
        ):
            build_product_launcher(
                db_path=db,
                cdp_port=9222,
                runtime_factory=lambda **kwargs: inventory,
                dashboard_factory=dashboard_factory,
                clock=lambda: NOW,
            )

        self.assertTrue(inventory.closed)
        self.assertEqual(self.write_runtime.start_calls, 1)
        self.assertEqual(self.write_runtime.close_calls, 1)
        self.assertTrue(self.write_runtime.closed)

    def test_write_runtime_start_failure_skips_dashboard_and_closes_inventory(self) -> None:
        _tmp, db = self.make_db()
        inventory = InventoryRuntime(ReadResult.success_empty(()))
        self.write_runtime.start_error = OSError("write server failed")
        dashboard_calls = 0

        def dashboard_factory(*args, **kwargs):
            nonlocal dashboard_calls
            dashboard_calls += 1
            return DashboardServer()

        with self.assertRaisesRegex(
            ProductLauncherError,
            "product launcher startup failed",
        ):
            build_product_launcher(
                db_path=db,
                cdp_port=9222,
                runtime_factory=lambda **kwargs: inventory,
                dashboard_factory=dashboard_factory,
                clock=lambda: NOW,
            )

        self.assertEqual(self.write_runtime.start_calls, 1)
        self.assertEqual(self.write_runtime.close_calls, 1)
        self.assertEqual(dashboard_calls, 0)
        self.assertTrue(inventory.closed)

    def test_close_shuts_down_active_serving_loop_before_cleanup(self) -> None:
        _tmp, db = self.make_db()
        inventory = InventoryRuntime(ReadResult.success_empty(()))
        server = BlockingDashboardServer()
        launcher = build_product_launcher(
            db_path=db,
            cdp_port=9222,
            runtime_factory=lambda **kwargs: inventory,
            dashboard_factory=lambda *args, **kwargs: server,
            clock=lambda: NOW,
        )
        thread = threading.Thread(target=launcher.serve_forever)
        self.addCleanup(server.release.set)
        self.addCleanup(lambda: thread.join(timeout=1))
        thread.start()
        self.assertTrue(server.started.wait(timeout=1))

        launcher.close()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(server.shutdown_calls, 1)
        self.assertEqual(server.close_calls, 1)
        self.assertTrue(server.closed)
        self.assertTrue(inventory.closed)

    def test_close_stops_real_dashboard_server_thread(self) -> None:
        _tmp, db = self.make_db()
        inventory = InventoryRuntime(ReadResult.success_empty(()))
        launcher = build_product_launcher(
            db_path=db,
            cdp_port=9222,
            dashboard_port=0,
            runtime_factory=lambda **kwargs: inventory,
            clock=lambda: NOW,
        )
        thread = threading.Thread(target=launcher.serve_forever)
        self.addCleanup(lambda: launcher.close())
        self.addCleanup(lambda: thread.join(timeout=1))
        thread.start()

        host, port = launcher.server_address
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://{host}:{port}/healthz", timeout=2) as response:
            self.assertEqual(response.status, 200)

        launcher.close()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertTrue(inventory.closed)

    def test_close_skips_shutdown_after_serve_loop_already_exited(self) -> None:
        _tmp, db = self.make_db()
        inventory = InventoryRuntime(ReadResult.success_empty(()))
        server = DashboardServer()
        launcher = build_product_launcher(
            db_path=db,
            cdp_port=9222,
            runtime_factory=lambda **kwargs: inventory,
            dashboard_factory=lambda *args, **kwargs: server,
            clock=lambda: NOW,
        )

        launcher.serve_forever()
        launcher.close()

        self.assertEqual(server.shutdown_calls, 0)
        self.assertEqual(server.close_calls, 1)
        self.assertTrue(inventory.closed)

    def test_failed_server_shutdown_blocks_cleanup_and_is_retryable(self) -> None:
        _tmp, db = self.make_db()
        inventory = InventoryRuntime(ReadResult.success_empty(()))
        server = BlockingDashboardServer(shutdown_failures=1)
        launcher = build_product_launcher(
            db_path=db,
            cdp_port=9222,
            runtime_factory=lambda **kwargs: inventory,
            dashboard_factory=lambda *args, **kwargs: server,
            clock=lambda: NOW,
        )
        thread = threading.Thread(target=launcher.serve_forever)
        self.addCleanup(server.release.set)
        self.addCleanup(lambda: thread.join(timeout=1))
        thread.start()
        self.assertTrue(server.started.wait(timeout=1))

        with self.assertRaisesRegex(ProductLauncherError, "cleanup failed"):
            launcher.close()

        self.assertTrue(thread.is_alive())
        self.assertEqual(server.shutdown_calls, 1)
        self.assertEqual(server.close_calls, 0)
        self.assertEqual(self.write_runtime.close_calls, 0)
        self.assertEqual(inventory.close_calls, 0)

        launcher.close()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(server.shutdown_calls, 2)
        self.assertEqual(server.close_calls, 1)
        self.assertEqual(self.write_runtime.close_calls, 1)
        self.assertEqual(inventory.close_calls, 1)

    def test_close_retries_failed_server_cleanup_without_reclosing_inventory(self) -> None:
        _tmp, db = self.make_db()
        inventory = InventoryRuntime(ReadResult.success_empty(()))
        server = DashboardServer(close_failures=1)
        launcher = build_product_launcher(
            db_path=db,
            cdp_port=9222,
            runtime_factory=lambda **kwargs: inventory,
            dashboard_factory=lambda *args, **kwargs: server,
            clock=lambda: NOW,
        )

        with self.assertRaisesRegex(
            ProductLauncherError,
            "cleanup failed",
        ):
            launcher.close()

        self.assertFalse(server.closed)
        self.assertEqual(server.close_calls, 1)
        self.assertTrue(self.write_runtime.closed)
        self.assertEqual(self.write_runtime.close_calls, 1)
        self.assertTrue(inventory.closed)
        self.assertEqual(inventory.close_calls, 1)

        launcher.close()
        self.assertTrue(server.closed)
        self.assertEqual(server.close_calls, 2)
        self.assertEqual(self.write_runtime.close_calls, 1)
        self.assertEqual(inventory.close_calls, 1)

        launcher.close()
        self.assertEqual(server.close_calls, 2)
        self.assertEqual(self.write_runtime.close_calls, 1)
        self.assertEqual(inventory.close_calls, 1)

    def test_failed_inventory_cleanup_remains_fail_closed_on_repeated_close(self) -> None:
        _tmp, db = self.make_db()
        inventory = InventoryRuntime(
            ReadResult.success_empty(()),
            close_failures=1,
        )
        server = DashboardServer()
        launcher = build_product_launcher(
            db_path=db,
            cdp_port=9222,
            runtime_factory=lambda **kwargs: inventory,
            dashboard_factory=lambda *args, **kwargs: server,
            clock=lambda: NOW,
        )

        with self.assertRaisesRegex(ProductLauncherError, "cleanup failed"):
            launcher.close()

        self.assertTrue(server.closed)
        self.assertEqual(server.close_calls, 1)
        self.assertFalse(inventory.closed)
        self.assertEqual(inventory.close_calls, 1)

        with self.assertRaisesRegex(ProductLauncherError, "cleanup failed"):
            launcher.close()

        self.assertEqual(server.close_calls, 1)
        self.assertEqual(inventory.close_calls, 1)
        with self.assertRaisesRegex(ProductLauncherError, "closed"):
            launcher.serve_forever()

    def test_write_shutdown_pending_preserves_inventory_owned_by_handler(self) -> None:
        _tmp, db = self.make_db()
        inventory = InventoryRuntime(ReadResult.success_empty(()))
        launcher = build_product_launcher(
            db_path=db,
            cdp_port=9222,
            runtime_factory=lambda **kwargs: inventory,
            dashboard_factory=lambda *args, **kwargs: DashboardServer(),
            clock=lambda: NOW,
        )
        from mark_api.private_web_runtime import PrivateWebRuntimeSetupError
        self.write_runtime.close_error = PrivateWebRuntimeSetupError(
            "active write handlers remain"
        )
        with self.assertRaisesRegex(ProductLauncherError, "cleanup"):
            launcher.close()
        self.assertEqual(inventory.close_calls, 0)
        self.assertFalse(inventory.closed)
        self.write_runtime.close_error = None
        launcher.close()
        self.assertEqual(inventory.close_calls, 1)
        self.assertTrue(inventory.closed)

    def test_unknown_media_close_can_reconcile_then_finish_shutdown(self) -> None:
        _tmp, db = self.make_db()
        inventory = InventoryRuntime(ReadResult.success_empty(()))
        server = DashboardServer()
        launcher = build_product_launcher(
            db_path=db,
            cdp_port=9222,
            runtime_factory=lambda **kwargs: inventory,
            dashboard_factory=lambda *args, **kwargs: server,
            clock=lambda: NOW,
        )
        self.write_runtime.pending_unknown = True

        with self.assertRaises(PrivateWebSubmitUnknownError):
            launcher.close()

        self.assertTrue(server.closed)
        self.assertTrue(inventory.closed)
        self.assertFalse(self.write_runtime.closed)

        launcher.reconcile_media_submit()
        launcher.close()

        self.assertEqual(self.write_runtime.reconcile_calls, 1)
        self.assertEqual(self.write_runtime.close_calls, 2)
        self.assertTrue(self.write_runtime.closed)

    def test_pyproject_registers_product_launcher_entry_point(self) -> None:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        with pyproject.open("rb") as handle:
            project = tomllib.load(handle)["project"]

        self.assertEqual(
            project["scripts"]["mark-api-launch"],
            "mark_api.launcher:main",
        )

    def test_main_reports_dashboard_and_closes_on_keyboard_interrupt(self) -> None:
        class Launcher:
            server_address = ("127.0.0.1", 18765)
            dashboard_url = (
                "http://127.0.0.1:18765/"
                "#write_token=fake-dashboard-write-token-0001"
            )
            write_server_address = ("127.0.0.1", 18766)
            write_bearer_token = self.TOKEN
            startup_inventory_count = 2
            startup_persisted_count = 3
            startup_email_import_report = EmailImportReport(
                parsed_files=1,
                inserted_events=1,
                duplicate_events=0,
                ad_ids=("3333333333",),
            )

            def __init__(self) -> None:
                self.closed = False

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def close(self) -> None:
                self.closed = True

        launcher = Launcher()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch(
                "mark_api.launcher.build_product_launcher",
                return_value=launcher,
            ) as build,
            patch("sys.stdout", stdout),
            patch("sys.stderr", stderr),
        ):
            status = main(
                [
                    "--db",
                    "/tmp/mark.sqlite",
                    "--cdp-port",
                    "9222",
                    "--dashboard-port",
                    "0",
                    "--write-port",
                    "0",
                    "--email",
                    "/tmp/reaction.eml",
                    "--reaction-metric",
                    "conversation_count",
                    "--objective-metric",
                    "views",
                ]
            )

        self.assertEqual(status, 0)
        self.assertTrue(launcher.closed)
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn(
            "Mark dashboard: http://127.0.0.1:18765/"
            "#write_token=fake-dashboard-write-token-0001",
            stdout.getvalue(),
        )
        self.assertIn(
            "Mark write API: http://127.0.0.1:18766/api/write/",
            stdout.getvalue(),
        )
        self.assertIn(
            f"Mark write bearer token: {self.TOKEN}",
            stdout.getvalue(),
        )
        self.assertIn(
            "Startup sync: 2 current ad(s), 3 observation(s) persisted.",
            stdout.getvalue(),
        )
        self.assertIn(
            "Startup reaction import: 1 email file(s), "
            "1 new event(s), 0 duplicate event(s).",
            stdout.getvalue(),
        )
        self.assertEqual(build.call_args.kwargs["write_port"], 0)
        self.assertEqual(
            build.call_args.kwargs["email_paths"],
            (Path("/tmp/reaction.eml"),),
        )
        contract = build.call_args.kwargs["analytics_contract"]
        self.assertEqual(contract.reaction_metric, "conversation_count")
        self.assertEqual(contract.objective_metric, "views")

    def test_main_reconciles_unknown_media_before_clean_exit(self) -> None:
        class Launcher:
            server_address = ("127.0.0.1", 18765)
            dashboard_url = (
                "http://127.0.0.1:18765/"
                "#write_token=runtime-dashboard-token-0001"
            )
            write_server_address = ("127.0.0.1", 18766)
            write_bearer_token = "runtime-token-0000001"
            startup_inventory_count = 0
            startup_persisted_count = 0

            def __init__(self) -> None:
                self.close_calls = 0
                self.reconcile_calls = 0
                self.pending_unknown = True

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def close(self) -> None:
                self.close_calls += 1
                if self.pending_unknown:
                    raise PrivateWebSubmitUnknownError(
                        "create_media_submit_settle"
                    )

            def reconcile_media_submit(self) -> None:
                self.reconcile_calls += 1
                self.pending_unknown = False

        launcher = Launcher()
        stdout = io.StringIO()
        with (
            patch(
                "mark_api.launcher.build_product_launcher",
                return_value=launcher,
            ),
            patch("sys.stdout", stdout),
        ):
            status = main(
                [
                    "--db",
                    "/tmp/mark.sqlite",
                    "--cdp-port",
                    "9222",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(launcher.reconcile_calls, 1)
        self.assertEqual(launcher.close_calls, 2)


    def test_main_sigterm_during_startup_defers_cleanup(self) -> None:
        original = signal.getsignal(signal.SIGTERM)
        stdout = io.StringIO()

        class Launcher:
            write_server_address = ("127.0.0.1", 18766)
            dashboard_url = "http://127.0.0.1/#write_token=secret-synthetic"
            write_bearer_token = "secret-synthetic"
            startup_inventory_count = 0
            startup_persisted_count = 0

            def __init__(self) -> None:
                self.serve_calls = 0
                self.close_calls = 0

            def serve_forever(self) -> None:
                self.serve_calls += 1

            def close(self) -> None:
                self.close_calls += 1

        runtime = Launcher()

        def build(**_kwargs: object) -> Launcher:
            handler = signal.getsignal(signal.SIGTERM)
            self.assertTrue(callable(handler))
            handler(signal.SIGTERM, None)
            # Repeated termination during setup is also deferred.
            handler(signal.SIGTERM, None)
            return runtime

        with (
            patch("mark_api.launcher.build_product_launcher", side_effect=build),
            patch("sys.stdout", stdout),
        ):
            code = main(["--db", "/tmp/synthetic-mark.sqlite", "--cdp-port", "9222"])
        self.assertEqual(code, 0)
        self.assertEqual(runtime.serve_calls, 0)
        self.assertEqual(runtime.close_calls, 1)
        self.assertNotIn("secret-synthetic", stdout.getvalue())
        self.assertIs(signal.getsignal(signal.SIGTERM), original)

    def test_main_sigterm_startup_error_restores_handler(self) -> None:
        original = signal.getsignal(signal.SIGTERM)

        def build(**_kwargs: object) -> None:
            handler = signal.getsignal(signal.SIGTERM)
            self.assertTrue(callable(handler))
            handler(signal.SIGTERM, None)
            raise ProductLauncherError("synthetic startup error")

        with (
            patch("mark_api.launcher.build_product_launcher", side_effect=build),
            patch("sys.stderr", io.StringIO()),
        ):
            code = main(["--db", "/tmp/synthetic-mark.sqlite", "--cdp-port", "9222"])
        self.assertEqual(code, 2)
        self.assertIs(signal.getsignal(signal.SIGTERM), original)

    def test_main_sigterm_runs_close_and_restores_signal_handler(self) -> None:
        original = signal.getsignal(signal.SIGTERM)

        class Launcher:
            write_server_address = ("127.0.0.1", 18766)
            dashboard_url = "http://127.0.0.1/#write_token=synthetic-test-only"
            write_bearer_token = "synthetic-test-only"
            startup_inventory_count = 0
            startup_persisted_count = 0

            def __init__(self) -> None:
                self.closed = False
                self.term_calls = 0

            def serve_forever(self) -> None:
                handler = signal.getsignal(signal.SIGTERM)
                if not callable(handler):
                    raise AssertionError("SIGTERM handler was not installed")
                self.term_calls += 1
                handler(signal.SIGTERM, None)
                raise AssertionError("SIGTERM did not exit serve_forever")

            def close(self) -> None:
                # A second SIGTERM during cleanup must not interrupt close.
                handler = signal.getsignal(signal.SIGTERM)
                if callable(handler):
                    handler(signal.SIGTERM, None)
                self.closed = True

        runtime = Launcher()
        with (
            patch("mark_api.launcher.build_product_launcher", return_value=runtime),
            patch("sys.stdout", io.StringIO()),
        ):
            code = main(["--db", "/tmp/synthetic-mark.sqlite", "--cdp-port", "9222"])
        self.assertEqual(code, 0)
        self.assertEqual(runtime.term_calls, 1)
        self.assertTrue(runtime.closed)
        self.assertIs(signal.getsignal(signal.SIGTERM), original)

    def test_main_sigterm_bounds_hung_media_reconciliation(self) -> None:
        class Launcher:
            write_server_address = ("127.0.0.1", 18766)
            dashboard_url = "http://127.0.0.1/#write_token=synthetic"
            write_bearer_token = "synthetic-only"
            startup_inventory_count = 0
            startup_persisted_count = 0

            def __init__(self) -> None:
                self.reconciliation_entered = threading.Event()
                self.release_reconciliation = threading.Event()
                self.close_calls = 0

            def serve_forever(self) -> None:
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)
                raise AssertionError("SIGTERM must interrupt serving")

            def close(self) -> None:
                self.close_calls += 1
                if self.close_calls == 1:
                    raise PrivateWebSubmitUnknownError("create_media_submit_settle")

            def reconcile_media_submit(self) -> None:
                self.reconciliation_entered.set()
                self.release_reconciliation.wait(timeout=3)

        runtime = Launcher()
        try:
            with (
                patch("mark_api.launcher.build_product_launcher", return_value=runtime),
                patch(
                    "mark_api.launcher._SHUTDOWN_MEDIA_RECONCILE_SECONDS",
                    0.15, create=True,
                ),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()) as stderr,
            ):
                started = monotonic()
                result = main([
                    "--db", "/tmp/synthetic-mark.sqlite",
                    "--cdp-port", "9222",
                ])
                elapsed = monotonic() - started
            self.assertTrue(runtime.reconciliation_entered.is_set())
            self.assertEqual(result, 2)
            self.assertLess(elapsed, 1.5)
            self.assertEqual(runtime.close_calls, 1)
            self.assertIn("media submit reconciliation required", stderr.getvalue())
        finally:
            runtime.release_reconciliation.set()

    def test_main_sigterm_preserves_ambiguous_media_reconciliation(self) -> None:
        original = signal.getsignal(signal.SIGTERM)

        class Launcher:
            write_server_address = ("127.0.0.1", 18766)
            dashboard_url = "http://127.0.0.1/#write_token=synthetic-test-only"
            write_bearer_token = "synthetic-test-only"
            startup_inventory_count = 0
            startup_persisted_count = 0

            def __init__(self) -> None:
                self.close_calls = 0
                self.reconcile_calls = 0

            def serve_forever(self) -> None:
                handler = signal.getsignal(signal.SIGTERM)
                if not callable(handler):
                    raise AssertionError("SIGTERM handler was not installed")
                handler(signal.SIGTERM, None)
                raise AssertionError("SIGTERM did not exit serve_forever")

            def close(self) -> None:
                self.close_calls += 1
                if self.close_calls == 1:
                    raise PrivateWebSubmitUnknownError("create_media_submit_settle")

            def reconcile_media_submit(self) -> None:
                self.reconcile_calls += 1

        runtime = Launcher()
        with (
            patch("mark_api.launcher.build_product_launcher", return_value=runtime),
            patch("sys.stdout", io.StringIO()),
        ):
            code = main(["--db", "/tmp/synthetic-mark.sqlite", "--cdp-port", "9222"])
        self.assertEqual(code, 0)
        self.assertEqual(runtime.close_calls, 2)
        self.assertEqual(runtime.reconcile_calls, 1)
        self.assertIs(signal.getsignal(signal.SIGTERM), original)

    def test_main_returns_two_for_sanitized_startup_failure(self) -> None:
        stderr = io.StringIO()
        with (
            patch(
                "mark_api.launcher.build_product_launcher",
                side_effect=ProductLauncherError("inventory unavailable"),
            ),
            patch("sys.stderr", stderr),
        ):
            status = main(
                [
                    "--db",
                    "/tmp/mark.sqlite",
                    "--cdp-port",
                    "9222",
                ]
            )

        self.assertEqual(status, 2)
        self.assertEqual(
            stderr.getvalue(),
            "mark-api-launch: inventory unavailable\n",
        )


if __name__ == "__main__":
    unittest.main()