"""Private A6 frontend config and fail-closed admission regression tests."""
import io
import json
import os
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from http.client import HTTPConnection
from pathlib import Path
import struct
import zlib
from threading import RLock, Thread
from types import ModuleType
from unittest.mock import MagicMock, patch

from mark_api.completion_dashboard import CompletionDashboardClient
from mark_api.dashboard import DashboardGenerationAccess
from mark_api.generation import AdvertisementDraft
from mark_api.protected_frontend import _read_private_config, main, create_protected_frontend_server
from mark_api.storage_owner import StorageOwnerError

_CONFIG = {
    "owner_socket": "/tmp/no-owner-on-this-test.sock", "owner_uid": os.geteuid()+100,
    "principal": "synthetic-owner", "owner_port": 18876,
    "api_token": "private-api-token-safe", "ui_token": "private-ui-token-safe",
    "dashboard_port": 0,
}


class ProtectedFrontendTests(unittest.TestCase):
    def _pipe(self, data):
        r, w = os.pipe()
        try:
            os.write(w, data)
        finally:
            os.close(w)
        self.addCleanup(os.close, r)
        return r

    def test_real_pathlib_socket_reaches_verified_owner_handshake(self):
        # PosixPath is the concrete type returned by Path(...). The factory
        # must not deny it before the kernel/owner verification can run.
        with patch(
            "mark_api.protected_frontend.CompletionDashboardClient",
            side_effect=StorageOwnerError("synthetic absent owner"),
        ) as authenticated:
            with self.assertRaises(StorageOwnerError):
                create_protected_frontend_server(
                    owner_socket=Path(_CONFIG["owner_socket"]),
                    owner_uid=_CONFIG["owner_uid"],
                    principal=_CONFIG["principal"],
                    owner_port=_CONFIG["owner_port"],
                    api_token=_CONFIG["api_token"],
                    ui_token=_CONFIG["ui_token"],
                    dashboard_port=0,
                )
            authenticated.assert_called_once_with(
                Path(_CONFIG["owner_socket"]),
                expected_owner_uid=_CONFIG["owner_uid"],
                expected_principal=_CONFIG["principal"],
            )

    def test_valid_strict_configuration_from_inherited_pipe(self):
        cfg = dict(_CONFIG)
        self.assertEqual(_read_private_config(
            self._pipe((json.dumps(cfg)+"\n").encode())), cfg)

    def test_malformed_duplicate_unsafe_and_oversized_inputs_deny(self):
        cases = [
            b'{"owner_uid":NaN}\n', b'{"owner_uid":1,"owner_uid":2}\n',
            b"{}\n{}\n", b"{}\n"+b"x"*8192, b"{}",
            (json.dumps({**_CONFIG, "sql": "DELETE"})+"\n").encode(),
            (json.dumps({**_CONFIG, "owner_uid": True})+"\n").encode(),
            (json.dumps({**_CONFIG, "owner_socket": "relative"})+"\n").encode(),
        ]
        for raw in cases:
            with self.subTest(data=raw[:35]), self.assertRaises(ValueError):
                _read_private_config(self._pipe(raw))

    def test_public_config_file_denied(self):
        with tempfile.TemporaryFile() as f:
            f.write((json.dumps(_CONFIG)+"\n").encode())
            f.flush()
            os.fchmod(f.fileno(), 0o644)
            f.seek(0)
            with self.assertRaises(ValueError):
                _read_private_config(f.fileno())

    def test_missing_attested_owner_fails_without_exposing_bearers(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = main(["--config-fd", str(self._pipe(
                (json.dumps(_CONFIG)+"\n").encode()))])
        self.assertEqual(result, 2)
        self.assertIn("unavailable (owner unavailable)", buf.getvalue())
        self.assertNotIn(_CONFIG["api_token"], buf.getvalue())
        self.assertNotIn(_CONFIG["ui_token"], buf.getvalue())


    def test_optional_generation_token_is_strict_and_backward_compatible(self):
        token = "private-generation-token-1234567890"
        cfg = {**_CONFIG, "generation_token": token}
        self.assertEqual(
            _read_private_config(self._pipe((json.dumps(cfg) + "\n").encode())),
            cfg,
        )
        for invalid in (None, False, 1, "", "x" * 15, "x" * 513,
                        "line\nfeed" * 3, "unicode-ü-token-123456789"):
            with self.subTest(token=repr(invalid)[:20]), self.assertRaises(ValueError):
                _read_private_config(self._pipe((
                    json.dumps({**cfg, "generation_token": invalid}) + "\n"
                ).encode()))

    def test_worker_generation_config_rejects_other_uid_and_unexpected_fields(self):
        from mark_api.protected_generation import ProtectedGenerationClient

        cfg = {**_CONFIG,
               "generation_token": "safe-generation-session-token-1234567890",
               "generation_socket": "/tmp/synthetic-generation-worker.sock",
               "generator_uid": os.geteuid() + 104}
        self.assertEqual(_read_private_config(self._pipe(
            (json.dumps(cfg) + "\n").encode())), cfg)
        for variant in (
            {**cfg, "generator_uid": os.geteuid()},
            {**cfg, "generator_uid": cfg["owner_uid"]},
            {**cfg, "generator_uid": True},
            {**cfg, "generation_socket": "relative/worker.sock"},
            {**cfg, "generation_socket": "/tmp/../tmp/worker.sock"},
            {**cfg, "generation_socket": cfg["owner_socket"]},
            {**cfg, "generator_uid": 0},
            {**cfg, "unexpected": "injected"},
        ):
            with self.subTest(variant=repr(variant)[:80]), self.assertRaises(ValueError):
                _read_private_config(self._pipe((json.dumps(variant) + "\n").encode()))

    def test_worker_generation_never_requires_secret_or_sdk_in_netless_frontend(self):
        from mark_api.protected_generation import ProtectedGenerationClient
        cfg = {**_CONFIG,
               "generation_token": "safe-generation-session-token-1234567890",
               "generation_socket": "/tmp/synthetic-generation-worker.sock",
               "generator_uid": os.geteuid() + 104}
        server = MagicMock(server_port=18875)
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), patch(
            "mark_api.protected_frontend.create_protected_frontend_server",
            return_value=server,
        ) as compose, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["--config-fd", str(self._pipe(
                (json.dumps(cfg) + "\n").encode()))]), 0)
        access = compose.call_args.kwargs["generation_access"]
        self.assertIsInstance(access, DashboardGenerationAccess)
        self.assertEqual(access.ui_token, cfg["generation_token"])
        delegated = access._factory()
        self.assertIsInstance(delegated, ProtectedGenerationClient)
        self.assertEqual(delegated.worker_uid, cfg["generator_uid"])
        self.assertEqual(str(delegated.path), cfg["generation_socket"])
        self.assertNotIn(cfg["generation_token"], out.getvalue())
        server.serve_forever.assert_called_once()
        server.server_close.assert_called_once()


    def test_protected_generation_uses_existing_dashboard_gate(self):
        token = "synthetic-generation-session-987654321"
        access = DashboardGenerationAccess(ui_token=token)
        stub_server = object()
        with patch("mark_api.protected_frontend.CompletionDashboardClient") as owner, patch(
            "mark_api.protected_frontend.create_server", return_value=stub_server
        ) as create:
            result = create_protected_frontend_server(
                owner_socket=Path(_CONFIG["owner_socket"]),
                owner_uid=_CONFIG["owner_uid"], principal=_CONFIG["principal"],
                owner_port=_CONFIG["owner_port"], api_token=_CONFIG["api_token"],
                ui_token=_CONFIG["ui_token"], dashboard_port=0,
                generation_access=access,
            )
        self.assertIs(result, stub_server)
        owner.assert_called_once()
        self.assertIs(create.call_args.kwargs["generation_access"], access)
        proxy = create.call_args.kwargs["write_proxy"]
        self.assertTrue(proxy.owner_reconciliation_enabled)
        self.assertFalse(proxy.local_reconciliation_enabled)

    def test_generation_without_explicit_credential_does_not_start_owner(self):
        cfg = {**_CONFIG, "generation_token": "safe-generation-token-1234567890"}
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), patch(
            "mark_api.protected_frontend.create_protected_frontend_server"
        ) as owner, redirect_stderr(io.StringIO()) as err:
            result = main(["--config-fd", str(self._pipe(
                (json.dumps(cfg) + "\n").encode()
            ))])
        self.assertEqual(result, 2)
        self.assertIn("generation credential missing", err.getvalue())
        self.assertNotIn(cfg["generation_token"], err.getvalue())
        owner.assert_not_called()

    def test_generation_explicit_opt_in_wires_token_without_provider_call(self):
        cfg = {**_CONFIG, "generation_token": "safe-generation-token-1234567890"}
        sdk = ModuleType("openai")
        sdk.OpenAI = MagicMock()
        server = MagicMock(server_port=18875)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-not-used"}), patch.dict(
            "sys.modules", {"openai": sdk}
        ), patch(
            "mark_api.protected_frontend.create_protected_frontend_server",
            return_value=server,
        ) as compose, redirect_stdout(io.StringIO()) as stdout:
            result = main(["--config-fd", str(self._pipe(
                (json.dumps(cfg) + "\n").encode()
            ))])
        self.assertEqual(result, 0)
        self.assertIsInstance(compose.call_args.kwargs["generation_access"], DashboardGenerationAccess)
        self.assertEqual(compose.call_args.kwargs["generation_access"].ui_token, cfg["generation_token"])
        self.assertNotIn(cfg["generation_token"], stdout.getvalue())
        self.assertNotIn(cfg["api_token"], stdout.getvalue())
        sdk.OpenAI.assert_not_called()
        server.serve_forever.assert_called_once()
        server.server_close.assert_called_once()


    def test_real_http_generation_via_protected_frontend(self):
        # Actual HTTP route with the exact protected-client type. The genuine
        # independent UID/socket admission is tested by the installed A6 suite.
        owner = CompletionDashboardClient.__new__(CompletionDashboardClient)
        owner._pid, owner._uid = os.getpid(), os.geteuid()
        owner.expected_owner_uid = os.geteuid() + 101
        owner.principal = _CONFIG["principal"]
        owner._dashboard_guard = RLock()
        owner.verify_completion_profile = lambda: None
        generator = MagicMock()
        generator.generate_text.return_value = AdvertisementDraft(
            title="Fahrrad 28 Zoll", description="Ein eigener Artikel"
        )
        def chunk(tag, payload):
            return (struct.pack(">I", len(payload)) + tag + payload
                    + struct.pack(">I", zlib.crc32(tag + payload) & 0xffffffff))
        ihdr = struct.pack(">IIBBBBB", 1024, 1024, 8, 6, 0, 0, 0)
        raster = (b"\x00" + b"\x00" * 4096) * 1024
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
               + chunk(b"IDAT", zlib.compress(raster)) + chunk(b"IEND", b""))
        generator.generate_image.return_value = png
        token = "synthetic-http-generation-token-123456"
        access = DashboardGenerationAccess(
            ui_token=token, generator_factory=lambda: generator, max_requests=2
        )
        with patch("mark_api.protected_frontend.CompletionDashboardClient",
                   return_value=owner):
            server = create_protected_frontend_server(
                owner_socket=Path(_CONFIG["owner_socket"]),
                owner_uid=owner.expected_owner_uid, principal=owner.principal,
                owner_port=_CONFIG["owner_port"], api_token=_CONFIG["api_token"],
                ui_token=_CONFIG["ui_token"], dashboard_port=0,
                generation_access=access,
            )
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            port = server.server_port
            with closing(HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
                connection.request("GET", "/api/dashboard/config")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                config = json.loads(response.read())
                self.assertIs(config["write_ui_available"], True)
                self.assertIs(config["generation_ui_available"], True)
            headers = {
                "Origin": f"http://127.0.0.1:{port}",
                "Content-Type": "application/json",
                "X-Mark-Generation": "1",
                "X-Mark-Generation-Token": token,
                "Sec-Fetch-Site": "same-origin",
            }

            def request(consent, *, session_token=token, mode="text"):
                body = json.dumps({
                    "mode": mode, "prompt": "Ein Fahrrad",
                    "acknowledge_provider_cost": consent,
                }).encode()
                with closing(HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
                    connection.request(
                        "POST", "/api/dashboard/generate", body,
                        {**headers, "X-Mark-Generation-Token": session_token},
                    )
                    response = connection.getresponse()
                    if mode == "image" and response.status == 200:
                        return response.status, response.read(), response.getheader("Content-Type")
                    return response.status, json.loads(response.read())

            self.assertEqual(request(True, session_token="wrong")[0], 403)
            self.assertEqual(request(False)[0], 400)
            generator.generate_text.assert_not_called()
            status, payload = request(True)
            self.assertEqual(status, 200)
            self.assertEqual(payload["title"], "Fahrrad 28 Zoll")
            self.assertIs(payload["published"], False)
            generator.generate_text.assert_called_once_with("Ein Fahrrad")
            image_response = request(True, mode="image")
            self.assertEqual(image_response[0], 200, image_response)
            image_status, received_png, content_type = image_response
            self.assertEqual(content_type, "image/png")
            self.assertEqual(received_png, png)
            generator.generate_image.assert_called_once_with("Ein Fahrrad")
            self.assertEqual(request(True)[0], 429)
            generator.generate_text.assert_called_once()
            generator.generate_image.assert_called_once()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)
            self.assertFalse(worker.is_alive())



    def test_private_ingress_profile_is_strict_and_generation_compatible(self):
        ingress = {
            "ingress_socket": "/tmp/kleinanzeiger-test-ingress.sock",
            "ingress_uid": os.geteuid() + 201,
            "ingress_gid": os.geteuid() + 201,
        }
        for variant in (
            {**_CONFIG, **ingress},
            {**_CONFIG, **ingress,
             "generation_token": "synthetic-generation-private-session"},
        ):
            with self.subTest(variant=bool(variant.get("generation_token"))):
                self.assertEqual(_read_private_config(self._pipe(
                    (json.dumps(variant) + "\n").encode(),
                )), variant)
        for variant in (
            {**_CONFIG, "ingress_uid": ingress["ingress_uid"]},
            {**_CONFIG, **ingress, "ingress_socket": "../escape.sock"},
            {**_CONFIG, **ingress, "ingress_socket": "/tmp/../tmp/escape.sock"},
            {**_CONFIG, **ingress, "ingress_socket": _CONFIG["owner_socket"]},
            {**_CONFIG, **ingress, "ingress_uid": _CONFIG["owner_uid"]},
            {**_CONFIG, **ingress, "ingress_uid": os.geteuid()},
            {**_CONFIG, **ingress, "ingress_gid": ingress["ingress_uid"]+1},
            {**_CONFIG, **ingress, "ingress_uid": True},
            {**_CONFIG, **ingress, "ingress_gid": -1},
        ):
            with self.subTest(variant=repr(variant)[-85:]), self.assertRaises(ValueError):
                _read_private_config(self._pipe(
                    (json.dumps(variant) + "\n").encode(),
                ))

    def test_frontend_cli_reuses_real_relay_without_leaking_tokens(self):
        config = {
            **_CONFIG, "ingress_socket": "/tmp/synthetic-ingress-ipc.sock",
            "ingress_uid": os.geteuid() + 201,
            "ingress_gid": os.geteuid() + 201,
        }
        dashboard = MagicMock()
        dashboard.server_port = 17772
        relay = MagicMock()
        relay._serving.is_alive.return_value = True
        relay.stop.return_value = True
        with patch("mark_api.protected_frontend.create_protected_frontend_server",
                   return_value=dashboard) as owner, patch(
                   "mark_api.dashboard_relay.UnixDashboardRelay",
                   return_value=relay) as relay_factory, patch(
                   "signal.signal",
                   ) as sig, redirect_stdout(io.StringIO()) as out:
            status = main(["--config-fd", str(self._pipe(
                (json.dumps(config) + "\n").encode(),
            ))])
        self.assertEqual(status, 0)
        self.assertEqual(owner.call_args.kwargs["public_origin_port"], 8875)
        relay_factory.assert_called_once_with(
            socket_path=Path(config["ingress_socket"]),
            frontend_uid=config["ingress_uid"],
            frontend_gid=config["ingress_gid"],
            dashboard_host="127.0.0.1", dashboard_port=dashboard.server_port,
        )
        relay.start.assert_called_once_with()
        relay.stop.assert_called_once()
        self.assertNotIn(config["api_token"], out.getvalue())
        self.assertNotIn(config["ui_token"], out.getvalue())
        self.assertNotIn(config["ingress_socket"], out.getvalue())

    def test_ingress_bind_failure_closes_dashboard_without_reporting_ready(self):
        config = {
            **_CONFIG, "ingress_socket": "/tmp/synthetic-ingress-ipc.sock",
            "ingress_uid": os.geteuid() + 201,
            "ingress_gid": os.geteuid() + 201,
        }
        dashboard = MagicMock()
        with patch("mark_api.protected_frontend.create_protected_frontend_server",
                   return_value=dashboard), patch(
                   "mark_api.dashboard_relay.UnixDashboardRelay",
                   side_effect=OSError("synthetic secret path " + config["api_token"]),
                   ), redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            status = main(["--config-fd", str(self._pipe(
                (json.dumps(config) + "\n").encode(),
            ))])
        self.assertEqual(status, 2)
        dashboard.serve_forever.assert_not_called()
        dashboard.server_close.assert_called_once_with()
        self.assertNotIn(config["api_token"], err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_real_local_http_over_uid_verified_frontend_unix_relay(self):
        import socket
        from http.client import HTTPResponse
        from mark_api.dashboard_relay import UnixDashboardRelay

        # Same-UID local functional seam; distinct kernel UIDs are verified
        # separately by the existing installed multi-UID suite.
        client = CompletionDashboardClient.__new__(CompletionDashboardClient)
        client._pid, client._uid = os.getpid(), os.geteuid()
        client.expected_owner_uid = os.geteuid() + 101
        client.principal = _CONFIG["principal"]
        client._dashboard_guard = RLock()
        client.verify_completion_profile = lambda: None
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ingress.sock"
            os.chmod(temp, 0o2710)
            with patch("mark_api.protected_frontend.CompletionDashboardClient",
                       return_value=client):
                dashboard = create_protected_frontend_server(
                    owner_socket=Path(_CONFIG["owner_socket"]),
                    owner_uid=client.expected_owner_uid,
                    principal=client.principal,
                    owner_port=_CONFIG["owner_port"],
                    api_token=_CONFIG["api_token"],
                    ui_token=_CONFIG["ui_token"],
                    dashboard_port=0, public_origin_port=8875,
                )
            relay = UnixDashboardRelay(
                socket_path=path,
                frontend_uid=os.geteuid(),
                frontend_gid=os.getegid(),
                dashboard_host="127.0.0.1",
                dashboard_port=dashboard.server_port,
            )
            server_thread = Thread(target=dashboard.serve_forever, daemon=True)
            server_thread.start()
            try:
                relay.start()
                with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as peer:
                    peer.settimeout(5)
                    peer.connect(str(path))
                    peer.sendall(
                        b"GET /api/dashboard/config HTTP/1.1\r\n"
                        b"Host: 127.0.0.1:8875\r\n"
                        b"Connection: close\r\n\r\n"
                    )
                    response = HTTPResponse(peer)
                    response.begin()
                    self.assertEqual(response.status, 200)
                    content = json.loads(response.read())
                    self.assertTrue(content["write_ui_available"])
            finally:
                self.assertTrue(relay.stop(3))
                dashboard.shutdown()
                dashboard.server_close()
                server_thread.join(3)
                self.assertFalse(server_thread.is_alive())


if __name__ == "__main__":
    unittest.main()