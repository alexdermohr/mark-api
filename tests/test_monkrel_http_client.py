from __future__ import annotations

import unittest

from mark_api.adapters.monkrel_http import MonkrelPrivateHttpContentClient


AD_NS = "{http://www.ebayclassifiedsgroup.com/schema/ad/v1}ad"
SHIPPING_NS = "http://www.ebayclassifiedsgroup.com/schema/shipping/v1"
PAYMENT_NS = "http://www.ebayclassifiedsgroup.com/schema/payment/v1"


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeRawClient:
    def __init__(self, payload, *, max_retries=1, built_xml="<ad/>"):
        self.payload = payload
        self.max_retries = max_retries
        self.user_id = "501"
        self.built_xml = built_xml
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
        return self.built_xml


def owner_payload(*, shipping_options=(), buy_now=False):
    ad = {
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
            "show-full-address": {"value": False},
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
        "buy-now": {"selected": buy_now},
    }
    if shipping_options:
        ad["shipping-options"] = {
            "shipping-option": [{"id": option_id} for option_id in shipping_options]
        }
    return {AD_NS: {"value": ad}}


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

    def test_read_invariant_captures_full_owner_state_without_writing(self):
        client = FakeRawClient(
            owner_payload(
                shipping_options=("HERMES_002", "HERMES_001", "DHL_001")
            )
        )
        writer = MonkrelPrivateHttpContentClient(client)

        state = writer.read_invariant("3521676801")

        self.assertEqual(state.ad_id, "3521676801")
        self.assertEqual(state.title, "Alter Titel")
        self.assertEqual(state.description, "Alte Beschreibung")
        self.assertEqual(state.category_id, "192")
        self.assertEqual(state.location_id, "3455")
        self.assertEqual(state.price_type, "FIXED")
        self.assertEqual(state.price_amount, "25")
        self.assertEqual(state.attributes, (("condition", ("USED",)),))
        self.assertEqual(
            state.pictures,
            (
                (
                    ("teaser", "https://img/teaser.jpg"),
                    ("XXL", "https://img/xxl.jpg"),
                ),
            ),
        )
        self.assertEqual(
            state.shipping_option_ids,
            ("HERMES_002", "HERMES_001", "DHL_001"),
        )
        self.assertFalse(state.buy_now_selected)
        self.assertTrue(state.shipping_metadata_empty)
        self.assertTrue(state.medias_empty)
        self.assertTrue(state.product_safety_empty)
        self.assertFalse(state.show_full_address)
        self.assertIsNone(state.imprint)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0], "GET")
        self.assertEqual(client.build_calls, [])

    def test_read_invariant_preserves_attribute_order(self):
        payload = owner_payload()
        payload[AD_NS]["value"]["attributes"]["attribute"] = [
            {"name": "second", "value": [{"value": "B"}]},
            {"name": "first", "value": [{"value": "A"}]},
        ]
        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(client)

        state = writer.read_invariant("3521676801")

        self.assertEqual(
            state.attributes,
            (("second", ("B",)), ("first", ("A",))),
        )

    def test_nonempty_collection_without_expected_child_fails_closed(self):
        cases = (
            (
                "attributes",
                {
                    "unexpected": [
                        {"name": "condition", "value": [{"value": "USED"}]}
                    ]
                },
                "attribute",
            ),
            ("pictures", {"count": 1}, "picture"),
            ("shipping-options", {"count": 1}, "shipping-option"),
        )
        for field, malformed, child in cases:
            with self.subTest(field=field):
                payload = owner_payload()
                payload[AD_NS]["value"][field] = malformed
                client = FakeRawClient(payload)
                writer = MonkrelPrivateHttpContentClient(client)

                with self.assertRaisesRegex(
                    ValueError,
                    rf"{field}.*missing {child}",
                ):
                    writer.update_ad("3521676801", title="Neu")

                self.assertEqual([call[0] for call in client.calls], ["GET"])
                self.assertEqual(client.build_calls, [])

    def test_empty_collection_child_with_nonempty_metadata_fails_closed(self):
        cases = (
            ("attributes", "attribute"),
            ("pictures", "picture"),
            ("shipping-options", "shipping-option"),
            ("locations", "location"),
        )
        for field, child in cases:
            with self.subTest(field=field):
                payload = owner_payload(shipping_options=("HERMES_001",))
                payload[AD_NS]["value"][field] = {child: [], "count": 1}
                client = FakeRawClient(payload)
                writer = MonkrelPrivateHttpContentClient(client)

                with self.assertRaisesRegex(
                    ValueError,
                    rf"{field}.*empty {child}.*nonempty metadata",
                ):
                    writer.update_ad("3521676801", title="Neu")

                self.assertEqual([call[0] for call in client.calls], ["GET"])
                self.assertEqual(client.build_calls, [])

    def test_empty_collection_child_with_zero_metadata_remains_empty(self):
        cases = (
            ("attributes", "attribute", "attributes"),
            ("pictures", "picture", "pictures"),
            ("shipping-options", "shipping-option", "shipping_option_ids"),
        )
        for field, child, invariant_field in cases:
            with self.subTest(field=field):
                payload = owner_payload(shipping_options=("HERMES_001",))
                payload[AD_NS]["value"][field] = {child: [], "count": 0}
                client = FakeRawClient(payload)
                writer = MonkrelPrivateHttpContentClient(client)

                state = writer.read_invariant("3521676801")

                self.assertEqual(getattr(state, invariant_field), ())
                self.assertEqual([call[0] for call in client.calls], ["GET"])
                self.assertEqual(client.build_calls, [])

    def test_populated_collection_count_must_match_items(self):
        cases = (
            ("attributes", "attribute"),
            ("pictures", "picture"),
            ("shipping-options", "shipping-option"),
            ("locations", "location"),
        )
        for field, child in cases:
            with self.subTest(field=field):
                payload = owner_payload(shipping_options=("HERMES_001",))
                payload[AD_NS]["value"][field]["count"] = 2
                client = FakeRawClient(payload)
                writer = MonkrelPrivateHttpContentClient(client)

                with self.assertRaisesRegex(
                    ValueError,
                    rf"{field}.*count metadata.*{child}",
                ):
                    writer.update_ad("3521676801", title="Neu")

                self.assertEqual([call[0] for call in client.calls], ["GET"])
                self.assertEqual(client.build_calls, [])

    def test_populated_collection_matching_count_remains_readable(self):
        payload = owner_payload(shipping_options=("HERMES_001",))
        ad = payload[AD_NS]["value"]
        ad["attributes"]["count"] = 1
        ad["pictures"]["count"] = 1
        ad["shipping-options"]["count"] = 1
        ad["locations"]["count"] = 1
        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(client)

        state = writer.read_invariant("3521676801")

        self.assertEqual(state.attributes, (("condition", ("USED",)),))
        self.assertEqual(len(state.pictures), 1)
        self.assertEqual(state.shipping_option_ids, ("HERMES_001",))
        self.assertEqual(state.location_id, "3455")
        self.assertEqual([call[0] for call in client.calls], ["GET"])
        self.assertEqual(client.build_calls, [])

    def test_populated_collection_unknown_nonempty_metadata_fails_closed(self):
        cases = (
            ("attributes", "attribute"),
            ("pictures", "picture"),
            ("shipping-options", "shipping-option"),
            ("locations", "location"),
        )
        for field, child in cases:
            with self.subTest(field=field):
                payload = owner_payload(shipping_options=("HERMES_001",))
                payload[AD_NS]["value"][field]["unexpected"] = 1
                client = FakeRawClient(payload)
                writer = MonkrelPrivateHttpContentClient(client)

                with self.assertRaisesRegex(
                    ValueError,
                    rf"{field}.*populated {child}.*nonempty metadata",
                ):
                    writer.update_ad("3521676801", title="Neu")

                self.assertEqual([call[0] for call in client.calls], ["GET"])
                self.assertEqual(client.build_calls, [])

    def test_blank_attribute_member_fails_closed_before_builder_and_put(self):
        payload = owner_payload()
        payload[AD_NS]["value"]["attributes"]["attribute"][0]["value"] = [
            {"value": "USED"},
            {"value": ""},
        ]
        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(client)

        with self.assertRaisesRegex(ValueError, "attribute condition has blank value"):
            writer.update_ad("3521676801", title="Neu")

        self.assertEqual([call[0] for call in client.calls], ["GET"])
        self.assertEqual(client.build_calls, [])

    def test_explicit_null_optional_wrappers_remain_update_safe(self):
        payload = owner_payload()
        ad = payload[AD_NS]["value"]
        ad["contact-name"] = {"value": None}
        ad["phone"] = {"value": None}
        ad["imprint"] = {"value": None}
        ad["ad-address"]["latitude"] = {"value": None}
        ad["ad-address"]["longitude"] = {"value": None}
        ad["price"]["amount"] = {"value": None}
        ad["price"]["price-type"] = {"value": "FREE"}

        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(client)

        writer.update_ad("3521676801", title="Neu")

        self.assertEqual(client.build_calls[0]["contact_name"], "")
        self.assertIsNone(client.build_calls[0]["phone"])
        self.assertIsNone(client.build_calls[0]["latitude"])
        self.assertIsNone(client.build_calls[0]["longitude"])
        self.assertIsNone(client.build_calls[0]["price"])
        self.assertEqual(client.build_calls[0]["price_type"], "FREE")
        self.assertEqual([call[0] for call in client.calls], ["GET", "PUT"])

    def test_boolean_reconstructed_scalars_fail_closed(self):
        cases = (
            (
                "attribute",
                lambda ad: ad["attributes"]["attribute"][0]["value"][0].__setitem__(
                    "value", True
                ),
            ),
            (
                "location-id",
                lambda ad: ad["locations"]["location"][0].__setitem__("id", True),
            ),
            (
                "shipping-option-id",
                lambda ad: ad["shipping-options"]["shipping-option"][0].__setitem__(
                    "id", True
                ),
            ),
            (
                "picture-href",
                lambda ad: ad["pictures"]["picture"][0]["link"][1].__setitem__(
                    "href", True
                ),
            ),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                payload = owner_payload(shipping_options=("HERMES_001",))
                mutate(payload[AD_NS]["value"])
                client = FakeRawClient(payload)
                writer = MonkrelPrivateHttpContentClient(client)

                with self.assertRaisesRegex(ValueError, "boolean"):
                    writer.update_ad("3521676801", title="Neu")

                self.assertEqual([call[0] for call in client.calls], ["GET"])
                self.assertEqual(client.build_calls, [])

    def test_read_invariant_rejects_nested_decoy_for_missing_required_field(self):
        payload = owner_payload()
        ad = payload[AD_NS]["value"]
        del ad["title"]
        ad["metadata"] = {"title": {"value": "decoy title"}}
        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(client)

        with self.assertRaisesRegex(ValueError, "missing title"):
            writer.read_invariant("3521676801")

        self.assertEqual([call[0] for call in client.calls], ["GET"])
        self.assertEqual(client.build_calls, [])

    def test_read_invariant_rejects_ambiguous_direct_local_names(self):
        payload = owner_payload()
        payload[AD_NS]["value"]["{urn:decoy}title"] = {
            "value": "decoy title"
        }
        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(client)

        with self.assertRaisesRegex(ValueError, "ambiguous title"):
            writer.read_invariant("3521676801")

        self.assertEqual([call[0] for call in client.calls], ["GET"])
        self.assertEqual(client.build_calls, [])

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
        payload[AD_NS]["value"]["description"] = {"value": "  Alte Beschreibung  "}
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

    def test_enabled_buy_now_is_rejected_before_builder_and_put(self):
        client = FakeRawClient(owner_payload(buy_now=True))
        writer = MonkrelPrivateHttpContentClient(client)

        with self.assertRaisesRegex(ValueError, "buy-now=true"):
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

    def test_full_address_true_is_rejected(self):
        payload = owner_payload()
        payload[AD_NS]["value"]["ad-address"]["show-full-address"]["value"] = True
        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(client)

        with self.assertRaisesRegex(ValueError, "show-full-address=true"):
            writer.update_ad("3521676801", title="Neu")

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.build_calls, [])

    def test_imprint_is_rejected(self):
        payload = owner_payload()
        payload[AD_NS]["value"]["imprint"] = {"value": "Impressum"}
        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(client)

        with self.assertRaisesRegex(ValueError, "imprint"):
            writer.update_ad("3521676801", title="Neu")

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.build_calls, [])

    def test_shipping_options_are_injected_before_buy_now_marker(self):
        payload = owner_payload(
            shipping_options=("HERMES_002", "HERMES_001", "DHL_001")
        )
        base_xml = (
            f'<ad xmlns:shipping="{SHIPPING_NS}" xmlns:payment="{PAYMENT_NS}">'
            '<payment:buy-now selected="false"/></ad>'
        )
        client = FakeRawClient(payload, built_xml=base_xml)
        writer = MonkrelPrivateHttpContentClient(client)

        writer.update_ad("3521676801", title="Neu")

        put_xml = client.calls[-1][2]["data"]
        expected_shipping = (
            "<shipping:shipping-options>"
            '<shipping:shipping-option id="HERMES_002"/>'
            '<shipping:shipping-option id="HERMES_001"/>'
            '<shipping:shipping-option id="DHL_001"/>'
            "</shipping:shipping-options>"
        )
        self.assertIn(expected_shipping, put_xml)
        self.assertLess(
            put_xml.index(expected_shipping),
            put_xml.index('<payment:buy-now selected="false"/>'),
        )
        self.assertEqual(len(client.build_calls), 1)
        self.assertEqual(client.calls[-1][0], "PUT")

    def test_shipping_options_reject_builder_that_already_emits_shipping_block(self):
        payload = owner_payload(shipping_options=("HERMES_001",))
        client = FakeRawClient(
            payload,
            built_xml=(
                f'<ad xmlns:shipping="{SHIPPING_NS}" '
                f'xmlns:payment="{PAYMENT_NS}">'
                "<shipping:shipping-options />"
                '<payment:buy-now selected="false"/>'
                "</ad>"
            ),
        )
        writer = MonkrelPrivateHttpContentClient(client)

        with self.assertRaisesRegex(RuntimeError, "already emits shipping options"):
            writer.update_ad("3521676801", title="Neu")

        self.assertEqual(len(client.build_calls), 1)
        self.assertEqual([call[0] for call in client.calls], ["GET"])

    def test_shipping_options_require_shipping_namespace(self):
        payload = owner_payload(shipping_options=("HERMES_001",))
        client = FakeRawClient(
            payload,
            built_xml='<ad><payment:buy-now selected="false"/></ad>',
        )
        writer = MonkrelPrivateHttpContentClient(client)

        with self.assertRaisesRegex(RuntimeError, "shipping namespace"):
            writer.update_ad("3521676801", title="Neu")

        self.assertEqual(len(client.build_calls), 1)
        self.assertEqual([call[0] for call in client.calls], ["GET"])

    def test_shipping_options_require_expected_buy_now_false_marker(self):
        payload = owner_payload(shipping_options=("HERMES_001",))
        client = FakeRawClient(
            payload,
            built_xml=f'<ad xmlns:shipping="{SHIPPING_NS}"></ad>',
        )
        writer = MonkrelPrivateHttpContentClient(client)

        with self.assertRaisesRegex(RuntimeError, "buy-now=false marker"):
            writer.update_ad("3521676801", title="Neu")

        self.assertEqual(len(client.build_calls), 1)
        self.assertEqual([call[0] for call in client.calls], ["GET"])

    def test_missing_owner_email_fails_closed_before_builder_even_with_provider(self):
        payload = owner_payload()
        del payload[AD_NS]["value"]["email"]
        provider_calls = []
        client = FakeRawClient(payload)
        writer = MonkrelPrivateHttpContentClient(
            client,
            contact_email_provider=lambda: provider_calls.append(True)
            or "account@example.invalid",
        )

        with self.assertRaisesRegex(ValueError, "owner ad email is unavailable"):
            writer.update_ad("3521676801", title="Neu")

        self.assertEqual([call[0] for call in client.calls], ["GET"])
        self.assertEqual(client.build_calls, [])
        self.assertEqual(provider_calls, [])

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