from __future__ import annotations

import unittest
from datetime import datetime, timezone
from importlib import metadata
from unittest.mock import patch
from urllib.request import ProxyHandler

from mark_api.adapters.management import MANAGEMENT_URL
from mark_api.domain import AdSnapshot, DeleteApproval, LifecycleState, OperationOutcome
from mark_api.orchestrator import SafeWriteOrchestrator
from mark_api.ports import WriteNotAttemptedError
from mark_api.private_web import (
    PrivateWebDeleteSnapshot,
    PrivateWebEditorSnapshot,
    PrivateWebEditorState,
    PrivateWebStateSnapshot,
    PrivateWebSubmitUnknownError,
)
from mark_api.private_web_runtime import (
    PrivateWebContentRuntime,
    PrivateWebRuntimeClosedError,
    PrivateWebRuntimeDependencyError,
    PrivateWebRuntimeSetupError,
    _NoRedirectManagementTransport,
    _RejectManagementRedirectHandler,
    build_private_web_content_runtime,
    require_private_web_runtime_dependency,
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


class PrivateWebContentRuntimeTests(unittest.TestCase):
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
            runtime.content_reader_for(AD_ID)
        with self.assertRaises(PrivateWebRuntimeClosedError):
            runtime.content_writer.update_content(AD_ID, title="new")
        with self.assertRaises(PrivateWebRuntimeClosedError):
            runtime.state_writer.set_state(AD_ID, LifecycleState.PAUSED)
        with self.assertRaises(PrivateWebRuntimeClosedError):
            runtime.delete_writer.delete_ad(AD_ID)
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