from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from importlib import import_module, metadata
from threading import Lock
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .adapters.management import (
    DEFAULT_SOURCE as MANAGEMENT_SOURCE,
    MANAGEMENT_URL,
    HttpResponse,
    ManagementReadAdapter,
    TransportFailure,
)
from .domain import AdCreateRequest, AdSnapshot, LifecycleState
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
)
from .results import ReadResult
from .private_web_cdp import (
    CdpCookieProvider,
    CdpPrivateWebOwnerReader,
    CdpPrivateWebPage,
)
from .private_web_cdp_media import CdpPrivateWebMediaPage
from .private_web_media import (
    PrivateWebCreateMediaPublishPage,
    PrivateWebCreateMediaWriter,
    PrivateWebMediaSource,
)


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


def require_private_web_runtime_dependency() -> None:
    """Fail before browser access unless the declared optional extra exists."""

    try:
        metadata.version("websocket-client")
    except metadata.PackageNotFoundError:
        raise PrivateWebRuntimeDependencyError(
            "private Web runtime requires the private-web optional dependency set"
        ) from None

    try:
        websocket = import_module("websocket")
    except Exception:
        raise PrivateWebRuntimeDependencyError(
            "private Web runtime requires the private-web optional dependency set"
        ) from None

    if not callable(getattr(websocket, "create_connection", None)):
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
        "transport": _NoRedirectManagementTransport(),
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