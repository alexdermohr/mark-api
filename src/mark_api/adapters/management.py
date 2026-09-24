from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..domain import AdSnapshot, LifecycleState
from ..results import ReadResult, ReadStatus


MANAGEMENT_URL = "https://www.kleinanzeigen.de/m-meine-anzeigen-verwalten.json"
DEFAULT_SOURCE = "kleinanzeigen-management"
MAX_PAGES = 100


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    body: bytes


class TransportFailure(RuntimeError):
    """A network/transport failure with no trustworthy HTTP response."""


class HttpTransport(Protocol):
    def get(self, url: str, *, headers: dict[str, str]) -> HttpResponse:
        """Perform one GET and return the response without interpreting JSON."""


class UrllibHttpTransport:
    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        self._timeout_seconds = timeout_seconds

    def get(self, url: str, *, headers: dict[str, str]) -> HttpResponse:
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                return HttpResponse(
                    status_code=int(response.status),
                    body=response.read(),
                )
        except HTTPError as exc:
            return HttpResponse(status_code=int(exc.code), body=exc.read())
        except (URLError, TimeoutError, OSError) as exc:
            raise TransportFailure(type(exc).__name__) from exc


def _normalize_state(value: Any) -> LifecycleState:
    if not isinstance(value, str):
        return LifecycleState.UNKNOWN
    normalized = value.strip().lower()
    return {
        "pending": LifecycleState.PENDING,
        "active": LifecycleState.ACTIVE,
        "paused": LifecycleState.PAUSED,
        "reserved": LifecycleState.PAUSED,
        "deleted": LifecycleState.ABSENT,
    }.get(normalized, LifecycleState.UNKNOWN)


def _optional_counter(ad: dict[str, Any], field: str) -> int | None:
    if field not in ad or ad[field] is None:
        return None
    value = ad[field]
    if isinstance(value, bool):
        raise ValueError(f"{field} must not be boolean")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"{field} must not be negative")
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ValueError(f"{field} must be an integer, numeric string, null or absent")


def _optional_text(ad: dict[str, Any], field: str) -> str | None:
    value = ad.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string, null or absent")
    return value


