"""Native SQLite types must not erase or conceal dashboard Write recovery fences.

Only synthetic temporary SQLite and local store APIs are exercised. A hostile
pre-cutover database is constructed by manipulating the test connection; this
is not an independent hostile-UID/MAP_SHARED A6 acceptance.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mark_api.storage import SnapshotStore


class DashboardPendingNativeCorruptionTests(unittest.TestCase):
    def setUp(self) -> None:
        root = tempfile.TemporaryDirectory(prefix="dashboard-native-corrupt-")
        self.addCleanup(root.cleanup)
        self.store = SnapshotStore(Path(root.name) / "pending.sqlite")
        self.intent = dict(
            scope="create", resource_key="create", idempotency_key="synthetic:1",
            method="POST", path="/api/write/ads",
            payload_json='{"description":"synthetic","title":"owned"}',
            ad_id=None,
        )
        self.assertTrue(self.store.claim_dashboard_pending_write(**self.intent))
        self.assertEqual(len(self.store.dashboard_pending_writes()), 1)

    def _corrupt(self, column: str, value: object) -> None:
        allowed = {
            "scope", "resource_key", "idempotency_key", "method", "path",
            "payload_json", "ad_id", "acknowledged",
        }
        self.assertIn(column, allowed)
        # Deliberately construct an invalid durable row, not an application
        # mutation. SQLite BLOB has a distinct native storage class even in
        # a TEXT-affinity column; bypass CHECK only for the ACK flag case.
        with closing(sqlite3.connect(self.store.path)) as db:
            with db:
                db.execute("PRAGMA ignore_check_constraints=ON")
                result = db.execute(
                    f"UPDATE dashboard_pending_writes SET {column}=? "
                    "WHERE idempotency_key='synthetic:1'",
                    (value,),
                )
                self.assertEqual(result.rowcount, 1)

    def test_native_blob_in_any_field_does_not_project_as_python_string(self) -> None:
        for field in (
            "scope", "resource_key", "idempotency_key", "method",
            "path", "payload_json", "ad_id",
        ):
            with self.subTest(field=field):
                other = self.store
                self._corrupt(field, sqlite3.Binary(b"hidden-pending-fence"))
                try:
                    with self.assertRaises(sqlite3.DatabaseError):
                        other.dashboard_pending_writes()
                finally:
                    with closing(sqlite3.connect(other.path)) as db:
                        with db:
                            # Restore by physical row rather than an equality
                            # predicate on the deliberately corrupted identity.
                            db.execute(
                                f"UPDATE dashboard_pending_writes SET {field}=?",
                                (self.intent[field],),
                            )
                self.assertEqual(other.dashboard_pending_writes()[0].scope, "create")

    def test_invalid_ack_storage_class_cannot_finalize_a_pending_fence(self) -> None:
        self._corrupt("acknowledged", sqlite3.Binary(b"0"))
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.acknowledge_dashboard_pending_write(
                scope="create", idempotency_key="synthetic:1",
            )
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM dashboard_pending_writes")
                .fetchone()[0], 1,
            )

    def test_page_does_not_hide_native_blob_scope_beyond_limit(self) -> None:
        second = dict(self.intent, scope="update:123", resource_key="update:123",
                      idempotency_key="synthetic:2", method="PATCH",
                      path="/api/write/ads/123",
                      payload_json='{"title":"change"}', ad_id="123")
        self.assertTrue(self.store.claim_dashboard_pending_write(**second))
        self._corrupt("scope", sqlite3.Binary(b"hidden-later-fence"))
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.dashboard_pending_writes_page(after="", limit=1)

    def test_claim_rejects_hidden_native_blob_identity_in_existing_record(self) -> None:
        self._corrupt("resource_key", sqlite3.Binary(b"create"))
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.claim_dashboard_pending_write(
                **dict(self.intent, scope="another", idempotency_key="synthetic:2"),
            )
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM dashboard_pending_writes")
                .fetchone()[0], 1,
            )

    def test_owner_socket_rejects_corruption_without_leaking_database_details(self) -> None:
        # Host non-root framing; not a real hostile-UID boundary test.
        import os
        from mark_api.storage_owner import (
            StorageOwnerClient, StorageOwnerError, StorageOwnerServer,
        )

        if os.geteuid() == 0:
            self.skipTest("protected StorageOwnerServer intentionally rejects root")
        directory = self.store.path.parent / "rpc"
        directory.mkdir(mode=0o700)
        directory.chmod(0o2710)
        server = StorageOwnerServer(
            database=self.store.path, socket_path=directory / "owner.sock",
            runtime_uid=os.geteuid() + 10001, runtime_gid=os.getegid(),
        )
        self.addCleanup(server.stop)
        server.runtime_uid = os.geteuid()  # Transport framing only.
        server.start()
        client = StorageOwnerClient(
            directory / "owner.sock", expected_owner_uid=os.geteuid(),
        )
        self.assertEqual(len(client.dashboard_pending_writes()), 1)
        for field in ("scope", "idempotency_key", "acknowledged"):
            with self.subTest(field=field):
                original = (
                    0 if field == "acknowledged"
                    else self.intent[field]
                )
                self._corrupt(field, sqlite3.Binary(b"secret-native-blob"))
                try:
                    with self.assertRaises(StorageOwnerError) as denied:
                        client.dashboard_pending_writes()
                    self.assertNotIn(str(self.store.path), str(denied.exception))
                    self.assertNotIn("secret-native-blob", str(denied.exception))
                finally:
                    with closing(sqlite3.connect(self.store.path)) as db:
                        with db:
                            db.execute(
                                f"UPDATE dashboard_pending_writes SET {field}=?",
                                (original,),
                            )
                self.assertEqual(len(client.dashboard_pending_writes()), 1)

    def test_corrupt_json_body_never_authorizes_ack_or_projection(self) -> None:
        for body in (
            '{"title":"one","title":"forged"}',
            '{"title":NaN}',
            '{"title":1e999}',
            '{"nested":' + '{"k":' * 70 + '0' + '}' * 70 + '}',
        ):
            with self.subTest(length=len(body)):
                self._corrupt("payload_json", body)
                with self.assertRaises(sqlite3.DatabaseError):
                    self.store.dashboard_pending_writes()
                with self.assertRaises(sqlite3.DatabaseError):
                    self.store.acknowledge_dashboard_pending_write(
                        scope="create", idempotency_key="synthetic:1",
                    )
                with closing(sqlite3.connect(self.store.path)) as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT acknowledged FROM dashboard_pending_writes"
                        ).fetchone()[0], 0,
                    )

    def test_uncorrupted_two_phase_ack_remains_functional(self) -> None:
        self.assertEqual(
            self.store.acknowledge_dashboard_pending_write(
                scope="create", idempotency_key="synthetic:1",
            ), "acknowledged",
        )
        self.assertEqual(
            self.store.acknowledge_dashboard_pending_write(
                scope="create", idempotency_key="synthetic:1",
            ), "finalized",
        )
        self.assertEqual(self.store.dashboard_pending_writes(), ())


if __name__ == "__main__":
    unittest.main()