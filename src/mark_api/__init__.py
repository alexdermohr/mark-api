"""Core domain and orchestration primitives for mark-api."""

from .domain import (
    AdSnapshot,
    DeleteApproval,
    LifecycleState,
    OperationOutcome,
    OperationReceipt,
    ReactionSnapshot,
)
from .results import ReadResult, ReadStatus

__all__ = [
    "AdSnapshot",
    "DeleteApproval",
    "LifecycleState",
    "OperationOutcome",
    "OperationReceipt",
    "ReactionSnapshot",
    "ReadResult",
    "ReadStatus",
]
