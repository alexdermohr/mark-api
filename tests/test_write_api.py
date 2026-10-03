from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from mark_api.domain import (
    AdCreateRequest,
    AdSnapshot,
    CreateOperationReceipt,
    DeleteApproval,
    LifecycleState,
    MediaPostReadStatus,
    OperationOutcome,
    OperationReceipt,
)
from mark_api.storage import SnapshotStore
from mark_api.write_api import (
    WriteApiAccess,
    WriteCapability,
    _request_fingerprint,
    create_write_api_server,
)


TOKEN = "fixture-write-api-bearer-token"
NOW = datetime(2026, 10, 1, 4, 30, tzinfo=timezone.utc)
_MISSING = object()


def receipt(
    operation: str,
    ad_id: str,
    *,
    outcome: OperationOutcome = OperationOutcome.CONFIRMED,
    authorization_by: str | None = None,
    authorization_reference: str | None = None,
) -> OperationReceipt:
    return OperationReceipt(
        operation=operation,
        ad_id=ad_id,
        started_at=NOW,
        completed_at=NOW,
        outcome=outcome,
        pre_read_status="success_nonempty",
        post_read_status=(
            "success_nonempty"
            if outcome is not OperationOutcome.PRECONDITION_FAILED
            else None
        ),
        writer_invoked=outcome is not OperationOutcome.PRECONDITION_FAILED,
        authorization_by=authorization_by,
        authorization_reference=authorization_reference,
    )


def create_receipt(
    *,
    request: AdCreateRequest,
    outcome: OperationOutcome = OperationOutcome.CONFIRMED,
    authorization_by: str | None = None,
    authorization_reference: str | None = None,
    operation: str = "create",
    include_snapshots: bool = True,
) -> CreateOperationReceipt:
    confirmed = outcome is OperationOutcome.CONFIRMED
    candidate = (
        AdSnapshot(
            ad_id="200",
            observed_at=NOW,
            source="management",
            lifecycle_state=LifecycleState.ACTIVE,
            title=request.title,
        )
        if confirmed and include_snapshots
        else None
    )
    content = (
        AdSnapshot(
            ad_id="200",
            observed_at=NOW,
            source="management+private-web",
            lifecycle_state=LifecycleState.ACTIVE,
            title=request.title,
            description=request.description,
        )
        if confirmed and include_snapshots
        else None
    )
    return CreateOperationReceipt(
        operation=operation,
        started_at=NOW,
        completed_at=NOW,
        outcome=outcome,
        pre_read_status="success_empty",
        confirmation_pre_read_status="success_empty",
        post_read_status=(
            "success_nonempty"
            if outcome is not OperationOutcome.PRECONDITION_FAILED
            else None
        ),
        confirmation_post_read_status=(
            "success_nonempty"
            if outcome is not OperationOutcome.PRECONDITION_FAILED
            else None
        ),
        content_post_read_status=(
            "success_nonempty"
            if confirmed
            else None
        ),
        writer_invoked=outcome is not OperationOutcome.PRECONDITION_FAILED,
        created_ad_id="200" if confirmed else None,
        authorization_by=authorization_by,
        authorization_reference=authorization_reference,
        post_snapshot=candidate,
        confirmation_post_snapshot=candidate,
        content_post_snapshot=content,
    )


