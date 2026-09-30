"""Core domain and orchestration primitives for mark-api."""

from .application import EnrichedOwnerReader, MarkService
from .domain import (
    AdCreateRequest,
    AdSnapshot,
    CreateOperationReceipt,
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
    "AdCreateRequest",
    "AdSnapshot",
    "AdView",
    "DashboardSummary",
    "CreateOperationReceipt",
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
