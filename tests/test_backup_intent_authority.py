"""Synthetic tests only; they do not demonstrate host UID/mmap separation."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
import tempfile
import unittest
from unittest.mock import patch

from mark_api.backup_intent_authority import (
    BackupIntentAuthority, BackupIntentError, _locked,
)
from mark_api.fence_witness import FenceLedger
from mark_api.paired_backup_operator import PairedBackupOperatorError, seal_offline_pair
from mark_api.paired_restore_preflight import inspect_offline_pair
from mark_api.storage import SnapshotStore


FIRST = "1" * 32
SECOND = "2" * 32


class BackupIntentAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mark-independent-intent-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.source = base / "trusted-cold-source"
        self.output = base / "trusted-generations"
        self.anchor = base / "trusted-head-anchors"
        self.intents = base / "trusted-one-time-intents"
        for directory in (self.source, self.output, self.anchor, self.intents):
            directory.mkdir(mode=0o700)
        self.sqlite = self.source / "mark.sqlite"
        self.witness = self.source / "witness.sqlite"
        SnapshotStore(self.sqlite)
        with FenceLedger(self.witness, initialize=True):
            pass
        self.operator = BackupIntentAuthority(self.intents)

    def _hash(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _prepare(self, generation: str = FIRST, previous: str | None = None) -> dict[str, str]:
        return self.operator.prepare(
            generation, self.sqlite, self.witness,
            previous_head_sha256=previous,
        )

    def _seal(self, generation: str = FIRST, previous: str | None = None) -> dict[str, object]:
        self._prepare(generation, previous)
        return self.operator.seal_prepared(generation, self.output, self.anchor)

    def test_trusted_operator_prepares_and_acknowledges_exactly_once(self) -> None:
        prepared = self._prepare()
        self.assertEqual(prepared["sqlite_sha256"], self._hash(self.sqlite))
        self.assertEqual(prepared["witness_sha256"], self._hash(self.witness))
        self.assertEqual(prepared["previous_head_sha256"], "0" * 64)
        self.assertFalse((self.intents / (FIRST + ".committing.json")).exists())
        result = self.operator.seal_prepared(FIRST, self.output, self.anchor)
        self.assertTrue(result["external_operator_intent_acknowledged"])
        self.assertFalse(result["restore_authorized"])
        self.assertFalse((self.anchor / ".inflight").exists())
        self.assertEqual(result["head_sha256"], self._hash(self.anchor / "latest.json"))
        for phase in ("prepared", "committing", "acknowledged"):
            path = self.intents / f"{FIRST}.{phase}.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(BackupIntentError):
            self.operator.acknowledge(FIRST, self.output, self.anchor)
        with self.assertRaises(BackupIntentError):
            self.operator.begin_commit(FIRST)
        with self.assertRaises(BackupIntentError):
            self._prepare()

    def test_replayed_original_attestation_cannot_consume_forged_marker(self) -> None:
        first = self._seal()
        marker = self.anchor / ".inflight"
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, FIRST.encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        # The same generation, actual original input SHAs and original
        # predecessor are present. External durable acknowledged state wins.
        with self.assertRaisesRegex(BackupIntentError, "no unconsumed"):
            self.operator.acknowledge(FIRST, self.output, self.anchor)
        self.assertEqual(marker.read_bytes(), FIRST.encode("ascii"))
        self.assertEqual(first["head_sha256"], self._hash(self.anchor / "latest.json"))

    def test_public_reconciliation_cannot_consume_replayed_acknowledged_intent(self) -> None:
        from mark_api.paired_backup_operator import reconcile_committed_inflight

        committed = self._seal()
        marker = self.anchor / ".inflight"
        marker.write_bytes(FIRST.encode("ascii"))
        marker.chmod(0o600)
        original_sha = self._hash(self.sqlite)
        witness_sha = self._hash(self.witness)
        with self.assertRaisesRegex(
            PairedBackupOperatorError, "direct reconciliation forbidden",
        ):
            reconcile_committed_inflight(
                self.output, self.anchor,
                expected_current_head_sha256=committed["head_sha256"],
                expected_generation=FIRST,
                expected_previous_head_sha256=None,
                expected_sqlite_sha256=original_sha,
                expected_witness_sha256=witness_sha,
            )
        self.assertEqual(marker.read_bytes(), FIRST.encode("ascii"))
        with self.assertRaisesRegex(BackupIntentError, "no unconsumed"):
            self.operator.acknowledge(FIRST, self.output, self.anchor)

    def test_second_generation_must_extend_independently_acknowledged_tip(self) -> None:
        first = self._seal()
        with self.assertRaisesRegex(BackupIntentError, "predecessor"):
            self._prepare(SECOND)
        prepared = self._prepare(SECOND, first["head_sha256"])
        self.assertEqual(prepared["previous_head_sha256"], first["head_sha256"])
        second = self.operator.seal_prepared(SECOND, self.output, self.anchor)
        self.assertNotEqual(first["head_sha256"], second["head_sha256"])
        self.assertEqual(
            json.loads((self.anchor / "latest.json").read_bytes())["predecessor_sha256"],
            first["head_sha256"],
        )
        with self.assertRaises(BackupIntentError):
            self.operator.acknowledge(FIRST, self.output, self.anchor)

    def test_prepared_or_committing_generation_blocks_other_preparations(self) -> None:
        self._prepare()
        with self.assertRaisesRegex(BackupIntentError, "pending"):
            self._prepare(SECOND)
        self.operator.begin_commit(FIRST)
        with self.assertRaisesRegex(BackupIntentError, "never retry"):
            self.operator.begin_commit(FIRST)
        with self.assertRaisesRegex(BackupIntentError, "pending"):
            self._prepare(SECOND)
        self.assertFalse((self.intents / (FIRST + ".acknowledged.json")).exists())

    @unittest.skipUnless(
        os.name == "posix" and hasattr(signal, "SIGKILL"), "POSIX SIGKILL required",
    )
    def test_real_sigkill_after_persisted_head_recovers_once_with_both_fences(self) -> None:
        # This kills a fresh *synthetic* sealer after durable HEAD publication.
        # It does not attest independent host UID/FD/MAP_SHARED isolation.
        key, request_sha = "pair:real-sigkill-pending", "b" * 64
        now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        store.claim_write_api_request(
            idempotency_key=key, request_sha256=request_sha,
            requested_at=now, claim_owner="synthetic-owner",
        )
        store.begin_write_api_request(
            idempotency_key=key, request_sha256=request_sha,
            execution_started_at=now, claim_owner="synthetic-owner",
            strict_owner_fence=True,
        )
        with FenceLedger(self.witness) as witness:
            session = witness.open_session()
            witness.execution_started(
                session, key=key, sha=request_sha, action="create",
            )
            witness.seal_session(session)
        self._prepare()

        env = dict(
            os.environ,
            PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
            MARK_PLATFORM_WRITES_ENABLED="false",
            PYTHONDONTWRITEBYTECODE="1",
        )
        kill_after_head = r"""
