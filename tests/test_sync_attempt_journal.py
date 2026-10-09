from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mark_api.domain import AdSnapshot, LifecycleState
from mark_api.results import ReadResult, ReadStatus
from mark_api.storage import SnapshotStore


NOW = datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc)


class SyncAttemptJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "mark.sqlite"
        self.store = SnapshotStore(self.db)

    def ad(self, ad_id: str = "123") -> AdSnapshot:
        return AdSnapshot(
            ad_id=ad_id, observed_at=NOW, source="provider+v2",
            lifecycle_state=LifecycleState.ACTIVE,
            views=0, watch_count=2, reply_count=1,
        )

    def test_historical_snapshots_without_sync_attempt_do_not_prove_success(self) -> None:
        self.store.append_ad_snapshot(self.ad())
        self.assertEqual(self.store.sync_status(), {
            "state": "never_attempted",
            "latest_attempt": None,
            "last_successful_attempt": None,
        })
        self.assertTrue(self.store.is_ready())

    def test_in_progress_survives_restart_without_invented_success(self) -> None:
        started = self.store.begin_sync_attempt(
            source="mark-service-owner-inventory", started_at=NOW,
        )
        reopened = SnapshotStore(self.db, create_if_missing=False)
        self.assertEqual(reopened.sync_status()["state"], "in_progress")
        self.assertEqual(reopened.latest_sync_attempt().id, started)
        self.assertIsNone(reopened.last_successful_sync_attempt())
        self.assertIsNone(reopened.latest_sync_attempt().completed_at)

    def test_success_nonempty_is_atomic_with_inventory_and_preserves_zero(self) -> None:
        started = self.store.begin_sync_attempt(source="owner+provider", started_at=NOW)
        self.store.append_inventory_result(
            ReadResult.success_nonempty((self.ad(),)),
            observed_at=NOW, tracked_ad_ids=(), source="owner+provider",
            attempt_id=started, completed_at=NOW + timedelta(seconds=1),
        )
        current = SnapshotStore(self.db, create_if_missing=False)
        self.assertEqual(current.latest_ad_snapshot("123").views, 0)
        status = current.sync_status()
        self.assertEqual(status["state"], "success_nonempty")
        self.assertEqual(status["latest_attempt"]["snapshot_count"], 1)
        self.assertEqual(status["last_successful_attempt"]["source"], "owner+provider")
        self.assertEqual(status["latest_attempt"]["started_at"], NOW.isoformat())

    def test_success_empty_and_later_failed_sync_preserve_prior_data_and_success(self) -> None:
        self.store.append_ad_snapshot(self.ad())
        first = self.store.begin_sync_attempt(source="owner", started_at=NOW)
        self.store.append_inventory_result(
            ReadResult.success_empty(()),
            tracked_ad_ids=["123"], observed_at=NOW + timedelta(minutes=1),
            source="owner", attempt_id=first,
            completed_at=NOW + timedelta(minutes=1),
        )
        self.assertEqual(self.store.latest_ad_snapshot("123").lifecycle_state,
                         LifecycleState.ABSENT)
        previous_history = self.store.ad_history("123")
        self.assertEqual(self.store.sync_status()["state"], "success_empty")
        failed = self.store.begin_sync_attempt(
            source="owner", started_at=NOW + timedelta(minutes=2),
        )
        self.assertEqual(self.store.append_inventory_result(
            ReadResult.failure(ReadStatus.HTTP_ERROR, error="private token and page", http_status=503),
            tracked_ad_ids=["123"], observed_at=NOW + timedelta(minutes=3),
            source="owner", attempt_id=failed,
            completed_at=NOW + timedelta(minutes=3),
        ), 0)
        self.assertEqual(self.store.ad_history("123"), previous_history)
        state = self.store.sync_status()
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["latest_attempt"]["error_kind"], "http_error")
        self.assertNotIn("private token", str(state))
        self.assertEqual(state["last_successful_attempt"]["outcome"], "success_empty")
        self.assertEqual(state["last_successful_attempt"]["id"], first)

    def test_unknown_target_and_duplicate_settlement_roll_back_all_snapshots(self) -> None:
        started = self.store.begin_sync_attempt(source="owner", started_at=NOW)
        with self.assertRaises(RuntimeError):
            self.store.append_inventory_result(
                ReadResult.success_nonempty((self.ad(),)),
                observed_at=NOW, source="other-source", attempt_id=started,
                completed_at=NOW,
            )
        with self.assertRaises(RuntimeError):
            self.store.fail_sync_attempt(
                started, source="other-source", completed_at=NOW,
                error_kind="transport_error",
            )
        self.assertEqual(self.store.tracked_ad_ids(), ())
        self.assertEqual(self.store.sync_status()["state"], "in_progress")
        with self.assertRaises(RuntimeError):
            self.store.append_inventory_result(
                ReadResult.success_nonempty((self.ad(),)),
                observed_at=NOW, source="owner", attempt_id=started + 1,
                completed_at=NOW,
            )
        self.assertEqual(self.store.tracked_ad_ids(), ())
        self.assertEqual(self.store.sync_status()["state"], "in_progress")
        self.store.fail_sync_attempt(
            started, source="owner", completed_at=NOW, error_kind="persistence_error",
        )
        with self.assertRaises(RuntimeError):
            self.store.fail_sync_attempt(
                started, source="owner", completed_at=NOW, error_kind="reader_exception",
            )
        self.assertEqual(self.store.sync_status()["latest_attempt"]["error_kind"],
                         "persistence_error")

    def test_invalid_payload_and_error_kind_never_forge_success(self) -> None:
        with self.assertRaises(ValueError):
            self.store.begin_sync_attempt(
                source="owner", started_at=datetime(2026, 10, 9, 4, 0),
            )
        self.assertEqual(self.store.sync_status()["state"], "never_attempted")
        started = self.store.begin_sync_attempt(source="owner", started_at=NOW)
        with self.assertRaises(ValueError):
            self.store.append_inventory_result(
                ReadResult.success_nonempty((self.ad(), self.ad())),
                observed_at=NOW, source="owner", attempt_id=started,
                completed_at=NOW,
            )
        with self.assertRaises(ValueError):
            self.store.fail_sync_attempt(
                started, source="owner", completed_at=NOW, error_kind="secret error text",
            )
        self.assertEqual(self.store.sync_status()["state"], "in_progress")
        self.assertEqual(self.store.tracked_ad_ids(), ())

    def test_malformed_persisted_attempt_rejected_by_readyz_and_projection(self) -> None:
        started = self.store.begin_sync_attempt(source="owner", started_at=NOW)
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE sync_attempts SET started_at='not-a-timestamp' WHERE id=?",
                (started,),
            )
        self.assertFalse(self.store.is_ready())
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.sync_status()
        with self.assertRaises(sqlite3.DatabaseError):
            SnapshotStore(self.db, create_if_missing=False)

    def test_additive_upgrade_after_existing_recovery_tables_validated(self) -> None:
        self.store.append_ad_snapshot(self.ad())
        with sqlite3.connect(self.db) as connection:
            connection.execute("DROP TABLE sync_attempts")
            connection.execute("PRAGMA user_version = 0")
        reopened = SnapshotStore(self.db, create_if_missing=False)
        self.assertTrue(reopened.is_ready())
        self.assertEqual(reopened.latest_ad_snapshot("123"), self.ad())
        self.assertEqual(reopened.sync_status()["state"], "never_attempted")
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='write_api_requests'"
            ).fetchone())

    def test_lost_versioned_journal_is_not_silently_regenerated(self) -> None:
        self.store.begin_sync_attempt(source="owner", started_at=NOW)
        with sqlite3.connect(self.db) as connection:
            connection.execute("DROP TABLE sync_attempts")
        self.assertFalse(self.store.is_ready())
        for initialize in (False, True):
            with self.subTest(initialize=initialize):
                with self.assertRaises(sqlite3.DatabaseError):
                    SnapshotStore(self.db, create_if_missing=initialize)
        with sqlite3.connect(self.db) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='sync_attempts'"
            ).fetchone())

    def test_missing_write_recovery_fence_blocks_additive_journal_upgrade(self) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute("DROP TABLE sync_attempts")
            connection.execute("DROP TABLE write_api_requests")
            connection.execute("PRAGMA user_version = 0")
        with self.assertRaises(sqlite3.DatabaseError):
            SnapshotStore(self.db, create_if_missing=False)
        with sqlite3.connect(self.db) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='sync_attempts'"
            ).fetchone())

    def test_restore_retains_attempts_and_last_success(self) -> None:
        started = self.store.begin_sync_attempt(source="owner", started_at=NOW)
        self.store.append_inventory_result(
            ReadResult.success_empty(()), observed_at=NOW, source="owner",
            attempt_id=started, completed_at=NOW,
        )
        backup = self.db.with_name("backup.sqlite")
        shutil.copyfile(self.db, backup)
        self.db.unlink()
        self.assertFalse(self.store.is_ready())
        shutil.copyfile(backup, self.db)
        restored = SnapshotStore(self.db, create_if_missing=False)
        self.assertEqual(restored.sync_status()["state"], "success_empty")
        self.assertEqual(restored.last_successful_sync_attempt().id, started)


if __name__ == "__main__":
    unittest.main()