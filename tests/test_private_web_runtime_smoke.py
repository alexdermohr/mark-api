from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mark_api.domain import AdSnapshot, LifecycleState
import mark_api.private_web_runtime_smoke as runtime_smoke
from mark_api.private_web_runtime_smoke import run_private_web_runtime_smoke
from mark_api.results import ReadResult, ReadStatus


NOW = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)


def snapshot(
    ad_id: str,
    *,
    state: LifecycleState = LifecycleState.ACTIVE,
    views: int | None = 7,
) -> AdSnapshot:
    return AdSnapshot(
        ad_id=ad_id,
        observed_at=NOW,
        source="kleinanzeigen-management",
        lifecycle_state=state,
        title=f"Title {ad_id}",
        description=None,
        views=views,
        watch_count=2,
        reply_count=1,
    )


class FakeInventoryRuntime:
    def __init__(self, result) -> None:
        self.result = result
        self.read_calls = 0
        self.close_calls = 0

    def read_inventory(self):
        self.read_calls += 1
        return self.result

    def close(self) -> None:
        self.close_calls += 1

    @property
    def content_writer(self):
        raise AssertionError("smoke must not access content writer")

    @property
    def create_writer(self):
        raise AssertionError("smoke must not access create writer")

    @property
    def state_writer(self):
        raise AssertionError("smoke must not access state writer")

    @property
    def delete_writer(self):
        raise AssertionError("smoke must not access delete writer")


