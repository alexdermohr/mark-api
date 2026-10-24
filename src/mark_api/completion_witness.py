"""Independent actor admission and completion in the existing witness journal.

The ordinary runtime UID has access to NEITHER socket. A protected owner starts
and durably stores the Write API result; a different protected browser actor
reserves one actuation and reports its own independently obtained pre/postread.
Only matching events from both authenticated roles settle a fence. Existing
executor-only witness events remain ambiguous forever; no history is rewritten.

This does not attest host cutover. Legacy paired-restore preflight intentionally
rejects these new event kinds until its full recovery matrix is integrated.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import os
from pathlib import Path
import re
import socket
import socketserver
from threading import BoundedSemaphore, Thread
import time

from .completion_authority import (
    CompletionAuthorityError, CompletionIntent, _MAX_FRAME, _canonical, _roles,
    decode_evidence,
)
from .domain import CreateOperationReceipt, OperationOutcome
from .fence_witness import (
    FenceLedger, FenceWitnessAlreadyRecorded, FenceWitnessError,
    _WitnessUnixServer, _canonical_json, _required_string, _KEY, _SHA,
)
from .storage_owner import _linux_ucred_uid
from .strict_json import strict_json_object
from .write_api import _create_receipt_to_dict, _receipt_to_dict

_AUTHORITY_INDEXES = {
    "one_authority_begin_per_key": "authority_begun",
    "one_authority_verification_per_key": "authority_verified",
}
_PROOF = re.compile(r"[0-9a-f]{64}\.[0-9a-f]{64}\Z")


def _error() -> FenceWitnessError:
    return FenceWitnessError("independent completion witness not verified")


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def evidence_response(intent: CompletionIntent, evidence: object) -> dict[str, object]:
    """Reconstruct the exact existing HTTP envelope; do not trust a caller hash."""
    try:
        receipt = decode_evidence(evidence, intent)
        row = (_create_receipt_to_dict(receipt) if type(receipt) is CreateOperationReceipt
               else _receipt_to_dict(receipt))
        response = {"idempotency_key": intent.key, "operation_receipt": row,
                    "platform_retry_authorized": False}
        if intent.action == "create_media":
            if type(receipt) is not CreateOperationReceipt or receipt.media_post_read_status is None:
                raise _error()
            response.update({
                "media_persistence_confirmed": receipt.media_persistence_confirmed,
                "media_post_read_status": receipt.media_post_read_status.value,
            })
        return response
    except (CompletionAuthorityError, TypeError, ValueError):
        raise _error() from None


# Both proof kinds have unique partial indexes. Exact session, request,
# generation-bound intent digest, immutable response and temporal event order
# are required. Ordinary/legacy completions cannot satisfy this join.
_SETTLED = """
    SELECT c.outcome FROM events AS b
    JOIN events AS v ON v.kind='authority_verified'
      AND v.request_key=b.request_key AND v.request_sha256=b.request_sha256
      AND v.session_id=b.session_id
    JOIN events AS c ON c.kind='completed'
      AND c.request_key=b.request_key AND c.request_sha256=b.request_sha256
      AND c.session_id=b.session_id
    WHERE b.kind='authority_begun' AND b.request_key=s.request_key
      AND b.request_sha256=s.request_sha256 AND b.session_id=s.session_id
      AND b.seq>s.seq AND v.seq>b.seq AND c.seq>v.seq
      AND b.action='' AND b.outcome='' AND b.response_sha256=''
      AND length(b.evidence_reference)=64
      AND b.evidence_reference NOT GLOB '*[^0-9a-f]*'
      AND v.action='' AND length(v.evidence_reference)=129
      AND substr(v.evidence_reference,1,64)=b.evidence_reference
      AND substr(v.evidence_reference,65,1)='.'
      AND substr(v.evidence_reference,66,64) NOT GLOB '*[^0-9a-f]*'
      AND c.action='' AND c.evidence_reference=''
      AND length(v.response_sha256)=64 AND v.response_sha256 NOT GLOB '*[^0-9a-f]*'
      AND c.response_sha256=v.response_sha256 AND c.outcome=v.outcome
      AND c.outcome IN ('confirmed','precondition_failed')
