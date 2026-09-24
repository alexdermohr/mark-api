from __future__ import annotations

import unittest
from dataclasses import dataclass
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


class FakeClient:
    def __init__(self) -> None:
        self.ads = []
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
        return self.ads

    def pause_ad(self, ad_id):
        self.calls.append(("pause_ad", ad_id))

    def activate_ad(self, ad_id):
        self.calls.append(("activate_ad", ad_id))

    def delete_ad(self, ad_id):
        self.calls.append(("delete_ad", ad_id))

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

    def test_reactions_count_conversations_unique_buyers_and_inbound_messages(self) -> None:
        client = FakeClient()
        client.conversation_pages = {
            0: [
                FakeConversation("c1", "3521676801", "Buyer A"),
                FakeConversation("c2", "3521676801", "Buyer A"),
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
            0: [FakeConversation("c1", "3521676801", "Buyer")]
        }
        client.messages_by_id = {"c1": [{"direction": "sideways"}]}

        result = self.adapter(client).read_reactions("3521676801")

        self.assertEqual(result.status, ReadStatus.PARSE_ERROR)
        self.assertEqual(result.error, "unknown_message_direction")

    def test_message_exception_is_sanitized(self) -> None:
        client = FakeClient()
        client.conversation_pages = {
            0: [FakeConversation("c1", "3521676801", "Buyer")]
        }
        client.messages_error = TimeoutError("secret provider response")

        result = self.adapter(client).read_reactions("3521676801")

        self.assertEqual(result.status, ReadStatus.TRANSPORT_ERROR)
        self.assertEqual(result.error, "TimeoutError")


if __name__ == "__main__":
    unittest.main()
