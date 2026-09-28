from __future__ import annotations

import argparse
import json
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from .dashboard import create_server
from .email_import import import_kleinanzeigen_email_files
from .storage import SnapshotStore


_HTTP_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class LocalSmokeReport:
    parsed_files: int
    inserted_events: int
    duplicate_events: int
    ad_ids: tuple[str, ...]
    dashboard_health_ok: bool
    email_reaction_ads: int
    email_conversation_total: int
    email_inbound_message_total: int
    analytics_ranked_ads: int
    http_write_methods_rejected: bool
    write_route_absent: bool
    platform_writes_enabled: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "parsed_files": self.parsed_files,
            "inserted_events": self.inserted_events,
            "duplicate_events": self.duplicate_events,
            "ad_ids": list(self.ad_ids),
            "dashboard_health_ok": self.dashboard_health_ok,
            "email_reaction_ads": self.email_reaction_ads,
            "email_conversation_total": self.email_conversation_total,
            "email_inbound_message_total": self.email_inbound_message_total,
            "analytics_ranked_ads": self.analytics_ranked_ads,
            "http_write_methods_rejected": self.http_write_methods_rejected,
            "write_route_absent": self.write_route_absent,
            "platform_writes_enabled": self.platform_writes_enabled,
        }


def _json_get(opener, base: str, path: str) -> object:
    request = Request(base + path, method="GET")
    with opener.open(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        if response.status != 200:
            raise RuntimeError(f"unexpected dashboard status: {response.status}")
        try:
            return json.loads(response.read())
        except json.JSONDecodeError as exc:
            raise RuntimeError("dashboard returned invalid JSON") from exc


def _expect_http_error(
    opener,
    request: Request,
    *,
    expected_status: int,
) -> HTTPError:
    try:
        opener.open(request, timeout=_HTTP_TIMEOUT_SECONDS)
    except HTTPError as exc:
        if exc.code != expected_status:
            actual_status = exc.code
            exc.close()
            raise RuntimeError(
                f"unexpected dashboard error status: {actual_status}"
            ) from exc
        return exc
    raise RuntimeError(
        f"dashboard request unexpectedly succeeded; expected {expected_status}"
    )


def run_local_smoke(
    email_paths: Iterable[str | Path],
) -> LocalSmokeReport:
    """Exercise the allowed local email -> SQLite -> dashboard/analytics path.

    The SQLite database is ephemeral. The only HTTP traffic is to the
    loopback-only dashboard server created by mark-api. No platform adapter is
    constructed and no Kleinanzeigen write capability is enabled.
    """

    normalized_paths = tuple(Path(path) for path in email_paths)
    if not normalized_paths:
        raise ValueError("at least one email file is required")

    with tempfile.TemporaryDirectory(prefix="mark-api-local-smoke-") as tmp:
        store = SnapshotStore(Path(tmp) / "smoke.sqlite")
        import_report = import_kleinanzeigen_email_files(
            store,
            normalized_paths,
        )

        server = create_server(store, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        if host != "127.0.0.1":
            server.shutdown()
            server.server_close()
            thread.join(timeout=_HTTP_TIMEOUT_SECONDS)
            raise RuntimeError("smoke dashboard is not bound to loopback")

        base = f"http://127.0.0.1:{port}"
        opener = build_opener(ProxyHandler({}))
        try:
            health = _json_get(opener, base, "/healthz")
            if health != {"status": "ok"}:
                raise RuntimeError("dashboard health response is unexpected")

            reactions = _json_get(opener, base, "/api/email-reactions")
            if not isinstance(reactions, list):
                raise RuntimeError("email reaction projection is not a list")

            metrics = _json_get(opener, base, "/api/analytics/metrics")
            metric_names = (
                metrics.get("metrics")
                if isinstance(metrics, dict)
                else None
            )
            if (
                not isinstance(metric_names, list)
                or "email_conversation_count" not in metric_names
                or "email_inbound_message_count" not in metric_names
            ):
                raise RuntimeError("email analytics metrics are unavailable")

            ranking = _json_get(
                opener,
                base,
                "/api/analytics/ads?metric=email_inbound_message_count",
            )
            if not isinstance(ranking, list):
                raise RuntimeError("email analytics ranking is not a list")

            post_error = _expect_http_error(
                opener,
                Request(
                    base + "/api/email-reactions",
                    data=b"{}",
                    method="POST",
                ),
                expected_status=405,
            )
            post_rejected = post_error.headers.get("Allow") == "GET"
            post_error.close()
            if not post_rejected:
                raise RuntimeError("dashboard POST rejection lacks Allow: GET")

            missing_write_route = _expect_http_error(
                opener,
                Request(base + "/api/write/delete", method="GET"),
                expected_status=404,
            )
            missing_write_route.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=_HTTP_TIMEOUT_SECONDS)
            if thread.is_alive():
                raise RuntimeError("dashboard smoke server did not stop")

        expected_ids = set(import_report.ad_ids)
        reaction_ids = {
            str(item.get("ad_id"))
            for item in reactions
            if isinstance(item, dict)
        }
        ranking_ids = {
            str(item.get("ad_id"))
            for item in ranking
            if isinstance(item, dict)
        }
        if (
            len(reactions) != len(expected_ids)
            or reaction_ids != expected_ids
        ):
            raise RuntimeError("email reaction projection does not match import")
        if len(ranking) != len(expected_ids) or ranking_ids != expected_ids:
            raise RuntimeError("email analytics ranking does not match import")

        try:
            reaction_message_counts = {
                str(item["ad_id"]): int(item["inbound_message_count"])
                for item in reactions
            }
            ranking_message_counts = {
                str(item["ad_id"]): int(item["value"])
                for item in ranking
            }
            conversation_total = sum(
                int(item["conversation_count"]) for item in reactions
            )
            message_total = sum(reaction_message_counts.values())
            ranking_total = sum(ranking_message_counts.values())
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("dashboard email metrics are malformed") from exc

        if reaction_message_counts != ranking_message_counts:
            raise RuntimeError(
                "email analytics per-ad message counts are inconsistent"
            )
        if message_total != import_report.inserted_events:
            raise RuntimeError("email projection message total is inconsistent")
        if ranking_total != message_total:
            raise RuntimeError("email analytics total is inconsistent")

        return LocalSmokeReport(
            parsed_files=import_report.parsed_files,
            inserted_events=import_report.inserted_events,
            duplicate_events=import_report.duplicate_events,
            ad_ids=import_report.ad_ids,
            dashboard_health_ok=True,
            email_reaction_ads=len(reactions),
            email_conversation_total=conversation_total,
            email_inbound_message_total=message_total,
            analytics_ranked_ads=len(ranking),
            http_write_methods_rejected=post_rejected,
            write_route_absent=True,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a local end-to-end smoke over user-provided Kleinanzeigen "
            "RFC822/.eml copies using an ephemeral SQLite database and the "
            "loopback-only read dashboard. No platform write is performed."
        )
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
    try:
        report = run_local_smoke(args.emails)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.error(str(exc))

    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
