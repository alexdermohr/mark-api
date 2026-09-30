from __future__ import annotations

import json
import math
import socket
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Protocol
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from .domain import AdSnapshot, LifecycleState
from .ports import AdsReader, WriteNotAttemptedError
from .private_web import (
    PrivateWebDeleteSnapshot,
    PrivateWebEditorSnapshot,
    PrivateWebEditorState,
    PrivateWebStateSnapshot,
    PrivateWebSubmitUnknownError,
    _validated_ad_id,
)
from .results import ReadResult, ReadStatus

_KLEINANZEIGEN_ORIGIN = "https://www.kleinanzeigen.de"
_EDITOR_PATH = "/p-anzeige-bearbeiten.html"
_MANAGEMENT_PATH = "/m-meine-anzeigen.html"
# Keep this fail-closed traversal bound aligned with the Management reader
# without importing adapter modules into the browser/CDP boundary.
_MANAGEMENT_MAX_PAGES = 100
_POST_SUBMIT_PATH = _MANAGEMENT_PATH
_MAX_TARGET_BYTES = 64 * 1024


class _RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _proxy_free_loopback_opener() -> Callable[..., Any]:
    return build_opener(ProxyHandler({}), _RejectRedirectHandler()).open


class PrivateWebCdpError(RuntimeError):
    """Sanitized local-CDP driver failure."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"private web cdp failed at {stage}")


class PrivateWebCdpWriteNotAttemptedError(
    PrivateWebCdpError,
    WriteNotAttemptedError,
):
    """CDP failed before browser input could reach the platform."""


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
            discovery_url = f"{self._endpoint}/json/list"
            with self._opener(
                discovery_url,
                timeout=self._timeout_seconds,
            ) as response:
                geturl = getattr(response, "geturl", None)
                if not callable(geturl) or geturl() != discovery_url:
                    raise ValueError("target discovery escaped loopback binding")
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
        if (
            timeout_seconds <= 0
            or timeout_seconds > 30
            or not math.isfinite(timeout_seconds)
        ):
            raise ValueError("timeout_seconds must be finite and in (0, 30]")
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
        if (
            timeout_seconds <= 0
            or timeout_seconds > 30
            or not math.isfinite(timeout_seconds)
        ):
            raise ValueError("timeout_seconds must be finite and in (0, 30]")
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
        self._bound_ad_id: str | None = None
        self._last_ready_snapshot: PrivateWebEditorSnapshot | None = None
        self._state_submit_attempted = False
        self._bound_state_ad_id: str | None = None
        self._last_state_snapshot: PrivateWebStateSnapshot | None = None
        self._delete_confirmation_attempted = False
        self._delete_submit_attempted = False
        self._bound_delete_ad_id: str | None = None
        self._last_delete_snapshot: PrivateWebDeleteSnapshot | None = None

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
        self._bound_ad_id = None
        self._last_ready_snapshot = None
        self._bound_state_ad_id = None
        self._last_state_snapshot = None
        self._bound_delete_ad_id = None
        self._last_delete_snapshot = None
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
        self._bound_ad_id = None
        self._last_ready_snapshot = None
        self._state_submit_attempted = False
        self._bound_state_ad_id = None
        self._last_state_snapshot = None
        self._delete_confirmation_attempted = False
        self._delete_submit_attempted = False
        self._bound_delete_ad_id = None
        self._last_delete_snapshot = None
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
            terminal_challenge_states = {
                PrivateWebEditorState.LOGIN_REQUIRED,
                PrivateWebEditorState.MFA_REQUIRED,
                PrivateWebEditorState.CAPTCHA_REQUIRED,
                PrivateWebEditorState.SECURITY_CHALLENGE,
            }
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
                    self._bound_ad_id = target_ad_id
                    try:
                        snapshot = self.read_editor()
                    except PrivateWebCdpError:
                        snapshot = None
                    self._last_ready_snapshot = None
                    if snapshot is not None and (
                        (
                            snapshot.state is PrivateWebEditorState.READY
                            and snapshot.ad_id == target_ad_id
                        )
                        or snapshot.state in terminal_challenge_states
                    ):
                        return
                    self._bound_ad_id = None
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
        self._last_ready_snapshot = None
        origin = json.dumps(self._expected_origin)
        path = json.dumps(_EDITOR_PATH)
        expression = f"""
