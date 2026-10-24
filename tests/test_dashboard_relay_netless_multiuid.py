"""Opt-in synthetic rootful-Docker network:none, UID and Unix relay proof.

Set MARK_NETLESS_TEST_IMAGE to an already locally present immutable image ID
containing a reviewed Python entrypoint. Unique Docker names cannot prevent
concurrent same-name replacement; mutating tests must fail closed until an
independently enforced exclusive Docker context is available.
No production volume, Chrome profile, account or platform Write is touched.
A passed test proves this particular kernel/Docker host configuration only.
"""
from __future__ import annotations

from http.client import HTTPConnection
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4


REPO = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("MARK_NETLESS_TEST_IMAGE", "")
IMAGE_OK = bool(re.fullmatch(r"sha256:[a-f0-9]{64}", IMAGE))
PYTHON = os.environ.get("MARK_NETLESS_TEST_PYTHON", "/usr/local/bin/python")
if PYTHON not in {"/usr/local/bin/python", "/opt/mark-api/venv/bin/python"}:
    raise ValueError("unattested netless Docker test interpreter")


if os.environ.get("MARK_REQUIRE_NETLESS_UID") == "1" and not IMAGE_OK:
    raise ValueError("required Docker UID test lacks a pinned image")


_DOCKER_AUTHORITY_ERROR = (
    "exclusive Docker-admin authority not established: shared-daemon Docker "
    "resource mutation forbidden before subprocess"
)


def _require_exclusive_docker_admin_authority() -> None:
    """Real test may only run with independent, enforced daemon exclusivity."""
    if os.environ.get("MARK_REQUIRE_NETLESS_UID") == "1":
        raise AssertionError(_DOCKER_AUTHORITY_ERROR)
    raise unittest.SkipTest(_DOCKER_AUTHORITY_ERROR)


