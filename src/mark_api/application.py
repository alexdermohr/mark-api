from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from .domain import (
    AdCreateRequest,
    AdSnapshot,
    CreateOperationReceipt,
    DeleteApproval,
    LifecycleState,
    OperationReceipt,
    ReactionSnapshot,
)
from .orchestrator import SafeWriteOrchestrator
from .ports import (
    AdContentUpdater,
    AdCreateWriter,
    AdDeleteWriter,
    AdsReader,
    AdStateWriter,
    InboxReader,
)
from .results import ReadResult, ReadStatus
from .storage import SnapshotStore


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _LazyAdsReader:
    """Instantiate one target-bound reader only when a read is actually needed."""

    def __init__(self, factory: Callable[[], AdsReader]) -> None:
        self._factory = factory
        self._reader: AdsReader | None = None

    def read_ads(self) -> ReadResult[tuple[AdSnapshot, ...]]:
        if self._reader is None:
            self._reader = self._factory()
        return self._reader.read_ads()


class EnrichedOwnerReader:
    """Combine authoritative owner inventory with optional mobile content.

    Management decides whether an ad exists and owns lifecycle/counter fields.
    The mobile reader may enrich title/description only for IDs already present
    in management. Mobile-only IDs never become current inventory.
    """

    def __init__(
        self,
        *,
        management_reader: AdsReader,
        mobile_reader: AdsReader,
    ) -> None:
        self._management_reader = management_reader
        self._mobile_reader = mobile_reader

    @staticmethod
    def _failure_like(
        result: ReadResult[tuple[AdSnapshot, ...]],
    ) -> ReadResult[tuple[AdSnapshot, ...]]:
        return ReadResult.failure(
            result.status,
            error=result.error,
            http_status=result.http_status,
        )

    @staticmethod
    def _unique_index(
        snapshots: tuple[AdSnapshot, ...],
        *,
        duplicate_error: str,
        shape_error: str,
    ) -> tuple[dict[str, AdSnapshot] | None, str | None]:
        index: dict[str, AdSnapshot] = {}
        for snapshot in snapshots:
            if not isinstance(snapshot, AdSnapshot):
                return None, shape_error
            if snapshot.ad_id in index:
                return None, duplicate_error
            index[snapshot.ad_id] = snapshot
        return index, None

    def read_ads(self) -> ReadResult[tuple[AdSnapshot, ...]]:
        management = self._management_reader.read_ads()
        if not management.is_success:
            return self._failure_like(management)

        management_snapshots = tuple(management.value or ())
        management_index, management_error = self._unique_index(
            management_snapshots,
            duplicate_error="duplicate_management_id",
            shape_error="invalid_management_snapshot",
        )
        if management_error is not None:
            return ReadResult.failure(
                ReadStatus.PARSE_ERROR,
                error=management_error,
            )

        if not management_snapshots:
            return ReadResult.success_empty(())

        mobile = self._mobile_reader.read_ads()
        if not mobile.is_success:
            return self._failure_like(mobile)

        mobile_snapshots = tuple(mobile.value or ())
        mobile_index, mobile_error = self._unique_index(
            mobile_snapshots,
            duplicate_error="duplicate_mobile_id",
            shape_error="invalid_mobile_snapshot",
        )
        if mobile_error is not None:
            return ReadResult.failure(
                ReadStatus.PARSE_ERROR,
                error=mobile_error,
            )

        merged: list[AdSnapshot] = []
        for management_snapshot in management_snapshots:
            mobile_snapshot = mobile_index.get(management_snapshot.ad_id)
            if mobile_snapshot is None:
                merged.append(management_snapshot)
                continue

            merged.append(
                AdSnapshot(
                    ad_id=management_snapshot.ad_id,
                    observed_at=management_snapshot.observed_at,
                    source=(
                        f"{management_snapshot.source}+{mobile_snapshot.source}"
                    ),
                    lifecycle_state=management_snapshot.lifecycle_state,
                    title=(
                        mobile_snapshot.title
                        if mobile_snapshot.title is not None
                        else management_snapshot.title
                    ),
                    description=(
                        mobile_snapshot.description
                        if mobile_snapshot.description is not None
                        else management_snapshot.description
                    ),
                    views=management_snapshot.views,
                    watch_count=management_snapshot.watch_count,
                    reply_count=management_snapshot.reply_count,
                    metric_source=(
                        management_snapshot.metric_source
                        if management_snapshot.metric_source is not None
                        else management_snapshot.source
                    ),
                )
            )

        return ReadResult.success_nonempty(tuple(merged))

    def read_ad(self, ad_id: str) -> ReadResult[AdSnapshot]:
        result = self.read_ads()
        if not result.is_success:
            return ReadResult.failure(
                result.status,
                error=result.error,
                http_status=result.http_status,
            )
        for snapshot in result.value or ():
            if snapshot.ad_id == ad_id:
                return ReadResult.success_nonempty(snapshot)
        return ReadResult.success_empty()


