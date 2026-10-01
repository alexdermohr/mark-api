from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
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
    PrivateWebCreateMediaPublishPage,
    PrivateWebCreateMediaSnapshot,
    PrivateWebCreateMediaWriter,
    PrivateWebMediaFileSnapshot,
    PrivateWebMediaSource,
    PrivateWebMediaUnknownError,
    _PreparedPrivateWebMedia,
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
        self.staged_paths: tuple[str, ...] = ()
        self.submit_staged_paths_exist: tuple[bool, ...] = ()
        self.submit_staged_bytes: tuple[bytes, ...] = ()
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
        self.staged_paths = files
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
        self.submit_staged_paths_exist = tuple(
            Path(path).exists() for path in self.staged_paths
        )
        self.submit_staged_bytes = tuple(
            Path(path).read_bytes() if Path(path).exists() else b""
            for path in self.staged_paths
        )
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


class WriterCdpPublishPage(CdpPrivateWebMediaPage):
    """Test harness: writer form methods plus the real media CDP path."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.form = create_snapshot()
        self.writer_stage_paths: tuple[str, ...] = ()
        self.writer_paths_alive_during_submit: tuple[bool, ...] = ()
        self.writer_bytes_during_submit: tuple[bytes, ...] = ()

    def open_create_form(self, category_path: tuple[str, ...]) -> None:
        self._create_bound = True
        self._create_submit_attempted = False
        self._last_create_snapshot = self.form

    def read_create_form(self) -> PrivateWebCreateSnapshot:
        self._last_create_snapshot = self.form
        return self.form

    def replace_create_title(self, value: str) -> None:
        self.form = replace(self.form, title=value)
        self._last_create_snapshot = self.form

    def replace_create_description(self, value: str) -> None:
        self.form = replace(self.form, description=value)
        self._last_create_snapshot = self.form

    def replace_create_price(self, value: str) -> None:
        self.form = replace(self.form, price_amount=value)
        self._last_create_snapshot = self.form

    def stage_create_media(self, files: tuple[str, ...]) -> None:
        self.writer_stage_paths = files
        super().stage_create_media(files)

    def submit_create_media(
        self,
        expected: PrivateWebCreateMediaSnapshot,
    ) -> None:
        self.writer_paths_alive_during_submit = tuple(
            Path(path).exists() for path in self.writer_stage_paths
        )
        self.writer_bytes_during_submit = tuple(
            Path(path).read_bytes() if Path(path).exists() else b""
            for path in self.writer_stage_paths
        )
        super().submit_create_media(expected)


class PrivateWebCreateMediaWriterTests(unittest.TestCase):
    def test_publish_page_protocol_keeps_media_free_submit_contract(self) -> None:
        self.assertIn("submit_create", PrivateWebCreateMediaPublishPage.__dict__)
        self.assertIn("submit_create_media", PrivateWebCreateMediaPublishPage.__dict__)

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

    def test_writer_owned_media_lives_through_submit_then_cleans(self) -> None:
        page = FakePublishPage()

        PrivateWebCreateMediaWriter(page).create_ad(
            create_request(),
            (self.source,),
        )

        self.assertEqual(page.submit_staged_paths_exist, (True,))
        self.assertEqual(page.submit_staged_bytes, (b"stable-media",))
        self.assertTrue(page.staged_paths)
        self.assertTrue(
            all(not Path(path).exists() for path in page.staged_paths)
        )

    def test_submit_unknown_survives_cleanup_failure(self) -> None:
        page = FakePublishPage(
            submit_error=PrivateWebSubmitUnknownError("create_media_submit")
        )
        original_close = _PreparedPrivateWebMedia.close

        def failing_close(prepared: _PreparedPrivateWebMedia) -> None:
            original_close(prepared)
            raise OSError("cleanup failed")

        with patch.object(_PreparedPrivateWebMedia, "close", new=failing_close):
            with self.assertRaises(PrivateWebSubmitUnknownError) as caught:
                PrivateWebCreateMediaWriter(page).create_ad(
                    create_request(),
                    (self.source,),
                )

        self.assertEqual(caught.exception.stage, "create_media_submit")
        self.assertEqual(
            [name for name, _value in page.calls].count("submit_create_media"),
            1,
        )

    def test_successful_submit_cleanup_failure_becomes_non_retryable_unknown(
        self,
    ) -> None:
        page = FakePublishPage()
        original_close = _PreparedPrivateWebMedia.close

        def failing_close(prepared: _PreparedPrivateWebMedia) -> None:
            original_close(prepared)
            raise OSError("cleanup failed")

        with patch.object(_PreparedPrivateWebMedia, "close", new=failing_close):
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

    def test_pre_submit_error_survives_cleanup_failure(self) -> None:
        page = FakePublishPage(
            media_override=media_snapshot("other.jpg", len(b"stable-media"))
        )
        original_close = _PreparedPrivateWebMedia.close

        def failing_close(prepared: _PreparedPrivateWebMedia) -> None:
            original_close(prepared)
            raise OSError("cleanup failed")

        with patch.object(_PreparedPrivateWebMedia, "close", new=failing_close):
            with self.assertRaises(PrivateWebMediaUnknownError) as caught:
                PrivateWebCreateMediaWriter(page).create_ad(
                    create_request(),
                    (self.source,),
                )

        self.assertEqual(caught.exception.stage, "media_readback")
        self.assertNotIn(
            "submit_create_media",
            [name for name, _value in page.calls],
        )

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

    def page(
        self,
        handler,
        **page_kwargs,
    ) -> tuple[CdpPrivateWebMediaPage, FakeClient]:
        client = FakeClient(handler)
        page = CdpPrivateWebMediaPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
            **page_kwargs,
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
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "type": "string",
                        "value": "confirmed",
                    }
                }
            if method == "Runtime.releaseObject":
                return {}
            raise AssertionError(method)

        page, client = self.page(handler)

        page.submit_create_media(self.expected_media)

        methods = [method for method, _params in client.calls]
        self.assertEqual(methods.count("Runtime.callFunctionOn"), 2)
        self.assertEqual(methods.count("Input.dispatchMouseEvent"), 2)
        self.assertEqual(methods.count("Runtime.evaluate"), 1)
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
        settlement_expression = str(
            next(
                params["expression"]
                for method, params in client.calls
                if method == "Runtime.evaluate"
            )
        )
        self.assertIn("/m-meine-anzeigen.html", settlement_expression)
        self.assertIn("/p-anzeige-aufgeben-schritt2.html", settlement_expression)
        self.assertIn('"confirmed"', settlement_expression)
        self.assertNotIn(".click(", settlement_expression)

        with self.assertRaises(PrivateWebCdpWriteNotAttemptedError):
            page.submit_create_media(self.expected_media)
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            2,
        )

    def test_submit_waits_for_delayed_post_submit_settlement(self) -> None:
        call_function_count = 0
        settlement_values = ["pending", "confirmed"]
        sleeps: list[float] = []

        def handler(method, params):
            nonlocal call_function_count
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
                return {}
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "type": "string",
                        "value": settlement_values.pop(0),
                    }
                }
            if method == "Runtime.releaseObject":
                return {}
            raise AssertionError(method)

        page, client = self.page(handler, sleep=sleeps.append)

        page.submit_create_media(self.expected_media)

        self.assertEqual(sleeps, [0.05])
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            2,
        )
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Runtime.evaluate"
            ),
            2,
        )
        self.assertIsNotNone(page._media_prepared)

    def test_submit_settlement_timeout_is_unknown_and_never_retried(
        self,
    ) -> None:
        call_function_count = 0
        input_calls = 0
        monotonic_values = iter((0.0, 1.0))

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
                return {}
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "type": "string",
                        "value": "pending",
                    }
                }
            if method == "Runtime.releaseObject":
                return {}
            raise AssertionError(method)

        page, _client = self.page(
            handler,
            timeout_seconds=0.5,
            sleep=lambda _seconds: None,
            monotonic=lambda: next(monotonic_values),
        )

        with self.assertRaises(PrivateWebSubmitUnknownError) as caught:
            page.submit_create_media(self.expected_media)

        self.assertEqual(
            caught.exception.stage,
            "create_media_submit_settle",
        )
        self.assertEqual(input_calls, 2)
        prepared = page._media_prepared
        self.assertIsNotNone(prepared)
        assert prepared is not None
        self.assertTrue(all(Path(path).exists() for path in prepared.paths))

        with self.assertRaises(PrivateWebCdpWriteNotAttemptedError):
            page.submit_create_media(self.expected_media)
        self.assertEqual(input_calls, 2)

    def test_unconfirmed_post_click_state_is_unknown_and_single_shot(
        self,
    ) -> None:
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
                return {}
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "type": "string",
                        "value": "unconfirmed",
                    }
                }
            if method == "Runtime.releaseObject":
                return {}
            raise AssertionError(method)

        page, _client = self.page(handler)

        with self.assertRaises(PrivateWebSubmitUnknownError) as caught:
            page.submit_create_media(self.expected_media)

        self.assertEqual(
            caught.exception.stage,
            "create_media_submit_settle",
        )
        self.assertEqual(input_calls, 2)
        with self.assertRaises(PrivateWebCdpWriteNotAttemptedError):
            page.submit_create_media(self.expected_media)
        self.assertEqual(input_calls, 2)

    def test_malformed_post_click_state_is_sanitized_as_unknown(
        self,
    ) -> None:
        call_function_count = 0

        def handler(method, params):
            nonlocal call_function_count
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
                return {}
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "type": "object",
                        "value": {"unexpected": "shape"},
                    }
                }
            if method == "Runtime.releaseObject":
                return {}
            raise AssertionError(method)

        page, _client = self.page(handler)

        with self.assertRaises(PrivateWebSubmitUnknownError) as caught:
            page.submit_create_media(self.expected_media)

        self.assertEqual(
            caught.exception.stage,
            "create_media_submit_settle",
        )

    def test_writer_and_cdp_media_lifetimes_span_submit_and_page_close(
        self,
    ) -> None:
        call_function_count = 0
        browser_paths: tuple[str, ...] = ()

        def handler(method, params):
            nonlocal call_function_count, browser_paths
            if method == "Runtime.evaluate":
                expression = str(params.get("expression", ""))
                if (
                    "/m-meine-anzeigen.html" in expression
                    and '"confirmed"' in expression
                ):
                    return {
                        "result": {
                            "type": "string",
                            "value": "confirmed",
                        }
                    }
                return {
                    "result": {
                        "type": "object",
                        "objectId": "file-input-1",
                    }
                }
            if method == "DOM.setFileInputFiles":
                browser_paths = tuple(params["files"])
                self.assertEqual(
                    tuple(Path(path).read_bytes() for path in browser_paths),
                    (b"123456",),
                )
                return {}
            if method == "Runtime.callFunctionOn":
                call_function_count += 1
                if call_function_count <= 2:
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

        client = FakeClient(handler)
        page = WriterCdpPublishPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        self.pages.append(page)

        PrivateWebCreateMediaWriter(page).create_ad(
            create_request(),
            (PrivateWebMediaSource(path=str(self.image)),),
        )

        self.assertEqual(page.writer_paths_alive_during_submit, (True,))
        self.assertEqual(page.writer_bytes_during_submit, (b"123456",))
        self.assertTrue(page.writer_stage_paths)
        self.assertTrue(
            all(not Path(path).exists() for path in page.writer_stage_paths)
        )
        self.assertTrue(browser_paths)
        self.assertTrue(all(Path(path).exists() for path in browser_paths))
        self.assertIsNotNone(page._media_prepared)

        page.close()

        self.assertTrue(all(not Path(path).exists() for path in browser_paths))

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