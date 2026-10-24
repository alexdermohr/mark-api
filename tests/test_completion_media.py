"""Protected media-completion contract. Does not attest a host cutover."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import os
import tempfile
import unittest

from mark_api.completion_authority import (
    CompletionAuthorityError, CompletionIntent, decode_evidence, encode_evidence,
)
from mark_api.completion_witness import CompletionWitnessLedger, evidence_response
from mark_api.domain import (
    AdSnapshot, CreateOperationReceipt, LifecycleState, MediaPostReadStatus,
    OperationOutcome,
)
from mark_api.write_api import _request_fingerprint


PRINCIPAL = "synthetic-protected-customer"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
PAYLOAD = {
    "category_path": ["Wohnen", "Möbel"],
    "title": "Solid table",
    "description": "Solid description",
    "price_eur": 42,
    "media_refs": ["media_opaque_image_1"],
}


def media_intent(key="protected:media:1"):
    return CompletionIntent.from_wire({
        "version": 1, "op": "execute", "key": key,
        "sha": _request_fingerprint(
            principal=PRINCIPAL, method="POST", path="/api/write/media/ads",
            payload=PAYLOAD,
        ),
        "generation": "write-api-" + "a" * 32,
        "principal": PRINCIPAL, "action": "create_media",
        "target": None, "payload": PAYLOAD,
    }, expected_principal=PRINCIPAL)


def media_receipt(key, *, media_confirmed=True):
    snap = AdSnapshot(
        ad_id="900", observed_at=NOW, source="synthetic-independent-media",
        lifecycle_state=LifecycleState.ACTIVE,
        title="Solid table", description="Solid description",
    )
    return CreateOperationReceipt(
        operation="create", started_at=NOW, completed_at=NOW,
        outcome=OperationOutcome.CONFIRMED,
        pre_read_status="success_nonempty",
        confirmation_pre_read_status="success_nonempty",
        post_read_status="success_nonempty",
        confirmation_post_read_status="success_nonempty",
        content_post_read_status="success_nonempty",
        writer_invoked=True, created_ad_id="900",
        authorization_by=PRINCIPAL,
        authorization_reference="write-api:" + key,
        post_snapshot=snap, confirmation_post_snapshot=snap, content_post_snapshot=snap,
        media_post_read_status=(
            MediaPostReadStatus.CONFIRMED if media_confirmed
            else MediaPostReadStatus.MISMATCH
        ),
        media_persistence_confirmed=media_confirmed,
    )


class ProtectedMediaEvidenceTests(unittest.TestCase):
    def test_create_media_intent_has_exact_path_and_media_handles_in_fingerprint(self):
        intent = media_intent()
        self.assertEqual(intent.action, "create_media")
        self.assertEqual(intent.to_wire()["payload"]["media_refs"], PAYLOAD["media_refs"])
        for mutation in ({"media_refs": ["media_other"]}, {"media_refs": ["/tmp/image.png"]}):
            wire = intent.to_wire()
            wire["payload"] = {**PAYLOAD, **mutation}
            with self.subTest(mutation=mutation), self.assertRaises(CompletionAuthorityError):
                CompletionIntent.from_wire(wire, expected_principal=PRINCIPAL)

    def test_confirmed_media_proof_round_trips_without_losing_media_fields(self):
        intent = media_intent()
        receipt = media_receipt(intent.key)
        self.assertEqual(decode_evidence(encode_evidence(intent, receipt), intent), receipt)
        response = evidence_response(intent, encode_evidence(intent, receipt))
        self.assertIs(response["media_persistence_confirmed"], True)
        self.assertEqual(response["media_post_read_status"], "confirmed")
        self.assertIs(response["operation_receipt"]["media_persistence_confirmed"], True)

    def test_missing_or_forged_media_status_never_becomes_a_success(self):
        intent = media_intent()
        correct = encode_evidence(intent, media_receipt(intent.key))
        for name, value in (
            ("media_persistence_confirmed", False),
            ("media_persistence_confirmed", 1),
            ("media_post_read_status", "mismatch"),
        ):
            proof = deepcopy(correct)
            proof["receipt"][name] = value
            with self.subTest(field=name), self.assertRaises(CompletionAuthorityError):
                decode_evidence(proof, intent)
        for name in ("media_persistence_confirmed", "media_post_read_status"):
            proof = deepcopy(correct)
            del proof["receipt"][name]
            with self.subTest(missing=name), self.assertRaises(CompletionAuthorityError):
                decode_evidence(proof, intent)

    @unittest.skipIf(os.geteuid() == 0, "actual nonroot identity required")
    def test_only_independently_confirmed_media_settles_witness(self):
        for confirmed in (True, False):
            with self.subTest(media_confirmed=confirmed), tempfile.TemporaryDirectory() as tmp:
                ledger = CompletionWitnessLedger(
                    Path(tmp) / "witness.sqlite", principal=PRINCIPAL, initialize=True,
                )
                try:
                    session = ledger.open_session()
                    intent = media_intent("protected:media:" + str(confirmed))
                    proof = encode_evidence(intent, media_receipt(intent.key, media_confirmed=confirmed))
                    if not confirmed:
                        from mark_api.fence_witness import FenceWitnessError
                        with self.assertRaises(FenceWitnessError):
                            evidence_response(intent, proof)
                        with self.assertRaises(CompletionAuthorityError):
                            decode_evidence(proof, intent)
                        continue
                    response = evidence_response(intent, proof)
                    ledger.execution_started(
                        session, key=intent.key, sha=intent.sha, action=intent.action,
                    )
                    ledger.authority_begin(intent)
                    ledger.authority_verified(intent, proof)
                    verdict = ledger.execution_completed(
                        session, key=intent.key, sha=intent.sha, response=response,
                    )
                    self.assertEqual(verdict, "confirmed" if confirmed else "ambiguous")
                    self.assertEqual(ledger.fence_pending(), not confirmed)
                finally:
                    ledger.close()



class ProtectedMediaDashboardTests(unittest.TestCase):
    """Real HTTP media Write + real SQLite pending/ACK, controlled witness seam.

    This does not assert UID isolation. Separate UID acceptance remains required.
    """

    def test_media_ack_requires_exact_independent_proof_in_both_phases(self):
        from types import SimpleNamespace
        from mark_api.completion_dashboard import CompletionDashboardOwnerServer
        from mark_api.storage_owner import StorageOwnerError
        from mark_api.dashboard import _json_bytes
        from mark_api.storage import SnapshotStore
        from mark_api.domain import AdSnapshot, CreateOperationReceipt, LifecycleState, MediaPostReadStatus, OperationOutcome
        from mark_api.write_api import _request_fingerprint, _create_receipt_to_dict
        import json

        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory) / "media.sqlite")
            key = "protected:media:dashboard-1"
            payload = {
                "category_path": ["Haus & Garten", "Dekoration"],
                "title": "Synthetic media listing",
                "description": "No external listing or upload",
                "price_eur": 12,
                "media_refs": ["media_opaque_image_1"],
            }
            principal = "synthetic-media-owner"
            digest = _request_fingerprint(
                principal=principal, method="POST", path="/api/write/media/ads", payload=payload,
            )
            generation = "write-api-" + "a" * 32
            store.claim_write_api_request(
                idempotency_key=key, request_sha256=digest, requested_at=NOW,
                claim_owner=generation,
            )
            store.begin_write_api_request(
                idempotency_key=key, request_sha256=digest, claim_owner=generation,
                execution_started_at=NOW,
            )
            snap = AdSnapshot(
                ad_id="900", observed_at=NOW, source="synthetic-independent-media",
                lifecycle_state=LifecycleState.ACTIVE,
                title=payload["title"], description=payload["description"],
            )
            receipt = CreateOperationReceipt(
                operation="create", started_at=NOW, completed_at=NOW,
                outcome=OperationOutcome.CONFIRMED,
                pre_read_status="success_empty",
                confirmation_pre_read_status="success_empty",
                post_read_status="success_nonempty",
                confirmation_post_read_status="success_nonempty",
                content_post_read_status="success_nonempty",
                writer_invoked=True, created_ad_id="900",
                authorization_by=principal, authorization_reference="write-api:" + key,
                post_snapshot=snap, confirmation_post_snapshot=snap, content_post_snapshot=snap,
                media_post_read_status=MediaPostReadStatus.CONFIRMED,
                media_persistence_confirmed=True,
            )
            store.append_create_operation_checkpoint(replace(receipt, media_post_read_status=None, media_persistence_confirmed=False))
            store.append_create_operation_receipt(receipt)
            response = {
                "idempotency_key": key, "operation_receipt": _create_receipt_to_dict(receipt),
                "platform_retry_authorized": False,
                "media_persistence_confirmed": True, "media_post_read_status": "confirmed",
            }
            encoded = json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            store.complete_write_api_request(
                idempotency_key=key, request_sha256=digest, claim_owner=generation,
                response_status=200, response_json=encoded, completed_at=NOW,
            )
            self.assertTrue(store.claim_dashboard_pending_write(
                scope="create-media", resource_key="create", idempotency_key=key,
                method="POST", path="/api/write/media/ads",
                payload_json=_json_bytes(payload).decode("utf-8"), ad_id=None,
            ))
            record = store.write_api_request(key)
            response_digest = hashlib.sha256(record.response_json.encode()).hexdigest()
            allowed = {"value": False}

            def replay_status(**binding):
                if allowed["value"] and binding == {
                    "key": key, "sha": digest, "response_sha": response_digest,
                }:
                    return "confirmed"
                return "ambiguous"

            owner = CompletionDashboardOwnerServer.__new__(CompletionDashboardOwnerServer)
            owner.store = store
            owner._attest = lambda: None
            owner._completion_principal = principal
            owner._completion_witness = SimpleNamespace(replay_status=replay_status)
            command = {
                "version": 1, "op": "dashboard_completed_ack",
                "scope": "create-media", "key": key,
            }
            with self.assertRaises(StorageOwnerError):
                owner.dispatch(command)
            self.assertFalse(store.dashboard_pending_writes()[0].acknowledged)
            allowed["value"] = True
            self.assertEqual(owner.dispatch(command), {"phase": "acknowledged"})
            allowed["value"] = False
            with self.assertRaises(StorageOwnerError):
                owner.dispatch(command)
            self.assertTrue(store.dashboard_pending_writes()[0].acknowledged)
            allowed["value"] = True
            self.assertEqual(owner.dispatch(command), {"phase": "finalized"})
            self.assertEqual(store.dashboard_pending_writes(), ())


if __name__ == "__main__":
    unittest.main()