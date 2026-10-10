"""Contract tests for the independent, not-yet-installed fence witness.

A green test does NOT mean a compromised runtime cannot forge live browser
evidence in its own active session or that the actual host is UID-isolated.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from mark_api.fence_witness import (
    FenceLedger,
    FenceWitnessClient,
    FenceWitnessError,
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

    def test_pending_media_reconciles_only_exact_current_session(self) -> None:
        session = self.owner.open_session()
        self.owner.execution_started(session, key=_KEY, sha=_SHA, action="create_media")
        self.assertTrue(self.owner.fence_pending())
        # A witness clearance before recording an ambiguous completed
        # operation must never remove the only durable execution fence.
        with self.assertRaisesRegex(FenceWitnessError, "completed ambiguous"):
            self.owner.runtime_clearance(
                session, key=_KEY, sha=_SHA, evidence="early:untrusted-claim",
            )
        self.assertTrue(self.owner.fence_pending())
        with self.assertRaises(FenceWitnessError):
            self.owner.execution_started(session, key=_KEY, sha=_SHA, action="create_media")
        self.assertEqual(
            self.owner.execution_completed(
                session, key=_KEY, sha=_SHA, response=_UNKNOWN_MEDIA,
            ),
            "ambiguous",
        )
        self.assertTrue(self.owner.fence_pending())
        with self.assertRaises(FenceWitnessError):
            self.owner.runtime_clearance(
                session, key=_KEY, sha="b" * 64, evidence="postread:evidence-1",
            )
        self.owner.runtime_clearance(
            session, key=_KEY, sha=_SHA, evidence="postread:evidence-1",
        )
        self.assertFalse(self.owner.fence_pending())
        with self.assertRaises(FenceWitnessError):
            self.owner.runtime_clearance(
                session, key=_KEY, sha=_SHA, evidence="postread:evidence-1",
            )
        self.owner.seal_session(session)
        self.owner.close()
        reopened = FenceLedger(self.ledger_file)
        self.addCleanup(reopened.close)
        self.assertFalse(reopened.fence_pending())
        with self.assertRaisesRegex(FenceWitnessError, "already"):
            new_session = reopened.open_session()
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
        with self.assertRaisesRegex(FenceWitnessError, "not active"):
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

    def test_same_key_never_reexecutes_after_completed_response_or_lost_local_db(self) -> None:
        session = self.owner.open_session()
        self.owner.execution_started(session, key="confirmed-key", sha=_SHA, action="create")
        self.assertEqual(
            self.owner.execution_completed(
                session, key="confirmed-key", sha=_SHA,
                response={
                    "operation_receipt": {
                        "writer_invoked": True, "outcome": "confirmed",
                    },
                },
            ),
            "confirmed",
        )
        self.assertFalse(self.owner.fence_pending())
        with self.assertRaisesRegex(FenceWitnessError, "already"):
            self.owner.execution_started(
                session, key="confirmed-key", sha=_SHA, action="create",
            )
        with self.assertRaisesRegex(FenceWitnessError, "already"):
            self.owner.execution_started(
                session, key="confirmed-key", sha="b" * 64, action="create",
            )
        self.owner.seal_session(session)
        self.owner.close()
        current = FenceLedger(self.ledger_file)
        self.addCleanup(current.close)
        next_session = current.open_session()
        with self.assertRaisesRegex(FenceWitnessError, "already"):
            current.execution_started(
                next_session, key="confirmed-key", sha=_SHA, action="create",
            )

    def test_conclusive_response_requires_exact_flags_and_media_persistence(self) -> None:
        session = self.owner.open_session()
        for number, response in enumerate((
            {"operation_receipt": {"writer_invoked": True, "outcome": "confirmed"},
             "media_persistence_confirmed": False},
            {"operation_receipt": {"writer_invoked": False, "outcome": "confirmed"},
             "media_persistence_confirmed": True},
            {"operation_receipt": {"writer_invoked": "true", "outcome": "confirmed"},
             "media_persistence_confirmed": True},
        )):
            key = f"ambiguous-{number}"
            self.owner.execution_started(
                session, key=key, sha=_SHA, action="create_media",
            )
            self.assertEqual(
                self.owner.execution_completed(
                    session, key=key, sha=_SHA, response=response,
                ),
                "ambiguous",
            )
            self.assertTrue(self.owner.fence_pending())
            self.owner.runtime_clearance(
                session, key=key, sha=_SHA,
                evidence=f"verified:browser-observation-{number}",
            )
        self.owner.execution_started(
            session, key="media-confirmed", sha=_SHA, action="create_media",
        )
        self.assertEqual(
            self.owner.execution_completed(
                session, key="media-confirmed", sha=_SHA,
                response=_GOOD_MEDIA,
            ), "confirmed",
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