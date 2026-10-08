"""Explicit offline migration of historical read-mostly Mark SQLite stores.

The legacy source is never upgraded in-place. The online SQLite backup is
retained independently, and a complete new target is published only when all
validation/copy steps have succeeded. Normal product startup remains strict.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .storage import (
    SnapshotStore,
    _LEGACY_ADDABLE_COLUMNS,
    _REQUIRED_SINGLE_COLUMN_UNIQUES,
    _REQUIRED_STORE_COLUMNS,
)


class LegacyMigrationError(RuntimeError):
    """Unsafe or incomplete historical-store migration; no source was changed."""


_CORE = frozenset(("ad_snapshots", "reaction_snapshots", "operation_receipts"))
_CLASSIFIED = _CORE | {"ad_classifications"}
_EMAIL = _CLASSIFIED | {"inbound_message_events"}
_CREATE = _EMAIL | {"create_operation_receipts"}
_WRITE_API = _CREATE | {"write_api_requests"}
_CHECKPOINTS = _WRITE_API | {"create_operation_checkpoints"}
_HISTORICAL_TABLE_SETS = (
    _CORE,
    _CLASSIFIED,
    _EMAIL,
    _CREATE,
    _WRITE_API,
    _CHECKPOINTS,
)
_KNOWN_HISTORY_INDEXES = frozenset(
    (
        "idx_ad_snapshots_ad_id_observed",
        "idx_reaction_snapshots_ad_id_observed",
        "idx_ad_classifications_ad_id_observed",
        "idx_inbound_message_events_ad_id_observed",
    )
)


@dataclass(frozen=True, slots=True)
class LegacyMigrationReceipt:
    source_table_count: int
    copied_rows: dict[str, int]
    backup_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "migrated",
            "historical_table_count": self.source_table_count,
            "copied_rows": dict(sorted(self.copied_rows.items())),
            "backup_sha256": self.backup_sha256,
            "recovery_attested": True,
            "source_preserved": True,
            "platform_writes_performed": False,
        }


def _uri(path: Path, mode: str) -> str:
    return "file:" + quote(str(path.absolute()), safe="/") + "?mode=" + mode


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _column_names(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(
        str(row["name"])
        for row in connection.execute(
            f"PRAGMA table_info({_quote_identifier(table)})"
        ).fetchall()
    )


def _unconditional_unique_columns(
    connection: sqlite3.Connection, table: str
) -> set[str]:
    columns: set[str] = set()
    for index in connection.execute(
        f"PRAGMA index_list({_quote_identifier(table)})"
    ).fetchall():
        if not index["unique"] or index["partial"]:
            continue
        index_name = _quote_identifier(str(index["name"]))
        fields = connection.execute(
            f"PRAGMA index_info({index_name})"
        ).fetchall()
        if len(fields) == 1 and fields[0]["name"] is not None:
            columns.add(str(fields[0]["name"]))
    return columns


def _inspect_historical_store(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, ...], dict[str, int]]:
    integrity = connection.execute("PRAGMA quick_check(1)").fetchone()
    if integrity is None or integrity[0] != "ok":
        raise LegacyMigrationError("legacy database integrity check failed")
    objects = connection.execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if any(row["type"] in {"view", "trigger"} for row in objects):
        raise LegacyMigrationError("legacy database contains unsupported objects")
    indices = {
        str(row["name"])
        for row in objects
        if row["type"] == "index"
    }
    if not indices.issubset(_KNOWN_HISTORY_INDEXES):
        raise LegacyMigrationError("legacy database contains unknown indexes")
    tables = frozenset(
        str(row["name"]) for row in objects if row["type"] == "table"
    )
    if tables not in _HISTORICAL_TABLE_SETS:
        raise LegacyMigrationError("database is not a recognized historical schema")

    # A matching set of names alone is not proof of historical lineage.
    # Preserve an explicit recovery attestation and reject every suspicious
    # known write state before copying to a separate database.
    for table in tables:
        expected = set(_REQUIRED_STORE_COLUMNS[table])
        current = set(_column_names(connection, table))
        required = expected - _LEGACY_ADDABLE_COLUMNS.get(table, frozenset())
        if not required.issubset(current) or not current.issubset(expected):
            raise LegacyMigrationError("legacy database has incompatible columns")
        if table in _REQUIRED_SINGLE_COLUMN_UNIQUES:
            if not set(_REQUIRED_SINGLE_COLUMN_UNIQUES[table]).issubset(
                _unconditional_unique_columns(connection, table)
            ):
                raise LegacyMigrationError("legacy database recovery keys are unsafe")

    # A persisted completed API response remains an idempotency fence after
    # import; unknown/in-progress operations cannot be safely reconstructed.
    if "write_api_requests" in tables and connection.execute(
        "SELECT 1 FROM write_api_requests "
        "WHERE state <> 'completed' OR completed_at IS NULL "
        "OR response_status IS NULL OR response_json IS NULL LIMIT 1"
    ).fetchone() is not None:
        raise LegacyMigrationError("legacy write reconciliation is required")
    if "create_operation_checkpoints" in tables and connection.execute(
        "SELECT 1 FROM create_operation_checkpoints LIMIT 1"
    ).fetchone() is not None:
        raise LegacyMigrationError("legacy write reconciliation is required")
    for table in ("operation_receipts", "create_operation_receipts"):
        if table in tables and connection.execute(
            f"SELECT 1 FROM {_quote_identifier(table)} "
            "WHERE writer_invoked <> 0 AND outcome <> 'confirmed' LIMIT 1"
        ).fetchone() is not None:
            raise LegacyMigrationError("legacy write reconciliation is required")

    names = tuple(sorted(tables))
    counts = {
        table: int(
            connection.execute(
                f"SELECT count(*) FROM {_quote_identifier(table)}"
            ).fetchone()[0]
        )
        for table in names
    }
    return names, counts


def _create_private_file(path: Path) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    os.close(descriptor)


def _file_sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _copy_historical_rows(
    backup: sqlite3.Connection,
    destination: Path,
    names: tuple[str, ...],
    counts: dict[str, int],
    backup_sha: str,
) -> None:
    # sqlite_sequence is not a user table, but it is part of append-only ID
    # continuity. Preserve advanced counters even when high IDs were deleted.
    sequence_rows = backup.execute(
        "SELECT name, seq FROM sqlite_sequence"
    ).fetchall()
    sequences: dict[str, int] = {}
    for item in sequence_rows:
        name, sequence = str(item["name"]), item["seq"]
        if (
            name not in names or name in sequences
            or not isinstance(sequence, int) or sequence < 0
        ):
            raise LegacyMigrationError("legacy autoincrement state is invalid")
        sequences[name] = sequence

    SnapshotStore(destination)
    with closing(sqlite3.connect(_uri(destination, "rw"), uri=True)) as target, target:
        target.execute("BEGIN IMMEDIATE")
        for table in names:
            columns = _column_names(backup, table)
            quoted = ", ".join(_quote_identifier(column) for column in columns)
            marks = ", ".join("?" for _ in columns)
            sql = (
                f"INSERT INTO {_quote_identifier(table)} ({quoted}) "
                f"VALUES ({marks})"
            )
            target.executemany(
                sql,
                (
                    tuple(record)
                    for record in backup.execute(
                        f"SELECT {quoted} FROM {_quote_identifier(table)}"
                    )
                ),
            )
            inserted = int(
                target.execute(
                    f"SELECT count(*) FROM {_quote_identifier(table)}"
                ).fetchone()[0]
            )
            if inserted != counts[table]:
                raise LegacyMigrationError("legacy table row count changed")

        for table, counter in sequences.items():
            target.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
            target.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                (table, counter),
            )

        # Immutable provenance travels with the upgraded store. No buyer
        # content or platform write token is stored in this receipt.
        target.execute(
            """
            CREATE TABLE mark_legacy_import_receipt (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                copied_at TEXT NOT NULL,
                source_table_count INTEGER NOT NULL,
                backup_sha256 TEXT NOT NULL,
                copied_rows_json TEXT NOT NULL,
                recovery_attested INTEGER NOT NULL CHECK (recovery_attested = 1)
            )
            """
        )
        target.execute(
            "INSERT INTO mark_legacy_import_receipt "
            "(id, copied_at, source_table_count, backup_sha256, "
            "copied_rows_json, recovery_attested) VALUES (1, ?, ?, ?, ?, 1)",
            (
                datetime.now(timezone.utc).isoformat(),
                len(names),
                backup_sha,
                json.dumps(counts, sort_keys=True),
            ),
        )

    if not SnapshotStore(destination, create_if_missing=False).is_ready():
        raise LegacyMigrationError("upgraded store failed validation")


def migrate_legacy_store(
    source_db: str | Path,
    *,
    backup_db: str | Path,
    output_db: str | Path,
    confirm_no_unresolved_writes: bool = False,
) -> LegacyMigrationReceipt:
    """Copy a known old schema into a new store, retaining its original/backup.

    The confirmation is required because historical databases never wrote a
    durable version marker. A missing table cannot prove it never existed.
    No platform operation is executed or automatically replayed here.
    """
    if confirm_no_unresolved_writes is not True:
        raise LegacyMigrationError("explicit recovery confirmation is required")
    source = Path(source_db).expanduser().absolute()
    backup = Path(backup_db).expanduser().absolute()
    output = Path(output_db).expanduser().absolute()
    if len({str(p.resolve(strict=False)) for p in (source, backup, output)}) != 3:
        raise LegacyMigrationError("source, backup and output must be different")
    metadata = source.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise LegacyMigrationError("source must be a regular unlinked-alias file")
    for target in (backup, output):
        if not target.parent.is_dir():
            raise LegacyMigrationError("backup and output parents must exist")
        try:
            target.lstat()
        except FileNotFoundError:
            pass
        else:
            raise LegacyMigrationError("backup/output already exists")

    with closing(
        sqlite3.connect(_uri(source, "ro"), uri=True, timeout=3)
    ) as original:
        original.row_factory = sqlite3.Row
        names, counts = _inspect_historical_store(original)
        version_before = int(original.execute("PRAGMA data_version").fetchone()[0])
        # The independent original is never modified: SQLite's online backup
        # captures a consistent snapshot including committed WAL data.
        _create_private_file(backup)
        with closing(sqlite3.connect(_uri(backup, "rw"), uri=True)) as copy:
            original.backup(copy)
        _fsync_file(backup)
        os.chmod(backup, 0o400)
        version_after = int(original.execute("PRAGMA data_version").fetchone()[0])
        if version_after != version_before:
            raise LegacyMigrationError("source changed during backup")

        backup_sha = _file_sha256(backup)
        with closing(sqlite3.connect(_uri(backup, "ro"), uri=True)) as frozen:
            frozen.row_factory = sqlite3.Row
            backed_names, backed_counts = _inspect_historical_store(frozen)
            if backed_names != names or backed_counts != counts:
                raise LegacyMigrationError("backup differs from source observation")
            with tempfile.TemporaryDirectory(
                prefix=".mark-legacy-", dir=output.parent
            ) as temporary_directory:
                stage = Path(temporary_directory) / "mark.sqlite"
                _copy_historical_rows(frozen, stage, names, counts, backup_sha)
                _fsync_file(stage)
                # Hard-link publication is atomic and refuses an existing target.
                # The original and backup are never overwritten.
                if int(original.execute("PRAGMA data_version").fetchone()[0]) != version_before:
                    raise LegacyMigrationError("source changed during migration")
                os.link(stage, output)
                parent_fd = os.open(output.parent, os.O_RDONLY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)

    return LegacyMigrationReceipt(len(names), counts, backup_sha)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Offline, backup-backed import of historical Mark SQLite stores. "
            "Stop all old users of the source DB first; no Kleinanzeigen "
            "requests or platform writes are performed."
        )
    )
    parser.add_argument("--db", type=Path, required=True, help="Historical source DB")
    parser.add_argument(
        "--backup", type=Path, required=True,
        help="New path for immutable original SQLite backup",
    )
    parser.add_argument(
        "--output-db", type=Path, required=True,
        help="New non-existing path for the upgraded store",
    )
    parser.add_argument(
        "--confirm-no-unresolved-writes", action="store_true",
        help=(
            "Confirm all prior platform writes and unknown outcomes were "
            "independently reconciled; required before any historical import"
        ),
    )
    args = parser.parse_args(argv)
    try:
        receipt = migrate_legacy_store(
            args.db,
            backup_db=args.backup,
            output_db=args.output_db,
            confirm_no_unresolved_writes=args.confirm_no_unresolved_writes,
        )
    except (LegacyMigrationError, OSError, sqlite3.Error) as error:
        if isinstance(error, LegacyMigrationError):
            parser.error(str(error))
        parser.error(
            "SQLite migration failed; inspect backup/output state before retry"
        )
    print(json.dumps(receipt.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
