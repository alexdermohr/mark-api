from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from importlib import import_module, metadata
from typing import Protocol

from .adapters.management import (
    DEFAULT_SOURCE as MANAGEMENT_SOURCE,
    MANAGEMENT_URL,
    ManagementReadAdapter,
)
from .ports import AdContentUpdater, AdsReader
from .private_web import PrivateWebContentWriter, PrivateWebPage
from .private_web_cdp import (
    CdpCookieProvider,
    CdpPrivateWebOwnerReader,
    CdpPrivateWebPage,
)


class PrivateWebRuntimeDependencyError(RuntimeError):
    """The optional private-Web runtime dependency set is unavailable."""


class PrivateWebRuntimeClosedError(RuntimeError):
    """The private-Web runtime bundle has already been closed."""


class _CloseablePrivateWebPage(PrivateWebPage, Protocol):
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
        page = self._page_factory()
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


class PrivateWebContentRuntime:
    """Compose target-bound private-Web content reads and writes.

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

    def _ensure_open(self) -> None:
        if self._closed:
            raise PrivateWebRuntimeClosedError("private Web runtime is closed")

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


def build_private_web_content_runtime(
    *,
    cdp_port: int,
    timeout_seconds: float = 5.0,
    management_source: str = MANAGEMENT_SOURCE,
    clock: Callable[[], datetime] | None = None,
) -> PrivateWebContentRuntime:
    """Build the private-account content runtime for an existing CDP worker.

    This function never starts, stops, authenticates or reauthenticates a
    browser. Login/MFA/CAPTCHA/security-challenge handling remains in the
    already-authenticated user browser session and the fail-closed page driver.
    """

    require_private_web_runtime_dependency()

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
    }
    if clock is not None:
        management_kwargs["clock"] = clock

    management_reader = ManagementReadAdapter(**management_kwargs)

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
