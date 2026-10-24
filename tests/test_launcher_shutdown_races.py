"""Synthetic adversarial SIGTERM and shutdown thread allocation regressions.

These tests mock all Kleinanzeigen, browser, SQLite and witness interactions.
No live account, host signals, platform writes or filesystem fixtures are used.
"""
from __future__ import annotations

import inspect
import io
import os
import signal
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import mark_api.launcher as launcher_module


def _args() -> list[str]:
    return [
        "--db", "/tmp/synthetic-not-opened.sqlite",
        "--cdp-port", "9222",
        "--witness-socket", "/tmp/synthetic-no-server.sock",
        "--witness-owner-uid", str(os.geteuid() + 10000),
    ]


class _Witness:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("witness-close")


class _Launcher:
    dashboard_url = "http://127.0.0.1:18765/#write_token=synthetic"
    write_server_address = ("127.0.0.1", 18766)
    write_bearer_token = "synthetic-token"
    startup_inventory_count = 0
    startup_persisted_count = 0

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def serve_forever(self) -> None:
        self.events.append("serve")
        raise KeyboardInterrupt

    def close(self) -> None:
        self.events.append("launcher-close")


class LauncherShutdownRacesTests(unittest.TestCase):
    def test_sigterm_after_outer_check_but_before_serving_flag_must_skip_serve(self) -> None:
        # CPython line tracing deterministically delivers termination on the
        # actual "serving = True" source line before that assignment executes.
        # The outer if was already evaluated, but serving remains False.
        lines, start = inspect.getsourcelines(launcher_module.main)
        stop_line = [
            start + i for i, line in enumerate(lines)
            if line.strip() == "serving = True"
        ]
        self.assertEqual(len(stop_line), 1)
        original_sigterm = signal.getsignal(signal.SIGTERM)
        events: list[str] = []
        witness = _Witness(events)
        runtime = _Launcher(events)
        injected: list[int] = []

        def trigger(frame, event, _arg):
            if (
                event == "line"
                and frame.f_code is launcher_module.main.__code__
                and frame.f_lineno == stop_line[0]
                and not injected
            ):
                handler = signal.getsignal(signal.SIGTERM)
                self.assertTrue(callable(handler))
                injected.append(frame.f_lineno)
                handler(signal.SIGTERM, None)
            return trigger

        prior_trace = sys.gettrace()
        try:
            with (
                patch.object(launcher_module, "FenceWitnessClient", return_value=witness),
                patch.object(launcher_module, "_require_private_web_authorization", create=True),
                patch.object(launcher_module, "build_product_launcher", return_value=runtime),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                sys.settrace(trigger)
                code = launcher_module.main(_args())
        finally:
            sys.settrace(prior_trace)

        self.assertEqual(code, 0)
        self.assertEqual(injected, stop_line)
        self.assertNotIn("serve", events, "a single SIGTERM during the serve transition was lost")
        self.assertEqual(events, ["launcher-close", "witness-close"])
        self.assertIs(signal.getsignal(signal.SIGTERM), original_sigterm)

    def test_failed_shutdown_worker_constructor_must_never_release_witness(self) -> None:
        self._failed_shutdown_worker("constructor")

    def test_failed_shutdown_worker_start_must_never_release_witness(self) -> None:
        self._failed_shutdown_worker("start")

    def _failed_shutdown_worker(self, failure: str) -> None:
        original_sigterm = signal.getsignal(signal.SIGTERM)
        events: list[str] = []
        witness = _Witness(events)
        runtime = _Launcher(events)
        worker_attempts: list[str] = []

        class BrokenStarter:
            def start(self) -> None:
                worker_attempts.append("start")
                raise RuntimeError("synthetic stop worker start failed")

        real_thread = threading.Thread

        def thread_factory(*args, **kwargs):
            name = kwargs.get("name")
            if name != "mark-owner-stop":
                return real_thread(*args, **kwargs)
            worker_attempts.append("construct")
            if failure == "constructor":
                raise RuntimeError("synthetic stop worker allocation failed")
            return BrokenStarter()

        stderr = io.StringIO()
        with (
            patch.object(launcher_module, "Thread", side_effect=thread_factory),
            patch.object(launcher_module, "FenceWitnessClient", return_value=witness),
            patch.object(launcher_module, "_require_private_web_authorization", create=True),
            patch.object(launcher_module, "build_product_launcher", return_value=runtime),
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", stderr),
        ):
            code = launcher_module.main(_args())
        self.assertEqual(code, 2)
        self.assertEqual(worker_attempts, (
            ["construct"] if failure == "constructor"
            else ["construct", "start"]
        ))
        # Since the shutdown worker was not started, owner quiescence was not
        # proven; the independent Witness may be closed only by OS teardown.
        self.assertEqual(events, ["serve"])
        self.assertIn("retain recovery fences", stderr.getvalue())
        self.assertIs(signal.getsignal(signal.SIGTERM), original_sigterm)


if __name__ == "__main__":
    unittest.main()
