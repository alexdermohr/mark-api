from __future__ import annotations

import unittest

from mark_api.adapters.monkrel_http import MonkrelPrivateHttpContentClient


AD_NS = "{http://www.ebayclassifiedsgroup.com/schema/ad/v1}ad"


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeRawClient:
    def __init__(self, payload, *, max_retries=1):
        self.payload = payload
        self.max_retries = max_retries
        self.user_id = "501"
        self.calls = []
        self.build_calls = []

    def _request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "GET":
            return FakeResponse(self.payload)
        if method == "PUT":
            return FakeResponse({})
        raise AssertionError(method)

    def _build_ad_xml(self, **kwargs):
        self.build_calls.append(kwargs)
        return "<ad/>"


def owner_payload():
    return {
        AD_NS: {
            "value": {
                "id": 3521676801,
                "title": {"value": "Alter Titel"},
                "description": {"value": "Alte Beschreibung"},
                "contact-name": {"value": "Mark"},
                "email": {"value": "mark@example.invalid"},
                "phone": {"value": "040000000"},
                "poster-type": {"value": "PRIVATE"},
                "ad-type": {"value": "OFFERED"},
                "category": {"id": "192"},
                "locations": {"location": [{"id": "3455"}]},
                "ad-address": {
                    "latitude": {"value": 53.55},
                    "longitude": {"value": 9.99},
                },
                "price": {
                    "amount": {"value": 25},
                    "price-type": {"value": "FIXED"},
                },
                "pictures": {
                    "picture": [
                        {
                            "link": [
                                {"rel": "teaser", "href": "https://img/teaser.jpg"},
                                {"rel": "XXL", "href": "https://img/xxl.jpg"},
                            ]
                        }
                    ]
                },
                "attributes": {
                    "attribute": [
                        {
                            "name": "condition",
                            "value": [{"value": "USED"}],
                        }
                    ]
                },
                "buy-now": {"selected": False},
            }
        }
    }


