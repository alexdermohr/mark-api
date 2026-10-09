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
            fake_usr = Path(tmp) / "usr"
            (fake_usr / "bin").mkdir(parents=True)
            (fake_usr / "bin/python3").write_bytes(b"synthetic-system-python")
            system_stdlib = fake_usr / "lib/python3.12"
            system_stdlib.mkdir(parents=True)
            (system_stdlib / "site.py").write_text("# trusted OS stdlib\n")
            (root / "pyvenv.cfg").write_text(
                f"home = {fake_usr / 'bin'}\ninclude-system-site-packages = false\n"
            )
            ancestor_paths = set(root.parents)

            def simulated_root_lstat(path):
                path = Path(path)
                actual = path.lstat()
                # Temp-directory ancestry is faked as trusted for the test.
                is_ancestor = path in ancestor_paths
                mode = actual.st_mode & ~0o022 if is_ancestor else actual.st_mode
                return SimpleNamespace(st_mode=mode, st_uid=0, st_gid=0)

            with (
                patch.object(boundary, "_lstat", side_effect=simulated_root_lstat) as mock,
                patch.object(boundary, "_SYSTEM_CODE_ROOT", fake_usr),
            ):
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
                boundary.DeploymentBoundaryError, "escapes audited install"
            ):
                boundary.check_installed_code(root)

    def test_preflight_rejects_executable_pth_on_startup(self) -> None:
        with self._fake_root_install() as (root, site, _probe):
            (site / "injected.pth").write_text("import writable_startup_module\n")
            with self.assertRaisesRegex(
                boundary.DeploymentBoundaryError, "executable package path"
            ):
                boundary.check_installed_code(root)

    def test_preflight_accepts_trusted_os_interpreter_symlink(self) -> None:
        with self._fake_root_install() as (root, site, _probe):
            python = root / "bin/python"
            python.unlink()
            python.symlink_to(root.parent.parent / "usr/bin/python3")
            boundary.check_installed_code(root)

    def test_preflight_rejects_unscanned_system_package_symlink(self) -> None:
        with self._fake_root_install() as (root, site, _probe):
            fake_usr = root.parent.parent / "usr"
            foreign = fake_usr / "lib/mark-unscanned"
            foreign.mkdir(parents=True)
            (foreign / "untrusted.py").write_text("# writable outside scanner\n")
            (site / "PIL/system-plugin").symlink_to(
                foreign, target_is_directory=True
            )
            with patch.object(boundary, "_SYSTEM_CODE_ROOT", fake_usr):
                with self.assertRaises(boundary.DeploymentBoundaryError):
                    boundary.check_installed_code(root)

    def test_preflight_rejects_pth_into_writable_system_tree(self) -> None:
        # Reproduce a root-owned .pth reaching a writable /usr subtree.
        with self._fake_root_install() as (root, site, _probe):
            fake_usr = root.parent.parent / "usr"
            foreign = fake_usr / "local/lib/mark-writable"
            foreign.mkdir(parents=True)
            foreign.chmod(0o777)
            (foreign / "payload.py").write_text("# attacker module\n")
            (site / "unexpected.pth").write_text(str(foreign) + "\n")
            with patch.object(boundary, "_SYSTEM_CODE_ROOT", fake_usr):
                with self.assertRaises(boundary.DeploymentBoundaryError):
                    boundary.check_installed_code(root)

    def test_preflight_allows_only_audited_internal_pth_paths(self) -> None:
        with self._fake_root_install() as (root, site, _probe):
            internal = site / "trusted-vendor"
            internal.mkdir()
            (internal / "library.py").write_text("# trusted module\n")
            (site / "internal.pth").write_text("trusted-vendor\n")
            boundary.check_installed_code(root)

    def test_preflight_rejects_pth_symlink_escape(self) -> None:
        with self._fake_root_install() as (root, site, _probe):
            foreign = root.parent.parent / "external-libs"
            foreign.mkdir()
            (site / "foreign").symlink_to(foreign, target_is_directory=True)
            (site / "escape.pth").write_text("foreign\n")
            with self.assertRaises(boundary.DeploymentBoundaryError):
                boundary.check_installed_code(root)

    def test_preflight_rejects_global_site_packages(self) -> None:
        with self._fake_root_install() as (root, site, _probe):
            (root / "pyvenv.cfg").write_text(
                "home = /usr/bin\ninclude-system-site-packages = true\n"
            )
            with self.assertRaises(boundary.DeploymentBoundaryError):
                boundary.check_installed_code(root)

    def test_preflight_rejects_missing_or_duplicate_site_flag(self) -> None:
        for content in (
            "home = /usr/bin\n",
            "include-system-site-packages = false\n"
            "include-system-site-packages = true\n",
        ):
            with (
                self.subTest(content=content),
                self._fake_root_install() as (root, site, _probe),
            ):
                (root / "pyvenv.cfg").write_text(content)
                with self.assertRaises(boundary.DeploymentBoundaryError):
                    boundary.check_installed_code(root)

    def test_preflight_rejects_untrusted_python_base(self) -> None:
        with self._fake_root_install() as (root, site, _probe):
            (root / "pyvenv.cfg").write_text(
                "home = /home/alex/.local/share/uv/python/bin\n"
                "include-system-site-packages = false\n"
            )
            with self.assertRaises(boundary.DeploymentBoundaryError):
                boundary.check_installed_code(root)

    def test_preflight_rejects_utf8_bom_executable_pth(self) -> None:
        # CPython site.addpackage strips a UTF-8 BOM before parsing imports.
        with self._fake_root_install() as (root, site, _probe):
            (site / "bom-execute.pth").write_bytes(
                b"\xef\xbb\xbfimport malicious_site_hook\n"
            )
            with self.assertRaisesRegex(
                boundary.DeploymentBoundaryError, "executable package path"
            ):
                boundary.check_installed_code(root)

    def test_preflight_rejects_custom_python_under_usr_with_writable_stdlib(self) -> None:
        # A trusted *prefix* does not secure derived stdlib/sitecustomize paths.
        with self._fake_root_install() as (root, site, _probe):
            fake_usr = root.parent.parent / "usr"
            home = fake_usr / "local/custom-python/bin"
            home.mkdir(parents=True)
            foreign_module = fake_usr / "local/custom-python/lib/python3.12/sitecustomize.py"
            foreign_module.parent.mkdir(parents=True)
            foreign_module.write_text("raise RuntimeError('untrusted module')\n")
            foreign_module.chmod(0o666)
            (root / "pyvenv.cfg").write_text(
                f"home = {home}\ninclude-system-site-packages = false\n"
            )
            with patch.object(boundary, "_SYSTEM_CODE_ROOT", fake_usr):
                with self.assertRaises(boundary.DeploymentBoundaryError):
                    boundary.check_installed_code(root)

    def test_preflight_rejects_writable_system_stdlib_under_trusted_bin(self) -> None:
        # Even /usr/bin may import writable /usr/lib/pythonX.Y/sitecustomize.
        with self._fake_root_install() as (root, site, _probe):
            fake_usr = root.parent.parent / "usr"
            system_bin = fake_usr / "bin"
            system_bin.mkdir(parents=True, exist_ok=True)
            system_lib = fake_usr / "lib/python3.12"
            system_lib.mkdir(parents=True, exist_ok=True)
            (system_lib / "sitecustomize.py").write_text("# attacker\n")
            (system_lib / "sitecustomize.py").chmod(0o666)
            (root / "pyvenv.cfg").write_text(
                f"home = {system_bin}\ninclude-system-site-packages = false\n"
            )
            with patch.object(boundary, "_SYSTEM_CODE_ROOT", fake_usr):
                with self.assertRaises(boundary.DeploymentBoundaryError):
                    boundary.check_installed_code(root)

    def test_preflight_rejects_bom_absolute_pth_path(self) -> None:
        with self._fake_root_install() as (root, site, _probe):
            foreign = root.parent.parent / "usr/local/python-plugins"
            foreign.mkdir(parents=True)
            (site / "bom-path.pth").write_bytes(
                b"\xef\xbb\xbf" + str(foreign).encode("utf-8") + b"\n"
            )
            with self.assertRaisesRegex(
                boundary.DeploymentBoundaryError, "escapes audited install"
            ):
                boundary.check_installed_code(root)

    def test_preflight_rejects_writable_stdlib_archive(self) -> None:
        with self._fake_root_install() as (root, site, _probe):
            archive = root.parent.parent / "usr/lib/python312.zip"
            archive.write_bytes(b"synthetic-importable-archive")
            archive.chmod(0o666)
            with self.assertRaises(boundary.DeploymentBoundaryError):
                boundary.check_installed_code(root)

    def test_preflight_rejects_stdlib_symlink_to_unscanned_os_code(self) -> None:
        with self._fake_root_install() as (root, site, _probe):
            usr = root.parent.parent / "usr"
            foreign = usr / "local/writable-plugin.py"
            foreign.parent.mkdir(parents=True)
            foreign.write_text("# external import\n")
            (usr / "lib/python3.12/alias.py").symlink_to(foreign)
            with self.assertRaises(boundary.DeploymentBoundaryError):
                boundary.check_installed_code(root)

    def test_preflight_rejects_nonroot_bootstrap_source(self) -> None:
        with self.assertRaisesRegex(
            boundary.DeploymentBoundaryError, "bootstrap must be installed"
        ):
            boundary._check_bootstrap_installation()


if __name__ == "__main__":
    unittest.main()