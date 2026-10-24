"""Fail-closed ingress TCP policy and real rootful-Docker kernel checks.

This test deliberately never connects to Chrome, a Mark owner or user data.
The optional Docker check talks only to a freshly bound synthetic local TCP
listener; all Docker mounts are read-only, no volumes are created.
"""
from __future__ import annotations

import errno
import importlib.util
import io
import os
from pathlib import Path
import re
import shutil
import socket
import tempfile
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "docs" / "mark-api-container-ingress-landlock.py"
SPEC = importlib.util.spec_from_file_location("mark_ingress_landlock_test", BOOTSTRAP)
assert SPEC is not None and SPEC.loader is not None
landlock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(landlock)
IMAGE = os.environ.get("MARK_INGRESS_LANDLOCK_TEST_IMAGE", "")
IMAGE_OK = bool(re.fullmatch(r"sha256:[a-f0-9]{64}", IMAGE))
PYTHON = os.environ.get("MARK_INGRESS_LANDLOCK_TEST_PYTHON", "/usr/local/bin/python")
if PYTHON not in {"/usr/local/bin/python", "/opt/mark-api/venv/bin/python"}:
    raise ValueError("unattested ingress kernel test interpreter")


class IngressLandlockContractTests(unittest.TestCase):
    def test_non_ingress_uid_or_unbounded_port_fails_before_syscall(self) -> None:
        with patch.object(landlock.os, "geteuid", return_value=50044), \
                patch.object(landlock.ctypes, "CDLL") as cdll:
            with self.assertRaises(landlock.IngressLandlockError):
                landlock.install_tcp_ingress_policy(bind_port=8875)
            cdll.assert_not_called()
        with patch.object(landlock.os, "geteuid", return_value=50045), \
                patch.object(landlock.ctypes, "CDLL") as cdll:
            for port in (-1, 0, 65536, True, "8875", None):
                with self.subTest(port=port), \
                        self.assertRaises(landlock.IngressLandlockError):
                    landlock.install_tcp_ingress_policy(bind_port=port)
            cdll.assert_not_called()

    def test_old_kernel_abi_refuses_to_install_any_policy(self) -> None:
        libc = MagicMock()
        libc.syscall.return_value = 3  # Landlock ABI 3 has no TCP isolation.
        with patch.object(landlock.os, "geteuid", return_value=50045), \
             patch.object(landlock.ctypes, "CDLL", return_value=libc):
            with self.assertRaisesRegex(
                landlock.IngressLandlockError, "TCP policy unavailable",
            ):
                landlock.install_tcp_ingress_policy(bind_port=8875)
        self.assertEqual(libc.syscall.call_count, 1)
        libc.prctl.assert_not_called()

    def test_unattested_draft_and_unexpected_args_prevent_exec(self) -> None:
        with patch.object(landlock, "_DRAFT_MARKER") as marker, \
             patch.object(landlock, "install_tcp_ingress_policy") as install, \
             patch.object(landlock.os, "execv") as execv, \
             redirect_stderr(io.StringIO()):
            marker.exists.return_value = True
            self.assertEqual(landlock.main([]), 2)
            marker.exists.return_value = False
            self.assertEqual(landlock.main(["--unsafe"]), 2)
        install.assert_not_called()
        execv.assert_not_called()

    def test_fixed_port_and_frontend_endpoint_are_applied_before_exec(self) -> None:
        with patch.object(landlock, "_DRAFT_MARKER") as marker, \
             patch.object(landlock, "install_tcp_ingress_policy") as install, \
             patch.object(landlock.os, "execv", side_effect=RuntimeError("exec")) as execute:
            marker.exists.return_value = False
            with self.assertRaisesRegex(RuntimeError, "exec"):
                landlock.main([])
        install.assert_called_once_with(bind_port=8875)
        args = execute.call_args.args[1]
        self.assertEqual(args[:4], [
            "/opt/mark-api/venv/bin/python", "-I", "-m",
            "mark_api.dashboard_relay",
        ])
        self.assertEqual(args[4:], [
            "public-ingress", "--frontend-socket",
            "/run/mark-api-ingress/frontend.sock", "--frontend-uid", "50042",
            "--port", "8875",
        ])


