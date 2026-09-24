from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar


T = TypeVar("T")


class ReadStatus(StrEnum):
    SUCCESS_NONEMPTY = "success_nonempty"
    SUCCESS_EMPTY = "success_empty"
    UNAUTHENTICATED = "unauthenticated"
    HTTP_ERROR = "http_error"
    TRANSPORT_ERROR = "transport_error"
    PARSE_ERROR = "parse_error"


_SUCCESS_STATUSES = {
    ReadStatus.SUCCESS_NONEMPTY,
    ReadStatus.SUCCESS_EMPTY,
}


@dataclass(frozen=True, slots=True)
class ReadResult(Generic[T]):
    status: ReadStatus
    value: T | None = None
    error: str | None = None
    http_status: int | None = None

    def __post_init__(self) -> None:
        if self.status is ReadStatus.SUCCESS_NONEMPTY and self.value is None:
            raise ValueError("success_nonempty requires a value")
        if self.status in _SUCCESS_STATUSES:
            if self.error is not None or self.http_status is not None:
                raise ValueError("successful reads must not carry error metadata")
        elif self.value is not None:
            raise ValueError("failed reads must not carry a value")

    @property
    def is_success(self) -> bool:
        return self.status in _SUCCESS_STATUSES

    @classmethod
    def success_nonempty(cls, value: T) -> "ReadResult[T]":
        return cls(status=ReadStatus.SUCCESS_NONEMPTY, value=value)

    @classmethod
    def success_empty(cls, value: T | None = None) -> "ReadResult[T]":
        return cls(status=ReadStatus.SUCCESS_EMPTY, value=value)

    @classmethod
    def failure(
        cls,
        status: ReadStatus,
        *,
        error: str | None = None,
        http_status: int | None = None,
    ) -> "ReadResult[T]":
        if status in _SUCCESS_STATUSES:
            raise ValueError("failure() requires a non-success status")
        return cls(
            status=status,
            error=error,
            http_status=http_status,
        )
