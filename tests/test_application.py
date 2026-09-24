from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mark_api.application import EnrichedOwnerReader, MarkService
from mark_api.domain import (
    AdSnapshot,
    DeleteApproval,
    LifecycleState,
    OperationOutcome,
    ReactionSnapshot,
)
from mark_api.results import ReadResult, ReadStatus
from mark_api.storage import SnapshotStore


NOW = datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc)


def ad(
    ad_id: str,
    *,
    state: LifecycleState = LifecycleState.UNKNOWN,
    title: str | None = None,
    description: str | None = None,
    views: int | None = None,
    watch_count: int | None = None,
    reply_count: int | None = None,
    source: str = "test",
    observed_at: datetime = NOW,
) -> AdSnapshot:
    return AdSnapshot(
        ad_id=ad_id,
        observed_at=observed_at,
        source=source,
        lifecycle_state=state,
        title=title,
        description=description,
        views=views,
        watch_count=watch_count,
        reply_count=reply_count,
    )


class SequenceAdsReader:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def read_ads(self):
        self.calls += 1
        if not self.results:
            raise AssertionError("unexpected read_ads call")
        return self.results.pop(0)


class ReactionReader:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def read_reactions(self, ad_id: str):
        self.calls.append(ad_id)
        return self.result


class StateDeleteWriter:
    def __init__(self):
        self.state_calls = []
        self.delete_calls = []

    def set_state(self, ad_id: str, state: LifecycleState) -> None:
        self.state_calls.append((ad_id, state))

    def delete_ad(self, ad_id: str) -> None:
        self.delete_calls.append(ad_id)


