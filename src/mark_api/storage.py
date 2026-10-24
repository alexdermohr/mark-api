from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from contextlib import AbstractContextManager, closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Callable, Iterable, Iterator, Mapping
from urllib.parse import quote

from .domain import (
    AdClassification,
    AdSnapshot,
    CreateOperationReceipt,
    InboundMessageEvent,
    LifecycleState,
    OperationOutcome,
    OperationReceipt,
    ReactionSnapshot,
)
from .results import ReadResult, ReadStatus
from .strict_json import strict_json_object

_SYNC_SCHEMA_VERSION = 1
_STORE_SCHEMA_VERSION = 2
# Unverified observations never establish independently confirmed account ownership.
LOCAL_CATALOG_SOURCE = "user-supplied-local-catalog"
_AD_HISTORY_PAGE_ROWS = 256  # Close read connection before projection.

# Historical ad IDs must have SQLite BINARY identity in every keyed history
# table: SQL WHERE identity and Python projection grouping must not disagree.
# A deliberately conservative DDL prefix rejects unrecognized manually
# altered/quoted/commented schemas rather than guessing at SQL collation syntax.
_HISTORY_BINARY_KEY_DDL = {
    table: re.compile(
        r"\ACREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + table
        + r"\s*\(\s*id\s+INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\s*,"
        + (
            r"\s*provider_message_id\s+TEXT\s+NOT\s+NULL\s+UNIQUE\s*,"
            if table == "inbound_message_events" else ""
        )
        + r"\s*ad_id\s+TEXT\s+NOT\s+NULL\s*,",
        re.IGNORECASE,
    )
    for table in (
        "ad_snapshots", "reaction_snapshots", "inbound_message_events",
        "ad_classifications",
    )
}

# Partial BINARY indexes contain ONLY whitespace-only IDs. SQLite's own
# trim-with-character-set matches Python str.strip() for these Unicode code
# points, including all control whitespace and U+3000. No full-table scan
# is permitted on history read.
_HISTORY_AD_ID_WHITESPACE_HEX = "".join(chr(codepoint) for codepoint in (
    9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760,
    *range(8192, 8203), 8232, 8233, 8239, 8287, 12288,
)).encode("utf-8").hex().upper()
_HISTORY_BLANK_AD_ID_PREDICATE = (
    f"trim(ad_id, CAST(X'{_HISTORY_AD_ID_WHITESPACE_HEX}' AS TEXT)) COLLATE BINARY = ''"
)

_SYNC_ERROR_KINDS = frozenset({
    "unauthenticated", "http_error", "transport_error", "parse_error",
    "reader_exception", "runtime_unavailable", "invalid_inventory",
    "persistence_error", "stopped_before_runtime", "stopped_before_read",
})


