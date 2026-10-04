from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Callable, Protocol
from urllib.parse import unquote, urlsplit

from .domain import (
    AdCreateRequest,
    CreateOperationReceipt,
    DeleteApproval,
    OperationOutcome,
    OperationReceipt,
)
from .storage import SnapshotStore


_MAX_BODY_BYTES = 16 * 1024
_MAX_MEDIA_BODY_BYTES = 25 * 1024 * 1024
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MEDIA_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WriteCapability(str, Enum):
    CREATE = "create"
    CREATE_MEDIA = "create_media"
    UPDATE_CONTENT = "update_content"
    SET_STATE = "set_state"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class WriteApiAccess:
    principal: str
    bearer_token: str = field(repr=False)
    capabilities: frozenset[WriteCapability] = frozenset()
    writes_enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.principal, str) or not self.principal.strip():
            raise ValueError("principal must not be empty")
        if (
            len(self.principal) > 128
            or "\r" in self.principal
            or "\n" in self.principal
        ):
            raise ValueError("principal is invalid")
        if (
            not isinstance(self.bearer_token, str)
            or len(self.bearer_token) < 16
            or any(character.isspace() for character in self.bearer_token)
        ):
            raise ValueError("bearer_token must contain at least 16 safe characters")
        try:
            normalized = frozenset(self.capabilities)
        except TypeError as exc:
            raise TypeError("capabilities must be iterable") from exc
        if any(not isinstance(item, WriteCapability) for item in normalized):
            raise TypeError("capabilities must contain WriteCapability values")
        if not isinstance(self.writes_enabled, bool):
            raise TypeError("writes_enabled must be bool")
        object.__setattr__(self, "principal", self.principal.strip())
        object.__setattr__(self, "capabilities", normalized)


