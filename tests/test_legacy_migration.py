from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import stat
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mark_api.legacy_migration import (
    LegacyMigrationError,
    main,
    migrate_legacy_store,
)
from mark_api.storage import SnapshotStore


NOW = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc).isoformat()
ALL_TABLES = frozenset(
    (
        "ad_snapshots",
        "reaction_snapshots",
        "operation_receipts",
        "ad_classifications",
        "inbound_message_events",
        "create_operation_receipts",
        "write_api_requests",
        "create_operation_checkpoints",
        "dashboard_pending_writes",
    )
)
STAGES = (
    frozenset(("ad_snapshots", "reaction_snapshots", "operation_receipts")),
    frozenset(
        ("ad_snapshots", "reaction_snapshots", "operation_receipts",
         "ad_classifications")
    ),
    frozenset(
        ("ad_snapshots", "reaction_snapshots", "operation_receipts",
         "ad_classifications", "inbound_message_events")
    ),
    frozenset(
        ("ad_snapshots", "reaction_snapshots", "operation_receipts",
         "ad_classifications", "inbound_message_events",
         "create_operation_receipts")
    ),
    frozenset(
        ("ad_snapshots", "reaction_snapshots", "operation_receipts",
         "ad_classifications", "inbound_message_events",
         "create_operation_receipts", "write_api_requests")
    ),
    ALL_TABLES - {"dashboard_pending_writes"},
)


def table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }


class LegacyMigrationTests(unittest.TestCase):
    def build_stage(self, path: Path, stage: frozenset[str]) -> None:
        SnapshotStore(path)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO ad_snapshots "
                "(ad_id,observed_at,source,lifecycle_state,title,views) "
                "VALUES(?,?,?,?,?,?)",
                ("42", NOW, "owner", "active", "Original", 17),
            )
            connection.execute(
                "INSERT INTO reaction_snapshots "
                "(ad_id, observed_at, source, conversation_count, "
                "unique_buyer_count, inbound_message_count) "
                "VALUES (?,?,?,?,?,?)",
                ("42", NOW, "owner", 2, 1, 3),
            )
            connection.execute(
                "INSERT INTO operation_receipts "
                "(operation,ad_id,started_at,completed_at,outcome,"
                "pre_read_status,writer_invoked) VALUES(?,?,?,?,?,?,?)",
                ("pause", "42", NOW, NOW, "precondition_failed",
                 "writes_disabled", 0),
            )
            if "ad_classifications" in stage:
                connection.execute(
                    "INSERT INTO ad_classifications "
                    "(ad_id,observed_at,source,city) VALUES (?,?,?,?)",
                    ("42", NOW, "manual", "Berlin"),
                )
            if "inbound_message_events" in stage:
                connection.execute(
                    "INSERT INTO inbound_message_events "
                    "(provider_message_id,ad_id,conversation_id,"
                    "observed_at,source) VALUES (?,?,?,?,?)",
                    ("email-id-42", "42", "conversation-42", NOW, "email"),
                )
            if "create_operation_receipts" in stage:
                connection.execute(
                    "INSERT INTO create_operation_receipts "
                    "(operation,started_at,completed_at,outcome,"
                    "pre_read_status,confirmation_pre_read_status,"
                    "writer_invoked) VALUES (?,?,?,?,?,?,?)",
                    ("create", NOW, NOW, "precondition_failed",
                     "writes_disabled", "not_read", 0),
                )
            for name in sorted(ALL_TABLES - stage):
                connection.execute(f"DROP TABLE {name}")

    def test_all_six_historical_stages_preserve_data_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for stage in STAGES:
                with self.subTest(stage=len(stage)):
                    source = root / f"old-{len(stage)}.sqlite"
                    backup = root / f"backup-{len(stage)}.sqlite"
                    target = root / f"new-{len(stage)}.sqlite"
                    self.build_stage(source, stage)
                    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()

                    # The ordinary start must never silently create missing
                    # recovery tables from either --init-db state.
                    for allow_init in (False, True):
                        with self.assertRaises(sqlite3.DatabaseError):
                            SnapshotStore(source, create_if_missing=allow_init)

                    receipt = migrate_legacy_store(
                        source,
                        backup_db=backup,
                        output_db=target,
                        confirm_no_unresolved_writes=True,
                    )
                    self.assertEqual(receipt.source_table_count, len(stage))
                    self.assertEqual(
                        receipt.backup_sha256,
                        hashlib.sha256(backup.read_bytes()).hexdigest(),
                    )
                    self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o400)
                    self.assertEqual(
                        hashlib.sha256(source.read_bytes()).hexdigest(),
                        original_hash,
                    )

                    with sqlite3.connect(source) as conn:
                        self.assertEqual(table_names(conn), stage)
                    with sqlite3.connect(backup) as conn:
                        self.assertEqual(table_names(conn), stage)
                    with sqlite3.connect(target) as conn:
                        self.assertTrue(ALL_TABLES.issubset(table_names(conn)))
                        preserved = conn.execute(
                            "SELECT backup_sha256, source_table_count "
                            "FROM mark_legacy_import_receipt WHERE id=1"
                        ).fetchone()
                        self.assertEqual(preserved, (receipt.backup_sha256, len(stage)))
                        for table, count in receipt.copied_rows.items():
                            self.assertEqual(
                                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                                count,
                            )

                    ready = SnapshotStore(target, create_if_missing=False)
                    self.assertTrue(ready.is_ready())
                    ad = ready.latest_ad_snapshot("42")
                    self.assertIsNotNone(ad)
                    assert ad is not None
                    self.assertEqual(ad.views, 17)
                    self.assertEqual(ad.title, "Original")
                    self.assertEqual(
                        len(ready.ad_history("42")), 1,
                    )

    def test_real_legacy_columns_are_migrated_without_losing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, backup, target = (
                root / "legacy.sqlite",
                root / "backup.sqlite",
                root / "modern.sqlite",
            )
            self.build_stage(source, STAGES[4])
            with sqlite3.connect(source) as conn:
                conn.execute("DROP TABLE write_api_requests")
                conn.execute(
                    """
                    CREATE TABLE write_api_requests (
                        idempotency_key TEXT PRIMARY KEY,
                        request_sha256 TEXT NOT NULL,
                        state TEXT NOT NULL,
                        requested_at TEXT NOT NULL,
                        completed_at TEXT,
                        response_status INTEGER,
                        response_json TEXT
                    )
                    """
                )
                conn.execute("DROP TABLE create_operation_receipts")
                conn.execute(
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
                conn.execute(
                    "INSERT INTO create_operation_receipts "
                    "(operation,started_at,completed_at,outcome,pre_read_status,"
                    "confirmation_pre_read_status,writer_invoked) "
                    "VALUES(?,?,?,?,?,?,?)",
                    ("create", NOW, NOW, "precondition_failed",
                     "writes_disabled", "not_read", 0),
                )
                # An already settled legacy API response must keep its key
                # and payload, never be executed as a new platform request.
                conn.execute(
                    "INSERT INTO write_api_requests "
                    "(idempotency_key,request_sha256,state,requested_at,"
                    "completed_at,response_status,response_json) "
                    "VALUES(?,?, 'completed', ?,?,?,?)",
                    (
                        "legacy-finished", "a" * 64, NOW, NOW, 200,
                        '{"already_completed":true}',
                    ),
                )

            migrate_legacy_store(
                source,
                backup_db=backup,
                output_db=target,
                confirm_no_unresolved_writes=True,
            )
            upgraded = SnapshotStore(target, create_if_missing=False)
            self.assertTrue(upgraded.is_ready())
            result = upgraded.claim_write_api_request(
                idempotency_key="legacy-finished",
                request_sha256="a" * 64,
                requested_at=datetime(2026, 10, 8, tzinfo=timezone.utc),
                claim_owner="recovery-test-owner",
            )
            self.assertFalse(result.created)
            self.assertEqual(result.record.state, "completed")
            self.assertEqual(result.record.response_status, 200)
            self.assertEqual(
                result.record.response_json, '{"already_completed":true}'
            )
            with sqlite3.connect(target) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM create_operation_receipts"
                    ).fetchone()[0],
                    1,
                )
                self.assertIsNone(
                    conn.execute(
                        "SELECT authorization_by FROM create_operation_receipts"
                    ).fetchone()[0]
                )

    def test_deleted_high_ids_do_not_reuse_historic_autoincrement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, backup, output = (
                root / "old.sqlite", root / "archived.sqlite", root / "new.sqlite"
            )
            self.build_stage(source, STAGES[0])
            with sqlite3.connect(source) as connection:
                connection.execute(
                    "INSERT INTO ad_snapshots "
                    "(id,ad_id,observed_at,source,lifecycle_state) "
                    "VALUES (100,?,?,?,?)",
                    ("100", NOW, "owner", "active"),
                )
                connection.execute("DELETE FROM ad_snapshots WHERE id=100")
                self.assertEqual(
                    connection.execute(
                        "SELECT seq FROM sqlite_sequence WHERE name='ad_snapshots'"
                    ).fetchone()[0],
                    100,
                )

            migrate_legacy_store(
                source, backup_db=backup, output_db=output,
                confirm_no_unresolved_writes=True,
            )
            with sqlite3.connect(output) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT seq FROM sqlite_sequence WHERE name='ad_snapshots'"
                    ).fetchone()[0],
                    100,
                )
                connection.execute(
                    "INSERT INTO ad_snapshots "
                    "(ad_id,observed_at,source,lifecycle_state) "
                    "VALUES (?,?,?,?)",
                    ("101", NOW, "owner", "active"),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT id FROM ad_snapshots WHERE ad_id='101'"
                    ).fetchone()[0],
                    101,
                )

    def test_confirmation_is_required_and_no_file_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, backup, out = (
                root / "old.sqlite", root / "copy.sqlite", root / "new.sqlite"
            )
            self.build_stage(src, STAGES[0])
            with self.assertRaisesRegex(
                LegacyMigrationError, "confirmation"
            ):
                migrate_legacy_store(src, backup_db=backup, output_db=out)
            self.assertFalse(backup.exists())
            self.assertFalse(out.exists())

    def test_suspicious_write_state_is_never_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for case in ("in_progress", "ambiguous_writer"):
                with self.subTest(case=case):
                    src = root / f"{case}.sqlite"
                    backup = root / f"{case}.backup.sqlite"
                    out = root / f"{case}.new.sqlite"
                    stage = STAGES[4] if case == "in_progress" else STAGES[0]
                    self.build_stage(src, stage)
                    with sqlite3.connect(src) as conn:
                        if case == "in_progress":
                            conn.execute(
                                "INSERT INTO write_api_requests "
                                "(idempotency_key,request_sha256,state,requested_at) "
                                "VALUES (?,?,?,?)",
                                ("unsettled", "a" * 64, "in_progress", NOW),
                            )
                        else:
                            conn.execute(
                                "INSERT INTO operation_receipts "
                                "(operation,ad_id,started_at,completed_at,"
                                "outcome,pre_read_status,writer_invoked) "
                                "VALUES (?,?,?,?,?,?,?)",
                                ("delete", "42", NOW, NOW, "ambiguous", "success", 1),
                            )
                    with self.assertRaisesRegex(
                        LegacyMigrationError, "reconciliation"
                    ):
                        migrate_legacy_store(
                            src,
                            backup_db=backup,
                            output_db=out,
                            confirm_no_unresolved_writes=True,
                        )
                    self.assertFalse(backup.exists())
                    self.assertFalse(out.exists())

    def test_nullable_historical_write_fields_require_reconciliation_before_backup(self) -> None:
        # A matching historical column-name set is not proof of NOT NULL
        # constraints. SQLite's <> predicate ignores NULL in a WHERE clause.
        for table, field, stage in (
            ("operation_receipts", "outcome", STAGES[0]),
            ("create_operation_receipts", "outcome", STAGES[3]),
            ("write_api_requests", "state", STAGES[4]),
        ):
            with self.subTest(table=table), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, backup, output = (
                    root / "legacy.sqlite", root / "backup.sqlite",
                    root / "upgraded.sqlite",
                )
                self.build_stage(source, stage)
                with sqlite3.connect(source) as connection:
                    original_ddl = connection.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type='table' AND name=?", (table,)
                    ).fetchone()[0]
                    declaration = field + " TEXT NOT NULL"
                    self.assertIn(declaration, original_ddl)
                    connection.execute(f"DROP TABLE {table}")
                    connection.execute(
                        original_ddl.replace(declaration, field + " TEXT", 1)
                    )
                    if table == "operation_receipts":
                        connection.execute(
                            "INSERT INTO operation_receipts "
                            "(operation,ad_id,started_at,completed_at,outcome,"
                            "pre_read_status,writer_invoked) VALUES (?,?,?,?,?,?,?)",
                            ("delete", "42", NOW, NOW, None, "success", 1),
                        )
                    elif table == "create_operation_receipts":
                        connection.execute(
                            "INSERT INTO create_operation_receipts "
                            "(operation,started_at,completed_at,outcome,"
                            "pre_read_status,confirmation_pre_read_status,"
                            "writer_invoked) VALUES (?,?,?,?,?,?,?)",
                            ("create", NOW, NOW, None, "success", "success", 1),
                        )
                    else:
                        connection.execute(
                            "INSERT INTO write_api_requests "
                            "(idempotency_key,request_sha256,state,requested_at,"
                            "completed_at,response_status,response_json) "
                            "VALUES (?,?,?,?,?,?,?)",
                            ("uncertain", "a" * 64, None, NOW, NOW, 200, "{}"),
                        )
                with self.assertRaisesRegex(
                    LegacyMigrationError, "reconciliation"
                ):
                    migrate_legacy_store(
                        source, backup_db=backup, output_db=output,
                        confirm_no_unresolved_writes=True,
                    )
                # Never defer an unknown platform-write interpretation until
                # after a new backup or target has already been created.
                self.assertFalse(backup.exists())
                self.assertFalse(output.exists())

    def test_unsettled_media_create_checkpoint_blocks_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, backup, output = (
                root / "old.sqlite", root / "backup.sqlite", root / "modern.sqlite"
            )
            self.build_stage(src, STAGES[5])
            with sqlite3.connect(src) as conn:
                conn.execute(
                    "INSERT INTO create_operation_checkpoints "
                    "(checkpoint_kind,operation,created_ad_id,started_at,"
                    "completed_at,outcome,pre_read_status,"
                    "confirmation_pre_read_status,writer_invoked) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        "before_media_post_read", "create", "42", NOW, NOW,
                        "ambiguous", "success", "success", 1,
                    ),
                )
            with self.assertRaisesRegex(LegacyMigrationError, "reconciliation"):
                migrate_legacy_store(
                    src, backup_db=backup, output_db=output,
                    confirm_no_unresolved_writes=True,
                )
            self.assertFalse(backup.exists())
            self.assertFalse(output.exists())

    def test_unknown_indexes_and_incompatible_columns_never_drop_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, backup, output = (
                root / "old.sqlite", root / "backup.sqlite", root / "modern.sqlite"
            )
            self.build_stage(src, STAGES[0])
            with sqlite3.connect(src) as conn:
                conn.execute(
                    "CREATE INDEX unknown_custom_state ON ad_snapshots(ad_id)"
                )
            with self.assertRaisesRegex(LegacyMigrationError, "unknown indexes"):
                migrate_legacy_store(
                    src, backup_db=backup, output_db=output,
                    confirm_no_unresolved_writes=True,
                )
            self.assertFalse(backup.exists())
            with sqlite3.connect(src) as conn:
                conn.execute("DROP INDEX unknown_custom_state")
                conn.execute("DROP TABLE operation_receipts")
                conn.execute(
                    "CREATE TABLE operation_receipts "
                    "(id INTEGER PRIMARY KEY AUTOINCREMENT)"
                )
            with self.assertRaisesRegex(
                LegacyMigrationError, "incompatible columns"
            ):
                migrate_legacy_store(
                    src, backup_db=backup, output_db=output,
                    confirm_no_unresolved_writes=True,
                )
            self.assertFalse(backup.exists())
            self.assertFalse(output.exists())

    def test_modern_or_unrecognized_schema_is_not_silently_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modern = root / "modern.sqlite"
            SnapshotStore(modern)
            backup, target = root / "backup.sqlite", root / "target.sqlite"
            with self.assertRaisesRegex(LegacyMigrationError, "historical schema"):
                migrate_legacy_store(
                    modern, backup_db=backup, output_db=target,
                    confirm_no_unresolved_writes=True,
                )
            self.assertFalse(backup.exists())
            self.assertFalse(target.exists())

            # A modern DB missing a non-suffix recovery table does not match
            # any historical stage; keep this damaged input blocked.
            with sqlite3.connect(modern) as conn:
                conn.execute("DROP TABLE write_api_requests")
            with self.assertRaisesRegex(LegacyMigrationError, "historical schema"):
                migrate_legacy_store(
                    modern, backup_db=backup, output_db=target,
                    confirm_no_unresolved_writes=True,
                )
            self.assertFalse(backup.exists())
            self.assertFalse(target.exists())

    def test_backup_and_output_are_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "old.sqlite"
            self.build_stage(source, STAGES[0])
            backup, out = root / "backup.sqlite", root / "modern.sqlite"
            backup.write_bytes(b"private-backup")
            with self.assertRaisesRegex(LegacyMigrationError, "already exists"):
                migrate_legacy_store(
                    source, backup_db=backup, output_db=out,
                    confirm_no_unresolved_writes=True,
                )
            self.assertEqual(backup.read_bytes(), b"private-backup")
            self.assertFalse(out.exists())
            backup.unlink()
            out.write_bytes(b"existing-target")
            with self.assertRaisesRegex(LegacyMigrationError, "already exists"):
                migrate_legacy_store(
                    source, backup_db=backup, output_db=out,
                    confirm_no_unresolved_writes=True,
                )
            self.assertEqual(out.read_bytes(), b"existing-target")
            self.assertFalse(backup.exists())

    def test_missing_corrupt_and_symlink_sources_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, backup, out = (
                root / "missing.sqlite", root / "backup.sqlite", root / "new.sqlite"
            )
            with self.assertRaises(FileNotFoundError):
                migrate_legacy_store(
                    source, backup_db=backup, output_db=out,
                    confirm_no_unresolved_writes=True,
                )
            source.write_bytes(b"not a SQLite database")
            with self.assertRaises(sqlite3.DatabaseError):
                migrate_legacy_store(
                    source, backup_db=backup, output_db=out,
                    confirm_no_unresolved_writes=True,
                )
            self.assertFalse(backup.exists())
            self.assertFalse(out.exists())
            source.unlink()
            genuine = root / "genuine.sqlite"
            self.build_stage(genuine, STAGES[0])
            source.symlink_to(genuine)
            with self.assertRaisesRegex(LegacyMigrationError, "regular"):
                migrate_legacy_store(
                    source, backup_db=backup, output_db=out,
                    confirm_no_unresolved_writes=True,
                )

    def test_failed_import_before_copy_completion_preserves_source_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, backup, out = (
                root / "old.sqlite", root / "backup.sqlite", root / "new.sqlite"
            )
            self.build_stage(source, STAGES[0])
            with patch(
                "mark_api.legacy_migration._copy_historical_rows",
                side_effect=LegacyMigrationError("copy failed safely"),
            ):
                with self.assertRaisesRegex(LegacyMigrationError, "copy failed"):
                    migrate_legacy_store(
                        source, backup_db=backup, output_db=out,
                        confirm_no_unresolved_writes=True,
                    )
            self.assertFalse(backup.exists())
            self.assertFalse(out.exists())
            with sqlite3.connect(source) as conn:
                self.assertEqual(table_names(conn), STAGES[0])

    def test_source_write_during_copy_is_blocked_by_sqlite_fence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, backup, out = (
                root / "old.sqlite", root / "backup.sqlite", root / "new.sqlite"
            )
            self.build_stage(source, STAGES[0])
            from mark_api import legacy_migration

            copy_actual = legacy_migration._copy_historical_rows
            attempts: list[str] = []

            def competing_writer(*args, **kwargs):
                copy_actual(*args, **kwargs)
                try:
                    with sqlite3.connect(source, timeout=0.05) as connection:
                        connection.execute(
                            "INSERT INTO ad_snapshots "
                            "(ad_id,observed_at,source,lifecycle_state) "
                            "VALUES (?,?,?,?)",
                            ("43", NOW, "concurrent", "active"),
                        )
                except sqlite3.OperationalError as error:
                    self.assertIn("locked", str(error))
                    attempts.append("blocked")
                else:
                    attempts.append("committed")

            with patch(
                "mark_api.legacy_migration._copy_historical_rows",
                side_effect=competing_writer,
            ):
                migrate_legacy_store(
                    source,
                    backup_db=backup,
                    output_db=out,
                    confirm_no_unresolved_writes=True,
                )
            self.assertEqual(attempts, ["blocked"])
            self.assertTrue(backup.exists())
            self.assertTrue(out.exists())
            for path in (source, backup, out):
                with sqlite3.connect(path) as connection:
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM ad_snapshots").fetchone()[0],
                        1,
                    )

    def test_sqlite_writer_is_fenced_through_publication_in_delete_and_wal(self) -> None:
        from mark_api import legacy_migration

        for journal_mode in ("DELETE", "WAL"):
            with self.subTest(journal_mode=journal_mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, backup, output = (
                    root / "old.sqlite", root / "backup.sqlite", root / "new.sqlite"
                )
                self.build_stage(source, STAGES[0])
                with sqlite3.connect(source) as connection:
                    self.assertEqual(
                        connection.execute(
                            "PRAGMA journal_mode=" + journal_mode
                        ).fetchone()[0].upper(),
                        journal_mode,
                    )
                real_link = os.link
                attempted = []

                def competing_at_link(stage: Path, target: Path) -> None:
                    if target != output:
                        real_link(stage, target)
                        return
                    with sqlite3.connect(source, timeout=0.05) as other:
                        try:
                            other.execute(
                                "INSERT INTO ad_snapshots "
                                "(ad_id,observed_at,source,lifecycle_state) "
                                "VALUES (?,?,?,?)",
                                ("late", NOW, "competing", "active"),
                            )
                            other.commit()
                        except sqlite3.OperationalError as error:
                            self.assertIn("locked", str(error))
                            attempted.append("blocked")
                        else:
                            attempted.append("committed")
                    real_link(stage, target)

                with patch(
                    "mark_api.legacy_migration.os.link",
                    side_effect=competing_at_link,
                ):
                    migrate_legacy_store(
                        source, backup_db=backup, output_db=output,
                        confirm_no_unresolved_writes=True,
                    )
                self.assertEqual(attempted, ["blocked"])
                with sqlite3.connect(output) as connection:
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM ad_snapshots").fetchone()[0],
                        1,
                    )
                with sqlite3.connect(source) as connection:
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM ad_snapshots").fetchone()[0],
                        1,
                    )

    def test_backup_parent_must_be_fsynced_before_output_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_parent, output_parent = root / "backup", root / "output"
            backup_parent.mkdir()
            output_parent.mkdir()
            source, backup, output = (
                root / "old.sqlite", backup_parent / "copy.sqlite",
                output_parent / "new.sqlite"
            )
            self.build_stage(source, STAGES[0])
            backup_id = (backup_parent.stat().st_dev, backup_parent.stat().st_ino)
            original_fsync, original_link = os.fsync, os.link
            synced_backup_parent = []

            def observe_fsync(fd: int) -> None:
                meta = os.fstat(fd)
                if (meta.st_dev, meta.st_ino) == backup_id:
                    synced_backup_parent.append(True)
                original_fsync(fd)

            def guard_publication(stage: Path, target: Path) -> None:
                if target == output:
                    self.assertTrue(
                        synced_backup_parent,
                        "backup directory entry not synced before target publication",
                    )
                original_link(stage, target)

            with (
                patch("mark_api.legacy_migration.os.fsync", side_effect=observe_fsync),
                patch("mark_api.legacy_migration.os.link", side_effect=guard_publication),
            ):
                migrate_legacy_store(
                    source, backup_db=backup, output_db=output,
                    confirm_no_unresolved_writes=True,
                )
            self.assertTrue(backup.is_file())
            self.assertTrue(output.is_file())

    def test_failed_backup_directory_fsync_prevents_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_parent, output_parent = root / "backup", root / "output"
            backup_parent.mkdir()
            output_parent.mkdir()
            source, backup, output = (
                root / "old.sqlite", backup_parent / "copy.sqlite",
                output_parent / "new.sqlite"
            )
            self.build_stage(source, STAGES[0])
            backup_id = (backup_parent.stat().st_dev, backup_parent.stat().st_ino)
            original_fsync = os.fsync

            def fail_backup_directory(fd: int) -> None:
                meta = os.fstat(fd)
                if (meta.st_dev, meta.st_ino) == backup_id:
                    raise OSError("simulated failed backup directory fsync")
                original_fsync(fd)

            with patch(
                "mark_api.legacy_migration.os.fsync", side_effect=fail_backup_directory
            ):
                with self.assertRaisesRegex(OSError, "backup directory fsync"):
                    migrate_legacy_store(
                        source, backup_db=backup, output_db=output,
                        confirm_no_unresolved_writes=True,
                    )
            self.assertFalse(output.exists())
            self.assertTrue(backup.is_file())

    def test_source_path_replacement_before_publication_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, replacement_source, backup, output = (
                root / "old.sqlite", root / "replacement.sqlite",
                root / "backup.sqlite", root / "new.sqlite"
            )
            self.build_stage(source, STAGES[0])
            self.build_stage(replacement_source, STAGES[0])
            from mark_api import legacy_migration

            original_copy = legacy_migration._copy_historical_rows

            def replace_original_path(*args, **kwargs):
                original_copy(*args, **kwargs)
                os.replace(replacement_source, source)

            with patch(
                "mark_api.legacy_migration._copy_historical_rows",
                side_effect=replace_original_path,
            ):
                with self.assertRaisesRegex(LegacyMigrationError, "identity changed"):
                    migrate_legacy_store(
                        source, backup_db=backup, output_db=output,
                        confirm_no_unresolved_writes=True,
                    )
            self.assertTrue(backup.is_file())
            self.assertFalse(output.exists())

    def test_sqlite_writer_remains_fenced_through_output_directory_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, backup_parent, output_parent = (
                root / "old.sqlite", root / "backup", root / "output"
            )
            backup_parent.mkdir()
            output_parent.mkdir()
            backup, output = backup_parent / "copy.sqlite", output_parent / "new.sqlite"
            self.build_stage(source, STAGES[0])
            from mark_api import legacy_migration

            original_sync = legacy_migration._fsync_directory
            attempts = []

            def probe_sync(path: Path) -> None:
                if path == output_parent:
                    with sqlite3.connect(source, timeout=0.05) as other:
                        with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                            other.execute(
                                "INSERT INTO ad_snapshots "
                                "(ad_id,observed_at,source,lifecycle_state) "
                                "VALUES (?,?,?,?)",
                                ("44", NOW, "at-fsync", "active"),
                            )
                    attempts.append("blocked")
                original_sync(path)

            with patch(
                "mark_api.legacy_migration._fsync_directory",
                side_effect=probe_sync,
            ):
                migrate_legacy_store(
                    source, backup_db=backup, output_db=output,
                    confirm_no_unresolved_writes=True,
                )
            self.assertEqual(attempts, ["blocked"])

    def test_failed_output_directory_sync_requires_inspection_not_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_parent, output_parent = root / "backup", root / "output"
            backup_parent.mkdir()
            output_parent.mkdir()
            source, backup, output = (
                root / "old.sqlite", backup_parent / "copy.sqlite",
                output_parent / "new.sqlite"
            )
            self.build_stage(source, STAGES[0])
            from mark_api import legacy_migration

            actual_sync = legacy_migration._fsync_directory

            def fail_after_link(path: Path) -> None:
                if path == output_parent:
                    raise OSError("simulated failed output directory sync")
                actual_sync(path)

            with patch(
                "mark_api.legacy_migration._fsync_directory",
                side_effect=fail_after_link,
            ):
                with self.assertRaisesRegex(OSError, "output directory sync"):
                    migrate_legacy_store(
                        source, backup_db=backup, output_db=output,
                        confirm_no_unresolved_writes=True,
                    )
            self.assertTrue(backup.is_file())
            self.assertTrue(output.is_file())
            with self.assertRaisesRegex(LegacyMigrationError, "already exists"):
                migrate_legacy_store(
                    source, backup_db=backup, output_db=output,
                    confirm_no_unresolved_writes=True,
                )

    def test_source_path_replacement_before_publication_is_refused(self) -> None:
        from mark_api import legacy_migration

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, displaced = root / "original.sqlite", root / "moved.sqlite"
            replacement, backup, output = (
                root / "replacement.sqlite", root / "backup.sqlite",
                root / "published.sqlite",
            )
            self.build_stage(source, STAGES[0])
            self.build_stage(replacement, STAGES[0])
            with sqlite3.connect(replacement) as conn:
                conn.execute(
                    "INSERT INTO ad_snapshots "
                    "(ad_id,observed_at,source,lifecycle_state) "
                    "VALUES (?,?,?,?)",
                    ("replacement", NOW, "owner", "active"),
                )
            actual_copy = legacy_migration._copy_historical_rows

            def swap_path_after_copy(*args, **kwargs):
                actual_copy(*args, **kwargs)
                source.rename(displaced)
                replacement.rename(source)

            with patch(
                "mark_api.legacy_migration._copy_historical_rows",
                side_effect=swap_path_after_copy,
            ):
                with self.assertRaisesRegex(
                    LegacyMigrationError, r"source path .*identity changed"
                ):
                    migrate_legacy_store(
                        source, backup_db=backup, output_db=output,
                        confirm_no_unresolved_writes=True,
                    )
            self.assertTrue(backup.exists())
            self.assertFalse(output.exists())
            with sqlite3.connect(source) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM ad_snapshots").fetchone()[0], 2
                )

    def test_published_output_remains_private_under_typical_umask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, backup, output = (
                root / "original.sqlite", root / "backup.sqlite",
                root / "published.sqlite",
            )
            self.build_stage(source, STAGES[0])
            previous_umask = os.umask(0o022)
            try:
                migrate_legacy_store(
                    source, backup_db=backup, output_db=output,
                    confirm_no_unresolved_writes=True,
                )
            finally:
                os.umask(previous_umask)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o400)

    def test_backup_is_not_public_until_staged_target_copy_is_complete(self) -> None:
        from mark_api import legacy_migration

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, backup, output = (
                root / "old.sqlite", root / "public-backup.sqlite",
                root / "new.sqlite",
            )
            self.build_stage(source, STAGES[0])
            actual_copy = legacy_migration._copy_historical_rows
            during_copy = []

            def copy_before_publication(*args, **kwargs):
                during_copy.append(backup.exists())
                self.assertFalse(
                    backup.exists(),
                    "public backup inode exposed while it can still influence target rows",
                )
                return actual_copy(*args, **kwargs)

            with patch(
                "mark_api.legacy_migration._copy_historical_rows",
                side_effect=copy_before_publication,
            ):
                migrate_legacy_store(
                    source, backup_db=backup, output_db=output,
                    confirm_no_unresolved_writes=True,
                )
            self.assertEqual(during_copy, [False])
            self.assertTrue(backup.is_file())
            self.assertTrue(output.is_file())
            with sqlite3.connect(output) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT views FROM ad_snapshots WHERE ad_id='42'"
                    ).fetchone()[0],
                    17,
                )

    def test_backup_never_reopens_public_path_as_sqlite(self) -> None:
        # A public backup pathname can be renamed after O_EXCL creation.
        # The SQLite backup/read connections must use a private pinned stage.
        from mark_api import legacy_migration

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, backup, output = (
                root / "old.sqlite", root / "published-backup.sqlite",
                root / "new.sqlite",
            )
            self.build_stage(source, STAGES[0])
            original_connect = sqlite3.connect
            public_uri = legacy_migration._uri(backup, "rw").split("?")[0]
            public_opens = []

            def connect_only_private(db, *args, **kwargs):
                if isinstance(db, str) and db.startswith(public_uri + "?"):
                    public_opens.append(db)
                    raise AssertionError("public backup path reopened for SQLite")
                return original_connect(db, *args, **kwargs)

            with patch(
                "mark_api.legacy_migration.sqlite3.connect",
                side_effect=connect_only_private,
            ):
                migrate_legacy_store(
                    source, backup_db=backup, output_db=output,
                    confirm_no_unresolved_writes=True,
                )
            self.assertEqual(public_opens, [])
            self.assertTrue(backup.is_file())
            self.assertTrue(SnapshotStore(output, create_if_missing=False).is_ready())

    def test_backup_publication_refuses_racing_symlink_and_hardlink(self) -> None:
        for link_kind in ("symlink", "hardlink"):
            with self.subTest(kind=link_kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, backup, output, victim = (
                    root / "old.sqlite", root / "backup.sqlite",
                    root / "new.sqlite", root / "unrelated.txt",
                )
                self.build_stage(source, STAGES[0])
                victim.write_bytes(b"unrelated original content")
                original_link = os.link
                attempted = []

                def competing_name_before_backup_link(src: Path, dst: Path) -> None:
                    if dst == backup:
                        attempted.append(True)
                        if link_kind == "symlink":
                            os.symlink(victim, backup)
                        else:
                            original_link(victim, backup)
                    original_link(src, dst)

                with patch(
                    "mark_api.legacy_migration.os.link",
                    side_effect=competing_name_before_backup_link,
                ):
                    with self.assertRaises(FileExistsError):
                        migrate_legacy_store(
                            source, backup_db=backup, output_db=output,
                            confirm_no_unresolved_writes=True,
                        )
                self.assertEqual(attempted, [True])
                self.assertEqual(victim.read_bytes(), b"unrelated original content")
                self.assertFalse(output.exists())

    def test_replaced_backup_name_before_output_publication_is_rejected(self) -> None:
        from mark_api import legacy_migration

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, backup, output, victim = (
                root / "old.sqlite", root / "backup.sqlite",
                root / "new.sqlite", root / "unrelated.sqlite",
            )
            self.build_stage(source, STAGES[0])
            self.build_stage(victim, STAGES[0])
            victim_bytes = victim.read_bytes()
            original_link = os.link

            def replace_published_backup(stage: Path, target: Path) -> None:
                original_link(stage, target)
                if target == backup:
                    backup.unlink()
                    backup.symlink_to(victim)

            with patch(
                "mark_api.legacy_migration.os.link",
                side_effect=replace_published_backup,
            ):
                with self.assertRaisesRegex(LegacyMigrationError, "backup path"):
                    migrate_legacy_store(
                        source, backup_db=backup, output_db=output,
                        confirm_no_unresolved_writes=True,
                    )
            self.assertFalse(output.exists())
            self.assertEqual(victim.read_bytes(), victim_bytes)

    def test_installed_product_exposes_offline_migration_cli(self) -> None:
        with (Path.cwd() / "pyproject.toml").open("rb") as stream:
            metadata = tomllib.load(stream)
        self.assertEqual(
            metadata["project"]["scripts"]["mark-api-migrate-legacy"],
            "mark_api.legacy_migration:main",
        )

    def test_cli_returns_sanitized_summary_and_never_replays_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, backup, out = (
                root / "old.sqlite", root / "backup.sqlite", root / "new.sqlite"
            )
            self.build_stage(source, STAGES[0])
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "--db", str(source),
                        "--backup", str(backup),
                        "--output-db", str(out),
                        "--confirm-no-unresolved-writes",
                    ]
                )
            self.assertEqual(result, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "migrated")
            self.assertEqual(payload["historical_table_count"], 3)
            self.assertFalse(payload["platform_writes_performed"])
            self.assertNotIn("Original", output.getvalue())
            self.assertTrue(SnapshotStore(out, create_if_missing=False).is_ready())

    def test_cli_missing_confirmation_is_explicit_without_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, backup, out = (
                root / "old.sqlite", root / "backup.sqlite", root / "new.sqlite"
            )
            self.build_stage(source, STAGES[0])
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
                main([
                    "--db", str(source),
                    "--backup", str(backup),
                    "--output-db", str(out),
                ])
            self.assertEqual(error.exception.code, 2)
            self.assertIn("confirmation", stderr.getvalue())
            self.assertNotIn(str(source), stderr.getvalue())
            self.assertFalse(backup.exists())
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()