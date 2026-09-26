from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from datetime import datetime, timezone

from mark_api.adapters.monkrel import MonkrelMobileApiAdapter
from mark_api.domain import LifecycleState
from mark_api.results import ReadStatus


NOW = datetime(2026, 9, 24, 12, 37, tzinfo=timezone.utc)


@dataclass
class FakeListing:
    id: str
    title: str
    description: str


@dataclass
class FakeConversation:
    id: str
    ad_id: str
    counterparty: str
    role: str = "SELLER"
    raw: dict = field(default_factory=dict)


class FakeClient:
    def __init__(self) -> None:
        self.ads = []
        self.ad_pages = None
        self.conversation_pages = {0: []}
        self.messages_by_id = {}
        self.calls = []
        self.my_ads_error = None
        self.conversations_error = None
        self.messages_error = None

    def my_ads(self, page=0, size=25, sort_type=None, q=None):
        self.calls.append(("my_ads", page, size))
        if self.my_ads_error is not None:
            raise self.my_ads_error
        if self.ad_pages is not None:
            return self.ad_pages.get(page, [])
        return self.ads

    def pause_ad(self, ad_id):
        self.calls.append(("pause_ad", ad_id))

    def activate_ad(self, ad_id):
        self.calls.append(("activate_ad", ad_id))

    def delete_ad(self, ad_id):
        self.calls.append(("delete_ad", ad_id))

    def update_ad(self, ad_id, *, title=None, description=None):
        self.calls.append(("update_ad", ad_id, title, description))

    def conversations(self, page=0, size=100):
        self.calls.append(("conversations", page, size))
        if self.conversations_error is not None:
            raise self.conversations_error
        return self.conversation_pages.get(page, [])

    def messages(self, conversation_id):
        self.calls.append(("messages", conversation_id))
        if self.messages_error is not None:
            raise self.messages_error
        return self.messages_by_id.get(conversation_id, [])


