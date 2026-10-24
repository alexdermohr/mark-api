"""Synthetic real-HTTP dashboard -> private media stage -> Write API -> SQLite.

No Kleinanzeigen session or external provider: all platform effects are observed
in the in-memory service. This exercises actual handlers, protected media handles,
durable receipts, duplicate/restart fencing and the local two-phase ACK.
"""
from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from mark_api.dashboard import DashboardWriteProxy, create_server
from mark_api.domain import (
    AdSnapshot,
    CreateOperationReceipt,
    LifecycleState,
    MediaPostReadStatus,
    OperationOutcome,
)
from mark_api.private_web_media import PrivateWebMediaHandleStore
from mark_api.storage import SnapshotStore
from mark_api.write_api import WriteApiAccess, WriteCapability, create_write_api_server

NOW = datetime(2026, 10, 23, 7, 0, tzinfo=timezone.utc)
BACKEND_TOKEN = "synthetic-media-backend-only-abcdefghijkl"
UI_TOKEN = "synthetic-media-dashboard-only-abcdefgh"
PRINCIPAL = "synthetic-media-owner"
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Zr4jV8AAAAASUVORK5CYII="
)


class NeverCalledStandardService:
    def create(self, *args, **kwargs):
        raise AssertionError("media create must not reach the ordinary create writer")


