from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from pathlib import Path

from mark_api.domain import InboundMessageEvent
from mark_api.email_import import (
    import_kleinanzeigen_email_files,
    parse_kleinanzeigen_email,
)
from mark_api.email_import_cli import main
from mark_api.storage import SnapshotStore


T0 = datetime(2026, 9, 24, 10, 37, 48, tzinfo=timezone.utc)


def notification(
    *,
    ad_id: str = "1234567890",
    conversation_id: str = "abc12:def34:ghi56",
    provider_message_id: str = "11111111-2222-3333-4444-555555555555",
    sender: str = "Kleinanzeigen <noreply@mail.kleinanzeigen.de>",
    body_conversation_id: str | None = None,
    body_ad_ids: tuple[str, ...] | None = None,
    standard_message_id: str | None = None,
    date: str = "Thu, 24 Sep 2026 10:37:48 +0000",
) -> bytes:
    message = EmailMessage(policy=policy.default)
    message["From"] = sender
    message["To"] = "owner@example.invalid"
    message["Date"] = date
    message["Message-ID"] = standard_message_id or (
        f"<{provider_message_id}@chat.kleinanzeigen.de>"
    )
    message["X-Conversation-ID"] = conversation_id
    message["X-Message-ID"] = provider_message_id

    body_conversation = body_conversation_id or conversation_id
    ids = body_ad_ids or (ad_id,)
    id_text = "\n".join(f"(Anzeigennummer: {item})" for item in ids)
    reply_url = (
        "https://www.kleinanzeigen.de/m-nachrichten.html?"
        f"conversationId={body_conversation}"
    )
    message.set_content(
        "Anfrage zu deiner Anzeige\n"
        + id_text
        + "\nUm auf diese Nachricht zu antworten: "
        + reply_url
    )
    message.add_alternative(
        "<html><body>"
        + "".join(f"<p>Anzeigennummer: {item}</p>" for item in ids)
        + f'<a href="{reply_url}">Antworten</a>'
        + "</body></html>",
        subtype="html",
    )
    return message.as_bytes(policy=policy.default)


