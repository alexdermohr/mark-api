from __future__ import annotations

import unittest
from datetime import datetime, timezone

from mark_api.domain import (
    AdCreateRequest,
    AdSnapshot,
    DeleteApproval,
    LifecycleState,
    OperationOutcome,
)
from mark_api.orchestrator import SafeWriteOrchestrator
from mark_api.ports import WriteNotAttemptedError
from mark_api.results import ReadResult, ReadStatus


NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def snapshot(
    state: LifecycleState,
    *,
    ad_id: str = "3521676801",
    title: str | None = "title",
    description: str | None = "description",
) -> AdSnapshot:
    return AdSnapshot(
        ad_id=ad_id,
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


class CreateWriter:
    def __init__(self, error: Exception | None = None):
        self.calls = 0
        self.error = error
        self.last_request: AdCreateRequest | None = None

    def create_ad(self, request: AdCreateRequest) -> None:
        self.calls += 1
        self.last_request = request
        if self.error is not None:
            raise self.error


def create_request() -> AdCreateRequest:
    return AdCreateRequest(
        category_path=("Haus & Garten", "Dekoration", "Weitere Dekoration"),
        title="Neue Vase",
        description="Beschreibung der neuen Vase",
        price_eur=12,
    )


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

    def test_create_writes_disabled_before_reads_or_writer(self) -> None:
        reader = SequenceReader()
        confirmation = SequenceReader()
        writer = CreateWriter()

        receipt = self.service(enabled=False).create(
            request=create_request(),
            reader=reader,
            confirmation_reader=confirmation,
            writer=writer,
            content_reader_factory=lambda _ad_id: SequenceReader(),
            authorization_by="api-owner",
            authorization_reference="write-api:create-disabled",
        )

        self.assertEqual(receipt.outcome, OperationOutcome.PRECONDITION_FAILED)
        self.assertEqual(receipt.pre_read_status, "writes_disabled")
        self.assertFalse(receipt.writer_invoked)
        self.assertEqual(receipt.authorization_by, "api-owner")
        self.assertEqual(
            receipt.authorization_reference,
            "write-api:create-disabled",
        )
        self.assertEqual(reader.calls, 0)
        self.assertEqual(confirmation.calls, 0)
        self.assertEqual(writer.calls, 0)

    def test_create_requires_writer_and_content_confirmation_before_reads(self) -> None:
        reader = SequenceReader()
        confirmation = SequenceReader()

        missing_writer = self.service().create(
            request=create_request(),
            reader=reader,
            confirmation_reader=confirmation,
            writer=None,
            content_reader_factory=lambda _ad_id: SequenceReader(),
        )
        self.assertEqual(
            missing_writer.pre_read_status,
            "create_writer_unavailable",
        )
        self.assertEqual(reader.calls, 0)
        self.assertEqual(confirmation.calls, 0)

        missing_content = self.service().create(
            request=create_request(),
            reader=reader,
            confirmation_reader=confirmation,
            writer=CreateWriter(),
            content_reader_factory=None,
        )
        self.assertEqual(
            missing_content.confirmation_pre_read_status,
            "content_confirmation_unavailable",
        )
        self.assertEqual(reader.calls, 0)
        self.assertEqual(confirmation.calls, 0)

    def test_create_accepts_same_inventory_reader_with_fresh_reads_and_exact_content(self) -> None:
        request = create_request()
        created = snapshot(
            LifecycleState.ACTIVE,
            ad_id="200",
            title=request.title,
            description=None,
        )
        exact_content = snapshot(
            LifecycleState.ACTIVE,
            ad_id="200",
            title=request.title,
            description=request.description,
        )
        reader = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_empty(()),
            ReadResult.success_nonempty((created,)),
            ReadResult.success_nonempty((created,)),
        )
        content_reader = SequenceReader(
            ReadResult.success_nonempty((exact_content,)),
        )
        writer = CreateWriter()

        receipt = self.service().create(
            request=request,
            reader=reader,
            confirmation_reader=reader,
            writer=writer,
            content_reader_factory=lambda _ad_id: content_reader,
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(receipt.created_ad_id, "200")
        self.assertEqual(reader.calls, 4)
        self.assertEqual(content_reader.calls, 1)
        self.assertEqual(writer.calls, 1)

    def test_create_confirms_one_new_id_in_both_inventories_and_exact_content(self) -> None:
        request = create_request()
        old_primary = snapshot(
            LifecycleState.ACTIVE,
            ad_id="100",
            title="Alt",
        )
        old_confirmation = snapshot(
            LifecycleState.ACTIVE,
            ad_id="100",
            title="Alt",
        )
        new_primary = snapshot(
            LifecycleState.ACTIVE,
            ad_id="200",
            title=request.title,
            description=None,
        )
        new_confirmation = snapshot(
            LifecycleState.ACTIVE,
            ad_id="200",
            title=request.title,
            description=None,
        )
        exact_content = snapshot(
            LifecycleState.ACTIVE,
            ad_id="200",
            title=request.title,
            description=request.description,
        )
        reader = SequenceReader(
            ReadResult.success_nonempty((old_primary,)),
            ReadResult.success_nonempty((old_primary, new_primary)),
        )
        confirmation = SequenceReader(
            ReadResult.success_nonempty((old_confirmation,)),
            ReadResult.success_nonempty((old_confirmation, new_confirmation)),
        )
        content_reader = SequenceReader(
            ReadResult.success_nonempty((exact_content,))
        )
        writer = CreateWriter()
        factory_calls: list[str] = []

        def factory(ad_id: str):
            factory_calls.append(ad_id)
            return content_reader

        receipt = self.service().create(
            request=request,
            reader=reader,
            confirmation_reader=confirmation,
            writer=writer,
            content_reader_factory=factory,
            authorization_by="api-owner",
            authorization_reference="write-api:create-confirmed",
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(receipt.created_ad_id, "200")
        self.assertEqual(receipt.authorization_by, "api-owner")
        self.assertEqual(
            receipt.authorization_reference,
            "write-api:create-confirmed",
        )
        self.assertEqual(receipt.content_post_snapshot, exact_content)
        self.assertEqual(writer.calls, 1)
        self.assertEqual(factory_calls, ["200"])
        self.assertEqual(reader.calls, 2)
        self.assertEqual(confirmation.calls, 2)
        self.assertEqual(content_reader.calls, 1)

    def test_create_rejects_inventory_reader_as_content_confirmation(self) -> None:
        request = create_request()
        candidate = snapshot(
            LifecycleState.ACTIVE,
            ad_id="200",
            title=request.title,
        )
        reader = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty((candidate,)),
        )
        confirmation = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty((candidate,)),
        )

        receipt = self.service().create(
            request=request,
            reader=reader,
            confirmation_reader=confirmation,
            writer=CreateWriter(),
            content_reader_factory=lambda _ad_id: reader,
        )

        self.assertEqual(receipt.outcome, OperationOutcome.AMBIGUOUS)
        self.assertIsNone(receipt.created_ad_id)
        self.assertEqual(
            receipt.content_post_read_status,
            "content_reader_not_independent",
        )
        self.assertEqual(reader.calls, 2)
        self.assertEqual(confirmation.calls, 2)

    def test_create_is_ambiguous_when_inventories_disagree_on_new_id(self) -> None:
        request = create_request()
        reader = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty(
                (
                    snapshot(
                        LifecycleState.ACTIVE,
                        ad_id="200",
                        title=request.title,
                    ),
                )
            ),
        )
        confirmation = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty(
                (
                    snapshot(
                        LifecycleState.ACTIVE,
                        ad_id="201",
                        title=request.title,
                    ),
                )
            ),
        )
        writer = CreateWriter()
        factory_calls: list[str] = []

        receipt = self.service().create(
            request=request,
            reader=reader,
            confirmation_reader=confirmation,
            writer=writer,
            content_reader_factory=lambda ad_id: (
                factory_calls.append(ad_id) or SequenceReader()
            ),
        )

        self.assertEqual(receipt.outcome, OperationOutcome.AMBIGUOUS)
        self.assertIsNone(receipt.created_ad_id)
        self.assertEqual(factory_calls, [])
        self.assertEqual(writer.calls, 1)

    def test_create_is_ambiguous_when_multiple_new_ids_appear(self) -> None:
        request = create_request()
        post = (
            snapshot(
                LifecycleState.ACTIVE,
                ad_id="200",
                title=request.title,
            ),
            snapshot(
                LifecycleState.ACTIVE,
                ad_id="201",
                title=request.title,
            ),
        )
        reader = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty(post),
        )
        confirmation = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty(post),
        )
        writer = CreateWriter()

        receipt = self.service().create(
            request=request,
            reader=reader,
            confirmation_reader=confirmation,
            writer=writer,
            content_reader_factory=lambda _ad_id: SequenceReader(),
        )

        self.assertEqual(receipt.outcome, OperationOutcome.AMBIGUOUS)
        self.assertIsNone(receipt.created_ad_id)

    def test_create_exact_inventory_candidate_still_needs_content_match(self) -> None:
        request = create_request()
        candidate = snapshot(
            LifecycleState.ACTIVE,
            ad_id="200",
            title=request.title,
        )
        reader = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty((candidate,)),
        )
        confirmation = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty((candidate,)),
        )
        content_reader = SequenceReader(
            ReadResult.success_nonempty(
                (
                    snapshot(
                        LifecycleState.ACTIVE,
                        ad_id="200",
                        title=request.title,
                        description="anderer Inhalt",
                    ),
                )
            )
        )

        receipt = self.service().create(
            request=request,
            reader=reader,
            confirmation_reader=confirmation,
            writer=CreateWriter(),
            content_reader_factory=lambda _ad_id: content_reader,
        )

        self.assertEqual(receipt.outcome, OperationOutcome.AMBIGUOUS)
        self.assertIsNone(receipt.created_ad_id)
        self.assertEqual(content_reader.calls, 1)

    def test_create_write_not_attempted_skips_all_post_reads(self) -> None:
        reader = SequenceReader(ReadResult.success_empty(()))
        confirmation = SequenceReader(ReadResult.success_empty(()))
        writer = CreateWriter(
            error=WriteNotAttemptedError("sanitized safe failure")
        )
        factory_calls: list[str] = []

        receipt = self.service().create(
            request=create_request(),
            reader=reader,
            confirmation_reader=confirmation,
            writer=writer,
            content_reader_factory=lambda ad_id: (
                factory_calls.append(ad_id) or SequenceReader()
            ),
        )

        self.assertEqual(receipt.outcome, OperationOutcome.PRECONDITION_FAILED)
        self.assertTrue(receipt.writer_invoked)
        self.assertEqual(receipt.writer_error, "WriteNotAttemptedError")
        self.assertEqual(reader.calls, 1)
        self.assertEqual(confirmation.calls, 1)
        self.assertEqual(factory_calls, [])
        self.assertIsNone(receipt.post_read_status)

    def test_create_unknown_writer_error_can_be_confirmed_without_retry(self) -> None:
        request = create_request()
        candidate = snapshot(
            LifecycleState.ACTIVE,
            ad_id="200",
            title=request.title,
        )
        exact_content = snapshot(
            LifecycleState.ACTIVE,
            ad_id="200",
            title=request.title,
            description=request.description,
        )
        reader = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty((candidate,)),
        )
        confirmation = SequenceReader(
            ReadResult.success_empty(()),
            ReadResult.success_nonempty((candidate,)),
        )
        writer = CreateWriter(error=TimeoutError("unknown delivery"))

        receipt = self.service().create(
            request=request,
            reader=reader,
            confirmation_reader=confirmation,
            writer=writer,
            content_reader_factory=lambda _ad_id: SequenceReader(
                ReadResult.success_nonempty((exact_content,))
            ),
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(receipt.created_ad_id, "200")
        self.assertEqual(receipt.writer_error, "TimeoutError")
        self.assertEqual(writer.calls, 1)

    def test_state_receipt_preserves_authorization_metadata(self) -> None:
        reader = SequenceReader(
            ReadResult.success_nonempty(
                (snapshot(LifecycleState.ACTIVE),)
            ),
            ReadResult.success_nonempty(
                (snapshot(LifecycleState.PAUSED),)
            ),
        )
        writer = StateWriter()

        receipt = self.service().set_state(
            ad_id="3521676801",
            target_state=LifecycleState.PAUSED,
            reader=reader,
            writer=writer,
            authorization_by="api-owner",
            authorization_reference="write-api:pause-1",
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(receipt.authorization_by, "api-owner")
        self.assertEqual(
            receipt.authorization_reference,
            "write-api:pause-1",
        )

    def test_content_receipt_preserves_authorization_metadata(self) -> None:
        reader = SequenceReader(
            ReadResult.success_nonempty(
                (
                    snapshot(
                        LifecycleState.ACTIVE,
                        title="old",
                    ),
                )
            ),
            ReadResult.success_nonempty(
                (
                    snapshot(
                        LifecycleState.ACTIVE,
                        title="new",
                    ),
                )
            ),
        )
        writer = ContentWriter()

        receipt = self.service().update_content(
            ad_id="3521676801",
            reader=reader,
            writer=writer,
            title="new",
            authorization_by="api-owner",
            authorization_reference="write-api:patch-1",
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(receipt.authorization_by, "api-owner")
        self.assertEqual(
            receipt.authorization_reference,
            "write-api:patch-1",
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

    def test_delete_confirms_only_when_both_owner_inventories_are_absent(self) -> None:
        reader = SequenceReader(
            ReadResult.success_nonempty((snapshot(LifecycleState.ACTIVE),)),
            ReadResult.success_empty(()),
        )
        confirmation_reader = SequenceReader(ReadResult.success_empty(()))
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
            confirmation_reader=confirmation_reader,
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertTrue(receipt.writer_invoked)
        self.assertEqual(receipt.authorization_by, "test-owner")
        self.assertEqual(receipt.authorization_reference, "issue-3-predelete")
        self.assertEqual(writer.calls, 1)
        self.assertEqual(reader.calls, 2)
        self.assertEqual(confirmation_reader.calls, 1)
        self.assertIsNone(receipt.post_snapshot)

    def test_delete_without_second_inventory_is_ambiguous(self) -> None:
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
            ),
            reader=reader,
            writer=writer,
        )

        self.assertEqual(receipt.outcome, OperationOutcome.AMBIGUOUS)
        self.assertEqual(writer.calls, 1)
        self.assertEqual(reader.calls, 2)

    def test_delete_is_ambiguous_when_second_inventory_still_contains_target(self) -> None:
        reader = SequenceReader(
            ReadResult.success_nonempty((snapshot(LifecycleState.ACTIVE),)),
            ReadResult.success_empty(()),
        )
        confirmation_reader = SequenceReader(
            ReadResult.success_nonempty((snapshot(LifecycleState.ACTIVE),))
        )
        writer = DeleteWriter()

        receipt = self.service().delete(
            ad_id="3521676801",
            approval=DeleteApproval(
                ad_id="3521676801",
                approved_by="test-owner",
            ),
            reader=reader,
            writer=writer,
            confirmation_reader=confirmation_reader,
        )

        self.assertEqual(receipt.outcome, OperationOutcome.AMBIGUOUS)
        self.assertEqual(confirmation_reader.calls, 1)

    def test_delete_is_ambiguous_when_second_inventory_fails(self) -> None:
        reader = SequenceReader(
            ReadResult.success_nonempty((snapshot(LifecycleState.ACTIVE),)),
            ReadResult.success_empty(()),
        )
        confirmation_reader = SequenceReader(
            ReadResult.failure(ReadStatus.HTTP_ERROR, http_status=500)
        )
        writer = DeleteWriter()

        receipt = self.service().delete(
            ad_id="3521676801",
            approval=DeleteApproval(
                ad_id="3521676801",
                approved_by="test-owner",
            ),
            reader=reader,
            writer=writer,
            confirmation_reader=confirmation_reader,
        )

        self.assertEqual(receipt.outcome, OperationOutcome.AMBIGUOUS)
        self.assertEqual(confirmation_reader.calls, 1)

    def test_delete_write_not_attempted_error_is_non_ambiguous(self) -> None:
        reader = SequenceReader(
            ReadResult.success_nonempty((snapshot(LifecycleState.ACTIVE),)),
        )
        confirmation_reader = SequenceReader()
        writer = DeleteWriter(
            error=WriteNotAttemptedError("sanitized safe failure"),
        )

        receipt = self.service().delete(
            ad_id="3521676801",
            approval=DeleteApproval(
                ad_id="3521676801",
                approved_by="test-owner",
            ),
            reader=reader,
            writer=writer,
            confirmation_reader=confirmation_reader,
        )

        self.assertEqual(
            receipt.outcome,
            OperationOutcome.PRECONDITION_FAILED,
        )
        self.assertTrue(receipt.writer_invoked)
        self.assertEqual(receipt.writer_error, "WriteNotAttemptedError")
        self.assertIsNone(receipt.post_read_status)
        self.assertIsNone(receipt.post_snapshot)
        self.assertEqual(reader.calls, 1)
        self.assertEqual(confirmation_reader.calls, 0)
        self.assertEqual(writer.calls, 1)

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

    def test_writer_exception_can_be_confirmed_by_both_post_readbacks(self) -> None:
        reader = SequenceReader(
            ReadResult.success_nonempty((snapshot(LifecycleState.ACTIVE),)),
            ReadResult.success_empty(()),
        )
        confirmation_reader = SequenceReader(ReadResult.success_empty(()))
        writer = DeleteWriter(error=TimeoutError("unknown delivery"))

        receipt = self.service().delete(
            ad_id="3521676801",
            approval=DeleteApproval(
                ad_id="3521676801",
                approved_by="test-owner",
            ),
            reader=reader,
            writer=writer,
            confirmation_reader=confirmation_reader,
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(receipt.writer_error, "TimeoutError")
        self.assertEqual(writer.calls, 1)
        self.assertEqual(confirmation_reader.calls, 1)

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



    def test_state_write_not_attempted_error_is_non_ambiguous(self) -> None:
        reader = SequenceReader(
            ReadResult.success_nonempty((snapshot(LifecycleState.ACTIVE),)),
        )
        writer = StateWriter(
            error=WriteNotAttemptedError("sanitized safe failure"),
        )

        receipt = self.service().set_state(
            ad_id="3521676801",
            target_state=LifecycleState.PAUSED,
            reader=reader,
            writer=writer,
        )

        self.assertEqual(
            receipt.outcome,
            OperationOutcome.PRECONDITION_FAILED,
        )
        self.assertTrue(receipt.writer_invoked)
        self.assertEqual(receipt.writer_error, "WriteNotAttemptedError")
        self.assertIsNone(receipt.post_read_status)
        self.assertIsNone(receipt.post_snapshot)
        self.assertEqual(reader.calls, 1)
        self.assertEqual(writer.calls, 1)

    def test_state_change_same_target_state_is_precondition_noop(self) -> None:
        for state in (LifecycleState.ACTIVE, LifecycleState.PAUSED):
            with self.subTest(state=state):
                reader = SequenceReader(
                    ReadResult.success_nonempty((snapshot(state),)),
                )
                writer = StateWriter()

                receipt = self.service().set_state(
                    ad_id="3521676801",
                    target_state=state,
                    reader=reader,
                    writer=writer,
                )

                self.assertEqual(
                    receipt.outcome,
                    OperationOutcome.PRECONDITION_FAILED,
                )
                self.assertFalse(receipt.writer_invoked)
                self.assertEqual(writer.calls, 0)
                self.assertEqual(reader.calls, 1)

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
