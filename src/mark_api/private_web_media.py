from __future__ import annotations

import os
import stat
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
    disclose local filesystem layout. The source is validated against the
    filesystem by PrivateWebCreateMediaStager before any browser access.
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
    local files. It does not mean Kleinanzeigen accepted, uploaded, persisted,
    or published any media.
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


def _validated_local_media(
    sources: tuple[PrivateWebMediaSource, ...],
) -> tuple[
    tuple[str, ...],
    tuple[PrivateWebMediaFileSnapshot, ...],
]:
    if not isinstance(sources, tuple):
        raise TypeError("media sources must be a tuple")
    if not sources:
        raise ValueError("at least one media source is required")
    if any(not isinstance(source, PrivateWebMediaSource) for source in sources):
        raise TypeError("media sources must contain PrivateWebMediaSource")

    paths: list[str] = []
    snapshots: list[PrivateWebMediaFileSnapshot] = []
    names: set[str] = set()

    for source in sources:
        path = source.path
        try:
            before = os.lstat(path)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                raise ValueError("media source must be a regular file")

            flags = os.O_RDONLY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW

            descriptor = os.open(path, flags)
            try:
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        except ValueError:
            raise
        except OSError:
            raise ValueError("media source is unavailable") from None

        if not stat.S_ISREG(after.st_mode):
            raise ValueError("media source must be a regular file")
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise ValueError("media source changed during validation")

        name = os.path.basename(path)
        if (
            not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
        ):
            raise ValueError("media source must have a safe basename")
        if name in names:
            raise ValueError("media source basenames must be unique")
        names.add(name)

        paths.append(path)
        snapshots.append(
            PrivateWebMediaFileSnapshot(
                name=name,
                size_bytes=int(after.st_size),
            )
        )

    return tuple(paths), tuple(snapshots)


class PrivateWebCreateMediaStager:
    """Select explicit local media on an already-bound create form.

    This contract deliberately stops before Publish. Success means only that a
    fresh target-bound FileList readback exactly matches the locally validated
    sources. It never claims server-side upload or publication and never
    retries a potentially effective file-input action.
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

        # All deterministic local checks happen before any page/CDP access.
        paths, expected_files = _validated_local_media(sources)

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
            self._page.stage_create_media(paths)
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
            # Once file selection may have happened, even readback failure is
            # ambiguous. The caller must not retry the file-input action.
            raise PrivateWebMediaUnknownError("media_readback") from None

        if (
            not isinstance(after, PrivateWebCreateMediaSnapshot)
            or after.state is not PrivateWebEditorState.READY
            or after.files != expected_files
        ):
            raise PrivateWebMediaUnknownError("media_readback")
        return after
