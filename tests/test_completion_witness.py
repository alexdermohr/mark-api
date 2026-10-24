"""Durable actor admission/completion tests; OS separation is tested separately."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from mark_api.completion_authority import CompletionIntent, encode_evidence
from mark_api.completion_witness import CompletionWitnessLedger, evidence_response
from mark_api.domain import AdSnapshot, LifecycleState, OperationOutcome, OperationReceipt
from mark_api.fence_witness import FenceLedger, FenceWitnessAlreadyRecorded, FenceWitnessError
from mark_api.write_api import _request_fingerprint

PRINCIPAL = "synthetic-protected-customer"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def _intent(key="protected:pause:701", generation="write-api-" + "a" * 32):
    payload = {}
    return CompletionIntent.from_wire({
        "version": 1, "op": "execute", "key": key,
        "sha": _request_fingerprint(principal=PRINCIPAL, method="POST", path="/api/write/ads/701/pause", payload=payload),
        "generation": generation, "principal": PRINCIPAL, "action": "pause", "target": "701", "payload": payload,
    }, expected_principal=PRINCIPAL)


def _evidence(intent):
    receipt = OperationReceipt(
        operation="set_state:paused", ad_id="701", started_at=NOW,
        completed_at=NOW + timedelta(seconds=1), outcome=OperationOutcome.CONFIRMED,
        pre_read_status="success_nonempty", post_read_status="success_nonempty", writer_invoked=True,
        authorization_by=PRINCIPAL, authorization_reference="write-api:" + intent.key,
        pre_snapshot=AdSnapshot(ad_id="701", observed_at=NOW, source="synthetic-protected-owner", lifecycle_state=LifecycleState.ACTIVE),
        post_snapshot=AdSnapshot(ad_id="701", observed_at=NOW + timedelta(seconds=1), source="synthetic-protected-owner", lifecycle_state=LifecycleState.PAUSED),
    )
    return encode_evidence(intent, receipt)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@unittest.skipIf(os.geteuid() == 0, "private owner ledger positive tests require real nonroot UID")
class CompletionWitnessLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="a6-protected-witness-")
        self.path = Path(self.temp.name) / "witness.sqlite"
        self.ledger = CompletionWitnessLedger(self.path, principal=PRINCIPAL, initialize=True)
        self.session = self.ledger.open_session()
        self.intent = _intent()
        self.evidence = _evidence(self.intent)
        self.response = evidence_response(self.intent, self.evidence)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def start(self):
        self.ledger.execution_started(self.session, key=self.intent.key, sha=self.intent.sha, action=self.intent.action)

    def finish(self):
        return self.ledger.execution_completed(self.session, key=self.intent.key, sha=self.intent.sha, response=self.response)

    def test_owner_success_assertion_without_actor_proof_remains_pending(self):
        self.start()
        self.assertEqual(self.finish(), "ambiguous")
        self.assertTrue(self.ledger.fence_pending())
        self.assertEqual(self.ledger.replay_status(key=self.intent.key, sha=self.intent.sha, response_sha=_digest(self.response)), "ambiguous")

    def test_actor_cannot_run_before_owner_durable_start(self):
        with self.assertRaises(FenceWitnessError):
            self.ledger.authority_begin(self.intent)
        self.assertFalse(self.ledger.fence_pending())

    def test_actor_postread_without_prior_reservation_is_rejected(self):
        self.start()
        with self.assertRaises(FenceWitnessError):
            self.ledger.authority_verified(self.intent, self.evidence)
        self.assertTrue(self.ledger.fence_pending())

    def test_one_durable_reservation_survives_actor_and_witness_restart(self):
        self.start()
        self.ledger.authority_begin(self.intent)
        for intent in (self.intent, _intent(generation="write-api-" + "b" * 32)):
            with self.subTest(generation=intent.generation), self.assertRaises(FenceWitnessError):
                self.ledger.authority_begin(intent)
        self.ledger.close()
        self.ledger = CompletionWitnessLedger(self.path, principal=PRINCIPAL)
        with self.assertRaises(FenceWitnessError):
            self.ledger.authority_begin(self.intent)
        self.assertTrue(self.ledger.fence_pending())

    def test_success_requires_actor_proof_and_exact_owner_persistence_ack(self):
        self.start()
        self.ledger.authority_begin(self.intent)
        self.ledger.authority_verified(self.intent, self.evidence)
        self.assertTrue(self.ledger.fence_pending())
        self.assertEqual(self.finish(), "confirmed")
        self.assertFalse(self.ledger.fence_pending())
        self.assertFalse(self.ledger.clearance_recorded(key=self.intent.key, sha=self.intent.sha))
        self.assertEqual(self.ledger.replay_status(key=self.intent.key, sha=self.intent.sha, response_sha=_digest(self.response)), "confirmed")
        with self.assertRaises(FenceWitnessAlreadyRecorded):
            self.start()
        self.ledger.execution_started(self.session, key="next:unused", sha="d" * 64, action="pause")
        self.assertTrue(self.ledger.fence_pending())

    def test_lost_completion_ack_replays_without_a_new_reservation(self):
        self.start()
        self.ledger.authority_begin(self.intent)
        self.ledger.authority_verified(self.intent, self.evidence)
        self.finish()  # Assume this reply is lost; only durable state is used.
        self.ledger.close()
        self.ledger = CompletionWitnessLedger(self.path, principal=PRINCIPAL)
        self.assertFalse(self.ledger.fence_pending())
        self.assertEqual(self.ledger.replay_status(key=self.intent.key, sha=self.intent.sha, response_sha=_digest(self.response)), "confirmed")
        self.assertEqual(self.ledger.replay_status(key=self.intent.key, sha=self.intent.sha, response_sha="0" * 64), "unverified")
        with self.assertRaises(FenceWitnessError):
            self.ledger.authority_begin(self.intent)

    def test_changed_generation_or_forged_postread_cannot_settle(self):
        self.start()
        self.ledger.authority_begin(self.intent)
        changed_generation = _intent(generation="write-api-" + "b" * 32)
        with self.assertRaises(FenceWitnessError):
            self.ledger.authority_verified(changed_generation, _evidence(changed_generation))
        forged = deepcopy(self.evidence)
        forged["snapshots"]["post_snapshot"]["lifecycle_state"] = "active"
        with self.assertRaises(FenceWitnessError):
            self.ledger.authority_verified(self.intent, forged)
        self.assertTrue(self.ledger.fence_pending())

    def test_changed_owner_response_cannot_reuse_valid_actor_proof(self):
        self.start()
        self.ledger.authority_begin(self.intent)
        self.ledger.authority_verified(self.intent, self.evidence)
        self.response["operation_receipt"]["ad_id"] = "702"
        self.assertEqual(self.finish(), "ambiguous")
        self.assertTrue(self.ledger.fence_pending())

    def test_uncertain_actor_postread_and_runtime_clearance_remain_fenced(self):
        self.start()
        self.ledger.authority_begin(self.intent)
        unknown = deepcopy(self.evidence)
        unknown["receipt"]["outcome"] = "ambiguous"
        unknown["receipt"]["post_read_status"] = None
        unknown["snapshots"]["post_snapshot"] = None
        self.ledger.authority_verified(self.intent, unknown)
        self.response = evidence_response(self.intent, unknown)
        self.assertEqual(self.finish(), "ambiguous")
        with self.assertRaises(FenceWitnessError):
            self.ledger.runtime_clearance(self.session, key=self.intent.key, sha=self.intent.sha, evidence="executor-claimed-proof")
        self.assertTrue(self.ledger.fence_pending())

    def test_old_reader_never_promotes_new_proof_or_forgets_a_start(self):
        self.start()
        self.ledger.authority_begin(self.intent)
        self.ledger.authority_verified(self.intent, self.evidence)
        self.finish()
        self.ledger.close()
        legacy = FenceLedger(self.path)
        try:
            self.assertTrue(legacy.fence_pending())
            self.assertEqual(legacy.replay_status(key=self.intent.key, sha=self.intent.sha, response_sha=_digest(self.response)), "ambiguous")
        finally:
            legacy.close()


if __name__ == "__main__":
    unittest.main()