class MonkrelMobileApiAdapterTests(unittest.TestCase):
    def adapter(self, client: FakeClient) -> MonkrelMobileApiAdapter:
        return MonkrelMobileApiAdapter(client, clock=lambda: NOW)

    def test_empty_my_ads_is_successful_empty(self) -> None:
        client = FakeClient()
        result = self.adapter(client).read_ads()

        self.assertEqual(result.status, ReadStatus.SUCCESS_EMPTY)
        self.assertEqual(result.value, ())

    def test_listing_is_normalized_without_invented_status_or_metrics(self) -> None:
        client = FakeClient()
        client.ads = [
            FakeListing(
                id="3521676801",
                title="Dekorativer Hirsch mit Geweih",
                description="beflockt",
            )
        ]

        result = self.adapter(client).read_ads()

        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        item = result.value[0]
        self.assertEqual(item.ad_id, "3521676801")
        self.assertEqual(item.lifecycle_state, LifecycleState.UNKNOWN)
        self.assertIsNone(item.views)
        self.assertIsNone(item.watch_count)
        self.assertIsNone(item.reply_count)

    def test_my_ads_pages_until_short_page_and_deduplicates_ids(self) -> None:
        client = FakeClient()
        first_page = [
            FakeListing(str(index), f"title-{index}", "description")
            for index in range(100)
        ]
        client.ad_pages = {
            0: first_page,
            1: [
                FakeListing("99", "duplicate", "description"),
                FakeListing("100", "last", "description"),
            ],
        }

        result = self.adapter(client).read_ads()

        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        self.assertEqual(len(result.value), 101)
        self.assertEqual(result.value[-1].ad_id, "100")
        self.assertEqual(
            client.calls,
            [("my_ads", 0, 100), ("my_ads", 1, 100)],
        )

    def test_my_ads_fails_closed_at_pagination_limit(self) -> None:
        client = FakeClient()
        full_page = [
            FakeListing(str(index), f"title-{index}", "description")
            for index in range(100)
        ]
        client.ad_pages = {page: full_page for page in range(100)}

        result = self.adapter(client).read_ads()

        self.assertEqual(result.status, ReadStatus.PARSE_ERROR)
        self.assertEqual(result.error, "my_ads_pagination_limit")
        self.assertEqual(client.calls[-1], ("my_ads", 99, 100))

    def test_read_ad_uses_my_ads_not_stale_detail_endpoint(self) -> None:
        client = FakeClient()

        result = self.adapter(client).read_ad("3521676801")

        self.assertEqual(result.status, ReadStatus.SUCCESS_EMPTY)
        self.assertEqual(client.calls, [("my_ads", 0, 100)])

    def test_client_exception_is_sanitized(self) -> None:
        client = FakeClient()
        client.my_ads_error = RuntimeError("token=secret-provider-body")

        result = self.adapter(client).read_ads()

        self.assertEqual(result.status, ReadStatus.TRANSPORT_ERROR)
        self.assertEqual(result.error, "RuntimeError")
        self.assertNotIn("secret", result.error)

    def test_not_logged_in_is_unauthenticated_without_error_text(self) -> None:
        class NotLoggedIn(RuntimeError):
            pass

        NotLoggedIn.__module__ = "kleinanzeigen_api.auth"
        client = FakeClient()
        client.my_ads_error = NotLoggedIn("token=must-not-leak")

        result = self.adapter(client).read_ads()

        self.assertEqual(result.status, ReadStatus.UNAUTHENTICATED)
        self.assertEqual(result.error, "NotLoggedIn")
        self.assertNotIn("token", result.error)

    def test_http_auth_runtime_error_is_unauthenticated_without_leaking_body(self) -> None:
        client = FakeClient()
        client.my_ads_error = RuntimeError(
            "GET https://provider.invalid -> 401: token=must-not-leak"
        )

        result = self.adapter(client).read_ads()

        self.assertEqual(result.status, ReadStatus.UNAUTHENTICATED)
        self.assertEqual(result.error, "RuntimeError")
        self.assertNotIn("token", result.error)

    def test_auth0_refresh_rejection_is_unauthenticated(self) -> None:
        client = FakeClient()
        client.my_ads_error = RuntimeError(
            'Auth0 token endpoint returned 400: {"error":"invalid_grant"}'
        )

        result = self.adapter(client).read_ads()

        self.assertEqual(result.status, ReadStatus.UNAUTHENTICATED)
        self.assertEqual(result.error, "RuntimeError")

    def test_state_writer_dispatches_only_active_and_paused(self) -> None:
        client = FakeClient()
        adapter = self.adapter(client)

        adapter.set_state("1", LifecycleState.PAUSED)
        adapter.set_state("1", LifecycleState.ACTIVE)

        self.assertIn(("pause_ad", "1"), client.calls)
        self.assertIn(("activate_ad", "1"), client.calls)
        with self.assertRaises(ValueError):
            adapter.set_state("1", LifecycleState.PENDING)

    def test_delete_writer_dispatches_exact_id(self) -> None:
        client = FakeClient()
        self.adapter(client).delete_ad("3521676801")

        self.assertEqual(client.calls, [("delete_ad", "3521676801")])

    def test_content_writer_dispatches_exact_partial_update(self) -> None:
        client = FakeClient()
        adapter = self.adapter(client)

        adapter.update_content(" 3521676801 ", title="Neuer Titel")
        adapter.update_content("3521676801", description="Neue Beschreibung")

        self.assertEqual(
            client.calls,
            [
                ("update_ad", "3521676801", "Neuer Titel", None),
                ("update_ad", "3521676801", None, "Neue Beschreibung"),
            ],
        )

    def test_content_writer_fails_closed_without_update_capability(self) -> None:
        class ClientWithoutUpdate:
            pass

        adapter = MonkrelMobileApiAdapter(ClientWithoutUpdate(), clock=lambda: NOW)  # type: ignore[arg-type]

        with self.assertRaisesRegex(
            RuntimeError,
            "content update capability unavailable",
        ):
            adapter.update_content("3521676801", title="Neuer Titel")

    def test_content_writer_requires_at_least_one_valid_field(self) -> None:
        client = FakeClient()
        adapter = self.adapter(client)

        with self.assertRaises(ValueError):
            adapter.update_content("3521676801")
        with self.assertRaises(TypeError):
            adapter.update_content("3521676801", title=123)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            adapter.update_content("   ", title="Neuer Titel")

        self.assertEqual(client.calls, [])

    def test_reactions_count_conversations_unique_buyers_and_inbound_messages(self) -> None:
        client = FakeClient()
        client.conversation_pages = {
            0: [
                FakeConversation(
                    "c1",
                    "3521676801",
                    "Buyer A",
                    raw={"userIdBuyer": 501},
                ),
                FakeConversation(
                    "c2",
                    "3521676801",
                    "Renamed Buyer",
                    raw={"userIdBuyer": 501},
                ),
                FakeConversation("c3", "other-ad", "Buyer B"),
            ]
        }
        client.messages_by_id = {
            "c1": [
                {"direction": "received", "text": "must not be read"},
                {"direction": "sent", "text": "must not be read"},
            ],
            "c2": [
                {"direction": "received", "text": "must not be read"},
            ],
        }

        result = self.adapter(client).read_reactions("3521676801")

        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        self.assertEqual(result.value.conversation_count, 2)
        self.assertEqual(result.value.unique_buyer_count, 1)
        self.assertEqual(result.value.inbound_message_count, 2)
        self.assertIn(("messages", "c1"), client.calls)
        self.assertIn(("messages", "c2"), client.calls)
        self.assertNotIn(("messages", "c3"), client.calls)

    def test_unique_buyer_count_uses_stable_buyer_id_not_display_name(self) -> None:
        client = FakeClient()
        client.conversation_pages = {
            0: [
                FakeConversation(
                    "c1",
                    "3521676801",
                    "Same Name",
                    raw={"userIdBuyer": 501},
                ),
                FakeConversation(
                    "c2",
                    "3521676801",
                    "Same Name",
                    raw={"userIdBuyer": 502},
                ),
            ]
        }

        result = self.adapter(client).read_reactions("3521676801")

        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        self.assertEqual(result.value.unique_buyer_count, 2)

    def test_missing_stable_buyer_id_is_parse_error(self) -> None:
        client = FakeClient()
        client.conversation_pages = {
            0: [FakeConversation("c1", "3521676801", "Buyer")]
        }

        result = self.adapter(client).read_reactions("3521676801")

        self.assertEqual(result.status, ReadStatus.PARSE_ERROR)
        self.assertEqual(result.error, "stable_buyer_id_unavailable")

    def test_zero_reactions_is_still_a_successful_metric_snapshot(self) -> None:
        client = FakeClient()

        result = self.adapter(client).read_reactions("3521676801")

        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        self.assertEqual(result.value.conversation_count, 0)
        self.assertEqual(result.value.unique_buyer_count, 0)
        self.assertEqual(result.value.inbound_message_count, 0)

    def test_unknown_message_direction_is_parse_error(self) -> None:
        client = FakeClient()
        client.conversation_pages = {
            0: [
                FakeConversation(
                    "c1",
                    "3521676801",
                    "Buyer",
                    raw={"userIdBuyer": 501},
                )
            ]
        }
        client.messages_by_id = {"c1": [{"direction": "sideways"}]}

        result = self.adapter(client).read_reactions("3521676801")

        self.assertEqual(result.status, ReadStatus.PARSE_ERROR)
        self.assertEqual(result.error, "unknown_message_direction")

    def test_message_exception_is_sanitized(self) -> None:
        client = FakeClient()
        client.conversation_pages = {
            0: [
                FakeConversation(
                    "c1",
                    "3521676801",
                    "Buyer",
                    raw={"userIdBuyer": 501},
                )
            ]
        }
        client.messages_error = TimeoutError("secret provider response")

        result = self.adapter(client).read_reactions("3521676801")

        self.assertEqual(result.status, ReadStatus.TRANSPORT_ERROR)
        self.assertEqual(result.error, "TimeoutError")


if __name__ == "__main__":
    unittest.main()