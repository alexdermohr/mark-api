from __future__ import annotations

from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
import gc
import hashlib
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

    def test_transient_existing_wal_content_edit_never_claims_success(self) -> None:
        # An attacker can edit and restore bytes on the same open WAL inode.
        # Device/inode, final WAL magic and st_size alone do not reveal this.
        wal = self.source.with_name(self.source.name + "-wal")
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key, request_sha256, state, requested_at) "
                "VALUES (?, ?, 'in_progress', ?)",
                ("pending-wal-content", "d" * 64, NOW.isoformat()),
            )
            writer.commit()
            self.assertGreater(wal.stat().st_size, 32)
            before = wal.stat()
            original_count = backup_cli._count
            tampered = False

            def transient_wal_edit(connection, sql):
                nonlocal tampered
                if not tampered:
                    tampered = True
                    with wal.open("r+b") as stream:
                        original_byte = stream.read(1)
                        self.assertEqual(len(original_byte), 1)
                        stream.seek(0)
                        stream.write(bytes((original_byte[0] ^ 1,)))
                        stream.flush()
                        os.fsync(stream.fileno())
                        stream.seek(0)
                        stream.write(original_byte)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.utime(wal, ns=(before.st_atime_ns, before.st_mtime_ns))
                return original_count(connection, sql)

            with patch("mark_api.backup_cli._count", side_effect=transient_wal_edit):
                with self.assertRaisesRegex(BackupError, "WAL source contents changed"):
                    backup_store(self.source, backup_db=self.backup)
            self.assertTrue(tampered)
            self.assertFalse(self.backup.exists())
            self.assertEqual(
                writer.execute(
                    "SELECT state FROM write_api_requests "
                    "WHERE idempotency_key='pending-wal-content'"
                ).fetchone(),
                ("in_progress",),
            )

    def test_transient_existing_shm_content_edit_never_claims_success(self) -> None:
        # A forged WAL-index could be reverted on the original -shm inode,
        # leaving inode, size and final bytes apparently intact. ctime must
        # still invalidate the backup receipt.
        shm = self.source.with_name(self.source.name + "-shm")
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key, request_sha256, state, requested_at) "
                "VALUES (?, ?, 'in_progress', ?)",
                ("pending-shm-content", "d" * 64, NOW.isoformat()),
            )
            writer.commit()
            self.assertTrue(shm.is_file())
            initial = shm.stat()
            original_count = backup_cli._count
            tampered = False

            def transient_shm_edit(connection, sql):
                nonlocal tampered
                if not tampered:
                    tampered = True
                    with shm.open("r+b") as handle:
                        old = handle.read(1)
                        self.assertEqual(len(old), 1)
                        handle.seek(0)
                        handle.write(bytes((old[0] ^ 1,)))
                        handle.flush()
                        os.fsync(handle.fileno())
                        handle.seek(0)
                        handle.write(old)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.utime(shm, ns=(initial.st_atime_ns, initial.st_mtime_ns))
                return original_count(connection, sql)

            with patch("mark_api.backup_cli._count", side_effect=transient_shm_edit):
                with self.assertRaisesRegex(BackupError, "WAL source contents changed"):
                    backup_store(self.source, backup_db=self.backup)
            self.assertTrue(tampered)
            self.assertFalse(self.backup.exists())

    def test_external_live_wal_writer_is_backed_up_without_false_content_drift(self) -> None:
        # A separate process keeps committed WAL frames live. A read-only
        # backup must include those frames without rejecting harmless
        # SQLite open-time metadata on a healthy source.
        child = """
import sqlite3, sys
from contextlib import closing
with closing(sqlite3.connect(sys.argv[1])) as writer:
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute(
        "INSERT INTO write_api_requests "
        "(idempotency_key,request_sha256,state,requested_at) "
        "VALUES ('external-live-fence','e','in_progress','t')"
    )
    writer.commit()
    print('READY',flush=True)
    sys.stdin.readline()
"""
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", child, str(self.source)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            self.assertTrue(select.select([process.stdout], [], [], 10)[0])
            self.assertEqual(process.stdout.readline().strip(), "READY")
            self.assertGreater(
                self.source.with_name(self.source.name + "-wal").stat().st_size, 32,
            )
            receipt = backup_store(self.source, backup_db=self.backup)
            self.assertEqual(receipt.pending_api_writes, 1)
            with closing(sqlite3.connect(self.backup)) as copy:
                self.assertEqual(
                    copy.execute(
                        "SELECT state FROM write_api_requests "
                        "WHERE idempotency_key='external-live-fence'"
                    ).fetchone(),
                    ("in_progress",),
                )
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

    def test_rollback_mode_source_read_uses_locking_not_immutable(self) -> None:
        # Ordinary Mark SQLite stores use rollback/DELETE journal mode. A
        # sidecar-free source is not necessarily WAL mode: immutable=1 bypasses
        # SQLite shared locks and can certify a torn concurrent Write snapshot.
        self.assertEqual(self.source.read_bytes()[18:20], bytes((1, 1)))
        original_connect = sqlite3.connect
        source_uri = backup_cli._uri(self.source, "ro")
        source_opened = False

        def attest_source_uses_locking(database, *args, **kwargs):
            nonlocal source_opened
            connection = original_connect(database, *args, **kwargs)
            if isinstance(database, str) and database.startswith(source_uri) and not source_opened:
                source_opened = True
                self.assertEqual(database, source_uri)
                connection.execute("BEGIN")
                connection.execute("SELECT count(*) FROM write_api_requests").fetchone()
                with closing(original_connect(self.source, timeout=0)) as concurrent:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                        concurrent.execute("BEGIN EXCLUSIVE")
                connection.execute("ROLLBACK")
            return connection

        with patch("mark_api.backup_cli.sqlite3.connect", side_effect=attest_source_uses_locking):
            receipt = backup_store(self.source, backup_db=self.backup)
        self.assertTrue(source_opened)
        self.assertEqual(receipt.pending_api_writes, 0)
        self.assertTrue(self.backup.is_file())

    def test_sqlite_open_time_wal_ctime_only_drift_keeps_valid_snapshot(self) -> None:
        # SQLite 3.45 on Python 3.14 may advance a healthy WAL's ctime
        # during schema_version without changing WAL bytes/size/mtime.
        # Emulate that filesystem event on Python 3.12 to cover both builds.
        wal = self.source.with_name(self.source.name + "-wal")
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key, request_sha256, state, requested_at) "
                "VALUES (?, ?, 'in_progress', ?)",
                ("open-time-ctime-fence", "d" * 64, NOW.isoformat()),
            )
            writer.commit()
            initial = wal.stat()
            source_uri = backup_cli._uri(self.source, "ro")
            original_connect = sqlite3.connect
            simulated = False

            def simulate_sqlite_open_time_ctime(database, *args, **kwargs):
                nonlocal simulated
                conn = original_connect(database, *args, **kwargs)
                if database == source_uri and not simulated:
                    simulated = True
                    conn.execute("PRAGMA schema_version").fetchone()
                    os.utime(wal, ns=(initial.st_atime_ns, initial.st_mtime_ns))
                    self.assertNotEqual(wal.stat().st_ctime_ns, initial.st_ctime_ns)
                    self.assertEqual(wal.stat().st_mtime_ns, initial.st_mtime_ns)
                return conn

            with patch(
                "mark_api.backup_cli.sqlite3.connect",
                side_effect=simulate_sqlite_open_time_ctime,
            ):
                receipt = backup_store(self.source, backup_db=self.backup)
            self.assertTrue(simulated)
            self.assertEqual(receipt.pending_api_writes, 1)
            with closing(sqlite3.connect(self.backup)) as restored:
                self.assertEqual(
                    restored.execute(
                        "SELECT state FROM write_api_requests "
                        "WHERE idempotency_key='open-time-ctime-fence'"
                    ).fetchone(),
                    ("in_progress",),
                )

    def test_quiescent_wal_database_without_sidecars_can_be_backed_up(self) -> None:
        # Closing the final WAL writer checkpoints and removes -wal/-shm.
        # A mode=ro SQLite reader may legitimately create an EMPTY WAL and
        # shared-memory index on the next open. Rejecting every new sidecar
        # would break normal backups of a healthy quiet WAL-mode database.
        wal = self.source.with_name(self.source.name + "-wal")
        shm = self.source.with_name(self.source.name + "-shm")
        with closing(sqlite3.connect(self.source)) as writer:
            self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))
        gc.collect()
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())

        receipt = backup_store(self.source, backup_db=self.backup)

        self.assertTrue(self.backup.exists())
        self.assertEqual(receipt.pending_api_writes, 0)
        with closing(sqlite3.connect(self.backup)) as check:
            self.assertEqual(check.execute("PRAGMA integrity_check").fetchone(), ("ok",))

    def test_previously_absent_wal_rejects_newly_planted_old_frames(self) -> None:
        # An attacker may plant an earlier valid -wal/-shm pair after the
        # initial "absent" identity read but before sqlite3.connect(path).
        # Unlike an empty SQLite-created WAL, the captured pair contains
        # frames and must fail closed before any backup is published.
        wal = self.source.with_name(self.source.name + "-wal")
        shm = self.source.with_name(self.source.name + "-shm")
        saved_wal = self.root / "captured-old-wal"
        saved_shm = self.root / "captured-old-shm"
        with closing(sqlite3.connect(self.source)) as writer:
            self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key, request_sha256, state, requested_at) "
                "VALUES (?, ?, 'in_progress', ?)",
                ("old-wal-fence", "e" * 64, NOW.isoformat()),
            )
            writer.commit()
            self.assertGreater(wal.stat().st_size, 32)
            shutil.copyfile(wal, saved_wal)
            shutil.copyfile(shm, saved_shm)
            writer.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key, request_sha256, state, requested_at) "
                "VALUES (?, ?, 'in_progress', ?)",
                ("newer-main-fence", "f" * 64, NOW.isoformat()),
            )
            writer.commit()
        gc.collect()
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())
        real_connect = sqlite3.connect
        source_uri = backup_cli._uri(self.source, "ro")
        planted = False

        def connect_after_planted_old_wal(database, *args, **kwargs):
            nonlocal planted
            if not isinstance(database, str) or not database.startswith(source_uri) or planted:
                return real_connect(database, *args, **kwargs)
            planted = True
            shutil.copyfile(saved_wal, wal)
            shutil.copyfile(saved_shm, shm)
            connection = real_connect(database, *args, **kwargs)
            connection.execute("PRAGMA schema_version").fetchone()
            return connection

        with patch("mark_api.backup_cli.sqlite3.connect", side_effect=connect_after_planted_old_wal):
            with self.assertRaisesRegex(BackupError, "WAL source identity"):
                backup_store(self.source, backup_db=self.backup)
        self.assertTrue(planted)
        self.assertFalse(self.backup.exists())

    def test_new_wal_during_immutable_backup_blocks_publication(self) -> None:
        # A quiet WAL database has no sidecars. Immutable source reads the
        # stable main file without consuming concurrent newly created WALs.
        # If a legitimate writer starts during the copy, fail closed rather
        # than claiming a backup with potentially incomplete recovery data.
        wal = self.source.with_name(self.source.name + "-wal")
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
        gc.collect()
        self.assertFalse(wal.exists())
        original_validate = backup_cli._require_healthy_store
        new_writer: sqlite3.Connection | None = None
        injected = False

        def concurrent_commit(connection):
            nonlocal injected, new_writer
            if not injected:
                injected = True
                self.assertFalse(wal.exists())
                new_writer = sqlite3.connect(self.source)
                new_writer.execute("PRAGMA wal_autocheckpoint=0")
                new_writer.execute(
                    "INSERT INTO write_api_requests "
                    "(idempotency_key, request_sha256, state, requested_at) "
                    "VALUES (?, ?, 'in_progress', ?)",
                    ("new-wal-concurrent-fence", "c" * 64, NOW.isoformat()),
                )
                new_writer.commit()
                self.assertGreater(wal.stat().st_size, 32)
            return original_validate(connection)

        try:
            with patch(
                "mark_api.backup_cli._require_healthy_store",
                side_effect=concurrent_commit,
            ):
                with self.assertRaisesRegex(BackupError, "WAL source identity"):
                    backup_store(self.source, backup_db=self.backup)
            self.assertTrue(injected)
            self.assertFalse(self.backup.exists())
        finally:
            if new_writer is not None:
                new_writer.close()
        with closing(sqlite3.connect(self.source)) as check:
            self.assertEqual(
                check.execute(
                    "SELECT state FROM write_api_requests "
                    "WHERE idempotency_key='new-wal-concurrent-fence'"
                ).fetchone(),
                ("in_progress",),
            )

    def test_quiet_source_stale_pager_cannot_lose_open_write_fence(self) -> None:
        # Model the outcome of a transient same-inode mmap overwrite during
        # SQLite's source read. Metadata and inode of the actual source stay
        # valid after restoration, but the copied SQLite pages are from an
        # older healthy Mark database without the pending Write fence.
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key, request_sha256, state, requested_at) "
                "VALUES ('source-current-fence', 'a', 'in_progress', ?)",
                (NOW.isoformat(),),
            )
            writer.commit()
        gc.collect()
        self.assertFalse(self.source.with_name(self.source.name + "-wal").exists())
        stale = self.root / "stale-valid.sqlite"
        SnapshotStore(stale)
        gc.collect()
        self.assertEqual(self.source.stat().st_size, stale.stat().st_size)
        normal_connect = sqlite3.connect
        uri = backup_cli._uri(self.source, "ro") + "&immutable=1"
        used = False

        class StaleSourcePages(sqlite3.Connection):
            def backup(self, target, *, pages=-1, progress=None, name="main", sleep=0.250):
                nonlocal used
                used = True
                with closing(normal_connect(stale)) as older:
                    return older.backup(
                        target, pages=pages, progress=progress, name=name, sleep=sleep,
                    )

        def source_with_transient_stale_pages(database, *args, **kwargs):
            if database == uri:
                kwargs["factory"] = StaleSourcePages
            return normal_connect(database, *args, **kwargs)

        with patch(
            "mark_api.backup_cli.sqlite3.connect",
            side_effect=source_with_transient_stale_pages,
        ):
            with self.assertRaisesRegex(
                BackupError, "source snapshot contents",
            ):
                backup_store(self.source, backup_db=self.backup)
        self.assertTrue(used)
        self.assertFalse(self.backup.exists())
        with closing(sqlite3.connect(self.source)) as current:
            self.assertEqual(
                current.execute(
                    "SELECT state FROM write_api_requests "
                    "WHERE idempotency_key='source-current-fence'"
                ).fetchone(),
                ("in_progress",),
            )

    def test_same_inode_main_content_restore_never_loses_pending_write(self) -> None:
        # An attacker can overwrite the SAME source inode with an older valid
        # Mark DB for SQLite's read, then restore the original contents while
        # preserving the original mtime. An inode/header check cannot detect
        # this; Linux st_ctime_ns can.
        with closing(sqlite3.connect(self.source)) as writer:
            self.assertEqual(
                writer.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",),
            )
            writer.execute("PRAGMA wal_autocheckpoint=0")
            self.assertEqual(writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0], 0)
            old_contents = self.source.read_bytes()
            writer.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key, request_sha256, state, requested_at) "
                "VALUES (?, ?, 'in_progress', ?)",
                ("real-main-pending-fence", "d" * 64, NOW.isoformat()),
            )
            writer.commit()
            self.assertEqual(writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0], 0)
            real_contents = self.source.read_bytes()
            original_stat = self.source.stat()
            self.assertNotEqual(old_contents, real_contents)
            self.assertTrue(self.source.with_name(self.source.name + "-wal").exists())
            source_uri = backup_cli._uri(self.source, "ro")
            original_connect = sqlite3.connect
            original_count = backup_cli._count
            swapped = False
            restored = False

            def set_main_bytes(contents: bytes) -> None:
                with self.source.open("r+b") as target:
                    target.write(contents)
                    target.truncate()
                    target.flush()
                    os.fsync(target.fileno())

            def swap_main_before_connect(database, *args, **kwargs):
                nonlocal swapped
                if isinstance(database, str) and database.startswith(source_uri) and not swapped:
                    swapped = True
                    set_main_bytes(old_contents)
                return original_connect(database, *args, **kwargs)

            def restore_main_after_snapshot(connection, sql):
                nonlocal restored
                if not restored:
                    restored = True
                    set_main_bytes(real_contents)
                    os.utime(
                        self.source,
                        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                    )
                return original_count(connection, sql)

            try:
                with (
                    patch("mark_api.backup_cli.sqlite3.connect", side_effect=swap_main_before_connect),
                    patch("mark_api.backup_cli._count", side_effect=restore_main_after_snapshot),
                ):
                    with self.assertRaisesRegex(BackupError, "source contents changed"):
                        backup_store(self.source, backup_db=self.backup)
            finally:
                if not restored:
                    set_main_bytes(real_contents)
                    os.utime(
                        self.source,
                        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                    )
            self.assertTrue(swapped)
            self.assertFalse(self.backup.exists())
        with closing(sqlite3.connect(self.source)) as verify:
            self.assertEqual(
                verify.execute(
                    "SELECT state FROM write_api_requests "
                    "WHERE idempotency_key='real-main-pending-fence'"
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

    def test_hot_rollback_journal_blocks_immutable_backup(self) -> None:
        # A killed SQLite writer can leave a hot rollback journal with
        # uncommitted pages already spilled into the main file. immutable=1
        # skips recovery, so it must never treat this as a clean snapshot.
        with closing(sqlite3.connect(self.source)) as db:
            self.assertEqual(
                db.execute("PRAGMA journal_mode=DELETE").fetchone(), ("delete",),
            )
            db.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key,request_sha256,state,requested_at) "
                "VALUES ('hot-journal-fence','e','in_progress','t')"
            )
            db.commit()
        child = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute("PRAGMA journal_mode=DELETE")