class FakeWriteService:
    def __init__(
        self,
        *,
        outcome: OperationOutcome = OperationOutcome.CONFIRMED,
        fail: bool = False,
    ) -> None:
        self.outcome = outcome
        self.fail = fail
        self.calls: list[tuple[object, ...]] = []
        self.delete_approval: DeleteApproval | None = None

    def _maybe_fail(self) -> None:
        if self.fail:
            raise RuntimeError("provider details must not escape")

    def create(
        self,
        request: AdCreateRequest,
        *,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> CreateOperationReceipt:
        self.calls.append(
            (
                "create",
                request,
                authorization_by,
                authorization_reference,
            )
        )
        self._maybe_fail()
        return create_receipt(
            request=request,
            outcome=self.outcome,
            authorization_by=authorization_by,
            authorization_reference=authorization_reference,
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
        self.calls.append(
            (
                "update_content",
                ad_id,
                title,
                description,
                authorization_by,
                authorization_reference,
            )
        )
        self._maybe_fail()
        return receipt(
            "update_content",
            ad_id,
            outcome=self.outcome,
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
        self.calls.append(
            ("pause", ad_id, authorization_by, authorization_reference)
        )
        self._maybe_fail()
        return receipt(
            "set_state:paused",
            ad_id,
            outcome=self.outcome,
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
        self.calls.append(
            ("activate", ad_id, authorization_by, authorization_reference)
        )
        self._maybe_fail()
        return receipt(
            "set_state:active",
            ad_id,
            outcome=self.outcome,
            authorization_by=authorization_by,
            authorization_reference=authorization_reference,
        )

    def delete(
        self,
        ad_id: str,
        *,
        approval: DeleteApproval,
    ) -> OperationReceipt:
        self.calls.append(("delete", ad_id))
        self.delete_approval = approval
        self._maybe_fail()
        return receipt(
            "delete",
            ad_id,
            outcome=self.outcome,
            authorization_by=approval.approved_by,
            authorization_reference=approval.reference,
        )


class FakeMediaWriteService:
    def __init__(
        self,
        *,
        outcome: OperationOutcome = OperationOutcome.CONFIRMED,
        fail: bool = False,
        media_post_read_status: MediaPostReadStatus | None = None,
        media_persistence_confirmed: bool = False,
    ) -> None:
        self.outcome = outcome
        self.fail = fail
        self.media_post_read_status = media_post_read_status
        self.media_persistence_confirmed = media_persistence_confirmed
        self.calls: list[tuple[object, ...]] = []

    def create_with_media(
        self,
        request: AdCreateRequest,
        media_refs: tuple[str, ...],
        *,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> CreateOperationReceipt:
        self.calls.append(
            (
                "create_with_media",
                request,
                media_refs,
                authorization_by,
                authorization_reference,
            )
        )
        if self.fail:
            raise RuntimeError("media provider details must not escape")
        receipt = create_receipt(
            request=request,
            outcome=self.outcome,
            authorization_by=authorization_by,
            authorization_reference=authorization_reference,
        )
        return replace(
            receipt,
            media_post_read_status=self.media_post_read_status,
            media_persistence_confirmed=self.media_persistence_confirmed,
        )


class MisboundWriteService(FakeWriteService):
    def update_content(
        self,
        ad_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> OperationReceipt:
        self.calls.append(
            (
                "update_content",
                ad_id,
                title,
                description,
                authorization_by,
                authorization_reference,
            )
        )
        return receipt(
            "update_content",
            "9999999999",
            outcome=self.outcome,
            authorization_by=authorization_by,
            authorization_reference=authorization_reference,
        )


class MisboundCreateService(FakeWriteService):
    def create(
        self,
        request: AdCreateRequest,
        *,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> CreateOperationReceipt:
        self.calls.append(
            (
                "create",
                request,
                authorization_by,
                authorization_reference,
            )
        )
        return create_receipt(
            request=request,
            outcome=self.outcome,
            authorization_by=authorization_by,
            authorization_reference=authorization_reference,
            include_snapshots=False,
        )


class WriteApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SnapshotStore(Path(self.tmp.name) / "mark.sqlite")
        self.opener = build_opener(ProxyHandler({}))

    @contextmanager
    def server(
        self,
        service: FakeWriteService,
        *,
        media_service: FakeMediaWriteService | None = None,
        capabilities: frozenset[WriteCapability] = frozenset(
            {
                WriteCapability.CREATE,
                WriteCapability.UPDATE_CONTENT,
                WriteCapability.SET_STATE,
                WriteCapability.DELETE,
            }
        ),
        writes_enabled: bool = True,
    ):
        access = WriteApiAccess(
            principal="api-test-owner",
            bearer_token=TOKEN,
            capabilities=capabilities,
            writes_enabled=writes_enabled,
        )
        server = create_write_api_server(
            service,
            self.store,
            access,
            media_service=media_service,
            port=0,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def request(
        self,
        server,
        method: str,
        path: str,
        *,
        payload: object = _MISSING,
        token: str | None = TOKEN,
        idempotency_key: str | None = "request-1",
    ):
        headers: dict[str, str] = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        data = None
        if payload is not _MISSING:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        host, port = server.server_address
        self.assertEqual(host, "127.0.0.1")
        request = Request(
            f"http://127.0.0.1:{port}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            response = self.opener.open(request, timeout=2)
        except HTTPError as exc:
            body = json.loads(exc.read())
            return exc.code, exc.headers, body
        with response:
            body = json.loads(response.read())
            return response.status, response.headers, body

    def test_access_hides_token_and_server_is_loopback_only(self) -> None:
        access = WriteApiAccess(
            principal="owner",
            bearer_token=TOKEN,
        )
        self.assertNotIn(TOKEN, repr(access))
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            create_write_api_server(
                FakeWriteService(),
                self.store,
                access,
                host="0.0.0.0",
                port=0,
            )

    def test_health_is_read_only_and_reports_write_gate(self) -> None:
        service = FakeWriteService()
        with self.server(service, writes_enabled=False) as server:
            status, _, body = self.request(
                server,
                "GET",
                "/healthz",
                token=None,
                idempotency_key=None,
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "writes_enabled": False})
        self.assertEqual(service.calls, [])

    def test_auth_write_gate_and_capability_fail_before_claim_or_service(self) -> None:
        cases = (
            ("bad-token", True, frozenset({WriteCapability.UPDATE_CONTENT}), 401),
            (TOKEN, False, frozenset({WriteCapability.UPDATE_CONTENT}), 403),
            (TOKEN, True, frozenset(), 403),
        )
        for index, (token, enabled, capabilities, expected_status) in enumerate(cases):
            with self.subTest(expected_status=expected_status, index=index):
                service = FakeWriteService()
                key = f"gate-{index}"
                with self.server(
                    service,
                    capabilities=capabilities,
                    writes_enabled=enabled,
                ) as server:
                    status, _, _ = self.request(
                        server,
                        "PATCH",
                        "/api/write/ads/1234567890",
                        payload={"title": "new"},
                        token=token,
                        idempotency_key=key,
                    )
                self.assertEqual(status, expected_status)
                self.assertEqual(service.calls, [])
                self.assertIsNone(self.store.write_api_request(key))

    def test_create_replays_normalized_request_across_server_restart(self) -> None:
        service = FakeWriteService()
        first_payload = {
            "category_path": [" Haus & Garten ", "Dekoration"],
            "title": "Neue Vase",
            "description": "Beschreibung",
            "price_eur": 12,
        }
        with self.server(service) as server:
            first_status, first_headers, first_body = self.request(
                server,
                "POST",
                "/api/write/ads",
                payload=first_payload,
                idempotency_key="create-once",
            )

        self.assertEqual(first_status, 200)
        self.assertIsNone(first_headers.get("Idempotency-Replayed"))
        self.assertEqual(len(service.calls), 1)
        call = service.calls[0]
        self.assertEqual(call[0], "create")
        self.assertEqual(
            call[1],
            AdCreateRequest(
                category_path=("Haus & Garten", "Dekoration"),
                title="Neue Vase",
                description="Beschreibung",
                price_eur=12,
            ),
        )
        self.assertEqual(call[2], "api-test-owner")
        self.assertEqual(call[3], "write-api:create-once")
        self.assertEqual(
            first_body["operation_receipt"]["created_ad_id"],
            "200",
        )
        self.assertEqual(
            first_body["operation_receipt"]["authorization_by"],
            "api-test-owner",
        )
        self.assertEqual(
            first_body["operation_receipt"]["authorization_reference"],
            "write-api:create-once",
        )
        self.assertFalse(first_body["platform_retry_authorized"])

        fresh_service = FakeWriteService()
        second_payload = {
            "category_path": ["Haus & Garten", "Dekoration"],
            "title": "Neue Vase",
            "description": "Beschreibung",
            "price_eur": 12,
        }
        with self.server(fresh_service) as server:
            second_status, second_headers, second_body = self.request(
                server,
                "POST",
                "/api/write/ads",
                payload=second_payload,
                idempotency_key="create-once",
            )

        self.assertEqual(second_status, 200)
        self.assertEqual(
            second_headers.get("Idempotency-Replayed"),
            "true",
        )
        self.assertEqual(second_body, first_body)
        self.assertEqual(fresh_service.calls, [])

    def test_create_capability_and_validation_fail_before_claim(self) -> None:
        valid_payload = {
            "category_path": ["Haus & Garten", "Dekoration"],
            "title": "Neue Vase",
            "description": "Beschreibung",
            "price_eur": 12,
        }
        service = FakeWriteService()
        with self.server(
            service,
            capabilities=frozenset({WriteCapability.UPDATE_CONTENT}),
        ) as server:
            status, _, body = self.request(
                server,
                "POST",
                "/api/write/ads",
                payload=valid_payload,
                idempotency_key="create-capability-denied",
            )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "capability_denied")
        self.assertEqual(service.calls, [])
        self.assertIsNone(
            self.store.write_api_request("create-capability-denied")
        )

        service = FakeWriteService()
        invalid_payload = {
            **valid_payload,
            "media": ["not-supported"],
        }
        with self.server(service) as server:
            status, _, body = self.request(
                server,
                "POST",
                "/api/write/ads",
                payload=invalid_payload,
                idempotency_key="create-invalid",
            )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_create_request")
        self.assertEqual(service.calls, [])
        self.assertIsNone(self.store.write_api_request("create-invalid"))

    def test_media_create_requires_distinct_capability_and_service(
        self,
    ) -> None:
        payload = {
            "category_path": ["Haus & Garten", "Dekoration"],
            "title": "Neue Vase",
            "description": "Beschreibung",
            "price_eur": 12,
            "media_refs": ["media_ref_1"],
        }

        standard_service = FakeWriteService()
        with self.server(standard_service) as server:
            status, _, body = self.request(
                server,
                "POST",
                "/api/write/media/ads",
                payload=payload,
                idempotency_key="media-capability-denied",
            )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "capability_denied")
        self.assertEqual(standard_service.calls, [])
        self.assertIsNone(
            self.store.write_api_request("media-capability-denied")
        )

        access = WriteApiAccess(
            principal="api-test-owner",
            bearer_token=TOKEN,
            capabilities=frozenset({WriteCapability.CREATE_MEDIA}),
            writes_enabled=True,
        )
        with self.assertRaises(ValueError):
            create_write_api_server(
                FakeWriteService(),
                self.store,
                access,
                port=0,
            )

        media_service = FakeMediaWriteService()
        with self.server(
            FakeWriteService(),
            media_service=media_service,
            capabilities=frozenset({WriteCapability.CREATE_MEDIA}),
        ) as server:
            standard_status, _, standard_body = self.request(
                server,
                "POST",
                "/api/write/ads",
                payload={
                    "category_path": ["Haus & Garten", "Dekoration"],
                    "title": "Neue Vase",
                    "description": "Beschreibung",
                    "price_eur": 12,
                },
                idempotency_key="standard-capability-denied",
            )
        self.assertEqual(standard_status, 403)
        self.assertEqual(standard_body["error"], "capability_denied")
        self.assertEqual(media_service.calls, [])
        self.assertIsNone(
            self.store.write_api_request("standard-capability-denied")
        )

    def test_media_create_replays_normalized_opaque_refs(self) -> None:
        service = FakeWriteService()
        media_service = FakeMediaWriteService()
        payload = {
            "category_path": [" Haus & Garten ", "Dekoration"],
            "title": "Neue Vase",
            "description": "Beschreibung",
            "price_eur": 12,
            "media_refs": ["cover_01", "detail-02"],
        }
        capabilities = frozenset({WriteCapability.CREATE_MEDIA})
        with self.server(
            service,
            media_service=media_service,
            capabilities=capabilities,
        ) as server:
            first_status, first_headers, first_body = self.request(
                server,
                "POST",
                "/api/write/media/ads",
                payload=payload,
                idempotency_key="media-create-once",
            )

        self.assertEqual(first_status, 202)
        self.assertIsNone(first_headers.get("Idempotency-Replayed"))
        self.assertEqual(service.calls, [])
        self.assertEqual(
            media_service.calls,
            [
                (
                    "create_with_media",
                    AdCreateRequest(
                        category_path=("Haus & Garten", "Dekoration"),
                        title="Neue Vase",
                        description="Beschreibung",
                        price_eur=12,
                    ),
                    ("cover_01", "detail-02"),
                    "api-test-owner",
                    "write-api:media-create-once",
                )
            ],
        )
        self.assertFalse(first_body["platform_retry_authorized"])
        self.assertFalse(first_body["media_persistence_confirmed"])
        self.assertIsNone(first_body["media_post_read_status"])
        self.assertNotIn(
            "media_post_read_status",
            first_body["operation_receipt"],
        )

        fresh_standard = FakeWriteService()
        fresh_media = FakeMediaWriteService()
        with self.server(
            fresh_standard,
            media_service=fresh_media,
            capabilities=capabilities,
        ) as server:
            second_status, second_headers, second_body = self.request(
                server,
                "POST",
                "/api/write/media/ads",
                payload={
                    "category_path": ["Haus & Garten", "Dekoration"],
                    "title": "Neue Vase",
                    "description": "Beschreibung",
                    "price_eur": 12,
                    "media_refs": ["cover_01", "detail-02"],
                },
                idempotency_key="media-create-once",
            )

        self.assertEqual(second_status, 202)
        self.assertEqual(
            second_headers.get("Idempotency-Replayed"),
            "true",
        )
        self.assertEqual(second_body, first_body)
        self.assertEqual(fresh_standard.calls, [])
        self.assertEqual(fresh_media.calls, [])

    def test_media_create_confirmed_persistence_returns_200_and_replays(
        self,
    ) -> None:
        service = FakeWriteService()
        media_service = FakeMediaWriteService(
            media_post_read_status=MediaPostReadStatus.CONFIRMED,
            media_persistence_confirmed=True,
        )
        payload = {
            "category_path": ["Haus & Garten", "Dekoration"],
            "title": "Neue Vase",
            "description": "Beschreibung",
            "price_eur": 12,
            "media_refs": ["cover_01"],
        }
        capabilities = frozenset({WriteCapability.CREATE_MEDIA})

        with self.server(
            service,
            media_service=media_service,
            capabilities=capabilities,
        ) as server:
            first_status, first_headers, first_body = self.request(
                server,
                "POST",
                "/api/write/media/ads",
                payload=payload,
                idempotency_key="media-create-confirmed",
            )

        self.assertEqual(first_status, 200)
        self.assertIsNone(first_headers.get("Idempotency-Replayed"))
        self.assertTrue(first_body["media_persistence_confirmed"])
        self.assertEqual(first_body["media_post_read_status"], "confirmed")
        self.assertEqual(
            first_body["operation_receipt"]["media_post_read_status"],
            "confirmed",
        )
        self.assertTrue(
            first_body["operation_receipt"]["media_persistence_confirmed"]
        )
        self.assertFalse(first_body["platform_retry_authorized"])
        serialized = json.dumps(first_body, sort_keys=True)
        self.assertNotIn("media_refs", serialized)
        self.assertNotIn("cover_01", serialized)

        fresh_standard = FakeWriteService()
        fresh_media = FakeMediaWriteService()
        with self.server(
            fresh_standard,
            media_service=fresh_media,
            capabilities=capabilities,
        ) as server:
            second_status, second_headers, second_body = self.request(
                server,
                "POST",
                "/api/write/media/ads",
                payload=payload,
                idempotency_key="media-create-confirmed",
            )

        self.assertEqual(second_status, 200)
        self.assertEqual(
            second_headers.get("Idempotency-Replayed"),
            "true",
        )
        self.assertEqual(second_body, first_body)
        self.assertEqual(fresh_standard.calls, [])
        self.assertEqual(fresh_media.calls, [])

    def test_media_and_standard_create_share_server_serialization(self) -> None:
        standard_entered = threading.Event()
        release_standard = threading.Event()
        media_entered = threading.Event()
        errors: list[BaseException] = []
        results: dict[str, tuple] = {}

        class BlockingStandardService(FakeWriteService):
            def create(
                self,
                request: AdCreateRequest,
                *,
                authorization_by: str | None = None,
                authorization_reference: str | None = None,
            ) -> CreateOperationReceipt:
                standard_entered.set()
                if not release_standard.wait(timeout=2):
                    raise AssertionError("standard create was not released")
                return super().create(
                    request,
                    authorization_by=authorization_by,
                    authorization_reference=authorization_reference,
                )

        class ObservedMediaService(FakeMediaWriteService):
            def create_with_media(
                self,
                request: AdCreateRequest,
                media_refs: tuple[str, ...],
                *,
                authorization_by: str | None = None,
                authorization_reference: str | None = None,
            ) -> CreateOperationReceipt:
                media_entered.set()
                return super().create_with_media(
                    request,
                    media_refs,
                    authorization_by=authorization_by,
                    authorization_reference=authorization_reference,
                )

        standard_service = BlockingStandardService()
        media_service = ObservedMediaService()
        standard_payload = {
            "category_path": ["Haus & Garten", "Dekoration"],
            "title": "Standard Vase",
            "description": "Beschreibung",
            "price_eur": 12,
        }
        media_payload = {
            "category_path": ["Haus & Garten", "Dekoration"],
            "title": "Media Vase",
            "description": "Beschreibung",
            "price_eur": 13,
            "media_refs": ["cover_01"],
        }

        def invoke(name: str, server, path: str, payload: dict, key: str) -> None:
            try:
                results[name] = self.request(
                    server,
                    "POST",
                    path,
                    payload=payload,
                    idempotency_key=key,
                )
            except BaseException as exc:
                errors.append(exc)

        with self.server(
            standard_service,
            media_service=media_service,
            capabilities=frozenset(
                {WriteCapability.CREATE, WriteCapability.CREATE_MEDIA}
            ),
        ) as server:
            standard = threading.Thread(
                target=invoke,
                args=(
                    "standard",
                    server,
                    "/api/write/ads",
                    standard_payload,
                    "cross-route-standard",
                ),
            )
            media = threading.Thread(
                target=invoke,
                args=(
                    "media",
                    server,
                    "/api/write/media/ads",
                    media_payload,
                    "cross-route-media",
                ),
            )

            media_started = False
            standard.start()
            try:
                self.assertTrue(standard_entered.wait(timeout=2))
                media.start()
                media_started = True

                deadline = time.monotonic() + 2
                media_claim = None
                while time.monotonic() < deadline:
                    media_claim = self.store.write_api_request(
                        "cross-route-media"
                    )
                    if (
                        media_claim is not None
                        and media_claim.state == "in_progress"
                    ):
                        break
                    time.sleep(0.01)

                self.assertIsNotNone(media_claim)
                assert media_claim is not None
                self.assertEqual(media_claim.state, "in_progress")
                self.assertFalse(media_entered.wait(timeout=0.1))
            finally:
                release_standard.set()
                standard.join(timeout=2)
                if media_started:
                    media.join(timeout=2)

        self.assertFalse(standard.is_alive())
        self.assertFalse(media.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(media_entered.is_set())
        self.assertEqual(results["standard"][0], 200)
        self.assertEqual(results["media"][0], 202)
        self.assertEqual(len(standard_service.calls), 1)
        self.assertEqual(len(media_service.calls), 1)

    def test_media_refs_are_opaque_unique_ascii_handles(self) -> None:
        invalid_refs = (
            [],
            ["/tmp/photo.jpg"],
            ["../photo"],
            ["photo.jpg"],
            ["C:\\photo.jpg"],
            ["ümlaut"],
            ["duplicate", "duplicate"],
            "not-a-list",
            [1],
        )
        for index, media_refs in enumerate(invalid_refs):
            with self.subTest(media_refs=media_refs):
                key = f"invalid-media-ref-{index}"
                media_service = FakeMediaWriteService()
                with self.server(
                    FakeWriteService(),
                    media_service=media_service,
                    capabilities=frozenset(
                        {WriteCapability.CREATE_MEDIA}
                    ),
                ) as server:
                    status, _, body = self.request(
                        server,
                        "POST",
                        "/api/write/media/ads",
                        payload={
                            "category_path": [
                                "Haus & Garten",
                                "Dekoration",
                            ],
                            "title": "Neue Vase",
                            "description": "Beschreibung",
                            "price_eur": 12,
                            "media_refs": media_refs,
                        },
                        idempotency_key=key,
                    )
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "invalid_media_refs")
                self.assertEqual(media_service.calls, [])
                self.assertIsNone(self.store.write_api_request(key))

    def test_media_refs_participate_in_idempotency_fingerprint(self) -> None:
        media_service = FakeMediaWriteService()
        capabilities = frozenset({WriteCapability.CREATE_MEDIA})
        base_payload = {
            "category_path": ["Haus & Garten", "Dekoration"],
            "title": "Neue Vase",
            "description": "Beschreibung",
            "price_eur": 12,
        }
        with self.server(
            FakeWriteService(),
            media_service=media_service,
            capabilities=capabilities,
        ) as server:
            first_status, _, _ = self.request(
                server,
                "POST",
                "/api/write/media/ads",
                payload={**base_payload, "media_refs": ["media_a"]},
                idempotency_key="media-ref-conflict",
            )
            second_status, _, second_body = self.request(
                server,
                "POST",
                "/api/write/media/ads",
                payload={**base_payload, "media_refs": ["media_b"]},
                idempotency_key="media-ref-conflict",
            )

        self.assertEqual(first_status, 202)
        self.assertEqual(second_status, 409)
        self.assertEqual(second_body["error"], "idempotency_conflict")
        self.assertFalse(second_body["platform_retry_authorized"])
        self.assertEqual(len(media_service.calls), 1)

    def test_media_create_outcomes_map_without_retry_authority(self) -> None:
        payload = {
            "category_path": ["Haus & Garten", "Dekoration"],
            "title": "Neue Vase",
            "description": "Beschreibung",
            "price_eur": 12,
            "media_refs": ["media_ref_1"],
        }
        for outcome, expected_status in (
            (OperationOutcome.AMBIGUOUS, 202),
            (OperationOutcome.PRECONDITION_FAILED, 409),
        ):
            with self.subTest(outcome=outcome.value):
                media_service = FakeMediaWriteService(outcome=outcome)
                key = f"media-create-{outcome.value}"
                with self.server(
                    FakeWriteService(),
                    media_service=media_service,
                    capabilities=frozenset(
                        {WriteCapability.CREATE_MEDIA}
                    ),
                ) as server:
                    status, _, body = self.request(
                        server,
                        "POST",
                        "/api/write/media/ads",
                        payload=payload,
                        idempotency_key=key,
                    )
                self.assertEqual(status, expected_status)
                self.assertEqual(
                    body["operation_receipt"]["outcome"],
                    outcome.value,
                )
                self.assertIsNone(
                    body["operation_receipt"]["created_ad_id"]
                )
                self.assertFalse(body["platform_retry_authorized"])
                self.assertFalse(body["media_persistence_confirmed"])

    def test_create_rejects_unpaired_surrogate_before_claim(self) -> None:
        service = FakeWriteService()
        with self.server(service) as server:
            status, _, body = self.request(
                server,
                "POST",
                "/api/write/ads",
                payload={
                    "category_path": ["Haus & Garten", "Dekoration"],
                    "title": "Neue Vase\ud800",
                    "description": "Beschreibung",
                    "price_eur": 12,
                },
                idempotency_key="create-surrogate",
            )

        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_create_request")
        self.assertEqual(service.calls, [])
        self.assertIsNone(
            self.store.write_api_request("create-surrogate")
        )

    def test_fingerprint_rejects_unpaired_surrogate_without_claim(self) -> None:
        service = FakeWriteService()
        with self.server(service) as server:
            status, _, body = self.request(
                server,
                "PATCH",
                "/api/write/ads/1234567890",
                payload={"title": "Neue Vase\ud800"},
                idempotency_key="patch-surrogate",
            )

        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_unicode_text")
        self.assertFalse(body["platform_retry_authorized"])
        self.assertEqual(service.calls, [])
        self.assertIsNone(
            self.store.write_api_request("patch-surrogate")
        )

    def test_create_outcomes_map_without_retry_authority(self) -> None:
        for outcome, expected_status in (
            (OperationOutcome.AMBIGUOUS, 202),
            (OperationOutcome.PRECONDITION_FAILED, 409),
        ):
            with self.subTest(outcome=outcome.value):
                service = FakeWriteService(outcome=outcome)
                key = f"create-{outcome.value}"
                with self.server(service) as server:
                    status, _, body = self.request(
                        server,
                        "POST",
                        "/api/write/ads",
                        payload={
                            "category_path": [
                                "Haus & Garten",
                                "Dekoration",
                            ],
                            "title": "Neue Vase",
                            "description": "Beschreibung",
                            "price_eur": 12,
                        },
                        idempotency_key=key,
                    )
                self.assertEqual(status, expected_status)
                self.assertEqual(
                    body["operation_receipt"]["outcome"],
                    outcome.value,
                )
                self.assertIsNone(
                    body["operation_receipt"]["created_ad_id"]
                )
                self.assertFalse(body["platform_retry_authorized"])

    def test_misbound_create_receipt_fails_closed_and_is_not_reexecuted(self) -> None:
        payload = {
            "category_path": ["Haus & Garten", "Dekoration"],
            "title": "Neue Vase",
            "description": "Beschreibung",
            "price_eur": 12,
        }
        service = MisboundCreateService()
        with self.server(service) as server:
            first_status, _, first_body = self.request(
                server,
                "POST",
                "/api/write/ads",
                payload=payload,
                idempotency_key="create-misbound",
            )

        self.assertEqual(first_status, 500)
        self.assertEqual(first_body["error"], "write_execution_error")
        self.assertFalse(first_body["platform_retry_authorized"])
        self.assertEqual(len(service.calls), 1)

        fresh_service = FakeWriteService()
        with self.server(fresh_service) as server:
            second_status, second_headers, second_body = self.request(
                server,
                "POST",
                "/api/write/ads",
                payload=payload,
                idempotency_key="create-misbound",
            )

        self.assertEqual(second_status, 500)
        self.assertEqual(
            second_headers.get("Idempotency-Replayed"),
            "true",
        )
        self.assertEqual(second_body, first_body)
        self.assertEqual(fresh_service.calls, [])

    def test_patch_replays_persisted_response_across_server_restart(self) -> None:
        service = FakeWriteService()
        payload = {"title": "new title", "description": ""}
        with self.server(service) as server:
            first_status, first_headers, first_body = self.request(
                server,
                "PATCH",
                "/api/write/ads/1234567890",
                payload=payload,
                idempotency_key="patch-once",
            )

        self.assertEqual(first_status, 200)
        self.assertIsNone(first_headers.get("Idempotency-Replayed"))
        self.assertEqual(
            service.calls,
            [
                (
                    "update_content",
                    "1234567890",
                    "new title",
                    "",
                    "api-test-owner",
                    "write-api:patch-once",
                )
            ],
        )
        self.assertEqual(
            first_body["operation_receipt"]["outcome"],
            "confirmed",
        )
        self.assertEqual(
            first_body["operation_receipt"]["authorization_by"],
            "api-test-owner",
        )
        self.assertEqual(
            first_body["operation_receipt"]["authorization_reference"],
            "write-api:patch-once",
        )
        self.assertFalse(first_body["platform_retry_authorized"])

        fresh_service = FakeWriteService()
        with self.server(fresh_service) as server:
            second_status, second_headers, second_body = self.request(
                server,
                "PATCH",
                "/api/write/ads/1234567890",
                payload=payload,
                idempotency_key="patch-once",
            )

        self.assertEqual(second_status, 200)
        self.assertEqual(second_headers.get("Idempotency-Replayed"), "true")
        self.assertEqual(second_body, first_body)
        self.assertEqual(fresh_service.calls, [])
        stored = self.store.write_api_request("patch-once")
        assert stored is not None
        self.assertEqual(stored.state, "completed")
        self.assertEqual(stored.response_status, 200)

    def test_same_key_with_different_request_is_conflict(self) -> None:
        service = FakeWriteService()
        with self.server(service) as server:
            first_status, _, _ = self.request(
                server,
                "PATCH",
                "/api/write/ads/1234567890",
                payload={"title": "one"},
                idempotency_key="conflict-key",
            )
            second_status, _, second_body = self.request(
                server,
                "PATCH",
                "/api/write/ads/1234567890",
                payload={"title": "two"},
                idempotency_key="conflict-key",
            )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 409)
        self.assertEqual(second_body["error"], "idempotency_conflict")
        self.assertFalse(second_body["platform_retry_authorized"])
        self.assertEqual(len(service.calls), 1)

    def test_in_progress_claim_blocks_retry_without_service_call(self) -> None:
        payload = {"title": "one"}
        fingerprint = _request_fingerprint(
            principal="api-test-owner",
            method="PATCH",
            path="/api/write/ads/1234567890",
            payload=payload,
        )
        claim = self.store.claim_write_api_request(
            idempotency_key="stuck-key",
            request_sha256=fingerprint,
            requested_at=NOW,
        )
        self.assertTrue(claim.created)

        service = FakeWriteService()
        with self.server(service) as server:
            status, _, body = self.request(
                server,
                "PATCH",
                "/api/write/ads/1234567890",
                payload=payload,
                idempotency_key="stuck-key",
            )

        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "idempotency_in_progress")
        self.assertFalse(body["platform_retry_authorized"])
        self.assertEqual(service.calls, [])

    def test_pause_and_activate_are_id_bound_and_bodyless(self) -> None:
        service = FakeWriteService()
        with self.server(service) as server:
            pause_status, _, _ = self.request(
                server,
                "POST",
                "/api/write/ads/1234567890/pause",
                idempotency_key="pause-1",
            )
            activate_status, _, _ = self.request(
                server,
                "POST",
                "/api/write/ads/1234567890/activate",
                idempotency_key="activate-1",
            )
            invalid_status, _, invalid_body = self.request(
                server,
                "POST",
                "/api/write/ads/1234567890/pause",
                payload={"unexpected": True},
                idempotency_key="pause-invalid",
            )

        self.assertEqual(pause_status, 200)
        self.assertEqual(activate_status, 200)
        self.assertEqual(invalid_status, 400)
        self.assertEqual(invalid_body["error"], "request_body_not_allowed")
        self.assertEqual(
            service.calls,
            [
                (
                    "pause",
                    "1234567890",
                    "api-test-owner",
                    "write-api:pause-1",
                ),
                (
                    "activate",
                    "1234567890",
                    "api-test-owner",
                    "write-api:activate-1",
                ),
            ],
        )
        self.assertIsNone(self.store.write_api_request("pause-invalid"))

    def test_delete_requires_explicit_matching_confirmation(self) -> None:
        service = FakeWriteService()
        with self.server(service) as server:
            bad_status, _, bad_body = self.request(
                server,
                "DELETE",
                "/api/write/ads/1234567890",
                payload={
                    "confirm_ad_id": "9999999999",
                    "approval_reference": "ticket-42",
                },
                idempotency_key="delete-bad",
            )
            ok_status, _, ok_body = self.request(
                server,
                "DELETE",
                "/api/write/ads/1234567890",
                payload={
                    "confirm_ad_id": "1234567890",
                    "approval_reference": "ticket-42",
                },
                idempotency_key="delete-ok",
            )

        self.assertEqual(bad_status, 400)
        self.assertEqual(bad_body["error"], "invalid_delete_approval")
        self.assertIsNone(self.store.write_api_request("delete-bad"))
        self.assertEqual(ok_status, 200)
        self.assertEqual(service.calls, [("delete", "1234567890")])
        assert service.delete_approval is not None
        self.assertEqual(service.delete_approval.ad_id, "1234567890")
        self.assertEqual(service.delete_approval.approved_by, "api-test-owner")
        self.assertEqual(service.delete_approval.reference, "ticket-42")
        self.assertEqual(
            ok_body["operation_receipt"]["authorization_by"],
            "api-test-owner",
        )

    def test_domain_outcomes_have_explicit_http_status_and_no_retry_authority(self) -> None:
        for outcome, expected in (
            (OperationOutcome.AMBIGUOUS, 202),
            (OperationOutcome.PRECONDITION_FAILED, 409),
        ):
            with self.subTest(outcome=outcome.value):
                service = FakeWriteService(outcome=outcome)
                key = f"outcome-{outcome.value}"
                with self.server(service) as server:
                    status, _, body = self.request(
                        server,
                        "PATCH",
                        "/api/write/ads/1234567890",
                        payload={"title": "new"},
                        idempotency_key=key,
                    )
                self.assertEqual(status, expected)
                self.assertEqual(
                    body["operation_receipt"]["outcome"],
                    outcome.value,
                )
                self.assertFalse(body["platform_retry_authorized"])

    def test_misbound_service_receipt_fails_closed_and_is_not_reexecuted(self) -> None:
        service = MisboundWriteService()
        with self.server(service) as server:
            first_status, _, first_body = self.request(
                server,
                "PATCH",
                "/api/write/ads/1234567890",
                payload={"title": "new"},
                idempotency_key="misbound-once",
            )

        self.assertEqual(first_status, 500)
        self.assertEqual(first_body["error"], "write_execution_error")
        self.assertFalse(first_body["platform_retry_authorized"])
        self.assertEqual(len(service.calls), 1)

        fresh_service = FakeWriteService()
        with self.server(fresh_service) as server:
            second_status, second_headers, second_body = self.request(
                server,
                "PATCH",
                "/api/write/ads/1234567890",
                payload={"title": "new"},
                idempotency_key="misbound-once",
            )

        self.assertEqual(second_status, 500)
        self.assertEqual(second_headers.get("Idempotency-Replayed"), "true")
        self.assertEqual(second_body, first_body)
        self.assertEqual(fresh_service.calls, [])

    def test_execution_error_is_sanitized_persisted_and_not_reexecuted(self) -> None:
        service = FakeWriteService(fail=True)
        with self.server(service) as server:
            first_status, _, first_body = self.request(
                server,
                "PATCH",
                "/api/write/ads/1234567890",
                payload={"title": "new"},
                idempotency_key="error-once",
            )

        self.assertEqual(first_status, 500)
        self.assertEqual(first_body["error"], "write_execution_error")
        self.assertNotIn("provider details", json.dumps(first_body))
        self.assertFalse(first_body["platform_retry_authorized"])
        self.assertEqual(len(service.calls), 1)

        fresh_service = FakeWriteService()
        with self.server(fresh_service) as server:
            second_status, second_headers, second_body = self.request(
                server,
                "PATCH",
                "/api/write/ads/1234567890",
                payload={"title": "new"},
                idempotency_key="error-once",
            )

        self.assertEqual(second_status, 500)
        self.assertEqual(second_headers.get("Idempotency-Replayed"), "true")
        self.assertEqual(second_body, first_body)
        self.assertEqual(fresh_service.calls, [])

    def test_media_reply_and_queries_are_not_routes(self) -> None:
        service = FakeWriteService()
        with self.server(service) as server:
            for method, path in (
                ("POST", "/api/write/ads/1234567890/media"),
                ("POST", "/api/write/conversations/abc/reply"),
                ("PATCH", "/api/write/ads/1234567890?force=1"),
            ):
                with self.subTest(method=method, path=path):
                    status, _, body = self.request(
                        server,
                        method,
                        path,
                        payload={} if method == "PATCH" else _MISSING,
                        idempotency_key=f"missing-{len(service.calls)}",
                    )
                    self.assertIn(status, {400, 404})
                    self.assertIn(
                        body["error"],
                        {"not_found", "query_not_allowed"},
                    )
        self.assertEqual(service.calls, [])


if __name__ == "__main__":
    unittest.main()
