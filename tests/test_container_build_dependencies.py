"""Real synthetic negative tests for the immutable offline wheel builder."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "docs" / "mark-api-container-build.sh"


class OfflineBuildDependencyTests(unittest.TestCase):
    def _fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path, str]:
        temp = tempfile.TemporaryDirectory(prefix="mark-build-dependency-")
        root = Path(temp.name) / "checkout"
        (root / "docs").mkdir(parents=True)
        (root / "src" / "mark_api").mkdir(parents=True)
        (root / "docs" / BUILDER.name).write_bytes(BUILDER.read_bytes())
        (root / "README.md").write_text("synthetic release fixture\n")
        (root / "pyproject.toml").write_bytes((ROOT / "pyproject.toml").read_bytes())
        (root / "src" / "mark_api" / "__init__.py").write_text("")
        subprocess.run(["/usr/bin/git", "init", "-q", str(root)], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(root), "add", "."], check=True)
        subprocess.run([
            "/usr/bin/git", "-C", str(root),
            "-c", "user.name=Release fixture",
            "-c", "user.email=release-fixture@example.invalid",
            "commit", "-qm", "fixture",
        ], check=True)
        sha = subprocess.check_output(
            ["/usr/bin/git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
        ).strip()
        return temp, root, sha

    def _run_builder(self, root: Path, sha: str, wheel: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("DOCKER_HOST", None)
        env.pop("DOCKER_CONTEXT", None)
        env["MARK_SETUPTOOLS_WHEEL"] = ""  # Explicit argv must own dependency admission.
        return subprocess.run(
            ["/bin/sh", str(root / "docs" / BUILDER.name), sha, wheel],
            cwd=root, env=env, text=True, capture_output=True, timeout=45,
            check=False,
        )

    def test_backend_is_exactly_pinned(self) -> None:
        import tomllib
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(project["build-system"]["requires"],
                         ["setuptools==80.9.0"])

    def test_missing_dependency_is_rejected_before_builder_runs(self) -> None:
        temp, root, sha = self._fixture()
        with temp:
            result = self._run_builder(root, sha, "")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("pinned setuptools wheel is required", result.stderr)

    def test_wrong_bytes_are_rejected_before_builder_runs(self) -> None:
        temp, root, sha = self._fixture()
        with temp:
            wheel = Path(temp.name) / "setuptools-80.9.0-py3-none-any.whl"
            wheel.write_bytes(b"not the reviewed setuptools artifact")
            result = self._run_builder(root, sha, str(wheel))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("pinned setuptools wheel checksum mismatch", result.stderr)


    def test_attested_commit_epoch_without_parent_objects(self) -> None:
        # The build's private Git object store intentionally contains only
        # the current attested commit and reachable tree/blob objects, not
        # its ancestor commits. A history-walking 'git show' must not be used.
        temp, root, _ = self._fixture()
        with temp:
            readme = root / "README.md"
            readme.write_text("new synthetic verified revision\\n")
            subprocess.run(["/usr/bin/git", "-C", str(root), "add", "README.md"],
                           check=True)
            subprocess.run([
                "/usr/bin/git", "-C", str(root),
                "-c", "user.name=Release fixture",
                "-c", "user.email=release-fixture@example.invalid",
                "commit", "-qm", "new revision",
            ], check=True)
            sha = subprocess.check_output(
                ["/usr/bin/git", "-C", str(root), "rev-parse", "HEAD"],
                text=True,
            ).strip()
            expected = subprocess.check_output(
                ["/usr/bin/git", "-C", str(root), "show", "-s", "--format=%ct", sha],
                text=True,
            ).strip()
            private = Path(temp.name) / "private.git"
            subprocess.run(["/usr/bin/git", "init", "--bare", "-q", str(private)],
                           check=True)
            commit = subprocess.check_output([
                "/usr/bin/git", "-C", str(root), "cat-file", "commit", sha,
            ])
            stored = subprocess.run([
                "/usr/bin/git", "--git-dir", str(private),
                "hash-object", "-w", "-t", "commit", "--stdin",
            ], input=commit, capture_output=True, check=True)
            self.assertEqual(stored.stdout.decode().strip(), sha)

            # Execute the actual production timestamp block, not a rewritten
            # reimplementation of its algorithm.
            source = BUILDER.read_text()
            start = source.index("verified_commit=$(private_git cat-file commit")
            end = source.index("export SOURCE_DATE_EPOCH", start)
            block = source[start:end] + "export SOURCE_DATE_EPOCH\\n"
            shell = "\n".join((
                "set -eu",
                'private_git() { /usr/bin/git --git-dir="$GIT_DIR" "$@"; }',
                'fail() { echo "$1" >&2; exit 1; }',
                'expected="$EXPECTED"',
                block,
                'printf "%s\\n" "$SOURCE_DATE_EPOCH"',
            )) + "\n"
            result = subprocess.run(
                ["/bin/sh", "-c", shell], capture_output=True, text=True,
                env={**os.environ, "GIT_DIR": str(private), "EXPECTED": sha},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expected)



    def test_untrusted_env_never_reaches_uv_build_backend(self) -> None:
        import hashlib
        temp, root, _ = self._fixture()
        with temp:
            # Dummy wheel bytes are attested only in a throwaway Git fixture.
            wheel = Path(temp.name) / "setuptools-80.9.0-py3-none-any.whl"
            wheel.write_bytes(b"synthetic wheel; never install or execute")
            script = root / "docs" / BUILDER.name
            before = script.read_text()
            actual = "062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922"
            self.assertEqual(before.count(actual), 1)
            script.write_text(before.replace(actual, hashlib.sha256(wheel.read_bytes()).hexdigest()))
            subprocess.run(["/usr/bin/git", "-C", str(root), "add", "docs"], check=True)
            subprocess.run([
                "/usr/bin/git", "-C", str(root),
                "-c", "user.name=Release fixture",
                "-c", "user.email=release-fixture@example.invalid",
                "commit", "-qm", "attest dummy input",
            ], check=True)
            sha = subprocess.check_output(
                ["/usr/bin/git", "-C", str(root), "rev-parse", "HEAD"],
                text=True,
            ).strip()
            marker = Path(temp.name) / "uv-command-environment"
            fake = Path(temp.name) / "fake-uv"
            fake.write_text(
                "#!/bin/sh\n"
                + "printf '%s\\n' "
                + '"${UV_VENV_SEED-unset}" "${UV_FIND_LINKS-unset}" '
                + '"${DIST_EXTRA_CONFIG-unset}" "${HOME-unset}" '
                + '"${PYTHONPATH-unset}" > '
                + "'" + str(marker) + "'\n"
                + "exit 77\n"
            )
            fake.chmod(0o700)
            poison_home = Path(temp.name) / "poison-home"
            poison_home.mkdir()
            (poison_home / ".pydistutils.cfg").write_text(
                "[install_lib]\nbuild_dir = /tmp/poison\n"
            )
            env = {
                **os.environ, "HOME": str(poison_home),
                "UV_VENV_SEED": "true",
                "UV_FIND_LINKS": str(poison_home),
                "DIST_EXTRA_CONFIG": str(poison_home / ".pydistutils.cfg"),
                "PYTHONPATH": str(poison_home), "MARK_UV": str(fake),
            }
            env.pop("DOCKER_HOST", None)
            env.pop("DOCKER_CONTEXT", None)
            run = subprocess.run(
                ["/bin/sh", str(script), sha, str(wheel)],
                cwd=root, env=env, capture_output=True, text=True,
                timeout=45,
            )
            self.assertNotEqual(run.returncode, 0)
            # The executable is attacker-controlled, not a legitimate uv.
            # Even an env -i wrapper must never invoke it.
            self.assertFalse(marker.exists(), run.stderr)
            self.assertIn("untrusted uv binary override", run.stderr)


    def test_caller_path_and_tar_checkpoint_never_execute(self) -> None:
        temp, root, sha = self._fixture()
        with temp:
            tools = Path(temp.name) / "untrusted-path"
            tools.mkdir()
            marker = Path(temp.name) / "executed-dirname"
            # A harmless, functional command shim proves preflight execution
            # without changing Git objects or any non-test files.
            executable = tools / "dirname"
            executable.write_text(
                "#!/bin/sh\n"
                + "/usr/bin/touch '" + str(marker) + "'\n"
                + "exec /usr/bin/dirname \"$@\"\n"
            )
            executable.chmod(0o700)
            env = {
                **os.environ,
                "PATH": str(tools) + ":/usr/bin:/bin",
                "TAR_OPTIONS": "--checkpoint=1 --checkpoint-action=echo=UNTRUSTED_TAR_MARKER",
                "MARK_SETUPTOOLS_WHEEL": "",
            }
            env.pop("DOCKER_HOST", None)
            env.pop("DOCKER_CONTEXT", None)
            result = subprocess.run(
                ["/bin/sh", str(root / "docs" / BUILDER.name), sha, ""],
                cwd=root, env=env, capture_output=True, text=True,
                timeout=45,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists(), "caller PATH executed before attestation")
            self.assertNotIn("UNTRUSTED_TAR_MARKER", result.stderr)
            self.assertIn("pinned setuptools wheel is required", result.stderr)


    def test_caller_tar_options_never_execute_even_with_safe_path(self) -> None:
        temp, root, sha = self._fixture()
        with temp:
            env = {
                **os.environ,
                "PATH": "/usr/bin:/bin",
                "TAR_OPTIONS": "--checkpoint=1 --checkpoint-action=echo=UNTRUSTED_TAR_MARKER",
                "MARK_SETUPTOOLS_WHEEL": "",
            }
            env.pop("DOCKER_HOST", None)
            env.pop("DOCKER_CONTEXT", None)
            result = subprocess.run(
                ["/bin/sh", str(root / "docs" / BUILDER.name), sha, ""],
                cwd=root, env=env, capture_output=True, text=True,
                timeout=45,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("UNTRUSTED_TAR_MARKER", result.stderr)
            # Do not mistake a failed Git object check for tar hardening.
            self.assertIn("pinned setuptools wheel is required", result.stderr)

if __name__ == "__main__":
    unittest.main()
