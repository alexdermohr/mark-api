from __future__ import annotations

import argparse
import json
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from .dashboard import create_server
from .domain import AdSnapshot, LifecycleState
from .private_web_runtime import build_private_web_inventory_runtime
from .results import ReadResult
from .storage import SnapshotStore


_HTTP_TIMEOUT_SECONDS = 2.0


class _InventoryRuntime(Protocol):
    def read_inventory(self) -> ReadResult[tuple[AdSnapshot, ...]]:
        ...

    def close(self) -> None:
        ...


_RuntimeFactory = Callable[..., _InventoryRuntime]


@dataclass(frozen=True, slots=True)
class PrivateWebRuntimeSmokeReport:
    inventory_status: str
    inventory_count: int
    persisted_snapshots: int
    dashboard_health_ok: bool
    dashboard_tracked_ads: int
    dashboard_current_ads: int
    dashboard_ads: int
    analytics_ranked_ads: int
    http_write_methods_rejected: bool
    write_route_absent: bool
    platform_writes_enabled: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "inventory_status": self.inventory_status,
            "inventory_count": self.inventory_count,
            "persisted_snapshots": self.persisted_snapshots,
            "dashboard_health_ok": self.dashboard_health_ok,
            "dashboard_tracked_ads": self.dashboard_tracked_ads,
            "dashboard_current_ads": self.dashboard_current_ads,
            "dashboard_ads": self.dashboard_ads,
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


def _validated_inventory(
    result: ReadResult[tuple[AdSnapshot, ...]],
) -> tuple[AdSnapshot, ...]:
    if not isinstance(result, ReadResult):
        raise RuntimeError("private Web inventory result is invalid")
    if not result.is_success:
        raise RuntimeError(
            f"private Web inventory read failed: {result.status.value}"
        )

    snapshots = tuple(result.value or ())
    if any(not isinstance(item, AdSnapshot) for item in snapshots):
        raise RuntimeError("private Web inventory contains invalid snapshots")
    ids = [item.ad_id for item in snapshots]
    if len(set(ids)) != len(ids):
        raise RuntimeError("private Web inventory contains duplicate ad IDs")
    return snapshots


