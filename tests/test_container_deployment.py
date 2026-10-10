"""Fail-closed structural and shell regressions for the optional Mark container.

No Docker daemon, external network, user files, or platform account is touched.
Actual root-owned UID, mapping and recovery verification is an additional
live deployment gate, not a result of these static checks.
"""
from __future__ import annotations

from pathlib import Path
import io
import os
import shutil
import subprocess
import tarfile
from tempfile import TemporaryDirectory
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
            "mark-api-container-control.sh",
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
        self.assertIn("MARK_API_IMAGE_DIGEST:?Verified immutable image digest required", compose)
        self.assertEqual(compose.count("image: sha256:"), 2)
        self.assertNotIn("image: ${MARK_API_IMAGE:", compose)
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


    def _fake_git_repo(self, root: Path) -> tuple[Path, str]:
        repo = root / "synthetic"
        repo.mkdir()
        (repo / "docs").mkdir()
        (repo / "src/mark_api").mkdir(parents=True)
        shutil.copyfile(DOCS / "mark-api-container-build.sh", repo / "docs/mark-api-container-build.sh")
        (repo / "src/mark_api/__init__.py").write_text("# mark\n")
        (repo / "pyproject.toml").write_text("[project]\nname='synthetic'\nversion='0.0.1'\n")
        (repo / "README.md").write_text("attested-version\n")
        def git(*args: str) -> str:
            p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            return p.stdout.strip()
        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        git("add", ".")
        git("commit", "-qm", "attested")
        expected = git("rev-parse", "HEAD")
        return repo, expected

    def test_rejects_replacement_commit_even_when_head_sha_looks_valid(self) -> None:
        with TemporaryDirectory(prefix="mark-git-replace-") as tmp:
            repo, expected = self._fake_git_repo(Path(tmp))
            def git(*args: str) -> str:
                p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                return p.stdout.strip()
            (repo / "README.md").write_text("untrusted-replacement\n")
            git("add", "README.md")
            git("commit", "-qm", "replacement")
            replacement = git("rev-parse", "HEAD")
            git("replace", expected, replacement)
            # HEAD still reports the original ID; normal Git archive does not.
            git("checkout", "--detach", "--force", expected)
            self.assertEqual(git("rev-parse", "HEAD"), expected)
            archived = subprocess.run(
                ["git", "-C", str(repo), "archive", expected, "--", "README.md"],
                capture_output=True, check=True,
            )
            with tarfile.open(fileobj=io.BytesIO(archived.stdout)) as archive:
                self.assertEqual(archive.extractfile("README.md").read(),
                                 b"untrusted-replacement\n")
            env = {**os.environ, "MARK_UV": "/usr/bin/false"}
            env.pop("GIT_NO_REPLACE_OBJECTS", None)
            p = subprocess.run(
                ["/bin/sh", str(repo / "docs/mark-api-container-build.sh"), expected],
                capture_output=True, text=True, env=env,
            )
            self.assertNotEqual(p.returncode, 0)
            build = _read("mark-api-container-build.sh")
            self.assertIn("export GIT_NO_REPLACE_OBJECTS=1", build)
            self.assertIn("refs/replace", build)
            self.assertTrue(
                "mark-api: source checkout is dirty" in p.stderr
                or "mark-api: Git replacement references are not allowed" in p.stderr,
                p.stderr,
            )

    def test_rejects_unused_replacement_ref_in_clean_repo(self) -> None:
        with TemporaryDirectory(prefix="mark-git-unused-replace-") as tmp:
            repo, expected = self._fake_git_repo(Path(tmp))
            (repo / "README.md").write_text("a second commit\n")
            for cmd in (
                ("add", "README.md"), ("commit", "-qm", "unrelated"),
            ):
                p = subprocess.run(["git", "-C", str(repo), *cmd],
                                   capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
            alt = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
            ).strip()
            p = subprocess.run(
                ["git", "-C", str(repo), "checkout", "--detach", expected],
                capture_output=True, text=True,
            )
            self.assertEqual(p.returncode, 0, p.stderr)
            p = subprocess.run(
                ["git", "-C", str(repo), "replace", alt, expected],
                capture_output=True, text=True,
            )
            self.assertEqual(p.returncode, 0, p.stderr)
            clean = subprocess.check_output(
                ["git", "-C", str(repo), "status", "--porcelain"], text=True
            )
            self.assertEqual(clean, "")
            result = subprocess.run(
                ["/bin/sh", str(repo / "docs/mark-api-container-build.sh"), expected],
                capture_output=True, text=True,
                env={**os.environ, "MARK_UV": "/usr/bin/false"},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("mark-api: Git replacement references are not allowed",
                          result.stderr)

    def test_rejects_git_graft_even_if_branch_and_index_look_clean(self) -> None:
        with TemporaryDirectory(prefix="mark-git-graft-") as tmp:
            repo, expected = self._fake_git_repo(Path(tmp))
            graft = repo / ".git/info/grafts"
            graft.parent.mkdir(parents=True, exist_ok=True)
            graft.write_text(expected + "\n")
            p = subprocess.run(
                ["/bin/sh", str(repo / "docs/mark-api-container-build.sh"), expected],
                capture_output=True, text=True,
                env={**os.environ, "MARK_UV": "/usr/bin/false"},
            )
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("mark-api: Git grafts are not allowed", p.stderr)

    def test_documented_product_run_uses_only_guarded_image_actions(self) -> None:
        runbook = _read("operations-runbook.md")
        self.assertIn('sh docs/mark-api-container-control.sh verify', runbook)
        self.assertIn('sh docs/mark-api-container-control.sh live', runbook)
        self.assertIn('sh docs/mark-api-container-control.sh backup', runbook)
        self.assertNotIn(
            "docker compose -f docs/mark-api-container.compose.yaml --profile live up",
            runbook,
        )
        guard = _read("mark-api-container-control.sh")
        self.assertIn("getent passwd 50042", guard)
        self.assertIn("getent group 50042", guard)
        self.assertIn("volume inspect", guard)
        self.assertIn("org.opencontainers.image.revision", guard)

    def test_guard_rejects_mutable_image_refs_before_docker(self) -> None:
        tool = str(DOCS / "mark-api-container-control.sh")
        sha = "a" * 40
        invalid_images = (
            "latest", "mark-api:pr75-21eb0bee", "sha256:latest",
            "sha256:" + "a" * 63,
            "sha256:" + "g" * 64,
            "sha256:" + "a" * 65,
        )
        for ref in invalid_images:
            with self.subTest(image=ref):
                p = subprocess.run(
                    ["/bin/sh", tool, "verify", ref, sha],
                    capture_output=True, text=True,
                )
                self.assertEqual(p.returncode, 64, p.stderr)
                self.assertIn("immutable", p.stderr.lower())

    def test_guard_rejects_unbound_commit_and_does_not_start_compose(self) -> None:
        tool = str(DOCS / "mark-api-container-control.sh")
        immutable = "sha256:" + "a" * 64
        for ref in ("21eb0bee", "z" * 40, ""):
            with self.subTest(commit=ref):
                p = subprocess.run(
                    ["/bin/sh", tool, "verify", immutable, ref],
                    capture_output=True, text=True,
                )
                self.assertEqual(p.returncode, 64, p.stderr)
        control = _read("mark-api-container-control.sh")
        self.assertIn("image inspect", control)
        self.assertIn("org.opencontainers.image.revision", control)
        self.assertIn('MARK_API_IMAGE_DIGEST="$digest"', control)
        self.assertIn("--pull never", control)

if __name__ == "__main__":
    unittest.main()