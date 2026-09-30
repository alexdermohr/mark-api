from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from mark_api.domain import AdSnapshot, LifecycleState, OperationOutcome
from mark_api.orchestrator import SafeWriteOrchestrator
from mark_api.private_web import (
    PrivateWebCreateSnapshot,
    PrivateWebDeleteSnapshot,
    PrivateWebEditorSnapshot,
    PrivateWebEditorState,
    PrivateWebStateSnapshot,
    PrivateWebSubmitUnknownError,
)
from mark_api.private_web_cdp import (
    CdpCookieProvider,
    CdpPrivateWebOwnerReader,
    CdpPrivateWebPage,
    PrivateWebCdpError,
    PrivateWebCdpWriteNotAttemptedError,
    _LoopbackCdpClient,
    _proxy_free_loopback_opener,
    _validated_loopback_endpoint,
)
from mark_api.results import ReadResult, ReadStatus


AD_ID = "3524046688"


class FakeClient:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def call(self, method: str, params=None):
        actual = dict(params or {})
        self.calls.append((method, actual))
        return self.handler(method, actual)

    def close(self) -> None:
        self.closed = True


class FakeResponse:
    def __init__(
        self,
        payload: object,
        *,
        url: str = "http://127.0.0.1:19610/json/list",
    ) -> None:
        self._payload = json.dumps(payload).encode("utf-8")
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, size: int) -> bytes:
        return self._payload[:size]


class FakeSocket:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.sent: list[dict[str, object]] = []
        self.closed = False

    def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def recv(self) -> str:
        if not self.responses:
            raise AssertionError("unexpected recv")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return json.dumps(value)

    def close(self) -> None:
        self.closed = True


class FakeTcpSocket:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def bind_ready_editor(
    page: CdpPrivateWebPage,
    *,
    title: str = "Existing title",
    description: str = "Existing description",
) -> None:
    page._bound_ad_id = AD_ID
    page._last_ready_snapshot = PrivateWebEditorSnapshot(
        state=PrivateWebEditorState.READY,
        ad_id=AD_ID,
        title=title,
        description=description,
    )


