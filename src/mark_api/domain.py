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


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_counter(value: int | None, field_name: str) -> None:
    if value is not None and (isinstance(value, bool) or value < 0):
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
