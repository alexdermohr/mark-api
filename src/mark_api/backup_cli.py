"""Create-only, WAL-aware backups of an initialized Mark product SQLite store.

This tool has no Kleinanzeigen clients, credentials, or platform write path.
It does not restore into any existing database. Uncertain platform writes
remain pending recovery fences in the backup.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
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


def _source_journal_mode(path: Path, validated: os.stat_result) -> str:
    """Read the SQLite header of the validated source inode, not a pathname alias."""
    descriptor = os.open(
        path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        actual = os.fstat(descriptor)
        header = os.pread(descriptor, 20, 0)
    except OSError as exc:
        raise BackupError("SQLite source journal mode cannot be attested") from exc
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(actual.st_mode)
        or (actual.st_dev, actual.st_ino, actual.st_nlink)
        != (validated.st_dev, validated.st_ino, validated.st_nlink)
        or header[:16] != _SQLITE_HEADER
    ):
        raise BackupError("SQLite source journal mode identity is invalid")
    if header[18:20] == bytes((1, 1)):
        return "rollback"
    if header[18:20] == bytes((2, 2)):
        return "wal"
    raise BackupError("SQLite source journal mode is unsupported")


_WAL_MAGICS = (b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83")


def _sqlite_file_fds(magics: tuple[bytes, ...]) -> dict[int, tuple[int, int]]:
    """Attest open SQLite/WAL file identities through Linux procfs."""
    try:
        names = os.listdir("/proc/self/fd")
    except OSError as exc:
        raise BackupError("SQLite source inode attestation is unavailable") from exc
    found: dict[int, tuple[int, int]] = {}
    length = max(map(len, magics))
    for item in names:
        try:
            descriptor = int(item)
            first = os.fstat(descriptor)
            if not stat.S_ISREG(first.st_mode):
                continue
            signature = os.pread(descriptor, length, 0)
            if not any(signature.startswith(magic) for magic in magics):
                continue
            second = os.fstat(descriptor)
        except (OSError, ValueError):
            continue
        if (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino):
            found[descriptor] = (first.st_dev, first.st_ino)
    return found


def _sqlite_main_fds() -> dict[int, tuple[int, int]]:
    return _sqlite_file_fds((_SQLITE_HEADER,))


def _sqlite_wal_fds() -> dict[int, tuple[int, int]]:
    return _sqlite_file_fds(_WAL_MAGICS)


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


def _sidecar_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BackupError("WAL source identity is unsafe")
    return info.st_dev, info.st_ino


def _capture_sidecar_contents(
    path: Path, identity: tuple[int, int] | None,
) -> os.stat_result | None:
    if identity is None:
        return None
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupError("WAL source contents cannot be inspected") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (info.st_dev, info.st_ino) != identity
    ):
        raise BackupError("WAL source contents changed during preflight")
    return info


def _assert_sidecar_contents_unchanged(
    path: Path, original: os.stat_result | None,
) -> None:
    if original is None:
        return
    try:
        current = path.lstat()
    except OSError as exc:
        raise BackupError("WAL source contents could not be verified") from exc
    fields = (
        "st_dev", "st_ino", "st_nlink", "st_size",
        "st_mtime_ns", "st_ctime_ns",
    )
    if any(getattr(current, field) != getattr(original, field) for field in fields):
        raise BackupError("WAL source contents changed during backup")


def _sidecar_digest(path: Path, original: os.stat_result | None) -> str | None:
    if original is None:
        return None
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as exc:
        raise BackupError("WAL source content digest is unavailable") from exc
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino, current.st_nlink)
            != (original.st_dev, original.st_ino, original.st_nlink)
        ):
            raise BackupError("WAL source content digest identity changed")
        return _sha256_fd(descriptor)
    finally:
        os.close(descriptor)


def _shared_file_mappings() -> set[tuple[int, int]]:
    """Identify file-backed shared mappings, including renamed SQLite -shm.

    Pathnames in /proc/self/maps are not reliable after a rename. Compare
    numeric mapped device/inode against the attested -shm inode instead.
    """
    found: set[tuple[int, int]] = set()
    try:
        with open("/proc/self/maps", encoding="ascii") as mappings:
            for line in mappings:
                fields = line.split(maxsplit=5)
                if len(fields) < 5 or len(fields[1]) != 4 or fields[1][3] != "s":
                    continue
                if int(fields[4]) == 0:
                    continue
                major, minor = fields[3].split(":", 1)
                found.add((os.makedev(int(major, 16), int(minor, 16)), int(fields[4])))
    except (OSError, ValueError) as exc:
        raise BackupError("WAL source identity mapping attestation is unavailable") from exc
    return found


def _attest_sidecars(
    wal_path: Path, shm_path: Path,
    wal_before: tuple[int, int] | None,
    shm_before: tuple[int, int] | None,
    open_wal_before: dict[int, tuple[int, int]],
    maps_before: set[tuple[int, int]],
    resources: ExitStack,
) -> tuple[tuple[int, int] | None, int | None, tuple[int, int] | None, int | None]:
    wal_now = _sidecar_identity(wal_path)
    shm_now = _sidecar_identity(shm_path)
    if (
        (wal_before is not None and wal_now != wal_before)
        or (shm_before is not None and shm_now != shm_before)
    ):
        raise BackupError("WAL source identity changed during connection")
    if wal_before is None and wal_now is not None:
        raise BackupError("WAL source identity appeared after initially absent WAL")
    if wal_now is None and shm_before is None and shm_now is not None:
        raise BackupError("WAL source identity has unexpected shared memory")
    # SQLite may create an empty WAL when it opens a quiet WAL-mode database
    # read-only. It contains no frames; a new nonempty WAL could instead be
    # an older captured sidecar planted after the first preflight read.
    empty_wal_fd: int | None = None
    if wal_now is not None:
        try:
            current = wal_path.lstat()
        except OSError as exc:
            raise BackupError("WAL source identity could not be inspected") from exc
        if current.st_size == 0:
            try:
                descriptor = os.open(
                    wal_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                )
            except OSError as exc:
                raise BackupError("WAL source identity empty file unavailable") from exc
            resources.callback(os.close, descriptor)
            try:
                actual = os.fstat(descriptor)
            except OSError as exc:
                raise BackupError("WAL source identity empty file unavailable") from exc
            if (
                not stat.S_ISREG(actual.st_mode)
                or actual.st_nlink != 1
                or (actual.st_dev, actual.st_ino) != wal_now
                or actual.st_size != 0
            ):
                raise BackupError("WAL source identity empty file changed")
            empty_wal_fd = descriptor
    observed = _sqlite_wal_fds()
    candidates = [
        (fd, identity) for fd, identity in observed.items()
        if open_wal_before.get(fd) != identity
    ]
    if wal_now is None or empty_wal_fd is not None:
        if candidates:
            raise BackupError("WAL source identity cannot be attested")
        wal_fd = None
    elif len(candidates) != 1 or candidates[0][1] != wal_now:
        raise BackupError("WAL source identity does not match SQLite connection")
    else:
        wal_fd = candidates[0][0]
    introduced_maps = _shared_file_mappings() - maps_before
    if any(identity != shm_now for identity in introduced_maps):
        raise BackupError("WAL source identity differs from SQLite shared mappings")
    return wal_now, wal_fd, shm_now, empty_wal_fd


def _assert_sidecars_unchanged(
    wal_path: Path, wal_identity: tuple[int, int] | None, wal_fd: int | None,
    shm_path: Path, shm_identity: tuple[int, int] | None,
    empty_wal_fd: int | None,
    maps_before: set[tuple[int, int]],
) -> None:
    if _sidecar_identity(wal_path) != wal_identity:
        raise BackupError("WAL source identity changed during backup")
    if _sidecar_identity(shm_path) != shm_identity:
        raise BackupError("WAL source identity changed during backup")
    if empty_wal_fd is not None:
        try:
            empty = os.fstat(empty_wal_fd)
        except OSError as exc:
            raise BackupError("WAL source identity empty file was lost") from exc
        if (
            (empty.st_dev, empty.st_ino) != wal_identity
            or empty.st_size != 0
        ):
            raise BackupError("WAL source identity newly introduced frames")
    if wal_fd is not None:
        try:
            now = os.fstat(wal_fd)
            signature = os.pread(wal_fd, 4, 0)
        except OSError as exc:
            raise BackupError("WAL source identity fd was lost") from exc
        if (
            (now.st_dev, now.st_ino) != wal_identity
            or signature not in _WAL_MAGICS
        ):
            raise BackupError("WAL source identity fd changed")
    if any(identity != shm_identity for identity in _shared_file_mappings() - maps_before):
        raise BackupError("WAL source identity mapping changed")


def _assert_source_contents_unchanged(
    descriptor: int, original: os.stat_result,
) -> None:
    """Fail closed if the validated SQLite main inode changed in-place."""
    try:
        current = os.fstat(descriptor)
    except OSError as exc:
        raise BackupError("immutable SQLite source descriptor was lost") from exc
    fields = (
        "st_dev", "st_ino", "st_size", "st_nlink", "st_mtime_ns", "st_ctime_ns",
    )
    if any(getattr(current, field) != getattr(original, field) for field in fields):
        raise BackupError("source contents changed during backup")


def _assert_no_rollback_journal(path: Path) -> None:
    # A left-behind SQLite rollback journal may contain uncommitted pages.
    # immutable=1 does not perform hot-journal recovery; never certify a DB
    # with such unresolved recovery state.
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise BackupError("source rollback journal requires offline recovery")


def _assert_stage_contents_unchanged(
    descriptor: int, original: os.stat_result,
) -> None:
    try:
        current = os.fstat(descriptor)
    except OSError as exc:
        raise BackupError("backup stage contents identity was lost") from exc
    fields = (
        "st_dev", "st_ino", "st_nlink", "st_size",
        "st_mtime_ns", "st_ctime_ns",
    )
    if any(getattr(current, field) != getattr(original, field) for field in fields):
        raise BackupError("backup stage contents changed during backup")


def _assert_destination_parent(path: Path, descriptor: int) -> None:
    try:
        current = path.lstat()
        pinned = os.fstat(descriptor)
    except OSError as exc:
        raise BackupError("destination directory identity was lost") from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or not stat.S_ISDIR(pinned.st_mode)
        or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino)
    ):
        raise BackupError("destination directory identity changed")


@contextmanager
def _pinned_destination_parent(path: Path):
    try:
        original = path.lstat()
    except FileNotFoundError as exc:
        raise BackupError("destination directory must already exist") from exc
    if not stat.S_ISDIR(original.st_mode):
        raise BackupError("destination directory must be a real directory")
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        if (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino) != (
            original.st_dev, original.st_ino
        ):
            raise BackupError("destination directory identity changed")
        _assert_destination_parent(path, descriptor)
        yield descriptor
    finally:
        os.close(descriptor)


def _new_target(path: Path, directory_fd: int) -> None:
    _assert_destination_parent(path.parent, directory_fd)
    try:
        os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise BackupError("backup destination already exists; never overwrite it")


def _fsync_directory(descriptor: int) -> None:
    os.fsync(descriptor)


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    before = os.fstat(descriptor)
    offset = 0
    while offset < before.st_size:
        part = os.pread(descriptor, min(1024 * 1024, before.st_size - offset), offset)
        if not part:
            raise BackupError("backup contents changed during hashing")
        digest.update(part)
        offset += len(part)
    after = os.fstat(descriptor)
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise BackupError("backup contents changed during hashing")
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
    canonical_source = source.resolve(strict=False)
    canonical_target = target.resolve(strict=False)
    if canonical_source == canonical_target:
        raise BackupError("source and backup must be different")
    if canonical_target in {
        canonical_source.with_name(canonical_source.name + suffix)
        for suffix in ("-wal", "-shm", "-journal")
    }:
        raise BackupError("backup destination conflicts with source SQLite sidecar")
    original = _validated_source(source)
    wal_path = source.with_name(source.name + "-wal")
    shm_path = source.with_name(source.name + "-shm")
    journal_path = source.with_name(source.name + "-journal")
    _assert_no_rollback_journal(journal_path)
    wal_before = _sidecar_identity(wal_path)
    shm_before = _sidecar_identity(shm_path)
    wal_contents_before = _capture_sidecar_contents(wal_path, wal_before)
    wal_digest_before = _sidecar_digest(wal_path, wal_contents_before)
    if wal_before is None and shm_before is not None:
        raise BackupError("WAL source identity has orphan shared memory")
    if wal_before is not None and shm_before is None:
        raise BackupError("WAL source identity missing shared memory")
    journal_mode = _source_journal_mode(source, original)
    immutable_source = (
        journal_mode == "wal" and wal_before is None and shm_before is None
    )
    # Only a quiet WAL-mode source without sidecars is safe to open immutable:
    # it must ignore injected WAL frames while its main file stays unchanged.
    # Rollback-mode databases must use normal SQLite read locks or concurrent
    # writes could produce a torn snapshot while skipping journaling.
    source_uri = _uri(source, "ro") + ("&immutable=1" if immutable_source else "")
    open_main_before = _sqlite_main_fds()
    open_wal_before = _sqlite_wal_fds()
    maps_before = _shared_file_mappings()
    source_identity = (original.st_dev, original.st_ino)
    if any(identity != source_identity for identity in open_main_before.values()):
        raise BackupError("SQLite source attestation has unrelated open SQLite files")
    if any(identity != wal_before for identity in open_wal_before.values()):
        raise BackupError("WAL source identity has unrelated open SQLite WAL files")

    try:
        with ExitStack() as resources:
            target_dir_fd = resources.enter_context(
                _pinned_destination_parent(target.parent)
            )
            _new_target(target, target_dir_fd)
            original_db = resources.enter_context(
                closing(sqlite3.connect(source_uri, uri=True, timeout=5))
            )
            sqlite_source_fd = _attest_source_connection(
                original_db, open_main_before, original,
            )
            _assert_source_contents_unchanged(sqlite_source_fd, original)
            wal_identity, sqlite_wal_fd, shm_identity, empty_wal_fd = _attest_sidecars(
                wal_path, shm_path, wal_before, shm_before,
                open_wal_before, maps_before, resources,
            )
            # On some supported SQLite versions the reader itself advances a
            # valid WAL inode's ctime during schema_version. Rebaseline only
            # after proving its entire contents are byte-identical to the
            # pre-open digest; subsequent edits remain fail-closed.
            if _sidecar_digest(wal_path, wal_contents_before) != wal_digest_before:
                raise BackupError("WAL source contents changed during opening")
            wal_contents_open = _capture_sidecar_contents(wal_path, wal_identity)
            # An ordinary reader may touch -shm while establishing its WAL
            # read mark. Pin the SHM metadata after this expected open-time
            # bookkeeping, but still reject later in-place modifications.
            shm_contents_open = _capture_sidecar_contents(shm_path, shm_identity)
            _assert_no_rollback_journal(journal_path)
            _require_healthy_store(original_db)
            # SQLite only writes into private process memory. Its unix VFS
            # can resolve /proc/self/fd/N into an attacker-replaceable path;
            # never give it a file-backed stage or journal pathname.
            with closing(sqlite3.connect(":memory:", timeout=5)) as snapshot:
                original_db.backup(snapshot, pages=128, sleep=0.05)
                _require_healthy_store(snapshot)
                integrity = snapshot.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise BackupError("backup integrity verification failed")
                data = {
                    "ad_snapshots": _count(snapshot, "SELECT count(*) FROM ad_snapshots"),
                    "inbound_message_events": _count(
                        snapshot, "SELECT count(*) FROM inbound_message_events"
                    ),
                    "sync_attempts": _count(snapshot, "SELECT count(*) FROM sync_attempts"),
                    "open_sync_attempts": _count(
                        snapshot, "SELECT count(*) FROM sync_attempts WHERE outcome='in_progress'"
                    ),
                    "pending_api_writes": _count(
                        snapshot, "SELECT count(*) FROM write_api_requests WHERE state='in_progress'"
                    ),
                    "pending_dashboard_writes": _count(
                        snapshot, "SELECT count(*) FROM dashboard_pending_writes"
                    ),
                }
                try:
                    image = snapshot.serialize()
                except (AttributeError, MemoryError, sqlite3.Error) as exc:
                    raise BackupError("in-memory backup serialization unavailable") from exc
            if not image or image[:16] != _SQLITE_HEADER:
                raise BackupError("in-memory backup serialization is invalid")
            # O_TMPFILE leaves no stage pathname to swap or pre-open. The
            # destination inode stays anonymous until verified and linked.
            if not hasattr(os, "O_TMPFILE"):
                raise BackupError("anonymous backup staging requires Linux O_TMPFILE")
            try:
                stage_fd = os.open(
                    ".", os.O_TMPFILE | os.O_RDWR | os.O_CLOEXEC, 0o600,
                    dir_fd=target_dir_fd,
                )
            except OSError as exc:
                raise BackupError("anonymous backup staging is unavailable") from exc
            resources.callback(os.close, stage_fd)
            stage_ref = Path(f"/proc/self/fd/{stage_fd}")
            expected_image_sha256 = hashlib.sha256(image).hexdigest()
            image_view = memoryview(image)
            offset = 0
            while offset < len(image_view):
                written = os.write(
                    stage_fd, image_view[offset:offset + 1024 * 1024],
                )
                if written <= 0:
                    raise BackupError("anonymous backup stage write incomplete")
                offset += written
            image_view.release()
            del image
            os.fchmod(stage_fd, 0o400)
            verified_stage = os.fstat(stage_fd)
            if _sha256_fd(stage_fd) != expected_image_sha256:
                raise BackupError("anonymous backup stage contents changed")
            _assert_stage_contents_unchanged(stage_fd, verified_stage)
            _source_identity_unchanged(source, original)
            _assert_source_connection_inode(sqlite_source_fd, original)
            _assert_source_contents_unchanged(sqlite_source_fd, original)
            _assert_sidecars_unchanged(
                wal_path, wal_identity, sqlite_wal_fd,
                shm_path, shm_identity, empty_wal_fd, maps_before,
            )
            _assert_sidecar_contents_unchanged(wal_path, wal_contents_open)
            _assert_sidecar_contents_unchanged(shm_path, shm_contents_open)
            _assert_no_rollback_journal(journal_path)
            os.fsync(stage_fd)
            backup_sha256 = _sha256_fd(stage_fd)
            _assert_stage_contents_unchanged(stage_fd, verified_stage)
            _source_identity_unchanged(source, original)
            _assert_source_connection_inode(sqlite_source_fd, original)
            _assert_source_contents_unchanged(sqlite_source_fd, original)
            _assert_sidecars_unchanged(
                wal_path, wal_identity, sqlite_wal_fd,
                shm_path, shm_identity, empty_wal_fd, maps_before,
            )
            _assert_sidecar_contents_unchanged(wal_path, wal_contents_open)
            _assert_sidecar_contents_unchanged(shm_path, shm_contents_open)
            _assert_no_rollback_journal(journal_path)
            _new_target(target, target_dir_fd)
            _assert_stage_contents_unchanged(stage_fd, verified_stage)
            # Linux linkat follows this procfd symlink to the exact open stage
            # inode; the destination is addressed only by pinned dirfd.
            os.link(
                str(stage_ref), target.name, dst_dir_fd=target_dir_fd,
                follow_symlinks=True,
            )
            # Hard-link publication legitimately increments nlink and ctime.
            # Rebind after that exact effect; never silently accept a later
            # in-place write even if the attacker restores the old mtime.
            linked_stage = os.fstat(stage_fd)
            try:
                _fsync_directory(target_dir_fd)
            except OSError as exc:
                raise BackupError(
                    "backup publication uncertain; inspect destination before retry"
                ) from exc
            copied = os.fstat(stage_fd)
            published = os.stat(
                target.name, dir_fd=target_dir_fd, follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(published.st_mode)
                or (copied.st_dev, copied.st_ino)
                != (published.st_dev, published.st_ino)
            ):
                raise BackupError("backup publication identity mismatch")
            if _sha256_fd(stage_fd) != backup_sha256:
                raise BackupError("backup stage contents changed after publication")
            _assert_stage_contents_unchanged(stage_fd, linked_stage)
            _assert_destination_parent(target.parent, target_dir_fd)
            _source_identity_unchanged(source, original)
            _assert_source_connection_inode(sqlite_source_fd, original)
            _assert_source_contents_unchanged(sqlite_source_fd, original)
            _assert_sidecars_unchanged(
                wal_path, wal_identity, sqlite_wal_fd,
                shm_path, shm_identity, empty_wal_fd, maps_before,
            )
            _assert_sidecar_contents_unchanged(wal_path, wal_contents_open)
            _assert_sidecar_contents_unchanged(shm_path, shm_contents_open)
            _assert_no_rollback_journal(journal_path)
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