def create_ready(
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


def bind_ready_create(
    page: CdpPrivateWebPage,
    *,
    title: str = "Neue Vase",
    description: str = "Beschreibung",
    price_amount: str = "12",
) -> None:
    page._create_bound = True
    page._last_create_snapshot = create_ready(
        title=title,
        description=description,
        price_amount=price_amount,
    )


def page_with_results(*values):
    clients: list[FakeClient] = []
    remaining = list(values)

    def factory():
        if clients:
            raise AssertionError("unexpected second client")

        def handler(method, params):
            if method == "Runtime.evaluate":
                if not remaining:
                    raise AssertionError("unexpected Runtime.evaluate")
                value = remaining.pop(0)
                if isinstance(value, Exception):
                    raise value
                return {"result": {"type": "object", "value": value}}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        clients.append(client)
        return client

    return CdpPrivateWebPage(
        "http://127.0.0.1:19610",
        client_factory=factory,
    ), clients

class CdpPrivateWebPageTests(unittest.TestCase):

    def test_open_create_form_uses_exact_labels_and_browser_input_only(self) -> None:
        ready_form = {
            "state": "ready",
            "title": "",
            "description": "",
            "price_amount": "",
        }
        runtime_values = [
            True,
            {
                "readyState": "complete",
                "origin": "https://www.kleinanzeigen.de",
                "path": "/p-anzeige-aufgeben.html",
                "oldDocument": False,
            },
            {"state": "ready", "x": 10.0, "y": 20.0},
            {"state": "ready", "x": 11.0, "y": 21.0},
            {"state": "ready", "x": 12.0, "y": 22.0},
            {"state": "ready", "x": 13.0, "y": 23.0},
            ready_form,
        ]

        def handler(method, params):
            if method == "Page.navigate":
                return {}
            if method == "Runtime.evaluate":
                if not runtime_values:
                    raise AssertionError("unexpected Runtime.evaluate")
                value = runtime_values.pop(0)
                return {"result": {"type": "object", "value": value}}
            if method == "Input.dispatchMouseEvent":
                return {}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )

        page.open_create_form(
            ("Haus & Garten", "Dekoration", "Weitere Dekoration")
        )

        self.assertEqual(runtime_values, [])
        self.assertTrue(page._create_bound)
        self.assertIsNone(page._last_create_snapshot)
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            8,
        )
        expressions = [
            str(params["expression"])
            for method, params in client.calls
            if method == "Runtime.evaluate" and "expression" in params
        ]
        joined = "\n".join(expressions)
        self.assertIn('"Haus & Garten"', joined)
        self.assertIn('"Dekoration"', joined)
        self.assertIn('"Weitere Dekoration"', joined)
        self.assertIn("/p-anzeige-aufgeben-schritt2.html", joined)
        category_expressions = [
            expression
            for expression in expressions
            if 'if ((link.innerText || "").trim() !==' in expression
        ]
        self.assertEqual(len(category_expressions), 3)
        for expression in category_expressions:
            self.assertIn("link.scrollIntoView", expression)
            self.assertIn('behavior: "instant"', expression)
            self.assertLess(
                expression.index("matches.length !== 1"),
                expression.index("link.scrollIntoView"),
            )
            self.assertLess(
                expression.index("link.scrollIntoView"),
                expression.index("document.elementFromPoint"),
            )
            javascript_check = subprocess.run(
                ["node", "--check", "-"],
                input=expression,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(
                javascript_check.returncode,
                0,
                javascript_check.stdout + javascript_check.stderr,
            )
        self.assertNotIn(".click(", joined)

    def test_open_create_form_never_retries_ambiguous_continue_input(self) -> None:
        runtime_values = [
            True,
            {
                "readyState": "complete",
                "origin": "https://www.kleinanzeigen.de",
                "path": "/p-anzeige-aufgeben.html",
                "oldDocument": False,
            },
            {"state": "ready", "x": 10.0, "y": 20.0},
            {"state": "ready", "x": 11.0, "y": 21.0},
            {"state": "ready", "x": 12.0, "y": 22.0},
            {"state": "ready", "x": 13.0, "y": 23.0},
            {
                "state": "ready",
                "title": "",
                "description": "",
                "price_amount": "",
            },
        ]
        input_calls = 0

        def handler(method, params):
            nonlocal input_calls
            if method == "Page.navigate":
                return {}
            if method == "Runtime.evaluate":
                value = runtime_values.pop(0)
                return {"result": {"type": "object", "value": value}}
            if method == "Input.dispatchMouseEvent":
                input_calls += 1
                if input_calls == 7:
                    raise PrivateWebCdpError("call")
                return {}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )

        page.open_create_form(
            ("Haus & Garten", "Dekoration", "Weitere Dekoration")
        )

        self.assertTrue(page._create_bound)
        self.assertEqual(input_calls, 7)
        self.assertEqual(runtime_values, [])

    def test_read_create_form_binds_only_supported_private_offer_baseline(self) -> None:
        client = FakeClient(
            lambda method, params: (
                {
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
                if method == "Runtime.evaluate"
                else (_ for _ in ()).throw(
                    AssertionError(f"unexpected method: {method}")
                )
            )
        )
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        page._create_bound = True

        snapshot = page.read_create_form()

        self.assertEqual(
            snapshot,
            create_ready(
                title="Neue Vase",
                description="Beschreibung",
                price_amount="12",
            ),
        )
        self.assertEqual(page._last_create_snapshot, snapshot)
        expression = str(client.calls[0][1]["expression"])
        for required in (
            'buyNowEligible.value !== "false"',
            'posterType.value !== "PRIVATE"',
            "addressVisibility.checked",
            "marketing.checked",
            "files[0].files.length !== 0",
            'adDraftUuid.value !== ""',
            'adId.value !== ""',
            'offer.value !== "OFFER"',
            'priceType.value !== "FIXED"',
            "unexpected.length !== 0",
            'normalizedButtonText(button) === "Anzeige aufgeben"',
            "city.form !== form",
            "street.form !== form",
            "addressVisibility.form !== form",
            "marketing.form !== form",
            "priceType.form !== form",
            "buyNowEligible.form !== form",
            "posterType.form !== form",
            "locationId.form !== form",
            "categoryId.form !== form",
            "adDraftUuid.form !== form",
            "adId.form !== form",
            "offer.form !== form",
            "wanted.form !== form",
            "files[0].form !== form",
        ):
            self.assertIn(required, expression)
        self.assertNotIn(".click(", expression)
        javascript_check = subprocess.run(
            ["node", "--check", "-"],
            input=expression,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            javascript_check.returncode,
            0,
            javascript_check.stdout + javascript_check.stderr,
        )

    def test_replace_create_title_uses_native_value_setter(self) -> None:
        runtime_values = [
            {
                "state": "ready",
                "title": "Alt",
                "description": "Beschreibung",
                "price_amount": "12",
            },
            True,
        ]

        def handler(method, params):
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "type": "object",
                        "value": runtime_values.pop(0),
                    }
                }
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        bind_ready_create(
            page,
            title="Alt",
            description="Beschreibung",
            price_amount="12",
        )

        page.replace_create_title("Neu")

        self.assertEqual(
            page._last_create_snapshot,
            create_ready(
                title="Neu",
                description="Beschreibung",
                price_amount="12",
            ),
        )
        expression = str(client.calls[1][1]["expression"])
        self.assertIn("Object.getOwnPropertyDescriptor", expression)
        self.assertIn("new InputEvent", expression)
        self.assertIn('element.getAttribute("name") !== "title"', expression)
        self.assertNotIn(".click(", expression)

    def test_submit_create_uses_one_browser_input_pair_and_is_single_shot(self) -> None:
        client = FakeClient(
            lambda method, params: (
                {
                    "result": {
                        "type": "object",
                        "value": {"state": "ready", "x": 10.0, "y": 20.0},
                    }
                }
                if method == "Runtime.evaluate"
                else {}
                if method == "Input.dispatchMouseEvent"
                else (_ for _ in ()).throw(
                    AssertionError(f"unexpected method: {method}")
                )
            )
        )
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        bind_ready_create(page)

        page.submit_create()

        self.assertEqual(
            [method for method, _params in client.calls],
            [
                "Runtime.evaluate",
                "Input.dispatchMouseEvent",
                "Input.dispatchMouseEvent",
            ],
        )
        expression = str(client.calls[0][1]["expression"])
        self.assertIn('"Anzeige aufgeben"', expression)
        self.assertIn('"Neue Vase"', expression)
        self.assertIn('"Beschreibung"', expression)
        self.assertIn('"12"', expression)
        self.assertNotIn(".click(", expression)
        with self.assertRaises(PrivateWebCdpWriteNotAttemptedError):
            page.submit_create()
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            2,
        )

    def test_submit_create_provider_failure_after_input_is_unknown_no_retry(self) -> None:
        input_calls = 0

        def handler(method, params):
            nonlocal input_calls
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "type": "object",
                        "value": {"state": "ready", "x": 10.0, "y": 20.0},
                    }
                }
            if method == "Input.dispatchMouseEvent":
                input_calls += 1
                raise PrivateWebCdpError("call")
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        bind_ready_create(page)

        with self.assertRaises(PrivateWebSubmitUnknownError) as caught:
            page.submit_create()

        self.assertEqual(caught.exception.stage, "create_submit")
        self.assertEqual(input_calls, 1)
        with self.assertRaises(PrivateWebCdpWriteNotAttemptedError):
            page.submit_create()
        self.assertEqual(input_calls, 1)

    def test_endpoint_is_strict_loopback_http(self) -> None:
        self.assertEqual(
            _validated_loopback_endpoint("http://127.0.0.1:19610"),
            ("http://127.0.0.1:19610", 19610),
        )
        for invalid in (
            "https://127.0.0.1:19610",
            "http://localhost:19610",
            "http://127.0.0.1:19610/json",
            "http://127.0.0.1:19610?x=1",
            "http://user@127.0.0.1:19610",
            "http://127.0.0.1",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    _validated_loopback_endpoint(invalid)

    def test_nonfinite_timeouts_are_rejected(self) -> None:
        for constructor in (CdpCookieProvider, CdpPrivateWebPage):
            for timeout_seconds in (
                float("nan"),
                float("inf"),
                float("-inf"),
            ):
                with self.subTest(
                    constructor=constructor.__name__,
                    timeout_seconds=timeout_seconds,
                ):
                    with self.assertRaisesRegex(ValueError, "finite"):
                        constructor(
                            "http://127.0.0.1:19610",
                            timeout_seconds=timeout_seconds,
                        )

    def test_open_editor_navigates_only_to_exact_ad_id(self) -> None:
        runtime_values = [
            True,
            {"readyState": "complete", "oldDocument": False},
            {
                "state": "ready",
                "ad_id": AD_ID,
                "title": "Existing title",
                "description": "Existing description",
            },
        ]

        def handler(method, params):
            if method == "Page.navigate":
                return {}
            if method == "Runtime.evaluate":
                value = runtime_values.pop(0)
                return {"result": {"type": "object", "value": value}}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )

        page.open_editor(AD_ID)

        self.assertEqual(client.calls[0][0], "Runtime.evaluate")
        self.assertIn(
            "__mark_private_web_navigation_probe_",
            str(client.calls[0][1]["expression"]),
        )
        self.assertEqual(client.calls[1][0], "Page.navigate")
        target = client.calls[1][1]["url"]
        parsed = urlparse(str(target))
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "www.kleinanzeigen.de")
        self.assertEqual(parsed.path, "/p-anzeige-bearbeiten.html")
        self.assertEqual(parse_qs(parsed.query), {"adId": [AD_ID]})
        self.assertFalse(client.closed)
        page.close()
        self.assertTrue(client.closed)

    def test_open_editor_waits_until_old_complete_document_is_replaced(self) -> None:
        runtime_values = [
            True,
            {"readyState": "complete", "oldDocument": True},
            {"readyState": "complete", "oldDocument": False},
            {
                "state": "ready",
                "ad_id": AD_ID,
                "title": "Existing title",
                "description": "Existing description",
            },
        ]

        def handler(method, params):
            if method == "Page.navigate":
                return {}
            if method == "Runtime.evaluate":
                value = runtime_values.pop(0)
                return {"result": {"type": "object", "value": value}}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
            sleep=lambda _seconds: None,
        )

        page.open_editor(AD_ID)

        self.assertEqual(runtime_values, [])
        self.assertEqual(
            [method for method, _params in client.calls],
            [
                "Runtime.evaluate",
                "Page.navigate",
                "Runtime.evaluate",
                "Runtime.evaluate",
                "Runtime.evaluate",
            ],
        )

    def test_open_editor_waits_for_client_rendered_editor_contract(self) -> None:
        runtime_values = [
            True,
            {"readyState": "complete", "oldDocument": False},
            {"state": "unknown"},
            {"readyState": "complete", "oldDocument": False},
            {
                "state": "ready",
                "ad_id": AD_ID,
                "title": "Existing title",
                "description": "Existing description",
            },
        ]

        def handler(method, params):
            if method == "Page.navigate":
                return {}
            if method == "Runtime.evaluate":
                value = runtime_values.pop(0)
                return {"result": {"type": "object", "value": value}}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
            sleep=lambda _seconds: None,
        )

        page.open_editor(AD_ID)

        self.assertEqual(runtime_values, [])
        self.assertEqual(
            [method for method, _params in client.calls],
            [
                "Runtime.evaluate",
                "Page.navigate",
                "Runtime.evaluate",
                "Runtime.evaluate",
                "Runtime.evaluate",
                "Runtime.evaluate",
            ],
        )
        first_contract_probe = client.calls[3][1]["expression"]
        second_contract_probe = client.calls[5][1]["expression"]
        for expression in (first_contract_probe, second_contract_probe):
            self.assertIn("#ad-title", expression)
            self.assertIn("#ad-description", expression)
            self.assertIn("Anzeige speichern", expression)
            self.assertIn("/p-anzeige-bearbeiten.html", expression)

    def test_open_editor_releases_known_challenge_for_classification(self) -> None:
        runtime_values = [
            True,
            {"readyState": "complete", "oldDocument": False},
            {"state": "captcha_required"},
        ]

        def handler(method, params):
            if method == "Page.navigate":
                return {}
            if method == "Runtime.evaluate":
                value = runtime_values.pop(0)
                return {"result": {"type": "object", "value": value}}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )

        page.open_editor(AD_ID)

        self.assertEqual(runtime_values, [])
        challenge_probe = client.calls[3][1]["expression"]
        self.assertIn('challengeText.includes("captcha")', challenge_probe)
        self.assertIn('input[type="password"]', challenge_probe)

    def test_open_editor_rejects_ready_snapshot_for_different_ad_id(self) -> None:
        wrong_ad_id = "9999999999"
        runtime_values = [
            True,
            {"readyState": "complete", "oldDocument": False},
            {
                "state": "ready",
                "ad_id": wrong_ad_id,
                "title": "Other title",
                "description": "Other description",
            },
        ]
        monotonic_values = iter((0.0, 0.2))

        def handler(method, params):
            if method == "Page.navigate":
                return {}
            if method == "Runtime.evaluate":
                value = runtime_values.pop(0)
                return {"result": {"type": "object", "value": value}}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
            timeout_seconds=0.1,
            sleep=lambda _seconds: None,
            monotonic=lambda: next(monotonic_values),
        )

        with self.assertRaisesRegex(PrivateWebCdpError, "navigate"):
            page.open_editor(AD_ID)

        self.assertEqual(runtime_values, [])
        self.assertIsNone(page._bound_ad_id)
        self.assertIsNone(page._last_ready_snapshot)

    def test_wrong_target_ready_snapshot_does_not_arm_mutation(self) -> None:
        wrong_ad_id = "9999999999"
        client = FakeClient(
            lambda method, params: (
                {
                    "result": {
                        "type": "object",
                        "value": {
                            "state": "ready",
                            "ad_id": wrong_ad_id,
                            "title": "Other title",
                            "description": "Other description",
                        },
                    }
                }
                if method == "Runtime.evaluate"
                else (_ for _ in ()).throw(
                    AssertionError(f"unexpected method: {method}")
                )
            )
        )
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        page._bound_ad_id = AD_ID

        snapshot = page.read_editor()
        self.assertEqual(snapshot.state, PrivateWebEditorState.READY)
        self.assertEqual(snapshot.ad_id, wrong_ad_id)
        self.assertIsNone(page._last_ready_snapshot)

        with self.assertRaisesRegex(PrivateWebCdpError, "replace_title"):
            page.replace_title("Must not write")

        self.assertEqual(
            [method for method, _params in client.calls],
            ["Runtime.evaluate"],
        )

    def test_open_editor_times_out_while_editor_contract_is_unknown(self) -> None:
        runtime_values = [
            True,
            {"readyState": "complete", "oldDocument": False},
            {"state": "unknown"},
        ]
        monotonic_values = iter((0.0, 0.2))

        def handler(method, params):
            if method == "Page.navigate":
                return {}
            if method == "Runtime.evaluate":
                value = runtime_values.pop(0)
                return {"result": {"type": "object", "value": value}}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
            timeout_seconds=0.1,
            sleep=lambda _seconds: None,
            monotonic=lambda: next(monotonic_values),
        )

        with self.assertRaisesRegex(PrivateWebCdpError, "navigate"):
            page.open_editor(AD_ID)

        self.assertEqual(runtime_values, [])

    def test_invalid_ad_id_never_touches_browser(self) -> None:
        calls = 0

        def factory():
            nonlocal calls
            calls += 1
            raise AssertionError("must not connect")

        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=factory,
        )
        with self.assertRaises(ValueError):
            page.open_editor("1;bad")
        self.assertEqual(calls, 0)

    def test_ready_snapshot_requires_exact_contract(self) -> None:
        page, clients = page_with_results(
            {
                "state": "ready",
                "ad_id": AD_ID,
                "title": "Existing title",
                "description": "Existing description",
            }
        )

        snapshot = page.read_editor()

        self.assertEqual(snapshot.state, PrivateWebEditorState.READY)
        self.assertEqual(snapshot.ad_id, AD_ID)
        self.assertEqual(snapshot.title, "Existing title")
        self.assertEqual(snapshot.description, "Existing description")
        expression = clients[0].calls[0][1]["expression"]
        self.assertIn("#ad-title", expression)
        self.assertIn("#ad-description", expression)
        self.assertIn("Anzeige speichern", expression)
        self.assertIn("/p-anzeige-bearbeiten.html", expression)
        self.assertIn('.join("\\n")', expression)
        javascript_check = subprocess.run(
            ["node", "--check", "-"],
            input=expression,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            javascript_check.returncode,
            0,
            javascript_check.stdout + javascript_check.stderr,
        )

    def test_captcha_detection_does_not_scan_entire_editor_body(self) -> None:
        page, clients = page_with_results(
            {
                "state": "ready",
                "ad_id": AD_ID,
                "title": "Captcha collection",
                "description": "Ordinary listing text mentioning captcha.",
            }
        )

        snapshot = page.read_editor()

        self.assertEqual(snapshot.state, PrivateWebEditorState.READY)
        expression = clients[0].calls[0][1]["expression"]
        self.assertNotIn("document.body", expression)
        self.assertNotIn('body.includes("captcha")', expression)
        self.assertIn('challengeText.includes("captcha")', expression)
        self.assertIn('[role="dialog"]', expression)
        self.assertIn('[aria-modal="true"]', expression)

    def test_challenge_states_return_no_ad_content(self) -> None:
        for raw_state in (
            "login_required",
            "mfa_required",
            "captcha_required",
            "security_challenge",
            "unknown",
        ):
            with self.subTest(raw_state=raw_state):
                page, _ = page_with_results({"state": raw_state})
                snapshot = page.read_editor()
                self.assertEqual(snapshot.state.value, raw_state)
                self.assertIsNone(snapshot.ad_id)
                self.assertIsNone(snapshot.title)
                self.assertIsNone(snapshot.description)

    def test_malformed_ready_snapshot_fails_closed(self) -> None:
        for value in (
            {"state": "ready", "ad_id": AD_ID, "title": 7, "description": "x"},
            {"state": "ready", "ad_id": "bad", "title": "x", "description": "y"},
            {"state": "not-a-state"},
            "not-a-dict",
        ):
            with self.subTest(value=value):
                page, _ = page_with_results(value)
                snapshot = page.read_editor()
                self.assertEqual(snapshot.state, PrivateWebEditorState.UNKNOWN)

    def test_replace_title_and_description_are_atomic_and_target_bound(self) -> None:
        client = FakeClient(
            lambda method, params: (
                {"result": {"type": "boolean", "value": True}}
                if method == "Runtime.evaluate"
                else (_ for _ in ()).throw(
                    AssertionError(f"unexpected method: {method}")
                )
            )
        )
        factory_calls = 0

        def factory():
            nonlocal factory_calls
            factory_calls += 1
            return client

        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=factory,
        )
        bind_ready_editor(page)

        page.replace_title("New title")
        page.replace_description("New description")

        self.assertEqual(factory_calls, 1)
        self.assertEqual(
            [method for method, _params in client.calls],
            ["Runtime.evaluate", "Runtime.evaluate"],
        )
        title_expression = client.calls[0][1]["expression"]
        description_expression = client.calls[1][1]["expression"]
        for expression in (title_expression, description_expression):
            self.assertIn("location.origin", expression)
            self.assertIn("/p-anzeige-bearbeiten.html", expression)
            self.assertIn(AD_ID, expression)
            self.assertIn("Object.getOwnPropertyDescriptor", expression)
            self.assertIn('new InputEvent(', expression)
            self.assertIn('new Event("change"', expression)
        self.assertIn("Existing title", title_expression)
        self.assertIn("Existing description", title_expression)
        self.assertIn("New title", description_expression)
        self.assertIn("Existing description", description_expression)
        self.assertNotIn("Input.dispatchKeyEvent", str(client.calls))
        self.assertNotIn("Input.insertText", str(client.calls))
        self.assertFalse(client.closed)
        page.close()
        self.assertTrue(client.closed)

    def test_replace_failure_is_sanitized(self) -> None:
        def handler(method, params):
            if method == "Runtime.evaluate":
                raise RuntimeError(
                    "provider https://example.invalid Cookie sensitive listing"
                )
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        bind_ready_editor(page)

        with self.assertRaises(PrivateWebCdpError) as caught:
            page.replace_title("sensitive listing text")

        self.assertEqual(caught.exception.stage, "replace_title")
        self.assertEqual(
            str(caught.exception),
            "private web cdp failed at replace_title",
        )
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertEqual([method for method, _params in client.calls], ["Runtime.evaluate"])
        self.assertFalse(client.closed)
        page.close()
        self.assertTrue(client.closed)

    def test_replace_requires_bound_verified_editor_before_browser_mutation(self) -> None:
        client = FakeClient(
            lambda method, params: (_ for _ in ()).throw(
                AssertionError("browser must not be touched")
            )
        )
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )

        with self.assertRaises(PrivateWebCdpError) as caught:
            page.replace_title("New title")

        self.assertEqual(caught.exception.stage, "replace_title")
        self.assertEqual(client.calls, [])

    def test_replace_rejects_editor_drift_in_same_mutation_expression(self) -> None:
        client = FakeClient(
            lambda method, params: (
                {"result": {"type": "boolean", "value": False}}
                if method == "Runtime.evaluate"
                else (_ for _ in ()).throw(
                    AssertionError(f"unexpected method: {method}")
                )
            )
        )
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        bind_ready_editor(page)

        with self.assertRaises(PrivateWebCdpError) as caught:
            page.replace_description("New description")

        self.assertEqual(caught.exception.stage, "replace_description")
        self.assertEqual(
            [method for method, _params in client.calls],
            ["Runtime.evaluate"],
        )
        expression = client.calls[0][1]["expression"]
        self.assertIn(AD_ID, expression)
        self.assertIn("Existing title", expression)
        self.assertIn("Existing description", expression)
        self.assertIn("descriptor.set.call", expression)

    def test_submit_is_atomic_and_bound_to_verified_editor(self) -> None:
        def handler(method, params):
            if method == "Runtime.evaluate":
                expression = params.get("expression")
                if "scrollIntoView" in str(expression):
                    return {
                        "result": {
                            "type": "object",
                            "value": {"x": 120.5, "y": 240.25},
                        }
                    }
                return {
                    "result": {
                        "type": "string",
                        "value": "confirmed",
                    }
                }
            if method == "Input.dispatchMouseEvent":
                return {}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        bind_ready_editor(page)

        page.submit()

        with self.assertRaises(PrivateWebCdpError) as caught:
            page.submit()
        self.assertEqual(caught.exception.stage, "submit_already_attempted")
        self.assertEqual(
            [method for method, _params in client.calls],
            [
                "Runtime.evaluate",
                "Input.dispatchMouseEvent",
                "Input.dispatchMouseEvent",
                "Runtime.evaluate",
            ],
        )
        expression = client.calls[0][1]["expression"]
        self.assertIn("location.origin", expression)
        self.assertIn("/p-anzeige-bearbeiten.html", expression)
        self.assertIn(AD_ID, expression)
        self.assertIn("Existing title", expression)
        self.assertIn("Existing description", expression)
        self.assertIn("Anzeige speichern", expression)
        self.assertIn("getBoundingClientRect", expression)
        self.assertIn("elementFromPoint", expression)
        self.assertIn("Number.isFinite(x)", expression)
        self.assertNotIn("button.click()", expression)
        activation_javascript_check = subprocess.run(
            ["node", "--check", "-"],
            input=expression,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            activation_javascript_check.returncode,
            0,
            activation_javascript_check.stdout + activation_javascript_check.stderr,
        )
        pressed = client.calls[1][1]
        released = client.calls[2][1]
        self.assertEqual(
            pressed,
            {
                "type": "mousePressed",
                "x": 120.5,
                "y": 240.25,
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
            },
        )
        self.assertEqual(
            released,
            {
                "type": "mouseReleased",
                "x": 120.5,
                "y": 240.25,
                "button": "left",
                "buttons": 0,
                "clickCount": 1,
            },
        )
        confirmation_expression = client.calls[3][1]["expression"]
        self.assertIn("location.origin", confirmation_expression)
        self.assertIn("/m-meine-anzeigen.html", confirmation_expression)
        self.assertIn('currentPath.startsWith("/u/login/")', confirmation_expression)
        self.assertIn('challengeText.includes("captcha")', confirmation_expression)
        javascript_check = subprocess.run(
            ["node", "--check", "-"],
            input=confirmation_expression,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            javascript_check.returncode,
            0,
            javascript_check.stdout + javascript_check.stderr,
        )
        self.assertFalse(client.closed)
        page.close()
        self.assertTrue(client.closed)

    def test_submit_requires_bound_verified_snapshot_before_browser_mutation(self) -> None:
        client = FakeClient(
            lambda method, params: (_ for _ in ()).throw(
                AssertionError("browser must not be touched")
            )
        )
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )

        with self.assertRaises(PrivateWebCdpError) as caught:
            page.submit()

        self.assertEqual(caught.exception.stage, "submit")
        self.assertEqual(client.calls, [])
        with self.assertRaises(PrivateWebCdpError) as second:
            page.submit()
        self.assertEqual(second.exception.stage, "submit_already_attempted")

    def test_submit_rejects_editor_drift_before_activation(self) -> None:
        def handler(method, params):
            if method != "Runtime.evaluate":
                raise AssertionError(f"unexpected method: {method}")
            return {"result": {"type": "boolean", "value": False}}

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        bind_ready_editor(page)

        with self.assertRaises(PrivateWebCdpError) as caught:
            page.submit()

        self.assertEqual(caught.exception.stage, "submit")
        self.assertEqual(
            [method for method, _params in client.calls],
            ["Runtime.evaluate"],
        )
        expression = client.calls[0][1]["expression"]
        self.assertIn(AD_ID, expression)
        self.assertIn("Existing title", expression)
        self.assertIn("Existing description", expression)
        self.assertNotIn("button.click()", expression)

    def test_submit_rejects_invalid_activation_coordinates_before_mouse_dispatch(self) -> None:
        client = FakeClient(
            lambda method, params: {
                "result": {
                    "type": "object",
                    "value": {"x": True, "y": 10.0},
                }
            }
        )
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        bind_ready_editor(page)

        with self.assertRaises(PrivateWebCdpError) as caught:
            page.submit()

        self.assertEqual(caught.exception.stage, "submit")
        self.assertEqual(
            [method for method, _params in client.calls],
            ["Runtime.evaluate"],
        )
        with self.assertRaises(PrivateWebCdpError) as second:
            page.submit()
        self.assertEqual(second.exception.stage, "submit_already_attempted")

    def test_submit_staying_in_editor_is_unconfirmed_and_not_retryable(self) -> None:
        times = iter((0.0, 10.0))

        def handler(method, params):
            if method == "Input.dispatchMouseEvent":
                return {}
            if method != "Runtime.evaluate":
                raise AssertionError(f"unexpected method: {method}")
            expression = params.get("expression")
            if "scrollIntoView" in str(expression):
                return {
                    "result": {
                        "type": "object",
                        "value": {"x": 100.0, "y": 200.0},
                    }
                }
            return {"result": {"type": "string", "value": "pending"}}

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            timeout_seconds=5,
            client_factory=lambda: client,
            sleep=lambda _seconds: None,
            monotonic=lambda: next(times),
        )
        bind_ready_editor(page)

        with self.assertRaises(PrivateWebCdpError) as caught:
            page.submit()
        self.assertEqual(caught.exception.stage, "submit_unconfirmed")

        with self.assertRaises(PrivateWebCdpError) as second:
            page.submit()
        self.assertEqual(second.exception.stage, "submit_already_attempted")
        self.assertEqual(
            [method for method, _params in client.calls],
            [
                "Runtime.evaluate",
                "Input.dispatchMouseEvent",
                "Input.dispatchMouseEvent",
                "Runtime.evaluate",
            ],
        )
        page.close()

    def test_submit_untrusted_redirect_is_unconfirmed_and_not_retryable(self) -> None:
        def handler(method, params):
            if method == "Input.dispatchMouseEvent":
                return {}
            if method != "Runtime.evaluate":
                raise AssertionError(f"unexpected method: {method}")
            expression = params.get("expression")
            if "scrollIntoView" in str(expression):
                return {
                    "result": {
                        "type": "object",
                        "value": {"x": 100.0, "y": 200.0},
                    }
                }
            return {"result": {"type": "string", "value": "unconfirmed"}}

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        bind_ready_editor(page)

        with self.assertRaises(PrivateWebCdpError) as caught:
            page.submit()
        self.assertEqual(caught.exception.stage, "submit_unconfirmed")

        with self.assertRaises(PrivateWebCdpError) as second:
            page.submit()
        self.assertEqual(second.exception.stage, "submit_already_attempted")

        confirmation_expression = client.calls[3][1]["expression"]
        self.assertIn('currentOrigin !== "https://www.kleinanzeigen.de"', confirmation_expression)
        self.assertIn('currentPath.startsWith("/u/login/")', confirmation_expression)
        self.assertIn('challengeText.includes("captcha")', confirmation_expression)
        self.assertIn('challengeText.includes("sicherheitsprüfung")', confirmation_expression)
        self.assertIn("/m-meine-anzeigen.html", confirmation_expression)

    def test_ambiguous_submit_failure_cannot_be_retried(self) -> None:
        def handler(method, params):
            if method != "Runtime.evaluate":
                raise AssertionError(f"unexpected method: {method}")
            raise RuntimeError(
                "provider https://example.invalid Cookie secret listing content"
            )

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        bind_ready_editor(page)

        with self.assertRaises(PrivateWebCdpError) as caught:
            page.submit()
        self.assertEqual(caught.exception.stage, "submit")
        self.assertNotIn("Cookie", str(caught.exception))

        with self.assertRaises(PrivateWebCdpError) as second:
            page.submit()
        self.assertEqual(second.exception.stage, "submit_already_attempted")
        self.assertEqual(
            [method for method, _params in client.calls],
            ["Runtime.evaluate"],
        )
        self.assertFalse(client.closed)
        page.close()
        self.assertTrue(client.closed)

    def test_mouse_press_failure_is_ambiguous_and_not_retryable(self) -> None:
        def handler(method, params):
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "type": "object",
                        "value": {"x": 100.0, "y": 200.0},
                    }
                }
            if method == "Input.dispatchMouseEvent":
                # Mirror _LoopbackCdpClient.call's production sanitization.
                raise PrivateWebCdpError("call")
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        bind_ready_editor(page)

        with self.assertRaises(PrivateWebCdpError) as caught:
            page.submit()
        self.assertEqual(caught.exception.stage, "submit")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(
            [method for method, _params in client.calls],
            ["Runtime.evaluate", "Input.dispatchMouseEvent"],
        )
        with self.assertRaises(PrivateWebCdpError) as second:
            page.submit()
        self.assertEqual(second.exception.stage, "submit_already_attempted")

    def test_mouse_release_failure_is_ambiguous_and_not_retryable(self) -> None:
        dispatches = 0

        def handler(method, params):
            nonlocal dispatches
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "type": "object",
                        "value": {"x": 100.0, "y": 200.0},
                    }
                }
            if method == "Input.dispatchMouseEvent":
                dispatches += 1
                if dispatches == 1:
                    return {}
                # Mirror _LoopbackCdpClient.call's production sanitization.
                raise PrivateWebCdpError("call")
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        bind_ready_editor(page)

        with self.assertRaises(PrivateWebCdpError) as caught:
            page.submit()
        self.assertEqual(caught.exception.stage, "submit")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(
            [method for method, _params in client.calls],
            [
                "Runtime.evaluate",
                "Input.dispatchMouseEvent",
                "Input.dispatchMouseEvent",
            ],
        )
        with self.assertRaises(PrivateWebCdpError) as second:
            page.submit()
        self.assertEqual(second.exception.stage, "submit_already_attempted")

    def test_cookie_provider_reuses_client_and_closes_explicitly(self) -> None:
        client = FakeClient(
            lambda method, params: {
                "cookies": [
                    {"name": "session", "value": "opaque-one"},
                    {"name": "other", "value": "opaque-two"},
                ]
            }
        )
        factory_calls = 0

        def factory():
            nonlocal factory_calls
            factory_calls += 1
            return client

        provider = CdpCookieProvider(
            "http://127.0.0.1:19610",
            client_factory=factory,
        )

        first = provider()
        second = provider()

        self.assertEqual(first, "session=opaque-one; other=opaque-two")
        self.assertEqual(second, first)
        self.assertEqual(factory_calls, 1)
        self.assertEqual(
            [method for method, _params in client.calls],
            ["Network.getCookies", "Network.getCookies"],
        )
        self.assertEqual(
            client.calls[0][1]["urls"],
            ["https://www.kleinanzeigen.de/m-meine-anzeigen-verwalten.json"],
        )
        self.assertFalse(client.closed)
        provider.close()
        self.assertTrue(client.closed)

    def test_cookie_provider_empty_or_malformed_is_fail_closed(self) -> None:
        empty_client = FakeClient(
            lambda method, params: {"cookies": []}
        )
        empty = CdpCookieProvider(
            "http://127.0.0.1:19610",
            client_factory=lambda: empty_client,
        )
        self.assertIsNone(empty())

        bad_client = FakeClient(
            lambda method, params: {
                "cookies": [
                    {"name": "session", "value": "secret\r\nInjected: yes"}
                ]
            }
        )
        bad = CdpCookieProvider(
            "http://127.0.0.1:19610",
            client_factory=lambda: bad_client,
        )
        with self.assertRaises(PrivateWebCdpError) as caught:
            bad()
        self.assertEqual(str(caught.exception), "private web cdp failed at cookies")
        self.assertNotIn("secret", str(caught.exception))

    def test_proxy_free_loopback_opener_has_no_configured_proxies_or_redirects(self) -> None:
        opener = _proxy_free_loopback_opener()
        director = opener.__self__
        proxy_handlers = [
            handler
            for handler in director.handlers
            if handler.__class__.__name__ == "ProxyHandler"
        ]
        redirect_handlers = [
            handler
            for handler in director.handlers
            if handler.__class__.__name__ == "_RejectRedirectHandler"
        ]
        self.assertEqual(proxy_handlers, [])
        self.assertEqual(len(redirect_handlers), 1)
        self.assertIsNone(
            redirect_handlers[0].redirect_request(
                None,
                None,
                302,
                "Found",
                {},
                "https://example.invalid/",
            )
        )

    def test_proxy_free_opener_ignores_environment_proxy_configuration(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://127.0.0.1:9",
                "http_proxy": "http://127.0.0.1:9",
                "NO_PROXY": "",
                "no_proxy": "",
            },
            clear=False,
        ):
            opener = _proxy_free_loopback_opener()

        director = opener.__self__
        proxy_handlers = [
            handler
            for handler in director.handlers
            if handler.__class__.__name__ == "ProxyHandler"
        ]
        self.assertEqual(proxy_handlers, [])

    def test_loopback_client_rejects_discovery_final_url_escape(self) -> None:
        tcp_calls = 0

        def tcp_socket_factory(*args, **kwargs):
            nonlocal tcp_calls
            tcp_calls += 1
            raise AssertionError("TCP connection must not be attempted")

        with self.assertRaises(PrivateWebCdpError) as caught:
            _LoopbackCdpClient(
                "http://127.0.0.1:19610",
                timeout_seconds=1,
                opener=lambda *args, **kwargs: FakeResponse(
                    [],
                    url="http://127.0.0.1:19610/redirected",
                ),
                websocket_factory=lambda *args, **kwargs: FakeSocket([]),
                tcp_socket_factory=tcp_socket_factory,
            )

        self.assertEqual(caught.exception.stage, "connect")
        self.assertEqual(tcp_calls, 0)

    def test_loopback_client_reuses_one_page_websocket(self) -> None:
        good_target = {
            "type": "page",
            "webSocketDebuggerUrl": (
                "ws://127.0.0.1:19610/devtools/page/opaque-target"
            ),
        }
        socket = FakeSocket(
            [
                {"method": "Page.event", "params": {}},
                {"id": 1, "result": {"value": 1}},
                {"id": 2, "result": {"value": 2}},
            ]
        )
        raw_socket = FakeTcpSocket()
        tcp_calls: list[tuple[object, object]] = []
        websocket_kwargs: dict[str, object] = {}

        def tcp_socket_factory(address, *, timeout):
            tcp_calls.append((address, timeout))
            return raw_socket

        def websocket_factory(*args, **kwargs):
            websocket_kwargs.update(kwargs)
            return socket

        client = _LoopbackCdpClient(
            "http://127.0.0.1:19610",
            timeout_seconds=1,
            opener=lambda *args, **kwargs: FakeResponse([good_target]),
            websocket_factory=websocket_factory,
            tcp_socket_factory=tcp_socket_factory,
        )

        self.assertEqual(tcp_calls, [(("127.0.0.1", 19610), 1)])
        self.assertIs(websocket_kwargs["socket"], raw_socket)
        self.assertEqual(client.call("Runtime.one"), {"value": 1})
        self.assertEqual(client.call("Runtime.two"), {"value": 2})
        self.assertEqual(
            [message["method"] for message in socket.sent],
            ["Runtime.one", "Runtime.two"],
        )
        self.assertEqual([message["id"] for message in socket.sent], [1, 2])
        client.close()
        self.assertTrue(socket.closed)

    def test_loopback_client_closes_direct_socket_on_websocket_failure(self) -> None:
        good_target = {
            "type": "page",
            "webSocketDebuggerUrl": (
                "ws://127.0.0.1:19610/devtools/page/opaque-target"
            ),
        }
        raw_socket = FakeTcpSocket()

        with self.assertRaises(PrivateWebCdpError) as caught:
            _LoopbackCdpClient(
                "http://127.0.0.1:19610",
                timeout_seconds=1,
                opener=lambda *args, **kwargs: FakeResponse([good_target]),
                websocket_factory=lambda *args, **kwargs: (
                    (_ for _ in ()).throw(RuntimeError("provider secret"))
                ),
                tcp_socket_factory=lambda *args, **kwargs: raw_socket,
            )

        self.assertEqual(caught.exception.stage, "connect")
        self.assertTrue(raw_socket.closed)
        self.assertNotIn("secret", str(caught.exception))

    def test_loopback_client_requires_one_same_port_page_target(self) -> None:
        good_target = {
            "type": "page",
            "webSocketDebuggerUrl": (
                "ws://127.0.0.1:19610/devtools/page/opaque-target"
            ),
        }
        bad_target_sets = (
            [],
            [good_target, good_target],
            [
                {
                    "type": "page",
                    "webSocketDebuggerUrl": (
                        "ws://127.0.0.1:19611/devtools/page/wrong-port"
                    ),
                }
            ],
            [
                {
                    "type": "page",
                    "webSocketDebuggerUrl": (
                        "ws://example.invalid:19610/devtools/page/offhost"
                    ),
                }
            ],
            [
                {
                    "type": "page",
                    "webSocketDebuggerUrl": (
                        "ws://127.0.0.1:19610/devtools/browser/not-page"
                    ),
                }
            ],
        )
        for targets in bad_target_sets:
            with self.subTest(targets=targets):
                with self.assertRaises(PrivateWebCdpError) as caught:
                    _LoopbackCdpClient(
                        "http://127.0.0.1:19610",
                        timeout_seconds=1,
                        opener=lambda *args, targets=targets, **kwargs: (
                            FakeResponse(targets)
                        ),
                        websocket_factory=lambda *args, **kwargs: FakeSocket([]),
                    )
                self.assertEqual(caught.exception.stage, "connect")

    def test_import_has_no_historical_adapter_or_websocket_side_effect(self) -> None:
        code = (
            "import sys; import mark_api.private_web_cdp; "
            "bad=sorted(name for name in sys.modules "
            "if name == 'websocket' or name.startswith('websocket.') "
            "or name == 'mark_api.adapters' "
            "or name.startswith('mark_api.adapters.')); "
            "print(bad); raise SystemExit(0 if not bad else 1)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr,
        )
        self.assertEqual(result.stdout.strip(), "[]")


