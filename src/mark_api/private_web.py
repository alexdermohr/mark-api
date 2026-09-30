from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .domain import AdCreateRequest, LifecycleState
from .ports import WriteNotAttemptedError


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


@dataclass(frozen=True, slots=True)
class PrivateWebCreateSnapshot:
    state: PrivateWebEditorState
    title: str | None = None
    description: str | None = None
    price_amount: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, PrivateWebEditorState):
            raise TypeError("state must be PrivateWebEditorState")
        if self.state is PrivateWebEditorState.READY:
            if not isinstance(self.title, str):
                raise ValueError("ready create snapshot requires title text")
            if not isinstance(self.description, str):
                raise ValueError(
                    "ready create snapshot requires description text"
                )
            if not isinstance(self.price_amount, str):
                raise ValueError(
                    "ready create snapshot requires price text"
                )
            return
        if any(
            value is not None
            for value in (self.title, self.description, self.price_amount)
        ):
            raise ValueError(
                "non-ready create snapshots must not expose form content"
            )


@dataclass(frozen=True, slots=True)
class PrivateWebStateSnapshot:
    state: PrivateWebEditorState
    ad_id: str | None = None
    lifecycle_state: LifecycleState | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, PrivateWebEditorState):
            raise TypeError("state must be PrivateWebEditorState")
        if self.state is PrivateWebEditorState.READY:
            if self.ad_id is None:
                raise ValueError("ready state snapshot requires ad_id")
            _validated_ad_id(self.ad_id)
            if not isinstance(self.lifecycle_state, LifecycleState):
                raise TypeError("ready state snapshot requires LifecycleState")
            if self.lifecycle_state not in {
                LifecycleState.ACTIVE,
                LifecycleState.PAUSED,
            }:
                raise ValueError(
                    "ready state snapshot requires ACTIVE or PAUSED"
                )
            return
        if self.ad_id is not None or self.lifecycle_state is not None:
            raise ValueError(
                "non-ready state snapshots must not expose ad state"
            )


@dataclass(frozen=True, slots=True)
class PrivateWebDeleteSnapshot:
    state: PrivateWebEditorState
    ad_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, PrivateWebEditorState):
            raise TypeError("state must be PrivateWebEditorState")
        if self.state is PrivateWebEditorState.READY:
            if self.ad_id is None:
                raise ValueError("ready delete snapshot requires ad_id")
            _validated_ad_id(self.ad_id)
            return
        if self.ad_id is not None:
            raise ValueError(
                "non-ready delete snapshots must not expose ad_id"
            )


class PrivateWebCreatePage(Protocol):
    """Minimal browser-page boundary for one new owner ad."""

    def open_create_form(self, category_path: tuple[str, ...]) -> None:
        ...

    def read_create_form(self) -> PrivateWebCreateSnapshot:
        ...

    def replace_create_title(self, value: str) -> None:
        ...

    def replace_create_description(self, value: str) -> None:
        ...

    def replace_create_price(self, value: str) -> None:
        ...

    def submit_create(self) -> None:
        ...


class PrivateWebDeletePage(Protocol):
    """Minimal browser-page boundary for deleting one owner ad."""

    def open_delete_controls(self, ad_id: str) -> None:
        ...

    def read_delete_controls(self) -> PrivateWebDeleteSnapshot:
        ...

    def open_delete_confirmation(self) -> None:
        ...

    def read_delete_confirmation(self) -> PrivateWebDeleteSnapshot:
        ...

    def submit_delete(self) -> None:
        ...


class PrivateWebStatePage(Protocol):
    """Minimal browser-page boundary for one owner's lifecycle control."""

    def open_state_controls(self, ad_id: str) -> None:
        ...

    def read_state_controls(self) -> PrivateWebStateSnapshot:
        ...

    def submit_state(self, state: LifecycleState) -> None:
        ...


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


class PrivateWebPreconditionError(PrivateWebError, WriteNotAttemptedError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"private web precondition failed: {reason}")


class PrivateWebInteractionError(PrivateWebError):
    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"private web interaction failed at {stage}")


class PrivateWebWriteNotAttemptedError(
    PrivateWebInteractionError,
    WriteNotAttemptedError,
):
    """A private-Web interaction failed before any platform write attempt."""


