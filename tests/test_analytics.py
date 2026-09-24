from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mark_api.analytics import (
    ANALYTICS_DIMENSIONS,
    ANALYTICS_METRICS,
    AnalyticsService,
    ad_metric_ranking_to_dict,
    group_metric_ranking_to_dict,
)
from mark_api.domain import (
    AdClassification,
    AdSnapshot,
    LifecycleState,
    ReactionSnapshot,
)
from mark_api.storage import SnapshotStore


T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=5)
T2 = T1 + timedelta(minutes=5)


class AnalyticsTests(unittest.TestCase):
    def make_store(self) -> SnapshotStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return SnapshotStore(Path(tmp.name) / "mark.sqlite")

    def test_classification_labels_are_opaque_trimmed_and_blank_rejected(self) -> None:
        item = AdClassification(
            ad_id="1",
            observed_at=T0,
            source="manual",
            image_type="  photo/raw::v1  ",
            city="  Magdeburg  ",
            text_type=None,
            title_type="TYPE / 17",
        )

        self.assertEqual(item.image_type, "photo/raw::v1")
        self.assertEqual(item.city, "Magdeburg")
        self.assertIsNone(item.text_type)
        self.assertEqual(item.title_type, "TYPE / 17")

        with self.assertRaises(ValueError):
            AdClassification(
                ad_id="1",
                observed_at=T0,
                source="manual",
                text_type="   ",
            )

    def test_classification_history_is_append_only_and_latest_is_by_observation_time(self) -> None:
        store = self.make_store()
        newer = AdClassification(
            ad_id="1",
            observed_at=T1,
            source="manual",
            city="Berlin",
        )
        older = AdClassification(
            ad_id="1",
            observed_at=T0,
            source="manual",
            city="Magdeburg",
        )

        store.append_classification(newer)
        store.append_classification(older)

        self.assertEqual(store.classification_history("1"), (older, newer))
        self.assertEqual(store.latest_classification("1"), newer)

    def test_unknown_metric_and_dimension_fail_closed(self) -> None:
        analytics = AnalyticsService(self.make_store())

        with self.assertRaises(ValueError):
            analytics.rank_ads("engagement")
        with self.assertRaises(ValueError):
            analytics.group_rankings("unknown", "views")
        with self.assertRaises(ValueError):
            analytics.group_rankings("city", "engagement")

    def test_missing_values_are_excluded_but_observed_zero_is_kept(self) -> None:
        store = self.make_store()
        store.append_ad_snapshot(
            AdSnapshot(
                ad_id="1",
                observed_at=T0,
                source="management",
                lifecycle_state=LifecycleState.ACTIVE,
                views=None,
            )
        )
        store.append_ad_snapshot(
            AdSnapshot(
                ad_id="2",
                observed_at=T0,
                source="management",
                lifecycle_state=LifecycleState.ACTIVE,
                views=0,
            )
        )
        store.append_reaction_snapshot(
            ReactionSnapshot(
                ad_id="2",
                observed_at=T0,
                source="mobile",
                conversation_count=0,
                unique_buyer_count=0,
                inbound_message_count=0,
            )
        )
        analytics = AnalyticsService(store)

        views = analytics.rank_ads("views")
        conversations = analytics.rank_ads("conversation_count")

        self.assertEqual([(row.ad_id, row.value) for row in views], [("2", 0)])
        self.assertEqual(
            [(row.ad_id, row.value) for row in conversations],
            [("2", 0)],
        )

    def test_latest_reaction_snapshot_is_used_and_missing_history_is_not_zero(self) -> None:
        store = self.make_store()
        for ad_id in ("1", "2"):
            store.append_ad_snapshot(
                AdSnapshot(
                    ad_id=ad_id,
                    observed_at=T0,
                    source="management",
                    lifecycle_state=LifecycleState.ACTIVE,
                    views=1,
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
        store.append_reaction_snapshot(
            ReactionSnapshot(
                ad_id="1",
                observed_at=T1,
                source="mobile",
                conversation_count=2,
                unique_buyer_count=2,
                inbound_message_count=3,
            )
        )

        rows = AnalyticsService(store).rank_ads("inbound_message_count")

        self.assertEqual([(row.ad_id, row.value) for row in rows], [("1", 3)])

    def test_absent_ad_keeps_last_known_metric_and_ties_use_ad_id(self) -> None:
        store = self.make_store()
        store.append_ad_snapshot(
            AdSnapshot(
                ad_id="1",
                observed_at=T0,
                source="management",
                lifecycle_state=LifecycleState.ACTIVE,
                title="Erste",
                views=10,
            )
        )
        store.append_ad_snapshot(
            AdSnapshot(
                ad_id="1",
                observed_at=T1,
                source="management",
                lifecycle_state=LifecycleState.ABSENT,
            )
        )
        store.append_ad_snapshot(
            AdSnapshot(
                ad_id="2",
                observed_at=T0,
                source="management",
                lifecycle_state=LifecycleState.ACTIVE,
                title="Zweite",
                views=10,
            )
        )

        rows = AnalyticsService(store).rank_ads("views")

        self.assertEqual([row.ad_id for row in rows], ["1", "2"])
        self.assertEqual(rows[0].value, 10)
        self.assertFalse(rows[0].present)
        self.assertEqual(rows[0].lifecycle_state, LifecycleState.ABSENT)
        self.assertEqual(rows[0].title, "Erste")

    def test_group_rankings_use_latest_explicit_labels_and_raw_aggregates(self) -> None:
        store = self.make_store()
        for ad_id, views in (("1", 10), ("2", 6), ("3", 4), ("4", 100)):
            store.append_ad_snapshot(
                AdSnapshot(
                    ad_id=ad_id,
                    observed_at=T0,
                    source="management",
                    lifecycle_state=LifecycleState.ACTIVE,
                    views=views,
                )
            )

        store.append_classification(
            AdClassification(
                ad_id="1",
                observed_at=T0,
                source="manual",
                city="Berlin",
            )
        )
        store.append_classification(
            AdClassification(
                ad_id="2",
                observed_at=T0,
                source="manual",
                city="Hamburg",
            )
        )
        store.append_classification(
            AdClassification(
                ad_id="2",
                observed_at=T1,
                source="manual",
                city="Berlin",
            )
        )
        store.append_classification(
            AdClassification(
                ad_id="3",
                observed_at=T0,
                source="manual",
                city="Leipzig",
            )
        )
        store.append_classification(
            AdClassification(
                ad_id="4",
                observed_at=T0,
                source="manual",
                image_type="detail",
            )
        )

        rows = AnalyticsService(store).group_rankings("city", "views")

        self.assertEqual([row.label for row in rows], ["Berlin", "Leipzig"])
        self.assertEqual(rows[0].sample_size, 2)
        self.assertEqual(rows[0].metric_sum, 16)
        self.assertEqual(rows[0].metric_mean, 8.0)
        self.assertEqual(rows[1].sample_size, 1)
        self.assertEqual(rows[1].metric_sum, 4)
        self.assertEqual(rows[1].metric_mean, 4.0)

    def test_group_ties_sort_by_label_and_payload_has_no_recommendation_semantics(self) -> None:
        store = self.make_store()
        for ad_id, label in (("1", "beta"), ("2", "alpha")):
            store.append_ad_snapshot(
                AdSnapshot(
                    ad_id=ad_id,
                    observed_at=T0,
                    source="management",
                    lifecycle_state=LifecycleState.ACTIVE,
                    views=5,
                )
            )
            store.append_classification(
                AdClassification(
                    ad_id=ad_id,
                    observed_at=T0,
                    source="manual",
                    city=label,
                )
            )

        analytics = AnalyticsService(store)
        groups = analytics.group_rankings("city", "views")
        ads = analytics.rank_ads("views")

        self.assertEqual([row.label for row in groups], ["alpha", "beta"])
        payload = {
            "groups": [group_metric_ranking_to_dict(row) for row in groups],
            "ads": [ad_metric_ranking_to_dict(row) for row in ads],
        }
        encoded = json.dumps(payload, sort_keys=True).lower()
        self.assertNotIn("recommend", encoded)
        self.assertNotIn("winner", encoded)
        self.assertNotIn("best", encoded)

    def test_allowed_contracts_are_exact_and_separate(self) -> None:
        self.assertEqual(
            ANALYTICS_DIMENSIONS,
            ("image_type", "city", "text_type", "title_type"),
        )
        self.assertEqual(
            ANALYTICS_METRICS,
            (
                "views",
                "watch_count",
                "reply_count",
                "conversation_count",
                "unique_buyer_count",
                "inbound_message_count",
            ),
        )


if __name__ == "__main__":
    unittest.main()
