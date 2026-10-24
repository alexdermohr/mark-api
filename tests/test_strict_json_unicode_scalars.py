"""UTF-8 scalar admission must be identical for direct and owner Write paths.

Only deterministic local JSON and a temporary synthetic SQLite file are used.
No browser, marketplace account, external network or paid provider is involved.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from mark_api.storage import SnapshotStore
from mark_api.strict_json import strict_json_object


class StrictJsonUnicodeScalarTests(unittest.TestCase):
    def test_unpaired_surrogates_are_rejected_in_values_and_member_names(self) -> None:
        for raw in (
            b'{"title":"\\ud800"}',
            b'{"title":"\\udfff"}',
            b'{"nested":[{"\\ud800":"invalid key"}]}',
            b'{"title":["valid",{"bad":"\\udc00"}]}',
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                strict_json_object(raw)

    def test_paired_surrogates_and_literal_backslash_escape_remain_valid(self) -> None:
        self.assertEqual(
            strict_json_object(b'{"title":"\\ud800\\udc00"}'),
            {"title": "\U00010000"},
        )
        self.assertEqual(
            strict_json_object(b'{"title":"\\\\ud800"}'),
            {"title": r"\ud800"},
        )

    def test_direct_pending_claim_refuses_surrogate_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scalar-pending-") as folder:
            store = SnapshotStore(Path(folder) / "synthetic.sqlite")
            for index, payload in enumerate((
                '{"title":"\\ud800"}',
                '{"metadata":{"\\udc00":"value"}}',
            )):
                with self.subTest(index=index), self.assertRaises(ValueError):
                    store.claim_dashboard_pending_write(
                        scope="create", resource_key="create",
                        idempotency_key=f"synthetic:scalar:{index}",
                        method="POST", path="/api/write/ads",
                        payload_json=payload, ad_id=None,
                    )
                self.assertEqual(store.dashboard_pending_writes(), ())


if __name__ == "__main__":
    unittest.main()
