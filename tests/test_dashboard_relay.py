"""Contract tests for a narrow, peer-UID-bound dashboard transport.

These same-UID unit tests verify framing/lifecycle, not production UID isolation;
a separate multi-UID process test remains required for the real installation.
"""
from __future__ import annotations

import errno
import http.client
import os
from contextlib import closing
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from mark_api.dashboard_relay import (
    DashboardRelayError,
    _peer_uid,
    _relay_streams,
    _chrome_cdp_reachable,
    FrontendDashboardRelay,
    UnixDashboardRelay,
    NetlessFrontendDashboardRelay,
    TrustedDashboardPublicIngress,
    relay_frontend_main,
    relay_netless_frontend_main,
    relay_public_ingress_main,
)


class _TypedDashboard(BaseHTTPRequestHandler):
    calls: list[tuple[str, str]] = []

    def _respond(self, body: bytes, *, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        type(self).calls.append(("GET", self.path))
        if self.path != "/readyz":
            self._respond(b'{"error":"not_found"}', status=404)
        else:
            self._respond(b'{"ready":true}')

    def do_POST(self) -> None:
        type(self).calls.append(("POST", self.path))
        if self.path != "/api/write/ads":
            self._respond(b'{"error":"not_found"}', status=404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self._respond(b'{"accepted":true}')

    def log_message(self, format: str, *args: object) -> None:
        pass


class DashboardRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mark-dashboard-relay-")
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name) / "private"
        self.dir.mkdir(mode=0o700)
        self.dir.chmod(0o2710)
        self.sock = self.dir / "dashboard.sock"
        _TypedDashboard.calls = []
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), _TypedDashboard)
        thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.addCleanup(thread.join, 3)
        self.relay = None
        self.front = None

    @unittest.skipUnless(hasattr(socket, "SO_PEERCRED"), "requires SO_PEERCRED")
    def test_linux_peer_uid_high_bit_is_unsigned(self) -> None:
        high_uid = 2**31 + 7
        credentials = struct.pack("=iII", 123, high_uid, 50042)
        peer = MagicMock()
        peer.getsockopt.return_value = credentials
        self.assertEqual(_peer_uid(peer), high_uid)

    def make_relay(self, *, peer_uid: int | None = None) -> None:
        self.relay = UnixDashboardRelay(
            socket_path=self.sock,
            frontend_uid=os.geteuid() if peer_uid is None else peer_uid,
            frontend_gid=os.getegid(),
            dashboard_host="127.0.0.1",
            dashboard_port=self.http.server_port,
        )
        self.relay.start()
        self.addCleanup(self.relay.stop)

    def make_front(self, *, owner_uid: int | None = None) -> None:
        self.front = FrontendDashboardRelay(
            socket_path=self.sock,
            expected_owner_uid=os.geteuid() if owner_uid is None else owner_uid,
            listen_host="127.0.0.1",
            listen_port=0,
        )
        self.front.start()
        self.addCleanup(self.front.stop)

    def get(self, path: str) -> tuple[int, bytes]:
        assert self.front is not None
        connection = http.client.HTTPConnection("127.0.0.1", self.front.server_port, timeout=3)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_exact_http_dashboard_and_write_route_not_cdp_passthrough(self) -> None:
        self.make_relay()
        self.make_front()
        status, body = self.get("/readyz")
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"ready":true}')
        status, body = self.get("/json/list")
        self.assertEqual(status, 404)
        self.assertEqual(body, b'{"error":"not_found"}')
        conn = http.client.HTTPConnection("127.0.0.1", self.front.server_port, timeout=3)
        try:
            conn.request("POST", "/api/write/ads", body=b"{}", headers={
                "Content-Type": "application/json",
            })
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b'{"accepted":true}')
        finally:
            conn.close()
        self.assertEqual(
            _TypedDashboard.calls,
            [("GET", "/readyz"), ("GET", "/json/list"), ("POST", "/api/write/ads")],
        )

    def test_idle_http_tunnel_has_hard_deadline_without_payload(self) -> None:
        # Fake the elapsed monotonic clock rather than holding a real socket
        # open for ninety seconds. This used to spin forever on select([],[],[]).
        first, first_peer = socket.socketpair()
        second, second_peer = socket.socketpair()
        self.addCleanup(first.close)
        self.addCleanup(first_peer.close)
        self.addCleanup(second.close)
        self.addCleanup(second_peer.close)

        ticks = iter((0.0, 0.0, 91.0))
        with (
            patch("mark_api.dashboard_relay.time.monotonic",
                  side_effect=lambda: next(ticks, 91.0)),
            patch("mark_api.dashboard_relay.select.select",
                  side_effect=[([], [], []), AssertionError(
                      "idle deadline not enforced before next select",
                  )]),
        ):
            with self.assertRaisesRegex(DashboardRelayError, "idle timeout"):
                _relay_streams(first, second)

    def test_shutdown_quiesces_incomplete_http_tunnel_before_owner_cleanup(self) -> None:
        """An unfinished browser request cannot hold the owner and witness open."""
        self.make_relay()
        self.make_front()
        assert self.front is not None
        assert self.relay is not None
        client = socket.create_connection(
            ("127.0.0.1", self.front.server_port), timeout=2,
        )
        try:
            # Deliberately incomplete headers; both proxy and backend
            # handlers would otherwise wait for the request indefinitely.
            client.sendall(b"GET /readyz HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\n")
            deadline = __import__("time").monotonic() + 2
            while __import__("time").monotonic() < deadline:
                with self.front._state:
                    frontend_active = self.front._active
                with self.relay._state:
                    owner_active = self.relay._active
                if frontend_active > 0 and owner_active > 0:
                    break
                __import__("time").sleep(.01)
            else:
                self.fail("relay did not accept the incomplete browser request")
            self.assertTrue(self.front.stop(timeout_seconds=.5))
            self.assertTrue(self.relay.stop(timeout_seconds=.5))
        finally:
            client.close()

    def test_idle_browser_tunnel_closes_without_waiting_full_session_lifetime(self) -> None:
        self.make_relay()
        self.make_front()
        assert self.front is not None
        with patch("mark_api.dashboard_relay._IDLE_TIMEOUT", .08):
            client = socket.create_connection(
                ("127.0.0.1", self.front.server_port), timeout=2,
            )
            try:
                client.settimeout(.8)
                client.sendall(b"GET /readyz HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\n")
                self.assertEqual(
                    client.recv(1), b"",
                    "unfinished HTTP request retained an idle relay tunnel",
                )
            finally:
                client.close()

    def test_unauthorized_frontend_uid_refused_before_dashboard_connect(self) -> None:
        self.make_relay(peer_uid=os.geteuid() + 10000)
        self.make_front()
        with self.assertRaises((OSError, http.client.HTTPException)):
            self.get("/readyz")
        self.assertEqual(_TypedDashboard.calls, [])

    def test_fake_owner_uid_refused_by_frontend(self) -> None:
        self.make_relay()
        self.make_front(owner_uid=os.geteuid() + 10000)
        with self.assertRaises((OSError, http.client.HTTPException)):
            self.get("/readyz")
        self.assertEqual(_TypedDashboard.calls, [])

    def test_relay_refuses_existing_socket_without_replacing_it(self) -> None:
        self.sock.write_text("preexisting", encoding="utf-8")
        with self.assertRaises(Exception):
            self.make_relay()
        self.assertEqual(self.sock.read_text(encoding="utf-8"), "preexisting")

    def test_relay_refuses_group_writable_parent(self) -> None:
        self.dir.chmod(0o2770)
        with self.assertRaises(Exception):
            self.make_relay()

    def test_frontend_refuses_to_bind_if_local_chrome_cdp_is_reachable(self) -> None:
        """Wrong host namespace must never expose direct Chrome to UID 50042."""
        with (
            patch("mark_api.dashboard_relay._chrome_cdp_reachable", return_value=True),
            patch("mark_api.dashboard_relay.FrontendDashboardRelay") as listener,
            patch("sys.stderr"),
        ):
            code = relay_frontend_main([
                "--socket", str(self.sock),
                "--expected-owner-uid", str(os.geteuid() + 10000),
                "--port", "8875",
            ])
        self.assertEqual(code, 2)
        listener.assert_not_called()

    def test_host_gateway_cdp_or_owner_ports_fail_closed(self) -> None:
        # 172.17.0.1 is a synthetic bridge gateway, not frontend loopback.
        routes = chr(10).join((
            "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT",
            "eth0 00000000 010011AC 0003 0 0 0 00000000 0 0 0",
        ))
        for port in (9222, 18875, 18876):
            observed = []

            def connect(destination, *, timeout):
                observed.append(destination)
                if destination == ("172.17.0.1", port):
                    return MagicMock()
                raise ConnectionRefusedError

            with self.subTest(port=port), patch(
                "mark_api.dashboard_relay.Path.read_text", return_value=routes,
            ), patch(
                "mark_api.dashboard_relay.socket.create_connection",
                side_effect=connect,
            ):
                self.assertTrue(_chrome_cdp_reachable())
                self.assertIn(("172.17.0.1", port), observed)

    def test_missing_or_unreadable_default_gateway_is_not_isolation_proof(self) -> None:
        for routes in ("Iface Destination Gateway Flags RefCnt Use Metric Mask",
                       "broken-row"):
            with self.subTest(routes=routes), patch(
                "mark_api.dashboard_relay.Path.read_text", return_value=routes,
            ), patch(
                "mark_api.dashboard_relay.socket.create_connection",
                side_effect=ConnectionRefusedError,
            ):
                self.assertTrue(_chrome_cdp_reachable())

    def test_frontend_cli_rejects_same_uid_before_binding(self) -> None:
        with patch("sys.stderr"):
            code = relay_frontend_main([
                "--socket", str(self.sock),
                "--expected-owner-uid", str(os.geteuid()),
                "--port", "0",
            ])
        self.assertEqual(code, 2)
        self.assertIsNone(self.front)



class NetlessFrontendContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mark-netless-relay-")
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name) / "ingress"
        self.parent.mkdir(mode=0o700)
        self.parent.chmod(0o2710)
        self.sock = self.parent / "frontend.sock"
        self.uid = os.geteuid()
        self.fake_ingress_uid = self.uid + 10010
        self.fake_owner_uid = self.uid + 10011

    def make_netless(self) -> NetlessFrontendDashboardRelay:
        relay = NetlessFrontendDashboardRelay(
            socket_path=self.sock, backend_path=self.parent / "executor.sock",
            ingress_uid=self.fake_ingress_uid, ingress_gid=os.getegid(),
            owner_uid=self.fake_owner_uid,
        )
        self.addCleanup(relay.stop)
        return relay

    def test_netless_rejects_same_uid_and_unsafe_existing_socket(self) -> None:
        with self.assertRaises(DashboardRelayError):
            NetlessFrontendDashboardRelay(
                socket_path=self.sock, backend_path=self.parent / "executor.sock",
                ingress_uid=self.uid, ingress_gid=os.getegid(),
                owner_uid=self.fake_owner_uid,
            )
        self.sock.symlink_to(self.parent / "missing")
        with self.assertRaises(DashboardRelayError):
            self.make_netless()
        self.assertTrue(self.sock.is_symlink())

    def test_netless_wrong_ingress_uid_cannot_access_backend(self) -> None:
        relay = self.make_netless()
        relay.start()
        with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as conn:
            conn.settimeout(1)
            conn.connect(str(self.sock))
            conn.sendall(b"GET /readyz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            try:
                self.assertEqual(conn.recv(1), b"")
            except ConnectionResetError:
                pass  # The rejected peer never reaches the trusted backend.
        self.assertTrue(relay.stop())
        self.assertFalse(self.sock.exists())

    def test_ingress_binds_only_exact_ipv4_loopback_and_other_uid(self) -> None:
        with self.assertRaises(DashboardRelayError):
            TrustedDashboardPublicIngress(
                socket_path=self.sock, frontend_uid=self.uid,
                listen_host="127.0.0.1", listen_port=8875,
            )
        with self.assertRaises(DashboardRelayError):
            TrustedDashboardPublicIngress(
                socket_path=self.sock, frontend_uid=self.fake_ingress_uid,
                listen_host="0.0.0.0", listen_port=8875,
            )

    def test_public_ingress_rebinds_after_http_and_never_double_binds(self) -> None:
        # Emulate a legitimate short-lived HTTP connection, then immediate
        # customer restart. Only the TCP bind is synthesized: the Unix socket
        # identity rules remain covered by independent UID tests.
        self.assertTrue(TrustedDashboardPublicIngress.allow_reuse_address)
        with patch.object(TrustedDashboardPublicIngress, "_validate_socket"):
            first = TrustedDashboardPublicIngress(
                socket_path=self.sock, frontend_uid=self.fake_ingress_uid,
                listen_port=0,
            )
            port = first.server_address[1]
            first.start()
            try:
                with closing(socket.create_connection(("127.0.0.1", port),
                                                      timeout=2)) as client:
                    client.sendall(b"GET /readyz HTTP/1.1\\r\\nHost: localhost\\r\\n\\r\\n")
                    try:
                        client.recv(1)
                    except ConnectionResetError:
                        pass
                with self.assertRaises(OSError) as busy:
                    TrustedDashboardPublicIngress(
                        socket_path=self.sock, frontend_uid=self.fake_ingress_uid,
                        listen_port=port,
                    )
                self.assertEqual(busy.exception.errno, errno.EADDRINUSE)
            finally:
                self.assertTrue(first.stop())
            restarted = TrustedDashboardPublicIngress(
                socket_path=self.sock, frontend_uid=self.fake_ingress_uid,
                listen_port=port,
            )
            restarted.server_close()

    def test_public_ingress_denial_reports_only_fixed_phase(self) -> None:
        from contextlib import redirect_stderr
        from io import StringIO

        args = ["--frontend-socket", str(self.sock),
                "--frontend-uid", str(self.fake_ingress_uid)]
        for cause, expected in (
            (DashboardRelayError("untrusted frontend ingress socket identity"),
             "mark public ingress: frontend socket metadata invalid"),
            (OSError(errno.EACCES, "synthetic sensitive path and secret"),
             "mark public ingress: localhost bind permission denied"),
            (OSError(errno.EADDRINUSE, "synthetic sensitive path and secret"),
             "mark public ingress: localhost port occupied"),
            (OSError(errno.EADDRNOTAVAIL, "synthetic sensitive path and secret"),
             "mark public ingress: localhost address unavailable"),
            (OSError("synthetic sensitive path and secret"),
             "mark public ingress: localhost listener unavailable"),
        ):
            with self.subTest(expected=expected):
                output = StringIO()
                with patch("mark_api.dashboard_relay.container_draft_denied",
                           return_value=False), patch(
                    "mark_api.dashboard_relay.TrustedDashboardPublicIngress",
                    side_effect=cause,
                ), redirect_stderr(output):
                    self.assertEqual(relay_public_ingress_main(args), 2)
                self.assertEqual(output.getvalue().strip(), expected)
                self.assertNotIn("sensitive", output.getvalue())

    def test_new_direct_python_mains_preserve_unattested_draft_stop(self) -> None:
        with patch("mark_api.dashboard_relay.container_draft_denied", return_value=True):
            self.assertEqual(relay_netless_frontend_main([]), 2)
            self.assertEqual(relay_public_ingress_main([]), 2)


if __name__ == "__main__":
    unittest.main()