from __future__ import annotations

import argparse
import math
import secrets
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread, current_thread
from typing import Protocol

from .analytics import (
    ANALYTICS_METRICS,
    REACTION_METRICS,
    AnalyticsContract,
)
from .dashboard import LoopbackDashboardServer, create_server
from .domain import AdSnapshot
from .private_web import PrivateWebSubmitUnknownError
from .private_web_runtime import (
    PrivateWebRuntimeDependencyError,
    build_private_web_inventory_runtime,
    build_private_web_write_api_runtime,
)
from .results import ReadResult
from .storage import SnapshotStore
from .write_api import WriteApiAccess, WriteCapability


_LAUNCHER_SOURCE = "private-web-product-launcher"


class ProductLauncherError(RuntimeError):
    """The user-facing product launcher could not start or stop safely."""


class _InventoryRuntime(Protocol):
    def read_inventory(self) -> ReadResult[tuple[AdSnapshot, ...]]:
        ...

    def close(self) -> None:
        ...


class _WriteRuntime(Protocol):
    @property
    def server_address(self) -> tuple[str, int]:
        ...

    def start(self) -> tuple[str, int]:
        ...

    def reconcile_media_submit(self) -> None:
        ...

    def close(self) -> None:
        ...


_RuntimeFactory = Callable[..., _InventoryRuntime]
_WriteRuntimeFactory = Callable[..., _WriteRuntime]
_DashboardFactory = Callable[..., LoopbackDashboardServer]
_StoreFactory = Callable[[Path], SnapshotStore]
_Clock = Callable[[], datetime]
_TokenFactory = Callable[[], str]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _default_write_token() -> str:
    return secrets.token_urlsafe(32)


def _validated_port(value: int, *, name: str, allow_zero: bool) -> int:
    minimum = 0 if allow_zero else 1
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= 65535
    ):
        qualifier = "0..65535" if allow_zero else "1..65535"
        raise ValueError(f"{name} must be an integer in {qualifier}")
    return value


