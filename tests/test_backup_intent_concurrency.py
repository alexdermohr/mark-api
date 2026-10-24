"""Synthetic operator transaction concurrency; no host UID/FD/MAP_SHARED attestation."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from mark_api import backup_intent_authority as intent_module
from mark_api.backup_intent_authority import BackupIntentAuthority, BackupIntentError
from mark_api.fence_witness import FenceLedger
from mark_api.storage import SnapshotStore


GENERATION = "c" * 32


class BackupIntentConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mark-intent-concurrency-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.cold = root / "cold"
        self.output = root / "output"
        self.anchor = root / "anchor"
        self.intents = root / "intents"
        for directory in (self.cold, self.output, self.anchor, self.intents):
            directory.mkdir(mode=0o700)
        self.sqlite = self.cold / "mark.sqlite"
        self.witness = self.cold / "witness.sqlite"
        SnapshotStore(self.sqlite)
        with FenceLedger(self.witness, initialize=True):
            pass
        self.operator = BackupIntentAuthority(self.intents)

    def test_external_ack_must_not_consume_active_sealers_commit(self) -> None:
        self.operator.prepare(
            GENERATION, self.sqlite, self.witness,
            previous_head_sha256=None,
        )
        entered, release = Event(), Event()
        result: dict[str, object] = {}
        original_seal = intent_module.seal_offline_pair

        def pause_after_successful_seal(*args: object, **kwargs: object) -> object:
            sealed = original_seal(*args, **kwargs)
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("synthetic sealer remained blocked")
            return sealed

        def seal() -> None:
            try:
                result["seal"] = self.operator.seal_prepared(
                    GENERATION, self.output, self.anchor,
                )
            except BaseException as exc:
                result["error"] = exc

        with patch(
            "mark_api.backup_intent_authority.seal_offline_pair",
            side_effect=pause_after_successful_seal,
        ):
            worker = Thread(target=seal, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                self.assertTrue((self.anchor / "latest.json").is_file())
                self.assertTrue((self.intents / f"{GENERATION}.committing.json").is_file())
                self.assertFalse(
                    (self.intents / f"{GENERATION}.acknowledged.json").exists()
                )
                # A concurrent operator must not consume a committing intent
                # while its owning seal transaction still holds the root lock.
                with self.assertRaisesRegex(
                    BackupIntentError, "another trusted seal or abandon",
                ):
                    self.operator.acknowledge(
                        GENERATION, self.output, self.anchor,
                    )
                self.assertFalse(
                    (self.intents / f"{GENERATION}.acknowledged.json").exists()
                )
            finally:
                release.set()
                worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", result)
        sealed = result["seal"]
        self.assertTrue(sealed["external_operator_intent_acknowledged"])
        self.assertFalse(sealed["restore_authorized"])
        self.assertFalse(sealed["external_write_authorized"])
        self.assertTrue((self.intents / f"{GENERATION}.acknowledged.json").is_file())
        with self.assertRaisesRegex(BackupIntentError, "no unconsumed"):
            self.operator.acknowledge(GENERATION, self.output, self.anchor)


    def test_child_process_ack_cannot_preempt_active_seal(self) -> None:
        self.operator.prepare(
            GENERATION, self.sqlite, self.witness,
            previous_head_sha256=None,
        )
        entered, release = Event(), Event()
        sealed: dict[str, object] = {}
        real_seal = intent_module.seal_offline_pair

        def pause_after_publishing(*args: object, **kwargs: object) -> object:
            result = real_seal(*args, **kwargs)
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("synthetic sealer remained blocked")
            return result

        def owner_seal() -> None:
            try:
                sealed["result"] = self.operator.seal_prepared(
                    GENERATION, self.output, self.anchor,
                )
            except BaseException as exc:
                sealed["error"] = exc

        # The child imports the installed or local package independently.
        # This does not imply a privileged host UID or exclusive image mount.
        source = Path(__file__).resolve().parents[1] / "src"
        environment = dict(os.environ, MARK_PLATFORM_WRITES_ENABLED="false")
        if source.is_dir():
            environment["PYTHONPATH"] = str(source)
        else:
            environment.pop("PYTHONPATH", None)
        outsider = r"""
import sys
from pathlib import Path
from mark_api.backup_intent_authority import BackupIntentAuthority, BackupIntentError
intent, output, anchor, generation = sys.argv[1:]
operator = BackupIntentAuthority(Path(intent))
try:
    operator.acknowledge(generation, Path(output), Path(anchor))
except BackupIntentError as exc:
    if "another trusted seal or abandon" not in str(exc):
        raise
    print("CONCURRENT_ACK_DENIED")
else:
    raise AssertionError("external child stole the active sealer's ACK")
"""

        with patch(
            "mark_api.backup_intent_authority.seal_offline_pair",
            side_effect=pause_after_publishing,
        ):
            worker = Thread(target=owner_seal, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                self.assertFalse(
                    (self.intents / f"{GENERATION}.acknowledged.json").exists()
                )
                child = subprocess.run(
                    [
                        sys.executable, "-B", "-c", outsider,
                        str(self.intents), str(self.output), str(self.anchor),
                        GENERATION,
                    ],
                    cwd=self.cold.parent, env=environment,
                    capture_output=True, text=True, timeout=10, check=False,
                )
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertIn("CONCURRENT_ACK_DENIED", child.stdout)
                self.assertFalse(
                    (self.intents / f"{GENERATION}.acknowledged.json").exists()
                )
            finally:
                release.set()
                worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", sealed)
        self.assertTrue(sealed["result"]["external_operator_intent_acknowledged"])
        self.assertTrue((self.intents / f"{GENERATION}.acknowledged.json").is_file())


    @unittest.skipUnless(hasattr(os, "fork"), "POSIX fork required")
    def test_forked_child_never_inherits_parent_operation_lock_claim(self) -> None:
        # Fork only a fresh Python child; the unittest runner may have threads.
        # This synthetic check is no host-UID or historical-FD attestation.
        source = Path(__file__).resolve().parents[1] / "src"
        environment = dict(os.environ, MARK_PLATFORM_WRITES_ENABLED="false")
        if source.is_dir():
            environment["PYTHONPATH"] = str(source)
        else:
            environment.pop("PYTHONPATH", None)
        fork_probe = r"""
