"""No live services or Kleinanzeigen access: cold pair sealing/rollback tests."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from mark_api.fence_witness import FenceLedger
from mark_api.paired_backup_operator import (
    PairedBackupOperatorError, seal_offline_pair,
)
from mark_api.paired_restore_preflight import verify_offline_pair
from mark_api.storage import SnapshotStore


SHA = "f" * 64
AT = datetime(2026, 10, 14, 17, 0, tzinfo=timezone.utc)
FIRST = "11111111111111111111111111111111"
SECOND = "22222222222222222222222222222222"


class PairedBackupOperatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mark-cold-pair-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / "operator-source"
        self.output = root / "private-generations"
        self.anchor = root / "separate-operator-anchors"
        for path in (self.source, self.output, self.anchor):
            path.mkdir(mode=0o700)
        self.sqlite = self.source / "mark.sqlite"
        self.witness = self.source / "witness.sqlite"
        SnapshotStore(self.sqlite)
        with FenceLedger(self.witness, initialize=True):
            pass

    def _hash(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _seal(
        self,
        *,
        generation: str = FIRST,
        previous: str | None = None,
        sqlite_sha: str | None = None,
        witness_sha: str | None = None,
    ) -> dict[str, object]:
        return seal_offline_pair(
            self.sqlite, self.witness, self.output, self.anchor,
            expected_sqlite_sha256=sqlite_sha or self._hash(self.sqlite),
            expected_witness_sha256=witness_sha or self._hash(self.witness),
            expected_previous_head_sha256=previous,
            generation=generation,
        )

    def _start_write(self) -> None:
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        claim = store.claim_write_api_request(
            idempotency_key="pair:operator:pending", request_sha256=SHA,
            claim_owner="owner-one", requested_at=AT,
        )
        self.assertTrue(claim.created)
        store.begin_write_api_request(
            idempotency_key="pair:operator:pending", request_sha256=SHA,
            claim_owner="owner-one", execution_started_at=AT,
            strict_owner_fence=True,
        )
        with FenceLedger(self.witness) as witness:
            sid = witness.open_session()
            witness.execution_started(
                sid, key="pair:operator:pending", sha=SHA, action="create",
            )
            witness.seal_session(sid)

    def test_first_generation_creates_independently_anchored_offline_pair(self) -> None:
        result = self._seal()
        generation = self.output / FIRST
        head = self.anchor / "latest.json"
        self.assertEqual(result["generation"], FIRST)
        self.assertEqual(result["head_sha256"], self._hash(head))
        self.assertEqual(result["manifest_sha256"], self._hash(generation / "manifest.json"))
        self.assertEqual((self.anchor / (FIRST + ".json")).read_bytes(), head.read_bytes())
        self.assertEqual((generation / "witness.sqlite.lock").stat().st_mode & 0o777, 0o600)
        self.assertFalse(result["restore_authorized"])
        self.assertFalse(result["live_source_quiescence_attested"])
        self.assertFalse(result["external_write_authorized"])
        self.assertTrue(result["offline_only"])
        inspected = verify_offline_pair(
            generation / "mark.sqlite", generation / "witness.sqlite",
            generation / "manifest.json",
            expected_manifest_sha256=result["manifest_sha256"],
        )
        self.assertEqual(inspected["generation"], FIRST)
        self.assertEqual(inspected["sqlite_started_count"], 0)
        self.assertFalse(inspected["restore_authorized"])

    def test_later_generation_keeps_real_open_sqlite_witness_fences(self) -> None:
        first = self._seal()
        self._start_write()
        second = self._seal(generation=SECOND, previous=first["head_sha256"])
        self.assertEqual(
            second["fences"]["sqlite_started_count"], 1,
        )
        self.assertEqual(
            second["fences"]["witness_unresolved_count"], 1,
        )
        self.assertEqual(second["fences"]["sqlite_unverified_replay_count"], 0)
        self.assertEqual(
            json.loads((self.anchor / "latest.json").read_bytes())["predecessor_sha256"],
            first["head_sha256"],
        )
        self.assertEqual(
            self._hash(self.anchor / (FIRST + ".json")),
            first["head_sha256"],
        )

    def test_published_inode_swap_while_preparing_head_cannot_publish_head(self) -> None:
        from mark_api import paired_backup_operator as operator

        real_new = operator._new_file

        def swap_before_retained_record(path: Path, data: bytes) -> None:
            if path == self.anchor / (FIRST + ".json"):
                old = self.output / FIRST / "mark.sqlite"
                replacement = self.output / FIRST / "replacement-image"
                replacement.write_bytes(old.read_bytes())
                replacement.chmod(0o600)
                os.replace(replacement, old)
            real_new(path, data)

        with patch("mark_api.paired_backup_operator._new_file",
                   side_effect=swap_before_retained_record):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal()
        self.assertFalse((self.anchor / "latest.json").exists())
        self.assertTrue((self.anchor / ".inflight").is_file())
        self.assertTrue((self.anchor / (FIRST + ".json")).is_file())

    def test_published_image_edit_while_preparing_head_cannot_publish_head(self) -> None:
        from mark_api import paired_backup_operator as operator

        real_new = operator._new_file

        def alter_before_retained_record(path: Path, data: bytes) -> None:
            if path == self.anchor / (FIRST + ".json"):
                published = self.output / FIRST / "witness.sqlite"
                altered = bytearray(published.read_bytes())
                altered[-1] ^= 1
                published.write_bytes(altered)
            real_new(path, data)

        with patch("mark_api.paired_backup_operator._new_file",
                   side_effect=alter_before_retained_record):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal()
        self.assertFalse((self.anchor / "latest.json").exists())
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_published_manifest_swap_while_preparing_head_cannot_publish_head(self) -> None:
        from mark_api import paired_backup_operator as operator

        real_new = operator._new_file

        def swap_manifest_before_retained_record(path: Path, data: bytes) -> None:
            if path == self.anchor / (FIRST + ".json"):
                old = self.output / FIRST / "manifest.json"
                replacement = self.output / FIRST / "replacement-manifest"
                replacement.write_bytes(old.read_bytes())
                replacement.chmod(0o600)
                os.replace(replacement, old)
            real_new(path, data)

        with patch("mark_api.paired_backup_operator._new_file",
                   side_effect=swap_manifest_before_retained_record):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal()
        self.assertFalse((self.anchor / "latest.json").exists())
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_unlinked_new_pair_sidecar_must_not_be_anchored(self) -> None:
        from mark_api import paired_backup_operator as operator

        original_verify = operator.verify_offline_pair

        def add_sidecar_after_verified(*args: object, **kwargs: object) -> object:
            checked = original_verify(*args, **kwargs)
            (self.output / FIRST / "mark.sqlite-wal").write_bytes(b"untrusted")
            return checked

        with patch(
            "mark_api.paired_backup_operator.verify_offline_pair",
            side_effect=add_sidecar_after_verified,
        ):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal()
        self.assertFalse((self.anchor / "latest.json").exists())
        self.assertFalse((self.anchor / (FIRST + ".json")).exists())
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_unlinked_new_pair_missing_lock_must_not_be_anchored(self) -> None:
        from mark_api import paired_backup_operator as operator

        original_verify = operator.verify_offline_pair

        def remove_lock_after_verified(*args: object, **kwargs: object) -> object:
            checked = original_verify(*args, **kwargs)
            (self.output / FIRST / "witness.sqlite.lock").unlink()
            return checked

        with patch(
            "mark_api.paired_backup_operator.verify_offline_pair",
            side_effect=remove_lock_after_verified,
        ):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal()
        self.assertFalse((self.anchor / "latest.json").exists())
        self.assertFalse((self.anchor / (FIRST + ".json")).exists())
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_bootstrap_refuses_orphan_output_injected_after_pair_verification(self) -> None:
        from mark_api import paired_backup_operator as operator

        original_verify = operator.verify_offline_pair

        def inject_orphan(*args: object, **kwargs: object) -> object:
            checked = original_verify(*args, **kwargs)
            (self.output / SECOND).mkdir(mode=0o700)
            return checked

        with patch(
            "mark_api.paired_backup_operator.verify_offline_pair",
            side_effect=inject_orphan,
        ):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal()
        self.assertFalse((self.anchor / "latest.json").exists())
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_bootstrap_refuses_orphan_anchor_injected_after_pair_verification(self) -> None:
        from mark_api import paired_backup_operator as operator

        original_verify = operator.verify_offline_pair

        def inject_anchor(*args: object, **kwargs: object) -> object:
            checked = original_verify(*args, **kwargs)
            (self.anchor / (SECOND + ".json")).write_bytes(b"unattested")
            return checked

        with patch(
            "mark_api.paired_backup_operator.verify_offline_pair",
            side_effect=inject_anchor,
        ):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal()
        self.assertFalse((self.anchor / "latest.json").exists())
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_later_seal_refuses_previous_head_modified_after_verification(self) -> None:
        from mark_api import paired_backup_operator as operator

        first = self._seal()
        original_verify = operator.verify_offline_pair

        def change_previous_head(*args: object, **kwargs: object) -> object:
            checked = original_verify(*args, **kwargs)
            if args[0] == self.output / SECOND / "mark.sqlite":
                (self.anchor / "latest.json").write_bytes(b"{}")
            return checked

        with patch(
            "mark_api.paired_backup_operator.verify_offline_pair",
            side_effect=change_previous_head,
        ):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal(generation=SECOND, previous=first["head_sha256"])
        self.assertEqual((self.anchor / "latest.json").read_bytes(), b"{}")
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_reconcile_refuses_head_changed_after_last_pair_verification(self) -> None:
        from mark_api import paired_backup_operator as operator
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        digest = self._crash_after_durable_head()
        original_verify = operator.verify_offline_pair
        call_count = 0

        def replace_after_final_check(*args: object, **kwargs: object) -> object:
            nonlocal call_count
            checked = original_verify(*args, **kwargs)
            call_count += 1
            if call_count == 2:
                (self.anchor / "latest.json").write_bytes(b"{}")
            return checked

        with patch(
            "mark_api.paired_backup_operator.verify_offline_pair",
            side_effect=replace_after_final_check,
        ):
            with self.assertRaises(PairedBackupOperatorError):
                reconcile_committed_inflight(
                    self.output, self.anchor,
                    expected_current_head_sha256=digest,
                    expected_generation=FIRST,
                    expected_previous_head_sha256=None,
                    expected_sqlite_sha256=self._hash(self.sqlite),
                    expected_witness_sha256=self._hash(self.witness),
                )
        self.assertGreaterEqual(call_count, 2)
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_next_head_refuses_ancestor_deleted_after_first_validation(self) -> None:
        from mark_api import paired_backup_operator as operator

        first = self._seal()
        original_verify = operator.verify_offline_pair

        def remove_ancestor_after_new_pair_verified(*args: object, **kwargs: object) -> object:
            inspected = original_verify(*args, **kwargs)
            if args[0] == self.output / SECOND / "mark.sqlite":
                (self.output / FIRST / "manifest.json").unlink()
            return inspected

        with patch(
            "mark_api.paired_backup_operator.verify_offline_pair",
            side_effect=remove_ancestor_after_new_pair_verified,
        ):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal(generation=SECOND, previous=first["head_sha256"])
        self.assertEqual(self._hash(self.anchor / "latest.json"), first["head_sha256"])
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_reconcile_keeps_intent_after_verified_image_is_removed(self) -> None:
        from mark_api import paired_backup_operator as operator
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        current = self._crash_after_durable_head()
        original_verify = operator.verify_offline_pair

        def remove_after_verification(*args: object, **kwargs: object) -> object:
            inspected = original_verify(*args, **kwargs)
            (self.output / FIRST / "witness.sqlite").unlink()
            return inspected

        with patch(
            "mark_api.paired_backup_operator.verify_offline_pair",
            side_effect=remove_after_verification,
        ):
            with self.assertRaises(PairedBackupOperatorError):
                reconcile_committed_inflight(
                    self.output, self.anchor,
                    expected_current_head_sha256=current,
                    expected_generation=FIRST,
                    expected_previous_head_sha256=None,
                    expected_sqlite_sha256=self._hash(self.sqlite),
                    expected_witness_sha256=self._hash(self.witness),
                )
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_reconcile_keeps_intent_after_verified_image_is_mutated(self) -> None:
        from mark_api import paired_backup_operator as operator
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        current = self._crash_after_durable_head()
        original_verify = operator.verify_offline_pair
        tampered = False

        def modify_after_verification(*args: object, **kwargs: object) -> object:
            nonlocal tampered
            inspected = original_verify(*args, **kwargs)
            if not tampered:
                tampered = True
                with sqlite3.connect(self.output / FIRST / "mark.sqlite") as db:
                    db.execute("PRAGMA user_version=99")
            return inspected

        with patch(
            "mark_api.paired_backup_operator.verify_offline_pair",
            side_effect=modify_after_verification,
        ):
            with self.assertRaises(PairedBackupOperatorError):
                reconcile_committed_inflight(
                    self.output, self.anchor,
                    expected_current_head_sha256=current,
                    expected_generation=FIRST,
                    expected_previous_head_sha256=None,
                    expected_sqlite_sha256=self._hash(self.sqlite),
                    expected_witness_sha256=self._hash(self.witness),
                )
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_next_generation_rejects_missing_committed_predecessor(self) -> None:
        first = self._seal()
        shutil.rmtree(self.output / FIRST)
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(generation=SECOND, previous=first["head_sha256"])
        self.assertFalse((self.output / SECOND).exists())
        self.assertEqual(self._hash(self.anchor / "latest.json"), first["head_sha256"])

    def test_next_generation_rejects_missing_retained_manifest(self) -> None:
        first = self._seal()
        (self.output / FIRST / "manifest.json").unlink()
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(generation=SECOND, previous=first["head_sha256"])
        self.assertFalse((self.output / SECOND).exists())

    def test_next_generation_rejects_missing_retained_sqlite(self) -> None:
        first = self._seal()
        (self.output / FIRST / "mark.sqlite").unlink()
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(generation=SECOND, previous=first["head_sha256"])
        self.assertFalse((self.output / SECOND).exists())

    def test_next_generation_rejects_missing_retained_witness(self) -> None:
        first = self._seal()
        (self.output / FIRST / "witness.sqlite").unlink()
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(generation=SECOND, previous=first["head_sha256"])
        self.assertFalse((self.output / SECOND).exists())

    def test_next_generation_rejects_missing_retained_lock(self) -> None:
        first = self._seal()
        (self.output / FIRST / "witness.sqlite.lock").unlink()
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(generation=SECOND, previous=first["head_sha256"])
        self.assertFalse((self.output / SECOND).exists())

    def test_next_generation_rejects_modified_retained_manifest(self) -> None:
        first = self._seal()
        (self.output / FIRST / "manifest.json").write_bytes(b"{}")
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(generation=SECOND, previous=first["head_sha256"])
        self.assertFalse((self.output / SECOND).exists())

    def test_next_generation_rejects_extra_retained_artifact(self) -> None:
        first = self._seal()
        (self.output / FIRST / "unpublished.tmp").write_bytes(b"")
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(generation=SECOND, previous=first["head_sha256"])
        self.assertFalse((self.output / SECOND).exists())

    def test_next_generation_rejects_missing_older_manifest(self) -> None:
        first = self._seal()
        second = self._seal(generation=SECOND, previous=first["head_sha256"])
        (self.output / FIRST / "manifest.json").unlink()
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(generation="3" * 32, previous=second["head_sha256"])
        self.assertFalse((self.output / ("3" * 32)).exists())

    def test_next_generation_rejects_missing_older_generation(self) -> None:
        first = self._seal()
        second = self._seal(generation=SECOND, previous=first["head_sha256"])
        shutil.rmtree(self.output / FIRST)
        third = "3" * 32
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(generation=third, previous=second["head_sha256"])
        self.assertFalse((self.output / third).exists())
        self.assertEqual(self._hash(self.anchor / "latest.json"), second["head_sha256"])

    def test_next_generation_rejects_unlinked_artifacts(self) -> None:
        first = self._seal()
        unrelated = "3" * 32
        orphan_dir = self.output / unrelated
        orphan_dir.mkdir(mode=0o700)
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(generation=SECOND, previous=first["head_sha256"])
        orphan_dir.rmdir()
        (self.anchor / (unrelated + ".json")).write_bytes(
            (self.anchor / (FIRST + ".json")).read_bytes()
        )
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(generation=SECOND, previous=first["head_sha256"])
        self.assertFalse((self.output / SECOND).exists())
        self.assertEqual(self._hash(self.anchor / "latest.json"), first["head_sha256"])

    def test_missing_or_stale_previous_anchor_never_publishes(self) -> None:
        first = self._seal()
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(generation=SECOND)
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(generation=SECOND, previous="0" * 64)
        self.assertFalse((self.output / SECOND).exists())
        self.assertEqual(self._hash(self.anchor / "latest.json"), first["head_sha256"])

    def test_replayed_old_head_rejected_against_latest_external_digest(self) -> None:
        first = self._seal()
        second = self._seal(generation=SECOND, previous=first["head_sha256"])
        (self.anchor / "latest.json").write_bytes(
            (self.anchor / (FIRST + ".json")).read_bytes()
        )
        with self.assertRaises(PairedBackupOperatorError):
            seal_offline_pair(
                self.sqlite, self.witness, self.output, self.anchor,
                expected_sqlite_sha256=self._hash(self.sqlite),
                expected_witness_sha256=self._hash(self.witness),
                expected_previous_head_sha256=second["head_sha256"],
                generation="3" * 32,
            )

    def test_missing_witness_lock_never_attests_quiescence(self) -> None:
        self.witness.with_name(self.witness.name + ".lock").unlink()
        with self.assertRaises(PairedBackupOperatorError):
            self._seal()
        self.assertFalse((self.output / FIRST).exists())

    def test_running_witness_refused(self) -> None:
        with FenceLedger(self.witness):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal()
        self.assertFalse((self.output / FIRST).exists())

    def test_sidecars_refused_even_with_valid_source_sha(self) -> None:
        sidecar = self.sqlite.with_name(self.sqlite.name + "-wal")
        sidecar.write_bytes(b"untrusted")
        with self.assertRaises(PairedBackupOperatorError):
            self._seal()
        self.assertFalse((self.output / FIRST).exists())

    def test_mutated_source_cannot_be_signed_using_stale_anchor(self) -> None:
        pinned = self._hash(self.sqlite)
        with sqlite3.connect(self.sqlite) as db:
            db.execute("PRAGMA user_version=77")
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(sqlite_sha=pinned)
        self.assertFalse((self.output / FIRST).exists())

    def test_rejects_private_source_substitution_and_untrusted_schema(self) -> None:
        with sqlite3.connect(self.witness) as db:
            db.execute("DROP TRIGGER events_no_delete")
        with self.assertRaises(PairedBackupOperatorError):
            self._seal()
        self.assertFalse((self.output / FIRST).exists())

    def test_source_symlink_cannot_be_sealed(self) -> None:
        different = self.source / "substituted.sqlite"
        self.sqlite.rename(different)
        self.sqlite.symlink_to(different)
        with self.assertRaises(PairedBackupOperatorError):
            self._seal()
        self.assertFalse((self.output / FIRST).exists())

    def test_after_uncertain_head_update_never_retries_same_generation(self) -> None:
        with patch("mark_api.paired_backup_operator.os.replace",
                   side_effect=OSError("synthetic late failure")):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal()
        # Both images are published, but there is no final HEAD: the
        # result is UNKNOWN, not a successful backup or restore authority.
        self.assertTrue((self.output / FIRST / "manifest.json").exists())
        self.assertFalse((self.anchor / "latest.json").exists())
        with self.assertRaises(PairedBackupOperatorError):
            self._seal()
        self.assertFalse((self.anchor / "latest.json").exists())

    def test_uncertain_first_head_cannot_be_silently_rebased(self) -> None:
        # A killed first-generation publish can leave both cold images and
        # a retained immutable record but no current HEAD. Another distinct
        # generation must NOT quietly bootstrap over that unresolved state.
        with patch("mark_api.paired_backup_operator.os.replace",
                   side_effect=OSError("synthetic late crash")):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal()
        self.assertFalse((self.anchor / "latest.json").exists())
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(generation=SECOND)

    def test_crashed_second_generation_cannot_be_discarded_by_third(self) -> None:
        # A completed first HEAD does not make a *second* generation safe
        # when its cold directory has been published but latest.json was not
        # advanced. A third generation must refuse rather than chain directly
        # to the first and lose the unanchored intermediate checkpoint.
        first = self._seal()
        from mark_api import paired_backup_operator as operator
        real_new_file = operator._new_file

        def fail_immutable_record(path: Path, payload: bytes) -> None:
            if path == self.anchor / (SECOND + ".json"):
                raise OSError("synthetic crash before immutable head record")
            real_new_file(path, payload)

        with patch(
            "mark_api.paired_backup_operator._new_file",
            side_effect=fail_immutable_record,
        ):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal(
                    generation=SECOND, previous=first["head_sha256"],
                )
        self.assertTrue((self.output / SECOND / "manifest.json").exists())
        self.assertEqual(
            self._hash(self.anchor / "latest.json"), first["head_sha256"],
        )
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(
                generation="3" * 32, previous=first["head_sha256"],
            )

    def _crash_after_durable_head(self) -> str:
        # A real published pair and HEAD exist, but fsync of the final
        # operator intent clear was interrupted. The fence must remain.
        from mark_api import paired_backup_operator as operator

        real_unlink = operator.os.unlink

        def interrupt_final_fence_unlink(path: object, *args: object, **kwargs: object) -> None:
            if path == self.anchor / ".inflight":
                raise OSError("synthetic SIGKILL after durable HEAD")
            real_unlink(path, *args, **kwargs)

        with patch(
            "mark_api.paired_backup_operator.os.unlink",
            side_effect=interrupt_final_fence_unlink,
        ):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal()
        self.assertTrue((self.anchor / ".inflight").is_file())
        self.assertTrue((self.output / FIRST / "manifest.json").is_file())
        return self._hash(self.anchor / "latest.json")

    def test_reconcile_cannot_attest_forged_pair_from_self_hashed_head(self) -> None:
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight
        from mark_api.paired_restore_preflight import _canonical, inspect_offline_pair

        attested_sqlite = self._hash(self.sqlite)
        attested_witness = self._hash(self.witness)
        self._crash_after_durable_head()

        # A same-UID actor prepares a separately valid but *different* pair,
        # then forges the manifest and the matching local head hash. The
        # operator still knows the originally attested source image hashes.
        self._start_write()
        for name in ("mark.sqlite", "witness.sqlite"):
            (self.output / FIRST / name).write_bytes((self.source / name).read_bytes())
        backup_sqlite = self.output / FIRST / "mark.sqlite"
        backup_witness = self.output / FIRST / "witness.sqlite"
        self.assertNotEqual(self._hash(backup_sqlite), attested_sqlite)
        observed = inspect_offline_pair(
            backup_sqlite, backup_witness,
            expected_sqlite_sha256=self._hash(backup_sqlite),
            expected_witness_sha256=self._hash(backup_witness),
        )
        manifest = _canonical({
            "schema_version": 5, "generation": FIRST, **observed,
        })
        (self.output / FIRST / "manifest.json").write_bytes(manifest)
        replacement_head = _canonical({
            "schema_version": 1, "generation": FIRST,
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "predecessor_sha256": "0" * 64,
        })
        (self.anchor / (FIRST + ".json")).write_bytes(replacement_head)
        (self.anchor / "latest.json").write_bytes(replacement_head)
        attacker_head_sha = self._hash(self.anchor / "latest.json")
        with self.assertRaises(PairedBackupOperatorError):
            reconcile_committed_inflight(
                self.output, self.anchor,
                expected_current_head_sha256=attacker_head_sha,
                expected_generation=FIRST,
                expected_previous_head_sha256=None,
                expected_sqlite_sha256=attested_sqlite,
                expected_witness_sha256=attested_witness,
            )
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_reconcile_cannot_reclear_previously_acknowledged_head(self) -> None:
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        previous = self._seal()
        marker = self.anchor / ".inflight"
        marker.write_bytes(FIRST.encode("ascii"))
        marker.chmod(0o600)
        with self.assertRaises(PairedBackupOperatorError):
            reconcile_committed_inflight(
                self.output, self.anchor,
                expected_current_head_sha256=previous["head_sha256"],
                expected_generation=FIRST,
                expected_previous_head_sha256=previous["head_sha256"],
                expected_sqlite_sha256=self._hash(self.sqlite),
                expected_witness_sha256=self._hash(self.witness),
            )
        self.assertTrue(marker.is_file())

    def test_reconcile_only_fully_anchored_post_head_crash(self) -> None:
        # RED: without an explicit pinned-HEAD recovery primitive a
        # successful HEAD followed by a crashed sentinel unlink deadlocks
        # all future generations. Never remove an incomplete generation.
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        digest = self._crash_after_durable_head()
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(generation=SECOND, previous=digest)
        recovered = reconcile_committed_inflight(
            self.output, self.anchor,
            expected_current_head_sha256=digest, expected_generation=FIRST,
            expected_previous_head_sha256=None,
            expected_sqlite_sha256=self._hash(self.sqlite),
            expected_witness_sha256=self._hash(self.witness),
        )
        self.assertTrue(recovered["reconciled"])
        self.assertEqual(recovered["head_sha256"], digest)
        self.assertFalse(recovered["restore_authorized"])
        self.assertFalse(recovered["live_source_quiescence_attested"])
        self.assertFalse(recovered["external_write_authorized"])
        self.assertFalse((self.anchor / ".inflight").exists())
        self.assertEqual(self._hash(self.anchor / "latest.json"), digest)
        second = self._seal(generation=SECOND, previous=digest)
        self.assertEqual(second["generation"], SECOND)

    def test_reconcile_rejects_pre_head_crash_and_does_not_skip_generation(self) -> None:
        from mark_api import paired_backup_operator as operator
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        first = self._seal()
        original = operator._new_file

        def abort_before_head_record(path: Path, data: bytes) -> None:
            if path == self.anchor / (SECOND + ".json"):
                raise OSError("synthetic interruption before immutable head")
            original(path, data)

        with patch(
            "mark_api.paired_backup_operator._new_file",
            side_effect=abort_before_head_record,
        ):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal(generation=SECOND, previous=first["head_sha256"])
        with self.assertRaises(PairedBackupOperatorError):
            reconcile_committed_inflight(
                self.output, self.anchor,
                expected_current_head_sha256=first["head_sha256"],
                expected_generation=SECOND,
                expected_previous_head_sha256=first["head_sha256"],
                expected_sqlite_sha256=self._hash(self.sqlite),
                expected_witness_sha256=self._hash(self.witness),
            )
        self.assertEqual((self.anchor / ".inflight").read_text(), SECOND)
        self.assertEqual(
            self._hash(self.anchor / "latest.json"), first["head_sha256"],
        )
        with self.assertRaises(PairedBackupOperatorError):
            self._seal(generation="3" * 32, previous=first["head_sha256"])

    def test_reconcile_rejects_wrong_external_head_and_tampered_pair(self) -> None:
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        digest = self._crash_after_durable_head()
        with self.assertRaises(PairedBackupOperatorError):
            reconcile_committed_inflight(
                self.output, self.anchor,
                expected_current_head_sha256="0" * 64,
                expected_generation=FIRST,
                expected_previous_head_sha256=None,
                expected_sqlite_sha256=self._hash(self.sqlite),
                expected_witness_sha256=self._hash(self.witness),
            )
        with sqlite3.connect(self.output / FIRST / "mark.sqlite") as db:
            db.execute("PRAGMA user_version=91")
        with self.assertRaises(PairedBackupOperatorError):
            reconcile_committed_inflight(
                self.output, self.anchor,
                expected_current_head_sha256=digest,
                expected_generation=FIRST,
                expected_previous_head_sha256=None,
                expected_sqlite_sha256=self._hash(self.sqlite),
                expected_witness_sha256=self._hash(self.witness),
            )
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_reconcile_refuses_unexpected_staging_artifacts(self) -> None:
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        digest = self._crash_after_durable_head()
        next_head = self.anchor / (".next-" + SECOND)
        next_head.write_bytes(b"unattested")
        with self.assertRaises(PairedBackupOperatorError):
            reconcile_committed_inflight(
                self.output, self.anchor,
                expected_current_head_sha256=digest,
                expected_generation=FIRST,
                expected_previous_head_sha256=None,
                expected_sqlite_sha256=self._hash(self.sqlite),
                expected_witness_sha256=self._hash(self.witness),
            )
        self.assertTrue((self.anchor / ".inflight").is_file())
        next_head.unlink()
        partial = self.output / (".partial-" + SECOND)
        partial.mkdir(mode=0o700)
        with self.assertRaises(PairedBackupOperatorError):
            reconcile_committed_inflight(
                self.output, self.anchor,
                expected_current_head_sha256=digest,
                expected_generation=FIRST,
                expected_previous_head_sha256=None,
                expected_sqlite_sha256=self._hash(self.sqlite),
                expected_witness_sha256=self._hash(self.witness),
            )
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_reconcile_refuses_symlinked_or_missing_intent_marker(self) -> None:
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        digest = self._crash_after_durable_head()
        marker = self.anchor / ".inflight"
        marker.unlink()
        target = self.anchor / "foreign"
        target.write_text(FIRST)
        marker.symlink_to(target)
        with self.assertRaises(PairedBackupOperatorError):
            reconcile_committed_inflight(
                self.output, self.anchor,
                expected_current_head_sha256=digest,
                expected_generation=FIRST,
                expected_previous_head_sha256=None,
                expected_sqlite_sha256=self._hash(self.sqlite),
                expected_witness_sha256=self._hash(self.witness),
            )
        marker.unlink()
        with self.assertRaises(PairedBackupOperatorError):
            reconcile_committed_inflight(
                self.output, self.anchor,
                expected_current_head_sha256=digest,
                expected_generation=FIRST,
                expected_previous_head_sha256=None,
                expected_sqlite_sha256=self._hash(self.sqlite),
                expected_witness_sha256=self._hash(self.witness),
            )

    def test_reconcile_does_not_claim_success_without_crashed_intent(self) -> None:
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        first = self._seal()
        with self.assertRaises(PairedBackupOperatorError):
            reconcile_committed_inflight(
                self.output, self.anchor,
                expected_current_head_sha256=first["head_sha256"],
                expected_generation=FIRST,
                expected_previous_head_sha256=None,
                expected_sqlite_sha256=self._hash(self.sqlite),
                expected_witness_sha256=self._hash(self.witness),
            )

    def test_reconcile_refuses_orphaned_published_pair_after_durable_head(self) -> None:
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        digest = self._crash_after_durable_head()
        orphan = self.output / SECOND
        orphan.mkdir(mode=0o700)
        with self.assertRaises(PairedBackupOperatorError):
            reconcile_committed_inflight(
                self.output, self.anchor,
                expected_current_head_sha256=digest,
                expected_generation=FIRST,
                expected_previous_head_sha256=None,
                expected_sqlite_sha256=self._hash(self.sqlite),
                expected_witness_sha256=self._hash(self.witness),
            )
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_reconcile_refuses_damaged_predecessor_in_durable_head_chain(self) -> None:
        from mark_api import paired_backup_operator as operator
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        first = self._seal()
        original_unlink = operator.os.unlink

        def interrupted_second(path: object, *args: object, **kwargs: object) -> None:
            if path == self.anchor / ".inflight":
                raise OSError("synthetic post-head crash")
            original_unlink(path, *args, **kwargs)

        with patch("mark_api.paired_backup_operator.os.unlink", side_effect=interrupted_second):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal(generation=SECOND, previous=first["head_sha256"])
        committed = self._hash(self.anchor / "latest.json")
        # The newest pair passes verification, but an older immutable head
        # is now corrupt. The operator must not erase evidence of lost history.
        (self.anchor / (FIRST + ".json")).write_text("corrupt")
        with self.assertRaises(PairedBackupOperatorError):
            reconcile_committed_inflight(
                self.output, self.anchor,
                expected_current_head_sha256=committed,
                expected_generation=SECOND,
                expected_previous_head_sha256=first["head_sha256"],
                expected_sqlite_sha256=self._hash(self.sqlite),
                expected_witness_sha256=self._hash(self.witness),
            )
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_reconcile_rejects_corrupted_predecessor_pair_image(self) -> None:
        from mark_api import paired_backup_operator as operator
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        first = self._seal()
        original_unlink = operator.os.unlink

        def interrupted_second(path: object, *args: object, **kwargs: object) -> None:
            if path == self.anchor / ".inflight":
                raise OSError("synthetic late crash")
            original_unlink(path, *args, **kwargs)

        with patch("mark_api.paired_backup_operator.os.unlink", side_effect=interrupted_second):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal(generation=SECOND, previous=first["head_sha256"])
        committed = self._hash(self.anchor / "latest.json")
        # Its current HEAD is still fully valid; corrupting an older
        # generation must not allow silent release of the incomplete chain.
        with sqlite3.connect(self.output / FIRST / "mark.sqlite") as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaises(PairedBackupOperatorError):
            reconcile_committed_inflight(
                self.output, self.anchor,
                expected_current_head_sha256=committed,
                expected_generation=SECOND,
                expected_previous_head_sha256=first["head_sha256"],
                expected_sqlite_sha256=self._hash(self.sqlite),
                expected_witness_sha256=self._hash(self.witness),
            )
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_reconcile_rejects_wrong_inflight_generation_bytes(self) -> None:
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        digest = self._crash_after_durable_head()
        (self.anchor / ".inflight").write_bytes(SECOND.encode("ascii"))
        with self.assertRaises(PairedBackupOperatorError):
            reconcile_committed_inflight(
                self.output, self.anchor,
                expected_current_head_sha256=digest,
                expected_generation=FIRST,
                expected_previous_head_sha256=None,
                expected_sqlite_sha256=self._hash(self.sqlite),
                expected_witness_sha256=self._hash(self.witness),
            )
        self.assertTrue((self.anchor / ".inflight").exists())

    def test_reconcile_respects_concurrent_authority_lock(self) -> None:
        import fcntl
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        digest = self._crash_after_durable_head()
        with (self.anchor / ".authority.lock").open("rb") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaises(PairedBackupOperatorError):
                    reconcile_committed_inflight(
                        self.output, self.anchor,
                        expected_current_head_sha256=digest,
                        expected_generation=FIRST,
                        expected_previous_head_sha256=None,
                        expected_sqlite_sha256=self._hash(self.sqlite),
                        expected_witness_sha256=self._hash(self.witness),
                    )
            finally:
                fcntl.flock(guard, fcntl.LOCK_UN)
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_reconcile_must_not_recreate_missing_operator_authority_lock(self) -> None:
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        digest = self._crash_after_durable_head()
        lock_path = self.anchor / ".authority.lock"
        lock_path.unlink()
        with self.assertRaises(PairedBackupOperatorError):
            reconcile_committed_inflight(
                self.output, self.anchor,
                expected_current_head_sha256=digest,
                expected_generation=FIRST,
                expected_previous_head_sha256=None,
                expected_sqlite_sha256=self._hash(self.sqlite),
                expected_witness_sha256=self._hash(self.witness),
            )
        self.assertFalse(lock_path.exists())
        self.assertTrue((self.anchor / ".inflight").is_file())

    def test_post_head_pre_fsync_crash_keeps_intent_if_reconcile_fsync_fails(self) -> None:
        from mark_api import paired_backup_operator as operator
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        original_fsync = operator._fsync_dir

        def crash_before_head_durability(directory: Path) -> None:
            # The HEAD inode was fsynced via _new_file but its directory
            # rename has not been durably persisted. Preserve the marker.
            if (
                directory == self.anchor
                and (self.anchor / "latest.json").exists()
                and (self.anchor / ".inflight").exists()
            ):
                raise OSError("simulated crash before HEAD directory fsync")
            original_fsync(directory)

        with patch(
            "mark_api.paired_backup_operator._fsync_dir",
            side_effect=crash_before_head_durability,
        ):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal()
        digest = self._hash(self.anchor / "latest.json")
        marker = self.anchor / ".inflight"
        self.assertTrue(marker.is_file())

        def fail_anchor_fsync(directory: Path) -> None:
            if directory == self.anchor:
                raise OSError("simulated durability barrier failure")
            original_fsync(directory)

        with patch(
            "mark_api.paired_backup_operator._fsync_dir",
            side_effect=fail_anchor_fsync,
        ):
            with self.assertRaises(PairedBackupOperatorError):
                reconcile_committed_inflight(
                    self.output, self.anchor,
                    expected_current_head_sha256=digest,
                    expected_generation=FIRST,
                    expected_previous_head_sha256=None,
                    expected_sqlite_sha256=self._hash(self.sqlite),
                    expected_witness_sha256=self._hash(self.witness),
                )
        # A failed first durability barrier must NOT consume the only
        # surviving intent marker. Without this, HEAD can be lost at poweroff.
        self.assertTrue(marker.is_file())
        result = reconcile_committed_inflight(
            self.output, self.anchor,
            expected_current_head_sha256=digest,
            expected_generation=FIRST,
            expected_previous_head_sha256=None,
            expected_sqlite_sha256=self._hash(self.sqlite),
            expected_witness_sha256=self._hash(self.witness),
        )
        self.assertTrue(result["reconciled"])
        self.assertFalse(marker.exists())

    def test_reconcile_success_keeps_pending_write_fences(self) -> None:
        from mark_api import paired_backup_operator as operator
        from mark_api.paired_backup_operator import _reconcile_committed_inflight as reconcile_committed_inflight

        first = self._seal()
        self._start_write()
        original_unlink = operator.os.unlink

        def interrupted_second(path: object, *args: object, **kwargs: object) -> None:
            if path == self.anchor / ".inflight":
                raise OSError("synthetic post-head crash with open write")
            original_unlink(path, *args, **kwargs)

        with patch("mark_api.paired_backup_operator.os.unlink", side_effect=interrupted_second):
            with self.assertRaises(PairedBackupOperatorError):
                self._seal(generation=SECOND, previous=first["head_sha256"])
        committed = self._hash(self.anchor / "latest.json")
        result = reconcile_committed_inflight(
            self.output, self.anchor,
            expected_current_head_sha256=committed,
            expected_generation=SECOND,
            expected_previous_head_sha256=first["head_sha256"],
            expected_sqlite_sha256=self._hash(self.sqlite),
            expected_witness_sha256=self._hash(self.witness),
        )
        self.assertEqual(result["fences"]["sqlite_started_count"], 1)
        self.assertEqual(result["fences"]["witness_unresolved_count"], 1)
        self.assertFalse(result["restore_authorized"])
        self.assertFalse(result["external_write_authorized"])


    def test_successful_second_generation_allows_third_after_sealing(self) -> None:
        first = self._seal()
        second = self._seal(generation=SECOND, previous=first["head_sha256"])
        third = self._seal(generation="3" * 32, previous=second["head_sha256"])
        self.assertEqual(third["generation"], "3" * 32)

    def test_predirtied_shared_mapping_rejects_stat_invariant_source_edit(self) -> None:
        # Real Linux MAP_SHARED pages, not a mocked byte change. A page
        # dirtied before stat attestation can be rewritten without updating
        # mtime/ctime. The independent source SHA must still defeat it.
        import mmap
        import sys

        if not sys.platform.startswith("linux"):
            self.skipTest("requires Linux MAP_SHARED")
        with self.sqlite.open("r+b") as source:
            image = mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_WRITE)
            try:
                previous = image[63]
                image[63] = previous ^ 1
                image[63] = previous
                before = self.sqlite.stat()
                pinned_sha = self._hash(self.sqlite)
                image[63] = previous ^ 1
                after = self.sqlite.stat()
                if (
                    before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns
                ):
                    self.skipTest("filesystem does not retain predirtied timestamp")
                with self.assertRaises(PairedBackupOperatorError):
                    self._seal(sqlite_sha=pinned_sha)
                self.assertFalse((self.output / FIRST).exists())
            finally:
                image.close()

    def test_bad_root_permissions_or_nested_roots_rejected(self) -> None:
        os.chmod(self.anchor, 0o770)
        with self.assertRaises(PairedBackupOperatorError):
            self._seal()
        os.chmod(self.anchor, 0o700)
        with self.assertRaises(PairedBackupOperatorError):
            seal_offline_pair(
                self.sqlite, self.witness, self.output, self.source,
                expected_sqlite_sha256=self._hash(self.sqlite),
                expected_witness_sha256=self._hash(self.witness),
                expected_previous_head_sha256=None, generation=FIRST,
            )

    def test_completed_generation_is_create_only(self) -> None:
        first = self._seal()
        immutable_head = (self.anchor / "latest.json").read_bytes()
        with self.assertRaises(PairedBackupOperatorError):
            self._seal()
        self.assertEqual((self.anchor / "latest.json").read_bytes(), immutable_head)
        self.assertEqual(self._hash(self.anchor / "latest.json"), first["head_sha256"])
        self.assertEqual({p.name for p in self.output.iterdir()}, {FIRST})
        self.assertFalse((self.anchor / ".inflight").exists())
