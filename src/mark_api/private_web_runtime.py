from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from importlib import import_module, metadata
from threading import Lock, RLock, Thread, current_thread
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .application import MarkService
from .adapters.management import (
    DEFAULT_SOURCE as MANAGEMENT_SOURCE,
    MANAGEMENT_URL,
    HttpResponse,
    ManagementReadAdapter,
    TransportFailure,
)
from .domain import (
    AdCreateRequest,
    AdSnapshot,
    CreateOperationReceipt,
    DeleteApproval,
    LifecycleState,
    MediaPostReadStatus,
    OperationOutcome,
    OperationReceipt,
)
from .orchestrator import SafeWriteOrchestrator
from .ports import (
    AdContentUpdater,
    AdCreateWriter,
    AdDeleteWriter,
    AdStateWriter,
    AdsReader,
    WriteNotAttemptedError,
)
from .private_web import (
    PrivateWebContentWriter,
    PrivateWebCreatePage,
    PrivateWebCreateWriter,
    PrivateWebDeletePage,
    PrivateWebDeleteWriter,
    PrivateWebPage,
    PrivateWebStatePage,
    PrivateWebStateWriter,
    PrivateWebSubmitUnknownError,
    PrivateWebWriteNotAttemptedError,
)
from .results import ReadResult, ReadStatus
from .private_web_cdp import (
    CdpCookieProvider,
    CdpPrivateWebOwnerReader,
    CdpPrivateWebPage,
)
from .private_web_cdp_media import CdpPrivateWebMediaPage
from .private_web_media_verify import (
    PrivateWebPublicMediaPersistenceVerifier,
)
from .private_web_media import (
    PrivateWebCreateMediaPublishPage,
    PrivateWebCreateMediaWriter,
    PrivateWebMediaPersistenceSnapshot,
    PrivateWebMediaPersistenceVerifier,
    PrivateWebMediaHandleStore,
    PrivateWebMediaRefResolver,
    PrivateWebMediaSource,
    _prepare_local_media,
)
from .storage import SnapshotStore
from .write_api import (
    LoopbackWriteApiServer,
    WriteApiAccess,
    WriteCapability,
    _WriteApiStoreLock,
    acquire_write_api_store_lock,
    create_write_api_server,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


_MEDIA_PERSISTENCE_VERIFY_TIMEOUT_SECONDS = 10.0


def _pending_dashboard_media_refs(store: SnapshotStore) -> frozenset[str]:
    refs: set[str] = set()
    for record in store.dashboard_pending_writes():
        if (
            record.resource_key != "create"
            or record.path != "/api/write/media/ads"
        ):
            continue
        if record.payload_json is None:
            raise RuntimeError("pending media recovery state is invalid")
        try:
            payload = json.loads(record.payload_json)
        except json.JSONDecodeError:
            raise RuntimeError("pending media recovery state is invalid") from None
        raw_refs = payload.get("media_refs") if isinstance(payload, dict) else None
        if (
            not isinstance(raw_refs, list)
            or not raw_refs
            or len(raw_refs) > 32
            or any(not isinstance(ref, str) for ref in raw_refs)
            or len(set(raw_refs)) != len(raw_refs)
        ):
            raise RuntimeError("pending media recovery state is invalid")
        refs.update(raw_refs)
    return frozenset(refs)


class PrivateWebRuntimeDependencyError(RuntimeError):
    """The optional private-Web runtime dependency set is unavailable."""


class PrivateWebRuntimeClosedError(WriteNotAttemptedError):
    """The private-Web runtime bundle has already been closed before a write."""


class PrivateWebRuntimeSetupError(WriteNotAttemptedError):
    """Per-call private-Web page setup failed before any platform write."""


class _RejectManagementRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _NoRedirectManagementTransport:
    """HTTP transport that never forwards browser cookies through redirects."""

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        self._timeout_seconds = timeout_seconds
        self._open = build_opener(
            ProxyHandler({}),
            _RejectManagementRedirectHandler(),
        ).open

    def get(self, url: str, *, headers: dict[str, str]) -> HttpResponse:
        request = Request(url, headers=headers, method="GET")
        try:
            with self._open(request, timeout=self._timeout_seconds) as response:
                return HttpResponse(
                    status_code=int(response.status),
                    body=response.read(),
                )
        except HTTPError as exc:
            return HttpResponse(status_code=int(exc.code), body=exc.read())
        except (URLError, TimeoutError, OSError) as exc:
            raise TransportFailure(type(exc).__name__) from exc


class _CloseablePrivateWebPage(
    PrivateWebPage,
    PrivateWebCreatePage,
    PrivateWebStatePage,
    PrivateWebDeletePage,
    Protocol,
):
    def close(self) -> None:
        ...


class _CloseablePrivateWebMediaPage(
    PrivateWebCreateMediaPublishPage,
    Protocol,
):
    def reconcile_create_media_submit(self) -> None:
        ...

    def close(self) -> None:
        ...


class _RuntimeCookieProvider:
    def __init__(self, delegate: CdpCookieProvider) -> None:
        self._delegate = delegate
        self._closed = False

    def __call__(self) -> str | None:
        if self._closed:
            raise PrivateWebRuntimeClosedError("private Web runtime is closed")
        return self._delegate()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._delegate.close()


class _PerCallPrivateWebContentWriter:
    """Use one fresh browser-page adapter for each explicit content write."""

    def __init__(
        self,
        *,
        page_factory: Callable[[], _CloseablePrivateWebPage],
        ensure_open: Callable[[], None],
    ) -> None:
        self._page_factory = page_factory
        self._ensure_open = ensure_open

    def update_content(
        self,
        ad_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        self._ensure_open()
        try:
            page = self._page_factory()
        except WriteNotAttemptedError:
            raise
        except Exception:
            raise PrivateWebRuntimeSetupError("private Web page setup failed") from None
        try:
            PrivateWebContentWriter(page).update_content(
                ad_id,
                title=title,
                description=description,
            )
        finally:
            try:
                page.close()
            except Exception:
                # Cleanup must not replace the stage-sanitized writer outcome.
                pass


class _PerCallPrivateWebCreateWriter:
    """Use one fresh browser-page adapter for each explicit create write."""

    def __init__(
        self,
        *,
        page_factory: Callable[[], _CloseablePrivateWebPage],
        ensure_open: Callable[[], None],
    ) -> None:
        self._page_factory = page_factory
        self._ensure_open = ensure_open

    def create_ad(self, request: AdCreateRequest) -> None:
        self._ensure_open()
        try:
            page = self._page_factory()
        except WriteNotAttemptedError:
            raise
        except Exception:
            raise PrivateWebRuntimeSetupError(
                "private Web page setup failed"
            ) from None
        try:
            PrivateWebCreateWriter(page).create_ad(request)
        finally:
            try:
                page.close()
            except Exception:
                # Cleanup must not replace a classified writer outcome.
                pass


class _PerCallPrivateWebStateWriter:
    """Use one fresh browser-page adapter for each explicit lifecycle write."""

    def __init__(
        self,
        *,
        page_factory: Callable[[], _CloseablePrivateWebPage],
        ensure_open: Callable[[], None],
    ) -> None:
        self._page_factory = page_factory
        self._ensure_open = ensure_open

    def set_state(self, ad_id: str, state: LifecycleState) -> None:
        self._ensure_open()
        try:
            page = self._page_factory()
        except WriteNotAttemptedError:
            raise
        except Exception:
            raise PrivateWebRuntimeSetupError("private Web page setup failed") from None
        try:
            PrivateWebStateWriter(page).set_state(ad_id, state)
        finally:
            try:
                page.close()
            except Exception:
                # Cleanup must not replace a classified writer outcome.
                pass


class _PerCallPrivateWebDeleteWriter:
    """Use one fresh browser-page adapter for each explicit delete write."""

    def __init__(
        self,
        *,
        page_factory: Callable[[], _CloseablePrivateWebPage],
        ensure_open: Callable[[], None],
    ) -> None:
        self._page_factory = page_factory
        self._ensure_open = ensure_open

    def delete_ad(self, ad_id: str) -> None:
        self._ensure_open()
        try:
            page = self._page_factory()
        except WriteNotAttemptedError:
            raise
        except Exception:
            raise PrivateWebRuntimeSetupError("private Web page setup failed") from None
        try:
            PrivateWebDeleteWriter(page).delete_ad(ad_id)
        finally:
            try:
                page.close()
            except Exception:
                # Cleanup must not replace a classified writer outcome.
                pass


class PrivateWebMediaCreateRuntime:
    """Own one serialized media-create state machine for an existing CDP worker.

    A submit that remains UNKNOWN is retained as a live page instead of being
    dropped by per-call cleanup. All create, reconciliation, and close
    transitions are serialized so at most one page/writer operation can own
    this runtime at a time. Reconciliation observes only the existing submit
    state and never repeats browser input.
    """

    def __init__(
        self,
        *,
        page_factory: Callable[[], _CloseablePrivateWebMediaPage],
    ) -> None:
        self._page_factory = page_factory
        self._pending_page: _CloseablePrivateWebMediaPage | None = None
        self._submit_unknown_fenced = False
        self._closed = False
        self._operation_lock = Lock()

    def _ensure_open(self) -> None:
        if self._closed:
            raise PrivateWebRuntimeClosedError(
                "private Web media runtime is closed"
            )

    def _ensure_create_available(self) -> None:
        if self._pending_page is not None:
            raise PrivateWebRuntimeSetupError(
                "private Web media submit requires reconciliation"
            )
        if self._submit_unknown_fenced:
            raise PrivateWebRuntimeSetupError(
                "private Web media submit outcome forbids retry"
            )

    @property
    def reconciliation_required(self) -> bool:
        """Whether one unresolved submit page still requires explicit observation."""

        with self._operation_lock:
            return self._pending_page is not None

    def _close_after_writer_outcome(
        self,
        page: _CloseablePrivateWebMediaPage,
    ) -> None:
        try:
            page.close()
        except PrivateWebSubmitUnknownError as exc:
            if exc.stage == "create_media_submit_unsettled":
                self._pending_page = page
                raise
            # Any other cleanup classification must not replace the already
            # classified writer outcome.
        except Exception:
            # Local cleanup failure must not replace the writer outcome.
            pass

    def _create_ad_locked(
        self,
        request: AdCreateRequest,
        sources: tuple[PrivateWebMediaSource, ...],
    ) -> None:
        self._ensure_open()
        self._ensure_create_available()
        try:
            page = self._page_factory()
        except WriteNotAttemptedError:
            raise
        except Exception:
            raise PrivateWebRuntimeSetupError(
                "private Web media page setup failed"
            ) from None

        try:
            PrivateWebCreateMediaWriter(page).create_ad(request, sources)
        except PrivateWebSubmitUnknownError:
            # Every media-submit UNKNOWN is explicitly non-retryable. Keep a
            # runtime-level fence even if the page itself is already settled
            # and can be closed, such as writer-owned local cleanup failure.
            self._submit_unknown_fenced = True
            try:
                self._close_after_writer_outcome(page)
            except PrivateWebSubmitUnknownError:
                pass
            raise
        except Exception:
            try:
                self._close_after_writer_outcome(page)
            except PrivateWebSubmitUnknownError:
                pass
            raise
        except BaseException:
            # Cancellation-style exits can arrive after browser input without
            # passing through an Exception subclass. Conservatively fence all
            # retries and let page.close() retain an unsettled submit page, but
            # never replace the original cancellation with cleanup outcome.
            self._submit_unknown_fenced = True
            try:
                self._close_after_writer_outcome(page)
            except PrivateWebSubmitUnknownError:
                pass
            raise

        # A successful media writer has already observed canonical settlement.
        # If close nevertheless reports an unsettled submit, preserve the page
        # and surface UNKNOWN rather than dropping its media lifetime.
        try:
            self._close_after_writer_outcome(page)
        except PrivateWebSubmitUnknownError:
            self._submit_unknown_fenced = True
            raise

    def create_ad(
        self,
        request: AdCreateRequest,
        sources: tuple[PrivateWebMediaSource, ...],
    ) -> None:
        with self._operation_lock:
            self._create_ad_locked(request, sources)

    def bind_create_writer(
        self,
        request: AdCreateRequest,
        sources: tuple[PrivateWebMediaSource, ...],
    ) -> AdCreateWriter:
        """Bind one explicit create request and media tuple to a one-shot writer."""
        return _BoundPrivateWebMediaCreateWriter(
            runtime=self,
            request=request,
            sources=sources,
        )

    def _reconcile_media_submit_locked(self) -> None:
        self._ensure_open()
        page = self._pending_page
        if page is None:
            raise PrivateWebRuntimeSetupError(
                "private Web media submit has no pending reconciliation"
            )

        page.reconcile_create_media_submit()
        self._pending_page = None
        self._submit_unknown_fenced = False
        try:
            page.close()
        except Exception:
            # Settlement is already confirmed. Local cleanup failure must not
            # manufacture platform retry authority.
            pass

    def reconcile_media_submit(self) -> None:
        """Observe unresolved browser submit state without repeating browser input."""

        with self._operation_lock:
            self._reconcile_media_submit_locked()

    def _close_locked(self) -> None:
        if self._closed:
            return
        if self._pending_page is not None:
            raise PrivateWebSubmitUnknownError(
                "media_runtime_close_unsettled"
            )
        self._closed = True

    def close(self) -> None:
        with self._operation_lock:
            self._close_locked()

    def __enter__(self) -> "PrivateWebMediaCreateRuntime":
        with self._operation_lock:
            self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        with self._operation_lock:
            if exc_type is not None and self._pending_page is not None:
                # Preserve the original submit/reconciliation classification.
                # A second close error would mask it while the runtime must keep
                # owning the unresolved page for explicit reconciliation.
                return
            self._close_locked()


class _BoundPrivateWebMediaCreateWriter:
    """Adapt one exact media-create attempt to the existing AdCreateWriter port."""

    def __init__(
        self,
        *,
        runtime: PrivateWebMediaCreateRuntime,
        request: AdCreateRequest,
        sources: tuple[PrivateWebMediaSource, ...],
    ) -> None:
        if not isinstance(request, AdCreateRequest):
            raise TypeError("media create writer request must be AdCreateRequest")
        if not isinstance(sources, tuple):
            raise TypeError("media sources must be a tuple")
        if not sources:
            raise ValueError("media create writer requires at least one source")
        if any(
            not isinstance(source, PrivateWebMediaSource)
            for source in sources
        ):
            raise TypeError("media sources must contain PrivateWebMediaSource")
        self._runtime = runtime
        self._request = request
        self._sources = sources
        self._call_lock = Lock()
        self._used = False

    def create_ad(self, request: AdCreateRequest) -> None:
        with self._call_lock:
            if self._used:
                raise PrivateWebRuntimeSetupError(
                    "private Web media create writer is already used"
                )
            if not isinstance(request, AdCreateRequest) or request != self._request:
                raise PrivateWebRuntimeSetupError(
                    "private Web media create writer request mismatch"
                )
            # This adapter represents one explicit logical create attempt.
            # Never manufacture retry authority from a later runtime outcome.
            self._used = True
        self._runtime.create_ad(request, self._sources)


class PrivateWebMediaCreateService:
    """Bind opaque media refs to one serialized safe media-create attempt."""

    def __init__(
        self,
        *,
        runtime: PrivateWebMediaCreateRuntime,
        resolver: PrivateWebMediaRefResolver,
        reader: AdsReader,
        confirmation_reader: AdsReader,
        content_reader_factory: Callable[[str], AdsReader],
        media_persistence_verifier: PrivateWebMediaPersistenceVerifier | None = None,
        store: SnapshotStore | None = None,
        writes_enabled: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(runtime, PrivateWebMediaCreateRuntime):
            raise TypeError("runtime must be PrivateWebMediaCreateRuntime")
        if not isinstance(resolver, PrivateWebMediaRefResolver):
            raise TypeError("resolver must be PrivateWebMediaRefResolver")
        if not isinstance(writes_enabled, bool):
            raise TypeError("writes_enabled must be bool")
        if (
            media_persistence_verifier is not None
            and not callable(
                getattr(media_persistence_verifier, "verify_media", None)
            )
        ):
            raise TypeError(
                "media_persistence_verifier must provide verify_media"
            )
        self._runtime = runtime
        self._resolver = resolver
        self._reader = reader
        self._confirmation_reader = confirmation_reader
        self._content_reader_factory = content_reader_factory
        self._media_persistence_verifier = media_persistence_verifier
        self._store = store
        self._clock = _utc_now if clock is None else clock
        self._writes_enabled = writes_enabled
        self._operation_lock = Lock()
        # Media receipts are persisted only after the media-aware post-read has
        # been classified. Persisting inside the generic orchestrator would
        # otherwise record a narrower content-only receipt first.
        self._writes = SafeWriteOrchestrator(
            store=None,
            writes_enabled=writes_enabled,
            clock=self._clock,
        )

    def _persist_create_receipt(
        self,
        receipt: CreateOperationReceipt,
    ) -> CreateOperationReceipt:
        if self._store is not None:
            self._store.append_create_operation_receipt(receipt)
        return receipt

    def _with_media_post_read(
        self,
        receipt: CreateOperationReceipt,
        stable_sources: tuple[PrivateWebMediaSource, ...] | None = None,
    ) -> CreateOperationReceipt:
        if (
            receipt.outcome is not OperationOutcome.CONFIRMED
            or receipt.created_ad_id is None
        ):
            status = MediaPostReadStatus.NOT_READ
        elif self._media_persistence_verifier is None:
            status = MediaPostReadStatus.VERIFIER_UNAVAILABLE
        elif stable_sources is None:
            status = MediaPostReadStatus.UNKNOWN
        else:
            try:
                result = self._media_persistence_verifier.verify_media(
                    receipt.created_ad_id,
                    stable_sources,
                    timeout_seconds=_MEDIA_PERSISTENCE_VERIFY_TIMEOUT_SECONDS,
                )
            except Exception:
                status = MediaPostReadStatus.UNKNOWN
            else:
                if (
                    not isinstance(result, ReadResult)
                    or result.status is not ReadStatus.SUCCESS_NONEMPTY
                    or not isinstance(
                        result.value,
                        PrivateWebMediaPersistenceSnapshot,
                    )
                    or result.value.ad_id != receipt.created_ad_id
                ):
                    status = MediaPostReadStatus.UNKNOWN
                elif result.value.exact_match:
                    status = MediaPostReadStatus.CONFIRMED
                else:
                    status = MediaPostReadStatus.MISMATCH

        return replace(
            receipt,
            completed_at=max(receipt.completed_at, self._clock()),
            media_post_read_status=status,
            media_persistence_confirmed=(
                status is MediaPostReadStatus.CONFIRMED
            ),
        )

    def _media_ref_precondition(
        self,
        *,
        started_at: datetime,
        authorization_by: str | None,
        authorization_reference: str | None,
    ) -> CreateOperationReceipt:
        receipt = CreateOperationReceipt(
            operation="create",
            started_at=started_at,
            completed_at=self._clock(),
            outcome=OperationOutcome.PRECONDITION_FAILED,
            pre_read_status="media_refs_unavailable",
            confirmation_pre_read_status="not_read",
            post_read_status=None,
            confirmation_post_read_status=None,
            content_post_read_status=None,
            writer_invoked=False,
            authorization_by=authorization_by,
            authorization_reference=authorization_reference,
            media_post_read_status=MediaPostReadStatus.NOT_READ,
        )
        return self._persist_create_receipt(receipt)

    def create_with_media(
        self,
        request: AdCreateRequest,
        media_refs: tuple[str, ...],
        *,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> CreateOperationReceipt:
        # The loopback HTTP server is threaded. Serialize resolution plus the
        # complete pre/write/post state machine so concurrent creates cannot
        # contaminate each other's inventory delta.
        with self._operation_lock:
            if not self._writes_enabled:
                receipt = self._writes.create(
                    request=request,
                    reader=self._reader,
                    confirmation_reader=self._confirmation_reader,
                    writer=None,
                    content_reader_factory=self._content_reader_factory,
                    authorization_by=authorization_by,
                    authorization_reference=authorization_reference,
                )
                return self._persist_create_receipt(
                    self._with_media_post_read(receipt)
                )

            started_at = self._clock()
            prepared = None
            try:
                resolved = self._resolver.resolve(media_refs)
                prepared = _prepare_local_media(resolved)
                stable_sources = tuple(
                    PrivateWebMediaSource(path=path)
                    for path in prepared.paths
                )
                discard = getattr(self._resolver, "discard", None)
                if callable(discard):
                    discard(media_refs)
            except (PrivateWebWriteNotAttemptedError, TypeError, ValueError):
                if prepared is not None:
                    try:
                        prepared.close()
                    except Exception:
                        pass
                # Resolution and stabilization happened entirely locally,
                # before owner/browser reads or any platform write.
                return self._media_ref_precondition(
                    started_at=started_at,
                    authorization_by=authorization_by,
                    authorization_reference=authorization_reference,
                )

            try:
                writer = self._runtime.bind_create_writer(
                    request,
                    stable_sources,
                )
                receipt = self._writes.create(
                    request=request,
                    reader=self._reader,
                    confirmation_reader=self._confirmation_reader,
                    writer=writer,
                    content_reader_factory=self._content_reader_factory,
                    authorization_by=authorization_by,
                    authorization_reference=authorization_reference,
                )
                if (
                    self._store is not None
                    and self._media_persistence_verifier is not None
                    and receipt.outcome is OperationOutcome.CONFIRMED
                    and receipt.created_ad_id is not None
                    and receipt.writer_invoked
                ):
                    # Commit content/create evidence before the injected
                    # authoritative verifier performs another external read.
                    # A process abort or BaseException during that read must
                    # not erase the audit trail of the platform write.
                    self._store.append_create_operation_checkpoint(receipt)
                receipt = self._with_media_post_read(
                    receipt,
                    stable_sources,
                )
                return self._persist_create_receipt(receipt)
            finally:
                try:
                    prepared.close()
                except Exception:
                    # Cleanup is local-only and must not replace the classified
                    # create outcome or manufacture platform retry authority.
                    pass


class PrivateWebInventoryRuntime:
    """Read-only owner-inventory runtime for an existing authenticated CDP worker."""

    def __init__(
        self,
        *,
        owner_reader: AdsReader,
        close_runtime: Callable[[], None],
    ) -> None:
        self._owner_reader = owner_reader
        self._close_runtime = close_runtime
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise PrivateWebRuntimeClosedError("private Web runtime is closed")

    def read_inventory(self) -> ReadResult[tuple[AdSnapshot, ...]]:
        self._ensure_open()
        return self._owner_reader.read_ads()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_runtime()

    def __enter__(self) -> "PrivateWebInventoryRuntime":
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class PrivateWebContentRuntime:
    """Compose target-bound private-Web create/content/state/delete reads and writes.

    The caller owns the browser worker/process. This bundle only consumes an
    already-running loopback CDP endpoint. It creates short-lived page adapters
    per read/write operation and owns the cookie-provider connection used by
    the dedicated management reader.
    """

    def __init__(
        self,
        *,
        owner_reader: AdsReader,
        page_factory: Callable[[], _CloseablePrivateWebPage],
        close_runtime: Callable[[], None],
    ) -> None:
        self._owner_reader = owner_reader
        self._page_factory = page_factory
        self._close_runtime = close_runtime
        self._closed = False
        self.content_writer: AdContentUpdater = _PerCallPrivateWebContentWriter(
            page_factory=page_factory,
            ensure_open=self._ensure_open,
        )
        self.create_writer: AdCreateWriter = _PerCallPrivateWebCreateWriter(
            page_factory=page_factory,
            ensure_open=self._ensure_open,
        )
        self.state_writer: AdStateWriter = _PerCallPrivateWebStateWriter(
            page_factory=page_factory,
            ensure_open=self._ensure_open,
        )
        self.delete_writer: AdDeleteWriter = _PerCallPrivateWebDeleteWriter(
            page_factory=page_factory,
            ensure_open=self._ensure_open,
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise PrivateWebRuntimeClosedError("private Web runtime is closed")

    def read_inventory(self) -> ReadResult[tuple[AdSnapshot, ...]]:
        """Read the authoritative private-Web owner inventory without a write path."""

        self._ensure_open()
        return self._owner_reader.read_ads()

    def content_reader_for(self, ad_id: str) -> AdsReader:
        self._ensure_open()
        return CdpPrivateWebOwnerReader(
            owner_reader=self._owner_reader,
            page_factory=self._page_factory,
            ad_id=ad_id,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_runtime()

    def __enter__(self) -> "PrivateWebContentRuntime":
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class _PrivateWebRuntimeAdsReader:
    """Adapt one owned PrivateWeb inventory runtime to the AdsReader port."""

    def __init__(
        self,
        runtime: PrivateWebContentRuntime | PrivateWebInventoryRuntime,
    ) -> None:
        self._runtime = runtime

    def read_ads(self) -> ReadResult[tuple[AdSnapshot, ...]]:
        return self._runtime.read_inventory()


def _ensure_no_pending_media_reconciliation(
    media_runtime: PrivateWebMediaCreateRuntime | None,
) -> None:
    """Fence every composed write while one media submit still needs observation."""

    if media_runtime is not None and media_runtime.reconciliation_required:
        raise PrivateWebRuntimeSetupError(
            "private Web media submit requires reconciliation"
        )


class _SerializedPrivateWebWriteService:
    """Serialize whole MarkService operations over one shared browser worker."""

    def __init__(
        self,
        delegate: MarkService,
        operation_lock: Lock,
        media_runtime: PrivateWebMediaCreateRuntime | None,
    ) -> None:
        self._delegate = delegate
        self._operation_lock = operation_lock
        self._media_runtime = media_runtime

    def _ensure_write_available(self) -> None:
        _ensure_no_pending_media_reconciliation(self._media_runtime)

    def create(
        self,
        request: AdCreateRequest,
        *,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> CreateOperationReceipt:
        with self._operation_lock:
            self._ensure_write_available()
            return self._delegate.create(
                request,
                authorization_by=authorization_by,
                authorization_reference=authorization_reference,
            )

    def pause(
        self,
        ad_id: str,
        *,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> OperationReceipt:
        with self._operation_lock:
            self._ensure_write_available()
            return self._delegate.pause(
                ad_id,
                authorization_by=authorization_by,
                authorization_reference=authorization_reference,
            )

    def activate(
        self,
        ad_id: str,
        *,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> OperationReceipt:
        with self._operation_lock:
            self._ensure_write_available()
            return self._delegate.activate(
                ad_id,
                authorization_by=authorization_by,
                authorization_reference=authorization_reference,
            )

    def delete(
        self,
        ad_id: str,
        *,
        approval: DeleteApproval,
    ) -> OperationReceipt:
        with self._operation_lock:
            self._ensure_write_available()
            return self._delegate.delete(ad_id, approval=approval)

    def update_content(
        self,
        ad_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> OperationReceipt:
        with self._operation_lock:
            self._ensure_write_available()
            return self._delegate.update_content(
                ad_id,
                title=title,
                description=description,
                authorization_by=authorization_by,
                authorization_reference=authorization_reference,
            )


class _SerializedPrivateWebMediaService:
    """Serialize media creates with every other PrivateWeb write operation."""

    def __init__(
        self,
        delegate: PrivateWebMediaCreateService,
        operation_lock: Lock,
        media_runtime: PrivateWebMediaCreateRuntime,
    ) -> None:
        self._delegate = delegate
        self._operation_lock = operation_lock
        self._media_runtime = media_runtime

    def create_with_media(
        self,
        request: AdCreateRequest,
        media_refs: tuple[str, ...],
        *,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> CreateOperationReceipt:
        with self._operation_lock:
            _ensure_no_pending_media_reconciliation(self._media_runtime)
            return self._delegate.create_with_media(
                request,
                media_refs,
                authorization_by=authorization_by,
                authorization_reference=authorization_reference,
            )


class _WriteOnlyReactionReader:
    """Deliberately unavailable reaction surface for the write-only composition."""

    def read_reactions(self, ad_id: str):
        del ad_id
        return ReadResult.failure(
            ReadStatus.TRANSPORT_ERROR,
            error="reaction_reader_not_composed",
        )


class PrivateWebWriteApiRuntime:
    """Own one loopback Write API plus the PrivateWeb runtimes behind it.

    The browser worker remains caller-owned. The bundle owns only the server
    and runtime objects passed at construction. A pending media submit keeps
    shutdown fail-closed and remains explicitly reconcilable after HTTP has
    been quiesced.
    """

    def __init__(
        self,
        *,
        server: LoopbackWriteApiServer,
        content_runtime: PrivateWebContentRuntime,
        confirmation_runtime: PrivateWebInventoryRuntime | None,
        media_runtime: PrivateWebMediaCreateRuntime | None,
        mark_service: MarkService,
        media_service: PrivateWebMediaCreateService | None,
        media_handle_store: PrivateWebMediaHandleStore | None,
        operation_lock: Lock,
    ) -> None:
        self._server = server
        self._content_runtime = content_runtime
        self._confirmation_runtime = confirmation_runtime
        self._media_runtime = media_runtime
        self._mark_service = mark_service
        self._media_service = media_service
        self._media_handle_store = media_handle_store
        self._operation_lock = operation_lock
        self._state_lock = Lock()
        self._close_lock = Lock()
        self._server_thread: Thread | None = None
        self._server_shutdown = False
        self._server_closed = False
        self._stopping = False
        self._closed = False
        self._runtime_cleanup_failed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise PrivateWebRuntimeClosedError(
                "private Web write API runtime is closed"
            )

    @property
    def server_address(self) -> tuple[str, int]:
        host, port = self._server.server_address
        return str(host), int(port)

    @property
    def media_reconciliation_required(self) -> bool:
        runtime = self._media_runtime
        return runtime is not None and runtime.reconciliation_required

    def start(self) -> tuple[str, int]:
        with self._state_lock:
            self._ensure_open()
            if self._stopping:
                raise PrivateWebRuntimeSetupError(
                    "private Web write API runtime shutdown is pending"
                )
            thread = self._server_thread
            if thread is not None:
                if thread.is_alive():
                    return self.server_address
                raise PrivateWebRuntimeSetupError(
                    "private Web write API server thread stopped"
                )
            thread = Thread(
                target=self._server.serve_forever,
                daemon=True,
                name="mark-private-web-write-api",
            )
            self._server_thread = thread
            thread.start()
            return self.server_address

    def reconcile_media_submit(self) -> None:
        with self._state_lock:
            self._ensure_open()
            runtime = self._media_runtime
        if runtime is None:
            raise PrivateWebRuntimeSetupError(
                "private Web media runtime is not configured"
            )
        with self._operation_lock:
            runtime.reconcile_media_submit()

    def close(self) -> None:
        with self._close_lock:
            with self._state_lock:
                if self._closed:
                    return
                thread = self._server_thread
                if thread is not None and current_thread() is thread:
                    raise PrivateWebRuntimeSetupError(
                        "private Web write API runtime cannot close from its server thread"
                    )
                self._stopping = True

            cleanup_failed = self._runtime_cleanup_failed

            if not self._server_shutdown:
                if thread is None:
                    self._server_shutdown = True
                elif thread.is_alive():
                    try:
                        self._server.shutdown()
                    except Exception:
                        cleanup_failed = True
                    thread.join(timeout=2.0)
                    if thread.is_alive():
                        cleanup_failed = True
                    else:
                        self._server_shutdown = True
                else:
                    self._server_shutdown = True

            if not self._server_shutdown:
                raise PrivateWebRuntimeSetupError(
                    "private Web write API runtime cleanup failed"
                )

            if not self._server_closed:
                try:
                    self._server.server_close()
                except Exception:
                    cleanup_failed = True
                else:
                    self._server_closed = True

            # server_close() is the request-handler drain boundary for this
            # composition. Never close browser runtimes unless that boundary
            # completed successfully.
            if not self._server_closed:
                raise PrivateWebRuntimeSetupError(
                    "private Web write API runtime cleanup failed"
                )

            media_unknown: PrivateWebSubmitUnknownError | None = None
            with self._operation_lock:
                if self._media_runtime is not None:
                    try:
                        self._media_runtime.close()
                    except PrivateWebSubmitUnknownError as exc:
                        media_unknown = exc
                    except Exception:
                        self._runtime_cleanup_failed = True
                        cleanup_failed = True

                # UNKNOWN is the only close outcome that must preserve the owned
                # browser/content state for an explicit observation-only
                # reconciliation. HTTP is already quiesced above, so no new write
                # can enter while shutdown remains pending.
                if media_unknown is not None:
                    raise media_unknown

                if self._confirmation_runtime is not None:
                    try:
                        self._confirmation_runtime.close()
                    except Exception:
                        self._runtime_cleanup_failed = True
                        cleanup_failed = True

                try:
                    self._content_runtime.close()
                except Exception:
                    self._runtime_cleanup_failed = True
                    cleanup_failed = True

                if self._media_handle_store is not None:
                    try:
                        self._media_handle_store.close()
                    except Exception:
                        self._runtime_cleanup_failed = True
                        cleanup_failed = True

            if cleanup_failed:
                raise PrivateWebRuntimeSetupError(
                    "private Web write API runtime cleanup failed"
                )

            with self._state_lock:
                self._closed = True

    def __enter__(self) -> "PrivateWebWriteApiRuntime":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def _validate_write_runtime_config(
    *,
    access: WriteApiAccess,
    core_writes_enabled: bool,
    media_writes_enabled: bool,
    media_runtime: PrivateWebMediaCreateRuntime | None,
    media_resolver: PrivateWebMediaRefResolver | None,
    media_persistence_verifier: PrivateWebMediaPersistenceVerifier | None,
) -> None:
    if not isinstance(access, WriteApiAccess):
        raise TypeError("access must be WriteApiAccess")
    if not isinstance(core_writes_enabled, bool):
        raise TypeError("core_writes_enabled must be bool")
    if not isinstance(media_writes_enabled, bool):
        raise TypeError("media_writes_enabled must be bool")
    if (media_runtime is None) != (media_resolver is None):
        raise ValueError(
            "media runtime and media resolver must be configured together"
        )
    if (
        media_persistence_verifier is not None
        and not callable(
            getattr(media_persistence_verifier, "verify_media", None)
        )
    ):
        raise TypeError(
            "media_persistence_verifier must provide verify_media"
        )

    media_capability = WriteCapability.CREATE_MEDIA in access.capabilities
    media_composed = media_runtime is not None
    if media_capability and not media_composed:
        raise ValueError(
            "create_media capability requires private Web media composition"
        )
    if media_composed and not media_capability:
        raise ValueError(
            "private Web media composition requires create_media capability"
        )
    if media_writes_enabled and not media_composed:
        raise ValueError(
            "media_writes_enabled requires private Web media composition"
        )
    if media_persistence_verifier is not None and not media_composed:
        raise ValueError(
            "media persistence verifier requires private Web media composition"
        )


def compose_private_web_write_api_runtime(
    *,
    content_runtime: PrivateWebContentRuntime,
    store: SnapshotStore,
    access: WriteApiAccess,
    confirmation_runtime: PrivateWebInventoryRuntime | None = None,
    media_runtime: PrivateWebMediaCreateRuntime | None = None,
    media_resolver: PrivateWebMediaRefResolver | None = None,
    media_persistence_verifier: PrivateWebMediaPersistenceVerifier | None = None,
    core_writes_enabled: bool = False,
    media_writes_enabled: bool = False,
    port: int = 0,
    clock: Callable[[], datetime] | None = None,
    _store_lock: _WriteApiStoreLock | None = None,
) -> PrivateWebWriteApiRuntime:
    """Compose already-built PrivateWeb runtimes behind one loopback Write API.

    Ownership transfers to the returned bundle only after composition succeeds.
    The HTTP gate, MarkService gate, and media-service gate remain independent.
    """

    if not isinstance(content_runtime, PrivateWebContentRuntime):
        raise TypeError("content_runtime must be PrivateWebContentRuntime")
    if (
        confirmation_runtime is not None
        and not isinstance(confirmation_runtime, PrivateWebInventoryRuntime)
    ):
        raise TypeError(
            "confirmation_runtime must be PrivateWebInventoryRuntime or None"
        )
    if not isinstance(store, SnapshotStore):
        raise TypeError("store must be SnapshotStore")
    _validate_write_runtime_config(
        access=access,
        core_writes_enabled=core_writes_enabled,
        media_writes_enabled=media_writes_enabled,
        media_runtime=media_runtime,
        media_resolver=media_resolver,
        media_persistence_verifier=media_persistence_verifier,
    )
    runtime_clock = _utc_now if clock is None else clock
    if not callable(runtime_clock):
        raise TypeError("clock must be callable")

    owner_reader = _PrivateWebRuntimeAdsReader(content_runtime)
    # Without a separate inventory runtime, reuse the authoritative owner
    # inventory for temporally distinct observations. Create still requires
    # the separate target-bound content/detail read; Delete requires a second
    # fresh absence observation after exactly one submit.
    confirmation_reader = (
        owner_reader
        if confirmation_runtime is None
        else _PrivateWebRuntimeAdsReader(confirmation_runtime)
    )
    mark_service = MarkService(
        owner_reader=owner_reader,
        management_reader=owner_reader,
        delete_confirmation_reader=confirmation_reader,
        reaction_reader=_WriteOnlyReactionReader(),
        state_writer=content_runtime.state_writer,
        delete_writer=content_runtime.delete_writer,
        content_writer=content_runtime.content_writer,
        create_writer=content_runtime.create_writer,
        content_reader_factory=content_runtime.content_reader_for,
        store=store,
        writes_enabled=core_writes_enabled,
        clock=runtime_clock,
    )

    media_service = None
    if media_runtime is not None:
        assert media_resolver is not None
        media_service = PrivateWebMediaCreateService(
            runtime=media_runtime,
            resolver=media_resolver,
            reader=owner_reader,
            confirmation_reader=confirmation_reader,
            content_reader_factory=content_runtime.content_reader_for,
            media_persistence_verifier=media_persistence_verifier,
            store=store,
            writes_enabled=media_writes_enabled,
            clock=runtime_clock,
        )

    operation_lock = RLock()
    serialized_mark_service = _SerializedPrivateWebWriteService(
        mark_service,
        operation_lock,
        media_runtime,
    )
    serialized_media_service = (
        None
        if media_service is None
        else _SerializedPrivateWebMediaService(
            media_service,
            operation_lock,
            media_runtime,
        )
    )
    media_handle_store = (
        media_resolver
        if isinstance(media_resolver, PrivateWebMediaHandleStore)
        else None
    )
    server = create_write_api_server(
        serialized_mark_service,
        store,
        access,
        media_service=serialized_media_service,
        media_stager=media_handle_store,
        host="127.0.0.1",
        port=port,
        clock=runtime_clock,
        execution_lock=operation_lock,
        _store_lock=_store_lock,
    )
    # The generic Write API keeps daemon request threads for its standalone
    # use. This composition owns browser runtimes, so close must drain every
    # accepted handler before those runtimes can be released.
    server.daemon_threads = False
    server.block_on_close = True
    try:
        return PrivateWebWriteApiRuntime(
            server=server,
            content_runtime=content_runtime,
            confirmation_runtime=confirmation_runtime,
            media_runtime=media_runtime,
            mark_service=mark_service,
            media_service=media_service,
            media_handle_store=media_handle_store,
            operation_lock=operation_lock,
        )
    except Exception:
        try:
            server.server_close()
        except Exception:
            pass
        raise


def build_private_web_write_api_runtime(
    *,
    cdp_port: int,
    store: SnapshotStore,
    access: WriteApiAccess,
    confirmation_runtime: PrivateWebInventoryRuntime | None = None,
    media_bindings: Mapping[str, PrivateWebMediaSource] | None = None,
    media_persistence_verifier: PrivateWebMediaPersistenceVerifier | None = None,
    core_writes_enabled: bool = False,
    media_writes_enabled: bool = False,
    port: int = 0,
    timeout_seconds: float = 5.0,
    clock: Callable[[], datetime] | None = None,
) -> PrivateWebWriteApiRuntime:
    """Build a loopback-only PrivateWeb write runtime for an existing CDP worker.

    A caller may still supply a separately established inventory runtime, but
    normal Create/Delete do not require one. When absent, the authoritative
    owner inventory is observed at distinct points in time and Create also
    requires the separate target-bound content/detail read.
    """

    if not isinstance(access, WriteApiAccess):
        raise TypeError("access must be WriteApiAccess")
    if (
        confirmation_runtime is not None
        and not isinstance(confirmation_runtime, PrivateWebInventoryRuntime)
    ):
        raise TypeError(
            "confirmation_runtime must be PrivateWebInventoryRuntime or None"
        )
    if not isinstance(core_writes_enabled, bool):
        raise TypeError("core_writes_enabled must be bool")
    if not isinstance(media_writes_enabled, bool):
        raise TypeError("media_writes_enabled must be bool")
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 0 <= port <= 65535
    ):
        raise ValueError("port must be an integer between 0 and 65535")
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
    if clock is not None and not callable(clock):
        raise TypeError("clock must be callable")

    media_capability = WriteCapability.CREATE_MEDIA in access.capabilities
    if media_capability and media_bindings is not None:
        if not isinstance(media_bindings, Mapping) or not media_bindings:
            raise ValueError(
                "media_bindings must be non-empty when provided"
            )
    elif not media_capability and media_bindings is not None:
        if not isinstance(media_bindings, Mapping) or media_bindings:
            raise ValueError(
                "media_bindings require create_media capability"
            )
    if media_writes_enabled and not media_capability:
        raise ValueError(
            "media_writes_enabled requires create_media capability"
        )
    if media_persistence_verifier is not None and not media_capability:
        raise ValueError(
            "media persistence verifier requires create_media capability"
        )
    if (
        media_persistence_verifier is not None
        and not callable(
            getattr(media_persistence_verifier, "verify_media", None)
        )
    ):
        raise TypeError(
            "media_persistence_verifier must provide verify_media"
        )

    content_runtime: PrivateWebContentRuntime | None = None
    media_runtime: PrivateWebMediaCreateRuntime | None = None
    media_handle_store: PrivateWebMediaHandleStore | None = None
    store_lock: _WriteApiStoreLock | None = None
    if media_capability and media_persistence_verifier is None:
        media_persistence_verifier = (
            PrivateWebPublicMediaPersistenceVerifier()
        )
    try:
        # Reserve the write runtime before constructing the persistent media
        # handle store. A competing launcher must fail before it can rehydrate
        # or clean files owned by the active runtime.
        store_lock = acquire_write_api_store_lock(store)
        content_runtime = build_private_web_content_runtime(
            cdp_port=cdp_port,
            timeout_seconds=float(timeout_seconds),
            clock=clock,
        )
        media_resolver = None
        if media_capability:
            if media_bindings is None:
                media_handle_store = PrivateWebMediaHandleStore(
                    directory=store.path.with_name(
                        store.path.name + ".media-handles"
                    ),
                    protected_refs=lambda: _pending_dashboard_media_refs(store),
                    protected_refs_guard=store.dashboard_pending_write_guard,
                )
                media_resolver = media_handle_store
            else:
                media_resolver = PrivateWebMediaRefResolver(media_bindings)
            media_runtime = build_private_web_media_create_runtime(
                cdp_port=cdp_port,
                timeout_seconds=float(timeout_seconds),
            )
        runtime = compose_private_web_write_api_runtime(
            content_runtime=content_runtime,
            confirmation_runtime=confirmation_runtime,
            media_runtime=media_runtime,
            media_resolver=media_resolver,
            media_persistence_verifier=media_persistence_verifier,
            store=store,
            access=access,
            core_writes_enabled=core_writes_enabled,
            media_writes_enabled=media_writes_enabled,
            port=port,
            clock=clock,
            _store_lock=store_lock,
        )
        store_lock = None
        return runtime
    except Exception:
        if media_handle_store is not None:
            try:
                media_handle_store.close()
            except Exception:
                pass
        if media_runtime is not None:
            try:
                media_runtime.close()
            except Exception:
                pass
        if content_runtime is not None:
            try:
                content_runtime.close()
            except Exception:
                pass
        if store_lock is not None:
            try:
                store_lock.close()
            except Exception:
                pass
        raise


def require_private_web_runtime_dependency() -> None:
    """Fail before browser access unless the declared optional extra exists."""

    try:
        metadata.version("websocket-client")
        metadata.version("Pillow")
    except metadata.PackageNotFoundError:
        raise PrivateWebRuntimeDependencyError(
            "private Web runtime requires the private-web optional dependency set"
        ) from None

    try:
        websocket = import_module("websocket")
        pillow_image = import_module("PIL.Image")
    except Exception:
        raise PrivateWebRuntimeDependencyError(
            "private Web runtime requires the private-web optional dependency set"
        ) from None

    if (
        not callable(getattr(websocket, "create_connection", None))
        or not callable(getattr(pillow_image, "open", None))
    ):
        raise PrivateWebRuntimeDependencyError(
            "private Web runtime requires the private-web optional dependency set"
        )


def _build_private_web_owner_reader(
    *,
    cdp_port: int,
    timeout_seconds: float,
    management_source: str,
    clock: Callable[[], datetime] | None,
) -> tuple[AdsReader, _RuntimeCookieProvider]:
    cookie_delegate = CdpCookieProvider.from_port(
        cdp_port,
        timeout_seconds=timeout_seconds,
    )
    cookie_provider = _RuntimeCookieProvider(cookie_delegate)

    management_kwargs = {
        "cookie_provider": cookie_provider,
        # Browser-authenticated cookies are deliberately coupled to the fixed
        # Kleinanzeigen management endpoint. The production runtime exposes no
        # endpoint or transport override that could receive that Cookie header.
        "endpoint": MANAGEMENT_URL,
        "source": management_source,
        "transport": _NoRedirectManagementTransport(timeout_seconds=timeout_seconds),
    }
    if clock is not None:
        management_kwargs["clock"] = clock

    try:
        management_reader = ManagementReadAdapter(**management_kwargs)
    except Exception:
        cookie_provider.close()
        raise
    return management_reader, cookie_provider


def build_private_web_media_create_runtime(
    *,
    cdp_port: int,
    timeout_seconds: float = 5.0,
) -> PrivateWebMediaCreateRuntime:
    """Build only the media-create lifecycle for an existing CDP worker.

    This surface is deliberately internal to the runtime layer. It does not
    change the media-free AdCreateRequest, MarkService, or loopback Write API.
    """

    require_private_web_runtime_dependency()

    def page_factory() -> CdpPrivateWebMediaPage:
        return CdpPrivateWebMediaPage.from_port(
            cdp_port,
            timeout_seconds=timeout_seconds,
        )

    return PrivateWebMediaCreateRuntime(page_factory=page_factory)


def build_private_web_inventory_runtime(
    *,
    cdp_port: int,
    timeout_seconds: float = 5.0,
    management_source: str = MANAGEMENT_SOURCE,
    clock: Callable[[], datetime] | None = None,
) -> PrivateWebInventoryRuntime:
    """Build only the owner-inventory read surface for an existing CDP worker."""

    require_private_web_runtime_dependency()
    owner_reader, cookie_provider = _build_private_web_owner_reader(
        cdp_port=cdp_port,
        timeout_seconds=timeout_seconds,
        management_source=management_source,
        clock=clock,
    )
    return PrivateWebInventoryRuntime(
        owner_reader=owner_reader,
        close_runtime=cookie_provider.close,
    )


def build_private_web_content_runtime(
    *,
    cdp_port: int,
    timeout_seconds: float = 5.0,
    management_source: str = MANAGEMENT_SOURCE,
    clock: Callable[[], datetime] | None = None,
) -> PrivateWebContentRuntime:
    """Build the private-account Web runtime for an existing CDP worker.

    This function never starts, stops, authenticates or reauthenticates a
    browser. Login/MFA/CAPTCHA/security-challenge handling remains in the
    already-authenticated user browser session and the fail-closed page driver.
    """

    require_private_web_runtime_dependency()
    management_reader, cookie_provider = _build_private_web_owner_reader(
        cdp_port=cdp_port,
        timeout_seconds=timeout_seconds,
        management_source=management_source,
        clock=clock,
    )

    def page_factory() -> CdpPrivateWebPage:
        return CdpPrivateWebPage.from_port(
            cdp_port,
            timeout_seconds=timeout_seconds,
        )

    return PrivateWebContentRuntime(
        owner_reader=management_reader,
        page_factory=page_factory,
        close_runtime=cookie_provider.close,
    )
