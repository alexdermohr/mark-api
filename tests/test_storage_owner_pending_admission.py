"""Socketless client/dispatch/SQLite pending recovery contract.

Transport and UID attestation are deliberately outside this harness. The
unchanged socket/multi-UID suites must separately establish those boundaries.
No HTTP, browser, provider, external platform or recovery clearance is used.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mark_api.storage import DashboardPendingWrite, SnapshotStore
from mark_api.storage_owner import (
    StorageOwnerClient, StorageOwnerError, StorageOwnerServer,
    _MAX_FRAME, _canonical_json, _pending_as_json, _pending_from_json,
    _reply_frame_fits, _strict_json,
)


class PendingAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "mark.sqlite"
        self.store = SnapshotStore(self.database)
        # Run the real closed dispatch table, without constructing a socket or
        # pretending the sandbox's mapped root UID is an attested ancestor.
        self.owner = object.__new__(StorageOwnerServer)
        self.owner.store = self.store
        self.attestation = patch.object(self.owner, "_attest")
        self.attestation.start()
        self.addCleanup(self.attestation.stop)
        self.record = DashboardPendingWrite(
            scope="create-media", resource_key="create",
            idempotency_key="ui:pending-boundary", method="POST",
            path="/api/write/media/ads", payload_json='{"title":"safe"}',
            ad_id=None, acknowledged=False,
        )

    def claim_request(self, record: DashboardPendingWrite) -> dict[str, object]:
        return {
            "version": 1, "op": "dashboard_pending_claim",
            "scope": record.scope, "resource_key": record.resource_key,
            "key": record.idempotency_key, "method": record.method,
            "path": record.path, "payload_json": record.payload_json,
            "ad_id": record.ad_id,
        }

    def transport(self, op: str, **fields: object) -> dict[str, object]:
        request = (_canonical_json({"version": 1, "op": op, **fields}) + "\n").encode()
        self.assertLessEqual(len(request), _MAX_FRAME + 1)
        result = self.owner.dispatch(_strict_json(request[:-1]))
        self.assertTrue(_reply_frame_fits(result))
        reply = _canonical_json({"ok": True, "result": result}).encode()
        return _strict_json(reply)["result"]

    def test_request_that_fits_but_cannot_be_recovered_is_rejected_before_commit(self) -> None:
        empty = replace(self.record, payload_json='{"title":""}')
        size = len((_canonical_json(self.claim_request(empty)) + "\n").encode())
        record = replace(empty, payload_json=json.dumps(
            {"title": "x" * (_MAX_FRAME + 1 - size)}, separators=(",", ":"),
        ))
        request = self.claim_request(record)
        wire = (_canonical_json(request) + "\n").encode()
        self.assertEqual(len(wire), _MAX_FRAME + 1)
        self.assertFalse(_reply_frame_fits({
            "rows": [_pending_as_json(record)], "more": False,
        }))
        with self.assertRaisesRegex(StorageOwnerError, "frame"):
            self.owner.dispatch(_strict_json(wire[:-1]))
        self.assertEqual(self.store.dashboard_pending_writes(), ())
        reopened = SnapshotStore(self.database, create_if_missing=False)
        self.assertEqual(reopened.dashboard_pending_writes(), ())
        self.assertEqual(self.owner.dispatch(self.claim_request(self.record)), {"created": True})

    def test_largest_recoverable_media_intent_roundtrips_after_response_loss(self) -> None:
        prefix = "Bilder: ä雪\n"
        empty = replace(self.record, payload_json=json.dumps(
            {"title": prefix}, ensure_ascii=False, separators=(",", ":"),
        ))
        envelope = _canonical_json({"ok": True, "result": {
            "rows": [_pending_as_json(empty)], "more": False,
        }})
        remaining = _MAX_FRAME - len(envelope.encode())
        record = replace(empty, payload_json=json.dumps(
            {"title": prefix + "x" * remaining}, ensure_ascii=False,
            separators=(",", ":"),
        ))
        self.assertEqual(len(_canonical_json({"ok": True, "result": {
            "rows": [_pending_as_json(record)], "more": False,
        }}).encode()), _MAX_FRAME)
        client = StorageOwnerClient(Path("/not-connected.sock"), expected_owner_uid=os.geteuid())
        with patch.object(client, "_call", side_effect=self.transport):
            details = dict(
                scope=record.scope, resource_key=record.resource_key,
                idempotency_key=record.idempotency_key, method=record.method,
                path=record.path, payload_json=record.payload_json, ad_id=record.ad_id,
            )
            self.assertTrue(client.claim_dashboard_pending_write(**details))
            # Drop the claim result and reconstruct storage as after a restart.
            self.owner.store = SnapshotStore(self.database, create_if_missing=False)
            self.assertEqual(client.dashboard_pending_writes(), (record,))
            self.assertFalse(client.claim_dashboard_pending_write(**details))
            with self.assertRaises(ValueError):
                client.claim_dashboard_pending_write(**(details | {"idempotency_key": "ui:new-key"}))
            self.assertEqual(client.dashboard_pending_writes(), (record,))
        self.assertFalse(hasattr(client, "acknowledge_dashboard_pending_write"))
        self.assertFalse(hasattr(client, "complete_write_api_request"))

    def test_pending_readback_rejects_swapped_route_identity_and_ambiguous_body(self) -> None:
        good = _pending_as_json(self.record)
        self.assertEqual(_pending_from_json(good), self.record)
        changes = (
            {"scope": "create"}, {"resource_key": "ad:123"}, {"ad_id": "123"},
            {"method": "DELETE"}, {"path": "/api/write/media/discard"},
            {"payload_json": None}, {"payload_json": '{"title":"one","title":"two"}'},
            {"payload_json": '{"price":1e999}'},
            {"payload_json": '{"title":"\\ud800"}'},
            {"payload_json": '{"title":' + '[' * 65 + '0' + ']' * 65 + '}'},
            {"payload_json": '{"z":2, "a":1}'},
        )
        for changed in changes:
            with self.subTest(changed=changed), self.assertRaises(StorageOwnerError):
                _pending_from_json(good | changed)

    def test_lost_begin_and_quarantine_responses_keep_unknown_fenced_after_restart(self) -> None:
        now = datetime(2026, 10, 23, tzinfo=timezone.utc)
        fields = dict(key=self.record.idempotency_key, sha="a" * 64,
                      owner="synthetic:runtime", at=now.isoformat())
        self.owner.dispatch(self.claim_request(self.record))
        self.transport("claim", **fields)
        self.transport("begin", **fields)  # Intentionally discard the response.
        self.owner.store = SnapshotStore(self.database, create_if_missing=False)
        client = StorageOwnerClient(Path("/not-connected.sock"), expected_owner_uid=os.geteuid())
        with patch.object(client, "_call", side_effect=self.transport):
            started = client.read_request(fields["key"])
            self.assertIsNotNone(started.execution_started_at)
            with self.assertRaises(ValueError):
                self.transport("cancel_unstarted", **fields)
            with self.assertRaises(ValueError):
                self.transport("begin", **fields)
            self.transport("quarantine_started", **fields)  # Lose this response too.
            self.owner.store = SnapshotStore(self.database, create_if_missing=False)
            record = client.read_request(fields["key"])
            self.assertEqual(record.response_status, 202)
            self.assertEqual(json.loads(record.response_json), {
                "error": "write_outcome_unverified",
                "idempotency_key": fields["key"], "platform_retry_authorized": False,
            })
            self.assertEqual(client.dashboard_pending_writes(), (self.record,))
            self.assertFalse(client.has_write_recovery_clearance(
                idempotency_key=fields["key"], request_sha256=fields["sha"],
            ))
        for forbidden in ("complete", "dashboard_pending_ack", "clearance", "sql"):
            with self.subTest(forbidden=forbidden), self.assertRaises(StorageOwnerError):
                self.transport(forbidden, **fields)
        another = fields | {"key": "ui:another-key"}
        self.transport("claim", **another)
        with self.assertRaisesRegex(ValueError, "quarantine"):
            self.transport("begin", **another)
        self.assertEqual(self.store.dashboard_pending_writes(), (self.record,))


if __name__ == "__main__":
    unittest.main()
