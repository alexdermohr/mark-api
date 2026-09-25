from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from .domain import (
    AdSnapshot,
    DeleteApproval,
    LifecycleState,
    OperationOutcome,
    OperationReceipt,
)
from .ports import (
    AdContentUpdater,
    AdDeleteWriter,
    AdsReader,
    AdStateWriter,
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

    def set_state(
        self,
        *,
        ad_id: str,
        target_state: LifecycleState,
        reader: AdsReader,
        writer: AdStateWriter,
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
        except Exception as exc:  # noqa: BLE001 - reconciled by both owner inventories.
            writer_error = type(exc).__name__

        post = reader.read_ads()
        post_snapshot = self._find_target(post, ad_id)

        confirmation = None
        confirmation_snapshot = None
        if confirmation_reader is not None and confirmation_reader is not reader:
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
