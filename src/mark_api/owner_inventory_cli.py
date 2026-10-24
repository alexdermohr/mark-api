"""Explicit, read-only private-browser inventory import through UID50046.

This is an independent storage-owner *inventory* worker, not a Write API or a
product launcher. It never opens a SQLite path, changes a platform listing,
attests browser write-completion, or clears a recovery fence. A compromised
browser executor can forge observations; observations carry only source
provenance and must NEVER authorize platform writes.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Callable, Protocol

from .container_draft import container_draft_denied
from .domain import AdSnapshot
from .private_web_runtime import build_private_web_inventory_runtime
from .results import ReadResult, ReadStatus
from .fence_witness import _canonical_json
from .storage_owner import (
    StorageOwnerClient, StorageOwnerError, _MAX_FRAME, _VERSION,
    _snapshot_as_json, _snapshot_from_json,
)


_SOURCE = "private-web-owner-inventory"


class OwnerInventorySyncError(RuntimeError):
    """Classified failure; never infer permission to retry from an exception."""

    def __init__(
        self, message: str, *, kind: str = "unknown",
        attempt_id: int | None = None, read_status: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.attempt_id = attempt_id
        self.read_status = read_status
        self.cleanup_failed = False


class _InventoryRuntime(Protocol):
    def read_inventory(self) -> ReadResult[tuple[AdSnapshot, ...]]: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class OwnerInventoryReceipt:
    attempt_id: int
    observed_count: int
    persisted_count: int
    source: str = _SOURCE


def _now(clock: Callable[[], datetime]) -> datetime:
    result = clock()
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise OwnerInventorySyncError("timezone-aware inventory clock required")
    return result


def _read_status(result: object) -> tuple[ReadResult[tuple[AdSnapshot, ...]], int]:
    if not isinstance(result, ReadResult) or not isinstance(result.status, ReadStatus):
        raise OwnerInventorySyncError(
            "private inventory observation is invalid", kind="invalid_inventory",
        )
    if not result.is_success:
        return result, 0
    if result.status is ReadStatus.SUCCESS_EMPTY and result.value is None:
        snapshots: tuple[AdSnapshot, ...] = ()
    elif isinstance(result.value, tuple):
        snapshots = result.value
    else:
        raise OwnerInventorySyncError(
            "private inventory observation is not a tuple", kind="invalid_inventory",
        )
    if (result.status is ReadStatus.SUCCESS_EMPTY and snapshots) or (
        result.status is ReadStatus.SUCCESS_NONEMPTY and not snapshots
    ):
        raise OwnerInventorySyncError(
            "private inventory status contradicts records", kind="invalid_inventory",
        )
    if len(snapshots) > 2048:
        raise OwnerInventorySyncError(
            "private inventory exceeds owner RPC bound", kind="invalid_inventory",
        )
    ids: set[str] = set()
    for item in snapshots:
        try:
            # Dry-run the EXACT receiver's schema/field validation before
            # sending ANY sync_stage. AdSnapshot itself permits values outside
            # the owner protocol (e.g. dict titles or 64-bit counter overflow).
            wire = _snapshot_as_json(item)
            _snapshot_from_json(wire)
            # A valid field can still exceed the bounded wire frame after
            # ASCII escaping. The real client chunks only after this check.
            frame = _canonical_json({
                "version": _VERSION, "op": "sync_stage",
                "attempt": 2**63 - 1, "offset": 2048,
                "snapshots": [wire],
            }).encode("utf-8")
            if len(frame) + 1 > _MAX_FRAME + 1:
                raise ValueError("inventory entry exceeds bounded IPC frame")
            if item.ad_id in ids:
                raise ValueError("inventory contains duplicate IDs")
            ids.add(item.ad_id)
        except (StorageOwnerError, TypeError, ValueError, UnicodeError, OverflowError):
            raise OwnerInventorySyncError(
                "private inventory record exceeds owner protocol contract",
                kind="invalid_inventory",
            ) from None
    return result, len(snapshots)


def sync_owner_inventory(
    owner: StorageOwnerClient,
    *,
    cdp_port: int,
    timeout_seconds: float = 5.0,
    runtime_factory: Callable[..., _InventoryRuntime] = build_private_web_inventory_runtime,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> OwnerInventoryReceipt:
    """One journaled observation, with no retry after an unknown storage ACK.

    A previous claim is never re-created. Preflight-invalid browser data is
    terminally FAILED before any chunk is transmitted. Once any stage or
    finish RPC is sent, its response can be lost after commit; report UNKNOWN
    without retrying or claiming a successful empty observation.
    """
    if not isinstance(owner, StorageOwnerClient):
        raise TypeError("the UID-attested StorageOwnerClient is required")
    if type(cdp_port) is not int or not 1 <= cdp_port <= 65535:
        raise ValueError("CDP port must be a positive TCP port")
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
        raise ValueError("inventory timeout must be between 0 and 30 seconds")
    if not callable(runtime_factory) or not callable(clock):
        raise TypeError("inventory runtime and clock must be callable")
    if not owner.ready():
        raise OwnerInventorySyncError("independent SQLite owner is not ready", kind="storage_unavailable")
    attempt = owner.begin_sync_attempt(source=_SOURCE, started_at=_now(clock))
    runtime: _InventoryRuntime | None = None
    primary_error: BaseException | None = None

    def record_failure(kind: str) -> None:
        try:
            owner.fail_sync_attempt(
                attempt, source=_SOURCE, completed_at=_now(clock), error_kind=kind,
            )
        except Exception as exc:
            raise OwnerInventorySyncError(
                "owner inventory failure acknowledgement uncertain",
                kind="storage_unknown", attempt_id=attempt,
            ) from exc

    try:
        try:
            runtime = runtime_factory(cdp_port=cdp_port, timeout_seconds=float(timeout_seconds))
        except Exception as exc:
            record_failure("runtime_unavailable")
            raise OwnerInventorySyncError(
                "private inventory runtime unavailable",
                kind="runtime_unavailable", attempt_id=attempt,
            ) from exc
        try:
            result = runtime.read_inventory()
        except Exception as exc:
            record_failure("reader_exception")
            raise OwnerInventorySyncError(
                "private inventory observation failed",
                kind="reader_exception", attempt_id=attempt,
            ) from exc
        try:
            checked, observed = _read_status(result)
        except OwnerInventorySyncError as exc:
            record_failure("invalid_inventory")
            raise OwnerInventorySyncError(
                str(exc), kind="invalid_inventory", attempt_id=attempt,
            ) from exc
        if not checked.is_success:
            try:
                owner.append_inventory_result(
                    checked, observed_at=_now(clock), source=_SOURCE,
                    attempt_id=attempt, completed_at=_now(clock),
                )
            except Exception as exc:
                raise OwnerInventorySyncError(
                    "owner inventory failure acknowledgement uncertain",
                    kind="storage_unknown", attempt_id=attempt,
                ) from exc
            raise OwnerInventorySyncError(
                "private inventory read unsuccessful: " + checked.status.value,
                kind="read_failed", attempt_id=attempt,
                read_status=checked.status.value,
            )
        try:
            persisted = owner.append_inventory_result(
                checked, observed_at=_now(clock), source=_SOURCE,
                attempt_id=attempt, completed_at=_now(clock),
            )
        except Exception as exc:
            raise OwnerInventorySyncError(
                "owner inventory commit outcome uncertain; no automatic retry",
                kind="storage_unknown", attempt_id=attempt,
            ) from exc
        return OwnerInventoryReceipt(
            attempt_id=attempt, observed_count=observed, persisted_count=persisted,
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if runtime is not None:
            try:
                runtime.close()
            except Exception as exc:
                if primary_error is None:
                    # An ACKed SQLite commit stays ACKed even if local browser
                    # cleanup fails. Never convert this into retry authority.
                    failure = OwnerInventorySyncError(
                        "owner inventory committed but cleanup failed; no retry",
                        kind="cleanup_after_commit", attempt_id=attempt,
                    )
                    failure.cleanup_failed = True
                    raise failure from exc
                # Keep UNKNOWN or classified read as primary outcome, with a
                # sanitized secondary cleanup flag for the operator's CLI.
                if isinstance(primary_error, OwnerInventorySyncError):
                    primary_error.cleanup_failed = True
                primary_error.add_note(
                    "private inventory cleanup also failed; no automatic retry"
                )


def main(argv: list[str] | None = None) -> int:
    if container_draft_denied():
        print("mark-api: unattested container draft cannot run Python entrypoints",
              file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="One private-Web read-only inventory sync via independent UID50046 SQLite owner")
    parser.add_argument("--storage-socket", type=Path, required=True)
    parser.add_argument("--storage-owner-uid", type=int, required=True)
    parser.add_argument("--cdp-port", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    if os.geteuid() == args.storage_owner_uid or args.storage_owner_uid <= 0:
        parser.error("inventory reader and SQLite owner must have distinct positive UIDs")
    try:
        owner = StorageOwnerClient(
            args.storage_socket, expected_owner_uid=args.storage_owner_uid,
        )
        receipt = sync_owner_inventory(
            owner, cdp_port=args.cdp_port, timeout_seconds=args.timeout_seconds,
        )
    except OwnerInventorySyncError as exc:
        print(json.dumps({
            "status": "failed", "kind": exc.kind,
            "attempt_id": exc.attempt_id,
            "read_status": exc.read_status,
            "cleanup_failed": exc.cleanup_failed,
            "platform_retry_authorized": False,
        }, sort_keys=True), file=sys.stderr)
        return 2
    except (ValueError, TypeError, OSError, RuntimeError):
        print(json.dumps({
            "status": "failed", "kind": "storage_unavailable_or_unknown",
            "attempt_id": None, "read_status": None,
            "cleanup_failed": False, "platform_retry_authorized": False,
        }, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps({
        "status": "success", "attempt_id": receipt.attempt_id,
        "observed": receipt.observed_count, "persisted": receipt.persisted_count,
        "source": receipt.source, "platform_write": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())