class MarkService:
    """Framework-free application service that composes the safe adapters."""

    def __init__(
        self,
        *,
        owner_reader: AdsReader,
        management_reader: AdsReader,
        delete_confirmation_reader: AdsReader,
        reaction_reader: InboxReader,
        state_writer: AdStateWriter,
        delete_writer: AdDeleteWriter,
        content_writer: AdContentUpdater,
        store: SnapshotStore,
        create_writer: AdCreateWriter | None = None,
        content_reader_factory: Callable[[str], AdsReader] | None = None,
        writes_enabled: bool = False,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._owner_reader = owner_reader
        self._management_reader = management_reader
        self._delete_confirmation_reader = delete_confirmation_reader
        self._reaction_reader = reaction_reader
        self._state_writer = state_writer
        self._delete_writer = delete_writer
        self._content_writer = content_writer
        self._create_writer = create_writer
        self._content_reader_factory = content_reader_factory
        self._store = store
        self._clock = clock
        self._writes = SafeWriteOrchestrator(
            store=store,
            clock=clock,
            writes_enabled=writes_enabled,
        )

    def refresh_inventory(self) -> ReadResult[tuple[AdSnapshot, ...]]:
        """Record read intent first; persist result and snapshots atomically."""
        tracked_ids = self._store.tracked_ad_ids()
        source = "mark-service-owner-inventory"
        attempt_id = self._store.begin_sync_attempt(
            source=source, started_at=self._clock(),
        )
        try:
            result = self._owner_reader.read_ads()
        except Exception:
            self._store.fail_sync_attempt(
                attempt_id, source=source, completed_at=self._clock(),
                error_kind="reader_exception",
            )
            raise
        try:
            self._store.append_inventory_result(
                result,
                tracked_ad_ids=tracked_ids,
                observed_at=self._clock(),
                source=source,
                attempt_id=attempt_id,
                completed_at=self._clock(),
            )
        except Exception:
            # The independent failed-record CAS refuses to overwrite a
            # committed successful outcome on an unknown SQLite write result.
            self._store.fail_sync_attempt(
                attempt_id, source=source, completed_at=self._clock(),
                error_kind="persistence_error",
            )
            raise
        return result

    def refresh_reactions(self, ad_id: str) -> ReadResult[ReactionSnapshot]:
        result = self._reaction_reader.read_reactions(ad_id)
        if (
            result.status is ReadStatus.SUCCESS_NONEMPTY
            and result.value is not None
        ):
            self._store.append_reaction_snapshot(result.value)
        return result

    def create(
        self,
        request: AdCreateRequest,
        *,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> CreateOperationReceipt:
        return self._writes.create(
            request=request,
            reader=self._management_reader,
            confirmation_reader=self._delete_confirmation_reader,
            writer=self._create_writer,
            content_reader_factory=self._content_reader_factory,
            authorization_by=authorization_by,
            authorization_reference=authorization_reference,
        )

    def pause(
        self,
        ad_id: str,
        *,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> OperationReceipt:
        return self._writes.set_state(
            ad_id=ad_id,
            target_state=LifecycleState.PAUSED,
            reader=self._management_reader,
            writer=self._state_writer,
            authorization_by=authorization_by,
            authorization_reference=authorization_reference,
        )

    def activate(
        self,
        ad_id: str,
        *,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> OperationReceipt:
        return self._writes.set_state(
            ad_id=ad_id,
            target_state=LifecycleState.ACTIVE,
            reader=self._management_reader,
            writer=self._state_writer,
            authorization_by=authorization_by,
            authorization_reference=authorization_reference,
        )

    def delete(
        self,
        ad_id: str,
        *,
        approval: DeleteApproval,
    ) -> OperationReceipt:
        return self._writes.delete(
            ad_id=ad_id,
            approval=approval,
            reader=self._management_reader,
            writer=self._delete_writer,
            confirmation_reader=self._delete_confirmation_reader,
        )

    def update_content(
        self,
        ad_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> OperationReceipt:
        reader = (
            self._owner_reader
            if self._content_reader_factory is None
            else _LazyAdsReader(
                lambda: self._content_reader_factory(ad_id)
            )
        )
        return self._writes.update_content(
            ad_id=ad_id,
            reader=reader,
            writer=self._content_writer,
            title=title,
            description=description,
            authorization_by=authorization_by,
            authorization_reference=authorization_reference,
        )