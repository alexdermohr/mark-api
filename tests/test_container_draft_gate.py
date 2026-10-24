"""Synthetic direct-python-bypass regressions for the Draft-only image marker."""
from __future__ import annotations

import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mark_api import backup_cli, container_draft, dashboard_relay
from mark_api import dashboard, classification_cli, email_import_cli
from mark_api import local_smoke, private_web_runtime_smoke
from mark_api import fence_witness, launcher, legacy_migration, storage_owner


class ContainerDraftGateTests(unittest.TestCase):
    def test_missing_marker_keeps_normal_noncontainer_product_path_unchanged(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "no-marker"
            with patch.object(container_draft, "_MARKER", path):
                self.assertFalse(container_draft.container_draft_denied())

    def test_image_marker_blocks_every_direct_python_cli_before_argparse(self) -> None:
        with TemporaryDirectory() as tmp:
            marker = Path(tmp) / "UNATTESTED_DRAFT"
            marker.write_text("draft", encoding="utf-8")
            for module, entry in (
                (launcher, launcher.main),
                (dashboard_relay, dashboard_relay.relay_frontend_main),
                (fence_witness, fence_witness.main),
                (storage_owner, storage_owner.main),
                (backup_cli, backup_cli.main),
                (legacy_migration, legacy_migration.main),
                (dashboard, dashboard.main),
                (classification_cli, classification_cli.main),
                (email_import_cli, email_import_cli.main),
                (local_smoke, local_smoke.main),
                (private_web_runtime_smoke, private_web_runtime_smoke.main),
            ):
                with self.subTest(module=module.__name__), patch.object(
                    container_draft, "_MARKER", marker,
                ), patch("sys.stderr", io.StringIO()) as errors:
                    self.assertEqual(entry([]), 2)
                    self.assertIn("unattested container draft", errors.getvalue())
            self.assertEqual(marker.read_text(encoding="utf-8"), "draft")

    def test_marker_symlink_or_unreadable_path_is_not_authorization(self) -> None:
        with TemporaryDirectory() as tmp:
            marker = Path(tmp) / "UNATTESTED_DRAFT"
            marker.symlink_to(Path(tmp) / "missing")
            with patch.object(container_draft, "_MARKER", marker):
                self.assertTrue(container_draft.container_draft_denied())
            with patch.object(container_draft.os, "lstat",
                              side_effect=PermissionError):
                self.assertTrue(container_draft.container_draft_denied())


if __name__ == "__main__":
    unittest.main()
