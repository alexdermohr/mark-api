from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mark_api.domain import (
    AdSnapshot,
    LifecycleState,
    OperationOutcome,
    OperationReceipt,
    ReactionSnapshot,
)
from mark_api.results import ReadResult, ReadStatus
from mark_api.storage import SnapshotStore


NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


class SnapshotStoreTests(unittest.TestCase):
    def make_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return SnapshotStore(Path(tmp.name) / "mark.sqlite")

    def test_failed_inventory_read_does_not_create_absent_snapshot(self) -> None:
        store = self.make_store()
        active = AdSnapshot(
            ad_id="3521676801",
            observed_at=NOW,
            source="management",
            lifecycle_state=LifecycleState.ACTIVE,
            views=15,
            watch_count=0,
            reply_count=1,
        )
        store.append_ad_snapshot(active)

        written = store.append_inventory_result(
            ReadResult.failure(ReadStatus.HTTP_ERROR, http_status=500),
            tracked_ad_ids=["3521676801"],
            observed_at=NOW + timedelta(minutes=1),
            source="management",
        )

        self.assertEqual(written, 0)
        history = store.ad_history("3521676801")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].lifecycle_state, LifecycleState.ACTIVE)

    def test_successful_empty_appends_absent_and_preserves_history(self) -> None:
        store = self.make_store()
        active = AdSnapshot(
            ad_id="3521676801",
            observed_at=NOW,
            source="management",
            lifecycle_state=LifecycleState.ACTIVE,
            views=15,
            watch_count=0,
            reply_count=1,
        )
        store.append_ad_snapshot(active)

        written = store.append_inventory_result(
            ReadResult.success_empty(()),
            tracked_ad_ids=["3521676801"],
            observed_at=NOW + timedelta(minutes=1),
            source="management",
        )

        self.assertEqual(written, 1)
        history = store.ad_history("3521676801")
        self.assertEqual(
            [item.lifecycle_state for item in history],
            [LifecycleState.ACTIVE, LifecycleState.ABSENT],
        )
        self.assertEqual(history[0].views, 15)
        self.assertIsNone(history[1].views)
        self.assertEqual(store.latest_ad_snapshot("3521676801"), history[-1])

    def test_nonempty_inventory_marks_other_tracked_ids_absent(self) -> None:
        store = self.make_store()
        present = AdSnapshot(
            ad_id="2",
            observed_at=NOW,
            source="management",
            lifecycle_state=LifecycleState.ACTIVE,
        )

        written = store.append_inventory_result(
            ReadResult.success_nonempty((present,)),
            tracked_ad_ids=["1", "2"],
            observed_at=NOW,
            source="management",
        )

        self.assertEqual(written, 2)
        self.assertEqual(
            store.latest_ad_snapshot("1").lifecycle_state,
            LifecycleState.ABSENT,
        )
        self.assertEqual(
            store.latest_ad_snapshot("2").lifecycle_state,
            LifecycleState.ACTIVE,
        )

    def test_operation_receipt_persists_authorization_metadata(self) -> None:
        store = self.make_store()
        receipt = OperationReceipt(
            operation="delete",
            ad_id="3521676801",
            started_at=NOW,
            completed_at=NOW,
            outcome=OperationOutcome.CONFIRMED,
            pre_read_status="success_nonempty",
            post_read_status="success_empty",
            writer_invoked=True,
            authorization_by="test-owner",
            authorization_reference="issue-3-predelete",
        )

        store.append_operation_receipt(receipt)

        with sqlite3.connect(store.path) as connection:
            row = connection.execute(
                """
                SELECT authorization_by, authorization_reference, outcome
                FROM operation_receipts
                WHERE ad_id = ?
                """,
                ("3521676801",),
            ).fetchone()

        self.assertEqual(
            row,
            ("test-owner", "issue-3-predelete", "confirmed"),
        )

    def test_reaction_snapshots_are_append_only(self) -> None:
        store = self.make_store()
        first = ReactionSnapshot(
            ad_id="3521676801",
            conversation_count=1,
            unique_buyer_count=1,
            inbound_message_count=1,
            observed_at=NOW,
            source="mobile-api",
        )
        second = ReactionSnapshot(
            ad_id="3521676801",
            conversation_count=1,
            unique_buyer_count=1,
            inbound_message_count=2,
            observed_at=NOW + timedelta(minutes=1),
            source="mobile-api",
        )
        store.append_reaction_snapshot(first)
        store.append_reaction_snapshot(second)

        self.assertEqual(store.reaction_history("3521676801"), (first, second))


    def test_merge_classification_serializes_symlink_aliases(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "mark.sqlite"
        direct_store = SnapshotStore(db_path)
        direct_store.append_ad_snapshot(
            AdSnapshot(
                ad_id="42",
                observed_at=NOW,
                source="management",
                lifecycle_state=LifecycleState.ACTIVE,
            )
        )

        alias_path = Path(tmp.name) / "mark-alias.sqlite"
        alias_path.symlink_to(db_path)
        alias_store = SnapshotStore(alias_path)
        barrier = Barrier(2)

        def merge(
            store: SnapshotStore,
            changes: dict[str, str | None],
        ) -> None:
            barrier.wait(timeout=5)
            store.merge_classification(
                ad_id="42",
                source="test",
                changes=changes,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            city_update = pool.submit(
                merge,
                direct_store,
                {"city": "Dresden"},
            )
            title_update = pool.submit(
                merge,
                alias_store,
                {"title_type": "question"},
            )
            city_update.result(timeout=5)
            title_update.result(timeout=5)

        latest = direct_store.latest_classification("42")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.city, "Dresden")
        self.assertEqual(latest.title_type, "question")
        self.assertEqual(len(direct_store.classification_history("42")), 2)


if __name__ == "__main__":
    unittest.main()
