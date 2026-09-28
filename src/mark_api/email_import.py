from __future__ import annotations

import re
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Iterable

from .domain import InboundMessageEvent
from .storage import SnapshotStore


_SOURCE = "kleinanzeigen-email"
_MAX_EMAIL_BYTES = 30 * 1024 * 1024
_AD_ID_RE = re.compile(r"Anzeigennummer:\s*([0-9]{5,20})", re.IGNORECASE)
_CONVERSATION_BODY_RE = re.compile(
    r"conversationId=([A-Za-z0-9][A-Za-z0-9:_-]{2,255})",
    re.IGNORECASE,
)
_CONVERSATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_-]{2,255}\Z")
_PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,255}\Z")
_CHAT_MESSAGE_ID_RE = re.compile(
    r"<([^<>@\s]+)@chat\.kleinanzeigen\.de>\Z",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class EmailImportReport:
    parsed_files: int
    inserted_events: int
    duplicate_events: int
    ad_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "parsed_files": self.parsed_files,
            "inserted_events": self.inserted_events,
            "duplicate_events": self.duplicate_events,
            "ad_ids": list(self.ad_ids),
        }


def _single_header(message: Message, name: str) -> str:
    values = tuple(
        str(value).strip()
        for value in message.get_all(name, failobj=[])
        if str(value).strip()
    )
    if len(values) != 1:
        raise ValueError(f"email must contain exactly one {name} header")
    return values[0]


def _validate_sender(message: Message) -> None:
    _, address = parseaddr(_single_header(message, "From"))
    normalized = address.strip().casefold()
    if normalized.count("@") != 1:
        raise ValueError("email sender is not a valid Kleinanzeigen notification address")
    local_part, domain = normalized.rsplit("@", 1)
    if (
        local_part != "noreply"
        or not (
            domain == "kleinanzeigen.de"
            or domain.endswith(".kleinanzeigen.de")
        )
    ):
        raise ValueError("email sender is not a Kleinanzeigen notification address")


def _decoded_text(message: Message) -> str:
    parts: list[str] = []
    candidates = message.walk() if message.is_multipart() else (message,)
    for part in candidates:
        if part.get_content_maintype() != "text":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError) as exc:
            raise ValueError("email text payload cannot be decoded") from exc
        if isinstance(content, str) and content:
            parts.append(content)
    if not parts:
        raise ValueError("email contains no decodable text payload")
    return "\n".join(parts)


def _provider_message_id(message: Message) -> str:
    provider_id = _single_header(message, "X-Message-ID")
    if _PROVIDER_ID_RE.fullmatch(provider_id) is None:
        raise ValueError("email X-Message-ID is malformed")

    standard_id = _single_header(message, "Message-ID")
    match = _CHAT_MESSAGE_ID_RE.fullmatch(standard_id)
    if match is None:
        raise ValueError("email Message-ID is not a Kleinanzeigen chat message id")

    standard_provider_id = match.group(1)
    if provider_id != standard_provider_id:
        raise ValueError("email message id headers disagree")
    return provider_id


def _conversation_id(message: Message, decoded_text: str) -> str:
    header_id = _single_header(message, "X-Conversation-ID")
    if _CONVERSATION_ID_RE.fullmatch(header_id) is None:
        raise ValueError("email X-Conversation-ID is malformed")

    body_ids = set(_CONVERSATION_BODY_RE.findall(decoded_text))
    if len(body_ids) != 1:
        raise ValueError("email body must contain exactly one conversation id")
    body_id = next(iter(body_ids))
    if header_id != body_id:
        raise ValueError("email conversation id header and body disagree")
    return header_id


def _ad_id(decoded_text: str) -> str:
    ad_ids = set(_AD_ID_RE.findall(decoded_text))
    if len(ad_ids) != 1:
        raise ValueError("email body must contain exactly one ad id")
    return next(iter(ad_ids))


def _observed_at(message: Message):
    raw_date = _single_header(message, "Date")
    try:
        observed_at = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("email Date header is invalid") from exc
    if (
        observed_at is None
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise ValueError("email Date header must contain a timezone")
    return observed_at


def parse_kleinanzeigen_email(
    raw: bytes,
    *,
    source: str = _SOURCE,
) -> InboundMessageEvent:
    """Parse one raw Kleinanzeigen inbound-message notification fail-closed.

    The returned event intentionally excludes message text and sender/person names.
    Header/body checks validate notification-format consistency only; they do
    not establish cryptographic sender authenticity.
    """

    if not isinstance(raw, bytes):
        raise TypeError("raw email must be bytes")
    if not raw:
        raise ValueError("raw email must not be empty")
    if len(raw) > _MAX_EMAIL_BYTES:
        raise ValueError("raw email exceeds the supported size limit")

    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:
        raise ValueError("raw email cannot be parsed") from exc

    _validate_sender(message)
    decoded_text = _decoded_text(message)
    return InboundMessageEvent(
        ad_id=_ad_id(decoded_text),
        conversation_id=_conversation_id(message, decoded_text),
        provider_message_id=_provider_message_id(message),
        observed_at=_observed_at(message),
        source=source,
    )


def import_kleinanzeigen_email_files(
    store: SnapshotStore,
    paths: Iterable[str | Path],
) -> EmailImportReport:
    """Parse all local RFC822 files first, then atomically append their events."""

    normalized_paths = tuple(Path(path) for path in paths)
    if not normalized_paths:
        raise ValueError("at least one email file is required")

    events: list[InboundMessageEvent] = []
    for path in normalized_paths:
        try:
            stat = path.stat()
        except OSError as exc:
            raise ValueError("email file is not readable") from exc
        if not path.is_file():
            raise ValueError("email input must be a regular file")
        if stat.st_size > _MAX_EMAIL_BYTES:
            raise ValueError("email file exceeds the supported size limit")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError("email file is not readable") from exc
        events.append(parse_kleinanzeigen_email(raw))

    inserted = store.append_inbound_message_events(events)
    return EmailImportReport(
        parsed_files=len(events),
        inserted_events=inserted,
        duplicate_events=len(events) - inserted,
        ad_ids=tuple(sorted({event.ad_id for event in events})),
    )
