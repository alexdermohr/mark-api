"""Fail-closed independent Witness CLI and preprovisioned-ledger tests."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from mark_api.protected_witness import (
    _read_private_witness_config, create_protected_witness_runtime, main,
)


def config():
    uid = os.geteuid()
    return {
        "database": "/tmp/synthetic-a6-protected-witness-ledger.sqlite",
        "owner_socket": "/tmp/synthetic-a6-protected-witness-owner.sock",
        "actor_socket": "/tmp/synthetic-a6-protected-witness-actor.sock",
        "owner_uid": uid + 101,
        "owner_gid": uid + 101,
        "actor_uid": uid + 102,
        "actor_gid": uid + 102,
        "runtime_uid": uid + 103,
        "principal": "synthetic-protected-witness-customer",
    }


class PrivateWitnessConfigTests(unittest.TestCase):
    def pipe(self, data: bytes) -> int:
        reader, writer = os.pipe()
        try:
            os.write(writer, data)
        finally:
            os.close(writer)
        self.addCleanup(os.close, reader)
        return reader

    def test_strict_single_frame_config(self):
        cfg = config()
        self.assertEqual(
            _read_private_witness_config(
                self.pipe((json.dumps(cfg) + "\n").encode())
            ), cfg
        )

    def test_duplicate_extra_missing_unknown_and_noncanonical_fields_denied(self):
        cfg = config()
        cases = [
            b'{"owner_uid":1,"owner_uid":2}\n',
            b'{"runtime_uid":NaN}\n',
            b"{}\n" + b"x" * 8192,
            b"{}\n{}\n",
            b"{}\n" + b"x" * 8192 + b"\n",
            (json.dumps({**cfg, "owner_uid": True}) + "\n").encode(),
            (json.dumps({**cfg, "actor_uid": cfg["owner_uid"]}) + "\n").encode(),
            (json.dumps({**cfg, "runtime_uid": os.geteuid()}) + "\n").encode(),
            (json.dumps({**cfg, "owner_gid": cfg["actor_gid"]}) + "\n").encode(),
            (json.dumps({**cfg, "principal": "control\nchar"}) + "\n").encode(),
            (json.dumps({**cfg, "database": "../relative.sqlite"}) + "\n").encode(),
            (json.dumps({**cfg, "database": "/tmp/../stale.sqlite"}) + "\n").encode(),
            (json.dumps({**cfg, "actor_socket": cfg["owner_socket"]}) + "\n").encode(),
            (json.dumps({**cfg, "exec": "forged"}) + "\n").encode(),
            (json.dumps({k: v for k, v in cfg.items() if k != "actor_uid"}) + "\n").encode(),
        ]
        for payload in cases:
            with self.subTest(data=payload[:48]), self.assertRaises(ValueError):
                _read_private_witness_config(self.pipe(payload))

    def test_public_regular_descriptor_rejected(self):
        with tempfile.TemporaryFile() as file:
            file.write((json.dumps(config()) + "\n").encode())
            file.flush()
            os.fchmod(file.fileno(), 0o644)
            file.seek(0)
            with self.assertRaises(ValueError):
                _read_private_witness_config(file.fileno())

    @unittest.skipIf(os.geteuid() == 0, "must run under nonroot Witness UID")
    def test_missing_witness_ledger_cannot_be_created_on_normal_start(self):
        cfg = config()
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "not-provisioned.sqlite"
            with self.assertRaises(Exception):
                create_protected_witness_runtime(
                    database=database,
                    owner_socket=Path(tmp) / "owner.sock",
                    actor_socket=Path(tmp) / "actor.sock",
                    owner_uid=cfg["owner_uid"], owner_gid=cfg["owner_gid"],
                    actor_uid=cfg["actor_uid"], actor_gid=cfg["actor_gid"],
                    runtime_uid=cfg["runtime_uid"], principal=cfg["principal"],
                )
            self.assertFalse(database.exists())
            self.assertFalse(database.with_name(database.name + ".lock").exists())

    def test_main_refuses_missing_ledger_without_path_or_secret_logs(self):
        cfg = config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["database"] = str(Path(tmp) / "missing-preprovisioned-ledger.sqlite")
            sensitive = cfg["database"]
            err = io.StringIO()
            with redirect_stderr(err):
                rc = main(["--config-fd", str(
                    self.pipe((json.dumps(cfg) + "\n").encode())
                )])
            self.assertEqual(rc, 2)
            self.assertIn("unavailable", err.getvalue())
            self.assertNotIn(sensitive, err.getvalue())
            self.assertNotIn(cfg["principal"], err.getvalue())

    def test_forged_owner_uid_is_rejected_without_traceback_or_paths(self):
        cfg = {**config(), "owner_uid": os.geteuid(),
               "owner_gid": os.geteuid()}
        err = io.StringIO()
        with redirect_stderr(err):
            rc = main(["--config-fd", str(
                self.pipe((json.dumps(cfg) + "\n").encode())
            )])
        self.assertEqual(rc, 2)
        self.assertIn("config rejected", err.getvalue())
        self.assertNotIn(cfg["database"], err.getvalue())
        self.assertNotIn(cfg["principal"], err.getvalue())

    def test_verified_shutdown_never_leaks_config(self):
        cfg = config()
        runtime = MagicMock()
        runtime.server._threads = (MagicMock(), MagicMock())
        for thread in runtime.server._threads:
            thread.is_alive.return_value = True
        with patch(
            "mark_api.protected_witness._read_private_witness_config",
            return_value=cfg,
        ), patch(
            "mark_api.protected_witness.create_protected_witness_runtime",
            return_value=runtime,
        ), patch(
            "mark_api.protected_witness.Event",
        ) as event, patch(
            "signal.signal", return_value=signal.SIG_DFL,
        ), redirect_stdout(io.StringIO()) as stdout:
            event.return_value.wait.return_value = True
            rc = main(["--config-fd", "0"])
        self.assertEqual(rc, 0)
        runtime.server.start.assert_called_once()
        runtime.close.assert_called_once()
        self.assertIn('"ready":true', stdout.getvalue())
        self.assertNotIn(cfg["database"], stdout.getvalue())

    def test_uncertain_shutdown_denied_without_path_disclosure(self):
        cfg = config()
        runtime = MagicMock()
        runtime.server._threads = (MagicMock(), MagicMock())
        for thread in runtime.server._threads:
            thread.is_alive.return_value = True
        runtime.close.side_effect = RuntimeError(cfg["database"] + " unknown ACK")
        err = io.StringIO()
        with patch(
            "mark_api.protected_witness._read_private_witness_config",
            return_value=cfg,
        ), patch(
            "mark_api.protected_witness.create_protected_witness_runtime",
            return_value=runtime,
        ), patch(
            "mark_api.protected_witness.Event",
        ) as event, patch(
            "signal.signal", return_value=signal.SIG_DFL,
        ), redirect_stdout(io.StringIO()), redirect_stderr(err):
            event.return_value.wait.return_value = True
            rc = main(["--config-fd", "0"])
        self.assertEqual(rc, 2)
        self.assertIn("shutdown uncertain", err.getvalue())
        self.assertNotIn(cfg["database"], err.getvalue())


if __name__ == "__main__":
    unittest.main()