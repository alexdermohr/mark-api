from __future__ import annotations

import argparse
import math
import secrets
import signal
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread, current_thread
from typing import Protocol
from urllib.parse import quote

from .analytics import (
    ANALYTICS_METRICS,
    REACTION_METRICS,
    AnalyticsContract,
)
from .dashboard import DashboardWriteProxy, LoopbackDashboardServer, create_server
from .domain import AdSnapshot
from .email_import import EmailImportReport, import_kleinanzeigen_email_files
from .private_web import PrivateWebSubmitUnknownError
from .private_web_runtime import (
    PrivateWebRuntimeDependencyError,
    build_private_web_inventory_runtime,
    build_private_web_write_api_runtime,
)
from .results import ReadResult, ReadStatus
from .storage import SnapshotStore
from .write_api import WriteApiAccess, WriteCapability


_LAUNCHER_SOURCE = "private-web-product-launcher"
_SHUTDOWN_MEDIA_RECONCILE_SECONDS = 8.0


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
_EmailImporter = Callable[[SnapshotStore, tuple[Path, ...]], EmailImportReport]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _default_write_token() -> str:
    return secrets.token_urlsafe(32)


def _default_dashboard_write_token() -> str:
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
    if (
        (result.status is ReadStatus.SUCCESS_NONEMPTY and not snapshots)
        or (result.status is ReadStatus.SUCCESS_EMPTY and snapshots)
    ):
        raise ProductLauncherError("private Web inventory status contradicts its data")
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
        dashboard_write_token: str,
        startup_inventory_count: int,
        startup_persisted_count: int,
        startup_email_import_report: EmailImportReport,
    ) -> None:
        self._inventory_runtime = inventory_runtime
        self._write_runtime = write_runtime
        self._write_access = write_access
        self._server = server
        self._dashboard_write_token = dashboard_write_token
        self._startup_inventory_count = startup_inventory_count
        self._startup_persisted_count = startup_persisted_count
        self._startup_email_import_report = startup_email_import_report
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
    def dashboard_url(self) -> str:
        host, port = self.server_address
        encoded_write_token = quote(self._dashboard_write_token, safe="")
        return (
            f"http://{host}:{port}/"
            f"#write_token={encoded_write_token}"
        )

    @property
    def startup_inventory_count(self) -> int:
        return self._startup_inventory_count

    @property
    def startup_persisted_count(self) -> int:
        return self._startup_persisted_count

    @property
    def startup_email_import_report(self) -> EmailImportReport:
        return self._startup_email_import_report

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

            # An un-drained Write API may still use its caller-owned inventory
            # runtime. Preserve it until the write runtime is quiesced.
            if self._write_closed or media_unknown is not None:
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
    email_paths: tuple[Path, ...] = (),
    email_importer: _EmailImporter = import_kleinanzeigen_email_files,
    runtime_factory: _RuntimeFactory = build_private_web_inventory_runtime,
    write_runtime_factory: _WriteRuntimeFactory | None = None,
    dashboard_factory: _DashboardFactory = create_server,
    store_factory: _StoreFactory = SnapshotStore,
    initialize_db: bool = False,
    token_factory: _TokenFactory | None = None,
    dashboard_token_factory: _TokenFactory | None = None,
    clock: _Clock = _utc_now,
) -> ProductLauncherRuntime:
    """Perform one fresh owner sync, then start Write API and dashboard.

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
    try:
        normalized_email_paths = tuple(email_paths)
    except TypeError as exc:
        raise TypeError("email_paths must be iterable") from exc
    if any(not isinstance(path, Path) for path in normalized_email_paths):
        raise TypeError("email_paths must contain pathlib.Path values")
    if not callable(email_importer):
        raise TypeError("email_importer must be callable")
    if not callable(runtime_factory):
        raise TypeError("runtime_factory must be callable")
    if write_runtime_factory is not None and not callable(write_runtime_factory):
        raise TypeError("write_runtime_factory must be callable or None")
    if not callable(dashboard_factory):
        raise TypeError("dashboard_factory must be callable")
    if not callable(store_factory):
        raise TypeError("store_factory must be callable")
    if not isinstance(initialize_db, bool):
        raise TypeError("initialize_db must be bool")
    if token_factory is not None and not callable(token_factory):
        raise TypeError("token_factory must be callable or None")
    if dashboard_token_factory is not None and not callable(
        dashboard_token_factory
    ):
        raise TypeError("dashboard_token_factory must be callable or None")
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
    resolved_dashboard_token_factory = (
        _default_dashboard_write_token
        if dashboard_token_factory is None
        else dashboard_token_factory
    )

    try:
        # User-facing launches must not silently create a new empty database.
        # Custom factories remain an explicit integration/test injection seam.
        if store_factory is SnapshotStore:
            store = SnapshotStore(db_path, create_if_missing=initialize_db)
        else:
            store = store_factory(db_path)
        tracked_ids = store.tracked_ad_ids()
        attempt_id = store.begin_sync_attempt(
            source=_LAUNCHER_SOURCE,
            started_at=_validated_observed_at(clock),
        )
    except ProductLauncherError:
        raise
    except Exception:
        raise ProductLauncherError("local snapshot store startup failed") from None

    try:
        inventory_runtime = runtime_factory(
            cdp_port=cdp_port,
            timeout_seconds=timeout,
        )
    except PrivateWebRuntimeDependencyError:
        store.fail_sync_attempt(
            attempt_id, source=_LAUNCHER_SOURCE, completed_at=_validated_observed_at(clock),
            error_kind="runtime_unavailable",
        )
        raise
    except Exception:
        store.fail_sync_attempt(
            attempt_id, source=_LAUNCHER_SOURCE, completed_at=_validated_observed_at(clock),
            error_kind="runtime_unavailable",
        )
        raise ProductLauncherError("private Web runtime startup failed") from None

    server: LoopbackDashboardServer | None = None
    write_runtime: _WriteRuntime | None = None
    try:
        try:
            result = inventory_runtime.read_inventory()
        except Exception:
            store.fail_sync_attempt(
                attempt_id, source=_LAUNCHER_SOURCE, completed_at=_validated_observed_at(clock),
                error_kind="reader_exception",
            )
            raise
        try:
            snapshots = _validated_inventory(result)
        except Exception:
            if isinstance(result, ReadResult) and not result.is_success:
                store.append_inventory_result(
                    result,
                    tracked_ad_ids=tracked_ids,
                    observed_at=_validated_observed_at(clock),
                    source=_LAUNCHER_SOURCE,
                    attempt_id=attempt_id,
                    completed_at=_validated_observed_at(clock),
                )
            else:
                store.fail_sync_attempt(
                    attempt_id, source=_LAUNCHER_SOURCE, completed_at=_validated_observed_at(clock),
                    error_kind="invalid_inventory",
                )
            raise
        try:
            observed_at = _validated_observed_at(clock)
            persisted = store.append_inventory_result(
                result,
                tracked_ad_ids=tracked_ids,
                observed_at=observed_at,
                source=_LAUNCHER_SOURCE,
                attempt_id=attempt_id,
                completed_at=observed_at,
            )
        except Exception:
            # An unknown SQLite commit is never reclassified as success.
            store.fail_sync_attempt(
                attempt_id, source=_LAUNCHER_SOURCE, completed_at=_validated_observed_at(clock),
                error_kind="persistence_error",
            )
            raise
        email_report = EmailImportReport(
            parsed_files=0,
            inserted_events=0,
            duplicate_events=0,
            ad_ids=(),
        )
        if normalized_email_paths:
            try:
                email_report = email_importer(store, normalized_email_paths)
            except Exception:
                raise ProductLauncherError(
                    "local reaction email import failed"
                ) from None
            if not isinstance(email_report, EmailImportReport):
                raise ProductLauncherError(
                    "local reaction email import failed"
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
        write_host, write_actual_port = write_runtime.server_address
        dashboard_write_token = resolved_dashboard_token_factory()
        write_proxy = DashboardWriteProxy(
            host=str(write_host),
            port=int(write_actual_port),
            bearer_token=write_access.bearer_token,
            ui_token=dashboard_write_token,
            timeout_seconds=max(30.0, timeout * 12.0),
        )
        server = dashboard_factory(
            store,
            host="127.0.0.1",
            port=dashboard_port,
            analytics_contract=analytics_contract,
            write_proxy=write_proxy,
        )
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
        dashboard_write_token=dashboard_write_token,
        startup_inventory_count=len(snapshots),
        startup_persisted_count=persisted,
        startup_email_import_report=email_report,
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
        "--init-db",
        action="store_true",
        help="Explicitly initialize a new SQLite store; omit on normal starts.",
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
        "--email",
        dest="email_paths",
        action="append",
        type=Path,
        default=[],
        metavar="FILE",
        help=(
            "Import one user-provided local Kleinanzeigen RFC822/.eml "
            "notification before serving; repeat for multiple files. "
            "No mailbox or Kleinanzeigen messaging API is contacted."
        ),
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

    # SIGTERM during startup is deferred until a runtime exists.
    # While serving, unwind into the existing close/reconciliation path.
    # Further SIGTERM signals during cleanup must not interrupt it.
    shutdown_requested = False
    serving = False
    previous_sigterm_handler = signal.getsignal(signal.SIGTERM)

    def _on_sigterm(_signum: int, _frame: object) -> None:
        nonlocal shutdown_requested, serving
        shutdown_requested = True
        if serving:
            serving = False
            raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_sigterm)
    try:

        try:
            launcher = build_product_launcher(
                db_path=args.db,
                initialize_db=args.init_db,
                cdp_port=args.cdp_port,
                dashboard_port=args.dashboard_port,
                write_port=args.write_port,
                timeout_seconds=args.timeout_seconds,
                analytics_contract=contract,
                email_paths=tuple(args.email_paths),
            )
        except (ProductLauncherError, PrivateWebRuntimeDependencyError) as exc:
            print(f"mark-api-launch: {exc}", file=sys.stderr)
            return 2
        except (TypeError, ValueError):
            print("mark-api-launch: invalid startup configuration", file=sys.stderr)
            return 2

        if not shutdown_requested:
            write_host, write_port = launcher.write_server_address
            print(f"Mark dashboard: {launcher.dashboard_url}", flush=True)
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
            if args.email_paths:
                email_report = launcher.startup_email_import_report
                print(
                    "Startup reaction import: "
                    f"{email_report.parsed_files} email file(s), "
                    f"{email_report.inserted_events} new event(s), "
                    f"{email_report.duplicate_events} duplicate event(s).",
                    flush=True,
                )

        exit_code = 0
        try:
            if not shutdown_requested:
                serving = True
                try:
                    launcher.serve_forever()
                finally:
                    serving = False
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
                # On SIGTERM, an unavailable CDP peer must not hold PID 1
                # past Docker's stop deadline. The durable in-progress media
                # fence forbids blind retry after this process exits.
                if shutdown_requested:
                    reconciled: list[bool] = []

                    def _settle_before_stop() -> None:
                        try:
                            launcher.reconcile_media_submit()
                            launcher.close()
                        except Exception:
                            return
                        reconciled.append(True)

                    settle_thread = Thread(
                        target=_settle_before_stop,
                        daemon=True,
                        name="mark-media-stop-reconciliation",
                    )
                    settle_thread.start()
                    settle_thread.join(_SHUTDOWN_MEDIA_RECONCILE_SECONDS)
                    media_settled = not settle_thread.is_alive() and bool(reconciled)
                else:
                    try:
                        launcher.reconcile_media_submit()
                        launcher.close()
                    except Exception:
                        media_settled = False
                    else:
                        media_settled = True
                if not media_settled:
                    print(
                        "mark-api-launch: media submit reconciliation required",
                        file=sys.stderr,
                    )
                    exit_code = 2
            except ProductLauncherError as exc:
                print(f"mark-api-launch: {exc}", file=sys.stderr)
                exit_code = 2
        return exit_code

    finally:
        signal.signal(signal.SIGTERM, previous_sigterm_handler)

if __name__ == "__main__":
    raise SystemExit(main())