import os, signal, sys
from pathlib import Path
from mark_api.backup_intent_authority import BackupIntentAuthority
from mark_api import paired_backup_operator as backup
intent, output, anchor, generation = map(Path, sys.argv[1:5])
real_unlink = backup.os.unlink
def kill_before_crash_marker_release(path, *args, **kwargs):
    if Path(path) == anchor / ".inflight":
        os.kill(os.getpid(), signal.SIGKILL)
    return real_unlink(path, *args, **kwargs)
backup.os.unlink = kill_before_crash_marker_release
BackupIntentAuthority(intent).seal_prepared(str(generation), output, anchor)
raise AssertionError("post-HEAD SIGKILL hook not reached")
"""
        def child(script: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    sys.executable, "-B", "-c", script, str(self.intents),
                    str(self.output), str(self.anchor), FIRST,
                ],
                cwd=self.temp.name, env=env,
                capture_output=True, text=True, timeout=20, check=False,
            )

        terminated = child(kill_after_head)
        self.assertEqual(terminated.returncode, -signal.SIGKILL, terminated.stderr)
        self.assertTrue((self.anchor / "latest.json").is_file())
        self.assertTrue((self.anchor / ".inflight").is_file())
        self.assertTrue((self.intents / f"{FIRST}.committing.json").is_file())
        self.assertFalse((self.intents / f"{FIRST}.acknowledged.json").exists())

        def pending_fences() -> tuple[int, int]:
            checked = inspect_offline_pair(
                self.output / FIRST / "mark.sqlite",
                self.output / FIRST / "witness.sqlite",
            )["fences"]
            return (
                len(checked["sqlite_started"]),
                len(checked["witness_unresolved"]),
            )

        original_fences = pending_fences()
        self.assertEqual(original_fences, (1, 1))

        restart_once = r"""
import sys
from pathlib import Path
from mark_api.backup_intent_authority import BackupIntentAuthority, BackupIntentError
intent, output, anchor, generation = sys.argv[1:5]
operator = BackupIntentAuthority(Path(intent))
receipt = operator.acknowledge(generation, Path(output), Path(anchor))
assert receipt["reconciled"] and receipt["acknowledged_once"]
assert not receipt["restore_authorized"] and not receipt["external_write_authorized"]
try:
    operator.acknowledge(generation, Path(output), Path(anchor))
except BackupIntentError:
    print("REPLAY_DENIED")
else:
    raise AssertionError("replayed operator intent accepted")
