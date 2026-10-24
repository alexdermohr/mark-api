"""Read-only container liveness: connecting sockets with kernel UID proof.

An existing Unix socket pathname is not liveness: it can survive a crash.
This script runs under the service's own UID and imports only OS stdlib.
It never opens SQLite, issues Write actions, repairs sockets or changes state.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import stat
import struct
import sys


def check_socket(path: Path, *, owner_uid: int, socket_mode: int) -> bool:
    """Require a listening Unix socket whose kernel peer is the right UID."""
    if not hasattr(socket, "SO_PEERCRED"):
        return False
    try:
        before = path.lstat()
        if (
            not stat.S_ISSOCK(before.st_mode)
            or before.st_uid != owner_uid
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != socket_mode
        ):
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.25)
            connection.connect(str(path))
            peer = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if len(peer) != 12:
                return False
            _pid, peer_uid, _gid = struct.unpack("3i", peer)
            if peer_uid != owner_uid:
                return False
        after = path.lstat()
        return (
            after.st_dev == before.st_dev
            and after.st_ino == before.st_ino
            and after.st_uid == before.st_uid
            and after.st_mode == before.st_mode
            and after.st_nlink == 1
        )
    except (OSError, ValueError, struct.error):
        return False


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["storage"] and os.geteuid() == 50046:
        # Read-only SO_PEERCRED liveness. Never open or initialize SQLite,
        # issue a recovery clearance or attempt any Write.
        return 0 if check_socket(
            Path("/run/mark-api-storage/storage.sock"),
            owner_uid=50046, socket_mode=0o660,
        ) else 1
    if args == ["owner"] and os.geteuid() == 50044:
        return 0 if (
            # Do not connect to the witness runtime socket as UID 50044:
            # that authenticates as the real executor and creates/seals
            # ledger sessions. Witness has its own authenticated healthcheck
            # and Compose requires service_healthy before starting owner.
            check_socket(
                Path("/run/mark-api-executor/dashboard.sock"),
                owner_uid=50044, socket_mode=0o660,
            )
        ) else 1
    if args == ["frontend"] and os.geteuid() == 50042:
        return 0 if check_socket(
            Path("/run/mark-api-ingress/frontend.sock"),
            owner_uid=50042, socket_mode=0o660,
        ) else 1
    if args == ["ingress"] and os.geteuid() == 50045:
        # Never ask the browser executor or witness for a liveness signal.
        # Check both the fixed untrusted socket peer and local TCP listener.
        if not check_socket(
            Path("/run/mark-api-ingress/frontend.sock"),
            owner_uid=50042, socket_mode=0o660,
        ):
            return 1
        try:
            with socket.create_connection(("127.0.0.1", 8875), timeout=0.25):
                return 0
        except OSError:
            return 1
    if args == ["witness"] and os.geteuid() == 50043:
        return 0 if (
            check_socket(
                Path("/run/mark-api-witness/runtime.sock"),
                owner_uid=50043, socket_mode=0o660,
            )
            and check_socket(
                Path("/run/mark-fence-operator/operator.sock"),
                owner_uid=50043, socket_mode=0o600,
            )
        ) else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
