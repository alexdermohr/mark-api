"""Protected, one-shot browser authority for the existing owner Write API.

Only the protected SQLite-owner UID may submit *intents* to this AF_UNIX
endpoint. The separately trusted actor runs the existing MarkService (fresh
precondition, one writer invocation, independent postread); it has no SQLite
store. The owner authenticates the kernel peer, binds the returned evidence to
its already-begun request/generation and persists the real operation receipt.
There is no executor-supplied completion, clearance, SQL or arbitrary dispatch.

This is a product composition seam, NOT an attestation of host cutover. Trusted
bootstrap must independently protect the actor's code, adapters, browser/CDP
endpoint and credentials from the ordinary runtime UID. Existing container,
old-FD/mapping and restore gates remain in force. Media composition is separate.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import socketserver
import stat
from threading import BoundedSemaphore, Condition, Lock, RLock, Thread
from typing import Any

from .application import MarkService
from .domain import (
    AdCreateRequest, CreateOperationReceipt, DeleteApproval, LifecycleState,
    MediaPostReadStatus, OperationOutcome, OperationReceipt,
)
from .fence_witness import FenceWitnessClient, FenceWitnessError, _private_directory
from .private_web_media import PrivateWebMediaHandleStore
from .protected_inventory import ActorInventoryPages
from .private_web_runtime import PrivateWebMediaCreateService
from .storage import SnapshotStore
from .storage_owner import (
    StorageOwnerError, _attested_database, _linux_ucred_uid,
    _snapshot_as_json, _snapshot_from_json,
)
from .strict_json import strict_json_object
from .write_api import (
    WriteApiAccess, WriteCapability, _MAX_MEDIA_BODY_BYTES, _MEDIA_REF_RE,
    _create_receipt_matches_request,
    _create_receipt_to_dict, _normalize_create_payload, _receipt_to_dict,
    _request_fingerprint, _valid_ad_id, create_write_api_server,
)

_MAX_FRAME = 256 * 1024
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_GENERATION = re.compile(r"write-api-[a-f0-9]{32}\Z")
_ACTIONS = frozenset({"create", "create_media", "update_content", "pause", "activate", "delete"})


class CompletionAuthorityError(RuntimeError):
    """Unknown or rejected authority exchange; never permission to retry a Write."""


def _fail() -> CompletionAuthorityError:
    return CompletionAuthorityError("independent completion not verified")


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _fail() from None


def _text(value: object, bound: int = 160, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if (type(value) is not str or not value or len(value) > bound
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise _fail()
    return value


def _time(value: object) -> datetime:
    value = _text(value, 80)
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None or result.isoformat() != value:
            raise ValueError()
        return result
    except (ValueError, TypeError, OverflowError):
        raise _fail() from None


def _roles(*uids: int) -> None:
    if (any(type(uid) is not int or uid <= 0 or uid >= 2**32 - 1 for uid in uids)
            or len(set(uids)) != len(uids) or not hasattr(socket, "SO_PEERCRED")):
        raise _fail()


def _media_name(value: object) -> str:
    name = _text(value, 255)
    if (name in {".", ".."} or "/" in name or "\\" in name
            or len(name.encode("utf-8")) > 255):
        raise _fail()
    return name


def _media_refs(value: object) -> tuple[str, ...]:
    if (type(value) is not list or not 1 <= len(value) <= 32
            or any(type(ref) is not str or _MEDIA_REF_RE.fullmatch(ref) is None
                   for ref in value) or len(set(value)) != len(value)):
        raise _fail()
    return tuple(value)


def _route(action: str, target: str | None) -> tuple[str, str]:
    if action in {"create", "create_media"}:
        return "POST", ("/api/write/media/ads" if action == "create_media" else "/api/write/ads")
    route = "/api/write/ads/" + str(target)
    if action in {"pause", "activate"}:
        return "POST", route + "/" + action
    return ("PATCH" if action == "update_content" else "DELETE"), route


@dataclass(frozen=True, slots=True)
class CompletionIntent:
    key: str
    sha: str
    generation: str
    principal: str
    action: str
    target: str | None
    payload_json: str

    @classmethod
    def from_wire(cls, raw: object, *, expected_principal: str) -> "CompletionIntent":
        try:
            if type(raw) is not dict or set(raw) != {
                "version", "op", "key", "sha", "generation", "principal",
                "action", "target", "payload",
            }:
                raise _fail()
            if type(raw["version"]) is not int or raw["version"] != 1 or raw["op"] != "execute":
                raise _fail()
            for name, pattern in (("key", _KEY), ("sha", _SHA), ("generation", _GENERATION)):
                if type(raw[name]) is not str or pattern.fullmatch(raw[name]) is None:
                    raise _fail()
            principal = _text(raw["principal"], 128)
            if principal != expected_principal or principal.strip() != principal:
                raise _fail()
            action, target, payload = raw["action"], raw["target"], raw["payload"]
            if type(action) is not str or action not in _ACTIONS or type(payload) is not dict:
                raise _fail()
            encoded = _canonical(payload)
            if len(encoded) > 16 * 1024:
                raise _fail()
            # Reparse even local callers: immutable canonical payload, no NaN,
            # aliasing, duplicate names, deep nesting or unpaired surrogates.
            payload = strict_json_object(encoded)
            if action in {"create", "create_media"}:
                if target is not None:
                    raise _fail()
                _, _, normalized = _normalize_create_payload(payload, with_media=action == "create_media")
                if _canonical(normalized) != encoded:
                    raise _fail()
            else:
                if type(target) is not str or not _valid_ad_id(target):
                    raise _fail()
                if action == "update_content":
                    if (not payload or set(payload) - {"title", "description"}
                            or any(type(value) is not str for value in payload.values())):
                        raise _fail()
                elif payload:
                    raise _fail()
            method, path = _route(action, target)
            if raw["sha"] != _request_fingerprint(
                principal=principal, method=method, path=path, payload=payload,
            ):
                raise _fail()
            return cls(raw["key"], raw["sha"], raw["generation"], principal,
                       action, target, encoded.decode("utf-8"))
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
            raise _fail() from None

    @property
    def payload(self) -> dict[str, object]:
        return strict_json_object(self.payload_json.encode("utf-8"))

    @property
    def binding(self) -> dict[str, object]:
        return {"key": self.key, "sha": self.sha, "generation": self.generation,
                "principal": self.principal, "action": self.action, "target": self.target}

    def to_wire(self) -> dict[str, object]:
        return {"version": 1, "op": "execute", **self.binding, "payload": self.payload}


def encode_evidence(intent: CompletionIntent, receipt: OperationReceipt | CreateOperationReceipt) -> dict[str, object]:
    if type(receipt) is OperationReceipt:
        row = _receipt_to_dict(receipt)
        names = ("pre_snapshot", "post_snapshot")
    elif type(receipt) is CreateOperationReceipt:
        row = _create_receipt_to_dict(receipt)
        names = ("post_snapshot", "confirmation_post_snapshot", "content_post_snapshot")
    else:
        raise _fail()
    return {
        "version": 1, "ok": True, "binding": intent.binding, "receipt": row,
        "snapshots": {name: None if getattr(receipt, name) is None else
                      _snapshot_as_json(getattr(receipt, name)) for name in names},
    }


def decode_evidence(raw: object, intent: CompletionIntent) -> OperationReceipt | CreateOperationReceipt:
    """Validate data only. Kernel peer authentication is additionally mandatory."""
    try:
        if (type(raw) is not dict or set(raw) != {"version", "ok", "binding", "receipt", "snapshots"}
                or type(raw["version"]) is not int or raw["version"] != 1
                or raw["ok"] is not True or raw["binding"] != intent.binding):
            raise _fail()
        row, snapshots = raw["receipt"], raw["snapshots"]
        common = {"operation", "started_at", "completed_at", "outcome", "pre_read_status",
                  "post_read_status", "writer_invoked", "authorization_by",
                  "authorization_reference", "writer_error"}
        create = intent.action in {"create", "create_media"}
        media = intent.action == "create_media"
        expected = common | (({"created_ad_id", "confirmation_pre_read_status",
                               "confirmation_post_read_status", "content_post_read_status"}
                              | ({"media_post_read_status", "media_persistence_confirmed"} if media else set()))
                             if create else {"ad_id"})
        snapshot_names = ({"post_snapshot", "confirmation_post_snapshot", "content_post_snapshot"}
                          if create else {"pre_snapshot", "post_snapshot"})
        if (type(row) is not dict or set(row) != expected or type(snapshots) is not dict
                or set(snapshots) != snapshot_names or type(row["writer_invoked"]) is not bool):
            raise _fail()
        operation = {"pause": "set_state:paused", "activate": "set_state:active", "create_media": "create"}.get(intent.action, intent.action)
        if (row["operation"] != operation or row["authorization_by"] != intent.principal
                or row["authorization_reference"] != "write-api:" + intent.key):
            raise _fail()
        values = {name: _text(row[name], optional=name in {"post_read_status", "writer_error"})
                  for name in ("operation", "pre_read_status", "post_read_status", "writer_error")}
        values.update(
            started_at=_time(row["started_at"]), completed_at=_time(row["completed_at"]),
            outcome=OperationOutcome(_text(row["outcome"])), writer_invoked=row["writer_invoked"],
            authorization_by=intent.principal, authorization_reference="write-api:" + intent.key,
        )
        values.update({name: None if snapshots[name] is None else _snapshot_from_json(snapshots[name])
                       for name in snapshot_names})
        if create:
            values.update({name: _text(row[name], optional=name != "confirmation_pre_read_status")
                           for name in ("created_ad_id", "confirmation_pre_read_status",
                                        "confirmation_post_read_status", "content_post_read_status")})
            if media:
                if type(row["media_persistence_confirmed"]) is not bool:
                    raise _fail()
                values.update(
                    media_post_read_status=MediaPostReadStatus(_text(row["media_post_read_status"])),
                    media_persistence_confirmed=row["media_persistence_confirmed"],
                )
            receipt = CreateOperationReceipt(**values)
            request, _, _ = _normalize_create_payload(intent.payload, with_media=media)
            if not _create_receipt_matches_request(
                receipt, request, principal=intent.principal, authorization_reference="write-api:" + intent.key,
            ):
                raise _fail()
        else:
            if row["ad_id"] != intent.target:
                raise _fail()
            receipt = OperationReceipt(ad_id=intent.target, **values)
            for snapshot in (receipt.pre_snapshot, receipt.post_snapshot):
                if snapshot is not None and snapshot.ad_id != intent.target:
                    raise _fail()
        if receipt.outcome is OperationOutcome.CONFIRMED:
            # Matching content is not a fresh observation of this execution.
            # All evidence admitted as success must have been observed during
            # this actor operation, not copied from an earlier valid snapshot.
            observations = [getattr(receipt, name) for name in snapshot_names
                            if getattr(receipt, name) is not None]
            if any(not receipt.started_at <= snapshot.observed_at <= receipt.completed_at
                   for snapshot in observations):
                raise _fail()
            if not create and (receipt.pre_snapshot is not None
                               and receipt.post_snapshot is not None
                               and receipt.post_snapshot.observed_at < receipt.pre_snapshot.observed_at):
                raise _fail()
            if not receipt.writer_invoked:
                raise _fail()
            if create:
                if media and (receipt.media_post_read_status is not MediaPostReadStatus.CONFIRMED
                              or receipt.media_persistence_confirmed is not True):
                    raise _fail()
                if any(getattr(receipt, name) != "success_nonempty" for name in (
                    "post_read_status", "confirmation_post_read_status", "content_post_read_status",
                )):
                    raise _fail()
            else:
                pre, post = receipt.pre_snapshot, receipt.post_snapshot
                if pre is None or receipt.pre_read_status != "success_nonempty":
                    raise _fail()
                if intent.action == "delete":
                    if post is not None or receipt.post_read_status not in {"success_empty", "success_nonempty"}:
                        raise _fail()
                else:
                    if post is None or receipt.post_read_status != "success_nonempty":
                        raise _fail()
                    if intent.action in {"pause", "activate"}:
                        desired = LifecycleState.PAUSED if intent.action == "pause" else LifecycleState.ACTIVE
                        previous = LifecycleState.ACTIVE if intent.action == "pause" else LifecycleState.PAUSED
                        if post.lifecycle_state is not desired or pre.lifecycle_state is not previous:
                            raise _fail()
                    elif (pre.lifecycle_state not in {LifecycleState.ACTIVE, LifecycleState.PAUSED}
                          or any(getattr(post, field) != value for field, value in intent.payload.items())):
                        raise _fail()
        return receipt
    except (KeyError, ValueError, TypeError, OverflowError, StorageOwnerError, UnicodeError):
        raise _fail() from None


class _AuthorityHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: CompletionAuthorityServer = self.server
        self.request.settimeout(10.0)
        try:
            # Authenticate before reading any caller-controlled operation data.
            credentials = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if _linux_ucred_uid(credentials) != server.owner_uid:
                return
            frame = self.rfile.readline(_MAX_FRAME + 2)
            if not frame.endswith(b"\n") or len(frame) > _MAX_FRAME + 1 or len(frame) <= 1:
                raise _fail()
            request = strict_json_object(frame[:-1])
            if request.get("op") == "media_stage":
                if (set(request) != {"version", "op", "filename", "length", "sha256"}
                        or type(request["version"]) is not int or request["version"] != 1
                        or type(request["length"]) is not int
                        or not 0 < request["length"] <= _MAX_MEDIA_BODY_BYTES
                        or type(request["sha256"]) is not str
                        or _SHA.fullmatch(request["sha256"]) is None):
                    raise _fail()
                name = _media_name(request["filename"])
                data = self.rfile.read(request["length"])
                if (len(data) != request["length"]
                        or hashlib.sha256(data).hexdigest() != request["sha256"]):
                    raise _fail()
                ref = server.stage_media(name, data)
                response = {"version": 1, "ok": True, "media_ref": ref}
            elif request.get("op") == "media_discard":
                if set(request) != {"version", "op", "media_refs"} or request["version"] != 1:
                    raise _fail()
                refs = _media_refs(request["media_refs"])
                server.discard_media(refs)
                response = {"version": 1, "ok": True, "discarded": len(refs)}
            elif request.get("op") == "media_profile":
                if set(request) != {"version", "op"} or request["version"] != 1:
                    raise _fail()
                response = {"version": 1, "ok": True,
                            "media_ready": server.media_ready}
            elif request.get("op") == "inventory_begin":
                if set(request) != {"version", "op"} or type(request["version"]) is not int or request["version"] != 1:
                    raise _fail()
                response = server.inventory_begin()
            elif request.get("op") == "inventory_page":
                if set(request) != {"version", "op", "token", "offset"} or type(request["version"]) is not int or request["version"] != 1:
                    raise _fail()
                response = server.inventory_page(request["token"], request["offset"])
            elif request.get("op") == "inventory_finish":
                if set(request) != {"version", "op", "token", "digest"} or type(request["version"]) is not int or request["version"] != 1:
                    raise _fail()
                response = server.inventory_finish(request["token"], request["digest"])
            else:
                intent = CompletionIntent.from_wire(request, expected_principal=server.principal)
                response = server.execute(intent)
            encoded = _canonical(response)
            if len(encoded) > _MAX_FRAME:
                raise _fail()
            self.wfile.write(encoded + b"\n")
            self.wfile.flush()
        except Exception:
            # Failure can follow a real platform effect. Never return 'not run'
            # or leak local paths, SQL, credentials or browser exception text.
            try:
                self.wfile.write(b'{"error":"completion_unknown","ok":false,"version":1}\n')
                self.wfile.flush()
            except OSError:
                pass


class CompletionAuthorityServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Trusted actuation/readback process. No SQLite and no public completion RPC."""
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(self, *, socket_path: Path, owner_uid: int, owner_gid: int,
                 runtime_uid: int, principal: str, service: MarkService,
                 witness_client=None, media_service: PrivateWebMediaCreateService | None = None,
                 media_stager: PrivateWebMediaHandleStore | None = None) -> None:
        from .completion_witness import AuthorityWitnessClient
        actor_uid = os.geteuid()
        if type(witness_client) is not AuthorityWitnessClient:
            raise _fail()
        _roles(actor_uid, owner_uid, runtime_uid, witness_client.expected_owner_uid)
        self._witness = witness_client
        if (type(owner_gid) is not int or owner_gid <= 0
                or not isinstance(socket_path, Path) or not socket_path.is_absolute()
                or socket_path != Path(os.path.normpath(socket_path))
                or type(service) is not MarkService or service._store is not None
                or service._writes._store is not None or service._writes._writes_enabled is not True):
            raise _fail()
        if (media_service is None) != (media_stager is None):
            raise _fail()
        if media_service is not None:
            # Only actor-owned, private durable handles are permitted. The
            # ordinary UID cannot supply filesystem paths, a fake media
            # verifier or another resolver through an owner HTTP request.
            if (type(media_service) is not PrivateWebMediaCreateService
                    or type(media_stager) is not PrivateWebMediaHandleStore
                    or media_service._resolver is not media_stager
                    or media_service._store is not None
                    or media_service._writes_enabled is not True
                    or media_service._media_persistence_verifier is None
                    or not media_stager._persistent):
                raise _fail()
            info = os.lstat(media_stager._directory_path)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != actor_uid
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise _fail()
        self.principal = _text(principal, 128)
        if self.principal.strip() != self.principal:
            raise _fail()
        self._media_service, self._media_stager = media_service, media_stager
        self.owner_uid, self.runtime_uid = owner_uid, runtime_uid
        self._service, self._socket_path = service, socket_path
        self._operation_lock = Lock()
        # Inventory observations and browser Writes serialize on the exact
        # actor lock. No ordinary runtime UID can query this owner-only UDS.
        self._inventory = ActorInventoryPages(service._owner_reader, self._operation_lock)
        self._workers = Condition()
        self._active = 0
        self._capacity = BoundedSemaphore(8)
        self._spent: set[str] = set()
        self._thread: Thread | None = None
        self._closed = False
        self._identity: tuple[int, int] | None = None
        _private_directory(socket_path.parent, actor_uid, mode=0o2710, gid=owner_gid)
        if socket_path.exists() or socket_path.is_symlink():
            raise _fail()
        try:
            super().__init__(str(socket_path), _AuthorityHandler)
            os.chmod(socket_path, 0o660, follow_symlinks=False)
            info = socket_path.lstat()
            if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != actor_uid
                    or info.st_gid != owner_gid or stat.S_IMODE(info.st_mode) != 0o660
                    or info.st_nlink != 1):
                raise _fail()
            self._identity = info.st_dev, info.st_ino
        except BaseException:
            if hasattr(self, "socket"):
                self.socket.close()
            raise

    def process_request(self, request, client_address) -> None:
        if not self._capacity.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._workers:
            self._active += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._finished()
            raise

    def _finished(self) -> None:
        self._capacity.release()
        with self._workers:
            self._active -= 1
            self._workers.notify_all()

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._finished()

    def inventory_begin(self) -> dict[str, object]:
        if self._closed:
            raise _fail()
        return self._inventory.begin()

    def inventory_page(self, token: object, offset: object) -> dict[str, object]:
        if self._closed:
            raise _fail()
        return self._inventory.page(token, offset)

    def inventory_finish(self, token: object, digest: object) -> dict[str, object]:
        if self._closed:
            raise _fail()
        return self._inventory.finish(token, digest)

    @property
    def media_ready(self) -> bool:
        return self._media_service is not None and not self._closed

    def stage_media(self, filename: str, data: bytes) -> str:
        if not self.media_ready or self._media_stager is None:
            raise _fail()
        name = _media_name(filename)
        if type(data) is not bytes or not 0 < len(data) <= _MAX_MEDIA_BODY_BYTES:
            raise _fail()
        return self._media_stager.stage_media(name, data)

    def discard_media(self, refs: tuple[str, ...]) -> None:
        if not self.media_ready or self._media_stager is None:
            raise _fail()
        self._media_stager.discard(_media_refs(list(refs)))

    def execute(self, intent: CompletionIntent) -> dict[str, object]:
        intent = CompletionIntent.from_wire(intent.to_wire(), expected_principal=self.principal)
        if not self._operation_lock.acquire(blocking=False):
            raise _fail()
        try:
            # A lost reply cannot turn a duplicate command into a second input.
            # A durable independent reservation also survives actor restart.
            # No reconnect/retry exists in OwnerCompletionService.
            if self._closed or intent.key in self._spent or len(self._spent) >= 65536:
                raise _fail()
            self._spent.add(intent.key)
            # Reserve before ANY potentially effectful service entry. A lost
            # reservation ACK remains fenced and must never trigger input.
            self._witness.reserve(intent)
            authorization = {"authorization_by": self.principal,
                             "authorization_reference": "write-api:" + intent.key}
            if intent.action == "create":
                request, _, _ = _normalize_create_payload(intent.payload, with_media=False)
                receipt = self._service.create(request, **authorization)
            elif intent.action == "create_media":
                if not self.media_ready:
                    raise _fail()
                request, refs, _ = _normalize_create_payload(intent.payload, with_media=True)
                if refs is None or len(refs) > 32:
                    raise _fail()
                receipt = self._media_service.create_with_media(request, refs, **authorization)
            elif intent.action == "update_content":
                receipt = self._service.update_content(intent.target, **intent.payload, **authorization)
            elif intent.action == "pause":
                receipt = self._service.pause(intent.target, **authorization)
            elif intent.action == "activate":
                receipt = self._service.activate(intent.target, **authorization)
            else:
                receipt = self._service.delete(intent.target, approval=DeleteApproval(
                    intent.target, self.principal, "write-api:" + intent.key,
                ))
            result = encode_evidence(intent, receipt)
            # Even trusted adapter mistakes are never promoted to completion.
            decode_evidence(result, intent)
            # The witness receives this proof directly from the actor UID,
            # never from the ordinary runtime or the owner HTTP response.
            self._witness.verify(intent, result)
            return result
        finally:
            self._operation_lock.release()

    def start(self) -> None:
        if self._closed or self._thread is not None:
            raise _fail()
        self._thread = Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()

    def close(self, timeout_seconds: float = 10.0) -> None:
        if self._closed:
            return
        if self._thread is not None:
            self.shutdown()
            self._thread.join(timeout_seconds)
            if self._thread.is_alive():
                raise _fail()
        with self._workers:
            if not self._workers.wait_for(lambda: self._active == 0, timeout_seconds):
                raise _fail()
        socketserver.UnixStreamServer.server_close(self)
        if self._identity is not None:
            try:
                info = self._socket_path.lstat()
            except FileNotFoundError:
                raise _fail() from None
            if (info.st_dev, info.st_ino) != self._identity or not stat.S_ISSOCK(info.st_mode):
                raise _fail()
            self._socket_path.unlink()
        self._closed = True


