from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from importlib import metadata
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from mark_api.adapters.management import MANAGEMENT_URL
from mark_api.domain import (
    AdCreateRequest,
    AdSnapshot,
    DeleteApproval,
    LifecycleState,
    MediaPostReadStatus,
    OperationOutcome,
    OperationReceipt,
)
from mark_api.orchestrator import SafeWriteOrchestrator
from mark_api.ports import WriteNotAttemptedError
from mark_api.private_web import (
    PrivateWebCreateSnapshot,
    PrivateWebDeleteSnapshot,
    PrivateWebEditorSnapshot,
    PrivateWebEditorState,
    PrivateWebStateSnapshot,
    PrivateWebSubmitUnknownError,
    PrivateWebWriteNotAttemptedError,
)
from mark_api.private_web_runtime import (
    PrivateWebContentRuntime,
    PrivateWebInventoryRuntime,
    PrivateWebMediaCreateRuntime,
    PrivateWebMediaCreateService,
    PrivateWebRuntimeClosedError,
    PrivateWebRuntimeDependencyError,
    PrivateWebRuntimeSetupError,
    PrivateWebWriteApiRuntime,
    _NoRedirectManagementTransport,
    _RejectManagementRedirectHandler,
    _pending_dashboard_media_refs,
    build_private_web_content_runtime,
    build_private_web_inventory_runtime,
    build_private_web_media_create_runtime,
    build_private_web_write_api_runtime,
    compose_private_web_write_api_runtime,
    require_private_web_runtime_dependency,
)
from mark_api.private_web_media import (
    PrivateWebCreateMediaSnapshot,
    PrivateWebMediaFileSnapshot,
    PrivateWebMediaPersistenceSnapshot,
    PrivateWebMediaHandleStore,
    PrivateWebMediaRefResolver,
    PrivateWebMediaSource,
)
from mark_api.results import ReadResult, ReadStatus
from mark_api.storage import SnapshotStore
from mark_api.write_api import (
    WriteApiAccess,
    WriteCapability,
    acquire_write_api_store_lock,
)


AD_ID = "3524046688"
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def owner_snapshot(
    *,
    ad_id: str = AD_ID,
    title: str = "Inventory title",
    description: str | None = None,
) -> AdSnapshot:
    return AdSnapshot(
        ad_id=ad_id,
        observed_at=NOW,
        source="kleinanzeigen-management",
        lifecycle_state=LifecycleState.ACTIVE,
        title=title,
        description=description,
        views=7,
        watch_count=2,
        reply_count=1,
    )


class OwnerReader:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = 0

    def read_ads(self):
        self.calls += 1
        return self.result


class SequenceReader:
    def __init__(self, *results) -> None:
        if not results:
            raise ValueError("at least one read result is required")
        self._results = results
        self.calls = 0

    def read_ads(self):
        if self.calls >= len(self._results):
            raise AssertionError("unexpected extra read")
        result = self._results[self.calls]
        self.calls += 1
        return result


class MediaPersistenceVerifier:
    def __init__(self, *results) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, tuple[tuple[str, bytes], ...]]] = []

    def verify_media(
        self,
        ad_id: str,
        expected_sources: tuple[PrivateWebMediaSource, ...],
        *,
        timeout_seconds: float,
    ):
        if timeout_seconds <= 0:
            raise AssertionError("media verifier timeout must be positive")
        self.calls.append(
            (
                ad_id,
                tuple(
                    (Path(source.path).name, Path(source.path).read_bytes())
                    for source in expected_sources
                ),
            )
        )
        if self._results:
            result = self._results.pop(0)
            if isinstance(result, ReadResult):
                return result
            exact_match = bool(result)
        else:
            exact_match = True
        return ReadResult.success_nonempty(
            PrivateWebMediaPersistenceSnapshot(
                ad_id=ad_id,
                observed_at=NOW,
                source="authoritative-media-test",
                exact_match=exact_match,
            )
        )


class SharedPage:
    def __init__(self, state: dict[str, str], events: list[tuple]) -> None:
        self._state = state
        self._events = events
        self._ad_id: str | None = None
        self._closed = False

    def open_editor(self, ad_id: str) -> None:
        self._events.append(("open_editor", ad_id))
        self._ad_id = ad_id

    def read_editor(self) -> PrivateWebEditorSnapshot:
        self._events.append(("read_editor", self._ad_id))
        return PrivateWebEditorSnapshot(
            state=PrivateWebEditorState.READY,
            ad_id=self._ad_id,
            title=self._state["title"],
            description=self._state["description"],
        )

    def replace_title(self, value: str) -> None:
        self._events.append(("replace_title", value))
        self._state["title"] = value

    def replace_description(self, value: str) -> None:
        self._events.append(("replace_description", value))
        self._state["description"] = value

    def submit(self) -> None:
        self._events.append(("submit", self._ad_id))

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._events.append(("close", self._ad_id))


class CreatePage:
    def __init__(
        self,
        events: list[tuple],
        *,
        close_error: Exception | None = None,
        submit_unknown: bool = False,
    ) -> None:
        self._events = events
        self._category_path: tuple[str, ...] | None = None
        self._title = ""
        self._description = ""
        self._price = ""
        self._closed = False
        self._close_error = close_error
        self._submit_unknown = submit_unknown

    def open_create_form(self, category_path: tuple[str, ...]) -> None:
        self._category_path = category_path
        self._events.append(("open_create_form", category_path))

    def read_create_form(self) -> PrivateWebCreateSnapshot:
        self._events.append(("read_create_form", self._category_path))
        return PrivateWebCreateSnapshot(
            state=PrivateWebEditorState.READY,
            title=self._title,
            description=self._description,
            price_amount=self._price,
        )

    def replace_create_title(self, value: str) -> None:
        self._events.append(("replace_create_title", value))
        self._title = value

    def replace_create_description(self, value: str) -> None:
        self._events.append(("replace_create_description", value))
        self._description = value

    def replace_create_price(self, value: str) -> None:
        self._events.append(("replace_create_price", value))
        self._price = value

    def submit_create(self) -> None:
        self._events.append(("submit_create", self._category_path))
        if self._submit_unknown:
            raise PrivateWebSubmitUnknownError("create_submit")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._events.append(("close_create", self._category_path))
        if self._close_error is not None:
            raise self._close_error


class MediaCreatePage:
    def __init__(
        self,
        events: list[tuple],
        *,
        submit_unknown: bool = False,
        reconcile_unknown: bool = False,
    ) -> None:
        self._events = events
        self._category_path: tuple[str, ...] | None = None
        self._title = ""
        self._description = ""
        self._price = ""
        self._media: PrivateWebCreateMediaSnapshot | None = None
        self._submit_unknown = submit_unknown
        self._reconcile_unknown = reconcile_unknown
        self._unsettled = False
        self._closed = False

    def open_create_form(self, category_path: tuple[str, ...]) -> None:
        self._category_path = category_path
        self._events.append(("media_open_create_form", category_path))

    def read_create_form(self) -> PrivateWebCreateSnapshot:
        self._events.append(("media_read_create_form", self._category_path))
        return PrivateWebCreateSnapshot(
            state=PrivateWebEditorState.READY,
            title=self._title,
            description=self._description,
            price_amount=self._price,
        )

    def replace_create_title(self, value: str) -> None:
        self._title = value
        self._events.append(("media_replace_title", value))

    def replace_create_description(self, value: str) -> None:
        self._description = value
        self._events.append(("media_replace_description", value))

    def replace_create_price(self, value: str) -> None:
        self._price = value
        self._events.append(("media_replace_price", value))

    def submit_create(self) -> None:
        raise AssertionError("media runtime must not use ordinary create submit")

    def stage_create_media(self, files: tuple[str, ...]) -> None:
        snapshots = tuple(
            PrivateWebMediaFileSnapshot(
                name=Path(item).name,
                size_bytes=Path(item).stat().st_size,
            )
            for item in files
        )
        self._media = PrivateWebCreateMediaSnapshot(
            state=PrivateWebEditorState.READY,
            files=snapshots,
        )
        self._events.append(("media_stage", snapshots))

    def read_create_media(self) -> PrivateWebCreateMediaSnapshot:
        self._events.append(("media_readback", None))
        assert self._media is not None
        return self._media

    def submit_create_media(
        self,
        expected: PrivateWebCreateMediaSnapshot,
    ) -> None:
        self._events.append(("media_submit", expected.files))
        if self._submit_unknown:
            self._unsettled = True
            raise PrivateWebSubmitUnknownError("create_media_submit_settle")

    def reconcile_create_media_submit(self) -> None:
        self._events.append(("media_reconcile", None))
        if not self._unsettled:
            raise AssertionError("only unsettled media submits are reconcilable")
        if self._reconcile_unknown:
            raise PrivateWebSubmitUnknownError("create_media_submit_settle")
        self._unsettled = False

    def close(self) -> None:
        self._events.append(("media_close", self._unsettled))
        if self._unsettled:
            raise PrivateWebSubmitUnknownError(
                "create_media_submit_unsettled"
            )
        self._closed = True


class LifecyclePage:
    def __init__(
        self,
        lifecycle: dict[str, LifecycleState],
        events: list[tuple],
        *,
        close_error: Exception | None = None,
        submit_unknown: bool = False,
    ) -> None:
        self._lifecycle = lifecycle
        self._events = events
        self._ad_id: str | None = None
        self._closed = False
        self._close_error = close_error
        self._submit_unknown = submit_unknown

    def open_state_controls(self, ad_id: str) -> None:
        self._events.append(("open_state_controls", ad_id))
        self._ad_id = ad_id

    def read_state_controls(self) -> PrivateWebStateSnapshot:
        self._events.append(("read_state_controls", self._ad_id))
        return PrivateWebStateSnapshot(
            state=PrivateWebEditorState.READY,
            ad_id=self._ad_id,
            lifecycle_state=self._lifecycle["state"],
        )

    def submit_state(self, state: LifecycleState) -> None:
        self._events.append(("submit_state", self._ad_id, state))
        if self._submit_unknown:
            raise PrivateWebSubmitUnknownError("state_submit")
        self._lifecycle["state"] = state

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._events.append(("close_state", self._ad_id))
        if self._close_error is not None:
            raise self._close_error


class DeletePage:
    def __init__(
        self,
        events: list[tuple],
        *,
        close_error: Exception | None = None,
        submit_unknown: bool = False,
    ) -> None:
        self._events = events
        self._ad_id: str | None = None
        self._closed = False
        self._close_error = close_error
        self._submit_unknown = submit_unknown

    def open_delete_controls(self, ad_id: str) -> None:
        self._events.append(("open_delete_controls", ad_id))
        self._ad_id = ad_id

    def read_delete_controls(self) -> PrivateWebDeleteSnapshot:
        self._events.append(("read_delete_controls", self._ad_id))
        return PrivateWebDeleteSnapshot(
            state=PrivateWebEditorState.READY,
            ad_id=self._ad_id,
        )

    def open_delete_confirmation(self) -> None:
        self._events.append(("open_delete_confirmation", self._ad_id))

    def read_delete_confirmation(self) -> PrivateWebDeleteSnapshot:
        self._events.append(("read_delete_confirmation", self._ad_id))
        return PrivateWebDeleteSnapshot(
            state=PrivateWebEditorState.READY,
            ad_id=self._ad_id,
        )

    def submit_delete(self) -> None:
        self._events.append(("submit_delete", self._ad_id))
        if self._submit_unknown:
            raise PrivateWebSubmitUnknownError("delete_submit")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._events.append(("close_delete", self._ad_id))
        if self._close_error is not None:
            raise self._close_error


class PrivateWebMediaCreateRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.image = Path(self.tmp.name) / "photo.jpg"
        self.image.write_bytes(b"123456")
        self.request = AdCreateRequest(
            category_path=(
                "Haus & Garten",
                "Dekoration",
                "Weitere Dekoration",
            ),
            title="Neue Vase",
            description="Beschreibung",
            price_eur=12,
        )
        self.sources = (PrivateWebMediaSource(path=str(self.image)),)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_success_closes_page_without_pending_submit(self) -> None:
        events: list[tuple] = []
        pages: list[MediaCreatePage] = []

        def page_factory() -> MediaCreatePage:
            page = MediaCreatePage(events)
            pages.append(page)
            return page

        runtime = PrivateWebMediaCreateRuntime(page_factory=page_factory)

        runtime.create_ad(self.request, self.sources)

        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0]._closed)
        self.assertEqual(
            [event[0] for event in events].count("media_submit"),
            1,
        )
        runtime.close()

    def test_unknown_submit_is_retained_and_blocks_duplicate_and_close(
        self,
    ) -> None:
        events: list[tuple] = []
        page_calls = 0
        page = MediaCreatePage(events, submit_unknown=True)

        def page_factory() -> MediaCreatePage:
            nonlocal page_calls
            page_calls += 1
            return page

        runtime = PrivateWebMediaCreateRuntime(page_factory=page_factory)

        with self.assertRaises(PrivateWebSubmitUnknownError):
            runtime.create_ad(self.request, self.sources)

        self.assertEqual(page_calls, 1)
        self.assertFalse(page._closed)
        self.assertTrue(page._unsettled)

        with self.assertRaises(PrivateWebRuntimeSetupError) as duplicate:
            runtime.create_ad(self.request, self.sources)
        self.assertIsInstance(duplicate.exception, WriteNotAttemptedError)
        self.assertEqual(page_calls, 1)
        self.assertEqual(
            [event[0] for event in events].count("media_submit"),
            1,
        )

        with self.assertRaises(PrivateWebSubmitUnknownError) as close_caught:
            runtime.close()
        self.assertEqual(
            close_caught.exception.stage,
            "media_runtime_close_unsettled",
        )
        self.assertFalse(page._closed)

        runtime.reconcile_media_submit()
        self.assertFalse(page._unsettled)
        self.assertTrue(page._closed)
        runtime.close()

    def test_context_manager_preserves_submit_unknown_stage(self) -> None:
        events: list[tuple] = []
        page = MediaCreatePage(events, submit_unknown=True)
        runtime = PrivateWebMediaCreateRuntime(page_factory=lambda: page)

        with self.assertRaises(PrivateWebSubmitUnknownError) as caught:
            with runtime:
                runtime.create_ad(self.request, self.sources)

        self.assertEqual(
            caught.exception.stage,
            "create_media_submit_settle",
        )
        self.assertTrue(page._unsettled)
        self.assertFalse(page._closed)

        runtime.reconcile_media_submit()
        runtime.close()

    def test_writer_unknown_without_pending_page_blocks_retry(self) -> None:
        events: list[tuple] = []
        pages: list[MediaCreatePage] = []

        def page_factory() -> MediaCreatePage:
            page = MediaCreatePage(events)
            pages.append(page)
            return page

        runtime = PrivateWebMediaCreateRuntime(page_factory=page_factory)

        with patch(
            "mark_api.private_web_runtime.PrivateWebCreateMediaWriter.create_ad",
            side_effect=PrivateWebSubmitUnknownError("submit_create_media"),
        ):
            with self.assertRaises(PrivateWebSubmitUnknownError) as caught:
                runtime.create_ad(self.request, self.sources)

        self.assertEqual(caught.exception.stage, "submit_create_media")
        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0]._closed)
        with self.assertRaises(PrivateWebRuntimeSetupError) as duplicate:
            runtime.create_ad(self.request, self.sources)
        self.assertIsInstance(duplicate.exception, WriteNotAttemptedError)
        self.assertEqual(len(pages), 1)

        with self.assertRaises(PrivateWebRuntimeSetupError):
            runtime.reconcile_media_submit()

        # No unresolved page-owned media remains, so runtime shutdown itself is
        # safe even though the non-retryable create fence remains armed.
        runtime.close()

    def test_cancellation_after_possible_submit_retains_page_and_fence(
        self,
    ) -> None:
        events: list[tuple] = []
        page = MediaCreatePage(events)
        runtime = PrivateWebMediaCreateRuntime(page_factory=lambda: page)

        def cancel_after_possible_submit(writer, request, sources) -> None:
            page._unsettled = True
            raise KeyboardInterrupt("cancelled after possible submit")

        with patch(
            "mark_api.private_web_runtime.PrivateWebCreateMediaWriter.create_ad",
            new=cancel_after_possible_submit,
        ):
            with self.assertRaises(KeyboardInterrupt) as caught:
                runtime.create_ad(self.request, self.sources)

        self.assertEqual(
            caught.exception.args,
            ("cancelled after possible submit",),
        )
        self.assertTrue(page._unsettled)
        self.assertFalse(page._closed)

        with self.assertRaises(PrivateWebRuntimeSetupError):
            runtime.create_ad(self.request, self.sources)
        with self.assertRaises(PrivateWebSubmitUnknownError) as close_caught:
            runtime.close()
        self.assertEqual(
            close_caught.exception.stage,
            "media_runtime_close_unsettled",
        )

        runtime.reconcile_media_submit()
        self.assertFalse(page._unsettled)
        self.assertTrue(page._closed)
        runtime.close()

    def test_bind_create_writer_requires_explicit_media_sources(self) -> None:
        runtime = PrivateWebMediaCreateRuntime(
            page_factory=lambda: MediaCreatePage([])
        )

        with self.assertRaises(TypeError):
            runtime.bind_create_writer(object(), self.sources)
        with self.assertRaises(TypeError):
            runtime.bind_create_writer(self.request, list(self.sources))
        with self.assertRaises(ValueError):
            runtime.bind_create_writer(self.request, ())
        with self.assertRaises(TypeError):
            runtime.bind_create_writer(self.request, (object(),))

        runtime.close()

    def test_bound_create_writer_is_request_bound_and_one_shot(self) -> None:
        events: list[tuple] = []
        pages: list[MediaCreatePage] = []

        def page_factory() -> MediaCreatePage:
            page = MediaCreatePage(events)
            pages.append(page)
            return page

        runtime = PrivateWebMediaCreateRuntime(page_factory=page_factory)
        writer = runtime.bind_create_writer(self.request, self.sources)
        mismatched = AdCreateRequest(
            category_path=self.request.category_path,
            title="Andere Vase",
            description=self.request.description,
            price_eur=self.request.price_eur,
        )

        with self.assertRaises(PrivateWebRuntimeSetupError) as mismatch:
            writer.create_ad(mismatched)
        self.assertIsInstance(mismatch.exception, WriteNotAttemptedError)
        self.assertEqual(len(pages), 0)

        writer.create_ad(self.request)
        self.assertEqual(len(pages), 1)
        self.assertEqual(
            [event[0] for event in events].count("media_submit"),
            1,
        )

        with self.assertRaises(PrivateWebRuntimeSetupError) as duplicate:
            writer.create_ad(self.request)
        self.assertIsInstance(duplicate.exception, WriteNotAttemptedError)
        self.assertEqual(len(pages), 1)
        self.assertEqual(
            [event[0] for event in events].count("media_submit"),
            1,
        )

        runtime.close()

    def test_local_media_preparation_failure_is_precondition_failed(
        self,
    ) -> None:
        events: list[tuple] = []
        page_calls = 0

        def page_factory() -> MediaCreatePage:
            nonlocal page_calls
            page_calls += 1
            return MediaCreatePage(events)

        runtime = PrivateWebMediaCreateRuntime(page_factory=page_factory)
        missing_sources = (
            PrivateWebMediaSource(
                path=str(Path(self.tmp.name) / "missing.jpg")
            ),
        )
        writer = runtime.bind_create_writer(
            self.request,
            missing_sources,
        )
        primary = SequenceReader(ReadResult.success_empty(()))
        confirmation = SequenceReader(ReadResult.success_empty(()))
        content_calls: list[str] = []

        receipt = SafeWriteOrchestrator(writes_enabled=True).create(
            request=self.request,
            reader=primary,
            confirmation_reader=confirmation,
            writer=writer,
            content_reader_factory=lambda ad_id: (
                content_calls.append(ad_id)
                or OwnerReader(ReadResult.success_empty(()))
            ),
        )

        self.assertEqual(
            receipt.outcome,
            OperationOutcome.PRECONDITION_FAILED,
        )
        self.assertTrue(receipt.writer_invoked)
        self.assertEqual(
            receipt.writer_error,
            "PrivateWebWriteNotAttemptedError",
        )
        self.assertIsNone(receipt.post_read_status)
        self.assertIsNone(receipt.confirmation_post_read_status)
        self.assertIsNone(receipt.content_post_read_status)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(confirmation.calls, 1)
        self.assertEqual(content_calls, [])
        self.assertEqual(page_calls, 1)
        self.assertEqual(
            [event[0] for event in events].count("media_submit"),
            0,
        )
        self.assertNotIn(
            "media_open_create_form",
            [event[0] for event in events],
        )

        runtime.close()

    def test_bound_writer_reuses_safe_create_confirmation_and_persistence(
        self,
    ) -> None:
        events: list[tuple] = []
        page = MediaCreatePage(events)
        runtime = PrivateWebMediaCreateRuntime(page_factory=lambda: page)
        writer = runtime.bind_create_writer(self.request, self.sources)
        created_id = "4000000001"
        created_inventory = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
        )
        created_content = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
            description=self.request.description,
        )
        primary = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty((created_inventory,)),
        )
        confirmation = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty((created_inventory,)),
        )
        content_calls: list[str] = []

        def content_reader_factory(ad_id: str) -> OwnerReader:
            content_calls.append(ad_id)
            return OwnerReader(
                ReadResult.success_nonempty((created_content,))
            )

        db_path = Path(self.tmp.name) / "media-orchestration.sqlite"
        store = SnapshotStore(db_path)
        receipt = SafeWriteOrchestrator(
            store=store,
            writes_enabled=True,
        ).create(
            request=self.request,
            reader=primary,
            confirmation_reader=confirmation,
            writer=writer,
            content_reader_factory=content_reader_factory,
            authorization_by="test-owner",
            authorization_reference="media-create-test",
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(receipt.created_ad_id, created_id)
        self.assertIsNone(receipt.writer_error)
        self.assertEqual(primary.calls, 2)
        self.assertEqual(confirmation.calls, 2)
        self.assertEqual(content_calls, [created_id])
        self.assertEqual(
            [event[0] for event in events].count("media_submit"),
            1,
        )
        with sqlite3.connect(db_path) as connection:
            persisted = connection.execute(
                "SELECT COUNT(*) FROM create_operation_receipts"
            ).fetchone()
        self.assertEqual(persisted, (1,))
        runtime.close()

    def test_media_create_service_freezes_selected_bytes_before_pre_reads(
        self,
    ) -> None:
        events: list[tuple] = []
        original = b"selected-before-owner-read"
        self.image.write_bytes(original)

        class CapturingPage(MediaCreatePage):
            def stage_create_media(self, files: tuple[str, ...]) -> None:
                events.append(
                    ("media_bytes", tuple(Path(item).read_bytes() for item in files))
                )
                super().stage_create_media(files)

        runtime = PrivateWebMediaCreateRuntime(
            page_factory=lambda: CapturingPage(events)
        )
        resolver = PrivateWebMediaRefResolver({"cover_01": self.sources[0]})
        created_id = "4000000010"
        created_inventory = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
        )
        created_content = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
            description=self.request.description,
        )
        outer = self

        class MutatingPrimaryReader(SequenceReader):
            def read_ads(self):
                if self.calls == 0:
                    outer.image.write_bytes(b"changed-during-owner-pre-read")
                return super().read_ads()

        primary = MutatingPrimaryReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty((created_inventory,)),
        )
        confirmation = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty((created_inventory,)),
        )
        db_path = Path(self.tmp.name) / "media-service-freeze.sqlite"
        media_verifier = MediaPersistenceVerifier()
        service = PrivateWebMediaCreateService(
            runtime=runtime,
            resolver=resolver,
            reader=primary,
            confirmation_reader=confirmation,
            content_reader_factory=lambda _ad_id: OwnerReader(
                ReadResult.success_nonempty((created_content,))
            ),
            media_persistence_verifier=media_verifier,
            store=SnapshotStore(db_path),
            writes_enabled=True,
        )

        receipt = service.create_with_media(
            self.request,
            ("cover_01",),
            authorization_by="test-owner",
            authorization_reference="media-freeze-test",
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(receipt.created_ad_id, created_id)
        self.assertEqual(receipt.authorization_by, "test-owner")
        self.assertEqual(
            receipt.authorization_reference,
            "media-freeze-test",
        )
        self.assertTrue(receipt.media_persistence_confirmed)
        self.assertEqual(
            receipt.media_post_read_status,
            MediaPostReadStatus.CONFIRMED,
        )
        self.assertEqual(
            media_verifier.calls,
            [(created_id, (("photo.jpg", original),))],
        )
        self.assertFalse(runtime.reconciliation_required)
        self.assertIn(("media_bytes", (original,)), events)
        self.assertEqual(primary.calls, 2)
        self.assertEqual(confirmation.calls, 2)
        self.assertEqual(
            [event[0] for event in events].count("media_submit"),
            1,
        )
        with sqlite3.connect(db_path) as connection:
            persisted = connection.execute(
                "SELECT COUNT(*) FROM create_operation_receipts"
            ).fetchone()
            checkpoints = connection.execute(
                "SELECT COUNT(*) FROM create_operation_checkpoints"
            ).fetchone()
        self.assertEqual(persisted, (1,))
        self.assertEqual(checkpoints, (1,))
        runtime.close()

    def test_media_create_service_checkpoint_survives_verifier_baseexception(
        self,
    ) -> None:
        events: list[tuple] = []
        runtime = PrivateWebMediaCreateRuntime(
            page_factory=lambda: MediaCreatePage(events)
        )
        created_id = "4000000025"
        created_inventory = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
        )
        created_content = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
            description=self.request.description,
        )
        calls: list[tuple[str, float]] = []

        class InterruptingVerifier:
            def verify_media(
                self,
                ad_id: str,
                expected_sources: tuple[PrivateWebMediaSource, ...],
                *,
                timeout_seconds: float,
            ):
                calls.append((ad_id, timeout_seconds))
                raise KeyboardInterrupt("simulated process-level interruption")

        db_path = Path(self.tmp.name) / "media-checkpoint-interrupt.sqlite"
        service = PrivateWebMediaCreateService(
            runtime=runtime,
            resolver=PrivateWebMediaRefResolver(
                {"cover_01": self.sources[0]}
            ),
            reader=SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_nonempty((created_inventory,)),
            ),
            confirmation_reader=SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_nonempty((created_inventory,)),
            ),
            content_reader_factory=lambda _ad_id: OwnerReader(
                ReadResult.success_nonempty((created_content,))
            ),
            media_persistence_verifier=InterruptingVerifier(),
            store=SnapshotStore(db_path),
            writes_enabled=True,
        )

        with self.assertRaises(KeyboardInterrupt):
            service.create_with_media(self.request, ("cover_01",))

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], created_id)
        self.assertGreater(calls[0][1], 0)
        with sqlite3.connect(db_path) as connection:
            checkpoint = connection.execute(
                """
                SELECT checkpoint_kind, created_ad_id, outcome,
                       content_post_read_status, writer_invoked
                FROM create_operation_checkpoints
                """
            ).fetchone()
            final_count = connection.execute(
                "SELECT COUNT(*) FROM create_operation_receipts"
            ).fetchone()
        self.assertEqual(
            checkpoint,
            (
                "before_media_post_read",
                created_id,
                "confirmed",
                "success_nonempty",
                1,
            ),
        )
        self.assertEqual(final_count, (0,))
        self.assertFalse(runtime.reconciliation_required)
        runtime.close()

    def test_media_create_service_completion_time_includes_media_post_read(
        self,
    ) -> None:
        events: list[tuple] = []
        runtime = PrivateWebMediaCreateRuntime(
            page_factory=lambda: MediaCreatePage(events)
        )
        created_id = "4000000024"
        created_inventory = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
        )
        created_content = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
            description=self.request.description,
        )
        times = iter(
            (
                NOW,
                NOW + timedelta(seconds=1),
                NOW + timedelta(seconds=2),
                NOW + timedelta(seconds=3),
            )
        )
        db_path = Path(self.tmp.name) / "media-completion-time.sqlite"
        verifier = MediaPersistenceVerifier()
        service = PrivateWebMediaCreateService(
            runtime=runtime,
            resolver=PrivateWebMediaRefResolver(
                {"cover_01": self.sources[0]}
            ),
            reader=SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_nonempty((created_inventory,)),
            ),
            confirmation_reader=SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_nonempty((created_inventory,)),
            ),
            content_reader_factory=lambda _ad_id: OwnerReader(
                ReadResult.success_nonempty((created_content,))
            ),
            media_persistence_verifier=verifier,
            store=SnapshotStore(db_path),
            writes_enabled=True,
            clock=lambda: next(times),
        )

        receipt = service.create_with_media(self.request, ("cover_01",))

        self.assertEqual(receipt.started_at, NOW + timedelta(seconds=1))
        self.assertEqual(receipt.completed_at, NOW + timedelta(seconds=3))
        self.assertTrue(receipt.media_persistence_confirmed)
        self.assertEqual(len(verifier.calls), 1)
        with sqlite3.connect(db_path) as connection:
            persisted_completed_at = connection.execute(
                "SELECT completed_at FROM create_operation_receipts"
            ).fetchone()
        self.assertEqual(
            persisted_completed_at,
            ((NOW + timedelta(seconds=3)).isoformat(),),
        )
        runtime.close()

    def test_media_create_service_consumes_product_handle_after_stabilizing(self) -> None:
        events: list[tuple] = []
        runtime = PrivateWebMediaCreateRuntime(
            page_factory=lambda: MediaCreatePage(events)
        )
        created_id = "4000000025"
        created_inventory = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
        )
        created_content = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
            description=self.request.description,
        )
        now = [100.0]
        handles = PrivateWebMediaHandleStore(clock=lambda: now[0])
        handles._STAGED_HANDLE_TTL_SECONDS = 10
        ref = handles.stage_media("photo.jpg", b"\xff\xd8\xffjpeg")
        service = PrivateWebMediaCreateService(
            runtime=runtime,
            resolver=handles,
            reader=SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_nonempty((created_inventory,)),
            ),
            confirmation_reader=SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_nonempty((created_inventory,)),
            ),
            content_reader_factory=lambda _ad_id: OwnerReader(
                ReadResult.success_nonempty((created_content,))
            ),
            media_persistence_verifier=MediaPersistenceVerifier(),
            writes_enabled=True,
        )

        now[0] = 110.0
        receipt = service.create_with_media(self.request, (ref,))

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertTrue(receipt.media_persistence_confirmed)
        with self.assertRaises(PrivateWebWriteNotAttemptedError):
            handles.resolve((ref,))
        handles.close()
        runtime.close()

    def test_media_create_service_unknown_ref_short_circuits_reads(self) -> None:
        page_calls = 0

        def page_factory() -> MediaCreatePage:
            nonlocal page_calls
            page_calls += 1
            return MediaCreatePage([])

        runtime = PrivateWebMediaCreateRuntime(page_factory=page_factory)
        primary = OwnerReader(ReadResult.success_empty(()))
        confirmation = OwnerReader(ReadResult.success_empty(()))
        media_verifier = MediaPersistenceVerifier()
        service = PrivateWebMediaCreateService(
            runtime=runtime,
            resolver=PrivateWebMediaRefResolver({"cover_01": self.sources[0]}),
            reader=primary,
            confirmation_reader=confirmation,
            content_reader_factory=lambda _ad_id: OwnerReader(
                ReadResult.success_empty(())
            ),
            media_persistence_verifier=media_verifier,
            writes_enabled=True,
        )

        receipt = service.create_with_media(self.request, ("missing",))

        self.assertEqual(receipt.outcome, OperationOutcome.PRECONDITION_FAILED)
        self.assertEqual(receipt.pre_read_status, "media_refs_unavailable")
        self.assertEqual(receipt.confirmation_pre_read_status, "not_read")
        self.assertFalse(receipt.writer_invoked)
        self.assertEqual(primary.calls, 0)
        self.assertEqual(confirmation.calls, 0)
        self.assertEqual(media_verifier.calls, [])
        self.assertEqual(
            receipt.media_post_read_status,
            MediaPostReadStatus.NOT_READ,
        )
        self.assertFalse(receipt.media_persistence_confirmed)
        self.assertEqual(page_calls, 0)
        runtime.close()

    def test_media_create_service_disabled_gate_skips_local_source_read(
        self,
    ) -> None:
        missing = PrivateWebMediaSource(
            path=str(Path(self.tmp.name) / "missing.jpg")
        )
        runtime = PrivateWebMediaCreateRuntime(
            page_factory=lambda: MediaCreatePage([])
        )
        media_verifier = MediaPersistenceVerifier()
        service = PrivateWebMediaCreateService(
            runtime=runtime,
            resolver=PrivateWebMediaRefResolver({"cover_01": missing}),
            reader=OwnerReader(ReadResult.success_empty(())),
            confirmation_reader=OwnerReader(ReadResult.success_empty(())),
            content_reader_factory=lambda _ad_id: OwnerReader(
                ReadResult.success_empty(())
            ),
            media_persistence_verifier=media_verifier,
            writes_enabled=False,
        )

        receipt = service.create_with_media(self.request, ("cover_01",))

        self.assertEqual(receipt.outcome, OperationOutcome.PRECONDITION_FAILED)
        self.assertEqual(receipt.pre_read_status, "writes_disabled")
        self.assertFalse(receipt.writer_invoked)
        self.assertEqual(
            receipt.media_post_read_status,
            MediaPostReadStatus.NOT_READ,
        )
        self.assertFalse(receipt.media_persistence_confirmed)
        self.assertEqual(media_verifier.calls, [])
        runtime.close()

    def test_media_create_service_serializes_whole_orchestrator(self) -> None:
        service = PrivateWebMediaCreateService(
            runtime=PrivateWebMediaCreateRuntime(
                page_factory=lambda: MediaCreatePage([])
            ),
            resolver=PrivateWebMediaRefResolver({"cover_01": self.sources[0]}),
            reader=OwnerReader(ReadResult.success_empty(())),
            confirmation_reader=OwnerReader(ReadResult.success_empty(())),
            content_reader_factory=lambda _ad_id: OwnerReader(
                ReadResult.success_empty(())
            ),
            media_persistence_verifier=MediaPersistenceVerifier(),
            writes_enabled=True,
        )
        first_entered = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        calls: list[str | None] = []
        errors: list[BaseException] = []

        original_writes = service._writes

        class BlockingWrites:
            def create(self, **kwargs):
                calls.append(kwargs.get("authorization_reference"))
                if len(calls) == 1:
                    first_entered.set()
                    if not release_first.wait(timeout=2):
                        raise AssertionError("first create was not released")
                return original_writes.create(**kwargs)

        service._writes = BlockingWrites()

        def invoke(reference: str, started: threading.Event | None = None) -> None:
            if started is not None:
                started.set()
            try:
                service.create_with_media(
                    self.request,
                    ("cover_01",),
                    authorization_reference=reference,
                )
            except BaseException as exc:
                errors.append(exc)

        first = threading.Thread(target=invoke, args=("first",))
        second = threading.Thread(target=invoke, args=("second", second_started))
        first.start()
        self.assertTrue(first_entered.wait(timeout=2))
        second.start()
        self.assertTrue(second_started.wait(timeout=2))
        self.assertEqual(calls, ["first"])

        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(errors, [])

    def test_media_create_service_mismatch_is_fail_closed_without_runtime_fence(
        self,
    ) -> None:
        original = b"stable-media-for-verification"
        self.image.write_bytes(original)
        runtime = PrivateWebMediaCreateRuntime(
            page_factory=lambda: MediaCreatePage([])
        )
        created_id = "4000000020"
        created_inventory = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
        )
        created_content = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
            description=self.request.description,
        )
        verifier = MediaPersistenceVerifier(False)
        service = PrivateWebMediaCreateService(
            runtime=runtime,
            resolver=PrivateWebMediaRefResolver(
                {"cover_01": self.sources[0]}
            ),
            reader=SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_nonempty((created_inventory,)),
            ),
            confirmation_reader=SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_nonempty((created_inventory,)),
            ),
            content_reader_factory=lambda _ad_id: OwnerReader(
                ReadResult.success_nonempty((created_content,))
            ),
            media_persistence_verifier=verifier,
            writes_enabled=True,
        )

        receipt = service.create_with_media(self.request, ("cover_01",))

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(receipt.created_ad_id, created_id)
        self.assertFalse(receipt.media_persistence_confirmed)
        self.assertEqual(
            receipt.media_post_read_status,
            MediaPostReadStatus.MISMATCH,
        )
        self.assertFalse(runtime.reconciliation_required)
        self.assertEqual(
            verifier.calls,
            [(created_id, (("photo.jpg", original),))],
        )
        runtime.close()

    def test_media_create_service_read_failure_is_unknown_without_runtime_fence(
        self,
    ) -> None:
        runtime = PrivateWebMediaCreateRuntime(
            page_factory=lambda: MediaCreatePage([])
        )
        created_id = "4000000021"
        created_inventory = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
        )
        created_content = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
            description=self.request.description,
        )
        verifier = MediaPersistenceVerifier(
            ReadResult.failure(ReadStatus.TRANSPORT_ERROR),
        )
        service = PrivateWebMediaCreateService(
            runtime=runtime,
            resolver=PrivateWebMediaRefResolver(
                {"cover_01": self.sources[0]}
            ),
            reader=SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_nonempty((created_inventory,)),
            ),
            confirmation_reader=SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_nonempty((created_inventory,)),
            ),
            content_reader_factory=lambda _ad_id: OwnerReader(
                ReadResult.success_nonempty((created_content,))
            ),
            media_persistence_verifier=verifier,
            writes_enabled=True,
        )

        receipt = service.create_with_media(self.request, ("cover_01",))

        self.assertFalse(receipt.media_persistence_confirmed)
        self.assertEqual(
            receipt.media_post_read_status,
            MediaPostReadStatus.UNKNOWN,
        )
        self.assertFalse(runtime.reconciliation_required)
        self.assertEqual(len(verifier.calls), 1)
        runtime.close()

    def test_media_create_service_without_verifier_stays_unconfirmed(
        self,
    ) -> None:
        runtime = PrivateWebMediaCreateRuntime(
            page_factory=lambda: MediaCreatePage([])
        )
        created_id = "4000000022"
        created_inventory = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
        )
        created_content = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
            description=self.request.description,
        )
        service = PrivateWebMediaCreateService(
            runtime=runtime,
            resolver=PrivateWebMediaRefResolver(
                {"cover_01": self.sources[0]}
            ),
            reader=SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_nonempty((created_inventory,)),
            ),
            confirmation_reader=SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_nonempty((created_inventory,)),
            ),
            content_reader_factory=lambda _ad_id: OwnerReader(
                ReadResult.success_nonempty((created_content,))
            ),
            writes_enabled=True,
        )

        receipt = service.create_with_media(self.request, ("cover_01",))

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertFalse(receipt.media_persistence_confirmed)
        self.assertEqual(
            receipt.media_post_read_status,
            MediaPostReadStatus.VERIFIER_UNAVAILABLE,
        )
        self.assertFalse(runtime.reconciliation_required)
        runtime.close()

    def test_media_create_service_preserves_runtime_submit_unknown_fence(
        self,
    ) -> None:
        events: list[tuple] = []
        page = MediaCreatePage(events, submit_unknown=True)
        runtime = PrivateWebMediaCreateRuntime(page_factory=lambda: page)
        primary = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_empty(()),
            ReadResult.success_empty(()),
        )
        confirmation = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_empty(()),
            ReadResult.success_empty(()),
        )
        content_calls: list[str] = []
        media_verifier = MediaPersistenceVerifier()
        service = PrivateWebMediaCreateService(
            runtime=runtime,
            resolver=PrivateWebMediaRefResolver({"cover_01": self.sources[0]}),
            reader=primary,
            confirmation_reader=confirmation,
            content_reader_factory=lambda ad_id: (
                content_calls.append(ad_id)
                or OwnerReader(ReadResult.success_empty(()))
            ),
            media_persistence_verifier=media_verifier,
            writes_enabled=True,
        )

        first = service.create_with_media(self.request, ("cover_01",))
        second = service.create_with_media(self.request, ("cover_01",))

        self.assertEqual(first.outcome, OperationOutcome.AMBIGUOUS)
        self.assertEqual(
            first.writer_error,
            "PrivateWebSubmitUnknownError",
        )
        self.assertEqual(
            second.outcome,
            OperationOutcome.PRECONDITION_FAILED,
        )
        self.assertEqual(
            second.writer_error,
            "PrivateWebRuntimeSetupError",
        )
        self.assertEqual(primary.calls, 3)
        self.assertEqual(confirmation.calls, 3)
        self.assertEqual(content_calls, [])
        self.assertEqual(media_verifier.calls, [])
        self.assertEqual(
            first.media_post_read_status,
            MediaPostReadStatus.NOT_READ,
        )
        self.assertEqual(
            second.media_post_read_status,
            MediaPostReadStatus.NOT_READ,
        )
        self.assertFalse(first.media_persistence_confirmed)
        self.assertFalse(second.media_persistence_confirmed)
        self.assertEqual(
            [event[0] for event in events].count("media_submit"),
            1,
        )

        runtime.reconcile_media_submit()
        self.assertTrue(page._closed)
        runtime.close()

    def test_service_server_confirmation_does_not_release_submit_unknown_fence(
        self,
    ) -> None:
        events: list[tuple] = []
        original = b"stable-media-submit-unknown"
        self.image.write_bytes(original)
        page = MediaCreatePage(events, submit_unknown=True)
        runtime = PrivateWebMediaCreateRuntime(page_factory=lambda: page)
        created_id = "4000000023"
        created_inventory = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
        )
        created_content = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
            description=self.request.description,
        )
        verifier = MediaPersistenceVerifier()
        service = PrivateWebMediaCreateService(
            runtime=runtime,
            resolver=PrivateWebMediaRefResolver(
                {"cover_01": self.sources[0]}
            ),
            reader=SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_nonempty((created_inventory,)),
            ),
            confirmation_reader=SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_nonempty((created_inventory,)),
            ),
            content_reader_factory=lambda _ad_id: OwnerReader(
                ReadResult.success_nonempty((created_content,))
            ),
            media_persistence_verifier=verifier,
            writes_enabled=True,
        )

        receipt = service.create_with_media(self.request, ("cover_01",))

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(receipt.created_ad_id, created_id)
        self.assertEqual(
            receipt.writer_error,
            "PrivateWebSubmitUnknownError",
        )
        self.assertTrue(receipt.media_persistence_confirmed)
        self.assertEqual(
            receipt.media_post_read_status,
            MediaPostReadStatus.CONFIRMED,
        )
        self.assertEqual(
            verifier.calls,
            [(created_id, (("photo.jpg", original),))],
        )
        self.assertTrue(runtime.reconciliation_required)
        self.assertEqual(
            [event[0] for event in events].count("media_submit"),
            1,
        )

        runtime.reconcile_media_submit()
        self.assertFalse(runtime.reconciliation_required)
        runtime.close()

    def test_submit_unknown_can_confirm_without_releasing_runtime_fence(
        self,
    ) -> None:
        events: list[tuple] = []
        page = MediaCreatePage(events, submit_unknown=True)
        runtime = PrivateWebMediaCreateRuntime(page_factory=lambda: page)
        writer = runtime.bind_create_writer(self.request, self.sources)
        created_id = "4000000002"
        created_inventory = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
        )
        created_content = owner_snapshot(
            ad_id=created_id,
            title=self.request.title,
            description=self.request.description,
        )
        primary = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty((created_inventory,)),
        )
        confirmation = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty((created_inventory,)),
        )

        receipt = SafeWriteOrchestrator(writes_enabled=True).create(
            request=self.request,
            reader=primary,
            confirmation_reader=confirmation,
            writer=writer,
            content_reader_factory=lambda ad_id: OwnerReader(
                ReadResult.success_nonempty((created_content,))
            ),
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(receipt.created_ad_id, created_id)
        self.assertEqual(
            receipt.writer_error,
            "PrivateWebSubmitUnknownError",
        )
        self.assertTrue(page._unsettled)
        self.assertFalse(page._closed)
        self.assertEqual(
            [event[0] for event in events].count("media_submit"),
            1,
        )

        second_primary = SequenceReader(
            ReadResult.success_nonempty((created_inventory,))
        )
        second_confirmation = SequenceReader(
            ReadResult.success_nonempty((created_inventory,))
        )
        second_writer = runtime.bind_create_writer(
            self.request,
            self.sources,
        )
        second = SafeWriteOrchestrator(writes_enabled=True).create(
            request=self.request,
            reader=second_primary,
            confirmation_reader=second_confirmation,
            writer=second_writer,
            content_reader_factory=lambda ad_id: OwnerReader(
                ReadResult.success_empty(())
            ),
        )

        self.assertEqual(
            second.outcome,
            OperationOutcome.PRECONDITION_FAILED,
        )
        self.assertEqual(
            second.writer_error,
            "PrivateWebRuntimeSetupError",
        )
        self.assertEqual(second_primary.calls, 1)
        self.assertEqual(second_confirmation.calls, 1)
        self.assertEqual(
            [event[0] for event in events].count("media_submit"),
            1,
        )

        runtime.reconcile_media_submit()
        self.assertTrue(page._closed)
        runtime.close()

    def test_submit_unknown_stays_ambiguous_when_readbacks_do_not_confirm(
        self,
    ) -> None:
        events: list[tuple] = []
        page = MediaCreatePage(events, submit_unknown=True)
        runtime = PrivateWebMediaCreateRuntime(page_factory=lambda: page)
        writer = runtime.bind_create_writer(self.request, self.sources)
        primary = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_empty(()),
        )
        confirmation = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_empty(()),
        )
        content_calls: list[str] = []

        receipt = SafeWriteOrchestrator(writes_enabled=True).create(
            request=self.request,
            reader=primary,
            confirmation_reader=confirmation,
            writer=writer,
            content_reader_factory=lambda ad_id: (
                content_calls.append(ad_id)
                or OwnerReader(ReadResult.success_empty(()))
            ),
        )

        self.assertEqual(receipt.outcome, OperationOutcome.AMBIGUOUS)
        self.assertIsNone(receipt.created_ad_id)
        self.assertEqual(
            receipt.writer_error,
            "PrivateWebSubmitUnknownError",
        )
        self.assertEqual(content_calls, [])
        self.assertTrue(page._unsettled)
        self.assertFalse(page._closed)
        self.assertEqual(
            [event[0] for event in events].count("media_submit"),
            1,
        )

        runtime.reconcile_media_submit()
        self.assertTrue(page._closed)
        runtime.close()

    def test_concurrent_create_calls_are_serialized(self) -> None:
        events: list[tuple] = []
        pages: list[MediaCreatePage] = []
        start = threading.Barrier(3)
        writer_barrier = threading.Barrier(2)
        counter_lock = threading.Lock()
        active_writers = 0
        max_active_writers = 0
        errors: list[BaseException] = []

        def page_factory() -> MediaCreatePage:
            page = MediaCreatePage(events)
            pages.append(page)
            return page

        def fake_create(writer, request, sources) -> None:
            nonlocal active_writers, max_active_writers
            with counter_lock:
                active_writers += 1
                max_active_writers = max(max_active_writers, active_writers)
            try:
                try:
                    writer_barrier.wait(timeout=0.2)
                except threading.BrokenBarrierError:
                    pass
            finally:
                with counter_lock:
                    active_writers -= 1

        runtime = PrivateWebMediaCreateRuntime(page_factory=page_factory)

        def invoke() -> None:
            try:
                start.wait(timeout=1)
                runtime.create_ad(self.request, self.sources)
            except BaseException as exc:
                errors.append(exc)

        with patch(
            "mark_api.private_web_runtime.PrivateWebCreateMediaWriter.create_ad",
            new=fake_create,
        ):
            threads = [threading.Thread(target=invoke) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.wait(timeout=1)
            for thread in threads:
                thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(pages), 2)
        self.assertEqual(max_active_writers, 1)
        runtime.close()

    def test_close_waits_for_inflight_create(self) -> None:
        events: list[tuple] = []
        writer_entered = threading.Event()
        release_writer = threading.Event()
        close_started = threading.Event()
        close_done = threading.Event()
        errors: list[BaseException] = []
        runtime = PrivateWebMediaCreateRuntime(
            page_factory=lambda: MediaCreatePage(events)
        )

        def fake_create(writer, request, sources) -> None:
            writer_entered.set()
            if not release_writer.wait(timeout=2):
                raise AssertionError("writer release was not signaled")

        def invoke_create() -> None:
            try:
                runtime.create_ad(self.request, self.sources)
            except BaseException as exc:
                errors.append(exc)

        def invoke_close() -> None:
            try:
                close_started.set()
                runtime.close()
                close_done.set()
            except BaseException as exc:
                errors.append(exc)

        with patch(
            "mark_api.private_web_runtime.PrivateWebCreateMediaWriter.create_ad",
            new=fake_create,
        ):
            create_thread = threading.Thread(target=invoke_create)
            create_thread.start()
            self.assertTrue(writer_entered.wait(timeout=1))

            close_thread = threading.Thread(target=invoke_close)
            close_thread.start()
            self.assertTrue(close_started.wait(timeout=1))
            self.assertFalse(close_done.wait(timeout=0.1))

            release_writer.set()
            create_thread.join(timeout=2)
            close_thread.join(timeout=2)

        self.assertFalse(create_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(close_done.is_set())
        with self.assertRaises(PrivateWebRuntimeClosedError):
            runtime.create_ad(self.request, self.sources)

    def test_reconcile_cleanup_blocks_next_create_until_complete(self) -> None:
        events: list[tuple] = []
        settled_close_entered = threading.Event()
        release_settled_close = threading.Event()
        second_page_created = threading.Event()
        pages: list[MediaCreatePage] = []
        errors: list[BaseException] = []

        class BlockingReconcilePage(MediaCreatePage):
            def __init__(self) -> None:
                super().__init__(events, submit_unknown=True)
                self._reconciled = False

            def reconcile_create_media_submit(self) -> None:
                super().reconcile_create_media_submit()
                self._reconciled = True

            def close(self) -> None:
                if self._reconciled and not self._unsettled:
                    settled_close_entered.set()
                    if not release_settled_close.wait(timeout=2):
                        raise AssertionError("reconcile close release was not signaled")
                super().close()

        first_page = BlockingReconcilePage()

        def page_factory() -> MediaCreatePage:
            if not pages:
                pages.append(first_page)
                return first_page
            page = MediaCreatePage(events)
            pages.append(page)
            second_page_created.set()
            return page

        runtime = PrivateWebMediaCreateRuntime(page_factory=page_factory)
        with self.assertRaises(PrivateWebSubmitUnknownError):
            runtime.create_ad(self.request, self.sources)

        def invoke_reconcile() -> None:
            try:
                runtime.reconcile_media_submit()
            except BaseException as exc:
                errors.append(exc)

        def invoke_second_create() -> None:
            try:
                runtime.create_ad(self.request, self.sources)
            except BaseException as exc:
                errors.append(exc)

        reconcile_thread = threading.Thread(target=invoke_reconcile)
        reconcile_thread.start()
        self.assertTrue(settled_close_entered.wait(timeout=1))

        create_thread = threading.Thread(target=invoke_second_create)
        create_thread.start()
        self.assertFalse(second_page_created.wait(timeout=0.1))

        release_settled_close.set()
        reconcile_thread.join(timeout=2)
        create_thread.join(timeout=2)

        self.assertFalse(reconcile_thread.is_alive())
        self.assertFalse(create_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(second_page_created.is_set())
        self.assertEqual(len(pages), 2)
        runtime.close()

    def test_failed_reconciliation_keeps_pending_page_owned(self) -> None:
        events: list[tuple] = []
        page = MediaCreatePage(
            events,
            submit_unknown=True,
            reconcile_unknown=True,
        )
        runtime = PrivateWebMediaCreateRuntime(page_factory=lambda: page)

        with self.assertRaises(PrivateWebSubmitUnknownError):
            runtime.create_ad(self.request, self.sources)
        with self.assertRaises(PrivateWebSubmitUnknownError):
            runtime.reconcile_media_submit()

        self.assertTrue(page._unsettled)
        self.assertFalse(page._closed)
        with self.assertRaises(PrivateWebRuntimeSetupError):
            runtime.create_ad(self.request, self.sources)
        with self.assertRaises(PrivateWebSubmitUnknownError):
            runtime.close()

        page._reconcile_unknown = False
        runtime.reconcile_media_submit()
        self.assertTrue(page._closed)
        runtime.close()

    def test_reconcile_without_pending_is_write_not_attempted(self) -> None:
        runtime = PrivateWebMediaCreateRuntime(
            page_factory=lambda: MediaCreatePage([])
        )

        with self.assertRaises(PrivateWebRuntimeSetupError) as caught:
            runtime.reconcile_media_submit()

        self.assertIsInstance(caught.exception, WriteNotAttemptedError)

    def test_page_setup_failure_is_write_not_attempted(self) -> None:
        runtime = PrivateWebMediaCreateRuntime(
            page_factory=lambda: (_ for _ in ()).throw(
                OSError("page setup failed")
            )
        )

        with self.assertRaises(PrivateWebRuntimeSetupError) as caught:
            runtime.create_ad(self.request, self.sources)

        self.assertIsInstance(caught.exception, WriteNotAttemptedError)

    def test_closed_runtime_rejects_create_and_reconcile(self) -> None:
        page_calls = 0

        def page_factory() -> MediaCreatePage:
            nonlocal page_calls
            page_calls += 1
            return MediaCreatePage([])

        runtime = PrivateWebMediaCreateRuntime(page_factory=page_factory)
        runtime.close()

        with self.assertRaises(PrivateWebRuntimeClosedError):
            runtime.create_ad(self.request, self.sources)
        with self.assertRaises(PrivateWebRuntimeClosedError):
            runtime.reconcile_media_submit()
        self.assertEqual(page_calls, 0)

    def test_builder_uses_media_page_factory_only(self) -> None:
        events: list[tuple] = []
        page = MediaCreatePage(events)

        with (
            patch(
                "mark_api.private_web_runtime."
                "require_private_web_runtime_dependency"
            ) as dependency,
            patch(
                "mark_api.private_web_runtime."
                "CdpPrivateWebMediaPage.from_port",
                return_value=page,
            ) as from_port,
        ):
            runtime = build_private_web_media_create_runtime(
                cdp_port=19610,
                timeout_seconds=7.0,
            )
            runtime.create_ad(self.request, self.sources)

        dependency.assert_called_once_with()
        from_port.assert_called_once_with(19610, timeout_seconds=7.0)
        runtime.close()


class PrivateWebContentRuntimeTests(unittest.TestCase):
    def test_create_writer_uses_fresh_page_per_call_and_closes_each_page(self) -> None:
        owner = OwnerReader(ReadResult.success_empty(()))
        events: list[tuple] = []
        pages: list[CreatePage] = []

        def page_factory() -> CreatePage:
            page = CreatePage(events)
            pages.append(page)
            return page

        runtime = PrivateWebContentRuntime(
            owner_reader=owner,
            page_factory=page_factory,
            close_runtime=lambda: None,
        )
        request = AdCreateRequest(
            category_path=("Haus & Garten", "Dekoration", "Weitere Dekoration"),
            title="Neue Vase",
            description="Beschreibung",
            price_eur=12,
        )

        runtime.create_writer.create_ad(request)
        runtime.create_writer.create_ad(request)

        self.assertEqual(len(pages), 2)
        self.assertEqual(
            [event for event in events if event[0] == "submit_create"],
            [
                ("submit_create", request.category_path),
                ("submit_create", request.category_path),
            ],
        )
        self.assertEqual(
            [event for event in events if event[0] == "close_create"],
            [
                ("close_create", request.category_path),
                ("close_create", request.category_path),
            ],
        )

    def test_create_writer_cleanup_failure_does_not_replace_submit_unknown(self) -> None:
        events: list[tuple] = []
        runtime = PrivateWebContentRuntime(
            owner_reader=OwnerReader(ReadResult.success_empty(())),
            page_factory=lambda: CreatePage(
                events,
                close_error=RuntimeError("cleanup failed"),
                submit_unknown=True,
            ),
            close_runtime=lambda: None,
        )
        request = AdCreateRequest(
            category_path=("Haus & Garten", "Dekoration", "Weitere Dekoration"),
            title="Neue Vase",
            description="Beschreibung",
            price_eur=12,
        )

        with self.assertRaises(PrivateWebSubmitUnknownError):
            runtime.create_writer.create_ad(request)

        self.assertEqual(
            [event for event in events if event[0] == "submit_create"],
            [("submit_create", request.category_path)],
        )
        self.assertEqual(
            [event for event in events if event[0] == "close_create"],
            [("close_create", request.category_path)],
        )

    def test_create_writer_page_setup_failure_is_write_not_attempted(self) -> None:
        runtime = PrivateWebContentRuntime(
            owner_reader=OwnerReader(ReadResult.success_empty(())),
            page_factory=lambda: (_ for _ in ()).throw(
                OSError("page setup failed")
            ),
            close_runtime=lambda: None,
        )
        request = AdCreateRequest(
            category_path=("Haus & Garten", "Dekoration"),
            title="Neue Vase",
            description="Beschreibung",
            price_eur=12,
        )

        with self.assertRaises(PrivateWebRuntimeSetupError) as caught:
            runtime.create_writer.create_ad(request)

        self.assertIsInstance(caught.exception, WriteNotAttemptedError)

    def test_inventory_read_uses_owner_reader_without_page_creation(self) -> None:
        owner = OwnerReader(
            ReadResult.success_nonempty((owner_snapshot(),))
        )
        page_calls = 0

        def page_factory():
            nonlocal page_calls
            page_calls += 1
            raise AssertionError("inventory read must not create a page")

        runtime = PrivateWebContentRuntime(
            owner_reader=owner,
            page_factory=page_factory,
            close_runtime=lambda: None,
        )

        result = runtime.read_inventory()

        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        self.assertEqual(result.value, (owner_snapshot(),))
        self.assertEqual(owner.calls, 1)
        self.assertEqual(page_calls, 0)

    def test_target_reader_enriches_only_exact_owner_target_and_closes_page(self) -> None:
        owner = OwnerReader(
            ReadResult.success_nonempty(
                (
                    owner_snapshot(ad_id="9999999999", title="Other"),
                    owner_snapshot(),
                )
            )
        )
        state = {
            "title": "Fresh title",
            "description": "Fresh description",
        }
        events: list[tuple] = []
        runtime_closed: list[bool] = []

        runtime = PrivateWebContentRuntime(
            owner_reader=owner,
            page_factory=lambda: SharedPage(state, events),
            close_runtime=lambda: runtime_closed.append(True),
        )

        result = runtime.content_reader_for(AD_ID).read_ads()

        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        self.assertEqual(owner.calls, 1)
        self.assertEqual(result.value[0].ad_id, "9999999999")
        target = result.value[1]
        self.assertEqual(target.ad_id, AD_ID)
        self.assertEqual(target.title, "Fresh title")
        self.assertEqual(target.description, "Fresh description")
        self.assertEqual(
            target.source,
            "kleinanzeigen-management+private-web",
        )
        self.assertEqual(
            events,
            [
                ("open_editor", AD_ID),
                ("read_editor", AD_ID),
                ("close", AD_ID),
            ],
        )

        runtime.close()
        runtime.close()
        self.assertEqual(runtime_closed, [True])

    def test_content_writer_uses_fresh_page_per_call_and_closes_each_page(self) -> None:
        owner = OwnerReader(ReadResult.success_nonempty((owner_snapshot(),)))
        state = {
            "title": "Original",
            "description": "Description",
        }
        events: list[tuple] = []
        pages: list[SharedPage] = []

        def page_factory() -> SharedPage:
            page = SharedPage(state, events)
            pages.append(page)
            return page

        runtime = PrivateWebContentRuntime(
            owner_reader=owner,
            page_factory=page_factory,
            close_runtime=lambda: None,
        )

        runtime.content_writer.update_content(AD_ID, title="First")
        runtime.content_writer.update_content(AD_ID, title="Second")

        self.assertEqual(len(pages), 2)
        self.assertEqual(state["title"], "Second")
        self.assertEqual(
            [event for event in events if event[0] == "submit"],
            [("submit", AD_ID), ("submit", AD_ID)],
        )
        self.assertEqual(
            [event for event in events if event[0] == "close"],
            [("close", AD_ID), ("close", AD_ID)],
        )


    def test_state_writer_uses_fresh_page_per_call_and_closes_each_page(self) -> None:
        owner = OwnerReader(ReadResult.success_nonempty((owner_snapshot(),)))
        lifecycle = {"state": LifecycleState.ACTIVE}
        events: list[tuple] = []
        pages: list[LifecyclePage] = []

        def page_factory() -> LifecyclePage:
            page = LifecyclePage(lifecycle, events)
            pages.append(page)
            return page

        runtime = PrivateWebContentRuntime(
            owner_reader=owner,
            page_factory=page_factory,
            close_runtime=lambda: None,
        )

        runtime.state_writer.set_state(AD_ID, LifecycleState.PAUSED)
        runtime.state_writer.set_state(AD_ID, LifecycleState.ACTIVE)

        self.assertEqual(len(pages), 2)
        self.assertEqual(lifecycle["state"], LifecycleState.ACTIVE)
        self.assertEqual(
            [event for event in events if event[0] == "submit_state"],
            [
                ("submit_state", AD_ID, LifecycleState.PAUSED),
                ("submit_state", AD_ID, LifecycleState.ACTIVE),
            ],
        )
        self.assertEqual(
            [event for event in events if event[0] == "close_state"],
            [("close_state", AD_ID), ("close_state", AD_ID)],
        )

    def test_state_writer_cleanup_failure_does_not_replace_submit_unknown(self) -> None:
        owner = OwnerReader(ReadResult.success_nonempty((owner_snapshot(),)))
        lifecycle = {"state": LifecycleState.ACTIVE}
        events: list[tuple] = []

        runtime = PrivateWebContentRuntime(
            owner_reader=owner,
            page_factory=lambda: LifecyclePage(
                lifecycle,
                events,
                close_error=RuntimeError("cleanup failed"),
                submit_unknown=True,
            ),
            close_runtime=lambda: None,
        )

        with self.assertRaises(PrivateWebSubmitUnknownError):
            runtime.state_writer.set_state(AD_ID, LifecycleState.PAUSED)

        self.assertEqual(
            [event for event in events if event[0] == "submit_state"],
            [("submit_state", AD_ID, LifecycleState.PAUSED)],
        )
        self.assertEqual(
            [event for event in events if event[0] == "close_state"],
            [("close_state", AD_ID)],
        )

    def test_delete_writer_uses_fresh_page_per_call_and_closes_each_page(self) -> None:
        owner = OwnerReader(ReadResult.success_nonempty((owner_snapshot(),)))
        events: list[tuple] = []
        pages: list[DeletePage] = []

        def page_factory() -> DeletePage:
            page = DeletePage(events)
            pages.append(page)
            return page

        runtime = PrivateWebContentRuntime(
            owner_reader=owner,
            page_factory=page_factory,
            close_runtime=lambda: None,
        )

        runtime.delete_writer.delete_ad(AD_ID)
        runtime.delete_writer.delete_ad(AD_ID)

        self.assertEqual(len(pages), 2)
        self.assertEqual(
            [event for event in events if event[0] == "submit_delete"],
            [("submit_delete", AD_ID), ("submit_delete", AD_ID)],
        )
        self.assertEqual(
            [event for event in events if event[0] == "close_delete"],
            [("close_delete", AD_ID), ("close_delete", AD_ID)],
        )

    def test_delete_writer_cleanup_failure_does_not_replace_submit_unknown(self) -> None:
        owner = OwnerReader(ReadResult.success_nonempty((owner_snapshot(),)))
        events: list[tuple] = []
        runtime = PrivateWebContentRuntime(
            owner_reader=owner,
            page_factory=lambda: DeletePage(
                events,
                close_error=RuntimeError("cleanup failed"),
                submit_unknown=True,
            ),
            close_runtime=lambda: None,
        )

        with self.assertRaises(PrivateWebSubmitUnknownError):
            runtime.delete_writer.delete_ad(AD_ID)

        self.assertEqual(
            [event for event in events if event[0] == "submit_delete"],
            [("submit_delete", AD_ID)],
        )
        self.assertEqual(
            [event for event in events if event[0] == "close_delete"],
            [("close_delete", AD_ID)],
        )

    def test_delete_writer_page_setup_failure_is_write_not_attempted(self) -> None:
        runtime = PrivateWebContentRuntime(
            owner_reader=OwnerReader(ReadResult.success_empty(())),
            page_factory=lambda: (_ for _ in ()).throw(
                OSError("page setup failed")
            ),
            close_runtime=lambda: None,
        )

        with self.assertRaises(PrivateWebRuntimeSetupError) as caught:
            runtime.delete_writer.delete_ad(AD_ID)

        self.assertIsInstance(caught.exception, WriteNotAttemptedError)

    def test_delete_setup_failure_is_non_ambiguous_in_orchestrator(self) -> None:
        owner = OwnerReader(ReadResult.success_nonempty((owner_snapshot(),)))
        confirmation = OwnerReader(ReadResult.success_empty(()))
        page_calls = 0

        def page_factory():
            nonlocal page_calls
            page_calls += 1
            raise OSError("page setup failed")

        runtime = PrivateWebContentRuntime(
            owner_reader=owner,
            page_factory=page_factory,
            close_runtime=lambda: None,
        )

        receipt = SafeWriteOrchestrator(writes_enabled=True).delete(
            ad_id=AD_ID,
            approval=DeleteApproval(ad_id=AD_ID, approved_by="test-owner"),
            reader=owner,
            writer=runtime.delete_writer,
            confirmation_reader=confirmation,
        )

        self.assertEqual(receipt.outcome, OperationOutcome.PRECONDITION_FAILED)
        self.assertTrue(receipt.writer_invoked)
        self.assertEqual(receipt.writer_error, "PrivateWebRuntimeSetupError")
        self.assertIsNone(receipt.post_read_status)
        self.assertIsNone(receipt.post_snapshot)
        self.assertEqual(owner.calls, 1)
        self.assertEqual(confirmation.calls, 0)
        self.assertEqual(page_calls, 1)

    def test_content_writer_page_setup_failure_is_write_not_attempted(self) -> None:
        runtime = PrivateWebContentRuntime(
            owner_reader=OwnerReader(ReadResult.success_empty(())),
            page_factory=lambda: (_ for _ in ()).throw(
                OSError("page setup failed")
            ),
            close_runtime=lambda: None,
        )

        with self.assertRaises(PrivateWebRuntimeSetupError) as caught:
            runtime.content_writer.update_content(AD_ID, title="new")

        self.assertIsInstance(caught.exception, WriteNotAttemptedError)

    def test_state_writer_page_setup_failure_is_write_not_attempted(self) -> None:
        runtime = PrivateWebContentRuntime(
            owner_reader=OwnerReader(ReadResult.success_empty(())),
            page_factory=lambda: (_ for _ in ()).throw(
                OSError("page setup failed")
            ),
            close_runtime=lambda: None,
        )

        with self.assertRaises(PrivateWebRuntimeSetupError) as caught:
            runtime.state_writer.set_state(AD_ID, LifecycleState.PAUSED)

        self.assertIsInstance(caught.exception, WriteNotAttemptedError)

    def test_state_setup_failure_is_non_ambiguous_in_orchestrator(self) -> None:
        owner = OwnerReader(ReadResult.success_nonempty((owner_snapshot(),)))
        page_calls = 0

        def page_factory():
            nonlocal page_calls
            page_calls += 1
            raise OSError("page setup failed")

        runtime = PrivateWebContentRuntime(
            owner_reader=owner,
            page_factory=page_factory,
            close_runtime=lambda: None,
        )

        receipt = SafeWriteOrchestrator(writes_enabled=True).set_state(
            ad_id=AD_ID,
            target_state=LifecycleState.PAUSED,
            reader=owner,
            writer=runtime.state_writer,
        )

        self.assertEqual(receipt.outcome, OperationOutcome.PRECONDITION_FAILED)
        self.assertTrue(receipt.writer_invoked)
        self.assertEqual(receipt.writer_error, "PrivateWebRuntimeSetupError")
        self.assertIsNone(receipt.post_read_status)
        self.assertIsNone(receipt.post_snapshot)
        self.assertEqual(owner.calls, 1)
        self.assertEqual(page_calls, 1)

    def test_closed_runtime_rejects_reader_and_writer_before_page_creation(self) -> None:
        page_calls = 0

        def page_factory():
            nonlocal page_calls
            page_calls += 1
            raise AssertionError("page must not be created after close")

        runtime = PrivateWebContentRuntime(
            owner_reader=OwnerReader(ReadResult.success_empty(())),
            page_factory=page_factory,
            close_runtime=lambda: None,
        )
        runtime.close()

        with self.assertRaises(PrivateWebRuntimeClosedError):
            runtime.read_inventory()
        with self.assertRaises(PrivateWebRuntimeClosedError):
            runtime.content_reader_for(AD_ID)
        with self.assertRaises(PrivateWebRuntimeClosedError):
            runtime.content_writer.update_content(AD_ID, title="new")
        with self.assertRaises(PrivateWebRuntimeClosedError):
            runtime.state_writer.set_state(AD_ID, LifecycleState.PAUSED)
        with self.assertRaises(PrivateWebRuntimeClosedError):
            runtime.delete_writer.delete_ad(AD_ID)
        with self.assertRaises(PrivateWebRuntimeClosedError):
            runtime.create_writer.create_ad(
                AdCreateRequest(
                    category_path=("Haus & Garten", "Dekoration"),
                    title="Neue Vase",
                    description="Beschreibung",
                    price_eur=12,
                )
            )
        self.assertEqual(page_calls, 0)

    def test_dependency_check_fails_when_distribution_is_missing(self) -> None:
        for missing in ("websocket-client", "Pillow"):
            with self.subTest(missing=missing):
                def version(name: str) -> str:
                    if name == missing:
                        raise metadata.PackageNotFoundError(name)
                    return "1.9.0"

                with patch(
                    "mark_api.private_web_runtime.metadata.version",
                    side_effect=version,
                ):
                    with self.assertRaises(
                        PrivateWebRuntimeDependencyError
                    ) as caught:
                        require_private_web_runtime_dependency()

                self.assertNotIn(
                    missing.lower(),
                    str(caught.exception).lower(),
                )

    def test_dependency_check_fails_when_import_module_is_missing(self) -> None:
        for missing in ("websocket", "PIL.Image"):
            with self.subTest(missing=missing):
                def importer(name: str):
                    if name == missing:
                        raise ModuleNotFoundError(name)
                    if name == "websocket":
                        return type(
                            "WebSocketModule",
                            (),
                            {
                                "create_connection": staticmethod(
                                    lambda *args, **kwargs: None
                                )
                            },
                        )()
                    return type(
                        "PillowImageModule",
                        (),
                        {"open": staticmethod(lambda *args, **kwargs: None)},
                    )()

                with (
                    patch(
                        "mark_api.private_web_runtime.metadata.version",
                        return_value="1.9.0",
                    ),
                    patch(
                        "mark_api.private_web_runtime.import_module",
                        side_effect=importer,
                    ),
                ):
                    with self.assertRaises(PrivateWebRuntimeDependencyError):
                        require_private_web_runtime_dependency()

    def test_dependency_check_rejects_shadow_module_without_client_api(self) -> None:
        cases = (
            (
                object(),
                type(
                    "PillowImageModule",
                    (),
                    {"open": staticmethod(lambda *args, **kwargs: None)},
                )(),
            ),
            (
                type(
                    "WebSocketModule",
                    (),
                    {
                        "create_connection": staticmethod(
                            lambda *args, **kwargs: None
                        )
                    },
                )(),
                object(),
            ),
        )
        for modules in cases:
            with self.subTest(modules=modules):
                with (
                    patch(
                        "mark_api.private_web_runtime.metadata.version",
                        return_value="1.9.0",
                    ),
                    patch(
                        "mark_api.private_web_runtime.import_module",
                        side_effect=modules,
                    ),
                ):
                    with self.assertRaises(PrivateWebRuntimeDependencyError):
                        require_private_web_runtime_dependency()

    def test_dependency_check_accepts_declared_distribution_and_module(self) -> None:
        modules = (
            type(
                "WebSocketModule",
                (),
                {
                    "create_connection": staticmethod(
                        lambda *args, **kwargs: None
                    )
                },
            )(),
            type(
                "PillowImageModule",
                (),
                {"open": staticmethod(lambda *args, **kwargs: None)},
            )(),
        )
        with (
            patch(
                "mark_api.private_web_runtime.metadata.version",
                return_value="1.9.0",
            ),
            patch(
                "mark_api.private_web_runtime.import_module",
                side_effect=modules,
            ),
        ):
            require_private_web_runtime_dependency()

    def test_builder_rejects_cookie_bearing_management_overrides(self) -> None:
        with self.assertRaises(TypeError):
            build_private_web_content_runtime(
                cdp_port=19610,
                management_endpoint="https://attacker.invalid/collect",
            )
        with self.assertRaises(TypeError):
            build_private_web_content_runtime(
                cdp_port=19610,
                management_transport=object(),
            )

    def test_management_redirect_handler_rejects_redirect(self) -> None:
        handler = _RejectManagementRedirectHandler()
        self.assertIsNone(
            handler.redirect_request(
                None,
                None,
                302,
                "Found",
                {},
                "https://attacker.invalid/collect",
            )
        )

    def test_management_transport_disables_proxies_and_redirects(self) -> None:
        opener = type("Opener", (), {"open": lambda *args, **kwargs: None})()
        with patch(
            "mark_api.private_web_runtime.build_opener",
            return_value=opener,
        ) as build:
            _NoRedirectManagementTransport()

        proxy_handler, redirect_handler = build.call_args.args
        self.assertIsInstance(proxy_handler, ProxyHandler)
        self.assertEqual(proxy_handler.proxies, {})
        self.assertIsInstance(
            redirect_handler,
            _RejectManagementRedirectHandler,
        )

    def test_inventory_builder_has_no_writer_surface_or_page_factory(self) -> None:
        class CookieDelegate:
            def __init__(self) -> None:
                self.closed = 0

            def __call__(self):
                return "session=opaque"

            def close(self) -> None:
                self.closed += 1

        delegate = CookieDelegate()
        owner_reader = OwnerReader(
            ReadResult.success_nonempty((owner_snapshot(),))
        )
        management_transport = object()

        with (
            patch(
                "mark_api.private_web_runtime.require_private_web_runtime_dependency"
            ) as dependency,
            patch(
                "mark_api.private_web_runtime.CdpCookieProvider.from_port",
                return_value=delegate,
            ) as cookies,
            patch(
                "mark_api.private_web_runtime._NoRedirectManagementTransport",
                return_value=management_transport,
            ) as transport_factory,
            patch(
                "mark_api.private_web_runtime.ManagementReadAdapter",
                return_value=owner_reader,
            ) as management,
            patch(
                "mark_api.private_web_runtime.CdpPrivateWebPage.from_port",
                side_effect=AssertionError("inventory runtime must not create a page"),
            ) as pages,
        ):
            runtime = build_private_web_inventory_runtime(
                cdp_port=19610,
                timeout_seconds=4.0,
            )

        self.assertIsInstance(runtime, PrivateWebInventoryRuntime)
        dependency.assert_called_once_with()
        cookies.assert_called_once_with(19610, timeout_seconds=4.0)
        transport_factory.assert_called_once_with(timeout_seconds=4.0)
        self.assertEqual(management.call_count, 1)
        self.assertEqual(management.call_args.kwargs["endpoint"], MANAGEMENT_URL)
        self.assertIs(
            management.call_args.kwargs["transport"],
            management_transport,
        )
        pages.assert_not_called()
        for name in (
            "content_writer",
            "create_writer",
            "state_writer",
            "delete_writer",
            "content_reader_for",
        ):
            self.assertFalse(hasattr(runtime, name), name)

        result = runtime.read_inventory()
        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        self.assertEqual(owner_reader.calls, 1)

        runtime.close()
        runtime.close()
        self.assertEqual(delegate.closed, 1)

    def test_builder_consumes_existing_cdp_port_without_opening_page_eagerly(self) -> None:
        class CookieDelegate:
            def __init__(self) -> None:
                self.closed = 0

            def __call__(self):
                return "session=opaque"

            def close(self) -> None:
                self.closed += 1

        delegate = CookieDelegate()
        owner_reader = OwnerReader(ReadResult.success_empty(()))
        management_transport = object()

        with (
            patch(
                "mark_api.private_web_runtime.require_private_web_runtime_dependency"
            ) as dependency,
            patch(
                "mark_api.private_web_runtime.CdpCookieProvider.from_port",
                return_value=delegate,
            ) as cookies,
            patch(
                "mark_api.private_web_runtime._NoRedirectManagementTransport",
                return_value=management_transport,
            ) as transport_factory,
            patch(
                "mark_api.private_web_runtime.ManagementReadAdapter",
                return_value=owner_reader,
            ) as management,
            patch(
                "mark_api.private_web_runtime.CdpPrivateWebPage.from_port",
                side_effect=AssertionError("page must be lazy"),
            ) as pages,
        ):
            runtime = build_private_web_content_runtime(
                cdp_port=19610,
                timeout_seconds=4.0,
            )

        dependency.assert_called_once_with()
        cookies.assert_called_once_with(19610, timeout_seconds=4.0)
        transport_factory.assert_called_once_with(timeout_seconds=4.0)
        self.assertEqual(management.call_count, 1)
        self.assertEqual(management.call_args.kwargs["endpoint"], MANAGEMENT_URL)
        self.assertIs(
            management.call_args.kwargs["transport"],
            management_transport,
        )
        pages.assert_not_called()

        runtime.close()
        self.assertEqual(delegate.closed, 1)


class PrivateWebWriteApiRuntimeCompositionTests(unittest.TestCase):
    TOKEN = "fake-runtime-token"

    @staticmethod
    def _content_runtime(
        reader: OwnerReader,
        close_events: list[str],
    ) -> PrivateWebContentRuntime:
        def page_factory():
            raise AssertionError("browser page must stay lazy")

        return PrivateWebContentRuntime(
            owner_reader=reader,
            page_factory=page_factory,
            close_runtime=lambda: close_events.append("content"),
        )

    @staticmethod
    def _confirmation_runtime(
        reader,
        close_events: list[str],
    ) -> PrivateWebInventoryRuntime:
        return PrivateWebInventoryRuntime(
            owner_reader=reader,
            close_runtime=lambda: close_events.append("confirmation"),
        )

    def _request(
        self,
        runtime: PrivateWebWriteApiRuntime,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, dict[str, object]]:
        host, port = runtime.start()
        self.assertEqual(host, "127.0.0.1")
        headers = {"Authorization": f"Bearer {self.TOKEN}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        request = Request(
            f"http://127.0.0.1:{port}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        opener = build_opener(ProxyHandler({}))
        try:
            with opener.open(request, timeout=2.0) as response:
                return int(response.status), json.loads(response.read())
        except HTTPError as exc:
            try:
                return int(exc.code), json.loads(exc.read())
            finally:
                exc.close()

    @staticmethod
    def _create_payload() -> dict[str, object]:
        return {
            "category_path": ["Haus & Garten", "Dekoration"],
            "title": "Neue Vase",
            "description": "Beschreibung",
            "price_eur": 12,
        }

    @staticmethod
    def _operation_receipt(
        operation: str,
        ad_id: str,
        *,
        authorization_by: str | None,
        authorization_reference: str | None,
    ) -> OperationReceipt:
        return OperationReceipt(
            operation=operation,
            ad_id=ad_id,
            started_at=NOW,
            completed_at=NOW,
            outcome=OperationOutcome.CONFIRMED,
            pre_read_status="success_nonempty",
            post_read_status="success_nonempty",
            writer_invoked=True,
            authorization_by=authorization_by,
            authorization_reference=authorization_reference,
        )

    def test_composition_starts_loopback_with_independent_core_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            reader = OwnerReader(ReadResult.success_empty(()))
            close_events: list[str] = []
            content_runtime = self._content_runtime(reader, close_events)
            access = WriteApiAccess(
                principal="runtime-test",
                bearer_token=self.TOKEN,
                capabilities=frozenset({WriteCapability.CREATE}),
                writes_enabled=True,
            )
            runtime = compose_private_web_write_api_runtime(
                content_runtime=content_runtime,
                store=store,
                access=access,
            )
            try:
                first_address = runtime.start()
                self.assertEqual(runtime.start(), first_address)
                status, body = self._request(
                    runtime,
                    "POST",
                    "/api/write/ads",
                    payload=self._create_payload(),
                    idempotency_key="core-gate",
                )
                self.assertEqual(status, 409)
                receipt = body["operation_receipt"]
                self.assertEqual(receipt["pre_read_status"], "writes_disabled")
                self.assertFalse(receipt["writer_invoked"])
                self.assertFalse(body["platform_retry_authorized"])
                self.assertEqual(reader.calls, 0)
            finally:
                runtime.close()
                runtime.close()

            self.assertEqual(close_events, ["content"])

    def test_api_gate_can_stay_closed_while_core_gate_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            reader = OwnerReader(ReadResult.success_empty(()))
            close_events: list[str] = []
            content_runtime = self._content_runtime(reader, close_events)
            access = WriteApiAccess(
                principal="runtime-test",
                bearer_token=self.TOKEN,
                capabilities=frozenset({WriteCapability.CREATE}),
                writes_enabled=False,
            )
            runtime = compose_private_web_write_api_runtime(
                content_runtime=content_runtime,
                store=store,
                access=access,
                core_writes_enabled=True,
            )
            try:
                status, _ = self._request(
                    runtime,
                    "POST",
                    "/api/write/ads",
                    payload=self._create_payload(),
                    idempotency_key="api-gate",
                )
                self.assertEqual(status, 403)
                self.assertEqual(reader.calls, 0)
                self.assertIsNone(store.write_api_request("api-gate"))
            finally:
                runtime.close()

            self.assertEqual(close_events, ["content"])

    def test_create_uses_fresh_owner_observations_and_target_detail_without_second_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            created = owner_snapshot(
                title="Neue Vase",
                description="Beschreibung",
            )
            reader = SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_empty(()),
                ReadResult.success_nonempty((created,)),
                ReadResult.success_nonempty((created,)),
                ReadResult.success_nonempty((created,)),
            )
            close_events: list[str] = []
            events: list[tuple] = []
            pages = [
                CreatePage(events),
                SharedPage(
                    {"title": "Neue Vase", "description": "Beschreibung"},
                    events,
                ),
            ]

            def page_factory():
                if not pages:
                    raise AssertionError("unexpected extra browser page")
                return pages.pop(0)

            content_runtime = PrivateWebContentRuntime(
                owner_reader=reader,
                page_factory=page_factory,
                close_runtime=lambda: close_events.append("content"),
            )
            runtime = compose_private_web_write_api_runtime(
                content_runtime=content_runtime,
                store=store,
                access=WriteApiAccess(
                    principal="runtime-test",
                    bearer_token=self.TOKEN,
                    capabilities=frozenset({WriteCapability.CREATE}),
                    writes_enabled=True,
                ),
                core_writes_enabled=True,
            )
            try:
                status, body = self._request(
                    runtime,
                    "POST",
                    "/api/write/ads",
                    payload=self._create_payload(),
                    idempotency_key="single-inventory-create",
                )
                self.assertEqual(status, 200, body)
                receipt = body["operation_receipt"]
                self.assertEqual(receipt["outcome"], "confirmed")
                self.assertEqual(receipt["created_ad_id"], AD_ID)
                self.assertTrue(receipt["writer_invoked"])
                self.assertEqual(reader.calls, 5)
                self.assertEqual(
                    [event[0] for event in events].count("submit_create"),
                    1,
                )
                self.assertIn(("open_editor", AD_ID), events)
                self.assertEqual(pages, [])
            finally:
                runtime.close()
            self.assertEqual(close_events, ["content"])

    def test_delete_uses_two_fresh_absence_reads_without_second_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            reader = SequenceReader(
                ReadResult.success_nonempty((owner_snapshot(),)),
                ReadResult.success_empty(()),
                ReadResult.success_empty(()),
            )
            close_events: list[str] = []
            events: list[tuple] = []
            content_runtime = PrivateWebContentRuntime(
                owner_reader=reader,
                page_factory=lambda: DeletePage(events),
                close_runtime=lambda: close_events.append("content"),
            )
            runtime = compose_private_web_write_api_runtime(
                content_runtime=content_runtime,
                store=store,
                access=WriteApiAccess(
                    principal="runtime-test",
                    bearer_token=self.TOKEN,
                    capabilities=frozenset({WriteCapability.DELETE}),
                    writes_enabled=True,
                ),
                core_writes_enabled=True,
            )
            try:
                status, body = self._request(
                    runtime,
                    "DELETE",
                    f"/api/write/ads/{AD_ID}",
                    idempotency_key="single-inventory-delete",
                )
                self.assertEqual(status, 200)
                receipt = body["operation_receipt"]
                self.assertEqual(receipt["outcome"], "confirmed")
                self.assertEqual(
                    receipt["authorization_reference"],
                    "write-api:single-inventory-delete",
                )
                self.assertTrue(receipt["writer_invoked"])
                self.assertFalse(body["platform_retry_authorized"])
                self.assertEqual(reader.calls, 3)
                self.assertEqual(
                    [event[0] for event in events].count("submit_delete"),
                    1,
                )
            finally:
                runtime.close()
            self.assertEqual(close_events, ["content"])

    def test_bundle_does_not_expose_internal_write_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            content_runtime = self._content_runtime(
                OwnerReader(ReadResult.success_empty(())),
                [],
            )
            runtime = compose_private_web_write_api_runtime(
                content_runtime=content_runtime,
                store=store,
                access=WriteApiAccess(
                    principal="runtime-test",
                    bearer_token=self.TOKEN,
                    capabilities=frozenset(),
                ),
            )
            try:
                for name in (
                    "content_runtime",
                    "media_runtime",
                    "mark_service",
                    "media_service",
                    "server",
                ):
                    self.assertFalse(hasattr(runtime, name), name)
            finally:
                runtime.close()

    def test_media_capability_requires_complete_media_composition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            reader = OwnerReader(ReadResult.success_empty(()))
            close_events: list[str] = []
            content_runtime = self._content_runtime(reader, close_events)
            access = WriteApiAccess(
                principal="runtime-test",
                bearer_token=self.TOKEN,
                capabilities=frozenset({WriteCapability.CREATE_MEDIA}),
            )
            with self.assertRaisesRegex(
                ValueError,
                "create_media capability requires private Web media composition",
            ):
                compose_private_web_write_api_runtime(
                    content_runtime=content_runtime,
                    store=store,
                    access=access,
                )
            self.assertEqual(close_events, [])
            content_runtime.close()
            self.assertEqual(close_events, ["content"])

    def test_builder_rejects_media_mismatch_before_browser_runtime(self) -> None:
        source = PrivateWebMediaSource("/tmp/runtime-test-photo.jpg")
        cases = (
            (
                WriteApiAccess(
                    principal="runtime-test",
                    bearer_token=self.TOKEN,
                    capabilities=frozenset({WriteCapability.CREATE_MEDIA}),
                ),
                {},
                "media_bindings must be non-empty when provided",
            ),
            (
                WriteApiAccess(
                    principal="runtime-test",
                    bearer_token=self.TOKEN,
                    capabilities=frozenset({WriteCapability.CREATE}),
                ),
                {"cover": source},
                "media_bindings require create_media capability",
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            for access, bindings, message in cases:
                with self.subTest(message=message):
                    with patch(
                        "mark_api.private_web_runtime.build_private_web_content_runtime",
                        side_effect=AssertionError("browser runtime must not build"),
                    ) as builder:
                        with self.assertRaisesRegex(ValueError, message):
                            build_private_web_write_api_runtime(
                                cdp_port=19610,
                                store=store,
                                access=access,
                                media_bindings=bindings,
                            )
                        builder.assert_not_called()

    def test_pending_dashboard_media_refs_selects_persisted_media_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            store.claim_dashboard_pending_write(
                scope="create-media",
                resource_key="create",
                idempotency_key="ui:pending-media",
                method="POST",
                path="/api/write/media/ads",
                payload_json=json.dumps(
                    {
                        "category_path": ["A", "B"],
                        "title": "Pending",
                        "description": "Recovery",
                        "price_eur": 1,
                        "media_refs": ["media_one", "media_two"],
                    }
                ),
                ad_id=None,
            )
            store.claim_dashboard_pending_write(
                scope="ad:123:pause",
                resource_key="ad:123",
                idempotency_key="ui:pending-pause",
                method="POST",
                path="/api/write/ads/123/pause",
                payload_json=None,
                ad_id="123",
            )

            self.assertEqual(
                _pending_dashboard_media_refs(store),
                frozenset({"media_one", "media_two"}),
            )

    def test_builder_restores_pending_media_handle_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            access = WriteApiAccess(
                principal="runtime-test",
                bearer_token=self.TOKEN,
                capabilities=frozenset({WriteCapability.CREATE_MEDIA}),
                writes_enabled=True,
            )

            def build_runtime(close_events: list[str]):
                content_runtime = self._content_runtime(
                    OwnerReader(ReadResult.success_empty(())),
                    close_events,
                )
                media_runtime = PrivateWebMediaCreateRuntime(
                    page_factory=lambda: (_ for _ in ()).throw(
                        AssertionError("media page must stay lazy")
                    )
                )
                with (
                    patch(
                        "mark_api.private_web_runtime.build_private_web_content_runtime",
                        return_value=content_runtime,
                    ),
                    patch(
                        "mark_api.private_web_runtime.build_private_web_media_create_runtime",
                        return_value=media_runtime,
                    ),
                ):
                    return build_private_web_write_api_runtime(
                        cdp_port=19610,
                        store=store,
                        access=access,
                        media_writes_enabled=True,
                    )

            first_close_events: list[str] = []
            first = build_runtime(first_close_events)
            assert first._media_handle_store is not None
            media_ref = first._media_handle_store.stage_media(
                "upload.jpg",
                b"\xff\xd8\xffrestart",
            )
            (first_source,) = first._media_handle_store.resolve((media_ref,))
            with self.assertRaisesRegex(RuntimeError, "already active"):
                build_runtime([])
            self.assertTrue(Path(first_source.path).exists())
            self.assertEqual(
                Path(first_source.path).read_bytes(),
                b"\xff\xd8\xffrestart",
            )
            store.claim_dashboard_pending_write(
                scope="create-media",
                resource_key="create",
                idempotency_key="ui:restart-media-create",
                method="POST",
                path="/api/write/media/ads",
                payload_json=json.dumps(
                    {
                        "category_path": ["A", "B"],
                        "title": "Restart",
                        "description": "Recovery",
                        "price_eur": 1,
                        "media_refs": [media_ref],
                    }
                ),
                ad_id=None,
            )
            first.close()
            self.assertEqual(first_close_events, ["content"])
            self.assertTrue(Path(first_source.path).exists())

            second_close_events: list[str] = []
            second = build_runtime(second_close_events)
            try:
                assert second._media_handle_store is not None
                (restored,) = second._media_handle_store.resolve((media_ref,))
                self.assertEqual(restored.path, first_source.path)
                self.assertEqual(
                    Path(restored.path).read_bytes(),
                    b"\xff\xd8\xffrestart",
                )
                second._media_handle_store.discard((media_ref,))
                self.assertFalse(Path(restored.path).exists())
            finally:
                second.close()
            self.assertEqual(second_close_events, ["content"])

    def test_builder_creates_product_media_stager_without_caller_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            reader = OwnerReader(ReadResult.success_empty(()))
            close_events: list[str] = []
            content_runtime = self._content_runtime(reader, close_events)
            media_runtime = PrivateWebMediaCreateRuntime(
                page_factory=lambda: (_ for _ in ()).throw(
                    AssertionError("media page must stay lazy")
                )
            )
            access = WriteApiAccess(
                principal="runtime-test",
                bearer_token=self.TOKEN,
                capabilities=frozenset({WriteCapability.CREATE_MEDIA}),
                writes_enabled=True,
            )
            with (
                patch(
                    "mark_api.private_web_runtime.build_private_web_content_runtime",
                    return_value=content_runtime,
                ),
                patch(
                    "mark_api.private_web_runtime.build_private_web_media_create_runtime",
                    return_value=media_runtime,
                ),
            ):
                runtime = build_private_web_write_api_runtime(
                    cdp_port=19610,
                    store=store,
                    access=access,
                    media_writes_enabled=True,
                )
            try:
                host, port = runtime.start()
                request = Request(
                    f"http://{host}:{port}/api/write/media/stage",
                    data=b"\xff\xd8\xffjpeg",
                    headers={
                        "Authorization": f"Bearer {self.TOKEN}",
                        "Content-Type": "image/jpeg",
                        "X-Mark-Media-Filename": "photo.jpg",
                    },
                    method="POST",
                )
                opener = build_opener(ProxyHandler({}))
                with opener.open(request, timeout=2.0) as response:
                    body = json.loads(response.read())
                    self.assertEqual(response.status, 201)
                self.assertRegex(body["media_ref"], r"^media_[A-Za-z0-9_-]+$")
            finally:
                runtime.close()
            self.assertEqual(close_events, ["content"])

    def test_builder_auto_composes_default_media_persistence_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            reader = OwnerReader(ReadResult.success_empty(()))
            close_events: list[str] = []
            content_runtime = self._content_runtime(reader, close_events)
            media_runtime = PrivateWebMediaCreateRuntime(
                page_factory=lambda: (_ for _ in ()).throw(
                    AssertionError("media page must stay lazy")
                )
            )
            access = WriteApiAccess(
                principal="runtime-test",
                bearer_token=self.TOKEN,
                capabilities=frozenset({WriteCapability.CREATE_MEDIA}),
                writes_enabled=True,
            )
            default_verifier = MediaPersistenceVerifier()
            sentinel_runtime = object()
            with (
                patch(
                    "mark_api.private_web_runtime.build_private_web_content_runtime",
                    return_value=content_runtime,
                ),
                patch(
                    "mark_api.private_web_runtime.build_private_web_media_create_runtime",
                    return_value=media_runtime,
                ),
                patch(
                    "mark_api.private_web_runtime.PrivateWebPublicMediaPersistenceVerifier",
                    return_value=default_verifier,
                ) as verifier_builder,
                patch(
                    "mark_api.private_web_runtime.compose_private_web_write_api_runtime",
                    return_value=sentinel_runtime,
                ) as compose,
            ):
                actual = build_private_web_write_api_runtime(
                    cdp_port=19610,
                    store=store,
                    access=access,
                    media_writes_enabled=True,
                )

            self.assertIs(actual, sentinel_runtime)
            verifier_builder.assert_called_once_with()
            self.assertIs(
                compose.call_args.kwargs["media_persistence_verifier"],
                default_verifier,
            )

    def test_builder_passes_explicit_media_persistence_verifier_to_composition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            reader = OwnerReader(ReadResult.success_empty(()))
            close_events: list[str] = []
            content_runtime = self._content_runtime(reader, close_events)
            media_runtime = PrivateWebMediaCreateRuntime(
                page_factory=lambda: (_ for _ in ()).throw(
                    AssertionError("media page must stay lazy")
                )
            )
            access = WriteApiAccess(
                principal="runtime-test",
                bearer_token=self.TOKEN,
                capabilities=frozenset({WriteCapability.CREATE_MEDIA}),
                writes_enabled=True,
            )
            explicit_verifier = MediaPersistenceVerifier()
            sentinel_runtime = object()
            with (
                patch(
                    "mark_api.private_web_runtime.build_private_web_content_runtime",
                    return_value=content_runtime,
                ),
                patch(
                    "mark_api.private_web_runtime.build_private_web_media_create_runtime",
                    return_value=media_runtime,
                ),
                patch(
                    "mark_api.private_web_runtime.PrivateWebPublicMediaPersistenceVerifier",
                    side_effect=AssertionError(
                        "default verifier must not be constructed"
                    ),
                ),
                patch(
                    "mark_api.private_web_runtime.compose_private_web_write_api_runtime",
                    return_value=sentinel_runtime,
                ) as compose,
            ):
                actual = build_private_web_write_api_runtime(
                    cdp_port=19610,
                    store=store,
                    access=access,
                    media_persistence_verifier=explicit_verifier,
                    media_writes_enabled=True,
                )

            self.assertIs(actual, sentinel_runtime)
            self.assertIs(
                compose.call_args.kwargs["media_persistence_verifier"],
                explicit_verifier,
            )

    def test_builder_composes_default_media_persistence_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            reader = OwnerReader(ReadResult.success_empty(()))
            close_events: list[str] = []
            content_runtime = self._content_runtime(reader, close_events)
            media_runtime = PrivateWebMediaCreateRuntime(
                page_factory=lambda: (_ for _ in ()).throw(
                    AssertionError("media page must stay lazy")
                )
            )
            access = WriteApiAccess(
                principal="runtime-test",
                bearer_token=self.TOKEN,
                capabilities=frozenset({WriteCapability.CREATE_MEDIA}),
                writes_enabled=True,
            )
            verifier = MediaPersistenceVerifier()
            with (
                patch(
                    "mark_api.private_web_runtime.build_private_web_content_runtime",
                    return_value=content_runtime,
                ),
                patch(
                    "mark_api.private_web_runtime.build_private_web_media_create_runtime",
                    return_value=media_runtime,
                ),
                patch(
                    "mark_api.private_web_runtime.PrivateWebPublicMediaPersistenceVerifier",
                    return_value=verifier,
                ) as verifier_builder,
            ):
                runtime = build_private_web_write_api_runtime(
                    cdp_port=19610,
                    store=store,
                    access=access,
                    media_writes_enabled=True,
                )
            try:
                verifier_builder.assert_called_once_with()
                self.assertIsNotNone(runtime._media_service)
                assert runtime._media_service is not None
                self.assertIs(
                    runtime._media_service._media_persistence_verifier,
                    verifier,
                )
            finally:
                runtime.close()
            self.assertEqual(close_events, ["content"])

    def test_builder_preserves_explicit_media_persistence_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            reader = OwnerReader(ReadResult.success_empty(()))
            close_events: list[str] = []
            content_runtime = self._content_runtime(reader, close_events)
            media_runtime = PrivateWebMediaCreateRuntime(
                page_factory=lambda: (_ for _ in ()).throw(
                    AssertionError("media page must stay lazy")
                )
            )
            access = WriteApiAccess(
                principal="runtime-test",
                bearer_token=self.TOKEN,
                capabilities=frozenset({WriteCapability.CREATE_MEDIA}),
                writes_enabled=True,
            )
            verifier = MediaPersistenceVerifier()
            with (
                patch(
                    "mark_api.private_web_runtime.build_private_web_content_runtime",
                    return_value=content_runtime,
                ),
                patch(
                    "mark_api.private_web_runtime.build_private_web_media_create_runtime",
                    return_value=media_runtime,
                ),
                patch(
                    "mark_api.private_web_runtime.PrivateWebPublicMediaPersistenceVerifier",
                    side_effect=AssertionError(
                        "default verifier must not be constructed"
                    ),
                ),
            ):
                runtime = build_private_web_write_api_runtime(
                    cdp_port=19610,
                    store=store,
                    access=access,
                    media_persistence_verifier=verifier,
                    media_writes_enabled=True,
                )
            try:
                self.assertIsNotNone(runtime._media_service)
                assert runtime._media_service is not None
                self.assertIs(
                    runtime._media_service._media_persistence_verifier,
                    verifier,
                )
            finally:
                runtime.close()
            self.assertEqual(close_events, ["content"])

    def test_builder_accepts_empty_media_bindings_without_media_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            reader = OwnerReader(ReadResult.success_empty(()))
            close_events: list[str] = []
            content_runtime = self._content_runtime(reader, close_events)
            access = WriteApiAccess(
                principal="runtime-test",
                bearer_token=self.TOKEN,
                capabilities=frozenset({WriteCapability.CREATE}),
                writes_enabled=True,
            )
            with (
                patch(
                    "mark_api.private_web_runtime.build_private_web_content_runtime",
                    return_value=content_runtime,
                ),
                patch(
                    "mark_api.private_web_runtime.build_private_web_inventory_runtime",
                    side_effect=AssertionError(
                        "same-source confirmation runtime must not build"
                    ),
                ) as confirmation_builder,
                patch(
                    "mark_api.private_web_runtime.build_private_web_media_create_runtime",
                    side_effect=AssertionError("media runtime must not build"),
                ) as media_builder,
            ):
                runtime = build_private_web_write_api_runtime(
                    cdp_port=19610,
                    store=store,
                    access=access,
                    media_bindings={},
                    core_writes_enabled=True,
                )
            try:
                self.assertEqual(runtime.server_address[0], "127.0.0.1")
                media_builder.assert_not_called()
                confirmation_builder.assert_not_called()
                self.assertEqual(reader.calls, 0)
            finally:
                runtime.close()
            self.assertEqual(close_events, ["content"])

    def test_builder_closes_content_runtime_if_media_builder_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            media_path = Path(tmp) / "photo.jpg"
            media_path.write_bytes(b"jpeg")
            source = PrivateWebMediaSource(str(media_path))
            close_events: list[str] = []
            content_runtime = self._content_runtime(
                OwnerReader(ReadResult.success_empty(())),
                close_events,
            )
            access = WriteApiAccess(
                principal="runtime-test",
                bearer_token=self.TOKEN,
                capabilities=frozenset({WriteCapability.CREATE_MEDIA}),
            )
            with (
                patch(
                    "mark_api.private_web_runtime.build_private_web_content_runtime",
                    return_value=content_runtime,
                ),
                patch(
                    "mark_api.private_web_runtime.build_private_web_inventory_runtime",
                    side_effect=AssertionError(
                        "same-source confirmation runtime must not build"
                    ),
                ) as confirmation_builder,
                patch(
                    "mark_api.private_web_runtime.build_private_web_media_create_runtime",
                    side_effect=RuntimeError("media setup failed"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "media setup failed"):
                    build_private_web_write_api_runtime(
                        cdp_port=19610,
                        store=store,
                        access=access,
                        media_bindings={"cover": source},
                    )
                confirmation_builder.assert_not_called()
            self.assertEqual(close_events, ["content"])

    def test_media_route_is_wired_but_closed_core_gate_skips_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            source = PrivateWebMediaSource(str(Path(tmp) / "not-read.jpg"))
            reader = OwnerReader(ReadResult.success_empty(()))
            close_events: list[str] = []
            content_runtime = self._content_runtime(reader, close_events)
            page_calls: list[str] = []

            def page_factory():
                page_calls.append("opened")
                raise AssertionError("media page must stay lazy")

            media_runtime = PrivateWebMediaCreateRuntime(
                page_factory=page_factory
            )
            access = WriteApiAccess(
                principal="runtime-test",
                bearer_token=self.TOKEN,
                capabilities=frozenset({WriteCapability.CREATE_MEDIA}),
                writes_enabled=True,
            )
            runtime = compose_private_web_write_api_runtime(
                content_runtime=content_runtime,
                media_runtime=media_runtime,
                media_resolver=PrivateWebMediaRefResolver({"cover": source}),
                store=store,
                access=access,
            )
            try:
                status, body = self._request(
                    runtime,
                    "POST",
                    "/api/write/media/ads",
                    payload={
                        **self._create_payload(),
                        "media_refs": ["cover"],
                    },
                    idempotency_key="media-core-gate",
                )
                self.assertEqual(status, 409)
                self.assertFalse(body["media_persistence_confirmed"])
                self.assertFalse(body["platform_retry_authorized"])
                receipt = body["operation_receipt"]
                self.assertEqual(receipt["pre_read_status"], "writes_disabled")
                self.assertFalse(receipt["writer_invoked"])
                self.assertEqual(reader.calls, 0)
                self.assertEqual(page_calls, [])
            finally:
                runtime.close()

            self.assertEqual(close_events, ["content"])

    def test_composition_serializes_shared_private_web_operations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            close_events: list[str] = []
            content_runtime = self._content_runtime(
                OwnerReader(ReadResult.success_empty(())),
                close_events,
            )
            runtime = compose_private_web_write_api_runtime(
                content_runtime=content_runtime,
                store=store,
                access=WriteApiAccess(
                    principal="runtime-test",
                    bearer_token=self.TOKEN,
                    capabilities=frozenset({WriteCapability.SET_STATE}),
                    writes_enabled=True,
                ),
                core_writes_enabled=True,
            )
            first_entered = threading.Event()
            release_first = threading.Event()
            second_entered = threading.Event()
            errors: list[BaseException] = []
            results: list[tuple[int, dict[str, object]]] = []

            def pause(
                ad_id: str,
                *,
                authorization_by: str | None = None,
                authorization_reference: str | None = None,
            ) -> OperationReceipt:
                first_entered.set()
                if not release_first.wait(timeout=2):
                    raise AssertionError("first operation was not released")
                return self._operation_receipt(
                    "set_state:paused",
                    ad_id,
                    authorization_by=authorization_by,
                    authorization_reference=authorization_reference,
                )

            def activate(
                ad_id: str,
                *,
                authorization_by: str | None = None,
                authorization_reference: str | None = None,
            ) -> OperationReceipt:
                second_entered.set()
                return self._operation_receipt(
                    "set_state:active",
                    ad_id,
                    authorization_by=authorization_by,
                    authorization_reference=authorization_reference,
                )

            runtime._mark_service.pause = pause
            runtime._mark_service.activate = activate

            def request(path: str, key: str) -> None:
                try:
                    results.append(
                        self._request(
                            runtime,
                            "POST",
                            path,
                            idempotency_key=key,
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            first = threading.Thread(
                target=request,
                args=(f"/api/write/ads/{AD_ID}/pause", "serialize-pause"),
            )
            second = threading.Thread(
                target=request,
                args=(f"/api/write/ads/{AD_ID}/activate", "serialize-activate"),
            )
            runtime.start()
            first.start()
            try:
                self.assertTrue(first_entered.wait(timeout=2))
                second.start()
                second_claim = None
                for _ in range(200):
                    second_claim = store.write_api_request("serialize-activate")
                    if second_claim is not None:
                        break
                    threading.Event().wait(0.01)
                self.assertIsNotNone(second_claim)
                assert second_claim is not None
                self.assertIsNone(second_claim.execution_started_at)
                self.assertFalse(second_entered.wait(timeout=0.25))
            finally:
                release_first.set()
                first.join(timeout=2)
                if second.ident is not None:
                    second.join(timeout=2)
                runtime.close()

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertTrue(second_entered.is_set())
            self.assertEqual(errors, [])
            self.assertEqual(sorted(status for status, _ in results), [200, 200])
            self.assertEqual(close_events, ["content"])

    def test_close_drains_active_handler_before_runtime_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            close_events: list[str] = []
            content_runtime = self._content_runtime(
                OwnerReader(ReadResult.success_empty(())),
                close_events,
            )
            runtime = compose_private_web_write_api_runtime(
                content_runtime=content_runtime,
                store=store,
                access=WriteApiAccess(
                    principal="runtime-test",
                    bearer_token=self.TOKEN,
                    capabilities=frozenset({WriteCapability.SET_STATE}),
                    writes_enabled=True,
                ),
                core_writes_enabled=True,
            )
            handler_entered = threading.Event()
            release_handler = threading.Event()
            request_errors: list[BaseException] = []
            request_results: list[tuple[int, dict[str, object]]] = []
            close_errors: list[BaseException] = []

            def pause(
                ad_id: str,
                *,
                authorization_by: str | None = None,
                authorization_reference: str | None = None,
            ) -> OperationReceipt:
                handler_entered.set()
                if not release_handler.wait(timeout=2):
                    raise AssertionError("handler was not released")
                return self._operation_receipt(
                    "set_state:paused",
                    ad_id,
                    authorization_by=authorization_by,
                    authorization_reference=authorization_reference,
                )

            runtime._mark_service.pause = pause

            def request() -> None:
                try:
                    request_results.append(
                        self._request(
                            runtime,
                            "POST",
                            f"/api/write/ads/{AD_ID}/pause",
                            idempotency_key="drain-pause",
                        )
                    )
                except BaseException as exc:
                    request_errors.append(exc)

            def close() -> None:
                try:
                    runtime.close()
                except BaseException as exc:
                    close_errors.append(exc)

            runtime.start()
            request_thread = threading.Thread(target=request)
            close_thread = threading.Thread(target=close)
            request_thread.start()
            try:
                self.assertTrue(handler_entered.wait(timeout=2))
                close_thread.start()
                for _ in range(200):
                    if runtime._server_shutdown:
                        break
                    threading.Event().wait(0.01)
                self.assertTrue(runtime._server_shutdown)
                self.assertTrue(close_thread.is_alive())
                self.assertEqual(close_events, [])
            finally:
                release_handler.set()
                request_thread.join(timeout=2)
                if close_thread.ident is not None:
                    close_thread.join(timeout=2)

            self.assertFalse(request_thread.is_alive())
            self.assertFalse(close_thread.is_alive())
            self.assertEqual(request_errors, [])
            self.assertEqual(close_errors, [])
            self.assertEqual(
                [status for status, _ in request_results],
                [200],
            )
            self.assertEqual(close_events, ["content"])

    def test_close_preserves_runtimes_if_http_cannot_quiesce(self) -> None:
        class StuckThread:
            def __init__(self) -> None:
                self.join_calls: list[float | None] = []

            def is_alive(self) -> bool:
                return True

            def join(self, timeout: float | None = None) -> None:
                self.join_calls.append(timeout)

        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            close_events: list[str] = []
            content_runtime = self._content_runtime(
                OwnerReader(ReadResult.success_empty(())),
                close_events,
            )
            runtime = compose_private_web_write_api_runtime(
                content_runtime=content_runtime,
                store=store,
                access=WriteApiAccess(
                    principal="runtime-test",
                    bearer_token=self.TOKEN,
                    capabilities=frozenset(),
                ),
            )
            stuck_thread = StuckThread()
            runtime._server_thread = stuck_thread
            try:
                with patch.object(
                    runtime._server,
                    "shutdown",
                    side_effect=RuntimeError("shutdown failed"),
                ):
                    with self.assertRaisesRegex(
                        PrivateWebRuntimeSetupError,
                        "cleanup failed",
                    ):
                        runtime.close()
                self.assertEqual(stuck_thread.join_calls, [2.0])
                self.assertEqual(close_events, [])
                with self.assertRaisesRegex(
                    PrivateWebRuntimeSetupError,
                    "shutdown is pending",
                ):
                    runtime.start()
            finally:
                runtime._server_thread = None
                runtime.close()
            self.assertEqual(close_events, ["content"])

    def test_runtime_cleanup_failure_stays_fail_closed_across_repeated_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            close_calls = 0

            def close_runtime() -> None:
                nonlocal close_calls
                close_calls += 1
                raise RuntimeError("content cleanup failed")

            content_runtime = PrivateWebContentRuntime(
                owner_reader=OwnerReader(ReadResult.success_empty(())),
                page_factory=lambda: (_ for _ in ()).throw(
                    AssertionError("browser page must stay lazy")
                ),
                close_runtime=close_runtime,
            )
            runtime = compose_private_web_write_api_runtime(
                content_runtime=content_runtime,
                store=store,
                access=WriteApiAccess(
                    principal="runtime-test",
                    bearer_token=self.TOKEN,
                    capabilities=frozenset(),
                ),
            )

            for _ in range(2):
                with self.assertRaisesRegex(
                    PrivateWebRuntimeSetupError,
                    "cleanup failed",
                ):
                    runtime.close()

            self.assertEqual(close_calls, 1)
            with self.assertRaisesRegex(
                PrivateWebRuntimeSetupError,
                "shutdown is pending",
            ):
                runtime.start()

    def test_unresolved_media_submit_quiesces_http_and_blocks_shutdown(self) -> None:
        class PendingMediaPage:
            def __init__(self) -> None:
                self.reconciled = 0
                self.closed = 0

            def reconcile_create_media_submit(self) -> None:
                self.reconciled += 1

            def close(self) -> None:
                self.closed += 1

        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            media_path = Path(tmp) / "photo.jpg"
            media_path.write_bytes(b"jpeg")
            source = PrivateWebMediaSource(str(media_path))
            close_events: list[str] = []
            content_runtime = self._content_runtime(
                OwnerReader(ReadResult.success_empty(())),
                close_events,
            )
            media_runtime = PrivateWebMediaCreateRuntime(
                page_factory=lambda: (_ for _ in ()).throw(
                    AssertionError("new media page must not open")
                )
            )
            pending = PendingMediaPage()
            media_runtime._pending_page = pending
            media_runtime._submit_unknown_fenced = True
            access = WriteApiAccess(
                principal="runtime-test",
                bearer_token=self.TOKEN,
                capabilities=frozenset({WriteCapability.CREATE_MEDIA}),
            )
            runtime = compose_private_web_write_api_runtime(
                content_runtime=content_runtime,
                media_runtime=media_runtime,
                media_resolver=PrivateWebMediaRefResolver({"cover": source}),
                store=store,
                access=access,
            )
            runtime.start()
            with self.assertRaisesRegex(
                PrivateWebSubmitUnknownError,
                "media_runtime_close_unsettled",
            ):
                runtime.close()
            with self.assertRaisesRegex(RuntimeError, "already active"):
                acquire_write_api_store_lock(store)
            self.assertTrue(runtime.media_reconciliation_required)
            self.assertEqual(close_events, [])
            with self.assertRaisesRegex(
                PrivateWebRuntimeSetupError,
                "shutdown is pending",
            ):
                runtime.start()
            runtime.reconcile_media_submit()
            self.assertFalse(runtime.media_reconciliation_required)
            self.assertEqual(pending.reconciled, 1)
            self.assertEqual(pending.closed, 1)
            runtime.close()
            self.assertEqual(close_events, ["content"])
            replacement_lock = acquire_write_api_store_lock(store)
            replacement_lock.close()



    def test_normal_create_route_uses_composed_mark_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            created = owner_snapshot(
                title="Neue Vase",
                description="Beschreibung",
            )
            reader = SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_nonempty((created,)),
                ReadResult.success_nonempty((created,)),
            )
            confirmation_reader = SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_nonempty((created,)),
            )
            close_events: list[str] = []
            events: list[tuple] = []
            pages = [
                CreatePage(events),
                SharedPage(
                    {
                        "title": "Neue Vase",
                        "description": "Beschreibung",
                    },
                    events,
                ),
            ]

            def page_factory():
                if not pages:
                    raise AssertionError("unexpected extra browser page")
                return pages.pop(0)

            content_runtime = PrivateWebContentRuntime(
                owner_reader=reader,
                page_factory=page_factory,
                close_runtime=lambda: close_events.append("content"),
            )
            confirmation_runtime = self._confirmation_runtime(
                confirmation_reader,
                close_events,
            )
            access = WriteApiAccess(
                principal="runtime-test",
                bearer_token=self.TOKEN,
                capabilities=frozenset({WriteCapability.CREATE}),
                writes_enabled=True,
            )
            runtime = compose_private_web_write_api_runtime(
                content_runtime=content_runtime,
                confirmation_runtime=confirmation_runtime,
                store=store,
                access=access,
                core_writes_enabled=True,
            )
            try:
                status, body = self._request(
                    runtime,
                    "POST",
                    "/api/write/ads",
                    payload=self._create_payload(),
                    idempotency_key="normal-create-composed",
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    body["operation_receipt"]["created_ad_id"],
                    AD_ID,
                )
                self.assertTrue(body["operation_receipt"]["writer_invoked"])
                self.assertFalse(body["platform_retry_authorized"])
                self.assertEqual(reader.calls, 3)
                self.assertEqual(confirmation_reader.calls, 2)
                self.assertEqual(
                    [event[0] for event in events].count("submit_create"),
                    1,
                )
                self.assertIn(("open_editor", AD_ID), events)
                self.assertEqual(pages, [])
            finally:
                runtime.close()
            self.assertEqual(close_events, ["confirmation", "content"])

    def test_media_service_gate_is_independent_from_core_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            source = PrivateWebMediaSource(str(Path(tmp) / "not-read.jpg"))
            reader = OwnerReader(ReadResult.success_empty(()))
            close_events: list[str] = []
            content_runtime = self._content_runtime(reader, close_events)
            media_runtime = PrivateWebMediaCreateRuntime(
                page_factory=lambda: (_ for _ in ()).throw(
                    AssertionError("media page must stay lazy")
                )
            )
            access = WriteApiAccess(
                principal="runtime-test",
                bearer_token=self.TOKEN,
                capabilities=frozenset(
                    {WriteCapability.CREATE, WriteCapability.CREATE_MEDIA}
                ),
                writes_enabled=True,
            )
            runtime = compose_private_web_write_api_runtime(
                content_runtime=content_runtime,
                media_runtime=media_runtime,
                media_resolver=PrivateWebMediaRefResolver({"cover": source}),
                store=store,
                access=access,
                core_writes_enabled=True,
                media_writes_enabled=False,
            )
            try:
                status, body = self._request(
                    runtime,
                    "POST",
                    "/api/write/media/ads",
                    payload={
                        **self._create_payload(),
                        "media_refs": ["cover"],
                    },
                    idempotency_key="media-service-gate",
                )
                self.assertEqual(status, 409)
                self.assertEqual(
                    body["operation_receipt"]["pre_read_status"],
                    "writes_disabled",
                )
                self.assertFalse(body["operation_receipt"]["writer_invoked"])
                self.assertEqual(reader.calls, 0)
            finally:
                runtime.close()
            self.assertEqual(close_events, ["content"])

    def test_media_route_uses_opaque_refs_and_write_api_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            media_path = Path(tmp) / "photo.jpg"
            media_path.write_bytes(b"jpeg-bytes")
            source = PrivateWebMediaSource(str(media_path))
            reader = SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_empty(()),
            )
            confirmation_reader = SequenceReader(
                ReadResult.success_empty(()),
                ReadResult.success_empty(()),
            )
            close_events: list[str] = []
            content_runtime = self._content_runtime(reader, close_events)
            confirmation_runtime = self._confirmation_runtime(
                confirmation_reader,
                close_events,
            )
            events: list[tuple] = []
            media_runtime = PrivateWebMediaCreateRuntime(
                page_factory=lambda: MediaCreatePage(events)
            )
            access = WriteApiAccess(
                principal="runtime-test",
                bearer_token=self.TOKEN,
                capabilities=frozenset({WriteCapability.CREATE_MEDIA}),
                writes_enabled=True,
            )
            runtime = compose_private_web_write_api_runtime(
                content_runtime=content_runtime,
                confirmation_runtime=confirmation_runtime,
                media_runtime=media_runtime,
                media_resolver=PrivateWebMediaRefResolver({"cover_01": source}),
                media_persistence_verifier=MediaPersistenceVerifier(),
                store=store,
                access=access,
                media_writes_enabled=True,
            )
            payload = {
                **self._create_payload(),
                "media_refs": ["cover_01"],
            }
            try:
                status, first = self._request(
                    runtime,
                    "POST",
                    "/api/write/media/ads",
                    payload=payload,
                    idempotency_key="media-runtime-idempotent",
                )
                self.assertEqual(status, 202)
                self.assertTrue(first["operation_receipt"]["writer_invoked"])
                self.assertFalse(first["platform_retry_authorized"])
                self.assertFalse(first["media_persistence_confirmed"])
                self.assertNotIn("media_refs", first)
                self.assertNotIn(str(media_path), json.dumps(first))

                replay_status, replay = self._request(
                    runtime,
                    "POST",
                    "/api/write/media/ads",
                    payload=payload,
                    idempotency_key="media-runtime-idempotent",
                )
                self.assertEqual(replay_status, 202)
                self.assertEqual(replay, first)
                self.assertEqual(
                    [event[0] for event in events].count("media_submit"),
                    1,
                )

                invalid_status, _ = self._request(
                    runtime,
                    "POST",
                    "/api/write/media/ads",
                    payload={
                        **self._create_payload(),
                        "media_refs": ["../photo.jpg"],
                    },
                    idempotency_key="media-runtime-path-rejected",
                )
                self.assertEqual(invalid_status, 400)
                self.assertEqual(
                    [event[0] for event in events].count("media_submit"),
                    1,
                )
            finally:
                runtime.close()
            self.assertEqual(close_events, ["confirmation", "content"])

    def test_builder_preserves_setup_error_when_cleanup_also_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            media_path = Path(tmp) / "photo.jpg"
            media_path.write_bytes(b"jpeg")
            source = PrivateWebMediaSource(str(media_path))
            content_runtime = PrivateWebContentRuntime(
                owner_reader=OwnerReader(ReadResult.success_empty(())),
                page_factory=lambda: (_ for _ in ()).throw(
                    AssertionError("browser page must stay lazy")
                ),
                close_runtime=lambda: (_ for _ in ()).throw(
                    RuntimeError("cleanup failed")
                ),
            )
            access = WriteApiAccess(
                principal="runtime-test",
                bearer_token=self.TOKEN,
                capabilities=frozenset({WriteCapability.CREATE_MEDIA}),
            )
            with (
                patch(
                    "mark_api.private_web_runtime.build_private_web_content_runtime",
                    return_value=content_runtime,
                ),
                patch(
                    "mark_api.private_web_runtime.build_private_web_media_create_runtime",
                    side_effect=RuntimeError("primary setup failed"),
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "primary setup failed",
                ):
                    build_private_web_write_api_runtime(
                        cdp_port=19610,
                        store=store,
                        access=access,
                        media_bindings={"cover": source},
                    )

if __name__ == "__main__":
    unittest.main()
