from __future__ import annotations

import json

from .ports import WriteNotAttemptedError
from .private_web import (
    PrivateWebCreateSnapshot,
    PrivateWebEditorState,
    PrivateWebSubmitUnknownError,
)
from .private_web_cdp import (
    CdpPrivateWebPage,
    PrivateWebCdpError,
    PrivateWebCdpWriteNotAttemptedError,
    _CREATE_FORM_PATH,
    _POST_SUBMIT_PATH,
)
from .private_web_media import (
    PrivateWebCreateMediaSnapshot,
    PrivateWebMediaFileSnapshot,
    PrivateWebMediaSource,
    PrivateWebMediaUnknownError,
    _PreparedPrivateWebMedia,
    _prepare_local_media,
)


class CdpPrivateWebMediaPage(CdpPrivateWebPage):
    """Narrow CDP extension for create-form file selection.

    The inherited create-form binding remains authoritative. Explicit sources
    are copied into page-owned private files before browser access. The exact
    validated file input is retained as one CDP object handle across the
    single DOM.setFileInputFiles call and the subsequent readback.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._media_selection_attempted = False
        self._media_expected_create_snapshot: PrivateWebCreateSnapshot | None = None
        self._media_file_object_id: str | None = None
        self._media_prepared: _PreparedPrivateWebMedia | None = None
        self._media_submit_unsettled = False

    def _release_media_handle(self) -> None:
        object_id = self._media_file_object_id
        self._media_file_object_id = None
        client = self._client_instance
        if object_id is None or client is None:
            return
        try:
            client.call("Runtime.releaseObject", {"objectId": object_id})
        except Exception:
            pass

    def _release_media_files(self) -> None:
        prepared = self._media_prepared
        self._media_prepared = None
        if prepared is not None:
            prepared.close()

    def open_create_form(self, category_path: tuple[str, ...]) -> None:
        if self._media_selection_attempted:
            raise PrivateWebCdpWriteNotAttemptedError(
                "create_media_already_attempted"
            )
        self._release_media_handle()
        self._release_media_files()
        self._media_expected_create_snapshot = None
        self._media_submit_unsettled = False
        super().open_create_form(category_path)

    def close(self) -> None:
        if self._media_submit_unsettled:
            # A one-shot publish may still be consuming the page-owned files.
            # Refuse destructive cleanup until observation-only reconciliation
            # reaches the same canonical post-submit state as the success path.
            raise PrivateWebSubmitUnknownError(
                "create_media_submit_unsettled"
            )
        self._release_media_handle()
        self._release_media_files()
        self._media_expected_create_snapshot = None
        super().close()

    @staticmethod
    def _prepare_media_paths(files: tuple[str, ...]) -> _PreparedPrivateWebMedia:
        if not isinstance(files, tuple):
            raise TypeError("media files must be a tuple")
        sources = tuple(PrivateWebMediaSource(path=path) for path in files)
        # Once browser file selection may be effective, GC must not silently
        # delete the private copies behind an unresolved one-shot submit.
        # Explicit settled close() remains the cleanup owner.
        return _prepare_local_media(sources, delete_on_gc=False)

    def _file_input_handle_expression(
        self,
        expected: PrivateWebCreateSnapshot,
    ) -> str:
        baseline = self._create_form_expression(expected=expected)
        return f"""
