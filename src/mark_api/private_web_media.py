from __future__ import annotations

import ctypes
import errno
import os
import re
import sys
import stat
import tempfile
from contextlib import AbstractContextManager, nullcontext
from secrets import token_urlsafe
from threading import Lock
from time import monotonic, time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .domain import AdCreateRequest
from .results import ReadResult
from .ports import WriteNotAttemptedError
from .private_web import (
    PrivateWebCreateSnapshot,
    PrivateWebCreateWriter,
    PrivateWebEditorState,
    PrivateWebError,
    PrivateWebPreconditionError,
    PrivateWebSubmitUnknownError,
    PrivateWebWriteNotAttemptedError,
)


@dataclass(frozen=True, slots=True)
class PrivateWebMediaSource:
    """One explicit local media source.

    Paths are intentionally excluded from repr so request logging does not
    disclose local filesystem layout. The source is copied from a validated
    open descriptor before any browser access.
    """

    path: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.path, str):
            raise TypeError("media source path must be a string")
        if not self.path or "\x00" in self.path or not os.path.isabs(self.path):
            raise ValueError("media source path must be an absolute path")


@dataclass(frozen=True, slots=True)
class PrivateWebMediaPersistenceSnapshot:
    """Authoritative server-side media verification result for one owner ad.

    The verifier owns the platform-specific identity algorithm. This snapshot
    deliberately carries no local path, browser FileList state, CDN URL, or
    provider-specific media identifier.
    """

    ad_id: str
    observed_at: datetime
    source: str
    exact_match: bool

    def __post_init__(self) -> None:
        if not isinstance(self.ad_id, str) or not self.ad_id.strip():
            raise ValueError("media persistence snapshot requires ad_id")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("media persistence snapshot requires source")
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise ValueError(
                "media persistence observed_at must be timezone-aware"
            )
        if not isinstance(self.exact_match, bool):
            raise TypeError("media persistence exact_match must be bool")


class PrivateWebMediaPersistenceVerifier(Protocol):
    """Verify stable expected media with caller-bounded authoritative I/O."""

    def verify_media(
        self,
        ad_id: str,
        expected_sources: tuple[PrivateWebMediaSource, ...],
        *,
        timeout_seconds: float,
    ) -> ReadResult[PrivateWebMediaPersistenceSnapshot]:
        """Return within timeout_seconds; transport expiry is a read failure."""
        ...