db.execute("PRAGMA cache_size=1")
db.execute("PRAGMA cache_spill=ON")
db.execute("BEGIN IMMEDIATE")
db.execute("DELETE FROM write_api_requests WHERE idempotency_key='hot-journal-fence'")
for n in range(300):
    db.execute(
        "INSERT INTO inbound_message_events "
        "(provider_message_id,ad_id,conversation_id,observed_at,source) "
        "VALUES (?,?,?,?,?)",
        (str(n), 'ad', 'conversation', 'now', 'email'),
    )
os._exit(7)
"""
        result = subprocess.run(
            [sys.executable, "-c", child, str(self.source)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        journal = self.source.with_name(self.source.name + "-journal")
        self.assertTrue(journal.is_file())
        self.assertGreater(journal.stat().st_size, 512)
        with self.assertRaisesRegex(BackupError, "rollback journal"):
            backup_store(self.source, backup_db=self.backup)
        self.assertFalse(self.backup.exists())
        self.assertTrue(journal.exists())

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
            if not isinstance(database, str) or not database.startswith(source_uri) or swapped:
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

    def test_nonempty_wal_without_shm_fails_closed(self) -> None:
        # An initially nonempty WAL with no SHM could be filled with earlier
        # valid frames during SQLite open, then restored after a snapshot.
        # Do not allow the SQLite connection to reconstruct an unverifiable
        # WAL index from an incomplete sidecar set.
        wal = self.source.with_name(self.source.name + "-wal")
        shm = self.source.with_name(self.source.name + "-shm")
        captured = self.root / "captured-wal"
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key,request_sha256,state,requested_at) "
                "VALUES ('captured-fence','a','in_progress','t')"
            )
            writer.commit()
            self.assertGreater(wal.stat().st_size, 32)
            shutil.copyfile(wal, captured)
        gc.collect()
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())
        shutil.copyfile(captured, wal)
        self.assertFalse(shm.exists())
        with self.assertRaisesRegex(BackupError, "missing shared memory"):
            backup_store(self.source, backup_db=self.backup)
        self.assertFalse(self.backup.exists())

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

    def test_named_stage_substitution_cannot_affect_anonymous_copy(self) -> None:
        # A same-UID attacker can fabricate the old predictable stage
        # directory and an older valid SQLite image. No SQLite connection
        # may read or write that named stage: backup goes to private memory,
        # then O_TMPFILE without a replaceable stage pathname.
        with closing(sqlite3.connect(self.source)) as db:
            db.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key, request_sha256, state, requested_at) "
                "VALUES (?, ?, 'in_progress', ?)",
                ("original-source-pending", "d" * 64, NOW.isoformat()),
            )
            db.commit()
        fake = self.root / "older-valid-mark.sqlite"
        SnapshotStore(fake)
        gc.collect()
        named_stage = self.root / ".mark-backup-attacker"
        original_open = os.open
        attacked = False

        def create_named_decoy(path, flags, *args, **kwargs):
            nonlocal attacked
            if not attacked and flags & os.O_TMPFILE == os.O_TMPFILE:
                attacked = True
                named_stage.mkdir(mode=0o700)
                shutil.copyfile(fake, named_stage / "backup.sqlite")
            return original_open(path, flags, *args, **kwargs)

        with patch("mark_api.backup_cli.os.open", side_effect=create_named_decoy):
            receipt = backup_store(self.source, backup_db=self.backup)

        self.assertTrue(attacked)
        self.assertEqual(receipt.pending_api_writes, 1)
        self.assertTrue(self.backup.exists())
        with closing(sqlite3.connect(self.backup)) as backed_up:
            self.assertEqual(
                backed_up.execute(
                    "SELECT state FROM write_api_requests "
                    "WHERE idempotency_key='original-source-pending'"
                ).fetchone(),
                ("in_progress",),
            )
        with closing(sqlite3.connect(named_stage / "backup.sqlite")) as decoy:
            self.assertEqual(
                decoy.execute(
                    "SELECT count(*) FROM write_api_requests "
                    "WHERE idempotency_key='original-source-pending'"
                ).fetchone(),
                (0,),
            )

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

    def test_stage_copy_connection_to_other_inode_never_receives_write_data(self) -> None:
        # The SQLite unix VFS can resolve /proc/self/fd/N to a mutable
        # pathname and open a different inode. Validate its FD before
        # invoking the data-bearing online backup, or pending write fences
        # would be copied into an attacker-accessible file.
        with closing(sqlite3.connect(self.source)) as db:
            db.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key,request_sha256,state,requested_at) "
                "VALUES ('private-write-fence','c','in_progress','t')"
            )
            db.commit()
        impostor = self.root / "attacker.sqlite"
        SnapshotStore(impostor)
        gc.collect()
        real_connect = sqlite3.connect
        used = False

        def redirect_stage_copy(database, *args, **kwargs):
            nonlocal used
            if (
                not used and isinstance(database, str)
                and database.startswith("file:/proc/self/fd/")
                and database.endswith("?mode=rw")
            ):
                used = True
                return real_connect(impostor)
            return real_connect(database, *args, **kwargs)

        with patch("mark_api.backup_cli.sqlite3.connect", side_effect=redirect_stage_copy):
            receipt = backup_store(self.source, backup_db=self.backup)
        # No file-backed SQLite stage connection is used: snapshot pages stay
        # within :memory: until serialized into an anonymous O_TMPFILE.
        self.assertFalse(used)
        self.assertEqual(receipt.pending_api_writes, 1)
        self.assertTrue(self.backup.exists())
        with closing(sqlite3.connect(self.backup)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT state FROM write_api_requests "
                    "WHERE idempotency_key='private-write-fence'"
                ).fetchone(), ("in_progress",),
            )
        with closing(sqlite3.connect(impostor)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM write_api_requests "
                    "WHERE idempotency_key='private-write-fence'"
                ).fetchone(),
                (0,),
            )

    def test_stage_check_connection_to_other_inode_never_uses_wrong_receipt(self) -> None:
        # A separate stage check connection also must prove the opened FD is
        # the pinned, freshly copied inode before integrity/receipt reads.
        with closing(sqlite3.connect(self.source)) as db:
            db.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key,request_sha256,state,requested_at) "
                "VALUES ('real-stage-check-fence','c','in_progress','t')"
            )
            db.commit()
        other = self.root / "attacker-healthy.sqlite"
        SnapshotStore(other)
        gc.collect()
        real_connect = sqlite3.connect
        used = False

        def redirect_check(database, *args, **kwargs):
            nonlocal used
            if (
                not used and isinstance(database, str)
                and database.startswith("file:/proc/self/fd/")
                and "?mode=ro" in database
            ):
                used = True
                return real_connect("file:" + str(other) + "?mode=ro", uri=True)
            return real_connect(database, *args, **kwargs)

        with patch("mark_api.backup_cli.sqlite3.connect", side_effect=redirect_check):
            receipt = backup_store(self.source, backup_db=self.backup)
        self.assertFalse(used)
        self.assertEqual(receipt.pending_api_writes, 1)
        with closing(sqlite3.connect(self.backup)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT state FROM write_api_requests "
                    "WHERE idempotency_key='real-stage-check-fence'"
                ).fetchone(), ("in_progress",),
            )

    def test_anonymous_stage_has_no_name_before_create_only_publication(self) -> None:
        # The staged inode cannot be reached by a swap of a .mark-backup-*
        # pathname: before link(2), it has no directory entry at all.
        original_link = os.link
        checked = False

        def check_anonymous_stage(src, dst, *args, **kwargs):
            nonlocal checked
            fd = int(str(src).rsplit("/", 1)[-1])
            info = os.fstat(fd)
            checked = True
            self.assertTrue(stat.S_ISREG(info.st_mode))
            self.assertEqual(info.st_nlink, 0)
            self.assertFalse(any(
                p.name.startswith(".mark-backup-") for p in self.root.iterdir()
            ))
            self.assertFalse(self.backup.exists())
            return original_link(src, dst, *args, **kwargs)

        with patch("mark_api.backup_cli.os.link", side_effect=check_anonymous_stage):
            receipt = backup_store(self.source, backup_db=self.backup)
        self.assertTrue(checked)
        self.assertTrue(self.backup.is_file())
        self.assertEqual(
            hashlib.sha256(self.backup.read_bytes()).hexdigest(),
            receipt.backup_sha256,
        )

    def test_stage_hash_never_rebaselines_after_verified_memory_image(self) -> None:
        # Model a stage whose bytes change after the first trusted
        # in-memory-image hash comparison, while filesystem metadata appears
        # unchanged (e.g. an already-dirty shared mmap page). Both later
        # readbacks may agree on a stale digest; neither may supersede the
        # original validated snapshot hash.
        genuine_hash = backup_cli._sha256_fd
        reads = 0
        substituted_hash = "f" * 64

        def later_stage_hash(descriptor):
            nonlocal reads
            reads += 1
            if reads == 1:
                return genuine_hash(descriptor)
            return substituted_hash

        with patch("mark_api.backup_cli._sha256_fd", side_effect=later_stage_hash):
            with self.assertRaisesRegex(BackupError, "anonymous backup stage contents changed"):
                backup_store(self.source, backup_db=self.backup)
        self.assertGreaterEqual(reads, 2)
        self.assertFalse(self.backup.exists())
        self.assertTrue(self.store.is_ready())

    def test_in_memory_backup_copy_memory_error_fails_closed(self) -> None:
        # Allocation can fail inside sqlite3.Connection.backup() before
        # serialize() runs. The CLI must return a controlled BackupError
        # and never create or publish a partial target.
        class SourceFailingDuringCopy(sqlite3.Connection):
            def backup(self, target, *, pages=-1, progress=None, name="main", sleep=0.250):
                raise MemoryError("synthetic in-memory SQLite copy exhaustion")

        source_uri = backup_cli._uri(self.source, "ro")
        actual_connect = sqlite3.connect
        triggered = False

        def connect_with_failing_backup(database, *args, **kwargs):
            nonlocal triggered
            if database == source_uri:
                triggered = True
                kwargs["factory"] = SourceFailingDuringCopy
            return actual_connect(database, *args, **kwargs)

        with patch(
            "mark_api.backup_cli.sqlite3.connect",
            side_effect=connect_with_failing_backup,
        ):
            with self.assertRaisesRegex(BackupError, "in-memory backup"):
                backup_store(self.source, backup_db=self.backup)
        self.assertTrue(triggered)
        self.assertFalse(self.backup.exists())
        self.assertTrue(self.store.is_ready())

    def test_in_memory_snapshot_serialization_memory_error_fails_closed(self) -> None:
        # In-memory snapshots require available RAM. Exhaustion must fail
        # before a target is linked, without deleting recovery fences.
        class UnserializableSnapshot(sqlite3.Connection):
            def serialize(self, name="main"):
                raise MemoryError("synthetic exhausted snapshot memory")

        original_connect = sqlite3.connect
        intercepted = False

        def memory_limited_connect(database, *args, **kwargs):
            nonlocal intercepted
            if database == ":memory:":
                intercepted = True
                kwargs["factory"] = UnserializableSnapshot
            return original_connect(database, *args, **kwargs)

        with patch(
            "mark_api.backup_cli.sqlite3.connect",
            side_effect=memory_limited_connect,
        ):
            with self.assertRaisesRegex(
                BackupError, "in-memory backup serialization unavailable",
            ):
                backup_store(self.source, backup_db=self.backup)
        self.assertTrue(intercepted)
        self.assertFalse(self.backup.exists())
        self.assertTrue(self.store.is_ready())

    def test_unavailable_anonymous_stage_fails_closed(self) -> None:
        original_open = os.open
        attempted = False

        def fail_unsupported_tmpfile(path, flags, *args, **kwargs):
            nonlocal attempted
            if flags & os.O_TMPFILE == os.O_TMPFILE:
                attempted = True
                raise OSError("filesystem does not support O_TMPFILE")
            return original_open(path, flags, *args, **kwargs)

        with patch("mark_api.backup_cli.os.open", side_effect=fail_unsupported_tmpfile):
            with self.assertRaisesRegex(BackupError, "anonymous backup staging is unavailable"):
                backup_store(self.source, backup_db=self.backup)
        self.assertTrue(attempted)
        self.assertFalse(self.backup.exists())
        self.assertTrue(self.store.is_ready())

    def test_stage_content_tamper_after_integrity_cannot_publish_stale_copy(self) -> None:
        # This is intentionally after the stage's SQL integrity/count reads.
        # A same-UID process can still write through an already writable FD,
        # even if the staged file has since been chmod'ed 0400.
        with closing(sqlite3.connect(self.source)) as db:
            db.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key,request_sha256,state,requested_at) "
                "VALUES ('real-stage-pending-fence','f','in_progress','t')"
            )
            db.commit()
        old = self.root / "stale-valid-mark.sqlite"
        SnapshotStore(old)
        gc.collect()
        older_data = old.read_bytes()
        original_hash = backup_cli._sha256_fd
        tampered = False

        def overwrite_verified_stage_before_hash(descriptor):
            nonlocal tampered
            tampered = True
            os.ftruncate(descriptor, len(older_data))
            self.assertEqual(os.pwrite(descriptor, older_data, 0), len(older_data))
            os.fsync(descriptor)
            return original_hash(descriptor)

        with patch(
            "mark_api.backup_cli._sha256_fd",
            side_effect=overwrite_verified_stage_before_hash,
        ):
            with self.assertRaisesRegex(BackupError, "backup stage contents changed"):
                backup_store(self.source, backup_db=self.backup)
        self.assertTrue(tampered)
        self.assertFalse(self.backup.exists())
        with closing(sqlite3.connect(self.source)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT state FROM write_api_requests "
                    "WHERE idempotency_key='real-stage-pending-fence'"
                ).fetchone(),
                ("in_progress",),
            )

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


    @unittest.expectedFailure
    def test_cross_process_predirtied_mmap_cannot_drop_sqlite_write_fence(self) -> None:
        """Issue #76 P2, intentionally XFAIL until backup has independent integrity.

        A distinct Linux process holds a writable MAP_SHARED source mapping
        before attestation. The parent controls *only timing*, not SQLite's
        copy or the mapped bytes: the child substitutes a valid older image
        during SQLite backup and the raw-image check, then restores it before
        final metadata checks. The security contract is to reject such a
        snapshot or preserve every durable fence. Remove expectedFailure only
        after a genuinely independent storage authority closes this attack.
        """
        if not hasattr(os, "O_TMPFILE") or not sys.platform.startswith("linux"):
            self.skipTest("requires Linux anonymous backup stage and mmap")
        with closing(sqlite3.connect(self.source)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA journal_mode=WAL").fetchone(),
                ("wal",),
            )
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute(
                "INSERT INTO write_api_requests "
                "(idempotency_key, request_sha256, state, requested_at) "
                "VALUES (?, ?, 'in_progress', ?)",
                ("cross-process-mmap-fence", "a" * 64, NOW.isoformat()),
            )
            connection.commit()
            self.assertEqual(
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0],
                0,
            )
        gc.collect()
        self.assertFalse(self.source.with_name(self.source.name + "-wal").exists())
        self.assertFalse(self.source.with_name(self.source.name + "-shm").exists())

        old = self.root / "before-fence.sqlite"
        SnapshotStore(old)
        gc.collect()
        current = self.root / "current-image.bin"
        current.write_bytes(self.source.read_bytes())
        self.assertEqual(old.stat().st_size, current.stat().st_size)
        self.assertNotEqual(old.read_bytes(), current.read_bytes())

        # No unsafe pickle, inherited SQLite handle, platform adapter or
        # app code in the child; only stdlib mmap and a two-command protocol.
        child_code = """