class PrivateWebRuntimeSmokeTests(unittest.TestCase):
    def test_smoke_connects_inventory_sqlite_dashboard_and_analytics_read_only(self) -> None:
        runtime = FakeInventoryRuntime(
            ReadResult.success_nonempty(
                (
                    snapshot("1234567890"),
                    snapshot(
                        "9876543210",
                        state=LifecycleState.PAUSED,
                        views=None,
                    ),
                )
            )
        )
        calls: list[dict[str, object]] = []

        def factory(**kwargs):
            calls.append(dict(kwargs))
            return runtime

        report = run_private_web_runtime_smoke(
            19610,
            timeout_seconds=3.5,
            runtime_factory=factory,
        )

        self.assertEqual(
            calls,
            [{"cdp_port": 19610, "timeout_seconds": 3.5}],
        )
        self.assertEqual(runtime.read_calls, 1)
        self.assertEqual(runtime.close_calls, 1)
        self.assertEqual(report.inventory_status, "success_nonempty")
        self.assertEqual(report.inventory_count, 2)
        self.assertEqual(report.persisted_snapshots, 2)
        self.assertTrue(report.dashboard_health_ok)
        self.assertEqual(report.dashboard_tracked_ads, 2)
        self.assertEqual(report.dashboard_current_ads, 2)
        self.assertEqual(report.dashboard_ads, 2)
        self.assertEqual(report.analytics_ranked_ads, 1)
        self.assertTrue(report.http_write_methods_rejected)
        self.assertTrue(report.write_route_absent)
        self.assertFalse(report.platform_writes_enabled)

    def test_smoke_rejects_corrupted_dashboard_summary(self) -> None:
        original_json_get = runtime_smoke._json_get

        for field_name in ("current_ads", "views_total_known"):
            with self.subTest(field_name=field_name):
                runtime = FakeInventoryRuntime(
                    ReadResult.success_nonempty(
                        (snapshot("1234567890", views=7),)
                    )
                )

                def corrupt_summary(opener, base: str, path: str):
                    payload = original_json_get(opener, base, path)
                    if path == "/api/summary":
                        assert isinstance(payload, dict)
                        row = dict(payload)
                        row[field_name] = int(row[field_name]) + 1
                        return row
                    return payload

                with patch(
                    "mark_api.private_web_runtime_smoke._json_get",
                    side_effect=corrupt_summary,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "dashboard summary does not match inventory",
                    ):
                        run_private_web_runtime_smoke(
                            19610,
                            runtime_factory=lambda **_kwargs: runtime,
                        )

                self.assertEqual(runtime.close_calls, 1)

    def test_smoke_exercises_all_http_write_methods(self) -> None:
        runtime = FakeInventoryRuntime(
            ReadResult.success_nonempty((snapshot("1234567890"),))
        )
        original_expect_http_error = runtime_smoke._expect_http_error
        methods: list[str] = []

        def record_method(opener, request, *, expected_status: int):
            if expected_status == 405:
                methods.append(request.get_method())
            return original_expect_http_error(
                opener,
                request,
                expected_status=expected_status,
            )

        with patch(
            "mark_api.private_web_runtime_smoke._expect_http_error",
            side_effect=record_method,
        ):
            report = run_private_web_runtime_smoke(
                19610,
                runtime_factory=lambda **_kwargs: runtime,
            )

        self.assertEqual(
            methods,
            ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        )
        self.assertTrue(report.http_write_methods_rejected)
        self.assertEqual(runtime.close_calls, 1)

    def test_smoke_rejects_corrupted_dashboard_fields_with_same_ids(self) -> None:
        runtime = FakeInventoryRuntime(
            ReadResult.success_nonempty((snapshot("1234567890"),))
        )
        original_json_get = runtime_smoke._json_get

        def corrupt_projection(opener, base: str, path: str):
            payload = original_json_get(opener, base, path)
            if path == "/api/ads":
                assert isinstance(payload, list)
                rows = [dict(item) for item in payload]
                rows[0]["title"] = "corrupted title"
                return rows
            return payload

        with patch(
            "mark_api.private_web_runtime_smoke._json_get",
            side_effect=corrupt_projection,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "dashboard ads projection does not match inventory",
            ):
                run_private_web_runtime_smoke(
                    19610,
                    runtime_factory=lambda **_kwargs: runtime,
                )

        self.assertEqual(runtime.close_calls, 1)

    def test_smoke_rejects_incorrect_analytics_value_with_same_id(self) -> None:
        runtime = FakeInventoryRuntime(
            ReadResult.success_nonempty((snapshot("1234567890", views=7),))
        )
        original_json_get = runtime_smoke._json_get

        def corrupt_ranking(opener, base: str, path: str):
            payload = original_json_get(opener, base, path)
            if path == "/api/analytics/ads?metric=views":
                assert isinstance(payload, list)
                rows = [dict(item) for item in payload]
                rows[0]["value"] = 8
                return rows
            return payload

        with patch(
            "mark_api.private_web_runtime_smoke._json_get",
            side_effect=corrupt_ranking,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "dashboard analytics views ranking does not match inventory",
            ):
                run_private_web_runtime_smoke(
                    19610,
                    runtime_factory=lambda **_kwargs: runtime,
                )

        self.assertEqual(runtime.close_calls, 1)

    def test_success_empty_inventory_is_valid_and_stays_read_only(self) -> None:
        runtime = FakeInventoryRuntime(ReadResult.success_empty(()))

        report = run_private_web_runtime_smoke(
            19610,
            runtime_factory=lambda **_kwargs: runtime,
        )

        self.assertEqual(runtime.read_calls, 1)
        self.assertEqual(runtime.close_calls, 1)
        self.assertEqual(report.inventory_status, "success_empty")
        self.assertEqual(report.inventory_count, 0)
        self.assertEqual(report.persisted_snapshots, 0)
        self.assertEqual(report.dashboard_tracked_ads, 0)
        self.assertEqual(report.dashboard_ads, 0)
        self.assertEqual(report.analytics_ranked_ads, 0)
        self.assertFalse(report.platform_writes_enabled)

    def test_failed_inventory_stops_before_dashboard_and_closes_runtime(self) -> None:
        runtime = FakeInventoryRuntime(
            ReadResult.failure(
                ReadStatus.TRANSPORT_ERROR,
                error="redacted",
            )
        )

        with patch(
            "mark_api.private_web_runtime_smoke.create_server",
            side_effect=AssertionError("dashboard must not start"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "transport_error",
            ):
                run_private_web_runtime_smoke(
                    19610,
                    runtime_factory=lambda **_kwargs: runtime,
                )

        self.assertEqual(runtime.read_calls, 1)
        self.assertEqual(runtime.close_calls, 1)

    def test_duplicate_inventory_stops_before_dashboard(self) -> None:
        duplicate = snapshot("1234567890")
        runtime = FakeInventoryRuntime(
            ReadResult.success_nonempty((duplicate, duplicate))
        )

        with patch(
            "mark_api.private_web_runtime_smoke.create_server",
            side_effect=AssertionError("dashboard must not start"),
        ):
            with self.assertRaisesRegex(RuntimeError, "duplicate ad IDs"):
                run_private_web_runtime_smoke(
                    19610,
                    runtime_factory=lambda **_kwargs: runtime,
                )

        self.assertEqual(runtime.close_calls, 1)

    def test_invalid_port_fails_before_runtime_construction(self) -> None:
        calls = 0

        def factory(**_kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("runtime must not be constructed")

        for value in (0, 65536, True, "19610"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    run_private_web_runtime_smoke(
                        value,  # type: ignore[arg-type]
                        runtime_factory=factory,
                    )

        self.assertEqual(calls, 0)

    def test_smoke_module_has_no_writer_or_write_enable_surface(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "mark_api"
            / "private_web_runtime_smoke.py"
        ).read_text(encoding="utf-8")

        for forbidden in (
            ".content_writer",
            ".create_writer",
            ".state_writer",
            ".delete_writer",
            "writes_enabled=True",
            "SafeWriteOrchestrator",
            "MarkService",
            "build_private_web_content_runtime",
            "submit_create",
            "submit_delete",
            "submit_state",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("build_private_web_inventory_runtime", source)


if __name__ == "__main__":
    unittest.main()
