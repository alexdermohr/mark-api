from __future__ import annotations

import argparse
import sys
import json
import sqlite3
from pathlib import Path

from .container_draft import container_draft_denied
from .email_import import import_kleinanzeigen_email_files
from .storage import SnapshotStore
from .storage_owner import StorageOwnerClient, StorageOwnerError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Import local RFC822 copies of Kleinanzeigen inbound-message "
            "notifications into mark-api without contacting Kleinanzeigen."
        )
    )
    backend = parser.add_mutually_exclusive_group(required=True)
    backend.add_argument(
        "--db",
        type=Path,
        help="Existing local SQLite database (offline/trusted mode).",
    )
    backend.add_argument(
        "--storage-owner-socket", type=Path,
        help="Private Unix socket of independent UID-owned SQLite service.",
    )
    parser.add_argument(
        "--storage-owner-uid", type=int,
        help="Expected independent SQLite owner UID, required with socket mode.",
    )
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="Explicitly initialize a new SQLite store; omit on normal imports.",
    )
    parser.add_argument(
        "emails",
        type=Path,
        nargs="+",
        help="Raw RFC822/.eml files supplied by the user.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if container_draft_denied():
        print("mark-api: unattested container draft cannot run Python entrypoints",
              file=sys.stderr)
        return 2
    parser = _parser()
    args = parser.parse_args(argv)

    if args.storage_owner_socket is not None:
        if args.init_db:
            parser.error("cannot initialize a database through storage owner")
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
            store = SnapshotStore(args.db, create_if_missing=args.init_db)
        except (OSError, sqlite3.Error):
            parser.error("SQLite database unavailable or not initialized")
    try:
        report = import_kleinanzeigen_email_files(store, args.emails)
    except StorageOwnerError:
        parser.error("storage owner outcome uncertain; inspect event IDs before retry")
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))

    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())