class OwnerMediaStager:
    """Owner-only, peer-authenticated binary relay into the actor handle store.

    The ordinary runtime submits bounded media bytes via the existing bearer-
    guarded HTTP upload, never an arbitrary path or executor-supplied handle.
    The actor alone owns all stable media sources. Stage/discard are local
    operations and cannot complete or retry a platform write.
    """

    def __init__(self, *, socket_path: Path, authority_uid: int,
                 runtime_uid: int, timeout_seconds: float = 30.0) -> None:
        _roles(os.geteuid(), authority_uid, runtime_uid)
        if (not isinstance(socket_path, Path) or not socket_path.is_absolute()
                or socket_path != Path(os.path.normpath(socket_path))
                or type(timeout_seconds) not in {int, float}
                or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 60):
            raise _fail()
        self._socket_path = socket_path
        self._actor_uid = authority_uid
        self._owner_uid = os.geteuid()
        self._timeout = float(timeout_seconds)
        self.verify_ready()

    def _call(self, request: dict[str, object], data: bytes = b"") -> dict[str, object]:
        if os.geteuid() != self._owner_uid:
            raise _fail()
        frame = _canonical(request)
        if len(frame) > _MAX_FRAME:
            raise _fail()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(self._timeout)
                peer.connect(str(self._socket_path))
                if _linux_ucred_uid(peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)) != self._actor_uid:
                    raise _fail()
                peer.sendall(frame + b"\n")
                if data:
                    peer.sendall(data)
                with peer.makefile("rb") as stream:
                    answer = stream.readline(_MAX_FRAME + 2)
            if not answer.endswith(b"\n") or not 1 < len(answer) <= _MAX_FRAME + 1:
                raise _fail()
            result = strict_json_object(answer[:-1])
            if type(result) is not dict or result.get("version") != 1 or result.get("ok") is not True:
                raise _fail()
            return result
        except (OSError, ValueError, UnicodeError):
            raise _fail() from None

    def verify_ready(self) -> None:
        response = self._call({"version": 1, "op": "media_profile"})
        if set(response) != {"version", "ok", "media_ready"} or response["media_ready"] is not True:
            raise _fail()

    def stage_media(self, filename: str, data: bytes) -> str:
        name = _media_name(filename)
        if type(data) is not bytes or not 0 < len(data) <= _MAX_MEDIA_BODY_BYTES:
            raise ValueError("invalid_media_upload")
        response = self._call({
            "version": 1, "op": "media_stage", "filename": name,
            "length": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        }, data)
        if (set(response) != {"version", "ok", "media_ref"}
                or type(response["media_ref"]) is not str
                or _MEDIA_REF_RE.fullmatch(response["media_ref"]) is None):
            raise _fail()
        return response["media_ref"]

    def discard(self, media_refs: tuple[str, ...]) -> None:
        refs = _media_refs(list(media_refs)) if type(media_refs) is tuple else _media_refs(None)
        response = self._call({"version": 1, "op": "media_discard", "media_refs": list(refs)})
        if (set(response) != {"version", "ok", "discarded"}
                or type(response["discarded"]) is not int
                or response["discarded"] != len(refs)):
            raise _fail()


