"""Fail-closed protected owner admission, before privileged services exist."""
from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from mark_api.completion_authority import CompletionAuthorityError
from mark_api.protected_owner import ProtectedOwnerRuntime, create_protected_owner_runtime


def params():
    uid = os.geteuid()
    return {
        "database": Path("/tmp/synthetic-owner-main.sqlite"),
        "dashboard_socket": Path("/tmp/synthetic-owner-dashboard.sock"),
        "witness_socket": Path("/tmp/synthetic-owner-witness.sock"),
        "actor_socket": Path("/tmp/synthetic-owner-actor.sock"),
        "witness_uid": uid + 101, "actor_uid": uid + 102,
        "runtime_uid": uid + 103, "runtime_gid": uid + 103,
        "principal": "synthetic-customer",
        "bearer_token": "synthetic-private-owner-bearer",
        "media_enabled": True, "write_port": 0,
    }


class ProtectedOwnerAdmissionTests(unittest.TestCase):
    def test_relative_non_path_or_overlapping_resources_deny_before_witness(self):
        defaults = params()
        cases = [
            {"database": Path("relative.sqlite")},
            {"dashboard_socket": "/tmp/owner.sock"},
            {"actor_socket": defaults["witness_socket"]},
            {"write_port": True},
            {"write_port": -1},
            {"media_enabled": 1},
            {"runtime_gid": 0},
        ]
        for altered in cases:
            with self.subTest(altered=altered), patch(
                "mark_api.protected_owner.FenceWitnessClient",
            ) as witness:
                with self.assertRaises(ValueError):
                    create_protected_owner_runtime(**{**defaults, **altered})
                witness.assert_not_called()

    def test_equal_runtime_actor_witness_or_root_identity_denied(self):
        cfg = params()
        for changed in (
            {"actor_uid": cfg["runtime_uid"]},
            {"witness_uid": cfg["actor_uid"]},
            {"runtime_uid": os.geteuid()},
        ):
            with self.subTest(changed=changed), patch(
                "mark_api.protected_owner.FenceWitnessClient",
            ) as witness:
                with self.assertRaises(CompletionAuthorityError):
                    create_protected_owner_runtime(**{**cfg, **changed})
                witness.assert_not_called()

    @unittest.skipIf(os.geteuid() == 0, "nonroot UID required")
    def test_missing_independent_witness_never_creates_sqlite_or_owner(self):
        cfg = params()
        with patch(
            "mark_api.protected_owner.FenceWitnessClient",
            side_effect=OSError("synthetic witness absent"),
        ) as witness, patch(
            "mark_api.protected_owner.CompletionDashboardOwnerServer",
        ) as owner:
            with self.assertRaises(OSError):
                create_protected_owner_runtime(**cfg)
            witness.assert_called_once_with(
                cfg["witness_socket"], expected_owner_uid=cfg["witness_uid"],
            )
            owner.assert_not_called()

    def test_owner_close_always_closes_witness_after_owner_stop_failure(self):
        dashboard = MagicMock()
        dashboard.stop.side_effect = RuntimeError("synthetic quiescence error")
        witness = MagicMock()
        runtime = ProtectedOwnerRuntime(object(), dashboard, witness)
        with self.assertRaises(RuntimeError):
            runtime.close()
        witness.close.assert_called_once_with()

    @unittest.skipIf(os.geteuid() == 0, "nonroot UID required")
    def test_factory_reuses_exact_attested_owner_store_for_write_api(self):
        cfg = params()
        independent_owner = MagicMock()
        independent_owner.store = object()
        witness = MagicMock()
        server = MagicMock()
        with patch(
            "mark_api.protected_owner.FenceWitnessClient", return_value=witness,
        ), patch(
            "mark_api.protected_owner.CompletionDashboardOwnerServer",
            return_value=independent_owner,
        ), patch(
            "mark_api.protected_owner.create_owner_write_api_server",
            return_value=server,
        ) as write_builder:
            runtime = create_protected_owner_runtime(**cfg)
            self.assertIs(runtime.write_server, server)
            independent_owner.start.assert_called_once_with()
            bound = write_builder.call_args.kwargs
            self.assertIs(bound["store"], independent_owner.store)
            self.assertIs(bound["fence_witness"], witness)
            self.assertEqual(bound["authority_uid"], cfg["actor_uid"])
            self.assertEqual(bound["runtime_uid"], cfg["runtime_uid"])
            self.assertEqual(bound["access"].principal, cfg["principal"])
            self.assertTrue(bound["access"].writes_enabled)
            runtime.close()
            independent_owner.stop.assert_called_once_with()
            witness.close.assert_called_once_with()


