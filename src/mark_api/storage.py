from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .domain import (
    AdClassification,
    AdSnapshot,
    LifecycleState,
    OperationReceipt,
    ReactionSnapshot,
)
from .results import ReadResult, ReadStatus


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
                """
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

    def append_classification(self, classification: AdClassification) -> None:
        with self._connect() as connection:
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

    @staticmethod
    def _snapshot_json(snapshot: AdSnapshot | None) -> str | None:
        if snapshot is None:
            return None
        data = asdict(snapshot)
        data["observed_at"] = snapshot.observed_at.isoformat()
        data["lifecycle_state"] = snapshot.lifecycle_state.value
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    def append_operation_receipt(self, receipt: OperationReceipt) -> None:
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
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT ad_id, observed_at, source, lifecycle_state, title,
                       description, views, watch_count, reply_count
                FROM ad_snapshots
                WHERE ad_id = ?
                ORDER BY id ASC
                """,
                (ad_id,),
            ).fetchall()
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
            for row in rows
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

    def classification_history(self, ad_id: str) -> tuple[AdClassification, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT ad_id, observed_at, source, image_type, city,
                       text_type, title_type
                FROM ad_classifications
                WHERE ad_id = ?
                ORDER BY observed_at ASC, id ASC
                """,
                (ad_id,),
            ).fetchall()
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
            for row in rows
        )

    def latest_classification(self, ad_id: str) -> AdClassification | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT ad_id, observed_at, source, image_type, city,
                       text_type, title_type
                FROM ad_classifications
                WHERE ad_id = ?
                ORDER BY observed_at DESC, id DESC
                LIMIT 1
                """,
                (ad_id,),
            ).fetchone()
        if row is None:
            return None
        return AdClassification(
            ad_id=row["ad_id"],
            observed_at=datetime.fromisoformat(row["observed_at"]),
            source=row["source"],
            image_type=row["image_type"],
            city=row["city"],
            text_type=row["text_type"],
            title_type=row["title_type"],
        )
