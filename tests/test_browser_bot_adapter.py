from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mark_api.adapters.browser_bot import (
    BrowserBotAdapter,
    BrowserBotProcessError,
    BrowserBotRuntime,
    BrowserBotWorkspaceError,
    ProcessResult,
)


class FakeRunner:
    def __init__(self, *results: ProcessResult) -> None:
        self.results = list(results)
        self.calls = []

    def run(
        self,
        argv,
        *,
        cwd,
        input_text,
        timeout_seconds,
    ):
        self.calls.append(
            {
                "argv": tuple(argv),
                "cwd": Path(cwd),
                "input_text": input_text,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.results:
            raise AssertionError("unexpected process call")
        return self.results.pop(0)


class RaisingRunner:
    def run(
        self,
        argv,
        *,
        cwd,
        input_text,
        timeout_seconds,
    ):
        raise OSError("provider path details must not escape")


class BrowserBotAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bot_checkout = root / "bot"
        self.bot_checkout.mkdir()
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.config = self.workspace / "config.yaml"
        self.config.write_text("login: {}\n", encoding="utf-8")
        self.runtime = BrowserBotRuntime(
            python_executable=Path("/opt/test/python"),
            bot_checkout=self.bot_checkout,
            config_path=self.config,
            timeout_seconds=42.0,
        )

    def adapter(self, runner: FakeRunner) -> BrowserBotAdapter:
        return BrowserBotAdapter(self.runtime, runner=runner)

    def create_synced_yaml(
        self,
        ad_id: str = "3521676801",
        *,
        suffix: str = "Hirsch",
        content: str | None = None,
    ) -> Path:
        folder = (
            self.workspace
            / "downloaded-ads"
            / f"ad_{ad_id}_{suffix}"
        )
        folder.mkdir(parents=True)
        path = folder / f"ad_{ad_id}.yaml"
        path.write_text(
            content
            or (
                f"id: {ad_id}\n"
                "title: Old title\n"
                "description: Old description\n"
                "price_type: GIVE_AWAY\n"
            ),
            encoding="utf-8",
        )
        return path

    def test_runtime_rejects_workspace_escape_configuration(self) -> None:
        with self.assertRaises(ValueError):
            BrowserBotRuntime(
                python_executable=Path("/opt/test/python"),
                bot_checkout=self.bot_checkout,
                config_path=self.config,
                download_dir_name="../outside",
            )

    def test_runner_exception_is_sanitized(self) -> None:
        adapter = BrowserBotAdapter(self.runtime, runner=RaisingRunner())

        with self.assertRaises(BrowserBotProcessError) as raised:
            adapter.sync("3521676801")

        self.assertEqual(raised.exception.phase, "sync")
        self.assertEqual(raised.exception.returncode, 127)
        self.assertNotIn("provider", str(raised.exception))

    def test_sync_uses_exact_argv_once(self) -> None:
        runner = FakeRunner(ProcessResult(0))

        self.adapter(runner).sync("3521676801")

        self.assertEqual(len(runner.calls), 1)
        call = runner.calls[0]
        self.assertEqual(
            call["argv"],
            (
                "/opt/test/python",
                "-m",
                "kleinanzeigen_bot",
                "--lang=de",
                "--workspace-mode=portable",
                "--config",
                str(self.config),
                "download",
                "--ads=3521676801",
            ),
        )
        self.assertIsNone(call["input_text"])
        self.assertEqual(call["cwd"], self.bot_checkout)

    def test_invalid_ad_id_is_rejected_before_process(self) -> None:
        runner = FakeRunner()

        with self.assertRaises(ValueError):
            self.adapter(runner).sync("1; rm -rf /")

        self.assertEqual(runner.calls, [])

    def test_update_requires_a_content_field(self) -> None:
        runner = FakeRunner()
        self.create_synced_yaml()

        with self.assertRaises(ValueError):
            self.adapter(runner).update_content("3521676801")

        self.assertEqual(runner.calls, [])

    def test_update_requires_exactly_one_synced_yaml(self) -> None:
        runner = FakeRunner()
        adapter = self.adapter(runner)

        with self.assertRaises(BrowserBotWorkspaceError):
            adapter.update_content(
                "3521676801",
                description="new",
            )

        self.create_synced_yaml(suffix="one")
        self.create_synced_yaml(suffix="two")
        with self.assertRaises(BrowserBotWorkspaceError):
            adapter.update_content(
                "3521676801",
                description="new",
            )

        self.assertEqual(runner.calls, [])

    def test_content_is_passed_only_via_stdin_not_argv(self) -> None:
        runner = FakeRunner(ProcessResult(0), ProcessResult(0))
        self.create_synced_yaml()
        secretish = "new description bearer-like-content"

        self.adapter(runner).update_content(
            "3521676801",
            description=secretish,
        )

        self.assertEqual(len(runner.calls), 2)
        stage, update = runner.calls
        self.assertNotIn(secretish, " ".join(stage["argv"]))
        self.assertNotIn(secretish, " ".join(update["argv"]))
        self.assertEqual(
            json.loads(stage["input_text"]),
            {"description": secretish},
        )
        self.assertEqual(update["argv"][-2:], ("update", "--ads=3521676801"))

    def test_stage_failure_prevents_remote_update(self) -> None:
        runner = FakeRunner(ProcessResult(25))
        self.create_synced_yaml()

        with self.assertRaises(BrowserBotProcessError) as raised:
            self.adapter(runner).update_content(
                "3521676801",
                description="new",
            )

        self.assertEqual(raised.exception.phase, "stage")
        self.assertEqual(raised.exception.returncode, 25)
        self.assertEqual(len(runner.calls), 1)

    def test_update_failure_is_not_retried(self) -> None:
        runner = FakeRunner(ProcessResult(0), ProcessResult(7))
        self.create_synced_yaml()

        with self.assertRaises(BrowserBotProcessError) as raised:
            self.adapter(runner).update_content(
                "3521676801",
                title="new",
            )

        self.assertEqual(raised.exception.phase, "update")
        self.assertEqual(raised.exception.returncode, 7)
        self.assertEqual(len(runner.calls), 2)

    def test_timeout_is_sanitized(self) -> None:
        runner = FakeRunner(ProcessResult(124, timed_out=True))

        with self.assertRaises(BrowserBotProcessError) as raised:
            self.adapter(runner).sync("3521676801")

        self.assertTrue(raised.exception.timed_out)
        self.assertEqual(str(raised.exception), "browser bot sync failed with return code 124 (timeout)")


if __name__ == "__main__":
    unittest.main()