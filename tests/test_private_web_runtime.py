from __future__ import annotations

import tempfile
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
    PrivateWebMediaSource,
)
from mark_api.results import ReadResult, ReadStatus


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
        self.assertEqual(management.call_count, 1)
        self.assertEqual(management.call_args.kwargs["endpoint"], MANAGEMENT_URL)
        self.assertIs(
            management.call_args.kwargs["transport"],
            management_transport,
        )
        pages.assert_not_called()

        runtime.close()
        self.assertEqual(delegate.closed, 1)


if __name__ == "__main__":
    unittest.main()