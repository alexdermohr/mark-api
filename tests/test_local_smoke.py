from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from email import policy
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

import mark_api.local_smoke as local_smoke
from mark_api.local_smoke import main, run_local_smoke


def notification(
    *,
    ad_id: str,
    conversation_id: str,
    provider_message_id: str,
    date: str,
    sender: str = "Kleinanzeigen <noreply@mail.kleinanzeigen.de>",
) -> bytes:
    message = EmailMessage(policy=policy.default)
    message["From"] = sender
    message["To"] = "owner@example.invalid"
    message["Date"] = date
    message["Message-ID"] = (
        f"<{provider_message_id}@chat.kleinanzeigen.de>"
    )
    message["X-Conversation-ID"] = conversation_id
    message["X-Message-ID"] = provider_message_id
    message.set_content(
        "Anfrage zu deiner Anzeige\n"
        f"(Anzeigennummer: {ad_id})\n"
        "Um auf diese Nachricht zu antworten: "
        "https://www.kleinanzeigen.de/m-nachrichten.html?"
        f"conversationId={conversation_id}"
    )
    return message.as_bytes(policy=policy.default)


class LocalSmokeTests(unittest.TestCase):
    def make_email(
        self,
        directory: Path,
        name: str,
        **kwargs: str,
    ) -> Path:
        path = directory / name
        path.write_bytes(notification(**kwargs))
        return path

    def test_local_smoke_exercises_import_sqlite_dashboard_and_analytics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = (
                self.make_email(
                    root,
                    "one.eml",
                    ad_id="1234567890",
                    conversation_id="conv-a:thread:one",
                    provider_message_id="message-a",
                    date="Thu, 24 Sep 2026 10:37:48 +0000",
                ),
                self.make_email(
                    root,
                    "two.eml",
                    ad_id="1234567890",
                    conversation_id="conv-a:thread:one",
                    provider_message_id="message-b",
                    date="Thu, 24 Sep 2026 10:38:48 +0000",
                ),
                self.make_email(
                    root,
                    "three.eml",
                    ad_id="9876543210",
                    conversation_id="conv-b:thread:two",
                    provider_message_id="message-c",
                    date="Thu, 24 Sep 2026 10:39:48 +0000",
                ),
            )

            report = run_local_smoke(paths)

        self.assertEqual(report.parsed_files, 3)
        self.assertEqual(report.inserted_events, 3)
        self.assertEqual(report.duplicate_events, 0)
        self.assertEqual(report.ad_ids, ("1234567890", "9876543210"))
        self.assertTrue(report.dashboard_health_ok)
        self.assertEqual(report.email_reaction_ads, 2)
        self.assertEqual(report.email_conversation_total, 2)
        self.assertEqual(report.email_inbound_message_total, 3)
        self.assertEqual(report.analytics_ranked_ads, 2)
        self.assertTrue(report.http_write_methods_rejected)
        self.assertTrue(report.write_route_absent)
        self.assertFalse(report.platform_writes_enabled)

    def test_cli_outputs_safe_json_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            email = self.make_email(
                root,
                "mail.eml",
                ad_id="1234567890",
                conversation_id="conv-a:thread:one",
                provider_message_id="message-a",
                date="Thu, 24 Sep 2026 10:37:48 +0000",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([str(email)]), 0)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["parsed_files"], 1)
        self.assertEqual(payload["email_inbound_message_total"], 1)
        self.assertFalse(payload["platform_writes_enabled"])
        encoded = output.getvalue().lower()
        self.assertNotIn("anfrage zu deiner anzeige", encoded)
        self.assertNotIn("owner@example.invalid", encoded)

    def test_swapped_per_ad_analytics_values_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = (
                self.make_email(
                    root,
                    "one.eml",
                    ad_id="1234567890",
                    conversation_id="conv-a:thread:one",
                    provider_message_id="message-a",
                    date="Thu, 24 Sep 2026 10:37:48 +0000",
                ),
                self.make_email(
                    root,
                    "two.eml",
                    ad_id="1234567890",
                    conversation_id="conv-a:thread:one",
                    provider_message_id="message-b",
                    date="Thu, 24 Sep 2026 10:38:48 +0000",
                ),
                self.make_email(
                    root,
                    "three.eml",
                    ad_id="9876543210",
                    conversation_id="conv-b:thread:two",
                    provider_message_id="message-c",
                    date="Thu, 24 Sep 2026 10:39:48 +0000",
                ),
            )
            original_json_get = local_smoke._json_get

            def swapped_ranking(opener, base: str, path: str):
                payload = original_json_get(opener, base, path)
                if path.endswith(
                    "metric=email_inbound_message_count"
                ):
                    rows = [dict(item) for item in payload]
                    rows[0]["value"], rows[1]["value"] = (
                        rows[1]["value"],
                        rows[0]["value"],
                    )
                    return rows
                return payload

            with patch(
                "mark_api.local_smoke._json_get",
                side_effect=swapped_ranking,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "per-ad message counts",
                ):
                    run_local_smoke(paths)

    def test_importing_smoke_does_not_load_platform_adapters(self) -> None:
        code = (
            "import sys; import mark_api.local_smoke; "
            "loaded=sorted(name for name in sys.modules "
            "if name == 'mark_api.adapters' "
            "or name.startswith('mark_api.adapters.')); "
            "print(loaded); raise SystemExit(0 if not loaded else 1)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr,
        )
        self.assertEqual(result.stdout.strip(), "[]")

    def test_invalid_mail_fails_before_dashboard_server_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            email = self.make_email(
                root,
                "invalid.eml",
                ad_id="1234567890",
                conversation_id="conv-a:thread:one",
                provider_message_id="message-a",
                date="Thu, 24 Sep 2026 10:37:48 +0000",
                sender="Other <noreply@example.invalid>",
            )
            with patch(
                "mark_api.local_smoke.create_server",
                side_effect=AssertionError("dashboard must not start"),
            ):
                with self.assertRaisesRegex(ValueError, "sender"):
                    run_local_smoke((email,))


if __name__ == "__main__":
    unittest.main()
