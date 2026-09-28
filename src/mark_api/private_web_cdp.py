from __future__ import annotations

import json
import socket
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlencode, urlparse
from urllib.request import ProxyHandler, build_opener

from .private_web import (
    PrivateWebEditorSnapshot,
    PrivateWebEditorState,
    _validated_ad_id,
)

_KLEINANZEIGEN_ORIGIN = "https://www.kleinanzeigen.de"
_EDITOR_PATH = "/p-anzeige-bearbeiten.html"
_MAX_TARGET_BYTES = 64 * 1024


def _proxy_free_loopback_opener() -> Callable[..., Any]:
    return build_opener(ProxyHandler({})).open


class PrivateWebCdpError(RuntimeError):
    """Sanitized local-CDP driver failure."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"private web cdp failed at {stage}")


class _CdpClient(Protocol):
    def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        ...

    def close(self) -> None:
        ...


def _validated_loopback_endpoint(endpoint: str) -> tuple[str, int]:
    if not isinstance(endpoint, str):
        raise TypeError("endpoint must be a string")
    parsed = urlparse(endpoint)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("endpoint must be an exact loopback HTTP origin")
    return f"http://127.0.0.1:{port}", port


def _validated_https_origin(origin: str) -> str:
    if not isinstance(origin, str):
        raise TypeError("origin must be a string")
    parsed = urlparse(origin)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("origin must be an exact HTTPS origin")
    return f"https://{parsed.hostname}"


class _LoopbackCdpClient:
    """Minimal CDP client bound to one loopback Chrome page websocket."""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_seconds: float,
        opener: Callable[..., Any] | None = None,
        websocket_factory: Callable[..., Any] | None = None,
        tcp_socket_factory: Callable[..., Any] = socket.create_connection,
    ) -> None:
        self._endpoint, self._port = _validated_loopback_endpoint(endpoint)
        self._timeout_seconds = timeout_seconds
        self._opener = opener or _proxy_free_loopback_opener()
        self._websocket_factory = websocket_factory
        self._tcp_socket_factory = tcp_socket_factory
        self._next_id = 0
        self._socket = self._connect()

    def _connect(self):
        try:
            with self._opener(
                f"{self._endpoint}/json/list",
                timeout=self._timeout_seconds,
            ) as response:
                payload = response.read(_MAX_TARGET_BYTES + 1)
            if len(payload) > _MAX_TARGET_BYTES:
                raise ValueError("target list too large")
            targets = json.loads(payload.decode("utf-8"))
            pages = [
                item
                for item in targets
                if isinstance(item, dict) and item.get("type") == "page"
            ]
            if len(pages) != 1:
                raise ValueError("exactly one page target is required")
            websocket_url = pages[0].get("webSocketDebuggerUrl")
            if not isinstance(websocket_url, str):
                raise ValueError("page target has no websocket URL")
            parsed = urlparse(websocket_url)
            try:
                websocket_port = parsed.port
            except ValueError:
                websocket_port = None
            if (
                parsed.scheme != "ws"
                or parsed.hostname != "127.0.0.1"
                or websocket_port != self._port
                or parsed.username is not None
                or parsed.password is not None
                or not parsed.path.startswith("/devtools/page/")
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("page websocket escaped loopback binding")
            factory = self._websocket_factory
            if factory is None:
                import websocket  # type: ignore[import-not-found]

                factory = websocket.create_connection
            raw_socket = self._tcp_socket_factory(
                ("127.0.0.1", self._port),
                timeout=self._timeout_seconds,
            )
            try:
                return factory(
                    websocket_url,
                    timeout=self._timeout_seconds,
                    suppress_origin=True,
                    socket=raw_socket,
                )
            except Exception:
                try:
                    raw_socket.close()
                except Exception:
                    pass
                raise
        except Exception:  # noqa: BLE001 - never expose browser/provider detail.
            raise PrivateWebCdpError("connect") from None

    def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        self._next_id += 1
        message_id = self._next_id
        request: dict[str, object] = {
            "id": message_id,
            "method": method,
        }
        if params:
            request["params"] = params
        try:
            self._socket.send(
                json.dumps(request, separators=(",", ":"), ensure_ascii=False)
            )
            while True:
                response = json.loads(self._socket.recv())
                if response.get("id") != message_id:
                    continue
                if "error" in response:
                    raise ValueError("cdp returned an error")
                result = response.get("result")
                if not isinstance(result, dict):
                    raise ValueError("cdp returned an invalid result")
                return result
        except Exception:  # noqa: BLE001 - never expose browser/provider detail.
            raise PrivateWebCdpError("call") from None

    def close(self) -> None:
        try:
            self._socket.close()
        except Exception:  # noqa: BLE001 - close is best effort and sanitized.
            pass


class CdpCookieProvider:
    """Ephemeral Cookie header provider backed by the same local CDP session."""

    def __init__(
        self,
        endpoint: str,
        *,
        expected_origin: str = _KLEINANZEIGEN_ORIGIN,
        timeout_seconds: float = 5.0,
        client_factory: Callable[[], _CdpClient] | None = None,
    ) -> None:
        canonical_endpoint, _ = _validated_loopback_endpoint(endpoint)
        self._endpoint = canonical_endpoint
        self._expected_origin = _validated_https_origin(expected_origin)
        if not isinstance(timeout_seconds, (int, float)):
            raise TypeError("timeout_seconds must be numeric")
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("timeout_seconds must be in (0, 30]")
        self._timeout_seconds = float(timeout_seconds)
        self._target_url = (
            f"{self._expected_origin}/m-meine-anzeigen-verwalten.json"
        )
        self._client_factory = client_factory or (
            lambda: _LoopbackCdpClient(
                self._endpoint,
                timeout_seconds=self._timeout_seconds,
            )
        )
        self._client_instance: _CdpClient | None = None

    @classmethod
    def from_port(
        cls,
        port: int,
        **kwargs,
    ) -> "CdpCookieProvider":
        if not isinstance(port, int) or isinstance(port, bool):
            raise TypeError("port must be an integer")
        return cls(f"http://127.0.0.1:{port}", **kwargs)

    def _client(self) -> _CdpClient:
        if self._client_instance is None:
            try:
                self._client_instance = self._client_factory()
            except Exception:  # noqa: BLE001 - sanitize browser boundary.
                raise PrivateWebCdpError("cookies") from None
        return self._client_instance

    def close(self) -> None:
        client = self._client_instance
        self._client_instance = None
        if client is not None:
            client.close()

    def __call__(self) -> str | None:
        client = self._client()
        try:
            result = client.call(
                "Network.getCookies",
                {"urls": [self._target_url]},
            )
            raw_cookies = result.get("cookies")
            if not isinstance(raw_cookies, list):
                raise ValueError("invalid cookie result")
            pairs: list[str] = []
            for item in raw_cookies:
                if not isinstance(item, dict):
                    raise ValueError("invalid cookie item")
                name = item.get("name")
                value = item.get("value")
                if (
                    not isinstance(name, str)
                    or not name
                    or any(char in name for char in "\r\n;=")
                    or not isinstance(value, str)
                    or any(char in value for char in "\r\n;")
                ):
                    raise ValueError("unsafe cookie item")
                pairs.append(f"{name}={value}")
            return "; ".join(pairs) if pairs else None
        except PrivateWebCdpError:
            raise PrivateWebCdpError("cookies") from None
        except Exception:  # noqa: BLE001 - never expose cookie/provider detail.
            raise PrivateWebCdpError("cookies") from None


class CdpPrivateWebPage:
    """Concrete PrivateWebPage over one Grabowski-managed local Chrome CDP port."""

    def __init__(
        self,
        endpoint: str,
        *,
        expected_origin: str = _KLEINANZEIGEN_ORIGIN,
        timeout_seconds: float = 5.0,
        client_factory: Callable[[], _CdpClient] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        canonical_endpoint, _ = _validated_loopback_endpoint(endpoint)
        self._endpoint = canonical_endpoint
        self._expected_origin = _validated_https_origin(expected_origin)
        if not isinstance(timeout_seconds, (int, float)):
            raise TypeError("timeout_seconds must be numeric")
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("timeout_seconds must be in (0, 30]")
        self._timeout_seconds = float(timeout_seconds)
        self._client_factory = client_factory or (
            lambda: _LoopbackCdpClient(
                self._endpoint,
                timeout_seconds=self._timeout_seconds,
            )
        )
        self._client_instance: _CdpClient | None = None
        self._sleep = sleep
        self._monotonic = monotonic
        self._submit_attempted = False

    @classmethod
    def from_port(
        cls,
        port: int,
        **kwargs,
    ) -> "CdpPrivateWebPage":
        if not isinstance(port, int) or isinstance(port, bool):
            raise TypeError("port must be an integer")
        return cls(f"http://127.0.0.1:{port}", **kwargs)

    def _client(self) -> _CdpClient:
        if self._client_instance is None:
            try:
                self._client_instance = self._client_factory()
            except Exception:  # noqa: BLE001 - sanitize runtime boundary.
                raise PrivateWebCdpError("connect") from None
        return self._client_instance

    def close(self) -> None:
        client = self._client_instance
        self._client_instance = None
        if client is not None:
            client.close()

    @staticmethod
    def _runtime_value(
        client: _CdpClient,
        expression: str,
    ) -> object:
        result = client.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": False,
            },
        )
        if "exceptionDetails" in result:
            raise PrivateWebCdpError("evaluate")
        remote = result.get("result")
        if not isinstance(remote, dict) or "value" not in remote:
            raise PrivateWebCdpError("evaluate")
        return remote["value"]

    def _evaluate(self, stage: str, expression: str) -> object:
        client = self._client()
        try:
            return self._runtime_value(client, expression)
        except PrivateWebCdpError:
            raise PrivateWebCdpError(stage) from None
        except Exception:  # noqa: BLE001 - sanitize runtime boundary.
            raise PrivateWebCdpError(stage) from None
        finally:
            pass

    def open_editor(self, ad_id: str) -> None:
        target_ad_id = _validated_ad_id(ad_id)
        target_url = (
            f"{self._expected_origin}{_EDITOR_PATH}?"
            + urlencode({"adId": target_ad_id})
        )
        self._submit_attempted = False
        client = self._client()
        marker = json.dumps(
            f"__mark_private_web_navigation_probe_{target_ad_id}__"
        )
        try:
            armed = self._runtime_value(
                client,
                (
                    "(() => { const key = "
                    + marker
                    + "; globalThis[key] = true; "
                    + "return globalThis[key] === true; })()"
                ),
            )
            if armed is not True:
                raise PrivateWebCdpError("navigate")
            navigation = client.call("Page.navigate", {"url": target_url})
            if navigation.get("errorText"):
                raise PrivateWebCdpError("navigate")
            deadline = self._monotonic() + self._timeout_seconds
            readiness_expression = (
                "(() => ({"
                + "readyState: document.readyState,"
                + "oldDocument: globalThis["
                + marker
                + "] === true"
                + "}))()"
            )
            while True:
                try:
                    readiness = self._runtime_value(
                        client,
                        readiness_expression,
                    )
                except PrivateWebCdpError:
                    readiness = None
                if (
                    isinstance(readiness, dict)
                    and readiness.get("readyState") == "complete"
                    and readiness.get("oldDocument") is False
                ):
                    return
                if self._monotonic() >= deadline:
                    raise PrivateWebCdpError("navigate")
                self._sleep(0.05)
        except PrivateWebCdpError:
            raise
        except Exception:  # noqa: BLE001 - sanitize runtime boundary.
            raise PrivateWebCdpError("navigate") from None
        finally:
            pass

    def read_editor(self) -> PrivateWebEditorSnapshot:
        origin = json.dumps(self._expected_origin)
        path = json.dumps(_EDITOR_PATH)
        expression = f"""
