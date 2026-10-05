from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mark_api.analytics import (
    ANALYTICS_DIMENSIONS,
    ANALYTICS_METRICS,
    REACTION_METRICS,
    AnalyticsContract,
    AnalyticsService,
    ad_metric_ranking_to_dict,
    analytics_contract_to_dict,
    group_metric_ranking_to_dict,
)
from mark_api.domain import (
    AdClassification,
    AdSnapshot,
    InboundMessageEvent,
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

    def test_contract_defaults_do_not_choose_reaction_or_objective(self) -> None:
        contract = AnalyticsContract()

        self.assertIsNone(contract.reaction_metric)
        self.assertIsNone(contract.objective_metric)
        self.assertEqual(
            REACTION_METRICS,
            (
                "conversation_count",
                "unique_buyer_count",
                "inbound_message_count",
            ),
        )
        payload = analytics_contract_to_dict(contract)
        self.assertIsNone(payload["reaction_metric"])
        self.assertIsNone(payload["objective_metric"])
        self.assertEqual(
            payload["allowed_reaction_metrics"],
            list(REACTION_METRICS),
        )
        self.assertEqual(
            payload["allowed_objective_metrics"],
            list(ANALYTICS_METRICS),
        )

    def test_contract_rejects_implicit_or_unknown_metric_choices(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown reaction metric"):
            AnalyticsContract(reaction_metric="views")
        with self.assertRaisesRegex(
            ValueError,
            "unknown analytics objective metric",
        ):
            AnalyticsContract(objective_metric="engagement")

        analytics = AnalyticsService(self.make_store())
        with self.assertRaisesRegex(
            ValueError,
            "analytics objective metric is not configured",
        ):
            analytics.rank_ads_for_objective()
        with self.assertRaisesRegex(
            ValueError,
            "analytics objective metric is not configured",
        ):
            analytics.group_rankings_for_objective("city")

    def test_explicit_reaction_and_objective_metrics_remain_distinct(self) -> None:
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
                conversation_count=4,
                unique_buyer_count=1,
                inbound_message_count=6,
            )
        )
        store.append_reaction_snapshot(
            ReactionSnapshot(
                ad_id="2",
                observed_at=T0,
                source="mobile",
                conversation_count=2,
                unique_buyer_count=2,
                inbound_message_count=3,
            )
        )
        analytics = AnalyticsService(
            store,
            contract=AnalyticsContract(
                reaction_metric="unique_buyer_count",
                objective_metric="inbound_message_count",
            ),
        )

        reaction_rows = analytics.rank_ads(
            analytics.contract.reaction_metric
        )
        objective_rows = analytics.rank_ads_for_objective()

        self.assertEqual(
            [(row.ad_id, row.metric, row.value) for row in reaction_rows],
            [
                ("2", "unique_buyer_count", 2),
                ("1", "unique_buyer_count", 1),
            ],
        )
        self.assertEqual(
            [(row.ad_id, row.metric, row.value) for row in objective_rows],
            [
                ("1", "inbound_message_count", 6),
                ("2", "inbound_message_count", 3),
            ],
        )

    def test_objective_ranking_excludes_missing_but_keeps_observed_zero(self) -> None:
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
        analytics = AnalyticsService(
            store,
            contract=AnalyticsContract(objective_metric="views"),
        )

        rows = analytics.rank_ads_for_objective()

        self.assertEqual(
            [(row.ad_id, row.metric, row.value) for row in rows],
            [("2", "views", 0)],
        )

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

    def test_classification_order_uses_instants_across_utc_offsets(self) -> None:
        store = self.make_store()
        older_instant = AdClassification(
            ad_id="1",
            observed_at=datetime(
                2026,
                9,
                24,
                12,
                0,
                tzinfo=timezone(timedelta(hours=2)),
            ),
            source="manual",
            city="Older",
        )
        newer_instant = AdClassification(
            ad_id="1",
            observed_at=datetime(2026, 9, 24, 11, 0, tzinfo=timezone.utc),
            source="manual",
            city="Newer",
        )

        store.append_classification(newer_instant)
        store.append_classification(older_instant)

        self.assertEqual(
            store.classification_history("1"),
            (older_instant, newer_instant),
        )
        self.assertEqual(store.latest_classification("1"), newer_instant)

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

    def test_reaction_metric_uses_latest_observed_at_not_append_order(self) -> None:
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
                observed_at=T1,
                source="mobile",
                conversation_count=2,
                unique_buyer_count=2,
                inbound_message_count=3,
            )
        )
        store.append_reaction_snapshot(
            ReactionSnapshot(
                ad_id="1",
                observed_at=T0,
                source="mobile-backfill",
                conversation_count=1,
                unique_buyer_count=1,
                inbound_message_count=1,
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

    def test_email_metrics_are_source_explicit_and_do_not_replace_reactions(self) -> None:
        store = self.make_store()
        store.append_ad_snapshot(
            AdSnapshot(
                ad_id="1",
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
        store.append_classification(
            AdClassification(
                ad_id="1",
                observed_at=T0,
                source="manual",
                city="Berlin",
            )
        )
        store.append_inbound_message_events(
            (
                InboundMessageEvent(
                    ad_id="1",
                    conversation_id="conversation-a",
                    provider_message_id="message-a",
                    observed_at=T0,
                    source="kleinanzeigen-email",
                ),
                InboundMessageEvent(
                    ad_id="1",
                    conversation_id="conversation-a",
                    provider_message_id="message-b",
                    observed_at=T1,
                    source="kleinanzeigen-email",
                ),
                InboundMessageEvent(
                    ad_id="1",
                    conversation_id="conversation-b",
                    provider_message_id="message-c",
                    observed_at=T2,
                    source="kleinanzeigen-email",
                ),
            )
        )
        analytics = AnalyticsService(store)

        mobile_messages = analytics.rank_ads("inbound_message_count")
        email_messages = analytics.rank_ads("email_inbound_message_count")
        email_conversations = analytics.rank_ads("email_conversation_count")
        email_groups = analytics.group_rankings(
            "city",
            "email_inbound_message_count",
        )

        self.assertEqual(
            [(row.ad_id, row.value) for row in mobile_messages],
            [("1", 1)],
        )
        self.assertEqual(
            [(row.ad_id, row.value) for row in email_messages],
            [("1", 3)],
        )
        self.assertEqual(
            [(row.ad_id, row.value) for row in email_conversations],
            [("1", 2)],
        )
        self.assertEqual(
            [(row.label, row.metric_sum) for row in email_groups],
            [("Berlin", 3)],
        )

    def test_email_metric_ranking_includes_email_only_ad_without_presence_inference(self) -> None:
        store = self.make_store()
        store.append_inbound_message_events(
            (
                InboundMessageEvent(
                    ad_id="9",
                    conversation_id="conversation-only",
                    provider_message_id="message-only",
                    observed_at=T0,
                    source="kleinanzeigen-email",
                ),
            )
        )

        rows = AnalyticsService(store).rank_ads("email_inbound_message_count")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].ad_id, "9")
        self.assertEqual(rows[0].value, 1)
        self.assertIsNone(rows[0].present)
        self.assertIsNone(rows[0].lifecycle_state)
        self.assertIsNone(rows[0].title)
        payload = ad_metric_ranking_to_dict(rows[0])
        self.assertIsNone(payload["present"])
        self.assertIsNone(payload["lifecycle_state"])

    def test_email_only_classification_groups_only_email_metrics(self) -> None:
        store = self.make_store()
        store.append_inbound_message_events(
            (
                InboundMessageEvent(
                    ad_id="9",
                    conversation_id="conversation-only",
                    provider_message_id="message-only",
                    observed_at=T0,
                    source="kleinanzeigen-email",
                ),
            )
        )
        store.append_classification(
            AdClassification(
                ad_id="9",
                observed_at=T1,
                source="manual-cli",
                city="Dresden",
            )
        )
        analytics = AnalyticsService(store)

        email_groups = analytics.group_rankings(
            "city",
            "email_inbound_message_count",
        )
        views_groups = analytics.group_rankings("city", "views")
        reaction_groups = analytics.group_rankings(
            "city",
            "inbound_message_count",
        )

        self.assertEqual(
            [
                (
                    row.label,
                    row.sample_size,
                    row.metric_sum,
                    row.metric_mean,
                )
                for row in email_groups
            ],
            [("Dresden", 1, 1, 1.0)],
        )
        self.assertEqual(views_groups, ())
        self.assertEqual(reaction_groups, ())

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
                "email_conversation_count",
                "email_inbound_message_count",
            ),
        )


if __name__ == "__main__":
    unittest.main()