class SyntheticConfirmedMediaService:
    def __init__(self, stager: PrivateWebMediaHandleStore, store: SnapshotStore, *, confirmed: bool = True):
        self.stager = stager
        self.store = store
        self.confirmed = confirmed
        self.effects: list[tuple[str, ...]] = []

    def create_with_media(
        self,
        request,
        media_refs: tuple[str, ...],
        *,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> CreateOperationReceipt:
        sources = self.stager.resolve(media_refs)
        assert len(sources) == 1 and len(media_refs) == 1
        assert Path(sources[0].path).read_bytes() == PNG_BYTES
        assert stat.S_IMODE(os.stat(sources[0].path).st_mode) == 0o600
        self.effects.append(media_refs)
        if self.confirmed:
            observed = AdSnapshot(
                ad_id="200", observed_at=NOW, source="synthetic:owner",
                lifecycle_state=LifecycleState.ACTIVE, title=request.title,
                description=request.description,
            )
            outcome = OperationOutcome.CONFIRMED
            media_read = MediaPostReadStatus.CONFIRMED
        else:
            observed = None
            outcome = OperationOutcome.AMBIGUOUS
            media_read = None
        receipt = CreateOperationReceipt(
            operation="create",
            started_at=NOW,
            completed_at=NOW,
            outcome=outcome,
            pre_read_status="success_empty",
            confirmation_pre_read_status="success_empty",
            post_read_status="success_nonempty" if observed else None,
            confirmation_post_read_status="success_nonempty" if observed else None,
            content_post_read_status="success_nonempty" if observed else None,
            writer_invoked=True,
            created_ad_id="200" if observed else None,
            authorization_by=authorization_by,
            authorization_reference=authorization_reference,
            post_snapshot=observed,
            confirmation_post_snapshot=observed,
            content_post_snapshot=observed,
            media_post_read_status=media_read,
            media_persistence_confirmed=self.confirmed,
        )
        # The real PrivateWebMediaCreateService persists an intermediate
        # content-confirmed checkpoint before its independent media postread.
        if observed is not None:
            from dataclasses import replace
            self.store.append_create_operation_checkpoint(replace(
                receipt, media_post_read_status=None,
                media_persistence_confirmed=False,
            ))
        self.store.append_create_operation_receipt(receipt)
        return receipt


@contextmanager
def servers(store, media_service, stager):
    access = WriteApiAccess(
        principal=PRINCIPAL, bearer_token=BACKEND_TOKEN,
        capabilities=frozenset({WriteCapability.CREATE_MEDIA}), writes_enabled=True,
    )
    backend = create_write_api_server(
        NeverCalledStandardService(), store, access,
        media_service=media_service, media_stager=stager, port=0,
        clock=lambda: NOW,
    )
    dashboard = create_server(
        store, port=0,
        write_proxy=DashboardWriteProxy(
            host="127.0.0.1", port=backend.server_port,
            bearer_token=BACKEND_TOKEN, ui_token=UI_TOKEN,
            backend_principal=PRINCIPAL, local_reconciliation_enabled=True,
        ),
    )
    workers = []
    try:
        for server in (backend, dashboard):
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            workers.append(worker)
        yield "http://127.0.0.1:" + str(dashboard.server_port)
    finally:
        for server in (dashboard, backend):
            server.shutdown()
            server.server_close()
        for worker in workers:
            worker.join(timeout=4)
            if worker.is_alive():
                raise AssertionError("HTTP worker failed to quiesce")


def request(base, path, *, method="GET", payload=None, image=None,
            filename=None, key=None):
    headers = {"Accept": "application/json"}
    if method != "GET":
        headers.update({
            "Origin": base, "X-Mark-Dashboard-Write": "1",
            "X-Mark-Dashboard-Token": UI_TOKEN,
        })
    if key is not None:
        headers["Idempotency-Key"] = key
    if image is not None:
        body = image
        headers["Content-Type"] = "image/png"
        headers["X-Mark-Media-Filename"] = filename or "sample.png"
    elif payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    else:
        body = None
    req = Request(base + path, data=body, method=method, headers=headers)
    opener = build_opener(ProxyHandler({}))
    try:
        response = opener.open(req, timeout=4)
    except HTTPError as exc:
        response = exc
    with response:
        return (
            response.status,
            json.loads(response.read().decode("utf-8")),
            dict(response.headers),
        )


class DashboardMediaFullLifecycleTests(unittest.TestCase):
    def setUp(self):
        private = tempfile.TemporaryDirectory(prefix="mark-media-http-e2e-")
        self.addCleanup(private.cleanup)
        self.store = SnapshotStore(Path(private.name) / "history.sqlite")
        self.stager = PrivateWebMediaHandleStore()
        self.addCleanup(self.stager.close)
        self.payload = {
            "category_path": ["Haus & Garten", "Dekoration"],
            "title": "Synthetic media listing",
            "description": "No external listing or upload",
            "price_eur": 12,
        }

    def _stage(self, base):
        code, body, _ = request(
            base, "/api/write/media/stage", method="POST",
            image=PNG_BYTES, filename="original.png",
        )
        self.assertEqual(code, 201, body)
        ref = body["media_ref"]
        self.assertRegex(ref, r"^media_[A-Za-z0-9_-]+$")
        self.assertEqual(len(self.stager.resolve((ref,))), 1)
        return ref

    def _ack(self, base, key, expected_finalized):
        status, body, _ = request(
            base, "/api/dashboard/pending-writes/ack",
            method="POST",
            payload={"scope": "create-media", "idempotency_key": key},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(
            body, {"acknowledged": True, "finalized": expected_finalized},
        )

    def test_stage_confirmed_media_replay_restart_then_ack(self):
        media = SyntheticConfirmedMediaService(self.stager, self.store)
        with servers(self.store, media, self.stager) as base:
            ref = self._stage(base)
            payload = {**self.payload, "media_refs": [ref]}
            code, created, _ = request(
                base, "/api/write/media/ads", method="POST",
                payload=payload, key="synthetic-media-create-01",
            )
            self.assertEqual(code, 200, created)
            self.assertTrue(created["media_persistence_confirmed"])
            self.assertEqual(created["media_post_read_status"], "confirmed")
            self.assertEqual(created["operation_receipt"]["outcome"], "confirmed")
            self.assertEqual(len(media.effects), 1)
            self.assertNotIn(ref, json.dumps(created))
            pending = self.store.dashboard_pending_writes()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].scope, "create-media")
            self.assertFalse(pending[0].acknowledged)
            # Match the dashboard's strict read-only persisted-media proof.
            from mark_api.dashboard import _local_completed_write_response
            proxy = DashboardWriteProxy(
                host="127.0.0.1", port=9,
                bearer_token=BACKEND_TOKEN, ui_token=UI_TOKEN,
                backend_principal=PRINCIPAL, local_reconciliation_enabled=True,
            )
            self.assertTrue(
                _local_completed_write_response(self.store, proxy, pending[0])
            )
        with servers(self.store, media, self.stager) as base:
            replay_code, replay, headers = request(
                base, "/api/write/media/ads", method="POST",
                payload=payload, key="synthetic-media-create-01",
            )
            self.assertEqual((replay_code, replay), (code, created))
            self.assertEqual(headers.get("Idempotency-Replayed"), "true")
            self.assertEqual(len(media.effects), 1)
            self._ack(base, "synthetic-media-create-01", False)
            self._ack(base, "synthetic-media-create-01", True)
            self.assertEqual(self.store.dashboard_pending_writes(), ())
            code, after, h = request(
                base, "/api/write/media/ads", method="POST",
                payload=payload, key="synthetic-media-create-01",
            )
            self.assertEqual((code, after), (replay_code, replay))
            self.assertEqual(h.get("Idempotency-Replayed"), "true")
            self.assertEqual(len(media.effects), 1)

    def test_tampered_final_media_receipt_does_not_replay_or_ack(self):
        media = SyntheticConfirmedMediaService(self.stager, self.store)
        key = "synthetic-media-tampered-final-01"
        with servers(self.store, media, self.stager) as base:
            ref = self._stage(base)
            payload = {**self.payload, "media_refs": [ref]}
            status, body, _ = request(
                base, "/api/write/media/ads", method="POST",
                payload=payload, key=key,
            )
            self.assertEqual(status, 200, body)
            self.assertTrue(body["media_persistence_confirmed"])
            self.assertEqual(len(media.effects), 1)
        # Simulate corruption of one durable operation row after a successful
        # backend HTTP-200, before any UI acknowledgement. The local replay
        # never accepts HTTP JSON as proof without its exact persisted receipt.
        from contextlib import closing
        with closing(self.store._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "UPDATE create_operation_receipts "
                    "SET media_persistence_confirmed = 0 "
                    "WHERE authorization_reference = ?",
                    ("write-api:" + key,),
                )
                self.assertEqual(cursor.rowcount, 1)
        with servers(self.store, media, self.stager) as base:
            code, replay, headers = request(
                base, "/api/write/media/ads", method="POST",
                payload=payload, key=key,
            )
            self.assertEqual(code, 409, replay)
            self.assertNotEqual(headers.get("Idempotency-Replayed"), "true")
            self.assertFalse(replay["platform_retry_authorized"])
            code, ack, _ = request(
                base, "/api/dashboard/pending-writes/ack",
                method="POST",
                payload={"scope": "create-media", "idempotency_key": key},
            )
            self.assertEqual(code, 409, ack)
            self.assertFalse(ack["platform_retry_authorized"])
            self.assertEqual(len(media.effects), 1)
            pending = self.store.dashboard_pending_writes()
            self.assertEqual(len(pending), 1)
            self.assertFalse(pending[0].acknowledged)

    def test_unverified_media_cannot_ack_or_retry(self):
        media = SyntheticConfirmedMediaService(self.stager, self.store, confirmed=False)
        with servers(self.store, media, self.stager) as base:
            ref = self._stage(base)
            payload = {**self.payload, "media_refs": [ref]}
            code, first, _ = request(
                base, "/api/write/media/ads", method="POST",
                payload=payload, key="synthetic-media-unknown-01",
            )
            self.assertEqual(code, 202, first)
            self.assertFalse(first.get("media_persistence_confirmed", True))
            self.assertEqual(len(media.effects), 1)
        with servers(self.store, media, self.stager) as base:
            code, blocked, _ = request(
                base, "/api/write/media/ads", method="POST",
                payload=payload, key="synthetic-media-unknown-01",
            )
            self.assertEqual(code, 409, blocked)
            self.assertFalse(blocked["platform_retry_authorized"])
            code, ack, _ = request(
                base, "/api/dashboard/pending-writes/ack", method="POST",
                payload={
                    "scope": "create-media",
                    "idempotency_key": "synthetic-media-unknown-01",
                },
            )
            self.assertEqual(code, 409, ack)
            self.assertFalse(ack["platform_retry_authorized"])
            self.assertEqual(len(media.effects), 1)
            self.assertEqual(len(self.store.dashboard_pending_writes()), 1)


if __name__ == "__main__":
    unittest.main()