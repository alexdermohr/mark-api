from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from mark_api.adapters.management import (
    HttpResponse,
    ManagementReadAdapter,
    TransportFailure,
)
from mark_api.domain import LifecycleState
from mark_api.results import ReadStatus


NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, *responses: HttpResponse | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def get(self, url: str, *, headers: dict[str, str]) -> HttpResponse:
        self.calls.append(url)
        if not self.responses:
            raise AssertionError("unexpected transport call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        self.last_headers = headers
        return response


def response(status: int, payload: object) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        body=json.dumps(payload).encode("utf-8"),
    )


class ManagementReadAdapterTests(unittest.TestCase):
    def adapter(self, transport: FakeTransport, cookies: str | None = "session=ok"):
        return ManagementReadAdapter(
            cookie_provider=lambda: cookies,
            transport=transport,
            endpoint="https://example.invalid/manage",
            clock=lambda: NOW,
        )

    def test_successful_empty_is_not_an_error(self) -> None:
        transport = FakeTransport(
            response(200, {"ads": [], "paging": {"last": 1}})
        )
        result = self.adapter(transport).read_ads()

        self.assertEqual(result.status, ReadStatus.SUCCESS_EMPTY)
        self.assertEqual(result.value, ())
        self.assertTrue(result.is_success)

    def test_missing_ads_array_is_parse_error_not_empty(self) -> None:
        transport = FakeTransport(response(200, {"paging": {"last": 1}}))
        result = self.adapter(transport).read_ads()

        self.assertEqual(result.status, ReadStatus.PARSE_ERROR)
        self.assertFalse(result.is_success)

    def test_missing_counters_remain_none(self) -> None:
        transport = FakeTransport(
            response(
                200,
                {
                    "ads": [
                        {
                            "id": 3521676801,
                            "title": "Dekorativer Hirsch mit Geweih",
                            "state": "active",
                        }
                    ],
                    "paging": {"last": 1},
                },
            )
        )
        result = self.adapter(transport).read_ads()

        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        snapshot = result.value[0]
        self.assertEqual(snapshot.ad_id, "3521676801")
        self.assertEqual(snapshot.lifecycle_state, LifecycleState.ACTIVE)
        self.assertIsNone(snapshot.views)
        self.assertIsNone(snapshot.watch_count)
        self.assertIsNone(snapshot.reply_count)

    def test_counters_and_pagination_are_normalized(self) -> None:
        transport = FakeTransport(
            response(
                200,
                {
                    "ads": [
                        {
                            "id": 1,
                            "state": "active",
                            "viewCount": "15",
                            "watchCount": 0,
                            "replies": 1,
                        }
                    ],
                    "paging": {"last": 2},
                },
            ),
            response(
                200,
                {
                    "ads": [
                        {
                            "id": 2,
                            "state": "paused",
                            "viewCount": 4,
                            "watchCount": 2,
                            "replies": 0,
                        }
                    ],
                    "paging": {"last": 2},
                },
            ),
        )
        result = self.adapter(transport).read_ads()

        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        self.assertEqual([item.ad_id for item in result.value], ["1", "2"])
        self.assertEqual(result.value[0].views, 15)
        self.assertEqual(result.value[1].lifecycle_state, LifecycleState.PAUSED)
        self.assertEqual(len(transport.calls), 2)

    def test_unauthenticated_and_http_error_stay_distinct(self) -> None:
        unauth = self.adapter(FakeTransport(response(401, {}))).read_ads()
        server_error = self.adapter(FakeTransport(response(500, {}))).read_ads()

        self.assertEqual(unauth.status, ReadStatus.UNAUTHENTICATED)
        self.assertEqual(unauth.http_status, 401)
        self.assertEqual(server_error.status, ReadStatus.HTTP_ERROR)
        self.assertEqual(server_error.http_status, 500)

    def test_missing_cookie_is_unauthenticated_without_http_call(self) -> None:
        transport = FakeTransport()
        result = self.adapter(transport, cookies=None).read_ads()

        self.assertEqual(result.status, ReadStatus.UNAUTHENTICATED)
        self.assertEqual(transport.calls, [])

    def test_transport_failure_is_not_empty(self) -> None:
        transport = FakeTransport(TransportFailure("offline"))
        result = self.adapter(transport).read_ads()

        self.assertEqual(result.status, ReadStatus.TRANSPORT_ERROR)
        self.assertFalse(result.is_success)

    def test_read_ad_uses_current_inventory_presence(self) -> None:
        transport = FakeTransport(
            response(200, {"ads": [], "paging": {"last": 1}})
        )
        result = self.adapter(transport).read_ad("3521676801")

        self.assertEqual(result.status, ReadStatus.SUCCESS_EMPTY)
        self.assertIsNone(result.value)


if __name__ == "__main__":
    unittest.main()
