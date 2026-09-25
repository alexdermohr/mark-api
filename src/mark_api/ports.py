from __future__ import annotations

from typing import Protocol

from .domain import AdSnapshot, LifecycleState, ReactionSnapshot
from .results import ReadResult


class AdsReader(Protocol):
    def read_ads(self) -> ReadResult[tuple[AdSnapshot, ...]]:
        """Read the current owner inventory."""


class MetricsReader(Protocol):
    def read_ad(self, ad_id: str) -> ReadResult[AdSnapshot]:
        """Read one current-owner ad snapshot by ID."""


class InboxReader(Protocol):
    def read_reactions(self, ad_id: str) -> ReadResult[ReactionSnapshot]:
        """Read normalized reaction metrics for exactly one ad."""


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
