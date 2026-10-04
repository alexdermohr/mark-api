from __future__ import annotations

import io
import tempfile
import threading
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

from mark_api.analytics import AnalyticsContract
from mark_api.domain import AdSnapshot, LifecycleState
from mark_api.launcher import (
    ProductLauncherError,
    build_product_launcher,
    main,
)
from mark_api.results import ReadResult, ReadStatus
from mark_api.storage import SnapshotStore


NOW = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)


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


class ProductLauncherTests(unittest.TestCase):
    def make_db(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp, Path(tmp.name) / "mark.sqlite"

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

        launcher.serve_forever()
        self.assertEqual(server.serve_calls, 1)

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
        self.assertEqual(SnapshotStore(db).ad_history("1111111111"), (original,))

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
        self.assertEqual(inventory.close_calls, 0)

        launcher.close()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(server.shutdown_calls, 2)
        self.assertEqual(server.close_calls, 1)
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
        self.assertTrue(inventory.closed)
        self.assertEqual(inventory.close_calls, 1)

        launcher.close()
        self.assertTrue(server.closed)
        self.assertEqual(server.close_calls, 2)
        self.assertEqual(inventory.close_calls, 1)

        launcher.close()
        self.assertEqual(server.close_calls, 2)
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
            startup_inventory_count = 2
            startup_persisted_count = 3

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
            "Mark dashboard: http://127.0.0.1:18765/",
            stdout.getvalue(),
        )
        self.assertIn(
            "Startup sync: 2 current ad(s), 3 observation(s) persisted.",
            stdout.getvalue(),
        )
        contract = build.call_args.kwargs["analytics_contract"]
        self.assertEqual(contract.reaction_metric, "conversation_count")
        self.assertEqual(contract.objective_metric, "views")

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
