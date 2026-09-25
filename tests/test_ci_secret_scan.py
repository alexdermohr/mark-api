from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "tools" / "ci_secret_scan.py"
SPEC = importlib.util.spec_from_file_location("mark_ci_secret_scan", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
ci_secret_scan = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ci_secret_scan
SPEC.loader.exec_module(ci_secret_scan)


class CiSecretScanTests(unittest.TestCase):
    def make_repo(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        self.git(repo, "init", "-q", "-b", "main")
        self.git(repo, "config", "user.name", "CI Test")
        self.git(repo, "config", "user.email", "ci@example.invalid")
        return repo

    def git(self, repo: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout.strip()

    def commit_file(
        self,
        repo: Path,
        path: str,
        content: str,
        message: str,
    ) -> str:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.git(repo, "add", "--", path)
        self.git(repo, "commit", "-q", "-m", message)
        return self.git(repo, "rev-parse", "HEAD")

    def test_intermediate_added_secret_is_detected_after_later_removal(self) -> None:
        repo = self.make_repo()
        self.commit_file(repo, "sample.txt", "safe\n", "base")
        base = self.git(repo, "rev-parse", "HEAD")

        assignment = "token" + " = " + '"temporary-value"\n'
        leak_commit = self.commit_file(
            repo,
            "sample.txt",
            "safe\n" + assignment,
            "add temporary credential",
        )
        head = self.commit_file(
            repo,
            "sample.txt",
            "safe\n",
            "remove temporary credential",
        )

        findings, commit_count = ci_secret_scan.scan_range(repo, base, head)

        self.assertEqual(commit_count, 2)
        self.assertTrue(
            any(
                item.commit == leak_commit
                and item.rule_name == "token_assignment"
                for item in findings
            )
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = ci_secret_scan.main(
                [base, head, "--repo", str(repo)]
            )

        self.assertEqual(status, 1)
        self.assertIn(leak_commit[:12], output.getvalue())
        self.assertIn("token_assignment", output.getvalue())
        self.assertNotIn("temporary-value", output.getvalue())

    def test_variable_declarations_and_placeholders_are_not_credentials(self) -> None:
        repo = self.make_repo()
        self.commit_file(repo, "sample.txt", "safe\n", "base")
        base = self.git(repo, "rev-parse", "HEAD")
        declarations = "\n".join(
            (
                'token = os.environ["TOKEN"]',
                "password = None",
                "secret = config.secret",
                'cookie = request.headers["Cookie"]',
                'authorization = "${AUTHORIZATION}"',
                'token = "placeholder-token"',
            )
        )
        head = self.commit_file(
            repo,
            "sample.txt",
            declarations + "\n",
            "add credential plumbing",
        )

        findings, commit_count = ci_secret_scan.scan_range(repo, base, head)

        self.assertEqual(commit_count, 1)
        self.assertEqual(findings, ())

    def test_specific_token_pattern_still_blocks(self) -> None:
        repo = self.make_repo()
        self.commit_file(repo, "sample.txt", "safe\n", "base")
        base = self.git(repo, "rev-parse", "HEAD")
        token = "ghp_" + ("A" * 24)
        head = self.commit_file(
            repo,
            "sample.txt",
            f"value = {token}\n",
            "add token",
        )

        findings, _ = ci_secret_scan.scan_range(repo, base, head)

        self.assertTrue(
            any(item.rule_name == "github_token" for item in findings)
        )

    def test_preexisting_fixture_does_not_block_unrelated_file_change(self) -> None:
        repo = self.make_repo()
        fixture = "token" + " = " + '"fixture-value"\n'
        self.commit_file(
            repo,
            "sample.txt",
            fixture + "before\n",
            "base fixture",
        )
        base = self.git(repo, "rev-parse", "HEAD")

        head = self.commit_file(
            repo,
            "sample.txt",
            fixture + "after\n",
            "unrelated change",
        )

        findings, commit_count = ci_secret_scan.scan_range(repo, base, head)

        self.assertEqual(commit_count, 1)
        self.assertEqual(findings, ())

    def test_merge_commit_uses_parent_on_base_ancestry(self) -> None:
        repo = self.make_repo()
        self.commit_file(repo, "base.txt", "base\n", "base")
        self.git(repo, "checkout", "-q", "-b", "feature")
        self.commit_file(repo, "feature.txt", "feature\n", "feature work")

        self.git(repo, "checkout", "-q", "main")
        base_only_secret = "token" + " = " + '"base-only-secret-value"\n'
        base = self.commit_file(
            repo,
            "base-only.txt",
            base_only_secret,
            "advance base",
        )

        self.git(repo, "checkout", "-q", "feature")
        self.git(repo, "merge", "-q", "--no-ff", "main", "-m", "merge main")
        head = self.git(repo, "rev-parse", "HEAD")

        findings, commit_count = ci_secret_scan.scan_range(repo, base, head)

        self.assertEqual(commit_count, 2)
        self.assertEqual(findings, ())

    def test_new_branch_push_resolves_default_branch_merge_base(self) -> None:
        repo = self.make_repo()
        base = self.commit_file(repo, "base.txt", "base\n", "base")
        self.git(repo, "update-ref", "refs/remotes/origin/main", base)
        self.git(repo, "checkout", "-q", "-b", "feature")
        self.commit_file(repo, "one.txt", "one\n", "one")
        head = self.commit_file(repo, "two.txt", "two\n", "two")

        resolved = ci_secret_scan.resolve_event_base(
            repo,
            event_name="push",
            pr_base_sha="",
            push_before_sha="0" * 40,
            default_branch="main",
            head=head,
        )

        self.assertEqual(resolved, base)

    def test_existing_push_uses_before_sha(self) -> None:
        repo = self.make_repo()
        before = self.commit_file(repo, "base.txt", "base\n", "base")
        head = self.commit_file(repo, "next.txt", "next\n", "next")

        resolved = ci_secret_scan.resolve_event_base(
            repo,
            event_name="push",
            pr_base_sha="",
            push_before_sha=before,
            default_branch="main",
            head=head,
        )

        self.assertEqual(resolved, before)

    def test_pull_request_uses_declared_base_sha(self) -> None:
        repo = self.make_repo()
        base = self.commit_file(repo, "base.txt", "base\n", "base")
        head = self.commit_file(repo, "next.txt", "next\n", "next")

        resolved = ci_secret_scan.resolve_event_base(
            repo,
            event_name="pull_request",
            pr_base_sha=base,
            push_before_sha="",
            default_branch="main",
            head=head,
        )

        self.assertEqual(resolved, base)

    def test_clean_added_lines_pass_without_value_output(self) -> None:
        repo = self.make_repo()
        self.commit_file(repo, "sample.txt", "before\n", "base")
        base = self.git(repo, "rev-parse", "HEAD")
        head = self.commit_file(
            repo,
            "sample.txt",
            "before\nafter\n",
            "safe change",
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = ci_secret_scan.main(
                [base, head, "--repo", str(repo)]
            )

        self.assertEqual(status, 0)
        self.assertIn("Secret scan passed for 1 commits.", output.getvalue())


if __name__ == "__main__":
    unittest.main()
