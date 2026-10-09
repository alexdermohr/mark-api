"""Create-only, WAL-aware backups of an initialized Mark product SQLite store.

This tool has no Kleinanzeigen clients, credentials, or platform write path.
It does not restore into any existing database. Uncertain platform writes
remain pending recovery fences in the backup.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from urllib.parse import quote

from .storage import SnapshotStore


class BackupError(RuntimeError):
    """Fail closed without exposing database contents or credentials."""


@dataclass(frozen=True, slots=True)
class BackupReceipt:
    created_at: str
    backup_sha256: str
    ad_snapshots: int
    inbound_message_events: int
    sync_attempts: int
    open_sync_attempts: int
    pending_api_writes: int
    pending_dashboard_writes: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _uri(path: Path, mode: str) -> str:
    return "file:" + quote(str(path), safe="/") + "?mode=" + mode


def _validated_source(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise BackupError("source database does not exist") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BackupError("source must be a regular non-aliased database file")
    return info


def _source_identity_unchanged(path: Path, original: os.stat_result) -> None:
    try:
        current = _validated_source(path)
    except BackupError as exc:
        raise BackupError("source identity changed during backup") from exc
    if (current.st_dev, current.st_ino, current.st_nlink) != (
        original.st_dev, original.st_ino, original.st_nlink
    ):
        raise BackupError("source identity changed during backup")


_SQLITE_HEADER = b"SQLite format 3\x00"


def _sqlite_main_fds() -> dict[int, tuple[int, int]]:
    """Identify live SQLite main-file descriptors in this Linux process.

    A connection's PRAGMA database_list only exposes its *path*, which can be
    swapped back after sqlite3.connect. Inspect actual open file descriptors
    instead. Fail closed on hosts without the Linux proc-fd readback.
    """
    try:
        names = os.listdir("/proc/self/fd")
    except OSError as exc:
        raise BackupError("SQLite source inode attestation is unavailable") from exc
    found: dict[int, tuple[int, int]] = {}
    for item in names:
        try:
            descriptor = int(item)
            first = os.fstat(descriptor)
            if not stat.S_ISREG(first.st_mode):
                continue
            if os.pread(descriptor, len(_SQLITE_HEADER), 0) != _SQLITE_HEADER:
                continue
            second = os.fstat(descriptor)
        except (OSError, ValueError):
            # Listing /proc/self/fd itself creates a transient directory fd.
            continue
        if (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino):
            found[descriptor] = (first.st_dev, first.st_ino)
    return found

def _attest_source_connection(
    connection: sqlite3.Connection,
    before: dict[int, tuple[int, int]],
    source: os.stat_result,
) -> int:
    """Require exactly one newly opened SQLite main fd bound to source.

    Preexisting SQLite main files must all be the validated source inode.
    Otherwise a preexisting foreign fd could close and be reused for the
    substituted inode with the same number, hiding it from the fd diff.
    Existing handles to the real source remain safe for live SQLite backups.
    Normal SQLite pathname/WAL resolution must remain intact.
    """
    connection.execute("PRAGMA schema_version").fetchone()
    opened = _sqlite_main_fds()
    candidates = [
        (descriptor, identity)
        for descriptor, identity in opened.items()
        if before.get(descriptor) != identity
    ]
    if len(candidates) != 1 or candidates[0][1] != (source.st_dev, source.st_ino):
        raise BackupError("SQLite source inode does not match validated database")
    return candidates[0][0]


def _assert_source_connection_inode(
    descriptor: int, source: os.stat_result,
) -> None:
    """Require the already-attested SQLite fd to remain bound until publish."""
    try:
        current = os.fstat(descriptor)
        header = os.pread(descriptor, len(_SQLITE_HEADER), 0)
    except OSError as exc:
        raise BackupError("SQLite source connection identity was lost") from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != (source.st_dev, source.st_ino)
        or header != _SQLITE_HEADER
    ):
        raise BackupError("SQLite source connection identity changed")


def _new_target(path: Path) -> None:
    try:
        parent = path.parent.lstat()
    except FileNotFoundError as exc:
        raise BackupError("destination directory must already exist") from exc
    if not stat.S_ISDIR(parent.st_mode):
        raise BackupError("destination directory must be a real directory")
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise BackupError("backup destination already exists; never overwrite it")


def _fsync_file(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_healthy_store(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    # Read-only: do not let SnapshotStore.__init__ implicitly migrate anything.
    SnapshotStore._validate_store_schema(connection)


def _count(connection: sqlite3.Connection, sql: str) -> int:
    row = connection.execute(sql).fetchone()
    if row is None or type(row[0]) is not int or row[0] < 0:
        raise BackupError("backup data is not countable")
    return int(row[0])


def backup_store(source_db: Path, *, backup_db: Path) -> BackupReceipt:
    """Publish an internally consistent point-in-time SQLite snapshot.

    SQLite's online backup includes committed WAL data. A new private staged
    file is verified before publication by a single atomic hard link.
    The final pathname is never opened by SQLite or overwritten.
    """
    if not isinstance(source_db, Path) or not isinstance(backup_db, Path):
        raise TypeError("source and backup paths must be pathlib.Path")
    source = source_db.expanduser().absolute()
    target = backup_db.expanduser().absolute()
    if source.resolve(strict=False) == target.resolve(strict=False):
        raise BackupError("source and backup must be different")
    original = _validated_source(source)
    _new_target(target)
    opened_before = _sqlite_main_fds()
    if any(
        identity != (original.st_dev, original.st_ino)
        for identity in opened_before.values()
    ):
        raise BackupError("SQLite source attestation has unrelated open SQLite files")

    try:
        with (
            closing(sqlite3.connect(_uri(source, "ro"), uri=True, timeout=5)) as original_db,
            tempfile.TemporaryDirectory(prefix=".mark-backup-", dir=target.parent) as temp_dir,
        ):
            sqlite_source_fd = _attest_source_connection(
                original_db, opened_before, original,
            )
            _require_healthy_store(original_db)
            staged = Path(temp_dir) / "backup.sqlite"
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(staged, flags, 0o600)
            os.close(fd)
            with closing(sqlite3.connect(_uri(staged, "rw"), uri=True, timeout=5)) as copy:
                original_db.backup(copy, pages=128, sleep=0.05)
            with closing(sqlite3.connect(_uri(staged, "ro"), uri=True, timeout=5)) as check:
                _require_healthy_store(check)
                integrity = check.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise BackupError("backup integrity verification failed")
                data = {
                    "ad_snapshots": _count(check, "SELECT count(*) FROM ad_snapshots"),
                    "inbound_message_events": _count(
                        check, "SELECT count(*) FROM inbound_message_events"
                    ),
                    "sync_attempts": _count(check, "SELECT count(*) FROM sync_attempts"),
                    "open_sync_attempts": _count(
                        check, "SELECT count(*) FROM sync_attempts WHERE outcome='in_progress'"
                    ),
                    "pending_api_writes": _count(
                        check, "SELECT count(*) FROM write_api_requests WHERE state='in_progress'"
                    ),
                    "pending_dashboard_writes": _count(
                        check, "SELECT count(*) FROM dashboard_pending_writes"
                    ),
                }
            _source_identity_unchanged(source, original)
            os.chmod(staged, 0o400)
            _fsync_file(staged)
            backup_sha256 = _sha256(staged)
            # Recheck both the path and the actual open SQLite source inode.
            _source_identity_unchanged(source, original)
            _assert_source_connection_inode(sqlite_source_fd, original)
            _new_target(target)
            os.link(staged, target)
            try:
                _fsync_directory(target.parent)
            except OSError as exc:
                # The destination may already exist even if fsync failed.
                raise BackupError(
                    "backup publication uncertain; inspect destination before retry"
                ) from exc
            copied = staged.lstat()
            published = target.lstat()
            if (
                not stat.S_ISREG(published.st_mode)
                or (copied.st_dev, copied.st_ino)
                != (published.st_dev, published.st_ino)
            ):
                raise BackupError("backup publication identity mismatch")
            # Post-publication drift invalidates a success claim. The copy
            # remains available for manual inspection; never blindly retry.
            _source_identity_unchanged(source, original)
            _assert_source_connection_inode(sqlite_source_fd, original)
            return BackupReceipt(
                created_at=datetime.now(timezone.utc).isoformat(),
                backup_sha256=backup_sha256,
                **data,
            )
    except sqlite3.Error as exc:
        raise BackupError("SQLite backup failed or source store is not ready") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create a verified, non-overwriting SQLite backup of the current Mark "
            "product database. No Kleinanzeigen platform calls are performed."
        )
    )
    parser.add_argument("--db", type=Path, required=True, help="Existing Mark SQLite database")
    parser.add_argument(
        "--backup", type=Path, required=True,
        help="New path in an existing directory; must not exist",
    )
    args = parser.parse_args(argv)
    try:
        receipt = backup_store(args.db, backup_db=args.backup)
    except BackupError as exc:
        parser.error(str(exc))
    except OSError:
        parser.error("backup failed; inspect source and destination before retry")
    print(json.dumps(receipt.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())