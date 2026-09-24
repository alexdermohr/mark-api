from __future__ import annotations

import unittest
from datetime import datetime, timezone

from mark_api.domain import AdSnapshot, LifecycleState
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

    def test_naive_timestamp_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AdSnapshot(
                ad_id="1",
                observed_at=datetime(2026, 9, 24, 12, 0),
                source="test",
            )


if __name__ == "__main__":
    unittest.main()