(() => {{
  const stateOnly = (state) => ({{state}});
  const currentOrigin = location.origin;
  const currentPath = location.pathname;
  const challengeText = Array.from(
    document.querySelectorAll(
      '[role="dialog"], [role="alert"], [aria-modal="true"], [id*="challenge" i], [class*="challenge" i]'
    )
  )
    .map((element) => (element.innerText || "").toLowerCase())
    .join("\\n");
  const hasCaptcha = Boolean(
    document.querySelector(
      'iframe[src*="captcha" i], [data-sitekey], [id*="captcha" i], [class*="captcha" i]'
    )
  ) ||
    challengeText.includes("captcha") ||
    challengeText.includes("ich bin kein roboter");
  const hasMfa = Boolean(
    document.querySelector(
      'input[autocomplete="one-time-code"], input[name*="otp" i], input[name*="mfa" i]'
    )
  ) ||
    challengeText.includes("bestätigungscode") ||
    challengeText.includes("sicherheitscode");
  const hasSecurityChallenge =
    challengeText.includes("sicherheitsprüfung") ||
    challengeText.includes("sicherheitscheck") ||
    challengeText.includes("ungewöhnliche aktivität") ||
    challengeText.includes("bestätige, dass du ein mensch bist");
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
            snapshot = PrivateWebEditorSnapshot(
                state=PrivateWebEditorState.READY,
                ad_id=ad_id,
                title=title,
                description=description,
            )
        except (TypeError, ValueError):
            return PrivateWebEditorSnapshot(state=PrivateWebEditorState.UNKNOWN)
        if ad_id == self._bound_ad_id:
            self._last_ready_snapshot = snapshot
        return snapshot


    def _state_control_expression(
        self,
        target_ad_id: str,
        *,
        activation_label: str | None = None,
        include_pagination: bool = False,
    ) -> str:
        origin = json.dumps(self._expected_origin)
        management_path = json.dumps(_MANAGEMENT_PATH)
        editor_path = json.dumps(_EDITOR_PATH)
        ad_id = json.dumps(target_ad_id)
        expected_control = json.dumps(activation_label)
        allow_pagination = json.dumps(include_pagination)
        return f"""
(() => {{
  const stateOnly = (state) => ({{state}});
  const currentOrigin = location.origin;
  const currentPath = location.pathname;
  const targetAdId = {ad_id};
  const editorPath = {editor_path};
  const expectedControl = {expected_control};
  const allowPagination = {allow_pagination};
  const challengeText = Array.from(
    document.querySelectorAll(
      '[role="dialog"], [role="alert"], [aria-modal="true"], [id*="challenge" i], [class*="challenge" i]'
    )
  )
    .map((element) => (element.innerText || "").toLowerCase())
    .join("\\n");
  const hasCaptcha = Boolean(
    document.querySelector(
      'iframe[src*="captcha" i], [data-sitekey], [id*="captcha" i], [class*="captcha" i]'
    )
  ) ||
    challengeText.includes("captcha") ||
    challengeText.includes("ich bin kein roboter");
  const hasMfa = Boolean(
    document.querySelector(
      'input[autocomplete="one-time-code"], input[name*="otp" i], input[name*="mfa" i]'
    )
  ) ||
    challengeText.includes("bestätigungscode") ||
    challengeText.includes("sicherheitscode");
  const hasSecurityChallenge =
    challengeText.includes("sicherheitsprüfung") ||
    challengeText.includes("sicherheitscheck") ||
    challengeText.includes("ungewöhnliche aktivität") ||
    challengeText.includes("bestätige, dass du ein mensch bist");
  const hasLogin =
    currentPath.startsWith("/u/login/") ||
    Boolean(document.querySelector('input[type="password"]'));

  if (currentOrigin !== {origin}) return stateOnly("unknown");
  if (hasCaptcha) return stateOnly("captcha_required");
  if (hasMfa) return stateOnly("mfa_required");
  if (hasSecurityChallenge) return stateOnly("security_challenge");
  if (hasLogin) return stateOnly("login_required");
  if (currentPath !== {management_path}) return stateOnly("unknown");

  const editorId = (link) => {{
    try {{
      const raw = link.getAttribute("href");
      if (!raw) return null;
      const url = new URL(raw, location.href);
      if (url.origin !== {origin} || url.pathname !== editorPath) return null;
      const value = url.searchParams.get("adId");
      if (
        typeof value !== "string" ||
        value.length === 0 ||
        value.length > 32 ||
        !/^[0-9]+$/.test(value)
      ) {{
        return null;
      }}
      return value;
    }} catch (_error) {{
      return null;
    }}
  }};
  const controlLabel = (element) =>
    (element.innerText || "").trim().toLowerCase();
  const isEligibleControl = (element) => {{
    if (
      (element instanceof HTMLButtonElement && element.disabled) ||
      element.getAttribute("aria-disabled") === "true"
    ) {{
      return false;
    }}
    const style = getComputedStyle(element);
    if (
      style.display === "none" ||
      style.visibility === "hidden" ||
      style.visibility === "collapse" ||
      style.pointerEvents === "none" ||
      Number(style.opacity) === 0
    ) {{
      return false;
    }}
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }};
  const lifecycleControls = (root) =>
    Array.from(root.querySelectorAll('button, a[href], [role="button"]')).filter(
      (element) => {{
        const label = controlLabel(element);
        return (
          (label === "reservieren" || label === "aktivieren") &&
          isEligibleControl(element)
        );
      }}
    );
  const targetEditLinks = Array.from(
    document.querySelectorAll("a[href]")
  ).filter((link) => editorId(link) === targetAdId);
  if (targetEditLinks.length > 1) return stateOnly("unknown");

  let bound = null;
  if (targetEditLinks.length === 1) {{
    let node = targetEditLinks[0].parentElement;
    for (let depth = 0; node && depth < 10; depth += 1) {{
      const editIds = Array.from(node.querySelectorAll("a[href]"))
        .map(editorId)
        .filter((value) => value !== null);
      const controls = lifecycleControls(node);
      if (
        editIds.length === 1 &&
        editIds[0] === targetAdId &&
        controls.length === 1
      ) {{
        bound = {{
          container: node,
          control: controls[0],
          label: controlLabel(controls[0]),
        }};
        break;
      }}
      node = node.parentElement;
    }}
    if (bound === null) return stateOnly("unknown");
  }} else {{
    if (!allowPagination) return stateOnly("unknown");
    const pageAdIds = Array.from(document.querySelectorAll("a[href]"))
      .map(editorId)
      .filter((value) => value !== null);
    const uniquePageAdIds = Array.from(new Set(pageAdIds));
    if (uniquePageAdIds.length === 0) return stateOnly("unknown");

    const nextCandidates = Array.from(
      document.querySelectorAll('button[aria-label="Nächste"]')
    ).filter((element) => isEligibleControl(element));
    if (nextCandidates.length > 1) return stateOnly("unknown");
    if (nextCandidates.length === 0) {{
      return {{
        state: "target_absent",
        page_ad_ids: uniquePageAdIds,
        next_page: null,
      }};
    }}

    const nextControl = nextCandidates[0];
    nextControl.scrollIntoView({{block: "center", inline: "center"}});
    const nextRect = nextControl.getBoundingClientRect();
    if (nextRect.width <= 0 || nextRect.height <= 0) return stateOnly("unknown");
    const nextX = nextRect.left + nextRect.width / 2;
    const nextY = nextRect.top + nextRect.height / 2;
    const nextHit = document.elementFromPoint(nextX, nextY);
    if (nextHit !== nextControl && !nextControl.contains(nextHit)) {{
      return stateOnly("unknown");
    }}
    if (
      !Number.isFinite(nextX) ||
      !Number.isFinite(nextY) ||
      nextX < 0 ||
      nextY < 0 ||
      nextX > window.innerWidth ||
      nextY > window.innerHeight
    ) {{
      return stateOnly("unknown");
    }}
    return {{
      state: "target_absent",
      page_ad_ids: uniquePageAdIds,
      next_page: {{x: nextX, y: nextY}},
    }};
  }}

  const lifecycleState =
    bound.label === "reservieren"
      ? "active"
      : bound.label === "aktivieren"
        ? "paused"
        : null;
  if (lifecycleState === null) return stateOnly("unknown");

  if (expectedControl === null) {{
    return {{
      state: "ready",
      ad_id: targetAdId,
      lifecycle_state: lifecycleState,
    }};
  }}
  if (bound.label !== expectedControl) return null;

  const control = bound.control;
  if (
    (control instanceof HTMLButtonElement && control.disabled) ||
    control.getAttribute("aria-disabled") === "true"
  ) {{
    return null;
  }}
  const style = getComputedStyle(control);
  if (
    style.display === "none" ||
    style.visibility === "hidden" ||
    style.visibility === "collapse" ||
    style.pointerEvents === "none" ||
    Number(style.opacity) === 0
  ) {{
    return null;
  }}
  control.scrollIntoView({{block: "center", inline: "center"}});
  const rect = control.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;
  const hit = document.elementFromPoint(x, y);
  if (hit !== control && !control.contains(hit)) return null;
  if (
    !Number.isFinite(x) ||
    !Number.isFinite(y) ||
    x < 0 ||
    y < 0 ||
    x > window.innerWidth ||
    y > window.innerHeight
  ) {{
    return null;
  }}
  return {{x, y}};
}})()
"""

    @staticmethod
    def _validated_state_page_probe(
        value: object,
        *,
        target_ad_id: str,
    ) -> tuple[str, tuple[str, ...] | None, tuple[float, float] | None]:
        if not isinstance(value, dict):
            return "unknown", None, None
        raw_state = value.get("state")
        if raw_state == PrivateWebEditorState.READY.value:
            ad_id = value.get("ad_id")
            lifecycle_state = value.get("lifecycle_state")
            if (
                ad_id == target_ad_id
                and lifecycle_state in {
                    LifecycleState.ACTIVE.value,
                    LifecycleState.PAUSED.value,
                }
            ):
                return "ready", None, None
            return "unknown", None, None
        if raw_state in {
            PrivateWebEditorState.LOGIN_REQUIRED.value,
            PrivateWebEditorState.MFA_REQUIRED.value,
            PrivateWebEditorState.CAPTCHA_REQUIRED.value,
            PrivateWebEditorState.SECURITY_CHALLENGE.value,
        }:
            return str(raw_state), None, None
        if raw_state != "target_absent":
            return "unknown", None, None

        raw_page_ad_ids = value.get("page_ad_ids")
        if (
            not isinstance(raw_page_ad_ids, list)
            or not raw_page_ad_ids
            or len(raw_page_ad_ids) > 1000
        ):
            return "unknown", None, None
        page_ad_ids: list[str] = []
        for raw_ad_id in raw_page_ad_ids:
            if not isinstance(raw_ad_id, str):
                return "unknown", None, None
            try:
                page_ad_ids.append(_validated_ad_id(raw_ad_id))
            except (TypeError, ValueError):
                return "unknown", None, None
        if len(set(page_ad_ids)) != len(page_ad_ids):
            return "unknown", None, None

        raw_next_page = value.get("next_page")
        if raw_next_page is None:
            return "target_absent", tuple(page_ad_ids), None
        if not isinstance(raw_next_page, dict):
            return "unknown", None, None
        raw_x = raw_next_page.get("x")
        raw_y = raw_next_page.get("y")
        if (
            isinstance(raw_x, bool)
            or not isinstance(raw_x, (int, float))
            or not math.isfinite(float(raw_x))
            or float(raw_x) < 0
            or isinstance(raw_y, bool)
            or not isinstance(raw_y, (int, float))
            or not math.isfinite(float(raw_y))
            or float(raw_y) < 0
        ):
            return "unknown", None, None
        return (
            "target_absent",
            tuple(page_ad_ids),
            (float(raw_x), float(raw_y)),
        )

    @staticmethod
    def _dispatch_browser_click(
        client: _CdpClient,
        *,
        x: float,
        y: float,
    ) -> None:
        client.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": x,
                "y": y,
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
            },
        )
        client.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": x,
                "y": y,
                "button": "left",
                "buttons": 0,
                "clickCount": 1,
            },
        )

    def open_state_controls(self, ad_id: str) -> None:
        target_ad_id = _validated_ad_id(ad_id)
        target_url = f"{self._expected_origin}{_MANAGEMENT_PATH}"
        self._state_submit_attempted = False
        self._bound_state_ad_id = None
        self._last_state_snapshot = None
        self._submit_attempted = False
        self._bound_ad_id = None
        self._last_ready_snapshot = None
        self._delete_confirmation_attempted = False
        self._delete_submit_attempted = False
        self._bound_delete_ad_id = None
        self._last_delete_snapshot = None
        client = self._client()
        marker = json.dumps(
            f"__mark_private_web_state_navigation_probe_{target_ad_id}__"
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
                raise PrivateWebCdpError("navigate_state")
            navigation = client.call("Page.navigate", {"url": target_url})
            if navigation.get("errorText"):
                raise PrivateWebCdpError("navigate_state")
            initial_deadline = self._monotonic() + self._timeout_seconds
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
                    break
                if self._monotonic() >= initial_deadline:
                    raise PrivateWebCdpError("navigate_state")
                self._sleep(0.05)

            self._bound_state_ad_id = target_ad_id
            prior_page_fingerprint: tuple[str, ...] | None = None
            seen_page_fingerprints: set[tuple[str, ...]] = set()
            for _page_number in range(1, _MANAGEMENT_MAX_PAGES + 1):
                page_deadline = self._monotonic() + self._timeout_seconds
                while True:
                    try:
                        probe_value = self._runtime_value(
                            client,
                            self._state_control_expression(
                                target_ad_id,
                                include_pagination=True,
                            ),
                        )
                    except PrivateWebCdpError:
                        probe_value = None
                    probe_state, page_fingerprint, next_point = (
                        self._validated_state_page_probe(
                            probe_value,
                            target_ad_id=target_ad_id,
                        )
                    )
                    if probe_state == "ready":
                        self._last_state_snapshot = None
                        return
                    if probe_state in {
                        PrivateWebEditorState.LOGIN_REQUIRED.value,
                        PrivateWebEditorState.MFA_REQUIRED.value,
                        PrivateWebEditorState.CAPTCHA_REQUIRED.value,
                        PrivateWebEditorState.SECURITY_CHALLENGE.value,
                    }:
                        self._last_state_snapshot = None
                        return
                    if (
                        probe_state == "target_absent"
                        and page_fingerprint is not None
                        and page_fingerprint != prior_page_fingerprint
                        and next_point is not None
                    ):
                        # The ad list can settle before the pager. A missing
                        # eligible Next control is provisional until this
                        # bounded page deadline expires.
                        break
                    if self._monotonic() >= page_deadline:
                        raise PrivateWebCdpError("navigate_state")
                    self._sleep(0.05)

                if page_fingerprint in seen_page_fingerprints:
                    raise PrivateWebCdpError("navigate_state")
                seen_page_fingerprints.add(page_fingerprint)
                if next_point is None:
                    raise PrivateWebCdpError("navigate_state")
                if len(seen_page_fingerprints) >= _MANAGEMENT_MAX_PAGES:
                    raise PrivateWebCdpError("navigate_state")

                try:
                    self._dispatch_browser_click(
                        client,
                        x=next_point[0],
                        y=next_point[1],
                    )
                except Exception:  # noqa: BLE001 - pagination is pre-submit.
                    raise PrivateWebCdpError("navigate_state") from None
                prior_page_fingerprint = page_fingerprint

            raise PrivateWebCdpError("navigate_state")
        except PrivateWebCdpError:
            self._bound_state_ad_id = None
            self._last_state_snapshot = None
            raise
        except Exception:  # noqa: BLE001 - sanitize runtime boundary.
            self._bound_state_ad_id = None
            self._last_state_snapshot = None
            raise PrivateWebCdpError("navigate_state") from None

    def read_state_controls(self) -> PrivateWebStateSnapshot:
        target_ad_id = self._bound_state_ad_id
        self._last_state_snapshot = None
        if target_ad_id is None:
            raise PrivateWebCdpError("read_state_controls")
        value = self._evaluate(
            "read_state_controls",
            self._state_control_expression(target_ad_id),
        )
        if not isinstance(value, dict):
            return PrivateWebStateSnapshot(state=PrivateWebEditorState.UNKNOWN)
        raw_state = value.get("state")
        try:
            state = PrivateWebEditorState(raw_state)
        except (TypeError, ValueError):
            return PrivateWebStateSnapshot(state=PrivateWebEditorState.UNKNOWN)
        if state is not PrivateWebEditorState.READY:
            return PrivateWebStateSnapshot(state=state)
        ad_id = value.get("ad_id")
        raw_lifecycle_state = value.get("lifecycle_state")
        if not isinstance(ad_id, str):
            return PrivateWebStateSnapshot(state=PrivateWebEditorState.UNKNOWN)
        try:
            lifecycle_state = LifecycleState(raw_lifecycle_state)
            snapshot = PrivateWebStateSnapshot(
                state=PrivateWebEditorState.READY,
                ad_id=ad_id,
                lifecycle_state=lifecycle_state,
            )
        except (TypeError, ValueError):
            return PrivateWebStateSnapshot(state=PrivateWebEditorState.UNKNOWN)
        if ad_id == target_ad_id:
            self._last_state_snapshot = snapshot
        return snapshot

    def _wait_for_state_settlement(
        self,
        client: _CdpClient,
        *,
        target_ad_id: str,
        target_state: LifecycleState,
    ) -> None:
        deadline = self._monotonic() + self._timeout_seconds
        while True:
            try:
                value = self._runtime_value(
                    client,
                    self._state_control_expression(target_ad_id),
                )
            except Exception:  # noqa: BLE001 - submit may already have succeeded.
                value = None
            if (
                isinstance(value, dict)
                and value.get("state") == PrivateWebEditorState.READY.value
                and value.get("ad_id") == target_ad_id
                and value.get("lifecycle_state") == target_state.value
            ):
                return
            if self._monotonic() >= deadline:
                raise PrivateWebSubmitUnknownError(
                    "state_submit_settle"
                )
            try:
                self._sleep(0.05)
            except Exception:  # noqa: BLE001 - submit may already have succeeded.
                raise PrivateWebSubmitUnknownError(
                    "state_submit_settle"
                ) from None

    def submit_state(self, state: LifecycleState) -> None:
        if not isinstance(state, LifecycleState):
            raise TypeError("state must be LifecycleState")
        if state not in {LifecycleState.ACTIVE, LifecycleState.PAUSED}:
            raise ValueError("state must be ACTIVE or PAUSED")
        if self._state_submit_attempted:
            raise PrivateWebCdpWriteNotAttemptedError(
                "state_submit_already_attempted"
            )
        self._state_submit_attempted = True

        target_ad_id = self._bound_state_ad_id
        snapshot = self._last_state_snapshot
        self._last_state_snapshot = None
        expected_pre_state = (
            LifecycleState.PAUSED
            if state is LifecycleState.ACTIVE
            else LifecycleState.ACTIVE
        )
        if (
            target_ad_id is None
            or snapshot is None
            or snapshot.state is not PrivateWebEditorState.READY
            or snapshot.ad_id != target_ad_id
            or snapshot.lifecycle_state is not expected_pre_state
        ):
            raise PrivateWebCdpWriteNotAttemptedError("state_submit")

        expected_label = (
            "aktivieren"
            if state is LifecycleState.ACTIVE
            else "reservieren"
        )
        try:
            client = self._client()
            activation = self._runtime_value(
                client,
                self._state_control_expression(
                    target_ad_id,
                    activation_label=expected_label,
                ),
            )
        except Exception:  # noqa: BLE001 - no browser input was attempted.
            raise PrivateWebCdpWriteNotAttemptedError(
                "state_submit"
            ) from None
        if not isinstance(activation, dict):
            raise PrivateWebCdpWriteNotAttemptedError("state_submit")
        raw_x = activation.get("x")
        raw_y = activation.get("y")
        if (
            isinstance(raw_x, bool)
            or not isinstance(raw_x, (int, float))
            or not math.isfinite(float(raw_x))
            or float(raw_x) < 0
            or isinstance(raw_y, bool)
            or not isinstance(raw_y, (int, float))
            or not math.isfinite(float(raw_y))
            or float(raw_y) < 0
        ):
            raise PrivateWebCdpWriteNotAttemptedError("state_submit")
        x = float(raw_x)
        y = float(raw_y)

        try:
            client.call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    "x": x,
                    "y": y,
                    "button": "left",
                    "buttons": 1,
                    "clickCount": 1,
                },
            )
            client.call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "x": x,
                    "y": y,
                    "button": "left",
                    "buttons": 0,
                    "clickCount": 1,
                },
            )
        except Exception:  # noqa: BLE001 - possible submit is never retried.
            raise PrivateWebSubmitUnknownError("state_submit") from None

        self._wait_for_state_settlement(
            client,
            target_ad_id=target_ad_id,
            target_state=state,
        )


    def _delete_control_expression(
        self,
        target_ad_id: str,
        *,
        activation: bool = False,
        include_pagination: bool = False,
    ) -> str:
        origin = json.dumps(self._expected_origin)
        management_path = json.dumps(_MANAGEMENT_PATH)
        editor_path = json.dumps(_EDITOR_PATH)
        ad_id = json.dumps(target_ad_id)
        activate = json.dumps(activation)
        allow_pagination = json.dumps(include_pagination)
        return f"""
(() => {{
  const stateOnly = (state) => ({{state}});
  const currentOrigin = location.origin;
  const currentPath = location.pathname;
  const targetAdId = {ad_id};
  const editorPath = {editor_path};
  const activate = {activate};
  const allowPagination = {allow_pagination};
  const challengeText = Array.from(
    document.querySelectorAll(
      '[role="dialog"], [role="alert"], [aria-modal="true"], [id*="challenge" i], [class*="challenge" i]'
    )
  )
    .map((element) => (element.innerText || "").toLowerCase())
    .join("\\n");
  const hasCaptcha = Boolean(
    document.querySelector(
      'iframe[src*="captcha" i], [data-sitekey], [id*="captcha" i], [class*="captcha" i]'
    )
  ) ||
    challengeText.includes("captcha") ||
    challengeText.includes("ich bin kein roboter");
  const hasMfa = Boolean(
    document.querySelector(
      'input[autocomplete="one-time-code"], input[name*="otp" i], input[name*="mfa" i]'
    )
  ) ||
    challengeText.includes("bestätigungscode") ||
    challengeText.includes("sicherheitscode");
  const hasSecurityChallenge =
    challengeText.includes("sicherheitsprüfung") ||
    challengeText.includes("sicherheitscheck") ||
    challengeText.includes("ungewöhnliche aktivität") ||
    challengeText.includes("bestätige, dass du ein mensch bist");
  const hasLogin =
    currentPath.startsWith("/u/login/") ||
    Boolean(document.querySelector('input[type="password"]'));

  if (currentOrigin !== {origin}) return stateOnly("unknown");
  if (hasCaptcha) return stateOnly("captcha_required");
  if (hasMfa) return stateOnly("mfa_required");
  if (hasSecurityChallenge) return stateOnly("security_challenge");
  if (hasLogin) return stateOnly("login_required");
  if (currentPath !== {management_path}) return stateOnly("unknown");
  if (document.querySelector("#delete-container")) return stateOnly("unknown");

  const editorId = (link) => {{
    try {{
      const raw = link.getAttribute("href");
      if (!raw) return null;
      const url = new URL(raw, location.href);
      if (url.origin !== {origin} || url.pathname !== editorPath) return null;
      const value = url.searchParams.get("adId");
      if (
        typeof value !== "string" ||
        value.length === 0 ||
        value.length > 32 ||
        !/^[0-9]+$/.test(value)
      ) {{
        return null;
      }}
      return value;
    }} catch (_error) {{
      return null;
    }}
  }};
  const label = (element) => (element.innerText || "").trim().toLowerCase();
  const isEligibleControl = (element) => {{
    if (
      (element instanceof HTMLButtonElement && element.disabled) ||
      element.getAttribute("aria-disabled") === "true"
    ) {{
      return false;
    }}
    const style = getComputedStyle(element);
    if (
      style.display === "none" ||
      style.visibility === "hidden" ||
      style.visibility === "collapse" ||
      style.pointerEvents === "none" ||
      Number(style.opacity) === 0
    ) {{
      return false;
    }}
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }};
  const deleteControls = (root) =>
    Array.from(root.querySelectorAll('button, [role="button"]')).filter(
      (element) => label(element) === "löschen" && isEligibleControl(element)
    );
  const targetEditLinks = Array.from(
    document.querySelectorAll("a[href]")
  ).filter((link) => editorId(link) === targetAdId);
  if (targetEditLinks.length > 1) return stateOnly("unknown");

  let bound = null;
  if (targetEditLinks.length === 1) {{
    let node = targetEditLinks[0].parentElement;
    for (let depth = 0; node && depth < 10; depth += 1) {{
      const editIds = Array.from(node.querySelectorAll("a[href]"))
        .map(editorId)
        .filter((value) => value !== null);
      const controls = deleteControls(node);
      if (
        editIds.length === 1 &&
        editIds[0] === targetAdId &&
        controls.length === 1
      ) {{
        bound = {{control: controls[0]}};
        break;
      }}
      node = node.parentElement;
    }}
    if (bound === null) return stateOnly("unknown");
  }} else {{
    if (!allowPagination) return stateOnly("unknown");
    const pageAdIds = Array.from(document.querySelectorAll("a[href]"))
      .map(editorId)
      .filter((value) => value !== null);
    const uniquePageAdIds = Array.from(new Set(pageAdIds));
    if (uniquePageAdIds.length === 0) return stateOnly("unknown");

    const nextCandidates = Array.from(
      document.querySelectorAll('button[aria-label="Nächste"]')
    ).filter((element) => isEligibleControl(element));
    if (nextCandidates.length > 1) return stateOnly("unknown");
    if (nextCandidates.length === 0) {{
      return {{
        state: "target_absent",
        page_ad_ids: uniquePageAdIds,
        next_page: null,
      }};
    }}
    const nextControl = nextCandidates[0];
    nextControl.scrollIntoView({{block: "center", inline: "center"}});
    const nextRect = nextControl.getBoundingClientRect();
    if (nextRect.width <= 0 || nextRect.height <= 0) return stateOnly("unknown");
    const nextX = nextRect.left + nextRect.width / 2;
    const nextY = nextRect.top + nextRect.height / 2;
    const nextHit = document.elementFromPoint(nextX, nextY);
    if (nextHit !== nextControl && !nextControl.contains(nextHit)) {{
      return stateOnly("unknown");
    }}
    if (
      !Number.isFinite(nextX) ||
      !Number.isFinite(nextY) ||
      nextX < 0 ||
      nextY < 0 ||
      nextX > window.innerWidth ||
      nextY > window.innerHeight
    ) {{
      return stateOnly("unknown");
    }}
    return {{
      state: "target_absent",
      page_ad_ids: uniquePageAdIds,
      next_page: {{x: nextX, y: nextY}},
    }};
  }}

  if (!activate) return {{state: "ready", ad_id: targetAdId}};

  const control = bound.control;
  control.scrollIntoView({{block: "center", inline: "center"}});
  const rect = control.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;
  const hit = document.elementFromPoint(x, y);
  if (hit !== control && !control.contains(hit)) return null;
  if (
    !Number.isFinite(x) ||
    !Number.isFinite(y) ||
    x < 0 ||
    y < 0 ||
    x > window.innerWidth ||
    y > window.innerHeight
  ) {{
    return null;
  }}
  return {{x, y}};
}})()
"""

    @staticmethod
    def _validated_delete_page_probe(
        value: object,
        *,
        target_ad_id: str,
    ) -> tuple[str, tuple[str, ...] | None, tuple[float, float] | None]:
        if not isinstance(value, dict):
            return "unknown", None, None
        raw_state = value.get("state")
        if raw_state == PrivateWebEditorState.READY.value:
            if value.get("ad_id") == target_ad_id:
                return "ready", None, None
            return "unknown", None, None
        if raw_state in {
            PrivateWebEditorState.LOGIN_REQUIRED.value,
            PrivateWebEditorState.MFA_REQUIRED.value,
            PrivateWebEditorState.CAPTCHA_REQUIRED.value,
            PrivateWebEditorState.SECURITY_CHALLENGE.value,
        }:
            return str(raw_state), None, None
        if raw_state != "target_absent":
            return "unknown", None, None
        raw_page_ad_ids = value.get("page_ad_ids")
        if (
            not isinstance(raw_page_ad_ids, list)
            or not raw_page_ad_ids
            or len(raw_page_ad_ids) > 1000
        ):
            return "unknown", None, None
        page_ad_ids: list[str] = []
        for raw_ad_id in raw_page_ad_ids:
            if not isinstance(raw_ad_id, str):
                return "unknown", None, None
            try:
                page_ad_ids.append(_validated_ad_id(raw_ad_id))
            except (TypeError, ValueError):
                return "unknown", None, None
        if len(set(page_ad_ids)) != len(page_ad_ids):
            return "unknown", None, None
        raw_next_page = value.get("next_page")
        if raw_next_page is None:
            return "target_absent", tuple(page_ad_ids), None
        if not isinstance(raw_next_page, dict):
            return "unknown", None, None
        raw_x = raw_next_page.get("x")
        raw_y = raw_next_page.get("y")
        if (
            isinstance(raw_x, bool)
            or not isinstance(raw_x, (int, float))
            or not math.isfinite(float(raw_x))
            or float(raw_x) < 0
            or isinstance(raw_y, bool)
            or not isinstance(raw_y, (int, float))
            or not math.isfinite(float(raw_y))
            or float(raw_y) < 0
        ):
            return "unknown", None, None
        return (
            "target_absent",
            tuple(page_ad_ids),
            (float(raw_x), float(raw_y)),
        )

    def open_delete_controls(self, ad_id: str) -> None:
        target_ad_id = _validated_ad_id(ad_id)
        target_url = f"{self._expected_origin}{_MANAGEMENT_PATH}"
        self._delete_confirmation_attempted = False
        self._delete_submit_attempted = False
        self._bound_delete_ad_id = None
        self._last_delete_snapshot = None
        self._submit_attempted = False
        self._bound_ad_id = None
        self._last_ready_snapshot = None
        self._state_submit_attempted = False
        self._bound_state_ad_id = None
        self._last_state_snapshot = None
        client = self._client()
        marker = json.dumps(
            f"__mark_private_web_delete_navigation_probe_{target_ad_id}__"
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
                raise PrivateWebCdpError("navigate_delete")
            navigation = client.call("Page.navigate", {"url": target_url})
            if navigation.get("errorText"):
                raise PrivateWebCdpError("navigate_delete")
            initial_deadline = self._monotonic() + self._timeout_seconds
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
                    readiness = self._runtime_value(client, readiness_expression)
                except PrivateWebCdpError:
                    readiness = None
                if (
                    isinstance(readiness, dict)
                    and readiness.get("readyState") == "complete"
                    and readiness.get("oldDocument") is False
                ):
                    break
                if self._monotonic() >= initial_deadline:
                    raise PrivateWebCdpError("navigate_delete")
                self._sleep(0.05)

            self._bound_delete_ad_id = target_ad_id
            prior_page_fingerprint: tuple[str, ...] | None = None
            seen_page_fingerprints: set[tuple[str, ...]] = set()
            for _page_number in range(1, _MANAGEMENT_MAX_PAGES + 1):
                page_deadline = self._monotonic() + self._timeout_seconds
                while True:
                    try:
                        probe_value = self._runtime_value(
                            client,
                            self._delete_control_expression(
                                target_ad_id,
                                include_pagination=True,
                            ),
                        )
                    except PrivateWebCdpError:
                        probe_value = None
                    probe_state, page_fingerprint, next_point = (
                        self._validated_delete_page_probe(
                            probe_value,
                            target_ad_id=target_ad_id,
                        )
                    )
                    if probe_state == "ready":
                        self._last_delete_snapshot = None
                        return
                    if probe_state in {
                        PrivateWebEditorState.LOGIN_REQUIRED.value,
                        PrivateWebEditorState.MFA_REQUIRED.value,
                        PrivateWebEditorState.CAPTCHA_REQUIRED.value,
                        PrivateWebEditorState.SECURITY_CHALLENGE.value,
                    }:
                        self._last_delete_snapshot = None
                        return
                    if (
                        probe_state == "target_absent"
                        and page_fingerprint is not None
                        and page_fingerprint != prior_page_fingerprint
                        and next_point is not None
                    ):
                        break
                    if self._monotonic() >= page_deadline:
                        raise PrivateWebCdpError("navigate_delete")
                    self._sleep(0.05)

                if page_fingerprint in seen_page_fingerprints:
                    raise PrivateWebCdpError("navigate_delete")
                seen_page_fingerprints.add(page_fingerprint)
                if next_point is None:
                    raise PrivateWebCdpError("navigate_delete")
                if len(seen_page_fingerprints) >= _MANAGEMENT_MAX_PAGES:
                    raise PrivateWebCdpError("navigate_delete")
                try:
                    self._dispatch_browser_click(
                        client,
                        x=next_point[0],
                        y=next_point[1],
                    )
                except Exception:  # noqa: BLE001 - pagination is pre-submit.
                    raise PrivateWebCdpError("navigate_delete") from None
                prior_page_fingerprint = page_fingerprint
            raise PrivateWebCdpError("navigate_delete")
        except PrivateWebCdpError:
            self._bound_delete_ad_id = None
            self._last_delete_snapshot = None
            raise
        except Exception:  # noqa: BLE001 - sanitize runtime boundary.
            self._bound_delete_ad_id = None
            self._last_delete_snapshot = None
            raise PrivateWebCdpError("navigate_delete") from None

    def read_delete_controls(self) -> PrivateWebDeleteSnapshot:
        target_ad_id = self._bound_delete_ad_id
        self._last_delete_snapshot = None
        if target_ad_id is None:
            raise PrivateWebCdpError("read_delete_controls")
        value = self._evaluate(
            "read_delete_controls",
            self._delete_control_expression(target_ad_id),
        )
        if not isinstance(value, dict):
            return PrivateWebDeleteSnapshot(state=PrivateWebEditorState.UNKNOWN)
        raw_state = value.get("state")
        try:
            state = PrivateWebEditorState(raw_state)
        except (TypeError, ValueError):
            return PrivateWebDeleteSnapshot(state=PrivateWebEditorState.UNKNOWN)
        if state is not PrivateWebEditorState.READY:
            return PrivateWebDeleteSnapshot(state=state)
        ad_id = value.get("ad_id")
        if not isinstance(ad_id, str):
            return PrivateWebDeleteSnapshot(state=PrivateWebEditorState.UNKNOWN)
        try:
            snapshot = PrivateWebDeleteSnapshot(
                state=PrivateWebEditorState.READY,
                ad_id=ad_id,
            )
        except (TypeError, ValueError):
            return PrivateWebDeleteSnapshot(state=PrivateWebEditorState.UNKNOWN)
        if ad_id == target_ad_id:
            self._last_delete_snapshot = snapshot
        return snapshot

    def _delete_confirmation_expression(
        self,
        target_ad_id: str,
        *,
        activation: bool = False,
    ) -> str:
        origin = json.dumps(self._expected_origin)
        management_path = json.dumps(_MANAGEMENT_PATH)
        ad_id = json.dumps(target_ad_id)
        activate = json.dumps(activation)
        return f"""
(() => {{
  const stateOnly = (state) => ({{state}});
  const targetAdId = {ad_id};
  const activate = {activate};
  if (location.origin !== {origin}) return stateOnly("unknown");
  const currentPath = location.pathname;
  if (currentPath !== {management_path}) return stateOnly("unknown");
  const challengeText = Array.from(
    document.querySelectorAll(
      '[role="dialog"], [role="alert"], [aria-modal="true"], [id*="challenge" i], [class*="challenge" i]'
    )
  )
    .map((element) => (element.innerText || "").toLowerCase())
    .join("\\n");
  const hasCaptcha = Boolean(
    document.querySelector(
      'iframe[src*="captcha" i], [data-sitekey], [id*="captcha" i], [class*="captcha" i]'
    )
  ) ||
    challengeText.includes("captcha") ||
    challengeText.includes("ich bin kein roboter");
  const hasMfa = Boolean(
    document.querySelector(
      'input[autocomplete="one-time-code"], input[name*="otp" i], input[name*="mfa" i]'
    )
  ) ||
    challengeText.includes("bestätigungscode") ||
    challengeText.includes("sicherheitscode");
  const hasSecurityChallenge =
    challengeText.includes("sicherheitsprüfung") ||
    challengeText.includes("sicherheitscheck") ||
    challengeText.includes("ungewöhnliche aktivität") ||
    challengeText.includes("bestätige, dass du ein mensch bist");
  const hasLogin =
    currentPath.startsWith("/u/login/") ||
    Boolean(document.querySelector('input[type="password"]'));
  if (hasCaptcha) return stateOnly("captcha_required");
  if (hasMfa) return stateOnly("mfa_required");
  if (hasSecurityChallenge) return stateOnly("security_challenge");
  if (hasLogin) return stateOnly("login_required");

  const containers = Array.from(document.querySelectorAll("#delete-container"));
  if (containers.length !== 1) return stateOnly("unknown");
  const container = containers[0];
  const normalizedText = (container.innerText || "")
    .replace(/\\s+/g, " ")
    .trim()
    .toLowerCase();
  if (
    !normalizedText.includes("anzeige löschen") ||
    !normalizedText.includes(
      "bist du sicher, dass du die anzeige löschen möchtest?"
    )
  ) {{
    return stateOnly("unknown");
  }}
  const isEligibleControl = (element) => {{
    if (
      (element instanceof HTMLButtonElement && element.disabled) ||
      element.getAttribute("aria-disabled") === "true"
    ) {{
      return false;
    }}
    const style = getComputedStyle(element);
    if (
      style.display === "none" ||
      style.visibility === "hidden" ||
      style.visibility === "collapse" ||
      style.pointerEvents === "none" ||
      Number(style.opacity) === 0
    ) {{
      return false;
    }}
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }};
  const label = (element) => (element.innerText || "").trim().toLowerCase();
  const confirms = Array.from(
    container.querySelectorAll(
      'button#delete-celebration-sbmt, [role="button"]#delete-celebration-sbmt'
    )
  ).filter(
    (element) =>
      label(element) === "ja, anzeige löschen" &&
      isEligibleControl(element)
  );
  const cancels = Array.from(
    container.querySelectorAll('button, [role="button"]')
  ).filter(
    (element) => label(element) === "abbrechen" && isEligibleControl(element)
  );
  if (confirms.length !== 1 || cancels.length !== 1) {{
    return stateOnly("unknown");
  }}
  if (!activate) return {{state: "ready", ad_id: targetAdId}};

  const control = confirms[0];
  control.scrollIntoView({{block: "center", inline: "center"}});
  const rect = control.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;
  const hit = document.elementFromPoint(x, y);
  if (hit !== control && !control.contains(hit)) return null;
  if (
    !Number.isFinite(x) ||
    !Number.isFinite(y) ||
    x < 0 ||
    y < 0 ||
    x > window.innerWidth ||
    y > window.innerHeight
  ) {{
    return null;
  }}
  return {{x, y}};
}})()
"""

    def open_delete_confirmation(self) -> None:
        if self._delete_confirmation_attempted:
            raise PrivateWebCdpWriteNotAttemptedError(
                "delete_confirmation_already_attempted"
            )
        target_ad_id = self._bound_delete_ad_id
        snapshot = self._last_delete_snapshot
        self._last_delete_snapshot = None
        if (
            target_ad_id is None
            or snapshot is None
            or snapshot.state is not PrivateWebEditorState.READY
            or snapshot.ad_id != target_ad_id
        ):
            raise PrivateWebCdpWriteNotAttemptedError(
                "delete_open_confirmation"
            )
        self._delete_confirmation_attempted = True
        try:
            client = self._client()
            activation = self._runtime_value(
                client,
                self._delete_control_expression(
                    target_ad_id,
                    activation=True,
                ),
            )
        except Exception:
            raise PrivateWebCdpWriteNotAttemptedError(
                "delete_open_confirmation"
            ) from None
        if not isinstance(activation, dict):
            raise PrivateWebCdpWriteNotAttemptedError(
                "delete_open_confirmation"
            )
        raw_x = activation.get("x")
        raw_y = activation.get("y")
        if (
            isinstance(raw_x, bool)
            or not isinstance(raw_x, (int, float))
            or not math.isfinite(float(raw_x))
            or float(raw_x) < 0
            or isinstance(raw_y, bool)
            or not isinstance(raw_y, (int, float))
            or not math.isfinite(float(raw_y))
            or float(raw_y) < 0
        ):
            raise PrivateWebCdpWriteNotAttemptedError(
                "delete_open_confirmation"
            )
        try:
            self._dispatch_browser_click(
                client,
                x=float(raw_x),
                y=float(raw_y),
            )
        except Exception:
            # Browser input may already have taken effect. Even though the
            # currently evidenced UI only opens a confirmation modal here,
            # provider/UI drift must never be classified as safely unattempted.
            raise PrivateWebSubmitUnknownError(
                "delete_open_confirmation"
            ) from None

        deadline = self._monotonic() + self._timeout_seconds
        while True:
            try:
                value = self._runtime_value(
                    client,
                    self._delete_confirmation_expression(target_ad_id),
                )
            except Exception:
                value = None
            if (
                isinstance(value, dict)
                and value.get("state") == PrivateWebEditorState.READY.value
                and value.get("ad_id") == target_ad_id
            ):
                return
            if self._monotonic() >= deadline:
                raise PrivateWebSubmitUnknownError(
                    "delete_open_confirmation_settle"
                )
            try:
                self._sleep(0.05)
            except Exception:
                raise PrivateWebSubmitUnknownError(
                    "delete_open_confirmation_settle"
                ) from None

    def read_delete_confirmation(self) -> PrivateWebDeleteSnapshot:
        target_ad_id = self._bound_delete_ad_id
        self._last_delete_snapshot = None
        if target_ad_id is None or not self._delete_confirmation_attempted:
            raise PrivateWebCdpError("read_delete_confirmation")
        value = self._evaluate(
            "read_delete_confirmation",
            self._delete_confirmation_expression(target_ad_id),
        )
        if not isinstance(value, dict):
            return PrivateWebDeleteSnapshot(state=PrivateWebEditorState.UNKNOWN)
        raw_state = value.get("state")
        try:
            state = PrivateWebEditorState(raw_state)
        except (TypeError, ValueError):
            return PrivateWebDeleteSnapshot(state=PrivateWebEditorState.UNKNOWN)
        if state is not PrivateWebEditorState.READY:
            return PrivateWebDeleteSnapshot(state=state)
        if value.get("ad_id") != target_ad_id:
            return PrivateWebDeleteSnapshot(state=PrivateWebEditorState.UNKNOWN)
        snapshot = PrivateWebDeleteSnapshot(
            state=PrivateWebEditorState.READY,
            ad_id=target_ad_id,
        )
        self._last_delete_snapshot = snapshot
        return snapshot

    def _delete_settled_expression(self) -> str:
        origin = json.dumps(self._expected_origin)
        return f"""
(() => {{
  if (location.origin !== {origin}) return false;
  if (document.querySelector("#celebration-container")) return true;
  return (
    document.readyState === "complete" &&
    document.querySelector("#delete-container") === null &&
    document.querySelector("#delete-celebration-sbmt") === null
  );
}})()
"""

    def _wait_for_delete_settlement(self, client: _CdpClient) -> None:
        deadline = self._monotonic() + self._timeout_seconds
        expression = self._delete_settled_expression()
        while True:
            try:
                settled = self._runtime_value(client, expression)
            except Exception:
                settled = False
            if settled is True:
                return
            if self._monotonic() >= deadline:
                raise PrivateWebSubmitUnknownError(
                    "delete_submit_settle"
                )
            try:
                self._sleep(0.05)
            except Exception:
                raise PrivateWebSubmitUnknownError(
                    "delete_submit_settle"
                ) from None

    def submit_delete(self) -> None:
        if self._delete_submit_attempted:
            raise PrivateWebCdpWriteNotAttemptedError(
                "delete_submit_already_attempted"
            )
        self._delete_submit_attempted = True
        target_ad_id = self._bound_delete_ad_id
        snapshot = self._last_delete_snapshot
        self._last_delete_snapshot = None
        if (
            target_ad_id is None
            or not self._delete_confirmation_attempted
            or snapshot is None
            or snapshot.state is not PrivateWebEditorState.READY
            or snapshot.ad_id != target_ad_id
        ):
            raise PrivateWebCdpWriteNotAttemptedError("delete_submit")
        try:
            client = self._client()
            activation = self._runtime_value(
                client,
                self._delete_confirmation_expression(
                    target_ad_id,
                    activation=True,
                ),
            )
        except Exception:
            raise PrivateWebCdpWriteNotAttemptedError(
                "delete_submit"
            ) from None
        if not isinstance(activation, dict):
            raise PrivateWebCdpWriteNotAttemptedError("delete_submit")
        raw_x = activation.get("x")
        raw_y = activation.get("y")
        if (
            isinstance(raw_x, bool)
            or not isinstance(raw_x, (int, float))
            or not math.isfinite(float(raw_x))
            or float(raw_x) < 0
            or isinstance(raw_y, bool)
            or not isinstance(raw_y, (int, float))
            or not math.isfinite(float(raw_y))
            or float(raw_y) < 0
        ):
            raise PrivateWebCdpWriteNotAttemptedError("delete_submit")
        try:
            self._dispatch_browser_click(
                client,
                x=float(raw_x),
                y=float(raw_y),
            )
        except Exception:
            raise PrivateWebSubmitUnknownError("delete_submit") from None
        self._wait_for_delete_settlement(client)


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
        target_ad_id = self._bound_ad_id
        snapshot = self._last_ready_snapshot
        if (
            target_ad_id is None
            or snapshot is None
            or snapshot.state is not PrivateWebEditorState.READY
            or snapshot.ad_id != target_ad_id
            or snapshot.title is None
            or snapshot.description is None
        ):
            raise PrivateWebCdpError(stage)

        origin = json.dumps(self._expected_origin)
        path = json.dumps(_EDITOR_PATH)
        ad_id = json.dumps(target_ad_id)
        expected_title = json.dumps(snapshot.title)
        expected_description = json.dumps(snapshot.description)
        replacement = json.dumps(value)
        expression = f"""
(() => {{
  const currentOrigin = location.origin;
  const currentPath = location.pathname;
  const currentAdId = new URL(location.href).searchParams.get("adId");
  const challengeText = Array.from(
    document.querySelectorAll(
      '[role="dialog"], [role="alert"], [aria-modal="true"], [id*="challenge" i], [class*="challenge" i]'
    )
  )
    .map((candidate) => (candidate.innerText || "").toLowerCase())
    .join("\\n");
  const hasCaptcha = Boolean(
    document.querySelector(
      'iframe[src*="captcha" i], [data-sitekey], [id*="captcha" i], [class*="captcha" i]'
    )
  ) ||
    challengeText.includes("captcha") ||
    challengeText.includes("ich bin kein roboter");
  const hasMfa = Boolean(
    document.querySelector(
      'input[autocomplete="one-time-code"], input[name*="otp" i], input[name*="mfa" i]'
    )
  ) ||
    challengeText.includes("bestätigungscode") ||
    challengeText.includes("sicherheitscode");
  const hasSecurityChallenge =
    challengeText.includes("sicherheitsprüfung") ||
    challengeText.includes("sicherheitscheck") ||
    challengeText.includes("ungewöhnliche aktivität") ||
    challengeText.includes("bestätige, dass du ein mensch bist");
  const title = document.querySelector("#ad-title");
  const description = document.querySelector("#ad-description");
  const saveButtons = Array.from(document.querySelectorAll("button")).filter(
    (button) => (button.innerText || "").trim() === "Anzeige speichern"
  );
  const element = document.querySelector({json.dumps(selector)});
  if (
    currentOrigin !== {origin} ||
    currentPath !== {path} ||
    currentAdId !== {ad_id} ||
    hasCaptcha ||
    hasMfa ||
    hasSecurityChallenge ||
    document.querySelector('input[type="password"]') ||
    !(title instanceof HTMLInputElement) ||
    title.getAttribute("name") !== "title" ||
    !(description instanceof HTMLTextAreaElement) ||
    description.getAttribute("name") !== "description" ||
    title.value !== {expected_title} ||
    description.value !== {expected_description} ||
    saveButtons.length !== 1 ||
    saveButtons[0].getAttribute("type") !== "button" ||
    saveButtons[0].disabled ||
    !(element instanceof {prototype}) ||
    element.getAttribute("name") !== {json.dumps(expected_name)}
  ) {{
    return false;
  }}
  const descriptor = Object.getOwnPropertyDescriptor(
    {prototype}.prototype,
    "value"
  );
  if (!descriptor || typeof descriptor.set !== "function") return false;
  element.focus();
  if (document.activeElement !== element) return false;
  descriptor.set.call(element, {replacement});
  element.dispatchEvent(
    new InputEvent(
      "input",
      {{bubbles: true, inputType: "insertText", data: {replacement}}}
    )
  );
  element.dispatchEvent(new Event("change", {{bubbles: true}}));
  return element.value === {replacement};
}})()
"""
        client = self._client()
        self._last_ready_snapshot = None
        try:
            if self._runtime_value(client, expression) is not True:
                raise PrivateWebCdpError(stage)
            if expected_name == "title":
                self._last_ready_snapshot = PrivateWebEditorSnapshot(
                    state=PrivateWebEditorState.READY,
                    ad_id=target_ad_id,
                    title=value,
                    description=snapshot.description,
                )
            else:
                self._last_ready_snapshot = PrivateWebEditorSnapshot(
                    state=PrivateWebEditorState.READY,
                    ad_id=target_ad_id,
                    title=snapshot.title,
                    description=value,
                )
        except PrivateWebCdpError:
            raise PrivateWebCdpError(stage) from None
        except Exception:  # noqa: BLE001 - sanitize browser/provider boundary.
            raise PrivateWebCdpError(stage) from None

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
        target_ad_id = self._bound_ad_id
        snapshot = self._last_ready_snapshot
        self._last_ready_snapshot = None
        if (
            target_ad_id is None
            or snapshot is None
            or snapshot.state is not PrivateWebEditorState.READY
            or snapshot.ad_id != target_ad_id
            or snapshot.title is None
            or snapshot.description is None
        ):
            raise PrivateWebCdpError("submit")

        origin = json.dumps(self._expected_origin)
        path = json.dumps(_EDITOR_PATH)
        ad_id = json.dumps(target_ad_id)
        expected_title = json.dumps(snapshot.title)
        expected_description = json.dumps(snapshot.description)
        expression = f"""
(() => {{
  const currentOrigin = location.origin;
  const currentPath = location.pathname;
  const currentAdId = new URL(location.href).searchParams.get("adId");
  const challengeText = Array.from(
    document.querySelectorAll(
      '[role="dialog"], [role="alert"], [aria-modal="true"], [id*="challenge" i], [class*="challenge" i]'
    )
  )
    .map((candidate) => (candidate.innerText || "").toLowerCase())
    .join("\\n");
  const hasCaptcha = Boolean(
    document.querySelector(
      'iframe[src*="captcha" i], [data-sitekey], [id*="captcha" i], [class*="captcha" i]'
    )
  ) ||
    challengeText.includes("captcha") ||
    challengeText.includes("ich bin kein roboter");
  const hasMfa = Boolean(
    document.querySelector(
      'input[autocomplete="one-time-code"], input[name*="otp" i], input[name*="mfa" i]'
    )
  ) ||
    challengeText.includes("bestätigungscode") ||
    challengeText.includes("sicherheitscode");
  const hasSecurityChallenge =
    challengeText.includes("sicherheitsprüfung") ||
    challengeText.includes("sicherheitscheck") ||
    challengeText.includes("ungewöhnliche aktivität") ||
    challengeText.includes("bestätige, dass du ein mensch bist");
  const title = document.querySelector("#ad-title");
  const description = document.querySelector("#ad-description");
  const buttons = Array.from(document.querySelectorAll("button")).filter(
    (button) => (button.innerText || "").trim() === "Anzeige speichern"
  );
  if (
    currentOrigin !== {origin} ||
    currentPath !== {path} ||
    currentAdId !== {ad_id} ||
    hasCaptcha ||
    hasMfa ||
    hasSecurityChallenge ||
    document.querySelector('input[type="password"]') ||
    !(title instanceof HTMLInputElement) ||
    title.getAttribute("name") !== "title" ||
    !(description instanceof HTMLTextAreaElement) ||
    description.getAttribute("name") !== "description" ||
    title.value !== {expected_title} ||
    description.value !== {expected_description} ||
    buttons.length !== 1
  ) {{
    return false;
  }}
  const button = buttons[0];
  if (button.getAttribute("type") !== "button" || button.disabled) return false;
  button.scrollIntoView({{block: "center", inline: "center"}});
  const rect = button.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return false;
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;
  const hit = document.elementFromPoint(x, y);
  if (hit !== button && !button.contains(hit)) return false;
  if (
    !Number.isFinite(x) ||
    !Number.isFinite(y) ||
    x < 0 ||
    y < 0 ||
    x > window.innerWidth ||
    y > window.innerHeight
  ) {{
    return false;
  }}
  return {{x, y}};
}})()
"""
        client = self._client()
        try:
            activation = self._runtime_value(client, expression)
            if not isinstance(activation, dict):
                raise PrivateWebCdpError("submit")
            raw_x = activation.get("x")
            raw_y = activation.get("y")
            if (
                isinstance(raw_x, bool)
                or not isinstance(raw_x, (int, float))
                or not math.isfinite(float(raw_x))
                or float(raw_x) < 0
                or isinstance(raw_y, bool)
                or not isinstance(raw_y, (int, float))
                or not math.isfinite(float(raw_y))
                or float(raw_y) < 0
            ):
                raise PrivateWebCdpError("submit")
            x = float(raw_x)
            y = float(raw_y)
            # Browser-level activation is necessarily coordinate-based. The exact
            # target/state/hit-test above minimizes, but cannot eliminate, the
            # narrow TOCTOU before Chrome consumes the next CDP command. Keep
            # that accepted residual window minimal and never retry a possible
            # activation.
            try:
                client.call(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mousePressed",
                        "x": x,
                        "y": y,
                        "button": "left",
                        "buttons": 1,
                        "clickCount": 1,
                    },
                )
                client.call(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseReleased",
                        "x": x,
                        "y": y,
                        "button": "left",
                        "buttons": 0,
                        "clickCount": 1,
                    },
                )
            except PrivateWebCdpError:
                # _LoopbackCdpClient.call sanitizes provider failures as
                # PrivateWebCdpError("call"). At this boundary the attempted
                # browser activation is semantically an ambiguous submit.
                raise PrivateWebCdpError("submit") from None

            confirmation_expression = f"""
(() => {{
  const currentOrigin = location.origin;
  const currentPath = location.pathname;
  const challengeText = Array.from(
    document.querySelectorAll(
      '[role="dialog"], [role="alert"], [aria-modal="true"], [id*="challenge" i], [class*="challenge" i]'
    )
  )
    .map((candidate) => (candidate.innerText || "").toLowerCase())
    .join("\\n");
  const hasCaptcha = Boolean(
    document.querySelector(
      'iframe[src*="captcha" i], [data-sitekey], [id*="captcha" i], [class*="captcha" i]'
    )
  ) ||
    challengeText.includes("captcha") ||
    challengeText.includes("ich bin kein roboter");
  const hasMfa = Boolean(
    document.querySelector(
      'input[autocomplete="one-time-code"], input[name*="otp" i], input[name*="mfa" i]'
    )
  ) ||
    challengeText.includes("bestätigungscode") ||
    challengeText.includes("sicherheitscode");
  const hasSecurityChallenge =
    challengeText.includes("sicherheitsprüfung") ||
    challengeText.includes("sicherheitscheck") ||
    challengeText.includes("ungewöhnliche aktivität") ||
    challengeText.includes("bestätige, dass du ein mensch bist");
  const hasLogin =
    currentPath.startsWith("/u/login/") ||
    Boolean(document.querySelector('input[type="password"]'));

  if (currentOrigin !== {origin}) return "unconfirmed";
  if (hasCaptcha || hasMfa || hasSecurityChallenge || hasLogin) {{
    return "unconfirmed";
  }}
  if (currentPath === {json.dumps(_POST_SUBMIT_PATH)}) return "confirmed";
  if (currentPath === {path}) return "pending";
  return "unconfirmed";
}})()
"""
            deadline = self._monotonic() + self._timeout_seconds
            while True:
                confirmation = self._runtime_value(
                    client,
                    confirmation_expression,
                )
                if confirmation == "confirmed":
                    return
                if confirmation != "pending":
                    raise PrivateWebCdpError("submit_unconfirmed")
                if self._monotonic() >= deadline:
                    raise PrivateWebCdpError("submit_unconfirmed")
                self._sleep(0.05)
        except PrivateWebCdpError as exc:
            raise PrivateWebCdpError(exc.stage) from None
        except Exception:  # noqa: BLE001 - ambiguous submit stays non-retryable.
            raise PrivateWebCdpError("submit") from None


