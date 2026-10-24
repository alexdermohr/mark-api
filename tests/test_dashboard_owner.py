"""Read-only Dashboard/analytics over the private typed SQLite owner socket.

Same-UID framing tests only. Rootful multi-UID proof remains separate in
test_storage_owner_multiuid.py; this file grants no direct SQLite access to
a future untrusted browser/executor runtime and never authorizes Writes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import tempfile
from threading import Thread
from unittest.mock import patch
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mark_api.dashboard import DashboardWriteProxy, create_server, main as dashboard_main
from mark_api.domain import AdClassification, AdSnapshot, LifecycleState, ReactionSnapshot
from mark_api.results import ReadResult
from mark_api.storage import SnapshotStore
from mark_api.storage_owner import StorageOwnerClient, StorageOwnerServer


NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


class DashboardOwnerSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mark-dashboard-owner-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        private_data = root / "data"
        private_data.mkdir(mode=0o700)
        database = private_data / "mark.sqlite"
        store = SnapshotStore(database)
        store.append_ad_snapshot(AdSnapshot(
            ad_id="777", observed_at=NOW, source="synthetic:owner",
            lifecycle_state=LifecycleState.ACTIVE, title="Observed title",
            views=40, watch_count=4, reply_count=3,
        ))
        store.append_reaction_snapshot(ReactionSnapshot(
            ad_id="777", observed_at=NOW, source="synthetic:reactions",
            conversation_count=2, unique_buyer_count=2,
            inbound_message_count=4,
        ))
        store.append_classification(AdClassification(
            ad_id="777", observed_at=NOW, source="synthetic:manual",
            city="Berlin",
        ))
        rpc = root / "rpc"
        rpc.mkdir(mode=0o700)
        rpc.chmod(0o2710)
        socket_path = rpc / "owner.sock"
        owner = StorageOwnerServer(
            database=database, socket_path=socket_path,
            runtime_uid=os.geteuid() + 10001, runtime_gid=os.getegid(),
        )
        # Only unit-test framing may use the same UID. Live owner refuses this.
        owner.runtime_uid = os.geteuid()
        owner.start()
        self.addCleanup(owner.stop)
        self.owner = owner
        self.client = StorageOwnerClient(
            socket_path, expected_owner_uid=os.geteuid(),
        )
        self.assertFalse(hasattr(self.client, "path"))
        self.server = create_server(self.client, port=0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def get_json(self, path: str) -> tuple[int, object]:
        with urlopen(self.base + path, timeout=3) as response:
            return response.status, json.loads(response.read())

    def test_dashboard_and_analytics_use_owner_rpc_without_sqlite_path(self) -> None:
        self.assertEqual(self.get_json("/readyz"),
                         (200, {"status": "ready"}))
        status, ads = self.get_json("/api/ads")
        self.assertEqual(status, 200)
        self.assertEqual(len(ads), 1)
        self.assertEqual(ads[0]["ad_id"], "777")
        self.assertEqual(ads[0]["source"], "synthetic:owner")
        self.assertEqual(ads[0]["views"], 40)
        status, summary = self.get_json("/api/summary")
        self.assertEqual(status, 200)
        self.assertEqual(summary["tracked_ads"], 1)
        self.assertEqual(summary["views_total_known"], 40)
        _, ranked = self.get_json("/api/analytics/ads?metric=views")
        self.assertEqual(ranked[0]["ad_id"], "777")
        self.assertEqual(ranked[0]["value"], 40)
        _, groups = self.get_json(
            "/api/analytics/groups?dimension=city&metric=views"
        )
        self.assertEqual(groups[0]["label"], "Berlin")
        self.assertEqual(self.get_json("/api/dashboard/config"),
                         (200, {"write_ui_available": False}))
        with self.assertRaises(HTTPError) as no_write:
            urlopen(Request(self.base + "/api/write/ads/777",
                            data=b'{"title":"unsafe"}', method="PATCH"), timeout=3)
        self.assertEqual(no_write.exception.code, 405)

    def test_dashboard_reports_old_open_sync_even_after_newer_success(self) -> None:
        old = self.owner.store.begin_sync_attempt(
            source="older-owner", started_at=NOW,
        )
        newer = self.owner.store.begin_sync_attempt(
            source="newer-owner", started_at=NOW,
        )
        self.owner.store.append_inventory_result(
            ReadResult.success_empty(()), source="newer-owner",
            observed_at=NOW, attempt_id=newer, completed_at=NOW,
        )
        _, latest = self.get_json("/api/sync/status")
        self.assertEqual(latest["state"], "success_empty")
        _, recovery = self.get_json("/api/sync/recovery")
        self.assertEqual(recovery["unresolved_count"], 1)
        self.assertEqual(recovery["oldest_unresolved_attempt"]["id"], old)
        _, summary = self.get_json("/api/summary")
        self.assertEqual(summary["sync_recovery"], recovery)
        self.assertEqual(summary["sync_status"]["state"], "success_empty")
        # Data observability grants neither write requests nor clearances.
        self.assertFalse(hasattr(self.client, "complete_write_api_request"))
        self.assertFalse(hasattr(self.client, "record_write_recovery_clearance"))

    def test_dashboard_cli_socket_mode_requires_attested_owner_without_db(self) -> None:
        args = [
            "--owner-socket", str(self.client.socket_path),
            "--owner-uid", str(os.geteuid()), "--port", "0",
        ]
        with (
            patch("mark_api.dashboard.SnapshotStore",
                  side_effect=AssertionError("direct SQLite must stay untouched")),
            patch("mark_api.dashboard.create_server") as factory,
        ):
            self.assertEqual(dashboard_main(args), 0)
            forwarded_store = factory.call_args.args[0]
            self.assertIsInstance(forwarded_store, StorageOwnerClient)
            self.assertFalse(hasattr(forwarded_store, "path"))
            factory.return_value.serve_forever.assert_called_once()
            factory.return_value.server_close.assert_called_once()

        import io
        from contextlib import redirect_stderr
        invalid_arguments = (
            args[:2],
            args + ["--init-db"],
            ["--db", "/not-mounted/mark.sqlite", "--owner-uid",
             str(os.geteuid())],
            ["--owner-socket", str(self.client.socket_path),
             "--owner-uid", str(os.geteuid() + 1)],
        )
        for invalid in invalid_arguments:
            with (
                self.subTest(args=invalid),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as stopped,
            ):
                dashboard_main(invalid)
            self.assertEqual(stopped.exception.code, 2)

    def test_owner_disconnect_returns_fail_closed_http_503(self) -> None:
        self.owner.stop()
        for path in ("/readyz", "/api/ads", "/api/sync/recovery",
                     "/api/analytics/ads?metric=views"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                urlopen(self.base + path, timeout=3)
            self.assertEqual(error.exception.code, 503)
            self.assertNotIn("sqlite", error.exception.read().decode("utf-8").lower())

    def test_owner_socket_cannot_enable_unmediated_write_proxy(self) -> None:
        proxy = DashboardWriteProxy(
            host="127.0.0.1", port=9,
            bearer_token="backend-write-token-00000001",
            ui_token="dashboard-write-token-00000001",
        )
        with self.assertRaises(ValueError):
            extra = create_server(self.client, port=0, write_proxy=proxy)
            extra.server_close()


if __name__ == "__main__":
    unittest.main()
