from __future__ import annotations

import errno
import gc
import io
import stat
import subprocess
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from unittest.mock import Mock, patch

import mark_api.private_web_media as private_web_media

from mark_api.domain import AdCreateRequest
from mark_api.private_web import (
    PrivateWebCreateSnapshot,
    PrivateWebEditorState,
    PrivateWebPreconditionError,
    PrivateWebWriteNotAttemptedError,
)
from mark_api.private_web_cdp import (
    PrivateWebCdpError,
    PrivateWebCdpWriteNotAttemptedError,
)
from mark_api.private_web_cdp_media import CdpPrivateWebMediaPage
from mark_api.private_web_media import (
    PrivateWebCreateMediaSnapshot,
    PrivateWebCreateMediaStager,
    PrivateWebCreateMediaWriter,
    PrivateWebMediaFileSnapshot,
    PrivateWebMediaHandleStore,
    PrivateWebMediaRefResolver,
    PrivateWebMediaSource,
    PrivateWebMediaUnknownError,
    _prepare_local_media,
)


def create_request() -> AdCreateRequest:
    return AdCreateRequest(
        category_path=("Haus & Garten", "Dekoration"),
        title="Neue Vase",
        description="Beschreibung",
        price_eur=12,
    )


def create_snapshot(
    state: PrivateWebEditorState = PrivateWebEditorState.READY,
    *,
    title: str = "Neue Vase",
    description: str = "Beschreibung",
    price_amount: str = "12",
) -> PrivateWebCreateSnapshot:
    if state is not PrivateWebEditorState.READY:
        return PrivateWebCreateSnapshot(state=state)
    return PrivateWebCreateSnapshot(
        state=state,
        title=title,
        description=description,
        price_amount=price_amount,
    )


def media_snapshot(name: str, size: int) -> PrivateWebCreateMediaSnapshot:
    return PrivateWebCreateMediaSnapshot(
        state=PrivateWebEditorState.READY,
        files=(PrivateWebMediaFileSnapshot(name=name, size_bytes=size),),
    )


class FakeMediaPage:
    def __init__(
        self,
        *,
        before: PrivateWebCreateSnapshot | None = None,
        after: PrivateWebCreateMediaSnapshot | Exception | None = None,
        stage_error: Exception | None = None,
    ) -> None:
        self.before = before or create_snapshot()
        self.after = after
        self.stage_error = stage_error
        self.calls: list[tuple[str, object]] = []

    def read_create_form(self) -> PrivateWebCreateSnapshot:
        self.calls.append(("read_create_form", None))
        return self.before

    def stage_create_media(self, files: tuple[str, ...]) -> None:
        self.calls.append(("stage_create_media", files))
        if self.stage_error is not None:
            raise self.stage_error

    def read_create_media(self) -> PrivateWebCreateMediaSnapshot:
        self.calls.append(("read_create_media", None))
        if isinstance(self.after, Exception):
            raise self.after
        if self.after is None:
            raise AssertionError("missing media readback")
        return self.after


class FakeClient:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        actual = dict(params or {})
        self.calls.append((method, actual))
        return self.handler(method, actual)

    def close(self) -> None:
        self.closed = True


def persistent_media_files(directory: Path) -> list[Path]:
    return [
        path
        for path in directory.iterdir()
        if path.name != PrivateWebMediaHandleStore._DURABLE_DIRECTORY_MARKER
    ]


class PrivateWebMediaContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.image = self.root / "photo.jpg"
        self.image.write_bytes(b"local-test-image")
        self.source = PrivateWebMediaSource(path=str(self.image))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_media_handle_store_stages_private_images_and_discards_handles(self) -> None:
        store = PrivateWebMediaHandleStore()
        try:
            ref = store.stage_media("photo.jpg", b"\xff\xd8\xffjpeg")
            self.assertRegex(ref, r"^media_[A-Za-z0-9_-]+$")
            (source,) = store.resolve((ref,))
            self.assertEqual(Path(source.path).read_bytes(), b"\xff\xd8\xffjpeg")
            self.assertEqual(stat.S_IMODE(Path(source.path).stat().st_mode), 0o600)
            self.assertNotIn("photo.jpg", source.path)
            store.discard((ref,))
            self.assertFalse(Path(source.path).exists())
            with self.assertRaises(PrivateWebWriteNotAttemptedError):
                store.resolve((ref,))
        finally:
            store.close()

    def test_media_handle_store_bounds_abandoned_handles_and_recovers_capacity(self) -> None:
        store = PrivateWebMediaHandleStore()
        try:
            store._MAX_STAGED_HANDLES = 2
            store._MAX_STAGED_BYTES = 16
            first = store.stage_media("one.jpg", b"\xff\xd8\xffone")
            second = store.stage_media("two.jpg", b"\xff\xd8\xfftwo")
            with self.assertRaisesRegex(ValueError, "media staging quota exceeded"):
                store.stage_media("three.jpg", b"\xff\xd8\xffx")
            store.discard((first,))
            third = store.stage_media("three.jpg", b"\xff\xd8\xffx")
            self.assertNotEqual(third, second)
        finally:
            store.close()

    def test_media_handle_store_expires_abandoned_handle_and_recovers_quota(self) -> None:
        now = [100.0]
        store = PrivateWebMediaHandleStore(clock=lambda: now[0])
        try:
            store._MAX_STAGED_HANDLES = 1
            store._STAGED_HANDLE_TTL_SECONDS = 10
            abandoned = store.stage_media("one.jpg", b"\xff\xd8\xffone")
            (source,) = store.resolve((abandoned,))

            now[0] = 109.999
            self.assertEqual(store.resolve((abandoned,)), (source,))
            self.assertTrue(Path(source.path).exists())

            now[0] = 110.0
            replacement = store.stage_media("two.jpg", b"\xff\xd8\xfftwo")
            self.assertNotEqual(replacement, abandoned)
            self.assertFalse(Path(source.path).exists())
            self.assertEqual(len(store._sources), 1)
            self.assertEqual(store._staged_bytes, len(b"\xff\xd8\xfftwo"))
            with self.assertRaises(PrivateWebWriteNotAttemptedError):
                store.resolve((abandoned,))
        finally:
            store.close()

    def test_media_handle_store_preserves_only_requested_expired_refs(self) -> None:
        now = [100.0]
        store = PrivateWebMediaHandleStore(clock=lambda: now[0])
        try:
            store._MAX_STAGED_HANDLES = 2
            store._STAGED_HANDLE_TTL_SECONDS = 10
            protected = store.stage_media("protected.jpg", b"\xff\xd8\xffone")
            orphan = store.stage_media("orphan.jpg", b"\xff\xd8\xfftwo")
            (protected_source,) = store.resolve((protected,))
            (orphan_source,) = store.resolve((orphan,))

            now[0] = 110.0
            self.assertEqual(store.resolve((protected,)), (protected_source,))
            self.assertTrue(Path(protected_source.path).exists())
            self.assertFalse(Path(orphan_source.path).exists())
            self.assertEqual(set(store._sources), {protected})
            self.assertEqual(store._staged_bytes, len(b"\xff\xd8\xffone"))

            with self.assertRaises(PrivateWebWriteNotAttemptedError):
                store.resolve((orphan,))
            with self.assertRaises(PrivateWebWriteNotAttemptedError):
                store.resolve(("media_missing",))

            store.discard((protected,))
            store.discard((protected,))
            self.assertFalse(Path(protected_source.path).exists())
            with self.assertRaises(PrivateWebWriteNotAttemptedError):
                store.resolve((protected,))
        finally:
            store.close()

    def test_media_handle_store_serializes_protected_snapshot_and_pruning(self) -> None:
        now = [100.0]
        protected_refs: set[str] = set()

        class Guard:
            def __init__(self) -> None:
                self.active = False

            def __enter__(self):
                self.active = True
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                self.active = False

        guard = Guard()

        def protected_snapshot() -> frozenset[str]:
            self.assertTrue(guard.active)
            return frozenset(protected_refs)

        store = PrivateWebMediaHandleStore(
            clock=lambda: now[0],
            protected_refs=protected_snapshot,
            protected_refs_guard=lambda: guard,
        )
        try:
            store._STAGED_HANDLE_TTL_SECONDS = 10
            protected = store.stage_media(
                "protected.jpg",
                b"\xff\xd8\xffprotected",
            )
            (protected_source,) = store.resolve((protected,))

            original_prune = store._prune_expired_locked

            def guarded_prune(
                current: float,
                preserve: frozenset[str] = frozenset(),
            ) -> None:
                self.assertTrue(guard.active)
                original_prune(current, preserve)

            store._prune_expired_locked = guarded_prune

            now[0] = 110.0
            protected_refs.add(protected)
            replacement = store.stage_media(
                "replacement.jpg",
                b"\xff\xd8\xffreplacement",
            )

            self.assertTrue(Path(protected_source.path).exists())
            self.assertEqual(
                set(store._sources),
                {protected, replacement},
            )
        finally:
            store.close()

    def test_persistent_media_handle_store_fsyncs_directory_creation_and_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            with patch.object(
                private_web_media,
                "_fsync_directory",
                wraps=private_web_media._fsync_directory,
            ) as fsync_directory:
                store = PrivateWebMediaHandleStore(
                    directory=directory,
                    protected_refs=lambda: frozenset(),
                )
                try:
                    ref = store.stage_media(
                        "photo.jpg",
                        b"\xff\xd8\xffdurable",
                    )
                    self.assertEqual(
                        Path(store.resolve((ref,))[0].path).parent,
                        directory,
                    )
                finally:
                    store.close()

            synced_paths = [Path(call.args[0]) for call in fsync_directory.call_args_list]
            self.assertEqual(
                synced_paths,
                [directory, directory.parent, directory, directory, directory],
            )

    def test_persistent_media_handle_store_removes_published_file_when_directory_fsync_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            real_fsync_directory = private_web_media._fsync_directory
            calls: list[Path] = []

            def fsync_directory(path: str) -> None:
                calls.append(Path(path))
                if len(calls) == 4:
                    raise OSError("directory fsync failed")
                real_fsync_directory(path)

            with patch.object(
                private_web_media,
                "_fsync_directory",
                side_effect=fsync_directory,
            ):
                store = PrivateWebMediaHandleStore(
                    directory=directory,
                    protected_refs=lambda: frozenset(),
                )
                try:
                    with self.assertRaisesRegex(OSError, "directory fsync failed"):
                        store.stage_media(
                            "photo.jpg",
                            b"\xff\xd8\xffdurable",
                        )
                    self.assertEqual(store._sources, {})
                    self.assertEqual(store._staged_bytes, 0)
                    self.assertEqual(persistent_media_files(directory), [])
                finally:
                    store.close()

            self.assertEqual(
                calls,
                [directory, directory.parent, directory, directory, directory],
            )

    def test_persistent_media_handle_store_tracks_failed_cleanup_and_blocks_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            real_fsync_directory = private_web_media._fsync_directory
            real_unlink = private_web_media.os.unlink
            fsync_calls: list[Path] = []

            def fsync_directory(path: str) -> None:
                fsync_calls.append(Path(path))
                if len(fsync_calls) == 4:
                    raise OSError("directory fsync failed")
                real_fsync_directory(path)

            def unlink(path: str) -> None:
                if Path(path).parent == directory and not Path(path).name.startswith("."):
                    raise OSError("cleanup unlink failed")
                real_unlink(path)

            store = None
            with (
                patch.object(
                    private_web_media,
                    "_fsync_directory",
                    side_effect=fsync_directory,
                ),
                patch.object(private_web_media.os, "unlink", side_effect=unlink),
            ):
                store = PrivateWebMediaHandleStore(
                    directory=directory,
                    protected_refs=lambda: frozenset(),
                )
                with self.assertRaisesRegex(OSError, "directory fsync failed"):
                    store.stage_media(
                        "photo.jpg",
                        b"\xff\xd8\xffdurable",
                    )
                self.assertTrue(store._cleanup_required)
                self.assertEqual(len(store._sources), 1)
                self.assertEqual(store._staged_bytes, len(b"\xff\xd8\xffdurable"))
                self.assertEqual(len(persistent_media_files(directory)), 1)
                with self.assertRaisesRegex(
                    RuntimeError,
                    "media handle store cleanup required",
                ):
                    store.stage_media(
                        "second.jpg",
                        b"\xff\xd8\xffsecond",
                    )

            assert store is not None
            store.close()
            self.assertEqual(persistent_media_files(directory), [])

    def test_persistent_media_handle_store_init_fsync_failure_retries_parent_sync(self) -> None:
        real_fsync_directory = private_web_media._fsync_directory
        for failing_level in ("handle", "parent"):
            with self.subTest(failing_level=failing_level):
                with tempfile.TemporaryDirectory() as tmp:
                    directory = Path(tmp) / "handles"
                    failing_path = directory if failing_level == "handle" else directory.parent
                    failed = False

                    def fsync_directory(path: str) -> None:
                        nonlocal failed
                        if Path(path) == failing_path and not failed:
                            failed = True
                            raise OSError("initial directory fsync failed")
                        real_fsync_directory(path)

                    with patch.object(
                        private_web_media,
                        "_fsync_directory",
                        side_effect=fsync_directory,
                    ):
                        with self.assertRaisesRegex(
                            OSError, "initial directory fsync failed"
                        ):
                            PrivateWebMediaHandleStore(
                                directory=directory,
                                protected_refs=lambda: frozenset(),
                            )
                    self.assertTrue(failed)
                    self.assertFalse(directory.exists())

                    with patch.object(
                        private_web_media,
                        "_fsync_directory",
                        wraps=real_fsync_directory,
                    ) as synced:
                        store = PrivateWebMediaHandleStore(
                            directory=directory,
                            protected_refs=lambda: frozenset(),
                        )
                        try:
                            ref = store.stage_media(
                                "photo.jpg", b"\xff\xd8\xffdurable"
                            )
                            self.assertTrue(Path(store.resolve((ref,))[0].path).exists())
                        finally:
                            store.close()
                    self.assertIn(
                        directory.parent,
                        [Path(call.args[0]) for call in synced.call_args_list],
                    )

    def test_persistent_media_handle_store_retries_failed_temp_cleanup_on_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            store = PrivateWebMediaHandleStore(
                directory=directory,
                protected_refs=lambda: frozenset(),
            )
            real_unlink = private_web_media.os.unlink

            def unlink(path: str, *args: object, **kwargs: object) -> None:
                if Path(path).parent == directory and Path(path).name.endswith(".tmp"):
                    raise OSError("temporary cleanup denied")
                real_unlink(path, *args, **kwargs)

            with (
                patch.object(
                    private_web_media.os, "utime",
                    side_effect=OSError("pre-publication failure"),
                ),
                patch.object(private_web_media.os, "unlink", side_effect=unlink),
            ):
                with self.assertRaisesRegex(OSError, "pre-publication failure"):
                    store.stage_media("photo.jpg", b"\xff\xd8\xffdurable")
                self.assertTrue(store._cleanup_required)
                self.assertEqual(store._sources, {})
                self.assertEqual(store._staged_bytes, 0)
                self.assertEqual(len(persistent_media_files(directory)), 1)
                with self.assertRaisesRegex(
                    RuntimeError, "media handle store cleanup required"
                ):
                    store.stage_media("second.jpg", b"\xff\xd8\xffsecond")
                with self.assertRaisesRegex(OSError, "temporary cleanup denied"):
                    store.close()
                self.assertFalse(store._closed)

            store.close()
            self.assertEqual(persistent_media_files(directory), [])

    def test_unmarked_legacy_directory_with_execute_only_parent_uses_checked_syncfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            directory.mkdir()
            marker = directory / PrivateWebMediaHandleStore._DURABLE_DIRECTORY_MARKER
            real_sync = private_web_media._fsync_directory
            syncfs_paths: list[Path] = []

            def fsync_directory(path: str) -> None:
                if Path(path) == directory.parent:
                    raise PermissionError(13, "parent is execute-only")
                real_sync(path)

            def syncfs_directory(path: str, parent_device: int) -> None:
                self.assertEqual(Path(path), directory)
                self.assertEqual(parent_device, directory.parent.stat().st_dev)
                self.assertFalse(marker.exists())
                syncfs_paths.append(Path(path))

            with (
                patch.object(
                    private_web_media,
                    "_fsync_directory",
                    side_effect=fsync_directory,
                ),
                patch.object(
                    private_web_media,
                    "_sync_filesystem_directory",
                    side_effect=syncfs_directory,
                    create=True,
                ),
            ):
                store = PrivateWebMediaHandleStore(
                    directory=directory, protected_refs=lambda: frozenset()
                )
                try:
                    ref = store.stage_media("photo.jpg", b"\xff\xd8\xffdurable")
                    self.assertTrue(Path(store.resolve((ref,))[0].path).exists())
                finally:
                    store.close()
            self.assertEqual(syncfs_paths, [directory])
            self.assertTrue(marker.is_file())

            with patch.object(
                private_web_media,
                "_fsync_directory",
                side_effect=fsync_directory,
            ):
                reopened = PrivateWebMediaHandleStore(
                    directory=directory, protected_refs=lambda: frozenset()
                )
                reopened.close()

    def test_readiness_marker_fifo_is_rejected_without_blocking_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            directory.mkdir()
            marker = directory / PrivateWebMediaHandleStore._DURABLE_DIRECTORY_MARKER
            private_web_media.os.mkfifo(marker)
            real_open = private_web_media.os.open

            def open_nonblocking(path: str, flags: int, *args: object) -> int:
                if str(path) == str(marker):
                    self.assertTrue(
                        flags & private_web_media.os.O_NONBLOCK,
                        "readiness marker open could block on FIFO",
                    )
                return real_open(path, flags, *args)

            with patch.object(
                private_web_media.os, "open", side_effect=open_nonblocking
            ):
                with self.assertRaisesRegex(ValueError, "readiness marker is invalid"):
                    PrivateWebMediaHandleStore(
                        directory=directory, protected_refs=lambda: frozenset()
                    )

    @unittest.skipUnless(private_web_media.sys.platform.startswith("linux"), "Linux only")
    def test_linux_syncfs_rejects_unsupported_filesystems_and_old_kernels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            device = directory.stat().st_dev
            major = private_web_media.os.major(device)
            minor = private_web_media.os.minor(device)
            for filesystem, release in (("fuse.sshfs", "6.10.0"), ("ext4", "4.18.0")):
                with self.subTest(filesystem=filesystem, release=release):
                    mountinfo = f"1 0 {major}:{minor} / / rw - {filesystem} dev rw\n"
                    fake_libc = type("Libc", (), {"syncfs": Mock(return_value=0)})()
                    with (
                        patch("builtins.open", return_value=io.StringIO(mountinfo)),
                        patch.object(
                            private_web_media.os,
                            "uname",
                            return_value=type("Uname", (), {"release": release})(),
                        ),
                        patch.object(private_web_media.ctypes, "CDLL", return_value=fake_libc),
                    ):
                        with self.assertRaises(OSError) as raised:
                            private_web_media._sync_filesystem_directory(str(directory), device)
                        self.assertIn(
                            raised.exception.errno, (errno.ENOTSUP, errno.EOPNOTSUPP)
                        )
                        fake_libc.syncfs.assert_not_called()

    @unittest.skipUnless(private_web_media.sys.platform.startswith("linux"), "Linux only")
    def test_linux_syncfs_propagates_writeback_errors_and_mount_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            device = directory.stat().st_dev
            major = private_web_media.os.major(device)
            minor = private_web_media.os.minor(device)
            with self.assertRaises(OSError) as mismatch:
                private_web_media._sync_filesystem_directory(str(directory), device + 1)
            self.assertEqual(mismatch.exception.errno, errno.EXDEV)

            mountinfo = f"1 0 {major}:{minor} / / rw - ext4 dev rw\n"
            fake_libc = type("Libc", (), {"syncfs": Mock(return_value=-1)})()
            with (
                patch("builtins.open", return_value=io.StringIO(mountinfo)),
                patch.object(private_web_media.ctypes, "CDLL", return_value=fake_libc),
                patch.object(private_web_media.ctypes, "get_errno", return_value=errno.EIO),
            ):
                with self.assertRaises(OSError) as error:
                    private_web_media._sync_filesystem_directory(str(directory), device)
                self.assertEqual(error.exception.errno, errno.EIO)
                fake_libc.syncfs.assert_called_once()

    def test_unmarked_legacy_directory_syncfs_error_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            directory.mkdir()
            real_sync = private_web_media._fsync_directory

            def fsync_directory(path: str) -> None:
                if Path(path) == directory.parent:
                    raise PermissionError(13, "parent is execute-only")
                real_sync(path)

            with (
                patch.object(
                    private_web_media,
                    "_fsync_directory",
                    side_effect=fsync_directory,
                ),
                patch.object(
                    private_web_media,
                    "_sync_filesystem_directory",
                    side_effect=OSError("filesystem sync failed"),
                    create=True,
                ),
            ):
                with self.assertRaisesRegex(OSError, "filesystem sync failed"):
                    PrivateWebMediaHandleStore(
                        directory=directory, protected_refs=lambda: frozenset()
                    )
            self.assertFalse(
                (directory / PrivateWebMediaHandleStore._DURABLE_DIRECTORY_MARKER).exists()
            )

    def test_existing_unmarked_directory_requires_parent_sync_after_interrupted_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            directory.mkdir()  # Simulates a crash before parent fsync.
            with patch.object(
                private_web_media,
                "_fsync_directory",
                wraps=private_web_media._fsync_directory,
            ) as synced:
                store = PrivateWebMediaHandleStore(
                    directory=directory,
                    protected_refs=lambda: frozenset(),
                )
                try:
                    ref = store.stage_media("photo.jpg", b"\xff\xd8\xffdurable")
                    self.assertTrue(Path(store.resolve((ref,))[0].path).exists())
                finally:
                    store.close()
            self.assertIn(
                directory.parent,
                [Path(call.args[0]) for call in synced.call_args_list],
            )

    def test_failed_first_parent_sync_and_failed_rmdir_do_not_bypass_retry_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            real_fsync = private_web_media._fsync_directory
            failed = False

            def sync(path: str) -> None:
                nonlocal failed
                if Path(path) == directory.parent and not failed:
                    failed = True
                    raise OSError("parent durability unavailable")
                real_fsync(path)

            with (
                patch.object(private_web_media, "_fsync_directory", side_effect=sync),
                patch.object(private_web_media.os, "rmdir", side_effect=OSError("rmdir failed")),
            ):
                with self.assertRaisesRegex(OSError, "parent durability unavailable"):
                    PrivateWebMediaHandleStore(
                        directory=directory, protected_refs=lambda: frozenset()
                    )
            self.assertTrue(failed)
            self.assertTrue(directory.exists())
            with patch.object(
                private_web_media,
                "_fsync_directory",
                wraps=real_fsync,
            ) as synced:
                store = PrivateWebMediaHandleStore(
                    directory=directory, protected_refs=lambda: frozenset()
                )
                try:
                    store.stage_media("photo.jpg", b"\xff\xd8\xffdurable")
                finally:
                    store.close()
            self.assertIn(
                directory.parent,
                [Path(call.args[0]) for call in synced.call_args_list],
            )

    def test_failed_partial_close_rejects_resolve_until_cleanup_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            store = PrivateWebMediaHandleStore(
                directory=directory, protected_refs=lambda: frozenset()
            )
            first = store.stage_media("first.jpg", b"\xff\xd8\xfffirst")
            second = store.stage_media("second.jpg", b"\xff\xd8\xffsecond")
            second_path = Path(store.resolve((second,))[0].path)
            first_path = Path(store.resolve((first,))[0].path)
            real_unlink = private_web_media.os.unlink

            def unlink(path: str, *args: object, **kwargs: object) -> None:
                if str(path) == str(second_path):
                    raise OSError("later cleanup failed")
                real_unlink(path, *args, **kwargs)

            with patch.object(private_web_media.os, "unlink", side_effect=unlink):
                with self.assertRaisesRegex(OSError, "later cleanup failed"):
                    store.close()
                self.assertFalse(first_path.exists())
                self.assertTrue(second_path.exists())
                self.assertTrue(store._cleanup_required)
                with self.assertRaises(PrivateWebWriteNotAttemptedError):
                    store.resolve((first,))
                with self.assertRaises(PrivateWebWriteNotAttemptedError):
                    store.resolve((second,))
            store.close()
            self.assertFalse(first_path.exists())
            self.assertFalse(second_path.exists())

    def test_persistent_media_handle_store_reopen_does_not_require_parent_fsync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            initial_store = PrivateWebMediaHandleStore(
                directory=directory, protected_refs=lambda: frozenset()
            )
            initial_store.close()
            real_fsync_directory = private_web_media._fsync_directory

            def fsync_directory(path: str) -> None:
                if Path(path) == directory.parent:
                    raise AssertionError("reopen must not fsync parent")
                real_fsync_directory(path)

            with patch.object(
                private_web_media,
                "_fsync_directory",
                side_effect=fsync_directory,
            ) as fsync_directory_mock:
                store = PrivateWebMediaHandleStore(
                    directory=directory,
                    protected_refs=lambda: frozenset(),
                )
                store.close()

            self.assertEqual(
                [Path(call.args[0]) for call in fsync_directory_mock.call_args_list],
                [directory],
            )

    def test_persistent_media_handle_store_requires_preexisting_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "missing-parent" / "handles"
            with self.assertRaisesRegex(
                ValueError,
                "media handle parent directory is invalid",
            ):
                PrivateWebMediaHandleStore(
                    directory=directory,
                    protected_refs=lambda: frozenset(),
                )
            self.assertFalse(directory.exists())

    def test_media_handle_store_rehydrates_only_protected_expired_refs(self) -> None:
        monotonic_now = [100.0]
        wall_now = [1_000.0]
        protected_refs: set[str] = set()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            first = PrivateWebMediaHandleStore(
                clock=lambda: monotonic_now[0],
                wall_clock=lambda: wall_now[0],
                directory=directory,
                protected_refs=lambda: frozenset(protected_refs),
            )
            protected = first.stage_media(
                "protected.jpg",
                b"\xff\xd8\xffprotected",
            )
            orphan = first.stage_media(
                "orphan.jpg",
                b"\xff\xd8\xfforphan",
            )
            protected_path = Path(first.resolve((protected,))[0].path)
            orphan_path = Path(first.resolve((orphan,))[0].path)
            protected_refs.add(protected)
            first.close()

            self.assertTrue(protected_path.exists())
            self.assertFalse(orphan_path.exists())

            monotonic_now[0] = 2_000.0
            wall_now[0] = 2_000.0
            second = PrivateWebMediaHandleStore(
                clock=lambda: monotonic_now[0],
                wall_clock=lambda: wall_now[0],
                directory=directory,
                protected_refs=lambda: frozenset(protected_refs),
            )
            try:
                self.assertEqual(
                    Path(second.resolve((protected,))[0].path),
                    protected_path,
                )
                self.assertTrue(protected_path.exists())
                self.assertFalse(orphan_path.exists())

                protected_refs.clear()
                replacement = second.stage_media(
                    "replacement.jpg",
                    b"\xff\xd8\xffreplacement",
                )
                self.assertFalse(protected_path.exists())
                with self.assertRaises(PrivateWebWriteNotAttemptedError):
                    second.resolve((protected,))
                second.discard((replacement,))
            finally:
                second.close()

    def test_media_handle_store_recovers_unprotected_incomplete_persistent_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            directory.mkdir()
            temp_ref = "media_interrupted_temp"
            temp_path = directory / f".{temp_ref}.jpg.tmp"
            temp_path.write_bytes(b"")
            orphan_ref = "media_interrupted_final"
            orphan_path = directory / f"{orphan_ref}.jpg"
            orphan_path.write_bytes(b"")

            store = PrivateWebMediaHandleStore(
                directory=directory,
                protected_refs=lambda: frozenset(),
            )
            try:
                self.assertFalse(temp_path.exists())
                self.assertFalse(orphan_path.exists())
                self.assertEqual(store._sources, {})

                ref = store.stage_media(
                    "replacement.jpg",
                    b"\xff\xd8\xffreplacement",
                )
                (source,) = store.resolve((ref,))
                self.assertTrue(Path(source.path).exists())
                self.assertFalse(
                    any(
                        entry.name.endswith(".tmp")
                        for entry in directory.iterdir()
                    )
                )
            finally:
                store.close()

    def test_media_handle_store_fails_closed_for_incomplete_protected_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            directory.mkdir()
            protected_ref = "media_protected_pending"
            protected_path = directory / f"{protected_ref}.jpg"
            protected_path.write_bytes(b"")

            with self.assertRaisesRegex(
                RuntimeError,
                "persistent media handle is invalid",
            ):
                PrivateWebMediaHandleStore(
                    directory=directory,
                    protected_refs=lambda: frozenset({protected_ref}),
                )

            self.assertTrue(protected_path.exists())

    def test_media_handle_store_rejects_ambiguous_temp_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            directory.mkdir()
            ambiguous = directory / ".media_bad!.jpg.tmp"
            ambiguous.write_bytes(b"partial")

            with self.assertRaisesRegex(
                RuntimeError,
                "persistent media handle directory is invalid",
            ):
                PrivateWebMediaHandleStore(
                    directory=directory,
                    protected_refs=lambda: frozenset(),
                )

            self.assertTrue(ambiguous.exists())

    def test_discard_after_failed_partial_close_keeps_cleanup_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            store = PrivateWebMediaHandleStore(
                directory=directory, protected_refs=lambda: frozenset()
            )
            first = store.stage_media("first.jpg", b"\xff\xd8\xfffirst")
            second = store.stage_media("second.jpg", b"\xff\xd8\xffsecond")
            first_path = Path(store.resolve((first,))[0].path)
            second_path = Path(store.resolve((second,))[0].path)
            remaining_bytes = store._staged_bytes
            real_unlink = private_web_media.os.unlink
            attempts = [0]

            def fail_second(target: str, *args: object, **kwargs: object) -> None:
                if str(target) == str(second_path):
                    attempts[0] += 1
                    raise OSError("cleanup still failing")
                real_unlink(target, *args, **kwargs)

            with patch.object(private_web_media.os, "unlink", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "cleanup still failing"):
                    store.close()
                self.assertFalse(first_path.exists())
                self.assertTrue(second_path.exists())
                with self.assertRaisesRegex(OSError, "cleanup still failing"):
                    store.discard((second,))
                self.assertGreaterEqual(attempts[0], 2)
                self.assertTrue(store._cleanup_required)
                self.assertIn(second, store._sources)
                self.assertEqual(store._staged_bytes, remaining_bytes)
                with self.assertRaises(PrivateWebWriteNotAttemptedError):
                    store.resolve((second,))

            store.close()
            self.assertFalse(second_path.exists())
            self.assertEqual(persistent_media_files(directory), [])

    def test_persistent_discard_unlink_failure_preserves_quota_and_close_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            store = PrivateWebMediaHandleStore(
                directory=directory, protected_refs=lambda: frozenset()
            )
            store._MAX_STAGED_HANDLES = 1
            data = b"\xff\xd8\xffkept"
            ref = store.stage_media("first.jpg", data)
            path = Path(store.resolve((ref,))[0].path)
            real_unlink = private_web_media.os.unlink

            def fail_unlink(target: str, *args: object, **kwargs: object) -> None:
                if str(target) == str(path):
                    raise OSError("discard unlink failed")
                real_unlink(target, *args, **kwargs)

            with patch.object(private_web_media.os, "unlink", side_effect=fail_unlink):
                with self.assertRaisesRegex(OSError, "discard unlink failed"):
                    store.discard((ref,))
                self.assertTrue(path.exists())
                self.assertEqual(store._staged_bytes, len(data))
                self.assertIn(ref, store._sources)
                self.assertTrue(store._cleanup_required)
                with self.assertRaisesRegex(RuntimeError, "cleanup required"):
                    store.stage_media("second.jpg", b"\xff\xd8\xffnew")
                with self.assertRaises(PrivateWebWriteNotAttemptedError):
                    store.resolve((ref,))
            store.close()
            self.assertFalse(path.exists())

    def test_persistent_prune_unlink_failure_cannot_lose_handle_accounting(self) -> None:
        now = [0.0]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            store = PrivateWebMediaHandleStore(
                clock=lambda: now[0],
                directory=directory,
                protected_refs=lambda: frozenset(),
            )
            data = b"\xff\xd8\xffkept"
            ref = store.stage_media("first.jpg", data)
            path = Path(store.resolve((ref,))[0].path)
            now[0] = store._STAGED_HANDLE_TTL_SECONDS + 1.0
            real_unlink = private_web_media.os.unlink

            def fail_unlink(target: str, *args: object, **kwargs: object) -> None:
                if str(target) == str(path):
                    raise OSError("prune unlink failed")
                real_unlink(target, *args, **kwargs)

            with patch.object(private_web_media.os, "unlink", side_effect=fail_unlink):
                with self.assertRaisesRegex(OSError, "prune unlink failed"):
                    store.stage_media("second.jpg", b"\xff\xd8\xffnew")
                self.assertEqual(store._staged_bytes, len(data))
                self.assertIn(ref, store._sources)
                self.assertTrue(store._cleanup_required)
            store.close()
            self.assertFalse(path.exists())

    def test_persistent_discard_directory_sync_failure_retains_accounting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "handles"
            store = PrivateWebMediaHandleStore(
                directory=directory, protected_refs=lambda: frozenset()
            )
            data = b"\xff\xd8\xffkept"
            ref = store.stage_media("first.jpg", data)
            path = Path(store.resolve((ref,))[0].path)
            with patch.object(
                private_web_media,
                "_fsync_directory",
                side_effect=OSError("discard durability failed"),
            ):
                with self.assertRaisesRegex(OSError, "discard durability failed"):
                    store.discard((ref,))
                self.assertFalse(path.exists())
                self.assertEqual(store._staged_bytes, len(data))
                self.assertIn(ref, store._sources)
                self.assertTrue(store._cleanup_required)
            store.close()
            self.assertEqual(store._staged_bytes, 0)

    def test_media_handle_store_enforces_total_byte_quota_before_writing(self) -> None:
        store = PrivateWebMediaHandleStore()
        try:
            store._MAX_STAGED_HANDLES = 10
            store._MAX_STAGED_BYTES = 7
            ref = store.stage_media("one.jpg", b"\xff\xd8\xffone")
            with self.assertRaisesRegex(ValueError, "media staging quota exceeded"):
                store.stage_media("two.jpg", b"\xff\xd8\xffx")
            self.assertEqual(len(store._sources), 1)
            store.discard((ref,))
            replacement = store.stage_media("two.jpg", b"\xff\xd8\xffx")
            self.assertIsInstance(replacement, str)
        finally:
            store.close()

    def test_media_handle_store_rejects_mismatched_or_unsafe_images(self) -> None:
        store = PrivateWebMediaHandleStore()
        try:
            with self.assertRaisesRegex(ValueError, "unsupported media image"):
                store.stage_media("photo.png", b"\xff\xd8\xffjpeg")
            with self.assertRaisesRegex(ValueError, "invalid media filename"):
                store.stage_media("../photo.jpg", b"\xff\xd8\xffjpeg")
        finally:
            store.close()

    def test_media_ref_resolver_copies_binding_map_and_preserves_order(
        self,
    ) -> None:
        detail = self.root / "detail.jpg"
        detail.write_bytes(b"detail-image")
        detail_source = PrivateWebMediaSource(path=str(detail))
        bindings = {"cover_01": self.source, "detail_02": detail_source}
        resolver = PrivateWebMediaRefResolver(bindings)

        bindings["cover_01"] = detail_source

        self.assertEqual(
            resolver.resolve(("detail_02", "cover_01")),
            (detail_source, self.source),
        )
        self.assertNotIn(str(self.root), repr(resolver))

    def test_media_ref_resolver_rejects_invalid_or_unknown_handles(
        self,
    ) -> None:
        resolver = PrivateWebMediaRefResolver({"cover_01": self.source})

        for refs in (("missing",), ("../photo",), ("cover_01", "cover_01"), ()):
            with self.subTest(refs=refs):
                with self.assertRaises(PrivateWebWriteNotAttemptedError):
                    resolver.resolve(refs)

        with self.assertRaises(ValueError):
            PrivateWebMediaRefResolver({"../photo": self.source})

    def test_invalid_local_input_never_accesses_browser_page(self) -> None:
        page = FakeMediaPage()
        stager = PrivateWebCreateMediaStager(page)

        for sources in (
            (),
            (PrivateWebMediaSource(path=str(self.root / "missing.jpg")),),
            (PrivateWebMediaSource(path=str(self.root)),),
        ):
            with self.subTest(sources=sources):
                page.calls.clear()
                with self.assertRaises(ValueError):
                    stager.stage_create_media(create_request(), sources)
                self.assertEqual(page.calls, [])

    def test_symlink_is_rejected_before_browser_access(self) -> None:
        link = self.root / "link.jpg"
        try:
            link.symlink_to(self.image)
        except OSError as exc:
            self.skipTest(type(exc).__name__)
        page = FakeMediaPage()

        with self.assertRaises(ValueError):
            PrivateWebCreateMediaStager(page).stage_create_media(
                create_request(),
                (PrivateWebMediaSource(path=str(link)),),
            )

        self.assertEqual(page.calls, [])

    def test_duplicate_basenames_are_rejected_locally(self) -> None:
        left = self.root / "left"
        right = self.root / "right"
        left.mkdir()
        right.mkdir()
        first = left / "same.jpg"
        second = right / "same.jpg"
        first.write_bytes(b"a")
        second.write_bytes(b"b")
        page = FakeMediaPage()

        with self.assertRaises(ValueError):
            PrivateWebCreateMediaStager(page).stage_create_media(
                create_request(),
                (
                    PrivateWebMediaSource(path=str(first)),
                    PrivateWebMediaSource(path=str(second)),
                ),
            )

        self.assertEqual(page.calls, [])

    def test_publish_writer_local_preparation_failures_are_not_attempted(
        self,
    ) -> None:
        missing = PrivateWebMediaSource(
            path=str(self.root / "missing.jpg")
        )
        left = self.root / "left"
        right = self.root / "right"
        left.mkdir()
        right.mkdir()
        first = left / "same.jpg"
        second = right / "same.jpg"
        first.write_bytes(b"a")
        second.write_bytes(b"b")

        for sources in (
            (missing,),
            (PrivateWebMediaSource(path=str(self.root)),),
            (
                PrivateWebMediaSource(path=str(first)),
                PrivateWebMediaSource(path=str(second)),
            ),
        ):
            with self.subTest(sources=sources):
                # The page deliberately exposes no browser methods. Reaching
                # it would fail the test instead of being mistaken for a local
                # source-validation outcome.
                writer = PrivateWebCreateMediaWriter(object())
                with self.assertRaises(
                    PrivateWebWriteNotAttemptedError
                ) as caught:
                    writer.create_ad(create_request(), sources)
                self.assertEqual(
                    caught.exception.stage,
                    "prepare_create_media",
                )

    def test_publish_writer_symlink_preparation_is_not_attempted(self) -> None:
        link = self.root / "writer-link.jpg"
        try:
            link.symlink_to(self.image)
        except OSError as exc:
            self.skipTest(type(exc).__name__)

        writer = PrivateWebCreateMediaWriter(object())
        with self.assertRaises(PrivateWebWriteNotAttemptedError) as caught:
            writer.create_ad(
                create_request(),
                (PrivateWebMediaSource(path=str(link)),),
            )

        self.assertEqual(caught.exception.stage, "prepare_create_media")

    def test_no_unproven_format_or_nonzero_size_limit_is_invented(self) -> None:
        arbitrary = self.root / "arbitrary.bin"
        arbitrary.write_bytes(b"")
        page = FakeMediaPage(after=media_snapshot("arbitrary.bin", 0))

        result = PrivateWebCreateMediaStager(page).stage_create_media(
            create_request(),
            (PrivateWebMediaSource(path=str(arbitrary)),),
        )

        self.assertEqual(result, media_snapshot("arbitrary.bin", 0))

    def test_prepared_media_finalizer_removes_private_copy(self) -> None:
        prepared = _prepare_local_media((self.source,))
        private_path = Path(prepared.paths[0])

        self.assertTrue(private_path.exists())
        self.assertNotIn(str(private_path), repr(prepared))

        del prepared
        gc.collect()

        self.assertFalse(private_path.exists())

    def test_original_path_replacement_cannot_change_prepared_bytes(self) -> None:
        original = b"validated-bytes"
        replacement = b"replaced--bytes"
        self.assertEqual(len(original), len(replacement))
        self.image.write_bytes(original)

        outer = self

        class ReplacingPage(FakeMediaPage):
            staged_path: str | None = None
            staged_bytes: bytes | None = None

            def read_create_form(self) -> PrivateWebCreateSnapshot:
                outer.image.write_bytes(replacement)
                return super().read_create_form()

            def stage_create_media(self, files: tuple[str, ...]) -> None:
                self.staged_path = files[0]
                self.staged_bytes = Path(files[0]).read_bytes()
                super().stage_create_media(files)

        page = ReplacingPage(
            after=media_snapshot("photo.jpg", len(original)),
        )

        result = PrivateWebCreateMediaStager(page).stage_create_media(
            create_request(),
            (self.source,),
        )

        self.assertEqual(result, media_snapshot("photo.jpg", len(original)))
        self.assertEqual(page.staged_bytes, original)
        self.assertIsNotNone(page.staged_path)
        assert page.staged_path is not None
        self.assertNotEqual(page.staged_path, str(self.image))
        self.assertFalse(Path(page.staged_path).exists())
        self.assertEqual(self.image.read_bytes(), replacement)

    def test_challenges_and_unknown_fail_before_file_input(self) -> None:
        for state in (
            PrivateWebEditorState.LOGIN_REQUIRED,
            PrivateWebEditorState.MFA_REQUIRED,
            PrivateWebEditorState.CAPTCHA_REQUIRED,
            PrivateWebEditorState.SECURITY_CHALLENGE,
            PrivateWebEditorState.UNKNOWN,
        ):
            with self.subTest(state=state):
                page = FakeMediaPage(before=create_snapshot(state))
                with self.assertRaises(PrivateWebPreconditionError):
                    PrivateWebCreateMediaStager(page).stage_create_media(
                        create_request(),
                        (self.source,),
                    )
                self.assertEqual(page.calls, [("read_create_form", None)])

    def test_wrong_create_target_values_fail_before_file_input(self) -> None:
        page = FakeMediaPage(before=create_snapshot(title="Drift"))

        with self.assertRaises(PrivateWebPreconditionError):
            PrivateWebCreateMediaStager(page).stage_create_media(
                create_request(),
                (self.source,),
            )

        self.assertEqual(page.calls, [("read_create_form", None)])

    def test_pre_effect_failure_stays_write_not_attempted(self) -> None:
        page = FakeMediaPage(
            stage_error=PrivateWebWriteNotAttemptedError("bind")
        )

        with self.assertRaises(PrivateWebWriteNotAttemptedError):
            PrivateWebCreateMediaStager(page).stage_create_media(
                create_request(),
                (self.source,),
            )

        self.assertEqual(
            [name for name, _value in page.calls],
            ["read_create_form", "stage_create_media"],
        )

    def test_unknown_file_effect_is_reconciled_by_exact_readback_without_retry(self) -> None:
        expected = media_snapshot("photo.jpg", self.image.stat().st_size)
        page = FakeMediaPage(
            after=expected,
            stage_error=PrivateWebMediaUnknownError("stage_create_media"),
        )

        result = PrivateWebCreateMediaStager(page).stage_create_media(
            create_request(),
            (self.source,),
        )

        self.assertEqual(result, expected)
        self.assertEqual(
            [name for name, _value in page.calls].count("stage_create_media"),
            1,
        )

    def test_divergent_readback_after_possible_effect_remains_unknown(self) -> None:
        page = FakeMediaPage(
            after=media_snapshot("other.jpg", self.image.stat().st_size),
            stage_error=PrivateWebMediaUnknownError("stage_create_media"),
        )

        with self.assertRaises(PrivateWebMediaUnknownError):
            PrivateWebCreateMediaStager(page).stage_create_media(
                create_request(),
                (self.source,),
            )

        self.assertEqual(
            [name for name, _value in page.calls].count("stage_create_media"),
            1,
        )

    def test_readback_failure_after_possible_effect_is_unknown(self) -> None:
        page = FakeMediaPage(after=RuntimeError("raw provider detail"))

        with self.assertRaises(PrivateWebMediaUnknownError) as caught:
            PrivateWebCreateMediaStager(page).stage_create_media(
                create_request(),
                (self.source,),
            )

        self.assertNotIn(str(self.image), str(caught.exception))
        self.assertNotIn("local-test-image", str(caught.exception))

    def test_paths_are_hidden_from_source_repr_and_browser_readback(self) -> None:
        expected = media_snapshot("photo.jpg", self.image.stat().st_size)
        self.assertNotIn(str(self.image), repr(self.source))
        self.assertNotIn(str(self.root), repr(expected))

    def test_create_request_remains_media_free(self) -> None:
        self.assertEqual(
            [item.name for item in fields(AdCreateRequest)],
            ["category_path", "title", "description", "price_eur"],
        )


class CdpPrivateWebMediaPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.image = self.root / "photo.jpg"
        self.image.write_bytes(b"123456")
        self.expected_create = create_snapshot()
        self.pages: list[CdpPrivateWebMediaPage] = []

    def tearDown(self) -> None:
        for page in reversed(self.pages):
            page.close()
        self.tmp.cleanup()

    def page(self, handler) -> tuple[CdpPrivateWebMediaPage, FakeClient]:
        client = FakeClient(handler)
        page = CdpPrivateWebMediaPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        page._create_bound = True
        page._last_create_snapshot = self.expected_create
        self.pages.append(page)
        return page, client

    def test_direct_invalid_local_path_never_creates_client(self) -> None:
        created = False

        def factory():
            nonlocal created
            created = True
            raise AssertionError("client must not be created")

        page = CdpPrivateWebMediaPage(
            "http://127.0.0.1:19610",
            client_factory=factory,
        )
        page._create_bound = True
        page._last_create_snapshot = self.expected_create

        with self.assertRaises(ValueError):
            page.stage_create_media((str(self.root / "missing.jpg"),))

        self.assertFalse(created)

    def test_missing_or_ambiguous_file_input_handle_is_not_attempted(self) -> None:
        def handler(method, params):
            if method == "Runtime.evaluate":
                if params.get("returnByValue") is True:
                    return {
                        "result": {
                            "type": "object",
                            "value": {
                                "state": "ready",
                                "title": "Neue Vase",
                                "description": "Beschreibung",
                                "price_amount": "12",
                            },
                        }
                    }
                return {"result": {"type": "object", "subtype": "null"}}
            raise AssertionError(method)

        page, client = self.page(handler)

        with self.assertRaises(PrivateWebCdpWriteNotAttemptedError):
            page.stage_create_media((str(self.image),))

        self.assertNotIn(
            "DOM.setFileInputFiles",
            [method for method, _params in client.calls],
        )

    def test_cdp_error_during_pre_effect_revalidation_is_not_attempted(self) -> None:
        def handler(method, params):
            if method == "Runtime.evaluate":
                raise PrivateWebCdpError("call")
            raise AssertionError(method)

        page, client = self.page(handler)

        with self.assertRaises(PrivateWebCdpWriteNotAttemptedError):
            page.stage_create_media((str(self.image),))

        self.assertNotIn(
            "DOM.setFileInputFiles",
            [method for method, _params in client.calls],
        )

    def test_exact_handle_single_shot_and_target_bound_readback(self) -> None:
        def handler(method, params):
            if method == "Runtime.evaluate":
                if params.get("returnByValue") is True:
                    self.image.write_bytes(b"654321")
                    return {
                        "result": {
                            "type": "object",
                            "value": {
                                "state": "ready",
                                "title": "Neue Vase",
                                "description": "Beschreibung",
                                "price_amount": "12",
                            },
                        }
                    }
                return {
                    "result": {
                        "type": "object",
                        "objectId": "file-input-1",
                    }
                }
            if method == "DOM.setFileInputFiles":
                return {}
            if method == "Runtime.callFunctionOn":
                self.assertEqual(params.get("objectId"), "file-input-1")
                return {
                    "result": {
                        "type": "object",
                        "value": {
                            "state": "ready",
                            "files": [
                                {
                                    "name": "photo.jpg",
                                    "size_bytes": self.image.stat().st_size,
                                }
                            ],
                        },
                    }
                }
            if method == "Runtime.releaseObject":
                return {}
            raise AssertionError(method)

        page, client = self.page(handler)

        page.stage_create_media((str(self.image),))
        result = page.read_create_media()

        self.assertEqual(
            result,
            media_snapshot("photo.jpg", self.image.stat().st_size),
        )
        calls = [
            params
            for method, params in client.calls
            if method == "DOM.setFileInputFiles"
        ]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["objectId"], "file-input-1")
        selected_paths = calls[0]["files"]
        self.assertIsInstance(selected_paths, list)
        self.assertEqual(len(selected_paths), 1)
        selected_path = Path(selected_paths[0])
        self.assertNotEqual(selected_path, self.image)
        self.assertEqual(selected_path.name, self.image.name)
        self.assertEqual(selected_path.read_bytes(), b"123456")
        self.assertEqual(self.image.read_bytes(), b"654321")
        self.assertTrue(selected_path.exists())

        with self.assertRaises(PrivateWebCdpWriteNotAttemptedError):
            page.stage_create_media((str(self.image),))
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "DOM.setFileInputFiles"
            ),
            1,
        )

        page.close()
        self.assertFalse(selected_path.exists())
        self.assertIn(
            ("Runtime.releaseObject", {"objectId": "file-input-1"}),
            client.calls,
        )

    def test_failure_after_file_input_dispatch_is_unknown_and_no_retry(self) -> None:
        def handler(method, params):
            if method == "Runtime.evaluate":
                if params.get("returnByValue") is True:
                    return {
                        "result": {
                            "type": "object",
                            "value": {
                                "state": "ready",
                                "title": "Neue Vase",
                                "description": "Beschreibung",
                                "price_amount": "12",
                            },
                        }
                    }
                return {
                    "result": {
                        "type": "object",
                        "objectId": "file-input-1",
                    }
                }
            if method == "DOM.setFileInputFiles":
                raise PrivateWebCdpError("call")
            if method == "Runtime.releaseObject":
                return {}
            raise AssertionError(method)

        page, client = self.page(handler)

        with self.assertRaises(PrivateWebMediaUnknownError):
            page.stage_create_media((str(self.image),))
        with self.assertRaises(PrivateWebCdpWriteNotAttemptedError):
            page.stage_create_media((str(self.image),))

        self.assertEqual(
            [method for method, _params in client.calls].count(
                "DOM.setFileInputFiles"
            ),
            1,
        )

    def test_ambiguous_dispatch_can_be_reconciled_without_second_file_input(self) -> None:
        def handler(method, params):
            if method == "Runtime.evaluate":
                if params.get("returnByValue") is True:
                    return {
                        "result": {
                            "type": "object",
                            "value": {
                                "state": "ready",
                                "title": "Neue Vase",
                                "description": "Beschreibung",
                                "price_amount": "12",
                            },
                        }
                    }
                return {
                    "result": {
                        "type": "object",
                        "objectId": "file-input-1",
                    }
                }
            if method == "DOM.setFileInputFiles":
                raise PrivateWebCdpError("call")
            if method == "Runtime.callFunctionOn":
                return {
                    "result": {
                        "type": "object",
                        "value": {
                            "state": "ready",
                            "files": [
                                {
                                    "name": "photo.jpg",
                                    "size_bytes": self.image.stat().st_size,
                                }
                            ],
                        },
                    }
                }
            if method == "Runtime.releaseObject":
                return {}
            raise AssertionError(method)

        page, client = self.page(handler)

        result = PrivateWebCreateMediaStager(page).stage_create_media(
            create_request(),
            (PrivateWebMediaSource(path=str(self.image)),),
        )

        self.assertEqual(
            result,
            media_snapshot("photo.jpg", self.image.stat().st_size),
        )
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "DOM.setFileInputFiles"
            ),
            1,
        )

    def test_media_staging_cannot_use_existing_create_publish_path(self) -> None:
        def handler(method, params):
            if method == "Runtime.evaluate":
                if params.get("returnByValue") is True:
                    return {
                        "result": {
                            "type": "object",
                            "value": {
                                "state": "ready",
                                "title": "Neue Vase",
                                "description": "Beschreibung",
                                "price_amount": "12",
                            },
                        }
                    }
                return {
                    "result": {
                        "type": "object",
                        "objectId": "file-input-1",
                    }
                }
            if method == "DOM.setFileInputFiles":
                return {}
            if method == "Runtime.releaseObject":
                return {}
            raise AssertionError(method)

        page, client = self.page(handler)
        page.stage_create_media((str(self.image),))

        with self.assertRaises(PrivateWebCdpWriteNotAttemptedError):
            page.submit_create()

        self.assertNotIn(
            "Input.dispatchMouseEvent",
            [method for method, _params in client.calls],
        )

    def test_readback_challenge_or_target_drift_is_not_confirmation(self) -> None:
        for state in (
            "login_required",
            "mfa_required",
            "captcha_required",
            "security_challenge",
            "unknown",
        ):
            with self.subTest(state=state):
                def handler(method, params, *, state=state):
                    if method == "Runtime.evaluate":
                        if params.get("returnByValue") is True:
                            return {
                                "result": {
                                    "type": "object",
                                    "value": {
                                        "state": "ready",
                                        "title": "Neue Vase",
                                        "description": "Beschreibung",
                                        "price_amount": "12",
                                    },
                                }
                            }
                        return {
                            "result": {
                                "type": "object",
                                "objectId": "file-input-1",
                            }
                        }
                    if method == "DOM.setFileInputFiles":
                        return {}
                    if method == "Runtime.callFunctionOn":
                        return {
                            "result": {
                                "type": "object",
                                "value": {"state": state},
                            }
                        }
                    raise AssertionError(method)

                page, _client = self.page(handler)

                with self.assertRaises(PrivateWebMediaUnknownError):
                    PrivateWebCreateMediaStager(page).stage_create_media(
                        create_request(),
                        (PrivateWebMediaSource(path=str(self.image)),),
                    )

    def test_invalid_browser_metadata_fails_closed(self) -> None:
        snapshot = CdpPrivateWebMediaPage._media_snapshot_from_value(
            {
                "state": "ready",
                "files": [
                    {"name": "/tmp/path.jpg", "size_bytes": 1},
                ],
            }
        )
        self.assertEqual(snapshot.state, PrivateWebEditorState.UNKNOWN)
        self.assertEqual(snapshot.files, ())

    def test_javascript_parses_and_contains_no_dom_click(self) -> None:
        page, _client = self.page(lambda method, params: {})
        sources = (
            page._file_input_handle_expression(self.expected_create),
            f"({page._media_readback_function(self.expected_create)})",
            f"({page._media_create_activation_function(
                self.expected_create,
                media_snapshot("photo.jpg", self.image.stat().st_size),
            )})",
        )
        for source in sources:
            check = subprocess.run(
                ["node", "--check", "-"],
                input=source,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(
                check.returncode,
                0,
                check.stdout + check.stderr,
            )
            self.assertNotIn(".click(", source)

    def test_file_control_primitive_is_confined_to_media_cdp_module(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src" / "mark_api"
        files = []
        for path in source_root.rglob("*.py"):
            if "DOM.setFileInputFiles" in path.read_text(encoding="utf-8"):
                files.append(path.name)

        self.assertEqual(files, ["private_web_cdp_media.py"])
        source = (
            source_root / "private_web_cdp_media.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn(".click(", source)
        self.assertNotIn("Input.dispatchMouseEvent", source)


if __name__ == "__main__":
    unittest.main()