class MonkrelPrivateHttpContentClientTests(unittest.TestCase):
    def test_title_update_rebuilds_current_ad_and_puts_exact_owner_id(self):
        client = FakeRawClient(owner_payload())
        writer = MonkrelPrivateHttpContentClient(client)

        writer.update_ad("3521676801", title="Neuer Titel")

        self.assertEqual(len(client.build_calls), 1)
        self.assertEqual(
            client.build_calls[0],
            {
                "title": "Neuer Titel",
                "description": "Alte Beschreibung",
                "category_id": "192",
                "location_id": "3455",
                "price": 25,
                "price_type": "FIXED",
                "poster_type": "PRIVATE",
                "ad_type": "OFFERED",
                "contact_name": "Mark",
                "email": "mark@example.invalid",
                "phone": "040000000",
                "attributes": {"condition": "USED"},
                "picture_urls": ["https://img/xxl.jpg"],
                "latitude": 53.55,
                "longitude": 9.99,
            },
        )
        self.assertEqual(
            client.calls,
            [
                (
                    "GET",
                    "https://api.kleinanzeigen.de/api/users/501/ads/3521676801.json",
                    {"authed": True},
                ),
                (
                    "PUT",
                    "https://api.kleinanzeigen.de/api/users/501/ads/3521676801",
                    {
                        "data": "<ad/>",
                        "content_type": "application/xml",
                        "authed": True,
                    },
                ),
            ],
        )

    def test_namespaced_root_value_can_include_metadata(self):
        payload = owner_payload()
        ad = payload[AD_NS]["value"]
        payload[AD_NS] = {"value": ad, "type": "ad"}

        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(client)

        writer.update_ad("3521676801", title="Neu")

        self.assertEqual(client.build_calls[0]["title"], "Neu")
        self.assertEqual(client.calls[-1][0], "PUT")

    def test_description_only_update_preserves_title(self):
        client = FakeRawClient(owner_payload())
        writer = MonkrelPrivateHttpContentClient(client)

        writer.update_ad("3521676801", description="Neu")

        self.assertEqual(client.build_calls[0]["title"], "Alter Titel")
        self.assertEqual(client.build_calls[0]["description"], "Neu")

    def test_partial_update_preserves_whitespace_in_untouched_content(self):
        payload = owner_payload()
        payload[AD_NS]["value"]["description"] = {
            "value": "  Alte Beschreibung  "
        }
        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(client)

        writer.update_ad("3521676801", title="Neu")

        self.assertEqual(
            client.build_calls[0]["description"],
            "  Alte Beschreibung  ",
        )

    def test_removing_existing_content_whitespace_is_not_a_noop(self):
        payload = owner_payload()
        payload[AD_NS]["value"]["title"] = {"value": "  Alter Titel  "}
        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(client)

        writer.update_ad("3521676801", title="Alter Titel")

        self.assertEqual(client.build_calls[0]["title"], "Alter Titel")
        self.assertEqual(client.calls[-1][0], "PUT")

    def test_requires_dedicated_single_attempt_monkrel_client(self):
        client = FakeRawClient(owner_payload(), max_retries=3)
        writer = MonkrelPrivateHttpContentClient(client)

        with self.assertRaisesRegex(RuntimeError, "max_retries=1"):
            writer.update_ad("3521676801", title="Neu")

        self.assertEqual(client.calls, [])
        self.assertEqual(client.build_calls, [])

    def test_owner_read_must_return_same_id(self):
        payload = owner_payload()
        payload[AD_NS]["value"]["id"] = 999
        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(client)

        with self.assertRaisesRegex(ValueError, "does not match"):
            writer.update_ad("3521676801", title="Neu")

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0], "GET")
        self.assertEqual(client.build_calls, [])

    def test_noop_is_rejected_before_write(self):
        client = FakeRawClient(owner_payload())
        writer = MonkrelPrivateHttpContentClient(client)

        with self.assertRaisesRegex(ValueError, "no-op"):
            writer.update_ad("3521676801", title="Alter Titel")

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0], "GET")
        self.assertEqual(client.build_calls, [])

    def test_multi_value_attribute_is_rejected(self):
        payload = owner_payload()
        payload[AD_NS]["value"]["attributes"]["attribute"][0]["value"] = [
            {"value": "A"},
            {"value": "B"},
        ]
        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(client)

        with self.assertRaisesRegex(ValueError, "multi-value"):
            writer.update_ad("3521676801", title="Neu")

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.build_calls, [])

    def test_enabled_buy_now_is_rejected(self):
        payload = owner_payload()
        payload[AD_NS]["value"]["buy-now"]["selected"] = True
        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(client)

        with self.assertRaisesRegex(ValueError, "buy-now"):
            writer.update_ad("3521676801", title="Neu")

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.build_calls, [])

    def test_nonempty_special_media_is_rejected(self):
        payload = owner_payload()
        payload[AD_NS]["value"]["medias"] = {"media": [{"id": "video-1"}]}
        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(client)

        with self.assertRaisesRegex(ValueError, "medias"):
            writer.update_ad("3521676801", title="Neu")

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.build_calls, [])

    def test_email_provider_is_used_only_when_owner_payload_has_no_email(self):
        payload = owner_payload()
        del payload[AD_NS]["value"]["email"]
        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(
            client,
            contact_email_provider=lambda: "account@example.invalid",
        )

        writer.update_ad("3521676801", title="Neu")

        self.assertEqual(
            client.build_calls[0]["email"],
            "account@example.invalid",
        )

    def test_missing_xxl_picture_blocks_update(self):
        payload = owner_payload()
        payload[AD_NS]["value"]["pictures"]["picture"][0]["link"] = [
            {"rel": "teaser", "href": "https://img/teaser.jpg"}
        ]
        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(client)

        with self.assertRaisesRegex(ValueError, "XXL"):
            writer.update_ad("3521676801", title="Neu")

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.build_calls, [])


if __name__ == "__main__":
    unittest.main()
