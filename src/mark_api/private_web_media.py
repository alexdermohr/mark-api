from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass, field
from typing import Protocol

from .domain import AdCreateRequest
from .ports import WriteNotAttemptedError
from .private_web import (
    PrivateWebCreateSnapshot,
    PrivateWebEditorState,
    PrivateWebError,
    PrivateWebPreconditionError,
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
) -> _PreparedPrivateWebMedia:
    names = _validated_source_names(sources)
    try:
        directory = tempfile.TemporaryDirectory(
            prefix="mark-private-web-media-",
            ignore_cleanup_errors=True,
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

    def stage_create_media(
        self,
        request: AdCreateRequest,
        sources: tuple[PrivateWebMediaSource, ...],
    ) -> PrivateWebCreateMediaSnapshot:
        if not isinstance(request, AdCreateRequest):
            raise TypeError("request must be AdCreateRequest")

        # All deterministic local validation and the stable-copy handoff happen
        # before any page/CDP access. The private copies stay alive through the
        # browser selection and readback.
        prepared = _prepare_local_media(sources)
        try:
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
        finally:
            prepared.close()
