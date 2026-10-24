"""Native SQLite corruption must never silently disappear through owner paging.

These are private, temporary, synthetic SQLite fixtures. They check the
exact owner-facing fixed pagination/last-classification backing methods.
No browser, Docker, untrusted clearance, platform or paid provider.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mark_api.domain import AdClassification, InboundMessageEvent, ReactionSnapshot
from mark_api.storage import SnapshotStore

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)


class OwnerPagedCorruptHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="owner-paged-corruption-")
        self.addCleanup(directory.cleanup)
        self.store = SnapshotStore(Path(directory.name) / "history.sqlite")
        for ad_id in ("101", "202"):
            self.store.append_reaction_snapshot(ReactionSnapshot(
                ad_id=ad_id, observed_at=NOW, source="synthetic",
                conversation_count=1, unique_buyer_count=1,
                inbound_message_count=1,
            ))
            self.store.append_inbound_message_events((InboundMessageEvent(
                ad_id=ad_id, conversation_id="c-" + ad_id,
                provider_message_id="event-" + ad_id, observed_at=NOW,
                source="synthetic",
            ),))
            self.store.append_classification(AdClassification(
                ad_id=ad_id, observed_at=NOW, source="synthetic",
                city="Berlin",
            ))

    def _mutate(self, table, column, ad_id, value):
        # Only fixed literal local test schema identifiers are accepted.
        self.assertIn(table, (
            "reaction_snapshots", "inbound_message_events", "ad_classifications",
        ))
        self.assertIn(column, ("ad_id", "observed_at", "source", "city"))
        with closing(sqlite3.connect(self.store.path)) as con:
            with con:
                old = con.execute(
                    f"SELECT {column} FROM {table} WHERE ad_id=?",
                    (ad_id,),
                ).fetchone()
                self.assertIsNotNone(old)
                updated = con.execute(
                    f"UPDATE {table} SET {column}=? WHERE ad_id=?",
                    (value, ad_id),
                )
                self.assertEqual(updated.rowcount, 1)
        return old[0]

    def _reads(self):
        return (
            ("reaction_snapshots", lambda: self.store.reaction_history_page(
                "101", after_id=0, watermark=None,
            )),
            ("inbound_message_events", lambda: self.store.inbound_message_page(
                "101", after_id=0, watermark=None,
            )),
            ("ad_classifications", lambda: self.store.latest_classification_bounded(
                "101",
            )),
        )

    def test_invalid_observed_at_fails_closed_on_every_owner_read(self):
        for table, read in self._reads():
            with self.subTest(table=table):
                original = self._mutate(table, "observed_at", "101", "not-a-timestamp")
                try:
                    with self.assertRaises(sqlite3.DatabaseError):
                        read()
                finally:
                    self._mutate(table, "observed_at", "101", original)
                self.assertIsNotNone(read())

    def test_corrupt_native_blob_ids_cannot_hide_behind_keyed_page(self):
        for table, read in self._reads():
            with self.subTest(table=table):
                original = self._mutate(
                    table, "ad_id", "202", sqlite3.Binary(b"malformed-native-blob"),
                )
                try:
                    # The valid requested ID 101 exists. Its result cannot
                    # certify the whole generation while BLOB 202 is hidden
                    # from WHERE ad_id='101' by SQLite native-type ordering.
                    with self.assertRaises(sqlite3.DatabaseError):
                        read()
                    if table == "inbound_message_events":
                        with self.assertRaises(sqlite3.DatabaseError):
                            self.store.inbound_message_ad_ids_page(
                                after="", watermark=None,
                            )
                finally:
                    with closing(sqlite3.connect(self.store.path)) as con:
                        with con:
                            con.execute(
                                f"UPDATE {table} SET ad_id=? WHERE "
                                "typeof(ad_id)='blob'",
                                (original,),
                            )
                self.assertIsNotNone(read())

    def test_blob_source_and_optional_classification_are_not_coerced(self):
        for table, read in self._reads():
            with self.subTest(table=table):
                original = self._mutate(
                    table, "source", "101", sqlite3.Binary(b"private-invalid-source"),
                )
                try:
                    with self.assertRaises(sqlite3.DatabaseError):
                        read()
                finally:
                    self._mutate(table, "source", "101", original)
        original = self._mutate(
            "ad_classifications", "city", "101",
            sqlite3.Binary(b"invalid-city"),
        )
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                self.store.latest_classification_bounded("101")
        finally:
            self._mutate("ad_classifications", "city", "101", original)


    def test_socket_rpc_denies_corruption_without_leaking_sqlite_details(self):
        # Same-UID framing only. Real independent hostile UID needs the
        # separately protected host/container test and is NOT certified here.
        import os
        if os.geteuid() == 0:
            self.skipTest(
                "AF_UNIX owner attestation requires a non-root storage UID; "
                "the offline wheel netns maps its runner to UID 0"
            )
        from mark_api.storage_owner import (
            StorageOwnerServer, StorageOwnerClient, StorageOwnerError,
        )

        rpc_dir = self.store.path.parent / "rpc"
        rpc_dir.mkdir(mode=0o700)
        rpc_dir.chmod(0o2710)
        endpoint = rpc_dir / "owner.sock"
        server = StorageOwnerServer(
            database=self.store.path, socket_path=endpoint,
            runtime_uid=os.geteuid() + 10001, runtime_gid=os.getegid(),
        )
        self.addCleanup(server.stop)
        server.runtime_uid = os.geteuid()  # Framing-only test.
        server.start()
        client = StorageOwnerClient(endpoint, expected_owner_uid=os.geteuid())
        routes = (
            ("reaction_snapshots", lambda: client.reaction_history("101")),
            ("inbound_message_events", lambda: client.inbound_message_history("101")),
            ("ad_classifications", lambda: client.latest_classification("101")),
        )
        self.assertTrue(client.ready())
        self.assertEqual(client.dashboard_pending_writes(), ())
        for table, read in routes:
            with self.subTest(table=table):
                self.assertTrue(read())
                original = self._mutate(table, "observed_at", "101", "secret-bad-clock")
                try:
                    with self.assertRaises(StorageOwnerError) as failure:
                        read()
                    self.assertNotIn("secret-bad-clock", str(failure.exception))
                    self.assertNotIn(str(self.store.path), str(failure.exception))
                    self.assertTrue(client.ready())
                    self.assertEqual(client.dashboard_pending_writes(), ())
                finally:
                    self._mutate(table, "observed_at", "101", original)
                self.assertTrue(read())
        self.assertFalse(self.store.has_write_recovery_clearance(
            idempotency_key="synthetic:never-settled",
            request_sha256="a" * 64,
        ))


if __name__ == "__main__":
    unittest.main()