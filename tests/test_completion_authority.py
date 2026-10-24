"""Unit regressions for the protected actor, not a hostile-UID release proof."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mark_api.completion_authority import (
    CompletionAuthorityError, CompletionIntent, OwnerCompletionService,
    decode_evidence, encode_evidence,
)
from mark_api.domain import AdSnapshot, LifecycleState, OperationOutcome, OperationReceipt
from mark_api.storage import SnapshotStore
from mark_api.write_api import _request_fingerprint

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
PRINCIPAL = "synthetic-a6-customer"
GENERATION = "write-api-" + "a" * 32


def intent_wire(*, action="pause", target="701", payload=None, key="a6:pause:701"):
    payload = {} if payload is None else payload
    method = "PATCH" if action == "update_content" else "DELETE" if action == "delete" else "POST"
    path = "/api/write/ads" if action == "create" else "/api/write/ads/" + target
    if action in {"pause", "activate"}:
        path += "/" + action
    return {
        "version": 1, "op": "execute", "key": key,
        "sha": _request_fingerprint(principal=PRINCIPAL, method=method, path=path, payload=payload),
        "generation": GENERATION, "principal": PRINCIPAL,
        "action": action, "target": target, "payload": payload,
    }


def pause_receipt():
    return OperationReceipt(
        operation="set_state:paused", ad_id="701", started_at=NOW, completed_at=NOW,
        outcome=OperationOutcome.CONFIRMED, pre_read_status="success_nonempty",
        post_read_status="success_nonempty", writer_invoked=True,
        authorization_by=PRINCIPAL, authorization_reference="write-api:a6:pause:701",
        pre_snapshot=AdSnapshot(ad_id="701", observed_at=NOW, source="synthetic-independent-owner",
                                lifecycle_state=LifecycleState.ACTIVE),
        post_snapshot=AdSnapshot(ad_id="701", observed_at=NOW, source="synthetic-independent-owner",
                                 lifecycle_state=LifecycleState.PAUSED),
    )


class CompletionAuthorityContractTests(unittest.TestCase):
    def test_pause_intent_recomputes_exact_request_identity(self):
        wire = intent_wire()
        intent = CompletionIntent.from_wire(wire, expected_principal=PRINCIPAL)
        self.assertEqual(intent.to_wire(), wire)
        self.assertEqual(intent.binding["generation"], GENERATION)

    def test_changed_target_action_payload_and_principal_cannot_reuse_hash(self):
        for field, value in (("target", "702"), ("action", "activate"),
                             ("payload", {"outcome": "confirmed"}), ("principal", "other-customer")):
            with self.subTest(field=field):
                wire = intent_wire()
                wire[field] = value
                with self.assertRaises(CompletionAuthorityError):
                    CompletionIntent.from_wire(wire, expected_principal=PRINCIPAL)

    def test_no_completion_clearance_or_caller_receipt_command(self):
        for operation in ("completed", "complete", "clearance", "sql", "execute"):
            with self.subTest(operation=operation):
                wire = intent_wire()
                wire["op"] = operation
                wire["receipt"] = {"outcome": "confirmed"}
                with self.assertRaises(CompletionAuthorityError):
                    CompletionIntent.from_wire(wire, expected_principal=PRINCIPAL)

    def test_native_types_generation_and_request_hash_are_strict(self):
        for field, value in (("version", True), ("key", 7), ("sha", "b" * 64),
                             ("generation", "executor-controlled"), ("target", 701),
                             ("payload", []), ("key", "line\nbreak")):
            with self.subTest(field=field, value=value):
                wire = intent_wire()
                wire[field] = value
                with self.assertRaises(CompletionAuthorityError):
                    CompletionIntent.from_wire(wire, expected_principal=PRINCIPAL)

    def test_valid_receipt_preserves_independent_snapshots(self):
        intent = CompletionIntent.from_wire(intent_wire(), expected_principal=PRINCIPAL)
        receipt = pause_receipt()
        self.assertEqual(decode_evidence(encode_evidence(intent, receipt), intent), receipt)

    def test_evidence_for_other_key_generation_hash_or_action_is_rejected(self):
        intent = CompletionIntent.from_wire(intent_wire(), expected_principal=PRINCIPAL)
        for field, value in (("key", "other-key"), ("generation", "write-api-" + "b" * 32),
                             ("sha", "b" * 64), ("action", "activate"), ("target", "702")):
            with self.subTest(field=field):
                wire = encode_evidence(intent, pause_receipt())
                wire["binding"][field] = value
                with self.assertRaises(CompletionAuthorityError):
                    decode_evidence(wire, intent)

    def test_forged_success_cannot_omit_or_misbind_postread(self):
        intent = CompletionIntent.from_wire(intent_wire(), expected_principal=PRINCIPAL)
        good = encode_evidence(intent, pause_receipt())
        variants = []
        for name, value in (("writer_invoked", 1), ("ad_id", "702"),
                            ("authorization_reference", "write-api:other"),
                            ("authorization_by", "untrusted")):
            row = deepcopy(good)
            row["receipt"][name] = value
            variants.append(row)
        for value in (None, {"ad_id": "701"}):
            row = deepcopy(good)
            row["snapshots"]["post_snapshot"] = value
            variants.append(row)
        row = deepcopy(good)
        row["snapshots"]["post_snapshot"]["lifecycle_state"] = "active"
        variants.append(row)
        row = deepcopy(good)
        row["receipt"]["started_at"] = "broken /private/sqlite/path"
        variants.append(row)
        for index, row in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(CompletionAuthorityError):
                decode_evidence(row, intent)

    def test_owner_never_contacts_actor_for_unbegun_or_misbound_claim(self):
        if os.geteuid() == 0:
            self.skipTest("positive owner object requires actual nonroot UID; isolated multi-UID job covers it")
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "owner.sqlite"
            store = SnapshotStore(database)
            other = os.geteuid() + 10000
            service = OwnerCompletionService(
                store=store, socket_path=Path(temporary) / "actor.sock",
                authority_uid=other, runtime_uid=other + 1, principal=PRINCIPAL,
            )
            wire = intent_wire()
            with patch.object(service, "_exchange", side_effect=AssertionError("must not contact actor")):
                with self.assertRaises(CompletionAuthorityError):
                    service.pause("701", authorization_by=PRINCIPAL,
                                  authorization_reference="write-api:a6:pause:701")
                store.claim_write_api_request(
                    idempotency_key=wire["key"], request_sha256=wire["sha"],
                    requested_at=NOW, claim_owner=GENERATION,
                )
                with self.assertRaises(CompletionAuthorityError):
                    service.pause("701", authorization_by=PRINCIPAL,
                                  authorization_reference="write-api:a6:pause:701")
                store.begin_write_api_request(
                    idempotency_key=wire["key"], request_sha256=wire["sha"],
                    claim_owner=GENERATION, execution_started_at=NOW,
                )
                with self.assertRaises(CompletionAuthorityError):
                    service.activate("701", authorization_by=PRINCIPAL,
                                     authorization_reference="write-api:a6:pause:701")
            record = store.write_api_request(wire["key"])
            self.assertEqual(record.state, "in_progress")
            self.assertIsNone(record.response_status)

    def test_same_uid_authority_is_not_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SnapshotStore(Path(temporary) / "owner.sqlite")
            with self.assertRaises(CompletionAuthorityError):
                OwnerCompletionService(
                    store=store, socket_path=Path(temporary) / "actor.sock",
                    authority_uid=os.geteuid(), runtime_uid=os.geteuid() + 10000,
                    principal=PRINCIPAL,
                )



class NonMediaActorAdmissionTests(unittest.TestCase):
    """Local protocol regressions, not a substitute for independent-UID E2E."""

    def test_missing_nonmedia_actor_is_denied(self):
        from mark_api.completion_authority import _verify_nonmedia_actor
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CompletionAuthorityError):
                _verify_nonmedia_actor(Path(temporary) / "absent.sock", os.geteuid() + 101)

    @unittest.skipIf(os.geteuid() == 0, "same-UID protocol fixture requires nonroot")
    def test_nonmedia_actor_profile_and_peer_are_strict(self):
        import socketserver
        from threading import Thread
        from mark_api.completion_authority import _verify_nonmedia_actor

        success = b'{"media_ready":false,"ok":true,"version":1}\n'
        cases = (
            ("valid_profile", success, 0, True),
            ("wrong_peer_uid", success, 111, False),
            ("media_mode_mismatch", b'{"media_ready":true,"ok":true,"version":1}\n', 0, False),
            ("forged_bool_version", b'{"media_ready":false,"ok":true,"version":true}\n', 0, False),
            ("extra_field", b'{"media_ready":false,"ok":true,"version":1,"clearance":true}\n', 0, False),
            ("malformed", b'{"media_ready":false,"ok":true,"version":1}\ntrailing', 0, True),
        )
        for label, reply, uid_delta, allowed in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "actor.sock"

                class Handler(socketserver.StreamRequestHandler):
                    def handle(self):
                        request = self.rfile.readline(256)
                        if request:
                            self.wfile.write(reply)
                            self.wfile.flush()

                with socketserver.UnixStreamServer(str(path), Handler) as server:
                    thread = Thread(target=server.handle_request, daemon=True)
                    thread.start()
                    try:
                        if allowed:
                            self.assertIsNone(_verify_nonmedia_actor(path, os.geteuid() + uid_delta))
                        else:
                            with self.assertRaises(CompletionAuthorityError):
                                _verify_nonmedia_actor(path, os.geteuid() + uid_delta)
                    finally:
                        thread.join(2)
                        self.assertFalse(thread.is_alive(), "actor admission fixture did not finish")


if __name__ == "__main__":
    unittest.main()