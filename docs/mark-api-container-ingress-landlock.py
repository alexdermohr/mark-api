"""Root-owned fail-closed network policy for the dedicated UID-50045 ingress.

Linux Landlock ABI 4+ blocks *all* TCP connects (including Chrome/owner
localhost, IPv4/IPv6 and future rebind) and permits only TCP bind port 8875.
AF_UNIX communication remains available for the UID-50042 frontend socket.

This is a process-tree restriction, NOT a global UID or root-Docker firewall.
It must run before importing the HTTP relay or opening any IP descriptors.
A compromised ingress after startup cannot disable or relax its Landlock rules.
A different preexisting process with the same host UID remains an external gate.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import platform
import sys


_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_NET_PORT = 2
_LANDLOCK_ACCESS_NET_BIND_TCP = 1 << 0
_LANDLOCK_ACCESS_NET_CONNECT_TCP = 1 << 1
_PR_SET_NO_NEW_PRIVS = 38
_REQUIRED_ABI = 4
_INGRESS_UID = 50045
_PUBLIC_PORT = 8875
_DRAFT_MARKER = Path("/etc/mark-api/UNATTESTED_DRAFT")
_RUNTIME_PYTHON = "/opt/mark-api/venv/bin/python"


class IngressLandlockError(RuntimeError):
    """Missing or unverified ingress outbound TCP isolation; deny startup."""


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
    ]


class _NetPortAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("port", ctypes.c_uint64),
    ]


def install_tcp_ingress_policy(*, bind_port: int = _PUBLIC_PORT) -> None:
    """Install irreversible deny-all-TCP-connect, bind-only-one-port policy.

    The configurable port is an in-process *test seam*. The deployment's main
    always calls this with literal 8875; no port or policy CLI is exposed.
    """
    if (
        sys.platform != "linux"
        or platform.machine() not in ("x86_64", "aarch64")
        or os.geteuid() != _INGRESS_UID
        or type(bind_port) is not int
        or not 1 <= bind_port <= 65535
    ):
        raise IngressLandlockError("unexpected ingress identity or platform")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    def call(number: int, *args: object) -> int:
        result = libc.syscall(number, *args)
        if result < 0:
            raise IngressLandlockError(
                "kernel rejected mandatory Landlock policy"
            )
        return int(result)

    # ABI v4 was the first to expose handled_access_net / TCP rules.
    abi = call(
        _LANDLOCK_CREATE_RULESET,
        ctypes.c_void_p(0), ctypes.c_size_t(0),
        ctypes.c_uint(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    if abi < _REQUIRED_ABI:
        raise IngressLandlockError("Landlock TCP policy unavailable")
    attributes = _RulesetAttr(
        handled_access_fs=0,
        handled_access_net=(
            _LANDLOCK_ACCESS_NET_BIND_TCP | _LANDLOCK_ACCESS_NET_CONNECT_TCP
        ),
    )
    ruleset_fd = call(
        _LANDLOCK_CREATE_RULESET,
        ctypes.byref(attributes), ctypes.sizeof(attributes), ctypes.c_uint(0),
    )
    try:
        rule = _NetPortAttr(
            allowed_access=_LANDLOCK_ACCESS_NET_BIND_TCP,
            port=bind_port,
        )
        call(
            _LANDLOCK_ADD_RULE,
            ctypes.c_int(ruleset_fd),
            ctypes.c_int(_LANDLOCK_RULE_NET_PORT),
            ctypes.byref(rule), ctypes.c_uint(0),
        )
        if libc.prctl(
            _PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0,
        ) != 0:
            raise IngressLandlockError("no-new-privileges not enforced")
        call(
            _LANDLOCK_RESTRICT_SELF,
            ctypes.c_int(ruleset_fd), ctypes.c_uint(0),
        )
    finally:
        os.close(ruleset_fd)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    # Root-owned container marker is never removed by a service UID.
    if arguments or _DRAFT_MARKER.exists():
        print("mark-api: ingress isolation unavailable in unattested draft",
              file=sys.stderr)
        return 2
    try:
        install_tcp_ingress_policy(bind_port=8875)
    except (IngressLandlockError, OSError):
        print("mark-api: independent ingress TCP isolation not attested",
              file=sys.stderr)
        return 2
    # No app/Python module is imported before Landlock restrictions apply.
    os.execv(
        _RUNTIME_PYTHON,
        [
            _RUNTIME_PYTHON, "-I", "-m", "mark_api.dashboard_relay",
            "public-ingress", "--frontend-socket",
            "/run/mark-api-ingress/frontend.sock",
            "--frontend-uid", "50042", "--port", "8875",
        ],
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
