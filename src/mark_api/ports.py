from __future__ import annotations

from typing import Protocol

from .domain import AdCreateRequest, AdSnapshot, LifecycleState, ReactionSnapshot
from .results import ReadResult


class WriteNotAttemptedError(RuntimeError):
    """The writer failed before any external mutation could be attempted."""


class AdsReader(Protocol):
    def read_ads(self) -> ReadResult[tuple[AdSnapshot, ...]]:
        """Read the current owner inventory."""


class MetricsReader(Protocol):
    def read_ad(self, ad_id: str) -> ReadResult[AdSnapshot]:
        """Read one current-owner ad snapshot by ID."""


class InboxReader(Protocol):
    def read_reactions(self, ad_id: str) -> ReadResult[ReactionSnapshot]:
        """Read normalized reaction metrics for exactly one ad."""


class AdCreateWriter(Protocol):
    def create_ad(self, request: AdCreateRequest) -> None:
        """Attempt exactly one new-ad publish for the explicit request."""


class AdStateWriter(Protocol):
    def set_state(self, ad_id: str, state: LifecycleState) -> None:
        """Request one state mutation for exactly one ad."""


class AdDeleteWriter(Protocol):
    def delete_ad(self, ad_id: str) -> None:
        """Delete exactly one ad."""


class AdContentUpdater(Protocol):
    def update_content(
        self,
        ad_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        """Update exactly one existing ad without creating a replacement."""