class ProtectedOwnerCliTests(unittest.TestCase):
    def pipe(self, content: bytes) -> int:
        fd, writer = os.pipe()
        try:
            os.write(writer, content)
        finally:
            os.close(writer)
        self.addCleanup(os.close, fd)
        return fd

    def test_private_owner_config_is_exact_and_bounded(self):
        import json
        from mark_api.protected_owner import _read_private_owner_config
        cfg = {**params(), **{
            key: str(params()[key])
            for key in ("database", "dashboard_socket", "witness_socket", "actor_socket")
        }}
        data = (json.dumps(cfg) + "\n").encode()
        self.assertEqual(_read_private_owner_config(self.pipe(data)), cfg)

    def test_malformed_foreign_or_config_escape_rejected(self):
        import json
        from mark_api.protected_owner import _read_private_owner_config
        cfg = {**params(), **{
            key: str(params()[key])
            for key in ("database", "dashboard_socket", "witness_socket", "actor_socket")
        }}
        cases = (
            b'{"actor_uid":1,"actor_uid":2}\n',
            b'{"actor_uid":NaN}\n',
            b"{}\n" + b"x" * 8200,
            (json.dumps({**cfg, "sql": "DROP TABLE"}) + "\n").encode(),
            (json.dumps({**cfg, "media_enabled": 1}) + "\n").encode(),
            (json.dumps({**cfg, "database": "../owner.sqlite"}) + "\n").encode(),
            (json.dumps({k: v for k, v in cfg.items() if k != "bearer_token"}) + "\n").encode(),
        )
        for data in cases:
            with self.subTest(data=data[:25]), self.assertRaises(ValueError):
                _read_private_owner_config(self.pipe(data))

    def test_missing_witness_and_bootstrap_never_open_untrusted_sqlite(self):
        import io
        import json
        from contextlib import redirect_stderr
        from mark_api.protected_owner import main
        cfg = {**params(), **{
            key: str(params()[key])
            for key in ("database", "dashboard_socket", "witness_socket", "actor_socket")
        }}
        buf = io.StringIO()
        with redirect_stderr(buf):
            status = main(["--config-fd", str(self.pipe((json.dumps(cfg) + "\n").encode()))])
        self.assertEqual(status, 2)
        self.assertIn("unavailable", buf.getvalue())
        self.assertNotIn(cfg["bearer_token"], buf.getvalue())
        self.assertNotIn(cfg["database"], buf.getvalue())


    def test_shutdown_exception_is_fail_closed_and_never_logs_private_path(self):
        import io
        import signal
        from contextlib import redirect_stderr
        from mark_api.protected_owner import main

        cfg = {**params(), **{
            name: str(params()[name])
            for name in ("database", "dashboard_socket", "witness_socket", "actor_socket")
        }}
        fake_runtime = MagicMock()
        fake_runtime.write_server.server_port = 17721
        fake_runtime.write_server.shutdown.side_effect = RuntimeError(
            "synthetic database path " + cfg["database"] + " token " + cfg["bearer_token"]
        )
        stderr = io.StringIO()
        with patch(
            "mark_api.protected_owner._read_private_owner_config",
            return_value=cfg,
        ), patch(
            "mark_api.protected_owner.create_protected_owner_runtime",
            return_value=fake_runtime,
        ), patch(
            "threading.Event"
        ), patch(
            "threading.Thread"
        ), patch(
            "signal.signal", return_value=signal.SIG_DFL
        ), redirect_stderr(stderr):
            result = main(["--config-fd", "0"])
        self.assertEqual(result, 2)
        self.assertNotIn(cfg["database"], stderr.getvalue())
        self.assertNotIn(cfg["bearer_token"], stderr.getvalue())
        fake_runtime.close.assert_not_called()


    def test_sync_interval_is_strictly_private_and_bounded(self):
        import json
        from mark_api.protected_owner import _read_private_owner_config
        cfg = {**params(), **{
            k: str(params()[k])
            for k in ("database", "dashboard_socket", "witness_socket", "actor_socket")
        }}
        for value in (0, 0.0, 60, 90.5, 86400):
            with self.subTest(allowed=value):
                candidate = {**cfg, "sync_interval_seconds": value}
                result = _read_private_owner_config(
                    self.pipe((json.dumps(candidate) + "\n").encode())
                )
                self.assertEqual(result, candidate)
        for value in (True, False, -1, 1, 59, 86401, 60.0 + 10**6,
                      float("nan"), float("inf"), "120", 10**350):
            with self.subTest(denied=repr(value)[:32]), self.assertRaises(ValueError):
                candidate = {**cfg, "sync_interval_seconds": value}
                _read_private_owner_config(
                    self.pipe((json.dumps(candidate) + "\n").encode())
                )

    def test_trusted_periodic_sync_reuses_same_attested_store_and_drains(self):
        import io
        import signal
        import threading
        from contextlib import redirect_stdout, redirect_stderr
        from mark_api.protected_owner import main

        config = {**params(), **{
            k: str(params()[k])
            for k in ("database", "dashboard_socket", "witness_socket", "actor_socket")
        }, "sync_interval_seconds": 60}
        runtime = MagicMock()
        runtime.write_server.server_port = 17723
        owner_store = runtime.dashboard_owner.store
        actor_client = object()
        RealEvent = threading.Event
        events = []
        threads = []

        def make_event():
            event = RealEvent()
            events.append(event)
            return event

        class FakeThread:
            def __init__(self, *, target, name=None, kwargs=None, daemon=None):
                self.target = target
                self.name = name
                self.kwargs = kwargs or {}
                self.daemon = daemon
                threads.append(self)

            def start(self):
                if self.name == "kleinanzeiger-protected-inventory":
                    self.target(**self.kwargs)

            def join(self, timeout=None):
                pass

            def is_alive(self):
                return False

        def persist_once(store, client):
            self.assertIs(store, owner_store)
            self.assertIs(client, actor_client)
            events[0].set()
            return 1

        with patch(
            "mark_api.protected_owner._read_private_owner_config",
            return_value=config,
        ), patch(
            "mark_api.protected_owner.create_protected_owner_runtime",
            return_value=runtime,
        ), patch(
            "mark_api.protected_inventory.OwnerActorInventoryClient",
            return_value=actor_client,
        ) as client_build, patch(
            "mark_api.protected_inventory.sync_actor_inventory_once",
            side_effect=persist_once,
        ) as sync, patch(
            "threading.Event", side_effect=make_event,
        ), patch(
            "threading.Thread", side_effect=FakeThread,
        ), patch(
            "signal.signal", return_value=signal.SIG_DFL,
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = main(["--config-fd", "0"])
        self.assertEqual(status, 0)
        client_build.assert_called_once_with(
            actor_socket=params()["actor_socket"],
            actor_uid=config["actor_uid"],
        )
        sync.assert_called_once_with(owner_store, actor_client)
        self.assertEqual(len(threads), 2)
        self.assertTrue(next(t for t in threads if t.name == "kleinanzeiger-protected-inventory").daemon)
        runtime.write_server.shutdown.assert_called_once_with()
        runtime.close.assert_called_once_with()

    def test_failed_actor_read_does_not_trigger_retry_or_success_exit(self):
        import io
        import signal
        import threading
        from contextlib import redirect_stdout, redirect_stderr
        from mark_api.protected_owner import main

        cfg = {**params(), **{
            k: str(params()[k])
            for k in ("database", "dashboard_socket", "witness_socket", "actor_socket")
        }, "sync_interval_seconds": 60}
        runtime = MagicMock()
        runtime.write_server.server_port = 17724
        original_event = threading.Event
        events = []

        def new_event():
            event = original_event()
            events.append(event)
            return event

        class FakeThread:
            def __init__(self, *, target, name=None, kwargs=None, daemon=None):
                self.target, self.name, self.kwargs = target, name, kwargs or {}

            def start(self):
                if self.name == "kleinanzeiger-protected-inventory":
                    self.target(**self.kwargs)
                    events[0].set()

            def join(self, timeout=None):
                pass

            def is_alive(self):
                return False

        with patch(
            "mark_api.protected_owner._read_private_owner_config",
            return_value=cfg,
        ), patch(
            "mark_api.protected_owner.create_protected_owner_runtime",
            return_value=runtime,
        ), patch(
            "mark_api.protected_inventory.OwnerActorInventoryClient",
        ), patch(
            "mark_api.protected_inventory.sync_actor_inventory_once",
            side_effect=RuntimeError("synthetic unknown actor ACK"),
        ) as sync, patch(
            "threading.Event", side_effect=new_event,
        ), patch(
            "threading.Thread", side_effect=FakeThread,
        ), patch(
            "signal.signal", return_value=signal.SIG_DFL,
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = main(["--config-fd", "0"])
        self.assertEqual(status, 2)
        sync.assert_called_once()
        runtime.close.assert_called_once()



    def test_unresolved_inventory_thread_cannot_block_process_exit_indefinitely(self):
        import io
        import signal
        from contextlib import redirect_stdout, redirect_stderr
        from mark_api.protected_owner import main

        cfg = {**params(), **{
            name: str(params()[name])
            for name in ("database", "dashboard_socket", "witness_socket", "actor_socket")
        }, "sync_interval_seconds": 60}
        runtime = MagicMock()
        runtime.write_server.server_port = 17725
        workers = []

        class HungThread:
            def __init__(self, *, target, name=None, kwargs=None, daemon=None):
                self.name = name
                self.daemon = daemon
                self.join_seconds = None
                workers.append(self)

            def start(self):
                pass

            def join(self, seconds=None):
                self.join_seconds = seconds

            def is_alive(self):
                return self.name == "kleinanzeiger-protected-inventory"

        with patch(
            "mark_api.protected_owner._read_private_owner_config", return_value=cfg,
        ), patch(
            "mark_api.protected_owner.create_protected_owner_runtime", return_value=runtime,
        ), patch(
            "mark_api.protected_inventory.OwnerActorInventoryClient",
        ), patch(
            "threading.Thread", side_effect=HungThread,
        ), patch(
            "threading.Event",
        ) as event, patch(
            "signal.signal", return_value=signal.SIG_DFL,
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            event.return_value.wait.return_value = True
            status = main(["--config-fd", "0"])
        self.assertEqual(status, 2)
        self.assertEqual(len(workers), 2)
        inventory_worker = next(t for t in workers if t.name == "kleinanzeiger-protected-inventory")
        self.assertTrue(inventory_worker.daemon)
        self.assertEqual(inventory_worker.join_seconds, 50.0)
        runtime.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()