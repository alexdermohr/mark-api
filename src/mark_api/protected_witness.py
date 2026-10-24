"""Independent A6 completion Witness for an already secured multi-UID host.

A trusted host supervisor must preprovision the SQLite ledger and isolate
owner, actor, witness and untrusted runtime UIDs. No automatic initialization,
host cutover, restore authorization or platform action is granted here.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import stat
import sys
from threading import Event

from .completion_authority import CompletionAuthorityError, _roles
from .completion_witness import CompletionWitnessLedger, CompletionWitnessServer
from .container_draft import container_draft_denied
from .strict_json import strict_json_object

_FIELDS = frozenset((
    "database", "owner_socket", "actor_socket", "owner_uid", "owner_gid",
    "actor_uid", "actor_gid", "runtime_uid", "principal",
))


def _read_private_witness_config(fd: int) -> dict[str, object]:
    """Read a single bounded newline-framed private config from inherited FD."""
    if type(fd) is not int or fd < 0:
        raise ValueError("private witness FD required")
    info = os.fstat(fd)
    if stat.S_ISREG(info.st_mode):
        if (stat.S_IMODE(info.st_mode) & 0o077
                or info.st_uid not in (0, os.geteuid())):
            raise ValueError("unsafe private witness config FD")
    elif not stat.S_ISFIFO(info.st_mode):
        raise ValueError("witness config must be private file or pipe")
    with os.fdopen(os.dup(fd), "rb") as source:
        raw = source.read(8193)
    if (not 3 <= len(raw) <= 8192 or not raw.endswith(b"\n")
            or raw.count(b"\n") != 1):
        raise ValueError("invalid private witness frame")
    config = strict_json_object(raw[:-1])
    if set(config) != _FIELDS:
        raise ValueError("unexpected private witness fields")
    resources = []
    for name in ("database", "owner_socket", "actor_socket"):
        value = config[name]
        if type(value) is not str or not value.startswith("/"):
            raise ValueError("noncanonical witness path")
        path = Path(value)
        if path != Path(os.path.normpath(path)):
            raise ValueError("noncanonical witness path")
        resources.append(path)
    if len(set(resources)) != len(resources):
        raise ValueError("overlapping witness resources")
    if any(type(config[k]) is not int for k in
           ("owner_uid", "owner_gid", "actor_uid", "actor_gid", "runtime_uid")):
        raise ValueError("invalid witness roles")
    if (config["owner_gid"] != config["owner_uid"]
            or config["actor_gid"] != config["actor_uid"]):
        raise ValueError("unsafe witness groups")
    try:
        _roles(os.geteuid(), config["owner_uid"], config["actor_uid"],
               config["runtime_uid"])
    except CompletionAuthorityError:
        # A malformed fourth-UID profile is a rejected private config,
        # never an uncaught traceback containing host state.
        raise ValueError("invalid private witness roles") from None
    principal = config["principal"]
    if (type(principal) is not str or not 1 <= len(principal) <= 128
            or principal.strip() != principal
            or any(ord(ch) < 32 or 0xD800 <= ord(ch) <= 0xDFFF
                   for ch in principal)):
        raise ValueError("invalid witness principal")
    return config


@dataclass(slots=True)
class ProtectedWitnessRuntime:
    server: CompletionWitnessServer
    ledger: CompletionWitnessLedger

    def close(self) -> None:
        # Never close the ledger before both socket roles and sessions drain.
        self.server.close()
        self.ledger.close()


def create_protected_witness_runtime(
    *, database: Path, owner_socket: Path, actor_socket: Path,
    owner_uid: int, owner_gid: int, actor_uid: int, actor_gid: int,
    runtime_uid: int, principal: str,
) -> ProtectedWitnessRuntime:
    """Open only the exact existing append-only journal under witness UID."""
    _roles(os.geteuid(), owner_uid, actor_uid, runtime_uid)
    if (type(owner_gid) is not int or owner_gid != owner_uid
            or type(actor_gid) is not int or actor_gid != actor_uid):
        raise ValueError("invalid Witness socket groups")
    paths = (database, owner_socket, actor_socket)
    if (any(not isinstance(path, Path) or not path.is_absolute()
            or path != Path(os.path.normpath(path)) for path in paths)
            or len(set(paths)) != len(paths)):
        raise ValueError("invalid Witness resources")
    if (type(principal) is not str or not 1 <= len(principal) <= 128
            or principal.strip() != principal
            or any(ord(c) < 32 for c in principal)):
        raise ValueError("invalid Witness principal")
    # initialize=False: missing/corrupt/rollbacked SQLite is never silently
    # replaced on start or restart.
    ledger = CompletionWitnessLedger(database, principal=principal, initialize=False)
    try:
        server = CompletionWitnessServer(
            ledger=ledger, owner_socket=owner_socket, authority_socket=actor_socket,
            owner_uid=owner_uid, owner_gid=owner_gid, authority_uid=actor_uid,
            authority_gid=actor_gid, runtime_uid=runtime_uid,
        )
        return ProtectedWitnessRuntime(server, ledger)
    except BaseException:
        ledger.close()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Kleinanzeiger independent A6 completion Witness")
    parser.add_argument("--config-fd", type=int, default=0,
                        help="inherited private configuration FD; no argv secrets")
    args = parser.parse_args(argv)
    if container_draft_denied():
        print("Kleinanzeiger: protected witness unavailable (draft denied)",
              file=sys.stderr)
        return 2
    try:
        cfg = _read_private_witness_config(args.config_fd)
    except (ValueError, TypeError, OSError):
        print("Kleinanzeiger: protected witness unavailable (config rejected)",
              file=sys.stderr)
        return 2
    try:
        runtime = create_protected_witness_runtime(
            database=Path(cfg["database"]),
            owner_socket=Path(cfg["owner_socket"]), actor_socket=Path(cfg["actor_socket"]),
            owner_uid=cfg["owner_uid"], owner_gid=cfg["owner_gid"],
            actor_uid=cfg["actor_uid"], actor_gid=cfg["actor_gid"],
            runtime_uid=cfg["runtime_uid"], principal=cfg["principal"],
        )
    except Exception:
        print("Kleinanzeiger: protected witness unavailable (admission denied)",
              file=sys.stderr)
        return 2

    stopped = Event()

    def on_stop(_sig, _frame):
        stopped.set()

    previous_term = signal.signal(signal.SIGTERM, on_stop)
    previous_int = signal.signal(signal.SIGINT, on_stop)
    try:
        runtime.server.start()
        threads = tuple(runtime.server._threads)
        if len(threads) != 2 or not all(t.is_alive() for t in threads):
            raise RuntimeError("witness listeners unavailable")
        print(json.dumps({"ready": True, "uid": os.geteuid()},
                         separators=(",", ":")), flush=True)
        while not stopped.wait(0.2):
            if (not all(t.is_alive() for t in threads)
                    or runtime.server._owner._fatal.is_set()
                    or runtime.server._authority._fatal.is_set()):
                raise RuntimeError("independent witness lost")
        runtime.close()
        return 0
    except Exception:
        # A lost ACK or shutdown exception can follow an external action.
        # Neither secrets nor a false completion success may escape.
        print("Kleinanzeiger: protected witness unavailable (shutdown uncertain)",
              file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


if __name__ == "__main__":
    raise SystemExit(main())