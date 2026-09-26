from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from .monkrel import DEFAULT_SOURCE, MonkrelMobileApiAdapter
from .monkrel_http import API_HOST, MonkrelPrivateHttpContentClient, RawMonkrelClient


class MonkrelRuntimeUpstream(Protocol):
    """Exact upstream surface consumed by MonkrelMobileApiAdapter."""

    def my_ads(
        self,
        page: int = 0,
        size: int = 25,
        sort_type: str | None = None,
        q: str | None = None,
    ) -> list:
        ...

    def pause_ad(self, ad_id: str) -> None:
        ...

    def activate_ad(self, ad_id: str) -> None:
        ...

    def delete_ad(self, ad_id: str) -> None:
        ...

    def conversations(self, page: int = 0, size: int = 100) -> list:
        ...

    def messages(self, conversation_id: str) -> list:
        ...


class MonkrelContentUpdater(Protocol):
    def update_ad(
        self,
        ad_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        ...


class MonkrelPrivateHttpRuntimeClient:
    """Expose only the upstream capabilities Mark currently consumes.

    Read/state/delete/inbox operations stay on the existing upstream client.
    Content updates are routed through a separate writer so that its stricter
    one-attempt transport policy can be enforced independently.

    This class intentionally has no generic attribute proxy: adding a new
    upstream capability to Mark requires an explicit method and test here.
    """

    def __init__(
        self,
        upstream: MonkrelRuntimeUpstream,
        *,
        content_updater: MonkrelContentUpdater,
    ) -> None:
        self._upstream = upstream
        self._content_updater = content_updater

    def my_ads(
        self,
        page: int = 0,
        size: int = 25,
        sort_type: str | None = None,
        q: str | None = None,
    ) -> list:
        return self._upstream.my_ads(
            page=page,
            size=size,
            sort_type=sort_type,
            q=q,
        )

    def pause_ad(self, ad_id: str) -> None:
        self._upstream.pause_ad(ad_id)

    def activate_ad(self, ad_id: str) -> None:
        self._upstream.activate_ad(ad_id)

    def delete_ad(self, ad_id: str) -> None:
        self._upstream.delete_ad(ad_id)

    def conversations(self, page: int = 0, size: int = 100) -> list:
        return self._upstream.conversations(page=page, size=size)

    def messages(self, conversation_id: str) -> list:
        return self._upstream.messages(conversation_id)

    def update_ad(
        self,
        ad_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        self._content_updater.update_ad(
            ad_id,
            title=title,
            description=description,
        )


def build_monkrel_private_http_runtime_client(
    upstream: MonkrelRuntimeUpstream,
    *,
    write_client: RawMonkrelClient,
    api_host: str = API_HOST,
    contact_email_provider: Callable[[], str | None] | None = None,
) -> MonkrelPrivateHttpRuntimeClient:
    """Compose the normal mobile client with the strict content writer."""

    return MonkrelPrivateHttpRuntimeClient(
        upstream,
        content_updater=MonkrelPrivateHttpContentClient(
            write_client,
            api_host=api_host,
            contact_email_provider=contact_email_provider,
        ),
    )


def build_monkrel_private_http_adapter(
    upstream: MonkrelRuntimeUpstream,
    *,
    write_client: RawMonkrelClient,
    api_host: str = API_HOST,
    contact_email_provider: Callable[[], str | None] | None = None,
    source: str = DEFAULT_SOURCE,
    clock: Callable[[], datetime] | None = None,
) -> MonkrelMobileApiAdapter:
    """Build the Mark adapter with an explicit separate content-write client."""

    runtime_client = build_monkrel_private_http_runtime_client(
        upstream,
        write_client=write_client,
        api_host=api_host,
        contact_email_provider=contact_email_provider,
    )
    if clock is None:
        return MonkrelMobileApiAdapter(runtime_client, source=source)
    return MonkrelMobileApiAdapter(
        runtime_client,
        source=source,
        clock=clock,
    )
