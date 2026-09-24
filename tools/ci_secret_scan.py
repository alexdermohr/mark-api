from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


PATTERNS = {
    "password_assignment": re.compile(rb"(?i)password\s*[:=]"),
    "passwd_assignment": re.compile(rb"(?i)passwd\s*[:=]"),
    "secret_assignment": re.compile(rb"(?i)secret\s*[:=]"),
    "token_assignment": re.compile(rb"(?i)token\s*[:=]"),
    "cookie_assignment": re.compile(rb"(?i)cookie\s*[:=]"),
    "authorization_assignment": re.compile(rb"(?i)authorization\s*[:=]"),
    "bearer_token": re.compile(rb"(?i)bearer\s+[A-Za-z0-9._~-]{16,}"),
    "openai_key": re.compile(rb"sk-[A-Za-z0-9_-]{16,}"),
    "github_token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    "aws_access_key": re.compile(rb"AKIA[0-9A-Z]{16}"),
}

_HUNK_RE = re.compile(
    rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@"
)


@dataclass(frozen=True, slots=True)
class Finding:
    commit: str
    path: str
    line_number: int
    rule_name: str


def _git(repo: Path, *arguments: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(repo), *arguments],
        stderr=subprocess.DEVNULL,
    )


def _commits(repo: Path, base: str, head: str) -> tuple[str, ...]:
    raw = _git(repo, "rev-list", "--reverse", f"{base}..{head}")
    return tuple(item.decode("ascii") for item in raw.splitlines() if item)


def _first_parent(repo: Path, commit: str) -> str | None:
    row = _git(repo, "rev-list", "--parents", "-n", "1", commit).split()
    if len(row) < 2:
        return None
    return row[1].decode("ascii")


def _patch(repo: Path, commit: str) -> bytes:
    parent = _first_parent(repo, commit)
    common = (
        "--no-ext-diff",
        "--no-renames",
        "--unified=0",
        "--no-color",
    )
    if parent is None:
        return _git(repo, "show", "--format=", *common, commit, "--")
    return _git(repo, "diff", *common, parent, commit, "--")


def _decode_path(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _scan_patch(commit: str, patch: bytes) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    current_path = "<unknown>"
    line_number: int | None = None

    for line in patch.splitlines():
        if line.startswith(b"+++ "):
            if line == b"+++ /dev/null":
                current_path = "<deleted>"
            elif line.startswith(b"+++ b/"):
                current_path = _decode_path(line[6:])
            line_number = None
            continue

        match = _HUNK_RE.match(line)
        if match is not None:
            line_number = int(match.group(1))
            continue

        if line_number is None:
            continue

        if line.startswith(b"+") and not line.startswith(b"+++"):
            payload = line[1:]
            for rule_name, pattern in PATTERNS.items():
                if pattern.search(payload):
                    findings.append(
                        Finding(
                            commit=commit,
                            path=current_path,
                            line_number=line_number,
                            rule_name=rule_name,
                        )
                    )
            line_number += 1
        elif line.startswith(b" "):
            line_number += 1

    return tuple(findings)


def scan_range(
    repo: Path,
    base: str,
    head: str,
) -> tuple[tuple[Finding, ...], int]:
    commits = _commits(repo, base, head)
    findings: list[Finding] = []
    for commit in commits:
        findings.extend(_scan_patch(commit, _patch(repo, commit)))
    return tuple(findings), len(commits)


def _github_escape(value: str) -> str:
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Scan newly added lines in every commit in a Git range for "
            "high-signal secret patterns without printing matched values."
        )
    )
    parser.add_argument("base", help="Exclusive base commit")
    parser.add_argument("head", help="Inclusive head commit")
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="Git repository path (default: current directory)",
    )
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    findings, commit_count = scan_range(repo, args.base, args.head)

    for finding in findings:
        path = _github_escape(finding.path)
        rule = _github_escape(finding.rule_name)
        commit = _github_escape(finding.commit[:12])
        print(
            f"::error file={path},line={finding.line_number}::"
            f"possible secret pattern in commit {commit}: {rule}"
        )

    if findings:
        return 1

    print(f"Secret scan passed for {commit_count} commits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
