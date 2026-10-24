"""Actor-only CDP observation -> independent-owner SQLite sync contracts."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from threading import Lock
import unittest
from unittest.mock import MagicMock, patch

from mark_api.domain import AdSnapshot, LifecycleState
from mark_api.protected_inventory import (
    ActorInventoryPages, OwnerActorInventoryClient,
    ProtectedInventoryError, sync_actor_inventory_once,
)
from mark_api.results import ReadResult, ReadStatus
from mark_api.storage import SnapshotStore


NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def sample(ad_id="701"):
    return AdSnapshot(
        ad_id=ad_id, observed_at=NOW,
        source="synthetic-actor-inventory",
        lifecycle_state=LifecycleState.ACTIVE,
        title="Synthetic ad " + ad_id, description="Not a public listing",
    )


class ActorInventoryPagesTests(unittest.TestCase):
    def reader(self, *snapshots):
        value = MagicMock()
        value.read_ads.return_value = (
            ReadResult.success_nonempty(tuple(snapshots))
            if snapshots else ReadResult.success_empty(())
        )
        return value

    def test_five_ads_are_pinned_across_two_pages_then_released(self):
        reader = self.reader(*(sample(str(n)) for n in range(701, 706)))
        pages = ActorInventoryPages(reader, Lock())
        start = pages.begin()
        self.assertEqual(start["total"], 5)
        with self.assertRaises(ProtectedInventoryError):
            pages.begin()
        first = pages.page(start["token"], 0)
        self.assertEqual(len(first["rows"]), 4)
        self.assertEqual(first["next_offset"], 4)
        self.assertTrue(first["more"])
        with self.assertRaises(ProtectedInventoryError):
            pages.page(start["token"], 0)
        second = pages.page(start["token"], 4)
        self.assertEqual(len(second["rows"]), 1)
        self.assertFalse(second["more"])
        with self.assertRaises(ProtectedInventoryError):
            pages.finish(start["token"], "0" * 64)
        self.assertEqual(
            pages.finish(start["token"], start["digest"]),
            {"version": 1, "ok": True, "released": True},
        )
        reader.read_ads.assert_called_once()

    def test_lost_page_ack_expires_old_read_token_without_write_clearance(self):
        actor = ActorInventoryPages(self.reader(sample()), Lock())
        with patch("mark_api.protected_inventory.monotonic", return_value=100.0):
            old = actor.begin()
            self.assertEqual(actor.page(old["token"], 0)["next_offset"], 1)
            with self.assertRaises(ProtectedInventoryError):
                actor.begin()
        with patch("mark_api.protected_inventory.monotonic", return_value=156.0):
            fresh = actor.begin()
            self.assertNotEqual(old["token"], fresh["token"])
            with self.assertRaises(ProtectedInventoryError):
                actor.finish(old["token"], old["digest"])
            self.assertEqual(actor.page(fresh["token"], 0)["next_offset"], 1)
            self.assertTrue(actor.finish(fresh["token"], fresh["digest"])["released"])

    def test_empty_inventory_has_a_bounded_positive_receipt(self):
        pages = ActorInventoryPages(self.reader(), Lock())
        start = pages.begin()
        self.assertEqual(start["total"], 0)
        with self.assertRaises(ProtectedInventoryError):
            pages.page(start["token"], 0)
        self.assertTrue(pages.finish(start["token"], start["digest"])["released"])

    def test_forged_token_offset_or_boolean_cursor_denied(self):
        pages = ActorInventoryPages(self.reader(sample()), Lock())
        start = pages.begin()
        for token, offset in (
            ("other", 0), (start["token"], True), (start["token"], -1),
            (start["token"], 1),
        ):
            with self.subTest(token=token,offset=offset), self.assertRaises(ProtectedInventoryError):
                pages.page(token, offset)
        self.assertEqual(pages.page(start["token"], 0)["next_offset"], 1)

    def test_failed_or_duplicate_inventory_does_not_grant_new_read(self):
        reader = self.reader(sample("701"), sample("701"))
        pages = ActorInventoryPages(reader, Lock())
        with self.assertRaises(ProtectedInventoryError):
            pages.begin()
        reader.read_ads.return_value = ReadResult.failure(ReadStatus.TRANSPORT_ERROR)
        with self.assertRaises(ProtectedInventoryError):
            pages.begin()
        reader.read_ads.return_value = ReadResult.success_nonempty((sample(),))
        self.assertEqual(pages.begin()["total"], 1)


class OwnerInventoryClientTests(unittest.TestCase):
    def client(self):
        return OwnerActorInventoryClient(
            actor_socket=Path("/tmp/synthetic-independent-a6-actor.sock"),
            actor_uid=os.geteuid() + 102,
        )

    def test_client_reads_all_pages_exactly_once_and_verifies_digest(self):
        pages = ActorInventoryPages(
            ActorInventoryPagesTests().reader(
                *(sample(str(n)) for n in range(701, 707))
            ), Lock(),
        )
        client = self.client()
        def request(packet):
            if packet["op"] == "inventory_begin":
                return pages.begin()
            if packet["op"] == "inventory_page":
                return pages.page(packet["token"], packet["offset"])
            if packet["op"] == "inventory_finish":
                return pages.finish(packet["token"], packet["digest"])
            raise AssertionError("unexpected protocol command")
        with patch.object(client, "_call", side_effect=request) as call:
            result, observed_at = client.read()
        self.assertEqual(len(result.value), 6)
        self.assertTrue(result.is_success)
        self.assertEqual(observed_at.tzinfo, timezone.utc)
        self.assertEqual(
            [entry.args[0]["op"] for entry in call.call_args_list],
            ["inventory_begin", "inventory_page", "inventory_page", "inventory_finish"],
        )

    def test_mismatched_digest_leaves_actor_pending_without_cleanup(self):
        pages = ActorInventoryPages(
            ActorInventoryPagesTests().reader(sample()), Lock(),
        )
        client = self.client()
        generation = None
        def request(packet):
            nonlocal generation
            if packet["op"] == "inventory_begin":
                generation = pages.begin()
                return {**generation, "digest": "0" * 64}
            if packet["op"] == "inventory_page":
                return pages.page(packet["token"], packet["offset"])
            raise AssertionError("unknown digest may not authorize finish")
        with patch.object(client, "_call", side_effect=request):
            with self.assertRaises(ProtectedInventoryError):
                client.read()
        self.assertIsNotNone(generation)
        with self.assertRaises(ProtectedInventoryError):
            pages.begin()

    def test_expired_overall_read_deadline_fails_before_network_connect(self):
        client = self.client()
        client._deadline = 100.0
        with patch("mark_api.protected_inventory.monotonic", return_value=101.0), patch(
            "mark_api.protected_inventory.socket.socket"
        ) as sock:
            with self.assertRaises(ProtectedInventoryError):
                client._call({"version": 1, "op": "inventory_begin"})
            sock.assert_not_called()

    def test_wrong_actor_uid_and_noncanonical_socket_denied(self):
        with self.assertRaises(ProtectedInventoryError):
            OwnerActorInventoryClient(
                actor_socket=Path("/tmp/a/../b.sock"), actor_uid=os.geteuid() + 1,
            )
        with self.assertRaises(ProtectedInventoryError):
            OwnerActorInventoryClient(
                actor_socket=Path("/tmp/actor.sock"), actor_uid=os.geteuid(),
            )


class OwnerSyncJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SnapshotStore(Path(self.temp.name) / "owner.sqlite")
        self.client = OwnerInventoryClientTests().client()

    def test_completed_sync_records_one_owner_snapshot_and_durable_attempt(self):
        with patch.object(
            self.client, "read",
            return_value=(ReadResult.success_nonempty((sample(),)), NOW),
        ):
            persisted = sync_actor_inventory_once(self.store, self.client)
        self.assertEqual(persisted, 1)
        self.assertEqual(self.store.latest_ad_snapshot("701").title, "Synthetic ad 701")
        self.assertEqual(self.store.sync_recovery_status()["unresolved_count"], 0)
        last = self.store.sync_status()["latest_attempt"]
        self.assertEqual(last["outcome"], "success_nonempty")

    def test_read_failure_is_journaled_and_never_forged_as_success(self):
        with patch.object(
            self.client, "read",
            side_effect=ProtectedInventoryError("synthetic read failed"),
        ):
            with self.assertRaises(ProtectedInventoryError):
                sync_actor_inventory_once(self.store, self.client)
        self.assertEqual(self.store.sync_recovery_status()["unresolved_count"], 0)
        self.assertEqual(self.store.sync_status()["latest_attempt"]["outcome"], "failed")
        self.assertEqual(self.store.tracked_ad_ids(), ())



    def test_read_failure_allows_new_read_only_cycle_only_after_durable_failure(self):
        calls = [ProtectedInventoryError("synthetic lost page ACK"),
                 (ReadResult.success_nonempty((sample(),)), NOW)]
        with patch.object(self.client, "read", side_effect=calls) as reader:
            with self.assertRaises(ProtectedInventoryError) as error:
                sync_actor_inventory_once(self.store, self.client)
            self.assertTrue(error.exception.retryable_read)
            self.assertEqual(self.store.sync_recovery_status()["unresolved_count"], 0)
            self.assertEqual(sync_actor_inventory_once(self.store, self.client), 1)
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(self.store.sync_status()["latest_attempt"]["outcome"], "success_nonempty")

    def test_lost_sqlite_success_ack_never_allows_reobservation(self):
        saved = self.store.append_inventory_result
        def persist_then_lose_ack(*args, **kwargs):
            saved(*args, **kwargs)
            raise OSError("synthetic SQLite success ACK lost")
        with patch.object(
            self.client, "read",
            return_value=(ReadResult.success_nonempty((sample(),)), NOW),
        ) as reader, patch.object(
            self.store, "append_inventory_result", side_effect=persist_then_lose_ack,
        ):
            with self.assertRaises(ProtectedInventoryError) as error:
                sync_actor_inventory_once(self.store, self.client)
        self.assertFalse(error.exception.retryable_read)
        reader.assert_called_once()
        self.assertEqual(self.store.sync_status()["latest_attempt"]["outcome"], "success_nonempty")
        self.assertEqual(self.store.sync_recovery_status()["unresolved_count"], 0)



if __name__ == "__main__":
    unittest.main()