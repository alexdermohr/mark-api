from __future__ import annotations

import unittest
from datetime import datetime, timezone

from mark_api.adapters.monkrel_runtime import (
    MonkrelPrivateHttpRuntimeClient,
    build_monkrel_private_http_adapter,
)


NOW = datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc)


class FakeUpstream:
    def __init__(self):
        self.calls = []

    def my_ads(self, page=0, size=25, sort_type=None, q=None):
        self.calls.append(("my_ads", page, size, sort_type, q))
        return []

    def pause_ad(self, ad_id):
        self.calls.append(("pause_ad", ad_id))

    def activate_ad(self, ad_id):
        self.calls.append(("activate_ad", ad_id))

    def delete_ad(self, ad_id):
        self.calls.append(("delete_ad", ad_id))

    def conversations(self, page=0, size=100):
        self.calls.append(("conversations", page, size))
        return ["conversation"]

    def messages(self, conversation_id):
        self.calls.append(("messages", conversation_id))
        return [{"direction": "received"}]

    def reply(self, conversation_id, text):
        self.calls.append(("reply", conversation_id, text))


class FakeContentUpdater:
    def __init__(self):
        self.calls = []

    def update_ad(self, ad_id, *, title=None, description=None):
        self.calls.append(
            (
                "update_ad",
                ad_id,
                title,
                description,
            )
        )


class FakeWriteClient:
    def __init__(self, *, max_retries):
        self.max_retries = max_retries
        self.user_id = "501"
        self.calls = []

    def _request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        raise AssertionError("write transport should not be reached")

    def _build_ad_xml(self, **kwargs):
        raise AssertionError("XML builder should not be reached")


class MonkrelPrivateHttpRuntimeClientTests(unittest.TestCase):
    def test_explicit_surface_delegates_exact_mobile_operations(self):
        upstream = FakeUpstream()
        updater = FakeContentUpdater()
        client = MonkrelPrivateHttpRuntimeClient(
            upstream,
            content_updater=updater,
        )

        self.assertEqual(
            client.my_ads(page=2, size=17, sort_type="DATE", q="stuhl"),
            [],
        )
        client.pause_ad("100")
        client.activate_ad("101")
        client.delete_ad("102")
        self.assertEqual(
            client.conversations(page=3, size=7),
            ["conversation"],
        )
        self.assertEqual(
            client.messages("conv-1"),
            [{"direction": "received"}],
        )

        self.assertEqual(
            upstream.calls,
            [
                ("my_ads", 2, 17, "DATE", "stuhl"),
                ("pause_ad", "100"),
                ("activate_ad", "101"),
                ("delete_ad", "102"),
                ("conversations", 3, 7),
                ("messages", "conv-1"),
            ],
        )

    def test_content_update_uses_separate_writer(self):
        upstream = FakeUpstream()
        updater = FakeContentUpdater()
        client = MonkrelPrivateHttpRuntimeClient(
            upstream,
            content_updater=updater,
        )

        client.update_ad(
            "3521676801",
            title="Neu",
            description="Beschreibung",
        )

        self.assertEqual(
            updater.calls,
            [
                (
                    "update_ad",
                    "3521676801",
                    "Neu",
                    "Beschreibung",
                )
            ],
        )
        self.assertEqual(upstream.calls, [])

    def test_unknown_upstream_capability_is_not_exposed(self):
        upstream = FakeUpstream()
        client = MonkrelPrivateHttpRuntimeClient(
            upstream,
            content_updater=FakeContentUpdater(),
        )

        self.assertTrue(callable(upstream.reply))
        self.assertFalse(hasattr(client, "reply"))

    def test_factory_keeps_reads_usable_with_strict_write_client_separate(self):
        upstream = FakeUpstream()
        write_client = FakeWriteClient(max_retries=3)
        adapter = build_monkrel_private_http_adapter(
            upstream,
            write_client=write_client,
            clock=lambda: NOW,
        )

        inventory = adapter.read_ads()

        self.assertTrue(inventory.is_success)
        self.assertEqual(inventory.value, ())
        self.assertEqual(
            upstream.calls,
            [("my_ads", 0, 100, None, None)],
        )
        self.assertEqual(write_client.calls, [])

    def test_factory_content_write_enforces_single_attempt_write_client(self):
        upstream = FakeUpstream()
        write_client = FakeWriteClient(max_retries=3)
        adapter = build_monkrel_private_http_adapter(
            upstream,
            write_client=write_client,
            clock=lambda: NOW,
        )

        with self.assertRaisesRegex(RuntimeError, "max_retries=1"):
            adapter.update_content(
                "3521676801",
                title="Neu",
            )

        self.assertEqual(upstream.calls, [])
        self.assertEqual(write_client.calls, [])


if __name__ == "__main__":
    unittest.main()