class KleinanzeigenEmailImportTests(unittest.TestCase):
    def make_store(self) -> tuple[tempfile.TemporaryDirectory[str], SnapshotStore]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp, SnapshotStore(Path(tmp.name) / "mark.sqlite")

    def test_parser_extracts_only_minimal_identifiers(self) -> None:
        event = parse_kleinanzeigen_email(notification())

        self.assertEqual(event.ad_id, "1234567890")
        self.assertEqual(event.conversation_id, "abc12:def34:ghi56")
        self.assertEqual(
            event.provider_message_id,
            "11111111-2222-3333-4444-555555555555",
        )
        self.assertEqual(event.observed_at, T0)
        self.assertEqual(event.source, "kleinanzeigen-email")
        self.assertEqual(
            tuple(event.__dataclass_fields__),
            (
                "ad_id",
                "conversation_id",
                "provider_message_id",
                "observed_at",
                "source",
            ),
        )

    def test_parser_rejects_non_kleinanzeigen_sender(self) -> None:
        with self.assertRaisesRegex(ValueError, "sender"):
            parse_kleinanzeigen_email(
                notification(sender="Other <noreply@example.invalid>")
            )

    def test_parser_rejects_conversation_header_body_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "conversation id header and body"):
            parse_kleinanzeigen_email(
                notification(body_conversation_id="other:conversation:id")
            )

    def test_parser_rejects_conflicting_ad_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one ad id"):
            parse_kleinanzeigen_email(
                notification(body_ad_ids=("1234567890", "9876543210"))
            )

    def test_parser_rejects_message_id_header_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "message id headers disagree"):
            parse_kleinanzeigen_email(
                notification(
                    standard_message_id=(
                        "<aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
                        "@chat.kleinanzeigen.de>"
                    )
                )
            )

    def test_parser_rejects_message_id_case_mismatch(self) -> None:
        provider_id = "AbCd1111-2222-3333-4444-555555555555"
        with self.assertRaisesRegex(ValueError, "message id headers disagree"):
            parse_kleinanzeigen_email(
                notification(
                    provider_message_id=provider_id,
                    standard_message_id=(
                        "<abcd1111-2222-3333-4444-555555555555"
                        "@chat.kleinanzeigen.de>"
                    ),
                )
            )

    def test_case_distinct_provider_ids_remain_distinct_events(self) -> None:
        _, store = self.make_store()
        upper = "AbCd1111-2222-3333-4444-555555555555"
        lower = "abcd1111-2222-3333-4444-555555555555"
        first = parse_kleinanzeigen_email(
            notification(provider_message_id=upper)
        )
        second = parse_kleinanzeigen_email(
            notification(
                provider_message_id=lower,
                date="Thu, 24 Sep 2026 10:37:49 +0000",
            )
        )

        self.assertEqual(first.provider_message_id, upper)
        self.assertEqual(second.provider_message_id, lower)
        self.assertEqual(
            store.append_inbound_message_events((first, second)),
            2,
        )
        self.assertEqual(
            tuple(
                item.provider_message_id
                for item in store.inbound_message_history(first.ad_id)
            ),
            (upper, lower),
        )

    def test_parser_rejects_naive_date(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            parse_kleinanzeigen_email(
                notification(date="Thu, 24 Sep 2026 10:37:48")
            )

    def test_exact_duplicates_are_idempotent(self) -> None:
        _, store = self.make_store()
        event = parse_kleinanzeigen_email(notification())

        self.assertEqual(store.append_inbound_message_events((event,)), 1)
        self.assertEqual(store.append_inbound_message_events((event,)), 0)
        self.assertEqual(store.inbound_message_history(event.ad_id), (event,))

    def test_provider_id_conflict_rolls_back_whole_batch(self) -> None:
        _, store = self.make_store()
        existing = parse_kleinanzeigen_email(notification(ad_id="200002"))
        store.append_inbound_message_events((existing,))

        new_event = parse_kleinanzeigen_email(
            notification(
                ad_id="100001",
                provider_message_id="22222222-2222-3333-4444-555555555555",
            )
        )
        conflict = InboundMessageEvent(
            ad_id="300003",
            conversation_id=existing.conversation_id,
            provider_message_id=existing.provider_message_id,
            observed_at=existing.observed_at,
            source=existing.source,
        )

        with self.assertRaisesRegex(ValueError, "provider_message_id conflict"):
            store.append_inbound_message_events((new_event, conflict))

        self.assertEqual(store.inbound_message_history("100001"), ())
        self.assertEqual(store.inbound_message_history("200002"), (existing,))

    def test_counts_distinguish_messages_from_conversations(self) -> None:
        _, store = self.make_store()
        events = (
            parse_kleinanzeigen_email(
                notification(
                    provider_message_id="11111111-1111-1111-1111-111111111111",
                )
            ),
            parse_kleinanzeigen_email(
                notification(
                    provider_message_id="22222222-2222-2222-2222-222222222222",
                )
            ),
            parse_kleinanzeigen_email(
                notification(
                    conversation_id="xyz12:xyz34:xyz56",
                    body_conversation_id="xyz12:xyz34:xyz56",
                    provider_message_id="33333333-3333-3333-3333-333333333333",
                )
            ),
        )

        self.assertEqual(store.append_inbound_message_events(events), 3)
        self.assertEqual(
            store.inbound_message_counts("1234567890"),
            (2, 3),
        )

    def test_storage_schema_contains_no_message_body_or_person_name(self) -> None:
        _, store = self.make_store()
        with sqlite3.connect(store.path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(inbound_message_events)"
                ).fetchall()
            }

        self.assertEqual(
            columns,
            {
                "id",
                "provider_message_id",
                "ad_id",
                "conversation_id",
                "observed_at",
                "source",
            },
        )

    def test_file_import_parses_every_file_before_database_write(self) -> None:
        tmp, store = self.make_store()
        valid = Path(tmp.name) / "valid.eml"
        invalid = Path(tmp.name) / "invalid.eml"
        valid.write_bytes(notification())
        invalid.write_bytes(
            notification(sender="Other <noreply@example.invalid>")
        )

        with self.assertRaisesRegex(ValueError, "sender"):
            import_kleinanzeigen_email_files(store, (valid, invalid))

        self.assertEqual(store.inbound_message_history("1234567890"), ())

    def test_mail_cli_requires_explicit_new_database_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "missing" / "mark.sqlite"
            mail = Path(tmp) / "reaction.eml"
            mail.write_bytes(notification())
            errors = io.StringIO()
            with redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
                main(["--db", str(db), str(mail)])
            self.assertEqual(caught.exception.code, 2)
            self.assertIn("SQLite database unavailable", errors.getvalue())
            self.assertNotIn(str(db), errors.getvalue())
            self.assertFalse(db.parent.exists())

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = main(["--db", str(db), "--init-db", str(mail)])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(stdout.getvalue())["inserted_events"], 1)
            self.assertTrue(SnapshotStore(db, create_if_missing=False).is_ready())

    def test_cli_outputs_only_safe_import_summary(self) -> None:
        tmp, store = self.make_store()
        first = Path(tmp.name) / "first.eml"
        second = Path(tmp.name) / "second.eml"
        first.write_bytes(notification())
        second.write_bytes(
            notification(
                provider_message_id="22222222-2222-2222-2222-222222222222",
            )
        )
        output = io.StringIO()

        with redirect_stdout(output):
            result = main(
                [
                    "--db",
                    str(store.path),
                    str(first),
                    str(second),
                ]
            )

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(
            payload,
            {
                "ad_ids": ["1234567890"],
                "duplicate_events": 0,
                "inserted_events": 2,
                "parsed_files": 2,
            },
        )
        self.assertEqual(len(store.inbound_message_history("1234567890")), 2)


if __name__ == "__main__":
    unittest.main()
