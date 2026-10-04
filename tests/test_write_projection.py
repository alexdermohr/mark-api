from __future__ import annotations

import json
import secrets
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from mark_api.application import MarkService
from mark_api.dashboard import create_server
from mark_api.domain import (
    AdSnapshot,
    CreateOperationReceipt,
    LifecycleState,
    MediaPostReadStatus,
    OperationOutcome,
    OperationReceipt,
)
from mark_api.results import ReadResult, ReadStatus
from mark_api.storage import SnapshotStore
from mark_api.write_api import WriteApiAccess, WriteCapability, create_write_api_server


NOW = datetime(2026, 10, 4, 4, 0, tzinfo=timezone.utc)
TARGET = "1234567890"
OTHER = "1234567891"
CREATED = "1234567892"
TOKEN = secrets.token_urlsafe(32)


def snapshot(ad_id: str = TARGET, **changes) -> AdSnapshot:
    return replace(
        AdSnapshot(
            ad_id=ad_id,
            observed_at=NOW,
            source="synthetic-owner",
            lifecycle_state=LifecycleState.ACTIVE,
            title="Original title",
            description="Original description",
            views=10,
            watch_count=0,
        ),
        **changes,
    )


class LocalPlatform:
    """In-memory test double only; never constructs a platform adapter."""

    def __init__(self) -> None:
        self.ads = {TARGET: snapshot(), OTHER: snapshot(OTHER)}
        self.calls = []
        self.fail_after_effect = False

    def _finish(self, operation: str) -> None:
        self.calls.append(operation)
        if self.fail_after_effect:
            raise RuntimeError("synthetic transport failure after effect")

    def update_content(self, ad_id, *, title=None, description=None) -> None:
        current = self.ads[ad_id]
        self.ads[ad_id] = replace(
            current,
            title=current.title if title is None else title,
            description=current.description if description is None else description,
            observed_at=NOW + timedelta(seconds=1),
        )
        self._finish("update_content")

    def set_state(self, ad_id, state) -> None:
        self.ads[ad_id] = replace(
            self.ads[ad_id], lifecycle_state=state,
            observed_at=NOW + timedelta(seconds=len(self.calls) + 1),
        )
        self._finish("set_state")

    def delete_ad(self, ad_id) -> None:
        del self.ads[ad_id]
        self._finish("delete")

    def create_ad(self, request) -> None:
        self.ads[CREATED] = snapshot(
            CREATED, title=request.title, description=request.description,
            observed_at=NOW + timedelta(seconds=1),
        )
        self._finish("create")


class LocalReader:
    def __init__(self, platform, ad_id=None) -> None:
        self.platform = platform
        self.ad_id = ad_id
        self.calls = 0
        self.fail_after_first_read = False

    def read_ads(self):
        self.calls += 1
        if self.fail_after_first_read and self.calls > 1:
            return ReadResult.failure(ReadStatus.TRANSPORT_ERROR, error="synthetic")
        rows = tuple(
            ad for ad in self.platform.ads.values()
            if self.ad_id is None or ad.ad_id == self.ad_id
        )
        return (
            ReadResult.success_nonempty(rows)
            if rows else ReadResult.success_empty(())
        )


class ProjectionStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SnapshotStore(Path(directory.name) / "state.sqlite")
        self.store.append_ad_snapshot(snapshot())
        self.store.append_ad_snapshot(snapshot(OTHER))
        self.post = snapshot(title="Verified title", observed_at=NOW + timedelta(seconds=1))
        self.receipt = OperationReceipt(
            operation="update_content", ad_id=TARGET, started_at=NOW,
            completed_at=NOW + timedelta(seconds=2),
            outcome=OperationOutcome.CONFIRMED,
            pre_read_status="success_nonempty", post_read_status="success_nonempty",
            writer_invoked=True, pre_snapshot=snapshot(), post_snapshot=self.post,
        )
        self.create_receipt = CreateOperationReceipt(
            operation="create", started_at=NOW,
            completed_at=NOW + timedelta(seconds=2),
            outcome=OperationOutcome.CONFIRMED,
            pre_read_status="success_nonempty",
            confirmation_pre_read_status="success_nonempty",
            post_read_status="success_nonempty",
            confirmation_post_read_status="success_nonempty",
            content_post_read_status="success_nonempty",
            writer_invoked=True, created_ad_id=CREATED,
            post_snapshot=snapshot(CREATED),
            confirmation_post_snapshot=snapshot(CREATED),
            content_post_snapshot=snapshot(CREATED),
        )

    def test_receipt_and_exact_target_snapshot_are_persisted_together(self) -> None:
        self.store.append_operation_receipt(self.receipt)
        self.assertEqual(self.store.ad_history(TARGET), (snapshot(), self.post))
        self.assertEqual(self.store.ad_history(OTHER), (snapshot(OTHER),))

    def test_nonconfirmed_or_unbound_receipts_do_not_change_projection(self) -> None:
        variants = (
            replace(self.receipt, outcome=OperationOutcome.AMBIGUOUS),
            replace(self.receipt, outcome=OperationOutcome.PRECONDITION_FAILED),
            replace(self.receipt, writer_invoked=False),
            replace(self.receipt, post_read_status="transport_error"),
            replace(self.receipt, post_snapshot=None),
            replace(self.receipt, post_snapshot=snapshot(OTHER)),
            replace(self.receipt, operation="unsupported"),
        )
        for receipt in variants:
            with self.subTest(receipt=receipt):
                self.store.append_operation_receipt(receipt)
                self.assertEqual(self.store.ad_history(TARGET), (snapshot(),))
                self.assertEqual(self.store.ad_history(OTHER), (snapshot(OTHER),))

    def test_confirmed_create_is_visible_without_claiming_media_confirmation(self) -> None:
        receipt = replace(
            self.create_receipt,
            media_post_read_status=MediaPostReadStatus.VERIFIER_UNAVAILABLE,
        )
        self.store.append_create_operation_receipt(receipt)
        self.assertEqual(self.store.latest_ad_snapshot(CREATED), receipt.content_post_snapshot)
        self.assertFalse(receipt.media_persistence_confirmed)
        self.assertEqual(self.store.ad_history(OTHER), (snapshot(OTHER),))

    def test_unconfirmed_or_incomplete_create_keeps_existing_projection(self) -> None:
        variants = (
            replace(self.create_receipt, outcome=OperationOutcome.AMBIGUOUS, created_ad_id=None),
            replace(self.create_receipt, outcome=OperationOutcome.PRECONDITION_FAILED, created_ad_id=None),
            replace(self.create_receipt, content_post_snapshot=None),
            replace(self.create_receipt, confirmation_post_read_status="transport_error"),
            replace(self.create_receipt, writer_invoked=False),
        )
        for receipt in variants:
            with self.subTest(receipt=receipt):
                self.store.append_create_operation_receipt(receipt)
                self.assertIsNone(self.store.latest_ad_snapshot(CREATED))
                self.assertEqual(self.store.tracked_ad_ids(), (TARGET, OTHER))

    def test_projection_failure_rolls_back_operation_receipt(self) -> None:
        with patch.object(self.store, "_insert_ad_snapshot", side_effect=sqlite3.OperationalError("synthetic")):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.append_operation_receipt(self.receipt)
        with sqlite3.connect(self.store.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM operation_receipts").fetchone()[0], 0)
        self.assertEqual(self.store.ad_history(TARGET), (snapshot(),))

    def test_projection_failure_rolls_back_create_receipt(self) -> None:
        with patch.object(self.store, "_insert_ad_snapshot", side_effect=sqlite3.OperationalError("synthetic")):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.append_create_operation_receipt(self.create_receipt)
        with sqlite3.connect(self.store.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM create_operation_receipts").fetchone()[0], 0)
        self.assertIsNone(self.store.latest_ad_snapshot(CREATED))


class WriteDashboardIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SnapshotStore(Path(directory.name) / "state.sqlite")
        self.platform = LocalPlatform()
        for item in self.platform.ads.values():
            self.store.append_ad_snapshot(item)
        self.primary = LocalReader(self.platform)
        self.confirmation = LocalReader(self.platform)
        self.content_readers = []

        def content_reader(ad_id):
            reader = LocalReader(self.platform, ad_id)
            self.content_readers.append(reader)
            return reader

        self.service = MarkService(
            owner_reader=self.primary, management_reader=self.primary,
            delete_confirmation_reader=self.confirmation,
            reaction_reader=None, state_writer=self.platform,
            delete_writer=self.platform, content_writer=self.platform,
            create_writer=self.platform, content_reader_factory=content_reader,
            store=self.store, writes_enabled=True,
            clock=lambda: NOW + timedelta(seconds=10),
        )
        self.access = WriteApiAccess(
            principal="synthetic-test", bearer_token=TOKEN,
            capabilities=frozenset(WriteCapability) - {WriteCapability.CREATE_MEDIA}, writes_enabled=True,
        )
        self.api = create_write_api_server(self.service, self.store, self.access, port=0)
        self.dashboard = create_server(self.store, port=0)
        for server in (self.api, self.dashboard):
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(self._stop, server, thread)
        self.opener = build_opener(ProxyHandler({}))

    @staticmethod
    def _stop(server, thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    def request(self, server, path, method="GET", payload=None, key="synthetic-request"):
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(
            f"http://127.0.0.1:{server.server_port}{path}", data=body, method=method,
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json", "Idempotency-Key": key},
        )
        try:
            response = self.opener.open(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response), dict(response.headers)

    def ads(self):
        status, rows, _ = self.request(self.dashboard, "/api/ads")
        self.assertEqual(status, 200)
        return {row["ad_id"]: row for row in rows}

    def test_content_update_and_replay_converge_without_refresh_or_extra_reads(self) -> None:
        status, receipt, _ = self.request(self.api, f"/api/write/ads/{TARGET}", "PATCH", {"title": "New verified title"})
        self.assertEqual(status, 200)
        self.assertEqual(self.ads()[TARGET]["title"], "New verified title")
        self.assertEqual(self.ads()[OTHER]["title"], "Original title")
        self.assertEqual(self.primary.calls, 0)
        self.assertEqual([reader.calls for reader in self.content_readers], [2])
        history = self.store.ad_history(TARGET)
        replay_status, replay, headers = self.request(self.api, f"/api/write/ads/{TARGET}", "PATCH", {"title": "New verified title"})
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay, receipt)
        self.assertEqual(headers["Idempotency-Replayed"], "true")
        self.assertEqual(self.store.ad_history(TARGET), history)
        self.assertEqual(self.platform.calls, ["update_content"])

    def test_pause_and_activate_are_visible_in_dashboard(self) -> None:
        for action, expected in (("pause", "paused"), ("activate", "active")):
            status, _, _ = self.request(self.api, f"/api/write/ads/{TARGET}/{action}", "POST", key=action)
            self.assertEqual(status, 200)
            self.assertEqual(self.ads()[TARGET]["lifecycle_state"], expected)
            self.assertEqual(self.ads()[OTHER]["lifecycle_state"], "active")
        self.assertEqual(self.primary.calls, 4)
        self.assertEqual(self.platform.calls, ["set_state", "set_state"])

    def test_confirmed_delete_marks_only_target_absent_and_retains_history(self) -> None:
        status, receipt, _ = self.request(self.api, f"/api/write/ads/{TARGET}", "DELETE", {"confirm_ad_id": TARGET, "approval_reference": "synthetic-approval"})
        self.assertEqual(status, 200)
        self.assertFalse(receipt["platform_retry_authorized"])
        rows = self.ads()
        self.assertFalse(rows[TARGET]["present"])
        self.assertEqual(rows[TARGET]["lifecycle_state"], "absent")
        self.assertEqual(rows[TARGET]["title"], "Original title")
        self.assertTrue(rows[OTHER]["present"])
        self.assertEqual(len(self.store.ad_history(TARGET)), 2)
        self.assertEqual(self.store.ad_history(OTHER), (snapshot(OTHER),))
        self.assertEqual((self.primary.calls, self.confirmation.calls), (2, 1))
        self.assertEqual(self.platform.calls, ["delete"])

    def test_create_is_visible_without_refresh_and_preserves_other_ads(self) -> None:
        status, _, _ = self.request(self.api, "/api/write/ads", "POST", {"category_path": ["Freizeit", "Sammeln"], "title": "Created title", "description": "Created description", "price_eur": 10})
        self.assertEqual(status, 200)
        rows = self.ads()
        self.assertEqual(rows[CREATED]["title"], "Created title")
        self.assertEqual(rows[CREATED]["description"], "Created description")
        self.assertEqual(set(rows), {TARGET, OTHER, CREATED})
        self.assertEqual(self.store.ad_history(OTHER), (snapshot(OTHER),))
        self.assertEqual((self.primary.calls, self.confirmation.calls), (2, 2))
        self.assertEqual([reader.calls for reader in self.content_readers], [1])
        self.assertEqual(self.platform.calls, ["create"])

    def test_ambiguous_delete_does_not_invent_absence(self) -> None:
        self.primary.fail_after_first_read = True
        status, receipt, _ = self.request(self.api, f"/api/write/ads/{TARGET}", "DELETE", {"confirm_ad_id": TARGET, "approval_reference": "synthetic-approval"})
        self.assertEqual(status, 202)
        self.assertEqual(receipt["operation_receipt"]["outcome"], "ambiguous")
        self.assertTrue(self.ads()[TARGET]["present"])
        self.assertEqual(self.store.ad_history(TARGET), (snapshot(),))
        self.assertEqual(self.platform.calls, ["delete"])

    def test_confirmed_effect_after_writer_error_still_converges_without_retry(self) -> None:
        self.platform.fail_after_effect = True
        status, receipt, _ = self.request(self.api, f"/api/write/ads/{TARGET}/pause", "POST")
        self.assertEqual(status, 200)
        self.assertEqual(receipt["operation_receipt"]["writer_error"], "RuntimeError")
        self.assertEqual(self.ads()[TARGET]["lifecycle_state"], "paused")
        self.assertEqual(self.platform.calls, ["set_state"])


    def test_projection_failure_is_persisted_as_non_retryable_http_error(self) -> None:
        path = f"/api/write/ads/{TARGET}/pause"
        with patch.object(
            self.store, "_insert_ad_snapshot",
            side_effect=sqlite3.OperationalError("synthetic projection failure"),
        ):
            status, failed, _ = self.request(self.api, path, "POST")
        self.assertEqual(status, 500)
        self.assertFalse(failed["platform_retry_authorized"])
        self.assertEqual(self.store.ad_history(TARGET), (snapshot(),))
        replay_status, replay, headers = self.request(self.api, path, "POST")
        self.assertEqual(replay_status, 500)
        self.assertEqual(replay, failed)
        self.assertEqual(headers["Idempotency-Replayed"], "true")
        self.assertEqual(self.platform.calls, ["set_state"])
        self.assertEqual(self.primary.calls, 2)

    def test_disabled_core_does_not_publish_a_snapshot(self) -> None:
        self.service._writes._writes_enabled = False
        status, receipt, _ = self.request(
            self.api, f"/api/write/ads/{TARGET}/pause", "POST",
        )
        self.assertEqual(status, 409)
        self.assertFalse(receipt["operation_receipt"]["writer_invoked"])
        self.assertEqual(self.store.ad_history(TARGET), (snapshot(),))
        self.assertEqual(self.primary.calls, 0)
        self.assertEqual(self.platform.calls, [])

    def test_create_with_failed_confirmation_does_not_publish_candidate(self) -> None:
        self.confirmation.fail_after_first_read = True
        status, receipt, _ = self.request(
            self.api, "/api/write/ads", "POST",
            {"category_path": ["Freizeit", "Sammeln"], "title": "Created title",
             "description": "Created description", "price_eur": 10},
        )
        self.assertEqual(status, 202)
        self.assertIsNone(receipt["operation_receipt"]["created_ad_id"])
        self.assertNotIn(CREATED, self.ads())
        self.assertEqual(self.platform.calls, ["create"])


if __name__ == "__main__":
    unittest.main()