def _validated_timeout(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError("timeout_seconds must be a positive finite number")
    return float(value)


def _validated_observed_at(clock: _Clock) -> datetime:
    observed_at = clock()
    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise ProductLauncherError("product launcher clock is not timezone-aware")
    return observed_at


def _validated_inventory(
    result: ReadResult[tuple[AdSnapshot, ...]],
) -> tuple[AdSnapshot, ...]:
    if not isinstance(result, ReadResult):
        raise ProductLauncherError("private Web inventory result is invalid")
    if not result.is_success:
        raise ProductLauncherError(
            f"private Web inventory read failed: {result.status.value}"
        )

    snapshots = tuple(result.value or ())
    if any(not isinstance(item, AdSnapshot) for item in snapshots):
        raise ProductLauncherError(
            "private Web inventory contains invalid snapshots"
        )
    ids = [item.ad_id for item in snapshots]
    if len(set(ids)) != len(ids):
        raise ProductLauncherError(
            "private Web inventory contains duplicate ad IDs"
        )
    return snapshots


class ProductLauncherRuntime:
    """Own the dashboard, default-on Write API, and startup inventory runtime."""

    def __init__(
        self,
        *,
        inventory_runtime: _InventoryRuntime,
        write_runtime: _WriteRuntime,
        write_access: WriteApiAccess,
        server: LoopbackDashboardServer,
        startup_inventory_count: int,
        startup_persisted_count: int,
    ) -> None:
        self._inventory_runtime = inventory_runtime
        self._write_runtime = write_runtime
        self._write_access = write_access
        self._server = server
        self._startup_inventory_count = startup_inventory_count
        self._startup_persisted_count = startup_persisted_count
        self._state_lock = Lock()
        self._close_lock = Lock()
        self._shutdown_started = False
        self._serve_thread: Thread | None = None
        self._server_quiesced = False
        self._server_closed = False
        self._write_closed = False
        self._inventory_closed = False
        self._inventory_close_failed = False

    def _ensure_open_locked(self) -> None:
        if self._shutdown_started:
            raise ProductLauncherError("product launcher is closed")

    def _ensure_open(self) -> None:
        with self._state_lock:
            self._ensure_open_locked()

    @property
    def server_address(self) -> tuple[str, int]:
        host, port = self._server.server_address
        return str(host), int(port)

    @property
    def startup_inventory_count(self) -> int:
        return self._startup_inventory_count

    @property
    def startup_persisted_count(self) -> int:
        return self._startup_persisted_count

    @property
    def write_server_address(self) -> tuple[str, int]:
        host, port = self._write_runtime.server_address
        return str(host), int(port)

    @property
    def write_bearer_token(self) -> str:
        return self._write_access.bearer_token

    def reconcile_media_submit(self) -> None:
        with self._state_lock:
            if self._write_closed:
                raise ProductLauncherError("product launcher write runtime is closed")
        self._write_runtime.reconcile_media_submit()

    def serve_forever(self) -> None:
        with self._state_lock:
            self._ensure_open_locked()
            if self._serve_thread is not None:
                raise ProductLauncherError("product launcher is already serving")
            self._serve_thread = current_thread()
            self._server_quiesced = False

        try:
            self._server.serve_forever()
        finally:
            with self._state_lock:
                if self._serve_thread is current_thread():
                    self._serve_thread = None
                    self._server_quiesced = True

    def close(self) -> None:
        with self._close_lock:
            with self._state_lock:
                if (
                    self._server_closed
                    and self._write_closed
                    and self._inventory_closed
                ):
                    return
                self._shutdown_started = True
                serve_thread = self._serve_thread
                server_quiesced = self._server_quiesced or serve_thread is None
                if serve_thread is current_thread():
                    raise ProductLauncherError(
                        "product launcher cannot close from serving thread"
                    )
                if server_quiesced:
                    self._server_quiesced = True

            cleanup_failed = False
            if not server_quiesced:
                try:
                    self._server.shutdown()
                except Exception:
                    with self._state_lock:
                        server_quiesced = self._server_quiesced
                    if not server_quiesced:
                        cleanup_failed = True
                else:
                    server_quiesced = True
                    with self._state_lock:
                        self._server_quiesced = True

            if not server_quiesced:
                raise ProductLauncherError("product launcher cleanup failed")

            if not self._server_closed:
                try:
                    self._server.server_close()
                except Exception:
                    cleanup_failed = True
                else:
                    self._server_closed = True

            media_unknown: PrivateWebSubmitUnknownError | None = None
            if not self._write_closed:
                try:
                    self._write_runtime.close()
                except PrivateWebSubmitUnknownError as exc:
                    media_unknown = exc
                except Exception:
                    cleanup_failed = True
                else:
                    self._write_closed = True

            if self._inventory_close_failed:
                cleanup_failed = True
            elif not self._inventory_closed:
                try:
                    self._inventory_runtime.close()
                except Exception:
                    self._inventory_close_failed = True
                    cleanup_failed = True
                else:
                    self._inventory_closed = True

            if media_unknown is not None:
                raise media_unknown
            if cleanup_failed:
                raise ProductLauncherError("product launcher cleanup failed")

    def __enter__(self) -> "ProductLauncherRuntime":
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def build_product_launcher(
    *,
    db_path: Path,
    cdp_port: int,
    dashboard_port: int = 8765,
    write_port: int = 8766,
    timeout_seconds: float = 5.0,
    analytics_contract: AnalyticsContract | None = None,
    runtime_factory: _RuntimeFactory = build_private_web_inventory_runtime,
    write_runtime_factory: _WriteRuntimeFactory | None = None,
    dashboard_factory: _DashboardFactory = create_server,
    store_factory: _StoreFactory = SnapshotStore,
    token_factory: _TokenFactory | None = None,
    clock: _Clock = _utc_now,
) -> ProductLauncherRuntime:
    """Perform one fresh owner sync, then start dashboard and Write API.

    Authentication and browser lifecycle remain caller-owned. This launcher
    consumes only an already-running loopback CDP endpoint. Product writes are
    composed by default through the existing authenticated/idempotent Write API.
    """

    if not isinstance(db_path, Path):
        raise TypeError("db_path must be pathlib.Path")
    _validated_port(cdp_port, name="cdp_port", allow_zero=False)
    _validated_port(
        dashboard_port,
        name="dashboard_port",
        allow_zero=True,
    )
    _validated_port(write_port, name="write_port", allow_zero=True)
    timeout = _validated_timeout(timeout_seconds)
    if analytics_contract is not None and not isinstance(
        analytics_contract,
        AnalyticsContract,
    ):
        raise TypeError("analytics_contract must be AnalyticsContract or None")
    if not callable(runtime_factory):
        raise TypeError("runtime_factory must be callable")
    if write_runtime_factory is not None and not callable(write_runtime_factory):
        raise TypeError("write_runtime_factory must be callable or None")
    if not callable(dashboard_factory):
        raise TypeError("dashboard_factory must be callable")
    if not callable(store_factory):
        raise TypeError("store_factory must be callable")
    if token_factory is not None and not callable(token_factory):
        raise TypeError("token_factory must be callable or None")
    if not callable(clock):
        raise TypeError("clock must be callable")

    resolved_write_runtime_factory = (
        build_private_web_write_api_runtime
        if write_runtime_factory is None
        else write_runtime_factory
    )
    resolved_token_factory = (
        _default_write_token if token_factory is None else token_factory
    )

    try:
        store = store_factory(db_path)
        tracked_ids = store.tracked_ad_ids()
    except Exception:
        raise ProductLauncherError("local snapshot store startup failed") from None

    try:
        inventory_runtime = runtime_factory(
            cdp_port=cdp_port,
            timeout_seconds=timeout,
        )
    except PrivateWebRuntimeDependencyError:
        raise
    except Exception:
        raise ProductLauncherError("private Web runtime startup failed") from None

    server: LoopbackDashboardServer | None = None
    write_runtime: _WriteRuntime | None = None
    try:
        result = inventory_runtime.read_inventory()
        snapshots = _validated_inventory(result)
        observed_at = _validated_observed_at(clock)
        persisted = store.append_inventory_result(
            result,
            tracked_ad_ids=tracked_ids,
            observed_at=observed_at,
            source=_LAUNCHER_SOURCE,
        )
        server = dashboard_factory(
            store,
            host="127.0.0.1",
            port=dashboard_port,
            analytics_contract=analytics_contract,
        )
        write_access = WriteApiAccess(
            principal="mark-api-launch",
            bearer_token=resolved_token_factory(),
            capabilities=frozenset(WriteCapability),
            writes_enabled=True,
        )
        write_runtime = resolved_write_runtime_factory(
            cdp_port=cdp_port,
            store=store,
            access=write_access,
            core_writes_enabled=True,
            media_writes_enabled=True,
            port=write_port,
            timeout_seconds=timeout,
            clock=clock,
        )
        write_runtime.start()
    except ProductLauncherError:
        if write_runtime is not None:
            try:
                write_runtime.close()
            except Exception:
                pass
        if server is not None:
            try:
                server.server_close()
            except Exception:
                pass
        try:
            inventory_runtime.close()
        except Exception:
            pass
        raise
    except PrivateWebRuntimeDependencyError:
        if write_runtime is not None:
            try:
                write_runtime.close()
            except Exception:
                pass
        if server is not None:
            try:
                server.server_close()
            except Exception:
                pass
        try:
            inventory_runtime.close()
        except Exception:
            pass
        raise
    except Exception:
        if write_runtime is not None:
            try:
                write_runtime.close()
            except Exception:
                pass
        if server is not None:
            try:
                server.server_close()
            except Exception:
                pass
        try:
            inventory_runtime.close()
        except Exception:
            pass
        raise ProductLauncherError("product launcher startup failed") from None

    return ProductLauncherRuntime(
        inventory_runtime=inventory_runtime,
        write_runtime=write_runtime,
        write_access=write_access,
        server=server,
        startup_inventory_count=len(snapshots),
        startup_persisted_count=persisted,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Start Mark from an already-authenticated local Chrome/CDP session, "
            "perform one fresh owner-inventory sync, and serve the dashboard "
            "plus default-on loopback Write API."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help="Path to the persistent mark-api SQLite database.",
    )
    parser.add_argument(
        "--cdp-port",
        type=int,
        required=True,
        help="Loopback Chrome DevTools port for the already-authenticated browser.",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8765,
        help="Loopback dashboard port (default: 8765; use 0 for an ephemeral port).",
    )
    parser.add_argument(
        "--write-port",
        type=int,
        default=8766,
        help="Loopback Write API port (default: 8766; use 0 for an ephemeral port).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=5.0,
        help="Private-Web setup/read timeout in seconds (default: 5).",
    )
    parser.add_argument(
        "--reaction-metric",
        choices=REACTION_METRICS,
        default=None,
        help=(
            "Explicit interpretation of 'wie viele geschrieben haben'; "
            "unset by default."
        ),
    )
    parser.add_argument(
        "--objective-metric",
        choices=ANALYTICS_METRICS,
        default=None,
        help=(
            "Explicit analytics objective used to preselect rankings; "
            "unset by default."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    contract = AnalyticsContract(
        reaction_metric=args.reaction_metric,
        objective_metric=args.objective_metric,
    )

    try:
        launcher = build_product_launcher(
            db_path=args.db,
            cdp_port=args.cdp_port,
            dashboard_port=args.dashboard_port,
            write_port=args.write_port,
            timeout_seconds=args.timeout_seconds,
            analytics_contract=contract,
        )
    except (ProductLauncherError, PrivateWebRuntimeDependencyError) as exc:
        print(f"mark-api-launch: {exc}", file=sys.stderr)
        return 2
    except (TypeError, ValueError):
        print("mark-api-launch: invalid startup configuration", file=sys.stderr)
        return 2

    host, port = launcher.server_address
    write_host, write_port = launcher.write_server_address
    print(f"Mark dashboard: http://{host}:{port}/", flush=True)
    print(
        f"Mark write API: http://{write_host}:{write_port}/api/write/",
        flush=True,
    )
    print(
        f"Mark write bearer token: {launcher.write_bearer_token}",
        flush=True,
    )
    print(
        "Startup sync: "
        f"{launcher.startup_inventory_count} current ad(s), "
        f"{launcher.startup_persisted_count} observation(s) persisted.",
        flush=True,
    )

    exit_code = 0
    try:
        launcher.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception:
        print(
            "mark-api-launch: dashboard server stopped unexpectedly",
            file=sys.stderr,
        )
        exit_code = 2
    finally:
        try:
            launcher.close()
        except PrivateWebSubmitUnknownError:
            try:
                launcher.reconcile_media_submit()
                launcher.close()
            except Exception:
                print(
                    "mark-api-launch: media submit reconciliation required",
                    file=sys.stderr,
                )
                exit_code = 2
        except ProductLauncherError as exc:
            print(f"mark-api-launch: {exc}", file=sys.stderr)
            exit_code = 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())