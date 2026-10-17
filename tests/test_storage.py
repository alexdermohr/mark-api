from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mark_api.domain import (
    AdSnapshot,
    CreateOperationReceipt,
    InboundMessageEvent,
    LifecycleState,
    MediaPostReadStatus,
    OperationOutcome,
    OperationReceipt,
    ReactionSnapshot,
)
from mark_api.results import ReadResult, ReadStatus
from mark_api.storage import SnapshotStore


NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


class SnapshotStoreTests(unittest.TestCase):
    def make_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return SnapshotStore(Path(tmp.name) / "mark.sqlite")

    def test_missing_store_is_rejected_without_creating_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "wrong" / "mark.sqlite"
            with self.assertRaises(sqlite3.OperationalError):
                SnapshotStore(missing, create_if_missing=False)
            self.assertFalse(missing.exists())
            self.assertFalse(missing.parent.exists())

    def test_existing_only_rejects_uninitialized_sqlite_without_migrating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            foreign = Path(tmp) / "foreign.sqlite"
            sqlite3.connect(foreign).close()
            with self.assertRaises(sqlite3.Error):
                SnapshotStore(foreign, create_if_missing=False)
            with sqlite3.connect(foreign) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall(),
                    [],
                )

    def test_lost_corrupt_restored_store_does_not_regenerate_or_lose_data(self) -> None:
        store = self.make_store()
        store.append_ad_snapshot(
            AdSnapshot(
                ad_id="123",
                observed_at=NOW,
                source="audit",
                lifecycle_state=LifecycleState.ACTIVE,
                views=7,
            )
        )
        backup = store.path.with_name("mark-backup.sqlite")
        shutil.copyfile(store.path, backup)
        self.assertTrue(store.is_ready())

        store.path.unlink()
        self.assertFalse(store.is_ready())
        with self.assertRaises(sqlite3.OperationalError):
            store.tracked_ad_ids()
        with self.assertRaises(sqlite3.OperationalError):
            SnapshotStore(store.path, create_if_missing=False)
        self.assertFalse(store.path.exists())

        store.path.write_bytes(b"corrupt sqlite database")
        self.assertFalse(store.is_ready())
        with self.assertRaises(sqlite3.DatabaseError):
            store.tracked_ad_ids()
        with self.assertRaises(sqlite3.DatabaseError):
            SnapshotStore(store.path, create_if_missing=False)

        shutil.copyfile(backup, store.path)
        self.assertTrue(store.is_ready())
        reopened = SnapshotStore(store.path, create_if_missing=False)
        self.assertEqual(reopened.tracked_ad_ids(), ("123",))
        self.assertEqual(reopened.latest_ad_snapshot("123").views, 7)

    def test_preexisting_source_prefix_values_preserve_exact_literal_identity(self) -> None:
        store = self.make_store()
        originals = (
            "mark:source-v1:ordinary-provider",
            'mark:source-v1:{"source":"forged","metric_source":"forged"}',
            "mark:source-v1:{not-json",
        )
        with sqlite3.connect(store.path) as conn:
            for index, source in enumerate(originals):
                conn.execute(
                    "INSERT INTO ad_snapshots "
                    "(ad_id, observed_at, source, lifecycle_state, views) "
                    "VALUES (?, ?, ?, 'active', 0)",
                    (f"legacy-{index}", NOW.isoformat(), source),
                )
        reopened = SnapshotStore(store.path, create_if_missing=False)
        for index, source in enumerate(originals):
            item = reopened.latest_ad_snapshot(f"legacy-{index}")
            self.assertEqual(item.source, source)
            self.assertIsNone(item.metric_source)
            self.assertEqual(item.views, 0)

    def test_metric_source_column_upgrade_requires_prior_recovery_validation(self) -> None:
        store = self.make_store()
        with sqlite3.connect(store.path) as conn:
            fields = {row[1] for row in conn.execute("PRAGMA table_info(ad_snapshots)")}
            if "metric_source" in fields:
                conn.execute("ALTER TABLE ad_snapshots DROP COLUMN metric_source")
        reopened = SnapshotStore(store.path, create_if_missing=False)
        self.assertTrue(reopened.is_ready())
        with sqlite3.connect(store.path) as conn:
            fields = {row[1] for row in conn.execute("PRAGMA table_info(ad_snapshots)")}
            self.assertIn("metric_source", fields)
            conn.execute("ALTER TABLE ad_snapshots DROP COLUMN metric_source")
            conn.execute("DROP TABLE write_api_requests")
        with self.assertRaises(sqlite3.DatabaseError):
            SnapshotStore(store.path, create_if_missing=False)
        with sqlite3.connect(store.path) as conn:
            fields = {row[1] for row in conn.execute("PRAGMA table_info(ad_snapshots)")}
            self.assertNotIn("metric_source", fields)
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='write_api_requests'"
            ).fetchone())

    def test_metric_source_column_preserves_raw_source_and_disallows_blank_origin(self) -> None:
        store = self.make_store()
        value = AdSnapshot(
            ad_id="origin", observed_at=NOW, source="owner+content",
            lifecycle_state=LifecycleState.ACTIVE,
            metric_source="management+stats", views=0, watch_count=2,
        )
        store.append_ad_snapshot(value)
        literal = AdSnapshot(
            ad_id="literal", observed_at=NOW,
            source="mark:source-v1:literal-legacy-provider",
            lifecycle_state=LifecycleState.ACTIVE, views=7,
        )
        store.append_ad_snapshot(literal)
        reopened = SnapshotStore(store.path, create_if_missing=False)
        self.assertEqual(reopened.latest_ad_snapshot("origin"), value)
        self.assertEqual(reopened.latest_ad_snapshot("literal"), literal)
        with sqlite3.connect(store.path) as connection:
            stored = connection.execute(
                "SELECT source, metric_source FROM ad_snapshots WHERE ad_id='origin'"
            ).fetchone()
            self.assertEqual(stored, ("owner+content", "management+stats"))
            literal_source = connection.execute(
                "SELECT source FROM ad_snapshots WHERE ad_id='literal'"
            ).fetchone()[0]
            self.assertEqual(literal_source, literal.source)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE ad_snapshots SET metric_source='' WHERE ad_id='origin'"
                )
        self.assertEqual(reopened.latest_ad_snapshot("origin"), value)

    def test_readiness_requires_write_recovery_tables(self) -> None:
        store = self.make_store()
        self.assertTrue(store.is_ready())
        with sqlite3.connect(store.path) as connection:
            connection.execute("DROP TABLE dashboard_pending_writes")
        self.assertFalse(store.is_ready())
        with self.assertRaises(sqlite3.DatabaseError):
            SnapshotStore(store.path, create_if_missing=False)
        with sqlite3.connect(store.path) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='dashboard_pending_writes'"
                ).fetchone()
            )

    def test_explicit_init_cannot_repair_existing_missing_write_fences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for table in ("write_api_requests", "dashboard_pending_writes"):
                with self.subTest(table=table):
                    path = Path(tmp) / (table + ".sqlite")
                    store = SnapshotStore(path)
                    self.assertTrue(store.is_ready())
                    with sqlite3.connect(path) as connection:
                        connection.execute(f"DROP TABLE {table}")
                    with self.assertRaises(sqlite3.DatabaseError):
                        SnapshotStore(path, create_if_missing=True)
                    with sqlite3.connect(path) as connection:
                        self.assertIsNone(
                            connection.execute(
                                "SELECT name FROM sqlite_master "
                                "WHERE type='table' AND name=?",
                                (table,),
                            ).fetchone()
                        )

    def test_malformed_recovery_columns_fail_before_migrations_and_on_readyz(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for table, columns in (
                ("write_api_requests", "idempotency_key TEXT PRIMARY KEY"),
                (
                    "dashboard_pending_writes",
                    "scope TEXT PRIMARY KEY, resource_key TEXT UNIQUE, "
                    "idempotency_key TEXT UNIQUE",
                ),
            ):
                with self.subTest(table=table):
                    path = Path(tmp) / (table + ".sqlite")
                    store = SnapshotStore(path)
                    self.assertTrue(store.is_ready())
                    with sqlite3.connect(path) as connection:
                        connection.execute(f"DROP TABLE {table}")
                        connection.execute(f"CREATE TABLE {table} ({columns})")
                    self.assertFalse(store.is_ready())
                    for initialize in (False, True):
                        with self.assertRaises(sqlite3.DatabaseError):
                            SnapshotStore(path, create_if_missing=initialize)
                    with sqlite3.connect(path) as connection:
                        self.assertEqual(
                            {row[1] for row in connection.execute(
                                f"PRAGMA table_info({table})"
                            ).fetchall()},
                            {
                                "idempotency_key"
                            } if table == "write_api_requests" else {
                                "scope", "resource_key", "idempotency_key"
                            },
                        )

    def test_partial_unique_write_key_is_not_full_recovery_uniqueness(self) -> None:
        store = self.make_store()
        with sqlite3.connect(store.path) as connection:
            connection.execute("DROP TABLE write_api_requests")
            connection.execute(
                """
                CREATE TABLE write_api_requests (
                    idempotency_key TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    claim_owner TEXT,
                    execution_started_at TEXT,
                    completed_at TEXT,
                    response_status INTEGER,
                    response_json TEXT
                )
                """
            )
            connection.execute(
                "CREATE UNIQUE INDEX partial_write_key "
                "ON write_api_requests(idempotency_key) WHERE state = 'never'"
            )
            for _ in range(2):
                connection.execute(
                    "INSERT INTO write_api_requests "
                    "(idempotency_key, request_sha256, state, requested_at) "
                    "VALUES (?, ?, ?, ?)",
                    ("ui:duplicate", "a" * 64, "in_progress", NOW.isoformat()),
                )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM write_api_requests "
                    "WHERE idempotency_key='ui:duplicate'"
                ).fetchone()[0],
                2,
            )

        self.assertFalse(store.is_ready())
        for initialize in (False, True):
            with self.subTest(initialize=initialize):
                with self.assertRaises(sqlite3.DatabaseError):
                    SnapshotStore(store.path, create_if_missing=initialize)
        with self.assertRaises(sqlite3.DatabaseError):
            store.claim_write_api_request(
                idempotency_key="ui:new-key",
                request_sha256="b" * 64,
                requested_at=NOW,
                claim_owner="runtime-owner",
            )

    def test_partial_unique_pending_resource_cannot_authorize_claim(self) -> None:
        store = self.make_store()
        with sqlite3.connect(store.path) as connection:
            connection.execute("DROP TABLE dashboard_pending_writes")
            connection.execute(
                """
                CREATE TABLE dashboard_pending_writes (
                    scope TEXT PRIMARY KEY,
                    resource_key TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    payload_json TEXT,
                    ad_id TEXT,
                    acknowledged INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                "CREATE UNIQUE INDEX partial_pending_resource "
                "ON dashboard_pending_writes(resource_key) WHERE method = 'never'"
            )
            for scope in ("ad:2:pause", "ad:2:activate"):
                connection.execute(
                    "INSERT INTO dashboard_pending_writes "
                    "(scope, resource_key, idempotency_key, method, path) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        scope, "ad:2", "ui:" + scope, "POST",
                        "/api/write/ads/2/pause",
                    ),
                )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM dashboard_pending_writes "
                    "WHERE resource_key='ad:2'"
                ).fetchone()[0],
                2,
            )

        self.assertFalse(store.is_ready())
        for initialize in (False, True):
            with self.subTest(initialize=initialize):
                with self.assertRaises(sqlite3.DatabaseError):
                    SnapshotStore(store.path, create_if_missing=initialize)
        with self.assertRaises(sqlite3.DatabaseError):
            store.claim_dashboard_pending_write(
                scope="ad:3:pause",
                resource_key="ad:3",
                idempotency_key="ui:new-pending",
                method="POST",
                path="/api/write/ads/3/pause",
                payload_json=None,
                ad_id="3",
            )

    def test_missing_uniqueness_blocks_live_pending_claim(self) -> None:
        store = self.make_store()
        with sqlite3.connect(store.path) as connection:
            connection.execute("DROP TABLE dashboard_pending_writes")
            connection.execute(
                """
                CREATE TABLE dashboard_pending_writes (
                    scope TEXT PRIMARY KEY,
                    resource_key TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    payload_json TEXT,
                    ad_id TEXT,
                    acknowledged INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        self.assertFalse(store.is_ready())
        with self.assertRaises(sqlite3.DatabaseError):
            store.claim_dashboard_pending_write(
                scope="ad:2:pause",
                resource_key="ad:2",
                idempotency_key="ui:unique-required",
                method="POST",
                path="/api/write/ads/2/pause",
                payload_json=None,
                ad_id="2",
            )

    def test_missing_write_request_primary_key_blocks_live_claim(self) -> None:
        store = self.make_store()
        with sqlite3.connect(store.path) as connection:
            connection.execute("DROP TABLE write_api_requests")
            connection.execute(
                """
                CREATE TABLE write_api_requests (
                    idempotency_key TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    claim_owner TEXT,
                    execution_started_at TEXT,
                    completed_at TEXT,
                    response_status INTEGER,
                    response_json TEXT
                )
                """
            )
        self.assertFalse(store.is_ready())
        with self.assertRaises(sqlite3.DatabaseError):
            store.claim_write_api_request(
                idempotency_key="ui:unique-required",
                request_sha256="a" * 64,
                requested_at=NOW,
                claim_owner="runtime-owner",
            )

    def test_readiness_requires_analytics_schema_too(self) -> None:
        store = self.make_store()
        self.assertTrue(store.is_ready())
        with sqlite3.connect(store.path) as connection:
            connection.execute("DROP TABLE reaction_snapshots")
        self.assertFalse(store.is_ready())
        with self.assertRaises(sqlite3.DatabaseError):
            SnapshotStore(store.path, create_if_missing=False)

    def test_sqlite_uri_preserves_encoded_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SQLite #? percent%.sqlite"
            store = SnapshotStore(path)
            self.assertTrue(store.is_ready())
            self.assertEqual(
                SnapshotStore(path, create_if_missing=False).tracked_ad_ids(),
                (),
            )

    def test_failed_inventory_read_does_not_create_absent_snapshot(self) -> None:
        store = self.make_store()
        active = AdSnapshot(
            ad_id="3521676801",
            observed_at=NOW,
            source="management",
            lifecycle_state=LifecycleState.ACTIVE,
            views=15,
            watch_count=0,
            reply_count=1,
        )
        store.append_ad_snapshot(active)

        written = store.append_inventory_result(
            ReadResult.failure(ReadStatus.HTTP_ERROR, http_status=500),
            tracked_ad_ids=["3521676801"],
            observed_at=NOW + timedelta(minutes=1),
            source="management",
        )

        self.assertEqual(written, 0)
        history = store.ad_history("3521676801")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].lifecycle_state, LifecycleState.ACTIVE)

    def test_successful_empty_appends_absent_and_preserves_history(self) -> None:
        store = self.make_store()
        active = AdSnapshot(
            ad_id="3521676801",
            observed_at=NOW,
            source="management",
            lifecycle_state=LifecycleState.ACTIVE,
            views=15,
            watch_count=0,
            reply_count=1,
        )
        store.append_ad_snapshot(active)

        written = store.append_inventory_result(
            ReadResult.success_empty(()),
            tracked_ad_ids=["3521676801"],
            observed_at=NOW + timedelta(minutes=1),
            source="management",
        )

        self.assertEqual(written, 1)
        history = store.ad_history("3521676801")
        self.assertEqual(
            [item.lifecycle_state for item in history],
            [LifecycleState.ACTIVE, LifecycleState.ABSENT],
        )
        self.assertEqual(history[0].views, 15)
        self.assertIsNone(history[1].views)
        self.assertEqual(store.latest_ad_snapshot("3521676801"), history[-1])

    def test_nonempty_inventory_marks_other_tracked_ids_absent(self) -> None:
        store = self.make_store()
        present = AdSnapshot(
            ad_id="2",
            observed_at=NOW,
            source="management",
            lifecycle_state=LifecycleState.ACTIVE,
        )

        written = store.append_inventory_result(
            ReadResult.success_nonempty((present,)),
            tracked_ad_ids=["1", "2"],
            observed_at=NOW,
            source="management",
        )

        self.assertEqual(written, 2)
        self.assertEqual(
            store.latest_ad_snapshot("1").lifecycle_state,
            LifecycleState.ABSENT,
        )
        self.assertEqual(
            store.latest_ad_snapshot("2").lifecycle_state,
            LifecycleState.ACTIVE,
        )

    def test_operation_receipt_persists_authorization_metadata(self) -> None:
        store = self.make_store()
        receipt = OperationReceipt(
            operation="delete",
            ad_id="3521676801",
            started_at=NOW,
            completed_at=NOW,
            outcome=OperationOutcome.CONFIRMED,
            pre_read_status="success_nonempty",
            post_read_status="success_empty",
            writer_invoked=True,
            authorization_by="test-owner",
            authorization_reference="issue-3-predelete",
        )

        store.append_operation_receipt(receipt)

        with sqlite3.connect(store.path) as connection:
            row = connection.execute(
                """
                SELECT authorization_by, authorization_reference, outcome
                FROM operation_receipts
                WHERE ad_id = ?
                """,
                ("3521676801",),
            ).fetchone()

        self.assertEqual(
            row,
            ("test-owner", "issue-3-predelete", "confirmed"),
        )

    def test_create_receipt_persists_confirmed_id_and_confirmation_statuses(self) -> None:
        store = self.make_store()
        candidate = AdSnapshot(
            ad_id="200",
            observed_at=NOW,
            source="management",
            lifecycle_state=LifecycleState.ACTIVE,
            title="Neue Vase",
            description="Beschreibung",
        )
        receipt = CreateOperationReceipt(
            operation="create",
            started_at=NOW,
            completed_at=NOW,
            outcome=OperationOutcome.CONFIRMED,
            pre_read_status="success_empty",
            confirmation_pre_read_status="success_empty",
            post_read_status="success_nonempty",
            confirmation_post_read_status="success_nonempty",
            content_post_read_status="success_nonempty",
            writer_invoked=True,
            created_ad_id="200",
            authorization_by="api-owner",
            authorization_reference="write-api:create-storage",
            post_snapshot=candidate,
            confirmation_post_snapshot=candidate,
            content_post_snapshot=candidate,
            media_post_read_status=MediaPostReadStatus.CONFIRMED,
            media_persistence_confirmed=True,
        )

        store.append_create_operation_receipt(receipt)

        with sqlite3.connect(store.path) as connection:
            row = connection.execute(
                """
                SELECT created_ad_id, outcome, confirmation_pre_read_status,
                       confirmation_post_read_status, content_post_read_status,
                       authorization_by, authorization_reference,
                       media_post_read_status, media_persistence_confirmed
                FROM create_operation_receipts
                """
            ).fetchone()

        self.assertEqual(
            row,
            (
                "200",
                "confirmed",
                "success_empty",
                "success_nonempty",
                "success_nonempty",
                "api-owner",
                "write-api:create-storage",
                "confirmed",
                1,
            ),
        )

    def test_create_receipt_checkpoint_is_append_only_content_evidence(self) -> None:
        store = self.make_store()
        candidate = AdSnapshot(
            ad_id="201",
            observed_at=NOW,
            source="management",
            lifecycle_state=LifecycleState.ACTIVE,
            title="Checkpoint Vase",
            description="Beschreibung",
        )
        receipt = CreateOperationReceipt(
            operation="create",
            started_at=NOW,
            completed_at=NOW,
            outcome=OperationOutcome.CONFIRMED,
            pre_read_status="success_empty",
            confirmation_pre_read_status="success_empty",
            post_read_status="success_nonempty",
            confirmation_post_read_status="success_nonempty",
            content_post_read_status="success_nonempty",
            writer_invoked=True,
            created_ad_id="201",
            authorization_by="api-owner",
            authorization_reference="write-api:create-checkpoint",
            post_snapshot=candidate,
            confirmation_post_snapshot=candidate,
            content_post_snapshot=candidate,
        )

        checkpoint_id = store.append_create_operation_checkpoint(receipt)

        self.assertGreater(checkpoint_id, 0)
        with sqlite3.connect(store.path) as connection:
            row = connection.execute(
                """
                SELECT checkpoint_kind, created_ad_id, outcome,
                       content_post_read_status, writer_invoked
                FROM create_operation_checkpoints
                WHERE id = ?
                """,
                (checkpoint_id,),
            ).fetchone()
        self.assertEqual(
            row,
            (
                "before_media_post_read",
                "201",
                "confirmed",
                "success_nonempty",
                1,
            ),
        )
        with self.assertRaises(ValueError):
            store.append_create_operation_checkpoint(
                replace(
                    receipt,
                    media_post_read_status=MediaPostReadStatus.CONFIRMED,
                    media_persistence_confirmed=True,
                )
            )

    def test_create_receipt_schema_migrates_legacy_authorization_columns(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "mark.sqlite"
        # Keep the complete store; only this table has the legacy column layout.
        # Missing write-recovery tables must never be silently restored.
        SnapshotStore(db_path)
        with sqlite3.connect(db_path) as connection:
            connection.execute("DROP TABLE create_operation_receipts")
            connection.execute(
                """
                CREATE TABLE create_operation_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    created_ad_id TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    pre_read_status TEXT NOT NULL,
                    confirmation_pre_read_status TEXT NOT NULL,
                    post_read_status TEXT,
                    confirmation_post_read_status TEXT,
                    content_post_read_status TEXT,
                    writer_invoked INTEGER NOT NULL,
                    writer_error TEXT,
                    post_snapshot_json TEXT,
                    confirmation_post_snapshot_json TEXT,
                    content_post_snapshot_json TEXT
                )
                """
            )

        store = SnapshotStore(db_path)
        receipt = CreateOperationReceipt(
            operation="create",
            started_at=NOW,
            completed_at=NOW,
            outcome=OperationOutcome.PRECONDITION_FAILED,
            pre_read_status="writes_disabled",
            confirmation_pre_read_status="not_read",
            post_read_status=None,
            confirmation_post_read_status=None,
            content_post_read_status=None,
            writer_invoked=False,
            authorization_by="api-owner",
            authorization_reference="write-api:create-legacy",
            media_post_read_status=MediaPostReadStatus.NOT_READ,
        )
        store.append_create_operation_receipt(receipt)

        with sqlite3.connect(db_path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(create_operation_receipts)"
                ).fetchall()
            }
            row = connection.execute(
                """
                SELECT authorization_by, authorization_reference,
                       media_post_read_status, media_persistence_confirmed
                FROM create_operation_receipts
                """
            ).fetchone()

        self.assertIn("authorization_by", columns)
        self.assertIn("authorization_reference", columns)
        self.assertIn("media_post_read_status", columns)
        self.assertIn("media_persistence_confirmed", columns)
        self.assertEqual(
            row,
            ("api-owner", "write-api:create-legacy", "not_read", 0),
        )

    def test_dashboard_pending_write_claim_uses_coordination_guard(self) -> None:
        store = self.make_store()
        events: list[str] = []

        class Guard:
            def __enter__(self):
                events.append("enter")
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                events.append("exit")

        store._dashboard_pending_write_lock = Guard()
        created = store.claim_dashboard_pending_write(
            scope="create-media",
            resource_key="create",
            idempotency_key="ui:guarded-claim",
            method="POST",
            path="/api/write/media/ads",
            payload_json='{"media_refs":["media_guarded"]}',
            ad_id=None,
        )

        self.assertTrue(created)
        self.assertEqual(events, ["enter", "exit"])

    def test_dashboard_pending_write_acknowledgement_is_two_phase(self) -> None:
        store = self.make_store()
        created = store.claim_dashboard_pending_write(
            scope="create",
            resource_key="create",
            idempotency_key="ui:two-phase",
            method="POST",
            path="/api/write/ads",
            payload_json='{"title":"first"}',
            ad_id=None,
        )
        self.assertTrue(created)
        self.assertFalse(store.dashboard_pending_writes()[0].acknowledged)

        self.assertEqual(
            store.acknowledge_dashboard_pending_write(
                scope="create",
                idempotency_key="ui:two-phase",
            ),
            "acknowledged",
        )
        acknowledged = store.dashboard_pending_writes()
        self.assertEqual(len(acknowledged), 1)
        self.assertTrue(acknowledged[0].acknowledged)

        with self.assertRaisesRegex(ValueError, "conflicts"):
            store.acknowledge_dashboard_pending_write(
                scope="create",
                idempotency_key="ui:wrong-key",
            )

        self.assertEqual(
            store.acknowledge_dashboard_pending_write(
                scope="create",
                idempotency_key="ui:two-phase",
            ),
            "finalized",
        )
        self.assertEqual(store.dashboard_pending_writes(), ())
        self.assertEqual(
            store.acknowledge_dashboard_pending_write(
                scope="create",
                idempotency_key="ui:two-phase",
            ),
            "missing",
        )

    def test_dashboard_pending_write_schema_migrates_acknowledged_column(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "mark.sqlite"
        # Keep the complete store; only this table has the legacy column layout.
        # Missing write-recovery tables must never be silently restored.
        SnapshotStore(db_path)
        with sqlite3.connect(db_path) as connection:
            connection.execute("DROP TABLE dashboard_pending_writes")
            connection.execute(
                """
                CREATE TABLE dashboard_pending_writes (
                    scope TEXT PRIMARY KEY,
                    resource_key TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    payload_json TEXT,
                    ad_id TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO dashboard_pending_writes (
                    scope, resource_key, idempotency_key, method, path,
                    payload_json, ad_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "create",
                    "create",
                    "ui:legacy-pending",
                    "POST",
                    "/api/write/ads",
                    '{"title":"legacy"}',
                    None,
                ),
            )

        store = SnapshotStore(db_path)
        with sqlite3.connect(db_path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(dashboard_pending_writes)"
                ).fetchall()
            }
        self.assertIn("acknowledged", columns)
        records = store.dashboard_pending_writes()
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0].acknowledged)

    def test_write_api_claim_owner_takeover_stops_at_execution_barrier(self) -> None:
        store = self.make_store()
        fingerprint = "a" * 64

        first = store.claim_write_api_request(
            idempotency_key="claim-owner",
            request_sha256=fingerprint,
            requested_at=NOW,
            claim_owner="runtime-owner-a",
        )
        self.assertTrue(first.created)
        self.assertEqual(first.record.claim_owner, "runtime-owner-a")
        self.assertIsNone(first.record.execution_started_at)

        same_owner = store.claim_write_api_request(
            idempotency_key="claim-owner",
            request_sha256=fingerprint,
            requested_at=NOW + timedelta(seconds=1),
            claim_owner="runtime-owner-a",
            allow_abandoned_takeover=True,
        )
        self.assertFalse(same_owner.created)

        takeover = store.claim_write_api_request(
            idempotency_key="claim-owner",
            request_sha256=fingerprint,
            requested_at=NOW + timedelta(seconds=2),
            claim_owner="runtime-owner-b",
            allow_abandoned_takeover=True,
        )
        self.assertTrue(takeover.created)
        self.assertEqual(takeover.record.claim_owner, "runtime-owner-b")
        self.assertEqual(takeover.record.requested_at, NOW)
        self.assertIsNone(takeover.record.execution_started_at)

        with self.assertRaisesRegex(ValueError, "another runtime"):
            store.begin_write_api_request(
                idempotency_key="claim-owner",
                request_sha256=fingerprint,
                claim_owner="runtime-owner-a",
                execution_started_at=NOW + timedelta(seconds=3),
            )

        started = store.begin_write_api_request(
            idempotency_key="claim-owner",
            request_sha256=fingerprint,
            claim_owner="runtime-owner-b",
            execution_started_at=NOW + timedelta(seconds=3),
        )
        self.assertEqual(
            started.execution_started_at,
            NOW + timedelta(seconds=3),
        )

        blocked_takeover = store.claim_write_api_request(
            idempotency_key="claim-owner",
            request_sha256=fingerprint,
            requested_at=NOW + timedelta(seconds=4),
            claim_owner="runtime-owner-c",
            allow_abandoned_takeover=True,
        )
        self.assertFalse(blocked_takeover.created)
        self.assertEqual(blocked_takeover.record.claim_owner, "runtime-owner-b")

        with self.assertRaisesRegex(ValueError, "another runtime"):
            store.complete_write_api_request(
                idempotency_key="claim-owner",
                request_sha256=fingerprint,
                claim_owner="runtime-owner-a",
                response_status=200,
                response_json='{"status":"ok"}',
                completed_at=NOW + timedelta(seconds=5),
            )

        completed = store.complete_write_api_request(
            idempotency_key="claim-owner",
            request_sha256=fingerprint,
            claim_owner="runtime-owner-b",
            response_status=200,
            response_json='{"status":"ok"}',
            completed_at=NOW + timedelta(seconds=5),
        )
        self.assertEqual(completed.state, "completed")
        self.assertEqual(completed.claim_owner, "runtime-owner-b")
        self.assertEqual(
            completed.execution_started_at,
            NOW + timedelta(seconds=3),
        )

    def test_write_api_legacy_in_progress_claim_remains_fail_closed(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "mark.sqlite"
        fingerprint = "b" * 64
        # Keep the complete store; only this table has the legacy column layout.
        # Missing write-recovery tables must never be silently restored.
        SnapshotStore(db_path)
        with sqlite3.connect(db_path) as connection:
            connection.execute("DROP TABLE write_api_requests")
            connection.execute(
                """
                CREATE TABLE write_api_requests (
                    idempotency_key TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL
                        CHECK (state IN ('in_progress', 'completed')),
                    requested_at TEXT NOT NULL,
                    completed_at TEXT,
                    response_status INTEGER,
                    response_json TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO write_api_requests (
                    idempotency_key, request_sha256, state, requested_at
                ) VALUES (?, ?, 'in_progress', ?)
                """,
                ("legacy-stuck", fingerprint, NOW.isoformat()),
            )

        store = SnapshotStore(db_path)
        with sqlite3.connect(db_path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(write_api_requests)"
                ).fetchall()
            }
        self.assertIn("claim_owner", columns)
        self.assertIn("execution_started_at", columns)

        claim = store.claim_write_api_request(
            idempotency_key="legacy-stuck",
            request_sha256=fingerprint,
            requested_at=NOW + timedelta(seconds=1),
            claim_owner="runtime-owner-new",
            allow_abandoned_takeover=True,
        )
        self.assertFalse(claim.created)
        self.assertIsNone(claim.record.claim_owner)
        self.assertIsNone(claim.record.execution_started_at)

        with self.assertRaisesRegex(ValueError, "another runtime"):
            store.begin_write_api_request(
                idempotency_key="legacy-stuck",
                request_sha256=fingerprint,
                claim_owner="runtime-owner-new",
                execution_started_at=NOW + timedelta(seconds=2),
            )

    def test_write_api_completion_requires_execution_start(self) -> None:
        store = self.make_store()
        fingerprint = "c" * 64
        store.claim_write_api_request(
            idempotency_key="not-started",
            request_sha256=fingerprint,
            requested_at=NOW,
            claim_owner="runtime-owner-a",
        )

        with self.assertRaisesRegex(ValueError, "has not started"):
            store.complete_write_api_request(
                idempotency_key="not-started",
                request_sha256=fingerprint,
                claim_owner="runtime-owner-a",
                response_status=500,
                response_json='{"error":"test"}',
                completed_at=NOW + timedelta(seconds=1),
            )

    def test_reaction_snapshots_are_append_only(self) -> None:
        store = self.make_store()
        first = ReactionSnapshot(
            ad_id="3521676801",
            conversation_count=1,
            unique_buyer_count=1,
            inbound_message_count=1,
            observed_at=NOW,
            source="mobile-api",
        )
        second = ReactionSnapshot(
            ad_id="3521676801",
            conversation_count=1,
            unique_buyer_count=1,
            inbound_message_count=2,
            observed_at=NOW + timedelta(minutes=1),
            source="mobile-api",
        )
        store.append_reaction_snapshot(first)
        store.append_reaction_snapshot(second)

        self.assertEqual(store.reaction_history("3521676801"), (first, second))


    def test_merge_classification_accepts_email_evidence_without_owner_snapshot(self) -> None:
        store = self.make_store()
        store.append_inbound_message_events(
            (
                InboundMessageEvent(
                    ad_id="9",
                    conversation_id="email-conversation",
                    provider_message_id="email-message",
                    observed_at=NOW,
                    source="kleinanzeigen-email",
                ),
            )
        )

        item = store.merge_classification(
            ad_id="9",
            source="manual-cli",
            changes={"city": "Dresden"},
            observed_at=NOW + timedelta(minutes=1),
        )

        self.assertEqual(item.city, "Dresden")
        self.assertEqual(store.latest_classification("9"), item)
        self.assertEqual(store.tracked_ad_ids(), ())

    def test_merge_classification_serializes_symlink_aliases(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "mark.sqlite"
        direct_store = SnapshotStore(db_path)
        direct_store.append_ad_snapshot(
            AdSnapshot(
                ad_id="42",
                observed_at=NOW,
                source="management",
                lifecycle_state=LifecycleState.ACTIVE,
            )
        )

        alias_path = Path(tmp.name) / "mark-alias.sqlite"
        alias_path.symlink_to(db_path)
        alias_store = SnapshotStore(alias_path)
        barrier = Barrier(2)

        def merge(
            store: SnapshotStore,
            changes: dict[str, str | None],
        ) -> None:
            barrier.wait(timeout=5)
            store.merge_classification(
                ad_id="42",
                source="test",
                changes=changes,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            city_update = pool.submit(
                merge,
                direct_store,
                {"city": "Dresden"},
            )
            title_update = pool.submit(
                merge,
                alias_store,
                {"title_type": "question"},
            )
            city_update.result(timeout=5)
            title_update.result(timeout=5)

        latest = direct_store.latest_classification("42")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.city, "Dresden")
        self.assertEqual(latest.title_type, "question")
        self.assertEqual(len(direct_store.classification_history("42")), 2)


    def _seed_ambiguous_media_api_request(self, store: SnapshotStore) -> str:
        key = "media-unknown-original"
        store.claim_write_api_request(
            idempotency_key=key, request_sha256="a" * 64,
            requested_at=NOW, claim_owner="original-runtime",
        )
        store.begin_write_api_request(
            idempotency_key=key, request_sha256="a" * 64,
            claim_owner="original-runtime", execution_started_at=NOW,
        )
        store.complete_write_api_request(
            idempotency_key=key, request_sha256="a" * 64,
            claim_owner="original-runtime", response_status=202,
            response_json=json.dumps({
                "media_persistence_confirmed": False,
                "operation_receipt": {
                    "outcome": "ambiguous", "writer_invoked": True,
                },
            }),
            completed_at=NOW,
        )
        return key

    def test_operation_bound_media_recovery_clearance_is_append_only(self) -> None:
        store = self.make_store()
        key = self._seed_ambiguous_media_api_request(store)
        original = store.write_api_request(key)
        self.assertTrue(store.create_recovery_pending(
            exclude_idempotency_key="fresh", claim_owner="second-runtime",
        ))
        store.record_write_recovery_clearance(
            idempotency_key=key,
            request_sha256="a" * 64,
            verification_kind="operator_verified_postread",
            evidence_reference="synthetic-independent-owner-postread:ad-123",
            observed_at=NOW,
        )
        reopened = SnapshotStore(store.path, create_if_missing=False)
        self.assertFalse(reopened.create_recovery_pending(
            exclude_idempotency_key="new-create", claim_owner="third-runtime",
        ))
        self.assertEqual(reopened.write_api_request(key), original)
        with closing(sqlite3.connect(store.path)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM write_recovery_clearances",
            ).fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "already cleared"):
            reopened.record_write_recovery_clearance(
                idempotency_key=key, request_sha256="a" * 64,
                verification_kind="operator_verified_postread",
                evidence_reference="synthetic-independent-owner-postread:ad-123",
                observed_at=NOW,
            )

    def test_media_recovery_clearance_rejects_bad_binding_and_missing_evidence(self) -> None:
        store = self.make_store()
        key = self._seed_ambiguous_media_api_request(store)
        for args in (
            {"idempotency_key": key, "request_sha256": "b" * 64,
             "verification_kind": "operator_verified_postread",
             "evidence_reference": "independent-owner-read", "observed_at": NOW},
            {"idempotency_key": key, "request_sha256": "a" * 64,
             "verification_kind": "operator_verified_postread",
             "evidence_reference": "", "observed_at": NOW},
            {"idempotency_key": "unrelated", "request_sha256": "a" * 64,
             "verification_kind": "operator_verified_postread",
             "evidence_reference": "independent-owner-read", "observed_at": NOW},
            {"idempotency_key": key, "request_sha256": "a" * 64,
             "verification_kind": "self_reported",
             "evidence_reference": "independent-owner-read", "observed_at": NOW},
        ):
            with self.subTest(args=args):
                with self.assertRaises((ValueError, TypeError)):
                    store.record_write_recovery_clearance(**args)
        self.assertTrue(store.create_recovery_pending(
            exclude_idempotency_key="new-create", claim_owner="other-runtime",
        ))

    def test_clear_executed_stale_in_progress_preserves_original_fence(self) -> None:
        store = self.make_store()
        store.claim_write_api_request(
            idempotency_key="stale-executed",
            request_sha256="c" * 64,
            requested_at=NOW, claim_owner="old-runtime",
        )
        store.begin_write_api_request(
            idempotency_key="stale-executed",
            request_sha256="c" * 64,
            claim_owner="old-runtime", execution_started_at=NOW,
        )
        self.assertTrue(store.create_recovery_pending(
            exclude_idempotency_key="new-create", claim_owner="new-runtime",
        ))
        store.record_write_recovery_clearance(
            idempotency_key="stale-executed", request_sha256="c" * 64,
            verification_kind="operator_verified_postread",
            evidence_reference="owner-plus-target-independent-2026-10-10",
            observed_at=NOW,
        )
        self.assertFalse(SnapshotStore(
            store.path, create_if_missing=False,
        ).create_recovery_pending(
            exclude_idempotency_key="new-create", claim_owner="new-runtime",
        ))
        record = store.write_api_request("stale-executed")
        self.assertEqual(record.state, "in_progress")
        self.assertIsNotNone(record.execution_started_at)

    def test_additive_clearance_upgrade_requires_intact_old_recovery_schema(self) -> None:
        store = self.make_store()
        self._seed_ambiguous_media_api_request(store)
        with closing(sqlite3.connect(store.path)) as db:
            db.execute("DROP TABLE write_recovery_clearances")
            db.execute("PRAGMA user_version = 1")
            db.commit()
        upgraded = SnapshotStore(store.path, create_if_missing=False)
        self.assertTrue(upgraded.is_ready())
        self.assertTrue(upgraded.create_recovery_pending(
            exclude_idempotency_key="fresh", claim_owner="new-runtime",
        ))
        with closing(sqlite3.connect(store.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            db.execute("DROP TABLE write_recovery_clearances")
            db.commit()
        self.assertFalse(upgraded.is_ready())
        with self.assertRaises(sqlite3.DatabaseError):
            SnapshotStore(store.path, create_if_missing=False)


if __name__ == "__main__":
    unittest.main()