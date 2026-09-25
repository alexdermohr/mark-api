from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .domain import AdSnapshot, LifecycleState, ReactionSnapshot
from .storage import SnapshotStore


@dataclass(frozen=True, slots=True)
class AdView:
    ad_id: str
    lifecycle_state: LifecycleState
    present: bool
    observed_at: datetime
    source: str
    title: str | None
    description: str | None
    views: int | None
    watch_count: int | None
    reply_count: int | None


@dataclass(frozen=True, slots=True)
class DashboardSummary:
    tracked_ads: int
    current_ads: int
    absent_ads: int
    unknown_state_ads: int
    views_total_known: int
    views_observed_ads: int
    watch_total_known: int
    watch_observed_ads: int
    replies_total_known: int
    replies_observed_ads: int


def _last_not_none(
    history: tuple[AdSnapshot, ...],
    field_name: str,
) -> str | int | None:
    for snapshot in reversed(history):
        value = getattr(snapshot, field_name)
        if value is not None:
            return value
    return None


class MarkQueryService:
    """Read-only projections over the append-only SQLite history."""

    def __init__(self, store: SnapshotStore) -> None:
        self._store = store

    def latest_ads(self) -> tuple[AdView, ...]:
        rows: list[AdView] = []
        for ad_id in self._store.tracked_ad_ids():
            history = self._store.ad_history(ad_id)
            if not history:
                continue
            latest = history[-1]
            rows.append(
                AdView(
                    ad_id=ad_id,
                    lifecycle_state=latest.lifecycle_state,
                    present=latest.lifecycle_state is not LifecycleState.ABSENT,
                    observed_at=latest.observed_at,
                    source=latest.source,
                    title=_last_not_none(history, "title"),
                    description=_last_not_none(history, "description"),
                    views=_last_not_none(history, "views"),
                    watch_count=_last_not_none(history, "watch_count"),
                    reply_count=_last_not_none(history, "reply_count"),
                )
            )
        return tuple(rows)

    def ad_history(self, ad_id: str) -> tuple[AdSnapshot, ...]:
        return self._store.ad_history(ad_id)

    def reaction_history(self, ad_id: str) -> tuple[ReactionSnapshot, ...]:
        return self._store.reaction_history(ad_id)

    def summary(self) -> DashboardSummary:
        ads = self.latest_ads()
        current_ads = sum(1 for item in ads if item.present)
        absent_ads = sum(
            1
            for item in ads
            if item.lifecycle_state is LifecycleState.ABSENT
        )
        unknown_state_ads = sum(
            1
            for item in ads
            if item.lifecycle_state is LifecycleState.UNKNOWN
        )

        views = [item.views for item in ads if item.views is not None]
        watches = [
            item.watch_count
            for item in ads
            if item.watch_count is not None
        ]
        replies = [
            item.reply_count
            for item in ads
            if item.reply_count is not None
        ]

        return DashboardSummary(
            tracked_ads=len(ads),
            current_ads=current_ads,
            absent_ads=absent_ads,
            unknown_state_ads=unknown_state_ads,
            views_total_known=sum(views),
            views_observed_ads=len(views),
            watch_total_known=sum(watches),
            watch_observed_ads=len(watches),
            replies_total_known=sum(replies),
            replies_observed_ads=len(replies),
        )


def ad_view_to_dict(item: AdView) -> dict[str, object]:
    return {
        "ad_id": item.ad_id,
        "lifecycle_state": item.lifecycle_state.value,
        "present": item.present,
        "observed_at": item.observed_at.isoformat(),
        "source": item.source,
        "title": item.title,
        "description": item.description,
        "views": item.views,
        "watch_count": item.watch_count,
        "reply_count": item.reply_count,
    }


def ad_snapshot_to_dict(item: AdSnapshot) -> dict[str, object]:
    return {
        "ad_id": item.ad_id,
        "lifecycle_state": item.lifecycle_state.value,
        "observed_at": item.observed_at.isoformat(),
        "source": item.source,
        "title": item.title,
        "description": item.description,
        "views": item.views,
        "watch_count": item.watch_count,
        "reply_count": item.reply_count,
    }


def reaction_snapshot_to_dict(item: ReactionSnapshot) -> dict[str, object]:
    return {
        "ad_id": item.ad_id,
        "observed_at": item.observed_at.isoformat(),
        "source": item.source,
        "conversation_count": item.conversation_count,
        "unique_buyer_count": item.unique_buyer_count,
        "inbound_message_count": item.inbound_message_count,
    }


def summary_to_dict(item: DashboardSummary) -> dict[str, int]:
    return {
        "tracked_ads": item.tracked_ads,
        "current_ads": item.current_ads,
        "absent_ads": item.absent_ads,
        "unknown_state_ads": item.unknown_state_ads,
        "views_total_known": item.views_total_known,
        "views_observed_ads": item.views_observed_ads,
        "watch_total_known": item.watch_total_known,
        "watch_observed_ads": item.watch_observed_ads,
        "replies_total_known": item.replies_total_known,
        "replies_observed_ads": item.replies_observed_ads,
    }
