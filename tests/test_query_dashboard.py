from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mark_api.dashboard import create_server
from mark_api.domain import (
    AdClassification,
    AdSnapshot,
    LifecycleState,
    ReactionSnapshot,
)
from mark_api.query import (
    MarkQueryService,
    ad_view_to_dict,
    summary_to_dict,
)
from mark_api.storage import SnapshotStore


T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=5)


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

    def test_serializers_are_json_safe_and_do_not_add_message_text(self) -> None:
        query = MarkQueryService(self.make_store())
        ad_payload = ad_view_to_dict(query.latest_ads()[0])
        summary_payload = summary_to_dict(query.summary())

        json.dumps(ad_payload)
        json.dumps(summary_payload)
        self.assertNotIn("message", ad_payload)
        self.assertEqual(ad_payload["lifecycle_state"], "absent")


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
        self.assertNotIn("https://", html)
        self.assertNotIn("http://", html)

        _, _, js_body = self.get("/dashboard.js")
        javascript = js_body.decode("utf-8")
        self.assertIn("summary.views_total_known", javascript)
        self.assertIn("/api/analytics/groups", javascript)
        self.assertNotIn(chr(92) + chr(96), javascript)

        _, _, css_body = self.get("/dashboard.css")
        self.assertIn(".cards", css_body.decode("utf-8"))

    def test_non_get_methods_are_405_and_no_write_route_exists(self) -> None:
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
