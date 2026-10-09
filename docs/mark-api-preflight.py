"""First-code, read-only bootstrap for the isolated Mark systemd services.

Install a reviewed copy at /etc/mark-api/preflight.py, owned by root with mode
0644, in a root-owned 0755 directory. Invoke only after the native OS-stdlib check in the root-managed
/etc/mark-api/bootstrap.sh, via the systemd ExecStartPre command. That
script then invokes:
    /usr/bin/python3 -I -S /etc/mark-api/preflight.py --db ... --backup-dir ...
No Mark or virtualenv module is imported before checking the entire installed
runtime tree. This preflight cannot exclude root compromise, or malicious code
already legitimately running under the trusted Mark service UID.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import pwd
import stat


class DeploymentBoundaryError(RuntimeError):
    """Fail closed without mutating any user files or network state."""


_INSTALL_ROOT = Path("/opt/mark-api/venv")
_BOOTSTRAP = Path("/etc/mark-api/preflight.py")
_SYSTEM_CODE_ROOT = Path("/usr")


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise DeploymentBoundaryError("required trusted path is unavailable") from exc


def _trusted_metadata(path: Path) -> os.stat_result:
    info = _lstat(path)
    if info.st_uid != 0 or (
        not stat.S_ISLNK(info.st_mode) and stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise DeploymentBoundaryError("installed code is not root-owned and non-writable")
    return info


def _trusted_parents(path: Path) -> None:
    for parent in path.parents:
        info = _trusted_metadata(parent)
        if not stat.S_ISDIR(info.st_mode):
            raise DeploymentBoundaryError("trusted installation has a symlinked parent")


def _trusted_link(path: Path, root: Path) -> None:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DeploymentBoundaryError("installed code symlink cannot be trusted") from exc
    inside_install = resolved.is_relative_to(root)
    if not inside_install and not resolved.is_relative_to(_SYSTEM_CODE_ROOT):
        raise DeploymentBoundaryError("installed code symlink escapes trusted roots")
    target_info = _trusted_metadata(resolved)
    if not (
        stat.S_ISREG(target_info.st_mode) or stat.S_ISDIR(target_info.st_mode)
    ):
        raise DeploymentBoundaryError("installed code symlink resolves to unsafe type")
    # A symlinked package directory under /usr would bypass the recursive
    # installation scan. Only interpreter symlinks may enter the OS trust base.
    if not inside_install and not (
        path.parent == root / "bin"
        and path.name.startswith("python")
        and resolved.parent == _SYSTEM_CODE_ROOT / "bin"
        and resolved.name.startswith("python3")
        and stat.S_ISREG(target_info.st_mode)
    ):
        raise DeploymentBoundaryError("installed package symlink escapes audited install")
    _trusted_parents(resolved)


def _safe_pth(path: Path, root: Path) -> None:
    # Site-package .pth files may extend sys.path or run code *before*
    # application preflight. Avoid executing code-bearing .pth declarations,
    # or adding data/code paths outside the trusted install and OS stdlib.
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DeploymentBoundaryError("package path declaration is unreadable") from exc
    for line in lines:
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        if item.startswith(("import ", "import\t")):
            raise DeploymentBoundaryError("executable package path declaration is unsafe")
        candidate = (path.parent / item).resolve(strict=False)
        if not candidate.is_relative_to(root):
            # Paths under /usr are NOT scanned by the venv walker. Even a
            # root-owned .pth can reference writable code below that prefix.
            raise DeploymentBoundaryError("package path declaration escapes audited install")



def _check_venv_config(root: Path) -> None:
    """Refuse Python site initialization that reaches unscanned code."""
    config = root / "pyvenv.cfg"
    if not stat.S_ISREG(_trusted_metadata(config).st_mode):
        raise DeploymentBoundaryError("venv configuration must be a regular file")
    try:
        lines = config.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DeploymentBoundaryError("venv configuration is unreadable") from exc
    values: dict[str, str] = {}
    for line in lines:
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        if "=" not in item:
            raise DeploymentBoundaryError("venv configuration is malformed")
        key, value = (part.strip() for part in item.split("=", 1))
        key = key.lower()
        if key in ("home", "include-system-site-packages"):
            if key in values:
                raise DeploymentBoundaryError("duplicate venv trust declaration")
            values[key] = value
    if values.get("include-system-site-packages", "").lower() != "false":
        raise DeploymentBoundaryError("system site-packages must be disabled")
    base_home = Path(values.get("home", ""))
    if not base_home.is_absolute():
        raise DeploymentBoundaryError("venv Python base must be absolute")
    try:
        resolved = base_home.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DeploymentBoundaryError("venv Python base is unavailable") from exc
    # Only the distro-managed /usr/bin Python layout is supported. Arbitrary
    # nested OS prefixes can load unscanned stdlib/sitecustomize modules.
    if base_home != _SYSTEM_CODE_ROOT / "bin" or resolved != base_home:
        raise DeploymentBoundaryError("venv Python base is not the audited OS installation")
    for path in (base_home, resolved):
        if not stat.S_ISDIR(_trusted_metadata(path).st_mode):
            raise DeploymentBoundaryError("venv Python base is not a trusted directory")
        _trusted_parents(path)


def _check_os_stdlib(version: str) -> None:
    """Audit the executable OS stdlib reachable before application imports.

    The only supported base is /usr/bin backed by /usr/lib/pythonX.Y.
    Do not trust the /usr prefix alone, including transitive importable code.
    """
    if (
        not version.startswith("python3.")
        or not version.removeprefix("python3.").isdigit()
    ):
        raise DeploymentBoundaryError("installed Python version is not supported")
    stdlib = _SYSTEM_CODE_ROOT / "lib" / version
    try:
        if stdlib.resolve(strict=True) != stdlib:
            raise DeploymentBoundaryError("system stdlib may not be symlinked")
    except (OSError, RuntimeError) as exc:
        raise DeploymentBoundaryError("required OS Python stdlib is unavailable") from exc
    _trusted_parents(stdlib)
    stack = [stdlib]
    while stack:
        path = stack.pop()
        info = _trusted_metadata(path)
        if stat.S_ISDIR(info.st_mode):
            try:
                stack.extend(path.iterdir())
            except OSError as exc:
                raise DeploymentBoundaryError("system stdlib cannot be audited") from exc
        elif stat.S_ISLNK(info.st_mode):
            try:
                target = path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise DeploymentBoundaryError("system stdlib symlink cannot be trusted") from exc
            target_info = _trusted_metadata(target)
            regular = stat.S_ISREG(target_info.st_mode)
            directory = stat.S_ISDIR(target_info.st_mode)
            if not (regular or directory):
                raise DeploymentBoundaryError("system stdlib symlink has unsafe target")
            # A distro may link sitecustomize.py to /etc/pythonX.Y. Accept
            # only trusted external *files*, never unscanned directories.
            if not target.is_relative_to(stdlib) and not regular:
                raise DeploymentBoundaryError("system stdlib symlink escapes audited tree")
            _trusted_parents(target)
        elif not stat.S_ISREG(info.st_mode):
            raise DeploymentBoundaryError("system stdlib has unsafe file type")
    # CPython can import a standard-library ZIP before the directory tree.
    # It need not exist, but if present it must be root-owned and immutable.
    archive = _SYSTEM_CODE_ROOT / "lib" / (
        version.replace(".", "") + ".zip"
    )
    if archive.exists() or archive.is_symlink():
        if not stat.S_ISREG(_trusted_metadata(archive).st_mode):
            raise DeploymentBoundaryError("system stdlib archive is not a trusted file")
        _trusted_parents(archive)



def _check_venv_interpreter(root: Path, version: str) -> None:
    """Bind the actual service interpreter to the audited OS stdlib version.

    A root-owned copied binary or an alias to another Python minor version
    can load unrelated import roots before mark_api is even imported.
    """
    expected = _SYSTEM_CODE_ROOT / "bin" / version
    if not stat.S_ISREG(_trusted_metadata(expected).st_mode):
        raise DeploymentBoundaryError("audited system Python must be a regular file")
    _trusted_parents(expected)
    try:
        entries = list((root / "bin").iterdir())
    except OSError as exc:
        raise DeploymentBoundaryError("venv interpreter directory is unavailable") from exc
    found_service_interpreter = False
    for entry in entries:
        name = entry.name
        if not (
            name in ("python", "python3", version)
            or name.startswith("python3.") and name[8:].isdigit()
        ):
            continue
        if not stat.S_ISLNK(_lstat(entry).st_mode):
            raise DeploymentBoundaryError("venv Python must link to audited OS interpreter")
        try:
            resolved = entry.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise DeploymentBoundaryError("venv Python interpreter link is unsafe") from exc
        if resolved != expected:
            raise DeploymentBoundaryError("venv Python version differs from audited OS stdlib")
        if name == "python":
            found_service_interpreter = True
    if not found_service_interpreter:
        raise DeploymentBoundaryError("isolated Mark Python interpreter is missing")


def check_installed_code(root: Path) -> None:
    """Audit *all* installed code, dependencies, entrypoints and .pth files.

    Symlinks are never followed during directory walking; targets and their
    entire parent chain are checked independently. The runtime Python and
    system stdlib under /usr are considered the root-owned OS trust base.
    """
    if not root.is_absolute():
        raise DeploymentBoundaryError("installation path must be absolute")
    try:
        if root.resolve(strict=True) != root:
            raise DeploymentBoundaryError("installation root may not be symlinked")
    except (OSError, RuntimeError) as exc:
        raise DeploymentBoundaryError("installation root is unavailable") from exc
    _trusted_parents(root)
    stack = [root]
    while stack:
        path = stack.pop()
        info = _trusted_metadata(path)
        if stat.S_ISDIR(info.st_mode):
            try:
                stack.extend(path.iterdir())
            except OSError as exc:
                raise DeploymentBoundaryError("installed code directory cannot be scanned") from exc
        elif stat.S_ISLNK(info.st_mode):
            _trusted_link(path, root)
        elif stat.S_ISREG(info.st_mode):
            if path.suffix == ".pth":
                _safe_pth(path, root)
        else:
            raise DeploymentBoundaryError("installed code has an unexpected file type")
    _check_venv_config(root)
    matches = list(root.glob("lib/python*/site-packages/mark_api/__init__.py"))
    if len(matches) != 1 or not matches[0].resolve(strict=True).is_relative_to(root):
        raise DeploymentBoundaryError("one non-editable Mark package must be installed")
    version = matches[0].parents[2].name
    _check_venv_interpreter(root, version)
    _check_os_stdlib(version)


def _require_private_path(
    path: Path, *, uid: int, gid: int, directory: bool, mode: int,
) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise DeploymentBoundaryError("required private runtime path is missing") from exc
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not correct_type
        or info.st_uid != uid
        or info.st_gid != gid
        or stat.S_IMODE(info.st_mode) != mode
        or (not directory and info.st_nlink != 1)
    ):
        raise DeploymentBoundaryError("runtime path ownership, mode or inode is unsafe")


def check_deployment(db: Path, backup_dir: Path) -> None:
    """Check service identity and private DB/sidecars without opening SQLite."""
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
        not db.is_absolute()
        or not backup_dir.is_absolute()
        or ".." in db.parts
        or ".." in backup_dir.parts
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
        if not sidecar.exists() and not sidecar.is_symlink():
            continue
        _require_private_path(
            sidecar, uid=account.pw_uid, gid=account.pw_gid,
            directory=False, mode=0o600,
        )


def _check_bootstrap_installation() -> None:
    if Path(__file__) != _BOOTSTRAP:
        raise DeploymentBoundaryError("bootstrap must be installed in /etc/mark-api")
    info = _trusted_metadata(_BOOTSTRAP)
    if not stat.S_ISREG(info.st_mode):
        raise DeploymentBoundaryError("bootstrap file is not a trusted regular file")
    _trusted_parents(_BOOTSTRAP)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only trusted Mark code tree and SQLite owner preflight"
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        _check_bootstrap_installation()
        check_installed_code(_INSTALL_ROOT)
        check_deployment(args.db, args.backup_dir)
    except DeploymentBoundaryError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())