import os, tempfile
from pathlib import Path
from mark_api.backup_intent_authority import _operation_lock, BackupIntentError
with tempfile.TemporaryDirectory(prefix="mark-intent-fork-") as temporary:
    root = Path(temporary)
    with _operation_lock(root):
        child = os.fork()
        if child == 0:
            try:
                with _operation_lock(root):
                    os._exit(31)
            except BackupIntentError as exc:
                os._exit(0 if "another trusted seal or abandon" in str(exc) else 32)
        _, status = os.waitpid(child, 0)
        if os.waitstatus_to_exitcode(status) != 0:
            raise AssertionError("forked child bypassed parent operation lock")
        print("FORK_CHILD_ACK_DENIED")
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", fork_probe],
            cwd=self.cold.parent, env=environment,
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FORK_CHILD_ACK_DENIED", result.stdout)


    def test_external_begin_commit_must_not_preempt_active_seal(self) -> None:
        self.operator.prepare(
            GENERATION, self.sqlite, self.witness, previous_head_sha256=None,
        )
        entered, release = Event(), Event()
        observed: dict[str, object] = {}
        real_inspect = intent_module.inspect_offline_pair

        def pause_before_commit(*args: object, **kwargs: object) -> object:
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("synthetic seal did not resume")
            return real_inspect(*args, **kwargs)

        def seal() -> None:
            try:
                observed["result"] = self.operator.seal_prepared(
                    GENERATION, self.output, self.anchor,
                )
            except BaseException as exc:
                observed["error"] = exc

        with patch(
            "mark_api.backup_intent_authority.inspect_offline_pair",
            side_effect=pause_before_commit,
        ):
            worker = Thread(target=seal, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                self.assertTrue(
                    (self.intents / f"{GENERATION}.prepared.json").is_file()
                )
                with self.assertRaisesRegex(
                    BackupIntentError, "another trusted seal or abandon",
                ):
                    self.operator.begin_commit(GENERATION)
                source = Path(__file__).resolve().parents[1] / "src"
                environment = dict(os.environ, MARK_PLATFORM_WRITES_ENABLED="false")
                if source.is_dir():
                    environment["PYTHONPATH"] = str(source)
                else:
                    environment.pop("PYTHONPATH", None)
                external = r"""
import sys
from pathlib import Path
from mark_api.backup_intent_authority import BackupIntentAuthority, BackupIntentError
root, generation = sys.argv[1:]
try:
    BackupIntentAuthority(Path(root)).begin_commit(generation)
except BackupIntentError as exc:
    if "another trusted seal or abandon" not in str(exc):
        raise
    print("EXTERNAL_BEGIN_COMMIT_DENIED")
else:
    raise AssertionError("external operator stole the active seal's intent")
"""
                child = subprocess.run(
                    [
                        sys.executable, "-B", "-c", external,
                        str(self.intents), GENERATION,
                    ],
                    cwd=self.cold.parent, env=environment,
                    capture_output=True, text=True, timeout=10, check=False,
                )
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertIn("EXTERNAL_BEGIN_COMMIT_DENIED", child.stdout)
                self.assertFalse(
                    (self.intents / f"{GENERATION}.committing.json").exists()
                )
            finally:
                release.set()
                worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", observed)
        self.assertTrue(observed["result"]["external_operator_intent_acknowledged"])
        self.assertTrue((self.intents / f"{GENERATION}.acknowledged.json").is_file())
        with self.assertRaises(BackupIntentError):
            self.operator.begin_commit(GENERATION)

    def test_begin_commit_cannot_turn_active_abandon_into_conflicting_intents(self) -> None:
        self.operator.prepare(
            GENERATION, self.sqlite, self.witness, previous_head_sha256=None,
        )
        entered, release = Event(), Event()
        result: dict[str, object] = {}
        real_fsync = intent_module._fsync_dir

        def pause_after_abandon_proof(directory: Path) -> None:
            if directory == self.output:
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("synthetic abandon did not resume")
            real_fsync(directory)

        def abandon() -> None:
            try:
                result["value"] = self.operator.abandon(
                    GENERATION, self.output, self.anchor,
                )
            except BaseException as exc:
                result["error"] = exc

        with patch(
            "mark_api.backup_intent_authority._fsync_dir",
            side_effect=pause_after_abandon_proof,
        ):
            worker = Thread(target=abandon, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                # abandon already holds its short-lived record lock here.
                # The operation lock is an additional, not the only, fence.
                with self.assertRaises(BackupIntentError):
                    self.operator.begin_commit(GENERATION)
                self.assertFalse(
                    (self.intents / f"{GENERATION}.committing.json").exists()
                )
            finally:
                release.set()
                worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", result)
        self.assertTrue(result["value"]["abandoned_once"])
        self.assertTrue((self.intents / f"{GENERATION}.abandoned.json").is_file())
        self.assertFalse((self.intents / f"{GENERATION}.committing.json").exists())
        with self.assertRaises(BackupIntentError):
            self.operator.begin_commit(GENERATION)


if __name__ == "__main__":
    unittest.main()
