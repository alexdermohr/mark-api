from __future__ import annotations

import unittest
from datetime import datetime, timezone

from mark_api.domain import (
    AdCreateRequest,
    AdSnapshot,
    CreateOperationReceipt,
    LifecycleState,
    OperationOutcome,
)
from mark_api.results import ReadResult, ReadStatus


NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


class ResultAndDomainTests(unittest.TestCase):
    def test_empty_success_and_http_error_are_distinct(self) -> None:
        empty = ReadResult.success_empty(())
        error = ReadResult.failure(ReadStatus.HTTP_ERROR, http_status=500)

        self.assertTrue(empty.is_success)
        self.assertFalse(error.is_success)
        self.assertEqual(empty.status, ReadStatus.SUCCESS_EMPTY)
        self.assertEqual(error.status, ReadStatus.HTTP_ERROR)

    def test_missing_metric_is_valid_and_not_coerced_to_zero(self) -> None:
        snapshot = AdSnapshot(
            ad_id="1",
            observed_at=NOW,
            source="test",
            lifecycle_state=LifecycleState.ACTIVE,
            views=None,
        )
        self.assertIsNone(snapshot.views)

    def test_negative_metric_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AdSnapshot(
                ad_id="1",
                observed_at=NOW,
                source="test",
                views=-1,
            )

    def test_fractional_metric_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AdSnapshot(
                ad_id="1",
                observed_at=NOW,
                source="test",
                views=1.5,  # type: ignore[arg-type]
            )

    def test_naive_timestamp_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AdSnapshot(
                ad_id="1",
                observed_at=datetime(2026, 9, 24, 12, 0),
                source="test",
            )


    def test_create_receipt_preserves_existing_positional_argument_order(self) -> None:
        receipt = CreateOperationReceipt(
            "create",
            NOW,
            NOW,
            OperationOutcome.AMBIGUOUS,
            "success_empty",
            "success_empty",
            "success_nonempty",
            "success_nonempty",
            None,
            True,
            None,
            "TimeoutError",
            None,
            None,
            None,
        )

        self.assertEqual(receipt.writer_error, "TimeoutError")
        self.assertIsNone(receipt.authorization_by)
        self.assertIsNone(receipt.authorization_reference)

    def test_create_request_normalizes_category_labels(self) -> None:
        request = AdCreateRequest(
            category_path=(" Haus & Garten ", " Dekoration ", " Weitere Dekoration "),
            title="Testanzeige",
            description="Beschreibung",
            price_eur=12,
        )

        self.assertEqual(
            request.category_path,
            ("Haus & Garten", "Dekoration", "Weitere Dekoration"),
        )

    def test_create_request_rejects_title_outside_current_ui_limit(self) -> None:
        with self.assertRaises(ValueError):
            AdCreateRequest(
                category_path=("Haus & Garten", "Dekoration"),
                title="x" * 66,
                description="Beschreibung",
                price_eur=12,
            )

    def test_create_request_counts_title_limit_in_utf16_code_units(self) -> None:
        boundary = "😀" * 32 + "x"
        request = AdCreateRequest(
            category_path=("Haus & Garten", "Dekoration"),
            title=boundary,
            description="Beschreibung",
            price_eur=12,
        )

        self.assertEqual(request.title, boundary)
        with self.assertRaises(ValueError):
            AdCreateRequest(
                category_path=("Haus & Garten", "Dekoration"),
                title="😀" * 33,
                description="Beschreibung",
                price_eur=12,
            )

    def test_create_request_rejects_title_line_breaks(self) -> None:
        for line_break in ("\n", "\r", "\r\n"):
            with self.subTest(line_break=repr(line_break)):
                with self.assertRaisesRegex(ValueError, "title must not contain line breaks"):
                    AdCreateRequest(
                        category_path=("Haus & Garten", "Dekoration"),
                        title=f"Test{line_break}anzeige",
                        description="Beschreibung",
                        price_eur=12,
                    )

    def test_create_request_normalizes_description_line_endings(self) -> None:
        request = AdCreateRequest(
            category_path=("Haus & Garten", "Dekoration"),
            title="Testanzeige",
            description="Erste Zeile\r\nZweite Zeile\rDritte Zeile\nVierte Zeile",
            price_eur=12,
        )

        self.assertEqual(
            request.description,
            "Erste Zeile\nZweite Zeile\nDritte Zeile\nVierte Zeile",
        )

    def test_create_request_counts_description_limit_in_utf16_code_units(self) -> None:
        boundary = "😀" * 2000
        request = AdCreateRequest(
            category_path=("Haus & Garten", "Dekoration"),
            title="Testanzeige",
            description=boundary,
            price_eur=12,
        )

        self.assertEqual(request.description, boundary)
        with self.assertRaises(ValueError):
            AdCreateRequest(
                category_path=("Haus & Garten", "Dekoration"),
                title="Testanzeige",
                description=boundary + "x",
                price_eur=12,
            )

    def test_create_request_rejects_non_positive_fixed_price(self) -> None:
        with self.assertRaises(ValueError):
            AdCreateRequest(
                category_path=("Haus & Garten", "Dekoration"),
                title="Testanzeige",
                description="Beschreibung",
                price_eur=0,
            )

    def test_create_request_requires_category_hierarchy(self) -> None:
        with self.assertRaises(ValueError):
            AdCreateRequest(
                category_path=("Haus & Garten",),
                title="Testanzeige",
                description="Beschreibung",
                price_eur=12,
            )


    def test_create_request_rejects_duplicate_category_labels(self) -> None:
        with self.assertRaises(ValueError):
            AdCreateRequest(
                category_path=("Haus & Garten", "Haus & Garten"),
                title="Testanzeige",
                description="Beschreibung",
                price_eur=12,
            )


if __name__ == "__main__":
    unittest.main()
