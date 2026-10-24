"""Independent protected browser actor for an already provisioned A6 host.

The actor UID has no SQLite or dashboard owner database access. It consumes
only a previously authenticated local CDP session, executes the existing
MarkService and media-create readbacks, and reports completion solely to an
independent Witness. This role never grants host cutover or browser login.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import signal
import socket
import stat
import sys
from threading import Event

from .application import MarkService
from .completion_authority import CompletionAuthorityServer, _roles
from .completion_witness import AuthorityWitnessClient
from .private_web_media import PrivateWebMediaHandleStore
from .protected_cdp_session import (
    ProtectedActorCdpSession, build_protected_actor_content_runtime,
    build_protected_actor_media_runtime,
)
from .private_web_media_verify import PrivateWebPublicMediaPersistenceVerifier
from .private_web_runtime import (
    PrivateWebContentRuntime, PrivateWebMediaCreateRuntime,
    PrivateWebMediaCreateService, _PrivateWebRuntimeAdsReader,
    _WriteOnlyReactionReader,
)
from .storage_owner import _linux_ucred_uid
from .strict_json import strict_json_object

_CONFIG_FIELDS = frozenset((
    "socket", "witness_socket", "owner_uid", "owner_gid", "witness_uid",
    "runtime_uid", "principal", "cdp_port", "timeout_seconds",
    "media_enabled", "media_directory",
))


def _canonical_path(value: object) -> Path:
    if type(value) is not str or not value.startswith("/"):
        raise ValueError("canonical actor path required")
    path = Path(value)
    if path != Path(os.path.normpath(path)):
        raise ValueError("canonical actor path required")
    return path


def _read_private_actor_config(fd: int) -> dict[str, object]:
    """Bounded exact config from a supervisor-held pipe or private regular FD."""
    if type(fd) is not int or fd < 0:
        raise ValueError("private actor FD required")
    info = os.fstat(fd)
    if stat.S_ISREG(info.st_mode):
        if stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid not in (0, os.geteuid()):
            raise ValueError("private actor FD rejected")
    elif not stat.S_ISFIFO(info.st_mode):
        raise ValueError("private actor FD rejected")
    with os.fdopen(os.dup(fd), "rb") as source:
        raw = source.read(8193)
    if (not 3 <= len(raw) <= 8192 or not raw.endswith(b"\n")
            or raw.count(b"\n") != 1):
        raise ValueError("invalid private actor config frame")
    config = strict_json_object(raw[:-1])
    if set(config) != _CONFIG_FIELDS:
        raise ValueError("private actor config fields rejected")
    socket_path = _canonical_path(config["socket"])
    witness_path = _canonical_path(config["witness_socket"])
    if socket_path == witness_path:
        raise ValueError("actor and witness sockets overlap")
    if any(type(config[k]) is not int for k in
           ("owner_uid", "owner_gid", "witness_uid", "runtime_uid", "cdp_port")):
        raise ValueError("invalid actor UID or CDP port")
    if type(config["timeout_seconds"]) not in (int, float):
        raise ValueError("invalid actor timeout")
    if (not math.isfinite(config["timeout_seconds"])
            or not 0 < config["timeout_seconds"] <= 30):
        raise ValueError("invalid actor timeout")
    if not 1 <= config["cdp_port"] <= 65535:
        raise ValueError("invalid actor CDP port")
    if type(config["media_enabled"]) is not bool:
        raise ValueError("invalid actor media gate")
    if (type(config["principal"]) is not str
            or not 1 <= len(config["principal"]) <= 128
            or config["principal"].strip() != config["principal"]
            or any(ord(c) < 32 for c in config["principal"])):
        raise ValueError("invalid actor principal")
    if config["media_enabled"]:
        media_path = _canonical_path(config["media_directory"])
        if media_path in {socket_path, witness_path}:
            raise ValueError("actor media path overlaps IPC")
    elif config["media_directory"] is not None:
        raise ValueError("disabled actor media directory must be null")
    return config


def _attest_witness_socket(path: Path, expected_uid: int) -> None:
    """Admission verifies a live independent peer; every operation reattests."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(2)
            peer.connect(str(path))
            if _linux_ucred_uid(
                peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            ) != expected_uid:
                raise ValueError("independent witness UID mismatch")
    except OSError:
        raise ValueError("independent witness unavailable") from None


@dataclass(slots=True)
class ProtectedActorRuntime:
    authority: CompletionAuthorityServer
    content: PrivateWebContentRuntime
    media_runtime: PrivateWebMediaCreateRuntime | None
    media_handles: PrivateWebMediaHandleStore | None
    cdp_session: ProtectedActorCdpSession | None = None

    def close(self) -> None:
        # Never shut down the browser or abandon handles while an external
        # submission is still in progress or while postread remains uncertain.
        self.authority.close()
        if self.media_runtime is not None:
            self.media_runtime.close()
        self.content.close()
        if self.cdp_session is not None:
            self.cdp_session.close()
        # Persistent staged handles deliberately survive an actor restart.
        # A future verified owner receipt/ACK may retire them; without such
        # authorization do not delete unresolved media on ordinary shutdown.


