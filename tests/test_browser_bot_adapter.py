from __future__ import annotations

import json
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from mark_api.adapters.browser_bot import (
    BrowserBotAdapter,
    BrowserBotProcessError,
    BrowserBotRuntime,
    BrowserBotWorkspaceError,
    ProcessResult,
    SubprocessRunner,
)


class FakeRunner:
    def __init__(self, *results: ProcessResult, on_call=None) -> None:
        self.results = list(results)
        self.calls = []
        self.on_call = on_call

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
        if self.on_call is not None:
            self.on_call(len(self.calls), self.calls[-1])
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

    def test_subprocess_runner_discards_output_and_starts_new_session(self) -> None:
        process = Mock()
        process.pid = 4321
        process.returncode = 0
        process.communicate.return_value = (None, None)

        with patch(
            "mark_api.adapters.browser_bot.subprocess.Popen",
            return_value=process,
        ) as popen:
            result = SubprocessRunner().run(
                ("/opt/test/python", "-m", "kleinanzeigen_bot"),
                cwd=self.bot_checkout,
                input_text="payload",
                timeout_seconds=42.0,
            )

        self.assertEqual(result, ProcessResult(0))
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["stdin"], subprocess.PIPE)
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["start_new_session"])
        process.communicate.assert_called_once_with(
            input="payload",
            timeout=42.0,
        )

    def test_subprocess_runner_terminates_whole_process_group_on_timeout(self) -> None:
        process = Mock()
        process.pid = 4321
        process.communicate.side_effect = subprocess.TimeoutExpired(
            cmd=["browser-bot"],
            timeout=42.0,
        )
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd=["browser-bot"], timeout=5.0),
            0,
        ]

        with (
            patch(
                "mark_api.adapters.browser_bot.subprocess.Popen",
                return_value=process,
            ),
            patch("mark_api.adapters.browser_bot.os.killpg") as killpg,
        ):
            result = SubprocessRunner().run(
                ("/opt/test/python", "-m", "kleinanzeigen_bot"),
                cwd=self.bot_checkout,
                input_text=None,
                timeout_seconds=42.0,
            )

        self.assertEqual(result, ProcessResult(124, timed_out=True))
        self.assertEqual(
            killpg.call_args_list,
            [
                call(4321, signal.SIGTERM),
                call(4321, signal.SIGKILL),
            ],
        )
        process.wait.assert_has_calls([call(timeout=5.0), call()])

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

    def test_update_requires_exactly_one_yaml_after_fresh_sync(self) -> None:
        runner = FakeRunner(ProcessResult(0), ProcessResult(0))
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

        self.assertEqual(len(runner.calls), 2)
        for process_call in runner.calls:
            self.assertEqual(
                process_call["argv"][-2:],
                ("download", "--ads=3521676801"),
            )

    def test_remote_title_change_after_sync_fails_closed_before_stage(self) -> None:
        self.create_synced_yaml(suffix="old")

        def create_new_remote_folder(call_number, _call) -> None:
            if call_number == 1:
                self.create_synced_yaml(suffix="new")

        runner = FakeRunner(
            ProcessResult(0),
            on_call=create_new_remote_folder,
        )

        with self.assertRaises(BrowserBotWorkspaceError):
            self.adapter(runner).update_content(
                "3521676801",
                description="new",
            )

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(
            runner.calls[0]["argv"][-2:],
            ("download", "--ads=3521676801"),
        )

    def test_update_runs_fresh_sync_then_stage_then_remote_update(self) -> None:
        runner = FakeRunner(
            ProcessResult(0),
            ProcessResult(0),
            ProcessResult(0),
        )
        self.create_synced_yaml()
        secretish = "new description bearer-like-content"

        self.adapter(runner).update_content(
            "3521676801",
            description=secretish,
        )

        self.assertEqual(len(runner.calls), 3)
        sync, stage, update = runner.calls
        self.assertEqual(sync["argv"][-2:], ("download", "--ads=3521676801"))
        self.assertNotIn(secretish, " ".join(sync["argv"]))
        self.assertNotIn(secretish, " ".join(stage["argv"]))
        self.assertNotIn(secretish, " ".join(update["argv"]))
        self.assertEqual(
            json.loads(stage["input_text"]),
            {"description": secretish},
        )
        self.assertEqual(update["argv"][-2:], ("update", "--ads=3521676801"))

    def test_stage_failure_prevents_remote_update(self) -> None:
        runner = FakeRunner(ProcessResult(0), ProcessResult(25))
        self.create_synced_yaml()

        with self.assertRaises(BrowserBotProcessError) as raised:
            self.adapter(runner).update_content(
                "3521676801",
                description="new",
            )

        self.assertEqual(raised.exception.phase, "stage")
        self.assertEqual(raised.exception.returncode, 25)
        self.assertEqual(len(runner.calls), 2)

    def test_sync_failure_prevents_stage_and_remote_update(self) -> None:
        runner = FakeRunner(ProcessResult(7))
        self.create_synced_yaml()

        with self.assertRaises(BrowserBotProcessError) as raised:
            self.adapter(runner).update_content(
                "3521676801",
                description="new",
            )

        self.assertEqual(raised.exception.phase, "sync")
        self.assertEqual(raised.exception.returncode, 7)
        self.assertEqual(len(runner.calls), 1)

    def test_update_failure_is_not_retried(self) -> None:
        runner = FakeRunner(
            ProcessResult(0),
            ProcessResult(0),
            ProcessResult(7),
        )
        self.create_synced_yaml()

        with self.assertRaises(BrowserBotProcessError) as raised:
            self.adapter(runner).update_content(
                "3521676801",
                title="new",
            )

        self.assertEqual(raised.exception.phase, "update")
        self.assertEqual(raised.exception.returncode, 7)
        self.assertEqual(len(runner.calls), 3)

    def test_timeout_is_sanitized(self) -> None:
        runner = FakeRunner(ProcessResult(124, timed_out=True))

        with self.assertRaises(BrowserBotProcessError) as raised:
            self.adapter(runner).sync("3521676801")

        self.assertTrue(raised.exception.timed_out)
        self.assertEqual(str(raised.exception), "browser bot sync failed with return code 124 (timeout)")


if __name__ == "__main__":
    unittest.main()