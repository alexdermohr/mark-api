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
        self.assertIn('attested_git archive "$expected"', build)
        self.assertIn('attested_git status --porcelain', build)
        self.assertIn('--git-dir="$temp/git"', build)
        self.assertIn('GIT_ALTERNATE_OBJECT_DIRECTORIES="$common/objects"', build)
        self.assertNotIn('safe_git -C "$repo" status --porcelain', build)
        self.assertIn('build --wheel --offline', build)
        self.assertIn('org.opencontainers.image.revision=$expected', build)
        self.assertIn('image inspect', build)
        self.assertIn('--host unix:///var/run/docker.sock', build)
        self.assertNotIn('docker push', build)
        self.assertNotIn('docker run', build)
        for script in (build, _read("mark-api-container-control.sh")):
            self.assertIn("safe_git() {", script)
            self.assertIn("/usr/bin/env -i", script)
            self.assertIn("GIT_CONFIG_NOSYSTEM=1", script)
            self.assertIn("GIT_CONFIG_GLOBAL=/dev/null", script)
            self.assertIn("-c core.fsmonitor=false", script)
            self.assertIn("-c core.hooksPath=/dev/null", script)
            self.assertIn("GIT_ALTERNATE_OBJECT_DIRECTORIES=", script)
            self.assertIn("attested_git status --porcelain", script)
            self.assertNotIn('safe_git -C "$repo" status --porcelain', script)


    def _fake_git_repo(self, root: Path) -> tuple[Path, str]:
        repo = root / "synthetic"
        repo.mkdir()
        (repo / "docs").mkdir()
        (repo / "src/mark_api").mkdir(parents=True)
        shutil.copyfile(DOCS / "mark-api-container-build.sh", repo / "docs/mark-api-container-build.sh")
        shutil.copyfile(DOCS / "mark-api-container-control.sh", repo / "docs/mark-api-container-control.sh")
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

    def test_untrusted_git_fsmonitor_cannot_execute(self) -> None:
        # Both environment-injected and checkout-local executable config must
        # be inert before any git status or revision validation runs.
        with TemporaryDirectory(prefix="mark-git-fsmonitor-") as tmp:
            root = Path(tmp)
            repo, expected = self._fake_git_repo(root)
            # Force a tracked-content refresh: fsmonitor is not called on a cache hit.
            (repo / "README.md").write_text("dirty tracked content\\n")
            marker = root / "fsmonitor-executed"
            hook = root / "fsmonitor.sh"
            hook.write_text(
                "#!/bin/sh\n"
                + "printf triggered > '" + str(marker) + "'\n"
                + "printf 'token\\000/\\000'\n",
                encoding="utf-8",
            )
            hook.chmod(0o700)
            paths = (
                (repo / "docs/mark-api-container-build.sh", [expected]),
                (repo / "docs/mark-api-container-control.sh",
                 ["verify", "sha256:" + "a" * 64, expected]),
            )
            for script, arguments in paths:
                for source in ("environment", "checkout-local"):
                    with self.subTest(script=script.name, source=source):
                        marker.unlink(missing_ok=True)
                        env = {**os.environ, "MARK_UV": "/usr/bin/false"}
                        env.pop("GIT_CONFIG_COUNT", None)
                        for key in list(env):
                            if key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
                                env.pop(key)
                        conf = subprocess.run(
                            ["git", "-C", str(repo), "config", "--unset-all", "core.fsmonitor"],
                            capture_output=True, text=True, env=os.environ.copy(),
                        )
                        self.assertIn(conf.returncode, (0, 5), conf.stderr)
                        if source == "environment":
                            env.update(
                                GIT_CONFIG_COUNT="1",
                                GIT_CONFIG_KEY_0="core.fsmonitor",
                                GIT_CONFIG_VALUE_0=str(hook),
                            )
                        else:
                            conf = subprocess.run(
                                ["git", "-C", str(repo), "config", "core.fsmonitor", str(hook)],
                                capture_output=True, text=True, env=os.environ.copy(),
                            )
                            self.assertEqual(conf.returncode, 0, conf.stderr)
                        completed = subprocess.run(
                            ["/bin/sh", str(script), *arguments],
                            capture_output=True, text=True, env=env,
                        )
                        self.assertNotEqual(completed.returncode, 0)
                        self.assertFalse(marker.exists(),
                            f"{script.name} executed {source} core.fsmonitor")

    def test_git_filter_injected_after_preflight_is_inert(self) -> None:
        # A one-time config allowlist is insufficient. Mutate .git/config
        # AFTER that preflight, immediately before the release status check.
        with TemporaryDirectory(prefix="mark-git-config-race-") as tmp:
            root = Path(tmp)
            repo, _ = self._fake_git_repo(root)
            (repo / ".gitattributes").write_text(
                "README.md filter=malicious\n", encoding="utf-8",
            )
            for command in (("add", ".gitattributes"),
                            ("commit", "-qm", "trusted attributes")):
                p = subprocess.run(
                    ["git", "-C", str(repo), *command],
                    capture_output=True, text=True,
                )
                self.assertEqual(p.returncode, 0, p.stderr)
            expected = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
            ).strip()
            marker = root / "late-injected-filter-executed"
            hook = root / "late-filter.sh"
            hook.write_text(
                "#!/bin/sh\n"
                + "printf executed > '" + str(marker) + "'\n"
                + "exec /bin/cat\n",
                encoding="utf-8",
            )
            hook.chmod(0o700)

            for name, anchor in (
                ("mark-api-container-build.sh",
                 'head=$(safe_git -C "$repo" rev-parse --verify HEAD)'),
                ("mark-api-container-control.sh",
                 'actual=$(safe_git -C "$repo" rev-parse --verify HEAD) || fail \'HEAD unavailable\''),
            ):
                with self.subTest(script=name):
                    marker.unlink(missing_ok=True)
                    script = repo / "docs" / name
                    source = (DOCS / name).read_text(encoding="utf-8")
                    self.assertEqual(source.count(anchor), 1)
                    # Fixture injection runs after allowlist (if present).
                    injection = (
                        '\n/usr/bin/git -C "$repo" config filter.malicious.clean "'
                        + str(hook) + '"\n'
                    )
                    script.write_text(
                        source.replace(anchor, anchor + injection, 1),
                        encoding="utf-8",
                    )
                    config = subprocess.run(
                        ["git", "-C", str(repo), "config", "--unset-all",
                         "filter.malicious.clean"],
                        capture_output=True, text=True,
                    )
                    self.assertIn(config.returncode, (0, 5), config.stderr)
                    readme = repo / "README.md"
                    readme.write_text("attested-changes\n", encoding="utf-8")
                    stamp = readme.stat()
                    os.utime(readme, (stamp.st_atime, stamp.st_mtime + 90))
                    p = subprocess.run(
                        ["/bin/sh", str(script), *(
                            [expected] if name.endswith("-build.sh") else
                            ["verify", "sha256:" + "a" * 64, expected]
                        )],
                        capture_output=True, text=True,
                        env={**os.environ, "MARK_UV": "/usr/bin/false"},
                    )
                    self.assertNotEqual(p.returncode, 0)
                    self.assertFalse(
                        marker.exists(),
                        f"{name} executed a Git filter introduced after preflight",
                    )
                    # Prove this is a working executable filter fixture.
                    marker.unlink(missing_ok=True)
                    stamp = readme.stat()
                    os.utime(readme, (stamp.st_atime, stamp.st_mtime + 90))
                    check = subprocess.run(
                        ["git", "-C", str(repo), "status", "--porcelain"],
                        capture_output=True, text=True,
                    )
                    self.assertEqual(check.returncode, 0, check.stderr)
                    self.assertTrue(
                        marker.exists(),
                        f"race fixture was not executable: script_stderr={p.stderr!r}; "
                        f"status={check.stdout!r}",
                    )

    def test_untrusted_git_clean_filter_cannot_execute(self) -> None:
        with TemporaryDirectory(prefix="mark-git-clean-filter-") as tmp:
            root = Path(tmp)
            repo, _ = self._fake_git_repo(root)
            (repo / ".gitattributes").write_text(
                "README.md filter=malicious\n", encoding="utf-8",
            )

            def git(*args: str) -> str:
                result = subprocess.run(
                    ["git", "-C", str(repo), *args],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            git("add", ".gitattributes")
            git("commit", "-qm", "trusted attributes")
            expected = git("rev-parse", "HEAD")
            marker = root / "filter-executed"
            hook = root / "filter.sh"
            hook.write_text(
                "#!/bin/sh\n"
                + "printf executed > '" + str(marker) + "'\n"
                + "exec /bin/cat\n",
                encoding="utf-8",
            )
            hook.chmod(0o700)
            git("config", "filter.malicious.clean", str(hook))
            for script, arguments in (
                (repo / "docs/mark-api-container-build.sh", [expected]),
                (repo / "docs/mark-api-container-control.sh",
                 ["verify", "sha256:" + "a" * 64, expected]),
            ):
                with self.subTest(script=script.name):
                    marker.unlink(missing_ok=True)
                    readme = repo / "README.md"
                    stamp = readme.stat()
                    # Force Git to compare a tracked worktree file to its index.
                    os.utime(readme, (stamp.st_atime, stamp.st_mtime + 90))
                    # Prove the fixture is executable before testing isolation.
                    check = subprocess.run(
                        ["git", "-C", str(repo), "status", "--porcelain"],
                        capture_output=True, text=True,
                    )
                    self.assertEqual(check.returncode, 0, check.stderr)
                    self.assertTrue(marker.exists(), "clean-filter fixture never executed")
                    marker.unlink()
                    stamp = readme.stat()
                    os.utime(readme, (stamp.st_atime, stamp.st_mtime + 90))
                    completed = subprocess.run(
                        ["/bin/sh", str(script), *arguments],
                        capture_output=True, text=True,
                        env={**os.environ, "MARK_UV": "/usr/bin/false"},
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertFalse(marker.exists(),
                        f"{script.name} executed checkout-local filter.clean")

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