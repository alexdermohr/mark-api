"""Owner-only inventory import: real AF_UNIX framing, synthetic browser data.

Same-UID protocol tests do not prove cross-UID kernel isolation; the existing
real-Docker UID50044/50046 exercises remain a separate mandatory gate.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mark_api.domain import AdSnapshot, LifecycleState
from mark_api.owner_inventory_cli import (
    OwnerInventorySyncError, main, sync_owner_inventory,
)
from mark_api.results import ReadResult, ReadStatus
from mark_api.storage import SnapshotStore
from mark_api.storage_owner import StorageOwnerClient, StorageOwnerError, StorageOwnerServer


NOW = datetime(2026, 10, 12, 17, 0, tzinfo=timezone.utc)
SOURCE = "private-web-owner-inventory"


class _FakeRuntime:
    def __init__(self, result: object = None, *, error: Exception | None = None) -> None:
        self.result, self.error = result, error
        self.reads = 0
        self.closed = False

    def read_inventory(self) -> object:
        self.reads += 1
        if self.error:
            raise self.error
        return self.result

    def close(self) -> None:
        self.closed = True


class OwnerInventoryCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mark-owner-inventory-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        store_parent = root / "data"
        store_parent.mkdir(mode=0o700)
        self.database = store_parent / "mark.sqlite"
        SnapshotStore(self.database)
        socket_parent = root / "rpc"
        socket_parent.mkdir(mode=0o700)
        socket_parent.chmod(0o2710)
        self.socket = socket_parent / "owner.sock"
        server = StorageOwnerServer(
            database=self.database, socket_path=self.socket,
            runtime_uid=os.geteuid() + 10001, runtime_gid=os.getegid(),
        )
        server.runtime_uid = os.geteuid()  # Framing only, not real UID isolation
        server.start()
        self.addCleanup(server.stop)
        self.server = server
        self.client = StorageOwnerClient(self.socket, expected_owner_uid=os.geteuid())

    def _sync(self, runtime: _FakeRuntime):
        return sync_owner_inventory(
            self.client, cdp_port=9222, timeout_seconds=5,
            runtime_factory=lambda **_args: runtime, clock=lambda: NOW,
        )

    @staticmethod
    def _ad(ad_id: str = "100") -> AdSnapshot:
        return AdSnapshot(
            ad_id=ad_id, observed_at=NOW,
            source="management-observed", metric_source="management-observed",
            lifecycle_state=LifecycleState.ACTIVE, title="External read",
            views=42,
        )

    def test_success_nonempty_journals_and_preserves_untrusted_provenance(self) -> None:
        read = _FakeRuntime(ReadResult.success_nonempty((self._ad(),)))
        receipt = self._sync(read)
        self.assertEqual((receipt.observed_count, receipt.persisted_count), (1, 1))
        self.assertTrue(read.closed)
        self.assertEqual(read.reads, 1)
        self.assertEqual(self.client.sync_status()["state"], "success_nonempty")
        self.assertEqual(
            self.client.sync_status()["last_successful_attempt"]["source"], SOURCE,
        )
        stored = SnapshotStore(self.database, create_if_missing=False)
        self.assertEqual(stored.ad_history("100"), (self._ad(),))
        # Typed source observation is NOT a write receipt or clearance.
        with self.assertRaises(StorageOwnerError):
            self.server.dispatch({"version": 1, "op": "complete", "key": "100"})
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM write_recovery_clearances").fetchone()[0], 0
            )

    def test_success_empty_preserves_historical_owner_inventory_and_absence(self) -> None:
        store = SnapshotStore(self.database, create_if_missing=False)
        store.append_ad_snapshot(self._ad("previous"))
        read = _FakeRuntime(ReadResult.success_empty(()))
        receipt = self._sync(read)
        self.assertEqual(receipt.observed_count, 0)
        self.assertTrue(read.closed)
        self.assertEqual(self.client.sync_status()["state"], "success_empty")
        history = store.ad_history("previous")
        self.assertGreaterEqual(len(history), 2)
        self.assertEqual(history[0], self._ad("previous"))
        self.assertEqual(history[-1].lifecycle_state, LifecycleState.ABSENT)

    def test_valid_empty_read_with_omitted_payload_is_success_empty(self) -> None:
        # ReadResult.success_empty() is a supported domain contract: None
        # means no observations, not a malformed or untrusted response.
        runtime = _FakeRuntime(ReadResult.success_empty())
        receipt = self._sync(runtime)
        self.assertEqual((receipt.observed_count, receipt.persisted_count), (0, 0))
        self.assertTrue(runtime.closed)
        self.assertEqual(self.client.sync_status()["state"], "success_empty")

    def test_failed_read_never_claims_successful_empty_inventory(self) -> None:
        read = _FakeRuntime(ReadResult.failure(ReadStatus.UNAUTHENTICATED))
        with self.assertRaisesRegex(OwnerInventorySyncError, "unauthenticated"):
            self._sync(read)
        self.assertTrue(read.closed)
        status = self.client.sync_status()
        self.assertEqual(status["state"], "failed")
        self.assertIsNone(status["last_successful_attempt"])
        self.assertEqual(status["latest_attempt"]["error_kind"], "unauthenticated")

    def test_runtime_builder_exception_is_classified_before_observation(self) -> None:
        with self.assertRaisesRegex(OwnerInventorySyncError, "runtime unavailable"):
            sync_owner_inventory(
                self.client, cdp_port=9222, clock=lambda: NOW,
                runtime_factory=lambda **kwargs: (_ for _ in ()).throw(OSError("secrets")),
            )
        self.assertEqual(self.client.sync_status()["state"], "failed")
        self.assertEqual(
            self.client.sync_status()["latest_attempt"]["error_kind"],
            "runtime_unavailable",
        )

    def test_reader_exception_is_classified_and_runtime_closed(self) -> None:
        runtime = _FakeRuntime(error=OSError("sensitive session detail"))
        with self.assertRaisesRegex(OwnerInventorySyncError, "observation failed"):
            self._sync(runtime)
        self.assertTrue(runtime.closed)
        self.assertEqual(self.client.sync_status()["state"], "failed")
        self.assertEqual(self.client.sync_status()["latest_attempt"]["error_kind"], "reader_exception")

    def test_invalid_record_shape_or_duplicate_id_is_not_committed(self) -> None:
        for result in ({"malformed": "success"}, ReadResult.success_nonempty((self._ad(), self._ad()))):
            with self.subTest(result_type=type(result).__name__):
                # A previous failure is terminal and a new attempt is allowed.
                runtime = _FakeRuntime(result)
                with self.assertRaises(OwnerInventorySyncError):
                    self._sync(runtime)
                self.assertTrue(runtime.closed)
                self.assertEqual(self.client.sync_status()["state"], "failed")
                self.assertEqual(self.client.sync_status()["latest_attempt"]["error_kind"], "invalid_inventory")
                self.assertEqual(self.client.ad_history("100"), ())

    def test_lost_final_ack_is_unknown_and_never_retried(self) -> None:
        runtime = _FakeRuntime(ReadResult.success_nonempty((self._ad(),)))
        original_call = self.client._call
        sent = 0

        def lose_final_ack(op: str, **kwargs):
            nonlocal sent
            result = original_call(op, **kwargs)
            if op == "sync_finish":
                sent += 1
                raise StorageOwnerError("response lost after persisted commit")
            return result

        with patch.object(self.client, "_call", side_effect=lose_final_ack):
            with self.assertRaisesRegex(OwnerInventorySyncError, "uncertain; no automatic retry"):
                self._sync(runtime)
        self.assertEqual(sent, 1)
        self.assertTrue(runtime.closed)
        self.assertEqual(self.client.sync_status()["state"], "success_nonempty")
        self.assertEqual(self.client.ad_history("100"), (self._ad(),))

    def test_wire_invalid_observation_never_stages_or_leaves_open_attempt(self) -> None:
        # AdSnapshot constructors accept data beyond the owner RPC schema.
        # The CLI must classify it BEFORE any ambiguous sync_stage RPC.
        for invalid in (
            AdSnapshot(
                ad_id="100", observed_at=NOW, source="management-observed",
                lifecycle_state=LifecycleState.ACTIVE, title="x" * 40000,
            ),
            AdSnapshot(
                ad_id="100", observed_at=NOW, source="management-observed",
                lifecycle_state=LifecycleState.ACTIVE, title={"not": "text"},
            ),
            AdSnapshot(
                ad_id="100", observed_at=NOW, source="management-observed",
                lifecycle_state=LifecycleState.ACTIVE, views=2**63,
            ),
        ):
            with self.subTest(invalid=type(invalid.title).__name__):
                with self.assertRaises(OwnerInventorySyncError):
                    self._sync(_FakeRuntime(ReadResult.success_nonempty((invalid,))))
                self.assertEqual(self.client.sync_status()["state"], "failed")
                self.assertEqual(
                    self.client.sync_status()["latest_attempt"]["error_kind"],
                    "invalid_inventory",
                )
        # A classified pre-execution failure must not indefinitely hold a
        # generation-local, unsatisfied storage stage lock.
        receipt = self._sync(_FakeRuntime(ReadResult.success_empty()))
        self.assertEqual(receipt.observed_count, 0)

    def test_teardown_error_must_not_mask_committed_or_unknown_outcome(self) -> None:
        class _BadClose(_FakeRuntime):
            def close(self) -> None:
                self.closed = True
                raise RuntimeError("sensitive cleanup failure")

        complete = _BadClose(ReadResult.success_nonempty((self._ad(),)))
        with self.assertRaises(OwnerInventorySyncError) as observed:
            self._sync(complete)
        self.assertIn("cleanup", str(observed.exception))
        self.assertIn("no retry", str(observed.exception))
        self.assertEqual(observed.exception.kind, "cleanup_after_commit")
        self.assertTrue(observed.exception.cleanup_failed)
        self.assertEqual(self.client.sync_status()["state"], "success_nonempty")
        self.assertEqual(self.client.ad_history("100"), (self._ad(),))

        lose = _BadClose(ReadResult.success_nonempty((self._ad("200"),)))
        original_call = self.client._call

        def lost_finish(op: str, **kwargs):
            answer = original_call(op, **kwargs)
            if op == "sync_finish":
                raise StorageOwnerError("lost reply")
            return answer

        with patch.object(self.client, "_call", side_effect=lost_finish):
            with self.assertRaisesRegex(
                OwnerInventorySyncError, "commit outcome uncertain",
            ) as uncertain:
                self._sync(lose)
        self.assertEqual(uncertain.exception.kind, "storage_unknown")
        self.assertTrue(uncertain.exception.cleanup_failed)
        self.assertTrue(lose.closed)
        self.assertEqual(self.client.ad_history("200"), (self._ad("200"),))

    def test_cli_emits_structured_distinct_failures_without_secrets(self) -> None:
        from contextlib import redirect_stderr
        from io import StringIO
        import json
        import mark_api.owner_inventory_cli as cli

        results = []
        for description, kind in (
            ("private inventory read unsuccessful: unauthenticated", "read_failed"),
            ("owner inventory commit outcome uncertain; no automatic retry", "storage_unknown"),
        ):
            out = StringIO()
            with (
                patch.object(cli, "container_draft_denied", return_value=False),
                patch.object(cli.os, "geteuid", return_value=50044),
                patch.object(cli, "sync_owner_inventory", side_effect=OwnerInventorySyncError(description, kind=kind, attempt_id=7)),
                redirect_stderr(out),
            ):
                self.assertEqual(
                    cli.main(["--storage-socket", str(self.socket),
                              "--storage-owner-uid", "50046", "--cdp-port", "9222"]), 2,
                )
            results.append(json.loads(out.getvalue()))
        self.assertEqual([item["kind"] for item in results], ["read_failed", "storage_unknown"])
        self.assertEqual([item["attempt_id"] for item in results], [7, 7])
        self.assertFalse(results[0]["platform_retry_authorized"])
        self.assertFalse(results[1]["platform_retry_authorized"])
        self.assertFalse(results[0]["cleanup_failed"])
        self.assertFalse(results[1]["cleanup_failed"])

    def test_bad_second_snapshot_and_unicode_frame_fail_before_any_stage(self) -> None:
        valid = self._ad("400")
        invalid_cases = (
            AdSnapshot(
                ad_id="401", observed_at=NOW, source="synthetic",
                lifecycle_state=LifecycleState.ACTIVE, description="😀" * 8192,
            ),
            AdSnapshot(
                ad_id="401", observed_at=NOW, source="synthetic",
                lifecycle_state=LifecycleState.ACTIVE, title="\ud800",
            ),
        )
        for invalid in invalid_cases:
            called_stages = 0
            original_call = self.client._call

            def count_stages(op: str, **kwargs):
                nonlocal called_stages
                if op == "sync_stage":
                    called_stages += 1
                return original_call(op, **kwargs)

            with patch.object(self.client, "_call", side_effect=count_stages):
                with self.assertRaises(OwnerInventorySyncError) as result:
                    self._sync(_FakeRuntime(ReadResult.success_nonempty((valid, invalid))))
            self.assertEqual(result.exception.kind, "invalid_inventory")
            self.assertEqual(called_stages, 0)
            self.assertEqual(self.client.sync_status()["state"], "failed")
        self.assertEqual(self.client.ad_history("400"), ())

    def test_lost_sync_failure_ack_stays_unknown_without_retry(self) -> None:
        original_call = self.client._call
        failures = 0

        def lost_failure(op: str, **kwargs):
            nonlocal failures
            result = original_call(op, **kwargs)
            if op == "sync_fail":
                failures += 1
                raise StorageOwnerError("ack lost after recorded failure")
            return result

        with patch.object(self.client, "_call", side_effect=lost_failure):
            with self.assertRaises(OwnerInventorySyncError) as outcome:
                self._sync(_FakeRuntime(ReadResult.failure(ReadStatus.HTTP_ERROR)))
        self.assertEqual(failures, 1)
        self.assertEqual(outcome.exception.kind, "storage_unknown")
        self.assertIsNotNone(outcome.exception.attempt_id)
        self.assertEqual(self.client.sync_status()["state"], "failed")

    def test_secondary_cleanup_error_keeps_read_failure_classified(self) -> None:
        class _BadClose(_FakeRuntime):
            def close(self) -> None:
                self.closed = True
                raise RuntimeError("private cleanup secret")

        failed = _BadClose(ReadResult.failure(ReadStatus.UNAUTHENTICATED))
        with self.assertRaises(OwnerInventorySyncError) as response:
            self._sync(failed)
        self.assertEqual(response.exception.kind, "read_failed")
        self.assertTrue(response.exception.cleanup_failed)
        self.assertTrue(failed.closed)
        self.assertEqual(self.client.sync_status()["state"], "failed")

    def test_missing_or_wrong_storage_uid_fails_before_any_browser_read(self) -> None:
        runtime = _FakeRuntime(ReadResult.success_empty(()))
        invalid = StorageOwnerClient(self.socket, expected_owner_uid=os.geteuid() + 100)
        with self.assertRaises(StorageOwnerError):
            sync_owner_inventory(invalid, cdp_port=9222,
                                 runtime_factory=lambda **kw: runtime)
        self.assertEqual(runtime.reads, 0)
        self.assertEqual(self.client.sync_status()["state"], "never_attempted")

    def test_cli_preserves_unattested_draft_entrypoint_denial(self) -> None:
        with patch("mark_api.owner_inventory_cli.container_draft_denied", return_value=True):
            self.assertEqual(main([]), 2)

    def test_invalid_cdp_and_clock_are_rejected_before_any_runtime_read(self) -> None:
        runtime = _FakeRuntime(ReadResult.success_nonempty((self._ad(),)))
        for invalid in (0, -1, 65536, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                sync_owner_inventory(
                    self.client, cdp_port=invalid,
                    runtime_factory=lambda **kw: runtime,
                )
        self.assertEqual(runtime.reads, 0)
        with self.assertRaises(OwnerInventorySyncError):
            sync_owner_inventory(
                self.client, cdp_port=9222, clock=lambda: datetime(2026, 10, 12),
                runtime_factory=lambda **kw: runtime,
            )
        self.assertEqual(runtime.reads, 0)


if __name__ == "__main__":
    unittest.main()