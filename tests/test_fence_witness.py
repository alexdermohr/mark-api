"""Contract tests for the independent, not-yet-installed fence witness.

A green test does NOT mean a compromised runtime cannot forge live browser
evidence in its own active session or that the actual host is UID-isolated.
"""
from __future__ import annotations

from contextlib import closing
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import select
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from mark_api.fence_witness import (
    FenceLedger,
    FenceWitnessClient,
    FenceWitnessError,
    FenceWitnessRejected,
    FenceWitnessServer,
)


_KEY = "test-media-create-20261010"
_SHA = "a" * 64
_GOOD_MEDIA = {
    "operation_receipt": {"writer_invoked": True, "outcome": "confirmed"},
    "media_persistence_confirmed": True,
    "platform_retry_authorized": False,
}
_UNKNOWN_MEDIA = {
    "operation_receipt": {"writer_invoked": True, "outcome": "ambiguous"},
    "media_persistence_confirmed": False,
    "platform_retry_authorized": False,
}


def _bounded_child_exit(pid: int, *, timeout_seconds: float = 1.5) -> int | None:
    """Never let a fork-regression hang the test suite after a deadlock."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        completed, status = os.waitpid(pid, os.WNOHANG)
        if completed == pid:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.01)
    os.kill(pid, 9)
    os.waitpid(pid, 0)
    return None


class FenceWitnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mark-witness-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "storage"
        self.data.mkdir(mode=0o700)
        self.ledger_file = self.data / "witness.sqlite"
        self.owner = FenceLedger(self.ledger_file, initialize=True)
        self.addCleanup(self.owner.close)

    @unittest.skipUnless(hasattr(socket, "SO_PEERCRED"), "requires SO_PEERCRED")
    def test_linux_peer_uid_high_bit_is_unsigned_in_client_and_handler(self) -> None:
        high_uid = 2**31 + 7
        credentials = struct.pack("=iII", 123, high_uid, 50042)

        # Client must accept an otherwise valid UNIX peer whose Linux uid_t
        # occupies the high half of the unsigned 32-bit range.
        with patch("mark_api.fence_witness.socket.socket") as ctor:
            sock = ctor.return_value
            sock.getsockopt.return_value = credentials
            sock.makefile.return_value = io.BytesIO(
                b'{"ok":true,"version":1}\n'
            )
            client = FenceWitnessClient(
                self.root / "synthetic.sock", expected_owner_uid=high_uid,
            )
            client.close()

        # Witness server must decode the same on-wire credentials. A role
        # mismatch remains rejected independently of signedness.
        from mark_api.fence_witness import _WitnessUnixHandler
        from types import SimpleNamespace

        handler = object.__new__(_WitnessUnixHandler)
        handler.request = MagicMock()
        handler.request.getsockopt.return_value = credentials
        handler.server = SimpleNamespace(peer_uid=high_uid, role="operator")
        handler._send = MagicMock()
        handler._next_frame = MagicMock(return_value=None)
        handler.handle()
        handler._send.assert_called_once_with({"ok": True, "version": 1})

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_forked_process_does_not_retain_crashed_witness_lease(self) -> None:
        """A forked child must not pin an orphaned owner's flock indefinitely."""
        read_fd, write_fd = os.pipe()
        ready_read, ready_write = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(write_fd)
            os.close(ready_read)
            try:
                # At-fork callbacks have already run before reaching here.
                os.write(ready_write, b"R")
                os.close(ready_write)
                os.read(read_fd, 1)  # Stay alive until the parent tests the lock.
            finally:
                os._exit(0)
        os.close(read_fd)
        os.close(ready_write)
        try:
            self.assertTrue(select.select([ready_read], [], [], 1.5)[0],
                            "fork child never reached ready barrier")
            self.assertEqual(os.read(ready_read, 1), b"R")
            os.close(ready_read)
            # Emulate process death by closing, WITHOUT explicitly LOCK_UN.
            original_fd = self.owner._lock_fd
            self.owner._lock_fd = None
            assert original_fd is not None
            os.close(original_fd)
            with FenceLedger(self.ledger_file) as replacement:
                self.assertFalse(replacement.fence_pending())
        finally:
            os.write(write_fd, b"X")
            os.close(write_fd)
            _bounded_child_exit(pid)

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_fork_child_public_read_fails_before_inherited_lock(self) -> None:
        """A fork child cannot block on an RLock held by a vanished thread."""
        entered = threading.Event()
        release = threading.Event()

        def hold_parent_lock() -> None:
            with self.owner._guard:
                entered.set()
                release.wait(3)

        holder = threading.Thread(target=hold_parent_lock)
        holder.start()
        self.assertTrue(entered.wait(1))
        pid = os.fork()
        if pid == 0:
            try:
                self.owner.fence_pending()
            except FenceWitnessError as exc:
                os._exit(0 if "fork" in str(exc) else 4)
            except BaseException:
                os._exit(5)
            os._exit(6)
        code = None
        try:
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline:
                complete, status = os.waitpid(pid, os.WNOHANG)
                if complete == pid:
                    code = os.waitstatus_to_exitcode(status)
                    break
                time.sleep(0.01)
        finally:
            if code is None:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            release.set()
            holder.join(timeout=3)
        self.assertFalse(holder.is_alive())
        self.assertEqual(code, 0, "forked public read hung on orphaned RLock")

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_fork_child_close_does_not_wait_on_inherited_thread_lock(self) -> None:
        """Fork child must release its inherited FD without waiting for a dead thread."""
        entered = threading.Event()
        release = threading.Event()

        def hold_parent_lock() -> None:
            with self.owner._guard:
                entered.set()
                release.wait(3)

        holder = threading.Thread(target=hold_parent_lock)
        holder.start()
        self.assertTrue(entered.wait(1))
        pid = os.fork()
        if pid == 0:
            try:
                self.owner.close()
            except BaseException:
                os._exit(4)
            os._exit(0)
        exitcode = None
        try:
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline:
                completed, status = os.waitpid(pid, os.WNOHANG)
                if completed == pid:
                    exitcode = os.waitstatus_to_exitcode(status)
                    break
                time.sleep(0.01)
        finally:
            if exitcode is None:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            release.set()
            holder.join(timeout=3)
        self.assertFalse(holder.is_alive())
        self.assertEqual(exitcode, 0, "fork child hung on orphaned RLock")
        self.assertFalse(self.owner.fence_pending())

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_child_close_cannot_unlock_live_parent_witness(self) -> None:
        """A forked child must never unlock the parent's shared flock OFD."""
        session = self.owner.open_session()
        self.owner.execution_started(
            session, key="fork-parent-live", sha=_SHA, action="create",
        )
        pid = os.fork()
        if pid == 0:
            try:
                try:
                    self.owner.fence_pending()
                except FenceWitnessError as exc:
                    if "inherited by fork" not in str(exc):
                        os._exit(5)
                else:
                    os._exit(6)
                self.owner.close()  # inherited descriptor, NOT the owner PID
            except BaseException:
                os._exit(3)
            os._exit(0)
        self.assertEqual(_bounded_child_exit(pid), 0,
                         "forked child did not exit within the deadline")
        try:
            second = FenceLedger(self.ledger_file)
        except FenceWitnessError as exc:
            self.assertIn("another witness owner", str(exc))
        else:
            second.close()
            self.fail("fork child released the live parent's ledger lease")
        self.assertTrue(self.owner.fence_pending())

    def test_real_kernel_lock_denial_does_not_publish_ledger(self) -> None:
        """Another open file description must hold first-init ownership."""
        fresh = self.data / "kernel-lock-denied.sqlite"
        lock_path = fresh.with_name(fresh.name + ".lock")
        lock_fd = os.open(
            lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600,
        )
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(FenceWitnessError, "another witness owner"):
                FenceLedger(fresh, initialize=True)
            self.assertFalse(fresh.exists(), "failed initial owner published a ledger")
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def test_denied_first_ledger_lock_creates_no_partial_database(self) -> None:
        """Initialization must acquire exclusive ownership before O_EXCL."""
        fresh = self.data / "first-init-blocked.sqlite"
        with patch.object(
            FenceLedger, "_acquire_lock",
            side_effect=FenceWitnessError("injected first-init lease denial"),
        ):
            with self.assertRaisesRegex(FenceWitnessError, "lease denial"):
                FenceLedger(fresh, initialize=True)
        self.assertFalse(fresh.exists(), "ownerless partial ledger was published")

    def test_second_owner_cannot_seal_live_session_or_clear_active_fence(self) -> None:
        """A second witness process must fail before touching active sessions."""
        session = self.owner.open_session()
        self.owner.execution_started(
            session, key="second-owner-fence", sha=_SHA, action="create_media",
        )
        child_code = """
import sys
from pathlib import Path
from mark_api.fence_witness import FenceLedger, FenceWitnessError
try:
    other = FenceLedger(Path(sys.argv[1]))
except FenceWitnessError:
    print("SECOND_OWNER_DENIED")
    raise SystemExit(0)
else:
    print("SECOND_OWNER_OPENED_AND_SEALED")
    raise SystemExit(2)
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", child_code, str(self.ledger_file)],
            capture_output=True, text=True, check=False, timeout=8,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("SECOND_OWNER_DENIED", result.stdout)
        with self.assertRaisesRegex(FenceWitnessError, "sealed"):
            self.owner.operator_clearance(
                key="second-owner-fence", sha=_SHA,
                evidence="operator:premature-clear",
            )
        self.assertEqual(
            self.owner.execution_completed(
                session, key="second-owner-fence", sha=_SHA,
                response=_UNKNOWN_MEDIA,
            ),
            "ambiguous",
        )
        self.assertTrue(self.owner.fence_pending())

    def test_acknowledged_ledger_commits_use_delete_journal_extra_sync(self) -> None:
        # A confirmed execution_started ACK must not be based on SQLite
        # FULL+DELETE, where the journal unlink itself lacks directory fsync.
        with closing(self.owner._connect()) as connection:
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone()[0],
                "delete",
            )
            self.assertEqual(
                connection.execute("PRAGMA synchronous").fetchone()[0],
                3,  # EXTRA: includes rollback-journal directory sync
            )
        session = self.owner.open_session()
        self.owner.execution_started(
            session, key=_KEY, sha=_SHA, action="create_media",
        )
        self.owner.seal_session(session)
        self.owner.close()
        with FenceLedger(self.ledger_file) as reopened:
            self.assertTrue(reopened.fence_pending())

    def test_never_initializes_missing_or_corrupt_previous_ledger_implicitly(self) -> None:
        absent = self.data / "missing.sqlite"
        with self.assertRaises(FenceWitnessError):
            FenceLedger(absent)
        self.assertFalse(absent.exists())
        self.assertFalse(absent.with_name(absent.name + ".lock").exists())
        self.assertTrue(self.ledger_file.exists())
        self.owner.close()
        with closing(sqlite3.connect(self.ledger_file)) as db:
            db.execute("DROP TABLE events")
            db.commit()
        with self.assertRaisesRegex(FenceWitnessError, "schema"):
            FenceLedger(self.ledger_file, initialize=True)
        with closing(sqlite3.connect(self.ledger_file)) as db:
            self.assertNotIn(
                "events",
                {row[0] for row in db.execute("SELECT name FROM sqlite_master")},
            )

    def test_file_and_parent_identity_are_private_and_non_aliasable(self) -> None:
        self.assertEqual(stat.S_IMODE(self.ledger_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.data.stat().st_mode), 0o700)
        link = self.root / "linked-storage"
        link.symlink_to(self.data, target_is_directory=True)
        with self.assertRaisesRegex(FenceWitnessError, "private directory"):
            FenceLedger(link / "witness.sqlite")
        other = self.root / "other.sqlite"
        other.hardlink_to(self.ledger_file)
        with self.assertRaisesRegex(FenceWitnessError, "identity"):
            FenceLedger(self.ledger_file)
        other.unlink()
        # Even if the immediate ledger directory stays 0700, a group
        # writable ancestor lets another account replace its name.
        unsafe_parent = self.root / "group-writable-parent"
        unsafe_parent.mkdir(mode=0o700)
        # CI/operator umask may be 077; set actual inode permissions.
        unsafe_parent.chmod(0o770)
        unsafe_child = unsafe_parent / "private"
        unsafe_child.mkdir(mode=0o700)
        with self.assertRaisesRegex(FenceWitnessError, "writable parent"):
            FenceLedger(unsafe_child / "must-not-be-created.sqlite", initialize=True)
        self.assertFalse(self.owner.fence_pending())

    def test_runtime_cannot_clear_ambiguous_non_media_write(self) -> None:
        """A compromised runtime must not self-release its other write types."""
        session = self.owner.open_session()
        self.owner.execution_started(
            session, key="ambiguous-pause-no-self-clear", sha=_SHA, action="pause",
        )
        self.assertEqual(self.owner.execution_completed(
            session, key="ambiguous-pause-no-self-clear", sha=_SHA,
            response={"operation_receipt": {
                "writer_invoked": True, "outcome": "ambiguous",
            }},
        ), "ambiguous")
        with self.assertRaisesRegex(FenceWitnessError, "media"):
            self.owner.runtime_clearance(
                session, key="ambiguous-pause-no-self-clear", sha=_SHA,
                evidence="self-claimed-runtime-observation",
            )
        self.assertTrue(self.owner.fence_pending())
        self.assertFalse(self.owner.clearance_recorded(
            key="ambiguous-pause-no-self-clear", sha=_SHA,
        ))

    def test_pending_media_cannot_self_clear_even_after_in_process_postread(self) -> None:
        session = self.owner.open_session()
        self.owner.execution_started(session, key=_KEY, sha=_SHA, action="create_media")
        self.assertTrue(self.owner.fence_pending())
        with self.assertRaisesRegex(FenceWitnessError, "independent proof"):
            self.owner.runtime_clearance(
                session, key=_KEY, sha=_SHA, evidence="runtime:claimed-postread",
            )
        self.assertEqual(self.owner.execution_completed(
            session, key=_KEY, sha=_SHA, response=_UNKNOWN_MEDIA,
        ), "ambiguous")
        with self.assertRaisesRegex(FenceWitnessError, "independent proof"):
            self.owner.runtime_clearance(
                session, key=_KEY, sha=_SHA, evidence="runtime:forged-postread",
            )
        self.assertFalse(self.owner.clearance_recorded(key=_KEY, sha=_SHA))
        self.assertTrue(self.owner.fence_pending())
        self.owner.seal_session(session)
        self.owner.close()
        reopened = FenceLedger(self.ledger_file)
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.fence_pending())
        new_session = reopened.open_session()
        with self.assertRaisesRegex(FenceWitnessError, "earlier write"):
            reopened.execution_started(
                new_session, key="new-without-recovery", sha=_SHA, action="create",
            )
        # The operator socket is a separate privileged authority, not
        # independent platform success evidence or a general executor RPC.
        reopened.operator_clearance(
            key=_KEY, sha=_SHA, evidence="operator:synthetic-independent-review",
        )
        self.assertFalse(reopened.fence_pending())
        with self.assertRaisesRegex(FenceWitnessError, "already"):
            reopened.execution_started(
                new_session, key=_KEY, sha=_SHA, action="create_media",
            )

    def test_sealed_crash_session_cannot_confirm_or_clear_after_restart(self) -> None:
        old_session = self.owner.open_session()
        self.owner.execution_started(
            old_session, key="old-in-progress", sha=_SHA, action="create_media",
        )
        with self.assertRaisesRegex(FenceWitnessError, "sealed"):
            self.owner.operator_clearance(
                key="old-in-progress", sha=_SHA,
                evidence="premature:operator-readback",
            )
        self.assertTrue(self.owner.fence_pending())
        self.owner.close()  # simulate process exit; the OS releases its flock
        reopened = FenceLedger(self.ledger_file)
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.fence_pending())
        with self.assertRaisesRegex(FenceWitnessError, "not active"):
            reopened.execution_completed(
                old_session, key="old-in-progress", sha=_SHA,
                response=_GOOD_MEDIA,
            )
        with self.assertRaisesRegex(FenceWitnessError, "independent proof"):
            reopened.runtime_clearance(
                old_session, key="old-in-progress", sha=_SHA,
                evidence="stale-browser:could-be-forged",
            )
        new_session = reopened.open_session()
        with self.assertRaisesRegex(FenceWitnessError, "earlier write"):
            reopened.execution_started(
                new_session, key="different-idempotency", sha="b" * 64,
                action="create",
            )
        reopened.operator_clearance(
            key="old-in-progress", sha=_SHA,
            evidence="operator:independent-external-verification",
        )
        self.assertFalse(reopened.fence_pending())
        reopened.execution_started(
            new_session, key="new-idempotency", sha="b" * 64,
            action="create_media",
        )
        self.assertTrue(reopened.fence_pending())

    def test_replay_status_records_exact_executor_claim_but_never_success(self) -> None:
        session = self.owner.open_session()
        response = {"operation_receipt": {
            "writer_invoked": True, "outcome": "confirmed",
        }}
        response_sha = hashlib.sha256(json.dumps(
            response, sort_keys=True, ensure_ascii=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        self.assertEqual(self.owner.replay_status(
            key="never-started", sha=_SHA, response_sha=response_sha,
        ), "never_started")
        self.owner.execution_started(
            session, key="executor-claimed-completion", sha=_SHA, action="pause",
        )
        self.assertEqual(self.owner.replay_status(
            key="executor-claimed-completion", sha=_SHA,
            response_sha=response_sha,
        ), "unverified")
        self.assertEqual(self.owner.execution_completed(
            session, key="executor-claimed-completion", sha=_SHA,
            response=response,
        ), "ambiguous")
        self.assertEqual(self.owner.replay_status(
            key="executor-claimed-completion", sha=_SHA,
            response_sha=response_sha,
        ), "ambiguous")
        self.assertEqual(self.owner.replay_status(
            key="executor-claimed-completion", sha=_SHA,
            response_sha="b" * 64,
        ), "unverified")
        self.assertTrue(self.owner.fence_pending())
        self.owner.seal_session(session)
        self.owner.operator_clearance(
            key="executor-claimed-completion", sha=_SHA,
            evidence="operator:synthetic-outcome-review",
        )
        self.assertFalse(self.owner.fence_pending())
        # An operator's later clearance never upgrades an old claimed 200.
        self.assertEqual(self.owner.replay_status(
            key="executor-claimed-completion", sha=_SHA,
            response_sha=response_sha,
        ), "ambiguous")
        new_session = self.owner.open_session()
        self.owner.execution_started(
            new_session, key="cleared-no-completion", sha=_SHA, action="pause",
        )
        self.owner.seal_session(new_session)
        self.owner.operator_clearance(
            key="cleared-no-completion", sha=_SHA,
            evidence="operator:synthetic-negative-postread",
        )
        self.assertFalse(self.owner.fence_pending())
        self.assertEqual(self.owner.replay_status(
            key="cleared-no-completion", sha=_SHA,
            response_sha=response_sha,
        ), "unverified")

    def test_runtime_media_clearance_is_not_independent_even_when_exact(self) -> None:
        session = self.owner.open_session()
        key = "media-ambiguous-clearance-readback"
        self.owner.execution_started(session, key=key, sha=_SHA, action="create_media")
        self.assertEqual(self.owner.execution_completed(
            session, key=key, sha=_SHA, response=_UNKNOWN_MEDIA,
        ), "ambiguous")
        for evidence in ("synthetic:verified-media-settlement", "runtime:forged"):
            with self.subTest(evidence=evidence), self.assertRaisesRegex(
                FenceWitnessError, "independent proof",
            ):
                self.owner.runtime_clearance(
                    session, key=key, sha=_SHA, evidence=evidence,
                )
        self.assertFalse(self.owner.clearance_recorded(key=key, sha=_SHA))
        self.assertFalse(self.owner.clearance_recorded(key=key, sha="f" * 64))
        self.assertTrue(self.owner.fence_pending())
        self.owner.seal_session(session)
        self.owner.operator_clearance(
            key=key, sha=_SHA, evidence="operator:synthetic-external-review",
        )
        self.assertTrue(self.owner.clearance_recorded(key=key, sha=_SHA))
        self.assertFalse(self.owner.fence_pending())

    def test_same_key_never_reexecutes_after_unverified_claim_or_restart(self) -> None:
        session = self.owner.open_session()
        self.owner.execution_started(session, key="confirmed-key", sha=_SHA, action="create")
        self.assertEqual(self.owner.execution_completed(
            session, key="confirmed-key", sha=_SHA,
            response={"operation_receipt": {
                "writer_invoked": True, "outcome": "confirmed",
            }},
        ), "ambiguous")
        self.assertTrue(self.owner.fence_pending())
        for sha in (_SHA, "b" * 64):
            with self.assertRaisesRegex(FenceWitnessError, "already"):
                self.owner.execution_started(
                    session, key="confirmed-key", sha=sha, action="create",
                )
        self.owner.seal_session(session)
        self.owner.close()
        reopened = FenceLedger(self.ledger_file)
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.fence_pending())
        next_session = reopened.open_session()
        with self.assertRaisesRegex(FenceWitnessError, "already"):
            reopened.execution_started(
                next_session, key="confirmed-key", sha=_SHA, action="create",
            )
        with self.assertRaisesRegex(FenceWitnessError, "earlier write"):
            reopened.execution_started(
                next_session, key="different-key", sha=_SHA, action="create",
            )

    def test_all_executor_confirmations_stay_ambiguous_even_with_media_flags(self) -> None:
        session = self.owner.open_session()
        responses = (
            {"operation_receipt": {"writer_invoked": True, "outcome": "confirmed"},
             "media_persistence_confirmed": False},
            {"operation_receipt": {"writer_invoked": False, "outcome": "confirmed"},
             "media_persistence_confirmed": True},
            {"operation_receipt": {"writer_invoked": "true", "outcome": "confirmed"},
             "media_persistence_confirmed": True},
            {"operation_receipt": {"writer_invoked": False, "outcome": "precondition_failed"},
             "media_persistence_confirmed": True},
        )
        for number, response in enumerate(responses):
            key = f"untrusted-response-{number}"
            self.owner.execution_started(
                session, key=key, sha=_SHA, action="create_media",
            )
            self.assertEqual(self.owner.execution_completed(
                session, key=key, sha=_SHA, response=response,
            ), "ambiguous")
            self.assertTrue(self.owner.fence_pending())
            with self.assertRaisesRegex(FenceWitnessError, "independent proof"):
                self.owner.runtime_clearance(
                    session, key=key, sha=_SHA,
                    evidence=f"runtime:forged-{number}",
                )
            self.owner.seal_session(session)
            self.owner.operator_clearance(
                key=key, sha=_SHA,
                evidence=f"operator:synthetic-clearance-{number}",
            )
            session = self.owner.open_session()
        key = "media-looks-fully-confirmed"
        self.owner.execution_started(
            session, key=key, sha=_SHA, action="create_media",
        )
        self.assertEqual(self.owner.execution_completed(
            session, key=key, sha=_SHA, response=_GOOD_MEDIA,
        ), "ambiguous")
        self.assertTrue(self.owner.fence_pending())
        self.assertFalse(self.owner.clearance_recorded(key=key, sha=_SHA))

    def test_historical_chained_executor_confirmed_and_runtime_cleared_stay_pending(self) -> None:
        # Valid legacy events must not be grandfathered into independent
        # confirmations on a new witness binary or restored ledger.
        session = self.owner.open_session()
        key = "legacy-runtime-claimed-success"
        self.owner.execution_started(session, key=key, sha=_SHA, action="pause")
        with self.owner._guard, closing(self.owner._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self.owner._append(
                db, session=session, kind="completed", key=key,
                sha=_SHA, outcome="confirmed", response_sha="b" * 64,
            )
            self.owner._append(
                db, session=session, kind="runtime_cleared", key=key,
                sha=_SHA, evidence="old:runtime-self-reported-postread",
            )
            db.commit()
        self.assertTrue(self.owner.fence_pending())
        self.assertFalse(self.owner.clearance_recorded(key=key, sha=_SHA))
        self.assertEqual(self.owner.replay_status(
            key=key, sha=_SHA, response_sha="b" * 64,
        ), "ambiguous")
        self.owner.seal_session(session)
        self.owner.close()
        with FenceLedger(self.ledger_file) as reopened:
            self.assertTrue(reopened.fence_pending())
            self.assertEqual(reopened.replay_status(
                key=key, sha=_SHA, response_sha="b" * 64,
            ), "ambiguous")
            next_session = reopened.open_session()
            with self.assertRaisesRegex(FenceWitnessError, "earlier write"):
                reopened.execution_started(
                    next_session, key="new-after-legacy", sha=_SHA,
                    action="create",
                )

    def test_v1_runtime_clearance_requires_explicit_migration_then_operator_override(self) -> None:
        """A valid legacy clearance cannot permanently block operator recovery."""
        session = self.owner.open_session()
        key = "v1-legacy-claimed-success"
        self.owner.execution_started(session, key=key, sha=_SHA, action="create")
        with self.owner._guard, closing(self.owner._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self.owner._append(
                db, session=session, kind="completed", key=key, sha=_SHA,
                outcome="confirmed", response_sha="b" * 64,
            )
            self.owner._append(
                db, session=session, kind="runtime_cleared", key=key,
                sha=_SHA, evidence="legacy:executor-postread",
            )
            db.commit()
        self.owner.seal_session(session)
        self.owner.close()

        # Synthetic historical schema: original unique index and version 1.
        # This fixture simulates old bytes, never a production migration.
        with closing(sqlite3.connect(self.ledger_file)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DROP INDEX one_runtime_clearance_per_key")
            db.execute("DROP INDEX one_operator_clearance_per_key")
            db.execute("CREATE UNIQUE INDEX one_clearance_per_key "
                       "ON events(request_key) "
                       "WHERE kind IN ('runtime_cleared', 'operator_cleared')")
            db.execute("PRAGMA user_version=1")
            db.commit()
            before = db.execute(
                "SELECT seq,kind,request_key,session_id,request_sha256,"
                "previous_sha256,row_sha256 FROM events ORDER BY seq"
            ).fetchall()

        with self.assertRaisesRegex(FenceWitnessError, "explicit.*migration"):
            FenceLedger(self.ledger_file)
        with FenceLedger(self.ledger_file, migrate_legacy=True) as restored:
            self.assertTrue(restored.fence_pending())
            self.assertFalse(restored.clearance_recorded(key=key, sha=_SHA))
            self.assertEqual(restored.replay_status(
                key=key, sha=_SHA, response_sha="b" * 64,
            ), "ambiguous")
            with self.assertRaisesRegex(FenceWitnessError, "operation-bound"):
                restored.operator_clearance(
                    key=key, sha="c" * 64, evidence="operator:incorrect-request",
                )
            restored.operator_clearance(
                key=key, sha=_SHA, evidence="operator:independently-reviewed",
            )
            self.assertFalse(restored.fence_pending())
            self.assertTrue(restored.clearance_recorded(key=key, sha=_SHA))
            with self.assertRaisesRegex(FenceWitnessError, "already cleared"):
                restored.operator_clearance(
                    key=key, sha=_SHA, evidence="operator:duplicate",
                )
            next_session = restored.open_session()
            with self.assertRaisesRegex(FenceWitnessError, "already"):
                restored.execution_started(
                    next_session, key=key, sha=_SHA, action="create",
                )
            restored.seal_session(next_session)
        with closing(sqlite3.connect(self.ledger_file)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            after = db.execute(
                "SELECT seq,kind,request_key,session_id,request_sha256,"
                "previous_sha256,row_sha256 FROM events ORDER BY seq"
            ).fetchall()
            self.assertEqual(after[:len(before)], before)
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM events WHERE request_key=? "
                    "AND kind='runtime_cleared'", (key,),
                ).fetchone()[0], 1,
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM events WHERE request_key=? "
                    "AND kind='operator_cleared'", (key,),
                ).fetchone()[0], 1,
            )
        with FenceLedger(self.ledger_file) as reopened:
            self.assertFalse(reopened.fence_pending())

    def test_operator_clearance_pending_query_has_index_plan_at_scale(self) -> None:
        """High-cardinality finished ledgers cannot scan every clearance."""
        session = self.owner.open_session()
        self.owner.seal_session(session)
        with self.owner._guard, closing(self.owner._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            for number in range(5000):
                key = f"bulk-reviewed-operation-{number}"
                self.owner._append(
                    db, session=session, kind="started", key=key,
                    sha=_SHA, action="pause",
                )
                self.owner._append(
                    db, session=session, kind="operator_cleared", key=key,
                    sha=_SHA, evidence="operator:synthetic-bulk-proof",
                )
            db.commit()
            sql: list[str] = []
            db.set_trace_callback(sql.append)
            try:
                self.assertFalse(FenceLedger._is_pending(db))
            finally:
                db.set_trace_callback(None)
            pending_sql = next(
                item for item in sql if "SELECT 1 FROM events AS started" in item
            )
            plan = db.execute("EXPLAIN QUERY PLAN " + pending_sql).fetchall()
            self.assertTrue(
                any("SEARCH cleared USING INDEX one_operator_clearance_per_key"
                    in detail for _, _, _, detail in plan),
                plan,
            )
            started = time.monotonic()
            for _ in range(5):
                self.assertFalse(FenceLedger._is_pending(db))
            self.assertLess(
                time.monotonic() - started, 2.0,
                "pending lookups must not scale quadratically with clearances",
            )

    def test_operator_pending_page_covers_legacy_claims_and_bounded_pages(self) -> None:
        """One root-only review queue must not infer success from runtime events."""
        session = self.owner.open_session()
        # A v1 ledger could retain multiple historically 'completed' starts.
        # Construct their hash-valid event chain without external operations.
        with self.owner._guard, closing(self.owner._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            for number in range(18):
                key = f"legacy-op-{number:02d}"
                self.owner._append(
                    db, session=session, kind="started", key=key,
                    sha=_SHA, action="create_media",
                )
                self.owner._append(
                    db, session=session, kind="completed", key=key,
                    sha=_SHA, outcome="confirmed", response_sha="d" * 64,
                )
                self.owner._append(
                    db, session=session, kind="runtime_cleared", key=key,
                    sha=_SHA, evidence="legacy:untrusted-executor",
                )
            db.commit()
        active = self.owner.pending_page(after_seq=0)
        self.assertEqual(set(active), {"rows", "more", "next_after_seq"})
        self.assertEqual(len(active["rows"]), 16)
        self.assertTrue(active["more"])
        self.assertTrue(all(not row["session_sealed"] for row in active["rows"]))
        self.assertEqual(
            [row["key"] for row in active["rows"]],
            [f"legacy-op-{number:02d}" for number in range(16)],
        )
        self.assertTrue(all(row["sha"] == _SHA for row in active["rows"]))
        self.assertTrue(all(row["action"] == "create_media" for row in active["rows"]))
        self.assertTrue(all(row["session"] == session for row in active["rows"]))
        self.assertTrue(all(row["started_at"] for row in active["rows"]))
        last = self.owner.pending_page(after_seq=active["next_after_seq"])
        self.assertFalse(last["more"])
        self.assertEqual([row["key"] for row in last["rows"]],
                         ["legacy-op-16", "legacy-op-17"])
        self.assertGreater(last["next_after_seq"], active["next_after_seq"])
        with self.assertRaisesRegex(FenceWitnessError, "sealed"):
            self.owner.operator_clearance(
                key="legacy-op-00", sha=_SHA, evidence="operator:real-review-required",
            )
        self.owner.seal_session(session)
        sealed = self.owner.pending_page(after_seq=0)
        self.assertTrue(all(row["session_sealed"] for row in sealed["rows"]))
        self.owner.operator_clearance(
            key="legacy-op-00", sha=_SHA, evidence="operator:synthetic-postread",
        )
        self.assertNotIn("legacy-op-00", [
            row["key"] for row in self.owner.pending_page(after_seq=0)["rows"]
        ])
        self.assertTrue(self.owner.fence_pending())
        for cursor in (True, -1, 2 ** 63, "0", None):
            with self.subTest(cursor=cursor), self.assertRaises(FenceWitnessError):
                self.owner.pending_page(after_seq=cursor)
        self.owner.close()
        with FenceLedger(self.ledger_file) as restarted:
            all_keys: list[str] = []
            cursor = 0
            while True:
                page = restarted.pending_page(after_seq=cursor)
                all_keys.extend(row["key"] for row in page["rows"])
                if not page["more"]:
                    break
                self.assertGreater(page["next_after_seq"], cursor)
                cursor = page["next_after_seq"]
            self.assertEqual(len(all_keys), 17)
            self.assertNotIn("legacy-op-00", all_keys)
            self.assertEqual(len(all_keys), len(set(all_keys)))

    def test_operator_pending_page_rejects_malformed_actions_and_closes(self) -> None:
        """JSON lists/maps cannot bypass malformed-reply poisoning via TypeError."""
        for invalid_action in ([], {}, 7, None, True):
            with self.subTest(action=invalid_action):
                # The server peer is authenticated on a real socket in another
                # test; this isolated seam injects a malicious reply AFTER the
                # IPC parser so strict client validation is independently tested.
                client = object.__new__(FenceWitnessClient)
                payload = {
                    "rows": [{
                        "seq": 1, "key": "synthetic-pending-op",
                        "sha": _SHA, "action": invalid_action,
                        "session": "0" * 32,
                        "started_at": "2026-10-13T10:00:00+00:00",
                        "session_sealed": False,
                    }],
                    "more": False, "next_after_seq": 1,
                }
                with patch.object(client, "_call", return_value=payload):
                    with patch.object(client, "close") as closed:
                        with self.assertRaisesRegex(
                            FenceWitnessError, "pending page is invalid",
                        ):
                            client.pending_page(after_seq=0)
                        closed.assert_called_once_with()

    def test_operator_pending_page_only_on_operator_socket_and_readonly(self) -> None:
        runtime_dir = self.root / "page-runtime-socket"
        operator_dir = self.root / "page-operator-socket"
        runtime_dir.mkdir(mode=0o700)
        runtime_dir.chmod(0o2710)
        operator_dir.mkdir(mode=0o700)
        runtime_path = runtime_dir / "witness.sock"
        operator_path = operator_dir / "operator.sock"
        server = FenceWitnessServer(
            ledger=self.owner, runtime_socket=runtime_path,
            operator_socket=operator_path,
            runtime_uid=os.geteuid() + 100000,
            runtime_gid=os.getegid(), operator_uid=os.geteuid(),
        )
        # Same-UID functional seam; real distinct UID kernel denial is
        # independently tested in the installed-image Multi-UID suite.
        server._runtime.peer_uid = os.geteuid()
        server.start()
        self.addCleanup(server.close)
        sealed = threading.Event()
        prior_seal = self.owner.seal_session

        def signal_seal(session: str) -> None:
            prior_seal(session)
            sealed.set()

        with patch.object(self.owner, "seal_session", side_effect=signal_seal):
            with FenceWitnessClient(
                runtime_path, expected_owner_uid=os.geteuid(),
            ) as runtime:
                runtime.started(key="pending-socket-op", sha=_SHA, action="pause")
                with self.assertRaises(FenceWitnessError):
                    runtime.pending_page(after_seq=0)
            self.assertTrue(sealed.wait(3), "runtime socket did not seal session")
        with closing(sqlite3.connect(self.ledger_file)) as db:
            count_before = db.execute("SELECT count(*) FROM events").fetchone()[0]
        with FenceWitnessClient(
            operator_path, expected_owner_uid=os.geteuid(),
        ) as operator:
            page = operator.pending_page(after_seq=0)
            self.assertEqual(len(page["rows"]), 1)
            self.assertEqual(page["rows"][0]["key"], "pending-socket-op")
            self.assertTrue(page["rows"][0]["session_sealed"])
            self.assertFalse(page["more"])
        with closing(sqlite3.connect(self.ledger_file)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM events").fetchone()[0],
                count_before,
            )
        self.assertTrue(self.owner.fence_pending())
        with FenceWitnessClient(
            operator_path, expected_owner_uid=os.geteuid(),
        ) as operator:
            with self.assertRaises(FenceWitnessError):
                operator.pending_page(after_seq=-1)
        with FenceWitnessClient(
            operator_path, expected_owner_uid=os.geteuid(),
        ) as operator:
            operator.operator_clear(
                key="pending-socket-op", sha=_SHA,
                evidence="operator:synthetic-independent-postread",
            )
        self.assertFalse(self.owner.fence_pending())

    def test_rejects_unbound_evidence_and_invalid_request_fields(self) -> None:
        session = self.owner.open_session()
        for key, sha, action in (
            ("", _SHA, "create"), ("test", "A" * 64, "create"),
            ("test", _SHA, "raw_sql"), ("test\nheader", _SHA, "create"),
        ):
            with self.subTest(key=key, action=action):
                with self.assertRaises(FenceWitnessError):
                    self.owner.execution_started(
                        session, key=key, sha=sha, action=action,
                    )
        self.owner.execution_started(
            session, key="exact-only", sha=_SHA, action="create",
        )
        for evidence in ("", "not\nindependent", "x" * 257):
            with self.subTest(evidence=evidence):
                with self.assertRaises(FenceWitnessError):
                    self.owner.operator_clearance(
                        key="exact-only", sha=_SHA, evidence=evidence,
                    )
        with self.assertRaises(FenceWitnessError):
            self.owner.operator_clearance(
                key="unknown-key", sha=_SHA, evidence="operator:readback",
            )
        self.assertTrue(self.owner.fence_pending())

    def test_sqlite_ledger_entries_are_append_only_and_reject_chain_tampering(self) -> None:
        session = self.owner.open_session()
        self.owner.execution_started(
            session, key=_KEY, sha=_SHA, action="create_media",
        )
        with closing(sqlite3.connect(self.ledger_file)) as db:
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("DELETE FROM events")
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("UPDATE events SET request_key='forged'")
            db.rollback()
        self.owner.seal_session(session)
        self.owner.close()
        with FenceLedger(self.ledger_file) as reopened:
            self.assertTrue(reopened.fence_pending())
        with closing(sqlite3.connect(self.ledger_file)) as db:
            db.execute("DROP TRIGGER events_no_update")
            db.execute("UPDATE events SET request_key='forged' WHERE kind='started'")
            db.commit()
        with self.assertRaisesRegex(FenceWitnessError, "schema|chain"):
            FenceLedger(self.ledger_file)

    def test_separate_role_scoped_unix_sockets_use_kernel_peer_credentials(self) -> None:
        # Unit tests run under one UID and cannot impersonate an independent
        # UID. The runtime socket is deliberately configured for a *different*
        # UID, so this actual connection must fail via SO_PEERCRED.
        runtime_dir = self.root / "runtime-socket"
        operator_dir = self.root / "operator-socket"
        runtime_dir.mkdir()
        operator_dir.mkdir(mode=0o700)
        runtime_dir.chmod(0o2710)
        runtime_path = runtime_dir / "witness.sock"
        operator_path = operator_dir / "operator.sock"
        server = FenceWitnessServer(
            ledger=self.owner, runtime_socket=runtime_path,
            operator_socket=operator_path, runtime_uid=os.geteuid() + 100000,
            runtime_gid=os.getegid(), operator_uid=os.geteuid(),
        )
        self.addCleanup(server.close)
        server.start()
        self.assertEqual(stat.S_IMODE(runtime_path.lstat().st_mode), 0o660)
        self.assertEqual(stat.S_IMODE(operator_path.lstat().st_mode), 0o600)

        def call(path: Path, command: bytes | None) -> dict[str, object]:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(3)
                peer.connect(str(path))
                with peer.makefile("rwb", buffering=0) as stream:
                    first = json.loads(stream.readline())
                    if command is None or not first.get("ok"):
                        return first
                    stream.write(command + b"\n")
                    return json.loads(stream.readline())

        self.assertEqual(
            call(runtime_path, None),
            {"ok": False, "error": "request_denied"},
        )
        self.assertEqual(
            call(operator_path, b'{"version":1,"op":"pending"}'),
            {"ok": True, "result": {"pending": False}},
        )
        for invalid in (
            b'{"version":1,"op":"raw_sql","sql":"DROP TABLE events"}',
            b'{"version":1,"op":"operator_clear","key":"arbitrary"}',
            b'{"version":1,"op":"pending","op":"operator_clear"}',
            b'{"version":1,"op":"pending","extra":"unsafe"}',
        ):
            with self.subTest(invalid=invalid):
                self.assertEqual(
                    call(operator_path, invalid),
                    {"ok": False, "error": "request_denied"},
                )
        self.assertFalse(self.owner.fence_pending())
        with self.assertRaisesRegex(FenceWitnessError, "peer UID"):
            FenceWitnessClient(
                operator_path, expected_owner_uid=os.geteuid() + 100000,
            )
        with self.assertRaisesRegex(FenceWitnessError, "handshake"):
            FenceWitnessClient(runtime_path, expected_owner_uid=os.geteuid())

        # Operator traffic gets its own kernel-authenticated socket. Runtime
        # cannot request an operator clearance merely by naming an operation.
        session = self.owner.open_session()
        self.owner.execution_started(
            session, key="old-fence-to-clear", sha=_SHA, action="create_media",
        )
        self.owner.seal_session(session)
        with FenceWitnessClient(
            operator_path, expected_owner_uid=os.geteuid(),
        ) as client:
            self.assertTrue(client.pending())
            # One request per operator socket; a disconnected client never
            # transparently reopens a session or retries an unknown write.
            with self.assertRaises(FenceWitnessError):
                client.operator_clear(
                    key="old-fence-to-clear", sha=_SHA,
                    evidence="operator:verified-external-postread",
                )
        self.assertTrue(self.owner.fence_pending())
        with FenceWitnessClient(
            operator_path, expected_owner_uid=os.geteuid(),
        ) as client:
            client.operator_clear(
                key="old-fence-to-clear", sha=_SHA,
                evidence="operator:verified-external-postread",
            )
        self.assertFalse(self.owner.fence_pending())
        server.close()
        self.assertFalse(runtime_path.exists())
        self.assertFalse(operator_path.exists())


    def test_seal_base_exception_marks_witness_fatal(self) -> None:
        """A non-sqlite BaseException during sealing must poison serve mode."""
        runtime_dir = self.root / "fatal-runtime"
        operator_dir = self.root / "fatal-operator"
        runtime_dir.mkdir()
        runtime_dir.chmod(0o2710)
        operator_dir.mkdir(mode=0o700)
        server = FenceWitnessServer(
            ledger=self.owner,
            runtime_socket=runtime_dir / "witness.sock",
            operator_socket=operator_dir / "operator.sock",
            runtime_uid=os.geteuid() + 100000,
            runtime_gid=os.getegid(),
            operator_uid=os.geteuid(),
        )
        # A temporary same-UID test client, not a production UID exemption.
        server._runtime.peer_uid = os.geteuid()
        server.start()
        try:
            with patch.object(self.owner, "seal_session", side_effect=SystemExit(57)):
                client = FenceWitnessClient(
                    runtime_dir / "witness.sock", expected_owner_uid=os.geteuid(),
                )
                client.close()
                self.assertTrue(
                    server._runtime._fatal.wait(1.5),
                    "unsealed session left witness serving after BaseException",
                )
                with self.assertRaisesRegex(FenceWitnessError, "one live runtime session"):
                    self.owner.open_session()
        finally:
            with self.assertRaisesRegex(FenceWitnessError, "shutdown could not seal"):
                server.close()

    def test_definite_pending_rejection_keeps_readback_but_no_runtime_clear(self) -> None:
        runtime_dir = self.root / "known-rejection-runtime"
        operator_dir = self.root / "known-rejection-operator"
        runtime_dir.mkdir(mode=0o700)
        runtime_dir.chmod(0o2710)
        operator_dir.mkdir(mode=0o700)
        server = FenceWitnessServer(
            ledger=self.owner,
            runtime_socket=runtime_dir / "runtime.sock",
            operator_socket=operator_dir / "operator.sock",
            runtime_uid=os.geteuid() + 100000,
            runtime_gid=os.getegid(), operator_uid=os.geteuid(),
        )
        self.addCleanup(server.close)
        server._runtime.peer_uid = os.geteuid()  # Test-only same-UID seam.
        server.start()
        with FenceWitnessClient(
            runtime_dir / "runtime.sock", expected_owner_uid=os.geteuid(),
        ) as client:
            client.started(key="live-ambiguous", sha=_SHA, action="create_media")
            self.assertEqual(client.completed(
                key="live-ambiguous", sha=_SHA, response=_UNKNOWN_MEDIA,
            ), "ambiguous")
            with self.assertRaises(FenceWitnessRejected):
                client.started(key="known-refused-key", sha=_SHA, action="create")
            with self.assertRaisesRegex(FenceWitnessError, "already recorded"):
                client.started(key="live-ambiguous", sha=_SHA, action="create_media")
            self.assertTrue(client.pending())
            encoded = json.dumps(
                _UNKNOWN_MEDIA, sort_keys=True, ensure_ascii=True,
                separators=(",", ":"), allow_nan=False,
            )
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            self.assertEqual(client.replay_status(
                key="live-ambiguous", sha=_SHA, response_sha=digest,
            ), "ambiguous")
            self.assertEqual(client.replay_status(
                key="known-refused-key", sha=_SHA, response_sha=digest,
            ), "never_started")
            with self.assertRaisesRegex(FenceWitnessError, "outcome uncertain"):
                client.runtime_clear(
                    key="live-ambiguous", sha=_SHA,
                    evidence="runtime:forged-independent-postread",
                )
            self.assertIsNone(client._socket)
        self.assertFalse(self.owner.clearance_recorded(
            key="live-ambiguous", sha=_SHA,
        ))
        self.assertTrue(self.owner.fence_pending())
        server.close()

    def test_runtime_forged_clearance_lost_response_never_grants_recovery(self) -> None:
        runtime_dir = self.root / "lost-clearance-runtime"
        operator_dir = self.root / "lost-clearance-operator"
        runtime_dir.mkdir(mode=0o700)
        runtime_dir.chmod(0o2710)
        operator_dir.mkdir(mode=0o700)
        server = FenceWitnessServer(
            ledger=self.owner,
            runtime_socket=runtime_dir / "runtime.sock",
            operator_socket=operator_dir / "operator.sock",
            runtime_uid=os.geteuid() + 100000,
            runtime_gid=os.getegid(), operator_uid=os.geteuid(),
        )
        self.addCleanup(server.close)
        server._runtime.peer_uid = os.geteuid()
        server.start()
        with FenceWitnessClient(
            runtime_dir / "runtime.sock", expected_owner_uid=os.geteuid(),
        ) as client:
            client.started(key="lost-clearance-ack", sha=_SHA, action="create_media")
            self.assertEqual(client.completed(
                key="lost-clearance-ack", sha=_SHA, response=_UNKNOWN_MEDIA,
            ), "ambiguous")
            receive = client._receive

            def discard_denied_reply() -> dict[str, object]:
                answer = receive()
                if answer == {"ok": False, "error": "request_denied"}:
                    raise OSError("synthetic lost denial frame")
                return answer

            with patch.object(client, "_receive", side_effect=discard_denied_reply):
                with self.assertRaisesRegex(FenceWitnessError, "outcome uncertain"):
                    client.runtime_clear(
                        key="lost-clearance-ack", sha=_SHA,
                        evidence="runtime:claimed-existing-submit",
                    )
            self.assertIsNone(client._socket)
            self.assertFalse(self.owner.clearance_recorded(
                key="lost-clearance-ack", sha=_SHA,
            ))
            self.assertTrue(self.owner.fence_pending())
            with self.assertRaises(FenceWitnessError):
                client.resume_from_verified_clearance(
                    key="lost-clearance-ack", sha=_SHA,
                )
            self.assertIsNone(client._socket)
            self.assertTrue(self.owner.fence_pending())
        server.close()

    def test_shutdown_does_not_block_on_continuous_runtime_session(self) -> None:
        """A long-lived client must not hold SIGTERM cleanup indefinitely.

        Same-UID authorization is enabled only on this throwaway *test*
        server after construction; production always rejects owner=runtime.
        """
        runtime_dir = self.root / "shutdown-runtime"
        operator_dir = self.root / "shutdown-operator"
        runtime_dir.mkdir(mode=0o700)
        runtime_dir.chmod(0o2710)
        operator_dir.mkdir(mode=0o700)
        server = FenceWitnessServer(
            ledger=self.owner,
            runtime_socket=runtime_dir / "witness.sock",
            operator_socket=operator_dir / "operator.sock",
            runtime_uid=os.geteuid() + 100000,
            runtime_gid=os.getegid(),
            operator_uid=os.geteuid(),
        )
        self.addCleanup(server.close)
        # This simulation needs a real persistent UNIX client but not UID
        # elevation. Modify only the isolated test server's peer allowlist.
        server._runtime.peer_uid = os.geteuid()
        server.start()
        client = FenceWitnessClient(
            runtime_dir / "witness.sock", expected_owner_uid=os.geteuid(),
        )
        # A normal browser operation may be idle for well over the old
        # three-second socket timeout; it must not be force-sealed as UNKNOWN.
        time.sleep(3.2)
        self.assertFalse(client.pending())
        stop = threading.Event()
        done = threading.Event()
        errors: list[BaseException] = []

        def busy_client() -> None:
            while not stop.wait(0.05):
                try:
                    client.pending()
                except FenceWitnessError:
                    return

        def close_server() -> None:
            try:
                server.close()
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

        poll = threading.Thread(target=busy_client, daemon=True)
        closer = threading.Thread(target=close_server, daemon=True)
        poll.start()
        closer.start()
        try:
            self.assertTrue(
                done.wait(1.5),
                "long-lived runtime session blocks bounded server shutdown",
            )
            self.assertEqual(errors, [])
        finally:
            stop.set()
            client.close()
            poll.join(timeout=2)
            closer.join(timeout=4)


if __name__ == "__main__":
    unittest.main()