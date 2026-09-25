from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from ..domain import AdSnapshot, LifecycleState, ReactionSnapshot
from ..results import ReadResult, ReadStatus


DEFAULT_SOURCE = "monkrel-mobile-api"
AD_PAGE_SIZE = 100
MAX_AD_PAGES = 100
CONVERSATION_PAGE_SIZE = 100
MAX_CONVERSATION_PAGES = 100


class MonkrelClient(Protocol):
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


def _safe_attr(value: Any, name: str) -> Any:
    return getattr(value, name, None)


def _required_id(value: Any, field_name: str) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field_name} is missing")
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name} is blank")
    return normalized


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string or null")
    return value


def _pseudonym(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _is_unauthenticated_exception(exc: Exception) -> bool:
    exc_type = type(exc)
    return (
        exc_type.__name__ == "NotLoggedIn"
        and exc_type.__module__ == "kleinanzeigen_api.auth"
    )


def _stable_buyer_id(conversation: Any) -> str:
    role = _safe_attr(conversation, "role")
    if not isinstance(role, str) or role.strip().upper() != "SELLER":
        raise ValueError("conversation.role must be SELLER")

    raw = _safe_attr(conversation, "raw")
    if not isinstance(raw, Mapping):
        raise ValueError("conversation.raw must be a mapping")
    return _required_id(raw.get("userIdBuyer"), "conversation.raw.userIdBuyer")


class MonkrelMobileApiAdapter:
    """Normalize the real-tested monkrel client behind mark-api contracts.

    Presence is derived from my_ads(), never get_my_ad(id), because the real
    post-delete test showed that the direct detail endpoint can remain stale.

    read_reactions() calls monkrel.messages(). In the tested upstream this opens
    the conversation through a PUT request and may therefore mark it read. The
    method is intentionally explicit and must not be treated as side-effect-free.
    """

    reaction_reads_may_mark_read = True

    def __init__(
        self,
        client: MonkrelClient,
        *,
        source: str = DEFAULT_SOURCE,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        self._client = client
        self._source = source
        self._clock = clock

    @staticmethod
    def _client_failure(exc: Exception) -> ReadResult[Any]:
        # Exception text can contain provider URLs, response bodies or secrets.
        status = (
            ReadStatus.UNAUTHENTICATED
            if _is_unauthenticated_exception(exc)
            else ReadStatus.TRANSPORT_ERROR
        )
        return ReadResult.failure(
            status,
            error=type(exc).__name__,
        )

    def _listing_snapshot(
        self,
        listing: Any,
        *,
        observed_at: datetime,
    ) -> AdSnapshot:
        ad_id = _required_id(_safe_attr(listing, "id"), "listing.id")
        title = _optional_string(_safe_attr(listing, "title"), "listing.title")
        description = _optional_string(
            _safe_attr(listing, "description"),
            "listing.description",
        )
        return AdSnapshot(
            ad_id=ad_id,
            observed_at=observed_at,
            source=self._source,
            lifecycle_state=LifecycleState.UNKNOWN,
            title=title,
            description=description,
            views=None,
            watch_count=None,
            reply_count=None,
        )

    def read_ads(self) -> ReadResult[tuple[AdSnapshot, ...]]:
        observed_at = self._clock()
        snapshots: list[AdSnapshot] = []
        seen_ids: set[str] = set()

        for page in range(MAX_AD_PAGES):
            try:
                listings = self._client.my_ads(page=page, size=AD_PAGE_SIZE)
            except Exception as exc:  # noqa: BLE001 - client boundary.
                return self._client_failure(exc)

            if not isinstance(listings, list):
                return ReadResult.failure(
                    ReadStatus.PARSE_ERROR,
                    error="my_ads_not_list",
                )

            try:
                for listing in listings:
                    snapshot = self._listing_snapshot(
                        listing,
                        observed_at=observed_at,
                    )
                    if snapshot.ad_id in seen_ids:
                        continue
                    seen_ids.add(snapshot.ad_id)
                    snapshots.append(snapshot)
            except ValueError:
                return ReadResult.failure(
                    ReadStatus.PARSE_ERROR,
                    error="invalid_listing_shape",
                )

            if len(listings) < AD_PAGE_SIZE:
                if not snapshots:
                    return ReadResult.success_empty(())
                return ReadResult.success_nonempty(tuple(snapshots))

        return ReadResult.failure(
            ReadStatus.PARSE_ERROR,
            error="my_ads_pagination_limit",
        )

    def read_ad(self, ad_id: str) -> ReadResult[AdSnapshot]:
        """Read presence from current inventory rather than stale detail GET."""
        inventory = self.read_ads()
        if not inventory.is_success:
            return ReadResult.failure(
                inventory.status,
                error=inventory.error,
                http_status=inventory.http_status,
            )
        for snapshot in inventory.value or ():
            if snapshot.ad_id == ad_id:
                return ReadResult.success_nonempty(snapshot)
        return ReadResult.success_empty()

    def set_state(self, ad_id: str, state: LifecycleState) -> None:
        if state is LifecycleState.ACTIVE:
            self._client.activate_ad(ad_id)
            return
        if state is LifecycleState.PAUSED:
            self._client.pause_ad(ad_id)
            return
        raise ValueError("monkrel writer supports only ACTIVE or PAUSED")

    def delete_ad(self, ad_id: str) -> None:
        self._client.delete_ad(ad_id)

    def _all_conversations(self) -> ReadResult[tuple[Any, ...]]:
        conversations: list[Any] = []
        seen_ids: set[str] = set()

        for page in range(MAX_CONVERSATION_PAGES):
            try:
                batch = self._client.conversations(
                    page=page,
                    size=CONVERSATION_PAGE_SIZE,
                )
            except Exception as exc:  # noqa: BLE001 - client boundary.
                return self._client_failure(exc)

            if not isinstance(batch, list):
                return ReadResult.failure(
                    ReadStatus.PARSE_ERROR,
                    error="conversations_not_list",
                )

            for conversation in batch:
                try:
                    conversation_id = _required_id(
                        _safe_attr(conversation, "id"),
                        "conversation.id",
                    )
                except ValueError:
                    return ReadResult.failure(
                        ReadStatus.PARSE_ERROR,
                        error="invalid_conversation_shape",
                    )
                if conversation_id in seen_ids:
                    continue
                seen_ids.add(conversation_id)
                conversations.append(conversation)

            if len(batch) < CONVERSATION_PAGE_SIZE:
                return ReadResult.success_nonempty(tuple(conversations))

        return ReadResult.failure(
            ReadStatus.PARSE_ERROR,
            error="conversation_pagination_limit",
        )

    def read_reactions(self, ad_id: str) -> ReadResult[ReactionSnapshot]:
        all_conversations = self._all_conversations()
        if not all_conversations.is_success:
            return ReadResult.failure(
                all_conversations.status,
                error=all_conversations.error,
                http_status=all_conversations.http_status,
            )

        matching: list[Any] = []
        buyer_ids: set[bytes] = set()

        for conversation in all_conversations.value or ():
            try:
                conversation_ad_id = _required_id(
                    _safe_attr(conversation, "ad_id"),
                    "conversation.ad_id",
                )
            except ValueError:
                return ReadResult.failure(
                    ReadStatus.PARSE_ERROR,
                    error="invalid_conversation_shape",
                )
            if conversation_ad_id != ad_id:
                continue

            try:
                stable_buyer_id = _stable_buyer_id(conversation)
            except ValueError:
                return ReadResult.failure(
                    ReadStatus.PARSE_ERROR,
                    error="stable_buyer_id_unavailable",
                )

            matching.append(conversation)
            buyer_ids.add(_pseudonym(stable_buyer_id))

        inbound_messages = 0
        for conversation in matching:
            conversation_id = _required_id(
                _safe_attr(conversation, "id"),
                "conversation.id",
            )
            try:
                messages = self._client.messages(conversation_id)
            except Exception as exc:  # noqa: BLE001 - client boundary.
                return self._client_failure(exc)

            if not isinstance(messages, list):
                return ReadResult.failure(
                    ReadStatus.PARSE_ERROR,
                    error="messages_not_list",
                )

            for message in messages:
                if not isinstance(message, Mapping):
                    return ReadResult.failure(
                        ReadStatus.PARSE_ERROR,
                        error="invalid_message_shape",
                    )
                direction = message.get("direction")
                if not isinstance(direction, str):
                    return ReadResult.failure(
                        ReadStatus.PARSE_ERROR,
                        error="missing_message_direction",
                    )
                normalized = direction.strip().lower()
                if normalized == "received":
                    inbound_messages += 1
                elif normalized == "sent":
                    continue
                else:
                    return ReadResult.failure(
                        ReadStatus.PARSE_ERROR,
                        error="unknown_message_direction",
                    )

        snapshot = ReactionSnapshot(
            ad_id=ad_id,
            conversation_count=len(matching),
            unique_buyer_count=len(buyer_ids),
            inbound_message_count=inbound_messages,
            observed_at=self._clock(),
            source=self._source,
        )
        return ReadResult.success_nonempty(snapshot)