class OwnerCompletionService:
    """Existing Write API service backed only by a separate trusted kernel UID.

    Normal runtime processes cannot instantiate this with the private owner DB,
    reach the actor as the owner UID, or provide any externally confirmed result.
    The service never creates claims itself and never resends an uncertain RPC.
    """
    def __init__(self, *, store: SnapshotStore, socket_path: Path, authority_uid: int,
                 runtime_uid: int, principal: str, timeout_seconds: float = 90.0) -> None:
        _roles(os.geteuid(), authority_uid, runtime_uid)
        if (type(store) is not SnapshotStore or not isinstance(socket_path, Path)
                or not socket_path.is_absolute() or socket_path != Path(os.path.normpath(socket_path))
                or type(timeout_seconds) not in {int, float}
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300):
            raise _fail()
        self._store, self._socket_path = store, socket_path
        self._authority_uid, self._runtime_uid = authority_uid, runtime_uid
        self._principal = _text(principal, 128)
        self._timeout = float(timeout_seconds)
        self._guard = RLock()
        self._poisoned = False
        try:
            self._store_identity = _attested_database(store.path, os.geteuid())
        except (StorageOwnerError, OSError):
            raise _fail() from None

    def _attest(self) -> None:
        try:
            if _attested_database(self._store.path, os.geteuid()) != self._store_identity:
                raise _fail()
        except (StorageOwnerError, OSError):
            raise _fail() from None

    def _exchange(self, intent: CompletionIntent) -> object:
        request = _canonical(intent.to_wire())
        if len(request) > _MAX_FRAME:
            raise _fail()
        # One socket, one bounded request, one reply. In particular there is no
        # reconnect after any uncertain send/receive, even for the same key.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(self._timeout)
            peer.connect(str(self._socket_path))
            credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if _linux_ucred_uid(credentials) != self._authority_uid:
                raise _fail()
            peer.sendall(request + b"\n")
            with peer.makefile("rb") as stream:
                frame = stream.readline(_MAX_FRAME + 2)
            if not frame.endswith(b"\n") or len(frame) > _MAX_FRAME + 1 or len(frame) <= 1:
                raise _fail()
            return strict_json_object(frame[:-1])

    def _execute(self, action: str, target: str | None, payload: dict[str, object],
                 principal: str | None, reference: str | None):
        if (principal != self._principal or type(reference) is not str
                or not reference.startswith("write-api:")):
            raise _fail()
        key = reference[len("write-api:"):]
        with self._guard:
            self._attest()
            if self._poisoned:
                raise _fail()
            record = self._store.write_api_request(key)
            if (record is None or record.state != "in_progress" or record.execution_started_at is None
                    or record.response_status is not None or record.response_json is not None):
                raise _fail()
            intent = CompletionIntent.from_wire({
                "version": 1, "op": "execute", "key": key, "sha": record.request_sha256,
                "generation": record.claim_owner, "principal": principal,
                "action": action, "target": target, "payload": payload,
            }, expected_principal=self._principal)
            try:
                receipt = decode_evidence(self._exchange(intent), intent)
                self._attest()
                current = self._store.write_api_request(key)
                if (current != record or receipt.started_at < record.execution_started_at
                        or record.execution_started_at < record.requested_at):
                    raise _fail()
                if type(receipt) is CreateOperationReceipt:
                    self._store.append_create_operation_receipt(receipt)
                else:
                    self._store.append_operation_receipt(receipt)
                return receipt
            except BaseException as exc:
                self._poisoned = True
                if not isinstance(exc, Exception):
                    raise
                raise _fail() from None

    def create_with_media(self, request: AdCreateRequest, media_refs: tuple[str, ...],
                          *, authorization_by=None, authorization_reference=None) -> CreateOperationReceipt:
        if type(request) is not AdCreateRequest:
            raise _fail()
        refs = _media_refs(list(media_refs)) if type(media_refs) is tuple else _media_refs(None)
        payload = {"category_path": list(request.category_path), "title": request.title,
                   "description": request.description, "price_eur": request.price_eur,
                   "media_refs": list(refs)}
        return self._execute("create_media", None, payload, authorization_by,
                             authorization_reference)

    def pause(self, ad_id: str, *, authorization_by=None, authorization_reference=None) -> OperationReceipt:
        return self._execute("pause", ad_id, {}, authorization_by, authorization_reference)

    def activate(self, ad_id: str, *, authorization_by=None, authorization_reference=None) -> OperationReceipt:
        return self._execute("activate", ad_id, {}, authorization_by, authorization_reference)

    def delete(self, ad_id: str, *, approval: DeleteApproval) -> OperationReceipt:
        if type(approval) is not DeleteApproval or approval.ad_id != ad_id:
            raise _fail()
        return self._execute("delete", ad_id, {}, approval.approved_by, approval.reference)

    def update_content(self, ad_id: str, *, title=None, description=None,
                       authorization_by=None, authorization_reference=None) -> OperationReceipt:
        payload = {name: value for name, value in (("title", title), ("description", description))
                   if value is not None}
        return self._execute("update_content", ad_id, payload, authorization_by, authorization_reference)

    def create(self, request: AdCreateRequest, *, authorization_by=None,
               authorization_reference=None) -> CreateOperationReceipt:
        if type(request) is not AdCreateRequest:
            raise _fail()
        payload = {"category_path": list(request.category_path), "title": request.title,
                   "description": request.description, "price_eur": request.price_eur}
        return self._execute("create", None, payload, authorization_by, authorization_reference)



