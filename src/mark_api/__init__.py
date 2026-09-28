"""Core domain and orchestration primitives for mark-api."""

from .application import EnrichedOwnerReader, MarkService
from .domain import (
    AdSnapshot,
    DeleteApproval,
    InboundMessageEvent,
    LifecycleState,
    OperationOutcome,
    OperationReceipt,
    ReactionSnapshot,
)
from .query import AdView, DashboardSummary, MarkQueryService
from .results import ReadResult, ReadStatus

__all__ = [
    "AdSnapshot",
    "AdView",
    "DashboardSummary",
    "DeleteApproval",
    "EnrichedOwnerReader",
    "InboundMessageEvent",
    "LifecycleState",
    "MarkQueryService",
    "MarkService",
    "OperationOutcome",
    "OperationReceipt",
    "ReactionSnapshot",
    "ReadResult",
    "ReadStatus",
]