class CdpPrivateWebOwnerReaderTests(unittest.TestCase):
    @staticmethod
    def owner_snapshot(
        *,
        ad_id: str = AD_ID,
        title: str = "Management title",
        description: str | None = None,
    ) -> AdSnapshot:
        return AdSnapshot(
            ad_id=ad_id,
            observed_at=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
            source="kleinanzeigen-management",
            lifecycle_state=LifecycleState.ACTIVE,
            title=title,
            description=description,
            views=7,
            watch_count=2,
            reply_count=1,
        )

    def test_owner_reader_enriches_exact_target_from_fresh_editor_read(self) -> None:
        target = self.owner_snapshot()
        other = self.owner_snapshot(ad_id="9999999999", title="Other")
        owner_result = ReadResult.success_nonempty((other, target))

        class OwnerReader:
            def read_ads(self):
                return owner_result

        class Page:
            def __init__(self) -> None:
                self.opened: list[str] = []
                self.closed = False

            def open_editor(self, ad_id: str) -> None:
                self.opened.append(ad_id)

            def read_editor(self) -> PrivateWebEditorSnapshot:
                return PrivateWebEditorSnapshot(
                    state=PrivateWebEditorState.READY,
                    ad_id=AD_ID,
                    title="Fresh title",
                    description="Fresh description",
                )

            def close(self) -> None:
                self.closed = True

        pages: list[Page] = []

        def page_factory():
            page = Page()
            pages.append(page)
            return page

        result = CdpPrivateWebOwnerReader(
            owner_reader=OwnerReader(),
            page_factory=page_factory,
            ad_id=AD_ID,
        ).read_ads()

        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        self.assertEqual(result.value[0], other)
        enriched = result.value[1]
        self.assertEqual(enriched.ad_id, AD_ID)
        self.assertEqual(enriched.title, "Fresh title")
        self.assertEqual(enriched.description, "Fresh description")
        self.assertEqual(enriched.lifecycle_state, LifecycleState.ACTIVE)
        self.assertEqual(enriched.views, 7)
        self.assertEqual(enriched.watch_count, 2)
        self.assertEqual(enriched.reply_count, 1)
        self.assertEqual(
            enriched.source,
            "kleinanzeigen-management+private-web",
        )
        self.assertEqual(pages[0].opened, [AD_ID])
        self.assertTrue(pages[0].closed)

    def test_owner_reader_does_not_open_editor_for_non_owner_target(self) -> None:
        owner_result = ReadResult.success_nonempty(
            (self.owner_snapshot(ad_id="9999999999"),)
        )

        class OwnerReader:
            def read_ads(self):
                return owner_result

        def page_factory():
            raise AssertionError("editor must not open for absent owner target")

        result = CdpPrivateWebOwnerReader(
            owner_reader=OwnerReader(),
            page_factory=page_factory,
            ad_id=AD_ID,
        ).read_ads()

        self.assertIs(result, owner_result)

    def test_owner_reader_fails_closed_on_editor_target_or_challenge_drift(self) -> None:
        owner_result = ReadResult.success_nonempty((self.owner_snapshot(),))
        snapshots = (
            PrivateWebEditorSnapshot(
                state=PrivateWebEditorState.READY,
                ad_id="9999999999",
                title="Wrong",
                description="Wrong",
            ),
            PrivateWebEditorSnapshot(
                state=PrivateWebEditorState.CAPTCHA_REQUIRED,
            ),
        )

        class OwnerReader:
            def read_ads(self):
                return owner_result

        for editor_snapshot in snapshots:
            with self.subTest(state=editor_snapshot.state):
                class Page:
                    def open_editor(self, ad_id: str) -> None:
                        self.ad_id = ad_id

                    def read_editor(self):
                        return editor_snapshot

                    def close(self) -> None:
                        self.closed = True

                result = CdpPrivateWebOwnerReader(
                    owner_reader=OwnerReader(),
                    page_factory=Page,
                    ad_id=AD_ID,
                ).read_ads()

                self.assertEqual(result.status, ReadStatus.TRANSPORT_ERROR)
                self.assertIsNone(result.value)

    def test_owner_reader_rejects_invalid_owner_snapshot_before_editor(self) -> None:
        class OwnerReader:
            def read_ads(self):
                return ReadResult.success_nonempty((object(),))

        result = CdpPrivateWebOwnerReader(
            owner_reader=OwnerReader(),
            page_factory=lambda: (_ for _ in ()).throw(
                AssertionError("editor must not open for invalid owner snapshot")
            ),
            ad_id=AD_ID,
        ).read_ads()

        self.assertEqual(result.status, ReadStatus.PARSE_ERROR)
        self.assertEqual(result.error, "invalid_owner_snapshot")

    def test_owner_reader_sanitizes_page_close_failure(self) -> None:
        owner_result = ReadResult.success_nonempty((self.owner_snapshot(),))

        class OwnerReader:
            def read_ads(self):
                return owner_result

        class Page:
            def open_editor(self, ad_id: str) -> None:
                if ad_id != AD_ID:
                    raise AssertionError("wrong target")

            def read_editor(self):
                return PrivateWebEditorSnapshot(
                    state=PrivateWebEditorState.READY,
                    ad_id=AD_ID,
                    title="Fresh title",
                    description="Fresh description",
                )

            def close(self) -> None:
                raise RuntimeError("provider secret")

        result = CdpPrivateWebOwnerReader(
            owner_reader=OwnerReader(),
            page_factory=Page,
            ad_id=AD_ID,
        ).read_ads()

        self.assertEqual(result.status, ReadStatus.TRANSPORT_ERROR)
        self.assertEqual(result.error, "private_web_owner_read_failed")
        self.assertNotIn("secret", result.error)

    def test_description_update_can_be_confirmed_by_safe_orchestrator(self) -> None:
        state = {
            "title": "Existing title",
            "description": "Existing description",
        }
        target = self.owner_snapshot(title="Inventory title")

        class OwnerReader:
            def read_ads(self):
                return ReadResult.success_nonempty((target,))

        class Page:
            def open_editor(self, ad_id: str) -> None:
                if ad_id != AD_ID:
                    raise AssertionError("wrong target")

            def read_editor(self):
                return PrivateWebEditorSnapshot(
                    state=PrivateWebEditorState.READY,
                    ad_id=AD_ID,
                    title=state["title"],
                    description=state["description"],
                )

            def close(self) -> None:
                return None

        class Writer:
            calls = 0

            def update_content(
                self,
                ad_id: str,
                *,
                title: str | None = None,
                description: str | None = None,
            ) -> None:
                self.calls += 1
                if ad_id != AD_ID:
                    raise AssertionError("wrong target")
                if title is not None:
                    state["title"] = title
                if description is not None:
                    state["description"] = description

        reader = CdpPrivateWebOwnerReader(
            owner_reader=OwnerReader(),
            page_factory=Page,
            ad_id=AD_ID,
        )
        writer = Writer()

        receipt = SafeWriteOrchestrator(writes_enabled=True).update_content(
            ad_id=AD_ID,
            reader=reader,
            writer=writer,
            description="Updated description",
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(writer.calls, 1)
        self.assertEqual(
            receipt.post_snapshot.description,
            "Updated description",
        )


class CdpPrivateWebStatePageTests(unittest.TestCase):
    def bind_state(
        self,
        page: CdpPrivateWebPage,
        state: LifecycleState,
    ) -> None:
        page._bound_state_ad_id = AD_ID
        page._last_state_snapshot = PrivateWebStateSnapshot(
            state=PrivateWebEditorState.READY,
            ad_id=AD_ID,
            lifecycle_state=state,
        )

    def test_open_state_controls_navigates_to_owner_management_and_binds_target(self) -> None:
        values = [
            True,
            {"readyState": "complete", "oldDocument": False},
            {
                "state": "ready",
                "ad_id": AD_ID,
                "lifecycle_state": "active",
            },
        ]

        def handler(method, params):
            if method == "Page.navigate":
                self.assertEqual(
                    params["url"],
                    "https://www.kleinanzeigen.de/m-meine-anzeigen.html",
                )
                return {}
            if method == "Runtime.evaluate":
                if not values:
                    raise AssertionError("unexpected Runtime.evaluate")
                value = values.pop(0)
                return {"result": {"type": "object", "value": value}}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )

        page.open_state_controls(AD_ID)

        self.assertEqual(page._bound_state_ad_id, AD_ID)
        self.assertEqual(values, [])
        self.assertEqual(
            [method for method, _params in client.calls],
            [
                "Runtime.evaluate",
                "Page.navigate",
                "Runtime.evaluate",
                "Runtime.evaluate",
            ],
        )

    def test_open_state_controls_follows_management_pagination_to_target(self) -> None:
        values = [
            True,
            {"readyState": "complete", "oldDocument": False},
            {
                "state": "target_absent",
                "page_ad_ids": ["1111111111"],
                "next_page": {"x": 40.0, "y": 60.0},
            },
            {
                "state": "ready",
                "ad_id": AD_ID,
                "lifecycle_state": "active",
            },
        ]

        def handler(method, params):
            if method == "Page.navigate":
                return {}
            if method == "Runtime.evaluate":
                if not values:
                    raise AssertionError("unexpected Runtime.evaluate")
                value = values.pop(0)
                return {"result": {"type": "object", "value": value}}
            if method == "Input.dispatchMouseEvent":
                return {}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
            sleep=lambda _seconds: None,
        )

        page.open_state_controls(AD_ID)

        self.assertEqual(values, [])
        self.assertEqual(page._bound_state_ad_id, AD_ID)
        self.assertEqual(
            [method for method, _params in client.calls],
            [
                "Runtime.evaluate",
                "Page.navigate",
                "Runtime.evaluate",
                "Runtime.evaluate",
                "Input.dispatchMouseEvent",
                "Input.dispatchMouseEvent",
                "Runtime.evaluate",
            ],
        )
        self.assertEqual(
            client.calls[4][1],
            {
                "type": "mousePressed",
                "x": 40.0,
                "y": 60.0,
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
            },
        )
        self.assertEqual(
            client.calls[5][1],
            {
                "type": "mouseReleased",
                "x": 40.0,
                "y": 60.0,
                "button": "left",
                "buttons": 0,
                "clickCount": 1,
            },
        )
        pagination_expression = client.calls[3][1]["expression"]
        self.assertIn('button[aria-label="Nächste"]', pagination_expression)
        self.assertIn("elementFromPoint", pagination_expression)
        self.assertNotIn(".click(", pagination_expression)

    def test_open_state_controls_waits_for_transient_next_control(self) -> None:
        values = [
            True,
            {"readyState": "complete", "oldDocument": False},
            {
                "state": "target_absent",
                "page_ad_ids": ["1111111111"],
                "next_page": None,
            },
            {
                "state": "target_absent",
                "page_ad_ids": ["1111111111"],
                "next_page": {"x": 40.0, "y": 60.0},
            },
            {
                "state": "ready",
                "ad_id": AD_ID,
                "lifecycle_state": "active",
            },
        ]

        def handler(method, params):
            if method == "Page.navigate":
                return {}
            if method == "Runtime.evaluate":
                if not values:
                    raise AssertionError("unexpected Runtime.evaluate")
                value = values.pop(0)
                return {"result": {"type": "object", "value": value}}
            if method == "Input.dispatchMouseEvent":
                return {}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
            sleep=lambda _seconds: None,
        )

        page.open_state_controls(AD_ID)

        self.assertEqual(values, [])
        self.assertEqual(page._bound_state_ad_id, AD_ID)
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            2,
        )

    def test_open_state_controls_fails_closed_when_target_is_absent_on_last_page(self) -> None:
        setup_values = [
            True,
            {"readyState": "complete", "oldDocument": False},
        ]
        last_page = {
            "state": "target_absent",
            "page_ad_ids": ["1111111111"],
            "next_page": None,
        }
        monotonic_values = iter((0.0, 0.0, 0.05, 0.1))

        def handler(method, params):
            if method == "Page.navigate":
                return {}
            if method == "Runtime.evaluate":
                value = setup_values.pop(0) if setup_values else last_page
                return {"result": {"type": "object", "value": value}}
            if method == "Input.dispatchMouseEvent":
                raise AssertionError("pagination input must not be attempted")
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            timeout_seconds=0.1,
            client_factory=lambda: client,
            sleep=lambda _seconds: None,
            monotonic=lambda: next(monotonic_values),
        )

        with self.assertRaises(PrivateWebCdpError) as caught:
            page.open_state_controls(AD_ID)

        self.assertEqual(caught.exception.stage, "navigate_state")
        self.assertIsNone(page._bound_state_ad_id)
        self.assertEqual(setup_values, [])
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            0,
        )
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Runtime.evaluate"
            ),
            4,
        )

    def test_read_state_controls_is_exact_owner_and_control_bound(self) -> None:
        page, clients = page_with_results(
            {
                "state": "ready",
                "ad_id": AD_ID,
                "lifecycle_state": "active",
            }
        )
        page._bound_state_ad_id = AD_ID

        snapshot = page.read_state_controls()

        self.assertEqual(snapshot.state, PrivateWebEditorState.READY)
        self.assertEqual(snapshot.ad_id, AD_ID)
        self.assertEqual(snapshot.lifecycle_state, LifecycleState.ACTIVE)
        self.assertEqual(page._last_state_snapshot, snapshot)
        expression = clients[0].calls[0][1]["expression"]
        self.assertIn("/m-meine-anzeigen.html", expression)
        self.assertIn("/p-anzeige-bearbeiten.html", expression)
        self.assertIn('url.searchParams.get("adId")', expression)
        self.assertIn("targetEditLinks.length > 1", expression)
        self.assertIn("targetEditLinks.length === 1", expression)
        self.assertIn("editIds.length === 1", expression)
        self.assertIn('label === "reservieren"', expression)
        self.assertIn('label === "aktivieren"', expression)
        self.assertIn("controls.length === 1", expression)
        self.assertNotIn("document.body", expression)
        javascript_check = subprocess.run(
            ["node", "--check", "-"],
            input=expression,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            javascript_check.returncode,
            0,
            javascript_check.stdout + javascript_check.stderr,
        )

    def test_state_binding_ignores_collapsed_counter_control(self) -> None:
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: None,
        )
        expression = page._state_control_expression(AD_ID)
        template = """
global.HTMLButtonElement = class HTMLButtonElement {};
const editLink = {
  getAttribute: (name) =>
    name === "href"
      ? "/p-anzeige-bearbeiten.html?adId=__AD_ID__"
      : null,
  parentElement: null,
};
const makeControl = (label, rect) => {
  const control = new HTMLButtonElement();
  control.innerText = label;
  control.disabled = false;
  control.getAttribute = (_name) => null;
  control.getBoundingClientRect = () => rect;
  return control;
};
const visibleControl = makeControl(
  __VISIBLE_LABEL__,
  {left: 20, top: 20, width: 120, height: 30}
);
const collapsedCounterControl = makeControl(
  __COLLAPSED_LABEL__,
  {left: 0, top: 0, width: 0, height: 0}
);
const container = {
  parentElement: null,
  querySelectorAll: (selector) => {
    if (selector === "a[href]") return [editLink];
    if (selector === 'button, a[href], [role="button"]') {
      return [visibleControl, collapsedCounterControl];
    }
    return [];
  },
};
editLink.parentElement = container;
global.location = {
  origin: "https://www.kleinanzeigen.de",
  pathname: "/m-meine-anzeigen.html",
  href: "https://www.kleinanzeigen.de/m-meine-anzeigen.html",
};
global.document = {
  querySelector: (_selector) => null,
  querySelectorAll: (selector) =>
    selector === "a[href]" ? [editLink] : [],
};
global.getComputedStyle = (_element) => ({
  display: "block",
  visibility: "visible",
  pointerEvents: "auto",
  opacity: "1",
});
const result = eval(__EXPRESSION__);
process.stdout.write(JSON.stringify(result));
"""
        cases = (
            ("Reservieren", "Aktivieren", "active"),
            ("Aktivieren", "Reservieren", "paused"),
        )
        for visible_label, collapsed_label, expected_state in cases:
            with self.subTest(expected_state=expected_state):
                script = (
                    template.replace("__AD_ID__", AD_ID)
                    .replace("__VISIBLE_LABEL__", json.dumps(visible_label))
                    .replace("__COLLAPSED_LABEL__", json.dumps(collapsed_label))
                    .replace("__EXPRESSION__", json.dumps(expression))
                )
                completed = subprocess.run(
                    ["node", "-"],
                    input=script,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )

                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stdout + completed.stderr,
                )
                self.assertEqual(
                    json.loads(completed.stdout),
                    {
                        "state": "ready",
                        "ad_id": AD_ID,
                        "lifecycle_state": expected_state,
                    },
                )

    def test_state_challenges_hide_target_data(self) -> None:
        for raw_state in (
            "login_required",
            "mfa_required",
            "captcha_required",
            "security_challenge",
            "unknown",
        ):
            with self.subTest(raw_state=raw_state):
                page, _ = page_with_results({"state": raw_state})
                page._bound_state_ad_id = AD_ID

                snapshot = page.read_state_controls()

                self.assertEqual(snapshot.state.value, raw_state)
                self.assertIsNone(snapshot.ad_id)
                self.assertIsNone(snapshot.lifecycle_state)
                self.assertIsNone(page._last_state_snapshot)

    def test_malformed_state_snapshot_fails_closed(self) -> None:
        for value in (
            {"state": "ready", "ad_id": AD_ID, "lifecycle_state": "pending"},
            {"state": "ready", "ad_id": "bad", "lifecycle_state": "active"},
            {"state": "ready", "ad_id": AD_ID, "lifecycle_state": 7},
            {"state": "not-a-state"},
            "not-a-dict",
        ):
            with self.subTest(value=value):
                page, _ = page_with_results(value)
                page._bound_state_ad_id = AD_ID

                snapshot = page.read_state_controls()

                self.assertEqual(snapshot.state, PrivateWebEditorState.UNKNOWN)
                self.assertIsNone(page._last_state_snapshot)

    def test_state_submit_uses_one_browser_level_hit_tested_activation(self) -> None:
        runtime_values = [
            {"x": 120.5, "y": 240.25},
            {
                "state": "ready",
                "ad_id": AD_ID,
                "lifecycle_state": "paused",
            },
        ]

        def handler(method, params):
            if method == "Runtime.evaluate":
                value = runtime_values.pop(0)
                return {"result": {"type": "object", "value": value}}
            if method == "Input.dispatchMouseEvent":
                return {}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        self.bind_state(page, LifecycleState.ACTIVE)

        page.submit_state(LifecycleState.PAUSED)

        self.assertEqual(
            [method for method, _params in client.calls],
            [
                "Runtime.evaluate",
                "Input.dispatchMouseEvent",
                "Input.dispatchMouseEvent",
                "Runtime.evaluate",
            ],
        )
        expression = client.calls[0][1]["expression"]
        self.assertIn('"reservieren"', expression)
        self.assertIn("getComputedStyle(control)", expression)
        self.assertIn("control.disabled", expression)
        self.assertIn('control.getAttribute("aria-disabled")', expression)
        self.assertIn("getBoundingClientRect", expression)
        self.assertIn("elementFromPoint", expression)
        self.assertNotIn(".click(", expression)
        javascript_check = subprocess.run(
            ["node", "--check", "-"],
            input=expression,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            javascript_check.returncode,
            0,
            javascript_check.stdout + javascript_check.stderr,
        )
        self.assertEqual(
            client.calls[1][1],
            {
                "type": "mousePressed",
                "x": 120.5,
                "y": 240.25,
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
            },
        )
        self.assertEqual(
            client.calls[2][1],
            {
                "type": "mouseReleased",
                "x": 120.5,
                "y": 240.25,
                "button": "left",
                "buttons": 0,
                "clickCount": 1,
            },
        )
        with self.assertRaisesRegex(
            PrivateWebCdpError,
            "state_submit_already_attempted",
        ):
            page.submit_state(LifecycleState.PAUSED)

    def test_activate_requires_paused_snapshot_and_activate_control(self) -> None:
        runtime_values = [
            {"x": 10.0, "y": 20.0},
            {
                "state": "ready",
                "ad_id": AD_ID,
                "lifecycle_state": "active",
            },
        ]

        def handler(method, params):
            if method == "Runtime.evaluate":
                value = runtime_values.pop(0)
                return {"result": {"type": "object", "value": value}}
            if method == "Input.dispatchMouseEvent":
                return {}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        self.bind_state(page, LifecycleState.PAUSED)

        page.submit_state(LifecycleState.ACTIVE)

        expression = client.calls[0][1]["expression"]
        self.assertIn('"aktivieren"', expression)
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            2,
        )

    def test_state_submit_fails_safe_before_browser_input(self) -> None:
        client = FakeClient(
            lambda method, params: (
                {"result": {"type": "object", "value": None}}
                if method == "Runtime.evaluate"
                else (_ for _ in ()).throw(
                    AssertionError("input must not be attempted")
                )
            )
        )
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        self.bind_state(page, LifecycleState.ACTIVE)

        with self.assertRaisesRegex(PrivateWebCdpError, "state_submit"):
            page.submit_state(LifecycleState.PAUSED)

        self.assertEqual(
            [method for method, _params in client.calls],
            ["Runtime.evaluate"],
        )
        with self.assertRaisesRegex(
            PrivateWebCdpError,
            "state_submit_already_attempted",
        ):
            page.submit_state(LifecycleState.PAUSED)

    def test_state_submit_waits_for_delayed_lifecycle_settlement(self) -> None:
        runtime_values = [
            {"x": 10.0, "y": 20.0},
            {
                "state": "ready",
                "ad_id": AD_ID,
                "lifecycle_state": "active",
            },
            {
                "state": "ready",
                "ad_id": AD_ID,
                "lifecycle_state": "paused",
            },
        ]
        sleeps: list[float] = []

        def handler(method, params):
            if method == "Runtime.evaluate":
                value = runtime_values.pop(0)
                return {"result": {"type": "object", "value": value}}
            if method == "Input.dispatchMouseEvent":
                return {}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
            sleep=sleeps.append,
        )
        self.bind_state(page, LifecycleState.ACTIVE)

        page.submit_state(LifecycleState.PAUSED)

        self.assertEqual(sleeps, [0.05])
        self.assertEqual(
            [method for method, _params in client.calls],
            [
                "Runtime.evaluate",
                "Input.dispatchMouseEvent",
                "Input.dispatchMouseEvent",
                "Runtime.evaluate",
                "Runtime.evaluate",
            ],
        )
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            2,
        )

    def test_state_submit_settlement_timeout_is_unknown_and_never_retried(self) -> None:
        runtime_values = [
            {"x": 10.0, "y": 20.0},
            {
                "state": "ready",
                "ad_id": AD_ID,
                "lifecycle_state": "active",
            },
        ]
        monotonic_values = iter((0.0, 0.1))

        def handler(method, params):
            if method == "Runtime.evaluate":
                value = runtime_values.pop(0)
                return {"result": {"type": "object", "value": value}}
            if method == "Input.dispatchMouseEvent":
                return {}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            timeout_seconds=0.1,
            client_factory=lambda: client,
            sleep=lambda _seconds: None,
            monotonic=lambda: next(monotonic_values),
        )
        self.bind_state(page, LifecycleState.ACTIVE)

        with self.assertRaises(PrivateWebSubmitUnknownError) as caught:
            page.submit_state(LifecycleState.PAUSED)

        self.assertEqual(caught.exception.stage, "state_submit_settle")
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            2,
        )
        with self.assertRaisesRegex(
            PrivateWebCdpError,
            "state_submit_already_attempted",
        ):
            page.submit_state(LifecycleState.PAUSED)
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            2,
        )

    def test_state_submit_provider_failure_after_input_is_submit_unknown(self) -> None:
        def handler(method, params):
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "type": "object",
                        "value": {"x": 10.0, "y": 20.0},
                    }
                }
            if method == "Input.dispatchMouseEvent":
                raise RuntimeError("provider response lost")
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        self.bind_state(page, LifecycleState.ACTIVE)

        with self.assertRaises(PrivateWebSubmitUnknownError) as caught:
            page.submit_state(LifecycleState.PAUSED)

        self.assertEqual(caught.exception.stage, "state_submit")
        self.assertEqual(
            [method for method, _params in client.calls],
            ["Runtime.evaluate", "Input.dispatchMouseEvent"],
        )
        with self.assertRaisesRegex(
            PrivateWebCdpError,
            "state_submit_already_attempted",
        ):
            page.submit_state(LifecycleState.PAUSED)

    def test_state_submit_rejects_wrong_prestate_before_browser_access(self) -> None:
        client = FakeClient(
            lambda method, params: (_ for _ in ()).throw(
                AssertionError("browser must not be touched")
            )
        )
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        self.bind_state(page, LifecycleState.PAUSED)

        with self.assertRaisesRegex(PrivateWebCdpError, "state_submit"):
            page.submit_state(LifecycleState.PAUSED)

        self.assertEqual(client.calls, [])


