"""Single protected customer launcher: role wiring and fail-closed boundaries.

The installed root/namespace/CDP acceptance is separately tested in disposable
PID/net namespaces; these tests do not confer host cutover or restore rights.
"""
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

from mark_api import protected_product as product


class ProtectedProductTests(unittest.TestCase):
    def test_nonroot_cannot_create_state_or_start_a_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "must-not-create"
            with patch("mark_api.protected_product._spawn") as child, patch(
                "mark_api.protected_product._initialize",
            ) as initialize, redirect_stderr(io.StringIO()) as stderr:
                result = product.main(["--state-root", str(path), "--init-new"])
            self.assertEqual(result, 2)
            self.assertFalse(path.exists())
            initialize.assert_not_called()
            child.assert_not_called()
            self.assertNotIn(str(path), stderr.getvalue())

    def test_wrong_cli_parameters_refuse_start_before_side_effects(self):
        with patch("mark_api.protected_product._root_admission"):
            for altered in (
                {"cdp_port": True}, {"cdp_port": 0},
                {"sync_interval": 59}, {"sync_interval": 86401},
                {"sync_interval": False},
                {"cdp_port": 443, "enable_generation": True},
                {"enable_generation": 1},
            ):
                with self.subTest(altered=altered), patch(
                    "mark_api.protected_product._root_path",
                ) as root:
                    with self.assertRaises(product.ProtectedProductError):
                        product.run(root=Path("/tmp/new-customer-product"), **altered)
                    root.assert_not_called()

    def test_readiness_rejects_wrong_uid_noncanonical_or_extra_fields(self):
        def role(text):
            rd, wr = os.pipe()
            try:
                os.write(wr, text)
            finally:
                os.close(wr)
            source = os.fdopen(rd, "rb", buffering=0)
            self.addCleanup(source.close)
            class Child:
                stdout = source
                def poll(self):
                    return None
            return Child()
        for reply in (
            {"ready": True, "uid": True},
            {"ready": True, "uid": 50123, "extra": True},
            {"ready": True, "uid": 50042, "port": True},
            {"ready": False, "uid": 50042},
            {"ready": True, "uid": 50043},
        ):
            with self.subTest(reply=reply), self.assertRaises(
                product.ProtectedProductError,
            ):
                product._ready(role(json.dumps(reply).encode()+b"\n"), 50042)
        self.assertEqual(
            product._ready(role(b'{"ready":true,"uid":50042,"port":8765}\n'), 50042)["port"],
            8765,
        )

    def test_exact_privileged_role_wiring_with_private_network_and_unix_ingress(self):
        children = []
        configs = []

        class Child:
            def __init__(self, name):
                self.name = name
                self.returncode = None
                self.stdout = io.BytesIO()
            def poll(self):
                return None
            def wait(self, timeout=None):
                self.returncode = 0
            def send_signal(self, sig):
                self.returncode = 0
            def kill(self):
                self.returncode = 2

        def spawn(module, uid, *, net=None, config=None, args=(), ingress=False):
            name = module.rsplit(".", 1)[-1]
            configs.append((name, uid, net, config, args, ingress))
            proc = Child(name)
            children.append(proc)
            return proc

        def ready(child, uid):
            return {"ready": True, "uid": uid, "port": 18333 if uid == product.OWNER else 18444}

        # Simulate a user shutdown after all five protected role processes
        # reached readiness. Every actual role child is still installed/exec'd
        # in the independent real multi-UID acceptance.
        def mark_stop(_duration):
            if len(children) == 5:
                raise KeyboardInterrupt("synthetic shutdown")

        root = Path("/tmp/synthetic-owned-product")
        with patch("mark_api.protected_product._root_admission"), patch(
            "mark_api.protected_product._root_path"
        ), patch("mark_api.protected_product._uids_free"), patch(
            "mark_api.protected_product._attest_existing"
        ), patch("mark_api.protected_product._lock", return_value=os.open(os.devnull, os.O_RDONLY)
        ), patch("mark_api.protected_product._private_network",
                  side_effect=lambda: os.open(os.devnull, os.O_RDONLY)
        ), patch("mark_api.protected_product._spawn", side_effect=spawn), patch(
            "mark_api.protected_product._ready", side_effect=ready
        ), patch("mark_api.protected_product._ready_ingress"), patch(
            "mark_api.protected_product.sleep", side_effect=mark_stop
        ), patch("signal.signal", return_value=signal.SIG_DFL), redirect_stdout(
            io.StringIO()
        ) as stdout:
            with self.assertRaises(KeyboardInterrupt):
                product.run(root=root, sync_interval=300)

        self.assertEqual([role[0] for role in configs], [
            "protected_witness", "protected_actor", "protected_owner",
            "protected_frontend", "dashboard_relay",
        ])
        for index in (0, 2, 3):
            self.assertIsInstance(configs[index][2], int)
        for index in (1, 4):
            self.assertIsNone(configs[index][2])
        self.assertEqual(configs[4][5], True)
        self.assertEqual(configs[2][3]["sync_interval_seconds"], 300)
        self.assertTrue(configs[1][3]["media_enabled"])
        self.assertEqual(configs[3][3]["owner_port"], 18333)
        self.assertEqual(configs[3][3]["ingress_uid"], product.INGRESS)
        self.assertEqual(configs[2][3]["bearer_token"],
                         configs[3][3]["api_token"])
        self.assertNotEqual(configs[3][3]["api_token"],
                            configs[3][3]["ui_token"])
        for _, _, _, config, args, _ in configs:
            if config is not None:
                self.assertFalse(any(token in str(args)
                                     for token in (configs[2][3]["bearer_token"],
                                                   configs[3][3]["ui_token"])))
        self.assertIn("#write_token=", stdout.getvalue())
        self.assertNotIn(configs[2][3]["bearer_token"], stdout.getvalue())

    def test_optin_missing_provider_fails_before_customer_state_creation(self):
        with patch("mark_api.protected_product._root_admission"), patch.dict(
            os.environ, {"OPENAI_API_KEY": ""}
        ), patch("mark_api.protected_product._initialize") as initialize, patch(
            "mark_api.protected_product._root_path"
        ) as path, patch("mark_api.protected_product._spawn") as child:
            with self.assertRaisesRegex(
                product.ProtectedProductError, "generation credential unavailable",
            ):
                product.run(root=Path("/tmp/synthetic-new-product"),
                            init_new=True, enable_generation=True)
            initialize.assert_not_called()
            path.assert_not_called()
            child.assert_not_called()

    def test_optin_launch_wires_sixth_uid_only_host_network_for_provider(self):
        configs = []
        children = []

        class Child:
            def __init__(self, name):
                self.name = name
                self.returncode = None
                self.stdout = io.BytesIO()
            def poll(self):
                return None
            def wait(self, timeout=None):
                self.returncode = 0
            def send_signal(self, sig):
                self.returncode = 0
            def kill(self):
                self.returncode = 2

        def spawn(module, uid, *, net=None, config=None, args=(), ingress=False):
            name = module.rsplit(".", 1)[-1]
            configs.append((name, uid, net, config, args, ingress))
            proc = Child(name)
            children.append(proc)
            return proc

        def ready(child, uid):
            return {"ready": True, "uid": uid,
                    "port": 18333 if uid == product.OWNER else 18444}

        def mark_stop(_duration):
            if len(children) == 6:
                raise KeyboardInterrupt("synthetic shutdown")

        root = Path("/tmp/synthetic-protected-optin")
        with patch("mark_api.protected_product._root_admission"), patch(
            "mark_api.protected_product._generation_admission",
            return_value="synthetic-nonbillable-provider-key",
        ), patch("mark_api.protected_product._root_path"), patch(
            "mark_api.protected_product._uids_free"
        ) as census, patch("mark_api.protected_product._attest_existing"), patch(
            "mark_api.protected_product._generation_directory"
        ) as directory, patch(
            "mark_api.protected_product._lock",
            side_effect=lambda _root: os.open(os.devnull, os.O_RDONLY),
        ), patch("mark_api.protected_product._private_network",
                 side_effect=lambda: os.open(os.devnull, os.O_RDONLY)), patch(
            "mark_api.protected_product._spawn", side_effect=spawn,
        ), patch("mark_api.protected_product._ready", side_effect=ready), patch(
            "mark_api.protected_product._ready_ingress",
        ), patch("mark_api.protected_product.sleep",
                 side_effect=mark_stop), patch(
            "signal.signal", return_value=signal.SIG_DFL,
        ), redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(KeyboardInterrupt):
                product.run(root=root, sync_interval=0, enable_generation=True)

        census.assert_called_once_with(include_generator=True)
        directory.assert_called_once_with(root)
        self.assertEqual([x[0] for x in configs], [
            "protected_witness", "protected_actor", "protected_owner",
            "protected_generation", "protected_frontend", "dashboard_relay",
        ])
        generator, frontend = configs[3], configs[4]
        self.assertEqual(generator[1], product.GENERATOR)
        self.assertIsNone(generator[2])
        self.assertIsInstance(frontend[2], int)
        self.assertEqual(generator[3]["generation_key"],
                         "synthetic-nonbillable-provider-key")
        self.assertEqual(generator[3]["frontend_uid"], product.FRONTEND)
        self.assertEqual(frontend[3]["generator_uid"], product.GENERATOR)
        self.assertEqual(frontend[3]["generation_socket"],
                         generator[3]["socket"])
        self.assertNotIn("generation_key", frontend[3])
        self.assertIn("#write_token=", stdout.getvalue())
        self.assertIn("&generation_token=", stdout.getvalue())
        self.assertNotIn(generator[3]["generation_key"], stdout.getvalue())
        self.assertNotIn(generator[3]["generation_key"], str(generator[4]))
        self.assertNotIn(generator[3]["generation_key"], str(frontend[3]))
        self.assertNotEqual(frontend[3]["generation_token"],
                            frontend[3]["ui_token"])

    def test_noncanonical_state_root_fails_closed_before_db_creation(self):
        for raw in ("relative/data", "/tmp/../var/lib/test", "/tmp/../../var/lib/test"):
            with self.subTest(path=raw), self.assertRaises(
                product.ProtectedProductError,
            ):
                product._root_path(Path(raw), fresh=True)

    def test_landlock_never_grants_non_ingress_uid_connect(self):
        with patch("mark_api.protected_product.os.geteuid", return_value=50042):
            with self.assertRaises(product.ProtectedProductError):
                product._ingress_landlock()


    def test_generator_landlock_requires_exact_nonroot_generator_uid(self):
        with patch("mark_api.protected_product.os.geteuid",
                   return_value=product.FRONTEND):
            with self.assertRaisesRegex(
                product.ProtectedProductError, "wrong generator UID",
            ):
                product._generator_egress_landlock()

    def test_concurrently_exiting_child_never_masks_startup_failure(self):
        class RacedChild:
            def __init__(self):
                self.stdout = io.BytesIO(b"")
                self.kills = 0

            def poll(self):
                return None

            def send_signal(self, _signal):
                raise ProcessLookupError("child exited during SIGTERM")

            def kill(self):
                self.kills += 1

            def wait(self, timeout=None):
                self.returncode = 2
                return 2

        witness = RacedChild()
        self.assertFalse(product._stop([("witness", witness)]))
        self.assertTrue(witness.stdout.closed)

    def test_unexpected_role_readiness_exception_is_sanitized_and_drained(self):
        import unittest.mock as mock
        children = []

        class Child:
            stdout = io.BytesIO(b"")
            returncode = 2

            def poll(self):
                return 2

            def wait(self, timeout=None):
                return 2

        child = Child()
        with patch("mark_api.protected_product._root_admission"), patch(
            "mark_api.protected_product._root_path",
        ), patch(
            "mark_api.protected_product._uids_free",
        ), patch(
            "mark_api.protected_product._attest_existing",
        ), patch(
            "mark_api.protected_product._lock", return_value=os.open(os.devnull, os.O_RDONLY),
        ), patch(
            "mark_api.protected_product._private_network",
            side_effect=lambda: os.open(os.devnull, os.O_RDONLY),
        ), patch(
            "mark_api.protected_product._spawn", return_value=child,
        ), patch(
            "mark_api.protected_product._ready",
            side_effect=OSError("synthetic private token must never leak"),
        ), patch(
            "signal.signal", return_value=signal.SIG_DFL,
        ):
            with self.assertRaisesRegex(
                product.ProtectedProductError, "witness readiness unavailable",
            ) as raised:
                product.run(root=Path("/tmp/disposable"), sync_interval=0)
        self.assertNotIn("private token", str(raised.exception))


    def test_trusted_ingress_failure_reports_only_fixed_socket_reason(self):
        class ExitedIngress:
            def __init__(self, raw):
                self.stderr = io.BytesIO(raw)
            def poll(self):
                return 2

        for data, phase in (
            (b"mark public ingress: verified frontend unavailable\n",
             "ingress frontend socket identity unavailable"),
            (b"mark public ingress: frontend socket stat unavailable\n",
             "ingress frontend socket stat unavailable"),
            (b"mark public ingress: frontend socket metadata invalid\n",
             "ingress frontend socket metadata invalid"),
            (b"mark public ingress: frontend identity invalid\n",
             "ingress frontend identity invalid"),
            (b"mark public ingress: localhost listener unavailable\n",
             "ingress localhost listener unavailable"),
            (b"mark public ingress: localhost bind permission denied\n",
             "ingress localhost bind permission denied"),
            (b"mark public ingress: localhost port occupied\n",
             "ingress localhost port occupied"),
            (b"mark public ingress: localhost address unavailable\n",
             "ingress localhost address unavailable"),
            (b"mark public ingress: frontend Unix connect unavailable\n",
             "ingress frontend Unix connect unavailable"),
            (b"mark public ingress: frontend peer identity invalid\n",
             "ingress frontend peer identity invalid"),
            (b"mark public ingress: fixed localhost port 8875 required\n",
             "ingress fixed port refused"),
            (b"synthetic secret api bearer should never be reported\n",
             "trusted HTTP ingress unavailable"),
        ):
            with self.subTest(reason=phase), self.assertRaises(
                product.ProtectedProductError
            ) as error:
                product._ready_ingress(ExitedIngress(data))
            self.assertEqual(str(error.exception), phase)
            self.assertNotIn("bearer", str(error.exception))


    def test_unexpected_runtime_exception_never_exposes_secret_details(self):
        with patch(
            "mark_api.protected_product.run",
            side_effect=OSError("synthetic db location and bearer secret"),
        ), redirect_stderr(io.StringIO()) as err:
            result = product.main(["--state-root", "/tmp/no-customer-data"])
        self.assertEqual(result, 2)
        self.assertIn("internal OSError", err.getvalue())
        self.assertNotIn("db location", err.getvalue())
        self.assertNotIn("bearer secret", err.getvalue())
        self.assertNotIn("/tmp/no-customer-data", err.getvalue())


    def test_real_readiness_probe_closes_http_connection_without_context_protocol(self):
        class HTTPClient:
            closed = False
            def request(self, method, path):
                self.seen = (method, path)
            def getresponse(self):
                return self
            status = 200
            def read(self, limit=None):
                return b'{"ready":true}'
            def close(self):
                self.closed = True

        class Ingress:
            def poll(self):
                return None

        connection = HTTPClient()
        self.assertFalse(hasattr(connection, "__enter__"))
        with patch("mark_api.protected_product.HTTPConnection", return_value=connection):
            self.assertIsNone(product._ready_ingress(Ingress()))
        self.assertTrue(connection.closed)
        self.assertEqual(connection.seen, ("GET", "/readyz"))


if __name__ == "__main__":
    unittest.main()