def _verify_nonmedia_actor(socket_path: Path, authority_uid: int) -> None:
    """Require the real independent actor even when media writes are disabled.

    The existing media_profile is an owner-only, side-effect-free probe. It
    grants no execute, completion, retry or recovery authority.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(3.0)
            peer.connect(str(socket_path))
            credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if _linux_ucred_uid(credentials) != authority_uid:
                raise _fail()
            peer.sendall(b'{"op":"media_profile","version":1}\n')
            with peer.makefile("rb") as reader:
                frame = reader.readline(_MAX_FRAME + 2)
        if not 1 < len(frame) <= _MAX_FRAME + 1 or not frame.endswith(b"\n"):
            raise _fail()
        answer = strict_json_object(frame[:-1])
        if (set(answer) != {"version", "ok", "media_ready"}
                or type(answer["version"]) is not int or answer["version"] != 1
                or answer["ok"] is not True or answer["media_ready"] is not False):
            raise _fail()
    except (OSError, ValueError, UnicodeError):
        raise _fail() from None


def create_owner_write_api_server(*, store: SnapshotStore, access: WriteApiAccess,
                                  authority_socket: Path, authority_uid: int,
                                  runtime_uid: int, fence_witness: FenceWitnessClient,
                                  port: int = 0):
    """Use the existing durable Write API, not a second idempotency journal.

    The real witness must be a fourth kernel UID. This constructor cannot be
    used with the ordinary StorageOwnerClient, a fake witness or a fake service.
    It does not bypass deployment cutover gates or attest browser isolation.
    """
    try:
        if type(fence_witness) is not FenceWitnessClient or fence_witness._socket is None:
            raise _fail()
        credentials = fence_witness._socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        witness_uid = _linux_ucred_uid(credentials)
        _roles(os.geteuid(), authority_uid, runtime_uid, witness_uid)
        if witness_uid != fence_witness._expected_owner_uid or type(access) is not WriteApiAccess:
            raise _fail()
        media_stager = None
        if WriteCapability.CREATE_MEDIA in access.capabilities:
            # Fail closed if the independent actor has not composed a private
            # persistent handle store AND a server-side media verifier.
            media_stager = OwnerMediaStager(
                socket_path=authority_socket, authority_uid=authority_uid,
                runtime_uid=runtime_uid,
            )
        else:
            _verify_nonmedia_actor(authority_socket, authority_uid)
        service = OwnerCompletionService(
            store=store, socket_path=authority_socket, authority_uid=authority_uid,
            runtime_uid=runtime_uid, principal=access.principal,
        )
        return create_write_api_server(service, store, access, port=port,
                                       media_service=service if media_stager is not None else None,
                                       media_stager=media_stager,
                                       execution_lock=RLock(), fence_witness=fence_witness)
    except (OSError, FenceWitnessError, StorageOwnerError):
        raise _fail() from None