class ManagementReadAdapter:
    """Strict current-owner inventory reader.

    This adapter intentionally does not use public/detail endpoints. In the real
    PoC those surfaces remained stale after delete while the owner inventory was
    already empty.
    """

    def __init__(
        self,
        *,
        cookie_provider: Callable[[], str | None],
        transport: HttpTransport | None = None,
        endpoint: str = MANAGEMENT_URL,
        source: str = DEFAULT_SOURCE,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._cookie_provider = cookie_provider
        self._transport = transport or UrllibHttpTransport()
        self._endpoint = endpoint
        self._source = source
        self._clock = clock

    def _headers(self, cookies: str) -> dict[str, str]:
        return {
            "Cookie": cookies,
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/152.0.0.0 Safari/537.36"
            ),
        }

    def _read_page(
        self,
        *,
        cookies: str,
        page: int,
    ) -> ReadResult[dict[str, Any]]:
        url = f"{self._endpoint}?{urlencode({'sort': 'DEFAULT', 'pageNum': page})}"
        try:
            response = self._transport.get(url, headers=self._headers(cookies))
        except TransportFailure as exc:
            return ReadResult.failure(
                ReadStatus.TRANSPORT_ERROR,
                error=type(exc).__name__,
            )
        except Exception as exc:  # noqa: BLE001 - transport boundary is typed here.
            return ReadResult.failure(
                ReadStatus.TRANSPORT_ERROR,
                error=type(exc).__name__,
            )

        if response.status_code in {401, 403}:
            return ReadResult.failure(
                ReadStatus.UNAUTHENTICATED,
                http_status=response.status_code,
            )
        if response.status_code < 200 or response.status_code >= 300:
            return ReadResult.failure(
                ReadStatus.HTTP_ERROR,
                http_status=response.status_code,
            )

        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ReadResult.failure(
                ReadStatus.PARSE_ERROR,
                error="invalid_json",
            )
        if not isinstance(payload, dict):
            return ReadResult.failure(
                ReadStatus.PARSE_ERROR,
                error="root_not_object",
            )
        return ReadResult.success_nonempty(payload)

    @staticmethod
    def _page_shape(
        payload: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], int]:
        if "ads" not in payload or not isinstance(payload["ads"], list):
            raise ValueError("management payload must contain an ads array")
        ads: list[dict[str, Any]] = []
        for item in payload["ads"]:
            if not isinstance(item, dict):
                raise ValueError("management ads entries must be objects")
            ads.append(item)

        paging = payload.get("paging")
        if paging is None:
            last_page = 1
        else:
            if not isinstance(paging, dict):
                raise ValueError("paging must be an object when present")
            last_value = paging.get("last", 1)
            if (
                isinstance(last_value, bool)
                or not isinstance(last_value, int)
                or last_value < 1
                or last_value > MAX_PAGES
            ):
                raise ValueError("paging.last is invalid")
            last_page = last_value

        if not ads and last_page != 1:
            raise ValueError("empty first page with additional pages is ambiguous")
        return ads, last_page

    def _snapshot(
        self,
        ad: dict[str, Any],
        *,
        observed_at: datetime,
    ) -> AdSnapshot:
        raw_id = ad.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
            raise ValueError("ad id is missing or invalid")
        ad_id = str(raw_id).strip()
        if not ad_id:
            raise ValueError("ad id must not be blank")

        return AdSnapshot(
            ad_id=ad_id,
            observed_at=observed_at,
            source=self._source,
            lifecycle_state=_normalize_state(ad.get("state")),
            title=_optional_text(ad, "title"),
            views=_optional_counter(ad, "viewCount"),
            watch_count=_optional_counter(ad, "watchCount"),
            reply_count=_optional_counter(ad, "replies"),
        )

    def read_ads(self) -> ReadResult[tuple[AdSnapshot, ...]]:
        try:
            cookies = self._cookie_provider()
        except Exception as exc:  # noqa: BLE001 - secret provider boundary.
            return ReadResult.failure(
                ReadStatus.TRANSPORT_ERROR,
                error=type(exc).__name__,
            )
        if not cookies or not cookies.strip():
            return ReadResult.failure(ReadStatus.UNAUTHENTICATED)

        first = self._read_page(cookies=cookies, page=1)
        if not first.is_success:
            return ReadResult.failure(
                first.status,
                error=first.error,
                http_status=first.http_status,
            )

        try:
            first_ads, last_page = self._page_shape(first.value or {})
        except ValueError:
            return ReadResult.failure(
                ReadStatus.PARSE_ERROR,
                error="invalid_page_shape",
            )

        raw_ads = list(first_ads)
        for page in range(2, last_page + 1):
            result = self._read_page(cookies=cookies, page=page)
            if not result.is_success:
                return ReadResult.failure(
                    result.status,
                    error=result.error,
                    http_status=result.http_status,
                )
            try:
                page_ads, page_last = self._page_shape(result.value or {})
            except ValueError:
                return ReadResult.failure(
                    ReadStatus.PARSE_ERROR,
                    error="invalid_page_shape",
                )
            if page_last != last_page:
                return ReadResult.failure(
                    ReadStatus.PARSE_ERROR,
                    error="pagination_drift",
                )
            raw_ads.extend(page_ads)

        observed_at = self._clock()
        try:
            snapshots = tuple(
                self._snapshot(ad, observed_at=observed_at)
                for ad in raw_ads
            )
        except ValueError:
            return ReadResult.failure(
                ReadStatus.PARSE_ERROR,
                error="invalid_ad_shape",
            )

        if not snapshots:
            return ReadResult.success_empty(())
        return ReadResult.success_nonempty(snapshots)

    def read_ad(self, ad_id: str) -> ReadResult[AdSnapshot]:
        result = self.read_ads()
        if not result.is_success:
            return ReadResult.failure(
                result.status,
                error=result.error,
                http_status=result.http_status,
            )
        for snapshot in result.value or ():
            if snapshot.ad_id == ad_id:
                return ReadResult.success_nonempty(snapshot)
        return ReadResult.success_empty()
