from __future__ import annotations

import argparse
import fcntl
import json
import os
import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .analytics import ANALYTICS_DIMENSIONS
from .domain import AdClassification
from .storage import SnapshotStore


_SOURCE = "manual-cli"
_CLEAR_CHOICES = tuple(item.replace("_", "-") for item in ANALYTICS_DIMENSIONS)
_LOCAL_CLASSIFICATION_LOCK = threading.Lock()


@contextmanager
def _classification_update_lock(store: SnapshotStore) -> Iterator[None]:
    lock_path = store.path.with_name(store.path.name + ".classification.lock")
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with _LOCAL_CLASSIFICATION_LOCK:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def update_classification(
    store: SnapshotStore,
    *,
    ad_id: str,
    labels: Mapping[str, str] | None = None,
    clears: Iterable[str] = (),
    observed_at: datetime | None = None,
) -> AdClassification:
    """Append one merged classification snapshot for an already tracked ad."""

    raw_labels = dict(labels or {})
    unknown_labels = sorted(set(raw_labels) - set(ANALYTICS_DIMENSIONS))
    if unknown_labels:
        raise ValueError(
            "unknown classification dimensions: " + ", ".join(unknown_labels)
        )

    normalized_labels: dict[str, str] = {}
    for dimension, value in raw_labels.items():
        if not isinstance(value, str):
            raise ValueError(f"{dimension} must be a string")
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError(f"{dimension} must not be blank")
        normalized_labels[dimension] = normalized_value

    normalized_clears = tuple(clears)
    unknown_clears = sorted(set(normalized_clears) - set(ANALYTICS_DIMENSIONS))
    if unknown_clears:
        raise ValueError(
            "unknown clear dimensions: " + ", ".join(unknown_clears)
        )

    if not normalized_labels and not normalized_clears:
        raise ValueError("at least one label update or clear is required")

    conflicts = sorted(set(normalized_labels) & set(normalized_clears))
    if conflicts:
        raise ValueError(
            "cannot set and clear the same dimensions: " + ", ".join(conflicts)
        )

    with _classification_update_lock(store):
        if ad_id not in store.tracked_ad_ids():
            raise ValueError(f"unknown tracked ad_id: {ad_id}")

        previous = store.latest_classification(ad_id)
        effective_observed_at = observed_at or datetime.now(timezone.utc)
        if (
            effective_observed_at.tzinfo is None
            or effective_observed_at.utcoffset() is None
        ):
            raise ValueError("observed_at must be timezone-aware")
        if (
            previous is not None
            and effective_observed_at <= previous.observed_at
        ):
            raise ValueError(
                "observed_at must be later than the latest classification"
            )

        merged = {
            dimension: (
                getattr(previous, dimension) if previous is not None else None
            )
            for dimension in ANALYTICS_DIMENSIONS
        }
        merged.update(normalized_labels)
        for dimension in normalized_clears:
            merged[dimension] = None

        previous_values = (
            {
                dimension: getattr(previous, dimension)
                for dimension in ANALYTICS_DIMENSIONS
            }
            if previous is not None
            else {dimension: None for dimension in ANALYTICS_DIMENSIONS}
        )
        if merged == previous_values:
            raise ValueError("classification update would not change any label")

        classification = AdClassification(
            ad_id=ad_id,
            observed_at=effective_observed_at,
            source=_SOURCE,
            **merged,
        )
        store.append_classification(classification)
        return classification


def _classification_to_dict(item: AdClassification) -> dict[str, object]:
    return {
        "ad_id": item.ad_id,
        "observed_at": item.observed_at.isoformat(),
        "source": item.source,
        **{
            dimension: getattr(item, dimension)
            for dimension in ANALYTICS_DIMENSIONS
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Append local analytics classification labels for an already "
            "tracked mark-api ad."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help="Path to the local mark-api SQLite database.",
    )
    parser.add_argument(
        "--ad-id",
        required=True,
        help="Already tracked ad ID to classify.",
    )
    parser.add_argument("--image-type")
    parser.add_argument("--city")
    parser.add_argument("--text-type")
    parser.add_argument("--title-type")
    parser.add_argument(
        "--clear",
        action="append",
        choices=_CLEAR_CHOICES,
        default=[],
        metavar="DIMENSION",
        help=(
            "Explicitly clear one label; repeat as needed. Choices: "
            + ", ".join(_CLEAR_CHOICES)
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    labels = {
        dimension: getattr(args, dimension)
        for dimension in ANALYTICS_DIMENSIONS
        if getattr(args, dimension) is not None
    }
    clears = tuple(item.replace("-", "_") for item in args.clear)

    store = SnapshotStore(args.db)
    try:
        classification = update_classification(
            store,
            ad_id=args.ad_id,
            labels=labels,
            clears=clears,
        )
    except ValueError as exc:
        parser.error(str(exc))

    print(
        json.dumps(
            _classification_to_dict(classification),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
