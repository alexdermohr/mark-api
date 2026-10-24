from __future__ import annotations

from .container_draft import container_draft_denied

import argparse
import math
import os
import secrets
import signal
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from time import monotonic
from typing import Protocol
from urllib.parse import quote

from .analytics import (
    ANALYTICS_METRICS,
    REACTION_METRICS,
    AnalyticsContract,
)
from .dashboard import DashboardGenerationAccess, DashboardWriteProxy, LoopbackDashboardServer, create_server
from .domain import AdSnapshot
from .dashboard_relay import UnixDashboardRelay, DashboardRelayError
from .email_import import EmailImportReport, import_kleinanzeigen_email_files
from .fence_witness import FenceWitnessClient, FenceWitnessError
from .private_web import PrivateWebSubmitUnknownError
from .private_web_cdp import PrivateWebCdpError, _require_private_web_cdp_authority
from .private_web_runtime import (
    PrivateWebRuntimeDependencyError,
    require_private_web_runtime_dependency,
    build_private_web_inventory_runtime,
    build_private_web_write_api_runtime,
)
from .results import ReadResult, ReadStatus
from .storage import SnapshotStore
from .write_api import WriteApiAccess, WriteCapability, _WriteFence


_LAUNCHER_SOURCE = "private-web-product-launcher"
# The default factory is not an entitlement; admission is unconditional.
_DEFAULT_INVENTORY_RUNTIME = build_private_web_inventory_runtime
_SHUTDOWN_MEDIA_RECONCILE_SECONDS = 25.0
# Owner Docker stop_grace_period is 60s. The entire cleanup,
# including frontend relay, browser/media and borrowed witness, must stay
# below that deadline. A timeout is UNKNOWN, never permission to retry writes.
_SHUTDOWN_OWNER_BUDGET_SECONDS = 50.0
# Programmatic ProductLauncherRuntime.close() also needs a bounded caller wait.
# Its background worker, not the caller, keeps SQLite/inventory/Witness users alive.
_PRODUCT_CLOSE_TOTAL_SECONDS = 12.0


class ProductLauncherError(RuntimeError):
    """The user-facing product launcher could not start or stop safely."""