(() => {{
  const stateOnly = (state) => ({{state}});
  const body = (document.body && document.body.innerText || "").toLowerCase();
  const currentOrigin = location.origin;
  const currentPath = location.pathname;
  const hasCaptcha = Boolean(
    document.querySelector(
      'iframe[src*="captcha" i], [data-sitekey], [id*="captcha" i], [class*="captcha" i]'
    )
  ) || body.includes("captcha") || body.includes("ich bin kein roboter");
  const hasMfa = Boolean(
    document.querySelector(
      'input[autocomplete="one-time-code"], input[name*="otp" i], input[name*="mfa" i]'
    )
  ) || body.includes("bestätigungscode") || body.includes("sicherheitscode");
  const hasSecurityChallenge =
    body.includes("sicherheitsprüfung") ||
    body.includes("sicherheitscheck") ||
    body.includes("ungewöhnliche aktivität") ||
    body.includes("bestätige, dass du ein mensch bist");
  const hasLogin =
    currentPath.startsWith("/u/login/") ||
    Boolean(document.querySelector('input[type="password"]'));

  if (currentOrigin !== {origin}) return stateOnly("unknown");
  if (hasCaptcha) return stateOnly("captcha_required");
  if (hasMfa) return stateOnly("mfa_required");
  if (hasSecurityChallenge) return stateOnly("security_challenge");
  if (hasLogin) return stateOnly("login_required");

  const title = document.querySelector("#ad-title");
  const description = document.querySelector("#ad-description");
  const saveButtons = Array.from(document.querySelectorAll("button")).filter(
    (button) => (button.innerText || "").trim() === "Anzeige speichern"
  );
  const adId = new URL(location.href).searchParams.get("adId");
  const idOk =
    typeof adId === "string" &&
    /^[0-9]+$/.test(adId) &&
    adId.length > 0 &&
    adId.length <= 32;
  const titleOk =
    title instanceof HTMLInputElement &&
    title.getAttribute("name") === "title";
  const descriptionOk =
    description instanceof HTMLTextAreaElement &&
    description.getAttribute("name") === "description";
  const saveOk =
    saveButtons.length === 1 &&
    saveButtons[0].getAttribute("type") === "button" &&
    !saveButtons[0].disabled;

  if (
    currentPath !== {path} ||
    !idOk ||
    !titleOk ||
    !descriptionOk ||
    !saveOk
  ) {{
    return stateOnly("unknown");
  }}
  return {{
    state: "ready",
    ad_id: adId,
    title: title.value,
    description: description.value,
  }};
}})()
"""
        value = self._evaluate("read_editor", expression)
        if not isinstance(value, dict):
            return PrivateWebEditorSnapshot(state=PrivateWebEditorState.UNKNOWN)
        raw_state = value.get("state")
        try:
            state = PrivateWebEditorState(raw_state)
        except (TypeError, ValueError):
            return PrivateWebEditorSnapshot(state=PrivateWebEditorState.UNKNOWN)
        if state is not PrivateWebEditorState.READY:
            return PrivateWebEditorSnapshot(state=state)
        ad_id = value.get("ad_id")
        title = value.get("title")
        description = value.get("description")
        if not isinstance(ad_id, str):
            return PrivateWebEditorSnapshot(state=PrivateWebEditorState.UNKNOWN)
        if not isinstance(title, str) or not isinstance(description, str):
            return PrivateWebEditorSnapshot(state=PrivateWebEditorState.UNKNOWN)
        try:
            return PrivateWebEditorSnapshot(
                state=PrivateWebEditorState.READY,
                ad_id=ad_id,
                title=title,
                description=description,
            )
        except (TypeError, ValueError):
            return PrivateWebEditorSnapshot(state=PrivateWebEditorState.UNKNOWN)

    def _replace_field(
        self,
        *,
        stage: str,
        selector: str,
        expected_name: str,
        prototype: str,
        value: str,
    ) -> None:
        if not isinstance(value, str):
            raise TypeError("field value must be a string")
        prepare_expression = f"""
