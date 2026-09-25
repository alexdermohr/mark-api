from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


PATTERNS = {
    "bearer_token": re.compile(rb"(?i)bearer\s+[A-Za-z0-9._~-]{16,}"),
    "openai_key": re.compile(rb"sk-[A-Za-z0-9_-]{16,}"),
    "github_token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    "aws_access_key": re.compile(rb"AKIA[0-9A-Z]{16}"),
}

_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    rb"""(?ix)
    ^\s*["']?
    (?P<name>password|passwd|secret|token|cookie|authorization)
    ["']?\s*[:=]\s*
    (?P<value>.+?)
    \s*$
    """
)
_IDENTIFIER_RE = re.compile(rb"^[A-Za-z_][A-Za-z0-9_.]*$")
_SIMPLE_LITERAL_RE = re.compile(rb"^[A-Za-z0-9._~+/@:!#%&*?=-]+$")
_PLACEHOLDER_VALUES = {
    b"none",
    b"null",
    b"true",
    b"false",
    b"password",
    b"passwd",
    b"secret",
    b"token",
    b"cookie",
    b"authorization",
    b"changeme",
    b"change-me",
    b"replace-me",
}
_PLACEHOLDER_PREFIXES = (
    b"example",
    b"dummy",
    b"fake",
    b"fixture",
    b"placeholder",
    b"your-",
    b"your_",
)

_HUNK_RE = re.compile(
    rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@"
)
_ZERO_SHA_RE = re.compile(r"^0{40}$")


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


def _git_ok(repo: Path, *arguments: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), *arguments],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def _commits(repo: Path, base: str, head: str) -> tuple[str, ...]:
    raw = _git(repo, "rev-list", "--reverse", f"{base}..{head}")
    return tuple(item.decode("ascii") for item in raw.splitlines() if item)


def _parents(repo: Path, commit: str) -> tuple[str, ...]:
    row = _git(repo, "rev-list", "--parents", "-n", "1", commit).split()
    return tuple(item.decode("ascii") for item in row[1:])


def _parent_for_patch(repo: Path, commit: str, base: str) -> str | None:
    parents = _parents(repo, commit)
    if not parents:
        return None
    if len(parents) == 1:
        return parents[0]

    base_side = [
        parent
        for parent in parents
        if _git_ok(repo, "merge-base", "--is-ancestor", parent, base)
    ]
    if not base_side:
        return parents[0]

    return min(
        base_side,
        key=lambda parent: int(
            _git(repo, "rev-list", "--count", f"{parent}..{base}")
        ),
    )


def _patch(repo: Path, commit: str, base: str) -> bytes:
    parent = _parent_for_patch(repo, commit, base)
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


def _credential_assignment_rule(line: bytes) -> str | None:
    match = _CREDENTIAL_ASSIGNMENT_RE.match(line)
    if match is None:
        return None

    name = match.group("name").decode("ascii").lower()
    raw_value = match.group("value").strip().rstrip(b",;")
    quoted = (
        len(raw_value) >= 2
        and raw_value[:1] == raw_value[-1:]
        and raw_value[:1] in {b'"', b"'"}
    )
    if quoted:
        value = raw_value[1:-1].strip()
        minimum_length = 8
    else:
        value = raw_value.split(None, 1)[0]
        minimum_length = 12
        if _IDENTIFIER_RE.fullmatch(value):
            return None

    lowered = value.lower()
    if len(value) < minimum_length:
        return None
    if lowered in _PLACEHOLDER_VALUES:
        return None
    if lowered.startswith(_PLACEHOLDER_PREFIXES):
        return None
    if (
        value.startswith((b"$", b"{{", b"${"))
        or b"(" in value
        or b"[" in value
        or b"{" in value
    ):
        return None
    if not _SIMPLE_LITERAL_RE.fullmatch(value):
        return None
    return f"{name}_assignment"


def _line_rules(line: bytes) -> tuple[str, ...]:
    direct = [
        rule_name
        for rule_name, pattern in PATTERNS.items()
        if pattern.search(line)
    ]
    if direct:
        return tuple(dict.fromkeys(direct))

    assignment_rule = _credential_assignment_rule(line)
    if assignment_rule is None:
        return ()
    return (assignment_rule,)


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
            for rule_name in _line_rules(payload):
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
        findings.extend(_scan_patch(commit, _patch(repo, commit, base)))
    return tuple(findings), len(commits)


def resolve_event_base(
    repo: Path,
    *,
    event_name: str,
    pr_base_sha: str,
    push_before_sha: str,
    default_branch: str,
    head: str,
) -> str:
    if event_name == "pull_request":
        if not pr_base_sha:
            raise ValueError("pull_request event requires pr_base_sha")
        _git(repo, "cat-file", "-e", f"{pr_base_sha}^{{commit}}")
        return pr_base_sha

    if push_before_sha and not _ZERO_SHA_RE.fullmatch(push_before_sha):
        _git(repo, "cat-file", "-e", f"{push_before_sha}^{{commit}}")
        return push_before_sha

    if not default_branch:
        raise ValueError("new branch push requires default_branch")
    default_ref = f"refs/remotes/origin/{default_branch}"
    _git(repo, "show-ref", "--verify", default_ref)
    base = _git(repo, "merge-base", head, default_ref).decode("ascii").strip()
    if not base:
        raise ValueError("could not resolve merge base for new branch push")
    return base


def _github_escape(value: str) -> str:
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def _scan_main(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    if not args.base or not args.head:
        raise ValueError("scan mode requires base and head")
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


def _resolve_base_main(args: argparse.Namespace) -> int:
    if args.base or args.head:
        raise ValueError("resolve-base mode does not accept scan range arguments")
    if not args.event_name or not args.event_head:
        raise ValueError("resolve-base mode requires event name and event head")
    base = resolve_event_base(
        args.repo.resolve(),
        event_name=args.event_name,
        pr_base_sha=args.pr_base_sha,
        push_before_sha=args.push_before_sha,
        default_branch=args.default_branch,
        head=args.event_head,
    )
    print(base)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CI range resolution and targeted secret scanning."
    )
    parser.add_argument("base", nargs="?", help="Exclusive base commit")
    parser.add_argument("head", nargs="?", help="Inclusive head commit")
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="Git repository path (default: current directory)",
    )
    parser.add_argument(
        "--resolve-base",
        action="store_true",
        help="Resolve and print the exclusive base commit for a GitHub event.",
    )
    parser.add_argument("--event-name", default="")
    parser.add_argument("--pr-base-sha", default="")
    parser.add_argument("--push-before-sha", default="")
    parser.add_argument("--default-branch", default="")
    parser.add_argument("--event-head", default="")
    args = parser.parse_args(argv)

    if args.resolve_base:
        return _resolve_base_main(args)
    return _scan_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