(() => {{
  const baseline = {baseline};
  if (!baseline || baseline.state !== "ready") return null;

  const title = document.querySelector("#ad-title");
  const files = Array.from(document.querySelectorAll('input[type="file"]'));
  if (
    !(title instanceof HTMLInputElement) ||
    files.length !== 1 ||
    !(files[0] instanceof HTMLInputElement) ||
    files[0].type !== "file" ||
    files[0].files === null ||
    files[0].files.length !== 0 ||
    files[0].form !== title.form
  ) return null;
  return files[0];
}})()
"""

    def stage_create_media(self, files: tuple[str, ...]) -> None:
        if self._media_selection_attempted:
            raise PrivateWebCdpWriteNotAttemptedError(
                "create_media_already_attempted"
            )

        # Direct CDP callers get the same stable-copy guarantee as the higher
        # level stager. These page-owned files remain until close().
        prepared = self._prepare_media_paths(files)

        snapshot = self._last_create_snapshot
        if (
            not self._create_bound
            or snapshot is None
            or snapshot.state is not PrivateWebEditorState.READY
        ):
            prepared.close()
            raise PrivateWebCdpWriteNotAttemptedError("create_media")

        try:
            try:
                current = self.read_create_form()
            except PrivateWebCdpWriteNotAttemptedError:
                raise
            except Exception:
                raise PrivateWebCdpWriteNotAttemptedError(
                    "create_media_revalidate"
                ) from None
            if current != snapshot:
                raise PrivateWebCdpWriteNotAttemptedError("create_media_drift")

            client = self._client()
            result = client.call(
                "Runtime.evaluate",
                {
                    "expression": self._file_input_handle_expression(current),
                    "returnByValue": False,
                    "awaitPromise": False,
                },
            )
            if "exceptionDetails" in result:
                raise PrivateWebCdpWriteNotAttemptedError(
                    "create_media_bind"
                )
            remote = result.get("result")
            if not isinstance(remote, dict):
                raise PrivateWebCdpWriteNotAttemptedError(
                    "create_media_bind"
                )
            object_id = remote.get("objectId")
            if not isinstance(object_id, str) or not object_id:
                raise PrivateWebCdpWriteNotAttemptedError(
                    "create_media_bind"
                )
        except PrivateWebCdpWriteNotAttemptedError:
            prepared.close()
            raise
        except PrivateWebCdpError:
            prepared.close()
            raise PrivateWebCdpWriteNotAttemptedError(
                "create_media_bind"
            ) from None
        except Exception:
            prepared.close()
            raise PrivateWebCdpWriteNotAttemptedError(
                "create_media_bind"
            ) from None

        # From this point on, file-input mutation may be effective. Mark the
        # attempt before dispatch and keep the private files alive until close.
        self._media_selection_attempted = True
        self._media_expected_create_snapshot = current
        self._media_file_object_id = object_id
        self._media_prepared = prepared
        self._last_create_snapshot = None
        try:
            client.call(
                "DOM.setFileInputFiles",
                {
                    "files": list(prepared.paths),
                    "objectId": object_id,
                },
            )
        except Exception:
            raise PrivateWebMediaUnknownError("stage_create_media") from None

    def _media_readback_function(
        self,
        expected: PrivateWebCreateSnapshot,
    ) -> str:
        # Reuse the canonical create-form contract, changing only the explicit
        # FileList requirement from empty to non-empty. The same CDP object
        # handle must still be the page's one and only file input.
        baseline = self._create_form_expression(
            expected=expected,
            allow_media_files=True,
        )
        return f"""
