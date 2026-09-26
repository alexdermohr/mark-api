from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from mark_api.classification_cli import (
    _classification_update_lock,
    main,
    update_classification,
)
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
                labels={"city": "  Berlin  "},
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

    def test_cross_process_cli_serializes_read_merge_append(self) -> None:
        tmp, store = self.make_store()
        self.track(store, "42")
        store.append_classification(
            AdClassification(
                ad_id="42",
                observed_at=T1,
                source="manual",
                city="Dresden",
            )
        )
        db_path = Path(tmp.name) / "mark.sqlite"
        marker = Path(tmp.name) / "child-ready"
        root = Path(__file__).resolve().parents[1]
        child_script = (
            "from pathlib import Path\n"
            "from mark_api.classification_cli import main\n"
            f"Path({str(marker)!r}).write_text('ready', encoding='utf-8')\n"
            "raise SystemExit(main(["
            f"{str('--db')!r}, {str(db_path)!r}, "
            f"{str('--ad-id')!r}, {str('42')!r}, "
            f"{str('--title-type')!r}, {str('question')!r}"
            "]))\n"
        )
        env = os.environ.copy()
        src_path = str(root / "src")
        if env.get("PYTHONPATH"):
            env["PYTHONPATH"] = src_path + os.pathsep + env["PYTHONPATH"]
        else:
            env["PYTHONPATH"] = src_path

        process: subprocess.Popen[str] | None = None
        try:
            with _classification_update_lock(store):
                process = subprocess.Popen(
                    [sys.executable, "-c", child_script],
                    cwd=root,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists(), "child did not reach CLI entrypoint")
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.3)

                store.append_classification(
                    AdClassification(
                        ad_id="42",
                        observed_at=datetime.now(timezone.utc),
                        source="manual-parent",
                        image_type="detail",
                        city="Dresden",
                    )
                )

            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)

        payload = json.loads(stdout)
        self.assertEqual(payload["city"], "Dresden")
        self.assertEqual(payload["image_type"], "detail")
        self.assertEqual(payload["title_type"], "question")

        latest = store.latest_classification("42")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.city, "Dresden")
        self.assertEqual(latest.image_type, "detail")
        self.assertEqual(latest.title_type, "question")

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