(() => {{
  const element = document.querySelector({json.dumps(selector)});
  if (!(element instanceof {prototype})) return false;
  if (element.getAttribute("name") !== {json.dumps(expected_name)}) return false;
  element.focus();
  return document.activeElement === element;
}})()
"""
        verify_expression = f"""
(() => {{
  const element = document.querySelector({json.dumps(selector)});
  return (
    element instanceof {prototype} &&
    element.getAttribute("name") === {json.dumps(expected_name)} &&
    element.value === {json.dumps(value)}
  );
}})()
"""
        client = self._client()
        try:
            if self._runtime_value(client, prepare_expression) is not True:
                raise PrivateWebCdpError(stage)

            client.call(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyDown",
                    "key": "a",
                    "code": "KeyA",
                    "windowsVirtualKeyCode": 65,
                    "nativeVirtualKeyCode": 65,
                    "modifiers": 2,
                },
            )
            client.call(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyUp",
                    "key": "a",
                    "code": "KeyA",
                    "windowsVirtualKeyCode": 65,
                    "nativeVirtualKeyCode": 65,
                    "modifiers": 2,
                },
            )
            client.call("Input.insertText", {"text": value})
            client.call(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyDown",
                    "key": "Tab",
                    "code": "Tab",
                    "windowsVirtualKeyCode": 9,
                    "nativeVirtualKeyCode": 9,
                },
            )
            client.call(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyUp",
                    "key": "Tab",
                    "code": "Tab",
                    "windowsVirtualKeyCode": 9,
                    "nativeVirtualKeyCode": 9,
                },
            )
            if self._runtime_value(client, verify_expression) is not True:
                raise PrivateWebCdpError(stage)
        except PrivateWebCdpError:
            raise PrivateWebCdpError(stage) from None
        except Exception:  # noqa: BLE001 - sanitize browser/provider boundary.
            raise PrivateWebCdpError(stage) from None
        finally:
            pass

    def replace_title(self, value: str) -> None:
        self._replace_field(
            stage="replace_title",
            selector="#ad-title",
            expected_name="title",
            prototype="HTMLInputElement",
            value=value,
        )

    def replace_description(self, value: str) -> None:
        self._replace_field(
            stage="replace_description",
            selector="#ad-description",
            expected_name="description",
            prototype="HTMLTextAreaElement",
            value=value,
        )

    def submit(self) -> None:
        if self._submit_attempted:
            raise PrivateWebCdpError("submit_already_attempted")
        self._submit_attempted = True
        expression = """