class PrivateWebSubmitUnknownError(PrivateWebError):
    """A browser-level activation may have reached the platform."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"private web submit outcome unknown at {stage}")


class PrivateWebCreateWriter:
    """Publish one narrow OFFER/FIXED ad through the authenticated normal Web UI."""

    def __init__(self, page: PrivateWebCreatePage) -> None:
        self._page = page

    @staticmethod
    def _require_ready(
        snapshot: PrivateWebCreateSnapshot,
        *,
        stage: str,
    ) -> None:
        if not isinstance(snapshot, PrivateWebCreateSnapshot):
            raise PrivateWebPreconditionError(f"{stage}:invalid_snapshot")
        if snapshot.state is not PrivateWebEditorState.READY:
            raise PrivateWebPreconditionError(
                f"{stage}:{snapshot.state.value}"
            )

    def _call(
        self,
        stage: str,
        func,
        *args,
        unmarked_error_may_be_submit: bool = False,
    ):
        try:
            return func(*args)
        except PrivateWebSubmitUnknownError:
            raise
        except WriteNotAttemptedError:
            raise PrivateWebWriteNotAttemptedError(stage) from None
        except Exception:  # noqa: BLE001 - sanitize browser/provider boundary.
            if unmarked_error_may_be_submit:
                raise PrivateWebSubmitUnknownError(stage) from None
            raise PrivateWebWriteNotAttemptedError(stage) from None

    def create_ad(self, request: AdCreateRequest) -> None:
        if not isinstance(request, AdCreateRequest):
            raise TypeError("request must be AdCreateRequest")

        self._call(
            "open_create_form",
            self._page.open_create_form,
            request.category_path,
        )
        before = self._call(
            "read_create_before",
            self._page.read_create_form,
        )
        self._require_ready(before, stage="before")

        expected_price = str(request.price_eur)
        if before.title != request.title:
            self._call(
                "replace_create_title",
                self._page.replace_create_title,
                request.title,
            )
        if before.description != request.description:
            self._call(
                "replace_create_description",
                self._page.replace_create_description,
                request.description,
            )
        if before.price_amount != expected_price:
            self._call(
                "replace_create_price",
                self._page.replace_create_price,
                expected_price,
            )

        before_submit = self._call(
            "read_create_before_submit",
            self._page.read_create_form,
        )
        self._require_ready(before_submit, stage="before_submit")
        if (
            before_submit.title != request.title
            or before_submit.description != request.description
            or before_submit.price_amount != expected_price
        ):
            raise PrivateWebPreconditionError(
                "before_submit:create_values_drift"
            )

        self._call(
            "submit_create",
            self._page.submit_create,
            unmarked_error_may_be_submit=True,
        )


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


class PrivateWebStateWriter:
    """Pause or activate exactly one owner ad through the normal Web UI."""

    def __init__(self, page: PrivateWebStatePage) -> None:
        self._page = page

    @staticmethod
    def _validated_target_state(state: LifecycleState) -> LifecycleState:
        if not isinstance(state, LifecycleState):
            raise TypeError("state must be LifecycleState")
        if state not in {LifecycleState.ACTIVE, LifecycleState.PAUSED}:
            raise ValueError("state must be ACTIVE or PAUSED")
        return state

    @staticmethod
    def _require_ready(
        snapshot: PrivateWebStateSnapshot,
        *,
        target_ad_id: str,
        stage: str,
    ) -> LifecycleState:
        if not isinstance(snapshot, PrivateWebStateSnapshot):
            raise PrivateWebPreconditionError(f"{stage}:invalid_snapshot")
        if snapshot.state is not PrivateWebEditorState.READY:
            raise PrivateWebPreconditionError(
                f"{stage}:{snapshot.state.value}"
            )
        if snapshot.ad_id != target_ad_id:
            raise PrivateWebPreconditionError(f"{stage}:ad_id_mismatch")
        lifecycle_state = snapshot.lifecycle_state
        if lifecycle_state not in {
            LifecycleState.ACTIVE,
            LifecycleState.PAUSED,
        }:
            raise PrivateWebPreconditionError(f"{stage}:invalid_lifecycle_state")
        return lifecycle_state

    def _call(
        self,
        stage: str,
        func,
        *args,
        unmarked_error_may_be_submit: bool = False,
    ):
        try:
            return func(*args)
        except PrivateWebSubmitUnknownError:
            raise
        except WriteNotAttemptedError:
            raise PrivateWebWriteNotAttemptedError(stage) from None
        except Exception:  # noqa: BLE001 - sanitize browser/provider boundary.
            error_type = (
                PrivateWebInteractionError
                if unmarked_error_may_be_submit
                else PrivateWebWriteNotAttemptedError
            )
            raise error_type(stage) from None

    def set_state(self, ad_id: str, state: LifecycleState) -> None:
        target_ad_id = _validated_ad_id(ad_id)
        target_state = self._validated_target_state(state)

        self._call(
            "open_state_controls",
            self._page.open_state_controls,
            target_ad_id,
        )
        before = self._call(
            "read_state_before",
            self._page.read_state_controls,
        )
        current_state = self._require_ready(
            before,
            target_ad_id=target_ad_id,
            stage="before",
        )
        if current_state is target_state:
            return

        expected_pre_state = (
            LifecycleState.PAUSED
            if target_state is LifecycleState.ACTIVE
            else LifecycleState.ACTIVE
        )
        if current_state is not expected_pre_state:
            raise PrivateWebPreconditionError("before:state_mismatch")

        before_submit = self._call(
            "read_state_before_submit",
            self._page.read_state_controls,
        )
        current_before_submit = self._require_ready(
            before_submit,
            target_ad_id=target_ad_id,
            stage="before_submit",
        )
        if current_before_submit is target_state:
            return
        if current_before_submit is not expected_pre_state:
            raise PrivateWebPreconditionError(
                "before_submit:state_drift"
            )

        self._call(
            "submit_state",
            self._page.submit_state,
            target_state,
            unmarked_error_may_be_submit=True,
        )


class PrivateWebDeleteWriter:
    """Delete exactly one owner ad through the normal Web UI.

    The first browser activation only opens Kleinanzeigen's explicit single-ad
    confirmation modal. The second activation is the sole platform delete
    attempt. This adapter never retries either activation; authoritative success
    remains the surrounding SafeWriteOrchestrator's owner-inventory readback.
    """

    def __init__(self, page: PrivateWebDeletePage) -> None:
        self._page = page

    @staticmethod
    def _require_ready(
        snapshot: PrivateWebDeleteSnapshot,
        *,
        target_ad_id: str,
        stage: str,
    ) -> None:
        if not isinstance(snapshot, PrivateWebDeleteSnapshot):
            raise PrivateWebPreconditionError(f"{stage}:invalid_snapshot")
        if snapshot.state is not PrivateWebEditorState.READY:
            raise PrivateWebPreconditionError(
                f"{stage}:{snapshot.state.value}"
            )
        if snapshot.ad_id != target_ad_id:
            raise PrivateWebPreconditionError(f"{stage}:ad_id_mismatch")

    def _call(
        self,
        stage: str,
        func,
        *args,
        unmarked_error_may_be_submit: bool = False,
    ):
        try:
            return func(*args)
        except PrivateWebSubmitUnknownError:
            raise
        except WriteNotAttemptedError:
            raise PrivateWebWriteNotAttemptedError(stage) from None
        except Exception:  # noqa: BLE001 - sanitize browser/provider boundary.
            error_type = (
                PrivateWebInteractionError
                if unmarked_error_may_be_submit
                else PrivateWebWriteNotAttemptedError
            )
            raise error_type(stage) from None

    def delete_ad(self, ad_id: str) -> None:
        target_ad_id = _validated_ad_id(ad_id)

        self._call(
            "open_delete_controls",
            self._page.open_delete_controls,
            target_ad_id,
        )
        before = self._call(
            "read_delete_before",
            self._page.read_delete_controls,
        )
        self._require_ready(
            before,
            target_ad_id=target_ad_id,
            stage="before",
        )

        self._call(
            "open_delete_confirmation",
            self._page.open_delete_confirmation,
        )
        confirmation = self._call(
            "read_delete_confirmation",
            self._page.read_delete_confirmation,
        )
        self._require_ready(
            confirmation,
            target_ad_id=target_ad_id,
            stage="confirmation",
        )

        self._call(
            "submit_delete",
            self._page.submit_delete,
            unmarked_error_may_be_submit=True,
        )