def run_private_web_runtime_smoke(
    cdp_port: int,
    *,
    timeout_seconds: float = 5.0,
    runtime_factory: _RuntimeFactory = build_private_web_inventory_runtime,
) -> PrivateWebRuntimeSmokeReport:
    """Exercise PrivateWeb owner-read -> SQLite -> loopback dashboard/analytics.

    The existing user-authenticated browser is consumed only through the
    read-only inventory surface. No writer method is called, no browser worker
    is started or authenticated here, and no platform write route is exposed.
    """

    if (
        isinstance(cdp_port, bool)
        or not isinstance(cdp_port, int)
        or not 1 <= cdp_port <= 65535
    ):
        raise ValueError("cdp_port must be an integer between 1 and 65535")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be positive")

    runtime = runtime_factory(
        cdp_port=cdp_port,
        timeout_seconds=float(timeout_seconds),
    )
    try:
        result = runtime.read_inventory()
        snapshots = _validated_inventory(result)

        with tempfile.TemporaryDirectory(
            prefix="mark-api-private-web-runtime-smoke-"
        ) as tmp:
            store = SnapshotStore(Path(tmp) / "smoke.sqlite")
            persisted = store.append_inventory_result(
                result,
                tracked_ad_ids=(),
                observed_at=datetime.now(timezone.utc),
                source="private-web-runtime-smoke",
            )
            if persisted != len(snapshots):
                raise RuntimeError(
                    "private Web inventory persistence count is inconsistent"
                )

            server = create_server(store, host="127.0.0.1", port=0)
            thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
            )
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
                    raise RuntimeError(
                        "dashboard health response is unexpected"
                    )

                summary = _json_get(opener, base, "/api/summary")
                if not isinstance(summary, dict):
                    raise RuntimeError("dashboard summary is not an object")

                ads = _json_get(opener, base, "/api/ads")
                if not isinstance(ads, list):
                    raise RuntimeError("dashboard ads projection is not a list")

                metrics = _json_get(opener, base, "/api/analytics/metrics")
                metric_names = (
                    metrics.get("metrics")
                    if isinstance(metrics, dict)
                    else None
                )
                if (
                    not isinstance(metric_names, list)
                    or "views" not in metric_names
                    or "watch_count" not in metric_names
                    or "reply_count" not in metric_names
                ):
                    raise RuntimeError("dashboard analytics metrics are unavailable")

                ranking = _json_get(
                    opener,
                    base,
                    "/api/analytics/ads?metric=views",
                )
                if not isinstance(ranking, list):
                    raise RuntimeError("dashboard analytics ranking is not a list")

                write_methods_rejected = True
                for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
                    request = Request(
                        base + "/api/ads",
                        data=(b"{}" if method in {"POST", "PUT", "PATCH"} else None),
                        method=method,
                    )
                    method_error = _expect_http_error(
                        opener,
                        request,
                        expected_status=405,
                    )
                    allowed = method_error.headers.get("Allow")
                    method_error.close()
                    if allowed != "GET":
                        write_methods_rejected = False
                        raise RuntimeError(
                            f"dashboard {method} rejection lacks Allow: GET"
                        )

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

            metric_values = {
                field_name: [
                    value
                    for item in snapshots
                    if (value := getattr(item, field_name)) is not None
                ]
                for field_name in ("views", "watch_count", "reply_count")
            }
            expected_summary = {
                "tracked_ads": len(snapshots),
                "current_ads": sum(
                    item.lifecycle_state is not LifecycleState.ABSENT
                    for item in snapshots
                ),
                "absent_ads": sum(
                    item.lifecycle_state is LifecycleState.ABSENT
                    for item in snapshots
                ),
                "unknown_state_ads": sum(
                    item.lifecycle_state is LifecycleState.UNKNOWN
                    for item in snapshots
                ),
                "views_total_known": sum(metric_values["views"]),
                "views_observed_ads": len(metric_values["views"]),
                "watch_total_known": sum(metric_values["watch_count"]),
                "watch_observed_ads": len(metric_values["watch_count"]),
                "replies_total_known": sum(metric_values["reply_count"]),
                "replies_observed_ads": len(metric_values["reply_count"]),
            }
            expected_ads = {
                item.ad_id: {
                    "ad_id": item.ad_id,
                    "lifecycle_state": item.lifecycle_state.value,
                    "present": item.lifecycle_state is not LifecycleState.ABSENT,
                    "observed_at": item.observed_at.isoformat(),
                    "source": item.source,
                    "title": item.title,
                    "description": item.description,
                    "views": item.views,
                    "watch_count": item.watch_count,
                    "reply_count": item.reply_count,
                }
                for item in snapshots
            }
            expected_views_ranking = sorted(
                (
                    (item.ad_id, item.views)
                    for item in snapshots
                    if item.views is not None
                ),
                key=lambda pair: (-pair[1], pair[0]),
            )
            try:
                projected_ads = {
                    str(item["ad_id"]): {
                        "ad_id": str(item["ad_id"]),
                        "lifecycle_state": item["lifecycle_state"],
                        "present": item["present"],
                        "observed_at": item["observed_at"],
                        "source": item["source"],
                        "title": item["title"],
                        "description": item["description"],
                        "views": item["views"],
                        "watch_count": item["watch_count"],
                        "reply_count": item["reply_count"],
                    }
                    for item in ads
                    if isinstance(item, dict)
                }
                ranked_views: list[tuple[str, int]] = []
                for item in ranking:
                    if (
                        not isinstance(item, dict)
                        or item.get("metric") != "views"
                    ):
                        raise TypeError("invalid analytics ranking row")
                    value = item.get("value")
                    if (
                        not isinstance(value, int)
                        or isinstance(value, bool)
                    ):
                        raise TypeError("invalid analytics ranking value")
                    ranked_views.append((str(item["ad_id"]), value))
                projected_summary = {
                    key: summary[key]
                    for key in expected_summary
                }
                if (
                    set(summary) != set(expected_summary)
                    or any(type(value) is not int for value in projected_summary.values())
                ):
                    raise TypeError("invalid dashboard summary")
                tracked_ads = projected_summary["tracked_ads"]
                current_ads = projected_summary["current_ads"]
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    "dashboard inventory projections are malformed"
                ) from exc

            if (
                len(ads) != len(snapshots)
                or len(projected_ads) != len(ads)
                or projected_ads != expected_ads
            ):
                raise RuntimeError(
                    "dashboard ads projection does not match inventory"
                )
            if projected_summary != expected_summary:
                raise RuntimeError(
                    "dashboard summary does not match inventory"
                )
            if (
                len(ranking) != len(expected_views_ranking)
                or len(ranked_views) != len(ranking)
                or ranked_views != expected_views_ranking
            ):
                raise RuntimeError(
                    "dashboard analytics views ranking does not match inventory"
                )

            return PrivateWebRuntimeSmokeReport(
                inventory_status=result.status.value,
                inventory_count=len(snapshots),
                persisted_snapshots=persisted,
                dashboard_health_ok=True,
                dashboard_tracked_ads=tracked_ads,
                dashboard_current_ads=current_ads,
                dashboard_ads=len(ads),
                analytics_ranked_ads=len(ranking),
                http_write_methods_rejected=write_methods_rejected,
                write_route_absent=True,
            )
    finally:
        runtime.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a read-only PrivateWeb runtime smoke against an existing "
            "user-authenticated loopback CDP browser. The smoke persists the "
            "owner inventory only to an ephemeral SQLite database and exposes "
            "only the loopback read-only dashboard during the run."
        ),
    )
    parser.add_argument(
        "--cdp-port",
        type=int,
        required=True,
        help="Loopback CDP port of the existing user-authenticated browser.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=5.0,
        help="PrivateWeb CDP timeout in seconds (default: 5).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        report = run_private_web_runtime_smoke(
            args.cdp_port,
            timeout_seconds=args.timeout_seconds,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.error(str(exc))

    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
