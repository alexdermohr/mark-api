from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from .domain import (
    AdCreateRequest,
    AdSnapshot,
    CreateOperationReceipt,
    DeleteApproval,
    LifecycleState,
    OperationOutcome,
    OperationReceipt,
)
from .ports import (
    AdContentUpdater,
    AdCreateWriter,
    AdDeleteWriter,
    AdsReader,
    AdStateWriter,
    WriteNotAttemptedError,
)
from .results import ReadResult
from .storage import SnapshotStore


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SafeWriteOrchestrator:
    """Execute one write attempt with fresh pre/post owner-inventory readbacks."""

    def __init__(
        self,
        *,
        store: SnapshotStore | None = None,
        clock: Callable[[], datetime] = _utc_now,
        writes_enabled: bool = False,
    ) -> None:
        self._store = store
        self._clock = clock
        self._writes_enabled = writes_enabled

    @staticmethod
    def _find_target(
        result: ReadResult[tuple[AdSnapshot, ...]],
        ad_id: str,
    ) -> AdSnapshot | None:
        if not result.is_success:
            return None
        for snapshot in result.value or ():
            if snapshot.ad_id == ad_id:
                return snapshot
        return None

    def _persist(self, receipt: OperationReceipt) -> OperationReceipt:
        if self._store is not None:
            self._store.append_operation_receipt(receipt)
        return receipt

    def _persist_create(
        self,
        receipt: CreateOperationReceipt,
    ) -> CreateOperationReceipt:
        if self._store is not None:
            self._store.append_create_operation_receipt(receipt)
        return receipt

    @staticmethod
    def _inventory_index(
        result: ReadResult[tuple[AdSnapshot, ...]],
    ) -> dict[str, AdSnapshot] | None:
        if not result.is_success:
            return None
        index: dict[str, AdSnapshot] = {}
        for snapshot in result.value or ():
            if not isinstance(snapshot, AdSnapshot):
                return None
            if snapshot.ad_id in index:
                return None
            index[snapshot.ad_id] = snapshot
        return index

    def _precondition_failed(
        self,
        *,
        operation: str,
        ad_id: str,
        started_at: datetime,
        pre_read_status: str,
        pre_snapshot: AdSnapshot | None = None,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> OperationReceipt:
        return self._persist(
            OperationReceipt(
                operation=operation,
                ad_id=ad_id,
                started_at=started_at,
                completed_at=self._clock(),
                outcome=OperationOutcome.PRECONDITION_FAILED,
                pre_read_status=pre_read_status,
                post_read_status=None,
                writer_invoked=False,
                authorization_by=authorization_by,
                authorization_reference=authorization_reference,
                pre_snapshot=pre_snapshot,
            )
        )

    def _execute(
        self,
        *,
        operation: str,
        ad_id: str,
        reader: AdsReader,
        writer_call: Callable[[], None],
        postcondition: Callable[
            [ReadResult[tuple[AdSnapshot, ...]], AdSnapshot | None],
            bool,
        ],
        allowed_pre_states: frozenset[LifecycleState] | None = None,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> OperationReceipt:
        started_at = self._clock()
        if not self._writes_enabled:
            return self._precondition_failed(
                operation=operation,
                ad_id=ad_id,
                started_at=started_at,
                pre_read_status="writes_disabled",
                authorization_by=authorization_by,
                authorization_reference=authorization_reference,
            )

        pre = reader.read_ads()
        pre_snapshot = self._find_target(pre, ad_id)

        if not pre.is_success:
            return self._precondition_failed(
                operation=operation,
                ad_id=ad_id,
                started_at=started_at,
                pre_read_status=pre.status.value,
                authorization_by=authorization_by,
                authorization_reference=authorization_reference,
            )

        if pre_snapshot is None:
            return self._precondition_failed(
                operation=operation,
                ad_id=ad_id,
                started_at=started_at,
                pre_read_status=pre.status.value,
                authorization_by=authorization_by,
                authorization_reference=authorization_reference,
            )

        if (
            allowed_pre_states is not None
            and pre_snapshot.lifecycle_state not in allowed_pre_states
        ):
            return self._precondition_failed(
                operation=operation,
                ad_id=ad_id,
                started_at=started_at,
                pre_read_status=pre.status.value,
                pre_snapshot=pre_snapshot,
                authorization_by=authorization_by,
                authorization_reference=authorization_reference,
            )

        writer_error: str | None = None
        try:
            writer_call()
        except WriteNotAttemptedError as exc:
            # This marker is a writer-contract assertion that no externally
            # visible mutation was attempted. Keep the failure non-ambiguous
            # and do not manufacture a post-read for a write that cannot have
            # reached the provider.
            return self._persist(
                OperationReceipt(
                    operation=operation,
                    ad_id=ad_id,
                    started_at=started_at,
                    completed_at=self._clock(),
                    outcome=OperationOutcome.PRECONDITION_FAILED,
                    pre_read_status=pre.status.value,
                    post_read_status=None,
                    writer_invoked=True,
                    authorization_by=authorization_by,
                    authorization_reference=authorization_reference,
                    writer_error=type(exc).__name__,
                    pre_snapshot=pre_snapshot,
                )
            )
        except Exception as exc:  # noqa: BLE001 - outcome is reconciled by readback.
            # Do not persist exception messages here: adapter exceptions may contain
            # cookies, URLs or provider response bodies. The type is enough for the
            # core receipt and preserves the no-secret logging boundary.
            writer_error = type(exc).__name__

        post = reader.read_ads()
        post_snapshot = self._find_target(post, ad_id)
        confirmed = post.is_success and postcondition(post, post_snapshot)

        return self._persist(
            OperationReceipt(
                operation=operation,
                ad_id=ad_id,
                started_at=started_at,
                completed_at=self._clock(),
                outcome=(
                    OperationOutcome.CONFIRMED
                    if confirmed
                    else OperationOutcome.AMBIGUOUS
                ),
                pre_read_status=pre.status.value,
                post_read_status=post.status.value,
                writer_invoked=True,
                authorization_by=authorization_by,
                authorization_reference=authorization_reference,
                writer_error=writer_error,
                pre_snapshot=pre_snapshot,
                post_snapshot=post_snapshot,
            )
        )

    def create(
        self,
        *,
        request: AdCreateRequest,
        reader: AdsReader,
        confirmation_reader: AdsReader,
        writer: AdCreateWriter | None,
        content_reader_factory: Callable[[str], AdsReader] | None,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> CreateOperationReceipt:
        started_at = self._clock()

        def precondition(
            *,
            pre_status: str,
            confirmation_pre_status: str,
            writer_invoked: bool = False,
            writer_error: str | None = None,
        ) -> CreateOperationReceipt:
            return self._persist_create(
                CreateOperationReceipt(
                    operation="create",
                    started_at=started_at,
                    completed_at=self._clock(),
                    outcome=OperationOutcome.PRECONDITION_FAILED,
                    pre_read_status=pre_status,
                    confirmation_pre_read_status=confirmation_pre_status,
                    post_read_status=None,
                    confirmation_post_read_status=None,
                    content_post_read_status=None,
                    writer_invoked=writer_invoked,
                    authorization_by=authorization_by,
                    authorization_reference=authorization_reference,
                    writer_error=writer_error,
                )
            )

        if not self._writes_enabled:
            return precondition(
                pre_status="writes_disabled",
                confirmation_pre_status="not_read",
            )
        if writer is None:
            return precondition(
                pre_status="create_writer_unavailable",
                confirmation_pre_status="not_read",
            )
        # A second inventory reader may be the same authoritative source.
        # Safety comes from fresh before/after observations plus the separate,
        # target-bound content read below; object identity is not evidence of
        # source independence.
        if content_reader_factory is None:
            return precondition(
                pre_status="not_read",
                confirmation_pre_status="content_confirmation_unavailable",
            )

        pre = reader.read_ads()
        if not pre.is_success:
            return precondition(
                pre_status=pre.status.value,
                confirmation_pre_status="not_read",
            )
        pre_index = self._inventory_index(pre)
        if pre_index is None:
            return precondition(
                pre_status="invalid_inventory_shape",
                confirmation_pre_status="not_read",
            )

        confirmation_pre = confirmation_reader.read_ads()
        if not confirmation_pre.is_success:
            return precondition(
                pre_status=pre.status.value,
                confirmation_pre_status=confirmation_pre.status.value,
            )
        confirmation_pre_index = self._inventory_index(confirmation_pre)
        if confirmation_pre_index is None:
            return precondition(
                pre_status=pre.status.value,
                confirmation_pre_status="invalid_inventory_shape",
            )

        writer_error: str | None = None
        try:
            writer.create_ad(request)
        except WriteNotAttemptedError as exc:
            return precondition(
                pre_status=pre.status.value,
                confirmation_pre_status=confirmation_pre.status.value,
                writer_invoked=True,
                writer_error=type(exc).__name__,
            )
        except Exception as exc:  # noqa: BLE001 - reconciled by fresh readbacks.
            writer_error = type(exc).__name__

        post = reader.read_ads()
        confirmation_post = confirmation_reader.read_ads()

        post_index = self._inventory_index(post)
        confirmation_post_index = self._inventory_index(confirmation_post)
        post_snapshot: AdSnapshot | None = None
        confirmation_post_snapshot: AdSnapshot | None = None
        content_post_snapshot: AdSnapshot | None = None
        content_post_status: str | None = None
        candidate_id: str | None = None

        if post_index is not None and confirmation_post_index is not None:
            new_primary = set(post_index) - set(pre_index)
            new_confirmation = (
                set(confirmation_post_index) - set(confirmation_pre_index)
            )
            if (
                len(new_primary) == 1
                and len(new_confirmation) == 1
                and new_primary == new_confirmation
            ):
                candidate_id = next(iter(new_primary))
                post_snapshot = post_index[candidate_id]
                confirmation_post_snapshot = confirmation_post_index[candidate_id]

        inventory_match = (
            candidate_id is not None
            and post_snapshot is not None
            and confirmation_post_snapshot is not None
            and post_snapshot.title == request.title
            and confirmation_post_snapshot.title == request.title
        )

        if inventory_match and candidate_id is not None:
            try:
                content_reader = content_reader_factory(candidate_id)
            except Exception:  # noqa: BLE001 - confirmation setup is read-only.
                content_post_status = "factory_error"
            else:
                if (
                    content_reader is reader
                    or content_reader is confirmation_reader
                ):
                    content_post_status = "content_reader_not_independent"
                else:
                    content = content_reader.read_ads()
                    content_post_status = content.status.value
                    if content.is_success:
                        content_post_snapshot = self._find_target(
                            content,
                            candidate_id,
                        )

        confirmed = (
            inventory_match
            and content_post_snapshot is not None
            and content_post_snapshot.title == request.title
            and content_post_snapshot.description == request.description
        )

        return self._persist_create(
            CreateOperationReceipt(
                operation="create",
                started_at=started_at,
                completed_at=self._clock(),
                outcome=(
                    OperationOutcome.CONFIRMED
                    if confirmed
                    else OperationOutcome.AMBIGUOUS
                ),
                pre_read_status=pre.status.value,
                confirmation_pre_read_status=confirmation_pre.status.value,
                post_read_status=post.status.value,
                confirmation_post_read_status=confirmation_post.status.value,
                content_post_read_status=content_post_status,
                writer_invoked=True,
                created_ad_id=candidate_id if confirmed else None,
                authorization_by=authorization_by,
                authorization_reference=authorization_reference,
                writer_error=writer_error,
                post_snapshot=post_snapshot,
                confirmation_post_snapshot=confirmation_post_snapshot,
                content_post_snapshot=content_post_snapshot,
            )
        )

    def set_state(
        self,
        *,
        ad_id: str,
        target_state: LifecycleState,
        reader: AdsReader,
        writer: AdStateWriter,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> OperationReceipt:
        if target_state not in {LifecycleState.ACTIVE, LifecycleState.PAUSED}:
            raise ValueError("target_state must be ACTIVE or PAUSED")

        allowed_pre_states = (
            frozenset({LifecycleState.PAUSED})
            if target_state is LifecycleState.ACTIVE
            else frozenset({LifecycleState.ACTIVE})
        )
        return self._execute(
            operation=f"set_state:{target_state.value}",
            ad_id=ad_id,
            reader=reader,
            writer_call=lambda: writer.set_state(ad_id, target_state),
            allowed_pre_states=allowed_pre_states,
            authorization_by=authorization_by,
            authorization_reference=authorization_reference,
            postcondition=lambda _result, snapshot: (
                snapshot is not None
                and snapshot.lifecycle_state is target_state
            ),
        )

    def update_content(
        self,
        *,
        ad_id: str,
        reader: AdsReader,
        writer: AdContentUpdater,
        title: str | None = None,
        description: str | None = None,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> OperationReceipt:
        if title is None and description is None:
            raise ValueError("at least one content field must be provided")

        def content_matches(
            _result: ReadResult[tuple[AdSnapshot, ...]],
            snapshot: AdSnapshot | None,
        ) -> bool:
            if snapshot is None:
                return False
            if title is not None and snapshot.title != title:
                return False
            if description is not None and snapshot.description != description:
                return False
            return True

        return self._execute(
            operation="update_content",
            ad_id=ad_id,
            reader=reader,
            writer_call=lambda: writer.update_content(
                ad_id,
                title=title,
                description=description,
            ),
            allowed_pre_states=frozenset(
                {LifecycleState.ACTIVE, LifecycleState.PAUSED}
            ),
            authorization_by=authorization_by,
            authorization_reference=authorization_reference,
            postcondition=content_matches,
        )

    def delete(
        self,
        *,
        ad_id: str,
        approval: DeleteApproval,
        reader: AdsReader,
        writer: AdDeleteWriter,
        confirmation_reader: AdsReader | None = None,
    ) -> OperationReceipt:
        started_at = self._clock()
        if approval.ad_id != ad_id:
            return self._precondition_failed(
                operation="delete",
                ad_id=ad_id,
                started_at=started_at,
                pre_read_status="approval_mismatch",
                authorization_by=approval.approved_by,
                authorization_reference=approval.reference,
            )

        if not self._writes_enabled:
            return self._precondition_failed(
                operation="delete",
                ad_id=ad_id,
                started_at=started_at,
                pre_read_status="writes_disabled",
                authorization_by=approval.approved_by,
                authorization_reference=approval.reference,
            )

        pre = reader.read_ads()
        pre_snapshot = self._find_target(pre, ad_id)
        if not pre.is_success or pre_snapshot is None:
            return self._precondition_failed(
                operation="delete",
                ad_id=ad_id,
                started_at=started_at,
                pre_read_status=pre.status.value,
                pre_snapshot=pre_snapshot,
                authorization_by=approval.approved_by,
                authorization_reference=approval.reference,
            )

        writer_error: str | None = None
        try:
            writer.delete_ad(ad_id)
        except WriteNotAttemptedError as exc:
            return self._persist(
                OperationReceipt(
                    operation="delete",
                    ad_id=ad_id,
                    started_at=started_at,
                    completed_at=self._clock(),
                    outcome=OperationOutcome.PRECONDITION_FAILED,
                    pre_read_status=pre.status.value,
                    post_read_status=None,
                    writer_invoked=True,
                    authorization_by=approval.approved_by,
                    authorization_reference=approval.reference,
                    writer_error=type(exc).__name__,
                    pre_snapshot=pre_snapshot,
                )
            )
        except Exception as exc:  # noqa: BLE001 - reconciled by both owner inventories.
            writer_error = type(exc).__name__

        post = reader.read_ads()
        post_snapshot = self._find_target(post, ad_id)

        confirmation = None
        confirmation_snapshot = None
        if confirmation_reader is not None:
            # This is a second fresh post-delete observation. It may use the
            # same authoritative inventory source; do not mistake object
            # identity for source independence.
            confirmation = confirmation_reader.read_ads()
            confirmation_snapshot = self._find_target(confirmation, ad_id)

        confirmed = (
            post.is_success
            and post_snapshot is None
            and confirmation is not None
            and confirmation.is_success
            and confirmation_snapshot is None
        )

        return self._persist(
            OperationReceipt(
                operation="delete",
                ad_id=ad_id,
                started_at=started_at,
                completed_at=self._clock(),
                outcome=(
                    OperationOutcome.CONFIRMED
                    if confirmed
                    else OperationOutcome.AMBIGUOUS
                ),
                pre_read_status=pre.status.value,
                post_read_status=post.status.value,
                writer_invoked=True,
                authorization_by=approval.approved_by,
                authorization_reference=approval.reference,
                writer_error=writer_error,
                pre_snapshot=pre_snapshot,
                post_snapshot=post_snapshot,
            )
        )
