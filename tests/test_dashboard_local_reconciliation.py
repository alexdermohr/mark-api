"""Socket-free dispatch tests complement the unchanged real HTTP lifecycle gates.

Only HTTP transport is in memory. The authenticated Write handler, service,
operation receipts, request ledger and two-phase pending store are real.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from dataclasses import replace
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mark_api import dashboard, write_api
from mark_api.storage import SnapshotStore
from mark_api.storage_owner import StorageOwnerClient
from test_dashboard_full_lifecycle_e2e import (
    BACKEND_TOKEN, NOW, UI_TOKEN, SyntheticPlatform, service_for,
)


def handler_instance(handler_type, path, body, headers):
    handler = handler_type.__new__(handler_type)
    handler.path = path
    handler.headers = Message()
    for key, value in headers.items():
        handler.headers[key] = value
    if handler.headers.get("Content-Length") is None:
        handler.headers["Content-Length"] = str(len(body))
    handler.rfile = io.BytesIO(body)
    handler.connection = SimpleNamespace(gettimeout=lambda: None, settimeout=lambda _: None)
    handler.server = SimpleNamespace(server_address=("127.0.0.1", 8765))
    return handler


class LocalReconciliationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SnapshotStore(Path(temporary.name) / "local.sqlite")
        self.platform = SyntheticPlatform()
        self.store.append_ad_snapshot(self.platform.ads["314"])
        self.access = write_api.WriteApiAccess(
            principal="synthetic-lifecycle-only", bearer_token=BACKEND_TOKEN,
            capabilities=frozenset(write_api.WriteCapability) - {write_api.WriteCapability.CREATE_MEDIA},
            writes_enabled=True,
        )
        self.proxy = dashboard.DashboardWriteProxy(
            host="127.0.0.1", port=8766, bearer_token=BACKEND_TOKEN, ui_token=UI_TOKEN,
            backend_principal=self.access.principal, local_reconciliation_enabled=True,
        )
        self.backend_requests = []
        self.reopen()

    def reopen(self):
        self.store = SnapshotStore(self.store.path)
        self.backend = write_api._handler_factory(
            service=service_for(self.platform, self.store), media_service=None,
            media_stager=None, store=self.store, access=self.access,
            clock=lambda: NOW, body_read_timeout_seconds=10,
            claim_owner="synthetic-dispatch", execution_lock=None, fence_witness=None,
        )
        self.frontend = dashboard._handler_factory(
            self.store, None, None, self.proxy, None,
        )

    def backend_connection(self, *_args, **_kwargs):
        harness = self

        class Connection:
            def request(self, method, path, *, body, headers):
                harness.backend_requests.append((method, path))
                handler = handler_instance(harness.backend, path, body, headers)
                handler._send_bytes = lambda status, raw, **kw: setattr(
                    self, "response", SimpleNamespace(
                        status=status, read=lambda _: raw,
                        getheader=lambda name, default=None: (
                            "application/json" if name == "Content-Type" else default
                        ),
                    ),
                )
                handler._dispatch_write(method)

            def getresponse(self):
                return self.response

            def close(self):
                pass

        return Connection()

    def send(self, method="PATCH", path="/api/write/ads/314", payload=None, key="local-key"):
        raw = b"" if payload is None else json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765",
            "X-Mark-Dashboard-Write": "1", "X-Mark-Dashboard-Token": UI_TOKEN,
        }
        if key is not None:
            headers["Idempotency-Key"] = key
        handler = handler_instance(self.frontend, path, raw, headers)
        result = []
        handler._send = lambda status, body, _content_type, **kw: result.append(
            (status, json.loads(body), kw)
        )
        with patch.object(dashboard.http.client, "HTTPConnection", side_effect=self.backend_connection):
            if path == "/api/dashboard/pending-writes/ack":
                handler._ack_dashboard_pending_write()
            else:
                handler._proxy_write(method)
        self.assertEqual(len(result), 1)
        return result[0]

    def ack(self, scope="ad:314:update", key="local-key"):
        return self.send("POST", "/api/dashboard/pending-writes/ack",
                         {"scope": scope, "idempotency_key": key}, key=None)

    def completed_edit(self):
        result = self.send(payload={"title": "Saved title"})
        self.assertEqual(result[0], 200, result)
        return result

    def assert_fenced(self):
        before = self.store.dashboard_pending_writes()
        count = len(self.backend_requests)
        self.assertEqual(self.send(payload={"title": "Saved title"})[0], 409)
        self.assertEqual(self.ack()[0], 409)
        self.assertEqual(self.store.dashboard_pending_writes(), before)
        self.assertEqual(len(self.backend_requests), count)

    def test_replay_restart_and_both_ack_phases_are_local(self):
        first = self.completed_edit()
        self.reopen()  # fresh handler closures and a fresh SnapshotStore, before ACK
        replay = self.send(payload={"title": "Saved title"})
        self.assertEqual(replay[:2], first[:2])
        self.assertTrue(replay[2]["idempotency_replayed"])
        self.assertEqual(self.ack()[:2], (200, {"acknowledged": True, "finalized": False}))
        self.reopen()  # restart between the durable phases, including replay
        self.assertEqual(self.send(payload={"title": "Saved title"})[:2], first[:2])
        self.assertEqual(self.ack()[:2], (200, {"acknowledged": True, "finalized": True}))
        self.assertEqual(self.store.dashboard_pending_writes(), ())
        self.assertEqual(self.ack()[0], 409)  # a third ACK has no authority
        self.assertEqual(self.send(payload={"title": "Saved title"})[:2], first[:2])
        self.assertEqual(self.store.dashboard_pending_writes(), ())
        self.assertEqual(self.backend_requests, [("PATCH", "/api/write/ads/314")])
        self.assertEqual(self.platform.effects, ["update_content"])

    def test_same_key_payload_route_target_and_key_collisions_are_fenced(self):
        self.completed_edit()
        before = self.store.dashboard_pending_writes()
        for method, path, payload in (
            ("PATCH", "/api/write/ads/314", {"title": "Forged title"}),
            ("PATCH", "/api/write/ads/315", {"title": "Saved title"}),
            ("POST", "/api/write/ads/314/pause", None),
        ):
            with self.subTest(path=path, payload=payload):
                self.assertEqual(self.send(method, path, payload)[0], 409)
        self.assertEqual(self.ack(scope="ad:314:pause")[0], 409)
        self.assertEqual(self.ack(key="another-key")[0], 409)
        for suffix in ("?", "?other=route", "#other-route"):
            self.assertEqual(self.send(path="/api/write/ads/314" + suffix,
                                       payload={"title": "Saved title"})[0], 404)
        self.assertEqual(self.store.dashboard_pending_writes(), before)
        self.assertEqual(len(self.backend_requests), 1)
        self.assertEqual(self.platform.effects, ["update_content"])

    def test_second_ack_rechecks_completion_before_finalizing(self):
        self.completed_edit()
        self.assertEqual(self.ack()[:2], (200, {"acknowledged": True, "finalized": False}))
        before = self.store.dashboard_pending_writes()
        self.assertTrue(before[0].acknowledged)
        with self.store._connect() as connection:
            connection.execute("UPDATE write_api_requests SET response_status = 202")
        self.reopen()
        self.assert_fenced()
        self.assertEqual(self.store.dashboard_pending_writes(), before)
        with self.store._connect() as connection:
            connection.execute("UPDATE write_api_requests SET response_status = 200")
        self.assertEqual(self.ack()[:2], (200, {"acknowledged": True, "finalized": True}))

    def test_mocked_http_success_without_a_trusted_receipt_stays_fenced(self):
        response = SimpleNamespace(
            status=200, read=lambda _: b'{"status":"ok"}',
            getheader=lambda name, default=None: "application/json" if name == "Content-Type" else default,
        )
        connection = SimpleNamespace(
            request=lambda *_args, **_kw: None, getresponse=lambda: response, close=lambda: None,
        )
        with patch.object(self, "backend_connection", return_value=connection):
            self.assertEqual(self.send(payload={"title": "Saved title"})[0], 200)
        self.assertIsNone(self.store.write_api_request("local-key"))
        self.assert_fenced()
        self.assertEqual(self.platform.effects, [])

    def test_uncertain_incomplete_missing_and_tampered_results_cannot_ack_or_replay(self):
        self.completed_edit()
        original = self.store.write_api_request("local-key")
        response = json.loads(original.response_json)
        changes = [
            ("state", "in_progress"), ("execution_started_at", None),
            ("response_status", 202), ("request_sha256", "0" * 64),
            ("response_json", json.dumps(response)),  # noncanonical
            ("response_json", '{"idempotency_key":"local-key"}'),
        ]
        for name, value in (
            ("outcome", "ambiguous"), ("operation", "pause"),
            ("ad_id", "315"), ("authorization_by", "browser-forgery"),
            ("authorization_reference", "write-api:other-key"),
            ("completed_at", "2026-10-22T10:00:01+00:00"),
            ("writer_invoked", False),
        ):
            forged = json.loads(original.response_json)
            forged["operation_receipt"][name] = value
            changes.append(("response_json", dashboard._json_bytes(forged).decode()))
        for column, value in changes:
            with self.subTest(column=column, value=value):
                with self.store._connect() as connection:
                    if column == "state":
                        connection.execute(
                            "UPDATE write_api_requests SET state = 'in_progress', "
                            "completed_at = NULL, response_status = NULL, response_json = NULL"
                        )
                    else:
                        connection.execute(f"UPDATE write_api_requests SET {column} = ?", (value,))
                self.assert_fenced()
                prior = getattr(original, column)
                if hasattr(prior, "isoformat"):
                    prior = prior.isoformat()
                with self.store._connect() as connection:
                    if column == "state":
                        connection.execute(
                            "UPDATE write_api_requests SET state = 'completed', "
                            "completed_at = ?, response_status = ?, response_json = ?",
                            (original.completed_at.isoformat(), original.response_status, original.response_json),
                        )
                    else:
                        connection.execute(f"UPDATE write_api_requests SET {column} = ?", (prior,))
        self.assertEqual(self.platform.effects, ["update_content"])

    def test_missing_or_colliding_operation_receipt_is_not_a_completion_proof(self):
        self.completed_edit()
        with self.store._connect() as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(operation_receipts)") if row[1] != "id"]
            names = ",".join(columns)
            connection.execute(f"INSERT INTO operation_receipts ({names}) SELECT {names} FROM operation_receipts")
        self.assert_fenced()
        with self.store._connect() as connection:
            connection.execute("DELETE FROM operation_receipts")
        self.assert_fenced()

    def test_actual_ambiguous_backend_result_keeps_the_fence(self):
        def uncertain_update(*args, **kwargs):
            self.platform.effects.append("update_content")
            raise RuntimeError("synthetic uncertain platform action")

        with patch.object(self.platform, "update_content", side_effect=uncertain_update):
            result = self.send(payload={"title": "Saved title"})
        self.assertEqual(result[0], 202, result)
        self.assertEqual(result[1]["operation_receipt"]["outcome"], "ambiguous")
        self.reopen()
        self.assert_fenced()
        self.assertEqual(self.platform.effects, ["update_content"])
        self.assertEqual(len(self.backend_requests), 1)

    def test_persisted_pending_identity_must_match_every_field(self):
        self.completed_edit()
        original = self.store.dashboard_pending_writes()[0]
        for column, value in (
            ("scope", "ad:314:pause"), ("resource_key", "ad:315"),
            ("method", "POST"), ("path", "/api/write/ads/314/pause"),
            ("ad_id", "315"), ("payload_json", '{"title":"Other title"}'),
            ("payload_json", '{ "title": "Saved title" }'),
            ("idempotency_key", "another-key"),
        ):
            with self.subTest(column=column):
                with self.store._connect() as connection:
                    connection.execute(f"UPDATE dashboard_pending_writes SET {column} = ?", (value,))
                self.assert_fenced()
                with self.store._connect() as connection:
                    connection.execute(f"UPDATE dashboard_pending_writes SET {column} = ?", (getattr(original, column),))

    def test_opt_in_and_trusted_principal_are_both_required(self):
        self.completed_edit()
        configured = self.proxy
        for proxy in (
            replace(configured, backend_principal=None),
            replace(configured, backend_principal="other-principal"),
            replace(configured, local_reconciliation_enabled=False),
        ):
            with self.subTest(proxy=proxy):
                self.proxy = proxy
                self.reopen()
                self.assert_fenced()

    def test_owner_proxy_rejected_even_with_local_opt_in(self):
        owner = StorageOwnerClient.__new__(StorageOwnerClient)
        with self.assertRaisesRegex(ValueError, "not yet attested"):
            dashboard.create_server(owner, port=0, write_proxy=self.proxy)

    def test_unattested_container_cannot_use_local_completion(self):
        self.completed_edit()
        with patch.object(dashboard, "container_draft_denied", return_value=True):
            self.assert_fenced()

    def test_create_normalization_and_receipt_replay(self):
        payload = {"category_path": [" Freizeit ", "Sammeln"], "title": "Local create",
                   "description": "Normalized\r\ntext", "price_eur": 10}
        first = self.send("POST", "/api/write/ads", payload)
        self.assertEqual(first[0], 200, first)
        self.reopen()
        self.assertEqual(self.send("POST", "/api/write/ads", payload)[:2], first[:2])
        self.assertEqual(self.ack(scope="create")[0], 200)
        self.assertEqual(self.ack(scope="create")[0], 200)
        self.assertEqual(self.platform.effects, ["create"])
        self.assertEqual(len(self.backend_requests), 1)

    def test_full_crud_dispatch_replays_each_receipt_without_backend_contact(self):
        create = {"category_path": ["Freizeit", "Sammeln"], "title": "Local create",
                  "description": "Synthetic only", "price_eur": 10}
        steps = (
            ("POST", "/api/write/ads", create, "create"),
            ("PATCH", "/api/write/ads/315", {"title": "Edited title"}, "ad:315:update"),
            ("POST", "/api/write/ads/315/pause", None, "ad:315:pause"),
            ("POST", "/api/write/ads/315/activate", None, "ad:315:activate"),
            ("DELETE", "/api/write/ads/315", None, "ad:315:delete"),
        )
        for index, (method, path, payload, scope) in enumerate(steps):
            key = f"crud-{index}"
            with self.subTest(method=method, path=path):
                first = self.send(method, path, payload, key)
                self.assertEqual(first[0], 200, first)
                self.assertEqual(first[1]["operation_receipt"]["outcome"], "confirmed")
                self.reopen()
                self.assertEqual(self.send(method, path, payload, key)[:2], first[:2])
                for finalized in (False, True):
                    self.assertEqual(self.ack(scope, key)[:2], (
                        200, {"acknowledged": True, "finalized": finalized},
                    ))
                self.reopen()
                self.assertEqual(self.send(method, path, payload, key)[:2], first[:2])
                self.assertEqual(self.store.dashboard_pending_writes(), ())
                self.assertEqual(len(self.backend_requests), index + 1)
        self.assertEqual(self.platform.effects, [
            "create", "update_content", "set_state:paused", "set_state:active", "delete",
        ])


if __name__ == "__main__":
    unittest.main()
