from __future__ import annotations

import unittest
from datetime import datetime, timezone
from importlib import metadata
from unittest.mock import patch

from mark_api.adapters.management import MANAGEMENT_URL
from mark_api.domain import AdSnapshot, LifecycleState
from mark_api.private_web import (
    PrivateWebEditorSnapshot,
    PrivateWebEditorState,
)
from mark_api.private_web_runtime import (
    PrivateWebContentRuntime,
    PrivateWebRuntimeClosedError,
    PrivateWebRuntimeDependencyError,
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
                side_effect=AssertionError("page must be lazy"),
            ) as pages,
        ):
            runtime = build_private_web_content_runtime(
                cdp_port=19610,
                timeout_seconds=4.0,
            )

        dependency.assert_called_once_with()
        cookies.assert_called_once_with(19610, timeout_seconds=4.0)
        self.assertEqual(management.call_count, 1)
        self.assertEqual(management.call_args.kwargs["endpoint"], MANAGEMENT_URL)
        self.assertNotIn("transport", management.call_args.kwargs)
        pages.assert_not_called()

        runtime.close()
        self.assertEqual(delegate.closed, 1)


if __name__ == "__main__":
    unittest.main()
