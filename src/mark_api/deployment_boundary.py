"""Fail-closed local identity preflight for the opt-in root-managed Mark service.

This validates OS file permissions and a dedicated non-login UID. It cannot
defend against root, malicious code already running under that trusted UID,
or concurrent hostile writable mappings held by such compromised code.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import pwd
import stat
import sys


class DeploymentBoundaryError(RuntimeError):
    """A root-managed Mark runtime precondition is not satisfied."""


def _require_private_path(
    path: Path, *, uid: int, gid: int, directory: bool, mode: int,
) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise DeploymentBoundaryError("required private runtime path is missing") from exc
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not kind_ok
        or info.st_uid != uid
        or info.st_gid != gid
        or stat.S_IMODE(info.st_mode) != mode
        or (not directory and info.st_nlink != 1)
    ):
        raise DeploymentBoundaryError("runtime path ownership, mode or inode is unsafe")


def check_deployment(db: Path, backup_dir: Path) -> None:
    """Check the real process UID and the exact private Mark SQLite store.

    No database connection, file creation, chmod/chown or platform request.
    This is a local trust-boundary preflight, not a SQLite content snapshot.
    """
    try:
        account = pwd.getpwnam("mark-api")
    except KeyError as exc:
        raise DeploymentBoundaryError("dedicated mark-api account is absent") from exc
    if (
        account.pw_uid == 0
        or account.pw_gid == 0
        or os.geteuid() != account.pw_uid
        or os.getegid() != account.pw_gid
        or not account.pw_shell.endswith("/nologin")
        or set(os.getgroups()) - {account.pw_gid}
    ):
        raise DeploymentBoundaryError("process is not the dedicated non-login service")
    if (
        not db.is_absolute() or not backup_dir.is_absolute()
        or ".." in db.parts or ".." in backup_dir.parts
        or db.parent == backup_dir
    ):
        raise DeploymentBoundaryError("runtime paths must be distinct absolute paths")
    _require_private_path(
        db.parent, uid=account.pw_uid, gid=account.pw_gid, directory=True, mode=0o700
    )
    _require_private_path(
        db, uid=account.pw_uid, gid=account.pw_gid, directory=False, mode=0o600
    )
    _require_private_path(
        backup_dir, uid=account.pw_uid, gid=account.pw_gid,
        directory=True, mode=0o700,
    )
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = db.with_name(db.name + suffix)
        # A nonexistent sidecar is normal for a quiet SQLite connection.
        if not sidecar.exists() and not sidecar.is_symlink():
            continue
        _require_private_path(
            sidecar, uid=account.pw_uid, gid=account.pw_gid,
            directory=False, mode=0o600,
        )


def _root_owned_regular(path: Path) -> None:
    """Require a root-owned regular code file without group/world writes."""
    try:
        info = path.lstat()
    except OSError as exc:
        raise DeploymentBoundaryError("trusted installation is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise DeploymentBoundaryError("trusted installation is user-writable")


def _require_root_owned_install() -> None:
    """Refuse protected-service execution from an untrusted checkout."""
    installed_root = Path("/opt/mark-api/venv")
    source = Path(__file__).resolve()
    executable = Path(sys.executable).resolve()
    if not source.is_relative_to(installed_root):
        raise DeploymentBoundaryError("Mark service requires a verified /opt installation")
    if not (
        executable.is_relative_to(Path("/usr"))
        or executable.is_relative_to(Path("/opt"))
    ):
        raise DeploymentBoundaryError("Mark service Python executable is not root-managed")

    scripts = [
        installed_root / "bin/mark-api-launch",
        installed_root / "bin/mark-api-backup",
    ]
    modules: list[Path] = []
    for name in ("mark_api.launcher", "mark_api.backup_cli", "mark_api.storage"):
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None:
            raise DeploymentBoundaryError("Mark installed entrypoint is missing")
        candidate = Path(spec.origin).resolve()
        if not candidate.is_relative_to(installed_root):
            raise DeploymentBoundaryError("Mark installed modules are untrusted")
        modules.append(candidate)

    # A writable console script or imported module can bypass the preflight,
    # even when this particular preflight module is root-owned.
    for node in (source, executable, *scripts, *modules):
        _root_owned_regular(node)
        for parent in node.parents:
            try:
                info = parent.lstat()
            except OSError as exc:
                raise DeploymentBoundaryError("trusted installation is unavailable") from exc
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise DeploymentBoundaryError("trusted installation directory is writable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the dedicated Mark service UID and SQLite file access"
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        _require_root_owned_install()
        check_deployment(args.db, args.backup_dir)
    except DeploymentBoundaryError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
