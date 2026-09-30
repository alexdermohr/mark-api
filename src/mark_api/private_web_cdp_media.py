from __future__ import annotations

from .private_web import PrivateWebCreateSnapshot, PrivateWebEditorState
from .private_web_cdp import (
    CdpPrivateWebPage,
    PrivateWebCdpError,
    PrivateWebCdpWriteNotAttemptedError,
)
from .private_web_media import (
    PrivateWebCreateMediaSnapshot,
    PrivateWebMediaFileSnapshot,
    PrivateWebMediaSource,
    PrivateWebMediaUnknownError,
    _validated_local_media,
)


class CdpPrivateWebMediaPage(CdpPrivateWebPage):
    """Narrow CDP extension for create-form file selection.

    The inherited create-form binding remains authoritative. The exact
    validated file input is retained as one CDP object handle across the
    single DOM.setFileInputFiles call and the subsequent readback.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._media_selection_attempted = False
        self._media_expected_create_snapshot: PrivateWebCreateSnapshot | None = None
        self._media_file_object_id: str | None = None

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

    def open_create_form(self, category_path: tuple[str, ...]) -> None:
        if self._media_selection_attempted:
            raise PrivateWebCdpWriteNotAttemptedError(
                "create_media_already_attempted"
            )
        self._release_media_handle()
        self._media_expected_create_snapshot = None
        super().open_create_form(category_path)

    def close(self) -> None:
        self._release_media_handle()
        self._media_expected_create_snapshot = None
        super().close()

    @staticmethod
    def _validated_media_paths(files: tuple[str, ...]) -> tuple[str, ...]:
        if not isinstance(files, tuple):
            raise TypeError("media files must be a tuple")
        sources = tuple(PrivateWebMediaSource(path=path) for path in files)
        paths, _snapshots = _validated_local_media(sources)
        return paths

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

        # Repeat local validation at the CDP boundary so direct callers cannot
        # bypass the no-browser-before-local-validation rule.
        paths = self._validated_media_paths(files)

        snapshot = self._last_create_snapshot
        if (
            not self._create_bound
            or snapshot is None
            or snapshot.state is not PrivateWebEditorState.READY
        ):
            raise PrivateWebCdpWriteNotAttemptedError("create_media")

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

        try:
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
            raise
        except PrivateWebCdpError:
            raise PrivateWebCdpWriteNotAttemptedError(
                "create_media_bind"
            ) from None
        except Exception:
            raise PrivateWebCdpWriteNotAttemptedError(
                "create_media_bind"
            ) from None

        # From this point on, file-input mutation may be effective. Mark the
        # attempt before dispatch and preserve the exact object handle for
        # readback. No failure below authorizes a second selection attempt.
        self._media_selection_attempted = True
        self._media_expected_create_snapshot = current
        self._media_file_object_id = object_id
        self._last_create_snapshot = None
        try:
            client.call(
                "DOM.setFileInputFiles",
                {
                    "files": list(paths),
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
