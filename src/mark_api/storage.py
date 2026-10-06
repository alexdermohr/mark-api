from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

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


_CLASSIFICATION_FIELDS = (
    "image_type",
    "city",
    "text_type",
    "title_type",
)


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


class SnapshotStore:
    """Append-only SQLite storage for normalized observations and write receipts."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

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
                    reply_count INTEGER
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
                """
            )
            connection.execute("BEGIN IMMEDIATE")
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

    @staticmethod
    def _insert_ad_snapshot(
        connection: sqlite3.Connection,
        snapshot: AdSnapshot,
    ) -> None:
        connection.execute(
            """
            INSERT INTO ad_snapshots (
                ad_id, observed_at, source, lifecycle_state, title, description,
                views, watch_count, reply_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    def _dashboard_pending_write_record(
        row: sqlite3.Row,
    ) -> DashboardPendingWrite:
        return DashboardPendingWrite(
            scope=str(row["scope"]),
            resource_key=str(row["resource_key"]),
            idempotency_key=str(row["idempotency_key"]),
            method=str(row["method"]),
            path=str(row["path"]),
            payload_json=(
                str(row["payload_json"])
                if row["payload_json"] is not None
                else None
            ),
            ad_id=(
                str(row["ad_id"])
                if row["ad_id"] is not None
                else None
            ),
            acknowledged=bool(row["acknowledged"]),
        )

    def dashboard_pending_writes(self) -> tuple[DashboardPendingWrite, ...]:
        with self._connect() as connection:
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
            if not isinstance(payload_json, str) or len(payload_json) > 64 * 1024:
                raise ValueError("payload_json is invalid")
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError as exc:
                raise ValueError("payload_json is invalid") from exc
            if not isinstance(payload, dict):
                raise ValueError("payload_json is invalid")
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
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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
            row = connection.execute(
                """
                SELECT idempotency_key, acknowledged
                FROM dashboard_pending_writes
                WHERE scope = ?
                """,
                (scope,),
            ).fetchone()
            if row is None:
                return "missing"
            if str(row["idempotency_key"]) != idempotency_key:
                raise ValueError(
                    "dashboard pending write conflicts with existing recovery state"
                )
            if not bool(row["acknowledged"]):
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

    def append_inventory_result(
        self,
        result: ReadResult[tuple[AdSnapshot, ...]],
        *,
        tracked_ad_ids: Iterable[str] = (),
        observed_at: datetime,
        source: str,
    ) -> int:
        """Persist a complete owner-inventory observation without erasing history.

        Failed reads create no snapshots. A successful owner-inventory read records
        returned ads and appends ABSENT snapshots for tracked IDs not present in the
        result. Existing rows are never deleted.
        """

        if not result.is_success:
            return 0

        snapshots = tuple(result.value or ())
        present_ids = {snapshot.ad_id for snapshot in snapshots}
        tracked_ids = {ad_id for ad_id in tracked_ad_ids if ad_id.strip()}
        absent_ids = sorted(tracked_ids - present_ids)

        with self._connect() as connection:
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
        return len(snapshots) + len(absent_ids)

    def ad_history(self, ad_id: str) -> tuple[AdSnapshot, ...]:
        """Return observations by actual instant, then insertion ID for ties."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, ad_id, observed_at, source, lifecycle_state, title,
                       description, views, watch_count, reply_count
                FROM ad_snapshots
                WHERE ad_id = ?
                """,
                (ad_id,),
            ).fetchall()
        ordered_rows = sorted(
            rows,
            key=lambda row: (
                datetime.fromisoformat(row["observed_at"]),
                int(row["id"]),
            ),
        )
        return tuple(
            AdSnapshot(
                ad_id=row["ad_id"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
                source=row["source"],
                lifecycle_state=LifecycleState(row["lifecycle_state"]),
                title=row["title"],
                description=row["description"],
                views=row["views"],
                watch_count=row["watch_count"],
                reply_count=row["reply_count"],
            )
            for row in ordered_rows
        )

    def latest_ad_snapshot(self, ad_id: str) -> AdSnapshot | None:
        history = self.ad_history(ad_id)
        return history[-1] if history else None

    def tracked_ad_ids(self) -> tuple[str, ...]:
        """Return every ad ID ever observed by this store."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT ad_id
                FROM ad_snapshots
                ORDER BY ad_id ASC
                """
            ).fetchall()
        return tuple(str(row["ad_id"]) for row in rows)

    def reaction_history(self, ad_id: str) -> tuple[ReactionSnapshot, ...]:
        with self._connect() as connection:
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
        return tuple(
            ReactionSnapshot(
                ad_id=row["ad_id"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
                source=row["source"],
                conversation_count=row["conversation_count"],
                unique_buyer_count=row["unique_buyer_count"],
                inbound_message_count=row["inbound_message_count"],
            )
            for row in rows
        )

    def inbound_message_history(
        self,
        ad_id: str,
    ) -> tuple[InboundMessageEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, ad_id, conversation_id, provider_message_id,
                       observed_at, source
                FROM inbound_message_events
                WHERE ad_id = ?
                """,
                (ad_id,),
            ).fetchall()

        ordered_rows = sorted(
            rows,
            key=lambda row: (
                datetime.fromisoformat(row["observed_at"]),
                int(row["id"]),
            ),
        )
        return tuple(
            InboundMessageEvent(
                ad_id=row["ad_id"],
                conversation_id=row["conversation_id"],
                provider_message_id=row["provider_message_id"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
                source=row["source"],
            )
            for row in ordered_rows
        )

    def inbound_message_ad_ids(self) -> tuple[str, ...]:
        """Return every ad ID represented by imported inbound-message events."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT ad_id
                FROM inbound_message_events
                ORDER BY ad_id ASC
                """
            ).fetchall()
        return tuple(str(row["ad_id"]) for row in rows)

    def inbound_message_counts(self, ad_id: str) -> tuple[int, int]:
        """Return (conversation_count, inbound_message_count) for one ad."""

        with self._connect() as connection:
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

        ordered_rows = sorted(
            rows,
            key=lambda row: (
                datetime.fromisoformat(row["observed_at"]),
                int(row["id"]),
            ),
        )
        return tuple(
            AdClassification(
                ad_id=row["ad_id"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
                source=row["source"],
                image_type=row["image_type"],
                city=row["city"],
                text_type=row["text_type"],
                title_type=row["title_type"],
            )
            for row in ordered_rows
        )

    def latest_classification(self, ad_id: str) -> AdClassification | None:
        history = self.classification_history(ad_id)
        return history[-1] if history else None