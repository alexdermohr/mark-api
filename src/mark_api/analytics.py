from __future__ import annotations

from dataclasses import dataclass

from .domain import LifecycleState
from .query import AdView, MarkQueryService
from .storage import SnapshotStore


ANALYTICS_DIMENSIONS = (
    "image_type",
    "city",
    "text_type",
    "title_type",
)

ANALYTICS_METRICS = (
    "views",
    "watch_count",
    "reply_count",
    "conversation_count",
    "unique_buyer_count",
    "inbound_message_count",
)

_AD_METRICS = frozenset(("views", "watch_count", "reply_count"))
_REACTION_METRICS = frozenset(
    ("conversation_count", "unique_buyer_count", "inbound_message_count")
)


@dataclass(frozen=True, slots=True)
class AdMetricRanking:
    ad_id: str
    metric: str
    value: int
    present: bool
    lifecycle_state: LifecycleState
    title: str | None


@dataclass(frozen=True, slots=True)
class GroupMetricRanking:
    label: str
    sample_size: int
    metric_sum: int
    metric_mean: float


class AnalyticsService:
    """Read-only raw-metric analytics over explicit classification labels.

    This is a current projection: the latest explicit classification is paired
    with the latest/last-known metric projection. It is not a time-aligned or
    causal comparison of historical classification periods.
    """

    def __init__(self, store: SnapshotStore) -> None:
        self._store = store
        self._query = MarkQueryService(store)

    @staticmethod
    def _validate_metric(metric: str) -> None:
        if metric not in ANALYTICS_METRICS:
            raise ValueError(f"unknown analytics metric: {metric}")

    @staticmethod
    def _validate_dimension(dimension: str) -> None:
        if dimension not in ANALYTICS_DIMENSIONS:
            raise ValueError(f"unknown analytics dimension: {dimension}")

    def _metric_value(self, ad: AdView, metric: str) -> int | None:
        if metric in _AD_METRICS:
            value = getattr(ad, metric)
            return value if isinstance(value, int) else None

        if metric in _REACTION_METRICS:
            history = self._store.reaction_history(ad.ad_id)
            if not history:
                return None
            value = getattr(history[-1], metric)
            return value if isinstance(value, int) else None

        raise ValueError(f"unknown analytics metric: {metric}")

    def rank_ads(self, metric: str) -> tuple[AdMetricRanking, ...]:
        self._validate_metric(metric)
        rows: list[AdMetricRanking] = []
        for ad in self._query.latest_ads():
            value = self._metric_value(ad, metric)
            if value is None:
                continue
            rows.append(
                AdMetricRanking(
                    ad_id=ad.ad_id,
                    metric=metric,
                    value=value,
                    present=ad.present,
                    lifecycle_state=ad.lifecycle_state,
                    title=ad.title,
                )
            )
        rows.sort(key=lambda item: (-item.value, item.ad_id))
        return tuple(rows)

    def group_rankings(
        self,
        dimension: str,
        metric: str,
    ) -> tuple[GroupMetricRanking, ...]:
        self._validate_dimension(dimension)
        self._validate_metric(metric)

        grouped: dict[str, list[int]] = {}
        for ad in self._query.latest_ads():
            classification = self._store.latest_classification(ad.ad_id)
            if classification is None:
                continue
            label = getattr(classification, dimension)
            if label is None:
                continue

            value = self._metric_value(ad, metric)
            if value is None:
                continue
            grouped.setdefault(label, []).append(value)

        rows = [
            GroupMetricRanking(
                label=label,
                sample_size=len(values),
                metric_sum=sum(values),
                metric_mean=sum(values) / len(values),
            )
            for label, values in grouped.items()
        ]
        rows.sort(
            key=lambda item: (
                -item.metric_mean,
                -item.metric_sum,
                -item.sample_size,
                item.label,
            )
        )
        return tuple(rows)


def ad_metric_ranking_to_dict(item: AdMetricRanking) -> dict[str, object]:
    return {
        "ad_id": item.ad_id,
        "metric": item.metric,
        "value": item.value,
        "present": item.present,
        "lifecycle_state": item.lifecycle_state.value,
        "title": item.title,
    }


def group_metric_ranking_to_dict(item: GroupMetricRanking) -> dict[str, object]:
    return {
        "label": item.label,
        "sample_size": item.sample_size,
        "metric_sum": item.metric_sum,
        "metric_mean": item.metric_mean,
    }
