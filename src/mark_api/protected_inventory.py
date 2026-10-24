"""UID-bound actor inventory transfer and journaled independent-owner storage.

This is read-only platform observation. It NEVER proves Write completion,
operator clearance, host cutover or restore authorization. The actor holds one
immutable snapshot through bounded pages; the owner alone owns SQLite and
its durable sync journal. Unknown acknowledgements are never blindly retried.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from time import monotonic
import re
import secrets
import socket
from threading import Lock

from .domain import AdSnapshot, LifecycleState
from .results import ReadResult, ReadStatus
from .storage import SnapshotStore
from .storage_owner import (
    StorageOwnerError, _linux_ucred_uid, _snapshot_as_json,
    _snapshot_from_json,
)
from .strict_json import strict_json_object

_PAGE_SIZE = 4
_MAX_ITEMS = 2048
_MAX_FRAME = 256 * 1024
# Only platform READ observations expire. This never releases durable Write
# fences, media handles or recovery/backup authority.
_SNAPSHOT_TTL_SECONDS = 55.0
_READ_CYCLE_SECONDS = 45.0
_MAX_RPC_SECONDS = 8.0
_SOURCE = "a6-protected-actor-inventory"
_TOKEN = re.compile(r"[0-9a-f]{48}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class ProtectedInventoryError(RuntimeError):
    """One-shot outcome; a new READ cycle is allowed only after durable failure."""

    def __init__(self, message: str, *, retryable_read: bool = False) -> None:
        super().__init__(message)
        self.retryable_read = retryable_read


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProtectedInventoryError("actor inventory frame invalid") from None


def _timestamp(value: object) -> datetime:
    if type(value) is not str or not 1 <= len(value) <= 80:
        raise ProtectedInventoryError("actor inventory time invalid")
    try:
        stamp = datetime.fromisoformat(value)
        if (stamp.tzinfo is None or stamp.utcoffset() is None
                or stamp.isoformat() != value):
            raise ValueError()
        return stamp
    except (ValueError, OverflowError, TypeError):
        raise ProtectedInventoryError("actor inventory time invalid") from None


class ActorInventoryPages:
    """Immutable, one-shot browser observation; actor keeps no SQLite handle."""

    def __init__(self, owner_reader: object, execution_lock: Lock) -> None:
        self._reader = owner_reader
        self._lock = execution_lock  # same lock as every actor Write operation
        self._pending: tuple[str, tuple[dict[str, object], ...], str, float] | None = None
        self._next = 0

    def _expire_observation(self) -> None:
        if self._pending is not None and monotonic() >= self._pending[3]:
            # Snapshot/page state is *only* a read cache: expiration cannot
            # revoke a durable idempotency or recovery fence.
            self._pending = None
            self._next = 0

    def begin(self) -> dict[str, object]:
        if not self._lock.acquire(timeout=_MAX_RPC_SECONDS):
            raise ProtectedInventoryError("actor busy")
        try:
            self._expire_observation()
            if self._pending is not None:
                # A live read-generation is immutable; only explicit finish
                # or bounded TTL permits a NEW read-only generation.
                raise ProtectedInventoryError("old inventory read unresolved")
            at = datetime.now(timezone.utc).isoformat()
            result = self._reader.read_ads()
            if (type(result) is not ReadResult or
                    result.status not in (ReadStatus.SUCCESS_EMPTY, ReadStatus.SUCCESS_NONEMPTY)):
                raise ProtectedInventoryError("actor inventory read unavailable")
            rows = result.value
            if (type(rows) is not tuple
                    or len(rows) > _MAX_ITEMS
                    or (result.status is ReadStatus.SUCCESS_EMPTY and rows)
                    or (result.status is ReadStatus.SUCCESS_NONEMPTY and not rows)):
                raise ProtectedInventoryError("actor inventory shape invalid")
            wire: list[dict[str, object]] = []
            seen: set[str] = set()
            for row in rows:
                if type(row) is not AdSnapshot or row.ad_id in seen:
                    raise ProtectedInventoryError("actor inventory identity invalid")
                checked = _snapshot_as_json(row)
                if _snapshot_from_json(checked) != row:
                    raise ProtectedInventoryError("actor inventory roundtrip invalid")
                if len(_canonical(checked)) > _MAX_FRAME // _PAGE_SIZE:
                    raise ProtectedInventoryError("actor inventory entry oversized")
                seen.add(row.ad_id)
                wire.append(checked)
            encoded = tuple(wire)
            digest = hashlib.sha256(_canonical(encoded)).hexdigest()
            token = secrets.token_hex(24)
            self._pending = (token, encoded, digest, monotonic() + _SNAPSHOT_TTL_SECONDS)
            self._next = 0
            return {
                "version": 1, "ok": True, "token": token, "total": len(encoded),
                "digest": digest, "observed_at": at,
            }
        except ProtectedInventoryError:
            raise
        except Exception:
            raise ProtectedInventoryError("actor inventory unavailable") from None
        finally:
            self._lock.release()

    def page(self, token: object, offset: object) -> dict[str, object]:
        with self._lock:
            self._expire_observation()
            if (self._pending is None or type(token) is not str
                    or _TOKEN.fullmatch(token) is None
                    or token != self._pending[0]
                    or type(offset) is not int or offset != self._next):
                raise ProtectedInventoryError("actor inventory cursor invalid")
            rows = self._pending[1]
            if offset >= len(rows):
                raise ProtectedInventoryError("actor inventory exhausted")
            end = min(offset + _PAGE_SIZE, len(rows))
            frame: dict[str, object] = {
                "version": 1, "ok": True, "offset": offset,
                "next_offset": end, "rows": list(rows[offset:end]),
                "more": end < len(rows),
            }
            if len(_canonical(frame)) > _MAX_FRAME:
                raise ProtectedInventoryError("actor inventory page oversized")
            # Advance before writing response. A lost ACK never permits
            # reusing the same cursor or minting a second observation.
            self._next = end
            return frame

    def finish(self, token: object, digest: object) -> dict[str, object]:
        with self._lock:
            self._expire_observation()
            if (self._pending is None or type(token) is not str
                    or token != self._pending[0]
                    or type(digest) is not str
                    or digest != self._pending[2]
                    or self._next != len(self._pending[1])):
                raise ProtectedInventoryError("actor inventory finish not verified")
            self._pending = None
            self._next = 0
            return {"version": 1, "ok": True, "released": True}


class OwnerActorInventoryClient:
    """Private A6 owner UID reads one exact actor UID without any retry."""

    def __init__(
        self, *, actor_socket: Path, actor_uid: int, timeout_seconds: float = _MAX_RPC_SECONDS,
    ) -> None:
        if (not isinstance(actor_socket, Path) or not actor_socket.is_absolute()
                or actor_socket != Path(os.path.normpath(actor_socket))
                or type(actor_uid) is not int or actor_uid <= 0
                or actor_uid == os.geteuid()
                or type(timeout_seconds) not in (int, float)
                or not math.isfinite(float(timeout_seconds))
                or not 0 < timeout_seconds <= _MAX_RPC_SECONDS
                or not hasattr(socket, "SO_PEERCRED")):
            raise ProtectedInventoryError("independent actor required")
        self.actor_socket = actor_socket
        self.actor_uid = actor_uid
        self.timeout = float(timeout_seconds)
        self.owner_uid = os.geteuid()
        self.owner_pid = os.getpid()
        self._deadline: float | None = None

    def _call(self, request: dict[str, object]) -> dict[str, object]:
        if (os.geteuid(), os.getpid()) != (self.owner_uid, self.owner_pid):
            raise ProtectedInventoryError("inventory client identity changed")
        try:
            remaining = (
                self._deadline - monotonic()
                if self._deadline is not None else self.timeout
            )
            if remaining <= 0:
                raise ProtectedInventoryError("actor inventory total deadline exceeded")
            data = _canonical(request)
            if len(data) > _MAX_FRAME:
                raise ValueError()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(min(self.timeout, remaining))
                peer.connect(str(self.actor_socket))
                if _linux_ucred_uid(
                    peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                ) != self.actor_uid:
                    raise ValueError()
                peer.sendall(data + b"\n")
                with peer.makefile("rb") as reader:
                    line = reader.readline(_MAX_FRAME + 2)
            if (not 1 < len(line) <= _MAX_FRAME + 1
                    or not line.endswith(b"\n")):
                raise ValueError()
            response = strict_json_object(line[:-1])
            if (type(response.get("version")) is not int
                    or response["version"] != 1 or response.get("ok") is not True):
                raise ValueError()
            return response
        except (OSError, ValueError, TypeError, StorageOwnerError, UnicodeError):
            raise ProtectedInventoryError(
                "actor inventory outcome unknown; do not retry"
            ) from None

    def read(self) -> tuple[ReadResult[tuple[AdSnapshot, ...]], datetime]:
        if self._deadline is not None:
            raise ProtectedInventoryError("inventory read already in progress")
        self._deadline = monotonic() + _READ_CYCLE_SECONDS
        try:
            return self._read_once()
        finally:
            self._deadline = None

    def _read_once(self) -> tuple[ReadResult[tuple[AdSnapshot, ...]], datetime]:
        start = self._call({"version": 1, "op": "inventory_begin"})
        if (set(start) != {"version", "ok", "token", "total", "digest", "observed_at"}
                or type(start["token"]) is not str
                or _TOKEN.fullmatch(start["token"]) is None
                or type(start["total"]) is not int
                or not 0 <= start["total"] <= _MAX_ITEMS
                or type(start["digest"]) is not str
                or _SHA.fullmatch(start["digest"]) is None):
            raise ProtectedInventoryError("actor inventory start unbound")
        at = _timestamp(start["observed_at"])
        rows: list[AdSnapshot] = []
        raw: list[dict[str, object]] = []
        while len(rows) < start["total"]:
            offset = len(rows)
            reply = self._call({
                "version": 1, "op": "inventory_page",
                "token": start["token"], "offset": offset,
            })
            entries = reply.get("rows")
            next_offset = reply.get("next_offset")
            if (set(reply) != {"version", "ok", "offset", "next_offset", "rows", "more"}
                    or type(reply["offset"]) is not int or reply["offset"] != offset
                    or type(next_offset) is not int
                    or not offset < next_offset <= min(offset + _PAGE_SIZE, start["total"])
                    or type(entries) is not list or len(entries) != next_offset - offset
                    or type(reply["more"]) is not bool
                    or reply["more"] != (next_offset < start["total"])):
                raise ProtectedInventoryError("actor inventory page unbound")
            try:
                for item in entries:
                    snapshot = _snapshot_from_json(item)
                    if any(existing.ad_id == snapshot.ad_id for existing in rows):
                        raise ValueError("duplicate actor ad id")
                    rows.append(snapshot)
                    raw.append(_snapshot_as_json(snapshot))
            except (StorageOwnerError, ValueError, TypeError, OverflowError):
                raise ProtectedInventoryError("actor inventory page invalid") from None
        if hashlib.sha256(_canonical(raw)).hexdigest() != start["digest"]:
            raise ProtectedInventoryError("actor inventory digest mismatch")
        end = self._call({
            "version": 1, "op": "inventory_finish",
            "token": start["token"], "digest": start["digest"],
        })
        if set(end) != {"version", "ok", "released"} or end["released"] is not True:
            raise ProtectedInventoryError("actor inventory close unverified")
        snapshots = tuple(rows)
        return (
            ReadResult.success_nonempty(snapshots)
            if snapshots else ReadResult.success_empty(()),
            at,
        )


def sync_actor_inventory_once(
    store: SnapshotStore, client: OwnerActorInventoryClient,
) -> int:
    """Exactly one journaled read; a crash/unknown outcome never retries."""
    if type(store) is not SnapshotStore or type(client) is not OwnerActorInventoryClient:
        raise TypeError("independent owner and actor required")
    try:
        tracked = tuple(
            ad_id for ad_id in store.tracked_ad_ids()
            if (snapshot := store.latest_ad_snapshot(ad_id)) is not None
            and snapshot.lifecycle_state is not LifecycleState.ABSENT
        )
        started_at = datetime.now(timezone.utc)
        attempt = store.begin_sync_attempt(
            source=_SOURCE, started_at=started_at, require_no_unresolved=True,
        )
    except Exception:
        raise ProtectedInventoryError("owner sync journal unavailable") from None
    try:
        observed, at = client.read()
    except Exception:
        try:
            store.fail_sync_attempt(
                attempt, source=_SOURCE, completed_at=datetime.now(timezone.utc),
                error_kind="runtime_unavailable",
            )
        except Exception:
            # Even a failed error ACK can have an unknown SQLite commit.
            raise ProtectedInventoryError("owner sync outcome unknown") from None
        raise ProtectedInventoryError("actor inventory read failed", retryable_read=True) from None
    try:
        return store.append_inventory_result(
            observed, tracked_ad_ids=tracked, observed_at=at,
            source=_SOURCE, attempt_id=attempt,
            completed_at=datetime.now(timezone.utc),
        )
    except Exception:
        try:
            store.fail_sync_attempt(
                attempt, source=_SOURCE, completed_at=datetime.now(timezone.utc),
                error_kind="persistence_error",
            )
        except Exception:
            raise ProtectedInventoryError("owner sync outcome unknown") from None
        raise ProtectedInventoryError("owner inventory persist not verified") from None