(() => {
  const buttons = Array.from(document.querySelectorAll("button")).filter(
    (button) => (button.innerText || "").trim() === "Anzeige speichern"
  );
  if (buttons.length !== 1) return null;
  const button = buttons[0];
  if (button.getAttribute("type") !== "button" || button.disabled) return null;
  button.scrollIntoView({block: "center", inline: "center"});
  const rect = button.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;
  const hit = document.elementFromPoint(x, y);
  if (hit !== button && !button.contains(hit)) return null;
  return {x, y};
})()
"""
        client = self._client()
        try:
            settled = client.call(
                "Runtime.evaluate",
                {
                    "expression": (
                        "new Promise(resolve => "
                        "requestAnimationFrame(() => "
                        "requestAnimationFrame(() => resolve(true))))"
                    ),
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
            settled_remote = settled.get("result")
            if (
                not isinstance(settled_remote, dict)
                or settled_remote.get("value") is not True
            ):
                raise PrivateWebCdpError("submit_settle")

            point = self._runtime_value(client, expression)
            if not isinstance(point, dict):
                raise PrivateWebCdpError("submit")
            x = point.get("x")
            y = point.get("y")
            if (
                isinstance(x, bool)
                or not isinstance(x, (int, float))
                or isinstance(y, bool)
                or not isinstance(y, (int, float))
                or x < 0
                or y < 0
            ):
                raise PrivateWebCdpError("submit")
            position = {
                "x": float(x),
                "y": float(y),
            }
            client.call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseMoved",
                    **position,
                    "button": "none",
                    "buttons": 0,
                    "pointerType": "mouse",
                },
            )
            client.call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    **position,
                    "button": "left",
                    "buttons": 1,
                    "clickCount": 1,
                    "pointerType": "mouse",
                },
            )
            client.call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    **position,
                    "button": "left",
                    "buttons": 0,
                    "clickCount": 1,
                    "pointerType": "mouse",
                },
            )

            deadline = self._monotonic() + self._timeout_seconds
            while True:
                path = self._runtime_value(client, "location.pathname")
                if path != _EDITOR_PATH:
                    return
                if self._monotonic() >= deadline:
                    raise PrivateWebCdpError("submit_unconfirmed")
                self._sleep(0.05)
        except PrivateWebCdpError as exc:
            raise PrivateWebCdpError(exc.stage) from None
        except Exception:  # noqa: BLE001 - ambiguous submit stays non-retryable.
            raise PrivateWebCdpError("submit") from None
        finally:
            pass