class _MediaWriteService(Protocol):
    def create_with_media(
        self,
        request: AdCreateRequest,
        media_refs: tuple[str, ...],
        *,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> CreateOperationReceipt:
        ...


class _MediaStager(Protocol):
    def stage_media(self, filename: str, data: bytes) -> str:
        ...



class _WriteService(Protocol):
    def create(
        self,
        request: AdCreateRequest,
        *,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> CreateOperationReceipt:
        ...

    def update_content(
        self,
        ad_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> OperationReceipt:
        ...

    def pause(
        self,
        ad_id: str,
        *,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> OperationReceipt:
        ...

    def activate(
        self,
        ad_id: str,
        *,
        authorization_by: str | None = None,
        authorization_reference: str | None = None,
    ) -> OperationReceipt:
        ...

    def delete(
        self,
        ad_id: str,
        *,
        approval: DeleteApproval,
    ) -> OperationReceipt:
        ...


def _valid_ad_id(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 32
        and value.isascii()
        and value.isdigit()
    )


def _normalize_create_payload(
    payload: dict[str, object],
    *,
    with_media: bool,
) -> tuple[
    AdCreateRequest,
    tuple[str, ...] | None,
    dict[str, object],
]:
    expected = {
        "category_path",
        "title",
        "description",
        "price_eur",
    }
    if with_media:
        expected.add("media_refs")
    if set(payload) != expected:
        raise ValueError(
            "invalid_media_create_request"
            if with_media
            else "invalid_create_request"
        )

    category_path = payload["category_path"]
    if (
        not isinstance(category_path, list)
        or any(not isinstance(label, str) for label in category_path)
    ):
        raise ValueError(
            "invalid_media_create_request"
            if with_media
            else "invalid_create_request"
        )
    try:
        request = AdCreateRequest(
            category_path=tuple(category_path),
            title=payload["title"],
            description=payload["description"],
            price_eur=payload["price_eur"],
        )
    except (TypeError, ValueError):
        raise ValueError(
            "invalid_media_create_request"
            if with_media
            else "invalid_create_request"
        ) from None

    normalized: dict[str, object] = {
        "category_path": list(request.category_path),
        "title": request.title,
        "description": request.description,
        "price_eur": request.price_eur,
    }
    if not with_media:
        return request, None, normalized

    raw_media_refs = payload["media_refs"]
    if (
        not isinstance(raw_media_refs, list)
        or not raw_media_refs
        or any(
            not isinstance(ref, str)
            or _MEDIA_REF_RE.fullmatch(ref) is None
            for ref in raw_media_refs
        )
    ):
        raise ValueError("invalid_media_refs")
    media_refs = tuple(raw_media_refs)
    if len(set(media_refs)) != len(media_refs):
        raise ValueError("invalid_media_refs")
    normalized["media_refs"] = list(media_refs)
    return request, media_refs, normalized


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _request_fingerprint(
    *,
    principal: str,
    method: str,
    path: str,
    payload: object,
) -> str:
    try:
        encoded = _canonical_json(
            {
                "principal": principal,
                "method": method,
                "path": path,
                "payload": payload,
            }
        ).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "request contains invalid Unicode scalar text"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _receipt_to_dict(receipt: OperationReceipt) -> dict[str, object]:
    return {
        "operation": receipt.operation,
        "ad_id": receipt.ad_id,
        "started_at": receipt.started_at.isoformat(),
        "completed_at": receipt.completed_at.isoformat(),
        "outcome": receipt.outcome.value,
        "pre_read_status": receipt.pre_read_status,
        "post_read_status": receipt.post_read_status,
        "writer_invoked": receipt.writer_invoked,
        "authorization_by": receipt.authorization_by,
        "authorization_reference": receipt.authorization_reference,
        "writer_error": receipt.writer_error,
    }


def _create_receipt_to_dict(
    receipt: CreateOperationReceipt,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "operation": receipt.operation,
        "created_ad_id": receipt.created_ad_id,
        "started_at": receipt.started_at.isoformat(),
        "completed_at": receipt.completed_at.isoformat(),
        "outcome": receipt.outcome.value,
        "pre_read_status": receipt.pre_read_status,
        "confirmation_pre_read_status": receipt.confirmation_pre_read_status,
        "post_read_status": receipt.post_read_status,
        "confirmation_post_read_status": receipt.confirmation_post_read_status,
        "content_post_read_status": receipt.content_post_read_status,
        "writer_invoked": receipt.writer_invoked,
        "authorization_by": receipt.authorization_by,
        "authorization_reference": receipt.authorization_reference,
        "writer_error": receipt.writer_error,
    }
    if receipt.media_post_read_status is not None:
        payload["media_post_read_status"] = receipt.media_post_read_status.value
        payload["media_persistence_confirmed"] = (
            receipt.media_persistence_confirmed
        )
    return payload


def _create_receipt_matches_request(
    receipt: CreateOperationReceipt,
    request: AdCreateRequest,
    *,
    principal: str,
    authorization_reference: str,
) -> bool:
    if (
        receipt.operation != "create"
        or receipt.authorization_by != principal
        or receipt.authorization_reference != authorization_reference
    ):
        return False
    if receipt.outcome is not OperationOutcome.CONFIRMED:
        return receipt.created_ad_id is None

    created_ad_id = receipt.created_ad_id
    snapshots = (
        receipt.post_snapshot,
        receipt.confirmation_post_snapshot,
        receipt.content_post_snapshot,
    )
    if created_ad_id is None or any(
        snapshot is None for snapshot in snapshots
    ):
        return False
    post_snapshot = receipt.post_snapshot
    confirmation_snapshot = receipt.confirmation_post_snapshot
    content_snapshot = receipt.content_post_snapshot
    assert post_snapshot is not None
    assert confirmation_snapshot is not None
    assert content_snapshot is not None
    return (
        post_snapshot.ad_id == created_ad_id
        and confirmation_snapshot.ad_id == created_ad_id
        and content_snapshot.ad_id == created_ad_id
        and post_snapshot.title == request.title
        and confirmation_snapshot.title == request.title
        and content_snapshot.title == request.title
        and content_snapshot.description == request.description
    )


def _receipt_status(
    receipt: OperationReceipt | CreateOperationReceipt,
) -> int:
    if receipt.outcome is OperationOutcome.CONFIRMED:
        return 200
    if receipt.outcome is OperationOutcome.PRECONDITION_FAILED:
        return 409
    return 202


def _handler_factory(
    *,
    service: _WriteService,
    media_service: _MediaWriteService | None,
    media_stager: _MediaStager | None,
    store: SnapshotStore,
    access: WriteApiAccess,
    clock: Callable[[], datetime],
):
    # Both create routes infer their result from owner-inventory deltas.
    # Serialize the complete service calls across this threaded server so a
    # media and media-free create cannot share/contaminate the same delta
    # window. Other write routes remain independent.
    create_lock = Lock()
    # A staging request may buffer up to _MAX_MEDIA_BODY_BYTES. Serialize
    # staging reads so ThreadingHTTPServer cannot multiply that bound by the
    # number of concurrent authenticated clients.
    media_staging_lock = Lock()

    class WriteApiHandler(BaseHTTPRequestHandler):
        server_version = "mark-api-write/0.1"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_bytes(
            self,
            status: int,
            body: bytes,
            *,
            allow: str | None = None,
            authenticate: bool = False,
            replayed: bool = False,
        ) -> None:
            self.send_response(status)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8",
            )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if allow is not None:
                self.send_header("Allow", allow)
            if authenticate:
                self.send_header("WWW-Authenticate", "Bearer")
            if replayed:
                self.send_header("Idempotency-Replayed", "true")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_json(
            self,
            status: int,
            value: object,
            *,
            allow: str | None = None,
            authenticate: bool = False,
            replayed: bool = False,
        ) -> None:
            self._send_bytes(
                status,
                _canonical_json(value).encode("utf-8"),
                allow=allow,
                authenticate=authenticate,
                replayed=replayed,
            )

        def _error(
            self,
            status: int,
            code: str,
            *,
            allow: str | None = None,
            authenticate: bool = False,
            platform_retry_authorized: bool | None = None,
        ) -> None:
            payload: dict[str, object] = {"error": code}
            if platform_retry_authorized is not None:
                payload["platform_retry_authorized"] = (
                    platform_retry_authorized
                )
            self._send_json(
                status,
                payload,
                allow=allow,
                authenticate=authenticate,
            )

        def _authorized(self) -> bool:
            values = self.headers.get_all("Authorization") or []
            if len(values) != 1:
                return False
            expected = f"Bearer {access.bearer_token}"
            return hmac.compare_digest(values[0], expected)

        def _idempotency_key(self) -> str | None:
            values = self.headers.get_all("Idempotency-Key") or []
            if len(values) != 1:
                return None
            value = values[0]
            if _IDEMPOTENCY_KEY_RE.fullmatch(value) is None:
                return None
            return value

        def _content_length(
            self,
            *,
            max_bytes: int = _MAX_BODY_BYTES,
        ) -> int | None:
            if self.headers.get("Transfer-Encoding") is not None:
                raise ValueError("transfer_encoding_not_allowed")
            values = self.headers.get_all("Content-Length") or []
            if not values:
                return None
            if len(values) != 1:
                raise ValueError("invalid_content_length")
            try:
                value = int(values[0], 10)
            except ValueError as exc:
                raise ValueError("invalid_content_length") from exc
            if value < 0 or value > max_bytes:
                raise ValueError("invalid_content_length")
            return value

        def _read_json_object(self) -> dict[str, object]:
            content_type = self.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise ValueError("content_type_must_be_json")
            length = self._content_length()
            if length is None or length == 0:
                raise ValueError("json_body_required")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete_request_body")
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid_json") from exc
            if not isinstance(value, dict):
                raise ValueError("json_body_must_be_object")
            return value

        def _read_media_upload(self) -> tuple[str, bytes]:
            filename_values = self.headers.get_all("X-Mark-Media-Filename") or []
            if len(filename_values) != 1:
                raise ValueError("invalid_media_filename")
            filename = filename_values[0]
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                raise ValueError("unsupported_media_type")
            length = self._content_length(max_bytes=_MAX_MEDIA_BODY_BYTES)
            if length is None or length == 0:
                raise ValueError("media_body_required")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete_request_body")
            return filename, raw

        def _require_empty_body(self) -> None:
            length = self._content_length()
            if length not in (None, 0):
                self.rfile.read(length)
                raise ValueError("request_body_not_allowed")

        @staticmethod
        def _route(path: str, method: str):
            target = urlsplit(path)
            if target.query:
                return ("invalid_query", None, None)
            parts = [
                unquote(item)
                for item in target.path.split("/")
                if item
            ]
            if parts == ["api", "write", "ads"]:
                if method == "POST":
                    return ("create", None, WriteCapability.CREATE)
                return ("method_not_allowed", "POST", None)

            if parts == ["api", "write", "media", "stage"]:
                if method == "POST":
                    return (
                        "stage_media",
                        None,
                        WriteCapability.CREATE_MEDIA,
                    )
                return ("method_not_allowed", "POST", None)

            if parts == ["api", "write", "media", "ads"]:
                if method == "POST":
                    return (
                        "create_media",
                        None,
                        WriteCapability.CREATE_MEDIA,
                    )
                return ("method_not_allowed", "POST", None)

            if len(parts) < 4 or parts[:3] != ["api", "write", "ads"]:
                return None

            ad_id = parts[3]
            if not _valid_ad_id(ad_id):
                return ("invalid_ad_id", None, None)

            if len(parts) == 4:
                if method == "PATCH":
                    return (
                        "update_content",
                        ad_id,
                        WriteCapability.UPDATE_CONTENT,
                    )
                if method == "DELETE":
                    return ("delete", ad_id, WriteCapability.DELETE)
                return ("method_not_allowed", "DELETE, PATCH", None)

            if len(parts) == 5 and parts[4] in {"pause", "activate"}:
                if method == "POST":
                    return (
                        parts[4],
                        ad_id,
                        WriteCapability.SET_STATE,
                    )
                return ("method_not_allowed", "POST", None)

            return None

        def _dispatch_write(self, method: str) -> None:
            route = self._route(self.path, method)
            if route is None:
                self._error(404, "not_found")
                return
            action, ad_id, capability = route
            if action == "invalid_query":
                self._error(400, "query_not_allowed")
                return
            if action == "invalid_ad_id":
                self._error(400, "invalid_ad_id")
                return
            if action == "method_not_allowed":
                assert isinstance(ad_id, str)
                self._error(
                    405,
                    "method_not_allowed",
                    allow=ad_id,
                )
                return

            if not self._authorized():
                self._error(
                    401,
                    "unauthorized",
                    authenticate=True,
                )
                return
            assert isinstance(capability, WriteCapability)
            if capability not in access.capabilities:
                self._error(
                    403,
                    "capability_denied",
                    platform_retry_authorized=False,
                )
                return

            if action == "stage_media":
                # Staging is a local-only product operation. Authentication
                # and CREATE_MEDIA capability still apply, but the platform
                # write gate is enforced only when the staged handle is later
                # used for the actual media Create.
                if media_stager is None:
                    self._error(503, "media_staging_unavailable")
                    return
                try:
                    with media_staging_lock:
                        filename, raw_media = self._read_media_upload()
                        media_ref = media_stager.stage_media(filename, raw_media)
                except ValueError as exc:
                    self._error(400, str(exc))
                    return
                except Exception:
                    self._error(500, "media_staging_error")
                    return
                self._send_json(201, {"media_ref": media_ref})
                return

            if not access.writes_enabled:
                self._error(
                    403,
                    "writes_disabled",
                    platform_retry_authorized=False,
                )
                return

            idempotency_key = self._idempotency_key()
            if idempotency_key is None:
                self._error(400, "invalid_or_missing_idempotency_key")
                return

            create_request: AdCreateRequest | None = None
            media_refs: tuple[str, ...] | None = None
            try:
                if action in {"create", "create_media"}:
                    raw_payload = self._read_json_object()
                    create_request, media_refs, payload = (
                        _normalize_create_payload(
                            raw_payload,
                            with_media=action == "create_media",
                        )
                    )
                elif action == "update_content":
                    payload = self._read_json_object()
                    if not payload or set(payload) - {"title", "description"}:
                        raise ValueError("invalid_content_fields")
                    if "title" in payload and not isinstance(
                        payload["title"],
                        str,
                    ):
                        raise ValueError("title_must_be_string")
                    if "description" in payload and not isinstance(
                        payload["description"],
                        str,
                    ):
                        raise ValueError("description_must_be_string")
                elif action == "delete":
                    # The exact URL target plus this authenticated user action
                    # is the approval. Audit/idempotency identity is generated
                    # internally; callers do not manufacture a second token.
                    self._require_empty_body()
                    payload = {}
                else:
                    self._require_empty_body()
                    payload = {}
            except ValueError as exc:
                self._error(400, str(exc))
                return

            if action not in {"create", "create_media"}:
                assert isinstance(ad_id, str)
            try:
                fingerprint = _request_fingerprint(
                    principal=access.principal,
                    method=method,
                    path=urlsplit(self.path).path,
                    payload=payload,
                )
            except ValueError:
                self._error(
                    400,
                    "invalid_unicode_text",
                    platform_retry_authorized=False,
                )
                return
            try:
                claim = store.claim_write_api_request(
                    idempotency_key=idempotency_key,
                    request_sha256=fingerprint,
                    requested_at=clock(),
                )
            except Exception:
                self._error(
                    500,
                    "idempotency_store_error",
                    platform_retry_authorized=False,
                )
                return

            if not claim.created:
                record = claim.record
                if record.request_sha256 != fingerprint:
                    self._error(
                        409,
                        "idempotency_conflict",
                        platform_retry_authorized=False,
                    )
                    return
                if record.state == "in_progress":
                    self._error(
                        409,
                        "idempotency_in_progress",
                        platform_retry_authorized=False,
                    )
                    return
                if (
                    record.state == "completed"
                    and record.response_status is not None
                    and record.response_json is not None
                ):
                    self._send_bytes(
                        record.response_status,
                        record.response_json.encode("utf-8"),
                        replayed=True,
                    )
                    return
                self._error(
                    500,
                    "invalid_idempotency_record",
                    platform_retry_authorized=False,
                )
                return

            try:
                authorization_reference = f"write-api:{idempotency_key}"
                if action in {"create", "create_media"}:
                    assert create_request is not None
                    with create_lock:
                        if action == "create":
                            create_receipt = service.create(
                                create_request,
                                authorization_by=access.principal,
                                authorization_reference=authorization_reference,
                            )
                        else:
                            assert media_service is not None
                            assert media_refs is not None
                            create_receipt = media_service.create_with_media(
                                create_request,
                                media_refs,
                                authorization_by=access.principal,
                                authorization_reference=authorization_reference,
                            )
                    if not isinstance(
                        create_receipt,
                        CreateOperationReceipt,
                    ):
                        raise TypeError(
                            "write service returned invalid create receipt"
                        )
                    if not _create_receipt_matches_request(
                        create_receipt,
                        create_request,
                        principal=access.principal,
                        authorization_reference=authorization_reference,
                    ):
                        raise TypeError(
                            "write service returned misbound create receipt"
                        )
                    status = _receipt_status(create_receipt)
                    response = {
                        "idempotency_key": idempotency_key,
                        "operation_receipt": _create_receipt_to_dict(
                            create_receipt
                        ),
                        "platform_retry_authorized": False,
                    }
                    if action == "create_media":
                        # Content confirmation remains distinct from exact
                        # server-side media persistence. A media create is HTTP
                        # 200 only when both are confirmed; otherwise a
                        # content-confirmed/media-unconfirmed result remains 202.
                        if (
                            status == 200
                            and not create_receipt.media_persistence_confirmed
                        ):
                            status = 202
                        response["media_persistence_confirmed"] = (
                            create_receipt.media_persistence_confirmed
                        )
                        response["media_post_read_status"] = (
                            create_receipt.media_post_read_status.value
                            if create_receipt.media_post_read_status is not None
                            else None
                        )
                    receipt = None
                elif action == "update_content":
                    receipt = service.update_content(
                        ad_id,
                        title=(
                            payload["title"]
                            if "title" in payload
                            else None
                        ),
                        description=(
                            payload["description"]
                            if "description" in payload
                            else None
                        ),
                        authorization_by=access.principal,
                        authorization_reference=(
                            f"write-api:{idempotency_key}"
                        ),
                    )
                elif action == "pause":
                    receipt = service.pause(
                        ad_id,
                        authorization_by=access.principal,
                        authorization_reference=(
                            f"write-api:{idempotency_key}"
                        ),
                    )
                elif action == "activate":
                    receipt = service.activate(
                        ad_id,
                        authorization_by=access.principal,
                        authorization_reference=(
                            f"write-api:{idempotency_key}"
                        ),
                    )
                else:
                    receipt = service.delete(
                        ad_id,
                        approval=DeleteApproval(
                            ad_id=ad_id,
                            approved_by=access.principal,
                            reference=authorization_reference,
                        ),
                    )

                if action not in {"create", "create_media"}:
                    if not isinstance(receipt, OperationReceipt):
                        raise TypeError("write service returned invalid receipt")
                    expected_operation = {
                        "update_content": "update_content",
                        "pause": "set_state:paused",
                        "activate": "set_state:active",
                        "delete": "delete",
                    }[action]
                    expected_authorization_reference = authorization_reference
                    if (
                        receipt.ad_id != ad_id
                        or receipt.operation != expected_operation
                        or receipt.authorization_by != access.principal
                        or receipt.authorization_reference
                        != expected_authorization_reference
                    ):
                        raise TypeError(
                            "write service returned misbound receipt"
                        )
                    status = _receipt_status(receipt)
                    response = {
                        "idempotency_key": idempotency_key,
                        "operation_receipt": _receipt_to_dict(receipt),
                        "platform_retry_authorized": False,
                    }
            except Exception:
                status = 500
                response = {
                    "error": "write_execution_error",
                    "idempotency_key": idempotency_key,
                    "platform_retry_authorized": False,
                }
                if action == "create_media":
                    response["media_persistence_confirmed"] = False

            response_json = _canonical_json(response)
            try:
                store.complete_write_api_request(
                    idempotency_key=idempotency_key,
                    request_sha256=fingerprint,
                    response_status=status,
                    response_json=response_json,
                    completed_at=clock(),
                )
            except Exception:
                self._error(
                    500,
                    "idempotency_persistence_failed",
                    platform_retry_authorized=False,
                )
                return

            self._send_bytes(status, response_json.encode("utf-8"))

        def do_GET(self) -> None:
            target = urlsplit(self.path)
            if target.path == "/healthz" and not target.query:
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "writes_enabled": access.writes_enabled,
                    },
                )
                return
            route = self._route(self.path, "GET")
            if route is not None and route[0] == "method_not_allowed":
                self._error(
                    405,
                    "method_not_allowed",
                    allow=str(route[1]),
                )
                return
            self._error(404, "not_found")

        def do_PATCH(self) -> None:
            self._dispatch_write("PATCH")

        def do_POST(self) -> None:
            self._dispatch_write("POST")

        def do_DELETE(self) -> None:
            self._dispatch_write("DELETE")

        def do_PUT(self) -> None:
            self._dispatch_write("PUT")

        def do_OPTIONS(self) -> None:
            self._dispatch_write("OPTIONS")

        def do_HEAD(self) -> None:
            self._dispatch_write("HEAD")

    return WriteApiHandler


class LoopbackWriteApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_write_api_server(
    service: _WriteService,
    store: SnapshotStore,
    access: WriteApiAccess,
    *,
    media_service: _MediaWriteService | None = None,
    media_stager: _MediaStager | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    clock: Callable[[], datetime] = _utc_now,
) -> LoopbackWriteApiServer:
    if host != "127.0.0.1":
        raise ValueError("write API must bind to 127.0.0.1")
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 0 <= port <= 65535
    ):
        raise ValueError("port must be an integer between 0 and 65535")
    if not isinstance(access, WriteApiAccess):
        raise TypeError("access must be WriteApiAccess")
    if (
        WriteCapability.CREATE_MEDIA in access.capabilities
        and media_service is None
    ):
        raise ValueError(
            "create_media capability requires media_service"
        )

    return LoopbackWriteApiServer(
        (host, port),
        _handler_factory(
            service=service,
            media_service=media_service,
            media_stager=media_stager,
            store=store,
            access=access,
            clock=clock,
        ),
    )
