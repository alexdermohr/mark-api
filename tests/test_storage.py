from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mark_api.domain import (
    AdSnapshot,
    CreateOperationReceipt,
    LifecycleState,
    MediaPostReadStatus,
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

    def test_create_receipt_persists_confirmed_id_and_confirmation_statuses(self) -> None:
        store = self.make_store()
        candidate = AdSnapshot(
            ad_id="200",
            observed_at=NOW,
            source="management",
            lifecycle_state=LifecycleState.ACTIVE,
            title="Neue Vase",
            description="Beschreibung",
        )
        receipt = CreateOperationReceipt(
            operation="create",
            started_at=NOW,
            completed_at=NOW,
            outcome=OperationOutcome.CONFIRMED,
            pre_read_status="success_empty",
            confirmation_pre_read_status="success_empty",
            post_read_status="success_nonempty",
            confirmation_post_read_status="success_nonempty",
            content_post_read_status="success_nonempty",
            writer_invoked=True,
            created_ad_id="200",
            authorization_by="api-owner",
            authorization_reference="write-api:create-storage",
            post_snapshot=candidate,
            confirmation_post_snapshot=candidate,
            content_post_snapshot=candidate,
            media_post_read_status=MediaPostReadStatus.CONFIRMED,
            media_persistence_confirmed=True,
        )

        store.append_create_operation_receipt(receipt)

        with sqlite3.connect(store.path) as connection:
            row = connection.execute(
                """
                SELECT created_ad_id, outcome, confirmation_pre_read_status,
                       confirmation_post_read_status, content_post_read_status,
                       authorization_by, authorization_reference,
                       media_post_read_status, media_persistence_confirmed
                FROM create_operation_receipts
                """
            ).fetchone()

        self.assertEqual(
            row,
            (
                "200",
                "confirmed",
                "success_empty",
                "success_nonempty",
                "success_nonempty",
                "api-owner",
                "write-api:create-storage",
                "confirmed",
                1,
            ),
        )

    def test_create_receipt_checkpoint_is_append_only_content_evidence(self) -> None:
        store = self.make_store()
        candidate = AdSnapshot(
            ad_id="201",
            observed_at=NOW,
            source="management",
            lifecycle_state=LifecycleState.ACTIVE,
            title="Checkpoint Vase",
            description="Beschreibung",
        )
        receipt = CreateOperationReceipt(
            operation="create",
            started_at=NOW,
            completed_at=NOW,
            outcome=OperationOutcome.CONFIRMED,
            pre_read_status="success_empty",
            confirmation_pre_read_status="success_empty",
            post_read_status="success_nonempty",
            confirmation_post_read_status="success_nonempty",
            content_post_read_status="success_nonempty",
            writer_invoked=True,
            created_ad_id="201",
            authorization_by="api-owner",
            authorization_reference="write-api:create-checkpoint",
            post_snapshot=candidate,
            confirmation_post_snapshot=candidate,
            content_post_snapshot=candidate,
        )

        checkpoint_id = store.append_create_operation_checkpoint(receipt)

        self.assertGreater(checkpoint_id, 0)
        with sqlite3.connect(store.path) as connection:
            row = connection.execute(
                """
                SELECT checkpoint_kind, created_ad_id, outcome,
                       content_post_read_status, writer_invoked
                FROM create_operation_checkpoints
                WHERE id = ?
                """,
                (checkpoint_id,),
            ).fetchone()
        self.assertEqual(
            row,
            (
                "before_media_post_read",
                "201",
                "confirmed",
                "success_nonempty",
                1,
            ),
        )
        with self.assertRaises(ValueError):
            store.append_create_operation_checkpoint(
                replace(
                    receipt,
                    media_post_read_status=MediaPostReadStatus.CONFIRMED,
                    media_persistence_confirmed=True,
                )
            )

    def test_create_receipt_schema_migrates_legacy_authorization_columns(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "mark.sqlite"
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """
                CREATE TABLE create_operation_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    created_ad_id TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    pre_read_status TEXT NOT NULL,
                    confirmation_pre_read_status TEXT NOT NULL,
                    post_read_status TEXT,
                    confirmation_post_read_status TEXT,
                    content_post_read_status TEXT,
                    writer_invoked INTEGER NOT NULL,
                    writer_error TEXT,
                    post_snapshot_json TEXT,
                    confirmation_post_snapshot_json TEXT,
                    content_post_snapshot_json TEXT
                )
                """
            )

        store = SnapshotStore(db_path)
        receipt = CreateOperationReceipt(
            operation="create",
            started_at=NOW,
            completed_at=NOW,
            outcome=OperationOutcome.PRECONDITION_FAILED,
            pre_read_status="writes_disabled",
            confirmation_pre_read_status="not_read",
            post_read_status=None,
            confirmation_post_read_status=None,
            content_post_read_status=None,
            writer_invoked=False,
            authorization_by="api-owner",
            authorization_reference="write-api:create-legacy",
            media_post_read_status=MediaPostReadStatus.NOT_READ,
        )
        store.append_create_operation_receipt(receipt)

        with sqlite3.connect(db_path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(create_operation_receipts)"
                ).fetchall()
            }
            row = connection.execute(
                """
                SELECT authorization_by, authorization_reference,
                       media_post_read_status, media_persistence_confirmed
                FROM create_operation_receipts
                """
            ).fetchone()

        self.assertIn("authorization_by", columns)
        self.assertIn("authorization_reference", columns)
        self.assertIn("media_post_read_status", columns)
        self.assertIn("media_persistence_confirmed", columns)
        self.assertEqual(
            row,
            ("api-owner", "write-api:create-legacy", "not_read", 0),
        )

    def test_write_api_claim_owner_takeover_stops_at_execution_barrier(self) -> None:
        store = self.make_store()
        fingerprint = "a" * 64

        first = store.claim_write_api_request(
            idempotency_key="claim-owner",
            request_sha256=fingerprint,
            requested_at=NOW,
            claim_owner="runtime-owner-a",
        )
        self.assertTrue(first.created)
        self.assertEqual(first.record.claim_owner, "runtime-owner-a")
        self.assertIsNone(first.record.execution_started_at)

        same_owner = store.claim_write_api_request(
            idempotency_key="claim-owner",
            request_sha256=fingerprint,
            requested_at=NOW + timedelta(seconds=1),
            claim_owner="runtime-owner-a",
            allow_abandoned_takeover=True,
        )
        self.assertFalse(same_owner.created)

        takeover = store.claim_write_api_request(
            idempotency_key="claim-owner",
            request_sha256=fingerprint,
            requested_at=NOW + timedelta(seconds=2),
            claim_owner="runtime-owner-b",
            allow_abandoned_takeover=True,
        )
        self.assertTrue(takeover.created)
        self.assertEqual(takeover.record.claim_owner, "runtime-owner-b")
        self.assertEqual(takeover.record.requested_at, NOW)
        self.assertIsNone(takeover.record.execution_started_at)

        with self.assertRaisesRegex(ValueError, "another runtime"):
            store.begin_write_api_request(
                idempotency_key="claim-owner",
                request_sha256=fingerprint,
                claim_owner="runtime-owner-a",
                execution_started_at=NOW + timedelta(seconds=3),
            )

        started = store.begin_write_api_request(
            idempotency_key="claim-owner",
            request_sha256=fingerprint,
            claim_owner="runtime-owner-b",
            execution_started_at=NOW + timedelta(seconds=3),
        )
        self.assertEqual(
            started.execution_started_at,
            NOW + timedelta(seconds=3),
        )

        blocked_takeover = store.claim_write_api_request(
            idempotency_key="claim-owner",
            request_sha256=fingerprint,
            requested_at=NOW + timedelta(seconds=4),
            claim_owner="runtime-owner-c",
            allow_abandoned_takeover=True,
        )
        self.assertFalse(blocked_takeover.created)
        self.assertEqual(blocked_takeover.record.claim_owner, "runtime-owner-b")

        with self.assertRaisesRegex(ValueError, "another runtime"):
            store.complete_write_api_request(
                idempotency_key="claim-owner",
                request_sha256=fingerprint,
                claim_owner="runtime-owner-a",
                response_status=200,
                response_json='{"status":"ok"}',
                completed_at=NOW + timedelta(seconds=5),
            )

        completed = store.complete_write_api_request(
            idempotency_key="claim-owner",
            request_sha256=fingerprint,
            claim_owner="runtime-owner-b",
            response_status=200,
            response_json='{"status":"ok"}',
            completed_at=NOW + timedelta(seconds=5),
        )
        self.assertEqual(completed.state, "completed")
        self.assertEqual(completed.claim_owner, "runtime-owner-b")
        self.assertEqual(
            completed.execution_started_at,
            NOW + timedelta(seconds=3),
        )

    def test_write_api_legacy_in_progress_claim_remains_fail_closed(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "mark.sqlite"
        fingerprint = "b" * 64
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """
                CREATE TABLE write_api_requests (
                    idempotency_key TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL
                        CHECK (state IN ('in_progress', 'completed')),
                    requested_at TEXT NOT NULL,
                    completed_at TEXT,
                    response_status INTEGER,
                    response_json TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO write_api_requests (
                    idempotency_key, request_sha256, state, requested_at
                ) VALUES (?, ?, 'in_progress', ?)
                """,
                ("legacy-stuck", fingerprint, NOW.isoformat()),
            )

        store = SnapshotStore(db_path)
        with sqlite3.connect(db_path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(write_api_requests)"
                ).fetchall()
            }
        self.assertIn("claim_owner", columns)
        self.assertIn("execution_started_at", columns)

        claim = store.claim_write_api_request(
            idempotency_key="legacy-stuck",
            request_sha256=fingerprint,
            requested_at=NOW + timedelta(seconds=1),
            claim_owner="runtime-owner-new",
            allow_abandoned_takeover=True,
        )
        self.assertFalse(claim.created)
        self.assertIsNone(claim.record.claim_owner)
        self.assertIsNone(claim.record.execution_started_at)

        with self.assertRaisesRegex(ValueError, "another runtime"):
            store.begin_write_api_request(
                idempotency_key="legacy-stuck",
                request_sha256=fingerprint,
                claim_owner="runtime-owner-new",
                execution_started_at=NOW + timedelta(seconds=2),
            )

    def test_write_api_completion_requires_execution_start(self) -> None:
        store = self.make_store()
        fingerprint = "c" * 64
        store.claim_write_api_request(
            idempotency_key="not-started",
            request_sha256=fingerprint,
            requested_at=NOW,
            claim_owner="runtime-owner-a",
        )

        with self.assertRaisesRegex(ValueError, "has not started"):
            store.complete_write_api_request(
                idempotency_key="not-started",
                request_sha256=fingerprint,
                claim_owner="runtime-owner-a",
                response_status=500,
                response_json='{"error":"test"}',
                completed_at=NOW + timedelta(seconds=1),
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
