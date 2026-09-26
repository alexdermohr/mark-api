from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path

from .analytics import ANALYTICS_DIMENSIONS
from .domain import AdClassification
from .storage import SnapshotStore


_SOURCE = "manual-cli"
_CLEAR_CHOICES = tuple(item.replace("_", "-") for item in ANALYTICS_DIMENSIONS)


def update_classification(
    store: SnapshotStore,
    *,
    ad_id: str,
    labels: Mapping[str, str] | None = None,
    clears: Iterable[str] = (),
    observed_at: datetime | None = None,
) -> AdClassification:
    """Atomically merge one local classification update."""

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

    changes: dict[str, str | None] = dict(normalized_labels)
    changes.update({dimension: None for dimension in normalized_clears})
    return store.merge_classification(
        ad_id=ad_id,
        source=_SOURCE,
        changes=changes,
        observed_at=observed_at,
    )


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
