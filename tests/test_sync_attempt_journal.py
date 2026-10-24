from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
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

    def test_older_open_sync_survives_newer_success_and_stays_visible(self) -> None:
        self.assertEqual(self.store.sync_recovery_status(), {
            "unresolved_count": 0, "oldest_unresolved_attempt": None,
        })
        old = self.store.begin_sync_attempt(
            source="older-owner", started_at=NOW,
        )
        newer = self.store.begin_sync_attempt(
            source="newer-owner", started_at=NOW + timedelta(seconds=1),
        )
        self.store.append_inventory_result(
            ReadResult.success_empty(()), observed_at=NOW + timedelta(seconds=2),
            source="newer-owner", attempt_id=newer,
            completed_at=NOW + timedelta(seconds=2),
        )
        reopened = SnapshotStore(self.db, create_if_missing=False)
        self.assertEqual(reopened.sync_status()["state"], "success_empty")
        status = reopened.sync_recovery_status()
        self.assertEqual(status["unresolved_count"], 1)
        self.assertEqual(status["oldest_unresolved_attempt"]["id"], old)
        self.assertEqual(status["oldest_unresolved_attempt"]["source"], "older-owner")
        self.assertEqual(status["oldest_unresolved_attempt"]["outcome"], "in_progress")
        self.assertIsNone(status["oldest_unresolved_attempt"]["completed_at"])
        # A successful newer read is never a clearance for the older attempt.
        with self.assertRaisesRegex(RuntimeError, "unresolved"):
            reopened.begin_sync_attempt(
                source="third-owner", started_at=NOW + timedelta(seconds=3),
                require_no_unresolved=True,
            )
        self.store.fail_sync_attempt(
            old, source="older-owner",
            completed_at=NOW + timedelta(seconds=4),
            error_kind="persistence_error",
        )
        self.assertEqual(reopened.sync_recovery_status(), {
            "unresolved_count": 0, "oldest_unresolved_attempt": None,
        })

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
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "UPDATE sync_attempts SET started_at='not-a-timestamp' WHERE id=?",
                (started,),
            )
        self.assertFalse(self.store.is_ready())
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.sync_status()
        with self.assertRaises(sqlite3.DatabaseError):
            SnapshotStore(self.db, create_if_missing=False)

    def test_corrupt_middle_open_sync_cannot_hide_behind_oldest_and_success(self) -> None:
        oldest = self.store.begin_sync_attempt(source="old-owner", started_at=NOW)
        hidden = self.store.begin_sync_attempt(
            source="middle-owner", started_at=NOW + timedelta(seconds=1),
        )
        newest_open = self.store.begin_sync_attempt(
            source="new-owner", started_at=NOW + timedelta(seconds=2),
        )
        terminal = self.store.begin_sync_attempt(
            source="finished-owner", started_at=NOW + timedelta(seconds=3),
        )
        self.store.append_inventory_result(
            ReadResult.success_empty(()), source="finished-owner",
            observed_at=NOW + timedelta(seconds=4),
            attempt_id=terminal, completed_at=NOW + timedelta(seconds=4),
        )
        self.assertEqual(self.store.sync_status()["state"], "success_empty")
        summary = self.store.sync_recovery_status()
        self.assertEqual(summary["unresolved_count"], 3)
        self.assertEqual(summary["oldest_unresolved_attempt"]["id"], oldest)
        self.assertTrue(self.store.is_ready())
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE sync_attempts SET started_at=? WHERE id=?",
                ("private-middle-owner-timestamp-corrupt", hidden),
            )
        self.assertEqual(self.store.latest_sync_attempt().id, terminal)
        self.assertEqual(self.store.last_successful_sync_attempt().id, terminal)
        self.assertEqual(self.store.latest_sync_attempt().outcome, "success_empty")
        for projection in (self.store.sync_status, self.store.sync_recovery_status):
            with self.subTest(projection=projection), self.assertRaises(
                sqlite3.DatabaseError,
            ):
                projection()
        self.assertFalse(self.store.is_ready())
        with self.assertRaises(sqlite3.DatabaseError):
            SnapshotStore(self.db, create_if_missing=False)
        # None of the three pending attempts may be silently resolved.
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT id FROM sync_attempts WHERE outcome='in_progress' "
                    "ORDER BY id"
                ).fetchall(),
                [(oldest,), (hidden,), (newest_open,)],
            )

    def test_corrupt_older_open_sync_cannot_hide_behind_new_success(self) -> None:
        older = self.store.begin_sync_attempt(source="stale-owner", started_at=NOW)
        newer = self.store.begin_sync_attempt(
            source="latest-owner", started_at=NOW + timedelta(seconds=1),
        )
        self.store.append_inventory_result(
            ReadResult.success_empty(()), source="latest-owner",
            observed_at=NOW + timedelta(seconds=2), attempt_id=newer,
            completed_at=NOW + timedelta(seconds=2),
        )
        self.assertTrue(self.store.is_ready())
        self.assertEqual(self.store.sync_status()["state"], "success_empty")
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE sync_attempts SET started_at=? WHERE id=?",
                ("corrupt-oldest-owner-time", older),
            )
        self.assertFalse(self.store.is_ready())
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.sync_status()
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.sync_recovery_status()
        with self.assertRaises(sqlite3.DatabaseError):
            SnapshotStore(self.db, create_if_missing=False)

    def test_additive_upgrade_after_existing_recovery_tables_validated(self) -> None:
        self.store.append_ad_snapshot(self.ad())
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("DROP TABLE sync_attempts")
            connection.execute("PRAGMA user_version = 0")
        reopened = SnapshotStore(self.db, create_if_missing=False)
        self.assertTrue(reopened.is_ready())
        self.assertEqual(reopened.latest_ad_snapshot("123"), self.ad())
        self.assertEqual(reopened.sync_status()["state"], "never_attempted")
        with closing(sqlite3.connect(self.db)) as connection, connection:
            # A6's complete store is schema-v2, including the additive sync
            # journal. Upgrading an unversioned store must preserve recovery.
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='write_api_requests'"
            ).fetchone())
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='write_recovery_clearances'"
            ).fetchone())

    def test_lost_versioned_journal_is_not_silently_regenerated(self) -> None:
        self.store.begin_sync_attempt(source="owner", started_at=NOW)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("DROP TABLE sync_attempts")
        self.assertFalse(self.store.is_ready())
        for initialize in (False, True):
            with self.subTest(initialize=initialize):
                with self.assertRaises(sqlite3.DatabaseError):
                    SnapshotStore(self.db, create_if_missing=initialize)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='sync_attempts'"
            ).fetchone())

    def test_missing_write_recovery_fence_blocks_additive_journal_upgrade(self) -> None:
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("DROP TABLE sync_attempts")
            connection.execute("DROP TABLE write_api_requests")
            connection.execute("PRAGMA user_version = 0")
        with self.assertRaises(sqlite3.DatabaseError):
            SnapshotStore(self.db, create_if_missing=False)
        with closing(sqlite3.connect(self.db)) as connection, connection:
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
