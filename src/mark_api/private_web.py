from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class PrivateWebEditorState(StrEnum):
    READY = "ready"
    LOGIN_REQUIRED = "login_required"
    MFA_REQUIRED = "mfa_required"
    CAPTCHA_REQUIRED = "captcha_required"
    SECURITY_CHALLENGE = "security_challenge"
    UNKNOWN = "unknown"


def _validated_ad_id(ad_id: str) -> str:
    if not isinstance(ad_id, str):
        raise TypeError("ad_id must be a string")
    normalized = ad_id.strip()
    if (
        not normalized
        or len(normalized) > 32
        or not normalized.isascii()
        or not normalized.isdigit()
    ):
        raise ValueError("ad_id must contain only ASCII digits")
    return normalized


@dataclass(frozen=True, slots=True)
class PrivateWebEditorSnapshot:
    state: PrivateWebEditorState
    ad_id: str | None = None
    title: str | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, PrivateWebEditorState):
            raise TypeError("state must be PrivateWebEditorState")
        if self.state is PrivateWebEditorState.READY:
            if self.ad_id is None:
                raise ValueError("ready editor requires ad_id")
            _validated_ad_id(self.ad_id)
            if not isinstance(self.title, str):
                raise ValueError("ready editor requires title text")
            if not isinstance(self.description, str):
                raise ValueError("ready editor requires description text")
            return
        if any(
            value is not None
            for value in (self.ad_id, self.title, self.description)
        ):
            raise ValueError(
                "non-ready editor snapshots must not expose ad content"
            )


class PrivateWebPage(Protocol):
    """Minimal browser-page boundary for one existing owner's ad editor."""

    def open_editor(self, ad_id: str) -> None:
        ...

    def read_editor(self) -> PrivateWebEditorSnapshot:
        ...

    def replace_title(self, value: str) -> None:
        ...

    def replace_description(self, value: str) -> None:
        ...

    def submit(self) -> None:
        ...


class PrivateWebError(RuntimeError):
    """Base error for the private-account Web UI writer boundary."""


class PrivateWebPreconditionError(PrivateWebError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"private web precondition failed: {reason}")


class PrivateWebInteractionError(PrivateWebError):
    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"private web interaction failed at {stage}")


class PrivateWebContentWriter:
    """Update title/description through one user-authenticated Web UI editor.

    This adapter never stores or accepts login credentials. Authentication,
    MFA and anti-automation challenges remain human-controlled browser state.
    The adapter performs no retry. Platform outcome confirmation belongs to the
    surrounding SafeWriteOrchestrator post-read.
    """

    def __init__(self, page: PrivateWebPage) -> None:
        self._page = page

    @staticmethod
    def _validated_updates(
        *,
        title: str | None,
        description: str | None,
    ) -> tuple[str | None, str | None]:
        if title is not None and not isinstance(title, str):
            raise TypeError("title must be a string or None")
        if description is not None and not isinstance(description, str):
            raise TypeError("description must be a string or None")
        if title is None and description is None:
            raise ValueError("at least one content field must be provided")
        return title, description

    def _call(self, stage: str, func, *args):
        try:
            return func(*args)
        except Exception:  # noqa: BLE001 - sanitize browser/provider boundary.
            raise PrivateWebInteractionError(stage) from None

    @staticmethod
    def _require_ready(
        snapshot: PrivateWebEditorSnapshot,
        *,
        target_ad_id: str,
        stage: str,
    ) -> None:
        if not isinstance(snapshot, PrivateWebEditorSnapshot):
            raise PrivateWebPreconditionError(f"{stage}:invalid_snapshot")
        if snapshot.state is not PrivateWebEditorState.READY:
            raise PrivateWebPreconditionError(
                f"{stage}:{snapshot.state.value}"
            )
        if snapshot.ad_id != target_ad_id:
            raise PrivateWebPreconditionError(f"{stage}:ad_id_mismatch")

    def update_content(
        self,
        ad_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        target_ad_id = _validated_ad_id(ad_id)
        title, description = self._validated_updates(
            title=title,
            description=description,
        )

        self._call("open_editor", self._page.open_editor, target_ad_id)
        before = self._call("read_before", self._page.read_editor)
        self._require_ready(
            before,
            target_ad_id=target_ad_id,
            stage="before",
        )

        expected_title = before.title if title is None else title
        expected_description = (
            before.description if description is None else description
        )
        assert expected_title is not None
        assert expected_description is not None

        changed = False
        if title is not None and title != before.title:
            self._call("replace_title", self._page.replace_title, title)
            changed = True
        if description is not None and description != before.description:
            self._call(
                "replace_description",
                self._page.replace_description,
                description,
            )
            changed = True

        if not changed:
            return

        before_submit = self._call(
            "read_before_submit",
            self._page.read_editor,
        )
        self._require_ready(
            before_submit,
            target_ad_id=target_ad_id,
            stage="before_submit",
        )
        if (
            before_submit.title != expected_title
            or before_submit.description != expected_description
        ):
            raise PrivateWebPreconditionError(
                "before_submit:editor_values_drift"
            )

        self._call("submit", self._page.submit)
