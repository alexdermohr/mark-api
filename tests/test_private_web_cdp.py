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
from mark_api.private_web import PrivateWebEditorSnapshot, PrivateWebEditorState
from mark_api.private_web_cdp import (
    CdpCookieProvider,
    CdpPrivateWebOwnerReader,
    CdpPrivateWebPage,
    PrivateWebCdpError,
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
            if method != "Runtime.evaluate":
                raise AssertionError(f"unexpected method: {method}")
            expression = params.get("expression")
            if "button.click()" in str(expression):
                return {
                    "result": {
                        "type": "boolean",
                        "value": True,
                    }
                }
            return {
                "result": {
                    "type": "string",
                    "value": "confirmed",
                }
            }

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
            ["Runtime.evaluate", "Runtime.evaluate"],
        )
        expression = client.calls[0][1]["expression"]
        self.assertIn("location.origin", expression)
        self.assertIn("/p-anzeige-bearbeiten.html", expression)
        self.assertIn(AD_ID, expression)
        self.assertIn("Existing title", expression)
        self.assertIn("Existing description", expression)
        self.assertIn("Anzeige speichern", expression)
        self.assertIn("getBoundingClientRect", expression)
        self.assertIn("button.click()", expression)
        self.assertNotIn("Input.dispatchMouseEvent", str(client.calls))
        confirmation_expression = client.calls[1][1]["expression"]
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

    def test_submit_rejects_editor_drift_before_click(self) -> None:
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
        self.assertIn("button.click()", expression)

    def test_submit_staying_in_editor_is_unconfirmed_and_not_retryable(self) -> None:
        times = iter((0.0, 10.0))

        def handler(method, params):
            if method != "Runtime.evaluate":
                raise AssertionError(f"unexpected method: {method}")
            expression = params.get("expression")
            if "button.click()" in str(expression):
                return {"result": {"type": "boolean", "value": True}}
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
            ["Runtime.evaluate", "Runtime.evaluate"],
        )
        page.close()

    def test_submit_untrusted_redirect_is_unconfirmed_and_not_retryable(self) -> None:
        def handler(method, params):
            if method != "Runtime.evaluate":
                raise AssertionError(f"unexpected method: {method}")
            expression = params.get("expression")
            if "button.click()" in str(expression):
                return {"result": {"type": "boolean", "value": True}}
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

        confirmation_expression = client.calls[1][1]["expression"]
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


if __name__ == "__main__":
    unittest.main()