import mmap
import os
from pathlib import Path
import sys

source, previous, current = map(Path, sys.argv[1:])
stale_bytes = previous.read_bytes()
current_bytes = current.read_bytes()
with source.open("r+b", buffering=0) as opened:
    with mmap.mmap(opened.fileno(), 0, access=mmap.ACCESS_WRITE) as shared:
        assert len(shared) == len(stale_bytes) == len(current_bytes)
        for offset in range(0, len(shared), mmap.PAGESIZE):
            shared[offset] = shared[offset]
        before = os.fstat(opened.fileno())
        print("READY", flush=True)
        for line in sys.stdin:
            command = line.strip()
            if command == "EXIT":
                break
            if command not in ("SWAP", "RESTORE"):
                raise ValueError("unexpected child command")
            shared[:] = stale_bytes if command == "SWAP" else current_bytes
            after = os.fstat(opened.fileno())
            if (before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_mtime_ns, after.st_ctime_ns
            ):
                print("METADATA_DRIFT", flush=True)
            else:
                print("SWAPPED" if command == "SWAP" else "RESTORED", flush=True)
"""
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", child_code,
             str(self.source), str(old), str(current)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        self.assertIsNotNone(child.stdin)
        self.assertIsNotNone(child.stdout)
        self.assertIsNotNone(child.stderr)

        def receive() -> str:
            assert child.stdout is not None
            ready, _, _ = select.select([child.stdout], [], [], 5.0)
            self.assertTrue(ready, "mapped subprocess did not respond")
            return child.stdout.readline().strip()

        def command(value: str, expected: str) -> None:
            assert child.stdin is not None
            child.stdin.write(value + "\n")
            child.stdin.flush()
            self.assertEqual(receive(), expected)

        try:
            self.assertEqual(receive(), "READY")
            actual_connect = sqlite3.connect
            actual_open = os.open
            source_uri = backup_cli._uri(self.source, "ro") + "&immutable=1"
            swapped = False
            restored = False

            def mapped_at_connect(database, *args, **kwargs):
                nonlocal swapped
                if database == source_uri and not swapped:
                    command("SWAP", "SWAPPED")
                    swapped = True
                return actual_connect(database, *args, **kwargs)

            def restored_before_stage(path, flags, *args, **kwargs):
                nonlocal restored
                if (
                    isinstance(flags, int)
                    and flags & os.O_TMPFILE == os.O_TMPFILE
                    and not restored
                ):
                    command("RESTORE", "RESTORED")
                    restored = True
                return actual_open(path, flags, *args, **kwargs)

            try:
                with (
                    patch("mark_api.backup_cli.sqlite3.connect",
                          side_effect=mapped_at_connect),
                    patch("mark_api.backup_cli.os.open",
                          side_effect=restored_before_stage),
                ):
                    receipt = backup_store(self.source, backup_db=self.backup)
            except BackupError:
                # A correctly hardened implementation must fail closed.
                return

            self.assertTrue(swapped and restored)
            self.assertTrue(self.backup.is_file())
            with closing(actual_connect(self.source)) as source_db:
                source_pending = source_db.execute(
                    "SELECT COUNT(*) FROM write_api_requests "
                    "WHERE state='in_progress'"
                ).fetchone()[0]
            with closing(actual_connect(self.backup)) as copied_db:
                copy_pending = copied_db.execute(
                    "SELECT COUNT(*) FROM write_api_requests "
                    "WHERE state='in_progress'"
                ).fetchone()[0]
            self.assertEqual(source_pending, 1)
            self.assertEqual(receipt.pending_api_writes, copy_pending)
            self.assertEqual(
                copy_pending, source_pending,
                "real cross-process MAP_SHARED omitted a durable recovery fence",
            )
        finally:
            if child.poll() is None:
                try:
                    assert child.stdin is not None
                    child.stdin.write("EXIT\n")
                    child.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            assert child.stdin is not None
            assert child.stdout is not None
            assert child.stderr is not None
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()


if __name__ == "__main__":
    unittest.main()