function() {{
  const stateOnly = (state) => ({{state}});
  const baseline = {baseline};
  if (!baseline || baseline.state !== "ready") {{
    return baseline || stateOnly("unknown");
  }}

  const input = this;
  const files = Array.from(document.querySelectorAll('input[type="file"]'));
  if (
    !(input instanceof HTMLInputElement) ||
    input.type !== "file" ||
    input.files === null ||
    input.files.length === 0 ||
    files.length !== 1 ||
    files[0] !== input ||
    !input.isConnected
  ) return stateOnly("unknown");

  return {{
    state: "ready",
    files: Array.from(input.files).map((file) => ({{
      name: file.name,
      size_bytes: file.size,
    }})),
  }};
}}
"""

    @staticmethod
    def _media_snapshot_from_value(
        value: object,
    ) -> PrivateWebCreateMediaSnapshot:
        if not isinstance(value, dict):
            return PrivateWebCreateMediaSnapshot(
                state=PrivateWebEditorState.UNKNOWN
            )
        try:
            state = PrivateWebEditorState(value.get("state"))
        except (TypeError, ValueError):
            return PrivateWebCreateMediaSnapshot(
                state=PrivateWebEditorState.UNKNOWN
            )
        if state is not PrivateWebEditorState.READY:
            return PrivateWebCreateMediaSnapshot(state=state)

        raw_files = value.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            return PrivateWebCreateMediaSnapshot(
                state=PrivateWebEditorState.UNKNOWN
            )

        files: list[PrivateWebMediaFileSnapshot] = []
        try:
            for item in raw_files:
                if not isinstance(item, dict):
                    raise ValueError
                files.append(
                    PrivateWebMediaFileSnapshot(
                        name=item.get("name"),
                        size_bytes=item.get("size_bytes"),
                    )
                )
            return PrivateWebCreateMediaSnapshot(
                state=PrivateWebEditorState.READY,
                files=tuple(files),
            )
        except (TypeError, ValueError):
            return PrivateWebCreateMediaSnapshot(
                state=PrivateWebEditorState.UNKNOWN
            )

    def read_create_media(self) -> PrivateWebCreateMediaSnapshot:
        expected = self._media_expected_create_snapshot
        object_id = self._media_file_object_id
        if (
            not self._create_bound
            or not self._media_selection_attempted
            or expected is None
            or object_id is None
            or self._media_prepared is None
        ):
            raise PrivateWebCdpWriteNotAttemptedError(
                "read_create_media"
            )

        try:
            client = self._client()
            result = client.call(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": self._media_readback_function(
                        expected
                    ),
                    "returnByValue": True,
                    "awaitPromise": False,
                },
            )
            if "exceptionDetails" in result:
                raise PrivateWebMediaUnknownError("media_readback")
            remote = result.get("result")
            if not isinstance(remote, dict) or "value" not in remote:
                raise PrivateWebMediaUnknownError("media_readback")
            return self._media_snapshot_from_value(remote["value"])
        except PrivateWebMediaUnknownError:
            raise
        except Exception:
            raise PrivateWebMediaUnknownError("media_readback") from None

    def _media_create_activation_function(
        self,
        expected_create: PrivateWebCreateSnapshot,
        expected_media: PrivateWebCreateMediaSnapshot,
    ) -> str:
        baseline = self._create_form_expression(
            expected=expected_create,
            allow_media_files=True,
        )
        expected_files = json.dumps(
            [
                {"name": item.name, "size_bytes": item.size_bytes}
                for item in expected_media.files
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return f"""
function() {{
  const stateOnly = (state) => ({{state}});
  const baseline = {baseline};
  if (!baseline || baseline.state !== "ready") {{
    return baseline || stateOnly("unknown");
  }}

  const input = this;
  const files = Array.from(document.querySelectorAll('input[type="file"]'));
  const expectedFiles = {expected_files};
  if (
    !(input instanceof HTMLInputElement) ||
    input.type !== "file" ||
    input.files === null ||
    files.length !== 1 ||
    files[0] !== input ||
    !input.isConnected ||
    input.files.length !== expectedFiles.length
  ) return stateOnly("unknown");

  for (let index = 0; index < expectedFiles.length; index += 1) {{
    const actual = input.files[index];
    const expected = expectedFiles[index];
    if (
      actual.name !== expected.name ||
      actual.size !== expected.size_bytes
    ) return stateOnly("unknown");
  }}

  const title = document.querySelector("#ad-title");
  if (!(title instanceof HTMLInputElement) || input.form !== title.form) {{
    return stateOnly("unknown");
  }}
  const form = title.form;
  if (!form) return stateOnly("unknown");
  const normalizedButtonText = (element) =>
    (element.innerText || "").replace(/\\s+/g, " ").trim();
  const publishButtons = Array.from(form.querySelectorAll("button")).filter(
    (button) => normalizedButtonText(button) === "Anzeige aufgeben"
  );
  if (
    publishButtons.length !== 1 ||
    publishButtons[0].type !== "button" ||
    publishButtons[0].disabled ||
    publishButtons[0].getAttribute("aria-disabled") === "true"
  ) return stateOnly("unknown");

  const button = publishButtons[0];
  const style = getComputedStyle(button);
  if (
    style.display === "none" ||
    style.visibility === "hidden" ||
    style.visibility === "collapse" ||
    style.pointerEvents === "none" ||
    Number(style.opacity) === 0
  ) return stateOnly("unknown");
  button.scrollIntoView({{block: "center", inline: "center"}});
  const rect = button.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return stateOnly("unknown");
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;
  const hit = document.elementFromPoint(x, y);
  if (hit !== button && !button.contains(hit)) return stateOnly("unknown");
  if (
    !Number.isFinite(x) ||
    !Number.isFinite(y) ||
    x < 0 ||
    y < 0 ||
    x > window.innerWidth ||
    y > window.innerHeight
  ) return stateOnly("unknown");
  return {{state: "ready", x, y}};
}}
"""

    def _media_submit_settlement_expression(self) -> str:
        origin = json.dumps(self._expected_origin)
        create_path = json.dumps(_CREATE_FORM_PATH)
        post_submit_path = json.dumps(_POST_SUBMIT_PATH)
        return f"""
