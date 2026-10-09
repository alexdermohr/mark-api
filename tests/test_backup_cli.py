from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from mark_api.backup_cli import BackupError, backup_store, main
from mark_api.domain import AdSnapshot, LifecycleState
from mark_api.results import ReadResult
from mark_api.storage import SnapshotStore


NOW = datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc)


class BackupCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="mark-backup-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "mark.sqlite"
        self.backup = self.root / "mark.backup.sqlite"
        self.store = SnapshotStore(self.source)

    def _seed_recovery_data(self) -> tuple[int, int, int]:
        success = self.store.begin_sync_attempt(source="owner-management", started_at=NOW)
        count = self.store.append_inventory_result(
            ReadResult.success_nonempty((
                AdSnapshot(
                    ad_id="1234567890", observed_at=NOW,
                    source="management", lifecycle_state=LifecycleState.ACTIVE,
                    views=0, watch_count=3,
                ),
            )),
            observed_at=NOW, source="owner-management",
            attempt_id=success, completed_at=NOW,
        )
        self.assertEqual(count, 1)
        failure = self.store.begin_sync_attempt(
            source="owner-management", started_at=NOW + timedelta(minutes=1),
        )
        self.store.fail_sync_attempt(
            failure, source="owner-management",
            completed_at=NOW + timedelta(minutes=1),
            error_kind="transport_error",
        )
        pending = self.store.begin_sync_attempt(
            source="owner-management", started_at=NOW + timedelta(minutes=2),
        )
        with sqlite3.connect(self.source) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key, request_sha256, state, requested_at) "
                "VALUES (?, ?, 'in_progress', ?)",
                ("pending-api-fence", "a" * 64, NOW.isoformat()),
            )
            db.execute(
                "INSERT INTO dashboard_pending_writes "
                "(scope, resource_key, idempotency_key, method, path) "
                "VALUES (?, ?, ?, 'POST', ?)",
                ("ad:create", "create-resource", "pending-dashboard-fence",
                 "/api/write/create"),
            )
            db.execute(
                "INSERT INTO inbound_message_events "
                "(provider_message_id, ad_id, conversation_id, observed_at, source) "
                "VALUES (?, ?, ?, ?, ?)",
                ("message-1", "1234567890", "conversation-1", NOW.isoformat(), "email"),
            )
        return success, failure, pending

    def test_online_backup_includes_committed_wal_and_all_recovery_fences(self) -> None:
        success, failure, pending = self._seed_recovery_data()
        original_id = self.source.stat().st_ino

        receipt = backup_store(self.source, backup_db=self.backup)

        self.assertTrue(self.backup.is_file())
        self.assertEqual(self.source.stat().st_ino, original_id)
        self.assertEqual(stat.S_IMODE(self.backup.stat().st_mode), 0o400)
        self.assertEqual(len(receipt.backup_sha256), 64)
        self.assertEqual(receipt.ad_snapshots, 1)
        self.assertEqual(receipt.inbound_message_events, 1)
        self.assertEqual(receipt.sync_attempts, 3)
        self.assertEqual(receipt.open_sync_attempts, 1)
        self.assertEqual(receipt.pending_api_writes, 1)
        self.assertEqual(receipt.pending_dashboard_writes, 1)

        # Never open the immutable backup in rw mode; restore to a NEW file.
        restored_path = self.root / "restored.sqlite"
        shutil.copyfile(self.backup, restored_path)
        os.chmod(restored_path, 0o600)
        restored = SnapshotStore(restored_path, create_if_missing=False)
        self.assertTrue(restored.is_ready())
        self.assertEqual(restored.latest_ad_snapshot("1234567890").views, 0)
        self.assertEqual(restored.sync_status(), self.store.sync_status())
        with sqlite3.connect(restored_path) as db:
            rows = db.execute(
                "SELECT id, outcome FROM sync_attempts ORDER BY id"
            ).fetchall()
            self.assertEqual(rows, [
                (success, "success_nonempty"),
                (failure, "failed"),
                (pending, "in_progress"),
            ])
            api = db.execute(
                "SELECT state FROM write_api_requests WHERE idempotency_key=?",
                ("pending-api-fence",),
            ).fetchone()
            self.assertEqual(api, ("in_progress",))
            ui = db.execute(
                "SELECT idempotency_key, acknowledged FROM dashboard_pending_writes"
            ).fetchone()
            self.assertEqual(ui, ("pending-dashboard-fence", 0))
            self.assertEqual(
                db.execute("SELECT count(*) FROM inbound_message_events").fetchone(),
                (1,),
            )

    def test_create_only_never_overwrites_existing_backup(self) -> None:
        first = backup_store(self.source, backup_db=self.backup)
        old_bytes = self.backup.read_bytes()
        with self.assertRaisesRegex(BackupError, "already exists"):
            backup_store(self.source, backup_db=self.backup)
        self.assertEqual(old_bytes, self.backup.read_bytes())
        self.assertEqual(len(first.backup_sha256), 64)

    def test_existing_symlink_target_is_not_followed_or_overwritten(self) -> None:
        target = self.root / "source-alias"
        target.symlink_to(self.source)
        with self.assertRaises(BackupError):
            backup_store(self.source, backup_db=target)
        self.assertTrue(target.is_symlink())
        self.assertTrue(self.store.is_ready())

    def test_broken_symlink_backup_path_is_not_overwritten(self) -> None:
        self.backup.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(BackupError, "already exists"):
            backup_store(self.source, backup_db=self.backup)
        self.assertTrue(self.backup.is_symlink())

    def test_source_symlink_and_hardlink_are_refused(self) -> None:
        alias = self.root / "alias.sqlite"
        alias.symlink_to(self.source)
        with self.assertRaisesRegex(BackupError, "regular"):
            backup_store(alias, backup_db=self.backup)
        alias.unlink()
        os.link(self.source, alias)
        with self.assertRaisesRegex(BackupError, "regular"):
            backup_store(self.source, backup_db=self.backup)
        self.assertFalse(self.backup.exists())

    def test_missing_source_and_missing_parent_are_refused(self) -> None:
        with self.assertRaisesRegex(BackupError, "does not exist"):
            backup_store(self.root / "not-here.sqlite", backup_db=self.backup)
        with self.assertRaisesRegex(BackupError, "directory"):
            backup_store(
                self.source, backup_db=self.root / "absent-parent" / "backup.sqlite",
            )
        self.assertFalse((self.root / "absent-parent").exists())

    def test_corrupt_source_is_refused_without_publishing_target(self) -> None:
        self.source.write_bytes(b"not a SQLite database")
        with self.assertRaises(BackupError):
            backup_store(self.source, backup_db=self.backup)
        self.assertFalse(self.backup.exists())

    def test_missing_write_recovery_table_is_never_implicitly_repaired(self) -> None:
        with sqlite3.connect(self.source) as db:
            db.execute("DROP TABLE write_api_requests")
        with self.assertRaises(BackupError):
            backup_store(self.source, backup_db=self.backup)
        self.assertFalse(self.backup.exists())
        with sqlite3.connect(self.source) as db:
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE name='write_api_requests'"
                ).fetchone()
            )

    def test_lost_versioned_sync_journal_blocks_backup_and_restore(self) -> None:
        self._seed_recovery_data()
        original = backup_store(self.source, backup_db=self.backup)
        self.assertEqual(original.sync_attempts, 3)
        copied = self.root / "broken.sqlite"
        shutil.copyfile(self.backup, copied)
        os.chmod(copied, 0o600)
        with sqlite3.connect(copied) as db:
            db.execute("DROP TABLE sync_attempts")
        with self.assertRaises(sqlite3.DatabaseError):
            SnapshotStore(copied, create_if_missing=False)
        with self.assertRaises(BackupError):
            backup_store(copied, backup_db=self.root / "broken.backup.sqlite")
        self.assertFalse((self.root / "broken.backup.sqlite").exists())

    def test_source_identity_loss_does_not_publish_backup(self) -> None:
        with patch(
            "mark_api.backup_cli._source_identity_unchanged",
            side_effect=BackupError("source identity changed during backup"),
        ):
            with self.assertRaises(BackupError):
                backup_store(self.source, backup_db=self.backup)
        self.assertFalse(self.backup.exists())
        self.assertTrue(self.store.is_ready())

    def test_post_publish_source_drift_preserves_backup_but_no_success_claim(self) -> None:
        # Simulate a source inode race *after* atomic publication. The
        # destination might already be durable; never delete or retry it.
        with patch(
            "mark_api.backup_cli._source_identity_unchanged",
            side_effect=[
                None, None,
                BackupError("source identity changed during backup"),
            ],
        ):
            with self.assertRaisesRegex(BackupError, "identity changed"):
                backup_store(self.source, backup_db=self.backup)
        self.assertTrue(self.backup.is_file())
        with self.assertRaisesRegex(BackupError, "already exists"):
            backup_store(self.source, backup_db=self.backup)

    def test_cli_receipt_is_safe_and_requires_explicit_destination(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["--db", str(self.source), "--backup", str(self.backup)]), 0
            )
        receipt = json.loads(buf.getvalue())
        self.assertEqual(receipt["open_sync_attempts"], 0)
        self.assertEqual(receipt["pending_api_writes"], 0)
        self.assertEqual(receipt["pending_dashboard_writes"], 0)
        self.assertNotIn(str(self.source), buf.getvalue())
        self.assertNotIn("bearer", buf.getvalue().lower())
        self.assertTrue(self.backup.exists())


if __name__ == "__main__":
    unittest.main()