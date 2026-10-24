from __future__ import annotations

import argparse
import sys
import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from .container_draft import container_draft_denied
from .analytics import ANALYTICS_DIMENSIONS
from .domain import AdClassification
from .storage import SnapshotStore
from .storage_owner import StorageOwnerClient, StorageOwnerError


_SOURCE = "manual-cli"
_CLEAR_CHOICES = tuple(item.replace("_", "-") for item in ANALYTICS_DIMENSIONS)


def update_classification(
    store: SnapshotStore | StorageOwnerClient,
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
    # The independent owner requires an explicit operation timestamp.
    # Offline SnapshotStore retains its existing transaction-clock default.
    if observed_at is None and isinstance(store, StorageOwnerClient):
        observed_at = datetime.now(timezone.utc)
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
            "Append local analytics classification labels for an ad ID "
            "already evidenced by owner snapshots or imported email events."
        ),
    )
    backend = parser.add_mutually_exclusive_group(required=True)
    backend.add_argument(
        "--db", type=Path,
        help="Existing local SQLite database (offline/trusted mode).",
    )
    backend.add_argument(
        "--storage-owner-socket", type=Path,
        help="Private Unix socket of the independent SQLite owner.",
    )
    parser.add_argument(
        "--storage-owner-uid", type=int,
        help="Expected SQLite-owner UID, required with --storage-owner-socket.",
    )
    parser.add_argument(
        "--ad-id",
        required=True,
        help=(
            "Locally evidenced ad ID to classify; owner inventory and "
            "imported Kleinanzeigen email events are accepted evidence."
        ),
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
    if container_draft_denied():
        print("mark-api: unattested container draft cannot run Python entrypoints",
              file=sys.stderr)
        return 2
    parser = _parser()
    args = parser.parse_args(argv)

    labels = {
        dimension: getattr(args, dimension)
        for dimension in ANALYTICS_DIMENSIONS
        if getattr(args, dimension) is not None
    }
    clears = tuple(item.replace("-", "_") for item in args.clear)

    if args.storage_owner_socket is not None:
        if args.storage_owner_uid is None:
            parser.error("--storage-owner-uid is required with --storage-owner-socket")
        try:
            store = StorageOwnerClient(
                args.storage_owner_socket,
                expected_owner_uid=args.storage_owner_uid,
            )
            if not store.ready():
                raise StorageOwnerError("unready SQLite owner")
        except (StorageOwnerError, OSError, ValueError):
            parser.error("storage owner unavailable")
    else:
        if args.storage_owner_uid is not None:
            parser.error("--storage-owner-uid requires --storage-owner-socket")
        try:
            store = SnapshotStore(args.db, create_if_missing=False)
        except (OSError, sqlite3.Error):
            parser.error("SQLite database unavailable or not initialized")
    try:
        classification = update_classification(
            store,
            ad_id=args.ad_id,
            labels=labels,
            clears=clears,
        )
    except StorageOwnerError:
        parser.error("storage owner outcome uncertain; inspect exact labels before retry")
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