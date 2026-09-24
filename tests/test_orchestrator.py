from __future__ import annotations

import unittest
from datetime import datetime, timezone

from mark_api.domain import (
    AdSnapshot,
    DeleteApproval,
    LifecycleState,
    OperationOutcome,
)
from mark_api.orchestrator import SafeWriteOrchestrator
from mark_api.results import ReadResult


NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def snapshot(
    state: LifecycleState,
    *,
    title: str | None = "title",
    description: str | None = "description",
) -> AdSnapshot:
    return AdSnapshot(
        ad_id="3521676801",
        observed_at=NOW,
        source="test",
        lifecycle_state=state,
        title=title,
        description=description,
    )


class SequenceReader:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def read_ads(self):
        self.calls += 1
        if not self.results:
            raise AssertionError("unexpected read")
        return self.results.pop(0)


class DeleteWriter:
    def __init__(self, error: Exception | None = None):
        self.calls = 0
        self.error = error

    def delete_ad(self, ad_id: str) -> None:
        self.calls += 1
        self.last_ad_id = ad_id
        if self.error is not None:
            raise self.error


class StateWriter:
    def __init__(self, error: Exception | None = None):
        self.calls = 0
        self.error = error

    def set_state(self, ad_id: str, state: LifecycleState) -> None:
        self.calls += 1
        self.last = (ad_id, state)
        if self.error is not None:
            raise self.error


class ContentWriter:
    def __init__(self):
        self.calls = 0

    def update_content(
        self,
        ad_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        self.calls += 1
        self.last = (ad_id, title, description)


class SafeWriteOrchestratorTests(unittest.TestCase):
    def service(self, *, enabled: bool = True) -> SafeWriteOrchestrator:
        return SafeWriteOrchestrator(
            clock=lambda: NOW,
            writes_enabled=enabled,
        )

    def test_writes_are_disabled_by_default(self) -> None:
        reader = SequenceReader()
        writer = DeleteWriter()

        receipt = self.service(enabled=False).delete(
            ad_id="3521676801",
            approval=DeleteApproval(
                ad_id="3521676801",
                approved_by="test-owner",
                reference="approval-1",
            ),
            reader=reader,
            writer=writer,
        )

        self.assertEqual(receipt.outcome, OperationOutcome.PRECONDITION_FAILED)
        self.assertEqual(receipt.pre_read_status, "writes_disabled")
        self.assertEqual(receipt.authorization_by, "test-owner")
        self.assertEqual(receipt.authorization_reference, "approval-1")
        self.assertEqual(reader.calls, 0)
        self.assertEqual(writer.calls, 0)

    def test_delete_requires_approval_for_exact_id(self) -> None:
        reader = SequenceReader()
        writer = DeleteWriter()

        receipt = self.service().delete(
            ad_id="3521676801",
            approval=DeleteApproval(
                ad_id="other",
                approved_by="test-owner",
            ),
            reader=reader,
            writer=writer,
        )

        self.assertEqual(receipt.outcome, OperationOutcome.PRECONDITION_FAILED)
        self.assertEqual(receipt.pre_read_status, "approval_mismatch")
        self.assertFalse(receipt.writer_invoked)
        self.assertEqual(reader.calls, 0)
        self.assertEqual(writer.calls, 0)

    def test_delete_runs_once_and_confirms_from_owner_inventory_absence(self) -> None:
        reader = SequenceReader(
            ReadResult.success_nonempty((snapshot(LifecycleState.ACTIVE),)),
            ReadResult.success_empty(()),
        )
        writer = DeleteWriter()

        receipt = self.service().delete(
            ad_id="3521676801",
            approval=DeleteApproval(
                ad_id="3521676801",
                approved_by="test-owner",
                reference="issue-3-predelete",
            ),
            reader=reader,
            writer=writer,
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertTrue(receipt.writer_invoked)
        self.assertEqual(receipt.authorization_by, "test-owner")
        self.assertEqual(receipt.authorization_reference, "issue-3-predelete")
        self.assertEqual(writer.calls, 1)
        self.assertEqual(reader.calls, 2)
        self.assertIsNone(receipt.post_snapshot)

    def test_writer_exception_is_not_retried_and_unconfirmed_is_ambiguous(self) -> None:
        active = snapshot(LifecycleState.ACTIVE)
        reader = SequenceReader(
            ReadResult.success_nonempty((active,)),
            ReadResult.success_nonempty((active,)),
        )
        writer = DeleteWriter(error=RuntimeError("provider body with secret"))

        receipt = self.service().delete(
            ad_id="3521676801",
            approval=DeleteApproval(
                ad_id="3521676801",
                approved_by="test-owner",
            ),
            reader=reader,
            writer=writer,
        )

        self.assertEqual(receipt.outcome, OperationOutcome.AMBIGUOUS)
        self.assertEqual(writer.calls, 1)
        self.assertEqual(reader.calls, 2)
        self.assertEqual(receipt.writer_error, "RuntimeError")
        self.assertNotIn("secret", receipt.writer_error)

    def test_writer_exception_can_be_confirmed_by_post_readback(self) -> None:
        reader = SequenceReader(
            ReadResult.success_nonempty((snapshot(LifecycleState.ACTIVE),)),
            ReadResult.success_empty(()),
        )
        writer = DeleteWriter(error=TimeoutError("unknown delivery"))

        receipt = self.service().delete(
            ad_id="3521676801",
            approval=DeleteApproval(
                ad_id="3521676801",
                approved_by="test-owner",
            ),
            reader=reader,
            writer=writer,
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(receipt.writer_error, "TimeoutError")
        self.assertEqual(writer.calls, 1)

    def test_state_change_uses_expected_pre_and_post_state(self) -> None:
        reader = SequenceReader(
            ReadResult.success_nonempty((snapshot(LifecycleState.ACTIVE),)),
            ReadResult.success_nonempty((snapshot(LifecycleState.PAUSED),)),
        )
        writer = StateWriter()

        receipt = self.service().set_state(
            ad_id="3521676801",
            target_state=LifecycleState.PAUSED,
            reader=reader,
            writer=writer,
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(writer.calls, 1)

    def test_state_change_wrong_precondition_does_not_write(self) -> None:
        reader = SequenceReader(
            ReadResult.success_nonempty((snapshot(LifecycleState.PENDING),)),
        )
        writer = StateWriter()

        receipt = self.service().set_state(
            ad_id="3521676801",
            target_state=LifecycleState.PAUSED,
            reader=reader,
            writer=writer,
        )

        self.assertEqual(receipt.outcome, OperationOutcome.PRECONDITION_FAILED)
        self.assertEqual(writer.calls, 0)

    def test_content_update_requires_independent_matching_readback(self) -> None:
        reader = SequenceReader(
            ReadResult.success_nonempty(
                (snapshot(LifecycleState.ACTIVE, description="old"),)
            ),
            ReadResult.success_nonempty(
                (snapshot(LifecycleState.ACTIVE, description="new"),)
            ),
        )
        writer = ContentWriter()

        receipt = self.service().update_content(
            ad_id="3521676801",
            reader=reader,
            writer=writer,
            description="new",
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(writer.calls, 1)


if __name__ == "__main__":
    unittest.main()