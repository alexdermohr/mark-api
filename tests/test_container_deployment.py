"""Fail-closed structural and shell regressions for the optional Mark container.

No Docker daemon, external network, user files, or platform account is touched.
Actual root-owned UID, mapping and recovery verification is an additional
live deployment gate, not a result of these static checks.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def _read(filename: str) -> str:
    return (DOCS / filename).read_text(encoding="utf-8")


class ContainerDeploymentTests(unittest.TestCase):
    def test_scripts_are_valid_posix_shell(self) -> None:
        for filename in (
            "mark-api-container-start.sh",
            "mark-api-container-backup.sh",
            "mark-api-container-build.sh",
        ):
            with self.subTest(filename=filename):
                result = subprocess.run(
                    ["/bin/sh", "-n", str(DOCS / filename)],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_image_has_attested_distro_and_no_mutable_code_owner(self) -> None:
        image = _read("mark-api-container.Dockerfile")
        self.assertIn("FROM ubuntu:24.04@sha256:", image)
        self.assertIn("python3-minimal python3.12 python3.12-venv", image)
        self.assertIn("RUN /usr/bin/python3.12 -m venv /opt/mark-api/venv", image)
        self.assertIn('COPY --chmod=0644 mark-api-bootstrap.sh', image)
        self.assertIn('COPY --chmod=0644 mark-api-preflight.py', image)
        self.assertIn('RUN /bin/sh /etc/mark-api/bootstrap.sh --help', image)
        self.assertIn('USER mark-api:mark-api', image)
        self.assertNotIn('USER root', image)
        self.assertIn('ARG MARK_UID=50042', image)
        self.assertIn('ARG MARK_GID=50042', image)
        self.assertIn('! getent passwd "$MARK_UID"', image)
        self.assertIn('! getent group "$MARK_GID"', image)
        self.assertIn('install -d -o mark-api -g mark-api -m 0700', image)

    def test_release_wheel_is_noneditable_and_deps_explicit(self) -> None:
        image = _read("mark-api-container.Dockerfile")
        self.assertIn('set -- /opt/mark-api-wheels/*.whl', image)
        self.assertIn('[ "$#" -eq 1 ]', image)
        self.assertIn('pip install --no-index --no-deps "$1"', image)
        self.assertIn("Pillow==12.3.0 websocket-client==1.9.2", image)
        self.assertNotIn('pip install -e', image)
        self.assertNotIn('COPY . /', image)

    def test_product_start_checks_deployment_before_default_on_write_launcher(self) -> None:
        script = _read("mark-api-container-start.sh")
        preflight = '/bin/sh /etc/mark-api/bootstrap.sh'
        launcher = 'exec /opt/mark-api/venv/bin/python -I -m mark_api.launcher'
        self.assertLess(script.index(preflight), script.index(launcher))
        for flag in ("--db /var/lib/mark-api/mark.sqlite", "--cdp-port 9222",
                     "--dashboard-port 8875", "--write-port 8876"):
            self.assertIn(flag, script)
        self.assertNotIn("--init-db", script)
        self.assertNotIn("--disable-writes", script)
        self.assertIn("> /run/mark-api/launcher.log", script)
        self.assertIn("umask 077", script)

    def test_backup_is_create_only_and_bounded_to_a_valid_instance(self) -> None:
        script = _read("mark-api-container-backup.sh")
        self.assertIn('instance=$1', script)
        self.assertIn("*[!A-Za-z0-9_-]*", script)
        self.assertIn('[ "${#instance}" -le 48 ]', script)
        self.assertLess(
            script.index("/bin/sh /etc/mark-api/bootstrap.sh"),
            script.index("exec /opt/mark-api/venv/bin/python"),
        )
        self.assertIn("-m mark_api.backup_cli", script)
        self.assertIn('mark-${instance}.sqlite', script)
        for args in ((), ("../escape",), ("",), ("a/b",), ("x" * 49,)):
            with self.subTest(args=args):
                run = subprocess.run(
                    ["/bin/sh", str(DOCS / "mark-api-container-backup.sh"), *args],
                    capture_output=True, check=False,
                )
                self.assertEqual(run.returncode, 64, run.stderr)

    def test_compose_requires_preexisting_private_volumes_and_does_not_autostart(self) -> None:
        compose = _read("mark-api-container.compose.yaml")
        self.assertIn("MARK_API_IMAGE:?Exact verified image ID required", compose)
        self.assertEqual(compose.count('user: "50042:50042"'), 2)
        self.assertEqual(compose.count("read_only: true\n    cap_drop:"), 2)
        self.assertEqual(compose.count('cap_drop: ["ALL"]'), 2)
        self.assertEqual(compose.count('security_opt: ["no-new-privileges:true"]'), 2)
        self.assertEqual(compose.count('restart: "no"'), 2)
        self.assertIn('profiles: ["live"]', compose)
        self.assertIn('profiles: ["backup"]', compose)
        self.assertIn('network_mode: host', compose)
        self.assertIn('network_mode: none', compose)
        self.assertIn('target: /var/lib/mark-api', compose)
        self.assertIn('target: /var/lib/mark-api-backups', compose)
        self.assertIn('read_only: true', compose)
        self.assertIn("/run/mark-api:rw,nosuid,nodev,noexec", compose)
        self.assertEqual(compose.count("external: true"), 2)
        self.assertNotIn("privileged: true", compose)
        self.assertNotIn("pid: host", compose)
        self.assertNotIn("userns_mode: host", compose)

    def test_release_image_build_uses_only_exact_committed_archive(self) -> None:
        build = _read("mark-api-container-build.sh")
        self.assertIn('rev-parse --verify HEAD', build)
        self.assertIn('[ "$head" = "$expected" ]', build)
        self.assertIn('status --porcelain --untracked-files=normal', build)
        self.assertIn('git -C "$repo" archive "$expected"', build)
        self.assertIn('build --wheel --offline', build)
        self.assertIn('org.opencontainers.image.revision=$expected', build)
        self.assertIn('image inspect', build)
        self.assertIn('--host unix:///var/run/docker.sock', build)
        self.assertNotIn('docker push', build)
        self.assertNotIn('docker run', build)


if __name__ == "__main__":
    unittest.main()
