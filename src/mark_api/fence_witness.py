"""Experimental independent, session-bound recovery-fence witness.

This is a separately owned, append-only authority for *remembering* accepted
write attempts, not authorization to mutate Kleinanzeigen or a replacement for
SnapshotStore. The regular launch path is NOT wired to this module yet.

The ledger and any rollback journal must reside under a distinct OS UID from Mark's
browser/runtime UID. A working unit test under one UID is not that OS proof.
"""
from __future__ import annotations

import argparse
import fcntl
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
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
import time
from threading import Event, RLock, Thread
from typing import Any
from urllib.parse import quote


class FenceWitnessError(RuntimeError):
    """Refuse potentially unsafe recovery progress without leaking records."""


_VERSION = 1
_MAX_FRAME = 32 * 1024
_KEY = re.compile(r"[A-Za-z0-9._:-]{1,160}\Z", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ACTIONS = frozenset({
    "create", "create_media", "update_content", "pause", "activate", "delete",
})
_SAFE_OUTCOMES = frozenset({"confirmed", "precondition_failed"})
_EVENT_KINDS = frozenset({
    "opened", "started", "completed", "runtime_cleared",
    "operator_cleared", "sealed",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_string(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise FenceWitnessError(f"invalid {label}")
    return value


def _evidence(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value.strip()) <= 256
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        raise FenceWitnessError("invalid independent evidence reference")
    return value.strip()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object member")
        result[key] = value
    return result


def _strict_json(data: bytes) -> dict[str, object]:
    if len(data) > _MAX_FRAME or not data:
        raise FenceWitnessError("invalid request frame")
    try:
        obj = json.loads(
            data.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise FenceWitnessError("invalid request frame") from exc
    if not isinstance(obj, dict):
        raise FenceWitnessError("invalid request frame")
    return obj


def _private_directory(path: Path, uid: int, *, mode: int, gid: int | None = None) -> None:
    if not path.is_absolute() or path != Path(os.path.normpath(path)):
        raise FenceWitnessError("private directory path is not absolute and normalized")
    try:
        info = path.lstat()
    except OSError as exc:
        raise FenceWitnessError("required private directory is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) != mode
        or (gid is not None and info.st_gid != gid)
    ):
        raise FenceWitnessError("private directory ownership or mode is unsafe")
    # Do not follow a caller-selected lexical symlink in any parent component.
    for ancestor in path.parents:
        try:
            parent = ancestor.lstat()
        except OSError as exc:
            raise FenceWitnessError("private directory parent unavailable") from exc
        if not stat.S_ISDIR(parent.st_mode):
            raise FenceWitnessError("private directory has a symlinked parent")
        permissions = stat.S_IMODE(parent.st_mode)
        if parent.st_uid not in (0, uid):
            raise FenceWitnessError("private directory has an untrusted owner")
        if permissions & 0o022 and not (
            parent.st_uid == 0
            and permissions & stat.S_ISVTX
            and permissions & 0o002
        ):
            raise FenceWitnessError("private directory has a writable parent")


def _file_identity(path: Path, uid: int) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise FenceWitnessError("ledger database is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise FenceWitnessError("ledger database identity or permissions are unsafe")
    return info


def _derived_outcome(action: str, response: object) -> str:
    """Fail closed when an untrusted runtime's claimed result is incomplete."""
    if not isinstance(response, dict):
        return "ambiguous"
    receipt = response.get("operation_receipt")
    if not isinstance(receipt, dict):
        return "ambiguous"
    outcome = receipt.get("outcome")
    invoked = receipt.get("writer_invoked")
    if type(invoked) is not bool or not isinstance(outcome, str):
        return "ambiguous"
    if outcome == "precondition_failed" and not invoked:
        return "precondition_failed"
    if outcome == "confirmed" and invoked:
        if action == "create_media" and response.get("media_persistence_confirmed") is not True:
            return "ambiguous"
        return "confirmed"
    return "ambiguous"


class FenceLedger:
    """Private SQLite v1 journal; only its distinct owner UID may open files."""

    def __init__(self, path: Path, *, initialize: bool = False) -> None:
        if not isinstance(path, Path) or not path.is_absolute():
            raise TypeError("ledger path must be an absolute Path")
        if not isinstance(initialize, bool):
            raise TypeError("initialize must be a boolean")
        self.path = path
        self._uid = os.geteuid()
        self._guard = RLock()
        _private_directory(path.parent, self._uid, mode=0o700)
        created = False
        if initialize:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                             os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
                created = True
        checked = _file_identity(path, self._uid)
        self._db_identity = (checked.st_dev, checked.st_ino)
        parent_info = path.parent.lstat()
        self._parent_identity = (parent_info.st_dev, parent_info.st_ino)
        self._lock_path = path.with_name(path.name + ".lock")
        self._lock_fd: int | None = self._acquire_lock()
        try:
            if created:
                with closing(self._connect()) as conn:
                    conn.executescript("""
                        BEGIN IMMEDIATE;
                        CREATE TABLE sessions (
                            id TEXT PRIMARY KEY,
                            state TEXT NOT NULL CHECK(state IN ('active','sealed')),
                            opened_at TEXT NOT NULL,
                            sealed_at TEXT
                        );
                        CREATE TABLE events (
                            seq INTEGER PRIMARY KEY,
                            session_id TEXT NOT NULL,
                            kind TEXT NOT NULL,
                            request_key TEXT,
                            request_sha256 TEXT,
                            action TEXT,
                            outcome TEXT,
                            response_sha256 TEXT,
                            evidence_reference TEXT,
                            created_at TEXT NOT NULL,
                            previous_sha256 TEXT NOT NULL,
                            row_sha256 TEXT NOT NULL
                        );
                        CREATE UNIQUE INDEX one_start_per_key ON events(request_key)
                            WHERE kind = 'started';
                        CREATE UNIQUE INDEX one_completion_per_key ON events(request_key)
                            WHERE kind = 'completed';
                        CREATE UNIQUE INDEX one_clearance_per_key ON events(request_key)
                            WHERE kind IN ('runtime_cleared', 'operator_cleared');
                        CREATE TRIGGER events_no_update BEFORE UPDATE ON events
                            BEGIN SELECT RAISE(ABORT, 'append-only events'); END;
                        CREATE TRIGGER events_no_delete BEFORE DELETE ON events
                            BEGIN SELECT RAISE(ABORT, 'append-only events'); END;
                        PRAGMA user_version = 1;
                        COMMIT;
                    """)
                # Persist first-time directory entry, not only the SQLite pages.
                parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            with closing(self._connect()) as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                objects = {
                    (row[0], row[1])
                    for row in conn.execute(
                        "SELECT type,name FROM sqlite_master WHERE type IN ('table','index','trigger')"
                    )
                }
                necessary = {
                    ("table", "sessions"), ("table", "events"),
                    ("index", "one_start_per_key"), ("index", "one_completion_per_key"),
                    ("index", "one_clearance_per_key"),
                    ("trigger", "events_no_update"), ("trigger", "events_no_delete"),
                }
                if version != _VERSION or not necessary.issubset(objects):
                    raise FenceWitnessError("ledger recovery schema is damaged")
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise FenceWitnessError("ledger integrity is invalid")
                self._verify_chain(conn)
                # After an owner crash, no old socket session may gain new writes.
                conn.execute("BEGIN IMMEDIATE")
                self._seal_orphaned_sessions(conn)
                conn.commit()

        except BaseException:
            self.close()
            raise

    def _acquire_lock(self) -> int:
        """Hold a separate OS flock before any recovery-session reconciliation."""
        try:
            descriptor = os.open(
                self._lock_path,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
        except OSError as exc:
            raise FenceWitnessError("exclusive witness ledger lock unavailable") from exc
        try:
            entry = self._lock_path.lstat()
            actual = os.fstat(descriptor)
            if (
                not stat.S_ISREG(actual.st_mode)
                or actual.st_uid != self._uid
                or stat.S_IMODE(actual.st_mode) != 0o600
                or actual.st_nlink != 1
                or (entry.st_dev, entry.st_ino) != (actual.st_dev, actual.st_ino)
            ):
                raise FenceWitnessError("witness ledger lock inode is unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                raise FenceWitnessError(
                    "another witness owner already controls this ledger"
                ) from exc
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def close(self) -> None:
        with self._guard:
            descriptor = getattr(self, "_lock_fd", None)
            self._lock_fd = None
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def __del__(self) -> None:
        try:
            self.close()
        except (AttributeError, OSError):
            pass

    def __enter__(self) -> "FenceLedger":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _connect(self) -> sqlite3.Connection:
        if self._lock_fd is None:
            raise FenceWitnessError("witness owner lease is closed")
        _private_directory(self.path.parent, self._uid, mode=0o700)
        current_lock = os.fstat(self._lock_fd)
        published_lock = self._lock_path.lstat()
        if (current_lock.st_dev, current_lock.st_ino) != (
            published_lock.st_dev, published_lock.st_ino
        ):
            raise FenceWitnessError("witness owner lock inode changed")
        parent = self.path.parent.lstat()
        file_info = _file_identity(self.path, self._uid)
        if (
            (parent.st_dev, parent.st_ino) != self._parent_identity
            or (file_info.st_dev, file_info.st_ino) != self._db_identity
        ):
            raise FenceWitnessError("witness ledger inode changed")
        uri = "file:" + quote(str(self.path), safe="/") + "?mode=rw"
        connection = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
        try:
            # In DELETE journal mode the unlink of the rollback journal is
            # the commit point. EXTRA syncs its directory entry as well:
            # an ACKed execution-start must survive a power loss.
            connection.execute("PRAGMA synchronous=EXTRA")
            if connection.execute("PRAGMA synchronous").fetchone()[0] != 3:
                raise FenceWitnessError("durable ledger sync mode is unavailable")
            journal = connection.execute("PRAGMA journal_mode").fetchone()
            if journal is None or journal[0].lower() != "delete":
                raise FenceWitnessError("unsupported ledger journal mode")
            connection.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    def _append(conn: sqlite3.Connection, *, session: str, kind: str,
                key: str = "", sha: str = "", action: str = "",
                outcome: str = "", response_sha: str = "",
                evidence: str = "") -> None:
        if kind not in _EVENT_KINDS:
            raise FenceWitnessError("unknown journal event")
        previous = conn.execute(
            "SELECT row_sha256 FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prior_hash = previous[0] if previous else "0" * 64
        timestamp = _now()
        values = (session, kind, key, sha, action, outcome, response_sha,
                  evidence, timestamp, prior_hash)
        digest = hashlib.sha256(_canonical_json(values).encode("ascii")).hexdigest()
        conn.execute(
            "INSERT INTO events (session_id,kind,request_key,request_sha256,"
            "action,outcome,response_sha256,evidence_reference,created_at,"
            "previous_sha256,row_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (*values, digest),
        )

    @staticmethod
    def _verify_chain(conn: sqlite3.Connection) -> None:
        previous = "0" * 64
        rows = conn.execute(
            "SELECT session_id,kind,request_key,request_sha256,action,outcome,"
            "response_sha256,evidence_reference,created_at,previous_sha256,"
            "row_sha256 FROM events ORDER BY seq"
        )
        for row in rows:
            data = tuple(value if value is not None else "" for value in row[:10])
            if data[-1] != previous:
                raise FenceWitnessError("ledger event chain is invalid")
            current = hashlib.sha256(_canonical_json(data).encode("ascii")).hexdigest()
            if current != row[10]:
                raise FenceWitnessError("ledger event chain is invalid")
            previous = current

    @staticmethod
    def _seal_orphaned_sessions(conn: sqlite3.Connection) -> None:
        sessions = conn.execute(
            "SELECT id FROM sessions WHERE state='active' ORDER BY id"
        ).fetchall()
        for (sid,) in sessions:
            FenceLedger._append(conn, session=sid, kind="sealed")
            conn.execute(
                "UPDATE sessions SET state='sealed',sealed_at=? WHERE id=?",
                (_now(), sid),
            )

    @staticmethod
    def _require_active(conn: sqlite3.Connection, session: str) -> None:
        row = conn.execute(
            "SELECT state FROM sessions WHERE id=?", (session,)
        ).fetchone()
        if row is None or row[0] != "active":
            raise FenceWitnessError("runtime session is not active")

    def open_session(self) -> str:
        with self._guard, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute(
                    "SELECT 1 FROM sessions WHERE state='active' LIMIT 1"
                ).fetchone():
                    raise FenceWitnessError("one live runtime session already exists")
                session = secrets.token_hex(16)
                conn.execute(
                    "INSERT INTO sessions (id,state,opened_at) VALUES (?,'active',?)",
                    (session, _now()),
                )
                self._append(conn, session=session, kind="opened")
                conn.commit()
                return session
            except BaseException:
                conn.rollback()
                raise

    def seal_session(self, session: str) -> None:
        with self._guard, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT state FROM sessions WHERE id=?", (session,)
                ).fetchone()
                if row is not None and row[0] == "active":
                    self._append(conn, session=session, kind="sealed")
                    conn.execute(
                        "UPDATE sessions SET state='sealed',sealed_at=? WHERE id=?",
                        (_now(), session),
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _is_pending(conn: sqlite3.Connection) -> bool:
        return conn.execute("""
            SELECT 1 FROM events AS started
            WHERE started.kind='started'
              AND NOT EXISTS(
                 SELECT 1 FROM events AS cleared
                 WHERE cleared.request_key=started.request_key
                   AND cleared.kind IN ('runtime_cleared','operator_cleared')
              )
              AND NOT EXISTS(
                 SELECT 1 FROM events AS completed
                 WHERE completed.request_key=started.request_key
                   AND completed.kind='completed'
                   AND completed.outcome IN ('confirmed','precondition_failed')
              )
            LIMIT 1
        """).fetchone() is not None

    def fence_pending(self) -> bool:
        with self._guard, closing(self._connect()) as conn:
            return self._is_pending(conn)

    def execution_started(
        self, session: str, *, key: str, sha: str, action: str,
    ) -> None:
        key = _required_string(key, _KEY, "idempotency key")
        sha = _required_string(sha, _SHA, "request SHA-256")
        if not isinstance(action, str) or action not in _ACTIONS:
            raise FenceWitnessError("write action is not supported")
        with self._guard, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._require_active(conn, session)
                if self._is_pending(conn):
                    raise FenceWitnessError("earlier write requires recovery")
                if conn.execute(
                    "SELECT 1 FROM events WHERE kind='started' AND request_key=?",
                    (key,),
                ).fetchone():
                    raise FenceWitnessError("operation already executed or recorded")
                self._append(conn, session=session, kind="started",
                             key=key, sha=sha, action=action)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def execution_completed(
        self, session: str, *, key: str, sha: str, response: object,
    ) -> str:
        key = _required_string(key, _KEY, "idempotency key")
        sha = _required_string(sha, _SHA, "request SHA-256")
        with self._guard, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._require_active(conn, session)
                start = conn.execute(
                    "SELECT action,session_id,request_sha256 FROM events "
                    "WHERE kind='started' AND request_key=?",
                    (key,),
                ).fetchone()
                if start is None or start[1] != session or start[2] != sha:
                    raise FenceWitnessError("write binding or session mismatch")
                if conn.execute(
                    "SELECT 1 FROM events WHERE kind='completed' AND request_key=?",
                    (key,),
                ).fetchone():
                    raise FenceWitnessError("response has already been recorded")
                serialized = _canonical_json(response)
                if len(serialized.encode("utf-8")) > _MAX_FRAME:
                    raise FenceWitnessError("response is too large")
                outcome = _derived_outcome(start[0], response)
                self._append(
                    conn, session=session, kind="completed", key=key, sha=sha,
                    outcome=outcome,
                    response_sha=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                )
                conn.commit()
                return outcome
            except BaseException:
                conn.rollback()
                raise

    def runtime_clearance(
        self, session: str, *, key: str, sha: str, evidence: str,
    ) -> None:
        self._clear(key=key, sha=sha, evidence=evidence, session=session,
                    operator=False)

    def operator_clearance(self, *, key: str, sha: str, evidence: str) -> None:
        self._clear(key=key, sha=sha, evidence=evidence, session="", operator=True)

    def _clear(self, *, key: str, sha: str, evidence: str,
               session: str, operator: bool) -> None:
        key = _required_string(key, _KEY, "idempotency key")
        sha = _required_string(sha, _SHA, "request SHA-256")
        evidence = _evidence(evidence)
        with self._guard, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if not operator:
                    self._require_active(conn, session)
                start = conn.execute(
                    "SELECT session_id,request_sha256 FROM events "
                    "WHERE kind='started' AND request_key=?", (key,),
                ).fetchone()
                if start is None or start[1] != sha:
                    raise FenceWitnessError("clearance is not operation-bound")
                if not operator and start[0] != session:
                    raise FenceWitnessError("runtime cannot clear another session")
                if operator:
                    originating = conn.execute(
                        "SELECT state FROM sessions WHERE id=?", (start[0],),
                    ).fetchone()
                    if originating is None or originating[0] != "sealed":
                        raise FenceWitnessError(
                            "operator clearance requires sealed originating session"
                        )
                if not operator:
                    completed = conn.execute(
                        "SELECT outcome FROM events WHERE kind='completed' "
                        "AND request_key=?", (key,),
                    ).fetchone()
                    if completed is None or completed[0] != "ambiguous":
                        raise FenceWitnessError(
                            "runtime clearance requires completed ambiguous outcome"
                        )
                if conn.execute(
                    "SELECT 1 FROM events WHERE request_key=? AND kind IN "
                    "('runtime_cleared','operator_cleared')",
                    (key,),
                ).fetchone():
                    raise FenceWitnessError("write recovery was already cleared")
                self._append(
                    conn, session=start[0] if operator else session,
                    kind="operator_cleared" if operator else "runtime_cleared",
                    key=key, sha=sha, evidence=evidence,
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise


class _WitnessUnixHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(3.0)
        server: _WitnessUnixServer = self.server  # type: ignore[assignment]
        session = None
        try:
            if not hasattr(socket, "SO_PEERCRED"):
                raise FenceWitnessError("kernel peer credential support is required")
            credentials = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _pid, uid, _gid = struct.unpack("3i", credentials)
            if uid != server.peer_uid:
                raise FenceWitnessError("socket peer is not authorized")
            if server.role == "runtime":
                session = server.ledger.open_session()
            self._send({"ok": True, "version": _VERSION})
            while True:
                request = self._next_frame(role=server.role)
                if request is None:
                    break
                if type(request.get("version")) is not int or request["version"] != _VERSION:
                    raise FenceWitnessError("protocol version mismatch")
                try:
                    answer = self._dispatch(request, server, session)
                except (FenceWitnessError, ValueError, sqlite3.Error, OSError):
                    self._send({"ok": False, "error": "request_denied"})
                else:
                    self._send({"ok": True, "result": answer})
                if server.role == "operator":
                    break
        except (FenceWitnessError, ConnectionError, OSError, TimeoutError):
            try:
                self._send({"ok": False, "error": "request_denied"})
            except OSError:
                pass
        finally:
            if session is not None:
                try:
                    server.ledger.seal_session(session)
                except (FenceWitnessError, sqlite3.Error, OSError):
                    # The caller is no longer authenticated to this session,
                    # and an unavailable journal must fail the entire owner
                    # service rather than silently retain an active session.
                    server._fatal.set()

    def _next_frame(self, *, role: str) -> dict[str, object] | None:
        # A runtime may legitimately need minutes for one external browser
        # operation. An idle connection is not an UNKNOWN outcome, but once
        # any frame begins it gets an absolute wall-clock read deadline.
        self.request.settimeout(None if role == "runtime" else 3.0)
        first = self.rfile.read(1)
        if not first:
            return None
        deadline = time.monotonic() + 10.0
        payload = bytearray(first)
        while payload[-1] != 10:  # newline terminates one bounded JSON frame
            if len(payload) > _MAX_FRAME:
                raise FenceWitnessError("request frame is not bounded")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FenceWitnessError("request frame read deadline expired")
            self.request.settimeout(min(3.0, remaining))
            next_byte = self.rfile.read(1)
            if not next_byte:
                raise FenceWitnessError("request frame is incomplete")
            payload.extend(next_byte)
        if len(payload) > _MAX_FRAME + 1:
            raise FenceWitnessError("request frame is not bounded")
        return _strict_json(bytes(payload[:-1]))

    def _send(self, value: object) -> None:
        self.wfile.write((_canonical_json(value) + "\n").encode("utf-8"))
        self.wfile.flush()

    @staticmethod
    def _dispatch(
        request: dict[str, object], server: "_WitnessUnixServer",
        session: str | None,
    ) -> dict[str, object]:
        op = request.get("op")
        if server.role == "runtime":
            if session is None:
                raise FenceWitnessError("runtime session absent")
            if op == "pending" and set(request) == {"version", "op"}:
                return {"pending": server.ledger.fence_pending()}
            if op == "started" and set(request) == {
                "version", "op", "key", "sha", "action",
            }:
                server.ledger.execution_started(
                    session, key=request["key"], sha=request["sha"],
                    action=request["action"],
                )
                return {"recorded": True}
            if op == "completed" and set(request) == {
                "version", "op", "key", "sha", "response",
            }:
                outcome = server.ledger.execution_completed(
                    session, key=request["key"], sha=request["sha"],
                    response=request["response"],
                )
                return {"outcome": outcome}
            if op == "runtime_clear" and set(request) == {
                "version", "op", "key", "sha", "evidence",
            }:
                server.ledger.runtime_clearance(
                    session, key=request["key"], sha=request["sha"],
                    evidence=request["evidence"],
                )
                return {"recorded": True}
        elif server.role == "operator":
            if op == "pending" and set(request) == {"version", "op"}:
                return {"pending": server.ledger.fence_pending()}
            if op == "operator_clear" and set(request) == {
                "version", "op", "key", "sha", "evidence",
            }:
                server.ledger.operator_clearance(
                    key=request["key"], sha=request["sha"],
                    evidence=request["evidence"],
                )
                return {"recorded": True}
        raise FenceWitnessError("operation is not exposed by this socket")


class _WitnessUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    # A persistent runtime session must not keep serve_forever() blocked
    # when PID 1 receives SIGTERM. Track and terminate active socket sessions
    # explicitly rather than merely spawning unbounded daemon threads.
    allow_reuse_address = False
    request_queue_size = 4
    daemon_threads = True
    block_on_close = False

    def process_request_thread(self, request: socket.socket, client_address: object) -> None:
        with self._peers_lock:
            self._peers.add(request)
            self._no_active_peers.clear()
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._peers_lock:
                self._peers.discard(request)
                if not self._peers:
                    self._no_active_peers.set()

    def terminate_sessions(self) -> bool:
        """Wake pending socket readers; require a bounded session drain."""
        with self._peers_lock:
            connections = tuple(self._peers)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                # Already disconnected is safe; the handler still seals
                # its operation-bound session in its finally block.
                pass
        return self._no_active_peers.wait(2.5)

    def __init__(
        self, path: Path, *, ledger: FenceLedger, role: str, peer_uid: int,
        socket_gid: int,
    ) -> None:
        if role not in {"runtime", "operator"} or not isinstance(peer_uid, int):
            raise FenceWitnessError("invalid socket role")
        if not path.is_absolute():
            raise FenceWitnessError("socket path must be absolute")
        mode = 0o2710 if role == "runtime" else 0o700
        _private_directory(
            path.parent, os.geteuid(), mode=mode,
            gid=socket_gid if role == "runtime" else None,
        )
        if path.exists() or path.is_symlink():
            raise FenceWitnessError("refusing to replace an existing socket path")
        self.ledger = ledger
        self.role = role
        self.peer_uid = peer_uid
        self._path = path
        self._fatal = Event()
        self._peers_lock = RLock()
        self._peers: set[socket.socket] = set()
        self._no_active_peers = Event()
        self._no_active_peers.set()
        super().__init__(str(path), _WitnessUnixHandler)
        os.chmod(path, 0o660 if role == "runtime" else 0o600)
        info = path.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_gid != socket_gid):
            super().server_close()
            raise FenceWitnessError("socket inode has unsafe ownership")
        self._inode = (info.st_dev, info.st_ino)

    def server_close(self) -> None:
        super().server_close()
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return
        if (stat.S_ISSOCK(info.st_mode)
                and (info.st_dev, info.st_ino) == getattr(self, "_inode", None)):
            os.unlink(self._path)


class FenceWitnessServer:
    """Two physically distinct role-scoped sockets, owned by the ledger UID."""

    def __init__(
        self, *, ledger: FenceLedger, runtime_socket: Path,
        operator_socket: Path, runtime_uid: int, runtime_gid: int,
        operator_uid: int,
    ) -> None:
        if runtime_uid == os.geteuid() or operator_uid == runtime_uid:
            raise FenceWitnessError("runtime and trusted operator must have separate UIDs")
        self._runtime = _WitnessUnixServer(
            runtime_socket, ledger=ledger, role="runtime",
            peer_uid=runtime_uid, socket_gid=runtime_gid,
        )
        try:
            self._operator = _WitnessUnixServer(
                operator_socket, ledger=ledger, role="operator",
                peer_uid=operator_uid, socket_gid=os.getegid(),
            )
        except BaseException:
            self._runtime.server_close()
            raise
        self._threads: list[Thread] = []

    def start(self) -> None:
        if self._threads:
            raise FenceWitnessError("witness server already started")
        for server in (self._runtime, self._operator):
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                            daemon=True, name="mark-fence-witness")
            thread.start()
            self._threads.append(thread)

    def close(self) -> None:
        servers = (self._runtime, self._operator)
        if self._threads:
            # Thread.start() can fail between the two servers. shutdown()
            # must never be called on a server without serve_forever().
            for server, thread in zip(servers, self._threads):
                if thread.is_alive():
                    server.shutdown()
        # Release the accepted sockets as well as the listeners. A malicious
        # authenticated peer could otherwise hold the witness process alive
        # indefinitely by sending a frame every few seconds.
        drained = True
        try:
            for server in servers:
                if not server.terminate_sessions():
                    drained = False
        finally:
            for server in servers:
                server.server_close()
            for thread in self._threads:
                thread.join(timeout=2)
        if not drained or any(server._fatal.is_set() for server in servers):
            raise FenceWitnessError(
                "witness shutdown could not seal all sessions before deadline"
            )


class FenceWitnessClient:
    """One persistent UNIX session; no automatic retries after unknown outcomes.

    The kernel, not socket path naming or a runtime-controlled response, must
    identify the independent owner UID before any fence request is trusted.
    """

    def __init__(
        self, socket_path: Path, *, expected_owner_uid: int,
        timeout_seconds: float = 12.0,
    ) -> None:
        if (
            not isinstance(socket_path, Path)
            or not socket_path.is_absolute()
            or type(expected_owner_uid) is not int
            or expected_owner_uid < 0
            or not 0 < timeout_seconds <= 30
        ):
            raise FenceWitnessError("invalid independent witness connection")
        if not hasattr(socket, "SO_PEERCRED"):
            raise FenceWitnessError("kernel peer credentials are unavailable")
        self._guard = RLock()
        self._socket: socket.socket | None = None
        self._stream: Any = None
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            peer.settimeout(timeout_seconds)
            peer.connect(str(socket_path))
            credentials = peer.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, 12,
            )
            _pid, actual_uid, _gid = struct.unpack("3i", credentials)
            if actual_uid != expected_owner_uid:
                raise FenceWitnessError("witness peer UID is not trusted")
            stream = peer.makefile("rwb", buffering=0)
            self._socket, self._stream = peer, stream
            hello = self._receive()
            if hello != {"ok": True, "version": _VERSION}:
                raise FenceWitnessError("witness handshake not verified")
        except BaseException:
            self.close()
            peer.close()
            raise

    def _receive(self) -> dict[str, object]:
        if self._stream is None:
            raise FenceWitnessError("witness connection is closed")
        raw = self._stream.readline(_MAX_FRAME + 2)
        if not raw.endswith(b"\n") or len(raw) > _MAX_FRAME + 1:
            raise FenceWitnessError("witness reply is incomplete or too large")
        return _strict_json(raw[:-1])

    def _call(self, operation: str, **arguments: object) -> dict[str, object]:
        with self._guard:
            if self._socket is None or self._stream is None:
                raise FenceWitnessError("witness connection is closed")
            request = {"version": _VERSION, "op": operation, **arguments}
            try:
                encoded = (_canonical_json(request) + "\n").encode("utf-8")
                if len(encoded) > _MAX_FRAME + 1:
                    raise FenceWitnessError("witness request exceeds frame bound")
                self._socket.sendall(encoded)
                received = self._receive()
                if received.get("ok") is not True or not isinstance(
                    received.get("result"), dict
                ):
                    raise FenceWitnessError("witness request was not acknowledged")
                return received["result"]
            except (OSError, FenceWitnessError, ValueError, TimeoutError) as exc:
                # A late SQLite commit or lost response is UNKNOWN, not a
                # reason to send a second execution_started on a fresh socket.
                self.close()
                raise FenceWitnessError(
                    "witness outcome uncertain; do not automatically retry"
                ) from exc
            except BaseException:
                # KeyboardInterrupt, SystemExit, MemoryError or a signal
                # between send and response must poison the connection:
                # an unread ACK for A cannot ever acknowledge a later B.
                self.close()
                raise

    def pending(self) -> bool:
        result = self._call("pending")
        if type(result.get("pending")) is not bool:
            self.close()
            raise FenceWitnessError("witness pending result is invalid")
        return result["pending"]

    def started(self, *, key: str, sha: str, action: str) -> None:
        if self._call("started", key=key, sha=sha, action=action) != {
            "recorded": True
        }:
            self.close()
            raise FenceWitnessError("witness start acknowledgement is invalid")

    def completed(self, *, key: str, sha: str, response: object) -> str:
        result = self._call(
            "completed", key=key, sha=sha, response=response,
        )
        value = result.get("outcome")
        if value not in (*_SAFE_OUTCOMES, "ambiguous"):
            self.close()
            raise FenceWitnessError("witness completion acknowledgement is invalid")
        return value

    def runtime_clear(self, *, key: str, sha: str, evidence: str) -> None:
        if self._call(
            "runtime_clear", key=key, sha=sha, evidence=evidence,
        ) != {"recorded": True}:
            self.close()
            raise FenceWitnessError("witness clearance acknowledgement is invalid")

    def operator_clear(self, *, key: str, sha: str, evidence: str) -> None:
        if self._call(
            "operator_clear", key=key, sha=sha, evidence=evidence,
        ) != {"recorded": True}:
            self.close()
            raise FenceWitnessError("witness operator acknowledgement is invalid")

    def close(self) -> None:
        with self._guard:
            stream, sock = self._stream, self._socket
            self._stream, self._socket = None, None
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def __enter__(self) -> "FenceWitnessClient":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def _positive_uid(value: str) -> int:
    if not value.isdigit() or int(value) == 0:
        raise argparse.ArgumentTypeError("positive numerical UID required")
    return int(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Separate-UID, append-only Mark write recovery fence witness"
    )
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--runtime-socket", type=Path, required=True)
    parser.add_argument("--operator-socket", type=Path, required=True)
    parser.add_argument("--runtime-uid", type=_positive_uid, required=True)
    parser.add_argument("--runtime-gid", type=_positive_uid, required=True)
    parser.add_argument("--operator-uid", type=int, default=0)
    parser.add_argument("--init-ledger", action="store_true")
    args = parser.parse_args(argv)
    if os.geteuid() == 0 or os.geteuid() == args.runtime_uid:
        parser.error("witness requires a separate non-root service UID")
    if args.operator_uid != 0:
        parser.error("operator clearance socket requires host root UID")
    if args.init_ledger:
        if args.ledger.exists() or args.ledger.is_symlink():
            parser.error("ledger already exists; never silently reinitialize")
        try:
            FenceLedger(args.ledger, initialize=True)
        except (FenceWitnessError, OSError, sqlite3.Error) as exc:
            parser.error(str(exc))
        return 0
    try:
        ledger = FenceLedger(args.ledger, initialize=False)
        server = FenceWitnessServer(
            ledger=ledger, runtime_socket=args.runtime_socket,
            operator_socket=args.operator_socket,
            runtime_uid=args.runtime_uid, runtime_gid=args.runtime_gid,
            operator_uid=args.operator_uid,
        )
        stopping = Event()
        previous_sigterm = signal.signal(
            signal.SIGTERM, lambda _signum, _frame: stopping.set(),
        )
        try:
            server.start()
            while not stopping.wait(0.2):
                if not all(thread.is_alive() for thread in server._threads):
                    raise FenceWitnessError("witness IPC service stopped unexpectedly")
                if server._runtime._fatal.is_set() or server._operator._fatal.is_set():
                    raise FenceWitnessError("witness session sealing failed")
        except KeyboardInterrupt:
            pass
        finally:
            server.close()
            signal.signal(signal.SIGTERM, previous_sigterm)
    except (FenceWitnessError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())