class ContentWriter:
    def __init__(self):
        self.calls = []

    def update_content(
        self,
        ad_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        self.calls.append((ad_id, title, description))


class EnrichedOwnerReaderTests(unittest.TestCase):
    def test_management_empty_is_authoritative_and_skips_mobile(self) -> None:
        management = SequenceAdsReader(ReadResult.success_empty(()))
        mobile = SequenceAdsReader(
            ReadResult.success_nonempty(
                (
                    ad(
                        "stale-mobile-id",
                        title="stale",
                        source="mobile",
                    ),
                )
            )
        )
        reader = EnrichedOwnerReader(
            management_reader=management,
            mobile_reader=mobile,
        )

        result = reader.read_ads()

        self.assertEqual(result.status, ReadStatus.SUCCESS_EMPTY)
        self.assertEqual(result.value, ())
        self.assertEqual(management.calls, 1)
        self.assertEqual(mobile.calls, 0)

    def test_matching_mobile_content_enriches_management_only(self) -> None:
        management = SequenceAdsReader(
            ReadResult.success_nonempty(
                (
                    ad(
                        "1",
                        state=LifecycleState.ACTIVE,
                        title="management title",
                        views=15,
                        watch_count=2,
                        reply_count=1,
                        source="management",
                    ),
                )
            )
        )
        mobile = SequenceAdsReader(
            ReadResult.success_nonempty(
                (
                    ad(
                        "1",
                        title="mobile title",
                        description="mobile description",
                        source="mobile",
                    ),
                    ad(
                        "2",
                        title="mobile-only",
                        description="must be ignored",
                        source="mobile",
                    ),
                )
            )
        )
        reader = EnrichedOwnerReader(
            management_reader=management,
            mobile_reader=mobile,
        )

        result = reader.read_ads()

        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        self.assertEqual(len(result.value), 1)
        item = result.value[0]
        self.assertEqual(item.ad_id, "1")
        self.assertEqual(item.lifecycle_state, LifecycleState.ACTIVE)
        self.assertEqual(item.title, "mobile title")
        self.assertEqual(item.description, "mobile description")
        self.assertEqual(item.views, 15)
        self.assertEqual(item.watch_count, 2)
        self.assertEqual(item.reply_count, 1)
        self.assertEqual(item.source, "management+mobile")

    def test_missing_mobile_match_preserves_management_snapshot(self) -> None:
        original = ad(
            "1",
            state=LifecycleState.PAUSED,
            title="management",
            views=4,
            source="management",
        )
        reader = EnrichedOwnerReader(
            management_reader=SequenceAdsReader(
                ReadResult.success_nonempty((original,))
            ),
            mobile_reader=SequenceAdsReader(ReadResult.success_empty(())),
        )

        result = reader.read_ads()

        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        self.assertEqual(result.value, (original,))

    def test_mobile_failure_fails_enrichment_without_changing_presence(self) -> None:
        reader = EnrichedOwnerReader(
            management_reader=SequenceAdsReader(
                ReadResult.success_nonempty(
                    (ad("1", state=LifecycleState.ACTIVE),)
                )
            ),
            mobile_reader=SequenceAdsReader(
                ReadResult.failure(
                    ReadStatus.TRANSPORT_ERROR,
                    error="TimeoutError",
                )
            ),
        )

        result = reader.read_ads()

        self.assertEqual(result.status, ReadStatus.TRANSPORT_ERROR)
        self.assertIsNone(result.value)

    def test_invalid_management_snapshot_fails_closed_before_mobile(self) -> None:
        mobile = SequenceAdsReader(ReadResult.success_empty(()))
        reader = EnrichedOwnerReader(
            management_reader=SequenceAdsReader(
                ReadResult.success_nonempty((object(),))
            ),
            mobile_reader=mobile,
        )

        result = reader.read_ads()

        self.assertEqual(result.status, ReadStatus.PARSE_ERROR)
        self.assertEqual(result.error, "invalid_management_snapshot")
        self.assertEqual(mobile.calls, 0)

    def test_duplicate_management_id_fails_closed_before_mobile(self) -> None:
        mobile = SequenceAdsReader(ReadResult.success_empty(()))
        reader = EnrichedOwnerReader(
            management_reader=SequenceAdsReader(
                ReadResult.success_nonempty((ad("1"), ad("1")))
            ),
            mobile_reader=mobile,
        )

        result = reader.read_ads()

        self.assertEqual(result.status, ReadStatus.PARSE_ERROR)
        self.assertEqual(result.error, "duplicate_management_id")
        self.assertEqual(mobile.calls, 0)

    def test_invalid_mobile_snapshot_fails_closed(self) -> None:
        reader = EnrichedOwnerReader(
            management_reader=SequenceAdsReader(
                ReadResult.success_nonempty((ad("1"),))
            ),
            mobile_reader=SequenceAdsReader(
                ReadResult.success_nonempty((object(),))
            ),
        )

        result = reader.read_ads()

        self.assertEqual(result.status, ReadStatus.PARSE_ERROR)
        self.assertEqual(result.error, "invalid_mobile_snapshot")

    def test_duplicate_mobile_id_fails_closed(self) -> None:
        reader = EnrichedOwnerReader(
            management_reader=SequenceAdsReader(
                ReadResult.success_nonempty((ad("1"),))
            ),
            mobile_reader=SequenceAdsReader(
                ReadResult.success_nonempty(
                    (
                        ad("1", source="mobile"),
                        ad("1", source="mobile"),
                    )
                )
            ),
        )

        result = reader.read_ads()

        self.assertEqual(result.status, ReadStatus.PARSE_ERROR)
        self.assertEqual(result.error, "duplicate_mobile_id")


class MarkServiceTests(unittest.TestCase):
    def make_store(self) -> SnapshotStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return SnapshotStore(Path(tmp.name) / "mark.sqlite")

    def service(
        self,
        *,
        owner_reader,
        management_reader,
        reaction_reader=None,
        state_writer=None,
        delete_writer=None,
        content_writer=None,
        writes_enabled=False,
        store=None,
    ):
        if reaction_reader is None:
            reaction_reader = ReactionReader(ReadResult.success_empty())
        if state_writer is None:
            state_writer = StateDeleteWriter()
        if delete_writer is None:
            delete_writer = state_writer
        if content_writer is None:
            content_writer = ContentWriter()
        if store is None:
            store = self.make_store()
        service = MarkService(
            owner_reader=owner_reader,
            management_reader=management_reader,
            reaction_reader=reaction_reader,
            state_writer=state_writer,
            delete_writer=delete_writer,
            content_writer=content_writer,
            store=store,
            writes_enabled=writes_enabled,
            clock=lambda: NOW,
        )
        return service, store, state_writer, content_writer

    def test_refresh_inventory_marks_historical_id_absent_on_successful_empty(self) -> None:
        store = self.make_store()
        store.append_ad_snapshot(
            ad(
                "3521676801",
                state=LifecycleState.ACTIVE,
                views=15,
                reply_count=1,
                source="management",
                observed_at=NOW - timedelta(minutes=1),
            )
        )
        service, _, _, _ = self.service(
            owner_reader=SequenceAdsReader(ReadResult.success_empty(())),
            management_reader=SequenceAdsReader(),
            store=store,
        )

        result = service.refresh_inventory()

        self.assertEqual(result.status, ReadStatus.SUCCESS_EMPTY)
        self.assertEqual(store.tracked_ad_ids(), ("3521676801",))
        self.assertEqual(
            store.latest_ad_snapshot("3521676801").lifecycle_state,
            LifecycleState.ABSENT,
        )
        self.assertEqual(
            [item.lifecycle_state for item in store.ad_history("3521676801")],
            [LifecycleState.ACTIVE, LifecycleState.ABSENT],
        )

    def test_failed_inventory_refresh_does_not_mark_history_absent(self) -> None:
        store = self.make_store()
        active = ad(
            "3521676801",
            state=LifecycleState.ACTIVE,
            source="management",
        )
        store.append_ad_snapshot(active)
        service, _, _, _ = self.service(
            owner_reader=SequenceAdsReader(
                ReadResult.failure(
                    ReadStatus.HTTP_ERROR,
                    http_status=500,
                )
            ),
            management_reader=SequenceAdsReader(),
            store=store,
        )

        result = service.refresh_inventory()

        self.assertEqual(result.status, ReadStatus.HTTP_ERROR)
        self.assertEqual(store.ad_history("3521676801"), (active,))

    def test_refresh_reactions_persists_snapshot(self) -> None:
        store = self.make_store()
        reactions = ReactionSnapshot(
            ad_id="3521676801",
            conversation_count=1,
            unique_buyer_count=1,
            inbound_message_count=1,
            observed_at=NOW,
            source="mobile",
        )
        reaction_reader = ReactionReader(
            ReadResult.success_nonempty(reactions)
        )
        service, _, _, _ = self.service(
            owner_reader=SequenceAdsReader(),
            management_reader=SequenceAdsReader(),
            reaction_reader=reaction_reader,
            store=store,
        )

        result = service.refresh_reactions("3521676801")

        self.assertEqual(result.value, reactions)
        self.assertEqual(
            store.reaction_history("3521676801"),
            (reactions,),
        )

    def test_writes_default_disabled_before_any_reader_or_writer_call(self) -> None:
        management = SequenceAdsReader()
        writer = StateDeleteWriter()
        service, _, _, _ = self.service(
            owner_reader=SequenceAdsReader(),
            management_reader=management,
            state_writer=writer,
            delete_writer=writer,
            writes_enabled=False,
        )

        receipt = service.pause("3521676801")

        self.assertEqual(
            receipt.outcome,
            OperationOutcome.PRECONDITION_FAILED,
        )
        self.assertEqual(receipt.pre_read_status, "writes_disabled")
        self.assertEqual(management.calls, 0)
        self.assertEqual(writer.state_calls, [])

    def test_pause_uses_management_reader_not_enriched_owner_reader(self) -> None:
        owner = SequenceAdsReader()
        management = SequenceAdsReader(
            ReadResult.success_nonempty(
                (
                    ad(
                        "3521676801",
                        state=LifecycleState.ACTIVE,
                        source="management",
                    ),
                )
            ),
            ReadResult.success_nonempty(
                (
                    ad(
                        "3521676801",
                        state=LifecycleState.PAUSED,
                        source="management",
                    ),
                )
            ),
        )
        writer = StateDeleteWriter()
        service, _, _, _ = self.service(
            owner_reader=owner,
            management_reader=management,
            state_writer=writer,
            delete_writer=writer,
            writes_enabled=True,
        )

        receipt = service.pause("3521676801")

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(
            writer.state_calls,
            [("3521676801", LifecycleState.PAUSED)],
        )
        self.assertEqual(owner.calls, 0)
        self.assertEqual(management.calls, 2)

    def test_activate_uses_management_reader_not_enriched_owner_reader(self) -> None:
        owner = SequenceAdsReader()
        management = SequenceAdsReader(
            ReadResult.success_nonempty(
                (
                    ad(
                        "3521676801",
                        state=LifecycleState.PAUSED,
                        source="management",
                    ),
                )
            ),
            ReadResult.success_nonempty(
                (
                    ad(
                        "3521676801",
                        state=LifecycleState.ACTIVE,
                        source="management",
                    ),
                )
            ),
        )
        writer = StateDeleteWriter()
        service, _, _, _ = self.service(
            owner_reader=owner,
            management_reader=management,
            state_writer=writer,
            delete_writer=writer,
            writes_enabled=True,
        )

        receipt = service.activate("3521676801")

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(
            writer.state_calls,
            [("3521676801", LifecycleState.ACTIVE)],
        )
        self.assertEqual(owner.calls, 0)
        self.assertEqual(management.calls, 2)

    def test_delete_uses_management_and_exact_approval(self) -> None:
        management = SequenceAdsReader(
            ReadResult.success_nonempty(
                (
                    ad(
                        "3521676801",
                        state=LifecycleState.ACTIVE,
                        source="management",
                    ),
                )
            ),
            ReadResult.success_empty(()),
        )
        writer = StateDeleteWriter()
        service, _, _, _ = self.service(
            owner_reader=SequenceAdsReader(),
            management_reader=management,
            state_writer=writer,
            delete_writer=writer,
            writes_enabled=True,
        )

        receipt = service.delete(
            "3521676801",
            approval=DeleteApproval(
                ad_id="3521676801",
                approved_by="owner",
                reference="approval-1",
            ),
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(writer.delete_calls, ["3521676801"])
        self.assertEqual(receipt.authorization_reference, "approval-1")

    def test_content_update_uses_enriched_owner_reader(self) -> None:
        owner = SequenceAdsReader(
            ReadResult.success_nonempty(
                (
                    ad(
                        "3521676801",
                        state=LifecycleState.ACTIVE,
                        description="old",
                        source="management+mobile",
                    ),
                )
            ),
            ReadResult.success_nonempty(
                (
                    ad(
                        "3521676801",
                        state=LifecycleState.ACTIVE,
                        description="new",
                        source="management+mobile",
                    ),
                )
            ),
        )
        management = SequenceAdsReader()
        content = ContentWriter()
        service, _, _, _ = self.service(
            owner_reader=owner,
            management_reader=management,
            content_writer=content,
            writes_enabled=True,
        )

        receipt = service.update_content(
            "3521676801",
            description="new",
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(
            content.calls,
            [("3521676801", None, "new")],
        )
        self.assertEqual(owner.calls, 2)
        self.assertEqual(management.calls, 0)


if __name__ == "__main__":
    unittest.main()