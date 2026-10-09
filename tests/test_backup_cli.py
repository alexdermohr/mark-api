from __future__ import annotations

from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
import gc
import io
import json
import os
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import sys
import stat
import tempfile
import unittest
from unittest.mock import patch

import mark_api.backup_cli as backup_cli
from mark_api.backup_cli import BackupError, backup_store, main
from mark_api.domain import AdSnapshot, LifecycleState
from mark_api.results import ReadResult
from mark_api.storage import SnapshotStore


NOW = datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc)


class BackupCliTests(unittest.TestCase):
    def setUp(self) -> None:
        # sqlite3.Connection.__exit__ commits but does not close. Reclaim
        # unreachable handles from prior tests so their temp DBs cannot be
        # mistaken for concurrently opened, unrelated SQLite files.
        gc.collect()
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

    def test_backup_includes_committed_frames_still_in_live_wal(self) -> None:
        # Keep the SQLite writer open after commit, with automatic checkpoint
        # disabled. This proves the pending recovery fence is read from the
        # actual nonempty WAL, not from an already checkpointed main database.
        wal = self.source.with_name(self.source.name + "-wal")
        with closing(sqlite3.connect(self.source)) as writer:
            self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key, request_sha256, state, requested_at) "
                "VALUES (?, ?, 'in_progress', ?)",
                ("live-wal-pending-fence", "d" * 64, NOW.isoformat()),
            )
            writer.commit()
            self.assertTrue(wal.is_file())
            self.assertGreater(wal.stat().st_size, 32)
            receipt = backup_store(self.source, backup_db=self.backup)
            self.assertEqual(receipt.pending_api_writes, 1)
            self.assertTrue(wal.is_file())
        with closing(sqlite3.connect(self.backup)) as restored:
            self.assertEqual(
                restored.execute(
                    "SELECT state FROM write_api_requests WHERE idempotency_key=?",
                    ("live-wal-pending-fence",),
                ).fetchone(),
                ("in_progress",),
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

    def test_reserved_source_sidecar_targets_are_rejected(self) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                sidecar = self.source.with_name(self.source.name + suffix)
                was_present = sidecar.exists()
                with self.assertRaisesRegex(BackupError, "source SQLite sidecar"):
                    backup_store(self.source, backup_db=sidecar)
                self.assertEqual(sidecar.exists(), was_present)

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

    def test_source_swapped_during_sqlite_connect_never_publishes_wrong_inode(self) -> None:
        # A valid substitute database can be connected while the public path
        # is temporarily replaced, then the original pathname restored. A
        # later lstat(path) is NOT proof of what the SQLite handle opened.
        with sqlite3.connect(self.source) as connection:
            connection.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key, request_sha256, state, requested_at) "
                "VALUES (?, ?, 'in_progress', ?)",
                ("real-pending-fence", "c" * 64, NOW.isoformat()),
            )
        fake = self.root / "other-valid.sqlite"
        SnapshotStore(fake)
        # The attack database is created in-process for this test. Drop
        # unreachable temporary SQLite connections to model a separate
        # attacker process rather than rejecting an unrelated local handle.
        gc.collect()
        moved = self.root / "original-temporarily-moved.sqlite"
        original = sqlite3.connect
        source_uri = backup_cli._uri(self.source, "ro")
        swapped = False
        starting_inode = self.source.stat().st_ino

        def malicious_connect(database, *args, **kwargs):
            nonlocal swapped
            if database != source_uri or swapped:
                return original(database, *args, **kwargs)
            swapped = True
            os.replace(self.source, moved)
            os.replace(fake, self.source)
            try:
                connection = original(database, *args, **kwargs)
                # Force SQLite to open the substituted file descriptor BEFORE
                # we put the original filename back.
                connection.execute("PRAGMA schema_version").fetchone()
                return connection
            finally:
                os.replace(self.source, fake)
                os.replace(moved, self.source)

        with patch("mark_api.backup_cli.sqlite3.connect", side_effect=malicious_connect):
            with self.assertRaisesRegex(BackupError, "source inode"):
                backup_store(self.source, backup_db=self.backup)

        self.assertTrue(swapped)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.source.stat().st_ino, starting_inode)
        with sqlite3.connect(self.source) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM write_api_requests "
                    "WHERE idempotency_key='real-pending-fence'"
                ).fetchone(),
                ("in_progress",),
            )

    def test_preexisting_sqlite_fd_fails_closed_instead_of_trusting_fd_reuse(self) -> None:
        # An fd-number delta does not identify an SQLite connection: a
        # concurrent close/reopen on the same inode could hide an impostor.
        # Backup therefore rejects unrelated main-file handles instead of
        # accepting a plausible but misattributed fd delta.
        other = self.root / "another.sqlite"
        SnapshotStore(other)
        with sqlite3.connect(other) as outside:
            outside.execute("PRAGMA schema_version").fetchone()
            self.assertTrue(backup_cli._sqlite_main_fds())
            with self.assertRaisesRegex(BackupError, "unrelated open SQLite files"):
                backup_store(self.source, backup_db=self.backup)
        self.assertFalse(self.backup.exists())
        self.assertTrue(self.store.is_ready())

    def test_existing_real_source_connection_still_allows_wal_backup(self) -> None:
        # Online backup must work while a legitimate handle holds the same
        # validated inode; only unrelated preexisting SQLite files are unsafe.
        with sqlite3.connect(self.source) as live:
            live.execute("PRAGMA schema_version").fetchone()
            receipt = backup_store(self.source, backup_db=self.backup)
            self.assertEqual(receipt.pending_api_writes, 0)
        self.assertTrue(self.backup.is_file())

    def test_swapped_older_wal_sidecars_never_publish_missing_write_fence(self) -> None:
        # Keep committed frames in WAL, snapshot an earlier valid WAL/SHM pair,
        # then transiently substitute those sidecars while SQLite opens its
        # read-only source. The main database inode never changes.
        wal = self.source.with_name(self.source.name + "-wal")
        shm = self.source.with_name(self.source.name + "-shm")
        old_wal = self.root / "old-source-wal"
        old_shm = self.root / "old-source-shm"
        moved_wal = self.root / "current-wal-moved"
        moved_shm = self.root / "current-shm-moved"
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key, request_sha256, state, requested_at) "
                "VALUES (?, ?, 'in_progress', ?)",
                ("before-wal-swap", "e" * 64, NOW.isoformat()),
            )
            writer.commit()
            self.assertTrue(wal.is_file())
            self.assertTrue(shm.is_file())
            shutil.copyfile(wal, old_wal)
            shutil.copyfile(shm, old_shm)
            writer.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key, request_sha256, state, requested_at) "
                "VALUES (?, ?, 'in_progress', ?)",
                ("latest-wal-fence", "f" * 64, NOW.isoformat()),
            )
            writer.commit()
            original_connect = sqlite3.connect
            original_uri = backup_cli._uri(self.source, "ro")
            swapped = False

            def connect_using_old_wal(database, *args, **kwargs):
                nonlocal swapped
                if database != original_uri or swapped:
                    return original_connect(database, *args, **kwargs)
                swapped = True
                os.replace(wal, moved_wal)
                os.replace(old_wal, wal)
                os.replace(shm, moved_shm)
                os.replace(old_shm, shm)
                try:
                    db = original_connect(database, *args, **kwargs)
                    db.execute("PRAGMA schema_version").fetchone()
                    return db
                finally:
                    os.replace(wal, old_wal)
                    os.replace(moved_wal, wal)
                    os.replace(shm, old_shm)
                    os.replace(moved_shm, shm)

            with patch("mark_api.backup_cli.sqlite3.connect", side_effect=connect_using_old_wal):
                with self.assertRaisesRegex(BackupError, "WAL source identity"):
                    backup_store(self.source, backup_db=self.backup)
            self.assertTrue(swapped)
            self.assertFalse(self.backup.exists())
            self.assertEqual(
                writer.execute(
                    "SELECT state FROM write_api_requests "
                    "WHERE idempotency_key='latest-wal-fence'"
                ).fetchone(),
                ("in_progress",),
            )

    def test_swapped_only_shm_during_sqlite_open_fails_closed(self) -> None:
        # Use a separate SQLite writer process so this backup process cannot
        # reuse that writer's already-open -shm mapping (SQLite VFS cache).
        shm = self.source.with_name(self.source.name + "-shm")
        stale_shm = self.root / "stale-shared-memory"
        held_shm = self.root / "current-shared-memory"
        source_uri = backup_cli._uri(self.source, "ro")
        original_connect = sqlite3.connect
        swapped = False
        child_code = """
import sys, sqlite3
from contextlib import closing
db = sys.argv[1]
with closing(sqlite3.connect(db)) as c:
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA wal_autocheckpoint=0')
    c.execute("INSERT INTO write_api_requests (idempotency_key,request_sha256,state,requested_at) VALUES (?,?, 'in_progress',?)",
              ('shm-base','a'*64,'2026-10-09T05:00:00+00:00'))
    c.commit()
    print('BASE', flush=True)
    sys.stdin.readline()
    c.execute("INSERT INTO write_api_requests (idempotency_key,request_sha256,state,requested_at) VALUES (?,?, 'in_progress',?)",
              ('shm-new-fence','b'*64,'2026-10-09T05:00:00+00:00'))
    c.commit()
    print('READY', flush=True)
    sys.stdin.readline()
"""
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", child_code, str(self.source)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )

        def require_child_signal(expected: str) -> None:
            assert process.stdout is not None
            self.assertTrue(select.select([process.stdout], [], [], 10)[0])
            self.assertEqual(process.stdout.readline().strip(), expected)

        try:
            require_child_signal("BASE")
            self.assertTrue(shm.exists())
            shutil.copyfile(shm, stale_shm)
            assert process.stdin is not None
            process.stdin.write(chr(10))
            process.stdin.flush()
            require_child_signal("READY")
            gc.collect()

            def swapped_connect(database, *args, **kwargs):
                nonlocal swapped
                if swapped or database != source_uri:
                    return original_connect(database, *args, **kwargs)
                swapped = True
                os.replace(shm, held_shm)
                os.replace(stale_shm, shm)
                try:
                    connection = original_connect(database, *args, **kwargs)
                    connection.execute("PRAGMA schema_version").fetchone()
                    return connection
                finally:
                    os.replace(shm, stale_shm)
                    os.replace(held_shm, shm)

            with patch("mark_api.backup_cli.sqlite3.connect", side_effect=swapped_connect):
                with self.assertRaisesRegex(BackupError, "WAL source identity"):
                    backup_store(self.source, backup_db=self.backup)
            self.assertTrue(swapped)
            self.assertFalse(self.backup.exists())
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def test_destination_parent_swap_never_claims_attacker_directory(self) -> None:
        destination_parent = self.root / "private-backups"
        destination_parent.mkdir(mode=0o700)
        destination = destination_parent / "new.sqlite"
        attacker_parent = self.root / "attacker-backups"
        attacker_parent.mkdir(mode=0o700)
        displaced = self.root / "private-backups-moved"
        real_link = os.link
        swapped = False

        def swap_parent_at_link(src, dst, *args, **kwargs):
            nonlocal swapped
            if swapped:
                return real_link(src, dst, *args, **kwargs)
            swapped = True
            destination_parent.rename(displaced)
            # A replacement *real directory* passes a path-only O_NOFOLLOW
            # parent fsync, unlike a symlink; this is the adversarial case.
            attacker_parent.rename(destination_parent)
            # The vulnerable path-based implementation could be lured into
            # linking an attacker copy of its temp stage and receipting it.
            try:
                relative = Path(src).relative_to(destination_parent)
            except ValueError:
                pass  # A pinned /proc/self/fd source cannot be redirected.
            else:
                shadow = destination_parent / relative
                shadow.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(displaced / relative, shadow)
            return real_link(src, dst, *args, **kwargs)

        try:
            with patch("mark_api.backup_cli.os.link", side_effect=swap_parent_at_link):
                with self.assertRaisesRegex(BackupError, "destination directory"):
                    backup_store(self.source, backup_db=destination)
        finally:
            if destination_parent.exists() and displaced.exists():
                destination_parent.rename(attacker_parent)
            if displaced.exists():
                displaced.rename(destination_parent)
        self.assertTrue(swapped)
        self.assertFalse((attacker_parent / "new.sqlite").exists())

    def test_missing_sqlite_fd_attestation_fails_closed(self) -> None:
        # Do not silently fall back to pathname validation on non-Linux or
        # procfs-restricted hosts: it would reintroduce the race above.
        with patch("mark_api.backup_cli.os.listdir", side_effect=OSError("blocked")):
            with self.assertRaisesRegex(BackupError, "attestation is unavailable"):
                backup_store(self.source, backup_db=self.backup)
        self.assertFalse(self.backup.exists())
        self.assertTrue(self.store.is_ready())

    def test_late_directory_fsync_error_preserves_new_backup_without_retry(self) -> None:
        # Publication may have succeeded even when the durability confirmation
        # fails. The caller must inspect the now-existing target, never retry
        # over that name or silently remove a potentially valid backup.
        self._seed_recovery_data()
        with patch("mark_api.backup_cli._fsync_directory", side_effect=OSError("io")):
            with self.assertRaisesRegex(BackupError, "publication uncertain"):
                backup_store(self.source, backup_db=self.backup)

        self.assertTrue(self.backup.is_file())
        original_bytes = self.backup.read_bytes()
        with self.assertRaisesRegex(BackupError, "already exists"):
            backup_store(self.source, backup_db=self.backup)
        self.assertEqual(self.backup.read_bytes(), original_bytes)

        # A late publication error does not assert complete durability. In the
        # injected environment the file is readable and its recovery fences
        # are still intact; no platform action is ever triggered.
        restored = self.root / "late-restore.sqlite"
        shutil.copyfile(self.backup, restored)
        os.chmod(restored, 0o600)
        store = SnapshotStore(restored, create_if_missing=False)
        self.assertTrue(store.is_ready())
        self.assertEqual(store.sync_status()["state"], "in_progress")
        with sqlite3.connect(restored) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM write_api_requests "
                    "WHERE idempotency_key='pending-api-fence'"
                ).fetchone(),
                ("in_progress",),
            )

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