def _require_private_web_authorization() -> None:
    """Deprecated compatibility hook: no blanket external approval is a product gate.

    Browser authentication, loopback target validation and all owner/confirmation,
    idempotency and reconciliation checks remain at their technical boundaries.
    No credentials or external platform permission are fabricated here.
    """
    return None


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
    """Own the default-on write runtime, dashboard and startup owner inventory."""

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
        writes_enabled: bool,
        generation_token: str | None = None,
        public_origin_port: int | None = None,
    ) -> None:
        self._inventory_runtime = inventory_runtime
        self._write_runtime = write_runtime
        self._write_access = write_access
        self._server = server
        self._dashboard_write_token = dashboard_write_token
        self._generation_token = generation_token
        self._writes_enabled = writes_enabled
        self._public_origin_port = public_origin_port
        self._startup_inventory_count = startup_inventory_count
        self._startup_persisted_count = startup_persisted_count
        self._startup_email_import_report = startup_email_import_report
        self._state_lock = Lock()
        self._close_lock = Lock()
        self._close_worker_guard = Lock()
        self._close_worker: Thread | None = None
        self._close_worker_result: dict[str, BaseException] | None = None
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
        if self._public_origin_port is not None:
            port = self._public_origin_port
        base_url = f"http://{host}:{port}/"
        tokens: list[str] = []
        if self._writes_enabled:
            tokens.append("write_token=" + quote(self._dashboard_write_token, safe=""))
        if self._generation_token is not None:
            tokens.append("generation_token=" + quote(self._generation_token, safe=""))
        return base_url + ("#" + "&".join(tokens) if tokens else "")

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

    @property
    def writes_enabled(self) -> bool:
        return self._writes_enabled

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
        """Bound direct callers while one retained worker completes safe teardown."""
        deadline = monotonic() + _PRODUCT_CLOSE_TOTAL_SECONDS
        # No new admissions once a close is requested, even if another
        # worker holds _state_lock or blocks within a cleanup callback.
        self._shutdown_started = True
        if self._serve_thread is current_thread():
            raise ProductLauncherError(
                "product launcher cannot close from serving thread"
            )
        if not self._close_worker_guard.acquire(
            timeout=_PRODUCT_CLOSE_TOTAL_SECONDS,
        ):
            raise ProductLauncherError(
                "product launcher shutdown deadline exceeded; retain recovery fences"
            )
        try:
            worker = self._close_worker
            outcome = self._close_worker_result
            if worker is None or not worker.is_alive():
                if (
                    self._server_closed
                    and self._write_closed
                    and self._inventory_closed
                ):
                    return
                outcome = {}

                def finish_shutdown() -> None:
                    try:
                        self._close_impl()
                    except BaseException as exc:
                        outcome["error"] = exc

                worker = Thread(
                    target=finish_shutdown,
                    daemon=True,
                    name="mark-product-close",
                )
                self._close_worker = worker
                self._close_worker_result = outcome
                try:
                    worker.start()
                except RuntimeError as exc:
                    self._close_worker = None
                    raise ProductLauncherError(
                        "product launcher shutdown worker unavailable; "
                        "retain recovery fences"
                    ) from exc
        finally:
            self._close_worker_guard.release()

        worker.join(timeout=max(0.0, deadline - monotonic()))
        if worker.is_alive():
            raise ProductLauncherError(
                "product launcher shutdown deadline exceeded; retain recovery fences"
            )
        if outcome is not None and "error" in outcome:
            raise outcome["error"]

    def _close_impl(self) -> None:
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
    runtime_factory: _RuntimeFactory = _DEFAULT_INVENTORY_RUNTIME,
    write_runtime_factory: _WriteRuntimeFactory | None = None,
    fence_witness: _WriteFence | None = None,
    public_origin_port: int | None = None,
    dashboard_factory: _DashboardFactory = create_server,
    store_factory: _StoreFactory = SnapshotStore,
    initialize_db: bool = False,
    enable_writes: bool = True,
    generation_access: DashboardGenerationAccess | None = None,
    token_factory: _TokenFactory | None = None,
    dashboard_token_factory: _TokenFactory | None = None,
    clock: _Clock = _utc_now,
) -> ProductLauncherRuntime:
    """Perform one fresh owner sync, then start Write API and dashboard.

    Authentication and browser lifecycle remain caller-owned. This launcher
    consumes only an already-running loopback CDP endpoint. Product writes are
    composed by default through the existing authenticated/idempotent Write API.
    An explicitly supplied independent witness is borrowed for the entire
    runtime lifetime; the caller owns and closes its connection after shutdown.
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
    if public_origin_port is not None:
        _validated_port(
            public_origin_port, name="public_origin_port", allow_zero=False,
        )
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
    if not isinstance(enable_writes, bool):
        raise TypeError("enable_writes must be bool")
    if generation_access is not None and not isinstance(generation_access, DashboardGenerationAccess):
        raise TypeError("generation_access must be DashboardGenerationAccess or None")
    if token_factory is not None and not callable(token_factory):
        raise TypeError("token_factory must be callable or None")
    if dashboard_token_factory is not None and not callable(
        dashboard_token_factory
    ):
        raise TypeError("dashboard_token_factory must be callable or None")
    if not callable(clock):
        raise TypeError("clock must be callable")
    # A new store must not be created just to discover missing browser extras.
    # Explicit synthetic factories remain independent of those dependencies.
    if initialize_db and runtime_factory is _DEFAULT_INVENTORY_RUNTIME:
        require_private_web_runtime_dependency()
    # Reuse the existing guarded browser and Write API; no blanket release gate.
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
            require_no_unresolved=True,
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
            capabilities=frozenset(WriteCapability) if enable_writes else frozenset(),
            writes_enabled=enable_writes,
        )
        write_runtime = resolved_write_runtime_factory(
            cdp_port=cdp_port,
            store=store,
            access=write_access,
            core_writes_enabled=enable_writes,
            media_writes_enabled=enable_writes,
            port=write_port,
            timeout_seconds=timeout,
            clock=clock,
            **({"fence_witness": fence_witness} if fence_witness is not None else {}),
        )
        write_runtime.start()
        write_host, write_actual_port = write_runtime.server_address
        dashboard_write_token = resolved_dashboard_token_factory()
        write_proxy = (
            DashboardWriteProxy(
                host=str(write_host),
                port=int(write_actual_port),
                bearer_token=write_access.bearer_token,
                ui_token=dashboard_write_token,
                timeout_seconds=max(30.0, timeout * 12.0),
                backend_principal=write_access.principal,
                local_reconciliation_enabled=fence_witness is None,
            )
            if enable_writes else None
        )
        dashboard_options: dict[str, object] = {
            "host": "127.0.0.1",
            "port": dashboard_port,
            "analytics_contract": analytics_contract,
            "write_proxy": write_proxy,
        }
        if generation_access is not None:
            dashboard_options["generation_access"] = generation_access
        if public_origin_port is not None:
            dashboard_options["public_origin_port"] = public_origin_port
        server = dashboard_factory(store, **dashboard_options)
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
        writes_enabled=enable_writes,
        generation_token=generation_access.ui_token if generation_access else None,
        public_origin_port=public_origin_port,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Kleinanzeiger: Anzeigen verwalten, synchronisieren und auswerten. "
            "Nutzt eine bestehende, vom Nutzer authentifizierte lokale Browser-Sitzung. "
            "Write-Funktionen sind im Produkt standardmäßig verfügbar."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help="Path to the persistent Kleinanzeiger SQLite database.",
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
        "--enable-writes",
        action="store_true",
        default=True,
        help="Backwards-compatible option; owner-scoped Write UI is enabled by default.",
    )
    parser.add_argument(
        "--read-only",
        dest="enable_writes",
        action="store_false",
        help="Explicitly start a read-only dashboard without the Write UI.",
    )
    parser.add_argument(
        "--enable-generation",
        action="store_true",
        help="Enable guarded text/image draft generation in this product dashboard; every provider call still requires individual cost consent.",
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
    parser.add_argument(
        "--witness-socket", type=Path, default=None,
        help="Authenticated runtime socket of an independent recovery witness.",
    )
    parser.add_argument(
        "--witness-owner-uid", type=int, default=None,
        help="Distinct numeric owner UID, required together with --witness-socket.",
    )
    parser.add_argument(
        "--frontend-socket", type=Path, default=None,
        help="Owner-only dashboard Unix socket; only a distinct frontend UID can connect.",
    )
    parser.add_argument(
        "--frontend-uid", type=int, default=None,
        help="Distinct numeric frontend peer UID (requires --frontend-socket).",
    )
    parser.add_argument(
        "--frontend-gid", type=int, default=None,
        help="Frontend socket traversal GID (requires --frontend-socket).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if container_draft_denied():
        print("mark-api: unattested container draft cannot run Python entrypoints",
              file=sys.stderr)
        return 2
    args = _parser().parse_args(argv)
    generation_access = None
    if args.enable_generation:
        if not os.environ.get("OPENAI_API_KEY"):
            print("kleinanzeiger: --enable-generation requires OPENAI_API_KEY", file=sys.stderr)
            return 2
        try:
            from openai import OpenAI as _GenerationSdkClient
        except Exception:
            print("kleinanzeiger: --enable-generation requires kleinanzeiger[generation]", file=sys.stderr)
            return 2
        if not callable(_GenerationSdkClient):
            print("kleinanzeiger: generation SDK unavailable", file=sys.stderr)
            return 2
        generation_access = DashboardGenerationAccess(ui_token=secrets.token_urlsafe(32))
    if (
        (args.witness_socket is None) != (args.witness_owner_uid is None)
        or (args.witness_owner_uid is not None
            and args.witness_owner_uid == os.geteuid())
    ):
        print("mark-api-launch: invalid independent witness configuration", file=sys.stderr)
        return 2
    frontend_settings = (
        args.frontend_socket, args.frontend_uid, args.frontend_gid,
    )
    if any(value is not None for value in frontend_settings) and (
        not all(value is not None for value in frontend_settings)
        or args.witness_socket is None
        or not args.frontend_socket.is_absolute()
        or args.frontend_uid < 0
        or args.frontend_gid < 0
        or args.frontend_uid in (0, os.geteuid(), args.witness_owner_uid)
        or args.frontend_gid in (0, os.getegid())
    ):
        print("mark-api-launch: independent frontend configuration invalid",
              file=sys.stderr)
        return 2
    if args.frontend_socket is not None:
        # A raw HTTP relay is never allowed to forward to the Chrome CDP
        # endpoint (or to the Write API by accident). The installed owner
        # deliberately uses three separate fixed ports.
        ports = (args.cdp_port, args.dashboard_port, args.write_port, 8875)
        if any(type(port) is not int or not 1 <= port <= 65535 for port in ports) \
                or len(set(ports)) != len(ports):
            print("mark-api-launch: invalid independent frontend ports",
                  file=sys.stderr)
            return 2
    if args.frontend_socket is not None:
        # Durable external volumes may retain this pathname after SIGKILL.
        # Refuse a known stale inode BEFORE authenticating the witness or
        # opening SQLite/CDP. Do not unlink: offline inode/process attestation
        # is required. The later relay bind remains the final race check.
        try:
            args.frontend_socket.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            print("mark-api-launch: trusted dashboard socket unavailable",
                  file=sys.stderr)
            return 2
        else:
            print("mark-api-launch: trusted dashboard socket unavailable",
                  file=sys.stderr)
            return 2
    contract = AnalyticsContract(
        reaction_metric=args.reaction_metric,
        objective_metric=args.objective_metric,
    )

    # The main thread owns signals. A single daemon startup worker owns
    # Witness IPC, inventory/CDP startup and optional frontend binding until
    # their handoff is complete. On SIGTERM the caller may time out without
    # closing any resource that this worker may still use.
    shutdown_requested = False
    shutdown_deadline: float | None = None
    serving = False
    previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
    previous_sigint_handler = signal.getsignal(signal.SIGINT)
    witness: FenceWitnessClient | None = None
    frontend_relay: UnixDashboardRelay | None = None
    settle_thread: Thread | None = None
    shutdown_handed_off = False
    startup_unresolved = False
    startup_cancelled = Event()
    startup_complete = Event()
    startup_guard = Lock()
    startup_result: dict[str, object] = {}
    startup_abandoned = False

    def _on_sigterm(_signum: int, _frame: object) -> None:
        nonlocal shutdown_requested, serving, shutdown_deadline
        if not shutdown_requested:
            shutdown_deadline = monotonic() + _SHUTDOWN_OWNER_BUDGET_SECONDS
        shutdown_requested = True
        startup_cancelled.set()
        if serving:
            serving = False
            raise KeyboardInterrupt

    def _start_owner() -> None:
        # Startup may be stuck in a foreign extension or remote IPC. No other
        # thread may close borrowed Witness while this worker is unresolved.
        local_witness: FenceWitnessClient | None = None
        local_launcher: ProductLauncherRuntime | None = None
        local_relay: UnixDashboardRelay | None = None
        frontend_failed = False
        error: str | None = None
        builder_entered = False
        abandoned = False
        try:
            if args.witness_socket is not None:
                try:
                    local_witness = FenceWitnessClient(
                        args.witness_socket,
                        expected_owner_uid=args.witness_owner_uid,
                    )
                except (FenceWitnessError, OSError):
                    error = "mark-api-launch: independent recovery witness unavailable"
            if error is None and not startup_cancelled.is_set():
                builder_entered = True
                try:
                    local_launcher = build_product_launcher(
                        db_path=args.db,
                        initialize_db=args.init_db,
                        cdp_port=args.cdp_port,
                        dashboard_port=args.dashboard_port,
                        write_port=args.write_port,
                        timeout_seconds=args.timeout_seconds,
                        analytics_contract=contract,
                        email_paths=tuple(args.email_paths),
                        enable_writes=args.enable_writes,
                        generation_access=generation_access,
                        **(
                            {"fence_witness": local_witness}
                            if local_witness is not None else {}
                        ),
                        **(
                            {"public_origin_port": 8875}
                            if args.frontend_socket is not None else {}
                        ),
                    )
                except (ProductLauncherError, PrivateWebRuntimeDependencyError) as exc:
                    error = f"mark-api-launch: {exc}"
                except (TypeError, ValueError):
                    error = "mark-api-launch: invalid startup configuration"
                except Exception:
                    error = "mark-api-launch: product startup failed"
            elif error is None:
                error = "mark-api-launch: startup interrupted"

            if local_launcher is not None and args.frontend_socket is not None and not startup_cancelled.is_set():
                try:
                    dashboard_host, dashboard_port = local_launcher.server_address
                    local_relay = UnixDashboardRelay(
                        socket_path=args.frontend_socket,
                        frontend_uid=args.frontend_uid,
                        frontend_gid=args.frontend_gid,
                        dashboard_host=dashboard_host,
                        dashboard_port=dashboard_port,
                    )
                    local_relay.start()
                except (OSError, DashboardRelayError, ValueError):
                    frontend_failed = True
        except BaseException:
            # A partial startup has unknown provenance; no exception grants
            # authority to retry writes, release leases or close IPC early.
            error = "mark-api-launch: product startup failed"
        finally:
            with startup_guard:
                abandoned = startup_abandoned
                if not abandoned:
                    startup_result.update(
                        witness=local_witness,
                        launcher=local_launcher,
                        relay=local_relay,
                        frontend_failed=frontend_failed,
                        error=error,
                        startup_unresolved=builder_entered and local_launcher is None,
                    )
                startup_complete.set()

            if abandoned:
                # No other caller has received these resources. After SIGTERM
                # timeout only this worker may unwind them; a failed close
                # leaves the borrowed Witness for kernel/process teardown.
                quiesced = True
                if local_relay is not None:
                    try:
                        if not local_relay.stop():
                            quiesced = False
                    except BaseException:
                        quiesced = False
                if local_launcher is not None:
                    try:
                        local_launcher.close()
                    except BaseException:
                        quiesced = False
                elif builder_entered:
                    # A failed/partial builder might have a live inner worker.
                    quiesced = False
                if local_witness is not None and quiesced:
                    try:
                        local_witness.close()
                    except Exception:
                        pass

    signal.signal(signal.SIGTERM, _on_sigterm)
    signal.signal(signal.SIGINT, _on_sigterm)
    try:
        startup_worker = Thread(
            target=_start_owner, daemon=True, name="mark-owner-start",
        )
        try:
            startup_worker.start()
        except RuntimeError:
            print("mark-api-launch: owner startup worker unavailable", file=sys.stderr)
            return 2

        while not startup_complete.is_set():
            if shutdown_requested and shutdown_deadline is not None:
                remaining = shutdown_deadline - monotonic()
                if remaining <= 0:
                    with startup_guard:
                        if not startup_complete.is_set():
                            startup_abandoned = True
                            print(
                                "mark-api-launch: owner startup deadline exceeded; "
                                "retain recovery fences",
                                file=sys.stderr,
                            )
                            return 2
                    continue
                startup_complete.wait(timeout=min(remaining, 0.05))
            else:
                startup_complete.wait(timeout=0.05)

        # Startup and resource ownership are now handed to this thread.
        with startup_guard:
            startup_outcome = dict(startup_result)
        witness = startup_outcome.get("witness")
        launcher = startup_outcome.get("launcher")
        frontend_relay = startup_outcome.get("relay")
        frontend_failed = bool(startup_outcome.get("frontend_failed"))
        failure = startup_outcome.get("error")
        startup_unresolved = bool(startup_outcome.get("startup_unresolved"))
        if failure is not None:
            print(str(failure), file=sys.stderr)
            if launcher is None:
                return 2
            # An unexpected error *after* launcher construction still owns
            # Write/Media/Inventory workers and their borrowed Witness.
            # Use the bounded owner cleanup; never take the early return.
            frontend_failed = True
        if launcher is None:
            print("mark-api-launch: owner startup incomplete", file=sys.stderr)
            return 2
        if frontend_failed:
            print("mark-api-launch: trusted dashboard transport unavailable",
                  file=sys.stderr)

        exit_code = 2 if frontend_failed else 0
        try:
            if not shutdown_requested and not frontend_failed:
                write_host, write_port = launcher.write_server_address
                print(f"Kleinanzeiger dashboard: {launcher.dashboard_url}", flush=True)
                print(
                    f"Kleinanzeiger write API: http://{write_host}:{write_port}/api/write/",
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
            if not shutdown_requested and not frontend_failed:
                serving = True
                try:
                    # A SIGTERM between the outer condition and serving=True
                    # sets shutdown_requested while serving is still False.
                    # Recheck after arming the handler. After this point any
                    # SIGTERM raises KeyboardInterrupt and cannot enter serve.
                    if not shutdown_requested:
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
            # One absolute shutdown deadline covers relay, browser cleanup,
            # media settlement and the borrowed witness. In particular,
            # neither a hung relay.shutdown() nor content_runtime.close()
            # may consume Docker's full stop grace before SIGKILL.
            outcomes: list[int] = []

            def _stop_owner() -> None:
                nonlocal settle_thread
                stop_code = exit_code
                # A failed close() does not prove the inner shutdown worker
                # has stopped using the borrowed witness IPC.
                owner_quiesced = False
                try:
                    # Frontend streams are revoked before the write executor
                    # drains. If relay close itself hangs, PID 1 can still
                    # return fail-closed without releasing its write fences.
                    if frontend_relay is not None:
                        try:
                            relay_drained = frontend_relay.stop()
                        except Exception:
                            relay_drained = False
                        if not relay_drained:
                            print(
                                "mark-api-launch: frontend transport drain failed",
                                file=sys.stderr,
                            )
                            stop_code = 2
                    try:
                        launcher.close()
                        owner_quiesced = True
                    except PrivateWebSubmitUnknownError:
                        # Only observation-only reconciliation of the original
                        # operation is permitted. A timed-out daemon retains
                        # borrowed witness ownership until it finishes.
                        reconciled: list[bool] = []

                        def _settle_before_stop() -> None:
                            # This thread alone owns witness closure after an
                            # UNKNOWN media outcome. A failure/timeout must
                            # retain the client for any inner cleanup worker.
                            try:
                                launcher.reconcile_media_submit()
                                launcher.close()
                                if witness is not None:
                                    witness.close()
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
                        if settle_thread.is_alive() or not reconciled:
                            print(
                                "mark-api-launch: media submit reconciliation required",
                                file=sys.stderr,
                            )
                            stop_code = 2
                    except ProductLauncherError as exc:
                        print(f"mark-api-launch: {exc}", file=sys.stderr)
                        stop_code = 2
                except BaseException:
                    # Unexpected shutdown failures must never allow a clean
                    # stop claim or an implicit replay of uncertain writes.
                    stop_code = 2
                finally:
                    try:
                        # Never infer quiescence from a caller timeout/error.
                        # With media settlement, its worker owns closure and
                        # must not race a second witness.close() here.
                        if (
                            witness is not None
                            and settle_thread is None
                            and owner_quiesced
                        ):
                            witness.close()
                    except Exception:
                        stop_code = 2
                    outcomes.append(stop_code)

            # No caller may close the borrowed Witness once the live
            # launcher's shutdown needs a worker. Thread allocation/start
            # failure is NOT proof of owner quiescence; preserve the
            # independent fence until process/kernel teardown.
            shutdown_handed_off = True
            try:
                worker = Thread(
                    target=_stop_owner, daemon=True, name="mark-owner-stop",
                )
                worker.start()
            except (RuntimeError, OSError):
                print(
                    "mark-api-launch: owner shutdown worker unavailable; "
                    "retain recovery fences",
                    file=sys.stderr,
                )
                exit_code = 2
            else:
                # SIGTERM consumed some of the global budget during startup.
                # The final close worker receives only the remaining duration.
                worker.join(
                    max(0.0, shutdown_deadline - monotonic())
                    if shutdown_deadline is not None
                    else _SHUTDOWN_OWNER_BUDGET_SECONDS
                )
                if worker.is_alive() or not outcomes:
                    print(
                        "mark-api-launch: owner shutdown deadline exceeded; "
                        "retain recovery fences",
                        file=sys.stderr,
                    )
                    exit_code = 2
                else:
                    exit_code = outcomes[0]
        return exit_code

    finally:
        try:
            # Startup failures, before a shutdown worker owns the borrowed
            # session, still close the authenticated witness here. Once the
            # worker starts it alone owns that lifetime. If Docker kills PID 1,
            # the kernel closes any remaining sockets without fabricating
            # recovery clearance.
            if (
                witness is not None
                and not shutdown_handed_off
                and not startup_unresolved
            ):
                witness.close()
        finally:
            try:
                signal.signal(signal.SIGINT, previous_sigint_handler)
            finally:
                signal.signal(signal.SIGTERM, previous_sigterm_handler)

if __name__ == "__main__":
    raise SystemExit(main())