class CdpPrivateWebDeletePageTests(unittest.TestCase):
    def bind_delete(
        self,
        page: CdpPrivateWebPage,
        *,
        confirmation: bool = False,
    ) -> None:
        page._bound_delete_ad_id = AD_ID
        page._delete_confirmation_attempted = confirmation
        page._last_delete_snapshot = PrivateWebDeleteSnapshot(
            state=PrivateWebEditorState.READY,
            ad_id=AD_ID,
        )

    def test_delete_control_expression_is_exact_target_and_browser_level_only(self) -> None:
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: None,
        )
        expression = page._delete_control_expression(AD_ID)

        self.assertIn("/m-meine-anzeigen.html", expression)
        self.assertIn("/p-anzeige-bearbeiten.html", expression)
        self.assertIn('url.searchParams.get("adId")', expression)
        self.assertIn('label(element) === "löschen"', expression)
        self.assertIn("targetEditLinks.length > 1", expression)
        self.assertIn("editIds.length === 1", expression)
        self.assertIn("controls.length === 1", expression)
        self.assertIn("elementFromPoint", expression)
        self.assertNotIn(".click(", expression)
        javascript_check = subprocess.run(
            ["node", "--check", "-"],
            input=expression,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            javascript_check.returncode,
            0,
            javascript_check.stdout + javascript_check.stderr,
        )

    def test_delete_confirmation_expression_matches_observed_single_delete_modal(self) -> None:
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: None,
        )
        expression = page._delete_confirmation_expression(AD_ID)

        self.assertIn("/m-meine-anzeigen.html", expression)
        self.assertIn("#delete-container", expression)
        self.assertIn("#delete-celebration-sbmt", expression)
        self.assertIn("anzeige löschen", expression)
        self.assertIn(
            "bist du sicher, dass du die anzeige löschen möchtest?",
            expression,
        )
        self.assertIn("ja, anzeige löschen", expression)
        self.assertIn("abbrechen", expression)
        self.assertIn("elementFromPoint", expression)
        self.assertNotIn(".click(", expression)
        javascript_check = subprocess.run(
            ["node", "--check", "-"],
            input=expression,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            javascript_check.returncode,
            0,
            javascript_check.stdout + javascript_check.stderr,
        )

    def test_open_delete_controls_follows_management_pagination_to_target(self) -> None:
        values = [
            True,
            {"readyState": "complete", "oldDocument": False},
            {
                "state": "target_absent",
                "page_ad_ids": ["1111111111"],
                "next_page": {"x": 40.0, "y": 60.0},
            },
            {
                "state": "ready",
                "ad_id": AD_ID,
            },
        ]

        def handler(method, params):
            if method == "Page.navigate":
                return {}
            if method == "Runtime.evaluate":
                if not values:
                    raise AssertionError("unexpected Runtime.evaluate")
                value = values.pop(0)
                return {"result": {"type": "object", "value": value}}
            if method == "Input.dispatchMouseEvent":
                return {}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
            sleep=lambda _seconds: None,
        )

        page.open_delete_controls(AD_ID)

        self.assertEqual(values, [])
        self.assertEqual(page._bound_delete_ad_id, AD_ID)
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            2,
        )

    def test_open_delete_controls_waits_for_transient_next_control(self) -> None:
        values = [
            True,
            {"readyState": "complete", "oldDocument": False},
            {
                "state": "target_absent",
                "page_ad_ids": ["1111111111"],
                "next_page": None,
            },
            {
                "state": "target_absent",
                "page_ad_ids": ["1111111111"],
                "next_page": {"x": 40.0, "y": 60.0},
            },
            {
                "state": "ready",
                "ad_id": AD_ID,
            },
        ]

        def handler(method, params):
            if method == "Page.navigate":
                return {}
            if method == "Runtime.evaluate":
                if not values:
                    raise AssertionError("unexpected Runtime.evaluate")
                value = values.pop(0)
                return {"result": {"type": "object", "value": value}}
            if method == "Input.dispatchMouseEvent":
                return {}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
            sleep=lambda _seconds: None,
        )

        page.open_delete_controls(AD_ID)

        self.assertEqual(values, [])
        self.assertEqual(page._bound_delete_ad_id, AD_ID)
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            2,
        )

    def test_open_delete_controls_last_page_miss_is_bounded_and_no_input(self) -> None:
        setup_values = [
            True,
            {"readyState": "complete", "oldDocument": False},
        ]
        last_page = {
            "state": "target_absent",
            "page_ad_ids": ["1111111111"],
            "next_page": None,
        }
        monotonic_values = iter((0.0, 0.0, 0.1))

        def handler(method, params):
            if method == "Page.navigate":
                return {}
            if method == "Runtime.evaluate":
                value = setup_values.pop(0) if setup_values else last_page
                return {"result": {"type": "object", "value": value}}
            if method == "Input.dispatchMouseEvent":
                raise AssertionError("pagination input must not be attempted")
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            timeout_seconds=0.1,
            client_factory=lambda: client,
            sleep=lambda _seconds: None,
            monotonic=lambda: next(monotonic_values),
        )

        with self.assertRaisesRegex(PrivateWebCdpError, "navigate_delete"):
            page.open_delete_controls(AD_ID)

        self.assertIsNone(page._bound_delete_ad_id)
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            0,
        )

    def test_open_delete_confirmation_uses_one_browser_input_pair(self) -> None:
        runtime_values = [
            {"x": 10.0, "y": 20.0},
            {"state": "ready", "ad_id": AD_ID},
        ]

        def handler(method, params):
            if method == "Runtime.evaluate":
                value = runtime_values.pop(0)
                return {"result": {"type": "object", "value": value}}
            if method == "Input.dispatchMouseEvent":
                return {}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        self.bind_delete(page)

        page.open_delete_confirmation()

        self.assertTrue(page._delete_confirmation_attempted)
        self.assertEqual(runtime_values, [])
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            2,
        )

    def test_open_delete_confirmation_input_failure_is_unknown_no_retry(self) -> None:
        def handler(method, params):
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "type": "object",
                        "value": {"x": 10.0, "y": 20.0},
                    }
                }
            if method == "Input.dispatchMouseEvent":
                raise RuntimeError("browser response lost")
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        self.bind_delete(page)

        with self.assertRaises(PrivateWebSubmitUnknownError) as caught:
            page.open_delete_confirmation()

        self.assertEqual(caught.exception.stage, "delete_open_confirmation")
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            1,
        )
        with self.assertRaisesRegex(
            PrivateWebCdpError,
            "delete_confirmation_already_attempted",
        ):
            page.open_delete_confirmation()
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            1,
        )

    def test_submit_delete_uses_one_confirm_input_pair_and_settles(self) -> None:
        runtime_values = [
            {"x": 10.0, "y": 20.0},
            True,
        ]

        def handler(method, params):
            if method == "Runtime.evaluate":
                value = runtime_values.pop(0)
                return {"result": {"type": "object", "value": value}}
            if method == "Input.dispatchMouseEvent":
                return {}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        self.bind_delete(page, confirmation=True)

        page.submit_delete()

        self.assertEqual(runtime_values, [])
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            2,
        )
        with self.assertRaisesRegex(
            PrivateWebCdpError,
            "delete_submit_already_attempted",
        ):
            page.submit_delete()
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            2,
        )

    def test_submit_delete_provider_failure_after_input_is_unknown_no_retry(self) -> None:
        def handler(method, params):
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "type": "object",
                        "value": {"x": 10.0, "y": 20.0},
                    }
                }
            if method == "Input.dispatchMouseEvent":
                raise RuntimeError("provider response lost")
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: client,
        )
        self.bind_delete(page, confirmation=True)

        with self.assertRaises(PrivateWebSubmitUnknownError) as caught:
            page.submit_delete()

        self.assertEqual(caught.exception.stage, "delete_submit")
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            1,
        )
        with self.assertRaisesRegex(
            PrivateWebCdpError,
            "delete_submit_already_attempted",
        ):
            page.submit_delete()
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            1,
        )

    def test_submit_delete_settlement_timeout_is_unknown_no_retry(self) -> None:
        runtime_values = [
            {"x": 10.0, "y": 20.0},
            False,
        ]
        monotonic_values = iter((0.0, 0.1))

        def handler(method, params):
            if method == "Runtime.evaluate":
                value = runtime_values.pop(0)
                return {"result": {"type": "object", "value": value}}
            if method == "Input.dispatchMouseEvent":
                return {}
            raise AssertionError(f"unexpected method: {method}")

        client = FakeClient(handler)
        page = CdpPrivateWebPage(
            "http://127.0.0.1:19610",
            timeout_seconds=0.1,
            client_factory=lambda: client,
            sleep=lambda _seconds: None,
            monotonic=lambda: next(monotonic_values),
        )
        self.bind_delete(page, confirmation=True)

        with self.assertRaises(PrivateWebSubmitUnknownError) as caught:
            page.submit_delete()

        self.assertEqual(caught.exception.stage, "delete_submit_settle")
        self.assertEqual(
            [method for method, _params in client.calls].count(
                "Input.dispatchMouseEvent"
            ),
            2,
        )
        with self.assertRaisesRegex(
            PrivateWebCdpError,
            "delete_submit_already_attempted",
        ):
            page.submit_delete()


if __name__ == "__main__":
    unittest.main()