def create_protected_actor_runtime(
    *, socket_path: Path, witness_socket: Path, owner_uid: int, owner_gid: int,
    witness_uid: int, runtime_uid: int, principal: str, cdp_port: int,
    timeout_seconds: float = 5.0, media_enabled: bool = True,
    media_directory: Path | None = None,
) -> ProtectedActorRuntime:
    """Use live CDP adapters; refuse SQLite access or test-only browser peers."""
    _roles(os.geteuid(), owner_uid, witness_uid, runtime_uid)
    if type(owner_gid) is not int or owner_gid != owner_uid:
        raise ValueError("protected actor owner group must match owner UID")
    paths = (socket_path, witness_socket)
    if (any(not isinstance(path, Path) or not path.is_absolute()
            or path != Path(os.path.normpath(path)) for path in paths)
            or len(set(paths)) != len(paths)):
        raise ValueError("protected actor paths invalid")
    if (type(cdp_port) is not int or not 1 <= cdp_port <= 65535
            or type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 30
            or type(media_enabled) is not bool):
        raise ValueError("protected actor runtime parameters invalid")
    if (type(principal) is not str or not 1 <= len(principal) <= 128
            or principal.strip() != principal
            or any(ord(c) < 32 for c in principal)):
        raise ValueError("invalid actor principal")
    if media_enabled:
        if (not isinstance(media_directory, Path) or not media_directory.is_absolute()
                or media_directory != Path(os.path.normpath(media_directory))
                or media_directory in paths):
            raise ValueError("actor media directory invalid")
    elif media_directory is not None:
        raise ValueError("actor media path requires media gate")

    _attest_witness_socket(witness_socket, witness_uid)
    witness = AuthorityWitnessClient(
        witness_socket, expected_owner_uid=witness_uid,
    )
    content = None
    media_runtime = None
    handles = None
    session = None
    try:
        # This is the real product's authenticated CDP transport, not a
        # synthetic page/service or a local SQLite-backed write runtime.
        session = ProtectedActorCdpSession(
            port=cdp_port, timeout_seconds=float(timeout_seconds),
        )
        content = build_protected_actor_content_runtime(session)
        reader = _PrivateWebRuntimeAdsReader(content)
        service = MarkService(
            owner_reader=reader, management_reader=reader,
            delete_confirmation_reader=reader,
            reaction_reader=_WriteOnlyReactionReader(),
            state_writer=content.state_writer,
            delete_writer=content.delete_writer,
            content_writer=content.content_writer,
            create_writer=content.create_writer,
            content_reader_factory=content.content_reader_for,
            store=None, writes_enabled=True,
        )
        media_service = None
        if media_enabled:
            handles = PrivateWebMediaHandleStore(directory=media_directory)
            media_runtime = build_protected_actor_media_runtime(session)
            media_service = PrivateWebMediaCreateService(
                runtime=media_runtime, resolver=handles, reader=reader,
                confirmation_reader=reader,
                content_reader_factory=content.content_reader_for,
                media_persistence_verifier=PrivateWebPublicMediaPersistenceVerifier(),
                store=None, writes_enabled=True,
            )
        authority = CompletionAuthorityServer(
            socket_path=socket_path, owner_uid=owner_uid, owner_gid=owner_gid,
            runtime_uid=runtime_uid, principal=principal, service=service,
            witness_client=witness, media_service=media_service,
            media_stager=handles,
        )
        return ProtectedActorRuntime(authority, content, media_runtime, handles, session)
    except BaseException:
        # A failed startup never grants write authority. Retain private
        # persistent media on error; a retry must meet the same host gates.
        try:
            if media_runtime is not None:
                media_runtime.close()
        finally:
            if content is not None:
                content.close()
            if session is not None:
                session.close()
        raise


def main(argv: list[str] | None = None) -> int:
    import argparse
    from .container_draft import container_draft_denied

    parser = argparse.ArgumentParser(
        description="Kleinanzeiger independent protected browser actor")
    parser.add_argument("--config-fd", type=int, default=0)
    args = parser.parse_args(argv)
    if container_draft_denied():
        print("Kleinanzeiger: protected actor unavailable (draft denied)", file=sys.stderr)
        return 2
    try:
        cfg = _read_private_actor_config(args.config_fd)
        runtime = create_protected_actor_runtime(
            socket_path=Path(cfg["socket"]),
            witness_socket=Path(cfg["witness_socket"]),
            owner_uid=cfg["owner_uid"], owner_gid=cfg["owner_gid"],
            witness_uid=cfg["witness_uid"], runtime_uid=cfg["runtime_uid"],
            principal=cfg["principal"], cdp_port=cfg["cdp_port"],
            timeout_seconds=cfg["timeout_seconds"],
            media_enabled=cfg["media_enabled"],
            media_directory=(Path(cfg["media_directory"])
                             if cfg["media_directory"] is not None else None),
        )
    except Exception:
        # No paths, CDP/browser errors, tokens or SQLite information in logs.
        print("Kleinanzeiger: protected actor unavailable (admission denied)", file=sys.stderr)
        return 2

    stopped = Event()

    def request_stop(_signal, _frame):
        stopped.set()

    previous_term = signal.signal(signal.SIGTERM, request_stop)
    previous_int = signal.signal(signal.SIGINT, request_stop)
    try:
        runtime.authority.start()
        print(json.dumps({"ready": True, "uid": os.geteuid()},
                         separators=(",", ":")), flush=True)
        stopped.wait()
        runtime.close()
        return 0
    except Exception:
        print("Kleinanzeiger: protected actor unavailable (shutdown uncertain)", file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


if __name__ == "__main__":
    raise SystemExit(main())