(() => {{
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

  if (currentOrigin !== {origin}) return "unconfirmed";
  if (hasCaptcha || hasMfa || hasSecurityChallenge || hasLogin) {{
    return "unconfirmed";
  }}
  if (currentPath === {post_submit_path}) return "confirmed";
  if (currentPath === {create_path}) return "pending";
  return "unconfirmed";
}})()
"""

    def _wait_for_media_submit_settlement(self, client) -> None:
        deadline = self._monotonic() + self._timeout_seconds
        expression = self._media_submit_settlement_expression()
        while True:
            try:
                settlement = self._runtime_value(client, expression)
            except Exception:
                # Navigation may transiently invalidate the execution context
                # after a successful click. Keep the one-shot submit fenced and
                # allow only bounded observation; never retry browser input.
                settlement = None
            if settlement == "confirmed":
                return
            if settlement not in ("pending", None):
                raise PrivateWebSubmitUnknownError(
                    "create_media_submit_settle"
                )
            if self._monotonic() >= deadline:
                raise PrivateWebSubmitUnknownError(
                    "create_media_submit_settle"
                )
            try:
                self._sleep(0.05)
            except Exception:
                raise PrivateWebSubmitUnknownError(
                    "create_media_submit_settle"
                ) from None

    def reconcile_create_media_submit(self) -> None:
        """Observe an unresolved one-shot media submit without retrying input."""
        if not self._media_submit_unsettled:
            raise PrivateWebCdpWriteNotAttemptedError(
                "create_media_submit_reconcile"
            )
        client = self._client()
        self._wait_for_media_submit_settlement(client)
        self._media_submit_unsettled = False

    def submit_create_media(
        self,
        expected: PrivateWebCreateMediaSnapshot,
    ) -> None:
        if not isinstance(expected, PrivateWebCreateMediaSnapshot):
            raise TypeError("expected must be PrivateWebCreateMediaSnapshot")
        if expected.state is not PrivateWebEditorState.READY:
            raise ValueError("expected media snapshot must be ready")
        if self._create_submit_attempted:
            raise PrivateWebCdpWriteNotAttemptedError(
                "create_media_submit_already_attempted"
            )

        expected_create = self._media_expected_create_snapshot
        object_id = self._media_file_object_id
        prepared = self._media_prepared
        if (
            not self._create_bound
            or not self._media_selection_attempted
            or expected_create is None
            or object_id is None
            or prepared is None
            or expected.files != prepared.files
        ):
            raise PrivateWebCdpWriteNotAttemptedError(
                "create_media_submit"
            )

        try:
            current = self.read_create_media()
        except Exception:
            raise PrivateWebCdpWriteNotAttemptedError(
                "create_media_submit_revalidate"
            ) from None
        if current != expected:
            raise PrivateWebCdpWriteNotAttemptedError(
                "create_media_submit_drift"
            )

        # The publish action itself is one-shot. Arm the no-retry fence before
        # the final page-side binding/point calculation, matching submit_create.
        self._create_submit_attempted = True
        client = self._client()
        try:
            result = client.call(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": self._media_create_activation_function(
                        expected_create,
                        expected,
                    ),
                    "returnByValue": True,
                    "awaitPromise": False,
                },
            )
            if "exceptionDetails" in result:
                raise PrivateWebCdpWriteNotAttemptedError(
                    "create_media_submit"
                )
            remote = result.get("result")
            if not isinstance(remote, dict) or "value" not in remote:
                raise PrivateWebCdpWriteNotAttemptedError(
                    "create_media_submit"
                )
            point = self._create_point(
                remote["value"],
                stage="create_media_submit",
            )
        except WriteNotAttemptedError:
            raise
        except Exception:
            raise PrivateWebCdpWriteNotAttemptedError(
                "create_media_submit"
            ) from None

        # Do not release the file handle or private copies before settlement:
        # the browser may still consume them asynchronously after the click.
        # close() remains the owner of page-local cleanup.
        self._last_create_snapshot = None
        self._media_expected_create_snapshot = None
        # From the first browser-input dispatch onward, cleanup is unsafe until
        # the existing post-submit state is observed. Keep this armed for every
        # UNKNOWN outcome; reconciliation below never repeats browser input.
        self._media_submit_unsettled = True
        try:
            self._dispatch_browser_click(
                client,
                x=point[0],
                y=point[1],
            )
        except Exception:
            raise PrivateWebSubmitUnknownError(
                "create_media_submit"
            ) from None

        # A caller may close the page immediately after this returns. Use the
        # same observation-only path for the initial wait and later UNKNOWN
        # recovery; only confirmed settlement disarms destructive cleanup.
        self.reconcile_create_media_submit()