def _docker(*args: str, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    # Positive allowlist: neither name-based cleanup nor command aliases may
    # dispatch without independently enforced exclusive Docker authority.
    if not (
        args[:1] == ("info",)
        or args[:2] in (
            ("image", "inspect"),
            ("container", "inspect"),
            ("volume", "inspect"),
        )
    ):
        raise AssertionError(_DOCKER_AUTHORITY_ERROR)
    result = subprocess.run(
        ["/usr/bin/docker", *args],
        capture_output=True, text=True, check=False, timeout=45,
    )
    if result.returncode != 0 and not allow_failure:
        raise AssertionError("synthetic Docker error: " + result.stderr[-900:])
    return result


# Two sockets and three real, separate kernel peer UIDs.
_OWNER = r"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from mark_api.dashboard_relay import UnixDashboardRelay
import time
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload=b'{"ready":true}' if self.path == '/readyz' else b'not_found'
        self.send_response(200 if self.path == '/readyz' else 404)
        self.send_header('Content-Length',str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def log_message(self,*_):pass
http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
Thread(target=http.serve_forever,daemon=True).start()
relay=UnixDashboardRelay(socket_path=Path('/executor/dashboard.sock'),
    frontend_uid=50042,frontend_gid=50042,dashboard_host='127.0.0.1',
    dashboard_port=http.server_port)
relay.start()
print('OWNER_READY',http.server_port,flush=True)
while True:time.sleep(2)
"""

_FRONTEND = r"""
from mark_api.dashboard_relay import NetlessFrontendDashboardRelay
from pathlib import Path
import time
relay=NetlessFrontendDashboardRelay(
    socket_path=Path('/ingress/frontend.sock'),
    backend_path=Path('/executor/dashboard.sock'),
    ingress_uid=50045,ingress_gid=50045,owner_uid=50044)
relay.start()
print('NETLESS_READY',flush=True)
while True:time.sleep(2)
"""

_INGRESS = r"""
# Apply the very same trusted bootloader before importing Mark or opening TCP.
import importlib.util,os,socket,time
from pathlib import Path
port=int(os.environ['MARK_ALLOWED_PORT'])
owner_port=int(os.environ['MARK_OWNER_PORT'])
spec=importlib.util.spec_from_file_location('guard','/src/ingress-landlock.py')
guard=importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
guard.install_tcp_ingress_policy(bind_port=port)
from mark_api.dashboard_relay import TrustedDashboardPublicIngress
# Host TCP owner/CDP is reachable before policy on this host-network topology,
# but must be forbidden *inside this very restricted ingress process*.
probe=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
try:
    probe.connect(('127.0.0.1',owner_port))
except PermissionError:
    print('INGRESS_OWNER_TCP_LANDLOCK_DENIED',flush=True)
else:
    raise AssertionError('trusted ingress bypassed Landlock to owner TCP')
finally:probe.close()
relay=TrustedDashboardPublicIngress(
    socket_path=Path('/ingress/frontend.sock'),frontend_uid=50042,
    listen_host='127.0.0.1',listen_port=port)
relay.start()
print('INGRESS_READY',relay.server_address[1],flush=True)
while True:time.sleep(2)
"""

_ATTACK = r"""
import socket
from pathlib import Path
port=int(__import__('os').environ['MARK_INGRESS_PORT'])
owner_port=int(__import__('os').environ['MARK_OWNER_PORT'])
routes=Path('/proc/net/route').read_text(encoding='ascii').splitlines()[1:]
assert not [line for line in routes if line.split()[1]=='00000000']
for address,port_number in (
    ('127.0.0.1',port),
    ('127.0.0.1',owner_port),
    ('::1',port),
    ('::1',owner_port),
    ('172.17.0.1',9222),
    ('192.0.2.55',9222),
):
    try:
        with socket.create_connection((address,port_number),timeout=0.4):pass
    except OSError:
        continue
    raise AssertionError('netless UID50042 reached outside TCP '+address)
print('NETLESS_CDP_HOST_ESCAPE_DENIED',flush=True)
"""

_FOREIGN = r"""
import socket
try:
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
        sock.connect('/ingress/frontend.sock')
except PermissionError:
    print('FOREIGN_INGRESS_UID_DENIED',flush=True)
else:
    raise AssertionError('foreign UID crossed private ingress directory')
"""


@unittest.skipUnless(IMAGE_OK, "Set explicit immutable local Docker test image ID")
class NetlessMultiUidDockerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _require_exclusive_docker_admin_authority()

    def setUp(self) -> None:
        super().setUp()
        _require_exclusive_docker_admin_authority()

    def test_networkless_frontend_live_loopback_ingress_and_stale_socket(self) -> None:
        _require_exclusive_docker_admin_authority()
        suffix = uuid4().hex[:12]
        owner, frontend, ingress = (
            "mark-uds-owner-" + suffix,
            "mark-uds-front-" + suffix,
            "mark-uds-public-" + suffix,
        )
        executor_volume = "mark-uds-exec-" + suffix
        ingress_volume = "mark-uds-sock-" + suffix
        names: list[str] = []
        volumes: list[str] = []
        with tempfile.TemporaryDirectory(prefix="mark-uds-netless-") as temp:
            root = Path(temp)
            code = root / "src"
            shutil.copytree(
                REPO / "src" / "mark_api", code / "mark_api",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            shutil.copyfile(
                REPO / "docs" / "mark-api-container-ingress-landlock.py",
                code / "ingress-landlock.py",
            )
            for directory in (root, code, *(x for x in code.rglob("*") if x.is_dir())):
                directory.chmod(0o755)
            for regular in code.rglob("*"):
                if regular.is_file():
                    regular.chmod(0o644)
            try:
                for name in (executor_volume, ingress_volume):
                    _docker("volume", "create", name)
                    volumes.append(name)
                for volume, ownership in (
                    (executor_volume, "50044:50042"),
                    (ingress_volume, "50042:50045"),
                ):
                    _docker(
                        "run", "--rm", "--network", "none", "--user", "0:0",
                        "--mount", f"type=volume,source={volume},target=/bridge",
                        "--entrypoint", "/bin/sh", IMAGE, "-ec",
                        f"chown {ownership} /bridge; chmod 2710 /bridge",
                    )
                base = [
                    "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true",
                    "--mount", f"type=bind,source={code},target=/src,readonly",
                    "-e", "PYTHONPATH=/src", "-e", "PYTHONDONTWRITEBYTECODE=1",
                    "--workdir", "/", "--entrypoint", PYTHON,
                ]
                executor_mount = [
                    "--mount", f"type=volume,source={executor_volume},target=/executor",
                ]
                ing_mount = [
                    "--mount", f"type=volume,source={ingress_volume},target=/ingress",
                ]
                front_args = [
                    "--network", "none", "--user", "50042:50042",
                    *base, *executor_mount, *ing_mount,
                    IMAGE, "-B", "-c", _FRONTEND,
                ]
                _docker(
                    "run", "-d", "--name", owner, "--network", "host",
                    "--user", "50044:50044", *base,
                    *executor_mount, IMAGE, "-B", "-c", _OWNER,
                )
                names.append(owner)
                owner_port = int(self._ready(owner, r"OWNER_READY (\d+)").group(1))
                _docker("run", "-d", "--name", frontend, *front_args)
                names.append(frontend)
                self._ready(frontend, r"NETLESS_READY")
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
                    held.bind(("127.0.0.1", 0))
                    allowed_port = held.getsockname()[1]
                _docker(
                    "run", "-d", "--name", ingress, "--network", "host",
                    "--user", "50045:50045", *base,
                    "--mount", f"type=volume,source={ingress_volume},target=/ingress,readonly",
                    "-e", f"MARK_ALLOWED_PORT={allowed_port}",
                    "-e", f"MARK_OWNER_PORT={owner_port}",
                    IMAGE, "-B", "-c", _INGRESS,
                )
                names.append(ingress)
                self._ready(ingress, r"INGRESS_OWNER_TCP_LANDLOCK_DENIED")
                port = int(self._ready(ingress, r"INGRESS_READY (\d+)").group(1))
                self.assertEqual(port, allowed_port)
                self.assertEqual(
                    _docker("inspect", "-f", "{{.HostConfig.NetworkMode}}", frontend
                    ).stdout.strip(), "none",
                )
                ingress_mounts = _docker(
                    "inspect", "-f", "{{range .Mounts}}{{.Name}};{{end}}", ingress,
                ).stdout
                self.assertNotIn(executor_volume, ingress_mounts)
                self.assertIn(ingress_volume, ingress_mounts)
                connection = HTTPConnection("127.0.0.1", port, timeout=4)
                try:
                    connection.request("GET", "/readyz")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b'{"ready":true}')
                finally:
                    connection.close()
                # The ingress must not become a Chrome/CDP HTTP proxy.
                connection = HTTPConnection("127.0.0.1", port, timeout=4)
                try:
                    connection.request("GET", "/json/version")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 404)
                    self.assertEqual(response.read(), b"not_found")
                finally:
                    connection.close()
                denial = _docker(
                    "exec", "-u", "50042:50042", "-e",
                    f"MARK_INGRESS_PORT={port}", "-e",
                    f"MARK_OWNER_PORT={owner_port}", frontend,
                    PYTHON, "-B", "-c", _ATTACK,
                )
                self.assertIn("NETLESS_CDP_HOST_ESCAPE_DENIED", denial.stdout)
                foreign = _docker(
                    "run", "--rm", "--network", "none", "--user", "50046:50046",
                    *base,
                    "--mount", f"type=volume,source={ingress_volume},target=/ingress,readonly",
                    IMAGE, "-B", "-c", _FOREIGN,
                )
                self.assertIn("FOREIGN_INGRESS_UID_DENIED", foreign.stdout)
                # A forced kill leaves the old inode; a new frontend instance
                # must not blindly unlink it or start before operator recovery.
                _docker("rm", "-f", frontend)
                names.remove(frontend)
                retry = _docker(
                    "run", "--rm", *front_args, allow_failure=True,
                )
                self.assertNotEqual(retry.returncode, 0)
                self.assertIn("refusing to replace ingress socket", retry.stderr)
            finally:
                for name in reversed(names):
                    result = _docker("rm", "-f", name, allow_failure=True)
                    self.assertEqual(result.returncode, 0, result.stderr[-400:])
                for volume in reversed(volumes):
                    result = _docker("volume", "rm", volume, allow_failure=True)
                    self.assertEqual(result.returncode, 0, result.stderr[-400:])

    def _ready(self, name: str, marker: str) -> re.Match[str]:
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            result = _docker("logs", name)
            match = re.search(marker, result.stdout)
            if match:
                return match
            if "Traceback" in result.stderr + result.stdout:
                self.fail("synthetic container failed: " + result.stdout[-900:])
            time.sleep(0.2)
        self.fail("synthetic container not ready: " + name)



class DockerAuthorityGuardTests(unittest.TestCase):
    """Mock-only shared-daemon safety checks; no real Docker operation."""

    def test_mutation_aliases_block_before_subprocess(self) -> None:
        for args in (
            ("volume", "rm", "decoy"), ("volume", "remove", "decoy"),
            ("volume", "prune"), ("rm", "-f", "decoy"),
            ("container", "rm", "decoy"), ("network", "rm", "decoy"),
            ("system", "prune", "--volumes"), ("run", "--rm", "fake-image"),
        ):
            with self.subTest(args=args):
                with patch.object(subprocess, "run") as dispatched:
                    with self.assertRaisesRegex(
                        AssertionError, "exclusive Docker-admin authority",
                    ):
                        _docker(*args)
                    dispatched.assert_not_called()

    def test_passive_metadata_and_direct_testcase_gates(self) -> None:
        for args in (
            ("info",), ("image", "inspect", "sha256:" + "0" * 64),
            ("container", "inspect", "decoy"), ("volume", "inspect", "decoy"),
        ):
            fake = subprocess.CompletedProcess(["/usr/bin/docker", *args], 0, "", "")
            with patch.object(subprocess, "run", return_value=fake) as dispatched:
                self.assertIs(_docker(*args), fake)
                dispatched.assert_called_once()
        with patch.dict(os.environ, {"MARK_REQUIRE_NETLESS_UID": "1"}):
            with self.assertRaisesRegex(
                AssertionError, "exclusive Docker-admin authority",
            ):
                _require_exclusive_docker_admin_authority()
        with patch.dict(os.environ, {"MARK_REQUIRE_NETLESS_UID": "0"}):
            with patch.object(NetlessMultiUidDockerTests, "__unittest_skip__", False, create=True):
                case = NetlessMultiUidDockerTests(
                    "test_networkless_frontend_live_loopback_ingress_and_stale_socket"
                )
                result = case.run(unittest.TestResult())
        self.assertEqual(len(result.skipped), 1, result.skipped)
        self.assertFalse(result.errors)
        self.assertFalse(result.failures)