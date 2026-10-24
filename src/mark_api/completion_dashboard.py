"""Protected dashboard reconciliation over the existing typed owner socket.

The frontend can request an exact saved result or acknowledge its display,
including independently verified media Creates. It cannot submit a result,
witness proof, clearance, arbitrary SQL or a completion.
The owner itself reads its persisted intent/receipt and asks its independent
witness before either ACK phase. The old StorageOwnerClient remains read/claim
only; this explicit composition is not a host-cutover or media-release claim.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import os
from pathlib import Path
from threading import RLock

from .completion_authority import _roles
from .fence_witness import FenceWitnessClient
from .storage import DashboardPendingWrite
from .storage_owner import (
    StorageOwnerClient, StorageOwnerError, StorageOwnerServer,
    _pending_as_json, _pending_from_json, _reply_frame_fits,
)
from .strict_json import strict_json_object
from .write_api import WriteApiAccess


_PROFILE = "a6-independent-dashboard-v2"
_MAX_RESPONSE = 24 * 1024


def _denied() -> StorageOwnerError:
    return StorageOwnerError("independent dashboard completion not verified")


class CompletionDashboardOwnerServer(StorageOwnerServer):
    """Owner-only completion reads and two-phase ACK, not frontend attestations."""

    def __init__(self, *, database: Path, socket_path: Path, runtime_uid: int,
                 runtime_gid: int, access: WriteApiAccess,
                 fence_witness: FenceWitnessClient) -> None:
        if type(access) is not WriteApiAccess or not access.writes_enabled:
            raise _denied()
        if type(fence_witness) is not FenceWitnessClient:
            raise _denied()
        _roles(os.geteuid(), runtime_uid, fence_witness._expected_owner_uid)
        self._completion_principal = access.principal
        self._completion_witness = fence_witness
        super().__init__(database=database, socket_path=socket_path,
                         runtime_uid=runtime_uid, runtime_gid=runtime_gid)

    def _confirmed(self, expected: DashboardPendingWrite) -> bytes:
        # Import lazily: this reuses the exact ordinary product's strict
        # fingerprint/receipt validation, not a second weaker ACK algorithm.
        from .dashboard import _local_pending_matches, _stored_completed_write_response
        if type(expected) is not DashboardPendingWrite:
            raise _denied()
        canonical = _pending_from_json(_pending_as_json(expected))
        if canonical.acknowledged:
            raise _denied()
        matches = _local_pending_matches(
            self.store, scope=canonical.scope, key=canonical.idempotency_key,
            resource=canonical.resource_key,
        )
        if matches and (len(matches) != 1 or replace(matches[0], acknowledged=False) != canonical):
            raise _denied()
        body = _stored_completed_write_response(self.store, self._completion_principal, canonical)
        record = self.store.write_api_request(canonical.idempotency_key)
        if (record is None or record.response_json is None
                or record.response_json.encode("utf-8") != body
                or self._completion_witness.replay_status(
                    key=canonical.idempotency_key, sha=record.request_sha256,
                    response_sha=hashlib.sha256(body).hexdigest(),
                ) != "confirmed"):
            raise _denied()
        if len(body) > _MAX_RESPONSE:
            raise _denied()
        return body

    def dispatch(self, request: dict[str, object]) -> dict[str, object]:
        if type(request) is not dict or type(request.get("version")) is not int or request["version"] != 1:
            raise _denied()
        operation = request.get("op")
        if operation == "dashboard_completion_profile":
            if set(request) != {"version", "op"}:
                raise _denied()
            self._attest()
            return {"profile": _PROFILE, "principal": self._completion_principal,
                    "owner_uid": os.geteuid(), "witness_uid": self._completion_witness._expected_owner_uid}
        if operation == "dashboard_confirmed_response":
            if set(request) != {"version", "op", "intent"}:
                raise _denied()
            self._attest()
            intent = _pending_from_json(request["intent"])
            with self.store.dashboard_pending_write_guard():
                answer = {"response_json": self._confirmed(intent).decode("utf-8")}
                if not _reply_frame_fits(answer):
                    raise _denied()
                return answer
        if operation == "dashboard_completed_ack":
            if set(request) != {"version", "op", "scope", "key"}:
                raise _denied()
            self._attest()
            scope, key = request["scope"], request["key"]
            if (type(scope) is not str or not 1 <= len(scope) <= 128
                    or type(key) is not str or not 1 <= len(key) <= 128):
                raise _denied()
            from .dashboard import _local_pending_matches
            with self.store.dashboard_pending_write_guard():
                matches = _local_pending_matches(self.store, scope=scope, key=key)
                if len(matches) != 1 or matches[0].scope != scope or matches[0].idempotency_key != key:
                    raise _denied()
                # Both phases require the same independent proof. A caller
                # cannot nominate a phase, response, digest or success flag.
                self._confirmed(replace(matches[0], acknowledged=False))
                phase = self.store.acknowledge_dashboard_pending_write(scope=scope, idempotency_key=key)
                if phase not in {"acknowledged", "finalized"}:
                    raise _denied()
                return {"phase": phase}
        return super().dispatch(request)


class CompletionDashboardClient(StorageOwnerClient):
    """Exact typed owner profile; the client has no completion/clearance method."""

    def __init__(self, socket_path: Path, *, expected_owner_uid: int,
                 expected_principal: str) -> None:
        _roles(os.geteuid(), expected_owner_uid)
        if (type(expected_principal) is not str or not 1 <= len(expected_principal) <= 128
                or expected_principal.strip() != expected_principal):
            raise _denied()
        super().__init__(socket_path, expected_owner_uid=expected_owner_uid)
        self._pid, self._uid = os.getpid(), os.geteuid()
        self.principal = expected_principal
        # This lock only serializes this frontend's handlers. It is NOT the
        # safety authority: owner-side guards and SQLite claims remain final.
        self._dashboard_guard = RLock()
        self.verify_completion_profile()

    def _identity(self) -> None:
        if (os.getpid(), os.geteuid()) != (self._pid, self._uid):
            raise _denied()

    def verify_completion_profile(self) -> None:
        self._identity()
        answer = self._call("dashboard_completion_profile")
        if (set(answer) != {"profile", "principal", "owner_uid", "witness_uid"}
                or answer["profile"] != _PROFILE or answer["principal"] != self.principal
                or type(answer["owner_uid"]) is not int or answer["owner_uid"] != self.expected_owner_uid):
            raise _denied()
        _roles(self._uid, self.expected_owner_uid, answer["witness_uid"])

    def write_api_request(self, idempotency_key: str):
        # The existing typed read_request already validates the exact key and
        # persisted record. This adapter adds no new RPC or completion right.
        self._identity()
        return self.read_request(idempotency_key)

    @contextmanager
    def dashboard_pending_write_guard(self):
        self._identity()
        with self._dashboard_guard:
            yield

    def confirmed_dashboard_write_response(self, pending: DashboardPendingWrite) -> bytes:
        self._identity()
        intent = _pending_from_json(_pending_as_json(replace(pending, acknowledged=False)))
        answer = self._call("dashboard_confirmed_response", intent=_pending_as_json(intent))
        if set(answer) != {"response_json"} or type(answer["response_json"]) is not str:
            raise _denied()
        try:
            data = answer["response_json"].encode("utf-8")
            if len(data) > _MAX_RESPONSE:
                raise _denied()
            response = strict_json_object(data)
        except (ValueError, UnicodeError):
            raise _denied() from None
        if response.get("idempotency_key") != intent.idempotency_key or response.get("platform_retry_authorized") is not False:
            raise _denied()
        return data

    def acknowledge_dashboard_pending_write(self, *, scope: str, idempotency_key: str) -> str:
        self._identity()
        answer = self._call("dashboard_completed_ack", scope=scope, key=idempotency_key)
        if set(answer) != {"phase"} or type(answer["phase"]) is not str or answer["phase"] not in {"acknowledged", "finalized"}:
            raise _denied()
        return answer["phase"]