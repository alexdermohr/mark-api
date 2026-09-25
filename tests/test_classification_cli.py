from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from mark_api.classification_cli import main, update_classification
from mark_api.domain import AdClassification, AdSnapshot, LifecycleState
from mark_api.storage import SnapshotStore


T0 = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 25, 12, 5, tzinfo=timezone.utc)


class ClassificationCliTests(unittest.TestCase):
    def make_store(self) -> tuple[tempfile.TemporaryDirectory[str], SnapshotStore]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp, SnapshotStore(Path(tmp.name) / "mark.sqlite")

    @staticmethod
    def track(store: SnapshotStore, ad_id: str = "1") -> None:
        store.append_ad_snapshot(
            AdSnapshot(
                ad_id=ad_id,
                observed_at=T0,
                source="management",
                lifecycle_state=LifecycleState.ACTIVE,
            )
        )

    def test_unknown_ad_is_rejected_before_append(self) -> None:
        _, store = self.make_store()

        with self.assertRaisesRegex(ValueError, "unknown tracked ad_id"):
            update_classification(
                store,
                ad_id="999",
                labels={"city": "Berlin"},
                observed_at=T1,
            )

        self.assertEqual(store.classification_history("999"), ())

    def test_partial_update_preserves_unspecified_latest_labels(self) -> None:
        _, store = self.make_store()
        self.track(store)
        store.append_classification(
            AdClassification(
                ad_id="1",
                observed_at=T0,
                source="manual",
                image_type="detail",
                city="Berlin",
                text_type="short",
            )
        )

        item = update_classification(
            store,
            ad_id="1",
            labels={"title_type": "question"},
            observed_at=T1,
        )

        self.assertEqual(item.image_type, "detail")
        self.assertEqual(item.city, "Berlin")
        self.assertEqual(item.text_type, "short")
        self.assertEqual(item.title_type, "question")
        self.assertEqual(item.source, "manual-cli")
        self.assertEqual(store.latest_classification("1"), item)

    def test_explicit_clear_only_clears_requested_dimension(self) -> None:
        _, store = self.make_store()
        self.track(store)
        store.append_classification(
            AdClassification(
                ad_id="1",
                observed_at=T0,
                source="manual",
                image_type="detail",
                city="Berlin",
                text_type="short",
                title_type="question",
            )
        )

        item = update_classification(
            store,
            ad_id="1",
            labels={"city": "Leipzig"},
            clears=("text_type",),
            observed_at=T1,
        )

        self.assertEqual(item.image_type, "detail")
        self.assertEqual(item.city, "Leipzig")
        self.assertIsNone(item.text_type)
        self.assertEqual(item.title_type, "question")

    def test_non_newer_timestamp_is_rejected_without_append(self) -> None:
        _, store = self.make_store()
        self.track(store)
        store.append_classification(
            AdClassification(
                ad_id="1",
                observed_at=T1,
                source="manual",
                city="Berlin",
            )
        )

        with self.assertRaisesRegex(ValueError, "later than the latest"):
            update_classification(
                store,
                ad_id="1",
                labels={"city": "Leipzig"},
                observed_at=T0,
            )

        history = store.classification_history("1")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].city, "Berlin")

    def test_set_and_clear_same_dimension_is_rejected(self) -> None:
        _, store = self.make_store()
        self.track(store)

        with self.assertRaisesRegex(ValueError, "cannot set and clear"):
            update_classification(
                store,
                ad_id="1",
                labels={"city": "Berlin"},
                clears=("city",),
                observed_at=T1,
            )

        self.assertEqual(store.classification_history("1"), ())

    def test_empty_and_semantic_noop_updates_are_rejected(self) -> None:
        _, store = self.make_store()
        self.track(store)

        with self.assertRaisesRegex(ValueError, "at least one"):
            update_classification(store, ad_id="1", observed_at=T1)

        store.append_classification(
            AdClassification(
                ad_id="1",
                observed_at=T0,
                source="manual",
                city="Berlin",
            )
        )
        with self.assertRaisesRegex(ValueError, "would not change"):
            update_classification(
                store,
                ad_id="1",
                labels={"city": "Berlin"},
                observed_at=T1,
            )
        with self.assertRaisesRegex(ValueError, "would not change"):
            update_classification(
                store,
                ad_id="1",
                clears=("image_type",),
                observed_at=T1,
            )

        self.assertEqual(len(store.classification_history("1")), 1)

    def test_main_appends_local_snapshot_and_emits_json(self) -> None:
        tmp, store = self.make_store()
        self.track(store, "42")
        output = io.StringIO()

        with redirect_stdout(output):
            result = main(
                [
                    "--db",
                    str(Path(tmp.name) / "mark.sqlite"),
                    "--ad-id",
                    "42",
                    "--city",
                    "Dresden",
                    "--image-type",
                    "overview",
                ]
            )

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["ad_id"], "42")
        self.assertEqual(payload["city"], "Dresden")
        self.assertEqual(payload["image_type"], "overview")
        self.assertEqual(payload["source"], "manual-cli")
        self.assertIsNone(payload["text_type"])

        stored = store.latest_classification("42")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.city, "Dresden")
        self.assertEqual(stored.image_type, "overview")

    def test_main_clear_flag_maps_cli_name_and_preserves_other_labels(self) -> None:
        tmp, store = self.make_store()
        self.track(store, "42")
        store.append_classification(
            AdClassification(
                ad_id="42",
                observed_at=T0,
                source="manual",
                city="Dresden",
                text_type="short",
                title_type="question",
            )
        )
        output = io.StringIO()

        with redirect_stdout(output):
            result = main(
                [
                    "--db",
                    str(Path(tmp.name) / "mark.sqlite"),
                    "--ad-id",
                    "42",
                    "--clear",
                    "text-type",
                ]
            )

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["city"], "Dresden")
        self.assertIsNone(payload["text_type"])
        self.assertEqual(payload["title_type"], "question")


if __name__ == "__main__":
    unittest.main()
