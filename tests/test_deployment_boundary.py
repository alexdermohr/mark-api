"""Opt-in isolated systemd deployment and read-only owner preflight contracts.

These tests validate template semantics and local file permission checks.
They do not prove any real host UID/process isolation has been installed.
"""
from __future__ import annotations

import configparser
import os
from pathlib import Path
import shlex
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mark_api import deployment_boundary as boundary


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SOURCE_DB = "/var/lib/mark-api/mark.sqlite"
BACKUP_DIR = "/var/lib/mark-api-backups"


def _unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    with (DOCS / name).open(encoding="utf-8") as stream:
        parser.read_file(stream)
    return parser


class MarkDeploymentBoundaryTests(unittest.TestCase):
    def test_application_runs_as_dedicated_nologin_owner_with_private_state(self) -> None:
        service = _unit("mark-api.service")["Service"]
        for key in ("User", "Group"):
            self.assertEqual(service[key], "mark-api")
        self.assertEqual(service["ReadWritePaths"], "/var/lib/mark-api")
        self.assertNotIn("StateDirectory", service)
        self.assertEqual(service["RuntimeDirectory"], "mark-api")
        self.assertEqual(service["RuntimeDirectoryMode"], "0700")
        self.assertEqual(service["UMask"], "0077")
        sysusers = [
            line.strip() for line in
            (DOCS / "mark-api.sysusers.conf").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(len(sysusers), 1)
        self.assertEqual(
            shlex.split(sysusers[0]),
            ["u", "mark-api", "-", "Trusted Mark runtime owner",
             "/var/lib/mark-api", "/usr/sbin/nologin"],
        )

    def test_service_retains_normal_default_on_write_launcher(self) -> None:
        service = _unit("mark-api.service")["Service"]
        arguments = shlex.split(service["ExecStart"])
        self.assertEqual(arguments[0], "/opt/mark-api/venv/bin/mark-api-launch")
        self.assertEqual(arguments[arguments.index("--db") + 1], SOURCE_DB)
        self.assertNotIn("--init-db", arguments)
        self.assertEqual(arguments[arguments.index("--dashboard-port") + 1], "8875")
        self.assertEqual(arguments[arguments.index("--write-port") + 1], "8876")
        self.assertEqual(arguments[arguments.index("--cdp-port") + 1], "9222")

    def test_bearer_and_dashboard_url_do_not_enter_journal_stdout(self) -> None:
        service = _unit("mark-api.service")["Service"]
        self.assertEqual(service["StandardOutput"],
                         "truncate:/run/mark-api/launcher.log")
        self.assertEqual(service["RuntimeDirectoryMode"], "0700")
        self.assertEqual(service["StandardError"], "journal")

    def test_both_units_have_preflight_without_implicit_data_chown(self) -> None:
        for filename in ("mark-api.service", "mark-api-backup@.service"):
            with self.subTest(filename=filename):
                service = _unit(filename)["Service"]
                self.assertEqual(
                    shlex.split(service["ExecStartPre"]),
                    ["/opt/mark-api/venv/bin/python", "-I", "-m",
                     "mark_api.deployment_boundary", "--db", SOURCE_DB,
                     "--backup-dir", BACKUP_DIR],
                )
                # systemd StateDirectory implicitly chowns already-present data.
                self.assertNotIn("StateDirectory", service)
                self.assertIn("/var/lib/mark-api", service["ReadWritePaths"])
                for key, expected in (
                    ("User", "mark-api"),
                    ("Group", "mark-api"),
                    ("NoNewPrivileges", "yes"),
                    ("CapabilityBoundingSet", ""),
                    ("AmbientCapabilities", ""),
                    ("ProtectSystem", "strict"),
                    ("ProtectHome", "yes"),
                    ("PrivateTmp", "yes"),
                    ("PrivateDevices", "yes"),
                    ("RestrictNamespaces", "yes"),
                    ("RestrictSUIDSGID", "yes"),
                    ("UMask", "0077"),
                ):
                    self.assertEqual(service[key], expected)

    def test_backup_is_create_only_and_runs_as_same_trusted_uid(self) -> None:
        service = _unit("mark-api-backup@.service")["Service"]
        arguments = shlex.split(service["ExecStart"])
        self.assertEqual(service["Type"], "oneshot")
        self.assertEqual(
            service["ReadWritePaths"],
            "/var/lib/mark-api /var/lib/mark-api-backups",
        )
        self.assertEqual(arguments[0], "/opt/mark-api/venv/bin/mark-api-backup")
        self.assertEqual(arguments[arguments.index("--db") + 1], SOURCE_DB)
        self.assertEqual(arguments[arguments.index("--backup") + 1],
                         "/var/lib/mark-api-backups/mark-%i.sqlite")
        self.assertEqual(service["RestrictAddressFamilies"], "AF_UNIX")

    def _trusted_paths(self, root: Path) -> tuple[Path, Path]:
        data, backups = root / "data", root / "backups"
        data.mkdir(mode=0o700)
        backups.mkdir(mode=0o700)
        db = data / "mark.sqlite"
        db.write_bytes(b"sentinel-do-not-rewrite")
        db.chmod(0o600)
        return db, backups

    def _trusted_identity(self):
        account = SimpleNamespace(
            pw_uid=os.geteuid(), pw_gid=os.getegid(),
            pw_shell="/usr/sbin/nologin",
        )
        return (
            patch.object(boundary.pwd, "getpwnam", return_value=account),
            patch.object(boundary.os, "getgroups", return_value=[os.getegid()]),
        )

    def test_valid_private_store_preflight_does_not_modify_data(self) -> None:
        with TemporaryDirectory() as tmp:
            db, backups = self._trusted_paths(Path(tmp))
            before = db.stat()
            with self._trusted_identity()[0], self._trusted_identity()[1]:
                boundary.check_deployment(db, backups)
            self.assertEqual(db.read_bytes(), b"sentinel-do-not-rewrite")
            self.assertEqual(db.stat().st_mtime_ns, before.st_mtime_ns)

    def test_preflight_rejects_unsafe_files_and_parent_modes(self) -> None:
        for bad in ("database", "data-directory", "backup-directory"):
            with self.subTest(bad=bad), TemporaryDirectory() as tmp:
                db, backups = self._trusted_paths(Path(tmp))
                target = {
                    "database": db,
                    "data-directory": db.parent,
                    "backup-directory": backups,
                }[bad]
                target.chmod(0o644 if bad == "database" else 0o755)
                with self._trusted_identity()[0], self._trusted_identity()[1]:
                    with self.assertRaisesRegex(
                        boundary.DeploymentBoundaryError, "ownership|mode"
                    ):
                        boundary.check_deployment(db, backups)

    def test_preflight_rejects_malicious_sidecar_alias(self) -> None:
        with TemporaryDirectory() as tmp:
            db, backups = self._trusted_paths(Path(tmp))
            (db.parent / "mark.sqlite-wal").symlink_to(db)
            with self._trusted_identity()[0], self._trusted_identity()[1]:
                with self.assertRaises(boundary.DeploymentBoundaryError):
                    boundary.check_deployment(db, backups)

    def test_preflight_rejects_foreign_identity_and_groups(self) -> None:
        with TemporaryDirectory() as tmp:
            db, backups = self._trusted_paths(Path(tmp))
            account = SimpleNamespace(
                pw_uid=os.geteuid() + 1, pw_gid=os.getegid(),
                pw_shell="/usr/sbin/nologin",
            )
            with patch.object(boundary.pwd, "getpwnam", return_value=account):
                with self.assertRaisesRegex(
                    boundary.DeploymentBoundaryError, "dedicated"
                ):
                    boundary.check_deployment(db, backups)
            account.pw_uid = os.geteuid()
            with patch.object(boundary.pwd, "getpwnam", return_value=account), \
                 patch.object(boundary.os, "getgroups",
                              return_value=[os.getegid(), 999999]):
                with self.assertRaisesRegex(
                    boundary.DeploymentBoundaryError, "dedicated"
                ):
                    boundary.check_deployment(db, backups)

    def test_preflight_rejects_untrusted_checkout_installation(self) -> None:
        with patch.object(boundary, "__file__", "/home/alex/repos/mark-api/src/mark_api/deployment_boundary.py"):
            with self.assertRaisesRegex(
                boundary.DeploymentBoundaryError, "/opt"
            ):
                boundary._require_root_owned_install()


if __name__ == "__main__":
    unittest.main()
