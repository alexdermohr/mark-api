"""Synthetic protocol tests for an unfinished independent storage-owner slice.

These same-UID tests exercise the protocol and state transitions only.
The real OS trust boundary requires test_storage_owner_multiuid.py.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import socket
import sqlite3
import struct
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch

from mark_api.storage import SnapshotStore
from mark_api import storage_owner as owner_module
from mark_api.results import ReadResult
from mark_api.storage_owner import (
    StorageOwnerClient, StorageOwnerError, StorageOwnerServer, main,
    _attested_database, _record_from_json, _VERSION,
)


NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 64


class StorageOwnerUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="mark-storage-owner-")
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.db_directory = base / "database"
        self.db_directory.mkdir(mode=0o700)
        self.database = self.db_directory / "mark.sqlite"
        SnapshotStore(self.database)
        self.rpc_dir = base / "rpc"
        self.rpc_dir.mkdir(mode=0o700)
        self.rpc_dir.chmod(0o2710)
        self.sock = self.rpc_dir / "storage.sock"

    def make_service(self) -> StorageOwnerServer:
        instance = StorageOwnerServer(
            database=self.database, socket_path=self.sock,
            runtime_uid=os.geteuid() + 10001,
            runtime_gid=os.getegid(),
        )
        self.addCleanup(instance.stop)
        return instance

    def test_existing_schema_is_mandatory_and_no_missing_file_is_created(self) -> None:
        self.database.unlink()
        with self.assertRaises(StorageOwnerError):
            self.make_service()
        self.assertFalse(self.database.exists())

    def test_owner_start_does_not_silently_add_missing_index(self) -> None:
        # A full recovery schema can be intact while an optional index is
        # absent. A trusted storage owner must not perform *any* startup DDL.
        with sqlite3.connect(self.database) as conn:
            conn.execute("DROP INDEX idx_ad_snapshots_ad_id_observed")
        service = self.make_service()
        self.assertTrue(service.dispatch({"version": 1, "op": "ready"})["ready"])
        with sqlite3.connect(self.database) as conn:
            indexes = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_ad_snapshots_ad_id_observed'"
            ).fetchall()
        self.assertEqual(indexes, [])

    def test_missing_recovery_table_cannot_be_auto_repaired(self) -> None:
        with sqlite3.connect(self.database) as conn:
            conn.execute("DROP TABLE write_recovery_clearances")
        with self.assertRaises(sqlite3.DatabaseError):
            self.make_service()
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(
                conn.execute("SELECT name FROM sqlite_master "
                             "WHERE name='write_recovery_clearances'").fetchall(),
                [],
            )

    def test_wrong_source_uid_or_mode_refused_before_socket(self) -> None:
        self.database.chmod(0o644)
        with self.assertRaises(StorageOwnerError):
            self.make_service()
        self.assertFalse(self.sock.exists())

    def test_non_private_or_symlink_sidecar_is_rejected(self) -> None:
        sidecar = Path(str(self.database) + "-wal")
        sidecar.symlink_to(self.database)
        with self.assertRaises(StorageOwnerError):
            self.make_service()
        sidecar.unlink()
        sidecar.write_bytes(b"fake WAL")
        sidecar.chmod(0o644)
        with self.assertRaises(StorageOwnerError):
            self.make_service()
        self.assertFalse(self.sock.exists())

    def test_root_uid_runtime_and_equal_uid_cannot_create_broker(self) -> None:
        for peer_uid in (0, os.geteuid()):
            with self.subTest(peer_uid=peer_uid), self.assertRaises(StorageOwnerError):
                StorageOwnerServer(
                    database=self.database, socket_path=self.sock,
                    runtime_uid=peer_uid, runtime_gid=os.getegid(),
                )

    def test_stale_socket_and_symlink_are_not_removed(self) -> None:
        with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as old:
            old.bind(str(self.sock))
        with self.assertRaisesRegex(StorageOwnerError, "refusing to replace"):
            self.make_service()
        self.assertTrue(self.sock.is_socket())
        self.sock.unlink()
        self.sock.symlink_to("/tmp/fake-storage.sock")
        with self.assertRaisesRegex(StorageOwnerError, "refusing to replace"):
            self.make_service()
        self.assertTrue(self.sock.is_symlink())

    def test_dashboard_pending_owner_claim_and_paged_read_no_clearance(self) -> None:
        service = self.make_service()
        service.runtime_uid = os.geteuid()  # Framing-only; OS isolation tested separately.
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        self.assertEqual(client.dashboard_pending_writes(), ())
        for number in range(22):
            details = dict(
                scope=f"ad:{100 + number}:update",
                resource_key=f"ad:{100 + number}",
                idempotency_key=f"pending:{number:03}", method="PATCH",
                path=f"/api/write/ads/{100 + number}",
                payload_json='{"title":"known"}',
                ad_id=str(100 + number),
            )
            self.assertTrue(client.claim_dashboard_pending_write(**details))
            self.assertFalse(client.claim_dashboard_pending_write(**details))
        pending = client.dashboard_pending_writes()
        self.assertEqual(len(pending), 22)
        self.assertEqual([item.scope for item in pending],
                         [f"ad:{100 + number}:update" for number in range(22)])
        self.assertEqual(pending[0].payload_json, '{"title":"known"}')
        self.assertFalse(any(item.acknowledged for item in pending))
        with self.assertRaises(StorageOwnerError):
            service.dispatch({
                "version": 1, "op": "dashboard_pending_ack",
                "scope": "ad:100:update", "key": "pending:000",
            })
        with self.assertRaises(StorageOwnerError):
            service.dispatch({
                "version": 1, "op": "dashboard_pending_claim",
                "scope": "ad:123:update", "resource_key": "ad:123",
                "key": "pending:023", "method": "PATCH",
                "path": "/api/write/ads/123",
                "payload_json": '{"title":"known"}', "ad_id": "123",
                "sql": "DELETE FROM write_recovery_clearances",
            })
        self.assertEqual(len(client.dashboard_pending_writes()), 22)
        self.assertFalse(service.store.has_write_recovery_clearance(
            idempotency_key="pending:000", request_sha256=SHA,
        ))

    def test_pending_owner_rejects_forged_route_identity_before_any_claim(self) -> None:
        service = self.make_service()
        good = {
            "version": 1, "op": "dashboard_pending_claim",
            "scope": "ad:123:update", "resource_key": "ad:123",
            "key": "pending:unchanged", "method": "PATCH",
            "path": "/api/write/ads/123", "ad_id": "123",
            "payload_json": '{"title":"safe"}',
        }
        invalid = (
            {"scope": "create"},
            {"scope": "ad:124:update"},
            {"resource_key": "create"},
            {"resource_key": "ad:124"},
            {"ad_id": "124"},
            {"ad_id": None},
            {"method": "DELETE"},
            {"path": "/api/write/ads/124"},
            {"path": "/api/write/ads/123?ignored=1"},
            {"path": "/api/write/ads/%31%32%33"},
            {"path": "/api/write/ads/123/activate"},
            {"path": "/api/write/media/discard"},
            {"path": "/api/write/media/stage"},
            {"path": "/api/write/ads/123/../123"},
            {"payload_json": None},
            {"payload_json": '{"title":"ok","title":"different"}'},
            {"payload_json": '{"title":NaN}'},
            {"payload_json": '{"price":1e999}'},
            {"payload_json": '{"title":"\\ud800"}'},
            {"payload_json": '{"z":2, "a":1}'},
            {"payload_json": '{"a":' + '[' * 65 + '0' + ']' * 65 + '}'},
        )
        for changes in invalid:
            with self.subTest(changes=changes):
                with patch.object(service.store, "claim_dashboard_pending_write") as writer:
                    with self.assertRaises(StorageOwnerError):
                        service.dispatch({**good, **changes})
                    writer.assert_not_called()
                self.assertEqual(
                    SnapshotStore(self.database, create_if_missing=False)
                    .dashboard_pending_writes(), (),
                )
        # The valid exact claim remains allowed after all failed attempts.
        self.assertEqual(service.dispatch(good), {"created": True})
        self.assertEqual(service.dispatch(good), {"created": False})
        self.assertEqual(len(service.store.dashboard_pending_writes()), 1)

    def test_pending_owner_accepts_only_six_actual_platform_write_routes(self) -> None:
        service = self.make_service()
        from mark_api.storage_owner import _dashboard_pending_contract
        expected = (
            ("POST", "/api/write/ads", ("create", "create", None, True)),
            ("POST", "/api/write/media/ads", ("create-media", "create", None, True)),
            ("PATCH", "/api/write/ads/123", ("ad:123:update", "ad:123", "123", True)),
            ("DELETE", "/api/write/ads/124", ("ad:124:delete", "ad:124", "124", False)),
            ("POST", "/api/write/ads/125/pause", ("ad:125:pause", "ad:125", "125", False)),
            ("POST", "/api/write/ads/126/activate", ("ad:126:activate", "ad:126", "126", False)),
        )
        for index, (method, path, binding) in enumerate(expected):
            with self.subTest(method=method, path=path):
                self.assertEqual(_dashboard_pending_contract(method, path), binding)
                # Direct owner dispatch is the authoritative trust boundary,
                # not the HTTP dashboard helper.
                scope, resource, ad_id, has_body = binding
                if index == 1:
                    # The earlier Create fence must block simultaneous media
                    # Create; test classification without weakening the fence.
                    continue
                response = service.dispatch({
                    "version": 1, "op": "dashboard_pending_claim",
                    "scope": scope, "resource_key": resource,
                    "key": f"ui:platform:{index}", "method": method,
                    "path": path,
                    "payload_json": '{"title":"safe"}' if has_body else None,
                    "ad_id": ad_id,
                })
                self.assertEqual(response, {"created": True})
        self.assertEqual(len(service.store.dashboard_pending_writes()), 5)

    def test_dashboard_pending_owner_rejects_json_bombs_before_persistence(self) -> None:
        service = self.make_service()
        service.runtime_uid = os.geteuid()  # Framing only; real UID test separate.
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        malformed = (
            '{"title":"first","title":"second"}',
            '{"nested":{"key":1,"key":2}}',
            '{"price":NaN}', '{"price":Infinity}', '{"price":-Infinity}',
            '{"price":1e999}', '{"price":-1E999}',
            '{"nested":{"price":9e999}}',
        )
        for payload_json in malformed:
            with self.subTest(payload_json=payload_json):
                with self.assertRaises(StorageOwnerError):
                    client.claim_dashboard_pending_write(
                        scope="create", resource_key="create",
                        idempotency_key="pending:bad-json", method="POST",
                        path="/api/write/ads", payload_json=payload_json,
                        ad_id=None,
                    )
                self.assertEqual(client.dashboard_pending_writes(), ())
                self.assertTrue(client.ready())
                self.assertFalse(service.store.has_write_recovery_clearance(
                    idempotency_key="pending:bad-json", request_sha256=SHA,
                ))
        valid = '{"title":"single-value"}'
        self.assertTrue(client.claim_dashboard_pending_write(
            scope="create", resource_key="create",
            idempotency_key="pending:good-json", method="POST",
            path="/api/write/ads", payload_json=valid, ad_id=None,
        ))
        self.assertEqual(client.dashboard_pending_writes()[0].payload_json, valid)

    def test_dashboard_pending_owner_rejects_oversize_and_forged_pages(self) -> None:
        service = self.make_service()
        service.runtime_uid = os.geteuid()  # Framing-only; OS isolation tested separately.
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        invalid = dict(scope="scope:invalid", resource_key="resource:invalid",
                       idempotency_key="pending:invalid", method="POST",
                       path="/api/write/ads", payload_json=None, ad_id=None)
        with self.assertRaises(StorageOwnerError):
            client.claim_dashboard_pending_write(**dict(invalid, method="GET"))
        self.assertEqual(client.dashboard_pending_writes(), ())
        # A valid legacy pending body can exceed the bounded IPC frame.
        # Do not silently truncate or acknowledge it.
        service.store.claim_dashboard_pending_write(
            scope="scope:legacy", resource_key="resource:legacy",
            idempotency_key="pending:legacy", method="POST",
            path="/api/write/ads",
            payload_json='{"body":"' + ("x" * 40000) + '"}', ad_id=None,
        )
        with self.assertRaises(StorageOwnerError):
            client.dashboard_pending_writes()
        self.assertEqual(service.store.dashboard_pending_writes()[0].scope,
                         "scope:legacy")
        self.assertFalse(service.store.dashboard_pending_writes()[0].acknowledged)
        with patch.object(client, "_call", return_value={
            "rows": [{"scope": "fake"}], "more": False,
        }):
            with self.assertRaises(StorageOwnerError):
                client.dashboard_pending_writes()

    def test_owner_cancels_only_exact_unstarted_claim_with_fixed_failure(self) -> None:
        service = self.make_service()
        def call(op: str, **kwargs: object) -> dict[str, object]:
            return service.dispatch({"version": 1, "op": op, **kwargs})

        key, owner = "ui:never-began:1", "runtime:one"
        call("claim", key=key, sha=SHA, owner=owner, at=NOW.isoformat())
        for kwargs in (
            {"sha": "b" * 64}, {"owner": "runtime:other"},
            {"response_status": 200},
            {"response": {"operation_receipt": {"outcome": "confirmed"}}},
            {"clearance": True}, {"sql": "DELETE FROM write_api_requests"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(
                (ValueError, StorageOwnerError)
            ):
                call("cancel_unstarted", **dict(
                    key=key, sha=SHA, owner=owner, at=NOW.isoformat(),
                ) | kwargs)
        self.assertIsNone(service.store.write_api_request(key).completed_at)
        with patch.object(
            service.store, "complete_write_api_request",
            side_effect=AssertionError("executor arbitrary completion reached"),
        ), patch.object(
            service.store, "record_write_recovery_clearance",
            side_effect=AssertionError("unsafe clearance reached"),
        ):
            receipt = call(
                "cancel_unstarted", key=key, sha=SHA, owner=owner,
                at=NOW.isoformat(),
            )["record"]
        self.assertEqual(receipt["state"], "completed")
        self.assertIsNone(receipt["execution_started_at"])
        self.assertEqual(receipt["response_status"], 409)
        self.assertEqual(json.loads(receipt["response_json"]), {
            "error": "write_claim_cancelled_before_begin",
            "idempotency_key": key, "platform_retry_authorized": False,
        })
        self.assertEqual(
            call(
                "cancel_unstarted", key=key, sha=SHA, owner=owner,
                at=(NOW + timedelta(hours=2)).isoformat(),
            )["record"], receipt,
        )
        with self.assertRaises(ValueError):
            call("begin", key=key, sha=SHA, owner=owner, at=NOW.isoformat())
        self.assertFalse(service.store.has_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
        ))

        other = "ui:maybe-started:2"
        call("claim", key=other, sha=SHA, owner=owner, at=NOW.isoformat())
        started = call("begin", key=other, sha=SHA, owner=owner, at=NOW.isoformat())
        with self.assertRaisesRegex(ValueError, "begun"):
            call("cancel_unstarted", key=other, sha=SHA,
                 owner=owner, at=NOW.isoformat())
        self.assertEqual(call("read_request", key=other), started)

    def test_client_unstarted_cancel_receipt_is_bound_and_unforgeable(self) -> None:
        service = self.make_service()
        service.runtime_uid = os.geteuid()  # Framing only.
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        key, owner = "ui:socket-cancel", "runtime:one"
        claim = client.claim(key=key, sha=SHA, owner=owner, at=NOW)
        self.assertTrue(claim.created)
        cancelled = client.cancel_unstarted(key=key, sha=SHA, owner=owner, at=NOW)
        self.assertEqual(client.read_request(key), cancelled)
        self.assertIsNone(cancelled.execution_started_at)
        self.assertEqual(cancelled.response_status, 409)
        self.assertEqual(
            client.cancel_unstarted(
                key=key, sha=SHA, owner=owner,
                at=NOW + timedelta(days=1),
            ), cancelled,
        )
        with self.assertRaises(StorageOwnerError):
            client.begin(key=key, sha=SHA, owner=owner, at=NOW)
        baseline = service.dispatch({
            "version": 1, "op": "read_request", "key": key,
        })["record"]
        for altered in (
            {"response_status": 200}, {"claim_owner": "runtime:other"},
            {"response_json": '{"error":"success"}'},
            {"execution_started_at": NOW.isoformat()},
        ):
            with self.subTest(altered=altered):
                with patch.object(client, "_call", return_value={
                    "record": {**baseline, **altered},
                }):
                    with self.assertRaises(StorageOwnerError):
                        client.cancel_unstarted(
                            key=key, sha=SHA, owner=owner, at=NOW,
                        )

    def test_begin_and_unstarted_cancel_serialize_one_exclusive_transition(self) -> None:
        service = self.make_service()
        service.dispatch({
            "version": 1, "op": "claim", "key": "ui:race:1",
            "sha": SHA, "owner": "runtime:race", "at": NOW.isoformat(),
        })
        def attempt(op: str) -> bool:
            try:
                service.dispatch({
                    "version": 1, "op": op, "key": "ui:race:1",
                    "sha": SHA, "owner": "runtime:race", "at": NOW.isoformat(),
                })
            except ValueError:
                return False
            return True
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, ("begin", "cancel_unstarted")))
        self.assertEqual(outcomes.count(True), 1)
        record = service.store.write_api_request("ui:race:1")
        if record.execution_started_at is not None:
            self.assertEqual(record.state, "in_progress")
            self.assertIsNone(record.completed_at)
        else:
            self.assertEqual(record.state, "completed")
            self.assertEqual(record.response_status, 409)
        self.assertFalse(service.store.has_write_recovery_clearance(
            idempotency_key="ui:race:1", request_sha256=SHA,
        ))

    def test_exact_write_claim_and_start_persist_without_clearance(self) -> None:
        service = self.make_service()
        def call(op: str, **fields: object) -> dict[str, object]:
            return service.dispatch({"version": _VERSION, "op": op, **fields})

        created = call("claim", key="ui:claim:1", sha=SHA,
                       owner="runtime:one", at=NOW.isoformat())
        self.assertTrue(created["created"])
        self.assertIsNone(created["record"]["execution_started_at"])
        self.assertEqual(
            call("claim", key="ui:claim:1", sha=SHA,
                 owner="runtime:one", at=NOW.isoformat())["created"], False,
        )
        self.assertEqual(call("create_pending", exclude_key="ui:next", owner="runtime:one"),
                         {"pending": False})
        begun = call("begin", key="ui:claim:1", sha=SHA,
                     owner="runtime:one", at=NOW.isoformat())
        self.assertEqual(begun["record"]["state"], "in_progress")
        self.assertEqual(begun["record"]["execution_started_at"], NOW.isoformat())
        self.assertEqual(call("create_pending", exclude_key="ui:next", owner="runtime:one"),
                         {"pending": True})
        self.assertEqual(call("read_request", key="ui:claim:1")["record"],
                         begun["record"])
        with self.assertRaises(ValueError):
            call("begin", key="ui:claim:1", sha=SHA,
                 owner="runtime:one", at=NOW.isoformat())
        self.assertFalse(SnapshotStore(self.database, create_if_missing=False)
                         .has_write_recovery_clearance(
                             idempotency_key="ui:claim:1", request_sha256=SHA,
                         ))
        self.assertEqual(
            SnapshotStore(self.database, create_if_missing=False)
            .write_api_request("ui:claim:1").state, "in_progress",
        )

    def test_owner_quarantines_only_exact_started_write_with_fixed_unknown(self) -> None:
        """An executor cannot mint HTTP success or retire a possible effect."""
        service = self.make_service()

        def rpc(op: str, **fields: object) -> dict[str, object]:
            return service.dispatch({"version": _VERSION, "op": op, **fields})

        key, owner = "ui:ambiguous:maybe-started", "runtime:one"
        args = dict(key=key, sha=SHA, owner=owner, at=NOW.isoformat())
        rpc("claim", **args)
        with self.assertRaises(ValueError):
            rpc("quarantine_started", **args)
        self.assertIsNone(service.store.write_api_request(key).execution_started_at)
        rpc("begin", **args)
        for override in (
            {"sha": "b" * 64},
            {"owner": "runtime:other"},
            {"status": 200},
            {"response_status": 200},
            {"response_json": '{"operation_receipt":{"outcome":"confirmed"}}'},
            {"outcome": "confirmed"},
            {"platform_retry_authorized": True},
            {"sql": "DELETE FROM write_api_requests"},
            {"clearance": True},
        ):
            with self.subTest(override=override), self.assertRaises(
                (StorageOwnerError, ValueError)
            ):
                rpc("quarantine_started", **(args | override))
        self.assertEqual(service.store.write_api_request(key).state, "in_progress")
        receipt = rpc("quarantine_started", **args)["record"]
        expected = {
            "error": "write_outcome_unverified",
            "idempotency_key": key, "platform_retry_authorized": False,
        }
        self.assertEqual(receipt["state"], "completed")
        self.assertIsNotNone(receipt["execution_started_at"])
        self.assertEqual(receipt["response_status"], 202)
        self.assertEqual(json.loads(receipt["response_json"]), expected)
        self.assertEqual(
            rpc("quarantine_started", **(args | {
                "at": (NOW + timedelta(hours=4)).isoformat(),
            }))["record"], receipt,
        )
        self.assertTrue(rpc(
            "create_pending", exclude_key="next-create", owner=owner,
        )["pending"])
        self.assertFalse(service.store.has_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
        ))
        with self.assertRaisesRegex(ValueError, "already completed"):
            service.store.complete_write_api_request(
                idempotency_key=key, request_sha256=SHA, claim_owner=owner,
                response_status=200,
                response_json='{"operation_receipt":{"outcome":"confirmed"}}',
                completed_at=NOW,
            )
        # Root-owned operator review is a separate LOCAL method, never the
        # executor's typed RPC. Reconciliation must match the original key/SHA
        # and independently confirmed outcome before unblocking another create.
        with self.assertRaises(ValueError):
            service.store.record_write_recovery_clearance(
                idempotency_key=key, request_sha256=SHA,
                verification_kind="media_submit_reconciled",
                evidence_reference="runtime:untrusted-media-observation",
                observed_at=NOW,
            )
        service.store.record_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
            verification_kind="operator_verified_postread",
            evidence_reference="operator:synthetic-external-review",
            observed_at=NOW,
        )
        self.assertTrue(service.store.has_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
        ))
        self.assertFalse(rpc(
            "create_pending", exclude_key="next-create", owner=owner,
        )["pending"])
        with self.assertRaises(ValueError):
            rpc("begin", **args)
        self.assertEqual(
            SnapshotStore(self.database, create_if_missing=False).write_api_request(key),
            service.store.write_api_request(key),
        )

    def test_owner_quarantine_client_validates_exact_fixed_unknown_receipt(self) -> None:
        service = self.make_service()
        service.runtime_uid = os.geteuid()  # Functional framing test, not real UID authority
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        key, owner = "socket:started:unknown", "runtime:one"
        client.claim(key=key, sha=SHA, owner=owner, at=NOW)
        with self.assertRaises(StorageOwnerError):
            client.quarantine_started(key=key, sha=SHA, owner=owner, at=NOW)
        self.assertIsNone(client.read_request(key).execution_started_at)
        client.begin(key=key, sha=SHA, owner=owner, at=NOW)
        quarantined = client.quarantine_started(key=key, sha=SHA, owner=owner, at=NOW)
        self.assertEqual(client.read_request(key), quarantined)
        self.assertEqual(quarantined.response_status, 202)
        self.assertEqual(json.loads(quarantined.response_json)["error"],
                         "write_outcome_unverified")
        self.assertEqual(
            client.quarantine_started(
                key=key, sha=SHA, owner=owner, at=NOW + timedelta(days=1),
            ), quarantined,
        )
        for mutation in (
            {"response_status": 200},
            {"request_sha256": "b" * 64},
            {"claim_owner": "other"},
            {"response_json": '{"error":"success"}'},
            {"execution_started_at": None},
        ):
            with self.subTest(mutation=mutation):
                raw = service.dispatch({
                    "version": _VERSION, "op": "read_request", "key": key,
                })["record"]
                with patch.object(client, "_call", return_value={
                    "record": raw | mutation,
                }):
                    with self.assertRaises(StorageOwnerError):
                        client.quarantine_started(
                            key=key, sha=SHA, owner=owner, at=NOW,
                        )

    def test_started_quarantine_lost_response_preserves_original_unknown(self) -> None:
        service = self.make_service()
        service.runtime_uid = os.geteuid()  # Only trusted socket framing seam
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        key, owner = "socket:quarantine:lost-ack", "runtime:original"
        client.claim(key=key, sha=SHA, owner=owner, at=NOW)
        client.begin(key=key, sha=SHA, owner=owner, at=NOW)
        attempted = 0
        transport = client._call

        def lose_only_the_commit_reply(op: str, **fields: object) -> dict[str, object]:
            nonlocal attempted
            if op == "quarantine_started":
                attempted += 1
                service.dispatch({"version": _VERSION, "op": op, **fields})
                raise OSError("synthetic response lost after durable commit")
            return transport(op, **fields)

        with patch.object(client, "_call", side_effect=lose_only_the_commit_reply):
            with self.assertRaisesRegex(OSError, "response lost"):
                client.quarantine_started(key=key, sha=SHA, owner=owner, at=NOW)
        self.assertEqual(attempted, 1)
        original = client.read_request(key)
        self.assertIsNotNone(original)
        self.assertEqual(original.response_status, 202)
        self.assertEqual(json.loads(original.response_json), {
            "error": "write_outcome_unverified",
            "idempotency_key": key, "platform_retry_authorized": False,
        })
        self.assertTrue(client.create_pending(
            exclude_key="another-create", owner=owner,
        ))
        fresh = SnapshotStore(self.database, create_if_missing=False)
        self.assertEqual(fresh.write_api_request(key), original)
        self.assertTrue(fresh.create_recovery_pending(
            exclude_idempotency_key="another-create", claim_owner=owner,
        ))
        self.assertFalse(fresh.has_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
        ))

    def test_legacy_media_key_containing_unknown_label_is_not_quarantine(self) -> None:
        """Never misclassify an ordinary media 202 based on arbitrary key text."""
        service = self.make_service()
        key, owner = "media-write_outcome_unverified-1", "runtime:one"
        service.store.claim_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            requested_at=NOW, claim_owner=owner,
        )
        service.store.begin_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            execution_started_at=NOW, claim_owner=owner,
        )
        service.store.complete_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            claim_owner=owner, response_status=202,
            response_json=json.dumps({
                "idempotency_key": key,
                "operation_receipt": {
                    "writer_invoked": True, "outcome": "confirmed",
                },
                "media_persistence_confirmed": False,
                "platform_retry_authorized": False,
            }, sort_keys=True, separators=(",", ":")),
            completed_at=NOW,
        )
        # Existing media semantics: this is NOT an owner-fixed UNKNOWN.
        # The new quarantine path must not turn unrelated key text into an
        # unremovable recovery fence or a service/database error.
        self.assertFalse(service.store.create_recovery_pending(
            exclude_idempotency_key="different-create", claim_owner=owner,
        ))

    def test_unverified_label_in_nonmedia_202_key_does_not_block_create(self) -> None:
        """A legitimate ambiguous pause is not an owner-issued quarantine."""
        service = self.make_service()
        key, owner = "write_outcome_unverified", "runtime:one"
        service.store.claim_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            requested_at=NOW, claim_owner=owner,
        )
        service.store.begin_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            execution_started_at=NOW, claim_owner=owner,
        )
        payload = json.dumps({
            "idempotency_key": key, "operation_receipt": {
                "writer_invoked": True, "outcome": "ambiguous",
            }, "platform_retry_authorized": False,
        }, sort_keys=True, separators=(",", ":"))
        service.store.complete_write_api_request(
            idempotency_key=key, request_sha256=SHA, claim_owner=owner,
            response_status=202, response_json=payload, completed_at=NOW,
        )
        self.assertFalse(service.store.create_recovery_pending(
            exclude_idempotency_key="new-create", claim_owner=owner,
        ))
        self.assertFalse(SnapshotStore(
            self.database, create_if_missing=False,
        ).create_recovery_pending(
            exclude_idempotency_key="new-create", claim_owner=owner,
        ))

    def test_owner_refuses_new_begin_until_earlier_unknown_is_operator_cleared(self) -> None:
        """An unresolved independent-owner UNKNOWN must fence a new Begin."""
        service = self.make_service()

        def call(op: str, *, key: str, sha: str = SHA) -> dict[str, object]:
            return service.dispatch({
                "version": _VERSION, "op": op, "key": key, "sha": sha,
                "owner": "runtime:one", "at": NOW.isoformat(),
            })

        old_key, next_key = "write:quarantined:older", "write:next:blocked"
        call("claim", key=old_key)
        call("begin", key=old_key)
        call("quarantine_started", key=old_key)
        self.assertTrue(service.store.create_recovery_pending(
            exclude_idempotency_key=next_key, claim_owner="runtime:one",
        ))
        call("claim", key=next_key)
        with self.assertRaisesRegex(ValueError, "unresolved.*quarantine"):
            call("begin", key=next_key)
        next_record = service.store.write_api_request(next_key)
        self.assertEqual(next_record.state, "in_progress")
        self.assertIsNone(next_record.execution_started_at)
        # A separate SnapshotStore connection must preserve the same fence
        # across a process restart and while the owner transaction is idle.
        restarted = SnapshotStore(self.database, create_if_missing=False)
        with self.assertRaisesRegex(ValueError, "unresolved.*quarantine"):
            restarted.begin_write_api_request(
                idempotency_key=next_key, request_sha256=SHA,
                claim_owner="runtime:one", execution_started_at=NOW,
            )
        with self.assertRaises(ValueError):
            service.store.record_write_recovery_clearance(
                idempotency_key=old_key, request_sha256="b" * 64,
                verification_kind="operator_verified_postread",
                evidence_reference="operator:wrong-original-sha",
                observed_at=NOW,
            )
        with self.assertRaisesRegex(ValueError, "unresolved.*quarantine"):
            call("begin", key=next_key)
        # An unused claim may safely cancel without fabricating any platform
        # response; it does not unblock the original unresolved write.
        rejected = call("cancel_unstarted", key=next_key)["record"]
        self.assertEqual(rejected["response_status"], 409)
        self.assertIsNone(rejected["execution_started_at"])
        new_key = "write:after:operator-review"
        call("claim", key=new_key)
        with self.assertRaisesRegex(ValueError, "unresolved.*quarantine"):
            call("begin", key=new_key)
        service.store.record_write_recovery_clearance(
            idempotency_key=old_key, request_sha256=SHA,
            verification_kind="operator_verified_postread",
            evidence_reference="operator:synthetic-postread",
            observed_at=NOW,
        )
        begun = call("begin", key=new_key)["record"]
        self.assertIsNotNone(begun["execution_started_at"])
        self.assertEqual(begun["state"], "in_progress")

    def test_owner_begin_blocks_unsettled_started_write_without_quarantine(self) -> None:
        """A crashed/malicious executor cannot skip voluntary quarantine to Begin B."""
        service = self.make_service()

        def rpc(op: str, key: str) -> dict[str, object]:
            return service.dispatch({
                "version": _VERSION, "op": op, "key": key,
                "sha": SHA, "owner": "runtime:one", "at": NOW.isoformat(),
            })

        first, second = "write:begun:unsettled", "write:next:must-wait"
        rpc("claim", first)
        self.assertIsNotNone(rpc("begin", first)["record"]["execution_started_at"])
        rpc("claim", second)
        self.assertTrue(service.store.create_recovery_pending(
            exclude_idempotency_key=second, claim_owner="runtime:one",
        ))
        with self.assertRaisesRegex(ValueError, "unresolved started owner write"):
            rpc("begin", second)
        self.assertIsNone(service.store.write_api_request(second).execution_started_at)
        # Readback across a separate connection must preserve the original fence.
        persisted = SnapshotStore(self.database, create_if_missing=False)
        self.assertIsNotNone(persisted.write_api_request(first).execution_started_at)
        self.assertIsNone(persisted.write_api_request(second).execution_started_at)
        # Only independent operator reconciliation can settle the first write.
        service.store.record_write_recovery_clearance(
            idempotency_key=first, request_sha256=SHA,
            verification_kind="operator_verified_postread",
            evidence_reference="operator:synthetic-independent-postread",
            observed_at=NOW,
        )
        self.assertIsNotNone(rpc("begin", second)["record"]["execution_started_at"])

    def test_owner_begin_serializes_different_keys_under_concurrent_calls(self) -> None:
        """Independent-owner BEGIN IMMEDIATE must admit at most one started key."""
        service = self.make_service()
        keys = ("write:parallel:first", "write:parallel:second")
        for key in keys:
            service.dispatch({
                "version": _VERSION, "op": "claim", "key": key,
                "sha": SHA, "owner": "runtime:one", "at": NOW.isoformat(),
            })

        def begin(key: str) -> bool:
            try:
                service.dispatch({
                    "version": _VERSION, "op": "begin", "key": key,
                    "sha": SHA, "owner": "runtime:one", "at": NOW.isoformat(),
                })
                return True
            except ValueError as exc:
                self.assertIn("unresolved started owner write", str(exc))
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(begin, keys))
        self.assertEqual(sum(outcomes), 1)
        begun = [service.store.write_api_request(key).execution_started_at
                 is not None for key in keys]
        self.assertEqual(sum(begun), 1)
        self.assertTrue(service.store.create_recovery_pending(
            exclude_idempotency_key="write:parallel:new", claim_owner="runtime:one",
        ))

    def test_closed_rpc_table_denies_sql_clearances_completion_and_paths(self) -> None:
        service = self.make_service()
        calls = [
            {"version": 1, "op": "execute", "sql": "DELETE FROM write_api_requests"},
            {"version": 1, "op": "record_write_recovery_clearance",
             "key": "ui:claim:1", "sha": SHA},
            {"version": 1, "op": "complete", "key": "ui:claim:1", "response": {}},
            {"version": 1, "op": "clearance", "key": "ui:claim:1"},
            {"version": 1, "op": "claim", "key": "ui:claim:1", "sha": SHA,
             "owner": "runtime:one", "at": NOW.isoformat(),
             "allow_abandoned_takeover": True},
            {"version": 1, "op": "read_request", "key": "ui:claim:1",
             "filename": str(self.database)},
            {"version": 1, "op": "claim", "key": "ui:claim:1", "sha": SHA,
             "owner": "runtime:one", "at": NOW.isoformat(), "sql": "VACUUM"},
            {"version": 1, "op": "ready", "other": True},
        ]
        with patch.object(service.store, "record_write_recovery_clearance",
                          side_effect=AssertionError("unsafe clearance reached")), \
             patch.object(service.store, "complete_write_api_request",
                          side_effect=AssertionError("unsafe completion reached")):
            for request in calls:
                with self.subTest(request=request), self.assertRaises(StorageOwnerError):
                    service.dispatch(request)
        self.assertIsNone(SnapshotStore(self.database, create_if_missing=False)
                          .write_api_request("ui:claim:1"))

    def test_concurrent_claim_and_begin_cannot_erase_or_double_start(self) -> None:
        service = self.make_service()
        claim = {
            "version": 1, "op": "claim", "key": "ui:parallel:1", "sha": SHA,
            "owner": "runtime:one", "at": NOW.isoformat(),
        }
        with ThreadPoolExecutor(max_workers=8) as executor:
            receipts = list(executor.map(service.dispatch, [claim] * 8))
        self.assertEqual(sum(bool(row["created"]) for row in receipts), 1)
        start = dict(claim, op="begin")
        def start_once(_: int) -> bool:
            try:
                service.dispatch(start)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=8) as executor:
            started = list(executor.map(start_once, range(8)))
        self.assertEqual(sum(started), 1)
        self.assertTrue(
            service.dispatch({"version": 1, "op": "create_pending",
                              "exclude_key": "ui:other", "owner": "runtime:one"})[
                                  "pending"
            ]
        )
        self.assertIsNone(
            SnapshotStore(self.database, create_if_missing=False)
            .write_api_request("ui:parallel:1").completed_at
        )

    def test_same_uid_test_bypass_only_models_framing_not_uid_isolation(self) -> None:
        service = self.make_service()
        service.runtime_uid = os.geteuid()  # Unit-only seam; real service never allows it.
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        self.assertTrue(client.ready())
        self.assertIsNone(client.read_request("ui:frame:1"))
        created = client.claim(key="ui:frame:1", sha=SHA,
                               owner="runner:one", at=NOW)
        self.assertTrue(created.created)
        self.assertEqual(created.record.idempotency_key, "ui:frame:1")
        begun = client.begin(key="ui:frame:1", sha=SHA,
                             owner="runner:one", at=NOW)
        self.assertEqual(client.read_request("ui:frame:1"), begun)
        self.assertTrue(client.create_pending(exclude_key="ui:other", owner="runner:one"))
        with self.assertRaises(StorageOwnerError):
            client._call("complete", key="ui:frame:1")
        with self.assertRaises(StorageOwnerError):
            client.begin(key="ui:frame:1", sha=SHA, owner="runner:one", at=NOW)

    def test_client_rejects_colliding_fingerprint_and_claim_owner(self) -> None:
        service = self.make_service()
        service.runtime_uid = os.geteuid()  # Only same-UID framing test.
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        claimed = client.claim(key="ui:collision", sha=SHA,
                               owner="runtime:one", at=NOW)
        self.assertTrue(claimed.created)
        with self.assertRaisesRegex(StorageOwnerError, "binding mismatch"):
            client.claim(key="ui:collision", sha="b" * 64,
                         owner="runtime:one", at=NOW)
        with self.assertRaisesRegex(StorageOwnerError, "binding mismatch"):
            client.claim(key="ui:collision", sha=SHA,
                         owner="runtime:other", at=NOW)
        with self.assertRaisesRegex(StorageOwnerError, "uncertain"):
            client.begin(key="ui:collision", sha=SHA,
                         owner="runtime:other", at=NOW)
        self.assertIsNone(client.read_request("ui:collision").execution_started_at)
        self.assertFalse(client.create_pending(
            exclude_key="ui:collision-next", owner="runtime:one",
        ))

    def test_client_rejects_mismatched_readback_key_even_from_owner_uid(self) -> None:
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        source = self.make_service()
        record = source.dispatch({
            "version": 1, "op": "claim", "key": "ui:elsewhere",
            "sha": SHA, "owner": "runtime:one", "at": NOW.isoformat(),
        })["record"]
        with patch.object(client, "_call", return_value={"record": record}):
            with self.assertRaisesRegex(StorageOwnerError, "binding mismatch"):
                client.read_request("ui:expected")

    def test_foreign_uid_rejected_before_any_rpc_mutation(self) -> None:
        service = self.make_service()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        with self.assertRaises(StorageOwnerError):
            client.claim(key="ui:forbidden", sha=SHA,
                         owner="runner:one", at=NOW)
        self.assertIsNone(SnapshotStore(self.database, create_if_missing=False)
                          .write_api_request("ui:forbidden"))

    def test_lost_or_untrusted_reply_never_triggers_hidden_retry(self) -> None:
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        with self.assertRaisesRegex(StorageOwnerError, "uncertain"):
            client.claim(key="ui:unavailable", sha=SHA,
                         owner="runner:one", at=NOW)
        self.assertIsNone(SnapshotStore(self.database, create_if_missing=False)
                          .write_api_request("ui:unavailable"))

    def test_wrong_response_type_rejected_and_no_auto_retry(self) -> None:
        with self.assertRaises(StorageOwnerError):
            _record_from_json({"state": "completed"})

    def test_literal_sqlite_filename_with_question_and_hash_is_not_uri_query(self) -> None:
        unusual = self.db_directory / "mark?#literal.sqlite"
        SnapshotStore(unusual)
        service = StorageOwnerServer(
            database=unusual, socket_path=self.sock,
            runtime_uid=os.geteuid() + 10001, runtime_gid=os.getegid(),
        )
        self.addCleanup(service.stop)
        self.assertTrue(service.dispatch({"version": 1, "op": "ready"})["ready"])
        self.assertTrue(unusual.exists())

    def test_existing_private_inode_not_replaced(self) -> None:
        identity = _attested_database(self.database, os.geteuid())
        self.assertEqual(identity[:2], (self.database.stat().st_dev,
                                        self.database.stat().st_ino))
        service = self.make_service()
        replacement = self.db_directory / "replacement.sqlite"
        self.database.rename(replacement)
        SnapshotStore(self.database)
        with self.assertRaisesRegex(StorageOwnerError, "replaced"):
            service.dispatch({"version": 1, "op": "ready"})

    def test_stop_does_not_release_sqlite_owner_while_rpc_is_in_flight(self) -> None:
        """A stopped authority must never publish a new generation early.

        A real accepted RPC is held just before persistent BEGIN. Its Unix
        connection must finish and the SQLite request must be committed
        before stop() can return or remove the socket name.
        """
        service = self.make_service()
        service.runtime_uid = os.geteuid()  # Framing-only in-process seam.
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        client.claim(key="ui:shutdown:1", sha=SHA, owner="runtime:one", at=NOW)
        entered, release, stop_done = Event(), Event(), Event()
        writes: list[object] = []
        exceptions: list[BaseException] = []
        begin = service.store.begin_write_api_request

        def blocked_begin(**kw: object) -> object:
            entered.set()
            if not release.wait(timeout=6):
                raise AssertionError("test blocked storage request was never released")
            return begin(**kw)

        def writer() -> None:
            try:
                writes.append(client.begin(
                    key="ui:shutdown:1", sha=SHA, owner="runtime:one", at=NOW,
                ))
            except BaseException as exc:
                exceptions.append(exc)

        def shutdown() -> None:
            try:
                service.stop()
            except BaseException as exc:
                exceptions.append(exc)
            finally:
                stop_done.set()

        with patch.object(
            service.store, "begin_write_api_request", side_effect=blocked_begin,
        ):
            request = Thread(target=writer, daemon=True)
            request.start()
            self.assertTrue(entered.wait(timeout=3))
            closing_thread = Thread(target=shutdown, daemon=True)
            closing_thread.start()
            try:
                self.assertFalse(
                    stop_done.wait(timeout=0.3),
                    "storage owner stopped before an outstanding RPC completed",
                )
            finally:
                release.set()
                request.join(timeout=7)
                closing_thread.join(timeout=7)
        self.assertFalse(request.is_alive())
        self.assertFalse(closing_thread.is_alive())
        self.assertFalse(exceptions, exceptions)
        self.assertTrue(stop_done.is_set())
        self.assertEqual(len(writes), 1)
        self.assertFalse(self.sock.exists())
        record = SnapshotStore(self.database, create_if_missing=False).write_api_request(
            "ui:shutdown:1",
        )
        self.assertIsNotNone(record)
        self.assertEqual(record.state, "in_progress")
        self.assertIsNotNone(record.execution_started_at)

    def test_inventory_restart_cannot_ignore_durable_unfinished_attempt(self) -> None:
        first = self.make_service()
        first.runtime_uid = os.geteuid()  # Same-UID framing, not OS separation.
        first.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        attempt = client.begin_sync_attempt(
            source="previous-generation", started_at=NOW,
        )
        first.stop()  # Simulate an owner restart and loss of its in-memory stage.
        restarted = self.make_service()
        restarted.runtime_uid = os.geteuid()
        restarted.start()
        with self.assertRaises(StorageOwnerError):
            client.begin_sync_attempt(
                source="new-source", started_at=NOW + timedelta(minutes=1),
            )
        store = SnapshotStore(self.database, create_if_missing=False)
        self.assertEqual(store.sync_status()["state"], "in_progress")
        self.assertEqual(store.sync_status()["latest_attempt"]["id"], attempt)
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM sync_attempts"
            ).fetchone()[0], 1)
        # Reconciliation requires trusted store authority; the executor has
        # no RPC to discard or re-label the old unknown result.
        store.fail_sync_attempt(
            attempt, source="previous-generation",
            completed_at=NOW + timedelta(minutes=2),
            error_kind="persistence_error",
        )
        next_attempt = client.begin_sync_attempt(
            source="new-source", started_at=NOW + timedelta(minutes=3),
        )
        self.assertEqual(next_attempt, attempt + 1)

    def test_out_of_order_sync_completions_are_not_spoofed_or_rejected(self) -> None:
        """Latest started and latest completed-success are distinct journal facts."""
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        t0, t1, t2, t3 = (
            NOW, NOW + timedelta(minutes=1),
            NOW + timedelta(minutes=2), NOW + timedelta(minutes=3),
        )
        older = store.begin_sync_attempt(source="overlap", started_at=t0)
        newer = store.begin_sync_attempt(source="overlap", started_at=t1)
        store.append_inventory_result(
            ReadResult.success_empty(()), source="overlap", observed_at=t2,
            attempt_id=newer, completed_at=t2,
        )
        store.append_inventory_result(
            ReadResult.success_empty(()), source="overlap", observed_at=t3,
            attempt_id=older, completed_at=t3,
        )
        actual = store.sync_status()
        self.assertEqual(actual["latest_attempt"]["id"], newer)
        self.assertEqual(actual["last_successful_attempt"]["id"], older)
        service.runtime_uid = os.geteuid()  # Framing only.
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        self.assertEqual(client.sync_status(), actual)
        # An older success cannot win the store's completion-time ordering
        # unless it was actually completed strictly later than the new one.
        forged = dict(actual)
        forged["last_successful_attempt"] = dict(
            actual["last_successful_attempt"], completed_at=t2.isoformat(),
        )
        with patch.object(client, "_call", return_value={"sync_status": forged}):
            with self.assertRaises(StorageOwnerError):
                client.sync_status()

    def test_linux_ucred_uid_is_unsigned_32_bit(self) -> None:
        """Even though deployed UIDs are 50044/50046, Linux uid_t is unsigned."""
        for uid in (50044, 50046, 2**31, 2**32 - 2):
            with self.subTest(uid=uid):
                raw = struct.pack("=iII", os.getpid(), uid, 50044)
                self.assertEqual(owner_module._linux_ucred_uid(raw), uid)
        for raw in (b"", bytes(11), bytes(13)):
            with self.subTest(length=len(raw)), self.assertRaises(StorageOwnerError):
                owner_module._linux_ucred_uid(raw)

    def test_shutdown_does_not_unlink_after_four_second_inflight_rpc(self) -> None:
        """The 4s serve_forever join is not a 4s handler drain deadline."""
        service = self.make_service()
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        client.claim(key="ui:slow:1", sha=SHA, owner="runtime:one", at=NOW)
        entered, release, done = Event(), Event(), Event()
        errors: list[BaseException] = []
        orig = service.store.begin_write_api_request
        def delayed(**kw: object) -> object:
            entered.set()
            if not release.wait(timeout=7):
                raise AssertionError("slow RPC was not released")
            return orig(**kw)
        def request() -> None:
            try:
                client.begin(key="ui:slow:1", sha=SHA, owner="runtime:one", at=NOW)
            except BaseException as exc:
                errors.append(exc)
        def close() -> None:
            try:
                service.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()
        with patch.object(service.store, "begin_write_api_request", side_effect=delayed):
            writer = Thread(target=request, daemon=True)
            writer.start()
            self.assertTrue(entered.wait(3))
            stopper = Thread(target=close, daemon=True)
            stopper.start()
            try:
                self.assertFalse(done.wait(timeout=4.3),
                                 "socket unlinked before in-flight RPC ended")
                self.assertTrue(self.sock.exists())
            finally:
                release.set()
                writer.join(9)
                stopper.join(9)
        self.assertFalse(writer.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertFalse(errors, errors)
        self.assertTrue(done.is_set())
        self.assertFalse(self.sock.exists())
        persisted = SnapshotStore(self.database, create_if_missing=False).write_api_request(
            "ui:slow:1",
        )
        self.assertIsNotNone(persisted.execution_started_at)

    def test_history_readback_is_bounded_and_orders_real_instants(self) -> None:
        from mark_api.domain import AdSnapshot, LifecycleState
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        timeline = (
            NOW + timedelta(hours=1),
            NOW + timedelta(minutes=2),
            NOW + timedelta(hours=1),
            NOW + timedelta(minutes=3),
        )
        entries = tuple(AdSnapshot(
            ad_id="123", observed_at=when,
            source='mark:source-v1:{"source":"opaque origin"}',
            lifecycle_state=LifecycleState.ACTIVE,
            title="Record " + str(index),
            description="Zeile Eins\n" + "x" * 7800,
            metric_source="management+stats",
            views=index,
        ) for index, when in enumerate(timeline))
        for row in entries:
            store.append_ad_snapshot(row)
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        self.assertEqual(client.ad_history("123"), store.ad_history("123"))
        self.assertEqual(client.ad_history("unknown"), ())

    def test_history_watermark_disallows_late_insert_but_next_read_includes(self) -> None:
        from mark_api.domain import AdSnapshot
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        for n in range(20):
            store.append_ad_snapshot(AdSnapshot(
                ad_id="123", observed_at=NOW, source="first", views=n,
            ))
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        late = AdSnapshot(ad_id="123", observed_at=NOW - timedelta(minutes=1),
                          source="late", views=99)
        old = client._call
        fired = False
        def insert_between_pages(op: str, **fields: object) -> dict[str, object]:
            nonlocal fired
            result = old(op, **fields)
            if op == "ad_history_page" and not fired:
                fired = True
                store.append_ad_snapshot(late)
            return result
        with patch.object(client, "_call", side_effect=insert_between_pages):
            before = client.ad_history("123")
        self.assertTrue(fired)
        self.assertNotIn(late, before)
        self.assertEqual(len(before), 20)
        self.assertIn(late, client.ad_history("123"))

    def test_history_terminal_page_cannot_overrun_reply_frame(self) -> None:
        from mark_api.domain import AdSnapshot
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        snapshot = AdSnapshot(ad_id="frame", source="owner", observed_at=NOW,
                              title="single row")
        store.append_ad_snapshot(snapshot)
        raw_record = owner_module._snapshot_as_json(snapshot)
        optimistic = {
            "ok": True,
            "result": {
                "watermark": 1, "more": True,
                "rows": [{"id": 1, "snapshot": raw_record}],
            },
        }
        # false is one byte larger than true. A final-page response must be
        # accounted for with its actual/worst-case byte length.
        tiny_limit = len(owner_module._canonical_json(optimistic).encode("utf-8"))
        with patch.object(owner_module, "_MAX_FRAME", tiny_limit):
            with self.assertRaises(StorageOwnerError):
                service.dispatch({
                    "version": 1, "op": "ad_history_page", "ad_id": "frame",
                    "after_id": 0, "watermark": None,
                })

    def test_history_readback_preserves_valid_empty_optional_text(self) -> None:
        from mark_api.domain import AdSnapshot
        service = self.make_service()
        stored = AdSnapshot(ad_id="empty", source="owner", observed_at=NOW,
                            title="", description="")
        SnapshotStore(self.database, create_if_missing=False).append_ad_snapshot(stored)
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        self.assertEqual(client.ad_history("empty"), (stored,))

    def test_history_pagination_rejects_forged_sql_and_cursor_envelope(self) -> None:
        service = self.make_service()
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        for fields in (
            {"ad_id": "123", "after_id": 0, "watermark": None, "sql": "DELETE FROM write_api_requests"},
            {"ad_id": "123", "after_id": True, "watermark": None},
            {"ad_id": "123", "after_id": -1, "watermark": None},
        ):
            with self.subTest(fields=fields), self.assertRaises(StorageOwnerError):
                service.dispatch({"version": 1, "op": "ad_history_page", **fields})
        with patch.object(client, "_call", return_value={
            "watermark": 10, "more": True, "rows": [],
        }), self.assertRaises(StorageOwnerError):
            client.ad_history("123")

    def test_email_only_query_and_analytics_use_typed_owner_not_raw_sqlite(self) -> None:
        from mark_api.domain import InboundMessageEvent, AdClassification
        from mark_api.analytics import AnalyticsService
        from mark_api.query import MarkQueryService
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        events = tuple(InboundMessageEvent(
            ad_id="email-only", conversation_id=f"buyer-{i % 3}",
            provider_message_id=f"provider:{i:02}",
            observed_at=NOW + timedelta(minutes=(20 - i)),
            source="email:sample",
        ) for i in range(19))
        self.assertEqual(store.append_inbound_message_events(events), 19)
        expected_classification = AdClassification(
            ad_id="email-only", observed_at=NOW,
            source="manual", city="Magdeburg",
        )
        store.append_classification(expected_classification)
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        self.assertEqual(client.inbound_message_history("email-only"),
                         store.inbound_message_history("email-only"))
        self.assertEqual(client.inbound_message_counts("email-only"), (3, 19))
        self.assertEqual(client.inbound_message_ad_ids(), ("email-only",))
        self.assertEqual(client.latest_classification("email-only"),
                         expected_classification)
        self.assertIsNone(client.latest_classification("missing"))
        query_expected, query_rpc = MarkQueryService(store), MarkQueryService(client)
        self.assertEqual(query_rpc.email_reactions(), query_expected.email_reactions())
        self.assertEqual(query_rpc.email_reaction("email-only"),
                         query_expected.email_reaction("email-only"))
        analytics_expected, analytics_rpc = AnalyticsService(store), AnalyticsService(client)
        self.assertEqual(analytics_rpc.rank_ads("email_inbound_message_count"),
                         analytics_expected.rank_ads("email_inbound_message_count"))
        self.assertEqual(analytics_rpc.group_rankings("city", "email_inbound_message_count"),
                         analytics_expected.group_rankings("city", "email_inbound_message_count"))

    def test_email_reaction_uses_one_pinned_history_for_counts(self) -> None:
        from mark_api.domain import InboundMessageEvent
        from mark_api.query import MarkQueryService
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        early = tuple(InboundMessageEvent(
            ad_id="email-only", conversation_id=f"buyer-{index}",
            provider_message_id=f"message-{index}",
            observed_at=NOW + timedelta(minutes=index),
            source="email:snapshot",
        ) for index in range(2))
        self.assertEqual(store.append_inbound_message_events(early), 2)
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        late = InboundMessageEvent(
            ad_id="email-only", conversation_id="buyer-late",
            provider_message_id="message-late",
            observed_at=NOW + timedelta(minutes=10),
            source="email:late",
        )
        original_call = client._call
        fired = False
        def append_after_first_page(op: str, **fields: object) -> dict[str, object]:
            nonlocal fired
            response = original_call(op, **fields)
            if op == "inbound_message_page" and not fired:
                fired = True
                store.append_inbound_message_events((late,))
            return response
        with patch.object(client, "_call", side_effect=append_after_first_page):
            view = MarkQueryService(client).email_reaction("email-only")
        self.assertTrue(fired)
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.conversation_count, 2)
        self.assertEqual(view.inbound_message_count, 2)
        self.assertEqual(view.first_observed_at, early[0].observed_at)
        self.assertEqual(view.last_observed_at, early[-1].observed_at)
        self.assertEqual(store.inbound_message_counts("email-only"), (3, 3))

    def test_inbox_multibyte_full_reply_frames_do_not_truncate_events(self) -> None:
        from mark_api.domain import InboundMessageEvent
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        events = tuple(InboundMessageEvent(
            ad_id="long-valid",
            conversation_id="buyer:" + str(i),
            provider_message_id=f"provider:{i:02}:" + "x" * 1800,
            observed_at=NOW + timedelta(minutes=30 - i),
            source="email:" + "ä" * 800,
        ) for i in range(22))
        self.assertEqual(store.append_inbound_message_events(events), len(events))
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        self.assertEqual(
            client.inbound_message_history("long-valid"),
            store.inbound_message_history("long-valid"),
        )
        self.assertEqual(client.inbound_message_counts("long-valid"), (22, 22))
        self.assertEqual(client.inbound_message_history("missing"), ())

    def test_atomic_inbox_import_preserves_idempotent_duplicate_evidence(self) -> None:
        from mark_api.domain import InboundMessageEvent
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        events = tuple(InboundMessageEvent(
            ad_id="email-only", conversation_id=f"buyer-{i % 4}",
            provider_message_id=f"email-evidence-{i:03}",
            observed_at=NOW + timedelta(minutes=20 - i),
            source="kleinanzeigen-email-import",
        ) for i in range(19))
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        self.assertEqual(client.append_inbound_message_events((*events, events[0])), 19)
        self.assertEqual(client.inbound_message_history("email-only"),
                         store.inbound_message_history("email-only"))
        self.assertEqual(client.inbound_message_counts("email-only"), (4, 19))
        # A separate explicitly authorized repeat is idempotent by provider ID.
        self.assertEqual(client.append_inbound_message_events(events), 0)
        self.assertEqual(len(store.inbound_message_history("email-only")), 19)

    def test_inbox_staged_events_never_persist_before_final_digest(self) -> None:
        from mark_api.domain import InboundMessageEvent
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        event = InboundMessageEvent(
            ad_id="email-only", conversation_id="buyer",
            provider_message_id="evidence-1", observed_at=NOW,
            source="email-synthetic",
        )
        begin = service.dispatch({
            "version": 1, "op": "inbound_begin", "id": "stage-one",
        })
        self.assertEqual(begin["accepted"], 0)
        started = service.dispatch({
            "version": 1, "op": "inbound_stage",
            "id": "stage-one", "offset": 0,
            "events": [owner_module._inbound_as_json(event)],
        })
        self.assertEqual(started["accepted"], 1)
        self.assertEqual(
            service.dispatch({
                "version": 1, "op": "inbound_stage_status", "id": "stage-one",
            }), started,
        )
        self.assertEqual(store.inbound_message_history("email-only"), ())
        for extra in (
            {"sql": "DELETE FROM write_recovery_clearances"},
            {"unexpected_method": "execute_sql"},
        ):
            with self.assertRaises(StorageOwnerError):
                service.dispatch({
                    "version": 1, "op": "inbound_finish",
                    "id": "stage-one", "digest": started["digest"],
                    "expected_count": 1, **extra,
                })
        with self.assertRaises(StorageOwnerError):
            service.dispatch({
                "version": 1, "op": "inbound_finish",
                "id": "stage-one", "digest": "0" * 64,
                "expected_count": 1,
            })
        self.assertEqual(store.inbound_message_history("email-only"), ())
        service.stop()
        self.assertEqual(store.inbound_message_history("email-only"), ())

    def test_inbox_finish_conflict_rolls_back_entire_batch(self) -> None:
        from mark_api.domain import InboundMessageEvent
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        existing = InboundMessageEvent(
            ad_id="email-only", conversation_id="buyer-a",
            provider_message_id="provider-1", observed_at=NOW,
            source="email-original",
        )
        store.append_inbound_message_events((existing,))
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        fresh = InboundMessageEvent(
            ad_id="email-only", conversation_id="buyer-b",
            provider_message_id="provider-2", observed_at=NOW,
            source="email-new",
        )
        collision = InboundMessageEvent(
            ad_id="email-only", conversation_id="wrong",
            provider_message_id="provider-1", observed_at=NOW,
            source="email-forged",
        )
        with self.assertRaises(StorageOwnerError):
            client.append_inbound_message_events((fresh, collision))
        self.assertEqual(store.inbound_message_history("email-only"), (existing,))
        self.assertEqual(store.inbound_message_counts("email-only"), (1, 1))

    def test_inbox_input_generator_has_total_work_bound_before_any_write(self) -> None:
        from mark_api.domain import InboundMessageEvent
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        event = InboundMessageEvent(
            ad_id="email-only", conversation_id="buyer",
            provider_message_id="one-duplicate", observed_at=NOW,
            source="email-synthetic",
        )
        too_many = (
            event for _ in range(owner_module._MAX_INBOX_ITEMS + 1)
        )
        with self.assertRaises(StorageOwnerError):
            client.append_inbound_message_events(too_many)
        self.assertEqual(store.inbound_message_history("email-only"), ())

    def test_inbox_finish_lost_ack_never_blindly_retries(self) -> None:
        from mark_api.domain import InboundMessageEvent
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        event = InboundMessageEvent(
            ad_id="email-only", conversation_id="buyer",
            provider_message_id="ack-lost", observed_at=NOW,
            source="kleinanzeigen-email",
        )
        original = client._call
        finishes = 0
        def lost_finish_ack(op: str, **fields: object) -> dict[str, object]:
            nonlocal finishes
            response = original(op, **fields)
            if op == "inbound_finish":
                finishes += 1
                raise StorageOwnerError("synthetic lost acknowledgement")
            return response
        with patch.object(client, "_call", side_effect=lost_finish_ack):
            with self.assertRaises(StorageOwnerError):
                client.append_inbound_message_events((event,))
        self.assertEqual(finishes, 1)
        self.assertEqual(store.inbound_message_history("email-only"), (event,))
        self.assertEqual(store.inbound_message_counts("email-only"), (1, 1))

    def test_classification_merge_rpc_preserves_prior_dimensions(self) -> None:
        from mark_api.domain import AdSnapshot
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        store.append_ad_snapshot(AdSnapshot(
            ad_id="123", observed_at=NOW, source="management",
        ))
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        first = client.merge_classification(
            ad_id="123", source="manual-cli",
            changes={"city": "Berlin"}, observed_at=NOW + timedelta(minutes=1),
        )
        self.assertEqual(first.city, "Berlin")
        self.assertEqual(first.source, "manual-cli")
        second = client.merge_classification(
            ad_id="123", source="manual-cli",
            changes={"image_type": "product_photo"},
            observed_at=NOW + timedelta(minutes=2),
        )
        self.assertEqual(second.city, "Berlin")
        self.assertEqual(second.image_type, "product_photo")
        self.assertEqual(store.classification_history("123"), (first, second))
        self.assertEqual(client.latest_classification("123"), second)

    def test_classification_merge_rejects_oversized_final_reply_before_commit(self) -> None:
        """Each request fits, but the merged classification must remain readable."""
        from mark_api.domain import AdSnapshot

        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        store.append_ad_snapshot(AdSnapshot(
            ad_id="123", observed_at=NOW, source="management",
        ))
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        label = "\U0001F600" * 1024
        first = client.merge_classification(
            ad_id="123", source="manual-cli",
            changes={"image_type": label},
            observed_at=NOW + timedelta(minutes=1),
        )
        second = client.merge_classification(
            ad_id="123", source="manual-cli",
            changes={"city": label},
            observed_at=NOW + timedelta(minutes=2),
        )
        self.assertEqual(first.image_type, label)
        self.assertEqual(second.city, label)
        self.assertEqual(len(store.classification_history("123")), 2)

        # A known oversized *response* must fail while still uncommitted.
        with self.assertRaises(StorageOwnerError):
            client.merge_classification(
                ad_id="123", source="manual-cli",
                changes={"text_type": label},
                observed_at=NOW + timedelta(minutes=3),
            )
        self.assertEqual(store.classification_history("123"), (first, second))
        self.assertEqual(client.latest_classification("123"), second)

        small = client.merge_classification(
            ad_id="123", source="manual-cli",
            changes={"text_type": "short"},
            observed_at=NOW + timedelta(minutes=4),
        )
        self.assertEqual(small.text_type, "short")
        self.assertEqual(client.latest_classification("123"), small)

    def test_classification_merge_refuses_unknown_id_fields_and_late_observation(self) -> None:
        from mark_api.domain import AdSnapshot
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        store.append_ad_snapshot(AdSnapshot(
            ad_id="123", observed_at=NOW, source="management",
        ))
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        for fields in (
            {"ad_id": "123", "source": "manual", "at": NOW.isoformat(),
             "changes": {"sql": "DELETE FROM write_recovery_clearances"}},
            {"ad_id": "123", "source": "manual", "at": NOW.isoformat(),
             "changes": {"city": True}},
            {"ad_id": "123", "source": "manual", "at": NOW.isoformat(),
             "changes": {"city": " "}},
            {"ad_id": "123", "source": "manual", "at": NOW.isoformat(),
             "changes": {"city": "Berlin"}, "sql": "DELETE FROM ad_snapshots"},
        ):
            with self.subTest(fields=fields), self.assertRaises(StorageOwnerError):
                service.dispatch({"version": 1, "op": "classification_merge", **fields})
        with self.assertRaises(StorageOwnerError):
            client.merge_classification(
                ad_id="absent", source="manual", changes={"city": "Berlin"},
                observed_at=NOW + timedelta(minutes=1),
            )
        accepted = client.merge_classification(
            ad_id="123", source="manual", changes={"city": "Berlin"},
            observed_at=NOW + timedelta(minutes=1),
        )
        with self.assertRaises(StorageOwnerError):
            client.merge_classification(
                ad_id="123", source="manual", changes={"text_type": "long"},
                observed_at=NOW,
            )
        self.assertEqual(store.classification_history("123"), (accepted,))
        self.assertEqual(client.latest_classification("absent"), None)
        self.assertTrue(store.is_ready())

    def test_classification_merge_lost_ack_does_not_retry_persisted_write(self) -> None:
        from mark_api.domain import AdSnapshot
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        store.append_ad_snapshot(AdSnapshot(
            ad_id="123", observed_at=NOW, source="management",
        ))
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        original = client._call
        sent = 0
        def lost_ack(op: str, **fields: object) -> dict[str, object]:
            nonlocal sent
            result = original(op, **fields)
            if op == "classification_merge":
                sent += 1
                raise StorageOwnerError("synthetic acknowledgement lost")
            return result
        with patch.object(client, "_call", side_effect=lost_ack):
            with self.assertRaises(StorageOwnerError):
                client.merge_classification(
                    ad_id="123", source="manual", changes={"city": "Berlin"},
                    observed_at=NOW + timedelta(minutes=1),
                )
        self.assertEqual(sent, 1)
        self.assertEqual(len(store.classification_history("123")), 1)
        self.assertEqual(store.latest_classification("123").city, "Berlin")

    def test_typed_inbox_pages_refuse_sql_and_untrusted_cursors(self) -> None:
        service = self.make_service()
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        for fields in (
            {"ad_id": "email-only", "after_id": True, "watermark": None},
            {"ad_id": "email-only", "after_id": 0, "watermark": None,
             "sql": "DELETE FROM write_api_requests"},
        ):
            with self.subTest(fields=fields), self.assertRaises(StorageOwnerError):
                service.dispatch({"version": 1, "op": "inbound_message_page", **fields})
        with self.assertRaises(StorageOwnerError):
            service.dispatch({
                "version": 1, "op": "inbound_ad_ids_page",
                "after": "", "watermark": None,
                "sql": "SELECT * FROM write_recovery_clearances",
            })
        with patch.object(client, "_call", return_value={
            "watermark": 2, "more": True, "rows": [],
        }), self.assertRaises(StorageOwnerError):
            client.inbound_message_history("email-only")
        with patch.object(client, "_call", return_value={
            "watermark": 2, "more": True, "ids": [],
        }), self.assertRaises(StorageOwnerError):
            client.inbound_message_ad_ids()

    def test_reaction_append_owner_only_retains_distinct_provenance(self) -> None:
        from mark_api.domain import ReactionSnapshot
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        row = ReactionSnapshot(
            ad_id="rx", observed_at=NOW, source="kleinanzeigen:observed",
            conversation_count=4, unique_buyer_count=3,
            inbound_message_count=7,
        )
        self.assertIsNone(client.append_reaction_snapshot(row))
        self.assertEqual(client.reaction_history("rx"), (row,))
        self.assertEqual(store.reaction_history("rx"), (row,))
        self.assertEqual(store.tracked_ad_ids(), ())

    def test_reaction_append_rpc_refuses_sql_extra_fields_and_bad_counters(self) -> None:
        from mark_api.domain import ReactionSnapshot
        service = self.make_service()
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        row = ReactionSnapshot(
            ad_id="rx", observed_at=NOW, source="owner",
            conversation_count=2, unique_buyer_count=2,
            inbound_message_count=5,
        )
        payload = owner_module._reaction_as_json(row)
        invalid = (
            {"reaction": payload, "sql": "DELETE FROM write_recovery_clearances"},
            {"reaction": {**payload, "conversation_count": True}},
            {"reaction": {**payload, "source": ""}},
            {"reaction": {**payload, "extra": "execute_sql"}},
        )
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(StorageOwnerError):
                service.dispatch({"version": 1, "op": "reaction_append", **fields})
        self.assertEqual(client.reaction_history("rx"), ())
        self.assertFalse(client.has_write_recovery_clearance(
            idempotency_key="rx", request_sha256="a" * 64,
        ))

    def test_reaction_append_lost_ack_remains_unknown_without_retry(self) -> None:
        from mark_api.domain import ReactionSnapshot
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        row = ReactionSnapshot(
            ad_id="rx", observed_at=NOW, source="owner",
            conversation_count=0, unique_buyer_count=0,
            inbound_message_count=0,
        )
        original = client._call
        called = 0
        def lost_ack(op: str, **fields: object) -> dict[str, object]:
            nonlocal called
            answer = original(op, **fields)
            if op == "reaction_append":
                called += 1
                raise StorageOwnerError("simulated lost acknowledgement")
            return answer
        with patch.object(client, "_call", side_effect=lost_ack):
            with self.assertRaises(StorageOwnerError):
                client.append_reaction_snapshot(row)
        self.assertEqual(called, 1)
        self.assertEqual(store.reaction_history("rx"), (row,))

    def test_reaction_history_rpc_preserves_original_provenance_and_counters(self) -> None:
        from mark_api.domain import ReactionSnapshot
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        originals = tuple(ReactionSnapshot(
            ad_id="rx", observed_at=NOW + timedelta(minutes=(20 - i)),
            source="kleinanzeigen:" + "ä" * 800,
            conversation_count=i, unique_buyer_count=i + 2,
            inbound_message_count=i + 7,
        ) for i in range(19))
        for row in originals:
            store.append_reaction_snapshot(row)
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        self.assertEqual(client.reaction_history("rx"), originals)
        self.assertEqual(client.reaction_history("no-match"), ())

    def test_reaction_history_rpc_pins_watermark_and_rejects_sql(self) -> None:
        from mark_api.domain import ReactionSnapshot
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        for i in range(19):
            store.append_reaction_snapshot(ReactionSnapshot(
                ad_id="rx", observed_at=NOW, source="first",
                conversation_count=i, unique_buyer_count=i,
                inbound_message_count=i,
            ))
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        late = ReactionSnapshot(
            ad_id="rx", observed_at=NOW, source="late",
            conversation_count=99, unique_buyer_count=99,
            inbound_message_count=99,
        )
        original_call = client._call
        fired = False
        def append_late(op: str, **fields: object) -> dict[str, object]:
            nonlocal fired
            result = original_call(op, **fields)
            if op == "reaction_history_page" and not fired:
                fired = True
                store.append_reaction_snapshot(late)
            return result
        with patch.object(client, "_call", side_effect=append_late):
            previous = client.reaction_history("rx")
        self.assertTrue(fired)
        self.assertEqual(len(previous), 19)
        self.assertNotIn(late, previous)
        self.assertIn(late, client.reaction_history("rx"))
        for fields in (
            {"ad_id": "rx", "after_id": True, "watermark": None},
            {"ad_id": "rx", "after_id": 0, "watermark": None,
             "sql": "DELETE FROM write_recovery_clearances"},
        ):
            with self.subTest(fields=fields), self.assertRaises(StorageOwnerError):
                service.dispatch({
                    "version": 1, "op": "reaction_history_page", **fields,
                })
        with patch.object(client, "_call", return_value={
            "watermark": 1, "more": True, "rows": [],
        }), self.assertRaises(StorageOwnerError):
            client.reaction_history("rx")

    def test_mark_service_refresh_inventory_keeps_raw_observation_provenance(self) -> None:
        from unittest.mock import Mock
        from mark_api.application import MarkService
        from mark_api.domain import AdSnapshot, LifecycleState
        service = self.make_service()
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        original = AdSnapshot(
            ad_id="management-observed", observed_at=NOW,
            source="management+stats", metric_source="management+stats",
            lifecycle_state=LifecycleState.ACTIVE,
            title="Original title", views=42,
        )
        owner_reader = Mock()
        owner_reader.read_ads.return_value = ReadResult.success_nonempty((original,))
        application = MarkService(
            owner_reader=owner_reader, management_reader=Mock(),
            delete_confirmation_reader=Mock(), reaction_reader=Mock(),
            state_writer=Mock(), delete_writer=Mock(), content_writer=Mock(),
            store=client, clock=lambda: NOW,
        )
        self.assertEqual(application.refresh_inventory(),
                         ReadResult.success_nonempty((original,)))
        store = SnapshotStore(self.database, create_if_missing=False)
        self.assertEqual(store.ad_history(original.ad_id), (original,))
        self.assertEqual(client.ad_history(original.ad_id), (original,))
        self.assertEqual(client.sync_status()["state"], "success_nonempty")
        self.assertEqual(
            client.sync_status()["last_successful_attempt"]["source"],
            "mark-service-owner-inventory",
        )

    def test_typed_inventory_journal_preserves_started_recovery_fences(self) -> None:
        from mark_api.domain import AdSnapshot, LifecycleState
        service = self.make_service()
        previous = SnapshotStore(self.database, create_if_missing=False)
        previous.append_ad_snapshot(AdSnapshot(
            ad_id="100", observed_at=NOW, source="synthetic", title="Old",
            lifecycle_state=LifecycleState.ACTIVE,
        ))
        service.runtime_uid = os.geteuid()  # Protocol only; real UID in Docker.
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        client.claim(key="uid:fenced:sync", sha=SHA, owner="runtime:one", at=NOW)
        client.begin(key="uid:fenced:sync", sha=SHA, owner="runtime:one", at=NOW)
        self.assertEqual(client.tracked_ad_ids(), ("100",))
        attempt = client.begin_sync_attempt(source="synthetic", started_at=NOW)
        self.assertEqual(client.sync_status()["state"], "in_progress")
        self.assertEqual(previous.ad_history("100")[-1].lifecycle_state,
                         LifecycleState.ACTIVE)
        snapshot = AdSnapshot(
            ad_id="200", source="synthetic",
            observed_at=NOW + timedelta(minutes=1), title="New",
            lifecycle_state=LifecycleState.ACTIVE, views=4,
        )
        count = client.append_inventory_result(
            ReadResult.success_nonempty((snapshot,)),
            tracked_ad_ids=("100",), source="synthetic", attempt_id=attempt,
            observed_at=NOW + timedelta(minutes=1),
            completed_at=NOW + timedelta(minutes=2),
        )
        self.assertEqual(count, 2)
        self.assertEqual(previous.latest_ad_snapshot("200"), snapshot)
        self.assertEqual(previous.latest_ad_snapshot("100").lifecycle_state,
                         LifecycleState.ABSENT)
        self.assertEqual(client.sync_status()["state"], "success_nonempty")
        self.assertTrue(client.create_pending(
            exclude_key="uid:next:sync", owner="runtime:one"))
        self.assertFalse(client.has_write_recovery_clearance(
            idempotency_key="uid:fenced:sync", request_sha256=SHA))
        # No silent replay of a potentially committed operation.
        with self.assertRaises(StorageOwnerError):
            client.append_inventory_result(
                ReadResult.success_nonempty((snapshot,)),
                tracked_ad_ids=("100",), source="synthetic", attempt_id=attempt,
                observed_at=NOW + timedelta(minutes=1),
                completed_at=NOW + timedelta(minutes=2),
            )
        self.assertEqual(len(previous.ad_history("200")), 1)

    def test_multi_batch_inventory_preserves_multiline_descriptions(self) -> None:
        from mark_api.domain import AdSnapshot, LifecycleState
        service = self.make_service()
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        attempt = client.begin_sync_attempt(source="synthetic", started_at=NOW)
        snapshots = tuple(
            AdSnapshot(ad_id=str(800 + index), observed_at=NOW,
                       source="synthetic", lifecycle_state=LifecycleState.ACTIVE,
                       title="Artikel", description="Zeile eins\\n" + "x" * 7800,
                       views=index)
            for index in range(9)
        )
        self.assertEqual(client.append_inventory_result(
            ReadResult.success_nonempty(snapshots), tracked_ad_ids=(),
            observed_at=NOW, source="synthetic", attempt_id=attempt,
            completed_at=NOW + timedelta(minutes=1),
        ), 9)
        store = SnapshotStore(self.database, create_if_missing=False)
        self.assertEqual(store.latest_ad_snapshot("808"), snapshots[-1])
        self.assertEqual(client.sync_status()["state"], "success_nonempty")

    def test_lost_stage_ack_never_retries_or_commits_a_success(self) -> None:
        from mark_api.domain import AdSnapshot
        service = self.make_service()
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        attempt = client.begin_sync_attempt(source="synthetic", started_at=NOW)
        original = client._call
        calls = []

        def lose_ack(op: str, **fields: object) -> dict[str, object]:
            calls.append(op)
            value = original(op, **fields)
            if op == "sync_stage":
                raise StorageOwnerError("synthetic ACK loss AFTER append")
            return value

        with patch.object(client, "_call", side_effect=lose_ack):
            with self.assertRaisesRegex(StorageOwnerError, "ACK loss"):
                client.append_inventory_result(
                    ReadResult.success_nonempty((owner_module._snapshot_from_json({
                        "ad_id": "700", "observed_at": NOW.isoformat(),
                        "source": "synthetic", "lifecycle_state": "active",
                        "title": "One", "description": None,
                        "views": None, "watch_count": None, "reply_count": None,
                        "metric_source": None,
                    }),)), tracked_ad_ids=(), source="synthetic",
                    attempt_id=attempt, observed_at=NOW,
                    completed_at=NOW + timedelta(minutes=1),
                )
        self.assertEqual(calls.count("sync_stage"), 1)
        self.assertNotIn("sync_finish", calls)
        self.assertEqual(client.sync_stage_status(attempt)["accepted"], 1)
        self.assertEqual(client.sync_status()["state"], "in_progress")
        self.assertEqual(SnapshotStore(self.database, create_if_missing=False)
                         .ad_history("700"), ())

    def test_incomplete_inventory_stage_never_commits_or_clears_fences(self) -> None:
        from mark_api.domain import AdSnapshot, LifecycleState
        service = self.make_service()
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        attempt = client.begin_sync_attempt(source="synthetic", started_at=NOW)
        snapshot = AdSnapshot(
            ad_id="300", source="synthetic", observed_at=NOW,
            lifecycle_state=LifecycleState.ACTIVE,
        )
        owner_module._snapshot_as_json(snapshot)
        reply = service.dispatch({
            "version": 1, "op": "sync_stage", "attempt": attempt, "offset": 0,
            "snapshots": [owner_module._snapshot_as_json(snapshot)],
        })
        self.assertEqual(reply["accepted"], 1)
        self.assertEqual(client.sync_status()["state"], "in_progress")
        self.assertEqual(client.sync_stage_status(attempt)["accepted"], 1)
        self.assertEqual(SnapshotStore(self.database, create_if_missing=False)
                         .ad_history("300"), ())
        # Untrusted executor cannot attach arbitrary SQL/clearance to the
        # operation-bound envelope, or claim success using a wrong digest.
        with self.assertRaises(StorageOwnerError):
            service.dispatch({
                "version": 1, "op": "sync_stage", "attempt": attempt,
                "offset": 1, "snapshots": [], "sql": "DELETE FROM write_api_requests",
            })
        with self.assertRaises(StorageOwnerError):
            service.dispatch({
                "version": 1, "op": "sync_finish", "attempt": attempt,
                "source": "synthetic", "digest": "0" * 64,
                "observed_at": NOW.isoformat(), "completed_at": NOW.isoformat(),
            })
        self.assertEqual(client.sync_status()["state"], "in_progress")
        service.stop()
        self.assertFalse(self.sock.exists())
        restarted = self.make_service()
        restarted.runtime_uid = os.geteuid()
        restarted.start()
        fresh = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        self.assertEqual(fresh.sync_status()["state"], "in_progress")
        self.assertEqual(SnapshotStore(self.database, create_if_missing=False)
                         .ad_history("300"), ())
        with self.assertRaises(StorageOwnerError):
            fresh.sync_stage_status(attempt)

    def test_bounded_inventory_batch_rejects_forged_record_and_unknown_attempt(self) -> None:
        from mark_api.domain import AdSnapshot
        service = self.make_service()
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        attempt = client.begin_sync_attempt(source="synthetic", started_at=NOW)
        record = owner_module._snapshot_as_json(AdSnapshot(
            ad_id="400", source="synthetic", observed_at=NOW,
        ))
        for forged in (
            dict(record, views=True),
            dict(record, metric_source=42),
            dict(record, sql="DROP TABLE write_recovery_clearances"),
            dict(record, source=42),  # a raw origin still must be a bounded string
        ):
            with self.subTest(forged=forged), self.assertRaises(StorageOwnerError):
                service.dispatch({"version": 1, "op": "sync_stage",
                                  "attempt": attempt, "offset": 0,
                                  "snapshots": [forged]})
        with self.assertRaises(StorageOwnerError):
            service.dispatch({"version": 1, "op": "sync_stage",
                              "attempt": attempt + 1, "offset": 0,
                              "snapshots": [record]})
        self.assertEqual(client.sync_stage_status(attempt)["accepted"], 0)
        client.fail_sync_attempt(attempt, source="synthetic",
                                 completed_at=NOW + timedelta(minutes=1),
                                 error_kind="reader_exception")
        self.assertEqual(client.sync_status()["state"], "failed")
        self.assertEqual(SnapshotStore(self.database, create_if_missing=False)
                         .ad_history("400"), ())

    def test_clearance_lookup_is_exact_and_cannot_mint_recovery_authority(self) -> None:
        service = self.make_service()
        claim = {"version": 1, "op": "claim", "key": "ui:clearance:1",
                 "sha": SHA, "owner": "runtime:one", "at": NOW.isoformat()}
        service.dispatch(claim)
        service.dispatch(dict(claim, op="begin"))
        query = {"version": 1, "op": "has_clearance", "key": "ui:clearance:1",
                 "sha": SHA}
        self.assertEqual(service.dispatch(query), {"cleared": False})
        # Synthetic operator-side setup, NOT independent platform evidence.
        trusted = SnapshotStore(self.database, create_if_missing=False)
        trusted.record_write_recovery_clearance(
            idempotency_key="ui:clearance:1", request_sha256=SHA,
            verification_kind="operator_verified_postread",
            evidence_reference="synthetic-test-only", observed_at=NOW,
        )
        service.runtime_uid = os.geteuid()  # Framing-only, no OS UID proof.
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        self.assertTrue(client.has_write_recovery_clearance(
            idempotency_key="ui:clearance:1", request_sha256=SHA,
        ))
        self.assertFalse(client.has_write_recovery_clearance(
            idempotency_key="ui:clearance:1", request_sha256="b" * 64,
        ))
        with self.assertRaises(StorageOwnerError):
            service.dispatch(dict(query, verification_kind="operator_verified_postread"))
        with self.assertRaises(StorageOwnerError):
            service.dispatch(dict(query, op="record_write_recovery_clearance"))

    def test_clearance_lookup_rejects_forged_response_without_retry(self) -> None:
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        for result in ({"cleared": 1}, {"cleared": None},
                       {"cleared": True, "evidence": "forged"}, {}):
            with self.subTest(result=result), patch.object(
                client, "_call", return_value=result,
            ), self.assertRaises(StorageOwnerError):
                client.has_write_recovery_clearance(
                    idempotency_key="ui:clearance:1", request_sha256=SHA,
                )

    def test_recovery_summary_exposes_older_open_owner_attempt(self) -> None:
        service = self.make_service()
        store = SnapshotStore(self.database, create_if_missing=False)
        older = store.begin_sync_attempt(source="previous-owner", started_at=NOW)
        newer = store.begin_sync_attempt(
            source="later-owner", started_at=NOW + timedelta(seconds=1),
        )
        store.append_inventory_result(
            ReadResult.success_empty(()), source="later-owner",
            observed_at=NOW + timedelta(seconds=2),
            attempt_id=newer, completed_at=NOW + timedelta(seconds=2),
        )
        service.runtime_uid = os.geteuid()  # Same-UID framing only.
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        self.assertEqual(client.sync_status()["state"], "success_empty")
        report = client.sync_recovery_status()
        self.assertEqual(report["unresolved_count"], 1)
        self.assertEqual(report["oldest_unresolved_attempt"]["id"], older)
        self.assertEqual(
            report["oldest_unresolved_attempt"]["source"], "previous-owner",
        )
        self.assertEqual(report, store.sync_recovery_status())
        with self.assertRaises(StorageOwnerError):
            service.dispatch({
                "version": 1, "op": "sync_recovery_status",
                "sql": "DELETE FROM sync_attempts",
            })
        store.fail_sync_attempt(
            older, source="previous-owner", error_kind="persistence_error",
            completed_at=NOW + timedelta(seconds=3),
        )
        self.assertEqual(client.sync_recovery_status(), {
            "unresolved_count": 0, "oldest_unresolved_attempt": None,
        })

    def test_recovery_summary_client_rejects_forged_counts_and_oldest(self) -> None:
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        record = {
            "id": 1, "source": "previous-owner", "started_at": NOW.isoformat(),
            "completed_at": None, "outcome": "in_progress",
            "error_kind": None, "snapshot_count": None,
        }
        invalid = (
            {},
            {"unresolved_count": 1, "oldest_unresolved_attempt": None},
            {"unresolved_count": 0, "oldest_unresolved_attempt": record},
            {"unresolved_count": True, "oldest_unresolved_attempt": record},
            {"unresolved_count": 1.0, "oldest_unresolved_attempt": record},
            {"unresolved_count": -1, "oldest_unresolved_attempt": record},
            {"unresolved_count": 2**80, "oldest_unresolved_attempt": record},
            {"unresolved_count": 1, "oldest_unresolved_attempt": {
                **record, "id": True,
            }},
            {"unresolved_count": 1, "oldest_unresolved_attempt": {
                **record, "outcome": "success_empty",
                "completed_at": NOW.isoformat(), "snapshot_count": 0,
            }},
            {"unresolved_count": 1, "oldest_unresolved_attempt": {
                **record, "source": "",
            }},
            {"unresolved_count": 1, "oldest_unresolved_attempt": {
                **record, "sql": "DELETE FROM write_api_requests",
            }},
            {"unresolved_count": 0, "oldest_unresolved_attempt": None,
             "cleared": True},
        )
        for forged in invalid:
            with self.subTest(forged=forged), patch.object(
                client, "_call", return_value={"sync_recovery": forged},
            ) as rpc, self.assertRaises(StorageOwnerError):
                client.sync_recovery_status()
            rpc.assert_called_once_with("sync_recovery_status")

    def test_owner_sync_begin_requires_unskippable_durable_quarantine(self) -> None:
        service = self.make_service()
        service.runtime_uid = os.geteuid()
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        store = SnapshotStore(self.database, create_if_missing=False)
        for disabled in (False, None, 0, 1, "true"):
            with self.subTest(disabled=disabled), self.assertRaises(StorageOwnerError):
                client.begin_sync_attempt(
                    source="owner", started_at=NOW,
                    require_no_unresolved=disabled,
                )
            self.assertEqual(store.sync_status()["state"], "never_attempted")
        first = client.begin_sync_attempt(
            source="owner", started_at=NOW, require_no_unresolved=True,
        )
        self.assertEqual(
            store.sync_recovery_status()["oldest_unresolved_attempt"]["id"],
            first,
        )
        with self.assertRaises(StorageOwnerError):
            client.begin_sync_attempt(source="owner", started_at=NOW)
        self.assertEqual(store.sync_recovery_status()["unresolved_count"], 1)

    def test_sync_status_exact_readback_without_recovery_mutation(self) -> None:
        # The broker must expose the real sync journal, not infer success
        # from last-known ad snapshots or synthesize a successful attempt.
        service = self.make_service()
        empty = service.dispatch({"version": 1, "op": "sync_status"})
        self.assertEqual(empty, {"sync_status": {
            "state": "never_attempted", "latest_attempt": None,
            "last_successful_attempt": None,
        }})
        store = SnapshotStore(self.database, create_if_missing=False)
        attempt = store.begin_sync_attempt(source="private_web_inventory", started_at=NOW)
        failed = service.dispatch({"version": 1, "op": "sync_status"})
        self.assertEqual(failed["sync_status"], store.sync_status())
        self.assertEqual(failed["sync_status"]["state"], "in_progress")
        store.fail_sync_attempt(
            attempt, source="private_web_inventory", completed_at=NOW,
            error_kind="transport_error",
        )
        service.runtime_uid = os.geteuid()  # Same-UID framing only.
        service.start()
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        self.assertEqual(client.sync_status(), store.sync_status())
        self.assertEqual(client.sync_status()["state"], "failed")
        self.assertIsNone(client.sync_status()["last_successful_attempt"])
        success_id = store.begin_sync_attempt(
            source="private_web_inventory", started_at=NOW,
        )
        self.assertEqual(store.append_inventory_result(
            ReadResult.success_empty(()), observed_at=NOW,
            source="private_web_inventory", attempt_id=success_id,
            completed_at=NOW,
        ), 0)
        self.assertEqual(client.sync_status()["state"], "success_empty")
        self.assertEqual(
            client.sync_status()["last_successful_attempt"]["id"], success_id,
        )
        failure_id = store.begin_sync_attempt(
            source="private_web_inventory", started_at=NOW,
        )
        store.fail_sync_attempt(
            failure_id, source="private_web_inventory", completed_at=NOW,
            error_kind="transport_error",
        )
        status = client.sync_status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["last_successful_attempt"]["id"], success_id)
        for fields in ({"sql": "DELETE FROM write_recovery_clearances"},
                       {"filename": str(self.database)}, {"success": True}):
            with self.subTest(fields=fields), self.assertRaises(StorageOwnerError):
                service.dispatch({"version": 1, "op": "sync_status", **fields})

    def test_sync_status_client_rejects_forged_or_inconsistent_status(self) -> None:
        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        malformed = [
            {"state": "success_empty", "latest_attempt": None,
             "last_successful_attempt": None},
            {"state": "never_attempted", "latest_attempt": None,
             "last_successful_attempt": {"id": 1}},
            {"state": "success_empty", "latest_attempt": {
                "id": 1, "source": "private_web_inventory",
                "started_at": NOW.isoformat(), "completed_at": NOW.isoformat(),
                "outcome": "success_empty", "error_kind": None,
                "snapshot_count": 0,
            }, "last_successful_attempt": None},
            {"state": "failed", "latest_attempt": {
                "id": 1, "source": "private_web_inventory",
                "started_at": NOW.isoformat(), "completed_at": NOW.isoformat(),
                "outcome": "success_empty", "error_kind": None,
                "snapshot_count": 0,
            }, "last_successful_attempt": None},
            {"state": "never_attempted", "latest_attempt": None,
             "last_successful_attempt": None, "sql": "SELECT *"},
        ]
        for result in malformed:
            with self.subTest(result=result), patch.object(
                client, "_call", return_value={"sync_status": result},
            ), self.assertRaises(StorageOwnerError):
                client.sync_status()

    def test_inventory_sync_rejects_boolean_and_float_receipt_aliases(self) -> None:
        """JSON true and 1.0 may compare equal to 1, but are not integer ACKs."""
        from mark_api.domain import AdSnapshot

        client = StorageOwnerClient(self.sock, expected_owner_uid=os.geteuid())
        record = AdSnapshot(
            ad_id="ack-1", observed_at=NOW, source="synthetic",
        )
        digest = owner_module._sync_digest(
            owner_module._SYNC_DIGEST_SEED,
            [owner_module._snapshot_as_json(record)],
        )
        for forged in (True, 1.0):
            with self.subTest(stage_accepted=forged), patch.object(
                client, "_call", return_value={"accepted": forged, "digest": digest},
            ) as rpc:
                with self.assertRaisesRegex(
                    StorageOwnerError, "chunk ACK",
                ):
                    client.append_inventory_result(
                        ReadResult.success_nonempty((record,)),
                        source="synthetic", attempt_id=1, observed_at=NOW,
                        completed_at=NOW + timedelta(minutes=1),
                    )
                self.assertEqual(rpc.call_count, 1)
                self.assertEqual(rpc.call_args.args, ("sync_stage",))

            with self.subTest(finish_attempt=forged), patch.object(
                client, "_call", return_value={
                    "persisted": 0, "attempt": forged,
                    "digest": owner_module._SYNC_DIGEST_SEED,
                },
            ) as rpc:
                with self.assertRaisesRegex(StorageOwnerError, "commit receipt"):
                    client.append_inventory_result(
                        ReadResult.success_empty(()), source="synthetic",
                        attempt_id=1, observed_at=NOW,
                        completed_at=NOW + timedelta(minutes=1),
                    )
                self.assertEqual(rpc.call_count, 1)
                self.assertEqual(rpc.call_args.args, ("sync_finish",))

            with self.subTest(failure_attempt=forged), patch.object(
                client, "_call", return_value={
                    "failed": True, "attempt": forged,
                },
            ) as rpc:
                with self.assertRaisesRegex(StorageOwnerError, "failure receipt"):
                    client.fail_sync_attempt(
                        1, source="synthetic", completed_at=NOW,
                        error_kind="reader_exception",
                    )
                self.assertEqual(rpc.call_count, 1)

        for forged_failed in (1, 1.0):
            with self.subTest(failure_flag=forged_failed), patch.object(
                client, "_call", return_value={
                    "failed": forged_failed, "attempt": 1,
                },
            ) as rpc:
                with self.assertRaisesRegex(StorageOwnerError, "failure receipt"):
                    client.fail_sync_attempt(
                        1, source="synthetic", completed_at=NOW,
                        error_kind="reader_exception",
                    )
                self.assertEqual(rpc.call_count, 1)

    def test_container_draft_denies_direct_python_main_before_argparse(self) -> None:
        with patch("mark_api.storage_owner.container_draft_denied", return_value=True):
            self.assertEqual(main(["--db", str(self.database)]), 2)


if __name__ == "__main__":
    unittest.main()