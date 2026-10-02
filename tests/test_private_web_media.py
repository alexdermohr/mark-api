from __future__ import annotations

import gc
import subprocess
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

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
    PrivateWebMediaRefRegistry,
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


class PrivateWebMediaContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.image = self.root / "photo.jpg"
        self.image.write_bytes(b"local-test-image")
        self.source = PrivateWebMediaSource(path=str(self.image))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_media_ref_registry_stabilizes_sources_and_preserves_order(
        self,
    ) -> None:
        detail = self.root / "detail.jpg"
        detail.write_bytes(b"detail-image")
        registry = PrivateWebMediaRefRegistry(
            {
                "cover_01": self.source,
                "detail_02": PrivateWebMediaSource(path=str(detail)),
            }
        )

        self.image.write_bytes(b"changed-after-registration")
        detail.unlink()

        with registry.acquire(("detail_02", "cover_01")) as sources:
            stable_paths = tuple(Path(source.path) for source in sources)
            self.assertEqual(
                tuple(path.read_bytes() for path in stable_paths),
                (b"detail-image", b"local-test-image"),
            )
            self.assertNotIn(str(self.root), repr(registry))

        registry.close()
        self.assertTrue(all(not path.exists() for path in stable_paths))

    def test_media_ref_registry_holds_sources_for_active_lease(self) -> None:
        registry = PrivateWebMediaRefRegistry({"cover_01": self.source})

        with registry.acquire(("cover_01",)) as sources:
            stable_path = Path(sources[0].path)
            self.assertTrue(stable_path.exists())
            with self.assertRaisesRegex(RuntimeError, "active leases"):
                registry.close()
            self.assertTrue(stable_path.exists())

        registry.close()
        self.assertFalse(stable_path.exists())

    def test_media_ref_registry_rejects_unknown_or_invalid_refs(self) -> None:
        registry = PrivateWebMediaRefRegistry({"cover_01": self.source})

        for refs in (
            ("missing",),
            ("../photo",),
            ("cover_01", "cover_01"),
            (),
        ):
            with self.subTest(refs=refs):
                with self.assertRaises(PrivateWebWriteNotAttemptedError):
                    with registry.acquire(refs):
                        self.fail("invalid media refs must not resolve")

        registry.close()

    def test_media_ref_registry_stabilizes_before_original_disappears(
        self,
    ) -> None:
        registry = PrivateWebMediaRefRegistry({"cover_01": self.source})
        self.image.unlink()

        with registry.acquire(("cover_01",)) as sources:
            self.assertEqual(
                Path(sources[0].path).read_bytes(),
                b"local-test-image",
            )

        registry.close()

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