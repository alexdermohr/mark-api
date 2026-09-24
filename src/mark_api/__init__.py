"""Core domain and orchestration primitives for mark-api."""

from .application import EnrichedOwnerReader, MarkService
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
    "EnrichedOwnerReader",
    "LifecycleState",
    "MarkService",
    "OperationOutcome",
    "OperationReceipt",
    "ReactionSnapshot",
    "ReadResult",
    "ReadStatus",
]