class CdpPrivateWebOwnerReader:
    """Enrich one fresh owner-inventory target with a normal-Web editor read."""

    def __init__(
        self,
        *,
        owner_reader: AdsReader,
        page_factory: Callable[[], CdpPrivateWebPage],
        ad_id: str,
    ) -> None:
        self._owner_reader = owner_reader
        self._page_factory = page_factory
        self._ad_id = _validated_ad_id(ad_id)

    def read_ads(self) -> ReadResult[tuple[AdSnapshot, ...]]:
        owner_result = self._owner_reader.read_ads()
        if not owner_result.is_success:
            return owner_result

        snapshots = owner_result.value or ()
        if any(not isinstance(snapshot, AdSnapshot) for snapshot in snapshots):
            return ReadResult.failure(
                ReadStatus.PARSE_ERROR,
                error="invalid_owner_snapshot",
            )
        matching = [
            (index, snapshot)
            for index, snapshot in enumerate(snapshots)
            if snapshot.ad_id == self._ad_id
        ]
        if not matching:
            return owner_result
        if len(matching) != 1:
            return ReadResult.failure(
                ReadStatus.PARSE_ERROR,
                error="duplicate_owner_target",
            )

        page = None
        try:
            page = self._page_factory()
            page.open_editor(self._ad_id)
            editor = page.read_editor()
        except Exception:  # noqa: BLE001 - sanitize browser/provider boundary.
            if page is not None:
                try:
                    page.close()
                except Exception:  # noqa: BLE001 - preserve sanitized boundary.
                    pass
            return ReadResult.failure(
                ReadStatus.TRANSPORT_ERROR,
                error="private_web_owner_read_failed",
            )

        try:
            page.close()
        except Exception:  # noqa: BLE001 - sanitize browser/provider boundary.
            return ReadResult.failure(
                ReadStatus.TRANSPORT_ERROR,
                error="private_web_owner_read_failed",
            )

        if (
            not isinstance(editor, PrivateWebEditorSnapshot)
            or editor.state is not PrivateWebEditorState.READY
            or editor.ad_id != self._ad_id
            or editor.title is None
            or editor.description is None
        ):
            return ReadResult.failure(
                ReadStatus.TRANSPORT_ERROR,
                error="private_web_owner_read_unavailable",
            )

        index, target = matching[0]
        enriched = replace(
            target,
            title=editor.title,
            description=editor.description,
            source=f"{target.source}+private-web",
        )
        result = list(snapshots)
        result[index] = enriched
        return ReadResult.success_nonempty(tuple(result))