"""


class CompletionWitnessLedger(FenceLedger):
    """Same append-only ledger, explicit initialization of two safety indexes."""

    def __init__(self, path: Path, *, principal: str, initialize: bool = False) -> None:
        if (type(principal) is not str or not 1 <= len(principal) <= 128
                or principal.strip() != principal or any(ord(c) < 32 for c in principal)):
            raise _error()
        self.principal = principal
        super().__init__(path, initialize=initialize)
        try:
            with self._guard, closing(self._connect()) as conn:
                # Adding indexes is an explicit owner initialization, never an
                # implicit serve-time migration or permission to rewrite events.
                if initialize:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        for name, kind in _AUTHORITY_INDEXES.items():
                            conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON events(request_key) WHERE kind='{kind}'")
                        conn.commit()
                    except BaseException:
                        conn.rollback()
                        raise
                for name, kind in _AUTHORITY_INDEXES.items():
                    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
                    sql = "".join(row[0].lower().split()) if row and type(row[0]) is str else ""
                    expected = f"createuniqueindex{name}onevents(request_key)wherekind='{kind}'"
                    if sql.replace("ifnotexists", "") != expected:
                        raise _error()
        except BaseException:
            self.close()
            raise

    def _intent(self, intent: CompletionIntent) -> CompletionIntent:
        try:
            if type(intent) is not CompletionIntent:
                raise _error()
            return CompletionIntent.from_wire(intent.to_wire(), expected_principal=self.principal)
        except (CompletionAuthorityError, TypeError, ValueError):
            raise _error() from None

    @staticmethod
    def _start(conn, intent: CompletionIntent):
        row = conn.execute("SELECT session_id,request_sha256,action FROM events WHERE kind='started' AND request_key=?", (intent.key,)).fetchone()
        if row is None or row[1:] != (intent.sha, intent.action):
            raise _error()
        FenceLedger._require_active(conn, row[0])
        return row[0]

    def authority_begin(self, intent: CompletionIntent) -> None:
        intent = self._intent(intent)
        self._ensure_owner_pid()
        with self._guard, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                session = self._start(conn, intent)
                if conn.execute("SELECT 1 FROM events WHERE request_key=? AND kind IN ('authority_begun','completed') LIMIT 1", (intent.key,)).fetchone():
                    raise FenceWitnessAlreadyRecorded("authority actuation already consumed")
                self._append(conn, session=session, kind="authority_begun", key=intent.key,
                             sha=intent.sha, evidence=_hash(intent.to_wire()))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def authority_verified(self, intent: CompletionIntent, evidence: object) -> None:
        intent = self._intent(intent)
        try:
            receipt = decode_evidence(evidence, intent)
            response = evidence_response(intent, evidence)
            outcome = receipt.outcome.value
            # A no-input claim is safe only when the trusted actor did not
            # invoke its writer at all. Anything less certain retains the fence.
            if receipt.outcome is OperationOutcome.PRECONDITION_FAILED and receipt.writer_invoked:
                outcome = "ambiguous"
            if (intent.action == "create_media" and receipt.outcome is OperationOutcome.CONFIRMED
                    and not receipt.media_persistence_confirmed):
                # A content-confirmed creation with unverified images is HTTP
                # 202, never an independently settled A6 media completion.
                outcome = "ambiguous"
            proof = _hash(intent.to_wire()) + "." + _hash(evidence)
            response_sha = _hash(response)
        except (CompletionAuthorityError, TypeError, ValueError):
            raise _error() from None
        self._ensure_owner_pid()
        with self._guard, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                session = self._start(conn, intent)
                begin = conn.execute("SELECT session_id,request_sha256,evidence_reference FROM events WHERE kind='authority_begun' AND request_key=?", (intent.key,)).fetchone()
                if begin != (session, intent.sha, proof[:64]):
                    raise _error()
                if conn.execute("SELECT 1 FROM events WHERE request_key=? AND kind IN ('authority_verified','completed') LIMIT 1", (intent.key,)).fetchone():
                    raise _error()
                self._append(conn, session=session, kind="authority_verified", key=intent.key,
                             sha=intent.sha, outcome=outcome, response_sha=response_sha, evidence=proof)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _is_pending(conn) -> bool:
        return conn.execute("SELECT 1 FROM events AS s WHERE s.kind='started' AND NOT EXISTS ("
                            "SELECT 1 FROM events AS o WHERE o.kind='operator_cleared' "
                            "AND o.request_key=s.request_key AND o.request_sha256=s.request_sha256 "
                            "AND o.session_id=s.session_id) AND NOT EXISTS (" + _SETTLED + ") LIMIT 1").fetchone() is not None

    def execution_completed(self, session: str, *, key: str, sha: str, response: object) -> str:
        key = _required_string(key, _KEY, "idempotency key")
        sha = _required_string(sha, _SHA, "request digest")
        try:
            encoded = _canonical_json(response).encode("utf-8")
            if len(encoded) > 32 * 1024:
                raise _error()
            response_sha = hashlib.sha256(encoded).hexdigest()
        except (ValueError, TypeError, UnicodeError):
            raise _error() from None
        self._ensure_owner_pid()
        with self._guard, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._require_active(conn, session)
                start = conn.execute("SELECT session_id,request_sha256,seq FROM events WHERE kind='started' AND request_key=?", (key,)).fetchone()
                if start is None or start[:2] != (session, sha):
                    raise _error()
                if conn.execute("SELECT 1 FROM events WHERE kind='completed' AND request_key=?", (key,)).fetchone():
                    raise _error()
                begin = conn.execute("SELECT session_id,request_sha256,evidence_reference,seq FROM events WHERE kind='authority_begun' AND request_key=?", (key,)).fetchone()
                proof = conn.execute("SELECT session_id,request_sha256,evidence_reference,seq,outcome,response_sha256 FROM events WHERE kind='authority_verified' AND request_key=?", (key,)).fetchone()
                outcome = "ambiguous"
                if (begin is not None and proof is not None and begin[:2] == proof[:2] == (session, sha)
                        and type(proof[2]) is str and _PROOF.fullmatch(proof[2])
                        and proof[2][:64] == begin[2] and start[2] < begin[3] < proof[3]
                        and proof[5] == response_sha and proof[4] in {"confirmed", "precondition_failed"}):
                    outcome = proof[4]
                self._append(conn, session=session, kind="completed", key=key, sha=sha,
                             outcome=outcome, response_sha=response_sha)
                conn.commit()
                return outcome
            except BaseException:
                conn.rollback()
                raise

    def replay_status(self, *, key: str, sha: str, response_sha: str) -> str:
        legacy = super().replay_status(key=key, sha=sha, response_sha=response_sha)
        if legacy != "ambiguous":
            return legacy
        with self._guard, closing(self._connect()) as conn:
            row = conn.execute("SELECT (" + _SETTLED + ") FROM events AS s WHERE s.kind='started' AND s.request_key=? AND s.request_sha256=?", (key, sha)).fetchone()
            return row[0] if row and row[0] in {"confirmed", "precondition_failed"} else "ambiguous"

    def pending_page(self, *, after_seq: int = 0) -> dict[str, object]:
        page = super().pending_page(after_seq=after_seq)
        with self._guard, closing(self._connect()) as conn:
            page["rows"] = [row for row in page["rows"] if not conn.execute(
                "SELECT 1 FROM events AS s WHERE s.kind='started' AND s.seq=? AND EXISTS (" + _SETTLED + ")",
                (row["seq"],),
            ).fetchone()]
        # Keep the original cursor and 'more' even if this page was all settled.
        return page


def _read_frame(peer, stream) -> dict[str, object]:
    deadline = time.monotonic() + 10.0
    data = bytearray()
    while len(data) <= _MAX_FRAME:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _error()
        peer.settimeout(min(3.0, remaining))
        value = stream.read(1)
        if not value:
            raise _error()
        if value == b"\n":
            if not data:
                raise _error()
            return strict_json_object(bytes(data))
        data.extend(value)
    raise _error()


class _ProofHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            if _linux_ucred_uid(self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)) != self.server.peer_uid:
                return
            request = _read_frame(self.request, self.rfile)
            op = request.get("op")
            required = {"version", "op", "intent"} | ({"evidence"} if op == "verify" else set())
            if set(request) != required or type(request["version"]) is not int or request["version"] != 1:
                raise _error()
            intent = CompletionIntent.from_wire(request["intent"], expected_principal=self.server.ledger.principal)
            if op == "reserve":
                self.server.ledger.authority_begin(intent)
            elif op == "verify":
                self.server.ledger.authority_verified(intent, request["evidence"])
            else:
                raise _error()
            self.wfile.write(b'{"ok":true,"recorded":true,"version":1}\n')
            self.wfile.flush()
        except Exception:
            try:
                self.wfile.write(b'{"error":"completion_unknown","ok":false,"version":1}\n')
                self.wfile.flush()
            except OSError:
                pass


class _ProofServer(_WitnessUnixServer):
    def __init__(self, path, *, ledger, authority_uid, authority_gid):
        self._capacity = BoundedSemaphore(8)
        super().__init__(path, ledger=ledger, role="runtime", peer_uid=authority_uid, socket_gid=authority_gid)
        self.RequestHandlerClass = _ProofHandler

    def process_request(self, request, client_address):
        if not self._capacity.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._capacity.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._capacity.release()


class CompletionWitnessServer:
    """Physically distinct owner and trusted-actor sockets; ordinary UID denied."""

    def __init__(self, *, ledger: CompletionWitnessLedger, owner_socket: Path,
                 authority_socket: Path, owner_uid: int, owner_gid: int,
                 authority_uid: int, authority_gid: int, runtime_uid: int) -> None:
        _roles(os.geteuid(), owner_uid, authority_uid, runtime_uid)
        if type(ledger) is not CompletionWitnessLedger or ledger._uid != os.geteuid():
            raise _error()
        self._owner = _WitnessUnixServer(owner_socket, ledger=ledger, role="runtime",
                                        peer_uid=owner_uid, socket_gid=owner_gid)
        try:
            self._authority = _ProofServer(authority_socket, ledger=ledger,
                                           authority_uid=authority_uid, authority_gid=authority_gid)
        except BaseException:
            self._owner.server_close()
            raise
        self._threads: list[Thread] = []
        self._closed = False

    def start(self):
        if self._threads or self._closed:
            raise _error()
        for server in (self._owner, self._authority):
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            self._threads.append(thread)

    def close(self):
        if self._closed:
            return
        servers = (self._owner, self._authority)
        for server, thread in zip(servers, self._threads):
            if thread.is_alive():
                server.shutdown()
        drained = all([server.terminate_sessions() for server in servers])
        for server in servers:
            server.server_close()
        for thread in self._threads:
            thread.join(2)
        self._closed = True
        if not drained or any(server._fatal.is_set() for server in servers):
            raise _error()


class AuthorityWitnessClient:
    """One-shot kernel-authenticated calls; never reconnect/retry a lost ACK."""

    def __init__(self, socket_path: Path, *, expected_owner_uid: int) -> None:
        _roles(os.geteuid(), expected_owner_uid)
        if (not isinstance(socket_path, Path) or not socket_path.is_absolute()
                or socket_path != Path(os.path.normpath(socket_path))):
            raise _error()
        self.socket_path = socket_path
        self.expected_owner_uid = expected_owner_uid
        self._pid, self._uid = os.getpid(), os.geteuid()

    def _call(self, op: str, intent: CompletionIntent, evidence=None):
        if (os.getpid(), os.geteuid()) != (self._pid, self._uid):
            raise _error()
        request = {"version": 1, "op": op, "intent": intent.to_wire()}
        if op == "verify":
            request["evidence"] = evidence
        try:
            encoded = _canonical(request)
            if len(encoded) > _MAX_FRAME:
                raise _error()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(10.0)
                peer.connect(str(self.socket_path))
                if _linux_ucred_uid(peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)) != self.expected_owner_uid:
                    raise _error()
                peer.sendall(encoded + b"\n")
                with peer.makefile("rb") as stream:
                    response = _read_frame(peer, stream)
                if response != {"ok": True, "version": 1, "recorded": True} or any(type(response[k]) is not bool for k in ("ok", "recorded")) or type(response["version"]) is not int:
                    raise _error()
        except Exception:
            raise _error() from None

    def reserve(self, intent: CompletionIntent) -> None:
        self._call("reserve", intent)

    def verify(self, intent: CompletionIntent, evidence: object) -> None:
        self._call("verify", intent, evidence)