from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class LifecycleState(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class OperationOutcome(StrEnum):
    CONFIRMED = "confirmed"
    AMBIGUOUS = "ambiguous"
    PRECONDITION_FAILED = "precondition_failed"


def _require_nonempty(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def _utf16_code_unit_length(value: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_counter(value: int | None, field_name: str) -> None:
    if value is not None and (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise ValueError(f"{field_name} must be an integer >= 0 or None")


def _normalize_optional_label(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string or None")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


@dataclass(frozen=True, slots=True)
class AdCreateRequest:
    """Narrow first private-Web create contract.

    The current implementation deliberately supports only OFFER + FIXED without
    media. Contact and location stay bound to the authenticated browser profile.
    """

    category_path: tuple[str, ...]
    title: str
    description: str
    price_eur: int

    def __post_init__(self) -> None:
        if not isinstance(self.category_path, tuple):
            raise ValueError("category_path must be a tuple")
        if len(self.category_path) < 2 or len(self.category_path) > 6:
            raise ValueError("category_path must contain between 2 and 6 labels")
        normalized_path: list[str] = []
        for label in self.category_path:
            if not isinstance(label, str):
                raise ValueError("category_path labels must be strings")
            normalized = label.strip()
            if not normalized:
                raise ValueError("category_path labels must not be blank")
            if len(normalized) > 120:
                raise ValueError("category_path labels must be <= 120 characters")
            normalized_path.append(normalized)
        if len(set(normalized_path)) != len(normalized_path):
            raise ValueError("category_path must not contain duplicate labels")
        object.__setattr__(self, "category_path", tuple(normalized_path))

        if not isinstance(self.title, str):
            raise ValueError("title must be a string")
        _require_nonempty(self.title, "title")
        if self.title != self.title.strip():
            raise ValueError("title must not have surrounding whitespace")
        if "\r" in self.title or "\n" in self.title:
            raise ValueError("title must not contain line breaks")
        if _utf16_code_unit_length(self.title) > 65:
            raise ValueError("title must be <= 65 characters")

        if not isinstance(self.description, str):
            raise ValueError("description must be a string")
        normalized_description = self.description.replace("\r\n", "\n").replace("\r", "\n")
        _require_nonempty(normalized_description, "description")
        if _utf16_code_unit_length(normalized_description) > 4000:
            raise ValueError("description must be <= 4000 characters")
        object.__setattr__(self, "description", normalized_description)

        if (
            not isinstance(self.price_eur, int)
            or isinstance(self.price_eur, bool)
            or self.price_eur < 1
            or self.price_eur > 99_999_999
        ):
            raise ValueError("price_eur must be an integer in [1, 99999999]")


@dataclass(frozen=True, slots=True)
class AdSnapshot:
    ad_id: str
    observed_at: datetime
    source: str
    lifecycle_state: LifecycleState = LifecycleState.UNKNOWN
    title: str | None = None
    description: str | None = None
    views: int | None = None
    watch_count: int | None = None
    reply_count: int | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.ad_id, "ad_id")
        _require_nonempty(self.source, "source")
        _require_aware(self.observed_at, "observed_at")
        _require_counter(self.views, "views")
        _require_counter(self.watch_count, "watch_count")
        _require_counter(self.reply_count, "reply_count")


@dataclass(frozen=True, slots=True)
class ReactionSnapshot:
    ad_id: str
    conversation_count: int
    unique_buyer_count: int
    inbound_message_count: int
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        _require_nonempty(self.ad_id, "ad_id")
        _require_nonempty(self.source, "source")
        _require_aware(self.observed_at, "observed_at")
        _require_counter(self.conversation_count, "conversation_count")
        _require_counter(self.unique_buyer_count, "unique_buyer_count")
        _require_counter(self.inbound_message_count, "inbound_message_count")


@dataclass(frozen=True, slots=True)
class InboundMessageEvent:
    """Minimal local evidence for one inbound Kleinanzeigen message notification."""

    ad_id: str
    conversation_id: str
    provider_message_id: str
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        _require_nonempty(self.ad_id, "ad_id")
        _require_nonempty(self.conversation_id, "conversation_id")
        _require_nonempty(self.provider_message_id, "provider_message_id")
        _require_nonempty(self.source, "source")
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class AdClassification:
    ad_id: str
    observed_at: datetime
    source: str
    image_type: str | None = None
    city: str | None = None
    text_type: str | None = None
    title_type: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.ad_id, "ad_id")
        _require_nonempty(self.source, "source")
        _require_aware(self.observed_at, "observed_at")
        for field_name in ("image_type", "city", "text_type", "title_type"):
            object.__setattr__(
                self,
                field_name,
                _normalize_optional_label(getattr(self, field_name), field_name),
            )


@dataclass(frozen=True, slots=True)
class DeleteApproval:
    ad_id: str
    approved_by: str
    reference: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.ad_id, "ad_id")
        _require_nonempty(self.approved_by, "approved_by")


@dataclass(frozen=True, slots=True)
class OperationReceipt:
    operation: str
    ad_id: str
    started_at: datetime
    completed_at: datetime
    outcome: OperationOutcome
    pre_read_status: str
    post_read_status: str | None
    writer_invoked: bool
    authorization_by: str | None = None
    authorization_reference: str | None = None
    writer_error: str | None = None
    pre_snapshot: AdSnapshot | None = None
    post_snapshot: AdSnapshot | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.operation, "operation")
        _require_nonempty(self.ad_id, "ad_id")
        _require_nonempty(self.pre_read_status, "pre_read_status")
        if self.authorization_by is not None:
            _require_nonempty(self.authorization_by, "authorization_by")
        _require_aware(self.started_at, "started_at")
        _require_aware(self.completed_at, "completed_at")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")


@dataclass(frozen=True, slots=True)
class CreateOperationReceipt:
    operation: str
    started_at: datetime
    completed_at: datetime
    outcome: OperationOutcome
    pre_read_status: str
    confirmation_pre_read_status: str
    post_read_status: str | None
    confirmation_post_read_status: str | None
    content_post_read_status: str | None
    writer_invoked: bool
    created_ad_id: str | None = None
    writer_error: str | None = None
    post_snapshot: AdSnapshot | None = None
    confirmation_post_snapshot: AdSnapshot | None = None
    content_post_snapshot: AdSnapshot | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.operation, "operation")
        _require_nonempty(self.pre_read_status, "pre_read_status")
        _require_nonempty(
            self.confirmation_pre_read_status,
            "confirmation_pre_read_status",
        )
        for field_name in (
            "post_read_status",
            "confirmation_post_read_status",
            "content_post_read_status",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_nonempty(value, field_name)
        if self.created_ad_id is not None:
            _require_nonempty(self.created_ad_id, "created_ad_id")
        _require_aware(self.started_at, "started_at")
        _require_aware(self.completed_at, "completed_at")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if (
            self.outcome is OperationOutcome.CONFIRMED
            and self.created_ad_id is None
        ):
            raise ValueError("confirmed create requires created_ad_id")
        if self.created_ad_id is not None:
            for field_name in (
                "post_snapshot",
                "confirmation_post_snapshot",
                "content_post_snapshot",
            ):
                snapshot = getattr(self, field_name)
                if snapshot is not None and snapshot.ad_id != self.created_ad_id:
                    raise ValueError(
                        f"{field_name} must match created_ad_id"
                    )
