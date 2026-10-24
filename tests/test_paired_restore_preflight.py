"""Offline-only paired SQLite/Witness restore preflight; never a release approval.

A trusted operator must pin the manifest digest outside both restored volumes.
No test invokes a real Kleinanzeigen adapter, clears a fence or restores files.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mark_api.domain import (
    AdSnapshot, CreateOperationReceipt, LifecycleState,
    MediaPostReadStatus, OperationOutcome,
)
from mark_api.fence_witness import FenceLedger, FenceWitnessError
from mark_api.paired_restore_preflight import (
    PairPreflightError, inspect_offline_pair, verify_offline_pair, main,
    _witness_owner_absent,
)
from mark_api.storage import SnapshotStore


GENERATION = "7c26d1ed1bed48c6a5a2f4d3c3cb5208"
SHA = "a" * 64
KEY = "pair:write:one"
AT = datetime(2026, 10, 13, 16, 0, tzinfo=timezone.utc)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


class PairedRestorePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mark-pair-restore-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / "trusted-offline-copies"
        self.source.mkdir(mode=0o700)
        self.sqlite = self.source / "mark.sqlite"
        self.ledger = self.source / "witness.sqlite"
        self.manifest = self.source / "manifest.json"
        SnapshotStore(self.sqlite)
        with FenceLedger(self.ledger, initialize=True):
            pass

    def _start(
        self, *, key: str = KEY, sha: str = SHA, witness: bool = True,
        action: str = "create",
    ) -> None:
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        result = store.claim_write_api_request(
            idempotency_key=key, request_sha256=sha,
            claim_owner="pair-owner", requested_at=AT,
        )
        self.assertTrue(result.created)
        store.begin_write_api_request(
            idempotency_key=key, request_sha256=sha,
            claim_owner="pair-owner", execution_started_at=AT,
            strict_owner_fence=True,
        )
        if witness:
            with FenceLedger(self.ledger) as owner:
                session = owner.open_session()
                owner.execution_started(
                    session, key=key, sha=sha, action=action,
                )
                owner.seal_session(session)

    def _rewrite_witness_events(
        self, *, order: tuple[str, ...] | None = None,
        started_key: str | None = None,
        legacy_media_completion: str | None = None,
        legacy_media_response: dict[str, object] | None = None,
        forged_completed_outcome: str | None = None,
        forged_started_timestamp: str | None = None,
        forged_started_action: str | None = None,
        extra_started_key: str | None = None,
    ) -> None:
        """Test-only adversary reconstructs a self-consistent event hash chain.

        This is explicitly *not* an authorized Witness mutation API. The
        trusted checker must reject states that could not be produced by
        FenceLedger even if every event hash matches.
        """
        with sqlite3.connect(self.ledger) as connection:
            events = [
                list(row) for row in connection.execute(
                    "SELECT session_id,kind,request_key,request_sha256,action,"
                    "outcome,response_sha256,evidence_reference,created_at "
                    "FROM events ORDER BY seq"
                )
            ]
            if order is not None:
                self.assertCountEqual([row[1] for row in events], order)
                events.sort(key=lambda row: order.index(row[1]))
            if forged_completed_outcome is not None:
                self.assertIn(forged_completed_outcome, ("confirmed", "precondition_failed"))
                completed = [row for row in events if row[1] == "completed"]
                self.assertEqual(len(completed), 1)
                completed[0][5] = forged_completed_outcome
            if started_key is not None:
                found = False
                for row in events:
                    if row[1] == "started":
                        row[2] = started_key
                        found = True
                self.assertTrue(found)
            if forged_started_timestamp is not None:
                started = [row for row in events if row[1] == "started"]
                self.assertEqual(len(started), 1)
                started[0][8] = forged_started_timestamp
            if forged_started_action is not None:
                started = [row for row in events if row[1] == "started"]
                self.assertEqual(len(started), 1)
                started[0][4] = forged_started_action
            if extra_started_key is not None:
                previous_start = next(row for row in events if row[1] == "started")
                seal_index = next(i for i, row in enumerate(events) if row[1] == "sealed")
                events.insert(seal_index, [
                    previous_start[0], "started", extra_started_key,
                    previous_start[3], previous_start[4], "", "", "",
                    previous_start[8],
                ])
            if legacy_media_completion is not None:
                # Forge an otherwise hash-consistent historical media sequence.
                self.assertIn(legacy_media_completion, ("confirmed", "ambiguous"))
                start = next(row for row in events if row[1] == "started")
                start[4] = "create_media"
                sid, _, key, sha, _, _, _, _, observed = start
                completion_sha = (
                    hashlib.sha256(_canonical(legacy_media_response)).hexdigest()
                    if legacy_media_response is not None else SHA
                )
                completed = [
                    sid, "completed", key, sha, "", legacy_media_completion,
                    completion_sha, "", observed,
                ]
                runtime = [
                    sid, "runtime_cleared", key, sha, "", "", "",
                    "synthetic:untrusted-runtime", observed,
                ]
                seal_index = next(i for i, row in enumerate(events) if row[1] == "sealed")
                events[seal_index:seal_index] = [completed, runtime]
            connection.execute("DROP TRIGGER events_no_update")
            connection.execute("DROP TRIGGER events_no_delete")
            connection.execute("DELETE FROM events")
            previous = "0" * 64
            for seq, values in enumerate(events, 1):
                material = [item if item is not None else "" for item in values]
                material.append(previous)
                current = hashlib.sha256(_canonical(material)).hexdigest()
                connection.execute(
                    "INSERT INTO events(seq,session_id,kind,request_key,"
                    "request_sha256,action,outcome,response_sha256,"
                    "evidence_reference,created_at,previous_sha256,row_sha256) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (seq, *values, previous, current),
                )
                previous = current
            connection.execute(
                "CREATE TRIGGER events_no_update BEFORE UPDATE ON events "
                "BEGIN SELECT RAISE(ABORT, 'append-only events'); END"
            )
            connection.execute(
                "CREATE TRIGGER events_no_delete BEFORE DELETE ON events "
                "BEGIN SELECT RAISE(ABORT, 'append-only events'); END"
            )
        with sqlite3.connect(self.ledger) as connection:
            # Prove that the forged chain still passes the original
            # cryptographic verifier; the new preflight must supply the
            # independently necessary semantic ordering validation.
            FenceLedger._verify_chain(connection)

    def test_rehashed_witness_second_start_while_first_pending_is_rejected(self) -> None:
        # Witness.execution_started() rejects a second key while ANY older
        # start lacks a post-seal operator clearance, even with a sealed
        # session and otherwise well-formed Hash/SHA/event order.
        first, second = "pending:prior-write", "pending:forged-second-write"
        self._start(key=first)
        with sqlite3.connect(self.sqlite) as db:
            db.execute(
                "INSERT INTO write_api_requests("
                "idempotency_key,request_sha256,state,requested_at,claim_owner,"
                "execution_started_at) VALUES(?,?,'in_progress',?,?,?)",
                (second, SHA, AT.isoformat(), "pair-owner", AT.isoformat()),
            )
        self._rewrite_witness_events(extra_started_key=second)
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_forged_sqlite_clearance_whitespace_evidence_is_not_canonical(self) -> None:
        key = "clearance:whitespace-evidence"
        self._start(key=key)
        SnapshotStore(self.sqlite, create_if_missing=False).record_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
            verification_kind="operator_verified_postread",
            evidence_reference="synthetic:independent-operator", observed_at=AT,
        )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:independent-operator",
            )
        for forged in (" padded-reference ", "\tstart-tab", "end-tab\t"):
            with self.subTest(evidence=forged):
                with sqlite3.connect(self.sqlite) as db:
                    db.execute(
                        "UPDATE write_recovery_clearances SET evidence_reference=? "
                        "WHERE idempotency_key=?", (forged, key),
                    )
                with self.assertRaises(PairPreflightError):
                    inspect_offline_pair(self.sqlite, self.ledger)

    def test_forged_witness_started_timestamp_must_be_runtime_readable(self) -> None:
        # A re-hashed, internally consistent event can still fail the
        # independent operator's pending_page started_at contract (<=64).
        self._start()
        for forged in (
            "2026-10-14T09:00:00+00:00" + "Z" * 65,
            "not-a-datetime",
            "2026-10-14T09:00:00",
            "2026-10-14T09:00:00-04:00",
        ):
            with self.subTest(forged=forged):
                self._rewrite_witness_events(forged_started_timestamp=forged)
                with self.assertRaises(PairPreflightError):
                    inspect_offline_pair(self.sqlite, self.ledger)

    def test_forged_witness_session_timestamp_is_rejected(self) -> None:
        self._start()
        # Session rows are NOT part of the hashed event chain.
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("UPDATE sessions SET opened_at='invalid'")
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def _pin(self, *, generation: str = GENERATION) -> str:
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        manifest = {"schema_version": 5, "generation": generation, **inspected}
        raw = _canonical(manifest)
        self.manifest.write_bytes(raw)
        os.chmod(self.manifest, 0o600)
        return hashlib.sha256(raw).hexdigest()

    def _create_checkpoint(self, *, reference: str = "write-api:paired-create") -> tuple[CreateOperationReceipt, int]:
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        observed = AdSnapshot(
            ad_id="201", observed_at=AT, source="management",
            lifecycle_state=LifecycleState.ACTIVE,
            title="Restore test vase", description="Synthetic item",
        )
        receipt = CreateOperationReceipt(
            operation="create", started_at=AT, completed_at=AT,
            outcome=OperationOutcome.CONFIRMED,
            pre_read_status="success_empty",
            confirmation_pre_read_status="success_empty",
            post_read_status="success_nonempty",
            confirmation_post_read_status="success_nonempty",
            content_post_read_status="success_nonempty",
            writer_invoked=True, created_ad_id="201",
            authorization_by="offline-test-owner",
            authorization_reference=reference,
            post_snapshot=observed,
            confirmation_post_snapshot=observed,
            content_post_snapshot=observed,
        )
        checkpoint_id = store.append_create_operation_checkpoint(receipt)
        return receipt, checkpoint_id

    def _ambiguous_final_create(
        self, *, reference: str = "write-api:media-unverified",
        media_status: MediaPostReadStatus | None = MediaPostReadStatus.UNKNOWN,
    ) -> int:
        """Browser wrote an ambiguous final media receipt without any checkpoint."""
        receipt = CreateOperationReceipt(
            operation="create", started_at=AT, completed_at=AT,
            outcome=OperationOutcome.AMBIGUOUS,
            pre_read_status="success_empty",
            confirmation_pre_read_status="success_empty",
            post_read_status="unknown",
            confirmation_post_read_status="unknown",
            content_post_read_status="unknown",
            writer_invoked=True, created_ad_id=None,
            authorization_by="offline-test-owner",
            authorization_reference=reference,
            media_post_read_status=media_status,
            media_persistence_confirmed=False,
        )
        SnapshotStore(self.sqlite, create_if_missing=False).append_create_operation_receipt(receipt)
        with sqlite3.connect(self.sqlite) as connection:
            return int(connection.execute(
                "SELECT MAX(id) FROM create_operation_receipts",
            ).fetchone()[0])

    def test_sqlite_operator_clearance_invalid_observed_at_fails_before_attestation(self) -> None:
        # A forged SQLite clearance can match the operation SHA and have a
        # separate sealed Witness operator clearance, yet still be unreadable
        # by the runtime's strict timezone-aware _sync_time contract.
        key = "clearance:bad-observed-at"
        self._start(key=key)
        SnapshotStore(self.sqlite, create_if_missing=False).record_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
            verification_kind="operator_verified_postread",
            evidence_reference="synthetic:independent-operator", observed_at=AT,
        )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:independent-operator",
            )
        for forged in (
            "broken",
            "2026-10-14T11:17:00",
            "2026-10-14T11:17:00-04:00",
            "2026-10-14T11:17:00+00:00" + "X" * 65,
        ):
            with self.subTest(observed_at=forged):
                with sqlite3.connect(self.sqlite) as connection:
                    connection.execute(
                        "UPDATE write_recovery_clearances SET observed_at=? "
                        "WHERE idempotency_key=?", (forged, key),
                    )
                with self.assertRaises(PairPreflightError):
                    inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_operator_clearance_without_sqlite_clearance_in_progress_fails(self) -> None:
        key = "inprogress:witness-operator-only"
        self._start(key=key)
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:operator-on-witness",
            )
        # The SQLite in_progress record remains recovery-blocking. A valid
        # digest-paired start alone must not make that one-sided clearance safe.
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_sqlite_only_in_progress_operator_clearance_is_rejected(self) -> None:
        key = "inprogress:sqlite-only-operator"
        self._start(key=key)
        SnapshotStore(self.sqlite, create_if_missing=False).record_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
            verification_kind="operator_verified_postread",
            evidence_reference="synthetic:only-sqlite-clear", observed_at=AT,
        )
        # Independent Witness remains unresolved: the SQLite-side clearance
        # alone must never suppress a Write recovery fence in an attested pair.
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_sqlite_only_in_progress_executor_clearance_is_rejected(self) -> None:
        key = "inprogress:sqlite-only-executor"
        self._start(key=key)
        SnapshotStore(self.sqlite, create_if_missing=False).record_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
            verification_kind="media_submit_reconciled",
            evidence_reference="synthetic:executor-only", observed_at=AT,
        )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_dual_operator_clearances_with_in_progress_request_are_recoverable(self) -> None:
        key = "inprogress:dual-operator-clearance"
        self._start(key=key)
        SnapshotStore(self.sqlite, create_if_missing=False).record_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
            verification_kind="operator_verified_postread",
            evidence_reference="synthetic:external-postread", observed_at=AT,
        )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:external-postread",
            )
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(inspected["fences"]["witness_unresolved"], [])
        self.assertEqual(inspected["fences"]["sqlite_unverified_replay"], [])

    def test_forged_clearance_for_never_executed_write_cannot_hide_media_fence(self) -> None:
        # Attack: staged pair has a valid but unstarted request, a separate
        # ambiguous media receipt, and an unauthorized forged clearance. The
        # restore inspector must not trust the SQLite join to suppress it.
        key = "media:unstarted-forged-clearance"
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        claim = store.claim_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            claim_owner="pair-owner", requested_at=AT,
        )
        self.assertTrue(claim.created)
        receipt_id = self._ambiguous_final_create(reference="write-api:" + key)
        with self.assertRaisesRegex(ValueError, "never executed"):
            store.record_write_recovery_clearance(
                idempotency_key=key, request_sha256=SHA,
                verification_kind="operator_verified_postread",
                evidence_reference="synthetic:unauthorized-claim", observed_at=AT,
            )
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "INSERT INTO write_recovery_clearances "
                "(idempotency_key,request_sha256,observed_at,"
                "verification_kind,evidence_reference) VALUES (?,?,?,?,?)",
                (key, SHA, AT.isoformat(), "operator_verified_postread",
                 "forged:unstarted"),
            )
            self.assertIsNone(connection.execute(
                "SELECT execution_started_at FROM write_api_requests "
                "WHERE idempotency_key=?", (key,),
            ).fetchone()[0])
        self.assertGreater(receipt_id, 0)
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_orphan_clearance_without_a_matching_write_is_rejected(self) -> None:
        # No request exists, but the clearance table lacks an FK.
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "INSERT INTO write_recovery_clearances "
                "(idempotency_key,request_sha256,observed_at,"
                "verification_kind,evidence_reference) VALUES (?,?,?,?,?)",
                ("orphan:fake", SHA, AT.isoformat(),
                 "operator_verified_postread", "forged:orphan"),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_clearance_fingerprint_mismatch_is_rejected(self) -> None:
        # Real started requests exist in BOTH ledgers. An additional SQL-only
        # clearance for that key with a different digest remains invalid.
        self._start()
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "INSERT INTO write_recovery_clearances "
                "(idempotency_key,request_sha256,observed_at,"
                "verification_kind,evidence_reference) VALUES (?,?,?,?,?)",
                (KEY, "b" * 64, AT.isoformat(),
                 "operator_verified_postread", "forged:digest"),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_executor_clearance_cannot_settle_owner_unknown(self) -> None:
        key = "unknown:forged-media-clearance"
        self._start(key=key)
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        store.complete_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            claim_owner="pair-owner", response_status=202,
            response_json=SnapshotStore._unverified_write_api_response(key),
            completed_at=AT,
        )
        with self.assertRaisesRegex(ValueError, "independent operator"):
            store.record_write_recovery_clearance(
                idempotency_key=key, request_sha256=SHA,
                verification_kind="media_submit_reconciled",
                evidence_reference="synthetic:executor", observed_at=AT,
            )
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "INSERT INTO write_recovery_clearances "
                "(idempotency_key,request_sha256,observed_at,"
                "verification_kind,evidence_reference) VALUES (?,?,?,?,?)",
                (key, SHA, AT.isoformat(),
                 "media_submit_reconciled", "forged:executor"),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_forged_clearance_for_confirmed_media_is_rejected(self) -> None:
        key = "media:confirmed-no-recovery"
        self._persist_media_http_response(
            key=key, status=200,
            envelope={
                "media_persistence_confirmed": True,
                "operation_receipt": {
                    "outcome": "confirmed", "writer_invoked": True,
                },
            },
        )
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "INSERT INTO write_recovery_clearances "
                "(idempotency_key,request_sha256,observed_at,"
                "verification_kind,evidence_reference) VALUES (?,?,?,?,?)",
                (key, SHA, AT.isoformat(),
                 "operator_verified_postread", "forged:confirmed"),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def _completed_response_pair(
        self, *, key: str, response: dict[str, object],
        status: int = 500, action: str = "create",
    ) -> None:
        """Exactly one durable SQL completion then same-object Witness completion."""
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        claim = store.claim_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            claim_owner="pair-owner", requested_at=AT,
        )
        self.assertTrue(claim.created)
        store.begin_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            claim_owner="pair-owner", execution_started_at=AT,
            strict_owner_fence=True,
        )
        with FenceLedger(self.ledger) as owner:
            session = owner.open_session()
            owner.execution_started(
                session, key=key, sha=SHA, action=action,
            )
            store.complete_write_api_request(
                idempotency_key=key, request_sha256=SHA,
                claim_owner="pair-owner", response_status=status,
                response_json=json.dumps(
                    response, sort_keys=True, ensure_ascii=False,
                    separators=(",", ":"),
                ),
                completed_at=AT,
            )
            outcome = owner.execution_completed(
                session, key=key, sha=SHA, response=response,
            )
            self.assertEqual(outcome, "ambiguous")
            owner.seal_session(session)

    def test_media_completed_response_requires_matching_witness_action(self) -> None:
        # RED: a hash-consistent forged Witness may claim any allowed action
        # while SQLite's canonical HTTP payload unambiguously says Create+Media.
        key = "media:forged-witness-action"
        response = {
            "error": "write_execution_error",
            "idempotency_key": key,
            "media_persistence_confirmed": False,
            "platform_retry_authorized": False,
        }
        self._completed_response_pair(
            key=key, response=response, action="create_media",
        )
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"]["witness_unresolved"],
            [[key, SHA]],
        )
        self._rewrite_witness_events(forged_started_action="delete")
        with self.assertRaisesRegex(PairPreflightError, "witness action"):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_media_uncompleted_response_requires_matching_witness_action(self) -> None:
        # Crash before Witness completion must not let a forged action pass.
        key = "media:uncompleted-forged-action"
        self._start(key=key)
        SnapshotStore(self.sqlite, create_if_missing=False).complete_write_api_request(
            idempotency_key=key, request_sha256=SHA, claim_owner="pair-owner",
            response_status=500,
            response_json=_canonical({
                "error": "write_execution_error",
                "idempotency_key": key,
                "media_persistence_confirmed": False,
                "platform_retry_authorized": False,
            }).decode("ascii"),
            completed_at=AT,
        )
        self._rewrite_witness_events(forged_started_action="activate")
        with self.assertRaisesRegex(PairPreflightError, "witness action"):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_oversized_owner_write_record_reply_is_not_restorable(self) -> None:
        # A canonical stored HTTP response may fit SQLite but not the
        # owner's actual claim/read/replay response envelope.
        key = "complete:oversized-owner-reply"
        self._start(key=key)
        SnapshotStore(self.sqlite, create_if_missing=False).complete_write_api_request(
            idempotency_key=key, request_sha256=SHA, claim_owner="pair-owner",
            response_status=500,
            response_json=_canonical({
                "error": "write_execution_error", "detail": "x" * 40000,
            }).decode("ascii"),
            completed_at=AT,
        )
        # This oversized response cannot cross Witness completion either,
        # so the valid paired case retains the original unresolved start.
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_bounded_owner_write_record_reply_remains_restorable(self) -> None:
        key = "complete:bounded-owner-reply"
        self._completed_response_pair(
            key=key, response={"error": "write_execution_error",
                               "detail": "x" * 28000},
        )
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"]["witness_unresolved"],
            [[key, SHA]],
        )

    def test_owner_write_reply_exact_frame_edge(self) -> None:
        # The byte-bound is checked on the *complete* Owner response. Its
        # last admissible byte is replayable; the next byte must fail closed.
        from mark_api.storage_owner import _record_as_json, _reply_frame_fits

        key = "complete:exact-owner-frame"
        self._start(key=key)
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        started = store.write_api_request(key)
        self.assertIsNotNone(started)
        assert started is not None

        def response(length: int) -> str:
            return json.dumps(
                {"detail": "x" * length, "error": "write_execution_error"},
                sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            )

        def envelope(length: int) -> dict[str, object]:
            completed = replace(
                started, state="completed", completed_at=AT,
                response_status=500, response_json=response(length),
            )
            return {"created": False, "record": _record_as_json(completed)}

        lower, upper = 0, 40000
        while lower < upper:
            candidate = (lower + upper + 1) // 2
            if _reply_frame_fits(envelope(candidate)):
                lower = candidate
            else:
                upper = candidate - 1
        boundary = lower
        self.assertTrue(_reply_frame_fits(envelope(boundary)))
        self.assertFalse(_reply_frame_fits(envelope(boundary + 1)))
        self.assertEqual(
            len(_canonical({"ok": True, "result": envelope(boundary)}) + b"\n"),
            32 * 1024 + 1,
        )

        store.complete_write_api_request(
            idempotency_key=key, request_sha256=SHA, claim_owner="pair-owner",
            response_status=500, response_json=response(boundary),
            completed_at=AT,
        )
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"][
                "witness_unresolved"
            ],
            [[key, SHA]],
        )
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET response_json=? "
                "WHERE idempotency_key=?",
                (response(boundary + 1), key),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_invalid_completed_http_status_is_not_restorable(self) -> None:
        key = "complete:bad-http"
        response = {"error": "write_execution_error"}
        self._completed_response_pair(key=key, response=response)
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET response_status=700 "
                "WHERE idempotency_key=?", (key,),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_invalid_claim_owner_is_not_restorable(self) -> None:
        self._start(key="request:bad-owner")
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET claim_owner='invalid owner' "
                "WHERE idempotency_key='request:bad-owner'",
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_noncanonical_completed_response_is_not_restorable(self) -> None:
        key = "complete:json-order"
        self._completed_response_pair(
            key=key, response={"error": "write_execution_error", "kind": 1},
        )
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET response_json=? "
                "WHERE idempotency_key=?",
                ('{"kind": 1, "error":"write_execution_error"}', key),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_completed_response_digest_divergence_is_not_restorable(self) -> None:
        key = "complete:diverged"
        self._completed_response_pair(
            key=key, response={"error": "write_execution_error"},
        )
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET response_json=? "
                "WHERE idempotency_key=?",
                ('{"error":"write_execution_error_changed"}', key),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_matching_witness_completion_with_unicode_response_is_valid(self) -> None:
        key = "complete:unicode"
        self._completed_response_pair(
            key=key, response={"error": "write_execution_error",
                               "detail": "Überprüfung"},
        )
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(inspected["fences"]["witness_unresolved"], [[key, SHA]])

    def test_crash_before_witness_completion_remains_explicitly_fenced(self) -> None:
        key = "complete:crashed-after-sqlite"
        self._persist_media_http_response(
            key=key, status=500,
            envelope={
                "media_persistence_confirmed": False,
                "operation_receipt": None,
            },
        )
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(inspected["fences"]["witness_unresolved"], [[key, SHA]])
        self.assertEqual(inspected["fences"]["sqlite_media_unresolved"], [[key, SHA]])
        self.assertEqual(
            inspected["fences"]["sqlite_unverified_replay"][0][:2],
            [key, SHA],
        )
        digest = self._pin()
        result = verify_offline_pair(
            self.sqlite, self.ledger, self.manifest,
            expected_manifest_sha256=digest,
        )
        self.assertEqual(result["sqlite_unverified_replay_count"], 1)
        self.assertFalse(result["restore_authorized"])

    def test_ambiguous_final_media_receipt_without_checkpoint_is_manifested(self) -> None:
        receipt_id = self._ambiguous_final_create()
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        self.assertTrue(store.create_recovery_pending(
            exclude_idempotency_key="new", claim_owner="another-runtime",
        ))
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(inspected["fences"]["ambiguous_creates"], [])
        self.assertEqual(inspected["fences"]["ambiguous_final_creates"], [receipt_id])
        pinned = self._pin()
        receipt = verify_offline_pair(
            self.sqlite, self.ledger, self.manifest,
            expected_manifest_sha256=pinned,
        )
        self.assertEqual(receipt["ambiguous_create_count"], 1)
        self.assertEqual(receipt["ambiguous_final_create_count"], 1)

    def test_nonmedia_ambiguous_final_create_is_not_media_recovery_fence(self) -> None:
        self._ambiguous_final_create(media_status=None)
        self.assertFalse(SnapshotStore(
            self.sqlite, create_if_missing=False,
        ).create_recovery_pending(
            exclude_idempotency_key="new", claim_owner="another-runtime",
        ))
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"]["ambiguous_final_creates"], [],
        )

    def test_unrelated_operator_clearance_cannot_hide_ambiguous_final_receipt(self) -> None:
        receipt_id = self._ambiguous_final_create(reference="write-api:unrelated")
        self._start(key="separate-write")
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        store.record_write_recovery_clearance(
            idempotency_key="separate-write", request_sha256=SHA,
            verification_kind="operator_verified_postread",
            evidence_reference="synthetic-only:operator-outcome", observed_at=AT,
        )
        self.assertTrue(store.create_recovery_pending(
            exclude_idempotency_key="new", claim_owner="another-runtime",
        ))
        # SQL-only clearance is a legitimate intermediate operator step,
        # but NOT yet a valid jointly attested recovery generation.
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key="separate-write", sha=SHA,
                evidence="synthetic-only:operator-outcome",
            )
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"]["ambiguous_final_creates"],
            [receipt_id],
        )

    def test_exact_cleared_final_receipt_no_longer_adds_unresolved_media_fence(self) -> None:
        self._start(key="media-exact")
        self._ambiguous_final_create(reference="write-api:media-exact")
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        store.record_write_recovery_clearance(
            idempotency_key="media-exact", request_sha256=SHA,
            verification_kind="operator_verified_postread",
            evidence_reference="synthetic-only:independent-review", observed_at=AT,
        )
        self.assertFalse(store.create_recovery_pending(
            exclude_idempotency_key="new", claim_owner="another-runtime",
        ))
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key="media-exact", sha=SHA,
                evidence="synthetic-only:independent-review",
            )
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(inspected["fences"]["ambiguous_final_creates"], [])
        self.assertEqual(inspected["fences"]["witness_unresolved"], [])

    def test_old_v4_manifest_is_rejected_after_replay_fence_upgrade(self) -> None:
        digest = self._pin()
        claimed = json.loads(self.manifest.read_bytes())
        self.assertEqual(claimed["schema_version"], 5)
        claimed["schema_version"] = 4
        downgraded = _canonical(claimed)
        self.manifest.write_bytes(downgraded)
        with self.assertRaises(PairPreflightError):
            verify_offline_pair(
                self.sqlite, self.ledger, self.manifest,
                expected_manifest_sha256=hashlib.sha256(downgraded).hexdigest(),
            )
        self.assertNotEqual(hashlib.sha256(downgraded).hexdigest(), digest)

    def test_witness_completed_response_without_sqlite_completion_is_rejected(self) -> None:
        key = "complete:lost-sqlite"
        self._completed_response_pair(
            key=key, response={"error": "write_execution_error"},
        )
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET state='in_progress',"
                "completed_at=NULL,response_status=NULL,response_json=NULL "
                "WHERE idempotency_key=?", (key,),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def _nonmedia_crash_without_witness_completion(
        self, *, key: str, status: int = 500,
        extra: dict[str, object] | None = None,
    ) -> None:
        self._start(key=key)
        envelope = {
            "error": "write_execution_error",
            "idempotency_key": key,
            "platform_retry_authorized": False,
        }
        if extra:
            envelope.update(extra)
        SnapshotStore(self.sqlite, create_if_missing=False).complete_write_api_request(
            idempotency_key=key, request_sha256=SHA, claim_owner="pair-owner",
            response_status=status, response_json=_canonical(envelope).decode("ascii"),
            completed_at=AT,
        )

    def test_nonmedia_500_without_witness_completion_after_operator_clearance_remains_unverified(self) -> None:
        # A completed non-media HTTP 500 does not have a SQLite recovery
        # clearance in SnapshotStore's supported protocol. Only the
        # independent Witness needs its original start operator-cleared.
        # Neither the local reply nor the restored manifest is safe replay.
        key = "complete:nonmedia-crash-500"
        self._nonmedia_crash_without_witness_completion(key=key)
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:independent-nonmedia-postread",
            )
        report = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(report["fences"]["witness_unresolved"], [])
        self.assertEqual(report["fences"]["sqlite_unverified_replay"][0][:2], [key, SHA])
        digest = self._pin()
        verified = verify_offline_pair(
            self.sqlite, self.ledger, self.manifest,
            expected_manifest_sha256=digest,
        )
        self.assertEqual(verified["sqlite_unverified_replay_count"], 1)
        self.assertFalse(verified["restore_authorized"])
        with FenceLedger(self.ledger) as reopened:
            self.assertEqual(reopened.replay_status(
                key=key, sha=SHA, response_sha=hashlib.sha256(
                    _canonical({
                        "error": "write_execution_error",
                        "idempotency_key": key,
                        "platform_retry_authorized": False,
                    })
                ).hexdigest(),
            ), "unverified")

    def test_nonmedia_500_with_extra_response_field_remains_unverified(self) -> None:
        # The independent Witness clearance, not the executor's asserted
        # response shape, is the external reconciliation authority. Canonical
        # non-media JSON stays permanently unverified after this crash.
        key = "complete:nonmedia-extra-envelope"
        self._nonmedia_crash_without_witness_completion(
            key=key, extra={"debug": "unexpected"},
        )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:independent-nonmedia-postread",
            )
        report = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(
            report["fences"]["sqlite_unverified_replay"][0][:2], [key, SHA],
        )

    def test_nonmedia_echoed_media_marker_is_not_an_sqlite_media_fence(self) -> None:
        # Adversarial independent-review P2: legal client-controlled keys can
        # contain the substring used by the old raw SQL media heuristic.
        key = "client:media_persistence_confirmed"
        self._nonmedia_crash_without_witness_completion(key=key)
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        self.assertFalse(store.create_recovery_pending(
            exclude_idempotency_key="another", claim_owner="other-owner",
        ))
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:independent-nonmedia-readback",
            )
        observed = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(observed["fences"]["sqlite_media_unresolved"], [])
        self.assertEqual(observed["fences"]["sqlite_quarantined"], [])
        self.assertEqual(
            observed["fences"]["sqlite_unverified_replay"][0][:2], [key, SHA],
        )

    def test_nonmedia_echoed_payload_media_marker_is_not_media(self) -> None:
        key = "complete:echoed-nonmedia-field"
        self._nonmedia_crash_without_witness_completion(
            key=key, extra={"debug": "media_persistence_confirmed"},
        )
        self.assertFalse(
            SnapshotStore(self.sqlite, create_if_missing=False)
            .create_recovery_pending(
                exclude_idempotency_key="another", claim_owner="other-owner",
            )
        )
        observed = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(observed["fences"]["sqlite_media_unresolved"], [])
        self.assertEqual(
            observed["fences"]["sqlite_unverified_replay"][0][:2], [key, SHA],
        )

    def test_owner_unknown_202_key_with_media_marker_is_only_quarantined(self) -> None:
        key = "client:media_persistence_confirmed"
        self._start(key=key)
        SnapshotStore(self.sqlite, create_if_missing=False).complete_write_api_request(
            idempotency_key=key, request_sha256=SHA, claim_owner="pair-owner",
            response_status=202,
            response_json=SnapshotStore._unverified_write_api_response(key),
            completed_at=AT,
        )
        self.assertTrue(
            SnapshotStore(self.sqlite, create_if_missing=False)
            .create_recovery_pending(
                exclude_idempotency_key="another", claim_owner="other-owner",
            )
        )
        observed = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(
            observed["fences"]["sqlite_quarantined"], [[key, SHA]],
        )
        self.assertEqual(observed["fences"]["sqlite_media_unresolved"], [])

    def test_nonmedia_200_without_witness_completion_never_becomes_verified_replay(self) -> None:
        key = "complete:nonmedia-unwitnessed-success"
        self._nonmedia_crash_without_witness_completion(key=key, status=200)
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:independent-nonmedia-postread",
            )
        report = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(
            report["fences"]["sqlite_unverified_replay"][0][:2], [key, SHA],
        )
        self.assertEqual(report["fences"]["witness_unresolved"], [])
        digest = self._pin()
        receipt = verify_offline_pair(
            self.sqlite, self.ledger, self.manifest,
            expected_manifest_sha256=digest,
        )
        self.assertEqual(receipt["sqlite_unverified_replay_count"], 1)
        self.assertFalse(receipt["restore_authorized"])

    def test_nonmedia_409_without_witness_completion_stays_unverified(self) -> None:
        key = "complete:nonmedia-unwitnessed-precondition"
        self._nonmedia_crash_without_witness_completion(
            key=key, status=409,
            extra={"error": "write_claim_cancelled_before_begin"},
        )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:independent-nonmedia-postread",
            )
        report = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(
            report["fences"]["sqlite_unverified_replay"][0][:2], [key, SHA],
        )

    def test_quarantined_202_without_sqlite_clearance_cannot_use_nonmedia_exception(self) -> None:
        key = "unknown:nonmedia-exclusion"
        self._start(key=key)
        SnapshotStore(self.sqlite, create_if_missing=False).complete_write_api_request(
            idempotency_key=key, request_sha256=SHA, claim_owner="pair-owner",
            response_status=202,
            response_json=SnapshotStore._unverified_write_api_response(key),
            completed_at=AT,
        )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:independent-operator",
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_missing_completion_with_operator_clearance_is_not_safe_replay(self) -> None:
        key = "complete:cleared-without-replay"
        self._persist_media_http_response(
            key=key, status=500,
            envelope={"media_persistence_confirmed": False,
                      "operation_receipt": None},
        )
        with FenceLedger(self.ledger) as owner:
            owner.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:independent-check",
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_completion_plus_operator_cannot_hide_executor_only_media_clearance(self) -> None:
        # Independent reviewer P2: a completed Witness reply exists, so the
        # missing-completion-only check used to skip a forged local settlement.
        key = "media:witness-completion-executor-only"
        response = {
            "media_persistence_confirmed": False,
            "operation_receipt": {"outcome": "ambiguous", "writer_invoked": True},
        }
        self._completed_response_pair(key=key, status=500, response=response, action="create_media")
        SnapshotStore(self.sqlite, create_if_missing=False).record_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
            verification_kind="media_submit_reconciled",
            evidence_reference="synthetic:executor-only-media-observation", observed_at=AT,
        )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:operator-witness-only",
            )
        # A restored generation with this executor-only SQLite settlement
        # would be UNSAFE: actual SnapshotStore.create_recovery_pending()
        # already treats it as cleared and would admit another Create now
        # that Witness was independently operator-cleared. Until those two
        # authorities have a shared production fence protocol, preflight
        # MUST refuse this generation rather than diagnose it as restorable.
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        self.assertFalse(store.create_recovery_pending(
            exclude_idempotency_key="different-key", claim_owner="next-runtime",
        ))
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_matching_witness_completion_with_dual_operator_media_clearance_remains_diagnosable(self) -> None:
        key = "media:completed-dual-operator"
        response = {
            "media_persistence_confirmed": False,
            "operation_receipt": {"outcome": "ambiguous", "writer_invoked": True},
        }
        self._completed_response_pair(key=key, status=500, response=response, action="create_media")
        SnapshotStore(self.sqlite, create_if_missing=False).record_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
            verification_kind="operator_verified_postread",
            evidence_reference="synthetic:independent-operator-check", observed_at=AT,
        )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:independent-operator-check",
            )
        observed = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(observed["fences"]["sqlite_media_unresolved"], [])
        self.assertEqual(observed["fences"]["witness_unresolved"], [])
        self.assertEqual(observed["fences"]["sqlite_unverified_replay"], [])

    def test_executor_clearance_cannot_replace_sqlite_operator_clearance_after_witness_operator(self) -> None:
        # A runtime media_submit_reconciled row is NOT an independent operator
        # clearance. Both authorities must be operator-cleared before a crash
        # between the SQLite reply and missing Witness completion can attest.
        key = "complete:executor-vs-operator"
        self._persist_media_http_response(
            key=key, status=500,
            envelope={"media_persistence_confirmed": False, "operation_receipt": None},
        )
        SnapshotStore(self.sqlite, create_if_missing=False).record_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
            verification_kind="media_submit_reconciled",
            evidence_reference="synthetic:executor-only-media", observed_at=AT,
        )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:independent-postread",
            )
        # Even with independent Witness operator evidence, an already
        # recorded executor-only local clearance is accepted by the *live*
        # runtime and suppresses its Create-fence: the apparent open offline
        # fence would disappear on restart. Reject this pair until runtime
        # and independent Witness share a proven secure release contract.
        self.assertFalse(
            SnapshotStore(self.sqlite, create_if_missing=False)
            .create_recovery_pending(
                exclude_idempotency_key="new-create", claim_owner="next-runtime",
            )
        )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_executor_clearance_without_witness_completion_keeps_witness_pending(self) -> None:
        key = "complete:executor-clearance-witness-pending"
        self._persist_media_http_response(
            key=key, status=500,
            envelope={"media_persistence_confirmed": False, "operation_receipt": None},
        )
        SnapshotStore(self.sqlite, create_if_missing=False).record_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
            verification_kind="media_submit_reconciled",
            evidence_reference="synthetic:executor-only-media", observed_at=AT,
        )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_dual_operator_clearance_with_missing_witness_completion_remains_attested(self) -> None:
        # Real crash window: SQLite reply persisted, Witness completion absent.
        # Later independent clearances in BOTH ledgers settle the write fence,
        # but do not turn the unverified reply into a proven replay.
        key = "complete:dual-operator-clearance"
        self._persist_media_http_response(
            key=key, status=500,
            envelope={"media_persistence_confirmed": False, "operation_receipt": None},
        )
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        store.record_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
            verification_kind="operator_verified_postread",
            evidence_reference="synthetic:independent-platform-check", observed_at=AT,
        )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:independent-platform-check",
            )
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(inspected["fences"]["witness_unresolved"], [])
        self.assertEqual(
            inspected["fences"]["sqlite_unverified_replay"][0][:2], [key, SHA],
        )
        digest = self._pin()
        verified = verify_offline_pair(
            self.sqlite, self.ledger, self.manifest,
            expected_manifest_sha256=digest,
        )
        self.assertEqual(verified["sqlite_unverified_replay_count"], 1)
        self.assertFalse(verified["restore_authorized"])

    def test_missing_witness_completion_with_only_sqlite_clearance_is_rejected(self) -> None:
        key = "complete:sqlite-only-clearance"
        self._persist_media_http_response(
            key=key, status=500,
            envelope={"media_persistence_confirmed": False, "operation_receipt": None},
        )
        SnapshotStore(
            self.sqlite, create_if_missing=False,
        ).record_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
            verification_kind="operator_verified_postread",
            evidence_reference="synthetic:incomplete-clearance", observed_at=AT,
        )
        # The independent Witness has no matching operator clearance.
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_dual_operator_clearance_of_quarantined_202_keeps_replay_unverified(self) -> None:
        key = "unknown:dual-operator-clearance"
        self._start(key=key)
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        store.complete_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            claim_owner="pair-owner", response_status=202,
            response_json=SnapshotStore._unverified_write_api_response(key),
            completed_at=AT,
        )
        store.record_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
            verification_kind="operator_verified_postread",
            evidence_reference="synthetic:operator-reconciliation", observed_at=AT,
        )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:operator-reconciliation",
            )
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(inspected["fences"]["sqlite_quarantined"], [])
        self.assertEqual(inspected["fences"]["witness_unresolved"], [])
        self.assertEqual(
            inspected["fences"]["sqlite_unverified_replay"][0][:2], [key, SHA],
        )

    def test_v1_manifest_not_accepted_after_fence_inventory_schema_upgrade(self) -> None:
        expected = self._pin()
        loaded = json.loads(self.manifest.read_bytes())
        self.assertEqual(loaded["schema_version"], 5)
        loaded["schema_version"] = 1
        older = _canonical(loaded)
        self.manifest.write_bytes(older)
        with self.assertRaises(PairPreflightError):
            verify_offline_pair(
                self.sqlite, self.ledger, self.manifest,
                expected_manifest_sha256=hashlib.sha256(older).hexdigest(),
            )
        self.assertNotEqual(hashlib.sha256(older).hexdigest(), expected)

    def _persist_media_http_response(
        self, *, key: str, status: int, envelope: dict[str, object],
    ) -> None:
        self._start(key=key, action="create_media")
        SnapshotStore(self.sqlite, create_if_missing=False).complete_write_api_request(
            idempotency_key=key, request_sha256=SHA, claim_owner="pair-owner",
            response_status=status,
            response_json=json.dumps(
                envelope, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
            ),
            completed_at=AT,
        )

    def test_http500_missing_media_receipt_is_an_unresolved_restore_fence(self) -> None:
        key = "media:executor-failure"
        self._persist_media_http_response(
            key=key, status=500,
            envelope={"media_persistence_confirmed": False, "operation_receipt": None},
        )
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        self.assertTrue(store.create_recovery_pending(
            exclude_idempotency_key="different-key", claim_owner="separate-owner",
        ))
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(inspected["fences"]["sqlite_quarantined"], [])
        self.assertEqual(inspected["fences"]["ambiguous_final_creates"], [])
        self.assertEqual(inspected["fences"]["sqlite_media_unresolved"], [[key, SHA]])
        manifest_sha = self._pin()
        report = verify_offline_pair(
            self.sqlite, self.ledger, self.manifest,
            expected_manifest_sha256=manifest_sha,
        )
        self.assertEqual(report["sqlite_media_unresolved_count"], 1)
        self.assertEqual(report["ambiguous_create_count"], 1)
        self.assertFalse(report["restore_authorized"])

    def test_ambiguous_media_receipt_in_http500_response_is_fenced(self) -> None:
        key = "media:unknown-500"
        self._persist_media_http_response(
            key=key, status=500,
            envelope={
                "media_persistence_confirmed": False,
                "operation_receipt": {"outcome": "ambiguous", "writer_invoked": True},
            },
        )
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"]["sqlite_media_unresolved"],
            [[key, SHA]],
        )

    def test_valid_confirmed_media_http_response_is_not_a_new_media_fence(self) -> None:
        key = "media:final-confirmed"
        self._persist_media_http_response(
            key=key, status=200,
            envelope={
                "media_persistence_confirmed": True,
                "operation_receipt": {"outcome": "confirmed", "writer_invoked": True},
            },
        )
        self.assertFalse(SnapshotStore(
            self.sqlite, create_if_missing=False,
        ).create_recovery_pending(
            exclude_idempotency_key="different-key", claim_owner="separate-owner",
        ))
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"]["sqlite_media_unresolved"],
            [],
        )

    def test_confirmed_media_crash_can_be_witness_cleared_without_sqlite_clearance(self) -> None:
        # RED: confirmed HTTP 200 persisted before independent Witness
        # completion. After external/operator reconciliation, SnapshotStore
        # correctly rejects an SQLite clearance: no ambiguous local media
        # recovery fence exists. The pair must still be inspectable, while
        # permanently retaining the *unverified* SQLite reply.
        key = "media:confirmed-crash-after-response"
        envelope = {
            "media_persistence_confirmed": True,
            "operation_receipt": {"outcome": "confirmed", "writer_invoked": True},
        }
        self._persist_media_http_response(key=key, status=200, envelope=envelope)
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        with self.assertRaisesRegex(ValueError, "no ambiguous media outcome"):
            store.record_write_recovery_clearance(
                idempotency_key=key, request_sha256=SHA,
                verification_kind="operator_verified_postread",
                evidence_reference="synthetic:independent-confirmed-postread",
                observed_at=AT,
            )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:independent-confirmed-postread",
            )
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(inspected["fences"]["sqlite_media_unresolved"], [])
        self.assertEqual(inspected["fences"]["witness_unresolved"], [])
        self.assertEqual(
            inspected["fences"]["sqlite_unverified_replay"][0][:2], [key, SHA],
        )
        digest = self._pin()
        verified = verify_offline_pair(
            self.sqlite, self.ledger, self.manifest,
            expected_manifest_sha256=digest,
        )
        self.assertEqual(verified["sqlite_unverified_replay_count"], 1)
        self.assertFalse(verified["restore_authorized"])
        with FenceLedger(self.ledger) as witness:
            self.assertEqual(
                witness.replay_status(
                    key=key, sha=SHA,
                    response_sha=hashlib.sha256(_canonical(envelope)).hexdigest(),
                ), "unverified",
            )

    def test_precondition_media_crash_after_witness_clearance_needs_no_sqlite_clearance(self) -> None:
        # A completed, non-ambiguous 409 media precondition is equally outside
        # SnapshotStore's SQLite recovery-clearance contract. Its persisted
        # response still cannot be independently replay-attested.
        key = "media:precondition-crash-after-response"
        envelope = {
            "media_persistence_confirmed": False,
            "operation_receipt": {"outcome": "precondition_failed", "writer_invoked": False},
        }
        self._persist_media_http_response(key=key, status=409, envelope=envelope)
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        with self.assertRaisesRegex(ValueError, "no ambiguous media outcome"):
            store.record_write_recovery_clearance(
                idempotency_key=key, request_sha256=SHA,
                verification_kind="operator_verified_postread",
                evidence_reference="synthetic:independent-precondition-readback",
                observed_at=AT,
            )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:independent-precondition-readback",
            )
        report = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(report["fences"]["sqlite_media_unresolved"], [])
        self.assertEqual(report["fences"]["witness_unresolved"], [])
        self.assertEqual(
            report["fences"]["sqlite_unverified_replay"][0][:2], [key, SHA],
        )

    def test_malformed_completed_media_result_cannot_yield_empty_fence_inventory(self) -> None:
        self._persist_media_http_response(
            key="media:bad500", status=500,
            envelope={
                "media_persistence_confirmed": "false",
                "operation_receipt": None,
            },
        )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_only_exact_clearance_removes_failed_media_http_response_fence(self) -> None:
        key = "media:failed-clear"
        self._persist_media_http_response(
            key=key, status=500,
            envelope={"media_persistence_confirmed": False, "operation_receipt": None},
        )
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        store.record_write_recovery_clearance(
            idempotency_key=key, request_sha256=SHA,
            verification_kind="operator_verified_postread",
            evidence_reference="synthetic-test-only:media-operator-postread",
            observed_at=AT,
        )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA,
                evidence="synthetic-test-only:media-operator-postread",
            )
        self.assertFalse(store.create_recovery_pending(
            exclude_idempotency_key="different-key", claim_owner="separate-owner",
        ))
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"]["sqlite_media_unresolved"],
            [],
        )
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"]["witness_unresolved"],
            [],
        )

    def test_v3_manifest_rejected_after_media_recovery_fence_upgrade(self) -> None:
        expected = self._pin()
        manifest = json.loads(self.manifest.read_bytes())
        self.assertEqual(manifest["schema_version"], 5)
        manifest["schema_version"] = 3
        downgraded = _canonical(manifest)
        self.manifest.write_bytes(downgraded)
        with self.assertRaises(PairPreflightError):
            verify_offline_pair(
                self.sqlite, self.ledger, self.manifest,
                expected_manifest_sha256=hashlib.sha256(downgraded).hexdigest(),
            )
        self.assertNotEqual(expected, hashlib.sha256(downgraded).hexdigest())

    def test_owner_quarantined_write_is_a_distinct_unresolved_restore_fence(self) -> None:
        self._start(key="quarantine:no-create-receipt")
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        store.quarantine_started_write_api_request(
            idempotency_key="quarantine:no-create-receipt",
            request_sha256=SHA,
            claim_owner="pair-owner",
            quarantined_at=AT,
        )
        self.assertTrue(store.create_recovery_pending(
            exclude_idempotency_key="another-key", claim_owner="separate-owner",
        ))
        observed = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(observed["fences"]["ambiguous_creates"], [])
        self.assertEqual(observed["fences"]["ambiguous_final_creates"], [])
        self.assertEqual(
            observed["fences"]["sqlite_quarantined"],
            [["quarantine:no-create-receipt", SHA]],
        )
        digest = self._pin()
        report = verify_offline_pair(
            self.sqlite, self.ledger, self.manifest,
            expected_manifest_sha256=digest,
        )
        self.assertEqual(report["sqlite_quarantined_count"], 1)
        self.assertEqual(report["ambiguous_create_count"], 1)
        self.assertFalse(report["restore_authorized"])

    def test_independent_operation_bound_clearance_excludes_quarantine_fence(self) -> None:
        self._start(key="quarantine:cleared")
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        store.quarantine_started_write_api_request(
            idempotency_key="quarantine:cleared", request_sha256=SHA,
            claim_owner="pair-owner", quarantined_at=AT,
        )
        store.record_write_recovery_clearance(
            idempotency_key="quarantine:cleared", request_sha256=SHA,
            verification_kind="operator_verified_postread",
            evidence_reference="synthetic-test-only:operator-observation",
            observed_at=AT,
        )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key="quarantine:cleared", sha=SHA,
                evidence="synthetic-test-only:operator-observation",
            )
        self.assertFalse(store.create_recovery_pending(
            exclude_idempotency_key="unrelated", claim_owner="separate-owner",
        ))
        result = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(result["fences"]["sqlite_quarantined"], [])
        self.assertEqual(result["fences"]["sqlite_started"], [["quarantine:cleared", SHA]])
        self.assertEqual(result["fences"]["witness_unresolved"], [])
        self.assertEqual(result["fences"]["sqlite_unverified_replay"][0][:2], ["quarantine:cleared", SHA])

    def test_quarantined_response_with_unsupported_envelope_fails_closed(self) -> None:
        self._start(key="quarantine:tamper")
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        store.quarantine_started_write_api_request(
            idempotency_key="quarantine:tamper", request_sha256=SHA,
            claim_owner="pair-owner", quarantined_at=AT,
        )
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET response_json=? WHERE idempotency_key=?",
                ('{"error":"write_outcome_unverified","idempotency_key":"other-key"}',
                 "quarantine:tamper"),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_v2_manifest_rejected_when_quarantine_fence_schema_is_upgraded(self) -> None:
        old_sha = self._pin()
        raw = json.loads(self.manifest.read_bytes())
        self.assertEqual(raw["schema_version"], 5)
        raw["schema_version"] = 2
        old_bytes = _canonical(raw)
        self.manifest.write_bytes(old_bytes)
        with self.assertRaises(PairPreflightError):
            verify_offline_pair(
                self.sqlite, self.ledger, self.manifest,
                expected_manifest_sha256=hashlib.sha256(old_bytes).hexdigest(),
            )
        self.assertNotEqual(old_sha, hashlib.sha256(old_bytes).hexdigest())

    def test_offline_candidate_sqlite_must_use_exact_protocol_mode_0600(self) -> None:
        os.chmod(self.sqlite, 0o400)
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_offline_candidate_witness_must_use_exact_protocol_mode_0600(self) -> None:
        os.chmod(self.ledger, 0o700)
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_world_writable_nonsticky_staging_ancestor_is_rejected(self) -> None:
        ancestor = self.source.parent
        old_mode = ancestor.stat().st_mode & 0o7777
        try:
            os.chmod(ancestor, 0o777)
            with self.assertRaises(PairPreflightError):
                inspect_offline_pair(self.sqlite, self.ledger)
        finally:
            os.chmod(ancestor, old_mode)

    def test_uid_owned_sticky_writable_ancestor_is_still_rejected(self) -> None:
        # Root-owned /tmp 1777 is allowed, but caller-owned 1777 is not.
        if os.geteuid() == 0:
            self.skipTest("root-owned sticky 01777 ancestors are allowed by protocol")
        ancestor = self.source.parent
        old_mode = ancestor.stat().st_mode & 0o7777
        try:
            os.chmod(ancestor, 0o1777)
            with self.assertRaises(PairPreflightError):
                inspect_offline_pair(self.sqlite, self.ledger)
        finally:
            os.chmod(ancestor, old_mode)

    def test_offline_private_parent_requires_protocol_mode_0700(self) -> None:
        try:
            os.chmod(self.source, 0o500)
            with self.assertRaises(PairPreflightError):
                inspect_offline_pair(self.sqlite, self.ledger)
        finally:
            os.chmod(self.source, 0o700)

    def test_missing_witness_lock_fails_closed_without_recreating_lock(self) -> None:
        lock = self.ledger.with_name(self.ledger.name + ".lock")
        lock.unlink()
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)
        self.assertFalse(lock.exists(), "read-only verifier must not create a lock")

    def test_held_offline_lock_rejects_new_witness_owner_after_scan_starts(self) -> None:
        with _witness_owner_absent(self.ledger):
            with self.assertRaises(FenceWitnessError):
                FenceLedger(self.ledger)

    def test_witness_lock_mode_must_match_owner_protocol(self) -> None:
        lock = self.ledger.with_name(self.ledger.name + ".lock")
        os.chmod(lock, 0o400)
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_operator_clearance_rejects_http_checkpoint_status_divergence(self) -> None:
        from mark_api.write_api import _create_receipt_to_dict

        self._start()
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        checkpoint, _ = self._create_checkpoint(reference="write-api:" + KEY)
        final = replace(
            checkpoint, media_post_read_status=MediaPostReadStatus.UNKNOWN,
            media_persistence_confirmed=False,
        )
        store.append_create_operation_receipt(final)
        wire = _create_receipt_to_dict(final)
        wire["pre_read_status"] = "incompatible-pre-read"
        store.complete_write_api_request(
            idempotency_key=KEY, request_sha256=SHA, claim_owner="pair-owner",
            response_status=202,
            response_json=_canonical({
                "media_persistence_confirmed": False, "operation_receipt": wire,
            }).decode("ascii"),
            completed_at=AT,
        )
        with self.assertRaises(ValueError):
            store.record_write_recovery_clearance(
                idempotency_key=KEY, request_sha256=SHA,
                verification_kind="operator_verified_postread",
                evidence_reference="synthetic:independent-operator",
                observed_at=AT,
            )

    def test_operator_core_clearance_rejects_all_immutable_http_mismatches(self) -> None:
        from mark_api.write_api import _create_receipt_to_dict

        self._start()
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        checkpoint, _ = self._create_checkpoint(reference="write-api:" + KEY)
        final = replace(
            checkpoint, media_post_read_status=MediaPostReadStatus.UNKNOWN,
            media_persistence_confirmed=False,
        )
        store.append_create_operation_receipt(final)
        original_wire = _create_receipt_to_dict(final)
        def attempt(wire: dict[str, object]) -> None:
            with sqlite3.connect(self.sqlite) as db:
                db.execute(
                    "UPDATE write_api_requests SET state='completed', "
                    "response_status=202, completed_at=?, response_json=? "
                    "WHERE idempotency_key=?",
                    (AT.isoformat(), _canonical({
                        "idempotency_key": KEY,
                        "platform_retry_authorized": False,
                        "media_persistence_confirmed": False,
                        "media_post_read_status": "unknown",
                        "operation_receipt": wire,
                    }).decode("ascii"), KEY),
                )
            with self.assertRaises(ValueError):
                store.record_write_recovery_clearance(
                    idempotency_key=KEY, request_sha256=SHA,
                    verification_kind="operator_verified_postread",
                    evidence_reference="synthetic:adversarial-http",
                    observed_at=AT,
                )

        changes = {
            "started_at": "2026-10-13T17:00:00+01:00",
            "completed_at": "2026-10-13T15:59:59.999999+00:00",
            "pre_read_status": "read_changed",
            "confirmation_pre_read_status": "confirmation_changed",
            "post_read_status": "post_read_changed",
            "confirmation_post_read_status": "confirmation_post_changed",
            "content_post_read_status": "content_post_changed",
            "writer_error": "forged-unrelated-error",
            "authorization_by": "another-operator",
            "authorization_reference": "write-api:other-key",
            "created_ad_id": "202",
            "media_post_read_status": ["unknown"],
        }
        for field, altered in changes.items():
            with self.subTest(field=field):
                wire = dict(original_wire)
                wire[field] = altered
                attempt(wire)
        for mode in ("missing", "extra"):
            with self.subTest(mode=mode):
                wire = dict(original_wire)
                if mode == "missing":
                    del wire["post_read_status"]
                else:
                    wire["untrusted_extra_status"] = "confirmed"
                attempt(wire)

    def test_operator_core_clearance_rejects_final_snapshot_or_duplicate(self) -> None:
        from mark_api.write_api import _create_receipt_to_dict

        self._start()
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        checkpoint, _ = self._create_checkpoint(reference="write-api:" + KEY)
        final = replace(
            checkpoint, media_post_read_status=MediaPostReadStatus.UNKNOWN,
            media_persistence_confirmed=False,
        )
        store.append_create_operation_receipt(final)
        store.complete_write_api_request(
            idempotency_key=KEY, request_sha256=SHA, claim_owner="pair-owner",
            response_status=202,
            response_json=_canonical({
                "idempotency_key": KEY,
                "platform_retry_authorized": False,
                "media_persistence_confirmed": False,
                "media_post_read_status": "unknown",
                "operation_receipt": _create_receipt_to_dict(final),
            }).decode("ascii"),
            completed_at=AT,
        )
        with sqlite3.connect(self.sqlite) as db:
            snapshots = dict(zip(
                ("post_snapshot_json", "confirmation_post_snapshot_json",
                 "content_post_snapshot_json"),
                db.execute(
                    "SELECT post_snapshot_json,confirmation_post_snapshot_json,"
                    "content_post_snapshot_json FROM create_operation_receipts"
                ).fetchone(),
            ))
        for column in snapshots:
            with self.subTest(column=column):
                with sqlite3.connect(self.sqlite) as db:
                    db.execute(
                        f"UPDATE create_operation_receipts SET {column}=?",
                        ('{"ad_id":"201"}',),
                    )
                with self.assertRaises(ValueError):
                    store.record_write_recovery_clearance(
                        idempotency_key=KEY, request_sha256=SHA,
                        verification_kind="operator_verified_postread",
                        evidence_reference="synthetic:forged-snapshot",
                        observed_at=AT,
                    )
                with sqlite3.connect(self.sqlite) as db:
                    db.execute(
                        f"UPDATE create_operation_receipts SET {column}=?",
                        (snapshots[column],),
                    )
        store.append_create_operation_receipt(final)
        with self.assertRaises(ValueError):
            store.record_write_recovery_clearance(
                idempotency_key=KEY, request_sha256=SHA,
                verification_kind="operator_verified_postread",
                evidence_reference="synthetic:duplicate-final",
                observed_at=AT,
            )

    def test_confirmed_core_operator_clearance_requires_original_open_checkpoint(self) -> None:
        # No browser-committed before_media_post_read checkpoint exists,
        # despite a response claiming confirmed core Create. A local operator
        # must not manufacture evidence for a nonexistent durable fence, even
        # if the independent Witness reply and clearance are well-formed.
        key = "media:core-no-original-checkpoint"
        response = {
            "media_persistence_confirmed": False,
            "operation_receipt": {
                "operation": "create", "outcome": "confirmed",
                "writer_invoked": True, "created_ad_id": "201",
                "authorization_by": "offline-test-owner",
                "authorization_reference": "write-api:" + key,
                "media_post_read_status": "unknown",
            },
        }
        self._completed_response_pair(key=key, response=response, status=200)
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        with self.assertRaises(ValueError):
            store.record_write_recovery_clearance(
                idempotency_key=key, request_sha256=SHA,
                verification_kind="operator_verified_postread",
                evidence_reference="synthetic:imagined-checkpoint",
                observed_at=AT,
            )
        # A directly forged SQLite clearance with correct key/SHA and
        # independent Witness operator proof must ALSO be rejected offline.
        with sqlite3.connect(self.sqlite) as db:
            db.execute(
                "INSERT INTO write_recovery_clearances "
                "(idempotency_key,request_sha256,observed_at,"
                "verification_kind,evidence_reference) VALUES(?,?,?,?,?)",
                (key, SHA, AT.isoformat(), "operator_verified_postread",
                 "synthetic:forged-nonexistent-checkpoint"),
            )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:witness-reconciled",
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_confirmed_core_operator_cannot_clear_duplicate_checkpoints(self) -> None:
        self._start()
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        receipt, _ = self._create_checkpoint(reference="write-api:" + KEY)
        # A second Create checkpoint with the same key could represent a
        # second external effect: one operator observation is insufficient.
        store.append_create_operation_checkpoint(receipt)
        store.complete_write_api_request(
            idempotency_key=KEY, request_sha256=SHA, claim_owner="pair-owner",
            response_status=200,
            response_json=_canonical({
                "media_persistence_confirmed": False,
                "operation_receipt": {
                    "operation": "create", "outcome": "confirmed",
                    "writer_invoked": True, "created_ad_id": "201",
                    "authorization_by": "offline-test-owner",
                    "authorization_reference": "write-api:" + KEY,
                    "media_post_read_status": "unknown",
                },
            }).decode("ascii"),
            completed_at=AT,
        )
        with self.assertRaises(ValueError):
            store.record_write_recovery_clearance(
                idempotency_key=KEY, request_sha256=SHA,
                verification_kind="operator_verified_postread",
                evidence_reference="synthetic:inadequate-duplicate-proof",
                observed_at=AT,
            )

    def test_confirmed_core_operator_cannot_clear_settled_checkpoint(self) -> None:
        self._start()
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        receipt, _ = self._create_checkpoint(reference="write-api:" + KEY)
        store.append_create_operation_receipt(replace(
            receipt, media_post_read_status=MediaPostReadStatus.CONFIRMED,
            media_persistence_confirmed=True,
        ))
        # A mismatched later HTTP envelope must not allow an invented
        # independent operator clearance for an already settled checkpoint.
        store.complete_write_api_request(
            idempotency_key=KEY, request_sha256=SHA, claim_owner="pair-owner",
            response_status=200,
            response_json=_canonical({
                "media_persistence_confirmed": False,
                "operation_receipt": {
                    "operation": "create", "outcome": "confirmed",
                    "writer_invoked": True, "created_ad_id": "201",
                    "authorization_by": "offline-test-owner",
                    "authorization_reference": "write-api:" + KEY,
                    "media_post_read_status": "unknown",
                },
            }).decode("ascii"),
            completed_at=AT,
        )
        with self.assertRaises(ValueError):
            store.record_write_recovery_clearance(
                idempotency_key=KEY, request_sha256=SHA,
                verification_kind="operator_verified_postread",
                evidence_reference="synthetic:already-settled-checkpoint",
                observed_at=AT,
            )

    def test_witness_only_ambiguous_media_with_completion_is_diagnostic(self) -> None:
        # A genuine Witness-completed ambiguous media reply may remain an
        # explicitly open SQLite recovery fence after Witness-only operator
        # clearance. Without completion the same state must be rejected.
        key = "media:witness-completed-with-open-sqlite"
        self._completed_response_pair(
            key=key, status=500, action="create_media", response={
                "media_persistence_confirmed": False,
                "operation_receipt": {"outcome": "ambiguous", "writer_invoked": True},
            },
        )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:witness-only-postread",
            )
        observed = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(observed["fences"]["sqlite_media_unresolved"], [[key, SHA]])
        self.assertEqual(observed["fences"]["witness_unresolved"], [])
        self.assertEqual(observed["fences"]["sqlite_unverified_replay"], [])
        self.assertTrue(
            SnapshotStore(self.sqlite, create_if_missing=False).create_recovery_pending(
                exclude_idempotency_key="next-after-witness-only",
                claim_owner="other-owner",
            ),
        )

    def test_witness_only_clearance_preserves_unverified_media_checkpoint(self) -> None:
        # An operator-cleared Witness alone does not attest the SQLite media
        # effect, even if the persisted HTTP response says core confirmed.
        # It remains a visible OPEN fence, never inferred restore authority.
        self._start(action="create_media")
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        receipt, checkpoint = self._create_checkpoint(
            reference="write-api:" + KEY,
        )
        store.append_create_operation_receipt(replace(
            receipt, media_post_read_status=MediaPostReadStatus.UNKNOWN,
            media_persistence_confirmed=False,
        ))
        response = _canonical({
            "media_persistence_confirmed": False,
            "operation_receipt": {
                "operation": "create", "outcome": "confirmed",
                "writer_invoked": True, "created_ad_id": "201",
                "authorization_by": "offline-test-owner",
                "authorization_reference": "write-api:" + KEY,
                "media_post_read_status": "unknown",
            },
        }).decode("ascii")
        store.complete_write_api_request(
            idempotency_key=KEY, request_sha256=SHA,
            claim_owner="pair-owner", response_status=200,
            response_json=response, completed_at=AT,
        )
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=KEY, sha=SHA, evidence="synthetic:witness-operator-only",
            )
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(inspected["fences"]["ambiguous_creates"], [checkpoint])
        self.assertEqual(inspected["fences"]["witness_unresolved"], [])
        self.assertEqual(
            [r[:2] for r in inspected["fences"]["sqlite_unverified_replay"]],
            [[KEY, SHA]],
        )
        self.assertTrue(store.create_recovery_pending(
            exclude_idempotency_key="new-after-witness", claim_owner="another",
        ))
        with self.assertRaises(ValueError):
            store.record_write_recovery_clearance(
                idempotency_key=KEY, request_sha256=SHA,
                verification_kind="media_submit_reconciled",
                evidence_reference="synthetic:executor-only",
                observed_at=AT,
            )

    def test_operator_only_dual_clearance_settles_unconfirmed_media_checkpoint(self) -> None:
        # Confirmed core Create + unresolved media is NOT automatically
        # settled. Only actual independent operator clearances in both
        # persistent authorities can permit a future Create after restore.
        self._start(action="create_media")
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        receipt, checkpoint = self._create_checkpoint(
            reference="write-api:" + KEY,
        )
        final = replace(
            receipt, media_post_read_status=MediaPostReadStatus.UNKNOWN,
            media_persistence_confirmed=False,
        )
        store.append_create_operation_receipt(final)
        from mark_api.write_api import _create_receipt_to_dict

        # This is the *actual* canonical Write API wire projection, not a
        # abbreviated synthetic subset admitted only for test convenience.
        response = _canonical({
            "idempotency_key": KEY,
            "platform_retry_authorized": False,
            "media_persistence_confirmed": False,
            "media_post_read_status": "unknown",
            "operation_receipt": _create_receipt_to_dict(final),
        }).decode("ascii")
        store.complete_write_api_request(
            idempotency_key=KEY, request_sha256=SHA,
            claim_owner="pair-owner", response_status=202,
            response_json=response, completed_at=AT,
        )
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"]["ambiguous_creates"],
            [checkpoint],
        )
        self.assertTrue(store.create_recovery_pending(
            exclude_idempotency_key="new-after-media", claim_owner="another",
        ))
        with self.assertRaises(ValueError):
            store.record_write_recovery_clearance(
                idempotency_key=KEY, request_sha256=SHA,
                verification_kind="media_submit_reconciled",
                evidence_reference="synthetic:executor-only",
                observed_at=AT,
            )
        store.record_write_recovery_clearance(
            idempotency_key=KEY, request_sha256=SHA,
            verification_kind="operator_verified_postread",
            evidence_reference="synthetic:independent-verified-media-outcome",
            observed_at=AT,
        )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)
        with FenceLedger(self.ledger) as witness:
            witness.operator_clearance(
                key=KEY, sha=SHA, evidence="synthetic:independent-operator",
            )
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(inspected["fences"]["ambiguous_creates"], [])
        self.assertEqual(inspected["fences"]["witness_unresolved"], [])
        self.assertTrue(inspected["fences"]["sqlite_unverified_replay"])
        self.assertFalse(store.create_recovery_pending(
            exclude_idempotency_key="new-after-media", claim_owner="another",
        ))

    def test_none_authorization_does_not_settle_mismatched_final_identity(self) -> None:
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        original, _ = self._create_checkpoint()
        unbound = replace(
            original, authorization_by=None, authorization_reference=None,
        )
        for field, value in (
            ("authorization_by", "unrelated-operator"),
            ("authorization_reference", "write-api:unrelated-key"),
        ):
            with self.subTest(field=field):
                with sqlite3.connect(self.sqlite) as db:
                    db.execute("DELETE FROM create_operation_checkpoints")
                    db.execute("DELETE FROM create_operation_receipts")
                store.append_create_operation_checkpoint(unbound)
                mismatched = replace(
                    unbound, media_post_read_status=MediaPostReadStatus.CONFIRMED,
                    media_persistence_confirmed=True, **{field: value},
                )
                store.append_create_operation_receipt(mismatched)
                self.assertTrue(store.create_recovery_pending(
                    exclude_idempotency_key="unrelated-new-create",
                    claim_owner="separate-owner",
                ))
                self.assertEqual(
                    len(inspect_offline_pair(
                        self.sqlite, self.ledger,
                    )["fences"]["ambiguous_creates"]),
                    1,
                )

    def test_matching_none_authorization_checkpoint_is_settled(self) -> None:
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        bound, _ = self._create_checkpoint()
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute("DELETE FROM create_operation_checkpoints")
        unbound = replace(
            bound, authorization_by=None, authorization_reference=None,
        )
        store.append_create_operation_checkpoint(unbound)
        store.append_create_operation_receipt(replace(
            unbound, media_post_read_status=MediaPostReadStatus.CONFIRMED,
            media_persistence_confirmed=True,
        ))
        self.assertFalse(store.create_recovery_pending(
            exclude_idempotency_key="new-key", claim_owner="other-owner",
        ))
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"]["ambiguous_creates"],
            [],
        )

    def test_confirmed_create_checkpoint_without_final_receipt_is_unresolved(self) -> None:
        receipt, checkpoint = self._create_checkpoint()
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(inspected["fences"]["ambiguous_creates"], [checkpoint])
        externally_anchored = self._pin()
        observed = verify_offline_pair(
            self.sqlite, self.ledger, self.manifest,
            expected_manifest_sha256=externally_anchored,
        )
        self.assertEqual(observed["ambiguous_create_count"], 1)
        final = replace(
            receipt,
            media_post_read_status=MediaPostReadStatus.CONFIRMED,
            media_persistence_confirmed=True,
        )
        SnapshotStore(self.sqlite, create_if_missing=False).append_create_operation_receipt(final)
        after = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(after["fences"]["ambiguous_creates"], [])
        # The old externally pinned manifest never silently accepts a newer
        # different source generation even if both are locally consistent.
        with self.assertRaises(PairPreflightError):
            verify_offline_pair(
                self.sqlite, self.ledger, self.manifest,
                expected_manifest_sha256=externally_anchored,
            )

    def test_forged_final_receipt_completed_before_start_cannot_settle_checkpoint(self) -> None:
        # A valid-looking final receipt matching the checkpoint's selected
        # SQL fields must still satisfy CreateOperationReceipt.__post_init__.
        receipt, checkpoint = self._create_checkpoint()
        final = replace(
            receipt, media_post_read_status=MediaPostReadStatus.CONFIRMED,
            media_persistence_confirmed=True,
        )
        SnapshotStore(self.sqlite, create_if_missing=False).append_create_operation_receipt(final)
        with sqlite3.connect(self.sqlite) as db:
            db.execute(
                "UPDATE create_operation_receipts SET completed_at=? "
                "WHERE id=(SELECT MAX(id) FROM create_operation_receipts)",
                ("2026-10-13T15:59:59+00:00",),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_forged_final_receipt_empty_required_status_cannot_settle_checkpoint(self) -> None:
        receipt, checkpoint = self._create_checkpoint()
        final = replace(
            receipt, media_post_read_status=MediaPostReadStatus.CONFIRMED,
            media_persistence_confirmed=True,
        )
        SnapshotStore(self.sqlite, create_if_missing=False).append_create_operation_receipt(final)
        with sqlite3.connect(self.sqlite) as db:
            db.execute(
                "UPDATE create_operation_receipts SET confirmation_pre_read_status='' "
                "WHERE id=(SELECT MAX(id) FROM create_operation_receipts)"
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_final_media_time_preceding_checkpoint_cannot_close_fence(self) -> None:
        # The real runtime builds final via max(checkpoint.completed_at,
        # clock()), so a separately valid earlier final is NOT a continuation.
        receipt, checkpoint = self._create_checkpoint()
        final = replace(
            receipt,
            completed_at=AT.replace(second=2),
            media_post_read_status=MediaPostReadStatus.CONFIRMED,
            media_persistence_confirmed=True,
        )
        SnapshotStore(
            self.sqlite, create_if_missing=False,
        ).append_create_operation_receipt(final)
        with sqlite3.connect(self.sqlite) as db:
            db.execute(
                "UPDATE create_operation_checkpoints SET completed_at=? WHERE id=?",
                (AT.replace(second=3).isoformat(), checkpoint),
            )
        observed = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(
            observed["fences"]["ambiguous_creates"], [checkpoint],
        )

    def test_final_media_time_after_offset_checkpoint_remains_settled(self) -> None:
        # Compare instants, not ISO strings: +02:00 and +00:00 can represent
        # the same operation and the final still chronologically follows.
        receipt, checkpoint = self._create_checkpoint()
        final = replace(
            receipt,
            completed_at=AT.replace(second=3),
            media_post_read_status=MediaPostReadStatus.CONFIRMED,
            media_persistence_confirmed=True,
        )
        SnapshotStore(
            self.sqlite, create_if_missing=False,
        ).append_create_operation_receipt(final)
        with sqlite3.connect(self.sqlite) as db:
            db.execute(
                "UPDATE create_operation_checkpoints SET completed_at=? WHERE id=?",
                ("2026-10-13T18:00:02+02:00", checkpoint),
            )
        observed = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(observed["fences"]["ambiguous_creates"], [])

    def test_mismatched_immutable_checkpoint_final_receipt_fields_remain_fenced(self) -> None:
        # Independently valid receipts may still be unrelated to the durable
        # before-media checkpoint: only exact immutable fields can settle it.
        # The genuine media runtime derives final from the same original
        # CreateOperationReceipt; status/error fields cannot change.
        changes = (
            ("pre_read_status", "success_nonempty"),
            ("confirmation_pre_read_status", "success_nonempty"),
            ("post_read_status", "success_empty"),
            ("confirmation_post_read_status", "success_empty"),
            ("content_post_read_status", "success_empty"),
            ("writer_error", "synthetic:altered-core-error"),
        )
        for field_name, changed_value in changes:
            with self.subTest(field=field_name):
                with sqlite3.connect(self.sqlite) as db:
                    db.execute("DELETE FROM create_operation_checkpoints")
                    db.execute("DELETE FROM create_operation_receipts")
                receipt, checkpoint = self._create_checkpoint()
                final = replace(
                    receipt,
                    media_post_read_status=MediaPostReadStatus.CONFIRMED,
                    media_persistence_confirmed=True,
                )
                SnapshotStore(
                    self.sqlite, create_if_missing=False,
                ).append_create_operation_receipt(final)
                with sqlite3.connect(self.sqlite) as db:
                    db.execute(
                        f"UPDATE create_operation_receipts SET {field_name}=?",
                        (changed_value,),
                    )
                inspected = inspect_offline_pair(self.sqlite, self.ledger)
                self.assertEqual(
                    inspected["fences"]["ambiguous_creates"], [checkpoint],
                )
                self.assertTrue(
                    SnapshotStore(self.sqlite, create_if_missing=False)
                    .create_recovery_pending(
                        exclude_idempotency_key="new:postrestore",
                        claim_owner="new-owner",
                    )
                )

    def test_runtime_checkpoint_fence_matches_offline_immutable_fields(self) -> None:
        # RED: an offline-open confirmed checkpoint must also block a new
        # Create in the restored runtime, including a forged final receipt.
        receipt, checkpoint = self._create_checkpoint()
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"]["ambiguous_creates"],
            [checkpoint],
        )
        self.assertTrue(store.create_recovery_pending(
            exclude_idempotency_key="new:postrestore",
            claim_owner="new-owner",
        ))
        final = replace(
            receipt, media_post_read_status=MediaPostReadStatus.CONFIRMED,
            media_persistence_confirmed=True,
        )
        store.append_create_operation_receipt(final)
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"]["ambiguous_creates"],
            [],
        )
        self.assertFalse(store.create_recovery_pending(
            exclude_idempotency_key="new:postrestore",
            claim_owner="new-owner",
        ))
        changes = (
            ("pre_read_status", "success_nonempty"),
            ("confirmation_pre_read_status", "success_nonempty"),
            ("post_read_status", "success_empty"),
            ("confirmation_post_read_status", "success_empty"),
            ("content_post_read_status", "success_empty"),
            ("writer_error", "synthetic:altered-core-error"),
        )
        for field_name, changed_value in changes:
            with self.subTest(field=field_name):
                with sqlite3.connect(self.sqlite) as db:
                    db.execute(
                        f"UPDATE create_operation_receipts SET {field_name}=?",
                        (changed_value,),
                    )
                self.assertEqual(
                    inspect_offline_pair(self.sqlite, self.ledger)
                    ["fences"]["ambiguous_creates"], [checkpoint],
                )
                self.assertTrue(store.create_recovery_pending(
                    exclude_idempotency_key="new:postrestore",
                    claim_owner="new-owner",
                ))
                with sqlite3.connect(self.sqlite) as db:
                    db.execute(
                        f"UPDATE create_operation_receipts SET {field_name}=?",
                        (getattr(receipt, field_name),),
                    )
                self.assertFalse(store.create_recovery_pending(
                    exclude_idempotency_key="new:postrestore",
                    claim_owner="new-owner",
                ))

    def test_runtime_checkpoint_microsecond_inversion_remains_fenced(self) -> None:
        receipt, checkpoint = self._create_checkpoint()
        final = replace(
            receipt, completed_at=AT.replace(microsecond=1),
            media_post_read_status=MediaPostReadStatus.CONFIRMED,
            media_persistence_confirmed=True,
        )
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        store.append_create_operation_receipt(final)
        with sqlite3.connect(self.sqlite) as db:
            db.execute(
                "UPDATE create_operation_checkpoints SET completed_at=? WHERE id=?",
                (AT.replace(microsecond=2).isoformat(), checkpoint),
            )
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"]["ambiguous_creates"],
            [checkpoint],
        )
        self.assertTrue(store.create_recovery_pending(
            exclude_idempotency_key="new:postrestore", claim_owner="new-owner",
        ))
        # The same exact instant with another timezone offset remains valid.
        with sqlite3.connect(self.sqlite) as db:
            db.execute(
                "UPDATE create_operation_checkpoints SET completed_at=? WHERE id=?",
                ("2026-10-13T18:00:00.000001+02:00", checkpoint),
            )
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"]["ambiguous_creates"],
            [],
        )
        self.assertFalse(store.create_recovery_pending(
            exclude_idempotency_key="new:postrestore", claim_owner="new-owner",
        ))

    def test_runtime_malformed_completed_response_is_fail_closed(self) -> None:
        key = "complete:non-json-recovery"
        self._nonmedia_crash_without_witness_completion(key=key)
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        self.assertFalse(store.create_recovery_pending(
            exclude_idempotency_key="another", claim_owner="new-owner",
        ))
        for raw in ("not-json", "null", "[]", "42", '"text"'):
            with self.subTest(raw=raw):
                with sqlite3.connect(self.sqlite) as db:
                    db.execute(
                        "UPDATE write_api_requests SET response_json=? "
                        "WHERE idempotency_key=?",
                        (raw, key),
                    )
                # Valid JSON primitives are not valid owner-produced HTTP
                # envelopes either, so none may silently release a fence.
                self.assertTrue(store.create_recovery_pending(
                    exclude_idempotency_key="another", claim_owner="new-owner",
                ))

    def test_ambiguous_final_create_may_contain_legitimate_unbound_snapshot(self) -> None:
        # A domain-valid ambiguous Create can retain an observation without
        # claiming a confirmed created_ad_id; it remains a recovery fence.
        observed = AdSnapshot(
            ad_id="201", observed_at=AT, source="management",
            lifecycle_state=LifecycleState.ACTIVE,
            title="Unconfirmed synthetic item",
        )
        receipt = CreateOperationReceipt(
            operation="create", started_at=AT, completed_at=AT,
            outcome=OperationOutcome.AMBIGUOUS,
            pre_read_status="success_empty",
            confirmation_pre_read_status="success_empty",
            post_read_status="unknown",
            confirmation_post_read_status="unknown",
            content_post_read_status="unknown",
            writer_invoked=True, created_ad_id=None,
            authorization_by="offline-test-owner",
            authorization_reference="write-api:ambiguous-observation",
            post_snapshot=observed,
            media_post_read_status=MediaPostReadStatus.UNKNOWN,
            media_persistence_confirmed=False,
        )
        SnapshotStore(
            self.sqlite, create_if_missing=False,
        ).append_create_operation_receipt(receipt)
        observed_pair = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(len(observed_pair["fences"]["ambiguous_final_creates"]), 1)

    def test_truncated_matching_snapshot_payloads_cannot_settle_checkpoint(self) -> None:
        # RED: matching but truncated snapshot JSON is not owner-produced
        # AdSnapshot evidence. The original checkpoint must never disappear.
        receipt, _ = self._create_checkpoint()
        final = replace(
            receipt,
            media_post_read_status=MediaPostReadStatus.CONFIRMED,
            media_persistence_confirmed=True,
        )
        SnapshotStore(
            self.sqlite, create_if_missing=False,
        ).append_create_operation_receipt(final)
        truncated = '{"ad_id": "201"}'
        with sqlite3.connect(self.sqlite) as db:
            for table in (
                "create_operation_checkpoints", "create_operation_receipts",
            ):
                db.execute(
                    f"UPDATE {table} SET post_snapshot_json=?, "
                    "confirmation_post_snapshot_json=?, "
                    "content_post_snapshot_json=?",
                    (truncated, truncated, truncated),
                )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_forged_checkpoint_domain_contract_cannot_be_settled_by_valid_final(self) -> None:
        # Even the EXACT matching final receipt cannot hide a forged durable
        # checkpoint whose values append_create_operation_checkpoint could
        # never have accepted. Every subcase recreates a trusted source pair.
        bad_rows = (
            ("outcome", "ambiguous"),
            ("writer_invoked", 0),
            ("completed_at", "not-an-iso-date"),
            ("completed_at", "2026-10-13T15:59:59+00:00"),
            ("pre_read_status", ""),
            ("confirmation_pre_read_status", ""),
            ("content_post_read_status", ""),
            ("authorization_by", ""),
            ("post_snapshot_json", '{"ad_id":"unrelated"}'),
        )
        for field_name, bad_value in bad_rows:
            with self.subTest(field=field_name, bad=bad_value):
                # Recreate the pair for each input to avoid hidden dependencies.
                with sqlite3.connect(self.sqlite) as db:
                    db.execute("DELETE FROM create_operation_checkpoints")
                    db.execute("DELETE FROM create_operation_receipts")
                receipt, _ = self._create_checkpoint()
                final = replace(
                    receipt, media_post_read_status=MediaPostReadStatus.CONFIRMED,
                    media_persistence_confirmed=True,
                )
                SnapshotStore(
                    self.sqlite, create_if_missing=False,
                ).append_create_operation_receipt(final)
                with sqlite3.connect(self.sqlite) as db:
                    db.execute(
                        f"UPDATE create_operation_checkpoints SET {field_name}=?",
                        (bad_value,),
                    )
                with self.assertRaises(PairPreflightError):
                    inspect_offline_pair(self.sqlite, self.ledger)

    def test_other_receipt_cannot_settle_a_crashed_checkpoint(self) -> None:
        receipt, checkpoint = self._create_checkpoint()
        foreign_final = replace(
            receipt,
            authorization_reference="write-api:unrelated",
            media_post_read_status=MediaPostReadStatus.CONFIRMED,
            media_persistence_confirmed=True,
        )
        SnapshotStore(self.sqlite, create_if_missing=False).append_create_operation_receipt(foreign_final)
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"]["ambiguous_creates"],
            [checkpoint],
        )

    def test_unconfirmed_media_receipt_does_not_settle_checkpoint(self) -> None:
        receipt, checkpoint = self._create_checkpoint()
        final = replace(receipt, media_persistence_confirmed=False)
        SnapshotStore(self.sqlite, create_if_missing=False).append_create_operation_receipt(final)
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"]["ambiguous_creates"],
            [checkpoint],
        )

    def test_empty_pair_with_explicit_external_anchor_is_read_only(self) -> None:
        expected = self._pin()
        files = {
            path: (path.stat().st_ino, path.stat().st_size)
            for path in (self.sqlite, self.ledger, self.manifest)
        }
        receipt = verify_offline_pair(
            self.sqlite, self.ledger, self.manifest,
            expected_manifest_sha256=expected,
        )
        self.assertEqual(receipt["generation"], GENERATION)
        self.assertEqual(receipt["sqlite_started_count"], 0)
        self.assertEqual(receipt["witness_unresolved_count"], 0)
        self.assertTrue(receipt["offline_only"])
        self.assertEqual(files, {
            path: (path.stat().st_ino, path.stat().st_size)
            for path in files
        })

    def test_matching_started_operation_survives_and_never_becomes_cleared(self) -> None:
        self._start()
        expected = self._pin()
        report = verify_offline_pair(
            self.sqlite, self.ledger, self.manifest,
            expected_manifest_sha256=expected,
        )
        self.assertEqual(report["sqlite_started_count"], 1)
        self.assertEqual(report["witness_unresolved_count"], 1)
        self.assertEqual(report["dashboard_pending_count"], 0)
        with FenceLedger(self.ledger) as owner:
            self.assertTrue(owner.fence_pending())
        persisted = SnapshotStore(self.sqlite, create_if_missing=False)
        self.assertTrue(persisted.create_recovery_pending(
            exclude_idempotency_key="other", claim_owner="pair-owner",
        ))

    def test_mismatched_started_key_between_independent_stores_fails(self) -> None:
        self._start(witness=False)
        with FenceLedger(self.ledger) as owner:
            session = owner.open_session()
            owner.execution_started(
                session, key="pair:wrong:key", sha=SHA, action="create",
            )
            owner.seal_session(session)
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_sqlite_fence_removed_after_attestation_is_not_a_valid_pair(self) -> None:
        self._start()
        expected = self._pin()
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute("DELETE FROM write_api_requests")
        with self.assertRaises(PairPreflightError):
            verify_offline_pair(
                self.sqlite, self.ledger, self.manifest,
                expected_manifest_sha256=expected,
            )

    def test_witness_partial_start_index_uppercase_literal_does_not_attest(self) -> None:
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("DROP INDEX one_start_per_key")
            connection.execute(
                "CREATE UNIQUE INDEX one_start_per_key ON events(request_key) "
                "WHERE kind='STARTED'"
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_partial_start_index_literal_with_space_does_not_attest(self) -> None:
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("DROP INDEX one_start_per_key")
            connection.execute(
                "CREATE UNIQUE INDEX one_start_per_key ON events(request_key) "
                "WHERE kind='start ed'"
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_seq_integer_primarykey_token_is_not_rowid_primary_key(self) -> None:
        # Change the table SQL in the staged untrusted candidate, keeping the
        # same normalized characters: integer primarykey != integer primary key.
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("PRAGMA writable_schema=ON")
            cursor = connection.execute(
                "UPDATE sqlite_master SET sql=replace("
                "sql,'seq INTEGER PRIMARY KEY','seq INTEGER PRIMARYKEY') "
                "WHERE name='events' AND type='table'"
            )
            self.assertEqual(cursor.rowcount, 1)
            connection.execute("PRAGMA schema_version=71")
            connection.execute("PRAGMA writable_schema=OFF")
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_started_key_requires_storage_owner_grammar(self) -> None:
        # Witness accepts ':bad', but StorageOwner._KEY does not.
        self._start()
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET idempotency_key=? WHERE idempotency_key=?",
                (":bad", KEY),
            )
        self._rewrite_witness_events(started_key=":bad")
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_dashboard_pending_route_identities_reject_forged_storage_records(self) -> None:
        # These records satisfy _pending_from_json but could NEVER pass the
        # independent StorageOwner dashboard_pending_claim route binding.
        cases = (
            ("DELETE", "/api/write/ads/222", "ad:222", "ad:222", None, "222"),
            ("DELETE", "/api/write/ads/222", "ad:111:delete", "ad:111", None, "111"),
            ("DELETE", "/api/write/ads/222", "ad:222:delete", "ad:111", None, "222"),
            ("DELETE", "/api/write/ads/222", "ad:222:delete", "ad:222", None, "111"),
            ("POST", "/api/write/media/ads", "create", "create", "{}", None),
            ("POST", "/api/write/ads", "create-media", "create", "{}", None),
            ("POST", "/api/write/ads/222/pause", "ad:222:activate", "ad:222", None, "222"),
            ("PATCH", "/api/write/ads/222", "ad:222:update", "ad:222", None, "222"),
            ("DELETE", "/api/write/ads/222", "ad:222:delete", "ad:222", "{}", "222"),
            ("POST", "/api/write/ads/222/delete", "ad:222:delete", "ad:222", None, "222"),
            ("DELETE", "/api/write/ads/0222", "ad:222:delete", "ad:222", None, "222"),
        )
        for method, path, scope, resource, payload, ad_id in cases:
            with self.subTest(method=method, path=path, scope=scope, resource=resource,
                              payload=payload, ad_id=ad_id):
                with sqlite3.connect(self.sqlite) as connection:
                    connection.execute("DELETE FROM dashboard_pending_writes")
                    connection.execute(
                        "INSERT INTO dashboard_pending_writes "
                        "(scope,resource_key,idempotency_key,method,path,payload_json,"
                        "ad_id,acknowledged) VALUES (?,?,?,?,?,?,?,?)",
                        (scope, resource, "pending:forged-route", method, path,
                         payload, ad_id, 0),
                    )
                with self.assertRaises(PairPreflightError):
                    inspect_offline_pair(self.sqlite, self.ledger)

    def test_dashboard_pending_wire_payload_must_be_owner_canonical(self) -> None:
        for payload in ('{"b":2, "a":1}', '{"title":"\\u00dc"}', '{ "title": "x" }'):
            with self.subTest(payload=payload):
                with sqlite3.connect(self.sqlite) as connection:
                    connection.execute("DELETE FROM dashboard_pending_writes")
                    connection.execute(
                        "INSERT INTO dashboard_pending_writes "
                        "(scope,resource_key,idempotency_key,method,path,payload_json,"
                        "ad_id,acknowledged) VALUES (?,?,?,?,?,?,?,?)",
                        ("create", "create", "pending:noncanonical-payload", "POST",
                         "/api/write/ads", payload, None, 0),
                    )
                with self.assertRaises(PairPreflightError):
                    inspect_offline_pair(self.sqlite, self.ledger)

    def test_dashboard_pending_all_storage_owner_write_routes_are_readable(self) -> None:
        # Every legal route must remain inspectable as an unresolved fence;
        # no restore or platform Write authority follows from inspection.
        cases = (
            ("POST", "/api/write/ads", "create", "create", "{}", None, 0),
            ("POST", "/api/write/media/ads", "create-media", "create", "{}", None, 0),
            ("PATCH", "/api/write/ads/222", "ad:222:update", "ad:222",
             '{"title":"Über"}', "222", 0),
            ("POST", "/api/write/ads/222/pause", "ad:222:pause", "ad:222", None, "222", 0),
            ("POST", "/api/write/ads/222/activate", "ad:222:activate", "ad:222",
             None, "222", 0),
            ("DELETE", "/api/write/ads/222", "ad:222:delete", "ad:222", None, "222", 1),
        )
        for method, path, scope, resource, payload, ad_id, acknowledged in cases:
            with self.subTest(path=path):
                with sqlite3.connect(self.sqlite) as connection:
                    connection.execute("DELETE FROM dashboard_pending_writes")
                    connection.execute(
                        "INSERT INTO dashboard_pending_writes "
                        "(scope,resource_key,idempotency_key,method,path,payload_json,"
                        "ad_id,acknowledged) VALUES (?,?,?,?,?,?,?,?)",
                        (scope, resource, "pending:valid-route", method, path,
                         payload, ad_id, acknowledged),
                    )
                self.assertEqual(
                    inspect_offline_pair(self.sqlite, self.ledger)["fences"]["dashboard_pending"],
                    [[scope, resource, "pending:valid-route", acknowledged]],
                )

    def test_dashboard_pending_escaped_page_overflow_is_not_restorable(self) -> None:
        # Inner JSON is below 32 KiB, while double-escaping in the actual
        # StorageOwner dashboard_pending_page response exceeds its IPC frame.
        payload = json.dumps(
            {"title": '"' * 15000}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
        self.assertLess(len(payload.encode("utf-8")), 32 * 1024)
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "INSERT INTO dashboard_pending_writes "
                "(scope,resource_key,idempotency_key,method,path,payload_json,"
                "ad_id,acknowledged) VALUES (?,?,?,?,?,?,?,?)",
                ("create", "create", "pending:escaped-overflow", "POST",
                 "/api/write/ads", payload, None, 0),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_dashboard_pending_page_exact_escaped_frame_edge(self) -> None:
        # A heavily escaped inner intent still has a valid final page byte.
        # Do not reject it early or admit the next-byte overflow.
        from mark_api.storage_owner import (
            _pending_as_json, _pending_from_json, _reply_frame_fits,
        )

        key = "pending:exact-escaped-frame"
        def payload(length: int) -> str:
            return json.dumps(
                {"padding": "x" * length, "title": '"' * 7000},
                sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            )

        def envelope(length: int) -> dict[str, object]:
            pending = _pending_from_json({
                "scope": "create", "resource_key": "create",
                "idempotency_key": key, "method": "POST",
                "path": "/api/write/ads", "payload_json": payload(length),
                "ad_id": None, "acknowledged": False,
            })
            return {"rows": [_pending_as_json(pending)], "more": False}

        lower, upper = 0, 30000
        while lower < upper:
            candidate = (lower + upper + 1) // 2
            if _reply_frame_fits(envelope(candidate)):
                lower = candidate
            else:
                upper = candidate - 1
        boundary = lower
        self.assertTrue(_reply_frame_fits(envelope(boundary)))
        self.assertFalse(_reply_frame_fits(envelope(boundary + 1)))
        self.assertEqual(
            len(_canonical({"ok": True, "result": envelope(boundary)}) + b"\n"),
            32 * 1024 + 1,
        )
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "INSERT INTO dashboard_pending_writes "
                "(scope,resource_key,idempotency_key,method,path,payload_json,"
                "ad_id,acknowledged) VALUES (?,?,?,?,?,?,?,?)",
                ("create", "create", key, "POST", "/api/write/ads",
                 payload(boundary), None, 0),
            )
        self.assertEqual(
            inspect_offline_pair(self.sqlite, self.ledger)["fences"][
                "dashboard_pending"
            ],
            [["create", "create", key, 0]],
        )
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE dashboard_pending_writes SET payload_json=? "
                "WHERE idempotency_key=?",
                (payload(boundary + 1), key),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_dashboard_pending_key_is_validated_by_storage_owner_reader(self) -> None:
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "INSERT INTO dashboard_pending_writes "
                "(scope,resource_key,idempotency_key,method,path,payload_json,"
                "ad_id,acknowledged) VALUES (?,?,?,?,?,?,?,?)",
                ("ad:777", "ad:777", "bad key", "DELETE",
                 "/api/write/ads/777", None, "777", 0),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_dashboard_pending_corrupt_payload_is_not_readable_after_restore(self) -> None:
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "INSERT INTO dashboard_pending_writes "
                "(scope,resource_key,idempotency_key,method,path,payload_json,"
                "ad_id,acknowledged) VALUES (?,?,?,?,?,?,?,?)",
                ("ad:777", "ad:777", "pending:777", "POST",
                 "/api/write/ads/777", "{not-json", "777", 0),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_legacy_confirmed_completion_never_proves_external_replay(self) -> None:
        # Schema-v1 completions may legitimately claim confirmed and survive
        # v1->v2 migration unchanged. Even with an independent operator
        # clearance, FenceLedger.replay_status must NEVER trust this claim.
        key = "complete:legacy-claimed-success"
        response = {"operation_receipt": {
            "outcome": "confirmed", "writer_invoked": True,
        }}
        self._completed_response_pair(key=key, status=200, response=response)
        with FenceLedger(self.ledger) as owner:
            owner.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:operator-check",
            )
        self._rewrite_witness_events(forged_completed_outcome="confirmed")
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(inspected["fences"]["witness_unresolved"], [])
        with FenceLedger(self.ledger) as reopened:
            self.assertEqual(reopened.replay_status(
                key=key, sha=SHA,
                response_sha=hashlib.sha256(_canonical(response)).hexdigest(),
            ), "ambiguous")

    def test_legacy_precondition_completion_remains_untrusted_pending(self) -> None:
        # A historical executor-claimed precondition_failed is NOT
        # independent authority to retire the original durable Witness start.
        key = "complete:legacy-precondition"
        response = {"operation_receipt": {
            "outcome": "precondition_failed", "writer_invoked": False,
        }}
        self._completed_response_pair(key=key, status=409, response=response)
        self._rewrite_witness_events(
            forged_completed_outcome="precondition_failed",
        )
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(inspected["fences"]["witness_unresolved"], [[key, SHA]])
        with FenceLedger(self.ledger) as reopened:
            self.assertTrue(reopened.fence_pending())
            self.assertEqual(reopened.replay_status(
                key=key, sha=SHA,
                response_sha=hashlib.sha256(_canonical(response)).hexdigest(),
            ), "ambiguous")

    def test_operator_clearance_after_ambiguous_witness_response_stays_valid(self) -> None:
        key = "complete:real-ambiguous"
        self._completed_response_pair(
            key=key, status=500,
            response={"error": "write_execution_error"},
        )
        with FenceLedger(self.ledger) as owner:
            owner.operator_clearance(
                key=key, sha=SHA, evidence="synthetic:operator-reconciliation",
            )
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(inspected["fences"]["witness_unresolved"], [])
        self.assertEqual(inspected["fences"]["sqlite_unverified_replay"], [])

    def test_runtime_clearance_after_confirmed_completion_is_not_trusted(self) -> None:
        self._start()
        self._rewrite_witness_events(legacy_media_completion="confirmed")
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_runtime_clearance_after_ambiguous_completion_remains_fenced(self) -> None:
        self._start()
        response = {
            "error": "write_execution_error",
            "idempotency_key": KEY,
            "platform_retry_authorized": False,
            "media_persistence_confirmed": False,
            "operation_receipt": None,
        }
        SnapshotStore(self.sqlite, create_if_missing=False).complete_write_api_request(
            idempotency_key=KEY, request_sha256=SHA, claim_owner="pair-owner",
            response_status=500,
            response_json=json.dumps(
                response, sort_keys=True, ensure_ascii=False,
                separators=(",", ":"),
            ),
            completed_at=AT,
        )
        self._rewrite_witness_events(
            legacy_media_completion="ambiguous",
            legacy_media_response=response,
        )
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(
            inspected["fences"]["witness_unresolved"], [[KEY, SHA]],
        )
        self.assertEqual(inspected["fences"]["sqlite_unverified_replay"], [])

    def test_unparseable_requested_at_cannot_restore_started_write(self) -> None:
        self._start()
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET requested_at=? "
                "WHERE idempotency_key=?", ("broken", KEY),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_unparseable_started_at_cannot_restore_started_write(self) -> None:
        self._start()
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET execution_started_at=? "
                "WHERE idempotency_key=?", ("broken", KEY),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_naive_started_at_cannot_restore_started_write(self) -> None:
        self._start()
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET execution_started_at=? "
                "WHERE idempotency_key=?", ("2026-10-13T16:00:00", KEY),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_unstarted_ownerless_in_progress_claim_cannot_restore(self) -> None:
        # A claim that never began must remain takeover/cancel-able.
        # Ownerlessness is not a legitimate current-schema write path.
        key = "unstarted:ownerless-open"
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        self.assertTrue(store.claim_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            claim_owner="first-runtime", requested_at=AT,
        ).created)
        with sqlite3.connect(self.sqlite) as db:
            db.execute(
                "UPDATE write_api_requests SET claim_owner=NULL WHERE idempotency_key=?",
                (key,),
            )
        self.assertFalse(store.claim_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            claim_owner="replacement-runtime", requested_at=AT,
            allow_abandoned_takeover=True,
        ).created)
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_unstarted_ownerless_cancelled_claim_cannot_restore(self) -> None:
        key = "unstarted:ownerless-cancelled"
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        self.assertTrue(store.claim_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            claim_owner="first-runtime", requested_at=AT,
        ).created)
        store.cancel_unstarted_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            claim_owner="first-runtime", cancelled_at=AT,
        )
        with sqlite3.connect(self.sqlite) as db:
            db.execute(
                "UPDATE write_api_requests SET claim_owner=NULL WHERE idempotency_key=?",
                (key,),
            )
        # Exact 409 response alone is not sufficient: the storage owner's
        # cancellation protocol could never have persisted a NULL owner.
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_unstarted_owner_cancellation_is_recoverable_without_witness(self) -> None:
        # Only the independent StorageOwner can complete an unstarted
        # request. It constructs an exact HTTP 409 cancellation envelope.
        key = "cancel:owner-before-begin"
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        claim = store.claim_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            claim_owner="pair-owner", requested_at=AT,
        )
        self.assertTrue(claim.created)
        receipt = store.cancel_unstarted_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            claim_owner="pair-owner", cancelled_at=AT,
        )
        self.assertIsNone(receipt.execution_started_at)
        self.assertEqual(receipt.response_status, 409)
        observed = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(observed["fences"]["sqlite_started"], [])
        self.assertEqual(observed["fences"]["witness_started"], [])
        anchor = self._pin()
        verified = verify_offline_pair(
            self.sqlite, self.ledger, self.manifest,
            expected_manifest_sha256=anchor,
        )
        self.assertTrue(verified["offline_only"])
        self.assertFalse(verified["restore_authorized"])

    def test_unstarted_completed_claim_must_match_exact_owner_cancel(self) -> None:
        key = "cancel:forged-no-begin"
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        self.assertTrue(store.claim_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            claim_owner="pair-owner", requested_at=AT,
        ).created)
        store.cancel_unstarted_write_api_request(
            idempotency_key=key, request_sha256=SHA,
            claim_owner="pair-owner", cancelled_at=AT,
        )
        # These forged rows are canonical response JSON with supported HTTP
        # status values. No Witness start must never mask their invalidity.
        cases = (
            (200, {"success": True, "idempotency_key": key}),
            (500, {"error": "write_execution_error", "idempotency_key": key,
                   "platform_retry_authorized": False}),
            (409, {"error": "write_claim_cancelled_before_begin",
                   "idempotency_key": key, "platform_retry_authorized": False,
                   "debug": "non-owner-extra"}),
            (409, {"error": "write_claim_cancelled_before_begin",
                   "idempotency_key": "different-key",
                   "platform_retry_authorized": False}),
            (409, {"error": "create_recovery_pending",
                   "idempotency_key": key, "platform_retry_authorized": False}),
        )
        for status, response in cases:
            with self.subTest(status=status, response=response):
                with sqlite3.connect(self.sqlite) as db:
                    db.execute(
                        "UPDATE write_api_requests SET response_status=?,"
                        "response_json=? WHERE idempotency_key=?",
                        (status, _canonical(response).decode("ascii"), key),
                    )
                with self.assertRaises(PairPreflightError):
                    inspect_offline_pair(self.sqlite, self.ledger)

    def test_unstarted_unreadable_requested_at_cannot_restore_claim(self) -> None:
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        claim = store.claim_write_api_request(
            idempotency_key="unstarted:bad-clock", request_sha256=SHA,
            claim_owner="pair-owner", requested_at=AT,
        )
        self.assertTrue(claim.created)
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET requested_at=? "
                "WHERE idempotency_key=?",
                ("not-a-timestamp", "unstarted:bad-clock"),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_bad_completed_timestamp_cannot_restore_media_response(self) -> None:
        key = "media:bad-completed-clock"
        self._persist_media_http_response(
            key=key, status=500,
            envelope={"media_persistence_confirmed": False,
                      "operation_receipt": None},
        )
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET completed_at=? "
                "WHERE idempotency_key=?", ("broken", key),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_operator_clearance_preceding_matching_start_is_not_trusted(self) -> None:
        self._start()
        with FenceLedger(self.ledger) as operator:
            operator.operator_clearance(
                key=KEY, sha=SHA, evidence="synthetic:independent-clearance",
            )
        self._rewrite_witness_events(
            order=("opened", "operator_cleared", "started", "sealed"),
        )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_invalid_whitespace_started_key_cannot_be_recovered(self) -> None:
        self._start()
        invalid = "bad write key"
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET idempotency_key=? WHERE idempotency_key=?",
                (invalid, KEY),
            )
        self._rewrite_witness_events(started_key=invalid)
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_oversized_started_key_cannot_be_recovered(self) -> None:
        self._start()
        invalid = "k" * 161
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET idempotency_key=? WHERE idempotency_key=?",
                (invalid, KEY),
            )
        self._rewrite_witness_events(started_key=invalid)
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)
    def test_operator_clearance_before_session_seal_is_not_trusted(self) -> None:
        self._start()
        with FenceLedger(self.ledger) as operator:
            operator.operator_clearance(
                key=KEY, sha=SHA, evidence="synthetic:independent-clearance",
            )
        self._rewrite_witness_events(
            order=("opened", "started", "operator_cleared", "sealed"),
        )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_started_before_session_open_is_not_trusted(self) -> None:
        self._start()
        self._rewrite_witness_events(order=("started", "opened", "sealed"))
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_started_after_session_seal_is_not_trusted(self) -> None:
        self._start()
        self._rewrite_witness_events(order=("opened", "sealed", "started"))
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_unicode_started_key_cannot_be_recovered(self) -> None:
        self._start()
        invalid = "write:ümlaut"
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET idempotency_key=? WHERE idempotency_key=?",
                (invalid, KEY),
            )
        self._rewrite_witness_events(started_key=invalid)
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_unsupported_symbol_key_cannot_be_recovered(self) -> None:
        self._start()
        invalid = "write/unsupported"
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET idempotency_key=? WHERE idempotency_key=?",
                (invalid, KEY),
            )
        self._rewrite_witness_events(started_key=invalid)
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_maximum_160_character_started_key_is_recoverable(self) -> None:
        self._start()
        valid = "k" * 160
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "UPDATE write_api_requests SET idempotency_key=? WHERE idempotency_key=?",
                (valid, KEY),
            )
        self._rewrite_witness_events(started_key=valid)
        inspected = inspect_offline_pair(self.sqlite, self.ledger)
        self.assertEqual(inspected["fences"]["witness_started"], [[valid, SHA]])

    def test_witness_started_operation_without_originating_session_fails(self) -> None:
        # After the original owner sealed, a candidate image is corrupted by
        # removing only its originating sessions row. Events and chain remain
        # cryptographically self-consistent, but operator_clearance cannot run.
        self._start(key="orphan:start")
        with sqlite3.connect(self.ledger) as connection:
            originating = connection.execute(
                "SELECT session_id FROM events "
                "WHERE kind='started' AND request_key='orphan:start'"
            ).fetchone()
            self.assertIsNotNone(originating)
            connection.execute(
                "DELETE FROM sessions WHERE id=?", (originating[0],),
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_orphan_open_event_cannot_pass_empty_fence_inventory(self) -> None:
        with FenceLedger(self.ledger) as owner:
            session = owner.open_session()
            owner.seal_session(session)
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("DELETE FROM sessions WHERE id=?", (session,))
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_noop_delete_trigger_cannot_imitate_secure_schema(self) -> None:
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("DROP TRIGGER events_no_delete")
            connection.execute(
                "CREATE TRIGGER events_no_delete BEFORE DELETE ON events "
                "BEGIN SELECT 1; END"
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_noop_update_trigger_cannot_imitate_secure_schema(self) -> None:
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("DROP TRIGGER events_no_update")
            connection.execute(
                "CREATE TRIGGER events_no_update BEFORE UPDATE ON events "
                "BEGIN SELECT 1; END"
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_extra_veto_trigger_cannot_bypass_append_only_guards(self) -> None:
        with sqlite3.connect(self.ledger) as connection:
            connection.execute(
                "CREATE TRIGGER extra_veto BEFORE DELETE ON events "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_unexpected_sqlite_store_insert_veto_trigger_is_rejected(self) -> None:
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute(
                "CREATE TRIGGER deny_write BEFORE INSERT ON write_api_requests "
                "BEGIN SELECT RAISE(ABORT, 'recovery write sabotaged'); END"
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_current_binary_blank_id_indexes_are_mandatory_for_restore(self) -> None:
        # Canonical v2 now includes native-ID blank guards from the product
        # integration. A removed guard must never be treated as a valid cold
        # restore just because legacy A6's older 22-object map was valid.
        index_names = (
            "idx_ad_classifications_blank_ad_id",
            "idx_ad_snapshots_blank_ad_id",
            "idx_inbound_message_events_blank_ad_id",
            "idx_reaction_snapshots_blank_ad_id",
        )
        for index in index_names:
            with self.subTest(index=index):
                with sqlite3.connect(self.sqlite) as connection:
                    row = connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                        (index,),
                    ).fetchone()
                    self.assertIsNotNone(row)
                    original_ddl = row[0]
                    self.assertIn("COLLATE BINARY", original_ddl)
                    connection.execute("DROP INDEX " + index)
                with self.assertRaises(PairPreflightError):
                    inspect_offline_pair(self.sqlite, self.ledger)
                with sqlite3.connect(self.sqlite) as connection:
                    connection.execute(original_ddl)
                self.assertIsInstance(inspect_offline_pair(self.sqlite, self.ledger), dict)

    def test_sqlite_store_weakened_recovery_clearance_constraint_is_rejected(self) -> None:
        # The old structure validator checked columns, not whether the
        # requirement for exact 64-character request SHA still exists.
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute("PRAGMA writable_schema=ON")
            row = connection.execute(
                "UPDATE sqlite_master SET sql=replace(sql,"
                "'CHECK(length(request_sha256) = 64)',"
                "'CHECK(length(request_sha256) >= 0)') "
                "WHERE name='write_recovery_clearances' AND type='table'"
            )
            self.assertEqual(row.rowcount, 1)
            connection.execute("PRAGMA schema_version=92")
            connection.execute("PRAGMA writable_schema=OFF")
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_reserved_sqlite_prefix_trigger_in_store_is_rejected(self) -> None:
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute(
                "INSERT INTO sqlite_master(type,name,tbl_name,rootpage,sql) "
                "VALUES('trigger','sqlite_write_veto','write_api_requests',0,?)",
                (
                    "CREATE TRIGGER sqlite_write_veto BEFORE INSERT "
                    "ON write_api_requests BEGIN SELECT RAISE(IGNORE); END",
                ),
            )
            connection.execute("PRAGMA schema_version=93")
            connection.execute("PRAGMA writable_schema=OFF")
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_uppercase_schema_type_cannot_hide_active_trigger(self) -> None:
        # SQLite can load a writable_schema-injected uppercase type whose SQL
        # still installs a real event veto. The verifier must not filter by
        # case-sensitive sqlite_master.type before inspecting every row.
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute(
                "INSERT INTO sqlite_master(type,name,tbl_name,rootpage,sql) "
                "VALUES('TRIGGER','uppercase_veto','events',0,?)",
                (
                    "CREATE TRIGGER uppercase_veto BEFORE INSERT ON events "
                    "WHEN NEW.kind='started' BEGIN SELECT RAISE(IGNORE); END",
                ),
            )
            connection.execute("PRAGMA schema_version=115")
            connection.execute("PRAGMA writable_schema=OFF")
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_forged_trigger_parent_table_cannot_be_ignored(self) -> None:
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute(
                "INSERT INTO sqlite_master(type,name,tbl_name,rootpage,sql) "
                "VALUES('TRIGGER','cross_table_veto','other',0,?)",
                (
                    "CREATE TRIGGER cross_table_veto BEFORE INSERT ON events "
                    "BEGIN SELECT RAISE(IGNORE); END",
                ),
            )
            connection.execute("PRAGMA schema_version=116")
            connection.execute("PRAGMA writable_schema=OFF")
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_reserved_sqlite_prefix_trigger_must_never_be_hidden(self) -> None:
        # A hostile writable_schema snapshot can contain a reserved sqlite_
        # name even though normal CREATE would reject that name. This trigger
        # can silently ignore future Witness started events after restore.
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute(
                "INSERT INTO sqlite_master(type,name,tbl_name,rootpage,sql) "
                "VALUES('trigger','sqlite_veto','events',0,?)",
                (
                    "CREATE TRIGGER sqlite_veto BEFORE INSERT ON events "
                    "WHEN NEW.kind='started' BEGIN SELECT RAISE(IGNORE); END",
                ),
            )
            connection.execute("PRAGMA schema_version=81")
            connection.execute("PRAGMA writable_schema=OFF")
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_sqlite_wal_header_rejected_before_image_parser(self) -> None:
        with sqlite3.connect(self.sqlite) as connection:
            self.assertEqual(connection.execute(
                "PRAGMA journal_mode=WAL",
            ).fetchone()[0].lower(), "wal")
        with patch(
            "mark_api.paired_restore_preflight._readonly",
            side_effect=AssertionError("untrusted WAL source reached SQLite parser"),
        ):
            with self.assertRaises(PairPreflightError):
                inspect_offline_pair(self.sqlite, self.ledger)

    def test_sqlite_wal_header_without_sidecar_is_not_safe_restore_source(self) -> None:
        with sqlite3.connect(self.sqlite) as connection:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            self.assertEqual(mode[0].lower(), "wal")
        self.assertEqual(self.sqlite.read_bytes()[18:20], bytes((2, 2)))
        self.assertFalse(self.sqlite.with_name(self.sqlite.name + "-wal").exists())
        self.assertFalse(self.sqlite.with_name(self.sqlite.name + "-shm").exists())
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_sqlite_prefix_lookalike_trigger_is_not_skipped(self) -> None:
        # In SQL LIKE, '_' matches any single character. User objects named
        # sqliteXveto must not be confused with the reserved sqlite_ prefix.
        with sqlite3.connect(self.ledger) as connection:
            connection.execute(
                "CREATE TRIGGER sqliteXveto BEFORE DELETE ON events "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_nonunique_start_index_cannot_imitate_secure_schema(self) -> None:
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("DROP INDEX one_start_per_key")
            connection.execute(
                "CREATE INDEX one_start_per_key ON events(request_key) "
                "WHERE kind='started'"
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_unconstrained_sessions_cannot_imitate_secure_schema(self) -> None:
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("DROP TABLE sessions")
            connection.execute(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL, "
                "opened_at TEXT NOT NULL, sealed_at TEXT)"
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_witness_rollback_to_older_valid_ledger_is_rejected(self) -> None:
        old_ledger = self.source / "witness-old.sqlite"
        shutil.copyfile(self.ledger, old_ledger)
        os.chmod(old_ledger, 0o600)
        self._start()
        expected = self._pin()
        shutil.copyfile(old_ledger, self.ledger)
        with self.assertRaises(PairPreflightError):
            verify_offline_pair(
                self.sqlite, self.ledger, self.manifest,
                expected_manifest_sha256=expected,
            )

    def test_manifest_swap_even_to_consistent_older_generation_fails(self) -> None:
        old = self._pin()
        self._start()
        self._pin(generation="abd4cdd67d41412f82feb2b099830817")
        with self.assertRaises(PairPreflightError):
            verify_offline_pair(
                self.sqlite, self.ledger, self.manifest,
                expected_manifest_sha256=old,
            )


    def test_tampered_but_sqlite_valid_witness_hash_chain_fails_closed(self) -> None:
        self._start()
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("DROP TRIGGER events_no_update")
            connection.execute(
                "UPDATE events SET row_sha256=? WHERE kind='opened'",
                ("f" * 64,),
            )
            connection.execute(
                "CREATE TRIGGER events_no_update BEFORE UPDATE ON events "
                "BEGIN SELECT RAISE(ABORT, 'append-only events'); END;"
            )
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_boolean_witness_chain_tip_is_not_numeric_zero_in_manifest(self) -> None:
        self._pin()
        claimed = json.loads(self.manifest.read_bytes())
        self.assertEqual(claimed["fences"]["witness_chain_tip"][0], 0)
        claimed["fences"]["witness_chain_tip"][0] = False
        poisoned = _canonical(claimed)
        self.manifest.write_bytes(poisoned)
        with self.assertRaises(PairPreflightError):
            verify_offline_pair(
                self.sqlite, self.ledger, self.manifest,
                expected_manifest_sha256=hashlib.sha256(poisoned).hexdigest(),
            )

    def test_boolean_create_checkpoint_id_does_not_match_integer_id(self) -> None:
        _, checkpoint_id = self._create_checkpoint()
        self._pin()
        claimed = json.loads(self.manifest.read_bytes())
        self.assertEqual(claimed["fences"]["ambiguous_creates"], [checkpoint_id])
        claimed["fences"]["ambiguous_creates"] = [True]
        poisoned = _canonical(claimed)
        self.manifest.write_bytes(poisoned)
        with self.assertRaises(PairPreflightError):
            verify_offline_pair(
                self.sqlite, self.ledger, self.manifest,
                expected_manifest_sha256=hashlib.sha256(poisoned).hexdigest(),
            )

    def test_float_sync_attempt_id_does_not_match_integer_id(self) -> None:
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        attempt = store.begin_sync_attempt(
            source="typed-restore", started_at=AT, require_no_unresolved=True,
        )
        self._pin()
        claimed = json.loads(self.manifest.read_bytes())
        self.assertEqual(claimed["fences"]["sqlite_sync_open"], [attempt])
        claimed["fences"]["sqlite_sync_open"] = [float(attempt)]
        poisoned = _canonical(claimed)
        self.manifest.write_bytes(poisoned)
        with self.assertRaises(PairPreflightError):
            verify_offline_pair(
                self.sqlite, self.ledger, self.manifest,
                expected_manifest_sha256=hashlib.sha256(poisoned).hexdigest(),
            )

    def test_boolean_ack_does_not_match_integer_dashboard_pending_field(self) -> None:
        SnapshotStore(self.sqlite, create_if_missing=False).claim_dashboard_pending_write(
            scope="ad:777:delete", resource_key="ad:777",
            idempotency_key="pending:typed", method="DELETE",
            path="/api/write/ads/777", payload_json=None, ad_id="777",
        )
        self._pin()
        claimed = json.loads(self.manifest.read_bytes())
        self.assertEqual(claimed["fences"]["dashboard_pending"][0][3], 0)
        claimed["fences"]["dashboard_pending"][0][3] = False
        poisoned = _canonical(claimed)
        self.manifest.write_bytes(poisoned)
        with self.assertRaises(PairPreflightError):
            verify_offline_pair(
                self.sqlite, self.ledger, self.manifest,
                expected_manifest_sha256=hashlib.sha256(poisoned).hexdigest(),
            )

    def test_missing_or_invalid_external_anchor_never_passes(self) -> None:
        self._pin()
        for invalid in ("", "a" * 63, "g" * 64):
            with self.subTest(invalid=invalid):
                with self.assertRaises((ValueError, PairPreflightError)):
                    verify_offline_pair(
                        self.sqlite, self.ledger, self.manifest,
                        expected_manifest_sha256=invalid,
                    )

    def test_bad_anchored_sqlite_digest_does_not_enter_sqlite_parser(self) -> None:
        pinned = self._pin()
        # Modify an otherwise valid source header after external attestation.
        # A parser trap proves that no SQLite connection may precede its digest.
        with sqlite3.connect(self.sqlite) as connection:
            connection.execute("PRAGMA application_id = 12345")
        from unittest.mock import patch
        with patch(
            "mark_api.paired_restore_preflight._readonly",
            side_effect=AssertionError("untrusted SQLite parser reached"),
        ):
            with self.assertRaises(PairPreflightError):
                verify_offline_pair(
                    self.sqlite, self.ledger, self.manifest,
                    expected_manifest_sha256=pinned,
                )

    def test_bad_anchored_witness_digest_does_not_enter_sqlite_parser(self) -> None:
        pinned = self._pin()
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("PRAGMA application_id = 12345")
        from unittest.mock import patch
        with patch(
            "mark_api.paired_restore_preflight._readonly",
            side_effect=AssertionError("untrusted Witness parser reached"),
        ):
            with self.assertRaises(PairPreflightError):
                verify_offline_pair(
                    self.sqlite, self.ledger, self.manifest,
                    expected_manifest_sha256=pinned,
                )

    def test_readonly_sqlite_connections_disable_trusted_schema_before_reads(self) -> None:
        from mark_api.paired_restore_preflight import _readonly
        connection = _readonly(self.sqlite)
        try:
            result = connection.execute("PRAGMA trusted_schema").fetchone()
            self.assertIsNotNone(result)
            self.assertEqual(result[0], 0)
            self.assertEqual(
                connection.execute("PRAGMA query_only").fetchone()[0], 1,
            )
        finally:
            connection.close()

    def test_symlinked_directory_ancestor_must_be_rejected(self) -> None:
        alias = self.source.parent / "alias-ancestor"
        alias.symlink_to(self.source.parent, target_is_directory=True)
        # Immediate parent resolves as a private real directory, but a
        # grandparent is the attacker-controlled link.
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(alias / self.source.name / self.sqlite.name, self.ledger)

    def test_oversized_sparse_image_is_rejected_before_hashing(self) -> None:
        from mark_api.paired_restore_preflight import _MAX_IMAGE_BYTES, _sha256_fd
        oversized = self.source / "oversized.sqlite"
        with oversized.open("wb") as target:
            target.truncate(_MAX_IMAGE_BYTES + 1)
        with oversized.open("rb") as source:
            with self.assertRaisesRegex(PairPreflightError, "bounded"):
                _sha256_fd(source.fileno())

    def test_witness_wal_header_not_accepted_after_deserialization(self) -> None:
        with sqlite3.connect(self.ledger) as source:
            mode = source.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            self.assertEqual(str(mode).lower(), "wal")
        with self.ledger.open("rb") as raw:
            raw.seek(18)
            self.assertEqual(raw.read(2), bytes((2, 2)))
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_parser_must_consume_pinned_fd_bytes_during_directory_swap(self) -> None:
        pinned = self._pin()
        attacker = self.source.parent / "attacker-copy"
        attacker.mkdir(mode=0o700)
        other = SnapshotStore(attacker / self.sqlite.name)
        other.claim_write_api_request(
            idempotency_key="attacker-unmatched-start", request_sha256=SHA,
            requested_at=AT, claim_owner="attacker",
        )
        other.begin_write_api_request(
            idempotency_key="attacker-unmatched-start", request_sha256=SHA,
            claim_owner="attacker", execution_started_at=AT,
        )
        parked = self.source.parent / "temporarily-parked-origin"
        from mark_api import paired_restore_preflight as pair
        from unittest.mock import patch
        original = pair._readonly
        intervened = False

        def swap_exactly_before_parse(path, *args, **kwargs):
            nonlocal intervened
            if path == self.sqlite and not intervened:
                intervened = True
                self.source.rename(parked)
                attacker.rename(self.source)
                try:
                    return original(path, *args, **kwargs)
                finally:
                    self.source.rename(attacker)
                    parked.rename(self.source)
            return original(path, *args, **kwargs)

        with patch.object(pair, "_readonly", side_effect=swap_exactly_before_parse):
            verified = verify_offline_pair(
                self.sqlite, self.ledger, self.manifest,
                expected_manifest_sha256=pinned,
            )
        self.assertTrue(intervened)
        self.assertEqual(verified["sqlite_started_count"], 0)
        self.assertEqual(verified["witness_unresolved_count"], 0)

    def test_installed_parser_never_reopens_a_filepath_after_digest(self) -> None:
        pinned = self._pin()
        from unittest.mock import patch
        original_connect = sqlite3.connect

        def reject_named_db_reopen(database, *args, **kwargs):
            if str(database).startswith("file:"):
                raise AssertionError("SQLite reopened mutable source path")
            return original_connect(database, *args, **kwargs)

        with patch(
            "mark_api.paired_restore_preflight.sqlite3.connect",
            side_effect=reject_named_db_reopen,
        ):
            report = verify_offline_pair(
                self.sqlite, self.ledger, self.manifest,
                expected_manifest_sha256=pinned,
            )
        self.assertEqual(report["manifest_sha256"], pinned)

    def test_sidecars_rejected_even_for_zero_byte_wal(self) -> None:
        expected = self._pin()
        wal = self.source / "mark.sqlite-wal"
        wal.write_bytes(b"")
        with self.assertRaises(PairPreflightError):
            verify_offline_pair(
                self.sqlite, self.ledger, self.manifest,
                expected_manifest_sha256=expected,
            )

    def test_active_witness_owner_blocks_offline_preflight(self) -> None:
        with FenceLedger(self.ledger) as owner:
            with self.assertRaises(PairPreflightError):
                inspect_offline_pair(self.sqlite, self.ledger)

    def test_symlink_and_world_writable_source_rejected(self) -> None:
        alias = self.source / "linked.sqlite"
        alias.symlink_to(self.sqlite)
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(alias, self.ledger)
        os.chmod(self.sqlite, 0o666)
        with self.assertRaises(PairPreflightError):
            inspect_offline_pair(self.sqlite, self.ledger)

    def test_malformed_duplicate_key_manifest_fails_closed(self) -> None:
        expected = self._pin()
        raw = self.manifest.read_bytes()
        poisoned = raw.replace(
            b',"generation":', b',"generation":"forged","generation":',
            1,
        )
        self.manifest.write_bytes(poisoned)
        # Even if the caller mistakenly pins the attacker-supplied bytes,
        # duplicate keys must not pass canonical JSON validation.
        with self.assertRaises(PairPreflightError):
            verify_offline_pair(
                self.sqlite, self.ledger, self.manifest,
                expected_manifest_sha256=hashlib.sha256(poisoned).hexdigest(),
            )
        self.assertNotEqual(hashlib.sha256(poisoned).hexdigest(), expected)

    def test_cli_requires_explicit_expected_manifest_digest(self) -> None:
        with self.assertRaises(SystemExit) as exit_code:
            main(["--sqlite", str(self.sqlite), "--witness", str(self.ledger),
                  "--manifest", str(self.manifest)])
        self.assertEqual(exit_code.exception.code, 2)


    def test_dashboard_tombstone_and_sync_open_are_preserved_in_pinned_pair(self) -> None:
        store = SnapshotStore(self.sqlite, create_if_missing=False)
        attempt = store.begin_sync_attempt(
            source="offline-qa", started_at=AT, require_no_unresolved=True,
        )
        store.claim_dashboard_pending_write(
            scope="ad:777:delete", resource_key="ad:777",
            idempotency_key="pending:one", method="DELETE",
            path="/api/write/ads/777", payload_json=None, ad_id="777",
        )
        expected = self._pin()
        result = verify_offline_pair(
            self.sqlite, self.ledger, self.manifest,
            expected_manifest_sha256=expected,
        )
        self.assertEqual(result["dashboard_pending_count"], 1)
        self.assertEqual(result["sync_open_count"], 1)
        self.assertEqual(
            SnapshotStore(self.sqlite, create_if_missing=False)
            .sync_recovery_status()["oldest_unresolved_attempt"]["id"],
            attempt,
        )
        manifest = json.loads(self.manifest.read_bytes())
        # An attacker may manufacture a new SHA, but not the independently
        # pinned floor supplied to the legitimate operator.
        manifest["fences"]["sqlite_sync_open"] = []
        tampered = _canonical(manifest)
        self.manifest.write_bytes(tampered)
        with self.assertRaises(PairPreflightError):
            verify_offline_pair(
                self.sqlite, self.ledger, self.manifest,
                expected_manifest_sha256=hashlib.sha256(tampered).hexdigest(),
            )

    def test_witness_sidecars_are_not_accepted_as_valid_snapshot(self) -> None:
        expected = self._pin()
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                sidecar = self.source / ("witness.sqlite" + suffix)
                sidecar.write_bytes(b"")
                with self.assertRaises(PairPreflightError):
                    verify_offline_pair(
                        self.sqlite, self.ledger, self.manifest,
                        expected_manifest_sha256=expected,
                    )
                sidecar.unlink()
