from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from mark_api.domain import AdCreateRequest
from mark_api.private_web import (
    PrivateWebCreateSnapshot,
    PrivateWebCreateWriter,
    PrivateWebEditorState,
    PrivateWebSubmitUnknownError,
)
from mark_api.private_web_cdp import (
    PrivateWebCdpError,
    PrivateWebCdpWriteNotAttemptedError,
)
from mark_api.private_web_cdp_media import CdpPrivateWebMediaPage
from mark_api.private_web_media import (
    PrivateWebCreateMediaSnapshot,
    PrivateWebCreateMediaWriter,
    PrivateWebMediaFileSnapshot,
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
    *,
    title: str = "",
    description: str = "",
    price_amount: str = "",
) -> PrivateWebCreateSnapshot:
    return PrivateWebCreateSnapshot(
        state=PrivateWebEditorState.READY,
        title=title,
        description=description,
        price_amount=price_amount,
    )


def media_snapshot(name: str, size: int) -> PrivateWebCreateMediaSnapshot:
    return PrivateWebCreateMediaSnapshot(
        state=PrivateWebEditorState.READY,
        files=(PrivateWebMediaFileSnapshot(name=name, size_bytes=size),),
    )


class FakePublishPage:
    def __init__(
        self,
        *,
        media_override: PrivateWebCreateMediaSnapshot | None = None,
        submit_error: Exception | None = None,
    ) -> None:
        self.form = create_snapshot()
        self.media: PrivateWebCreateMediaSnapshot | None = None
        self.media_override = media_override
        self.submit_error = submit_error
        self.staged_bytes: tuple[bytes, ...] = ()
        self.calls: list[tuple[str, object]] = []

    def open_create_form(self, category_path: tuple[str, ...]) -> None:
        self.calls.append(("open_create_form", category_path))

    def read_create_form(self) -> PrivateWebCreateSnapshot:
        self.calls.append(("read_create_form", None))
        return self.form

    def replace_create_title(self, value: str) -> None:
        self.calls.append(("replace_create_title", value))
        self.form = replace(self.form, title=value)

    def replace_create_description(self, value: str) -> None:
        self.calls.append(("replace_create_description", value))
        self.form = replace(self.form, description=value)

    def replace_create_price(self, value: str) -> None:
        self.calls.append(("replace_create_price", value))
        self.form = replace(self.form, price_amount=value)

    def submit_create(self) -> None:
        raise AssertionError("ordinary media-free submit must not be used")

    def stage_create_media(self, files: tuple[str, ...]) -> None:
        self.calls.append(("stage_create_media", files))
        self.staged_bytes = tuple(Path(path).read_bytes() for path in files)
        snapshots = tuple(
            PrivateWebMediaFileSnapshot(
                name=Path(path).name,
                size_bytes=Path(path).stat().st_size,
            )
            for path in files
        )
        self.media = PrivateWebCreateMediaSnapshot(
            state=PrivateWebEditorState.READY,
            files=snapshots,
        )

    def read_create_media(self) -> PrivateWebCreateMediaSnapshot:
        self.calls.append(("read_create_media", None))
        if self.media_override is not None:
            return self.media_override
        if self.media is None:
            raise AssertionError("media was not staged")
        return self.media

    def submit_create_media(
        self,
        expected: PrivateWebCreateMediaSnapshot,
    ) -> None:
        self.calls.append(("submit_create_media", expected))
        if self.submit_error is not None:
            raise self.submit_error


class FakeClient:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        actual = dict(params or {})
        self.calls.append((method, actual))
        return self.handler(method, actual)

    def close(self) -> None:
        return None


class PrivateWebCreateMediaWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.image = self.root / "photo.jpg"
        self.image.write_bytes(b"stable-media")
        self.source = PrivateWebMediaSource(path=str(self.image))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_invalid_media_fails_before_any_browser_access(self) -> None:
        page = FakePublishPage()

        with self.assertRaises(ValueError):
            PrivateWebCreateMediaWriter(page).create_ad(
                create_request(),
                (
                    PrivateWebMediaSource(
                        path=str(self.root / "missing.jpg")
                    ),
                ),
            )

        self.assertEqual(page.calls, [])

    def test_prepare_create_remains_non_publishing(self) -> None:
        page = FakePublishPage()

        result = PrivateWebCreateWriter(page).prepare_create(create_request())

        self.assertEqual(
            result,
            create_snapshot(
                title="Neue Vase",
                description="Beschreibung",
                price_amount="12",
            ),
        )
        self.assertNotIn("submit_create_media", [name for name, _ in page.calls])

    def test_writer_prepares_stages_and_uses_only_media_publish(self) -> None:
        page = FakePublishPage()

        PrivateWebCreateMediaWriter(page).create_ad(
            create_request(),
            (self.source,),
        )

        names = [name for name, _value in page.calls]
        self.assertEqual(names.count("stage_create_media"), 1)
        self.assertEqual(names.count("submit_create_media"), 1)
        self.assertNotIn("submit_create", names)
        staged_paths = next(
            value for name, value in page.calls if name == "stage_create_media"
        )
        self.assertIsInstance(staged_paths, tuple)
        self.assertEqual(len(staged_paths), 1)
        staged_path = Path(staged_paths[0])
        self.assertNotEqual(staged_path, self.image)
        self.assertEqual(page.staged_bytes, (b"stable-media",))
        # The outer stable copy is cleaned after the writer returns.
        self.assertFalse(staged_path.exists())

    def test_media_drift_blocks_publish(self) -> None:
        page = FakePublishPage(
            media_override=media_snapshot("other.jpg", len(b"stable-media"))
        )

        with self.assertRaises(PrivateWebMediaUnknownError):
            PrivateWebCreateMediaWriter(page).create_ad(
                create_request(),
                (self.source,),
            )

        self.assertNotIn(
            "submit_create_media",
            [name for name, _value in page.calls],
        )

    def test_unmarked_media_publish_failure_is_unknown_and_not_retried(self) -> None:
        page = FakePublishPage(submit_error=RuntimeError("provider detail"))

        with self.assertRaises(PrivateWebSubmitUnknownError) as caught:
            PrivateWebCreateMediaWriter(page).create_ad(
                create_request(),
                (self.source,),
            )

        self.assertEqual(caught.exception.stage, "submit_create_media")
        self.assertEqual(
            [name for name, _value in page.calls].count("submit_create_media"),
            1,
        )
        self.assertNotIn("provider detail", str(caught.exception))


class CdpPrivateWebMediaPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.image = self.root / "photo.jpg"
        self.image.write_bytes(b"123456")
        self.expected_create = create_snapshot(
            title="Neue Vase",
            description="Beschreibung",
            price_amount="12",
        )
        self.expected_media = media_snapshot("photo.jpg", 6)
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
        prepared = _prepare_local_media(
            (PrivateWebMediaSource(path=str(self.image)),)
        )
        page._create_bound = True
        page._last_create_snapshot = None
        page._media_selection_attempted = True
        page._media_expected_create_snapshot = self.expected_create
        page._media_file_object_id = "file-input-1"
        page._media_prepared = prepared
        self.pages.append(page)
        return page, client

    def test_submit_revalidates_same_handle_and_dispatches_one_click_pair(self) -> None:
        call_function_count = 0

        def handler(method, params):
            nonlocal call_function_count
            if method == "Runtime.callFunctionOn":
                call_function_count += 1
                self.assertEqual(params.get("objectId"), "file-input-1")
                if call_function_count == 1:
                    return {
                        "result": {
                            "type": "object",
                            "value": {
                                "state": "ready",
                                "files": [
                                    {"name": "photo.jpg", "size_bytes": 6}
                                ],
                            },
                        }
                    }
                return {
                    "result": {
                        "type": "object",
                        "value": {"state": "ready", "x": 10.0, "y": 20.0},
                    }
                }
            if method == "Input.dispatchMouseEvent":
                return {}
            if method == "Runtime.releaseObject":
                return {}
            raise AssertionError(method)

        page, client = self.page(handler)

        page.submit_create_media(self.expected_media)

        methods = [method for method, _params in client.calls]
        self.assertEqual(methods.count("Runtime.callFunctionOn"), 2)
        self.assertEqual(methods.count("Input.dispatchMouseEvent"), 2)
        activation = str(
            [
                params["functionDeclaration"]
                for method, params in client.calls
                if method == "Runtime.callFunctionOn"
            ][1]
        )
        self.assertIn('"Anzeige aufgeben"', activation)
        self.assertIn('"photo.jpg"', activation)
        self.assertIn('"size_bytes":6', activation)
        self.assertIn("files[0] !== input", activation)
        self.assertNotIn(".click(", activation)

        with self.assertRaises(PrivateWebCdpWriteNotAttemptedError):
            page.submit_create_media(self.expected_media)
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            2,
        )

    def test_media_drift_before_click_is_not_attempted(self) -> None:
        def handler(method, params):
            if method == "Runtime.callFunctionOn":
                return {
                    "result": {
                        "type": "object",
                        "value": {
                            "state": "ready",
                            "files": [
                                {"name": "other.jpg", "size_bytes": 6}
                            ],
                        },
                    }
                }
            if method == "Runtime.releaseObject":
                return {}
            raise AssertionError(method)

        page, client = self.page(handler)

        with self.assertRaises(PrivateWebCdpWriteNotAttemptedError):
            page.submit_create_media(self.expected_media)

        self.assertNotIn(
            "Input.dispatchMouseEvent",
            [method for method, _params in client.calls],
        )

    def test_input_failure_is_unknown_and_publish_is_single_shot(self) -> None:
        call_function_count = 0
        input_calls = 0

        def handler(method, params):
            nonlocal call_function_count, input_calls
            if method == "Runtime.callFunctionOn":
                call_function_count += 1
                if call_function_count == 1:
                    return {
                        "result": {
                            "type": "object",
                            "value": {
                                "state": "ready",
                                "files": [
                                    {"name": "photo.jpg", "size_bytes": 6}
                                ],
                            },
                        }
                    }
                return {
                    "result": {
                        "type": "object",
                        "value": {"state": "ready", "x": 10.0, "y": 20.0},
                    }
                }
            if method == "Input.dispatchMouseEvent":
                input_calls += 1
                raise PrivateWebCdpError("call")
            if method == "Runtime.releaseObject":
                return {}
            raise AssertionError(method)

        page, _client = self.page(handler)

        with self.assertRaises(PrivateWebSubmitUnknownError) as caught:
            page.submit_create_media(self.expected_media)

        self.assertEqual(caught.exception.stage, "create_media_submit")
        self.assertEqual(input_calls, 1)
        with self.assertRaises(PrivateWebCdpWriteNotAttemptedError):
            page.submit_create_media(self.expected_media)
        self.assertEqual(input_calls, 1)


if __name__ == "__main__":
    unittest.main()