"""
        restarted = child(restart_once)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertIn("REPLAY_DENIED", restarted.stdout)
        self.assertFalse((self.anchor / ".inflight").exists())
        self.assertTrue((self.intents / f"{FIRST}.acknowledged.json").is_file())
        self.assertEqual(pending_fences(), original_fences)

    def test_crash_before_head_keeps_external_intent_and_inflight(self) -> None:
        self._prepare()
        def crash_at_publish(src: Path, dst: Path) -> None:
            raise OSError("synthetic before HEAD publication")
        with patch("mark_api.paired_backup_operator.os.replace", side_effect=crash_at_publish):
            with self.assertRaises(PairedBackupOperatorError):
                self.operator.seal_prepared(FIRST, self.output, self.anchor)
        self.assertFalse((self.anchor / "latest.json").exists())
        self.assertTrue((self.anchor / ".inflight").exists())
        self.assertTrue((self.intents / (FIRST + ".committing.json")).exists())
        self.assertFalse((self.intents / (FIRST + ".acknowledged.json")).exists())
        with self.assertRaises(BackupIntentError):
            self.operator.acknowledge(FIRST, self.output, self.anchor)
        self.assertTrue((self.anchor / ".inflight").exists())

    def test_crash_after_head_with_inflight_reconciles_only_open_intent(self) -> None:
        self._prepare()
        real_unlink = os.unlink
        def crash_on_fence_release(path: Path, *args: object, **kwargs: object) -> None:
            if Path(path) == self.anchor / ".inflight":
                raise OSError("synthetic crash after HEAD durability")
            real_unlink(path, *args, **kwargs)
        with patch("mark_api.paired_backup_operator.os.unlink", side_effect=crash_on_fence_release):
            with self.assertRaises(PairedBackupOperatorError):
                self.operator.seal_prepared(FIRST, self.output, self.anchor)
        self.assertTrue((self.anchor / "latest.json").exists())
        self.assertTrue((self.anchor / ".inflight").exists())
        self.assertFalse((self.intents / (FIRST + ".acknowledged.json")).exists())
        recovered = self.operator.acknowledge(FIRST, self.output, self.anchor)
        self.assertTrue(recovered["reconciled"])
        self.assertFalse((self.anchor / ".inflight").exists())
        with self.assertRaisesRegex(BackupIntentError, "no unconsumed"):
            self.operator.acknowledge(FIRST, self.output, self.anchor)

    def test_crash_after_sealer_success_before_external_ack_is_recoverable(self) -> None:
        self._prepare()
        with patch.object(
            BackupIntentAuthority, "acknowledge",
            side_effect=BackupIntentError("simulated lost external ack"),
        ):
            with self.assertRaises(BackupIntentError):
                self.operator.seal_prepared(FIRST, self.output, self.anchor)
        self.assertTrue((self.anchor / "latest.json").exists())
        self.assertFalse((self.anchor / ".inflight").exists())
        self.assertFalse((self.intents / (FIRST + ".acknowledged.json")).exists())
        result = self.operator.acknowledge(FIRST, self.output, self.anchor)
        self.assertFalse(result["reconciled"])
        self.assertTrue(result["acknowledged_once"])

    def test_source_image_tamper_after_preparation_blocks_seal(self) -> None:
        self._prepare()
        raw = bytearray(self.sqlite.read_bytes())
        raw[-1] ^= 1
        self.sqlite.write_bytes(raw)
        with self.assertRaises(BackupIntentError):
            self.operator.seal_prepared(FIRST, self.output, self.anchor)
        self.assertFalse((self.intents / (FIRST + ".committing.json")).exists())
        self.assertTrue((self.intents / (FIRST + ".prepared.json")).exists())
        self.assertFalse((self.intents / (FIRST + ".acknowledged.json")).exists())

    def test_published_image_tamper_does_not_consume_external_intent(self) -> None:
        prepared = self._prepare()
        self.operator.begin_commit(FIRST)
        seal_offline_pair(
            self.sqlite, self.witness, self.output, self.anchor,
            expected_sqlite_sha256=prepared["sqlite_sha256"],
            expected_witness_sha256=prepared["witness_sha256"],
            expected_previous_head_sha256=None,
            generation=FIRST,
        )
        target = self.output / FIRST / "mark.sqlite"
        data = bytearray(target.read_bytes())
        data[-1] ^= 1
        target.write_bytes(data)
        with self.assertRaises(BackupIntentError):
            self.operator.acknowledge(FIRST, self.output, self.anchor)
        self.assertFalse((self.intents / (FIRST + ".acknowledged.json")).exists())
        self.assertTrue((self.intents / (FIRST + ".committing.json")).exists())

    def test_invalid_external_history_and_unknown_artifacts_fail_closed(self) -> None:
        self._prepare()
        intent = self.intents / f"{FIRST}.prepared.json"
        altered = json.loads(intent.read_bytes())
        altered["sqlite_sha256"] = "b" * 64
        intent.write_text(json.dumps(altered), encoding="utf-8")
        with self.assertRaises(BackupIntentError):
            self.operator.begin_commit(FIRST)
        # Nothing is deleted or self-repaired.
        self.assertTrue(intent.exists())
        self.assertFalse((self.intents / (FIRST + ".committing.json")).exists())

    def test_unrecognized_operator_artifact_blocks_prepare(self) -> None:
        (self.intents / "unrelated").write_text("bogus", encoding="utf-8")
        with self.assertRaises(BackupIntentError):
            self._prepare()
        self.assertFalse((self.intents / (FIRST + ".prepared.json")).exists())

    def test_external_lock_blocks_competing_prepare(self) -> None:
        with _locked(self.intents):
            with self.assertRaisesRegex(BackupIntentError, "another independent"):
                self._prepare()
        self.assertFalse((self.intents / (FIRST + ".prepared.json")).exists())

    def test_missing_lock_is_not_recreated_under_old_flock_holder(self) -> None:
        lock = self.intents / ".intent.lock"
        second_entered = False
        try:
            with _locked(self.intents):
                lock.unlink()
                try:
                    with _locked(self.intents):
                        second_entered = True
                except BackupIntentError:
                    pass
        except BackupIntentError:
            # The outer pin must also fail closed after its inode was removed.
            pass
        self.assertFalse(second_entered, "new lock inode enabled a concurrent sealer")
        self.assertFalse(lock.exists(), "normal operations must never replace lost lock")

    def test_reinitializing_nonempty_history_must_not_restore_missing_lock(self) -> None:
        self._prepare()
        lock = self.intents / ".intent.lock"
        lock.unlink()
        with self.assertRaises(BackupIntentError):
            BackupIntentAuthority(self.intents).begin_commit(FIRST)
        self.assertFalse(lock.exists())
        self.assertFalse((self.intents / f"{FIRST}.committing.json").exists())

    def test_corrupted_acknowledged_ancestor_blocks_later_seal(self) -> None:
        first = self._seal()
        self._prepare(SECOND, first["head_sha256"])
        target = self.output / FIRST / "mark.sqlite"
        damaged = bytearray(target.read_bytes())
        damaged[-1] ^= 1
        target.write_bytes(damaged)
        with self.assertRaises(BackupIntentError):
            self.operator.seal_prepared(SECOND, self.output, self.anchor)
        self.assertEqual(self._hash(self.anchor / "latest.json"), first["head_sha256"])
        self.assertFalse((self.output / SECOND).exists())
        self.assertFalse((self.intents / f"{SECOND}.committing.json").exists())
        self.assertTrue((self.intents / f"{SECOND}.prepared.json").is_file())
        self.assertFalse((self.intents / f"{SECOND}.acknowledged.json").exists())

    def test_late_corrupted_ancestor_blocks_external_acknowledgement(self) -> None:
        first = self._seal()
        self._prepare(SECOND, first["head_sha256"])
        self.operator.begin_commit(SECOND)
        prepared = json.loads((self.intents / f"{SECOND}.prepared.json").read_bytes())
        seal_offline_pair(
            self.sqlite, self.witness, self.output, self.anchor,
            expected_sqlite_sha256=prepared["sqlite_sha256"],
            expected_witness_sha256=prepared["witness_sha256"],
            expected_previous_head_sha256=first["head_sha256"],
            generation=SECOND,
        )
        ancestor = self.output / FIRST / "mark.sqlite"
        damaged = bytearray(ancestor.read_bytes())
        damaged[-1] ^= 1
        ancestor.write_bytes(damaged)
        with self.assertRaises(BackupIntentError):
            self.operator.acknowledge(SECOND, self.output, self.anchor)
        self.assertTrue((self.intents / f"{SECOND}.committing.json").exists())
        self.assertFalse((self.intents / f"{SECOND}.acknowledged.json").exists())

    def test_lock_replacement_with_same_bytes_is_not_trusted(self) -> None:
        lock = self.intents / ".intent.lock"
        replacement = self.intents / "replacement-lock"
        replacement.write_bytes(b"")
        replacement.chmod(0o600)
        os.replace(replacement, lock)
        with self.assertRaisesRegex(BackupIntentError, "lock inode"):
            self._prepare()
        self.assertFalse((self.intents / f"{FIRST}.prepared.json").exists())

    def test_operator_restart_reuses_original_pinned_lock(self) -> None:
        lock = self.intents / ".intent.lock"
        original_inode = lock.stat().st_ino
        restarted = BackupIntentAuthority(self.intents)
        result = restarted.prepare(FIRST, self.sqlite, self.witness, previous_head_sha256=None)
        self.assertEqual(result["generation"], FIRST)
        self.assertEqual(lock.stat().st_ino, original_inode)

    def test_out_of_process_sealer_must_not_race_committing_abandon(self) -> None:
        """A sealer with no effect yet may still own the independent anchor lock."""
        from threading import current_thread
        from mark_api import paired_backup_operator as sealer

        prepared = self._prepare()
        self.operator.begin_commit(FIRST)
        original = sealer._no_sidecars
        entered, release = Event(), Event()
        result: dict[str, object] = {}
        worker: Thread | None = None

        def before_first_effect(path: Path) -> None:
            if current_thread() is worker and not entered.is_set():
                entered.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("synthetic external sealer did not resume")
            original(path)

        def external_seal() -> None:
            try:
                result["sealed"] = seal_offline_pair(
                    self.sqlite, self.witness, self.output, self.anchor,
                    expected_sqlite_sha256=prepared["sqlite_sha256"],
                    expected_witness_sha256=prepared["witness_sha256"],
                    expected_previous_head_sha256=None, generation=FIRST,
                )
            except Exception as exc:
                result["error"] = exc

        with patch("mark_api.paired_backup_operator._no_sidecars",
                   side_effect=before_first_effect):
            worker = Thread(target=external_seal, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=6))
                self.assertFalse((self.anchor / ".inflight").exists())
                self.assertFalse((self.anchor / "latest.json").exists())
                with self.assertRaisesRegex(BackupIntentError, "quiescence"):
                    self.operator.abandon(FIRST, self.output, self.anchor)
            finally:
                release.set()
                worker.join(timeout=15)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", result)
        self.assertIn("sealed", result)
        self.assertFalse((self.intents / f"{FIRST}.abandoned.json").exists())
        self.assertTrue((self.anchor / "latest.json").is_file())
        acknowledged = self.operator.acknowledge(FIRST, self.output, self.anchor)
        self.assertTrue(acknowledged["acknowledged_once"])

    def test_after_ack_prepared_abandon_rejects_individual_artifacts(self) -> None:
        """Check named fences after a predecessor, not only the genesis empty-root shortcut."""
        first = self._seal()
        self._prepare(SECOND, first["head_sha256"])
        head_before = (self.anchor / "latest.json").read_bytes()
        cases = (
            ("output", SECOND, True, "generation output may already have effects"),
            ("anchor", SECOND + ".json", False, "generation head record may already exist"),
            ("output", ".partial-orphan", True, "partial generation may contain effects"),
            ("anchor", ".inflight", False, "crash fence or pending HEAD exists"),
            ("anchor", ".next-orphan", False, "crash fence or pending HEAD exists"),
        )
        for location, name, directory, expected_reason in cases:
            with self.subTest(location=location, artifact=name):
                artifact = (self.output if location == "output" else self.anchor) / name
                if directory:
                    artifact.mkdir(mode=0o700)
                else:
                    artifact.write_bytes(b"synthetic-blocking-artifact")
                    artifact.chmod(0o600)
                try:
                    with self.assertRaisesRegex(BackupIntentError, expected_reason):
                        self.operator.abandon(SECOND, self.output, self.anchor)
                    self.assertFalse((self.intents / f"{SECOND}.abandoned.json").exists())
                    self.assertEqual((self.anchor / "latest.json").read_bytes(), head_before)
                    self.assertTrue((self.intents / f"{SECOND}.prepared.json").is_file())
                finally:
                    if directory:
                        artifact.rmdir()
                    else:
                        artifact.unlink()

    def test_after_ack_second_effect_proof_rejects_fsync_race(self) -> None:
        """The second check matters even with a previously acknowledged history."""
        from mark_api import backup_intent_authority as module

        first = self._seal()
        self._prepare(SECOND, first["head_sha256"])
        head_before = (self.anchor / "latest.json").read_bytes()
        original = module._fsync_dir
        injected = False

        def create_generation_during_sync(path: Path) -> None:
            nonlocal injected
            original(path)
            if path == self.output and not injected:
                injected = True
                (self.output / SECOND).mkdir(mode=0o700)

        with patch("mark_api.backup_intent_authority._fsync_dir",
                   side_effect=create_generation_during_sync):
            with self.assertRaisesRegex(BackupIntentError, "generation output may already have effects"):
                self.operator.abandon(SECOND, self.output, self.anchor)
        self.assertTrue(injected)
        self.assertFalse((self.intents / f"{SECOND}.abandoned.json").exists())
        self.assertEqual((self.anchor / "latest.json").read_bytes(), head_before)

    def test_committing_requires_independent_quiescence_and_remains_pending(self) -> None:
        self._prepare()
        self.operator.begin_commit(FIRST)
        with self.assertRaisesRegex(BackupIntentError, "pending"):
            self._prepare(SECOND)
        with self.assertRaisesRegex(BackupIntentError, "independent host quiescence"):
            self.operator.abandon(FIRST, self.output, self.anchor)
        self.assertTrue((self.intents / f"{FIRST}.committing.json").is_file())
        self.assertFalse((self.intents / f"{FIRST}.abandoned.json").exists())
        self.assertFalse((self.intents / f"{FIRST}.acknowledged.json").exists())
        with self.assertRaisesRegex(BackupIntentError, "pending"):
            self._prepare(SECOND)
    def test_effect_free_prepared_only_can_be_abandoned(self) -> None:
        self._prepare()
        self.operator.abandon(FIRST, self.output, self.anchor)
        self.assertFalse((self.intents / f"{FIRST}.committing.json").exists())
        with self.assertRaises(BackupIntentError):
            self.operator.acknowledge(FIRST, self.output, self.anchor)
        self.assertEqual(self._seal(SECOND)["generation"], SECOND)

    def test_failed_ancestor_check_keeps_head_and_supports_safe_abandon(self) -> None:
        first = self._seal()
        self._prepare(SECOND, first["head_sha256"])
        original = (self.output / FIRST / "mark.sqlite").read_bytes()
        mutated = bytearray(original)
        mutated[-1] ^= 1
        (self.output / FIRST / "mark.sqlite").write_bytes(mutated)
        with self.assertRaises(BackupIntentError):
            self.operator.seal_prepared(SECOND, self.output, self.anchor)
        self.assertEqual(self._hash(self.anchor / "latest.json"), first["head_sha256"])
        self.assertFalse((self.output / SECOND).exists())
        self.operator.abandon(SECOND, self.output, self.anchor)
        self.assertFalse((self.intents / f"{SECOND}.acknowledged.json").exists())
        (self.output / FIRST / "mark.sqlite").write_bytes(original)
        third = "3" * 32
        self.assertEqual(self._seal(third, first["head_sha256"])["generation"], third)

    def test_abandon_refuses_open_inflight_and_preserves_fences(self) -> None:
        self._prepare()
        (self.anchor / ".inflight").write_bytes(FIRST.encode("ascii"))
        with self.assertRaisesRegex(BackupIntentError, "crash fence or pending HEAD"):
            self.operator.abandon(FIRST, self.output, self.anchor)
        self.assertTrue((self.anchor / ".inflight").exists())
        self.assertTrue((self.intents / f"{FIRST}.prepared.json").exists())
        self.assertFalse((self.intents / f"{FIRST}.abandoned.json").exists())
    def test_abandon_refuses_existing_generation_artifact(self) -> None:
        self._prepare()
        (self.output / FIRST).mkdir(mode=0o700)
        with self.assertRaisesRegex(BackupIntentError, "generation output may already have effects"):
            self.operator.abandon(FIRST, self.output, self.anchor)
        self.assertFalse((self.intents / f"{FIRST}.abandoned.json").exists())
    def test_abandon_refuses_running_cooperating_sealer_without_publishing_fork(self) -> None:
        from mark_api import backup_intent_authority as module

        self._prepare()
        real_seal = module.seal_offline_pair
        entered, proceed = Event(), Event()
        results: dict[str, object] = {}

        def pause_before_seal(*args: object, **kwargs: object) -> dict[str, object]:
            entered.set()
            if not proceed.wait(timeout=10):
                raise RuntimeError("timed out waiting for concurrent proof")
            return real_seal(*args, **kwargs)

        def seal_worker() -> None:
            try:
                results["result"] = self.operator.seal_prepared(
                    FIRST, self.output, self.anchor,
                )
            except Exception as exc:
                results["error"] = exc

        with patch("mark_api.backup_intent_authority.seal_offline_pair",
                   side_effect=pause_before_seal):
            worker = Thread(target=seal_worker, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                with self.assertRaises(BackupIntentError):
                    self.operator.abandon(FIRST, self.output, self.anchor)
            finally:
                proceed.set()
                worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", results)
        self.assertTrue(results["result"]["external_operator_intent_acknowledged"])
        self.assertFalse((self.intents / f"{FIRST}.abandoned.json").exists())
        self.assertTrue((self.intents / f"{FIRST}.acknowledged.json").exists())


    def _write_canonical_record(self, path: Path, record: dict[str, object]) -> None:
        # Adversarial records must otherwise satisfy the operator's exact
        # canonical serialization and DAC checks, or a negative is vacuous.
        path.write_bytes(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii"))
        path.chmod(0o600)

    def test_post_commit_crash_preserves_original_sealer_exception_and_pending_intent(self) -> None:
        self._prepare()
        with patch(
            "mark_api.backup_intent_authority.seal_offline_pair",
            side_effect=PairedBackupOperatorError("synthetic unknown post-commit result"),
        ):
            with self.assertRaises(PairedBackupOperatorError) as raised:
                self.operator.seal_prepared(FIRST, self.output, self.anchor)
        self.assertIs(type(raised.exception), PairedBackupOperatorError)
        self.assertTrue((self.intents / f"{FIRST}.committing.json").is_file())
        self.assertFalse((self.intents / f"{FIRST}.acknowledged.json").exists())
        self.assertFalse((self.intents / f"{FIRST}.abandoned.json").exists())
        with self.assertRaisesRegex(BackupIntentError, "never retry"):
            self.operator.begin_commit(FIRST)

    def test_abandon_refuses_published_head_without_crash_marker_or_ack(self) -> None:
        prepared = self._prepare()
        self.operator.begin_commit(FIRST)
        sealed = seal_offline_pair(
            self.sqlite, self.witness, self.output, self.anchor,
            expected_sqlite_sha256=prepared["sqlite_sha256"],
            expected_witness_sha256=prepared["witness_sha256"],
            expected_previous_head_sha256=None, generation=FIRST,
        )
        self.assertFalse((self.anchor / ".inflight").exists())
        head_bytes = (self.anchor / "latest.json").read_bytes()
        self.assertEqual(self._hash(self.anchor / "latest.json"), sealed["head_sha256"])
        with self.assertRaises(BackupIntentError):
            self.operator.abandon(FIRST, self.output, self.anchor)
        self.assertEqual((self.anchor / "latest.json").read_bytes(), head_bytes)
        self.assertTrue((self.intents / f"{FIRST}.committing.json").exists())
        self.assertFalse((self.intents / f"{FIRST}.abandoned.json").exists())

    def test_abandon_refuses_pending_head_and_partial_generation_artifacts(self) -> None:
        cases = (
            ("anchor", ".next-synthetic", False, "crash fence or pending HEAD"),
            ("output", ".partial-synthetic", True, "partial generation may contain effects"),
            ("anchor", f"{FIRST}.json", False, "generation head record may already exist"),
            ("anchor", "unrecognized-bootstrap-artifact", False, "bootstrap state is ambiguous"),
            ("output", "unrecognized-bootstrap-artifact", False, "bootstrap state is ambiguous"),
        )
        self._prepare()
        for where, name, directory, reason in cases:
            with self.subTest(root=where, name=name):
                item = (self.anchor if where == "anchor" else self.output) / name
                if directory:
                    item.mkdir(mode=0o700)
                else:
                    item.write_bytes(b"nonempty-untrusted-artifact")
                    item.chmod(0o600)
                try:
                    with self.assertRaisesRegex(BackupIntentError, reason):
                        self.operator.abandon(FIRST, self.output, self.anchor)
                    self.assertFalse((self.intents / f"{FIRST}.abandoned.json").exists())
                    self.assertTrue((self.intents / f"{FIRST}.prepared.json").is_file())
                finally:
                    if directory:
                        item.rmdir()
                    else:
                        item.unlink()
    def test_abandon_second_proof_detects_effect_injected_during_directory_fsync(self) -> None:
        from mark_api import backup_intent_authority as module
        self._prepare()
        true_fsync = module._fsync_dir
        injected = False

        def inject_on_first_output_sync(path: Path) -> None:
            nonlocal injected
            true_fsync(path)
            if path == self.output and not injected:
                injected = True
                (self.output / FIRST).mkdir(mode=0o700)

        with patch("mark_api.backup_intent_authority._fsync_dir",
                   side_effect=inject_on_first_output_sync):
            with self.assertRaisesRegex(BackupIntentError, "generation output may already have effects"):
                self.operator.abandon(FIRST, self.output, self.anchor)
        self.assertTrue(injected)
        self.assertTrue((self.output / FIRST).is_dir())
        self.assertTrue((self.intents / f"{FIRST}.prepared.json").exists())
        self.assertFalse((self.intents / f"{FIRST}.committing.json").exists())
        self.assertFalse((self.intents / f"{FIRST}.abandoned.json").exists())
    def test_operator_rejects_conflicting_acknowledged_and_abandoned_transitions(self) -> None:
        first = self._seal()
        abandoned = {
            "schema_version": 1, "generation": FIRST,
            "prepared_sha256": self._hash(self.intents / f"{FIRST}.prepared.json"),
            "committing_sha256": self._hash(self.intents / f"{FIRST}.committing.json"),
            "previous_head_sha256": "0" * 64,
        }
        self._write_canonical_record(self.intents / f"{FIRST}.abandoned.json", abandoned)
        with self.assertRaisesRegex(BackupIntentError, "conflicting terminal"):
            self._prepare(SECOND, first["head_sha256"])
        self.assertEqual(self._hash(self.anchor / "latest.json"), first["head_sha256"])

    def test_operator_rejects_misbound_abandoned_digest(self) -> None:
        self._prepare()
        self.operator.abandon(FIRST, self.output, self.anchor)
        path = self.intents / f"{FIRST}.abandoned.json"
        record = json.loads(path.read_bytes())
        record["prepared_sha256"] = "f" * 64
        self._write_canonical_record(path, record)
        with self.assertRaisesRegex(BackupIntentError, "not bound"):
            self._prepare(SECOND)
        self.assertFalse((self.intents / f"{SECOND}.prepared.json").exists())

    def test_operator_rejects_committing_after_prepared_only_abandon(self) -> None:
        self._prepare()
        self.operator.abandon(FIRST, self.output, self.anchor)
        committing = {
            "schema_version": 1,
            "generation": FIRST,
            "prepared_sha256": self._hash(self.intents / f"{FIRST}.prepared.json"),
        }
        self._write_canonical_record(self.intents / f"{FIRST}.committing.json", committing)
        with self.assertRaisesRegex(BackupIntentError, "committing abandonment lacks independent quiescence"):
            self._prepare(SECOND)
        self.assertFalse((self.intents / f"{SECOND}.prepared.json").exists())

    def test_operator_rejects_abandoned_unknown_predecessor_fork(self) -> None:
        self._prepare()
        self.operator.abandon(FIRST, self.output, self.anchor)
        prepared_path = self.intents / f"{FIRST}.prepared.json"
        prepared = json.loads(prepared_path.read_bytes())
        prepared["previous_head_sha256"] = "f" * 64
        self._write_canonical_record(prepared_path, prepared)
        abandoned_path = self.intents / f"{FIRST}.abandoned.json"
        abandoned = json.loads(abandoned_path.read_bytes())
        abandoned["previous_head_sha256"] = "f" * 64
        abandoned["prepared_sha256"] = self._hash(prepared_path)
        self._write_canonical_record(abandoned_path, abandoned)
        with self.assertRaisesRegex(BackupIntentError, "predecessor not acknowledged"):
            self._prepare(SECOND)
        self.assertFalse((self.intents / f"{SECOND}.prepared.json").exists())

    def test_committing_plus_abandoned_record_must_fail_closed_on_restart(self) -> None:
        """A forged historical cancelled committing intent is not an authority."""
        self._prepare()
        self.operator.begin_commit(FIRST)
        self._write_canonical_record(
            self.intents / f"{FIRST}.abandoned.json",
            {
                "schema_version": 1,
                "generation": FIRST,
                "prepared_sha256": self._hash(self.intents / f"{FIRST}.prepared.json"),
                "committing_sha256": self._hash(self.intents / f"{FIRST}.committing.json"),
                "previous_head_sha256": "0" * 64,
            },
        )
        with self.assertRaisesRegex(BackupIntentError, "committing abandonment lacks independent quiescence"):
            BackupIntentAuthority(self.intents).prepare(
                SECOND, self.sqlite, self.witness, previous_head_sha256=None,
            )
        self.assertFalse((self.intents / f"{SECOND}.prepared.json").exists())

    def test_operator_restart_never_reuses_abandoned_identity(self) -> None:
        self._prepare()
        self.operator.abandon(FIRST, self.output, self.anchor)
        restarted = BackupIntentAuthority(self.intents)
        with self.assertRaises(BackupIntentError):
            restarted.prepare(FIRST, self.sqlite, self.witness, previous_head_sha256=None)
        with self.assertRaises(BackupIntentError):
            restarted.acknowledge(FIRST, self.output, self.anchor)
        self.assertEqual(
            restarted.prepare(SECOND, self.sqlite, self.witness, previous_head_sha256=None)["generation"],
            SECOND,
        )
    def test_external_ack_requires_anchor_fsync_before_ack(self) -> None:
        from mark_api import backup_intent_authority as module
        prepared = self._prepare()
        self.operator.begin_commit(FIRST)
        seal_offline_pair(
            self.sqlite, self.witness, self.output, self.anchor,
            expected_sqlite_sha256=prepared["sqlite_sha256"],
            expected_witness_sha256=prepared["witness_sha256"],
            expected_previous_head_sha256=None, generation=FIRST,
        )
        original_fsync = module._fsync_dir
        def fail_anchor_barrier(directory: Path) -> None:
            if directory == self.anchor:
                raise OSError("synthetic unsynced HEAD parent")
            original_fsync(directory)
        with patch("mark_api.backup_intent_authority._fsync_dir", side_effect=fail_anchor_barrier):
            with self.assertRaises(BackupIntentError):
                self.operator.acknowledge(FIRST, self.output, self.anchor)
        self.assertFalse((self.intents / f"{FIRST}.acknowledged.json").exists())
        self.assertTrue((self.intents / f"{FIRST}.committing.json").exists())

    def test_dangling_inflight_symlink_blocks_acknowledgement(self) -> None:
        prepared = self._prepare()
        self.operator.begin_commit(FIRST)
        seal_offline_pair(
            self.sqlite, self.witness, self.output, self.anchor,
            expected_sqlite_sha256=prepared["sqlite_sha256"],
            expected_witness_sha256=prepared["witness_sha256"],
            expected_previous_head_sha256=None, generation=FIRST,
        )
        (self.anchor / ".inflight").symlink_to(self.anchor / "absent-marker")
        with self.assertRaises(BackupIntentError):
            self.operator.acknowledge(FIRST, self.output, self.anchor)
        self.assertTrue((self.anchor / ".inflight").is_symlink())
        self.assertFalse((self.intents / f"{FIRST}.acknowledged.json").exists())

    def test_private_operator_state_is_required(self) -> None:
        self.intents.chmod(0o755)
        with self.assertRaises((BackupIntentError, PairedBackupOperatorError)):
            BackupIntentAuthority(self.intents)


if __name__ == "__main__":
    unittest.main()