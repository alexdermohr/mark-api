"""Protected SQLite/Write owner composition for an already trusted A6 host.

This is the real packaged role factory used by the installed multi-UID test.
It never launches a root helper, grants an untrusted UID raw SQLite or certifies
an existing host FD/MAP_SHARED/WAL/restore cutover.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from http.server import ThreadingHTTPServer

from .completion_authority import _roles, create_owner_write_api_server
from .completion_dashboard import CompletionDashboardOwnerServer
from .fence_witness import FenceWitnessClient
from .write_api import WriteApiAccess, WriteCapability


@dataclass(slots=True)
class ProtectedOwnerRuntime:
    """Owner-UID-only resources; caller must drain/close write_server first."""

    write_server: ThreadingHTTPServer
    dashboard_owner: CompletionDashboardOwnerServer
    witness: FenceWitnessClient

    def close(self) -> None:
        # Owner socket is retired only after the public API is quiescent. Do
        # not mark a still-running browser mutation as safely abandoned.
        try:
            self.dashboard_owner.stop()
        finally:
            self.witness.close()


def create_protected_owner_runtime(
    *, database: Path, dashboard_socket: Path, witness_socket: Path,
    actor_socket: Path, witness_uid: int, actor_uid: int,
    runtime_uid: int, runtime_gid: int, principal: str,
    bearer_token: str, media_enabled: bool = True, write_port: int = 0,
) -> ProtectedOwnerRuntime:
    """Compose only the existing verified A6 owner, actor and witness paths.

    Every SQLite open is through the independent owner UID. The dashboard
    uses the SAME already-attested SnapshotStore as the Write API, never a
    second weaker database identity or a generic untrusted completion RPC.
    """
    paths = (database, dashboard_socket, witness_socket, actor_socket)
    if any(not isinstance(path, Path) or not path.is_absolute()
           or path != Path(os.path.normpath(path)) for path in paths):
        raise ValueError("absolute canonical protected owner paths required")
    if len(set(paths)) != len(paths):
        raise ValueError("protected owner resources must be distinct")
    if (type(runtime_gid) is not int or runtime_gid <= 0
            or type(media_enabled) is not bool
            or type(write_port) is not int or not 0 <= write_port <= 65535):
        raise ValueError("invalid protected owner runtime parameters")
    # No root or shared runtime/actor/witness UID may become an owner.
    _roles(os.geteuid(), witness_uid, actor_uid, runtime_uid)
    witness = FenceWitnessClient(
        witness_socket, expected_owner_uid=witness_uid,
    )
    dashboard_owner = None
    try:
        capabilities = {
            WriteCapability.CREATE, WriteCapability.UPDATE_CONTENT,
            WriteCapability.SET_STATE, WriteCapability.DELETE,
        }
        if media_enabled:
            capabilities.add(WriteCapability.CREATE_MEDIA)
        access = WriteApiAccess(
            principal=principal, bearer_token=bearer_token, writes_enabled=True,
            capabilities=frozenset(capabilities),
        )
        dashboard_owner = CompletionDashboardOwnerServer(
            database=database, socket_path=dashboard_socket,
            runtime_uid=runtime_uid, runtime_gid=runtime_gid,
            access=access, fence_witness=witness,
        )
        dashboard_owner.start()
        write_server = create_owner_write_api_server(
            store=dashboard_owner.store, access=access,
            authority_socket=actor_socket, authority_uid=actor_uid,
            runtime_uid=runtime_uid, fence_witness=witness,
            port=write_port,
        )
        return ProtectedOwnerRuntime(write_server, dashboard_owner, witness)
    except BaseException:
        # Startup must never leave an orphaned protected owner listener after
        # the actor / witness / media contract refused its admission.
        try:
            if dashboard_owner is not None:
                dashboard_owner.stop()
        finally:
            witness.close()
        raise


# An independent host supervisor must provide this service's private config
# over an inherited pipe/FD. No untrusted process can select another UID.
def _read_private_owner_config(fd: int) -> dict[str, object]:
    import stat
    from .strict_json import strict_json_object

    if type(fd) is not int or fd < 0:
        raise ValueError("private owner configuration FD required")
    info = os.fstat(fd)
    if stat.S_ISREG(info.st_mode):
        if stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid not in (0, os.geteuid()):
            raise ValueError("unsafe private owner configuration FD")
    elif not stat.S_ISFIFO(info.st_mode):
        raise ValueError("private owner configuration must be a file or pipe")
    with os.fdopen(os.dup(fd), "rb") as private:
        raw = private.read(8193)
    if len(raw) < 3 or len(raw) > 8192 or raw.count(b"\n") != 1 or not raw.endswith(b"\n"):
        raise ValueError("invalid bounded private owner config frame")
    config = strict_json_object(raw[:-1])
    expected = {
        "database", "dashboard_socket", "witness_socket", "actor_socket",
        "witness_uid", "actor_uid", "runtime_uid", "runtime_gid",
        "principal", "bearer_token", "media_enabled", "write_port",
    }
    # Older preprovisioned runtimes remain valid with sync disabled; a
    # trusted supervisor can explicitly enable periodic Actor-sourced reads.
    # No untrusted runtime chooses the interval or CDP endpoint.
    optional = {"sync_interval_seconds"}
    if set(config) not in (expected, expected | optional):
        raise ValueError("unexpected private owner config")
    if optional <= set(config):
        value = config["sync_interval_seconds"]
        if (type(value) not in (int, float)
                or not (value == 0 or 60 <= value <= 86400)):
            raise ValueError("invalid protected sync interval")
    for key in ("database", "dashboard_socket", "witness_socket", "actor_socket"):
        path = config[key]
        if type(path) is not str or not path.startswith("/"):
            raise ValueError("invalid owner path")
    if any(type(config[key]) is not int
           for key in ("witness_uid", "actor_uid", "runtime_uid", "runtime_gid", "write_port")):
        raise ValueError("invalid owner UID/port")
    if type(config["media_enabled"]) is not bool:
        raise ValueError("invalid protected media gate")
    for key in ("principal", "bearer_token"):
        if type(config[key]) is not str or not config[key]:
            raise ValueError("invalid protected principal/token")
    return config


def main(argv: list[str] | None = None) -> int:
    """Serve only a preprovisioned multi-UID product. No host cutover powers."""
    import argparse
    import json
    import signal
    import sys
    from threading import Event, Thread

    from .container_draft import container_draft_denied
    from .protected_inventory import (
        OwnerActorInventoryClient, ProtectedInventoryError,
        _READ_CYCLE_SECONDS, sync_actor_inventory_once,
    )

    parser = argparse.ArgumentParser(
        description="Kleinanzeiger independent A6 SQLite owner and Write API")
    parser.add_argument("--config-fd", type=int, default=0,
                        help="inherited private pipe/file descriptor, never argv secrets")
    args = parser.parse_args(argv)
    if container_draft_denied():
        print("Kleinanzeiger: protected owner unavailable (draft denied)", file=sys.stderr)
        return 2
    try:
        config = _read_private_owner_config(args.config_fd)
    except (ValueError, TypeError, OSError):
        print("Kleinanzeiger: protected owner unavailable (config rejected)", file=sys.stderr)
        return 2
    try:
        runtime = create_protected_owner_runtime(
            database=Path(config["database"]),
            dashboard_socket=Path(config["dashboard_socket"]),
            witness_socket=Path(config["witness_socket"]),
            actor_socket=Path(config["actor_socket"]),
            witness_uid=config["witness_uid"], actor_uid=config["actor_uid"],
            runtime_uid=config["runtime_uid"], runtime_gid=config["runtime_gid"],
            principal=config["principal"], bearer_token=config["bearer_token"],
            media_enabled=config["media_enabled"], write_port=config["write_port"],
        )
    except Exception:
        # Never log a path, token, low-level host, kernel or SQLite exception.
        print("Kleinanzeiger: protected owner unavailable (admission denied)", file=sys.stderr)
        return 2

    server = runtime.write_server
    stopped = Event()
    sync_failed = Event()
    interval = float(config.get("sync_interval_seconds", 0))
    sync_worker: Thread | None = None

    if interval:
        try:
            # This is the SAME attested SnapshotStore used by the write API.
            # The actor owns CDP reads; neither the ordinary frontend nor a
            # second SQLite writer can inject owner-observed inventory.
            reader = OwnerActorInventoryClient(
                actor_socket=Path(config["actor_socket"]),
                actor_uid=config["actor_uid"],
            )

            def sync_periodically() -> None:
                while not stopped.is_set():
                    try:
                        sync_actor_inventory_once(runtime.dashboard_owner.store, reader)
                    except ProtectedInventoryError as exc:
                        if not exc.retryable_read:
                            # Unresolved SQLite commit/Write recovery is never
                            # transformed into a new actor observation.
                            sync_failed.set()
                            return
                        # A proven *durably failed* read-only cycle may start
                        # a fresh observation after the configured interval.
                        # The actor's old page token expires independently.
                    except Exception:
                        sync_failed.set()
                        return
                    if stopped.wait(interval):
                        return

            # With a non-daemon worker, a stuck kernel/socket extension keeps
            # the process alive even AFTER a failed bounded join. The owner
            # exits nonzero without closing still-active resource fences.
            sync_worker = Thread(
                target=sync_periodically, daemon=True,
                name="kleinanzeiger-protected-inventory",
            )
        except Exception:
            print("Kleinanzeiger: protected inventory unavailable (admission denied)",
                  file=sys.stderr)
            return 2
    def stop(_signum, _frame) -> None:
        stopped.set()
    previous_term = signal.signal(signal.SIGTERM, stop)
    previous_int = signal.signal(signal.SIGINT, stop)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                    daemon=True, name="kleinanzeiger-owner-write-api")
    try:
        thread.start()
        if sync_worker is not None:
            sync_worker.start()
        print(json.dumps({"ready": True, "uid": os.geteuid(),
                          "port": server.server_port}, separators=(",", ":")), flush=True)
        stopped.wait()
        server.shutdown()
        if hasattr(server, "wait_for_active_handlers"):
            if not server.wait_for_active_handlers(10):
                # Keep witness/fences in the conservative unresolved state;
                # a timeout is no proof that an external Write was quiescent.
                return 2
        server.server_close()
        thread.join(3)
        if thread.is_alive():
            return 2
        if sync_worker is not None:
            # A pending browser read or SQLite commit is not proven stopped
            # by SIGTERM alone. Keep that process/journal alive until drained.
            sync_worker.join(_READ_CYCLE_SECONDS + 5)
            if sync_worker.is_alive():
                # Never claim a successful drain while an old read or SQLite
                # commit might still run. Process exit does not authorize
                # restore or any new platform Write.
                return 2
        runtime.close()
        return 2 if sync_failed.is_set() else 0
    except Exception:
        # A host/SQLite/server error during SIGTERM is UNKNOWN, never a
        # successful drain. Do not expose paths, tokens or low-level details.
        print("Kleinanzeiger: protected owner unavailable (shutdown uncertain)", file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


if __name__ == "__main__":
    raise SystemExit(main())