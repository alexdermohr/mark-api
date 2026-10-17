"""Draft-only, independent SQLite ownership authority (Issue #76).

This is a deliberately *incomplete* storage-authority slice, NOT a product
runtime switch. The browser UID must have neither SQLite mounts nor inherited
writable descriptors before this can protect real Write flows. Only bounded
typed request-claim/start/read, read-only status/clearance lookups and
transactional, in-memory-staged owner-inventory/sync journaling are exposed.
Executor-supplied inventory is observation data, NOT independent platform
attestation, and cannot manufacture write-recovery evidence or clear fences.
There is NO arbitrary SQL, executor-selected completion/success, clearance,
file, migration, backup or general method-dispatch RPC. The sole started-write
completion primitive records a fixed owner-constructed UNKNOWN/HTTP202 and
retains its unresolved recovery fence.

A lost acknowledgement of claim/start is UNKNOWN: inspect the persisted key
and independent witness before deciding what happened, never blindly retry.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import socketserver
import sqlite3
import stat
import struct
import sys
from threading import BoundedSemaphore, Event, RLock, Thread
from typing import Any, Iterable
from urllib.parse import quote

from .container_draft import container_draft_denied
from .strict_json import strict_json_object
from .fence_witness import (
    _canonical_json, _file_identity, _private_directory,
    _strict_json, FenceWitnessError,
)
from .storage import (SnapshotStore, WriteApiRequestClaim, WriteApiRequestRecord,
                      DashboardPendingWrite, _SYNC_ERROR_KINDS)
from .domain import (AdSnapshot, LifecycleState, ReactionSnapshot,
                     InboundMessageEvent, AdClassification)
from .results import ReadResult, ReadStatus


class StorageOwnerError(RuntimeError):
    """Outcome/authority not proven. Never infer that a write was rejected."""


_VERSION = 1
_MAX_FRAME = 32 * 1024
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_OWNER = re.compile(r"[A-Za-z0-9._:@-]{1,128}\Z", re.ASCII)
_MAX_PEERS = 8
_MAX_SYNC_BATCH = 4
_MAX_SYNC_ITEMS = 2048
_MAX_INBOX_BATCH = 4
_MAX_INBOX_ITEMS = 2048
_TRACKED_PAGE = 64
_PENDING_AD_PATH = re.compile(
    r"/api/write/ads/([0-9]{1,32})(?:/(pause|activate))?\Z", re.ASCII,
)

_MAX_HISTORY_PAGE = 16
_MAX_HISTORY_RECORDS = 200000
_SYNC_DIGEST_SEED = hashlib.sha256(b"mark-storage-owner-sync-v1").hexdigest()
_INBOX_DIGEST_SEED = hashlib.sha256(b"mark-storage-owner-inbox-v1").hexdigest()
_UCRED = struct.Struct("=iII")  # Linux pid_t (signed), uid_t/gid_t (unsigned).


def _linux_ucred_uid(raw: bytes) -> int:
    """Decode a complete Linux SO_PEERCRED payload without signed-UID aliasing."""
    if type(raw) is not bytes or len(raw) != _UCRED.size:
        raise StorageOwnerError("malformed Linux peer credential")
    return _UCRED.unpack(raw)[1]


def _validated(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise StorageOwnerError("invalid operation-bound request value")
    return value


def _timestamp(value: object) -> datetime:
    if type(value) is not str or not 1 <= len(value) <= 80:
        raise StorageOwnerError("invalid request timestamp")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StorageOwnerError("invalid request timestamp") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise StorageOwnerError("request timestamp must be timezone-aware")
    return timestamp


def _reply_frame_fits(result: dict[str, object]) -> bool:
    """Use exactly the UTF-8 success-frame envelope sent by _StorageHandler."""
    return len(
        (_canonical_json({"ok": True, "result": result}) + "\n").encode("utf-8")
    ) <= _MAX_FRAME + 1


def _require_classification_reply_frame(classification: AdClassification) -> None:
    """Reject a would-be merged classification before its SQLite commit."""
    if not _reply_frame_fits({
        "classification": _class_as_json(classification),
    }):
        raise StorageOwnerError("merged classification exceeds IPC frame bound")


def _record_as_json(record: WriteApiRequestRecord) -> dict[str, object]:
    return {
        "idempotency_key": record.idempotency_key,
        "request_sha256": record.request_sha256,
        "state": record.state,
        "requested_at": record.requested_at.isoformat(),
        "claim_owner": record.claim_owner,
        "execution_started_at": (
            record.execution_started_at.isoformat()
            if record.execution_started_at is not None else None
        ),
        "completed_at": (
            record.completed_at.isoformat()
            if record.completed_at is not None else None
        ),
        "response_status": record.response_status,
        "response_json": record.response_json,
    }


def _record_from_json(value: object) -> WriteApiRequestRecord:
    if not isinstance(value, dict) or set(value) != {
        "idempotency_key", "request_sha256", "state", "requested_at",
        "claim_owner", "execution_started_at", "completed_at",
        "response_status", "response_json",
    }:
        raise StorageOwnerError("storage reply has invalid record schema")
    key = _validated(value["idempotency_key"], _KEY)
    sha = _validated(value["request_sha256"], _SHA)
    state = value["state"]
    if type(state) is not str or state not in {"in_progress", "completed"}:
        raise StorageOwnerError("storage reply has invalid record state")
    claimed = value["claim_owner"]
    if claimed is not None:
        claimed = _validated(claimed, _OWNER)
    started = value["execution_started_at"]
    completed = value["completed_at"]
    if started is not None:
        started = _timestamp(started)
    if completed is not None:
        completed = _timestamp(completed)
    status = value["response_status"]
    response_json = value["response_json"]
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise StorageOwnerError("storage reply has invalid HTTP status")
    if response_json is not None and type(response_json) is not str:
        raise StorageOwnerError("storage reply has invalid response")
    if (
        (state == "in_progress" and (
            completed is not None or status is not None or response_json is not None
        ))
        or (state == "completed" and (
            completed is None or status is None or response_json is None
        ))
    ):
        raise StorageOwnerError("storage reply has invalid recovery transition")
    return WriteApiRequestRecord(
        idempotency_key=key, request_sha256=sha, state=state,
        requested_at=_timestamp(value["requested_at"]),
        claim_owner=claimed, execution_started_at=started, completed_at=completed,
        response_status=status, response_json=response_json,
    )


def _dashboard_pending_contract(
    method: object, path: object,
) -> tuple[str, str, str | None, bool] | None:
    """Independent owner admission: only exact platform Write route identities.

    The browser executor cannot choose a recovery scope, resource, ad identity,
    or local-media route. No wildcard, percent-encoded or query-path aliases.
    """
    if type(method) is not str or type(path) is not str:
        return None
    if method == "POST" and path == "/api/write/ads":
        return ("create", "create", None, True)
    if method == "POST" and path == "/api/write/media/ads":
        return ("create-media", "create", None, True)
    match = _PENDING_AD_PATH.fullmatch(path)
    if match is None:
        return None
    ad_id, state_action = match.groups()
    if state_action is not None:
        if method != "POST":
            return None
        return (f"ad:{ad_id}:{state_action}", f"ad:{ad_id}", ad_id, False)
    if method == "PATCH":
        return (f"ad:{ad_id}:update", f"ad:{ad_id}", ad_id, True)
    if method == "DELETE":
        return (f"ad:{ad_id}:delete", f"ad:{ad_id}", ad_id, False)
    return None


def _validate_pending_wire_body(payload_json: str | None) -> None:
    if payload_json is None:
        return
    try:
        # The dashboard canonicalizes after strict wire decoding. Reject deep,
        # ambiguous or noncanonical serialized intents before persistence.
        data = strict_json_object(payload_json.encode("utf-8"))
        canonical = json.dumps(
            data, ensure_ascii=False, sort_keys=True,
            allow_nan=False, separators=(",", ":"),
        )
        canonical.encode("utf-8")  # No unpaired surrogate scalars.
    except (UnicodeError, ValueError, OverflowError, RecursionError) as exc:
        raise StorageOwnerError("invalid pending write JSON") from exc
    if canonical != payload_json:
        raise StorageOwnerError("pending write JSON is not canonical")



def _pending_as_json(record: DashboardPendingWrite) -> dict[str, object]:
    return {
        "scope": record.scope, "resource_key": record.resource_key,
        "idempotency_key": record.idempotency_key, "method": record.method,
        "path": record.path, "payload_json": record.payload_json,
        "ad_id": record.ad_id, "acknowledged": record.acknowledged,
    }


def _pending_from_json(value: object) -> DashboardPendingWrite:
    if type(value) is not dict or set(value) != {
        "scope", "resource_key", "idempotency_key", "method",
        "path", "payload_json", "ad_id", "acknowledged",
    }:
        raise StorageOwnerError("pending write reply schema invalid")
    scope = _limited_text(value["scope"], bound=128)
    resource = _limited_text(value["resource_key"], bound=128)
    key = _validated(value["idempotency_key"], _KEY)
    method = value["method"]
    path = _limited_text(value["path"], bound=512)
    payload = _limited_text(value["payload_json"], bound=64 * 1024,
                            optional=True, allow_linebreaks=True)
    ad_id = _limited_text(value["ad_id"], bound=32, optional=True)
    acknowledged = value["acknowledged"]
    if (
        any(ch.isspace() for text in (scope, resource, key, path)
            for ch in text)
        or len(key) > 128 or method not in ("POST", "PATCH", "DELETE")
        or not path.startswith("/api/write/") or "?" in path or "#" in path
        or type(acknowledged) is not bool
        or (ad_id is not None and (
            not ad_id.isascii() or not ad_id.isdigit()
        ))
    ):
        raise StorageOwnerError("pending write reply record invalid")
    if payload is not None:
        try:
            data = _strict_json(payload.encode("utf-8"))
        except (UnicodeError, FenceWitnessError, ValueError) as exc:
            raise StorageOwnerError("pending write reply JSON invalid") from exc
        if type(data) is not dict:
            raise StorageOwnerError("pending write payload must be object")
    return DashboardPendingWrite(
        scope=scope, resource_key=resource, idempotency_key=key,
        method=method, path=path, payload_json=payload,
        ad_id=ad_id, acknowledged=acknowledged,
    )


def _sync_status_from_json(value: object) -> dict[str, object]:
    """Validate the exact persisted journal projection, never invent freshness.

    The independent owner is trusted for storage integrity only after UID and
    inode attestation. Its replies must still be schema- and state-consistent.
    In particular, no successful sync may be inferred from absent attempts.
    """
    if type(value) is not dict or set(value) != {
        "state", "latest_attempt", "last_successful_attempt",
    }:
        raise StorageOwnerError("storage sync status reply schema invalid")

    def checked_attempt(row: object) -> dict[str, object] | None:
        if row is None:
            return None
        if type(row) is not dict or set(row) != {
            "id", "source", "started_at", "completed_at",
            "outcome", "error_kind", "snapshot_count",
        }:
            raise StorageOwnerError("storage sync attempt reply schema invalid")
        if (
            type(row["id"]) is not int or row["id"] < 1
            or type(row["source"]) is not str
            or not row["source"].strip() or len(row["source"]) > 1024
        ):
            raise StorageOwnerError("storage sync attempt identity invalid")
        started = _timestamp(row["started_at"])
        completed = row["completed_at"]
        if completed is not None:
            completed = _timestamp(completed)
            if completed < started:
                raise StorageOwnerError("storage sync completion predates start")
        outcome, error, count = (row["outcome"], row["error_kind"],
                                 row["snapshot_count"])
        if outcome == "in_progress":
            valid = completed is None and error is None and count is None
        elif outcome == "failed":
            valid = completed is not None and type(error) is str \
                and error in _SYNC_ERROR_KINDS and count is None
        elif outcome == "success_empty":
            valid = completed is not None and error is None \
                and type(count) is int and count == 0
        elif outcome == "success_nonempty":
            valid = completed is not None and error is None \
                and type(count) is int and count > 0
        else:
            valid = False
        if not valid:
            raise StorageOwnerError("storage sync attempt transition invalid")
        return row

    latest = checked_attempt(value["latest_attempt"])
    success = checked_attempt(value["last_successful_attempt"])
    if latest is None:
        if value["state"] != "never_attempted" or success is not None:
            raise StorageOwnerError("storage sync never-attempted state invalid")
    else:
        if value["state"] != latest["outcome"]:
            raise StorageOwnerError("storage sync latest state mismatch")
        if latest["outcome"] in {"success_empty", "success_nonempty"}:
            # Latest *started* is ordered by id, but last successful is
            # ordered by completion time. An older attempt can finish later.
            # Reject a missing success or a supposedly newer result that
            # could not win the store's (completed_at DESC, id DESC) order.
            if success is None or (
                success["id"] != latest["id"]
                and _timestamp(success["completed_at"])
                    <= _timestamp(latest["completed_at"])
            ):
                raise StorageOwnerError("storage latest success ordering invalid")
        if success is not None:
            if (
                success["outcome"] not in {"success_empty", "success_nonempty"}
                or success["id"] > latest["id"]
                or (success["id"] == latest["id"] and success != latest)
            ):
                raise StorageOwnerError("storage last-success binding invalid")
    return value



def _sync_recovery_from_json(value: object) -> dict[str, object]:
    """Reject forged unresolved counts and falsely terminal oldest records."""
    if type(value) is not dict or set(value) != {
        "unresolved_count", "oldest_unresolved_attempt",
    }:
        raise StorageOwnerError("storage recovery summary schema invalid")
    count = value["unresolved_count"]
    oldest = value["oldest_unresolved_attempt"]
    if (
        type(count) is not int or not 0 <= count <= 2**63 - 1
        or (count == 0) != (oldest is None)
    ):
        raise StorageOwnerError("storage unresolved sync count invalid")
    if oldest is not None:
        # Reuse exact SyncAttempt validation. This is read-only evidence,
        # never an executor power to terminate old journal entries.
        _sync_status_from_json({
            "state": "in_progress",
            "latest_attempt": oldest,
            "last_successful_attempt": None,
        })
    return value


def _limited_text(
    value: object, *, bound: int, optional: bool = False,
    allow_linebreaks: bool = False, allow_empty: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if (
        type(value) is not str or (not allow_empty and not value.strip())
        or len(value) > bound or any(
            ord(ch) == 0 or (not allow_linebreaks and ord(ch) in (10, 13))
            for ch in value
        )
        or any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)
    ):
        raise StorageOwnerError("invalid bounded inventory field")
    return value


def _snapshot_as_json(snapshot: AdSnapshot) -> dict[str, object]:
    """Canonical, fixed-schema wire representation; no SQL/path dispatch."""
    if not isinstance(snapshot, AdSnapshot):
        raise StorageOwnerError("inventory entry is not an AdSnapshot")
    return {
        "ad_id": snapshot.ad_id, "observed_at": snapshot.observed_at.isoformat(),
        "source": snapshot.source, "lifecycle_state": str(snapshot.lifecycle_state),
        "title": snapshot.title, "description": snapshot.description,
        "views": snapshot.views, "watch_count": snapshot.watch_count,
        "reply_count": snapshot.reply_count, "metric_source": snapshot.metric_source,
    }


def _snapshot_from_json(raw: object) -> AdSnapshot:
    required = {
        "ad_id", "observed_at", "source", "lifecycle_state",
        "title", "description", "views", "watch_count",
        "reply_count", "metric_source",
    }
    if type(raw) is not dict or set(raw) != required:
        raise StorageOwnerError("inventory entry has invalid schema")
    counters = {}
    for name in ("views", "watch_count", "reply_count"):
        value = raw[name]
        if value is not None and (type(value) is not int or value < 0 or value > 2**63 - 1):
            raise StorageOwnerError("inventory entry has invalid counter")
        counters[name] = value
    state = raw["lifecycle_state"]
    if type(state) is not str:
        raise StorageOwnerError("inventory entry has invalid lifecycle state")
    try:
        lifecycle = LifecycleState(state)
    except ValueError as exc:
        raise StorageOwnerError("inventory entry has invalid lifecycle state") from exc
    return AdSnapshot(
        ad_id=_limited_text(raw["ad_id"], bound=160),
        observed_at=_timestamp(raw["observed_at"]),
        source=_limited_text(raw["source"], bound=1024),
        lifecycle_state=lifecycle,
        title=_limited_text(raw["title"], bound=512, optional=True,
                            allow_empty=True),
        description=_limited_text(
            raw["description"], bound=8192, optional=True,
            allow_linebreaks=True, allow_empty=True,
        ),
        metric_source=_limited_text(raw["metric_source"], bound=160, optional=True),
        **counters,
    )


def _reaction_as_json(value: ReactionSnapshot) -> dict[str, object]:
    if not isinstance(value, ReactionSnapshot):
        raise StorageOwnerError("reaction is not a typed snapshot")
    return {
        "ad_id": value.ad_id, "observed_at": value.observed_at.isoformat(),
        "source": value.source, "conversation_count": value.conversation_count,
        "unique_buyer_count": value.unique_buyer_count,
        "inbound_message_count": value.inbound_message_count,
    }


def _reaction_from_json(raw: object) -> ReactionSnapshot:
    if type(raw) is not dict or set(raw) != {
        "ad_id", "observed_at", "source", "conversation_count",
        "unique_buyer_count", "inbound_message_count",
    }:
        raise StorageOwnerError("reaction reply schema invalid")
    metrics: dict[str, int] = {}
    for field_name in (
        "conversation_count", "unique_buyer_count", "inbound_message_count",
    ):
        value = raw[field_name]
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            raise StorageOwnerError("reaction reply counter invalid")
        metrics[field_name] = value
    return ReactionSnapshot(
        ad_id=_limited_text(raw["ad_id"], bound=160),
        observed_at=_timestamp(raw["observed_at"]),
        source=_limited_text(raw["source"], bound=1024),
        **metrics,
    )


def _inbound_as_json(value: InboundMessageEvent) -> dict[str, object]:
    if not isinstance(value, InboundMessageEvent):
        raise StorageOwnerError("invalid typed inbound message")
    return {
        "ad_id": value.ad_id, "conversation_id": value.conversation_id,
        "provider_message_id": value.provider_message_id,
        "observed_at": value.observed_at.isoformat(), "source": value.source,
    }


def _inbound_from_json(raw: object) -> InboundMessageEvent:
    if type(raw) is not dict or set(raw) != {
        "ad_id", "conversation_id", "provider_message_id", "observed_at", "source",
    }:
        raise StorageOwnerError("inbound message reply schema invalid")
    return InboundMessageEvent(
        ad_id=_limited_text(raw["ad_id"], bound=160),
        conversation_id=_limited_text(raw["conversation_id"], bound=1024),
        provider_message_id=_limited_text(raw["provider_message_id"], bound=2048),
        observed_at=_timestamp(raw["observed_at"]),
        source=_limited_text(raw["source"], bound=1024),
    )


def _class_as_json(value: AdClassification) -> dict[str, object]:
    if not isinstance(value, AdClassification):
        raise StorageOwnerError("invalid stored classification")
    return {
        "ad_id": value.ad_id, "observed_at": value.observed_at.isoformat(),
        "source": value.source, "image_type": value.image_type,
        "city": value.city, "text_type": value.text_type,
        "title_type": value.title_type,
    }


def _class_from_json(raw: object) -> AdClassification:
    fields = {
        "ad_id", "observed_at", "source", "image_type",
        "city", "text_type", "title_type",
    }
    if type(raw) is not dict or set(raw) != fields:
        raise StorageOwnerError("classification reply schema invalid")
    label_names = ("image_type", "city", "text_type", "title_type")
    labels = {
        name: _limited_text(raw[name], bound=1024, optional=True)
        for name in label_names
    }
    value = AdClassification(
        ad_id=_limited_text(raw["ad_id"], bound=160),
        observed_at=_timestamp(raw["observed_at"]),
        source=_limited_text(raw["source"], bound=1024),
        **labels,
    )
    if any(getattr(value, name) != raw[name] for name in label_names):
        raise StorageOwnerError("stored classification is not canonical")
    return value


def _sync_digest(previous: str, snapshots: list[dict[str, object]]) -> str:
    """Append-only chunk commitment, independent of JSON key ordering."""
    return hashlib.sha256(
        bytes.fromhex(previous) + _canonical_json(snapshots).encode("utf-8")
    ).hexdigest()


@dataclass(slots=True)
class _InboxStage:
    identity: str
    events: list[InboundMessageEvent] = field(default_factory=list)
    digest: str = _INBOX_DIGEST_SEED


@dataclass(slots=True)
class _SyncStage:
    attempt: int
    source: str
    snapshots: list[AdSnapshot] = field(default_factory=list)
    digest: str = _SYNC_DIGEST_SEED


def _attested_database(path: Path, uid: int) -> tuple[int, int, int, int]:
    """Existing, owner-private schema-v2 store; never create or repair files."""
    if (
        not isinstance(path, Path) or not path.is_absolute()
        or path != Path(os.path.normpath(path))
        or path.name in {"", ".", ".."}
    ):
        raise StorageOwnerError("database path is not absolute and normalized")
    try:
        _private_directory(path.parent, uid, mode=0o700)
        info = _file_identity(path, uid)
        # Existing SQLite sidecars can be created by its own DB writer; all
        # present sidecars must be equally private. No symlink or hardlink.
        for ending in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(path) + ending)
            try:
                sidecar.lstat()
            except FileNotFoundError:
                continue
            _file_identity(sidecar, uid)
        parent = path.parent.lstat()
    except (OSError, FenceWitnessError) as exc:
        raise StorageOwnerError("untrusted database inode or sidecar") from exc
    return info.st_dev, info.st_ino, parent.st_dev, parent.st_ino


class _StorageHandler(socketserver.StreamRequestHandler):
    """One bounded request per authenticated connection; no implicit retry."""

    def handle(self) -> None:
        self.request.settimeout(5.0)
        service: StorageOwnerServer = self.server  # type: ignore[assignment]
        try:
            if not hasattr(socket, "SO_PEERCRED"):
                raise StorageOwnerError("SO_PEERCRED unavailable")
            raw = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if _linux_ucred_uid(raw) != service.runtime_uid:
                raise StorageOwnerError("runtime UID not authorized")
            frame = self.rfile.readline(_MAX_FRAME + 2)
            if (
                not frame.endswith(b"\n") or len(frame) > _MAX_FRAME + 1
                or len(frame) <= 1
            ):
                raise StorageOwnerError("malformed storage request frame")
            request = _strict_json(frame[:-1])
            if type(request.get("version")) is not int or request["version"] != _VERSION:
                raise StorageOwnerError("storage protocol version mismatch")
            answer = service.dispatch(request)
            self._send({"ok": True, "result": answer})
        except (StorageOwnerError, FenceWitnessError, ValueError,
                sqlite3.Error, OSError, TimeoutError):
            # A late commit, connection failure or reply failure is UNKNOWN.
            # No response here is a promise of a pre-commit rejection.
            try:
                self._send({"ok": False, "error": "request_denied"})
            except OSError:
                pass

    def _send(self, value: dict[str, object]) -> None:
        data = (_canonical_json(value) + "\n").encode("utf-8")
        if len(data) > _MAX_FRAME + 1:
            raise StorageOwnerError("storage reply exceeds frame bound")
        self.wfile.write(data)
        self.wfile.flush()


class StorageOwnerServer(
    socketserver.ThreadingMixIn, socketserver.UnixStreamServer,
):
    """Private UID-owned SQLite; only an exact distinct runtime UID can RPC."""

    allow_reuse_address = False
    # An accepted RPC may already have entered SQLite when SIGTERM arrives.
    # Never unlink the public socket / report stop complete until all such
    # operations have finished; else an old generation can commit *after*
    # the operator believes its authority has been revoked.
    daemon_threads = False
    block_on_close = True
    request_queue_size = _MAX_PEERS

    def __init__(
        self, *, database: Path, socket_path: Path,
        runtime_uid: int, runtime_gid: int,
    ) -> None:
        uid = os.geteuid()
        if (
            uid == 0 or type(runtime_uid) is not int or runtime_uid <= 0
            or type(runtime_gid) is not int or runtime_gid <= 0
            or runtime_uid == uid or not hasattr(socket, "SO_PEERCRED")
            or not isinstance(socket_path, Path) or not socket_path.is_absolute()
            or socket_path != Path(os.path.normpath(socket_path))
            or socket_path == database
        ):
            raise StorageOwnerError("distinct unprivileged storage/runtime UIDs required")
        identity = _attested_database(database, uid)
        # Validate the full existing schema BEFORE SnapshotStore can add
        # historical columns. A missing recovery table is never auto-created.
        with closing(sqlite3.connect(
            "file:" + quote(str(database), safe="/") + "?mode=rw", uri=True, timeout=3,
        )) as connection:
            connection.row_factory = sqlite3.Row
            marker = connection.execute("PRAGMA user_version").fetchone()
            if marker is None or marker[0] != 2:
                raise StorageOwnerError("storage schema-v2 cutover not attested")
            SnapshotStore._validate_store_schema(connection)
        self.store = SnapshotStore(database, create_if_missing=False,
                                   require_existing_schema=True)
        if _attested_database(database, uid) != identity:
            raise StorageOwnerError("storage inode changed during startup")
        try:
            _private_directory(socket_path.parent, uid, mode=0o2710,
                               gid=runtime_gid)
        except FenceWitnessError as exc:
            raise StorageOwnerError("unsafe storage RPC directory") from exc
        if socket_path.exists() or socket_path.is_symlink():
            raise StorageOwnerError("refusing to replace storage RPC socket")
        self.database = database
        self.socket_path = socket_path
        self._identity = identity
        self.runtime_uid = runtime_uid
        self._runtime_gid = runtime_gid
        self._inode: tuple[int, int] | None = None
        self._slots = BoundedSemaphore(_MAX_PEERS)
        # Staged inventory is memory-only. A crash retains the committed
        # in-progress journal but NEVER publishes an incomplete success.
        self._sync_lock = RLock()
        self._active_sync: _SyncStage | None = None
        self._inbox_lock = RLock()
        self._inbox_stage: _InboxStage | None = None
        self._serving: Thread | None = None
        super().__init__(str(socket_path), _StorageHandler)
        try:
            os.chmod(socket_path, 0o660)
            file = socket_path.lstat()
            self._inode = file.st_dev, file.st_ino
            if (
                not stat.S_ISSOCK(file.st_mode) or file.st_uid != uid
                or file.st_gid != runtime_gid or file.st_nlink != 1
                or stat.S_IMODE(file.st_mode) != 0o660
            ):
                raise StorageOwnerError("storage RPC socket ownership invalid")
        except BaseException:
            self.server_close()
            raise

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def _attest(self) -> None:
        if _attested_database(self.database, os.geteuid()) != self._identity:
            raise StorageOwnerError("storage source was replaced")

    def dispatch(self, request: dict[str, object]) -> dict[str, object]:
        """Closed operation table: no arbitrary method names, SQL or clearance."""
        self._attest()
        op = request.get("op")
        if op == "ready" and set(request) == {"version", "op"}:
            return {"ready": self.store.is_ready()}
        if op == "sync_status" and set(request) == {"version", "op"}:
            return {"sync_status": self.store.sync_status()}
        if op == "sync_recovery_status" and set(request) == {"version", "op"}:
            return {"sync_recovery": self.store.sync_recovery_status()}

        if op == "inbound_begin" and set(request) == {
            "version", "op", "id",
        }:
            identity = _validated(request["id"], _KEY)
            with self._inbox_lock:
                if self._inbox_stage is not None:
                    raise StorageOwnerError("another inbox batch is unresolved")
                self._inbox_stage = _InboxStage(identity=identity)
                return {
                    "id": identity, "accepted": 0, "digest": _INBOX_DIGEST_SEED,
                }
        if op == "inbound_stage_status" and set(request) == {
            "version", "op", "id",
        }:
            identity = _validated(request["id"], _KEY)
            with self._inbox_lock:
                stage = self._inbox_stage
                if stage is None or stage.identity != identity:
                    raise StorageOwnerError("inbox stage not active")
                return {"accepted": len(stage.events), "digest": stage.digest}
        if op == "inbound_stage" and set(request) == {
            "version", "op", "id", "offset", "events",
        }:
            identity = _validated(request["id"], _KEY)
            offset, raw_events = request["offset"], request["events"]
            if (
                type(offset) is not int or offset < 0
                or type(raw_events) is not list
                or not 1 <= len(raw_events) <= _MAX_INBOX_BATCH
            ):
                raise StorageOwnerError("inbox batch cursor invalid")
            with self._inbox_lock:
                stage = self._inbox_stage
                if stage is None or stage.identity != identity:
                    raise StorageOwnerError("inbox stage not active")
                if offset != len(stage.events) or (
                    offset + len(raw_events) > _MAX_INBOX_ITEMS
                ):
                    raise StorageOwnerError("inbox batch offset/capacity mismatch")
                parsed = [_inbound_from_json(row) for row in raw_events]
                prior = {row.provider_message_id for row in stage.events}
                fresh = [row.provider_message_id for row in parsed]
                if len(set(fresh)) != len(fresh) or any(
                    item in prior for item in fresh
                ):
                    raise StorageOwnerError("duplicate provider evidence within batch")
                canonical = [_inbound_as_json(event) for event in parsed]
                stage.digest = _sync_digest(stage.digest, canonical)
                stage.events.extend(parsed)
                return {"accepted": len(stage.events), "digest": stage.digest}
        if op == "inbound_finish" and set(request) == {
            "version", "op", "id", "digest", "expected_count",
        }:
            identity = _validated(request["id"], _KEY)
            digest = _validated(request["digest"], _SHA)
            count = request["expected_count"]
            if type(count) is not int or not 0 <= count <= _MAX_INBOX_ITEMS:
                raise StorageOwnerError("inbox finish count invalid")
            with self._inbox_lock:
                stage = self._inbox_stage
                if stage is None or stage.identity != identity:
                    raise StorageOwnerError("inbox finish stage not active")
                if len(stage.events) != count or stage.digest != digest:
                    raise StorageOwnerError("inbox finish manifest mismatch")
                # All provider IDs commit in one SQLite BEGIN IMMEDIATE.
                # SIGKILL before finish loses only uncommitted staged RAM data.
                inserted = self.store.append_inbound_message_events(
                    tuple(stage.events)
                )
                self._inbox_stage = None
                return {
                    "id": identity, "received": count,
                    "inserted": inserted, "digest": digest,
                }
        if op == "inbound_message_page" and set(request) == {
            "version", "op", "ad_id", "after_id", "watermark",
        }:
            ad_id = _limited_text(request["ad_id"], bound=160)
            after_id, watermark = request["after_id"], request["watermark"]
            if (
                type(after_id) is not int or not 0 <= after_id <= 2**63 - 1
                or (watermark is not None and (
                    type(watermark) is not int
                    or not 0 <= watermark <= 2**63 - 1
                    or watermark < after_id
                ))
            ):
                raise StorageOwnerError("inbound history cursor invalid")
            pinned, entries, more = self.store.inbound_message_page(
                ad_id, after_id=after_id, watermark=watermark,
                limit=_MAX_HISTORY_PAGE,
            )
            rows: list[dict[str, object]] = []
            for rowid, event in entries:
                trial = [*rows, {"id": rowid, "event": _inbound_as_json(event)}]
                envelope = {
                    "ok": True, "result": {
                        "watermark": pinned, "more": False, "rows": trial,
                    },
                }
                if len(_canonical_json(envelope).encode("utf-8")) + 1 > _MAX_FRAME + 1:
                    break
                rows = trial
            if entries and not rows:
                raise StorageOwnerError("stored inbound event exceeds IPC frame")
            return {
                "watermark": pinned,
                "more": more or len(rows) < len(entries),
                "rows": rows,
            }
        if op == "inbound_ad_ids_page" and set(request) == {
            "version", "op", "after", "watermark",
        }:
            after = _limited_text(
                request["after"], bound=160, allow_empty=True,
            )
            watermark = request["watermark"]
            if watermark is not None and (
                type(watermark) is not int
                or not 0 <= watermark <= 2**63 - 1
            ):
                raise StorageOwnerError("inbound ad-ID watermark invalid")
            pinned, ids, more = self.store.inbound_message_ad_ids_page(
                after=after, watermark=watermark, limit=64,
            )
            selected: list[str] = []
            for item in ids:
                item = _limited_text(item, bound=160)
                trial = [*selected, item]
                envelope = {
                    "ok": True, "result": {
                        "watermark": pinned, "more": False, "ids": trial,
                    },
                }
                if len(_canonical_json(envelope).encode("utf-8")) + 1 > _MAX_FRAME + 1:
                    break
                selected = trial
            if ids and not selected:
                raise StorageOwnerError("stored email ad ID exceeds IPC frame")
            return {
                "watermark": pinned, "more": more or len(selected) < len(ids),
                "ids": selected,
            }
        if op == "inbound_counts" and set(request) == {
            "version", "op", "ad_id",
        }:
            ad_id = _limited_text(request["ad_id"], bound=160)
            conversations, messages = self.store.inbound_message_counts(ad_id)
            return {"conversations": conversations, "messages": messages}
        if op == "classification_merge" and set(request) == {
            "version", "op", "ad_id", "source", "at", "changes",
        }:
            ad_id = _limited_text(request["ad_id"], bound=160)
            source = _limited_text(request["source"], bound=1024)
            observed_at = _timestamp(request["at"])
            changes = request["changes"]
            allowed = {"image_type", "city", "text_type", "title_type"}
            if (
                type(changes) is not dict
                or not 1 <= len(changes) <= 4
                or not set(changes) <= allowed
            ):
                raise StorageOwnerError("invalid operation-bound classification fields")
            normalized = {
                field: (
                    _limited_text(value, bound=1024).strip()
                    if value is not None else None
                )
                for field, value in changes.items()
            }
            # BEGIN IMMEDIATE in SnapshotStore is the atomic merge boundary.
            # No executor-controlled clearance, completion, file or SQL.
            result = self.store.merge_classification(
                ad_id=ad_id, source=source,
                changes=normalized, observed_at=observed_at,
                validate_result=_require_classification_reply_frame,
            )
            return {"classification": _class_as_json(result)}
        if op == "classification_latest" and set(request) == {
            "version", "op", "ad_id",
        }:
            ad_id = _limited_text(request["ad_id"], bound=160)
            classification = self.store.latest_classification_bounded(
                ad_id, max_records=_MAX_HISTORY_RECORDS,
            )
            return {
                "classification": (
                    _class_as_json(classification)
                    if classification is not None else None
                ),
            }

        if op == "reaction_append" and set(request) == {
            "version", "op", "reaction",
        }:
            reaction = _reaction_from_json(request["reaction"])
            # Append-only observation. No claim, completion or clearance minted.
            self.store.append_reaction_snapshot(reaction)
            return {"reaction": _reaction_as_json(reaction)}
        if op == "reaction_history_page" and set(request) == {
            "version", "op", "ad_id", "after_id", "watermark",
        }:
            ad_id = _limited_text(request["ad_id"], bound=160)
            after_id, watermark = request["after_id"], request["watermark"]
            if (
                type(after_id) is not int or not 0 <= after_id <= 2**63 - 1
                or (watermark is not None and (
                    type(watermark) is not int
                    or not 0 <= watermark <= 2**63 - 1
                    or watermark < after_id
                ))
            ):
                raise StorageOwnerError("invalid reaction cursor")
            pinned, entries, more = self.store.reaction_history_page(
                ad_id, after_id=after_id, watermark=watermark,
                limit=_MAX_HISTORY_PAGE,
            )
            rows: list[dict[str, object]] = []
            for rowid, snapshot in entries:
                trial = [*rows, {"id": rowid, "reaction": _reaction_as_json(snapshot)}]
                envelope = {
                    "ok": True, "result": {
                        "watermark": pinned, "more": False, "rows": trial,
                    },
                }
                if len(_canonical_json(envelope).encode("utf-8")) + 1 > _MAX_FRAME + 1:
                    break
                rows = trial
            if entries and not rows:
                raise StorageOwnerError("stored reaction exceeds IPC frame")
            return {
                "watermark": pinned,
                "more": more or len(rows) < len(entries),
                "rows": rows,
            }

        if op == "ad_history_page" and set(request) == {
            "version", "op", "ad_id", "after_id", "watermark",
        }:
            ad_id = _limited_text(request["ad_id"], bound=160)
            after_id, watermark = request["after_id"], request["watermark"]
            if (
                type(after_id) is not int or not 0 <= after_id <= 2**63 - 1
                or (watermark is not None and (
                    type(watermark) is not int or not 0 <= watermark <= 2**63 - 1
                    or watermark < after_id
                ))
            ):
                raise StorageOwnerError("invalid history cursor")
            pinned, entries, more = self.store.ad_history_page(
                ad_id, after_id=after_id, watermark=watermark,
                limit=_MAX_HISTORY_PAGE,
            )
            # A single large historical description can occupy most of an
            # IPC frame. Pack complete rows only, never truncate user data.
            rows: list[dict[str, object]] = []
            for rowid, snapshot in entries:
                trial = [*rows, {"id": rowid, "snapshot": _snapshot_as_json(snapshot)}]
                envelope = {
                    "ok": True, "result": {
                        "watermark": pinned, "more": False, "rows": trial,
                    },
                }
                if len(_canonical_json(envelope).encode("utf-8")) + 1 > _MAX_FRAME + 1:
                    break
                rows = trial
            if entries and not rows:
                raise StorageOwnerError("stored snapshot exceeds IPC frame")
            return {
                "watermark": pinned,
                "more": more or len(rows) < len(entries),
                "rows": rows,
            }
        if op == "tracked_ids" and set(request) == {"version", "op", "after"}:
            after = request["after"]
            if after != "":
                after = _limited_text(after, bound=160)
            if type(after) is not str:
                raise StorageOwnerError("invalid tracked-ID cursor")
            # Strictly ordered and bounded reply. Never grant a query/SQL API.
            ids = [key for key in self.store.tracked_ad_ids() if key > after]
            batch = ids[:_TRACKED_PAGE]
            return {"ids": batch, "more": len(ids) > _TRACKED_PAGE}
        if op == "sync_begin" and set(request) == {"version", "op", "source", "at"}:
            source = _validated(request["source"], _OWNER)
            started_at = _timestamp(request["at"])
            with self._sync_lock:
                if self._active_sync is not None:
                    raise StorageOwnerError("another inventory sync is unresolved")
                try:
                    attempt = self.store.begin_sync_attempt(
                        source=source, started_at=started_at,
                        require_no_unresolved=True,
                    )
                except RuntimeError as exc:
                    # A previous generation may have lost its Begin, Stage or
                    # Finish ACK. It is not safe to infer an empty/failed read
                    # merely because the in-memory stage disappeared.
                    raise StorageOwnerError(
                        "durable inventory sync recovery is unresolved"
                    ) from exc
                self._active_sync = _SyncStage(attempt=attempt, source=source)
                return {"attempt": attempt}
        if op == "sync_stage_status" and set(request) == {
            "version", "op", "attempt",
        }:
            with self._sync_lock:
                stage = self._required_stage(request["attempt"])
                return {"accepted": len(stage.snapshots), "digest": stage.digest}
        if op == "sync_stage" and set(request) == {
            "version", "op", "attempt", "offset", "snapshots",
        }:
            raw_snapshots = request["snapshots"]
            offset = request["offset"]
            if (
                type(raw_snapshots) is not list
                or not 1 <= len(raw_snapshots) <= _MAX_SYNC_BATCH
                or type(offset) is not int or offset < 0
            ):
                raise StorageOwnerError("invalid inventory stage batch")
            with self._sync_lock:
                stage = self._required_stage(request["attempt"])
                if offset != len(stage.snapshots) or (
                    len(stage.snapshots) + len(raw_snapshots) > _MAX_SYNC_ITEMS
                ):
                    raise StorageOwnerError("inventory stage cursor or capacity mismatch")
                snapshots = [_snapshot_from_json(row) for row in raw_snapshots]
                # The attempt source attests only the sync journal identity;
                # each snapshot retains its original observation provenance.
                # Neither field grants write-completion or recovery authority.
                existing = {item.ad_id for item in stage.snapshots}
                added = [item.ad_id for item in snapshots]
                if len(set(added)) != len(added) or any(key in existing for key in added):
                    raise StorageOwnerError("inventory duplicate ad identity")
                serialized = [_snapshot_as_json(item) for item in snapshots]
                digest = _sync_digest(stage.digest, serialized)
                stage.snapshots.extend(snapshots)
                stage.digest = digest
                return {"accepted": len(stage.snapshots), "digest": stage.digest}
        if op == "sync_fail" and set(request) == {
            "version", "op", "attempt", "source", "at", "error_kind",
        }:
            source = _validated(request["source"], _OWNER)
            timestamp = _timestamp(request["at"])
            error = request["error_kind"]
            if type(error) is not str or error not in _SYNC_ERROR_KINDS:
                raise StorageOwnerError("invalid sync failure classification")
            with self._sync_lock:
                stage = self._required_stage(request["attempt"])
                if stage.source != source:
                    raise StorageOwnerError("sync failure source mismatch")
                self.store.fail_sync_attempt(
                    stage.attempt, source=source, completed_at=timestamp,
                    error_kind=error,
                )
                self._active_sync = None
                return {"failed": True, "attempt": stage.attempt}
        if op == "sync_finish" and set(request) == {
            "version", "op", "attempt", "source",
            "digest", "observed_at", "completed_at",
        }:
            source = _validated(request["source"], _OWNER)
            digest = _validated(request["digest"], _SHA)
            observed = _timestamp(request["observed_at"])
            completed = _timestamp(request["completed_at"])
            with self._sync_lock:
                stage = self._required_stage(request["attempt"])
                if stage.source != source or stage.digest != digest:
                    raise StorageOwnerError("sync completion manifest mismatch")
                snapshots = tuple(stage.snapshots)
                result = (ReadResult.success_nonempty(snapshots)
                          if snapshots else ReadResult.success_empty(()))
                # Existing SnapshotStore commits missing-AD observations and
                # the exact terminal sync journal in one SQLite transaction.
                persisted = self.store.append_inventory_result(
                    result, tracked_ad_ids=self.store.tracked_ad_ids(),
                    observed_at=observed, source=source,
                    attempt_id=stage.attempt, completed_at=completed,
                )
                self._active_sync = None
                return {"persisted": persisted, "attempt": stage.attempt,
                        "digest": digest}
        if op == "has_clearance" and set(request) == {
            "version", "op", "key", "sha",
        }:
            key = _validated(request["key"], _KEY)
            sha = _validated(request["sha"], _SHA)
            return {"cleared": self.store.has_write_recovery_clearance(
                idempotency_key=key, request_sha256=sha,
            )}
        if op == "read_request" and set(request) == {"version", "op", "key"}:
            key = _validated(request["key"], _KEY)
            rec = self.store.write_api_request(key)
            return {"record": _record_as_json(rec) if rec is not None else None}
        if op == "dashboard_pending_page" and set(request) == {
            "version", "op", "after",
        }:
            after = _limited_text(request["after"], bound=128, allow_empty=True)
            if any(ch.isspace() for ch in after):
                raise StorageOwnerError("invalid pending write cursor")
            records, more = self.store.dashboard_pending_writes_page(
                after=after, limit=16,
            )
            rows: list[dict[str, object]] = []
            for record in records:
                trial = [*rows, _pending_as_json(record)]
                if not _reply_frame_fits({"rows": trial, "more": False}):
                    break
                rows = trial
            if records and not rows:
                raise StorageOwnerError("stored pending record exceeds IPC frame")
            return {"rows": rows, "more": more or len(rows) < len(records)}
        if op == "dashboard_pending_claim" and set(request) == {
            "version", "op", "scope", "resource_key", "key",
            "method", "path", "payload_json", "ad_id",
        }:
            scope = _limited_text(request["scope"], bound=128)
            resource_key = _limited_text(request["resource_key"], bound=128)
            key = _validated(request["key"], _KEY)
            method = request["method"]
            path = _limited_text(request["path"], bound=512)
            payload = _limited_text(request["payload_json"], bound=64 * 1024,
                                    optional=True, allow_linebreaks=True)
            ad_id = _limited_text(request["ad_id"], bound=32, optional=True)
            expected = _dashboard_pending_contract(method, path)
            if (
                len(key) > 128
                or any(ch.isspace() for text in (scope, resource_key, key, path)
                       for ch in text)
                or expected is None
                or (scope, resource_key, ad_id, payload is not None) != expected
            ):
                raise StorageOwnerError("pending claim operation binding invalid")
            _validate_pending_wire_body(payload)
            created = self.store.claim_dashboard_pending_write(
                scope=scope, resource_key=resource_key, idempotency_key=key,
                method=method, path=path, payload_json=payload, ad_id=ad_id,
            )
            return {"created": created}
        if op == "create_pending" and set(request) == {
            "version", "op", "exclude_key", "owner",
        }:
            key = _validated(request["exclude_key"], _KEY)
            owner = _validated(request["owner"], _OWNER)
            return {"pending": self.store.create_recovery_pending(
                exclude_idempotency_key=key, claim_owner=owner,
            )}
        if op == "claim" and set(request) == {
            "version", "op", "key", "sha", "owner", "at",
        }:
            claim = self.store.claim_write_api_request(
                idempotency_key=_validated(request["key"], _KEY),
                request_sha256=_validated(request["sha"], _SHA),
                requested_at=_timestamp(request["at"]),
                claim_owner=_validated(request["owner"], _OWNER),
                allow_abandoned_takeover=False,
            )
            return {"created": claim.created, "record": _record_as_json(claim.record)}
        if op == "cancel_unstarted" and set(request) == {
            "version", "op", "key", "sha", "owner", "at",
        }:
            # No executor-selected outcome, response, receipt or clearance.
            # A begun operation (including lost Begin ACK) can never retire.
            record = self.store.cancel_unstarted_write_api_request(
                idempotency_key=_validated(request["key"], _KEY),
                request_sha256=_validated(request["sha"], _SHA),
                claim_owner=_validated(request["owner"], _OWNER),
                cancelled_at=_timestamp(request["at"]),
            )
            return {"record": _record_as_json(record)}
        if op == "quarantine_started" and set(request) == {
            "version", "op", "key", "sha", "owner", "at",
        }:
            # The executor never selects a result, success status, response,
            # clearance or retry authority. Only the original started claim
            # can receive the SQLite owner's fixed unverified observation.
            record = self.store.quarantine_started_write_api_request(
                idempotency_key=_validated(request["key"], _KEY),
                request_sha256=_validated(request["sha"], _SHA),
                claim_owner=_validated(request["owner"], _OWNER),
                quarantined_at=_timestamp(request["at"]),
            )
            return {"record": _record_as_json(record)}
        if op == "begin" and set(request) == {
            "version", "op", "key", "sha", "owner", "at",
        }:
            rec = self.store.begin_write_api_request(
                idempotency_key=_validated(request["key"], _KEY),
                request_sha256=_validated(request["sha"], _SHA),
                claim_owner=_validated(request["owner"], _OWNER),
                execution_started_at=_timestamp(request["at"]),
                strict_owner_fence=True,
            )
            return {"record": _record_as_json(rec)}
        raise StorageOwnerError("storage operation is not exposed")


    def _required_stage(self, attempt: object) -> _SyncStage:
        if type(attempt) is not int or attempt < 1:
            raise StorageOwnerError("invalid inventory sync attempt identity")
        stage = self._active_sync
        if stage is None or stage.attempt != attempt:
            raise StorageOwnerError("inventory sync not active in this generation")
        return stage

    def start(self) -> None:
        if self._serving is not None:
            raise StorageOwnerError("storage owner already started")
        self._serving = Thread(target=self.serve_forever, kwargs={
            "poll_interval": 0.1,
        }, daemon=True, name="mark-independent-storage-owner")
        self._serving.start()

    def stop(self) -> None:
        if self._serving is not None:
            if self._serving.is_alive():
                self.shutdown()
            self._serving.join(timeout=4)
        self.server_close()

    def server_close(self) -> None:
        super().server_close()
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == self._inode:
            self.socket_path.unlink()


class StorageOwnerClient:
    """Typed RPC client: transport ambiguity poisons this operation, no retry."""

    def __init__(self, socket_path: Path, *, expected_owner_uid: int) -> None:
        if (
            not isinstance(socket_path, Path) or not socket_path.is_absolute()
            or type(expected_owner_uid) is not int or expected_owner_uid <= 0
            or not hasattr(socket, "SO_PEERCRED")
        ):
            raise StorageOwnerError("independent storage UID/socket required")
        self.socket_path = socket_path
        self.expected_owner_uid = expected_owner_uid

    def _call(self, op: str, **fields: object) -> dict[str, object]:
        message = (_canonical_json({"version": _VERSION, "op": op, **fields}) +
                   "\n").encode("utf-8")
        if len(message) > _MAX_FRAME + 1:
            raise StorageOwnerError("storage request exceeds protocol bound")
        try:
            with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as peer:
                peer.settimeout(8)
                peer.connect(str(self.socket_path))
                credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                if (
                    _linux_ucred_uid(credentials) != self.expected_owner_uid
                ):
                    raise StorageOwnerError("independent storage peer UID is wrong")
                peer.sendall(message)
                with peer.makefile("rb", buffering=0) as stream:
                    frame = stream.readline(_MAX_FRAME + 2)
                if (
                    not frame.endswith(b"\n") or len(frame) > _MAX_FRAME + 1
                    or len(frame) <= 1
                ):
                    raise StorageOwnerError("storage reply missing or truncated")
                answer = _strict_json(frame[:-1])
                if set(answer) != {"ok", "result"} or answer["ok"] is not True:
                    raise StorageOwnerError("storage request not acknowledged")
                result = answer["result"]
                if not isinstance(result, dict):
                    raise StorageOwnerError("storage reply is not typed")
                return result
        except (StorageOwnerError, FenceWitnessError, OSError,
                TimeoutError, ValueError) as exc:
            raise StorageOwnerError(
                "storage outcome uncertain; read exact key and witness before retry"
            ) from exc

    def ready(self) -> bool:
        answer = self._call("ready")
        if set(answer) != {"ready"} or type(answer["ready"]) is not bool:
            raise StorageOwnerError("storage readiness reply invalid")
        return answer["ready"]

    def is_ready(self) -> bool:
        """Dashboard readiness over the existing typed owner RPC."""
        return self.ready()

    def sync_status(self) -> dict[str, object]:
        answer = self._call("sync_status")
        if set(answer) != {"sync_status"}:
            raise StorageOwnerError("storage sync status reply invalid")
        return _sync_status_from_json(answer["sync_status"])

    def sync_recovery_status(self) -> dict[str, object]:
        answer = self._call("sync_recovery_status")
        if set(answer) != {"sync_recovery"}:
            raise StorageOwnerError("storage recovery reply invalid")
        return _sync_recovery_from_json(answer["sync_recovery"])

    def append_inbound_message_events(
        self, events: Iterable[InboundMessageEvent],
    ) -> int:
        """Atomic bounded mailbox import without hidden transport retries.

        One ambiguous finish ACK is UNKNOWN. Inspect all exact provider IDs
        in the independently persisted history rather than blindly retrying.
        """
        unique: dict[str, InboundMessageEvent] = {}
        for seen_count, item in enumerate(events, start=1):
            if seen_count > _MAX_INBOX_ITEMS:
                raise StorageOwnerError("inbox input exceeds bounded work budget")
            canonical = _inbound_from_json(_inbound_as_json(item))
            previous = unique.get(canonical.provider_message_id)
            if previous is not None and previous != canonical:
                raise StorageOwnerError("conflicting input provider evidence")
            unique[canonical.provider_message_id] = canonical
            if len(unique) > _MAX_INBOX_ITEMS:
                raise StorageOwnerError("inbox import exceeds bounded capacity")
        if not unique:
            return 0
        identity = secrets.token_hex(16)
        payloads = [_inbound_as_json(row) for row in unique.values()]
        # Prevalidate that each legitimate event can fit a complete frame.
        # Otherwise we must not start a server-side stage we cannot complete.
        for item in payloads:
            frame = _canonical_json({
                "version": _VERSION, "op": "inbound_stage",
                "id": identity, "offset": _MAX_INBOX_ITEMS,
                "events": [item],
            }).encode("utf-8")
            if len(frame) + 1 > _MAX_FRAME + 1:
                raise StorageOwnerError("inbox event exceeds IPC frame")
        begin = self._call("inbound_begin", id=identity)
        if begin != {
            "id": identity, "accepted": 0, "digest": _INBOX_DIGEST_SEED,
        }:
            raise StorageOwnerError("inbox stage admission unbound")
        offset = 0
        digest = _INBOX_DIGEST_SEED
        while offset < len(payloads):
            batch: list[dict[str, object]] = []
            for item in payloads[offset:offset + _MAX_INBOX_BATCH]:
                trial = [*batch, item]
                frame = _canonical_json({
                    "version": _VERSION, "op": "inbound_stage",
                    "id": identity, "offset": offset, "events": trial,
                }).encode("utf-8")
                if len(frame) + 1 > _MAX_FRAME + 1:
                    break
                batch = trial
            if not batch:
                raise StorageOwnerError("inbox stage frame cannot advance")
            digest = _sync_digest(digest, batch)
            ack = self._call(
                "inbound_stage", id=identity, offset=offset, events=batch,
            )
            if ack != {
                "accepted": offset + len(batch), "digest": digest,
            }:
                raise StorageOwnerError("inbox stage digest ACK invalid")
            offset += len(batch)
        finish = self._call(
            "inbound_finish", id=identity, digest=digest,
            expected_count=len(payloads),
        )
        if (
            set(finish) != {"id", "received", "inserted", "digest"}
            or finish["id"] != identity or finish["digest"] != digest
            or type(finish["received"]) is not int
            or finish["received"] != len(payloads)
            or type(finish["inserted"]) is not int
            or not 0 <= finish["inserted"] <= len(payloads)
        ):
            raise StorageOwnerError("inbox commit receipt does not match manifest")
        return finish["inserted"]

    def inbound_message_history(self, ad_id: str) -> tuple[InboundMessageEvent, ...]:
        """One pinned inbox generation ordered by real time, insertion ID."""
        ad_id = _limited_text(ad_id, bound=160)
        found: list[tuple[int, InboundMessageEvent]] = []
        after_id = 0
        watermark: int | None = None
        while True:
            page = self._call(
                "inbound_message_page", ad_id=ad_id,
                after_id=after_id, watermark=watermark,
            )
            if (
                set(page) != {"watermark", "more", "rows"}
                or type(page["watermark"]) is not int
                or not 0 <= page["watermark"] <= 2**63 - 1
                or type(page["more"]) is not bool
                or type(page["rows"]) is not list
                or len(page["rows"]) > _MAX_HISTORY_PAGE
            ):
                raise StorageOwnerError("inbound history page envelope invalid")
            pinned = page["watermark"]
            if watermark is not None and pinned != watermark:
                raise StorageOwnerError("inbound history watermark drift")
            watermark = pinned
            rows = page["rows"]
            if page["more"] and not rows:
                raise StorageOwnerError("inbound history cursor did not advance")
            for row in rows:
                if type(row) is not dict or set(row) != {"id", "event"}:
                    raise StorageOwnerError("inbound event schema invalid")
                rowid = row["id"]
                if type(rowid) is not int or not after_id < rowid <= watermark:
                    raise StorageOwnerError("inbound history cursor invalid")
                event = _inbound_from_json(row["event"])
                if event.ad_id != ad_id:
                    raise StorageOwnerError("inbound history identity changed")
                found.append((rowid, event))
                after_id = rowid
                if len(found) > _MAX_HISTORY_RECORDS:
                    raise StorageOwnerError("inbound history query capacity exceeded")
            if not page["more"]:
                return tuple(
                    event for _, event in sorted(
                        found, key=lambda pair: (pair[1].observed_at, pair[0]),
                    )
                )

    def inbound_message_ad_ids(self) -> tuple[str, ...]:
        after = ""
        watermark: int | None = None
        all_ids: list[str] = []
        while True:
            page = self._call(
                "inbound_ad_ids_page", after=after, watermark=watermark,
            )
            if (
                set(page) != {"watermark", "more", "ids"}
                or type(page["watermark"]) is not int
                or not 0 <= page["watermark"] <= 2**63 - 1
                or type(page["more"]) is not bool
                or type(page["ids"]) is not list
                or len(page["ids"]) > 64
            ):
                raise StorageOwnerError("inbound ad-ID page envelope invalid")
            pinned = page["watermark"]
            if watermark is not None and pinned != watermark:
                raise StorageOwnerError("inbound ad-ID watermark drift")
            watermark = pinned
            ids = page["ids"]
            if page["more"] and not ids:
                raise StorageOwnerError("inbound ad-ID cursor did not advance")
            for value in ids:
                item = _limited_text(value, bound=160)
                if item <= after:
                    raise StorageOwnerError("inbound ad-ID ordering invalid")
                all_ids.append(item)
                after = item
                if len(all_ids) > _MAX_HISTORY_RECORDS:
                    raise StorageOwnerError("inbound ad-ID capacity exceeded")
            if not page["more"]:
                return tuple(all_ids)

    def inbound_message_counts(self, ad_id: str) -> tuple[int, int]:
        ad_id = _limited_text(ad_id, bound=160)
        value = self._call("inbound_counts", ad_id=ad_id)
        if set(value) != {"conversations", "messages"}:
            raise StorageOwnerError("inbound counts reply schema invalid")
        conversations, messages = value["conversations"], value["messages"]
        if (type(conversations) is not int or type(messages) is not int
                or conversations < 0 or messages < conversations
                or messages > 2**63 - 1):
            raise StorageOwnerError("inbound counts reply value invalid")
        return conversations, messages

    def merge_classification(
        self, *, ad_id: str, source: str,
        changes: dict[str, str | None], observed_at: datetime,
    ) -> AdClassification:
        """Atomic typed label merge; transport ambiguity never triggers retry."""
        ad_id = _limited_text(ad_id, bound=160)
        source = _limited_text(source, bound=1024)
        if type(observed_at) is not datetime:
            raise StorageOwnerError("classification timestamp invalid")
        observed_at = _timestamp(observed_at.isoformat())
        allowed = {"image_type", "city", "text_type", "title_type"}
        if (
            type(changes) is not dict
            or not 1 <= len(changes) <= 4
            or not set(changes) <= allowed
        ):
            raise StorageOwnerError("invalid classification changes")
        normalized = {
            key: (
                _limited_text(value, bound=1024).strip()
                if value is not None else None
            )
            for key, value in changes.items()
        }
        response = self._call(
            "classification_merge", ad_id=ad_id,
            source=source, at=observed_at.isoformat(), changes=normalized,
        )
        if set(response) != {"classification"}:
            raise StorageOwnerError("classification merge reply invalid")
        merged = _class_from_json(response["classification"])
        if (
            merged.ad_id != ad_id or merged.source != source
            or merged.observed_at != observed_at
            or any(getattr(merged, key) != val
                   for key, val in normalized.items())
        ):
            raise StorageOwnerError("classification merge readback unbound")
        return merged

    def latest_classification(self, ad_id: str) -> AdClassification | None:
        ad_id = _limited_text(ad_id, bound=160)
        value = self._call("classification_latest", ad_id=ad_id)
        if set(value) != {"classification"}:
            raise StorageOwnerError("classification reply schema invalid")
        if value["classification"] is None:
            return None
        result = _class_from_json(value["classification"])
        if result.ad_id != ad_id:
            raise StorageOwnerError("classification reply identity mismatch")
        return result

    def append_reaction_snapshot(self, snapshot: ReactionSnapshot) -> None:
        """Write exactly one typed observation, never replay lost acknowledgements."""
        item = _reaction_from_json(_reaction_as_json(snapshot))
        answer = self._call("reaction_append", reaction=_reaction_as_json(item))
        if set(answer) != {"reaction"} or _reaction_from_json(answer["reaction"]) != item:
            raise StorageOwnerError("reaction append receipt does not match original")
        return None

    def reaction_history(self, ad_id: str) -> tuple[ReactionSnapshot, ...]:
        """Read bounded, generation-pinned reaction history in insertion order."""
        ad_id = _limited_text(ad_id, bound=160)
        result: list[ReactionSnapshot] = []
        watermark: int | None = None
        after_id = 0
        while True:
            answer = self._call(
                "reaction_history_page", ad_id=ad_id, after_id=after_id,
                watermark=watermark,
            )
            if (
                set(answer) != {"watermark", "more", "rows"}
                or type(answer["watermark"]) is not int
                or not 0 <= answer["watermark"] <= 2**63 - 1
                or type(answer["more"]) is not bool
                or type(answer["rows"]) is not list
                or len(answer["rows"]) > _MAX_HISTORY_PAGE
            ):
                raise StorageOwnerError("reaction page envelope invalid")
            pinned = answer["watermark"]
            if watermark is not None and pinned != watermark:
                raise StorageOwnerError("reaction watermark changed")
            watermark = pinned
            rows = answer["rows"]
            if answer["more"] and not rows:
                raise StorageOwnerError("reaction paging failed to advance")
            for row in rows:
                if type(row) is not dict or set(row) != {"id", "reaction"}:
                    raise StorageOwnerError("reaction item schema invalid")
                rowid = row["id"]
                if type(rowid) is not int or not after_id < rowid <= watermark:
                    raise StorageOwnerError("reaction cursor repeated or out of range")
                item = _reaction_from_json(row["reaction"])
                if item.ad_id != ad_id:
                    raise StorageOwnerError("reaction identity mismatch")
                result.append(item)
                after_id = rowid
                if len(result) > _MAX_HISTORY_RECORDS:
                    raise StorageOwnerError("reaction history exceeds query capacity")
            if not answer["more"]:
                return tuple(result)

    def ad_history(self, ad_id: str) -> tuple[AdSnapshot, ...]:
        """Read one pinned, immutable observation history in bounded pages.

        Response ordering matches SnapshotStore.ad_history: actual timestamp
        and insertion ID. A later append is visible only to a *new* read.
        """
        ad_id = _limited_text(ad_id, bound=160)
        entries: list[tuple[int, AdSnapshot]] = []
        watermark: int | None = None
        after_id = 0
        while True:
            answer = self._call(
                "ad_history_page", ad_id=ad_id, after_id=after_id,
                watermark=watermark,
            )
            if (
                set(answer) != {"watermark", "more", "rows"}
                or type(answer["watermark"]) is not int
                or not 0 <= answer["watermark"] <= 2**63 - 1
                or type(answer["more"]) is not bool
                or type(answer["rows"]) is not list
                or len(answer["rows"]) > _MAX_HISTORY_PAGE
            ):
                raise StorageOwnerError("invalid history page envelope")
            pinned = answer["watermark"]
            if watermark is not None and pinned != watermark:
                raise StorageOwnerError("history watermark changed during read")
            watermark = pinned
            rows = answer["rows"]
            if answer["more"] and not rows:
                raise StorageOwnerError("history paging failed to advance")
            for row in rows:
                if type(row) is not dict or set(row) != {"id", "snapshot"}:
                    raise StorageOwnerError("invalid history item schema")
                rowid = row["id"]
                if type(rowid) is not int or not after_id < rowid <= watermark:
                    raise StorageOwnerError("history cursor repeated or out of range")
                item = _snapshot_from_json(row["snapshot"])
                if item.ad_id != ad_id:
                    raise StorageOwnerError("history ad identity mismatch")
                entries.append((rowid, item))
                after_id = rowid
                if len(entries) > _MAX_HISTORY_RECORDS:
                    raise StorageOwnerError("history exceeds bounded query capacity")
            if not answer["more"]:
                ordered = sorted(
                    entries, key=lambda pair: (pair[1].observed_at, pair[0]),
                )
                return tuple(snapshot for _, snapshot in ordered)

    def latest_ad_snapshot(self, ad_id: str) -> AdSnapshot | None:
        history = self.ad_history(ad_id)
        return history[-1] if history else None

    def tracked_ad_ids(self) -> tuple[str, ...]:
        """Read bounded ascending pages, never a raw SQLite table or cursor."""
        keys: list[str] = []
        after = ""
        while True:
            response = self._call("tracked_ids", after=after)
            if (
                set(response) != {"ids", "more"}
                or type(response["ids"]) is not list
                or type(response["more"]) is not bool
                or len(response["ids"]) > _TRACKED_PAGE
            ):
                raise StorageOwnerError("invalid tracked-ID page")
            page = response["ids"]
            for value in page:
                item = _limited_text(value, bound=160)
                if item <= after:
                    raise StorageOwnerError("tracked-ID page is not strictly sorted")
                keys.append(item)
                after = item
            if not response["more"]:
                return tuple(keys)
            if not page or len(page) != _TRACKED_PAGE:
                raise StorageOwnerError("tracked-ID page falsely claims more")

    def begin_sync_attempt(
        self, *, source: str, started_at: datetime,
        require_no_unresolved: bool = True,
    ) -> int:
        # The isolated owner *always* applies its BEGIN IMMEDIATE durable
        # unresolved-sync check. Do not accept a facade opt-out that would
        # imply a weaker contract or broaden the executor's authority.
        if require_no_unresolved is not True:
            raise StorageOwnerError("durable inventory sync recovery fence is required")
        answer = self._call("sync_begin", source=_validated(source, _OWNER),
                            at=started_at.isoformat())
        if set(answer) != {"attempt"} or type(answer["attempt"]) is not int or answer["attempt"] < 1:
            raise StorageOwnerError("invalid sync begin receipt")
        return answer["attempt"]

    def sync_stage_status(self, attempt_id: int) -> dict[str, object]:
        """Observation-only readback after an uncertain append ACK."""
        answer = self._call("sync_stage_status", attempt=attempt_id)
        if (
            set(answer) != {"accepted", "digest"}
            or type(answer["accepted"]) is not int
            or not 0 <= answer["accepted"] <= _MAX_SYNC_ITEMS
        ):
            raise StorageOwnerError("invalid inventory stage status")
        _validated(answer["digest"], _SHA)
        return answer

    def fail_sync_attempt(
        self, attempt_id: int, *, source: str, completed_at: datetime,
        error_kind: str,
    ) -> None:
        if error_kind not in _SYNC_ERROR_KINDS:
            raise StorageOwnerError("invalid sync failure status")
        answer = self._call(
            "sync_fail", attempt=attempt_id, source=_validated(source, _OWNER),
            at=completed_at.isoformat(), error_kind=error_kind,
        )
        if (
            set(answer) != {"failed", "attempt"}
            or answer["failed"] is not True
            or type(answer["attempt"]) is not int
            or answer["attempt"] != attempt_id
        ):
            raise StorageOwnerError("invalid sync failure receipt")

    def append_inventory_result(
        self, result: ReadResult[tuple[AdSnapshot, ...]], *,
        tracked_ad_ids: Any = (), observed_at: datetime, source: str,
        attempt_id: int | None = None, completed_at: datetime | None = None,
    ) -> int:
        """Commit an exact owner-inventory attempt atomically, never on lost ACK.

        The independent owner computes the already-tracked IDs from SQLite:
        caller-provided tracked IDs cannot omit historical source records.
        A lost stage/finish reply is UNKNOWN, never an automatic RPC retry.
        """
        if not isinstance(result, ReadResult):
            raise StorageOwnerError("invalid owner inventory read result")
        if type(attempt_id) is not int or attempt_id < 1 or completed_at is None:
            raise StorageOwnerError("explicit durable sync attempt is required")
        source = _validated(source, _OWNER)
        if not result.is_success:
            self.fail_sync_attempt(
                attempt_id, source=source, completed_at=completed_at,
                error_kind=result.status.value,
            )
            return 0
        snapshots = tuple(result.value or ())
        if (
            (result.status is ReadStatus.SUCCESS_EMPTY and snapshots)
            or (result.status is ReadStatus.SUCCESS_NONEMPTY and not snapshots)
            or any(not isinstance(item, AdSnapshot) for item in snapshots)
            or len(snapshots) > _MAX_SYNC_ITEMS
        ):
            raise StorageOwnerError("invalid inventory read result")
        digest = _SYNC_DIGEST_SEED
        offset = 0
        while offset < len(snapshots):
            batch: list[dict[str, object]] = []
            for item in snapshots[offset:offset + _MAX_SYNC_BATCH]:
                candidate = [*batch, _snapshot_as_json(item)]
                frame = _canonical_json({
                    "version": _VERSION, "op": "sync_stage",
                    "attempt": attempt_id, "offset": offset,
                    "snapshots": candidate,
                }).encode("utf-8")
                if len(frame) + 1 > _MAX_FRAME + 1:
                    break
                batch = candidate
            if not batch:
                raise StorageOwnerError("inventory item exceeds protocol frame limit")
            digest = _sync_digest(digest, batch)
            answer = self._call(
                "sync_stage", attempt=attempt_id, offset=offset, snapshots=batch,
            )
            if (
                set(answer) != {"accepted", "digest"}
                or type(answer["accepted"]) is not int
                or answer["accepted"] != offset + len(batch)
                or answer["digest"] != digest
            ):
                raise StorageOwnerError("inventory chunk ACK does not match manifest")
            offset += len(batch)
        answer = self._call(
            "sync_finish", attempt=attempt_id, source=source, digest=digest,
            observed_at=observed_at.isoformat(), completed_at=completed_at.isoformat(),
        )
        if (
            set(answer) != {"persisted", "attempt", "digest"}
            or type(answer["attempt"]) is not int
            or answer["attempt"] != attempt_id
            or answer["digest"] != digest
            or type(answer["persisted"]) is not int
            or answer["persisted"] < len(snapshots)
        ):
            raise StorageOwnerError("inventory commit receipt is unverified")
        return answer["persisted"]

    def has_write_recovery_clearance(
        self, *, idempotency_key: str, request_sha256: str,
    ) -> bool:
        """Read one exact persisted clearance; never issue or imply one."""
        answer = self._call(
            "has_clearance", key=_validated(idempotency_key, _KEY),
            sha=_validated(request_sha256, _SHA),
        )
        if set(answer) != {"cleared"} or type(answer["cleared"]) is not bool:
            raise StorageOwnerError("storage clearance lookup reply invalid")
        return answer["cleared"]

    def read_request(self, key: str) -> WriteApiRequestRecord | None:
        answer = self._call("read_request", key=_validated(key, _KEY))
        if set(answer) != {"record"}:
            raise StorageOwnerError("storage request lookup reply invalid")
        if answer["record"] is None:
            return None
        record = _record_from_json(answer["record"])
        if record.idempotency_key != key:
            raise StorageOwnerError("storage readback key binding mismatch")
        return record

    def dashboard_pending_writes(self) -> tuple[DashboardPendingWrite, ...]:
        """Read advisory pending UI intents; never acknowledge or clear fences."""
        rows: list[DashboardPendingWrite] = []
        after = ""
        while True:
            answer = self._call("dashboard_pending_page", after=after)
            if (
                set(answer) != {"rows", "more"} or type(answer["rows"]) is not list
                or type(answer["more"]) is not bool or len(answer["rows"]) > 16
            ):
                raise StorageOwnerError("pending write page reply invalid")
            page = [_pending_from_json(item) for item in answer["rows"]]
            for item in page:
                if item.scope <= after:
                    raise StorageOwnerError("pending write page cursor invalid")
                after = item.scope
                rows.append(item)
            if len(rows) > 4096:
                raise StorageOwnerError("pending write history exceeds IPC budget")
            if not answer["more"]:
                return tuple(rows)
            if not page:
                raise StorageOwnerError("pending write page made no progress")

    def claim_dashboard_pending_write(
        self, *, scope: str, resource_key: str, idempotency_key: str,
        method: str, path: str, payload_json: str | None, ad_id: str | None,
    ) -> bool:
        """Create one durable UI pending fence; no successful write attestation."""
        answer = self._call(
            "dashboard_pending_claim", scope=scope, resource_key=resource_key,
            key=idempotency_key, method=method, path=path,
            payload_json=payload_json, ad_id=ad_id,
        )
        if set(answer) != {"created"} or type(answer["created"]) is not bool:
            raise StorageOwnerError("pending write claim reply invalid")
        return answer["created"]

    def cancel_unstarted(
        self, *, key: str, sha: str, owner: str, at: datetime,
    ) -> WriteApiRequestRecord:
        """Record a fixed pre-Begin failure, never a platform success/retry."""
        key = _validated(key, _KEY)
        sha = _validated(sha, _SHA)
        owner = _validated(owner, _OWNER)
        if type(at) is not datetime:
            raise StorageOwnerError("cancellation timestamp invalid")
        at = _timestamp(at.isoformat())
        answer = self._call(
            "cancel_unstarted", key=key, sha=sha, owner=owner,
            at=at.isoformat(),
        )
        if set(answer) != {"record"}:
            raise StorageOwnerError("unstarted cancellation reply invalid")
        record = _record_from_json(answer["record"])
        expected = _canonical_json({
            "error": "write_claim_cancelled_before_begin",
            "idempotency_key": key, "platform_retry_authorized": False,
        })
        if (
            (record.idempotency_key, record.request_sha256, record.claim_owner)
            != (key, sha, owner)
            or record.state != "completed"
            or record.execution_started_at is not None
            or record.completed_at is None
            or record.response_status != 409
            or record.response_json != expected
        ):
            raise StorageOwnerError("unstarted cancellation receipt not bound")
        return record

    def create_pending(self, *, exclude_key: str, owner: str) -> bool:
        answer = self._call("create_pending", exclude_key=_validated(exclude_key, _KEY),
                            owner=_validated(owner, _OWNER))
        if set(answer) != {"pending"} or type(answer["pending"]) is not bool:
            raise StorageOwnerError("storage pending reply invalid")
        return answer["pending"]

    def claim(self, *, key: str, sha: str, owner: str,
              at: datetime) -> WriteApiRequestClaim:
        answer = self._call("claim", key=_validated(key, _KEY),
                            sha=_validated(sha, _SHA),
                            owner=_validated(owner, _OWNER),
                            at=at.isoformat())
        if set(answer) != {"created", "record"} or type(answer["created"]) is not bool:
            raise StorageOwnerError("storage claim reply invalid")
        record = _record_from_json(answer["record"])
        if (
            (record.idempotency_key, record.request_sha256) != (key, sha)
            or record.claim_owner != owner
        ):
            raise StorageOwnerError("storage claim operation binding mismatch")
        return WriteApiRequestClaim(created=answer["created"], record=record)

    def begin(self, *, key: str, sha: str, owner: str,
              at: datetime) -> WriteApiRequestRecord:
        answer = self._call("begin", key=_validated(key, _KEY),
                            sha=_validated(sha, _SHA),
                            owner=_validated(owner, _OWNER), at=at.isoformat())
        if set(answer) != {"record"}:
            raise StorageOwnerError("storage start reply invalid")
        record = _record_from_json(answer["record"])
        if (
            (record.idempotency_key, record.request_sha256) != (key, sha)
            or record.claim_owner != owner
        ):
            raise StorageOwnerError("storage start operation binding mismatch")
        if record.execution_started_at is None:
            raise StorageOwnerError("storage start not persisted")
        return record



    def quarantine_started(
        self, *, key: str, sha: str, owner: str, at: datetime,
    ) -> WriteApiRequestRecord:
        """Owner-fixed HTTP202 UNKNOWN, not independent platform settlement."""
        key = _validated(key, _KEY)
        sha = _validated(sha, _SHA)
        owner = _validated(owner, _OWNER)
        if type(at) is not datetime:
            raise StorageOwnerError("quarantine timestamp invalid")
        at = _timestamp(at.isoformat())
        answer = self._call(
            "quarantine_started", key=key, sha=sha, owner=owner,
            at=at.isoformat(),
        )
        if set(answer) != {"record"}:
            raise StorageOwnerError("write quarantine reply invalid")
        record = _record_from_json(answer["record"])
        expected = _canonical_json({
            "error": "write_outcome_unverified",
            "idempotency_key": key,
            "platform_retry_authorized": False,
        })
        if (
            (record.idempotency_key, record.request_sha256, record.claim_owner)
            != (key, sha, owner)
            or record.state != "completed"
            or record.execution_started_at is None
            or record.completed_at is None
            or record.response_status != 202
            or record.response_json != expected
        ):
            raise StorageOwnerError("write quarantine is not operation-bound")
        return record


def main(argv: list[str] | None = None) -> int:
    if container_draft_denied():
        print("mark-api: unattested container draft cannot run Python entrypoints",
              file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Draft independent SQLite owner")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--runtime-uid", type=int, required=True)
    parser.add_argument("--runtime-gid", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        service = StorageOwnerServer(
            database=args.db, socket_path=args.socket,
            runtime_uid=args.runtime_uid, runtime_gid=args.runtime_gid,
        )
    except (StorageOwnerError, sqlite3.Error, OSError, ValueError):
        print("mark storage owner: unavailable existing private store or IPC",
              file=sys.stderr)
        return 2
    stopping = Event()
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda _sig, _frame: stopping.set())
    try:
        service.start()
        while not stopping.wait(0.2):
            if service._serving is None or not service._serving.is_alive():
                return 2
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
