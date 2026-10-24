"""A6 paired offline Witness recovery must remain conservative and read-only.

These tests validate actual CompletionWitnessLedger events. They do not prove
host FD/mmap quiescence, authorize restore, or permit any external action.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mark_api.completion_authority import (
    CompletionIntent, encode_evidence,
)
from mark_api.completion_witness import (
    CompletionWitnessLedger, evidence_response,
)
from mark_api.domain import (
    AdSnapshot, LifecycleState, OperationOutcome, OperationReceipt,
)
from mark_api.fence_witness import FenceLedger
from mark_api.paired_restore_preflight import (
    PairPreflightError, _witness_inventory,
)
from mark_api.write_api import _request_fingerprint


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("ascii")


class A6WitnessOfflineRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="a6-offline-ledger-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        os.chmod(self.root, 0o700)
        self.witness = self.root / "witness.sqlite"
        self.principal = "offline-a6-synthetic-owner"
        self.key = "a6:pause:701"
        self.sha = _request_fingerprint(
            principal=self.principal, method="POST",
            path="/api/write/ads/701/pause", payload={},
        )
        self.intent = CompletionIntent.from_wire({
            "version": 1, "op": "execute", "key": self.key, "sha": self.sha,
            "generation": "write-api-" + "a" * 32,
            "principal": self.principal, "action": "pause",
            "target": "701", "payload": {},
        }, expected_principal=self.principal)
        self.now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)

    def inspect(self) -> dict[str, object]:
        # Same read-only in-memory SQLite shape as the real pinned-FD
        # pair verifier: source journal_mode does not get mutated.
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.deserialize(self.witness.read_bytes())
            return _witness_inventory(connection)

    def _create(self) -> None:
        with CompletionWitnessLedger(
            self.witness, principal=self.principal, initialize=True,
        ):
            pass

    def _record(self, *, stage: str) -> None:
        self._create()
        with CompletionWitnessLedger(
            self.witness, principal=self.principal,
        ) as ledger:
            session = ledger.open_session()
            ledger.execution_started(
                session, key=self.key, sha=self.sha, action="pause",
            )
            if stage in {"begun", "verified", "completed"}:
                ledger.authority_begin(self.intent)
            if stage in {"verified", "completed"}:
                original = AdSnapshot(
                    ad_id="701", observed_at=self.now,
                    source="synthetic-independent-platform",
                    lifecycle_state=LifecycleState.ACTIVE,
                )
                changed = AdSnapshot(
                    ad_id="701", observed_at=self.now,
                    source="synthetic-independent-platform",
                    lifecycle_state=LifecycleState.PAUSED,
                )
                receipt = OperationReceipt(
                    operation="set_state:paused", ad_id="701",
                    started_at=self.now, completed_at=self.now,
                    outcome=OperationOutcome.CONFIRMED,
                    pre_read_status="success_nonempty",
                    post_read_status="success_nonempty",
                    writer_invoked=True,
                    authorization_by=self.principal,
                    authorization_reference="write-api:" + self.key,
                    pre_snapshot=original, post_snapshot=changed,
                )
                evidence = encode_evidence(self.intent, receipt)
                ledger.authority_verified(self.intent, evidence)
                if stage == "completed":
                    self.completed_receipt = receipt
                    response = evidence_response(self.intent, evidence)
                    self.completed_response = response
                    self.assertEqual(ledger.execution_completed(
                        session, key=self.key, sha=self.sha, response=response,
                    ), "confirmed")
            ledger.seal_session(session)

    def test_canonical_empty_a6_schema_is_readable_without_opening_authority(self):
        self._create()
        result = self.inspect()
        self.assertEqual(result["witness_started"], [])
        self.assertEqual(result["witness_unresolved"], [])
        self.assertEqual(result["witness_chain_tip"], [0, "0" * 64])

    def test_real_a6_partial_proofs_remain_unresolved_after_restart(self):
        for stage in ("started", "begun", "verified", "completed"):
            with self.subTest(stage=stage):
                # New independent test ledger each time.
                file = self.root / (stage + ".sqlite")
                self.witness = file
                self._record(stage=stage)
                observed = self.inspect()
                self.assertEqual(
                    observed["witness_started"], [[self.key, self.sha]],
                )
                # Even a completed proof does NOT by itself permit a host
                # cutover or independently clear a recovery fence.
                self.assertEqual(
                    observed["witness_unresolved"], [[self.key, self.sha]],
                )

    def test_a6_completion_indexes_are_atomic_schema_requirement(self):
        self._create()
        with sqlite3.connect(self.witness) as conn:
            conn.execute("DROP INDEX one_authority_verification_per_key")
        with self.assertRaises(PairPreflightError):
            self.inspect()

    def test_rehashed_forged_actor_proof_is_not_accepted(self):
        self._record(stage="completed")
        with sqlite3.connect(self.witness) as conn:
            rows = [
                list(item) for item in conn.execute(
                    "SELECT session_id,kind,request_key,request_sha256,action,"
                    "outcome,response_sha256,evidence_reference,created_at "
                    "FROM events ORDER BY seq"
                )
            ]
            verified = next(row for row in rows if row[1] == "authority_verified")
            verified[7] = ("b" * 64) + "." + verified[7][65:]
            conn.execute("DROP TRIGGER events_no_update")
            conn.execute("DROP TRIGGER events_no_delete")
            conn.execute("DELETE FROM events")
            previous = "0" * 64
            for seq, row in enumerate(rows, 1):
                material = [value if value is not None else "" for value in row]
                digest = hashlib.sha256(_canonical(material + [previous])).hexdigest()
                conn.execute(
                    "INSERT INTO events (seq,session_id,kind,request_key,"
                    "request_sha256,action,outcome,response_sha256,"
                    "evidence_reference,created_at,previous_sha256,row_sha256) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (seq, *row, previous, digest),
                )
                previous = digest
            conn.execute(
                "CREATE TRIGGER events_no_update BEFORE UPDATE ON events "
                "BEGIN SELECT RAISE(ABORT, 'append-only events'); END"
            )
            conn.execute(
                "CREATE TRIGGER events_no_delete BEFORE DELETE ON events "
                "BEGIN SELECT RAISE(ABORT, 'append-only events'); END"
            )
        with closing(sqlite3.connect(self.witness)) as conn:
            # Hash-only checks cannot detect the malicious proof rewrite.
            FenceLedger._verify_chain(conn)
        with self.assertRaises(PairPreflightError):
            self.inspect()

    def test_cold_sqlite_and_a6_witness_pair_preserves_open_write_fences(self):
        from mark_api.paired_restore_preflight import inspect_offline_pair
        from mark_api.storage import SnapshotStore

        for stage in ("started", "begun", "verified"):
            with self.subTest(stage=stage):
                self.witness = self.root / (stage + "-witness.sqlite")
                owner_db = self.root / (stage + "-owner.sqlite")
                store = SnapshotStore(owner_db)
                claimed = store.claim_write_api_request(
                    idempotency_key=self.key, request_sha256=self.sha,
                    claim_owner="a6-offline-owner", requested_at=self.now,
                )
                self.assertTrue(claimed.created)
                store.begin_write_api_request(
                    idempotency_key=self.key, request_sha256=self.sha,
                    claim_owner="a6-offline-owner", execution_started_at=self.now,
                    strict_owner_fence=True,
                )
                self._record(stage=stage)
                observed = inspect_offline_pair(owner_db, self.witness)
                self.assertEqual(
                    observed["fences"]["sqlite_started"], [[self.key, self.sha]],
                )
                self.assertEqual(
                    observed["fences"]["witness_unresolved"], [[self.key, self.sha]],
                )
                # An offline pair inspector is evidence only: no authority,
                # no replay token and no automatic write clearance is created.
                self.assertNotIn("restore_authorized", observed)

    def test_a6_cold_pair_sealing_and_restore_preflight_preserve_open_fences(self):
        from mark_api.paired_backup_operator import seal_offline_pair
        from mark_api.paired_restore_preflight import verify_offline_pair
        from mark_api.storage import SnapshotStore

        source = self.root / "sources"
        output = self.root / "generations"
        anchor_dir = self.root / "independent-anchors"
        for folder in (source, output, anchor_dir):
            folder.mkdir(mode=0o700)
        self.witness = source / "witness.sqlite"
        owner_db = source / "mark.sqlite"
        store = SnapshotStore(owner_db)
        claim = store.claim_write_api_request(
            idempotency_key=self.key, request_sha256=self.sha,
            claim_owner="a6-cold-owner", requested_at=self.now,
        )
        self.assertTrue(claim.created)
        store.begin_write_api_request(
            idempotency_key=self.key, request_sha256=self.sha,
            claim_owner="a6-cold-owner", execution_started_at=self.now,
            strict_owner_fence=True,
        )
        self._record(stage="begun")
        receipt = seal_offline_pair(
            owner_db, self.witness, output, anchor_dir,
            expected_sqlite_sha256=hashlib.sha256(owner_db.read_bytes()).hexdigest(),
            expected_witness_sha256=hashlib.sha256(self.witness.read_bytes()).hexdigest(),
            expected_previous_head_sha256=None,
            generation="a" * 32,
        )
        self.assertFalse(receipt["restore_authorized"])
        self.assertFalse(receipt["live_source_quiescence_attested"])
        self.assertEqual(receipt["fences"]["sqlite_started_count"], 1)
        self.assertEqual(receipt["fences"]["witness_unresolved_count"], 1)
        published = output / ("a" * 32)
        checked = verify_offline_pair(
            published / "mark.sqlite", published / "witness.sqlite",
            published / "manifest.json",
            expected_manifest_sha256=receipt["manifest_sha256"],
        )
        self.assertEqual(checked["witness_unresolved_count"], 1)
        self.assertFalse(checked["restore_authorized"])
        self.assertFalse(checked["external_write_authorized"])

    def test_completed_a6_sqlite_reply_and_actor_proof_remain_pair_bound(self):
        from mark_api.paired_restore_preflight import inspect_offline_pair
        from mark_api.storage import SnapshotStore

        self.witness = self.root / "confirmed-witness.sqlite"
        database = self.root / "confirmed-owner.sqlite"
        store = SnapshotStore(database)
        claim = store.claim_write_api_request(
            idempotency_key=self.key, request_sha256=self.sha,
            claim_owner="a6-cold-owner", requested_at=self.now,
        )
        self.assertTrue(claim.created)
        store.begin_write_api_request(
            idempotency_key=self.key, request_sha256=self.sha,
            claim_owner="a6-cold-owner", execution_started_at=self.now,
            strict_owner_fence=True,
        )
        self._record(stage="completed")
        store.append_operation_receipt(self.completed_receipt)
        store.complete_write_api_request(
            idempotency_key=self.key, request_sha256=self.sha,
            claim_owner="a6-cold-owner", response_status=200,
            response_json=json.dumps(
                self.completed_response, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ), completed_at=self.now,
        )
        observed = inspect_offline_pair(database, self.witness)
        self.assertEqual(observed["fences"]["sqlite_started"], [[self.key, self.sha]])
        # Both sides agree on a completed response, but without an operator-
        # attested host cutover this scanner cannot clear old write authority.
        self.assertEqual(observed["fences"]["witness_unresolved"], [[self.key, self.sha]])
        self.assertEqual(observed["fences"]["sqlite_unverified_replay"], [])

    def test_legacy_four_index_witness_stays_compatible(self):
        with FenceLedger(self.witness, initialize=True) as ledger:
            session = ledger.open_session()
            ledger.execution_started(
                session, key=self.key, sha=self.sha, action="pause",
            )
            ledger.seal_session(session)
        result = self.inspect()
        self.assertEqual(result["witness_unresolved"], [[self.key, self.sha]])


if __name__ == "__main__":
    unittest.main()