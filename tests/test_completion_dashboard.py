"""Protected dashboard dispatch contracts with real SQLite and product handlers.

Only the transport and independent witness lookup are controlled test seams.
Actual peer credentials, separate UIDs and installed UI composition are checked
by a6_completion_uid_acceptance.py, not claimed by these dispatch unit tests.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
import unittest

from mark_api import dashboard, write_api
from mark_api.completion_dashboard import CompletionDashboardClient, CompletionDashboardOwnerServer
from mark_api.storage import SnapshotStore
from mark_api.storage_owner import StorageOwnerClient, StorageOwnerError, _pending_as_json
import test_dashboard_local_reconciliation as _local


class ProtectedDashboardCompletionTests(unittest.TestCase):
    setUp = _local.LocalReconciliationTests.setUp
    send = _local.LocalReconciliationTests.send
    ack = _local.LocalReconciliationTests.ack
    completed_edit = _local.LocalReconciliationTests.completed_edit
    backend_connection = _local.LocalReconciliationTests.backend_connection

    def reopen(self):
        self.store = SnapshotStore(self.store.path)
        self.backend = write_api._handler_factory(
            service=_local.service_for(self.platform, self.store), media_service=None,
            media_stager=None, store=self.store, access=self.access,
            clock=lambda: _local.NOW, body_read_timeout_seconds=10,
            claim_owner="synthetic-dispatch", execution_lock=None, fence_witness=None,
        )
        self.proxy = replace(self.proxy, local_reconciliation_enabled=False, owner_reconciliation_enabled=True)
        if not hasattr(self, "proofs"):
            self.proofs = {}
        # No same-UID constructor is being accepted: these isolated dispatch
        # objects deliberately omit socket construction and kernel attestation.
        self.owner = CompletionDashboardOwnerServer.__new__(CompletionDashboardOwnerServer)
        self.owner.store = self.store
        self.owner._attest = lambda: None
        self.owner._completion_principal = self.access.principal
        self.owner._completion_witness = SimpleNamespace(
            _expected_owner_uid=os.geteuid() + 100,
            replay_status=lambda **binding: self.proofs.get(tuple(sorted(binding.items())), "ambiguous"),
        )
        self.client = CompletionDashboardClient.__new__(CompletionDashboardClient)
        self.client._pid, self.client._uid = os.getpid(), os.geteuid()
        self.client.expected_owner_uid = os.geteuid() + 101
        self.client.principal = self.access.principal
        self.client._dashboard_guard = RLock()
        self.client._call = lambda op, **fields: self.owner.dispatch({"version": 1, "op": op, **fields})
        self.frontend = dashboard._handler_factory(self.client, None, None, self.proxy, None)

    def grant_proof(self, key="local-key", *, outcome="confirmed"):
        record = self.store.write_api_request(key)
        self.assertIsNotNone(record)
        binding = {"key": key, "sha": record.request_sha256,
                   "response_sha": hashlib.sha256(record.response_json.encode()).hexdigest()}
        self.proofs[tuple(sorted(binding.items()))] = outcome

    def assert_fenced(self):
        before = self.store.dashboard_pending_writes()
        count = len(self.backend_requests)
        self.assertIn(self.send(payload={"title": "Saved title"})[0], (409, 500))
        self.assertIn(self.ack()[0], (409, 500))
        self.assertEqual(self.store.dashboard_pending_writes(), before)
        self.assertEqual(len(self.backend_requests), count)

    def test_owner_http_success_alone_never_authorizes_replay_or_ack(self):
        self.completed_edit()
        self.assert_fenced()
        self.assertEqual(self.platform.effects, ["update_content"])
        self.assertFalse(self.store.dashboard_pending_writes()[0].acknowledged)

    def test_confirmed_replay_restart_and_both_ack_phases_use_owner_proof(self):
        first = self.completed_edit()
        self.grant_proof()
        self.reopen()
        replay = self.send(payload={"title": "Saved title"})
        self.assertEqual(replay[:2], first[:2])
        self.assertTrue(replay[2]["idempotency_replayed"])
        self.assertEqual(self.ack()[:2], (200, {"acknowledged": True, "finalized": False}))
        self.reopen()
        self.assertEqual(self.ack()[:2], (200, {"acknowledged": True, "finalized": True}))
        self.assertEqual(self.store.dashboard_pending_writes(), ())
        self.assertEqual(self.send(payload={"title": "Saved title"})[:2], first[:2])
        self.assertEqual(self.store.dashboard_pending_writes(), ())
        self.assertEqual(self.backend_requests, [("PATCH", "/api/write/ads/314")])
        self.assertEqual(self.platform.effects, ["update_content"])
        self.assertIn(self.ack()[0], (409, 500))

    def test_witness_is_checked_again_between_ack_phases(self):
        self.completed_edit()
        self.grant_proof()
        self.assertEqual(self.ack()[0], 200)
        before = self.store.dashboard_pending_writes()
        self.assertTrue(before[0].acknowledged)
        self.proofs.clear()
        self.reopen()
        self.assert_fenced()
        self.assertEqual(self.store.dashboard_pending_writes(), before)
        self.grant_proof()
        self.assertTrue(self.ack()[1]["finalized"])

    def test_uncertain_and_precondition_failed_witness_results_cannot_retire_ui_fence(self):
        self.completed_edit()
        for outcome in ("ambiguous", "unverified", "precondition_failed", "missing", True, None):
            with self.subTest(outcome=outcome):
                self.proofs.clear()
                self.grant_proof(outcome=outcome)
                self.assert_fenced()

    def test_witness_proof_never_replaces_exact_persisted_receipt(self):
        self.completed_edit()
        self.grant_proof()
        for field, value in (("response_status", 202), ("request_sha256", "0" * 64), ("claim_owner", None)):
            record = self.store.write_api_request("local-key")
            original = getattr(record, field)
            with self.subTest(field=field), closing(self.store._connect()) as conn, conn:
                conn.execute(f"UPDATE write_api_requests SET {field}=? WHERE idempotency_key='local-key'", (value,))
            self.assert_fenced()
            with closing(self.store._connect()) as conn, conn:
                conn.execute(f"UPDATE write_api_requests SET {field}=? WHERE idempotency_key='local-key'", (original,))
        with closing(self.store._connect()) as conn, conn:
            conn.execute("DELETE FROM operation_receipts")
        self.assert_fenced()

    def test_changed_response_hash_cannot_use_old_independent_confirmation(self):
        self.completed_edit()
        self.grant_proof()
        with closing(self.store._connect()) as conn, conn:
            conn.execute("UPDATE write_api_requests SET response_json=replace(response_json,'Saved','Other')")
            conn.execute("UPDATE operation_receipts SET writer_error='invented_error'")
        self.assert_fenced()

    def test_frontend_cannot_supply_success_proof_or_select_an_ack_phase(self):
        self.completed_edit()
        before = self.store.dashboard_pending_writes()
        for field, value in (("response", {"outcome": "confirmed"}), ("sha", "a" * 64),
                             ("phase", "finalized"), ("clearance", True), ("receipt", {})):
            with self.subTest(field=field), self.assertRaises(StorageOwnerError):
                self.owner.dispatch({"version": 1, "op": "dashboard_completed_ack",
                                     "scope": "ad:314:update", "key": "local-key", field: value})
        for operation in ("complete", "operator_clear", "runtime_clearance", "sql", "finalize"):
            with self.subTest(operation=operation), self.assertRaises(StorageOwnerError):
                self.owner.dispatch({"version": 1, "op": operation, "key": "local-key"})
        self.assertEqual(self.store.dashboard_pending_writes(), before)

    def test_changed_scope_key_payload_or_target_cannot_borrow_confirmation(self):
        self.completed_edit()
        self.grant_proof()
        original = self.store.dashboard_pending_writes()[0]
        candidates = [replace(original, idempotency_key="different-key"),
                      replace(original, payload_json='{"title":"Different title"}'),
                      replace(original, scope="ad:315:update", resource_key="ad:315", ad_id="315", path="/api/write/ads/315"),
                      replace(original, acknowledged=True)]
        for pending in candidates:
            with self.subTest(pending=pending), self.assertRaises((StorageOwnerError, ValueError)):
                self.owner.dispatch({"version": 1, "op": "dashboard_confirmed_response", "intent": _pending_as_json(pending)})
        self.assertEqual(self.store.dashboard_pending_writes(), (original,))

    def test_old_owner_client_does_not_gain_write_authority_from_a_flag(self):
        client = StorageOwnerClient(Path("/tmp/not-a-real-owner.sock"), expected_owner_uid=os.geteuid() + 100)
        with self.assertRaisesRegex(ValueError, "not yet attested"):
            dashboard.create_server(client, port=0, write_proxy=self.proxy)
        with self.assertRaises(ValueError):
            dashboard.create_server(self.store, port=0, write_proxy=self.proxy)
        with self.assertRaises(ValueError):
            replace(self.proxy, local_reconciliation_enabled=True)
        with self.assertRaises(ValueError):
            replace(self.proxy, owner_reconciliation_enabled=1)

    def test_profile_and_confirmed_reply_are_strictly_typed(self):
        self.completed_edit()
        pending = self.store.dashboard_pending_writes()[0]
        original_call = self.client._call
        try:
            for answer in ({"response_json": 200}, {"response_json": "{}"},
                           {"response_json": '{"idempotency_key":"local-key","platform_retry_authorized":true}'},
                           {"response_json": "{}", "clearance": True}):
                self.client._call = lambda *args, _answer=answer, **kwargs: _answer
                with self.subTest(answer=answer), self.assertRaises(StorageOwnerError):
                    self.client.confirmed_dashboard_write_response(pending)
            for version in (True, 1.0, "1", None):
                with self.subTest(version=version), self.assertRaises(StorageOwnerError):
                    self.owner.dispatch({"version": version, "op": "dashboard_completion_profile"})
        finally:
            self.client._call = original_call


if __name__ == "__main__":
    unittest.main()