@dataclass(frozen=True, slots=True)
class PrivateWebMediaFileSnapshot:
    """Path-free identity available on both the local and browser sides."""

    name: str
    size_bytes: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not self.name
            or self.name in {".", ".."}
            or "/" in self.name
            or "\\" in self.name
        ):
            raise ValueError("media file name must be a basename")
        if (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes < 0
        ):
            raise ValueError("media file size must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class PrivateWebCreateMediaSnapshot:
    """Target-bound browser FileList readback for the create form.

    READY means only that the exact browser file input contains the listed
    locally prepared files. It does not mean Kleinanzeigen accepted, uploaded,
    persisted, or published any media.
    """

    state: PrivateWebEditorState
    files: tuple[PrivateWebMediaFileSnapshot, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, PrivateWebEditorState):
            raise TypeError("state must be PrivateWebEditorState")
        if not isinstance(self.files, tuple):
            raise TypeError("files must be a tuple")
        if any(
            not isinstance(item, PrivateWebMediaFileSnapshot)
            for item in self.files
        ):
            raise TypeError("files must contain PrivateWebMediaFileSnapshot")
        if self.state is PrivateWebEditorState.READY:
            if not self.files:
                raise ValueError("ready media snapshot requires files")
            return
        if self.files:
            raise ValueError("non-ready media snapshot must not expose files")


class PrivateWebMediaUnknownError(PrivateWebError):
    """A file-input action may have taken effect; retry is not authorized."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"private web media outcome unknown at {stage}")


class PrivateWebCreateMediaPage(Protocol):
    """Minimal create-form boundary for explicit browser file selection."""

    def read_create_form(self) -> PrivateWebCreateSnapshot:
        ...

    def stage_create_media(self, files: tuple[str, ...]) -> None:
        ...

    def read_create_media(self) -> PrivateWebCreateMediaSnapshot:
        ...


class PrivateWebCreateMediaPublishPage(PrivateWebCreateMediaPage, Protocol):
    """Create-form boundary for one separately gated media-filled publish."""

    def open_create_form(self, category_path: tuple[str, ...]) -> None:
        ...

    def replace_create_title(self, value: str) -> None:
        ...

    def replace_create_description(self, value: str) -> None:
        ...

    def replace_create_price(self, value: str) -> None:
        ...

    def submit_create(self) -> None:
        """Required by PrivateWebCreateWriter; MediaWriter never invokes it."""
        ...

    def submit_create_media(
        self,
        expected: PrivateWebCreateMediaSnapshot,
    ) -> None:
        ...


class _PreparedPrivateWebMedia:
    """Own browser-readable private copies of validated source descriptors."""

    __slots__ = ("_directory", "paths", "files", "_closed")

    def __init__(
        self,
        *,
        directory: tempfile.TemporaryDirectory,
        paths: tuple[str, ...],
        files: tuple[PrivateWebMediaFileSnapshot, ...],
    ) -> None:
        self._directory = directory
        self.paths = paths
        self.files = files
        self._closed = False

    def __repr__(self) -> str:
        return (
            "_PreparedPrivateWebMedia("
            f"files={self.files!r}, closed={self._closed!r})"
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._directory.cleanup()


def _validated_source_names(
    sources: tuple[PrivateWebMediaSource, ...],
) -> tuple[str, ...]:
    if not isinstance(sources, tuple):
        raise TypeError("media sources must be a tuple")
    if not sources:
        raise ValueError("at least one media source is required")
    if any(not isinstance(source, PrivateWebMediaSource) for source in sources):
        raise TypeError("media sources must contain PrivateWebMediaSource")

    names: list[str] = []
    seen: set[str] = set()
    for source in sources:
        name = os.path.basename(source.path)
        if (
            not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
        ):
            raise ValueError("media source must have a safe basename")
        if name in seen:
            raise ValueError("media source basenames must be unique")
        seen.add(name)
        names.append(name)
    return tuple(names)


def _copy_validated_descriptor(
    source: PrivateWebMediaSource,
    *,
    name: str,
    directory: str,
) -> PrivateWebMediaFileSnapshot:
    path = source.path
    try:
        pathname_snapshot = os.lstat(path)
        if (
            stat.S_ISLNK(pathname_snapshot.st_mode)
            or not stat.S_ISREG(pathname_snapshot.st_mode)
        ):
            raise ValueError("media source must be a regular file")

        source_flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            source_flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            source_flags |= os.O_NOFOLLOW
        source_fd = os.open(path, source_flags)
        try:
            opened = os.fstat(source_fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or pathname_snapshot.st_dev != opened.st_dev
                or pathname_snapshot.st_ino != opened.st_ino
                or pathname_snapshot.st_size != opened.st_size
                or pathname_snapshot.st_mtime_ns != opened.st_mtime_ns
                or pathname_snapshot.st_ctime_ns != opened.st_ctime_ns
            ):
                raise ValueError("media source changed during validation")

            destination = os.path.join(directory, name)
            destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_CLOEXEC"):
                destination_flags |= os.O_CLOEXEC
            destination_fd = os.open(destination, destination_flags, 0o600)
            copied = 0
            try:
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    offset = 0
                    while offset < len(chunk):
                        written = os.write(destination_fd, chunk[offset:])
                        if written <= 0:
                            raise OSError("short media copy")
                        offset += written
                        copied += written
                os.fsync(destination_fd)
            finally:
                os.close(destination_fd)

            after_copy = os.fstat(source_fd)
        finally:
            os.close(source_fd)
    except ValueError:
        raise
    except OSError:
        raise ValueError("media source is unavailable") from None

    if (
        opened.st_dev != after_copy.st_dev
        or opened.st_ino != after_copy.st_ino
        or opened.st_size != after_copy.st_size
        or opened.st_mtime_ns != after_copy.st_mtime_ns
        or opened.st_ctime_ns != after_copy.st_ctime_ns
        or copied != opened.st_size
    ):
        raise ValueError("media source changed during validation")

    try:
        copied_stat = os.stat(os.path.join(directory, name), follow_symlinks=False)
    except OSError:
        raise ValueError("media source is unavailable") from None
    if (
        not stat.S_ISREG(copied_stat.st_mode)
        or copied_stat.st_size != opened.st_size
    ):
        raise ValueError("media source changed during validation")

    return PrivateWebMediaFileSnapshot(
        name=name,
        size_bytes=int(opened.st_size),
    )


def _prepare_local_media(
    sources: tuple[PrivateWebMediaSource, ...],
    *,
    delete_on_gc: bool = True,
) -> _PreparedPrivateWebMedia:
    if not isinstance(delete_on_gc, bool):
        raise TypeError("delete_on_gc must be a bool")
    names = _validated_source_names(sources)
    try:
        directory = tempfile.TemporaryDirectory(
            prefix="mark-private-web-media-",
            ignore_cleanup_errors=True,
            delete=delete_on_gc,
        )
        os.chmod(directory.name, 0o700)
    except OSError:
        raise ValueError("media source is unavailable") from None

    paths: list[str] = []
    snapshots: list[PrivateWebMediaFileSnapshot] = []
    try:
        for source, name in zip(sources, names, strict=True):
            snapshot = _copy_validated_descriptor(
                source,
                name=name,
                directory=directory.name,
            )
            paths.append(os.path.join(directory.name, name))
            snapshots.append(snapshot)
        return _PreparedPrivateWebMedia(
            directory=directory,
            paths=tuple(paths),
            files=tuple(snapshots),
        )
    except Exception:
        directory.cleanup()
        raise


_MEDIA_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class PrivateWebMediaRefResolver:
    """Resolve pre-authorized opaque refs through one immutable binding map.

    Bindings are copied at construction time and have no mutation surface.
    Resolution itself grants no filesystem authority from ref text; the media
    service immediately stabilizes the selected sources before its first
    owner/browser read.
    """

    def __init__(
        self,
        bindings: Mapping[str, PrivateWebMediaSource],
    ) -> None:
        if not isinstance(bindings, Mapping):
            raise TypeError("media ref bindings must be a mapping")
        copied: dict[str, PrivateWebMediaSource] = {}
        for ref, source in bindings.items():
            if (
                not isinstance(ref, str)
                or _MEDIA_REF_RE.fullmatch(ref) is None
            ):
                raise ValueError("media ref is invalid")
            if not isinstance(source, PrivateWebMediaSource):
                raise TypeError(
                    "media ref bindings must contain PrivateWebMediaSource"
                )
            copied[ref] = source
        self._sources = copied

    def __repr__(self) -> str:
        return f"PrivateWebMediaRefResolver(refs={len(self._sources)})"

    def resolve(
        self,
        media_refs: tuple[str, ...],
    ) -> tuple[PrivateWebMediaSource, ...]:
        if (
            not isinstance(media_refs, tuple)
            or not media_refs
            or any(
                not isinstance(ref, str)
                or _MEDIA_REF_RE.fullmatch(ref) is None
                for ref in media_refs
            )
            or len(set(media_refs)) != len(media_refs)
        ):
            raise PrivateWebWriteNotAttemptedError("resolve_media_refs")
        try:
            return tuple(self._sources[ref] for ref in media_refs)
        except KeyError:
            raise PrivateWebWriteNotAttemptedError(
                "resolve_media_refs"
            ) from None


def _fsync_directory(path: str) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_filesystem_directory(path: str, parent_device: int) -> None:
    """Durably sync a same-filesystem parent when it is not readable (Linux)."""
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOSYS, "filesystem sync is not supported")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(path, flags)
    try:
        device = os.fstat(descriptor).st_dev
        if device != parent_device:
            raise OSError(errno.EXDEV, "media handle directory is on another filesystem")
        # syncfs on FUSE/stacked filesystems can return success without
        # syncing the parent's directory entry. Older kernels also do not
        # report writeback errors. Never certify a marker on those paths.
        kernel_parts = os.uname().release.split(".")
        try:
            kernel_version = (int(kernel_parts[0]), int(kernel_parts[1]))
        except (IndexError, ValueError) as exc:
            raise OSError(errno.ENOTSUP, "filesystem sync kernel is unverified") from exc
        if kernel_version < (5, 8):
            raise OSError(errno.ENOTSUP, "filesystem sync kernel is too old")
        device_key = f"{os.major(device)}:{os.minor(device)}"
        types: set[str] = set()
        with open("/proc/self/mountinfo", encoding="utf-8") as mounts:
            for line in mounts:
                parts = line.split(" - ", 1)
                if len(parts) != 2:
                    continue
                mount = parts[0].split()
                filesystem = parts[1].split()
                if len(mount) >= 3 and mount[2] == device_key and filesystem:
                    types.add(filesystem[0])
        if not types or not types.issubset({"ext4", "xfs", "btrfs", "f2fs"}):
            raise OSError(errno.ENOTSUP, "filesystem sync durability is unverified")
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            syncfs = libc.syncfs
        except AttributeError as exc:
            raise OSError(errno.ENOSYS, "filesystem sync is unavailable") from exc
        syncfs.argtypes = [ctypes.c_int]
        syncfs.restype = ctypes.c_int
        if syncfs(descriptor) != 0:
            error = ctypes.get_errno() or errno.EIO
            raise OSError(error, os.strerror(error))
    finally:
        os.close(descriptor)


class PrivateWebMediaHandleStore(PrivateWebMediaRefResolver):
    """Own bounded private media copies behind generated opaque handles."""

    _DURABLE_DIRECTORY_MARKER = ".mark-private-media-ready-v1"
    _MAX_STAGED_HANDLES = 32
    _MAX_STAGED_BYTES = 100 * 1024 * 1024
    _STAGED_HANDLE_TTL_SECONDS = 15 * 60

    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
        wall_clock: Callable[[], float] = time,
        directory: str | os.PathLike[str] | None = None,
        protected_refs: Callable[[], frozenset[str]] | None = None,
        protected_refs_guard: Callable[[], AbstractContextManager[None]] | None = None,
    ) -> None:
        if not callable(clock):
            raise TypeError("media handle store clock must be callable")
        if not callable(wall_clock):
            raise TypeError("media handle store wall clock must be callable")
        if protected_refs is not None and not callable(protected_refs):
            raise TypeError("protected media refs provider must be callable")
        if protected_refs_guard is not None and not callable(protected_refs_guard):
            raise TypeError("protected media refs guard must be callable")
        super().__init__({})
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        if directory is None:
            self._temporary_directory = tempfile.TemporaryDirectory(
                prefix="mark-private-web-media-handles-"
            )
            self._directory_path = self._temporary_directory.name
            self._persistent = False
        else:
            raw_directory = os.fspath(directory)
            if (
                not isinstance(raw_directory, str)
                or not raw_directory
                or "\x00" in raw_directory
            ):
                raise ValueError("media handle directory is invalid")
            directory_path = os.path.abspath(raw_directory)
            parent_path = os.path.dirname(directory_path)
            try:
                parent_stat = os.stat(parent_path)
            except OSError as exc:
                raise ValueError(
                    "media handle parent directory is invalid"
                ) from exc
            if not stat.S_ISDIR(parent_stat.st_mode):
                raise ValueError("media handle parent directory is invalid")
            created_directory = False
            try:
                os.mkdir(directory_path, 0o700)
                created_directory = True
            except FileExistsError:
                pass
            marker_path = os.path.join(
                directory_path, self._DURABLE_DIRECTORY_MARKER
            )
            try:
                directory_stat = os.stat(directory_path, follow_symlinks=False)
                if not stat.S_ISDIR(directory_stat.st_mode):
                    raise ValueError("media handle directory is invalid")
                os.chmod(directory_path, 0o700)
                _fsync_directory(directory_path)
                try:
                    marker_fd = os.open(
                        marker_path,
                        os.O_RDONLY
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0)
                        | os.O_NONBLOCK,
                    )
                except FileNotFoundError:
                    # An existing directory might be left by a crash after mkdir,
                    # before its parent entry was durable. Only a completed
                    # parent fsync authorizes publishing the readiness marker.
                    try:
                        _fsync_directory(parent_path)
                    except PermissionError as exc:
                        if exc.errno != errno.EACCES:
                            raise
                        # syncfs flushes all metadata on the same Linux filesystem,
                        # including the unreadable parent's new directory entry.
                        _sync_filesystem_directory(
                            directory_path, parent_stat.st_dev
                        )
                    marker_fd = os.open(
                        marker_path,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                    )
                    try:
                        os.fsync(marker_fd)
                    finally:
                        os.close(marker_fd)
                    _fsync_directory(directory_path)
                else:
                    try:
                        marker_stat = os.fstat(marker_fd)
                        if (
                            not stat.S_ISREG(marker_stat.st_mode)
                            or marker_stat.st_uid != os.getuid()
                            or marker_stat.st_nlink != 1
                            or marker_stat.st_size != 0
                            or stat.S_IMODE(marker_stat.st_mode) & 0o077
                        ):
                            raise ValueError(
                                "media handle directory readiness marker is invalid"
                            )
                    finally:
                        os.close(marker_fd)
            except Exception:
                if created_directory:
                    try:
                        os.rmdir(directory_path)
                    except OSError:
                        # Failed rollback never certifies an incomplete directory.
                        # Its missing marker forces parent fsync on next startup.
                        pass
                raise
            self._directory_path = directory_path
            self._persistent = True
        self._lock = Lock()
        self._closed = False
        self._cleanup_required = False
        self._pending_cleanup_paths: set[str] = set()
        self._staged_bytes = 0
        self._clock = clock
        self._wall_clock = wall_clock
        self._protected_refs = protected_refs
        self._protected_refs_guard = protected_refs_guard
        self._sizes: dict[str, int] = {}
        self._expires_at: dict[str, float] = {}
        if self._persistent:
            self._rehydrate_persistent_handles()

    def _protected_refs_snapshot(self) -> frozenset[str]:
        provider = self._protected_refs
        if provider is None:
            return frozenset()
        try:
            refs = frozenset(provider())
        except Exception:
            raise RuntimeError("protected media refs are unavailable") from None
        if any(
            not isinstance(ref, str) or _MEDIA_REF_RE.fullmatch(ref) is None
            for ref in refs
        ):
            raise RuntimeError("protected media refs are invalid")
        return refs

    def _protected_refs_guard_context(self) -> AbstractContextManager[None]:
        provider = self._protected_refs_guard
        if provider is None:
            return nullcontext()
        try:
            return provider()
        except Exception:
            raise RuntimeError("protected media refs guard is unavailable") from None

    def _rehydrate_persistent_handles(self) -> None:
        protected = self._protected_refs_snapshot()
        now = float(self._clock())
        wall_now = float(self._wall_clock())
        total_bytes = 0
        loaded_refs: set[str] = set()
        with os.scandir(self._directory_path) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    raise RuntimeError("persistent media handle directory is invalid")
                name = entry.name
                if name == self._DURABLE_DIRECTORY_MARKER:
                    continue
                temp_extension = next(
                    (
                        suffix
                        for suffix in (".jpg", ".png", ".webp")
                        if name.endswith(suffix + ".tmp")
                    ),
                    None,
                )
                if name.startswith(".") and temp_extension is not None:
                    temp_ref = name[1 : -(len(temp_extension) + len(".tmp"))]
                    if _MEDIA_REF_RE.fullmatch(temp_ref) is not None:
                        try:
                            os.unlink(entry.path)
                        except FileNotFoundError:
                            pass
                        continue
                extension = next(
                    (
                        suffix
                        for suffix in (".jpg", ".png", ".webp")
                        if name.endswith(suffix)
                    ),
                    None,
                )
                if extension is None:
                    raise RuntimeError("persistent media handle directory is invalid")
                ref = name[: -len(extension)]
                if (
                    _MEDIA_REF_RE.fullmatch(ref) is None
                    or ref in loaded_refs
                ):
                    raise RuntimeError("persistent media handle directory is invalid")
                file_stat = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise RuntimeError("persistent media handle is invalid")
                if file_stat.st_size <= 0:
                    if ref in protected:
                        raise RuntimeError("persistent media handle is invalid")
                    try:
                        os.unlink(entry.path)
                    except FileNotFoundError:
                        pass
                    continue
                fd = os.open(
                    entry.path,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    prefix = os.read(fd, 12)
                finally:
                    os.close(fd)
                try:
                    detected_extension = self._extension(name, prefix)
                except ValueError:
                    if ref in protected:
                        raise RuntimeError("persistent media handle is invalid") from None
                    try:
                        os.unlink(entry.path)
                    except FileNotFoundError:
                        pass
                    continue
                if detected_extension != extension:
                    if ref in protected:
                        raise RuntimeError("persistent media handle is invalid")
                    try:
                        os.unlink(entry.path)
                    except FileNotFoundError:
                        pass
                    continue
                expires_wall = (
                    float(file_stat.st_mtime) + self._STAGED_HANDLE_TTL_SECONDS
                )
                if expires_wall <= wall_now and ref not in protected:
                    try:
                        os.unlink(entry.path)
                    except FileNotFoundError:
                        pass
                    continue
                if (
                    len(loaded_refs) >= self._MAX_STAGED_HANDLES
                    or total_bytes + int(file_stat.st_size) > self._MAX_STAGED_BYTES
                ):
                    raise RuntimeError("persistent media staging quota exceeded")
                loaded_refs.add(ref)
                total_bytes += int(file_stat.st_size)
                self._sources[ref] = PrivateWebMediaSource(entry.path)
                self._sizes[ref] = int(file_stat.st_size)
                self._expires_at[ref] = now + max(0.0, expires_wall - wall_now)
        self._staged_bytes = total_bytes

    @staticmethod
    def _unlink_sources(
        sources: tuple[PrivateWebMediaSource, ...],
    ) -> None:
        for source in sources:
            try:
                os.unlink(source.path)
            except FileNotFoundError:
                pass

    def _pop_handles_locked(
        self,
        media_refs: tuple[str, ...],
    ) -> tuple[PrivateWebMediaSource, ...]:
        sources: list[PrivateWebMediaSource] = []
        released_bytes = 0
        for ref in media_refs:
            source = self._sources.pop(ref, None)
            released_bytes += self._sizes.pop(ref, 0)
            self._expires_at.pop(ref, None)
            if source is not None:
                sources.append(source)
        self._staged_bytes = max(0, self._staged_bytes - released_bytes)
        return tuple(sources)

    def _retire_handles_locked(self, media_refs: tuple[str, ...]) -> None:
        sources = tuple(
            self._sources[ref] for ref in media_refs if ref in self._sources
        )
        if not sources:
            return
        try:
            self._unlink_sources(sources)
            if self._persistent:
                _fsync_directory(self._directory_path)
        except Exception:
            # Keep all affected refs and their quota until durable cleanup
            # completes; close() can retry partial removals safely.
            self._cleanup_required = True
            raise
        self._pop_handles_locked(media_refs)

    def _prune_expired_locked(
        self,
        now: float,
        preserve: frozenset[str] = frozenset(),
    ) -> None:
        expired = tuple(
            ref
            for ref, expires_at in self._expires_at.items()
            if expires_at <= now and ref not in preserve
        )
        if expired:
            self._retire_handles_locked(expired)

    @staticmethod
    def _extension(filename: str, data: bytes) -> str:
        if (
            not isinstance(filename, str)
            or not filename
            or filename != os.path.basename(filename)
            or filename in {".", ".."}
            or len(filename) > 255
            or any(ord(char) < 32 or ord(char) == 127 for char in filename)
        ):
            raise ValueError("invalid media filename")
        lowered = filename.lower()
        if data.startswith(b"\xff\xd8\xff") and lowered.endswith((".jpg", ".jpeg")):
            return ".jpg"
        if data.startswith(b"\x89PNG\r\n\x1a\n") and lowered.endswith(".png"):
            return ".png"
        if (
            len(data) >= 12
            and data[:4] == b"RIFF"
            and data[8:12] == b"WEBP"
            and lowered.endswith(".webp")
        ):
            return ".webp"
        raise ValueError("unsupported media image")

    def stage_media(self, filename: str, data: bytes) -> str:
        if not isinstance(data, bytes) or not data:
            raise ValueError("media bytes are required")
        extension = self._extension(filename, data)
        with self._protected_refs_guard_context(), self._lock:
            protected = self._protected_refs_snapshot()
            if self._closed:
                raise RuntimeError("media handle store is closed")
            if self._cleanup_required:
                raise RuntimeError("media handle store cleanup required")
            self._prune_expired_locked(
                float(self._clock()),
                preserve=protected,
            )
            if (
                len(self._sources) >= self._MAX_STAGED_HANDLES
                or self._staged_bytes + len(data) > self._MAX_STAGED_BYTES
            ):
                raise ValueError("media staging quota exceeded")
            while True:
                ref = "media_" + token_urlsafe(18)
                if _MEDIA_REF_RE.fullmatch(ref) is not None and ref not in self._sources:
                    break
            path = os.path.join(self._directory_path, ref + extension)
            temp_path = os.path.join(
                self._directory_path,
                "." + ref + extension + ".tmp",
            )
            fd = os.open(
                temp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                view = memoryview(data)
                written = 0
                while written < len(view):
                    count = os.write(fd, view[written:])
                    if count <= 0:
                        raise OSError("short media write")
                    written += count
                os.fsync(fd)
            except Exception:
                try:
                    os.unlink(temp_path)
                except OSError:
                    self._pending_cleanup_paths.add(temp_path)
                    self._cleanup_required = True
                raise
            finally:
                os.close(fd)
            published = False
            try:
                staged_at = float(self._wall_clock())
                os.utime(
                    temp_path,
                    (staged_at, staged_at),
                    follow_symlinks=False,
                )
                os.replace(temp_path, path)
                published = True
                if self._persistent:
                    _fsync_directory(self._directory_path)
            except Exception:
                cleanup_path = path if published else temp_path
                cleanup_unlink_failed = False
                try:
                    os.unlink(cleanup_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    cleanup_unlink_failed = True
                cleanup_sync_failed = False
                if published and self._persistent and not cleanup_unlink_failed:
                    try:
                        _fsync_directory(self._directory_path)
                    except OSError:
                        cleanup_sync_failed = True
                if cleanup_unlink_failed:
                    if published:
                        self._sources[ref] = PrivateWebMediaSource(path)
                        self._sizes[ref] = len(data)
                        self._expires_at[ref] = float("inf")
                        self._staged_bytes += len(data)
                    else:
                        self._pending_cleanup_paths.add(temp_path)
                if cleanup_unlink_failed or cleanup_sync_failed:
                    self._cleanup_required = True
                raise
            self._sources[ref] = PrivateWebMediaSource(path)
            self._sizes[ref] = len(data)
            self._expires_at[ref] = (
                float(self._clock()) + self._STAGED_HANDLE_TTL_SECONDS
            )
            self._staged_bytes += len(data)
            return ref

    def resolve(
        self,
        media_refs: tuple[str, ...],
    ) -> tuple[PrivateWebMediaSource, ...]:
        requested = (
            frozenset(media_refs)
            if isinstance(media_refs, tuple)
            and all(isinstance(ref, str) for ref in media_refs)
            else frozenset()
        )
        with self._protected_refs_guard_context(), self._lock:
            preserve = requested | self._protected_refs_snapshot()
            if self._closed or self._cleanup_required:
                raise PrivateWebWriteNotAttemptedError("resolve_media_refs")
            self._prune_expired_locked(
                float(self._clock()),
                preserve=preserve,
            )
            return super().resolve(media_refs)

    def discard(self, media_refs: tuple[str, ...]) -> None:
        with self._lock:
            self._retire_handles_locked(media_refs)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
        with self._protected_refs_guard_context(), self._lock:
            protected = (
                self._protected_refs_snapshot()
                if self._persistent
                else frozenset()
            )
            if self._closed:
                return
            orphan_refs = tuple(
                ref for ref in self._sources if ref not in protected
            )
            orphan_sources = tuple(
                self._sources[ref]
                for ref in orphan_refs
                if ref in self._sources
            )
            cleanup_sources = orphan_sources + tuple(
                PrivateWebMediaSource(path) for path in self._pending_cleanup_paths
            )
            try:
                self._unlink_sources(cleanup_sources)
                if self._persistent and (cleanup_sources or self._cleanup_required):
                    _fsync_directory(self._directory_path)
            except Exception:
                self._cleanup_required = True
                raise
            self._pop_handles_locked(orphan_refs)
            self._pending_cleanup_paths.clear()
            self._cleanup_required = False
            self._closed = True
            self._sources.clear()
            self._sizes.clear()
            self._expires_at.clear()
            self._staged_bytes = 0
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()



class PrivateWebCreateMediaStager:
    """Select explicit local media on an already-bound create form.

    This contract deliberately stops before Publish. Success means only that a
    fresh target-bound FileList readback exactly matches stable private copies
    made from the locally validated source descriptors. It never claims
    server-side upload or publication and never retries a potentially effective
    file-input action.
    """

    def __init__(self, page: PrivateWebCreateMediaPage) -> None:
        self._page = page

    @staticmethod
    def _require_expected_create(
        snapshot: PrivateWebCreateSnapshot,
        request: AdCreateRequest,
    ) -> None:
        if not isinstance(snapshot, PrivateWebCreateSnapshot):
            raise PrivateWebPreconditionError("media_before:invalid_snapshot")
        if snapshot.state is not PrivateWebEditorState.READY:
            raise PrivateWebPreconditionError(
                f"media_before:{snapshot.state.value}"
            )
        if (
            snapshot.title != request.title
            or snapshot.description != request.description
            or snapshot.price_amount != str(request.price_eur)
        ):
            raise PrivateWebPreconditionError(
                "media_before:create_values_mismatch"
            )

    def _stage_prepared_media(
        self,
        request: AdCreateRequest,
        prepared: _PreparedPrivateWebMedia,
    ) -> PrivateWebCreateMediaSnapshot:
        """Stage caller-owned stable media without taking cleanup ownership."""
        if not isinstance(request, AdCreateRequest):
            raise TypeError("request must be AdCreateRequest")
        if not isinstance(prepared, _PreparedPrivateWebMedia):
            raise TypeError("prepared must be _PreparedPrivateWebMedia")

        try:
            before = self._page.read_create_form()
        except WriteNotAttemptedError:
            raise PrivateWebWriteNotAttemptedError(
                "read_create_before_media"
            ) from None
        except Exception:
            raise PrivateWebWriteNotAttemptedError(
                "read_create_before_media"
            ) from None
        self._require_expected_create(before, request)

        try:
            self._page.stage_create_media(prepared.paths)
        except WriteNotAttemptedError:
            raise PrivateWebWriteNotAttemptedError(
                "stage_create_media"
            ) from None
        except PrivateWebMediaUnknownError:
            # Reconcile by readback only; never retry file selection.
            pass
        except Exception:
            # An unclassified page/provider failure may have happened after
            # file-input mutation began. Reconcile by readback, never retry.
            pass

        try:
            after = self._page.read_create_media()
        except Exception:
            # Once file selection may have happened, even readback failure
            # is ambiguous. Never retry the file-input action.
            raise PrivateWebMediaUnknownError("media_readback") from None

        if (
            not isinstance(after, PrivateWebCreateMediaSnapshot)
            or after.state is not PrivateWebEditorState.READY
            or after.files != prepared.files
        ):
            raise PrivateWebMediaUnknownError("media_readback")
        return after

    def stage_create_media(
        self,
        request: AdCreateRequest,
        sources: tuple[PrivateWebMediaSource, ...],
    ) -> PrivateWebCreateMediaSnapshot:
        if not isinstance(request, AdCreateRequest):
            raise TypeError("request must be AdCreateRequest")

        # Public staging owns its preparation and keeps the historical
        # staging-only lifecycle. The publish writer uses _stage_prepared_media
        # with its own preparation so those exact stable files remain alive
        # through the subsequent media-aware submit.
        prepared = _prepare_local_media(sources)
        try:
            return self._stage_prepared_media(request, prepared)
        finally:
            prepared.close()


class PrivateWebCreateMediaWriter:
    """Publish one create request with explicit media through a separate gate.

    Local sources are stabilized before any browser access. The writer owns
    those stable files through the media-aware submit. The ordinary media-free
    create writer is reused only to prepare and revalidate the form; the CDP
    page owns its separate browser-facing copies until page.close().
    """

    def __init__(self, page: PrivateWebCreateMediaPublishPage) -> None:
        self._page = page

    @staticmethod
    def _close_after_error(prepared: _PreparedPrivateWebMedia) -> None:
        # Cleanup is secondary to an already classified writer outcome. Never
        # let a local cleanup failure replace a pre-submit or submit-unknown
        # classification and thereby accidentally authorize a retry.
        try:
            prepared.close()
        except Exception:
            pass

    def create_ad(
        self,
        request: AdCreateRequest,
        sources: tuple[PrivateWebMediaSource, ...],
    ) -> None:
        if not isinstance(request, AdCreateRequest):
            raise TypeError("request must be AdCreateRequest")

        # One writer-owned stable preparation lives through submit. The stager
        # borrows it without taking cleanup ownership; the CDP page separately
        # creates browser-facing copies that remain alive until page.close().
        # This phase is entirely local and precedes every browser mutation, so
        # validation/copy failures are provably safe to classify as not attempted.
        try:
            prepared = _prepare_local_media(sources)
        except (TypeError, ValueError):
            raise PrivateWebWriteNotAttemptedError(
                "prepare_create_media"
            ) from None
        try:
            PrivateWebCreateWriter(self._page).prepare_create(request)

            staged = PrivateWebCreateMediaStager(
                self._page
            )._stage_prepared_media(
                request,
                prepared,
            )
            if staged.files != prepared.files:
                raise PrivateWebMediaUnknownError("media_readback")

            try:
                self._page.submit_create_media(staged)
            except PrivateWebSubmitUnknownError:
                raise
            except WriteNotAttemptedError:
                raise PrivateWebWriteNotAttemptedError(
                    "submit_create_media"
                ) from None
            except Exception:  # noqa: BLE001 - submit may already have reached UI.
                raise PrivateWebSubmitUnknownError(
                    "submit_create_media"
                ) from None
        except Exception:
            self._close_after_error(prepared)
            raise

        try:
            prepared.close()
        except Exception:
            # submit_create_media returned after the one-shot browser-input
            # path. A cleanup failure must therefore remain non-retryable even
            # though it says nothing authoritative about platform persistence.
            raise PrivateWebSubmitUnknownError(
                "submit_create_media"
            ) from None