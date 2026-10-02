from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler

from mark_api.adapters.management import MANAGEMENT_URL
from mark_api.domain import AdCreateRequest, AdSnapshot, DeleteApproval, LifecycleState, OperationOutcome
from mark_api.orchestrator import SafeWriteOrchestrator
from mark_api.ports import WriteNotAttemptedError
from mark_api.private_web import (
    PrivateWebCreateSnapshot,
    PrivateWebDeleteSnapshot,
    PrivateWebEditorSnapshot,
    PrivateWebEditorState,
    PrivateWebStateSnapshot,
    PrivateWebSubmitUnknownError,
)
from mark_api.private_web_runtime import (
    PrivateWebContentRuntime,
    PrivateWebInventoryRuntime,
    PrivateWebMediaCreateRuntime,
    PrivateWebMediaCreateService,
    PrivateWebRuntimeClosedError,
    PrivateWebRuntimeDependencyError,
    PrivateWebRuntimeSetupError,
    _NoRedirectManagementTransport,
    _RejectManagementRedirectHandler,
    build_private_web_content_runtime,
    build_private_web_inventory_runtime,
    build_private_web_media_create_runtime,
    require_private_web_runtime_dependency,
)
from mark_api.private_web_media import (
    PrivateWebCreateMediaSnapshot,
    PrivateWebMediaFileSnapshot,
    PrivateWebMediaRefRegistry,
    PrivateWebMediaSource,
)
from mark_api.results import ReadResult, ReadStatus
from mark_api.storage import SnapshotStore


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

    def test_media_create_service_resolves_refs_through_safe_orchestrator(
        self,
    ) -> None:
        events: list[tuple] = []
        page = MediaCreatePage(events)
        runtime = PrivateWebMediaCreateRuntime(page_factory=lambda: page)
        registry = PrivateWebMediaRefRegistry({"cover_01": self.sources[0]})
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
        primary = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty((created_inventory,)),
        )
        confirmation = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty((created_inventory,)),
        )
        content_calls: list[str] = []
        db_path = Path(self.tmp.name) / "media-service.sqlite"
        service = PrivateWebMediaCreateService(
            runtime=runtime,
            registry=registry,
            reader=primary,
            confirmation_reader=confirmation,
            content_reader_factory=lambda ad_id: (
                content_calls.append(ad_id)
                or OwnerReader(
                    ReadResult.success_nonempty((created_content,))
                )
            ),
            store=SnapshotStore(db_path),
            writes_enabled=True,
        )

        receipt = service.create_with_media(
            self.request,
            ("cover_01",),
            authorization_by="test-owner",
            authorization_reference="opaque-media-test",
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(receipt.created_ad_id, created_id)
        self.assertEqual(receipt.authorization_by, "test-owner")
        self.assertEqual(
            receipt.authorization_reference,
            "opaque-media-test",
        )
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
        registry.close()
        runtime.close()

    def test_media_create_service_unknown_ref_is_precondition_failed(
        self,
    ) -> None:
        page_calls = 0

        def page_factory() -> MediaCreatePage:
            nonlocal page_calls
            page_calls += 1
            return MediaCreatePage([])

        runtime = PrivateWebMediaCreateRuntime(page_factory=page_factory)
        registry = PrivateWebMediaRefRegistry({"cover_01": self.sources[0]})
        primary = SequenceReader(ReadResult.success_empty(()))
        confirmation = SequenceReader(ReadResult.success_empty(()))
        content_calls: list[str] = []
        service = PrivateWebMediaCreateService(
            runtime=runtime,
            registry=registry,
            reader=primary,
            confirmation_reader=confirmation,
            content_reader_factory=lambda ad_id: (
                content_calls.append(ad_id)
                or OwnerReader(ReadResult.success_empty(()))
            ),
            writes_enabled=True,
        )

        receipt = service.create_with_media(
            self.request,
            ("missing",),
            authorization_by="test-owner",
            authorization_reference="opaque-media-missing",
        )

        self.assertEqual(
            receipt.outcome,
            OperationOutcome.PRECONDITION_FAILED,
        )
        self.assertEqual(receipt.pre_read_status, "media_refs_unavailable")
        self.assertEqual(receipt.confirmation_pre_read_status, "not_read")
        self.assertFalse(receipt.writer_invoked)
        self.assertIsNone(receipt.writer_error)
        self.assertIsNone(receipt.post_read_status)
        self.assertIsNone(receipt.confirmation_post_read_status)
        self.assertIsNone(receipt.content_post_read_status)
        self.assertEqual(primary.calls, 0)
        self.assertEqual(confirmation.calls, 0)
        self.assertEqual(content_calls, [])
        self.assertEqual(page_calls, 0)
        registry.close()
        runtime.close()

    def test_media_create_service_writes_disabled_skips_registry(self) -> None:
        runtime = PrivateWebMediaCreateRuntime(
            page_factory=lambda: MediaCreatePage([])
        )
        registry = PrivateWebMediaRefRegistry({"cover_01": self.sources[0]})
        registry.close()
        service = PrivateWebMediaCreateService(
            runtime=runtime,
            registry=registry,
            reader=OwnerReader(ReadResult.success_empty(())),
            confirmation_reader=OwnerReader(ReadResult.success_empty(())),
            content_reader_factory=lambda _ad_id: OwnerReader(
                ReadResult.success_empty(())
            ),
            writes_enabled=False,
        )

        receipt = service.create_with_media(self.request, ("cover_01",))

        self.assertEqual(
            receipt.outcome,
            OperationOutcome.PRECONDITION_FAILED,
        )
        self.assertEqual(receipt.pre_read_status, "writes_disabled")
        self.assertFalse(receipt.writer_invoked)
        runtime.close()

    def test_media_create_service_serializes_complete_orchestration(
        self,
    ) -> None:
        runtime = PrivateWebMediaCreateRuntime(
            page_factory=lambda: MediaCreatePage([])
        )
        registry = PrivateWebMediaRefRegistry(
            {"cover_01": self.sources[0]}
        )
        reader = OwnerReader(ReadResult.success_empty(()))
        confirmation = OwnerReader(ReadResult.success_empty(()))
        service = PrivateWebMediaCreateService(
            runtime=runtime,
            registry=registry,
            reader=reader,
            confirmation_reader=confirmation,
            content_reader_factory=lambda _ad_id: OwnerReader(
                ReadResult.success_empty(())
            ),
            writes_enabled=True,
        )

        first_entered = threading.Event()
        first_release = threading.Event()
        second_attempted = threading.Event()
        second_entered = threading.Event()
        calls: list[str | None] = []
        errors: list[BaseException] = []

        class BlockingWrites:
            def create(self, **kwargs):
                reference = kwargs.get("authorization_reference")
                calls.append(reference)
                if len(calls) == 1:
                    first_entered.set()
                    if not first_release.wait(timeout=2):
                        raise AssertionError("first create was not released")
                else:
                    second_entered.set()
                return object()

        service._writes = BlockingWrites()

        def invoke(reference: str, attempted: threading.Event | None = None) -> None:
            if attempted is not None:
                attempted.set()
            try:
                service.create_with_media(
                    self.request,
                    ("cover_01",),
                    authorization_reference=reference,
                )
            except BaseException as exc:
                errors.append(exc)

        first = threading.Thread(target=invoke, args=("first",))
        second = threading.Thread(
            target=invoke,
            args=("second", second_attempted),
        )
        first.start()
        self.assertTrue(first_entered.wait(timeout=2))
        second.start()
        self.assertTrue(second_attempted.wait(timeout=2))

        self.assertFalse(second_entered.wait(timeout=0.1))
        self.assertEqual(calls, ["first"])

        first_release.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_entered.is_set())
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(errors, [])

        registry.close()
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
        with patch(
            "mark_api.private_web_runtime.metadata.version",
            side_effect=metadata.PackageNotFoundError("websocket-client"),
        ):
            with self.assertRaises(PrivateWebRuntimeDependencyError) as caught:
                require_private_web_runtime_dependency()

        self.assertNotIn("websocket-client", str(caught.exception).lower())

    def test_dependency_check_fails_when_import_module_is_missing(self) -> None:
        with (
            patch(
                "mark_api.private_web_runtime.metadata.version",
                return_value="1.9.0",
            ),
            patch(
                "mark_api.private_web_runtime.import_module",
                side_effect=ModuleNotFoundError("websocket"),
            ),
        ):
            with self.assertRaises(PrivateWebRuntimeDependencyError):
                require_private_web_runtime_dependency()

    def test_dependency_check_rejects_shadow_module_without_client_api(self) -> None:
        with (
            patch(
                "mark_api.private_web_runtime.metadata.version",
                return_value="1.9.0",
            ),
            patch(
                "mark_api.private_web_runtime.import_module",
                return_value=object(),
            ),
        ):
            with self.assertRaises(PrivateWebRuntimeDependencyError):
                require_private_web_runtime_dependency()

    def test_dependency_check_accepts_declared_distribution_and_module(self) -> None:
        with (
            patch(
                "mark_api.private_web_runtime.metadata.version",
                return_value="1.9.0",
            ),
            patch(
                "mark_api.private_web_runtime.import_module",
                return_value=type(
                    "WebSocketModule",
                    (),
                    {"create_connection": staticmethod(lambda *args, **kwargs: None)},
                )(),
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

        with (
            patch(
                "mark_api.private_web_runtime.require_private_web_runtime_dependency"
            ) as dependency,
            patch(
                "mark_api.private_web_runtime.CdpCookieProvider.from_port",
                return_value=delegate,
            ) as cookies,
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
        self.assertEqual(management.call_count, 1)
        self.assertEqual(management.call_args.kwargs["endpoint"], MANAGEMENT_URL)
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
        transport_factory.assert_called_once_with()