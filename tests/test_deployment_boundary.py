"""Static and adversarial tests for the trusted pre-import Mark service bootstrap.

The tests exercise an entirely synthetic installation directory, without
creating any host user, changing ownership, or contacting Kleinanzeigen.
"""
from __future__ import annotations

import configparser
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import shlex
import stat
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SOURCE_DB = "/var/lib/mark-api/mark.sqlite"
BACKUP_DIR = "/var/lib/mark-api-backups"

spec = importlib.util.spec_from_file_location(
    "mark_api_root_bootstrap", DOCS / "mark-api-preflight.py"
)
assert spec is not None and spec.loader is not None
boundary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boundary)


def _unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    with (DOCS / name).open(encoding="utf-8") as stream:
        parser.read_file(stream)
    return parser


class MarkDeploymentBoundaryTests(unittest.TestCase):
    def test_application_runs_as_dedicated_nologin_owner(self) -> None:
        service = _unit("mark-api.service")["Service"]
        for key in ("User", "Group"):
            self.assertEqual(service[key], "mark-api")
        self.assertEqual(service["ReadWritePaths"], "/var/lib/mark-api")
        self.assertNotIn("StateDirectory", service)
        self.assertEqual(service["RuntimeDirectory"], "mark-api")
        self.assertEqual(service["RuntimeDirectoryMode"], "0700")
        self.assertEqual(service["UMask"], "0077")
        lines = [
            line.strip() for line in
            (DOCS / "mark-api.sysusers.conf").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            shlex.split(lines[0]),
            ["u", "mark-api", "-", "Trusted Mark runtime owner",
             "/var/lib/mark-api", "/usr/sbin/nologin"],
        )

    def test_service_retains_normal_default_on_write_launcher(self) -> None:
        service = _unit("mark-api.service")["Service"]
        args = shlex.split(service["ExecStart"])
        self.assertEqual(
            args[:4],
            ["/opt/mark-api/venv/bin/python", "-I", "-m", "mark_api.launcher"]
        )
        self.assertEqual(args[args.index("--db") + 1], SOURCE_DB)
        self.assertNotIn("--init-db", args)
        self.assertEqual(args[args.index("--dashboard-port") + 1], "8875")
        self.assertEqual(args[args.index("--write-port") + 1], "8876")
        self.assertEqual(args[args.index("--cdp-port") + 1], "9222")

    def test_bearer_and_dashboard_url_do_not_enter_journal_stdout(self) -> None:
        service = _unit("mark-api.service")["Service"]
        self.assertEqual(service["StandardOutput"],
                         "truncate:/run/mark-api/launcher.log")
        self.assertEqual(service["RuntimeDirectoryMode"], "0700")
        self.assertEqual(service["StandardError"], "journal")

    def test_both_units_preflight_first_code_via_system_python_without_site(self) -> None:
        for filename in ("mark-api.service", "mark-api-backup@.service"):
            with self.subTest(filename=filename):
                service = _unit(filename)["Service"]
                self.assertEqual(
                    shlex.split(service["ExecStartPre"]),
                    ["/usr/bin/python3", "-I", "-S", "/etc/mark-api/preflight.py",
                     "--db", SOURCE_DB, "--backup-dir", BACKUP_DIR],
                )
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

    def test_backup_uses_create_only_isolated_package_module(self) -> None:
        service = _unit("mark-api-backup@.service")["Service"]
        args = shlex.split(service["ExecStart"])
        self.assertEqual(service["Type"], "oneshot")
        self.assertEqual(
            service["ReadWritePaths"],
            "/var/lib/mark-api /var/lib/mark-api-backups",
        )
        self.assertEqual(
            args[:4],
            ["/opt/mark-api/venv/bin/python", "-I", "-m", "mark_api.backup_cli"],
        )
        self.assertEqual(args[args.index("--db") + 1], SOURCE_DB)
        self.assertEqual(args[args.index("--backup") + 1],
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

    @contextmanager
    def _trusted_identity(self):
        account = SimpleNamespace(
            pw_uid=os.geteuid(), pw_gid=os.getegid(),
            pw_shell="/usr/sbin/nologin",
        )
        with (
            patch.object(boundary.pwd, "getpwnam", return_value=account),
            patch.object(boundary.os, "getgroups", return_value=[os.getegid()]),
        ):
            yield

    def test_valid_private_store_preflight_does_not_modify_data(self) -> None:
        with TemporaryDirectory() as tmp:
            db, backups = self._trusted_paths(Path(tmp))
            before = db.stat()
            with self._trusted_identity():
                boundary.check_deployment(db, backups)
            self.assertEqual(db.read_bytes(), b"sentinel-do-not-rewrite")
            self.assertEqual(db.stat().st_mtime_ns, before.st_mtime_ns)

    def test_preflight_rejects_unsafe_files_and_parent_modes(self) -> None:
        for bad in ("database", "data-directory", "backup-directory"):
            with self.subTest(bad=bad), TemporaryDirectory() as tmp:
                db, backups = self._trusted_paths(Path(tmp))
                target = {
                    "database": db, "data-directory": db.parent,
                    "backup-directory": backups,
                }[bad]
                target.chmod(0o644 if bad == "database" else 0o755)
                with self._trusted_identity():
                    with self.assertRaisesRegex(
                        boundary.DeploymentBoundaryError, "ownership|mode"
                    ):
                        boundary.check_deployment(db, backups)

    def test_preflight_rejects_malicious_sidecar_alias(self) -> None:
        with TemporaryDirectory() as tmp:
            db, backups = self._trusted_paths(Path(tmp))
            (db.parent / "mark.sqlite-wal").symlink_to(db)
            with self._trusted_identity():
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
            with (
                patch.object(boundary.pwd, "getpwnam", return_value=account),
                patch.object(boundary.os, "getgroups",
                             return_value=[os.getegid(), 999999]),
            ):
                with self.assertRaisesRegex(
                    boundary.DeploymentBoundaryError, "dedicated"
                ):
                    boundary.check_deployment(db, backups)

    def test_preflight_rejects_missing_dedicated_account(self) -> None:
        with TemporaryDirectory() as tmp:
            db, backups = self._trusted_paths(Path(tmp))
            with patch.object(boundary.pwd, "getpwnam", side_effect=KeyError):
                with self.assertRaisesRegex(
                    boundary.DeploymentBoundaryError, "account is absent"
                ):
                    boundary.check_deployment(db, backups)

    @contextmanager
    def _fake_root_install(self):
        # Simulate root metadata without chown or any privileged host change.
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "mark-api" / "venv"
            site = root / "lib/python3.12/site-packages"
            package = site / "mark_api"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("# mark\n", encoding="utf-8")
            third = site / "PIL"
            third.mkdir()
            (third / "Image.py").write_text("# code\n", encoding="utf-8")
            (root / "bin").mkdir()
            (root / "bin/python").write_bytes(b"python-test-executable")
            (root / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
            ancestor_paths = set(root.parents)

            def simulated_root_lstat(path):
                path = Path(path)
                actual = path.lstat()
                # Temp-directory ancestry is faked as trusted for the test.
                is_ancestor = path in ancestor_paths
                mode = actual.st_mode & ~0o022 if is_ancestor else actual.st_mode
                return SimpleNamespace(st_mode=mode, st_uid=0, st_gid=0)

            with patch.object(boundary, "_lstat", side_effect=simulated_root_lstat) as mock:
                yield root, site, mock

    def test_bootstrap_scans_all_installed_first_and_third_party_code(self) -> None:
        with self._fake_root_install() as (root, site, probe):
            boundary.check_installed_code(root)
            visited = {call.args[0] for call in probe.call_args_list}
            self.assertIn(site / "mark_api/__init__.py", visited)
            self.assertIn(site / "PIL/Image.py", visited)

    def test_preflight_rejects_untrusted_file_in_any_transitive_package(self) -> None:
        with self._fake_root_install() as (root, site, probe):
            bad = site / "PIL/Image.py"
            real = probe.side_effect

            def one_untrusted(path):
                info = real(path)
                if Path(path) == bad:
                    info.st_uid = 1001
                return info

            probe.side_effect = one_untrusted
            with self.assertRaisesRegex(
                boundary.DeploymentBoundaryError, "root-owned"
            ):
                boundary.check_installed_code(root)

    def test_preflight_rejects_group_writable_mark_module(self) -> None:
        with self._fake_root_install() as (root, site, _probe):
            module = site / "mark_api/__init__.py"
            module.chmod(0o664)
            with self.assertRaisesRegex(
                boundary.DeploymentBoundaryError, "non-writable"
            ):
                boundary.check_installed_code(root)

    def test_preflight_rejects_external_symlink_in_package(self) -> None:
        with self._fake_root_install() as (root, site, _probe):
            (site / "PIL/external.py").symlink_to("/home/attacker/module.py")
            with self.assertRaisesRegex(
                boundary.DeploymentBoundaryError, "symlink"
            ):
                boundary.check_installed_code(root)

    def test_preflight_rejects_external_editable_package_pth(self) -> None:
        with self._fake_root_install() as (root, site, _probe):
            (site / "editable.pth").write_text("/home/attacker/mark-api/src\n")
            with self.assertRaisesRegex(
                boundary.DeploymentBoundaryError, "escapes trusted roots"
            ):
                boundary.check_installed_code(root)

    def test_preflight_rejects_executable_pth_on_startup(self) -> None:
        with self._fake_root_install() as (root, site, _probe):
            (site / "injected.pth").write_text("import writable_startup_module\n")
            with self.assertRaisesRegex(
                boundary.DeploymentBoundaryError, "executable package path"
            ):
                boundary.check_installed_code(root)

    def test_preflight_rejects_nonroot_bootstrap_source(self) -> None:
        with self.assertRaisesRegex(
            boundary.DeploymentBoundaryError, "bootstrap must be installed"
        ):
            boundary._check_bootstrap_installation()


if __name__ == "__main__":
    unittest.main()
