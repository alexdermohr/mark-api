"""Negative-only A6 Linux pre-cutover checks for old SQLite inode references.

The host operator must pin source inodes *before* a potentially compromised
service can rename or replace them. A failed scan prevents cutover. A scan
with no findings does NOT authorize it: the processes are not frozen, a new
descriptor/mapping may appear, inaccessible namespaces may exist, and this
module cannot revoke references. Never use these observations as A6 proof.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import sys

__all__ = [
    "PinnedInode", "KernelReference", "HostReferenceScan",
    "pin_source_inodes", "inspect_live_references",
]


@dataclass(frozen=True, slots=True)
class PinnedInode:
    """Original kernel inode identity, not a pathname after rename/restore."""

    name: str
    device: int
    inode: int

    @classmethod
    def from_fd(cls, name: str, descriptor: int) -> "PinnedInode":
        if type(name) is not str or name not in {"main", "wal", "shm"}:
            raise ValueError("invalid protected file kind")
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_ino <= 0:
            raise ValueError("protected file is not regular")
        return cls(name=name, device=info.st_dev, inode=info.st_ino)


@dataclass(frozen=True, slots=True)
class KernelReference:
    kind: str  # writable_fd | writable_shared_mapping | unknown_process
    pid: int
    slot: str  # descriptor or maps-line number; never a sensitive path
    name: str  # main | wal | shm | unknown


@dataclass(frozen=True, slots=True)
class HostReferenceScan:
    references: tuple[KernelReference, ...]
    examined_pids: int

    @property
    def found_blocker(self) -> bool:
        return bool(self.references)

    @property
    def cutover_attested(self) -> bool:
        """Never infer host quiescence from a finite /proc observation."""
        return False


def pin_source_inodes(database: Path, stack: ExitStack) -> tuple[PinnedInode, ...]:
    """Pin original files while trusted. Missing WAL/SHM is UNKNOWN, not safe.

    This function is deliberately strict: a historical store with already
    unlinked WAL/SHM requires separate witness/generation analysis, which
    is not provided by a simple file scan. Never create new SQLite files.
    """
    if not isinstance(database, Path) or not database.is_absolute():
        raise ValueError("absolute source database required")
    if sys.platform != "linux" or not hasattr(os, "O_PATH"):
        raise RuntimeError("Linux inode-only descriptors required")
    pins: list[PinnedInode] = []
    for name, suffix in (("main", ""), ("wal", "-wal"), ("shm", "-shm")):
        path = Path(str(database) + suffix)
        # Pin the inode without opening its contents: O_RDONLY can block on
        # an attacker-supplied FIFO or activate a device before fstat rejects
        # it. O_PATH also pins a final symlink itself with O_NOFOLLOW; the
        # regular-file check below rejects that link rather than following it.
        descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
        stack.callback(os.close, descriptor)
        pins.append(PinnedInode.from_fd(name, descriptor))
    if len({(pin.device, pin.inode) for pin in pins}) != len(pins):
        raise ValueError("protected SQLite files share one inode")
    return tuple(pins)


def _mapping_inode(line: str) -> tuple[int, int, bool] | None:
    """Parse Linux /proc/PID/maps, including deleted and bind-mount aliases."""
    columns = line.split(maxsplit=5)
    if len(columns) < 5:
        raise ValueError("truncated kernel maps row")
    _address, permissions, _offset, major_minor, inode_text = columns[:5]
    if (
        len(permissions) != 4 or permissions[1] not in "-w"
        or permissions[3] not in "sp" or not re.fullmatch(r"[0-9]+", inode_text)
        or not re.fullmatch(r"[0-9a-fA-F]+:[0-9a-fA-F]+", major_minor)
    ):
        raise ValueError("invalid kernel maps row")
    inode = int(inode_text)
    if inode == 0:
        return None
    major, minor = major_minor.split(":")
    return os.makedev(int(major, 16), int(minor, 16)), inode, (
        permissions[1] == "w" and permissions[3] == "s"
    )


def _fd_writable(fdinfo: str) -> bool:
    """Use kernel FD flags; a guessed /proc/PID/fd symlink is not authority."""
    flags = [line.split(":", 1)[1].strip() for line in fdinfo.splitlines()
             if line.startswith("flags:")]
    if len(flags) != 1 or not re.fullmatch(r"0[0-7]+", flags[0]):
        raise ValueError("incomplete kernel FD flags")
    return int(flags[0], 8) & os.O_ACCMODE in (os.O_WRONLY, os.O_RDWR)


def inspect_live_references(
    pins: tuple[PinnedInode, ...], *,
    proc_root: Path = Path("/proc"),
) -> HostReferenceScan:
    """Observe old writable FDs/MAP_SHARED, blocking on any unreadable PID.

    Kernel observations are negative evidence only. The function never
    grants release authority, even with no references or an all-root scan.
    The user-facing tool must not accept an alternate /proc tree.
    """
    if (
        not isinstance(pins, tuple) or len(pins) != 3
        or any(type(pin) is not PinnedInode for pin in pins)
        or any(
            type(pin.name) is not str or type(pin.device) is not int
            or type(pin.inode) is not int or pin.device <= 0 or pin.inode <= 0
            for pin in pins
        )
        or {pin.name for pin in pins} != {"main", "wal", "shm"}
        or len({(pin.device, pin.inode) for pin in pins}) != len(pins)
        or not isinstance(proc_root, Path)
    ):
        raise ValueError("invalid pinned SQLite generation")
    identity = {(pin.device, pin.inode): pin.name for pin in pins}
    seen: list[KernelReference] = []
    count = 0
    try:
        processes = sorted(
            (entry for entry in proc_root.iterdir() if entry.name.isdecimal()),
            key=lambda path: int(path.name),
        )
    except OSError as exc:
        raise RuntimeError("kernel process inventory unavailable") from exc
    for entry in processes:
        pid = int(entry.name)
        count += 1
        try:
            # Read mappings first; a mapped writable shared page survives
            # closure of its original file descriptor and path unlink/rename.
            with (entry / "maps").open("r", encoding="ascii") as stream:
                for line_number, line in enumerate(stream, 1):
                    try:
                        found = _mapping_inode(line)
                    except ValueError:
                        seen.append(KernelReference("unknown_process", pid, "maps", "unknown"))
                        break
                    if found is not None:
                        device, inode, writable_shared = found
                        if writable_shared and (device, inode) in identity:
                            seen.append(KernelReference(
                                "writable_shared_mapping", pid, str(line_number),
                                identity[(device, inode)],
                            ))
            for descriptor_path in (entry / "fd").iterdir():
                if not descriptor_path.name.isdecimal():
                    continue
                name = None
                try:
                    info = descriptor_path.stat()
                    name = identity.get((info.st_dev, info.st_ino))
                    if name is None:
                        continue
                    fdinfo_path = entry / "fdinfo" / descriptor_path.name
                    flags = fdinfo_path.read_text(encoding="ascii")
                    if _fd_writable(flags):
                        seen.append(KernelReference(
                            "writable_fd", pid, descriptor_path.name, name,
                        ))
                except FileNotFoundError:
                    # A descriptor that vanished before stat has no observed
                    # binding. Once a protected inode was seen, missing flags
                    # are UNKNOWN, not evidence that it was read-only/closed.
                    if name is not None:
                        seen.append(KernelReference(
                            "unknown_process", pid, "fdinfo", "unknown",
                        ))
                except (OSError, ValueError, UnicodeError):
                    seen.append(KernelReference(
                        "unknown_process", pid, "fdinfo", "unknown",
                    ))
        except FileNotFoundError:
            # If /proc/<pid> still exists but maps/fd is missing, this was
            # NOT a proven exited process. It is an unreadable/hostile view.
            # A disappeared PID remains advisory only, never a cutover proof.
            if entry.exists():
                seen.append(KernelReference(
                    "unknown_process", pid, "proc", "unknown",
                ))
        except (OSError, UnicodeError, ValueError):
            # Do not silently ignore other users' inaccessible /proc entries.
            seen.append(KernelReference("unknown_process", pid, "proc", "unknown"))
    return HostReferenceScan(tuple(seen), count)


def main(argv: list[str] | None = None) -> int:
    """Print a read-only negative result. Never promote this to release proof.

    A successful subprocess exit could be misconstrued as host A6 safety:
    keep exit 2 even when no live writable reference is observed. A complete
    snapshot requires separate root-verified process freeze and UID cutover.
    """
    parser = argparse.ArgumentParser(
        description="Negative-only Linux SQLite old-FD/MAP_SHARED diagnostic",
    )
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args(argv)
    result: dict[str, object] = {
        "schema": "kleinanzeiger-a6-host-preflight-negative-v1",
        "cutover_attested": False,
        "release_authorized": False,
        "scope": "unfrozen-local-proc-snapshot-only",
    }
    try:
        if sys.platform != "linux":
            raise RuntimeError("Linux kernel required")
        with ExitStack() as stack:
            pins = pin_source_inodes(args.db, stack)
            report = inspect_live_references(pins)
        counters: dict[str, int] = {}
        for finding in report.references:
            counters[finding.kind] = counters.get(finding.kind, 0) + 1
        result.update({
            "status": "blocked" if report.found_blocker else "unattested",
            "processes_examined": report.examined_pids,
            "findings_by_kind": counters,
        })
    except (OSError, RuntimeError, ValueError):
        result.update({"status": "unknown", "reason": "source_or_proc_unattested"})
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
