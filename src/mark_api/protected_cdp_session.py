"""Actor-owned CDP lifetime across browser writes and independent post-reads.

The native exclusive target claim must not be released by a writer page before
the owning actor's authoritative inventory post-read. This module is usable
only inside the protected actor composition; ordinary CDP clients keep their
one-call ownership and UNKNOWN fencing unchanged.
"""
from __future__ import annotations

import math
from threading import Lock

from .private_web_cdp import (
    CdpPrivateWebPage, PrivateWebCdpWriteNotAttemptedError,
    _LoopbackCdpClient, _require_private_web_cdp_authority,
)
from .private_web_cdp_media import CdpPrivateWebMediaPage
from .private_web_inventory_cdp import CdpManagementInventoryReader
from .private_web_runtime import (
    PrivateWebContentRuntime, PrivateWebMediaCreateRuntime,
    require_private_web_runtime_dependency,
)


class _ActorCdpBorrower(_LoopbackCdpClient):
    """Nonowning view; native subclass recognition preserves media UNKNOWN pins.

    Do not initialize a second native CDP socket or kernel claim. All allowed
    operations delegate to the exact actor-owned native session. The inherited
    native destructor sees no owned reservation and cannot free the actor's.
    """

    def __init__(self, native: _LoopbackCdpClient, session: ProtectedActorCdpSession) -> None:
        if type(native) is not _LoopbackCdpClient:
            raise TypeError("native actor CDP owner required")
        self._native = native
        self._session = session
        self._retired = False

    def _owner(self) -> _LoopbackCdpClient:
        _require_private_web_cdp_authority()
        if self._retired or self._session._closed:
            raise PrivateWebCdpWriteNotAttemptedError("target_unowned")
        return self._native

    def call(self, method, params=None):
        return self._owner().call(method, params)

    def arm_unknown_submit(self, *, settlement_expression=None, click_point=None):
        self._owner().arm_unknown_submit(
            settlement_expression=settlement_expression, click_point=click_point,
        )

    def reconnect_for_observation(self):
        self._owner().reconnect_for_observation()

    def settle_unknown_submit(self):
        self._owner().settle_unknown_submit()

    def close(self):
        # Local page/reader cleanup is not platform settlement. Only the
        # protected actor owns the native socket's lifetime.
        self._retired = True


class ProtectedActorCdpSession:
    """Single native pinned Chrome target for a serialized actor process."""

    def __init__(self, *, port: int, timeout_seconds: float) -> None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid actor CDP port")
        if (type(timeout_seconds) not in (int, float)
                or not math.isfinite(float(timeout_seconds))
                or not 0 < timeout_seconds <= 30):
            raise ValueError("invalid actor CDP timeout")
        self.port = port
        self.timeout_seconds = float(timeout_seconds)
        self._lock = Lock()
        self._native: _LoopbackCdpClient | None = None
        self._closed = False

    def borrow(self) -> _ActorCdpBorrower:
        _require_private_web_cdp_authority()
        with self._lock:
            if self._closed:
                raise PrivateWebCdpWriteNotAttemptedError("target_unowned")
            if self._native is None:
                self._native = _LoopbackCdpClient(
                    f"http://127.0.0.1:{self.port}",
                    timeout_seconds=self.timeout_seconds,
                )
            return _ActorCdpBorrower(self._native, self)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            native = self._native
            if native is None:
                return
            # A completed browser action is still an effectful session. Never
            # release its exclusive target claim on a mere transport ACK.
            # Native FDs remain pinned until this actor process exits; the
            # durable Witness continues to fence replay across restarts.
            if native._possible_browser_side_effect:
                return
            native.close()


def build_protected_actor_content_runtime(
    session: ProtectedActorCdpSession,
) -> PrivateWebContentRuntime:
    require_private_web_runtime_dependency()
    owner = CdpManagementInventoryReader(
        cdp_port=session.port, timeout_seconds=session.timeout_seconds,
        client_factory=session.borrow,
    )
    return PrivateWebContentRuntime(
        owner_reader=owner,
        page_factory=lambda: CdpPrivateWebPage.from_port(
            session.port, timeout_seconds=session.timeout_seconds,
            client_factory=session.borrow,
        ),
        close_runtime=owner.close,
    )


def build_protected_actor_media_runtime(
    session: ProtectedActorCdpSession,
) -> PrivateWebMediaCreateRuntime:
    require_private_web_runtime_dependency()
    return PrivateWebMediaCreateRuntime(
        page_factory=lambda: CdpPrivateWebMediaPage.from_port(
            session.port, timeout_seconds=session.timeout_seconds,
            client_factory=session.borrow,
        ),
    )