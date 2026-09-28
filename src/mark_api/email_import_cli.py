from __future__ import annotations

import argparse
import json
from pathlib import Path

from .email_import import import_kleinanzeigen_email_files
from .storage import SnapshotStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Import local RFC822 copies of Kleinanzeigen inbound-message "
            "notifications into mark-api without contacting Kleinanzeigen."
        )
    )
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help="Path to the local mark-api SQLite database.",
    )
    parser.add_argument(
        "emails",
        type=Path,
        nargs="+",
        help="Raw RFC822/.eml files supplied by the user.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    store = SnapshotStore(args.db)
    try:
        report = import_kleinanzeigen_email_files(store, args.emails)
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))

    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