def _sync_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("sync timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()



_CLASSIFICATION_FIELDS = (
    "image_type",
    "city",
    "text_type",
    "title_type",
)

# Used for startup validation and read-only readiness checks.
_REQUIRED_STORE_COLUMNS: dict[str, tuple[str, ...]] = {
    "ad_snapshots": (
        "id",
        "ad_id",
        "observed_at",
        "source",
        "lifecycle_state",
        "title",
        "description",
        "views",
        "watch_count",
        "reply_count",
        "metric_source",
    ),
    "reaction_snapshots": (
        "id",
        "ad_id",
        "observed_at",
        "source",
        "conversation_count",
        "unique_buyer_count",
        "inbound_message_count",
    ),
    "inbound_message_events": (
        "id",
        "provider_message_id",
        "ad_id",
        "conversation_id",
        "observed_at",
        "source",
    ),
    "ad_classifications": (
        "id",
        "ad_id",
        "observed_at",
        "source",
        "image_type",
        "city",
        "text_type",
        "title_type",
    ),
    "operation_receipts": (
        "id",
        "operation",
        "ad_id",
        "started_at",
        "completed_at",
        "outcome",
        "pre_read_status",
        "post_read_status",
        "writer_invoked",
        "authorization_by",
        "authorization_reference",
        "writer_error",
        "pre_snapshot_json",
        "post_snapshot_json",
    ),
    "create_operation_receipts": (
        "id",
        "operation",
        "created_ad_id",
        "started_at",
        "completed_at",
        "outcome",
        "pre_read_status",
        "confirmation_pre_read_status",
        "post_read_status",
        "confirmation_post_read_status",
        "content_post_read_status",
        "writer_invoked",
        "authorization_by",
        "authorization_reference",
        "media_post_read_status",
        "media_persistence_confirmed",
        "writer_error",
        "post_snapshot_json",
        "confirmation_post_snapshot_json",
        "content_post_snapshot_json",
    ),
    "create_operation_checkpoints": (
        "id",
        "checkpoint_kind",
        "operation",
        "created_ad_id",
        "started_at",
        "completed_at",
        "outcome",
        "pre_read_status",
        "confirmation_pre_read_status",
        "post_read_status",
        "confirmation_post_read_status",
        "content_post_read_status",
        "writer_invoked",
        "authorization_by",
        "authorization_reference",
        "writer_error",
        "post_snapshot_json",
        "confirmation_post_snapshot_json",
        "content_post_snapshot_json",
    ),
    "write_api_requests": (
        "idempotency_key",
        "request_sha256",
        "state",
        "requested_at",
        "claim_owner",
        "execution_started_at",
        "completed_at",
        "response_status",
        "response_json",
    ),
    "write_recovery_clearances": (
        "idempotency_key",
        "request_sha256",
        "observed_at",
        "verification_kind",
        "evidence_reference",
    ),
    "dashboard_pending_writes": (
        "scope",
        "resource_key",
        "idempotency_key",
        "method",
        "path",
        "payload_json",
        "ad_id",
        "acknowledged",
    ),
}

# Only these missing historical columns may be added to an otherwise complete store.
_LEGACY_ADDABLE_COLUMNS: dict[str, frozenset[str]] = {
    "ad_snapshots": frozenset(("metric_source",)),
    "create_operation_receipts": frozenset((
        "authorization_by", "authorization_reference",
        "media_post_read_status", "media_persistence_confirmed",
    )),
    "write_api_requests": frozenset(("claim_owner", "execution_started_at")),
    "dashboard_pending_writes": frozenset(("acknowledged",)),
}

# Single-column uniqueness prevents duplicate pending keys or buyer event IDs.
_REQUIRED_SINGLE_COLUMN_UNIQUES: dict[str, tuple[str, ...]] = {
    "inbound_message_events": ("provider_message_id",),
    "write_api_requests": ("idempotency_key",),
    "write_recovery_clearances": ("idempotency_key",),
    "dashboard_pending_writes": ("scope", "resource_key", "idempotency_key"),
}

@dataclass(frozen=True, slots=True)
class SyncAttemptRecord:
    id: int
    source: str
    started_at: datetime
    completed_at: datetime | None
    outcome: str
    error_kind: str | None
    snapshot_count: int | None


def _sync_attempt_from_row(row: sqlite3.Row) -> SyncAttemptRecord:
    """Treat corrupt journal data as a database error, never a claimed sync."""
    try:
        attempt_id = row["id"]
        source = row["source"]
        raw_started = row["started_at"]
        raw_completed = row["completed_at"]
        outcome = row["outcome"]
        error_kind = row["error_kind"]
        count = row["snapshot_count"]
        if (
            type(attempt_id) is not int or attempt_id < 1
            or not isinstance(source, str) or not source.strip()
            or not isinstance(raw_started, str)
            or (raw_completed is not None and not isinstance(raw_completed, str))
        ):
            raise ValueError("invalid sync journal fields")
        started = datetime.fromisoformat(raw_started)
        completed = (
            datetime.fromisoformat(raw_completed)
            if raw_completed is not None else None
        )
        if started.utcoffset() is None or (
            completed is not None and completed.utcoffset() is None
        ):
            raise ValueError("sync journal datetime has no timezone")
        if completed is not None and completed < started:
            raise ValueError("sync completion before start")
        if outcome == "in_progress":
            valid = completed is None and error_kind is None and count is None
        elif outcome == "failed":
            valid = completed is not None and error_kind in _SYNC_ERROR_KINDS and count is None
        elif outcome == "success_empty":
            valid = completed is not None and error_kind is None and count == 0
        elif outcome == "success_nonempty":
            valid = completed is not None and error_kind is None and type(count) is int and count > 0
        else:
            valid = False
        if not valid:
            raise ValueError("inconsistent sync journal outcome")
    except (TypeError, ValueError, IndexError, KeyError) as exc:
        raise sqlite3.DatabaseError("invalid sync attempt journal record") from exc
    return SyncAttemptRecord(
        id=attempt_id, source=source,
        started_at=started, completed_at=completed,
        outcome=outcome, error_kind=error_kind, snapshot_count=count,
    )


def _unresolved_sync_attempt_summary(
    connection: sqlite3.Connection,
) -> tuple[int, SyncAttemptRecord | None]:
    """Inspect all open generations in one bounded-memory SQL traversal.

    Checking only the oldest outstanding attempt can conceal a malformed
    middle generation when a later completed attempt is otherwise healthy.
    SQL cursors stream rows without materializing private journal history.
    This read-only check neither settles nor grants retry authorization.
    """
    count = 0
    oldest: SyncAttemptRecord | None = None
    for row in connection.execute(
        "SELECT * FROM sync_attempts WHERE outcome='in_progress' ORDER BY id ASC"
    ):
        record = _sync_attempt_from_row(row)
        if oldest is None:
            oldest = record
        count += 1
    return count, oldest


def _sync_attempt_to_dict(item: SyncAttemptRecord | None) -> dict[str, object] | None:
    if item is None:
        return None
    return {
        "id": item.id, "source": item.source,
        "started_at": item.started_at.isoformat(),
        "completed_at": (item.completed_at.isoformat()
                         if item.completed_at is not None else None),
        "outcome": item.outcome, "error_kind": item.error_kind,
        "snapshot_count": item.snapshot_count,
    }


@dataclass(frozen=True, slots=True)
class WriteApiRequestRecord:
    idempotency_key: str
    request_sha256: str
    state: str
    requested_at: datetime
    claim_owner: str | None
    execution_started_at: datetime | None
    completed_at: datetime | None
    response_status: int | None
    response_json: str | None


@dataclass(frozen=True, slots=True)
class WriteApiRequestClaim:
    created: bool
    record: WriteApiRequestRecord


@dataclass(frozen=True, slots=True)
class DashboardPendingWrite:
    scope: str
    resource_key: str
    idempotency_key: str
    method: str
    path: str
    payload_json: str | None
    ad_id: str | None
    acknowledged: bool


def _validated_write_api_claim_owner(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(character.isspace() for character in value)
    ):
        raise ValueError("claim_owner must contain 1..128 safe characters")
    return value


class _ClosingSQLiteConnection(sqlite3.Connection):
    """End the transaction and close the handle when a with-block exits.

    sqlite3.Connection.__exit__ only commits or rolls back: it does not close
    the descriptor. SnapshotStore uses transaction scopes throughout, so the
    exit must keep the original commit/rollback semantics *then* close, even
    on malformed data, cancellation, or a failing commit.
    """

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class SnapshotStore:
    """Append-only SQLite storage for normalized observations and write receipts."""

    def __init__(
        self, path: str | Path, *, create_if_missing: bool = True,
        require_existing_schema: bool = False,
    ) -> None:
        if not isinstance(create_if_missing, bool):
            raise TypeError("create_if_missing must be bool")
        if not isinstance(require_existing_schema, bool):
            raise TypeError("require_existing_schema must be bool")
        if require_existing_schema and create_if_missing:
            raise ValueError("strict existing storage cannot initialize a database")
        self.path = Path(path)
        # Same construction-time absolute path for URI and inode comparison.
        self._identity_path = self.path.absolute()
        # Each post-start connection must refuse to create a replacement file
        # if the original store is unlinked or temporarily unavailable.
        self._existing_uri = (
            "file:" + quote(str(self._identity_path), safe="/") + "?mode=rw"
        )
        created_new = False
        if create_if_missing:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation prevents --init-db from ever repairing a
            # damaged existing database into an empty write-recovery store.
            try:
                descriptor = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
                created_new = True
        if not created_new:
            # Never repair lost recovery schema in an existing store, even
            # when the caller explicitly passed --init-db.
            with self._connect() as connection:
                self._validate_store_schema(
                    connection,
                    allow_additive_migrations=not require_existing_schema,
                )
        self._dashboard_pending_write_lock = RLock()
        # A separately owned, existing-only SQLite store must not run DDL
        # (including silently recreating missing optional performance indexes).
        # The default launcher and historical import paths remain unchanged.
        if not require_existing_schema:
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._existing_uri, uri=True, factory=_ClosingSQLiteConnection
        )
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _assert_binary_history_key_schema(
        connection: sqlite3.Connection, table: str,
    ) -> None:
        """Fail closed when SQLite identity and Python exact IDs diverge.

        PRAGMA table_info omits native column collation. Verify an anchored
        canonical unquoted DDL prefix, then validate actual index collations
        via SQLite's index_xinfo. The table is always from a fixed whitelist.
        """
        rule = _HISTORY_BINARY_KEY_DDL.get(table)
        if rule is None:
            raise ValueError("unsupported historical ad ID table")
        record = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if (
            record is None
            or type(record["sql"]) is not str
            or rule.match(record["sql"]) is None
        ):
            raise sqlite3.DatabaseError("invalid history key collation")

        index_name = f"idx_{table}_ad_id_observed"
        index = next((
            row for row in connection.execute(f"PRAGMA index_list({table})")
            if row["name"] == index_name
        ), None)
        if index is None or index["partial"] or index["origin"] != "c":
            raise sqlite3.DatabaseError("invalid history key index")
        key_columns = [
            (row["name"], row["coll"], row["desc"])
            for row in connection.execute(f"PRAGMA index_xinfo({index_name})")
            if row["key"]
        ]
        if key_columns != [
            ("ad_id", "BINARY", 0),
            ("observed_at", "BINARY", 0),
            ("id", "BINARY", 0),
        ]:
            raise sqlite3.DatabaseError("invalid history key index")

    @staticmethod
    def _blank_history_index_sql(table: str) -> str:
        if table not in _HISTORY_BINARY_KEY_DDL:
            raise ValueError("unsupported historical ad ID table")
        return (
            f"CREATE INDEX IF NOT EXISTS idx_{table}_blank_ad_id "
            f"ON {table}(ad_id) WHERE {_HISTORY_BLANK_AD_ID_PREDICATE}"
        )

    @classmethod
    def _assert_blank_history_key_index(
        cls, connection: sqlite3.Connection, table: str,
        *, allow_missing: bool = False,
    ) -> None:
        """Only a canonical, truly partial BINARY index can prove absence."""
        # SQLite removes IF NOT EXISTS from sqlite_master.sql on creation.
        expected_sql = cls._blank_history_index_sql(table).replace(
            "CREATE INDEX IF NOT EXISTS ", "CREATE INDEX ", 1
        )
        name = f"idx_{table}_blank_ad_id"
        matches = [
            row for row in connection.execute(f"PRAGMA index_list({table})")
            if row["name"] == name
        ]
        if not matches and allow_missing:
            return  # Prevalidated additive migration; validate after creation.
        if (
            len(matches) != 1
            or matches[0]["origin"] != "c"
            or matches[0]["unique"] != 0
            or matches[0]["partial"] != 1
        ):
            raise sqlite3.DatabaseError("invalid history blank-key index")
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name=? AND name=?", (table, name),
        ).fetchone()
        if (
            definition is None or type(definition["sql"]) is not str
            or definition["sql"] != expected_sql
        ):
            raise sqlite3.DatabaseError("invalid history blank-key index")
        columns = [
            (row["name"], row["coll"], row["desc"])
            for row in connection.execute(f"PRAGMA index_xinfo({name})")
            if row["key"] == 1
        ]
        if columns != [("ad_id", "BINARY", 0)]:
            raise sqlite3.DatabaseError("invalid history blank-key index")

    @staticmethod
    def _assert_no_blank_history_keys(
        connection: sqlite3.Connection, table: str,
    ) -> None:
        if table not in _HISTORY_BINARY_KEY_DDL:
            raise ValueError("unsupported historical ad ID table")
        # INDEXED BY makes a missing index fail closed; no unbounded fallback.
        invalid = connection.execute(
            f"SELECT 1 FROM {table} INDEXED BY idx_{table}_blank_ad_id "
            f"WHERE {_HISTORY_BLANK_AD_ID_PREDICATE} LIMIT 1"
        ).fetchone()
        if invalid is not None:
            raise sqlite3.DatabaseError("invalid stored history ad ID")

    @staticmethod
    def _validate_store_schema(
        connection: sqlite3.Connection, *, allow_additive_migrations: bool = False
    ) -> None:
        integrity = connection.execute("PRAGMA quick_check(1)").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise sqlite3.DatabaseError("database integrity check failed")

        marker = connection.execute("PRAGMA user_version").fetchone()
        version = int(marker[0]) if marker is not None else -1
        if version not in (0, _SYNC_SCHEMA_VERSION, _STORE_SCHEMA_VERSION):
            raise sqlite3.DatabaseError("database version is unsupported")

        for table, expected in _REQUIRED_STORE_COLUMNS.items():
            table_type = connection.execute(
                "SELECT type FROM sqlite_master WHERE name = ?", (table,)
            ).fetchone()
            if table_type is None:
                # A version-1 store predates only this new append-only table.
                # Never recreate a missing clearance table in a version-2
                # database: doing so would erase previously granted fences.
                if (
                    allow_additive_migrations
                    and version < _STORE_SCHEMA_VERSION
                    and table == "write_recovery_clearances"
                ):
                    continue
                raise sqlite3.DatabaseError("database schema is incomplete")
            if table_type["type"] != "table":
                raise sqlite3.DatabaseError("database schema is incomplete")
            columns = {
                str(row["name"])
                for row in connection.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            migratable = (
                _LEGACY_ADDABLE_COLUMNS.get(table, frozenset())
                if allow_additive_migrations
                else frozenset()
            )
            if not (set(expected) - migratable).issubset(columns):
                raise sqlite3.DatabaseError("database schema is incomplete")
            if table in _HISTORY_BINARY_KEY_DDL:
                SnapshotStore._assert_binary_history_key_schema(connection, table)
                SnapshotStore._assert_blank_history_key_index(
                    connection, table, allow_missing=allow_additive_migrations,
                )
                if not allow_additive_migrations:
                    SnapshotStore._assert_no_blank_history_keys(connection, table)

            if table in _REQUIRED_SINGLE_COLUMN_UNIQUES:
                unique_columns: set[str] = set()
                for index in connection.execute(
                    f"PRAGMA index_list({table})"
                ).fetchall():
                    # A partial UNIQUE index only protects rows matching its
                    # WHERE predicate and is not a global recovery-key fence.
                    if not index["unique"] or index["partial"]:
                        continue
                    # Index names are database data, not trusted SQL strings.
                    index_name = str(index["name"]).replace('"', '""')
                    fields = connection.execute(
                        f'PRAGMA index_info("{index_name}")'
                    ).fetchall()
                    if len(fields) == 1 and fields[0]["name"] is not None:
                        unique_columns.add(str(fields[0]["name"]))
                if not set(_REQUIRED_SINGLE_COLUMN_UNIQUES[table]).issubset(
                    unique_columns
                ):
                    raise sqlite3.DatabaseError("database recovery keys are not unique")

        journal_type = connection.execute(
            "SELECT type FROM sqlite_master WHERE name='sync_attempts'"
        ).fetchone()
        if journal_type is None:
            if version != 0 or not allow_additive_migrations:
                raise sqlite3.DatabaseError("sync attempt journal is missing")
        else:
            if journal_type["type"] != "table":
                raise sqlite3.DatabaseError("sync attempt journal schema is invalid")
            columns = {
                str(row["name"]): row
                for row in connection.execute("PRAGMA table_info(sync_attempts)")
            }
            expected = {
                "id", "source", "started_at", "completed_at",
                "outcome", "error_kind", "snapshot_count",
            }
            if set(columns) != expected or columns["id"]["pk"] != 1:
                raise sqlite3.DatabaseError("sync attempt journal schema is invalid")
            if version == 0:
                if not allow_additive_migrations:
                    raise sqlite3.DatabaseError("sync attempt schema version is missing")
                if connection.execute("SELECT 1 FROM sync_attempts LIMIT 1").fetchone():
                    raise sqlite3.DatabaseError("unversioned sync journal is ambiguous")
            else:
                # Readiness must not claim success when the values required
                # by the actual dashboard projection cannot be decoded.
                for statement in (
                    "SELECT * FROM sync_attempts ORDER BY id DESC LIMIT 1",
                    "SELECT * FROM sync_attempts WHERE outcome IN "
                    "('success_empty','success_nonempty') "
                    "ORDER BY completed_at DESC, id DESC LIMIT 1",
                ):
                    record = connection.execute(statement).fetchone()
                    if record is not None:
                        _sync_attempt_from_row(record)
                # A valid oldest open record does not establish that a later
                # unfinished predecessor is safe. Audit every open generation.
                _unresolved_sync_attempt_summary(connection)

    def is_ready(self) -> bool:
        """Observe complete schema/data without creating or repairing a store."""
        try:
            with self._connect() as connection:
                self._validate_store_schema(connection)
        except sqlite3.Error:
            return False
        return True

    def dashboard_pending_write_guard(self) -> AbstractContextManager[None]:
        """Serialize pending-write protection changes with media pruning."""

        return self._dashboard_pending_write_lock

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ad_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ad_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    lifecycle_state TEXT NOT NULL,
                    title TEXT,
                    description TEXT,
                    views INTEGER,
                    watch_count INTEGER,
                    reply_count INTEGER,
                    metric_source TEXT CHECK (
                        metric_source IS NULL OR
                        (typeof(metric_source) = 'text' AND length(trim(metric_source)) > 0)
                    )
                );

                CREATE INDEX IF NOT EXISTS idx_ad_snapshots_ad_id_observed
                    ON ad_snapshots(ad_id, observed_at, id);

                CREATE TABLE IF NOT EXISTS reaction_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ad_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    conversation_count INTEGER NOT NULL,
                    unique_buyer_count INTEGER NOT NULL,
                    inbound_message_count INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_reaction_snapshots_ad_id_observed
                    ON reaction_snapshots(ad_id, observed_at, id);

                CREATE TABLE IF NOT EXISTS inbound_message_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider_message_id TEXT NOT NULL UNIQUE,
                    ad_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    source TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_inbound_message_events_ad_id_observed
                    ON inbound_message_events(ad_id, observed_at, id);

                CREATE TABLE IF NOT EXISTS ad_classifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ad_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    image_type TEXT,
                    city TEXT,
                    text_type TEXT,
                    title_type TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_ad_classifications_ad_id_observed
                    ON ad_classifications(ad_id, observed_at, id);

                CREATE TABLE IF NOT EXISTS operation_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    ad_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    pre_read_status TEXT NOT NULL,
                    post_read_status TEXT,
                    writer_invoked INTEGER NOT NULL,
                    authorization_by TEXT,
                    authorization_reference TEXT,
                    writer_error TEXT,
                    pre_snapshot_json TEXT,
                    post_snapshot_json TEXT
                );


                CREATE TABLE IF NOT EXISTS create_operation_receipts (
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
                    authorization_by TEXT,
                    authorization_reference TEXT,
                    media_post_read_status TEXT,
                    media_persistence_confirmed INTEGER NOT NULL DEFAULT 0,
                    writer_error TEXT,
                    post_snapshot_json TEXT,
                    confirmation_post_snapshot_json TEXT,
                    content_post_snapshot_json TEXT
                );

                CREATE TABLE IF NOT EXISTS create_operation_checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checkpoint_kind TEXT NOT NULL
                        CHECK (checkpoint_kind = 'before_media_post_read'),
                    operation TEXT NOT NULL,
                    created_ad_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    pre_read_status TEXT NOT NULL,
                    confirmation_pre_read_status TEXT NOT NULL,
                    post_read_status TEXT,
                    confirmation_post_read_status TEXT,
                    content_post_read_status TEXT,
                    writer_invoked INTEGER NOT NULL,
                    authorization_by TEXT,
                    authorization_reference TEXT,
                    writer_error TEXT,
                    post_snapshot_json TEXT,
                    confirmation_post_snapshot_json TEXT,
                    content_post_snapshot_json TEXT
                );

                CREATE TABLE IF NOT EXISTS write_api_requests (
                    idempotency_key TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL
                        CHECK (state IN ('in_progress', 'completed')),
                    requested_at TEXT NOT NULL,
                    claim_owner TEXT,
                    execution_started_at TEXT,
                    completed_at TEXT,
                    response_status INTEGER,
                    response_json TEXT,
                    CHECK (
                        (
                            state = 'in_progress'
                            AND completed_at IS NULL
                            AND response_status IS NULL
                            AND response_json IS NULL
                        )
                        OR
                        (
                            state = 'completed'
                            AND completed_at IS NOT NULL
                            AND response_status IS NOT NULL
                            AND response_json IS NOT NULL
                        )
                    )
                );

                CREATE TABLE IF NOT EXISTS write_recovery_clearances (
                    idempotency_key TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL
                        CHECK(length(request_sha256) = 64),
                    observed_at TEXT NOT NULL,
                    verification_kind TEXT NOT NULL CHECK (
                        verification_kind IN (
                            'media_submit_reconciled',
                            'operator_verified_postread'
                        )
                    ),
                    evidence_reference TEXT NOT NULL CHECK (
                        length(trim(evidence_reference)) BETWEEN 1 AND 256
                    )
                );

                CREATE TABLE IF NOT EXISTS dashboard_pending_writes (
                    scope TEXT PRIMARY KEY,
                    resource_key TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    method TEXT NOT NULL
                        CHECK (method IN ('POST', 'PATCH', 'DELETE')),
                    path TEXT NOT NULL,
                    payload_json TEXT,
                    ad_id TEXT,
                    acknowledged INTEGER NOT NULL DEFAULT 0
                        CHECK (acknowledged IN (0, 1))
                );

                CREATE TABLE IF NOT EXISTS sync_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL CHECK(length(trim(source)) > 0),
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    outcome TEXT NOT NULL CHECK (
                        outcome IN ('in_progress', 'success_empty', 'success_nonempty', 'failed')
                    ),
                    error_kind TEXT,
                    snapshot_count INTEGER,
                    CHECK (
                        (outcome = 'in_progress' AND completed_at IS NULL
                         AND error_kind IS NULL AND snapshot_count IS NULL)
                        OR (outcome = 'failed' AND completed_at IS NOT NULL
                            AND error_kind IS NOT NULL AND snapshot_count IS NULL)
                        OR (outcome = 'success_empty' AND completed_at IS NOT NULL
                            AND error_kind IS NULL AND snapshot_count = 0)
                        OR (outcome = 'success_nonempty' AND completed_at IS NOT NULL
                            AND error_kind IS NULL AND snapshot_count > 0)
                    )
                );
                """
            )
            for table in _HISTORY_BINARY_KEY_DDL:
                connection.execute(self._blank_history_index_sql(table))
            connection.execute("BEGIN IMMEDIATE")
            # Existing stores are pre-validated, including write-recovery fences,
            # before this strictly additive data-only column upgrade.
            metric_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(ad_snapshots)"
                ).fetchall()
            }
            if "metric_source" not in metric_columns:
                connection.execute(
                    "ALTER TABLE ad_snapshots ADD COLUMN metric_source TEXT "
                    "CHECK (metric_source IS NULL OR "
                    "(typeof(metric_source) = 'text' "
                    "AND length(trim(metric_source)) > 0))"
                )
            dashboard_pending_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(dashboard_pending_writes)"
                ).fetchall()
            }
            if "acknowledged" not in dashboard_pending_columns:
                connection.execute(
                    "ALTER TABLE dashboard_pending_writes "
                    "ADD COLUMN acknowledged INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (acknowledged IN (0, 1))"
                )

            create_receipt_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(create_operation_receipts)"
                ).fetchall()
            }
            if "authorization_by" not in create_receipt_columns:
                connection.execute(
                    "ALTER TABLE create_operation_receipts "
                    "ADD COLUMN authorization_by TEXT"
                )
            if "authorization_reference" not in create_receipt_columns:
                connection.execute(
                    "ALTER TABLE create_operation_receipts "
                    "ADD COLUMN authorization_reference TEXT"
                )
            if "media_post_read_status" not in create_receipt_columns:
                connection.execute(
                    "ALTER TABLE create_operation_receipts "
                    "ADD COLUMN media_post_read_status TEXT"
                )
            if "media_persistence_confirmed" not in create_receipt_columns:
                connection.execute(
                    "ALTER TABLE create_operation_receipts "
                    "ADD COLUMN media_persistence_confirmed "
                    "INTEGER NOT NULL DEFAULT 0"
                )

            write_api_request_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(write_api_requests)"
                ).fetchall()
            }
            if "claim_owner" not in write_api_request_columns:
                connection.execute(
                    "ALTER TABLE write_api_requests "
                    "ADD COLUMN claim_owner TEXT"
                )
            if "execution_started_at" not in write_api_request_columns:
                connection.execute(
                    "ALTER TABLE write_api_requests "
                    "ADD COLUMN execution_started_at TEXT"
                )

            # Existing stores pass old Write-recovery validation before
            # migrations. The journal and version marker commit together.
            connection.execute(f"PRAGMA user_version = {_STORE_SCHEMA_VERSION}")
            self._validate_store_schema(connection)

    @staticmethod
    def _insert_ad_snapshot(
        connection: sqlite3.Connection,
        snapshot: AdSnapshot,
    ) -> None:
        connection.execute(
            """
            INSERT INTO ad_snapshots (
                ad_id, observed_at, source, lifecycle_state, title, description,
                views, watch_count, reply_count, metric_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.ad_id,
                snapshot.observed_at.isoformat(),
                snapshot.source,
                snapshot.lifecycle_state.value,
                snapshot.title,
                snapshot.description,
                snapshot.views,
                snapshot.watch_count,
                snapshot.reply_count,
                snapshot.metric_source,
            ),
        )

    def append_ad_snapshot(self, snapshot: AdSnapshot) -> None:
        with self._connect() as connection:
            self._insert_ad_snapshot(connection, snapshot)

    def append_reaction_snapshot(self, snapshot: ReactionSnapshot) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO reaction_snapshots (
                    ad_id, observed_at, source, conversation_count,
                    unique_buyer_count, inbound_message_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.ad_id,
                    snapshot.observed_at.isoformat(),
                    snapshot.source,
                    snapshot.conversation_count,
                    snapshot.unique_buyer_count,
                    snapshot.inbound_message_count,
                ),
            )

    def append_inbound_message_events(
        self,
        events: Iterable[InboundMessageEvent],
    ) -> int:
        """Atomically append minimal inbound-message events.

        Exact provider-message duplicates are idempotent. Reusing one provider
        message ID for different event data fails closed and rolls back the
        complete batch.
        """

        unique_events: dict[str, InboundMessageEvent] = {}
        for event in tuple(events):
            previous = unique_events.get(event.provider_message_id)
            if previous is not None and previous != event:
                raise ValueError(
                    "provider_message_id conflict inside import batch"
                )
            unique_events[event.provider_message_id] = event

        if not unique_events:
            return 0

        inserted = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for event in unique_events.values():
                row = connection.execute(
                    """
                    SELECT ad_id, conversation_id, provider_message_id,
                           observed_at, source
                    FROM inbound_message_events
                    WHERE provider_message_id = ?
                    """,
                    (event.provider_message_id,),
                ).fetchone()
                candidate = (
                    event.ad_id,
                    event.conversation_id,
                    event.provider_message_id,
                    event.observed_at.isoformat(),
                    event.source,
                )
                if row is not None:
                    stored = (
                        row["ad_id"],
                        row["conversation_id"],
                        row["provider_message_id"],
                        row["observed_at"],
                        row["source"],
                    )
                    if stored != candidate:
                        raise ValueError(
                            "provider_message_id conflict with stored event"
                        )
                    continue

                connection.execute(
                    """
                    INSERT INTO inbound_message_events (
                        provider_message_id, ad_id, conversation_id,
                        observed_at, source
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event.provider_message_id,
                        event.ad_id,
                        event.conversation_id,
                        event.observed_at.isoformat(),
                        event.source,
                    ),
                )
                inserted += 1
        return inserted

    @staticmethod
    def _insert_classification(
        connection: sqlite3.Connection,
        classification: AdClassification,
    ) -> None:
        connection.execute(
            """
            INSERT INTO ad_classifications (
                ad_id, observed_at, source, image_type, city,
                text_type, title_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                classification.ad_id,
                classification.observed_at.isoformat(),
                classification.source,
                classification.image_type,
                classification.city,
                classification.text_type,
                classification.title_type,
            ),
        )

    def append_classification(self, classification: AdClassification) -> None:
        with self._connect() as connection:
            self._insert_classification(connection, classification)

    def merge_classification(
        self,
        *,
        ad_id: str,
        source: str,
        changes: Mapping[str, str | None],
        observed_at: datetime | None = None,
        validate_result: Callable[[AdClassification], None] | None = None,
    ) -> AdClassification:
        """Atomically merge one classification for a locally evidenced ad."""

        requested_changes = dict(changes)
        unknown_fields = sorted(
            set(requested_changes) - set(_CLASSIFICATION_FIELDS)
        )
        if unknown_fields:
            raise ValueError(
                "unknown classification dimensions: "
                + ", ".join(unknown_fields)
            )
        if not requested_changes:
            raise ValueError("at least one classification change is required")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            known = connection.execute(
                """
                SELECT 1
                FROM ad_snapshots
                WHERE ad_id = ?
                UNION ALL
                SELECT 1
                FROM inbound_message_events
                WHERE ad_id = ?
                LIMIT 1
                """,
                (ad_id, ad_id),
            ).fetchone()
            if known is None:
                raise ValueError(f"unknown analytics ad_id: {ad_id}")

            rows = connection.execute(
                """
                SELECT id, ad_id, observed_at, source, image_type, city,
                       text_type, title_type
                FROM ad_classifications
                WHERE ad_id = ?
                """,
                (ad_id,),
            ).fetchall()
            previous_row = (
                max(
                    rows,
                    key=lambda row: (
                        datetime.fromisoformat(row["observed_at"]),
                        int(row["id"]),
                    ),
                )
                if rows
                else None
            )
            previous = (
                AdClassification(
                    ad_id=previous_row["ad_id"],
                    observed_at=datetime.fromisoformat(
                        previous_row["observed_at"]
                    ),
                    source=previous_row["source"],
                    image_type=previous_row["image_type"],
                    city=previous_row["city"],
                    text_type=previous_row["text_type"],
                    title_type=previous_row["title_type"],
                )
                if previous_row is not None
                else None
            )

            merged = {
                field_name: (
                    getattr(previous, field_name)
                    if previous is not None
                    else None
                )
                for field_name in _CLASSIFICATION_FIELDS
            }
            merged.update(requested_changes)

            classification = AdClassification(
                ad_id=ad_id,
                observed_at=observed_at or datetime.now(timezone.utc),
                source=source,
                **merged,
            )
            if (
                previous is not None
                and classification.observed_at <= previous.observed_at
            ):
                raise ValueError(
                    "observed_at must be later than the latest classification"
                )

            previous_values = (
                {
                    field_name: getattr(previous, field_name)
                    for field_name in _CLASSIFICATION_FIELDS
                }
                if previous is not None
                else {
                    field_name: None
                    for field_name in _CLASSIFICATION_FIELDS
                }
            )
            classification_values = {
                field_name: getattr(classification, field_name)
                for field_name in _CLASSIFICATION_FIELDS
            }
            if classification_values == previous_values:
                raise ValueError(
                    "classification update would not change any label"
                )

            # Callers with bounded IPC must validate the fully merged
            # representation *inside* this BEGIN IMMEDIATE transaction.
            # An invalid reply cannot be committed and then made unreadable.
            if validate_result is not None:
                validate_result(classification)
            self._insert_classification(connection, classification)
            return classification

    @staticmethod
    def _snapshot_json(snapshot: AdSnapshot | None) -> str | None:
        if snapshot is None:
            return None
        data = asdict(snapshot)
        data["observed_at"] = snapshot.observed_at.isoformat()
        data["lifecycle_state"] = snapshot.lifecycle_state.value
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _confirmed_write_snapshot(
        receipt: OperationReceipt | CreateOperationReceipt,
    ) -> AdSnapshot | None:
        """Project only target-bound observations from a confirmed core outcome.

        This consumes the orchestrator's confirmation; it does not establish
        independent evidence or treat a target-only read as a full inventory.
        Incomplete/legacy receipts remain audit records without a projection.
        """

        if (
            receipt.outcome is not OperationOutcome.CONFIRMED
            or receipt.writer_invoked is not True
        ):
            return None

        if isinstance(receipt, CreateOperationReceipt):
            snapshots = (
                receipt.post_snapshot,
                receipt.confirmation_post_snapshot,
                receipt.content_post_snapshot,
            )
            if (
                receipt.operation != "create"
                or receipt.created_ad_id is None
                or any(
                    status != ReadStatus.SUCCESS_NONEMPTY.value
                    for status in (
                        receipt.post_read_status,
                        receipt.confirmation_post_read_status,
                        receipt.content_post_read_status,
                    )
                )
                or any(
                    item is None or item.ad_id != receipt.created_ad_id
                    for item in snapshots
                )
            ):
                return None
            # This is the final target-bound owner/content observation, not
            # the submitted request and not a claim about media persistence.
            return receipt.content_post_snapshot

        if (
            receipt.pre_read_status != ReadStatus.SUCCESS_NONEMPTY.value
            or receipt.pre_snapshot is None
            or receipt.pre_snapshot.ad_id != receipt.ad_id
        ):
            return None
        if receipt.operation == "delete":
            if (
                receipt.post_snapshot is not None
                or receipt.post_read_status not in {
                    ReadStatus.SUCCESS_EMPTY.value,
                    ReadStatus.SUCCESS_NONEMPTY.value,
                }
            ):
                return None
            # The core has already confirmed absence through both inventories.
            # Only this ID is absent; never infer anything about other ads.
            return AdSnapshot(
                ad_id=receipt.ad_id,
                observed_at=receipt.completed_at,
                source="confirmed-write:delete",
                lifecycle_state=LifecycleState.ABSENT,
            )
        if (
            receipt.operation not in {
                "update_content", "set_state:active", "set_state:paused",
            }
            or receipt.post_read_status != ReadStatus.SUCCESS_NONEMPTY.value
            or receipt.post_snapshot is None
            or receipt.post_snapshot.ad_id != receipt.ad_id
        ):
            return None
        return receipt.post_snapshot

    def append_operation_receipt(self, receipt: OperationReceipt) -> None:
        snapshot = self._confirmed_write_snapshot(receipt)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO operation_receipts (
                    operation, ad_id, started_at, completed_at, outcome,
                    pre_read_status, post_read_status, writer_invoked,
                    authorization_by, authorization_reference, writer_error,
                    pre_snapshot_json, post_snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.operation,
                    receipt.ad_id,
                    receipt.started_at.isoformat(),
                    receipt.completed_at.isoformat(),
                    receipt.outcome.value,
                    receipt.pre_read_status,
                    receipt.post_read_status,
                    1 if receipt.writer_invoked else 0,
                    receipt.authorization_by,
                    receipt.authorization_reference,
                    receipt.writer_error,
                    self._snapshot_json(receipt.pre_snapshot),
                    self._snapshot_json(receipt.post_snapshot),
                ),
            )
            if snapshot is not None:
                self._insert_ad_snapshot(connection, snapshot)

    def append_create_operation_checkpoint(
        self,
        receipt: CreateOperationReceipt,
    ) -> int:
        """Durably checkpoint a confirmed write before external media reads."""

        if (
            receipt.outcome is not OperationOutcome.CONFIRMED
            or receipt.created_ad_id is None
            or not receipt.writer_invoked
        ):
            raise ValueError(
                "media post-read checkpoint requires a confirmed invoked create"
            )
        if (
            receipt.media_post_read_status is not None
            or receipt.media_persistence_confirmed
        ):
            raise ValueError(
                "media post-read checkpoint requires unclassified media evidence"
            )

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO create_operation_checkpoints (
                    checkpoint_kind, operation, created_ad_id,
                    started_at, completed_at, outcome,
                    pre_read_status, confirmation_pre_read_status,
                    post_read_status, confirmation_post_read_status,
                    content_post_read_status, writer_invoked,
                    authorization_by, authorization_reference, writer_error,
                    post_snapshot_json, confirmation_post_snapshot_json,
                    content_post_snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "before_media_post_read",
                    receipt.operation,
                    receipt.created_ad_id,
                    receipt.started_at.isoformat(),
                    receipt.completed_at.isoformat(),
                    receipt.outcome.value,
                    receipt.pre_read_status,
                    receipt.confirmation_pre_read_status,
                    receipt.post_read_status,
                    receipt.confirmation_post_read_status,
                    receipt.content_post_read_status,
                    1,
                    receipt.authorization_by,
                    receipt.authorization_reference,
                    receipt.writer_error,
                    self._snapshot_json(receipt.post_snapshot),
                    self._snapshot_json(receipt.confirmation_post_snapshot),
                    self._snapshot_json(receipt.content_post_snapshot),
                ),
            )
            checkpoint_id = cursor.lastrowid
        if checkpoint_id is None:
            raise RuntimeError("create operation checkpoint did not return an id")
        return int(checkpoint_id)

    def append_create_operation_receipt(
        self,
        receipt: CreateOperationReceipt,
    ) -> None:
        snapshot = self._confirmed_write_snapshot(receipt)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO create_operation_receipts (
                    operation, created_ad_id, started_at, completed_at, outcome,
                    pre_read_status, confirmation_pre_read_status,
                    post_read_status, confirmation_post_read_status,
                    content_post_read_status, writer_invoked,
                    authorization_by, authorization_reference,
                    media_post_read_status, media_persistence_confirmed,
                    writer_error, post_snapshot_json,
                    confirmation_post_snapshot_json, content_post_snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.operation,
                    receipt.created_ad_id,
                    receipt.started_at.isoformat(),
                    receipt.completed_at.isoformat(),
                    receipt.outcome.value,
                    receipt.pre_read_status,
                    receipt.confirmation_pre_read_status,
                    receipt.post_read_status,
                    receipt.confirmation_post_read_status,
                    receipt.content_post_read_status,
                    1 if receipt.writer_invoked else 0,
                    receipt.authorization_by,
                    receipt.authorization_reference,
                    (
                        receipt.media_post_read_status.value
                        if receipt.media_post_read_status is not None
                        else None
                    ),
                    1 if receipt.media_persistence_confirmed else 0,
                    receipt.writer_error,
                    self._snapshot_json(receipt.post_snapshot),
                    self._snapshot_json(receipt.confirmation_post_snapshot),
                    self._snapshot_json(receipt.content_post_snapshot),
                ),
            )
            if snapshot is not None:
                self._insert_ad_snapshot(connection, snapshot)

    @staticmethod
    def _write_api_request_record(
        row: sqlite3.Row,
    ) -> WriteApiRequestRecord:
        return WriteApiRequestRecord(
            idempotency_key=str(row["idempotency_key"]),
            request_sha256=str(row["request_sha256"]),
            state=str(row["state"]),
            requested_at=datetime.fromisoformat(row["requested_at"]),
            claim_owner=(
                str(row["claim_owner"])
                if row["claim_owner"] is not None
                else None
            ),
            execution_started_at=(
                datetime.fromisoformat(row["execution_started_at"])
                if row["execution_started_at"] is not None
                else None
            ),
            completed_at=(
                datetime.fromisoformat(row["completed_at"])
                if row["completed_at"] is not None
                else None
            ),
            response_status=(
                int(row["response_status"])
                if row["response_status"] is not None
                else None
            ),
            response_json=(
                str(row["response_json"])
                if row["response_json"] is not None
                else None
            ),
        )

    def write_api_request(
        self,
        idempotency_key: str,
    ) -> WriteApiRequestRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT idempotency_key, request_sha256, state, requested_at,
                       claim_owner, execution_started_at, completed_at,
                       response_status, response_json
                FROM write_api_requests
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        return (
            self._write_api_request_record(row)
            if row is not None
            else None
        )

    def create_recovery_pending(
        self, *, exclude_idempotency_key: str, claim_owner: str
    ) -> bool:
        """Fail closed on older executed Writes or unresolved media Creates.

        An ambiguous media receipt can be HTTP-completed while its browser
        submit remains unsettled. Its durable HTTP response, rather than the
        process-local media page, must prevent a new key from re-submitting.
        An older in-progress execution is similarly non-retryable even if the
        current server owns both request claims.
        This check does not resolve an old fence; independent reconciliation
        and an explicit persisted clearance are required for that.
        """
        if not isinstance(exclude_idempotency_key, str) or not exclude_idempotency_key:
            raise ValueError("excluded idempotency key is required")
        _validated_write_api_claim_owner(claim_owner)
        with closing(self._connect()) as connection:
            # Keep accepted but never-started claims recoverable. A started
            # write with unknown outcome can still have reached the platform.
            if connection.execute(
                """
                SELECT 1 FROM write_api_requests AS w
                LEFT JOIN write_recovery_clearances AS c
                  ON c.idempotency_key = w.idempotency_key
                 AND c.request_sha256 = w.request_sha256
                WHERE w.idempotency_key != ?
                  AND w.state = 'in_progress'
                  AND w.execution_started_at IS NOT NULL
                  AND c.idempotency_key IS NULL
                LIMIT 1
                """,
                (exclude_idempotency_key,),
            ).fetchone() is not None:
                return True
            # Runtime must enforce the same durable before-media Create fences
            # the independent offline pair inventory promises to preserve.
            # A checkpoint is settled only by a final media-confirmed receipt
            # bound to EVERY immutable source field. In particular, an
            # independently valid but unrelated final receipt, or a final
            # timestamp earlier by even one microsecond, cannot release it.
            # Keep this runtime predicate independent of offline inspector
            # code; the paired acceptance tests cross-check both contracts.
            previous_checkpoint_id = None
            previous_checkpoint_settled = False
            checkpoint_rows = connection.execute(
                """
                SELECT c.id, c.completed_at, r.completed_at,
                       independent.idempotency_key
                FROM create_operation_checkpoints AS c
                LEFT JOIN create_operation_receipts AS r
                  ON c.operation = r.operation
                 AND c.created_ad_id = r.created_ad_id
                 AND c.started_at = r.started_at
                 AND c.authorization_by IS r.authorization_by
                 AND c.authorization_reference IS r.authorization_reference
                 AND c.pre_read_status = r.pre_read_status
                 AND c.confirmation_pre_read_status = r.confirmation_pre_read_status
                 AND c.post_read_status IS r.post_read_status
                 AND c.confirmation_post_read_status IS r.confirmation_post_read_status
                 AND c.content_post_read_status IS r.content_post_read_status
                 AND c.writer_error IS r.writer_error
                 AND c.post_snapshot_json IS r.post_snapshot_json
                 AND c.confirmation_post_snapshot_json IS r.confirmation_post_snapshot_json
                 AND c.content_post_snapshot_json IS r.content_post_snapshot_json
                 AND r.outcome = 'confirmed'
                 AND r.writer_invoked = 1
                 AND r.media_persistence_confirmed = 1
                 AND r.media_post_read_status = 'confirmed'
                LEFT JOIN write_api_requests AS original_write
                  ON c.authorization_reference =
                        ('write-api:' || original_write.idempotency_key)
                 AND original_write.execution_started_at IS NOT NULL
                LEFT JOIN write_recovery_clearances AS independent
                  ON independent.idempotency_key = original_write.idempotency_key
                 AND independent.request_sha256 = original_write.request_sha256
                 AND independent.verification_kind = 'operator_verified_postread'
                ORDER BY c.id, r.id
                """
            )
            for (checkpoint_id, checkpoint_completed, final_completed,
                 independent_clearance) in checkpoint_rows:
                if checkpoint_id != previous_checkpoint_id:
                    if previous_checkpoint_id is not None and not previous_checkpoint_settled:
                        return True
                    previous_checkpoint_id = checkpoint_id
                    # Only a persisted exact operation-bound operator
                    # clearance can independently reconcile an unfinished
                    # checkpoint. Executor-only media evidence is never enough.
                    previous_checkpoint_settled = independent_clearance is not None
                if final_completed is None:
                    continue
                try:
                    checkpoint_time = datetime.fromisoformat(checkpoint_completed)
                    final_time = datetime.fromisoformat(final_completed)
                    if (
                        checkpoint_time.utcoffset() is not None
                        and final_time.utcoffset() is not None
                        and final_time >= checkpoint_time
                    ):
                        previous_checkpoint_settled = True
                except (TypeError, ValueError):
                    # Invalid historical data cannot prove settlement.
                    continue
            if previous_checkpoint_id is not None and not previous_checkpoint_settled:
                return True
            if connection.execute(
                """
                SELECT 1 FROM create_operation_receipts AS r
                LEFT JOIN write_api_requests AS w
                  ON r.authorization_reference = ('write-api:' || w.idempotency_key)
                LEFT JOIN write_recovery_clearances AS c
                  ON c.idempotency_key = w.idempotency_key
                 AND c.request_sha256 = w.request_sha256
                WHERE r.operation = 'create' AND r.outcome = 'ambiguous'
                  AND r.writer_invoked = 1
                  AND r.media_post_read_status IS NOT NULL
                  AND c.idempotency_key IS NULL
                LIMIT 1
                """
            ).fetchone() is not None:
                return True
            rows = connection.execute(
                """
                SELECT w.idempotency_key, w.response_status, w.response_json
                FROM write_api_requests AS w
                LEFT JOIN write_recovery_clearances AS c
                  ON c.idempotency_key = w.idempotency_key
                 AND c.request_sha256 = w.request_sha256
                WHERE w.idempotency_key != ?
                  AND w.state = 'completed'
                  AND c.idempotency_key IS NULL
                  AND (
                    (CASE WHEN json_valid(w.response_json) = 0 THEN 1
                          WHEN json_type(w.response_json) != 'object' THEN 1
                          ELSE json_type(
                            w.response_json, '$.media_persistence_confirmed'
                          ) IS NOT NULL END)
                    OR (
                      w.response_status = 202
                      AND substr(
                        w.response_json, 1,
                        length('{"error":"write_outcome_unverified",')
                      ) = '{"error":"write_outcome_unverified",'
                    )
                  )
                """,
                (exclude_idempotency_key,),
            )
            for row in rows:
                try:
                    response = json.loads(row["response_json"])
                except (TypeError, ValueError):
                    # Historical corrupt JSON is an unresolved write, not
                    # permission to re-submit and not an opaque SQLite crash.
                    return True
                if not isinstance(response, dict):
                    # A parseable JSON primitive is not an HTTP response
                    # envelope and cannot authorize another Create.
                    return True
                try:
                    if (
                        row["response_status"] == 202
                        and response.get("error") == "write_outcome_unverified"
                    ):
                        if row["response_json"] != self._unverified_write_api_response(
                            row["idempotency_key"]
                        ):
                            raise ValueError("unverified response is not exact")
                        return True
                    if not isinstance(response.get("media_persistence_confirmed"), bool):
                        raise ValueError("invalid media response")
                    receipt = response.get("operation_receipt")
                    if receipt is None:
                        # An execution error may have followed browser submit.
                        return True
                    if not isinstance(receipt, dict):
                        raise ValueError("invalid media receipt")
                    outcome = receipt.get("outcome")
                    invoked = receipt.get("writer_invoked")
                    if not isinstance(invoked, bool) or not isinstance(outcome, str):
                        raise ValueError("invalid media receipt outcome")
                    if invoked and outcome == "ambiguous":
                        return True
                    if outcome not in {
                        "confirmed", "ambiguous", "precondition_failed",
                    }:
                        raise ValueError("unknown media outcome")
                except (TypeError, ValueError) as exc:
                    raise sqlite3.DatabaseError(
                        "invalid persisted media recovery evidence"
                    ) from exc
        return False

    @staticmethod
    def _original_unsettled_media_checkpoint(
        connection: sqlite3.Connection, *, idempotency_key: str,
        operation_receipt: Mapping[str, object],
    ) -> bool:
        """Require one original checkpoint, matching final row and full HTTP receipt.

        The HTTP receipt omits snapshots. Their immutable identity must be
        established by comparing all three persisted SQLite snapshot fields
        against the sole final Create receipt. No media result reported by the
        browser/executor supplies independent operator authority.
        """
        if type(operation_receipt) is not dict:
            return False
        wire = operation_receipt
        required = {
            "operation", "created_ad_id", "started_at", "completed_at",
            "outcome", "pre_read_status", "confirmation_pre_read_status",
            "post_read_status", "confirmation_post_read_status",
            "content_post_read_status", "writer_invoked",
            "authorization_by", "authorization_reference", "writer_error",
            "media_post_read_status", "media_persistence_confirmed",
        }
        if set(wire) != required or (
            wire["operation"] != "create"
            or wire["outcome"] != "confirmed"
            or wire["writer_invoked"] is not True
            or wire["media_persistence_confirmed"] is not False
            or type(wire["media_post_read_status"]) is not str
            or wire["media_post_read_status"] not in {
                "not_read", "verifier_unavailable", "mismatch", "unknown",
            }
            or type(wire["created_ad_id"]) is not str
            or not wire["created_ad_id"].strip()
            or wire["authorization_reference"] != "write-api:" + idempotency_key
            or (wire["authorization_by"] is not None and (
                type(wire["authorization_by"]) is not str
                or not wire["authorization_by"].strip()
            ))
        ):
            return False

        checkpoint_fields = (
            "checkpoint_kind", "operation", "created_ad_id", "started_at",
            "completed_at", "outcome", "pre_read_status",
            "confirmation_pre_read_status", "post_read_status",
            "confirmation_post_read_status", "content_post_read_status",
            "writer_invoked", "authorization_by", "authorization_reference",
            "writer_error", "post_snapshot_json",
            "confirmation_post_snapshot_json", "content_post_snapshot_json",
        )
        rows = connection.execute(
            "SELECT " + ",".join(checkpoint_fields)
            + " FROM create_operation_checkpoints "
            "WHERE operation='create' AND authorization_reference=?",
            ("write-api:" + idempotency_key,),
        ).fetchall()
        if len(rows) != 1:
            return False
        original = dict(zip(checkpoint_fields, rows[0]))
        if (
            original["checkpoint_kind"] != "before_media_post_read"
            or original["outcome"] != "confirmed"
            or type(original["writer_invoked"]) is not int
            or original["writer_invoked"] != 1
        ):
            return False

        final_fields = tuple(
            name for name in checkpoint_fields if name != "checkpoint_kind"
        ) + ("media_post_read_status", "media_persistence_confirmed")
        rows = connection.execute(
            "SELECT " + ",".join(final_fields)
            + " FROM create_operation_receipts "
            "WHERE operation='create' AND authorization_reference=?",
            ("write-api:" + idempotency_key,),
        ).fetchall()
        if len(rows) != 1:
            return False
        final = dict(zip(final_fields, rows[0]))
        if (
            type(final["writer_invoked"]) is not int
            or final["writer_invoked"] != 1
            or type(final["media_persistence_confirmed"]) is not int
            or final["media_persistence_confirmed"] != 0
        ):
            return False

        immutable = (
            "operation", "created_ad_id", "started_at", "outcome",
            "pre_read_status", "confirmation_pre_read_status",
            "post_read_status", "confirmation_post_read_status",
            "content_post_read_status", "authorization_by",
            "authorization_reference", "writer_error",
        )
        if any(
            original[name] != final[name] or wire[name] != final[name]
            for name in immutable
        ):
            return False
        if any(
            original[name] != final[name]
            for name in (
                "post_snapshot_json", "confirmation_post_snapshot_json",
                "content_post_snapshot_json",
            )
        ):
            return False
        if (
            wire["completed_at"] != final["completed_at"]
            or wire["media_post_read_status"] != final["media_post_read_status"]
        ):
            return False

        try:
            def canonical_time(raw: object) -> datetime:
                if type(raw) is not str or not 1 <= len(raw) <= 80:
                    raise ValueError("receipt timestamp invalid")
                value = datetime.fromisoformat(raw)
                if value.utcoffset() is None or value.isoformat() != raw:
                    raise ValueError("receipt timestamp not canonical")
                return value

            started = canonical_time(original["started_at"])
            checkpoint_done = canonical_time(original["completed_at"])
            final_done = canonical_time(final["completed_at"])
            return started <= checkpoint_done <= final_done
        except (TypeError, ValueError, OverflowError):
            return False

    def record_write_recovery_clearance(
        self, *, idempotency_key: str, request_sha256: str,
        verification_kind: str, evidence_reference: str, observed_at: datetime,
    ) -> None:
        """Append an operation-bound clearance after independent readback.

        This trusted local method does not itself access the external platform.
        The caller must have independently established the outcome before
        passing its specific evidence reference. It is never exposed as a
        Write API route or used to authorize an automatic browser retry.
        """
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency key is required")
        if (
            not isinstance(request_sha256, str)
            or len(request_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in request_sha256)
        ):
            raise ValueError("exact request fingerprint is required")
        if verification_kind not in {
            "media_submit_reconciled", "operator_verified_postread",
        }:
            raise ValueError("independent verification kind is required")
        if (
            not isinstance(evidence_reference, str)
            or not 1 <= len(evidence_reference.strip()) <= 256
            or any(ch in evidence_reference for ch in "\r\n\x00")
        ):
            raise ValueError("independent evidence reference is required")
        timestamp = _sync_time(observed_at)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._validate_store_schema(connection)
            row = connection.execute(
                """
                SELECT request_sha256, state, execution_started_at,
                       response_status, response_json FROM write_api_requests
                WHERE idempotency_key = ?
                """, (idempotency_key,),
            ).fetchone()
            if row is None or row["request_sha256"] != request_sha256:
                raise ValueError("write recovery operation binding mismatch")
            if row["execution_started_at"] is None:
                raise ValueError("write recovery was never executed")
            if (
                row["state"] == "completed"
                and row["response_status"] == 202
                and row["response_json"] == self._unverified_write_api_response(
                    idempotency_key
                )
            ):
                # The executor cannot use the in-process media observation
                # path to clear a fixed owner UNKNOWN. Only a separate,
                # independently authorized operator may later reconcile it.
                if verification_kind != "operator_verified_postread":
                    raise ValueError("unknown write requires independent operator")
            elif row["state"] == "completed":
                try:
                    response = json.loads(row["response_json"])
                    if not isinstance(response, dict):
                        raise ValueError("invalid response")
                    if not isinstance(response.get("media_persistence_confirmed"), bool):
                        raise ValueError("not a media response")
                    receipt = response.get("operation_receipt")
                    # A confirmed core Create can have an unconfirmed media
                    # post-read. The durable before-media checkpoint remains
                    # a fence until independent operator evidence reconciles
                    # this exact started, completed Write. Executor-side media
                    # observations may never issue this kind of clearance.
                    operator_reconciled_core = (
                        verification_kind == "operator_verified_postread"
                        and response["media_persistence_confirmed"] is False
                        and isinstance(receipt, dict)
                        and receipt.get("operation") == "create"
                        and receipt.get("outcome") == "confirmed"
                        and receipt.get("writer_invoked") is True
                        and receipt.get("media_post_read_status") in {
                            "not_read", "verifier_unavailable", "mismatch", "unknown",
                        }
                        and type(receipt.get("created_ad_id")) is str
                        and bool(receipt["created_ad_id"].strip())
                        and receipt.get("authorization_reference")
                            == "write-api:" + idempotency_key
                        and self._original_unsettled_media_checkpoint(
                            connection, idempotency_key=idempotency_key,
                            operation_receipt=receipt,
                        )
                    )
                    unresolved = (
                        receipt is None
                        or (
                            isinstance(receipt, dict)
                            and receipt.get("writer_invoked") is True
                            and receipt.get("outcome") == "ambiguous"
                        )
                        or operator_reconciled_core
                    )
                except (TypeError, ValueError):
                    unresolved = False
                if not unresolved:
                    raise ValueError("write recovery has no ambiguous media outcome")
            elif row["state"] != "in_progress":
                raise ValueError("write recovery state is invalid")

            existing = connection.execute(
                "SELECT 1 FROM write_recovery_clearances "
                "WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                raise ValueError("write recovery operation already cleared")
            connection.execute(
                """
                INSERT INTO write_recovery_clearances (
                    idempotency_key, request_sha256, observed_at,
                    verification_kind, evidence_reference
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key, request_sha256, timestamp,
                    verification_kind, evidence_reference.strip(),
                ),
            )
            connection.commit()

    def has_write_recovery_clearance(
        self, *, idempotency_key: str, request_sha256: str,
    ) -> bool:
        """Check an exact persisted operation clearance, never infer one."""
        if not isinstance(idempotency_key, str) or not idempotency_key:
            return False
        if (
            not isinstance(request_sha256, str)
            or len(request_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in request_sha256)
        ):
            return False
        with closing(self._connect()) as connection:
            return connection.execute(
                """
                SELECT 1 FROM write_recovery_clearances AS c
                JOIN write_api_requests AS w
                  ON w.idempotency_key = c.idempotency_key
                 AND w.request_sha256 = c.request_sha256
                WHERE c.idempotency_key = ?
                  AND c.request_sha256 = ?
                  AND w.execution_started_at IS NOT NULL
                LIMIT 1
                """,
                (idempotency_key, request_sha256),
            ).fetchone() is not None

    def claim_write_api_request(
        self,
        *,
        idempotency_key: str,
        request_sha256: str,
        requested_at: datetime,
        claim_owner: str,
        allow_abandoned_takeover: bool = False,
    ) -> WriteApiRequestClaim:
        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")
        if (
            len(request_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in request_sha256
            )
        ):
            raise ValueError("request_sha256 must be a lowercase SHA-256 digest")
        if (
            requested_at.tzinfo is None
            or requested_at.utcoffset() is None
        ):
            raise ValueError("requested_at must be timezone-aware")
        owner = _validated_write_api_claim_owner(claim_owner)
        if not isinstance(allow_abandoned_takeover, bool):
            raise TypeError("allow_abandoned_takeover must be bool")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._validate_store_schema(connection)
            row = connection.execute(
                """
                SELECT idempotency_key, request_sha256, state, requested_at,
                       claim_owner, execution_started_at, completed_at,
                       response_status, response_json
                FROM write_api_requests
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                record = self._write_api_request_record(row)
                can_take_over = (
                    allow_abandoned_takeover
                    and record.request_sha256 == request_sha256
                    and record.state == "in_progress"
                    and record.claim_owner is not None
                    and record.claim_owner != owner
                    and record.execution_started_at is None
                )
                if can_take_over:
                    updated = connection.execute(
                        """
                        UPDATE write_api_requests
                        SET claim_owner = ?
                        WHERE idempotency_key = ?
                          AND request_sha256 = ?
                          AND state = 'in_progress'
                          AND claim_owner = ?
                          AND execution_started_at IS NULL
                        """,
                        (
                            owner,
                            idempotency_key,
                            request_sha256,
                            record.claim_owner,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise RuntimeError(
                            "write API claim ownership changed unexpectedly"
                        )
                    row = connection.execute(
                        """
                        SELECT idempotency_key, request_sha256, state,
                               requested_at, claim_owner,
                               execution_started_at, completed_at,
                               response_status, response_json
                        FROM write_api_requests
                        WHERE idempotency_key = ?
                        """,
                        (idempotency_key,),
                    ).fetchone()
                    assert row is not None
                    return WriteApiRequestClaim(
                        created=True,
                        record=self._write_api_request_record(row),
                    )
                return WriteApiRequestClaim(
                    created=False,
                    record=record,
                )

            connection.execute(
                """
                INSERT INTO write_api_requests (
                    idempotency_key, request_sha256, state, requested_at,
                    claim_owner
                ) VALUES (?, ?, 'in_progress', ?, ?)
                """,
                (
                    idempotency_key,
                    request_sha256,
                    requested_at.isoformat(),
                    owner,
                ),
            )
            row = connection.execute(
                """
                SELECT idempotency_key, request_sha256, state, requested_at,
                       claim_owner, execution_started_at, completed_at,
                       response_status, response_json
                FROM write_api_requests
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            assert row is not None
            return WriteApiRequestClaim(
                created=True,
                record=self._write_api_request_record(row),
            )

    def begin_write_api_request(
        self,
        *,
        idempotency_key: str,
        request_sha256: str,
        claim_owner: str,
        execution_started_at: datetime,
        strict_owner_fence: bool = False,
    ) -> WriteApiRequestRecord:
        owner = _validated_write_api_claim_owner(claim_owner)
        if (
            execution_started_at.tzinfo is None
            or execution_started_at.utcoffset() is None
        ):
            raise ValueError("execution_started_at must be timezone-aware")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT idempotency_key, request_sha256, state, requested_at,
                       claim_owner, execution_started_at, completed_at,
                       response_status, response_json
                FROM write_api_requests
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise ValueError("write API request was not claimed")
            record = self._write_api_request_record(row)
            if record.request_sha256 != request_sha256:
                raise ValueError("write API request fingerprint mismatch")
            if record.state != "in_progress":
                raise ValueError("write API request is not in progress")
            if record.claim_owner != owner:
                raise ValueError("write API request is owned by another runtime")
            if record.execution_started_at is not None:
                raise ValueError("write API request execution already started")

            # The independent owner must not admit a second externally
            # effectful Begin while any earlier owner-constructed UNKNOWN is
            # still unresolved. This test and the Begin update share the same
            # BEGIN IMMEDIATE transaction, including with concurrent
            # quarantine and trusted operator-clearance commits.
            #
            # Only the canonical owner-fixed error envelope is a candidate:
            # unrelated valid HTTP 202 receipts (including an idempotency key
            # named write_outcome_unverified) must never be misclassified.
            if connection.execute(
                """
                SELECT 1 FROM write_api_requests AS prior
                LEFT JOIN write_recovery_clearances AS cleared
                  ON cleared.idempotency_key = prior.idempotency_key
                 AND cleared.request_sha256 = prior.request_sha256
                WHERE prior.idempotency_key != ?
                  AND prior.state = 'completed'
                  AND prior.response_status = 202
                  AND prior.execution_started_at IS NOT NULL
                  AND cleared.idempotency_key IS NULL
                  AND substr(
                    prior.response_json, 1,
                    length('{"error":"write_outcome_unverified",')
                  ) = '{"error":"write_outcome_unverified",'
                LIMIT 1
                """, (idempotency_key,),
            ).fetchone() is not None:
                raise ValueError("unresolved owner write quarantine blocks Begin")

            # The untrusted browser executor may crash or deliberately omit
            # quarantine_started after Begin. For the independent owner,
            # a still-in-progress earlier execution is already UNKNOWN.
            # Keep this opt-in: legacy SnapshotStore callers currently support
            # concurrent non-Create actions until the owner cutover is complete.
            # Both checks and the new Begin remain one IMMEDIATE transaction.
            if strict_owner_fence and connection.execute(
                """
                SELECT 1 FROM write_api_requests AS prior
                LEFT JOIN write_recovery_clearances AS cleared
                  ON cleared.idempotency_key = prior.idempotency_key
                 AND cleared.request_sha256 = prior.request_sha256
                WHERE prior.idempotency_key != ?
                  AND prior.state = 'in_progress'
                  AND prior.execution_started_at IS NOT NULL
                  AND cleared.idempotency_key IS NULL
                LIMIT 1
                """, (idempotency_key,),
            ).fetchone() is not None:
                raise ValueError("unresolved started owner write blocks Begin")

            updated = connection.execute(
                """
                UPDATE write_api_requests
                SET execution_started_at = ?
                WHERE idempotency_key = ?
                  AND request_sha256 = ?
                  AND state = 'in_progress'
                  AND claim_owner = ?
                  AND execution_started_at IS NULL
                """,
                (
                    execution_started_at.isoformat(),
                    idempotency_key,
                    request_sha256,
                    owner,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    "write API execution-start ownership changed unexpectedly"
                )
            row = connection.execute(
                """
                SELECT idempotency_key, request_sha256, state, requested_at,
                       claim_owner, execution_started_at, completed_at,
                       response_status, response_json
                FROM write_api_requests
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            assert row is not None
            return self._write_api_request_record(row)

    def cancel_unstarted_write_api_request(
        self,
        *,
        idempotency_key: str,
        request_sha256: str,
        claim_owner: str,
        cancelled_at: datetime,
    ) -> WriteApiRequestRecord:
        """Retire only an exact claim that never crossed the owner Begin gate.

        This is NOT external platform reconciliation, a write success or retry
        authorization. The owner constructs the entire immutable failure
        response; neither a browser executor nor an RPC client chooses its
        status or contents. Historical started operations remain untouched.
        """
        if (
            not isinstance(idempotency_key, str) or not idempotency_key
            or not isinstance(request_sha256, str)
            or len(request_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in request_sha256)
        ):
            raise ValueError("exact write claim identity required")
        owner = _validated_write_api_claim_owner(claim_owner)
        if (
            not isinstance(cancelled_at, datetime)
            or cancelled_at.tzinfo is None
            or cancelled_at.utcoffset() is None
        ):
            raise ValueError("cancelled_at must be timezone-aware")
        response_json = json.dumps(
            {
                "error": "write_claim_cancelled_before_begin",
                "idempotency_key": idempotency_key,
                "platform_retry_authorized": False,
            },
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._validate_store_schema(connection)
            row = connection.execute(
                """
                SELECT idempotency_key, request_sha256, state, requested_at,
                       claim_owner, execution_started_at, completed_at,
                       response_status, response_json
                FROM write_api_requests
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise ValueError("write API claim is absent")
            record = self._write_api_request_record(row)
            if (
                record.request_sha256 != request_sha256
                or record.claim_owner != owner
            ):
                raise ValueError("write API claim binding mismatch")
            if record.execution_started_at is not None:
                raise ValueError("begun write cannot be cancelled")
            if record.state == "completed":
                if (
                    record.response_status == 409
                    and record.response_json == response_json
                ):
                    return record
                raise ValueError("write API claim has another terminal outcome")
            if record.state != "in_progress":
                raise ValueError("write API claim state invalid")
            changed = connection.execute(
                """
                UPDATE write_api_requests
                SET state='completed', completed_at=?,
                    response_status=409, response_json=?
                WHERE idempotency_key=? AND request_sha256=?
                  AND claim_owner=? AND state='in_progress'
                  AND execution_started_at IS NULL
                """,
                (
                    cancelled_at.isoformat(), response_json, idempotency_key,
                    request_sha256, owner,
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("unstarted claim cancellation lost binding")
            row = connection.execute(
                """
                SELECT idempotency_key, request_sha256, state, requested_at,
                       claim_owner, execution_started_at, completed_at,
                       response_status, response_json
                FROM write_api_requests
                WHERE idempotency_key=?
                """,
                (idempotency_key,),
            ).fetchone()
            assert row is not None
            return self._write_api_request_record(row)

    @staticmethod
    def _unverified_write_api_response(key: str) -> str:
        return json.dumps({
            "error": "write_outcome_unverified",
            "idempotency_key": key,
            "platform_retry_authorized": False,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def quarantine_started_write_api_request(
        self, *, idempotency_key: str, request_sha256: str,
        claim_owner: str, quarantined_at: datetime,
    ) -> WriteApiRequestRecord:
        """Persist owner-constructed UNKNOWN only after a bound Begin.

        No executor-selectable response, success, clearance or retry right.
        A lost reply requires readback of the original persisted operation.
        """
        return self.complete_write_api_request(
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            claim_owner=claim_owner,
            response_status=202,
            response_json=self._unverified_write_api_response(idempotency_key),
            completed_at=quarantined_at,
        )

    def complete_write_api_request(
        self,
        *,
        idempotency_key: str,
        request_sha256: str,
        claim_owner: str,
        response_status: int,
        response_json: str,
        completed_at: datetime,
    ) -> WriteApiRequestRecord:
        if (
            isinstance(response_status, bool)
            or not isinstance(response_status, int)
            or not 100 <= response_status <= 599
        ):
            raise ValueError("response_status must be an HTTP status code")
        if not isinstance(response_json, str) or not response_json:
            raise ValueError("response_json must not be empty")
        if (
            completed_at.tzinfo is None
            or completed_at.utcoffset() is None
        ):
            raise ValueError("completed_at must be timezone-aware")
        owner = _validated_write_api_claim_owner(claim_owner)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT idempotency_key, request_sha256, state, requested_at,
                       claim_owner, execution_started_at, completed_at,
                       response_status, response_json
                FROM write_api_requests
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise ValueError("write API request was not claimed")
            record = self._write_api_request_record(row)
            if record.request_sha256 != request_sha256:
                raise ValueError("write API request fingerprint mismatch")
            if record.claim_owner != owner:
                raise ValueError("write API request is owned by another runtime")
            if record.state == "completed":
                if (
                    record.response_status == response_status
                    and record.response_json == response_json
                ):
                    return record
                raise ValueError("write API request is already completed")
            if record.state != "in_progress":
                raise ValueError("write API request has invalid state")
            if record.execution_started_at is None:
                raise ValueError("write API request execution has not started")

            updated = connection.execute(
                """
                UPDATE write_api_requests
                SET state = 'completed',
                    completed_at = ?,
                    response_status = ?,
                    response_json = ?
                WHERE idempotency_key = ?
                  AND request_sha256 = ?
                  AND state = 'in_progress'
                  AND claim_owner = ?
                  AND execution_started_at IS NOT NULL
                """,
                (
                    completed_at.isoformat(),
                    response_status,
                    response_json,
                    idempotency_key,
                    request_sha256,
                    owner,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    "write API completion ownership changed unexpectedly"
                )
            row = connection.execute(
                """
                SELECT idempotency_key, request_sha256, state, requested_at,
                       claim_owner, execution_started_at, completed_at,
                       response_status, response_json
                FROM write_api_requests
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            assert row is not None
            return self._write_api_request_record(row)

    @staticmethod
    def _required_dashboard_pending_key(value: object) -> str:
        if (
            type(value) is not str or not 1 <= len(value) <= 128
            or any(character.isspace() for character in value)
        ):
            raise sqlite3.DatabaseError("invalid dashboard pending key")
        return value

    @classmethod
    def _assert_native_dashboard_pending_keys(
        cls, connection: sqlite3.Connection,
    ) -> None:
        """Reject hidden native SQLite keys before any keyed read or ACK.

        All three identities have canonical BINARY unique autoindexes. Native
        NULL/numbers sort below TEXT, BLOB above. Their indexed extrema detect
        type-class corruption even if WHERE or a page cursor hides the row.
        The probes and following read/write share the same SQLite snapshot.
        """
        if not connection.in_transaction:
            connection.execute("BEGIN")
        for position, name in enumerate(
            ("scope", "resource_key", "idempotency_key"), start=1,
        ):
            index = f"sqlite_autoindex_dashboard_pending_writes_{position}"
            columns = [
                (row["name"], row["coll"], row["desc"])
                for row in connection.execute(f"PRAGMA index_xinfo({index})")
                if row["key"]
            ]
            if columns != [(name, "BINARY", 0)]:
                raise sqlite3.DatabaseError("invalid dashboard pending identity index")
            for direction in ("ASC", "DESC"):
                row = connection.execute(
                    f"SELECT {name} FROM dashboard_pending_writes "
                    f"INDEXED BY {index} ORDER BY {name} {direction} LIMIT 1"
                ).fetchone()
                if row is not None:
                    cls._required_dashboard_pending_key(row[name])

    @staticmethod
    def _dashboard_pending_write_record(
        row: sqlite3.Row,
    ) -> DashboardPendingWrite:
        scope = SnapshotStore._required_dashboard_pending_key(row["scope"])
        resource_key = SnapshotStore._required_dashboard_pending_key(
            row["resource_key"]
        )
        key = SnapshotStore._required_dashboard_pending_key(
            row["idempotency_key"]
        )
        method, path = row["method"], row["path"]
        payload, ad_id, acknowledged = (
            row["payload_json"], row["ad_id"], row["acknowledged"],
        )
        if type(method) is not str or method not in {"POST", "PATCH", "DELETE"}:
            raise sqlite3.DatabaseError("invalid stored dashboard pending method")
        if (
            type(path) is not str or not 1 <= len(path) <= 512
            or not path.startswith("/api/write/")
            or any(character.isspace() for character in path)
            or "?" in path or "#" in path
        ):
            raise sqlite3.DatabaseError("invalid stored dashboard pending path")
        if payload is not None:
            if type(payload) is not str or len(payload) > 64 * 1024:
                raise sqlite3.DatabaseError("invalid stored dashboard pending body")
            try:
                strict_json_object(payload.encode("utf-8"))
            except (UnicodeError, ValueError):
                raise sqlite3.DatabaseError(
                    "invalid stored dashboard pending body"
                ) from None
        if ad_id is not None and (
            type(ad_id) is not str or not 1 <= len(ad_id) <= 32
            or not ad_id.isascii() or not ad_id.isdigit()
        ):
            raise sqlite3.DatabaseError("invalid stored dashboard pending ad ID")
        if type(acknowledged) is not int or acknowledged not in (0, 1):
            raise sqlite3.DatabaseError("invalid stored dashboard pending ACK")
        return DashboardPendingWrite(
            scope=scope, resource_key=resource_key, idempotency_key=key,
            method=method, path=path, payload_json=payload, ad_id=ad_id,
            acknowledged=bool(acknowledged),
        )

    def dashboard_pending_writes(self) -> tuple[DashboardPendingWrite, ...]:
        with self._connect() as connection:
            self._assert_native_dashboard_pending_keys(connection)
            rows = connection.execute(
                """
                SELECT scope, resource_key, idempotency_key, method, path,
                       payload_json, ad_id, acknowledged
                FROM dashboard_pending_writes
                ORDER BY scope
                """
            ).fetchall()
        return tuple(
            self._dashboard_pending_write_record(row)
            for row in rows
        )

    def dashboard_pending_writes_page(
        self, *, after: str = "", limit: int = 16,
    ) -> tuple[tuple[DashboardPendingWrite, ...], bool]:
        """Bound pending-write reads without granting recovery-clearance authority.

        This is a read-only advisory view, not a witness or clearance.
        Concurrent insertions/deletions may affect later pages.
        """
        if (
            type(after) is not str or len(after) > 128
            or any(character.isspace() for character in after)
        ):
            raise ValueError("invalid pending-write cursor")
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("invalid pending-write page capacity")
        with self._connect() as connection:
            self._assert_native_dashboard_pending_keys(connection)
            rows = connection.execute(
                """
                SELECT scope, resource_key, idempotency_key, method, path,
                       payload_json, ad_id, acknowledged
                FROM dashboard_pending_writes
                WHERE scope > ?
                ORDER BY scope
                LIMIT ?
                """,
                (after, limit + 1),
            ).fetchall()
        return (
            tuple(self._dashboard_pending_write_record(row)
                  for row in rows[:limit]),
            len(rows) > limit,
        )

    def claim_dashboard_pending_write(
        self,
        *,
        scope: str,
        resource_key: str,
        idempotency_key: str,
        method: str,
        path: str,
        payload_json: str | None,
        ad_id: str | None,
    ) -> bool:
        for name, value, maximum in (
            ("scope", scope, 128),
            ("resource_key", resource_key, 128),
            ("idempotency_key", idempotency_key, 128),
            ("path", path, 512),
        ):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > maximum
                or any(character.isspace() for character in value)
            ):
                raise ValueError(f"{name} is invalid")
        if method not in {"POST", "PATCH", "DELETE"}:
            raise ValueError("method is invalid")
        if not path.startswith("/api/write/") or "?" in path or "#" in path:
            raise ValueError("path is invalid")
        if payload_json is not None:
            if type(payload_json) is not str or len(payload_json) > 64 * 1024:
                raise ValueError("payload_json is invalid")
            try:
                # Admit only payloads the recovery reader can independently
                # decode: duplicate members, nonfinite values, surrogate
                # codepoints and excess nesting cannot poison a durable fence.
                strict_json_object(payload_json.encode("utf-8"))
            except (UnicodeError, ValueError):
                raise ValueError("payload_json is invalid") from None
        if ad_id is not None and (
            not isinstance(ad_id, str)
            or not ad_id
            or len(ad_id) > 32
            or not ad_id.isascii()
            or not ad_id.isdigit()
        ):
            raise ValueError("ad_id is invalid")

        expected = DashboardPendingWrite(
            scope=scope,
            resource_key=resource_key,
            idempotency_key=idempotency_key,
            method=method,
            path=path,
            payload_json=payload_json,
            ad_id=ad_id,
            acknowledged=False,
        )
        with self.dashboard_pending_write_guard(), self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._validate_store_schema(connection)
            self._assert_native_dashboard_pending_keys(connection)
            rows = connection.execute(
                """
                SELECT scope, resource_key, idempotency_key, method, path,
                       payload_json, ad_id, acknowledged
                FROM dashboard_pending_writes
                WHERE scope = ?
                   OR resource_key = ?
                   OR idempotency_key = ?
                """,
                (scope, resource_key, idempotency_key),
            ).fetchall()
            if rows:
                records = [
                    self._dashboard_pending_write_record(row)
                    for row in rows
                ]
                if len(records) == 1 and records[0] == expected:
                    return False
                raise ValueError("dashboard pending write conflicts with existing recovery state")

            connection.execute(
                """
                INSERT INTO dashboard_pending_writes (
                    scope, resource_key, idempotency_key, method, path,
                    payload_json, ad_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope,
                    resource_key,
                    idempotency_key,
                    method,
                    path,
                    payload_json,
                    ad_id,
                ),
            )
        return True

    def acknowledge_dashboard_pending_write(
        self,
        *,
        scope: str,
        idempotency_key: str,
    ) -> str:
        if not isinstance(scope, str) or not scope:
            raise ValueError("scope is invalid")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_native_dashboard_pending_keys(connection)
            row = connection.execute(
                """
                SELECT scope, resource_key, idempotency_key, method, path,
                       payload_json, ad_id, acknowledged
                FROM dashboard_pending_writes
                WHERE scope = ?
                """,
                (scope,),
            ).fetchone()
            if row is None:
                return "missing"
            record = self._dashboard_pending_write_record(row)
            if record.idempotency_key != idempotency_key:
                raise ValueError(
                    "dashboard pending write conflicts with existing recovery state"
                )
            if not record.acknowledged:
                updated = connection.execute(
                    """
                    UPDATE dashboard_pending_writes
                    SET acknowledged = 1
                    WHERE scope = ?
                      AND idempotency_key = ?
                      AND acknowledged = 0
                    """,
                    (scope, idempotency_key),
                )
                if updated.rowcount != 1:
                    raise RuntimeError(
                        "dashboard pending write acknowledgement lost exact record"
                    )
                return "acknowledged"
            deleted = connection.execute(
                """
                DELETE FROM dashboard_pending_writes
                WHERE scope = ?
                  AND idempotency_key = ?
                  AND acknowledged = 1
                """,
                (scope, idempotency_key),
            )
            if deleted.rowcount != 1:
                raise RuntimeError(
                    "dashboard pending write finalization lost exact record"
                )
            return "finalized"

    def begin_sync_attempt(
        self, *, source: str, started_at: datetime,
        require_no_unresolved: bool = False,
    ) -> int:
        """Journal intent; the isolated owner also fences *older* open attempts.

        BEGIN IMMEDIATE makes the unresolved-attempt check and insertion one
        serialized SQLite transaction across owner generations/processes.
        Historical local import callers retain their existing journal contract.
        An open attempt is never silently settled or cleared by a new read.
        """
        if not isinstance(source, str) or not source.strip():
            raise ValueError("sync source must not be empty")
        if type(require_no_unresolved) is not bool:
            raise TypeError("sync recovery fence flag must be bool")
        started = _sync_time(started_at)
        with self._connect() as connection:
            if require_no_unresolved:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM sync_attempts "
                    "WHERE outcome='in_progress' LIMIT 1"
                ).fetchone() is not None:
                    raise RuntimeError("unresolved inventory sync requires reconciliation")
            record = connection.execute(
                "INSERT INTO sync_attempts (source, started_at, outcome) "
                "VALUES (?, ?, 'in_progress')",
                (source, started),
            )
            if record.lastrowid is None:
                raise sqlite3.DatabaseError("sync attempt insert failed")
            return int(record.lastrowid)

    @staticmethod
    def _finish_sync_attempt(
        connection: sqlite3.Connection,
        attempt_id: int,
        *,
        expected_source: str,
        completed_at: datetime,
        outcome: str,
        error_kind: str | None,
        snapshot_count: int | None,
    ) -> None:
        if type(attempt_id) is not int or attempt_id < 1:
            raise ValueError("sync attempt id is invalid")
        if not isinstance(expected_source, str) or not expected_source.strip():
            raise ValueError("sync source binding is invalid")
        if outcome == "failed":
            if error_kind not in _SYNC_ERROR_KINDS or snapshot_count is not None:
                raise ValueError("sync failure classification is invalid")
        elif outcome == "success_empty":
            if error_kind is not None or snapshot_count != 0:
                raise ValueError("empty sync result is invalid")
        elif outcome == "success_nonempty":
            if error_kind is not None or type(snapshot_count) is not int or snapshot_count < 1:
                raise ValueError("nonempty sync result is invalid")
        else:
            raise ValueError("terminal sync outcome is invalid")
        terminal = _sync_time(completed_at)
        original = connection.execute(
            "SELECT started_at FROM sync_attempts "
            "WHERE id=? AND source=? AND outcome='in_progress'",
            (attempt_id, expected_source),
        ).fetchone()
        if original is None:
            raise RuntimeError("sync attempt missing or already completed")
        if datetime.fromisoformat(terminal) < datetime.fromisoformat(original["started_at"]):
            raise ValueError("sync completion predates its start")
        updated = connection.execute(
            "UPDATE sync_attempts SET completed_at=?, outcome=?, "
            "error_kind=?, snapshot_count=? "
            "WHERE id=? AND source=? AND outcome='in_progress' AND completed_at IS NULL",
            (terminal, outcome, error_kind, snapshot_count, attempt_id, expected_source),
        )
        if updated.rowcount != 1:
            raise RuntimeError("sync attempt has been concurrently completed")

    def fail_sync_attempt(
        self, attempt_id: int, *, source: str, completed_at: datetime, error_kind: str,
    ) -> None:
        """Settle an open read failure without changing any owner snapshots."""
        with self._connect() as connection:
            self._finish_sync_attempt(
                connection, attempt_id, expected_source=source, completed_at=completed_at,
                outcome="failed", error_kind=error_kind, snapshot_count=None,
            )

    def latest_sync_attempt(self) -> SyncAttemptRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sync_attempts ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return _sync_attempt_from_row(row) if row is not None else None

    def last_successful_sync_attempt(self) -> SyncAttemptRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sync_attempts "
                "WHERE outcome IN ('success_empty', 'success_nonempty') "
                "ORDER BY completed_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return _sync_attempt_from_row(row) if row is not None else None

    def sync_status(self) -> dict[str, object]:
        """Display attempts independently of old metric/status observations."""
        with self._connect() as connection:
            connection.execute("BEGIN")
            latest = connection.execute(
                "SELECT * FROM sync_attempts ORDER BY id DESC LIMIT 1"
            ).fetchone()
            successful = connection.execute(
                "SELECT * FROM sync_attempts "
                "WHERE outcome IN ('success_empty', 'success_nonempty') "
                "ORDER BY completed_at DESC, id DESC LIMIT 1"
            ).fetchone()
            # Do not claim a newer success if any older open recovery
            # row is malformed, even when the oldest such row is valid.
            _unresolved_sync_attempt_summary(connection)
        latest_item = _sync_attempt_from_row(latest) if latest is not None else None
        success_item = (
            _sync_attempt_from_row(successful) if successful is not None else None
        )
        return {
            "state": latest_item.outcome if latest_item is not None else "never_attempted",
            "latest_attempt": _sync_attempt_to_dict(latest_item),
            "last_successful_attempt": _sync_attempt_to_dict(success_item),
        }

    def sync_recovery_status(self) -> dict[str, object]:
        """Read every unresolved sync generation, not only the latest start.

        A completed newer attempt cannot conceal an older in_progress journal
        row. Count and oldest record come from one SQLite read transaction.
        This method never settles, replays, authorizes or clears an attempt.
        """
        with self._connect() as connection:
            connection.execute("BEGIN")
            count, oldest = _unresolved_sync_attempt_summary(connection)
        return {
            "unresolved_count": count,
            "oldest_unresolved_attempt": _sync_attempt_to_dict(oldest),
        }

    def append_inventory_result(
        self,
        result: ReadResult[tuple[AdSnapshot, ...]],
        *,
        tracked_ad_ids: Iterable[str] = (),
        observed_at: datetime,
        source: str,
        attempt_id: int | None = None,
        completed_at: datetime | None = None,
    ) -> int:
        """Persist owner inventory and an optional journal outcome atomically.

        Failed reads append no snapshots. Successful reads carry forward ABSENT
        for tracked IDs not observed. Direct snapshot imports with no attempt id
        make no claim about an independently completed owner sync.
        """
        if not isinstance(result, ReadResult):
            raise TypeError("inventory result must be ReadResult")
        if (attempt_id is None) != (completed_at is None):
            raise ValueError("sync attempt and completion time must be paired")
        if not result.is_success:
            if attempt_id is not None:
                with self._connect() as connection:
                    self._finish_sync_attempt(
                        connection, attempt_id, expected_source=source, completed_at=completed_at,
                        outcome="failed", error_kind=result.status.value,
                        snapshot_count=None,
                    )
            return 0

        snapshots = tuple(result.value or ())
        if attempt_id is not None and (
            (result.status is ReadStatus.SUCCESS_EMPTY and snapshots)
            or (result.status is ReadStatus.SUCCESS_NONEMPTY and not snapshots)
            or any(not isinstance(item, AdSnapshot) for item in snapshots)
            or len({item.ad_id for item in snapshots}) != len(snapshots)
        ):
            raise ValueError("inconsistent owner inventory")
        present_ids = {snapshot.ad_id for snapshot in snapshots}
        # Never turn an untrusted BLOB/whitespace/NUL ID into owner evidence.
        # Validate before any possible SQLite journal or snapshot mutation.
        tracked_ids: set[str] = set()
        for ad_id in tracked_ad_ids:
            if (
                type(ad_id) is not str or not ad_id.strip()
                or ad_id != ad_id.strip() or "\x00" in ad_id
            ):
                raise sqlite3.DatabaseError("invalid tracked owner ID")
            tracked_ids.add(ad_id)

        with self._connect() as connection:
            # Bind evidence validation and history writes to the same
            # serialized transaction. A local catalog is never owner proof.
            connection.execute("BEGIN IMMEDIATE")
            owned_ids: set[str] = set()
            if tracked_ids:
                for row in connection.execute(
                    "SELECT DISTINCT ad_id FROM ad_snapshots "
                    "WHERE source != ? AND lifecycle_state != ?",
                    (LOCAL_CATALOG_SOURCE, LifecycleState.ABSENT.value),
                ):
                    owned_id = row["ad_id"]
                    if (
                        type(owned_id) is not str or not owned_id.strip()
                        or owned_id != owned_id.strip() or "\x00" in owned_id
                    ):
                        raise sqlite3.DatabaseError("invalid stored owner ID")
                    owned_ids.add(owned_id)
            absent_ids = sorted((tracked_ids & owned_ids) - present_ids)
            for snapshot in snapshots:
                self._insert_ad_snapshot(connection, snapshot)
            for ad_id in absent_ids:
                self._insert_ad_snapshot(
                    connection,
                    AdSnapshot(
                        ad_id=ad_id,
                        observed_at=observed_at,
                        source=source,
                        lifecycle_state=LifecycleState.ABSENT,
                    ),
                )
            if attempt_id is not None:
                self._finish_sync_attempt(
                    connection, attempt_id, expected_source=source, completed_at=completed_at,
                    outcome="success_nonempty" if snapshots else "success_empty",
                    error_kind=None, snapshot_count=len(snapshots),
                )
        return len(snapshots) + len(absent_ids)

    @staticmethod
    def _decode_ad_history(
        rows: Iterable[sqlite3.Row],
    ) -> tuple[AdSnapshot, ...]:
        """Order actual instants, then insertion IDs; preserve field evidence."""

        try:
            parsed = []
            for row in rows:
                # SQLite TEXT affinity still admits native BLOB values.
                # AdSnapshot accepts bytes for source/title/description, but
                # JSON serialization would abort the HTTP connection.
                for field in ("ad_id", "observed_at", "source", "lifecycle_state"):
                    if type(row[field]) is not str:
                        raise sqlite3.DatabaseError("invalid ad snapshot data")
                SnapshotStore._required_observation_ad_id(row["ad_id"])
                for field in ("metric_source", "title", "description"):
                    value = row[field]
                    if value is not None and type(value) is not str:
                        raise sqlite3.DatabaseError("invalid ad snapshot data")
                parsed.append(
                    (datetime.fromisoformat(row["observed_at"]), SnapshotStore._history_row_id(row["id"]), row)
                )
            parsed.sort(key=lambda item: (item[0], item[1]))
            return tuple(
                AdSnapshot(
                    ad_id=row["ad_id"],
                    observed_at=observed_at,
                    source=row["source"],
                    metric_source=row["metric_source"],
                    lifecycle_state=LifecycleState(row["lifecycle_state"]),
                    title=row["title"],
                    description=row["description"],
                    views=row["views"],
                    watch_count=row["watch_count"],
                    reply_count=row["reply_count"],
                )
                for observed_at, _, row in parsed
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise sqlite3.DatabaseError("invalid ad snapshot data") from exc
    def ad_history_page(
        self, ad_id: str, *, after_id: int, watermark: int | None,
        limit: int = 16,
    ) -> tuple[int, tuple[tuple[int, AdSnapshot], ...], bool]:
        """Read one bounded append-only observation page in insertion order.

        The high-water mark is pinned on the first page in the same SQLite
        read transaction. Later appends cannot change this logical snapshot;
        the consumer sorts the pinned rows by (actual instant, insertion ID).
        No caller-supplied SQL, table name or unlimited LIMIT is accepted.
        """
        if not isinstance(ad_id, str) or not ad_id.strip():
            raise ValueError("ad history ID is invalid")
        if (type(after_id) is not int or not 0 <= after_id <= 2**63 - 1
                or type(limit) is not int or not 1 <= limit <= 32
                or (watermark is not None and (
                    type(watermark) is not int
                    or not 0 <= watermark <= 2**63 - 1
                    or watermark < after_id
                ))):
            raise ValueError("ad history page cursor is invalid")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            if watermark is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS bound "
                    "FROM ad_snapshots WHERE ad_id = ?", (ad_id,),
                ).fetchone()
                assert row is not None
                watermark = int(row["bound"])
            rows = conn.execute(
                """SELECT id, ad_id, observed_at, source, lifecycle_state,
                          title, description, views, watch_count, reply_count,
                          metric_source
                   FROM ad_snapshots
                   WHERE ad_id = ? AND id > ? AND id <= ?
                   ORDER BY id ASC LIMIT ?""",
                (ad_id, after_id, watermark, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        page = tuple(
            (
                int(row["id"]),
                AdSnapshot(
                    ad_id=row["ad_id"],
                    observed_at=datetime.fromisoformat(row["observed_at"]),
                    source=row["source"], metric_source=row["metric_source"],
                    lifecycle_state=LifecycleState(row["lifecycle_state"]),
                    title=row["title"], description=row["description"],
                    views=row["views"], watch_count=row["watch_count"],
                    reply_count=row["reply_count"],
                ),
            )
            for row in rows[:limit]
        )
        return watermark, page, more

    def ad_history(self, ad_id: str) -> tuple[AdSnapshot, ...]:
        """Return observations by actual instant, then insertion ID for ties."""

        with self._connect() as connection:
            self._assert_native_history_ad_ids(connection, "ad_snapshots")
            rows = connection.execute(
                """
                SELECT id, ad_id, observed_at, source, lifecycle_state, title,
                       description, views, watch_count, reply_count, metric_source
                FROM ad_snapshots
                WHERE ad_id = ?
                """,
                (ad_id,),
            ).fetchall()
        return self._decode_ad_history(rows)

    def ad_histories(self) -> Iterator[tuple[str, tuple[AdSnapshot, ...]]]:
        """Page a high-water-mark snapshot without holding reader locks on yield.

        IDs are AUTOINCREMENT and snapshots append-only: the captured maximum
        ID excludes later inserts from every page. Each page is fully fetched
        and its connection CLOSED before CPU-heavy decoding or yielding.
        Unlike a live SELECT iterator, this cannot hold a rollback-journal
        shared lock while a slow dashboard request blocks concurrent writes.

        Keyset pagination uses the existing (ad_id, observed_at, id) index
        without temp sorting. The largest single history plus one fixed page
        bounds memory; actual UTC instants are sorted within every group.
        Arbitrary external updates/deletes are outside the append-only model.
        A replacement DB inode or changed schema version while the generator
        is paused is *not* an append: fail closed rather than return a
        partial/cross-generation analytics view.
        """
        def _file_identity() -> tuple[int, int]:
            try:
                info = self._identity_path.stat()
            except OSError:
                raise sqlite3.DatabaseError(
                    "history database identity unavailable"
                ) from None
            return info.st_dev, info.st_ino

        expected_generation: tuple[int, int, int] | None = None
        high_water_id: int | None = None
        last_key: tuple[str, str, int] | None = None
        pending_ad_id: str | None = None
        pending_rows: list[sqlite3.Row] = []
        while True:
            # Inspect before connect and again before closing each page read:
            # an atomic file swap cannot silently splice histories from two
            # different SQLite files even if both use identical schema DDL.
            file_identity = _file_identity()
            with closing(self._connect()) as connection:
                connection.execute("PRAGMA query_only=ON")
                # Schema validation and keyset lookup share one read snapshot.
                connection.execute("BEGIN")
                self._assert_binary_history_key_schema(connection, "ad_snapshots")
                # Global indexed certificate before the first generator yield:
                # a corrupt key on a later page must not expose early groups.
                self._assert_blank_history_key_index(connection, "ad_snapshots")
                self._assert_no_blank_history_keys(connection, "ad_snapshots")
                # Validate native key storage classes globally within this
                # page's read snapshot: BLOB/numeric rows can be on later
                # keyset pages and must not leak early valid history groups.
                for direction in ("ASC", "DESC"):
                    boundary = connection.execute(
                        f"SELECT ad_id FROM ad_snapshots "
                        f"ORDER BY ad_id {direction} LIMIT 1"
                    ).fetchone()
                    if boundary is not None and (
                        type(boundary["ad_id"]) is not str
                        or not boundary["ad_id"].strip()
                    ):
                        raise sqlite3.DatabaseError(
                            "invalid ad snapshot key storage class"
                        )
                version_row = connection.execute(
                    "PRAGMA schema_version"
                ).fetchone()
                if (
                    version_row is None
                    or type(version_row[0]) is not int
                    or version_row[0] < 0
                ):
                    raise sqlite3.DatabaseError(
                        "invalid history schema generation"
                    )
                generation = (*file_identity, version_row[0])
                if expected_generation is None:
                    expected_generation = generation
                elif generation != expected_generation:
                    raise sqlite3.DatabaseError(
                        "history schema generation changed during pagination"
                    )
                if high_water_id is None:
                    maximum = connection.execute(
                        "SELECT MAX(id) AS max_id FROM ad_snapshots"
                    ).fetchone()
                    if maximum is None or maximum["max_id"] is None:
                        return
                    high_water_id = int(maximum["max_id"])
                if last_key is None:
                    page = connection.execute(
                        """
                        SELECT id, ad_id, observed_at, source, lifecycle_state,
                               title, description, views, watch_count,
                               reply_count, metric_source
                        FROM ad_snapshots
                        WHERE id <= ?
                        ORDER BY ad_id ASC, observed_at ASC, id ASC
                        LIMIT ?
                        """,
                        (high_water_id, _AD_HISTORY_PAGE_ROWS),
                    ).fetchall()
                else:
                    page = connection.execute(
                        """
                        SELECT id, ad_id, observed_at, source, lifecycle_state,
                               title, description, views, watch_count,
                               reply_count, metric_source
                        FROM ad_snapshots
                        WHERE id <= ?
                          AND (ad_id, observed_at, id) > (?, ?, ?)
                        ORDER BY ad_id ASC, observed_at ASC, id ASC
                        LIMIT ?
                        """,
                        (high_water_id, *last_key, _AD_HISTORY_PAGE_ROWS),
                    ).fetchall()
                if _file_identity() != file_identity:
                    raise sqlite3.DatabaseError(
                        "history database identity changed during pagination"
                    )

            # Every yield below runs AFTER SQLite has released the read lock.
            if not page:
                break
            # SQLite uses dynamic value types even in TEXT columns. A BLOB
            # key must not be stringified into a TEXT keyset cursor: SQLite
            # would compare the changed storage class and repeat this page.
            # Validate the whole page before yielding any of its groups.
            for row in page:
                if (
                    type(row["ad_id"]) is not str
                    or not row["ad_id"].strip()
                    or type(row["observed_at"]) is not str
                    or type(row["id"]) is not int
                    or row["id"] < 1
                ):
                    raise sqlite3.DatabaseError("invalid ad snapshot key storage class")
            for row in page:
                ad_id = row["ad_id"]
                if pending_ad_id is None:
                    pending_ad_id = ad_id
                elif ad_id != pending_ad_id:
                    yield pending_ad_id, self._decode_ad_history(pending_rows)
                    pending_rows = []
                    pending_ad_id = ad_id
                pending_rows.append(row)
            tail = page[-1]
            last_key = (tail["ad_id"], tail["observed_at"], tail["id"])
            if len(page) < _AD_HISTORY_PAGE_ROWS:
                break

        if pending_ad_id is not None:
            yield pending_ad_id, self._decode_ad_history(pending_rows)

    def latest_ad_snapshot(self, ad_id: str) -> AdSnapshot | None:
        history = self.ad_history(ad_id)
        return history[-1] if history else None

    def tracked_ad_ids(self) -> tuple[str, ...]:
        """Return every ad ID ever observed by this store."""
        with self._connect() as connection:
            self._assert_native_history_ad_ids(connection, "ad_snapshots")
            rows = connection.execute(
                """
                SELECT DISTINCT ad_id
                FROM ad_snapshots
                ORDER BY ad_id ASC
                """
            ).fetchall()
        return tuple(self._required_observation_ad_id(row["ad_id"]) for row in rows)


    @classmethod
    def _assert_native_history_ad_ids(
        cls, connection: sqlite3.Connection, table: str,
    ) -> None:
        """Fail closed on rows that a typed WHERE ad_id=? could silently hide.

        SQLite sorts NULL/numeric values before native TEXT and BLOB values
        after TEXT. Both extrema prove native TEXT. A verified partial
        index additionally finds blank TEXT anywhere among valid ad IDs.
        Never coerce a native BLOB into a synthetic ad ID.
        """
        if table not in (
            "ad_snapshots", "reaction_snapshots",
            "inbound_message_events", "ad_classifications",
        ):
            raise ValueError("unsupported historical ad ID table")
        # sqlite3 SELECT alone does not pin a transaction across statements.
        # Bind both validation probes and the following keyed SELECT to the
        # same read snapshot, even while a WAL writer commits concurrently.
        connection.execute("BEGIN")
        cls._assert_binary_history_key_schema(connection, table)
        cls._assert_blank_history_key_index(connection, table)
        cls._assert_no_blank_history_keys(connection, table)
        for direction in ("ASC", "DESC"):
            row = connection.execute(
                f"SELECT ad_id FROM {table} ORDER BY ad_id {direction} LIMIT 1"
            ).fetchone()
            if row is not None:
                cls._required_observation_ad_id(row["ad_id"])

    @staticmethod
    def _required_observation_ad_id(value: object) -> str:
        if type(value) is not str or not value.strip():
            raise sqlite3.DatabaseError("invalid history ad ID")
        return value

    @staticmethod
    def _required_observation_text(value: object) -> str:
        # SQLite TEXT affinity does not prohibit BLOB/INTEGER/FLOAT storage.
        if type(value) is not str:
            raise sqlite3.DatabaseError("invalid observation text")
        return value

    @staticmethod
    def _optional_observation_text(value: object) -> str | None:
        if value is None:
            return None
        return SnapshotStore._required_observation_text(value)

    @staticmethod
    def _observed_history_time(value: object) -> datetime:
        text = SnapshotStore._required_observation_text(value)
        try:
            return datetime.fromisoformat(text)
        except (ValueError, TypeError, OverflowError):
            raise sqlite3.DatabaseError("invalid observation timestamp") from None

    @staticmethod
    def _history_row_id(value: object) -> int:
        if type(value) is not int or value <= 0:
            raise sqlite3.DatabaseError("invalid observation row id")
        return value

    @staticmethod
    def _history_nonnegative_count(value: object) -> int:
        if type(value) is not int or value < 0:
            raise sqlite3.DatabaseError("invalid observation counter")
        return value

    def reaction_history_page(
        self, ad_id: str, *, after_id: int, watermark: int | None,
        limit: int = 16,
    ) -> tuple[int, tuple[tuple[int, ReactionSnapshot], ...], bool]:
        """Read one bounded immutable-generation reaction page in insertion order.

        The first page pins MAX(id) inside its read transaction. Later appends
        cannot enter this generation; no caller-supplied SQL is evaluated.
        """
        if not isinstance(ad_id, str) or not ad_id.strip():
            raise ValueError("reaction history ID is invalid")
        if (type(after_id) is not int or not 0 <= after_id <= 2**63 - 1
                or type(limit) is not int or not 1 <= limit <= 32
                or (watermark is not None and (
                    type(watermark) is not int
                    or not 0 <= watermark <= 2**63 - 1
                    or watermark < after_id
                ))):
            raise ValueError("reaction history page cursor is invalid")
        with closing(self._connect()) as conn:
            self._assert_native_history_ad_ids(conn, "reaction_snapshots")
            if watermark is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS bound "
                    "FROM reaction_snapshots WHERE ad_id = ?", (ad_id,),
                ).fetchone()
                assert row is not None
                watermark = int(row["bound"])
            rows = conn.execute(
                """SELECT id, ad_id, observed_at, source,
                          conversation_count, unique_buyer_count,
                          inbound_message_count
                   FROM reaction_snapshots
                   WHERE ad_id = ? AND id > ? AND id <= ?
                   ORDER BY id ASC LIMIT ?""",
                (ad_id, after_id, watermark, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        try:
            page = tuple((
                self._history_row_id(row["id"]),
                ReactionSnapshot(
                    ad_id=self._required_observation_ad_id(row["ad_id"]),
                    observed_at=self._observed_history_time(row["observed_at"]),
                    source=self._required_observation_text(row["source"]),
                    conversation_count=self._history_nonnegative_count(
                        row["conversation_count"]
                    ),
                    unique_buyer_count=self._history_nonnegative_count(
                        row["unique_buyer_count"]
                    ),
                    inbound_message_count=self._history_nonnegative_count(
                        row["inbound_message_count"]
                    ),
                ),
            ) for row in rows[:limit])
        except (TypeError, ValueError, OverflowError):
            raise sqlite3.DatabaseError("invalid reaction history page") from None
        return watermark, page, more

    def reaction_history(self, ad_id: str) -> tuple[ReactionSnapshot, ...]:
        with self._connect() as connection:
            self._assert_native_history_ad_ids(connection, "reaction_snapshots")
            rows = connection.execute(
                """
                SELECT ad_id, observed_at, source, conversation_count,
                       unique_buyer_count, inbound_message_count
                FROM reaction_snapshots
                WHERE ad_id = ?
                ORDER BY id ASC
                """,
                (ad_id,),
            ).fetchall()

        try:
            return tuple(
                ReactionSnapshot(
                    ad_id=self._required_observation_ad_id(row["ad_id"]),
                    observed_at=self._observed_history_time(row["observed_at"]),
                    source=self._required_observation_text(row["source"]),
                    conversation_count=self._history_nonnegative_count(
                        row["conversation_count"]
                    ),
                    unique_buyer_count=self._history_nonnegative_count(
                        row["unique_buyer_count"]
                    ),
                    inbound_message_count=self._history_nonnegative_count(
                        row["inbound_message_count"]
                    ),
                )
                for row in rows
            )
        except (ValueError, TypeError, OverflowError):
            raise sqlite3.DatabaseError("invalid reaction history") from None

    def inbound_message_page(
        self, ad_id: str, *, after_id: int, watermark: int | None,
        limit: int = 16,
    ) -> tuple[int, tuple[tuple[int, InboundMessageEvent], ...], bool]:
        """Bounded immutable-generation inbox read ordered by insertion id."""
        if not isinstance(ad_id, str) or not ad_id.strip() or len(ad_id) > 160:
            raise ValueError("inbound history ID is invalid")
        if (type(after_id) is not int or not 0 <= after_id <= 2**63 - 1
                or type(limit) is not int or not 1 <= limit <= 32
                or (watermark is not None and (
                    type(watermark) is not int
                    or not 0 <= watermark <= 2**63 - 1
                    or watermark < after_id
                ))):
            raise ValueError("inbound history page cursor is invalid")
        with closing(self._connect()) as connection:
            self._assert_native_history_ad_ids(connection, "inbound_message_events")
            if watermark is None:
                row = connection.execute(
                    "SELECT COALESCE(MAX(id), 0) AS bound "
                    "FROM inbound_message_events WHERE ad_id = ?", (ad_id,),
                ).fetchone()
                assert row is not None
                watermark = int(row["bound"])
            rows = connection.execute(
                """SELECT id, ad_id, conversation_id, provider_message_id,
                          observed_at, source
                   FROM inbound_message_events
                   WHERE ad_id = ? AND id > ? AND id <= ?
                   ORDER BY id ASC LIMIT ?""",
                (ad_id, after_id, watermark, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        try:
            page = tuple((
                self._history_row_id(row["id"]),
                InboundMessageEvent(
                    ad_id=self._required_observation_ad_id(row["ad_id"]),
                    conversation_id=self._required_observation_text(
                        row["conversation_id"]
                    ),
                    provider_message_id=self._required_observation_text(
                        row["provider_message_id"]
                    ),
                    observed_at=self._observed_history_time(row["observed_at"]),
                    source=self._required_observation_text(row["source"]),
                ),
            ) for row in rows[:limit])
        except (TypeError, ValueError, OverflowError):
            raise sqlite3.DatabaseError("invalid inbound history page") from None
        return watermark, page, more

    def inbound_message_ad_ids_page(
        self, *, after: str, watermark: int | None,
        limit: int = 64,
    ) -> tuple[int, tuple[str, ...], bool]:
        """List imported email-only and tracked IDs with a pinned generation."""
        if (type(after) is not str or len(after) > 160
                or (after and not after.strip())
                or type(limit) is not int or not 1 <= limit <= 64
                or (watermark is not None and (
                    type(watermark) is not int
                    or not 0 <= watermark <= 2**63 - 1
                ))):
            raise ValueError("inbound ad-ID page cursor is invalid")
        with closing(self._connect()) as connection:
            self._assert_native_history_ad_ids(connection, "inbound_message_events")
            if watermark is None:
                row = connection.execute(
                    "SELECT COALESCE(MAX(id), 0) AS bound FROM inbound_message_events",
                ).fetchone()
                assert row is not None
                watermark = int(row["bound"])
            rows = connection.execute(
                """SELECT DISTINCT ad_id
                   FROM inbound_message_events
                   WHERE id <= ? AND ad_id > ?
                   ORDER BY ad_id ASC LIMIT ?""",
                (watermark, after, limit + 1),
            ).fetchall()
        return watermark, tuple(
            self._required_observation_ad_id(row["ad_id"])
            for row in rows[:limit]
        ), len(rows) > limit

    def inbound_message_history(
        self,
        ad_id: str,
    ) -> tuple[InboundMessageEvent, ...]:
        with self._connect() as connection:
            self._assert_native_history_ad_ids(connection, "inbound_message_events")
            rows = connection.execute(
                """
                SELECT id, ad_id, conversation_id, provider_message_id,
                       observed_at, source
                FROM inbound_message_events
                WHERE ad_id = ?
                """,
                (ad_id,),
            ).fetchall()

        try:
            ordered_rows = sorted(
                (
                    (
                        self._observed_history_time(row["observed_at"]),
                        self._history_row_id(row["id"]),
                        row,
                    )
                    for row in rows
                ),
                key=lambda item: (item[0], item[1]),
            )
            return tuple(
                InboundMessageEvent(
                    ad_id=self._required_observation_ad_id(row["ad_id"]),
                    conversation_id=self._required_observation_text(
                        row["conversation_id"]
                    ),
                    provider_message_id=self._required_observation_text(
                        row["provider_message_id"]
                    ),
                    observed_at=observed_at,
                    source=self._required_observation_text(row["source"]),
                )
                for observed_at, _, row in ordered_rows
            )
        except (ValueError, TypeError, OverflowError):
            raise sqlite3.DatabaseError("invalid inbound message history") from None

    def inbound_message_ad_ids(self) -> tuple[str, ...]:
        """Return every ad ID represented by imported inbound-message events."""

        with self._connect() as connection:
            self._assert_native_history_ad_ids(connection, "inbound_message_events")
            rows = connection.execute(
                """
                SELECT DISTINCT ad_id
                FROM inbound_message_events
                ORDER BY ad_id ASC
                """
            ).fetchall()
        return tuple(self._required_observation_ad_id(row["ad_id"]) for row in rows)

    def inbound_message_counts(self, ad_id: str) -> tuple[int, int]:
        """Return (conversation_count, inbound_message_count) for one ad."""

        with self._connect() as connection:
            self._assert_native_history_ad_ids(connection, "inbound_message_events")
            row = connection.execute(
                """
                SELECT COUNT(DISTINCT conversation_id) AS conversations,
                       COUNT(*) AS messages
                FROM inbound_message_events
                WHERE ad_id = ?
                """,
                (ad_id,),
            ).fetchone()
        assert row is not None
        return int(row["conversations"]), int(row["messages"])

    def classification_history(self, ad_id: str) -> tuple[AdClassification, ...]:
        with self._connect() as connection:
            self._assert_native_history_ad_ids(connection, "ad_classifications")
            rows = connection.execute(
                """
                SELECT id, ad_id, observed_at, source, image_type, city,
                       text_type, title_type
                FROM ad_classifications
                WHERE ad_id = ?
                ORDER BY id ASC
                """,
                (ad_id,),
            ).fetchall()

        try:
            ordered_rows = sorted(
                (
                    (
                        self._observed_history_time(row["observed_at"]),
                        self._history_row_id(row["id"]),
                        row,
                    )
                    for row in rows
                ),
                key=lambda item: (item[0], item[1]),
            )
            return tuple(
                AdClassification(
                    ad_id=self._required_observation_ad_id(row["ad_id"]),
                    observed_at=observed_at,
                    source=self._required_observation_text(row["source"]),
                    image_type=self._optional_observation_text(row["image_type"]),
                    city=self._optional_observation_text(row["city"]),
                    text_type=self._optional_observation_text(row["text_type"]),
                    title_type=self._optional_observation_text(row["title_type"]),
                )
                for observed_at, _, row in ordered_rows
            )
        except (ValueError, TypeError, OverflowError):
            raise sqlite3.DatabaseError("invalid classification history") from None

    def latest_classification_bounded(
        self, ad_id: str, *, max_records: int = 200000,
    ) -> AdClassification | None:
        """Select latest real observation instant in bounded constant memory.

        This yields the same (observed_at, insertion-ID) winner as the existing
        classification_history sort without materializing arbitrary history.
        """
        if (type(ad_id) is not str or not ad_id.strip() or len(ad_id) > 160
                or type(max_records) is not int or not 1 <= max_records <= 200000):
            raise ValueError("classification lookup bound is invalid")
        selected: sqlite3.Row | None = None
        selected_key: tuple[datetime, int] | None = None
        with closing(self._connect()) as connection:
            self._assert_native_history_ad_ids(connection, "ad_classifications")
            cursor = connection.execute(
                """SELECT id, ad_id, observed_at, source, image_type, city,
                          text_type, title_type
                   FROM ad_classifications WHERE ad_id = ?
                   ORDER BY id ASC""", (ad_id,),
            )
            for count, row in enumerate(cursor, start=1):
                if count > max_records:
                    raise ValueError("classification history capacity exceeded")
                try:
                    when = self._observed_history_time(row["observed_at"])
                    key = when, self._history_row_id(row["id"])
                    if selected_key is None or key > selected_key:
                        selected, selected_key = row, key
                except (TypeError, ValueError, OverflowError):
                    raise sqlite3.DatabaseError("invalid classification history") from None
        if selected is None:
            return None
        try:
            return AdClassification(
                ad_id=self._required_observation_ad_id(selected["ad_id"]),
                observed_at=self._observed_history_time(selected["observed_at"]),
                source=self._required_observation_text(selected["source"]),
                image_type=self._optional_observation_text(selected["image_type"]),
                city=self._optional_observation_text(selected["city"]),
                text_type=self._optional_observation_text(selected["text_type"]),
                title_type=self._optional_observation_text(selected["title_type"]),
            )
        except (TypeError, ValueError, OverflowError):
            raise sqlite3.DatabaseError("invalid classification history") from None

    def latest_classification(self, ad_id: str) -> AdClassification | None:
        history = self.classification_history(ad_id)
        return history[-1] if history else None
