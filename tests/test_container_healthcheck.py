"""Synthetic healthcheck tests: stale path != listening trusted UID.

No Docker/host service, real database, or platform operation is touched.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch


HEALTHCHECK = (
    Path(__file__).resolve().parents[1] / "docs/mark-api-container-healthcheck.py"
)
SPEC = importlib.util.spec_from_file_location("mark_container_healthcheck", HEALTHCHECK)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


class ContainerHealthcheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mark-uds-health-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "trusted.sock"

    def listen(self) -> socket.socket:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        self.path.chmod(0o660)
        server.listen(4)
        self.addCleanup(server.close)
        return server

    def test_bound_socket_has_live_peer_with_expected_uid(self) -> None:
        self.listen()
        self.assertTrue(check.check_socket(
            self.path, owner_uid=os.geteuid(), socket_mode=0o660,
        ))

    def test_socket_path_surviving_server_crash_is_not_healthy(self) -> None:
        server = self.listen()
        self.assertTrue(check.check_socket(
            self.path, owner_uid=os.geteuid(), socket_mode=0o660,
        ))
        server.close()
        self.assertFalse(check.check_socket(
            self.path, owner_uid=os.geteuid(), socket_mode=0o660,
        ))
        self.assertTrue(self.path.exists(), "healthcheck must never clean up state")

    def test_peer_owner_and_mode_mismatch_are_refused(self) -> None:
        self.listen()
        self.assertFalse(check.check_socket(
            self.path, owner_uid=os.geteuid() + 10000, socket_mode=0o660,
        ))
        self.assertFalse(check.check_socket(
            self.path, owner_uid=os.geteuid(), socket_mode=0o600,
        ))

    def test_regular_file_and_symlink_are_never_followed(self) -> None:
        self.path.write_text("private", encoding="utf-8")
        self.assertFalse(check.check_socket(
            self.path, owner_uid=os.geteuid(), socket_mode=0o660,
        ))
        alias = self.path.with_name("alias.sock")
        alias.symlink_to(self.path)
        self.assertFalse(check.check_socket(
            alias, owner_uid=os.geteuid(), socket_mode=0o660,
        ))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "private")


    def test_owner_healthcheck_never_opens_witness_runtime_session(self) -> None:
        # A UID50044 connection to witness/runtime.sock is a real authenticated
        # runtime session and appends opened/sealed events. A healthcheck may
        # only probe the owner's own socket (witness has a separate check).
        with (
            patch.object(check.os, "geteuid", return_value=50044),
            patch.object(check, "check_socket", return_value=True) as probe,
        ):
            self.assertEqual(check.main(["owner"]), 0)
        probe.assert_called_once_with(
            Path("/run/mark-api-executor/dashboard.sock"),
            owner_uid=50044, socket_mode=0o660,
        )

    def test_cli_rejects_unexpected_and_wrong_uid_before_io(self) -> None:
        for role in ([], ["unexpected"], ["owner", "extra"]):
            with self.subTest(role=role), patch.object(check, "check_socket") as probe:
                self.assertEqual(check.main(role), 1)
                probe.assert_not_called()
        with patch.object(check.os, "geteuid", return_value=99999), patch.object(
            check, "check_socket",
        ) as probe:
            self.assertEqual(check.main(["owner"]), 1)
            self.assertEqual(check.main(["witness"]), 1)
            probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()