_CHILD_AFTER_EXEC = r"""
import errno, socket, sys
destination=int(sys.argv[1]); permitted=int(sys.argv[2])
for family, address in (
    (socket.AF_INET, ('127.0.0.1',destination)),
    (socket.AF_INET6, ('::1',destination)),
):
    sock=socket.socket(family,socket.SOCK_STREAM)
    try:
        sock.connect(address)
    except PermissionError as exc:
        assert exc.errno in (errno.EACCES,errno.EPERM),exc
    else:
        raise AssertionError('direct TCP CONNECT escaped Landlock')
    finally:sock.close()
other=permitted+1 if permitted<65535 else permitted-1
sock=socket.socket()
try:
    sock.bind(('127.0.0.1',other))
except PermissionError as exc:
    assert exc.errno in (errno.EACCES,errno.EPERM),exc
else:
    raise AssertionError('unexpected TCP bind allowed')
finally:sock.close()
sock=socket.socket()
try:
    sock.bind(('127.0.0.1',permitted))
finally:sock.close()
left,right=socket.socketpair()
try:
    left.sendall(b'UDS_OK')
    assert right.recv(6)==b'UDS_OK'
finally:left.close();right.close()
print('LANDLOCK_DENIES_V4_V6_CONNECT_WRONG_BIND_AND_ALLOWS_UNIX_AFTER_EXEC',flush=True)
"""


@unittest.skipUnless(
    IMAGE_OK, "Set immutable local MARK_INGRESS_LANDLOCK_TEST_IMAGE for real Docker host-kernel test",
)
class IngressLandlockKernelTests(unittest.TestCase):
    def test_no_cdp_or_owner_tcp_escape_even_after_exec(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as target:
            target.bind(("127.0.0.1", 0))
            target.listen(2)
            target_port = target.getsockname()[1]
            # A distinct unoccupied permitted bind port; the restricted
            # container is the only component that will briefly bind it.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                permitted_port = probe.getsockname()[1]
            self.assertNotEqual(target_port, permitted_port)
            parent = (
                "import importlib.util,os,socket,sys\n"
                "spec=importlib.util.spec_from_file_location('guard',"
                "'/safety/mark-api-container-ingress-landlock.py')\n"
                "guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)\n"
                "target=int(sys.argv[1]);allowed=int(sys.argv[2])\n"
                "with socket.create_connection(('127.0.0.1',target),timeout=2):pass\n"
                "guard.install_tcp_ingress_policy(bind_port=allowed)\n"
                "os.execv(sys.executable,[sys.executable,'-B','-c',"
                + repr(_CHILD_AFTER_EXEC) +
                ",str(target),str(allowed)])\n"
            )
            # Grabowski creates only a public, synthetic read-only bootstrap
            # copy for UID50045. Its private checkout itself is not exposed.
            with tempfile.TemporaryDirectory(prefix="mark-landlock-") as tmp:
                stage = Path(tmp)
                copy = stage / BOOTSTRAP.name
                shutil.copyfile(BOOTSTRAP, copy)
                stage.chmod(0o755)
                copy.chmod(0o644)
                args = [
                    "/usr/bin/docker", "run", "--rm", "--network", "host",
                    "--user", "50045:50045", "--read-only",
                    "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true",
                    "--pids-limit", "32",
                    "--mount",
                    f"type=bind,source={stage},target=/safety,readonly",
                    "-e", "PYTHONDONTWRITEBYTECODE=1",
                    "--workdir", "/",
                    "--entrypoint", PYTHON, IMAGE,
                    "-B", "-c", parent, str(target_port), str(permitted_port),
                ]
                proc = subprocess.run(
                    args, capture_output=True, text=True,
                    check=False, timeout=45,
                )
        self.assertEqual(proc.returncode, 0, proc.stderr[-1600:])
        self.assertIn(
            "LANDLOCK_DENIES_V4_V6_CONNECT_WRONG_BIND_AND_ALLOWS_UNIX_AFTER_EXEC",
            proc.stdout,
        )


if __name__ == "__main__":
    unittest.main()