"""Real installed-wheel acceptance: synthetic local mail -> SQLite -> loopback HTTP.

This module deliberately imports no mark_api implementation from the checkout.
Run with MARK_WHEEL_PATH=/absolute/path/to/mark_api-*.whl to exercise the
installed distribution in a separate, isolated virtual environment.
"""
from __future__ import annotations

import csv
import errno
import hashlib
import io
import json
import os
import re
import selectors
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import closing, contextmanager
from email import policy
from email.message import EmailMessage
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


def _require_loopback_only_network_namespace() -> None:
    """Fail closed unless the Linux kernel exposes only the loopback NIC.

    CI must invoke the entire test using a fresh network namespace with only
    'lo' enabled. Environment variables, missing API keys and proxy removal
    are not an outbound-network isolation boundary.
    """
    if sys.platform != "linux":
        raise AssertionError("installed offline wheel requires Linux network namespace")
    interfaces = {name for _, name in socket.if_nameindex()}
    if interfaces != {"lo"}:
        raise AssertionError(
            "offline wheel test must run in a loopback-only Linux network "
            f"namespace; visible interfaces: {sorted(interfaces)}"
        )

    # A root-created netns with plain runuser is insecure: the CI user may
    # sudo/nsenter into the host network namespace again. Admit ONLY one of:
    # (1) a user namespace mapping inside UID 0 to a NON-ROOT host UID; or
    # (2) an ordinary non-root process with no-new-privs and ZERO capability
    #     sets (including bounding set), enforced by the Linux kernel.
    uid_maps = Path("/proc/self/uid_map").read_text(encoding="ascii").splitlines()
    fields = uid_maps[0].split() if len(uid_maps) == 1 else []
    unprivileged_userns = (
        len(fields) == 3 and fields[0] == "0"
        and fields[1].isdigit() and int(fields[1]) > 0
        and fields[2] == "1"
    )
    status_values = {}
    for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            status_values[key] = value.strip()
    cap_names = ("CapEff", "CapPrm", "CapInh", "CapBnd", "CapAmb")
    unprivileged_no_new_privs = (
        os.geteuid() != 0
        and status_values.get("NoNewPrivs") == "1"
        and all(int(status_values.get(name, "-1"), 16) == 0 for name in cap_names)
    )
    if not (unprivileged_userns or unprivileged_no_new_privs):
        raise AssertionError(
            "offline acceptance requires unprivileged userns OR "
            "non-root no-new-privs with all capabilities dropped"
        )

    # Route lookup for a reserved documentation address must be denied by the
    # kernel, not merely time out or be refused by a remote host.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        status = probe.connect_ex(("192.0.2.1", 443))
    if status != errno.ENETUNREACH:
        raise AssertionError(f"outbound route unexpectedly available: {status}")


def _private_environment(home: Path) -> dict[str, str]:
    """No account credentials, proxy redirects or checkout import paths."""
    scrub_prefixes = ("OPENAI_", "KLEINANZEIGEN_", "MARK_", "AZURE_", "AWS_")
    environment = {
        k: v for k, v in os.environ.items()
        if not k.upper().startswith(scrub_prefixes)
        and k.upper() not in {
            "PYTHONPATH", "PYTHONHOME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "FTP_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy",
            "ftp_proxy", "no_proxy",
        }
    }
    environment["HOME"] = str(home)
    environment["XDG_CONFIG_HOME"] = str(home / "config")
    environment["XDG_DATA_HOME"] = str(home / "data")
    environment["XDG_CACHE_HOME"] = str(home / "cache")
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    return environment


def _notification(message_id: str, date: str) -> bytes:
    message = EmailMessage(policy=policy.default)
    message["From"] = "Kleinanzeigen <noreply@mail.kleinanzeigen.de>"
    message["To"] = "synthetic@example.invalid"
    message["Date"] = date
    message["Message-ID"] = f"<{message_id}@chat.kleinanzeigen.de>"
    message["X-Conversation-ID"] = "wheel:local:demo"
    message["X-Message-ID"] = message_id
    message.set_content(
        "Anfrage zu deiner Anzeige\n"
        "(Anzeigennummer: 1234567890)\n"
        "https://www.kleinanzeigen.de/m-nachrichten.html?"
        "conversationId=wheel:local:demo"
    )
    return message.as_bytes(policy=policy.default)


def _command(*args: str, cwd: Path, env: dict[str, str], timeout: int = 45):
    return subprocess.run(
        list(args), cwd=cwd, env=env, capture_output=True, text=True,
        timeout=timeout, check=False,
    )


@contextmanager
def _offline_server(
    python: Path, directory: Path, env: dict[str, str], database: Path,
    emails: tuple[Path, ...], *, initialize: bool = False,
    catalogs: tuple[Path, ...] = (),
):
    argv = [
        str(python), "-u", "-I", "-m", "mark_api", "--db", str(database),
        "--port", "0",
    ]
    if initialize:
        argv.append("--init-db")
    for email in emails:
        argv.extend(["--email", str(email)])
    for catalog in catalogs:
        argv.extend(["--catalog", str(catalog)])
    stderr_path = directory / "offline-stderr.log"
    with stderr_path.open("ab") as error_output:
        process = subprocess.Popen(
            argv, cwd=directory, env=env, stdout=subprocess.PIPE,
            stderr=error_output, text=True, bufsize=1,
        )
        address = None
        selector = selectors.DefaultSelector()
        try:
            assert process.stdout is not None
            fd = process.stdout.fileno()
            os.set_blocking(fd, False)
            selector.register(fd, selectors.EVENT_READ)
            deadline = time.monotonic() + 12.0
            pending = bytearray()
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(
                        "installed offline launcher exited before HTTP ready: "
                        f"code={process.returncode}"
                    )
                remaining = max(0.0, deadline - time.monotonic())
                if not selector.select(timeout=min(0.2, remaining)):
                    continue
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                if not chunk:
                    raise AssertionError("installed offline launcher closed stdout")
                pending.extend(chunk)
                if len(pending) > 8192:
                    raise AssertionError("installed launcher emitted too much startup output")
                while b"\n" in pending:
                    raw, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    line = raw.decode("utf-8", errors="strict").strip()
                    match = re.fullmatch(
                        r"Mark offline dashboard: http://127\.0\.0\.1:([0-9]+)/",
                        line,
                    )
                    if match:
                        port = int(match.group(1))
                        if not 0 < port < 65536:
                            raise AssertionError("offline launcher emitted invalid port")
                        address = f"http://127.0.0.1:{port}"
                        break
                if address is not None:
                    break
            if address is None:
                raise AssertionError("installed offline launcher never announced loopback")
            yield address
        finally:
            selector.close()
            if process.poll() is None:
                process.terminate()
            try:
                output, _ = process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=4)
                raise AssertionError("installed offline launcher did not terminate")
            if process.returncode not in (0, -15, 143):
                raise AssertionError(
                    f"installed offline launcher abnormal shutdown: {process.returncode}; "
                    f"stdout={output[:250]}"
                )


def _request(address: str, path: str, *, method: str = "GET", data=None):
    opener = build_opener(ProxyHandler({}))
    request = Request(
        address + path, method=method, data=data,
        headers={"Accept": "application/json"},
    )
    try:
        with opener.open(request, timeout=4) as response:
            return response.status, response.headers, response.read()
    except HTTPError as exc:
        with exc:
            return exc.code, exc.headers, exc.read()


class InstalledOfflineWheelAcceptance(unittest.TestCase):
    def test_exact_installed_wheel_without_checkout_imports_or_platform_calls(self):
        wheel_setting = os.environ.get("MARK_WHEEL_PATH")
        self.assertTrue(wheel_setting, "MARK_WHEEL_PATH is required; never skip release acceptance")
        _require_loopback_only_network_namespace()
        wheel = Path(wheel_setting)
        self.assertTrue(wheel.is_absolute(), "wheel must be an absolute path")
        self.assertTrue(wheel.is_file(), "wheel must exist before acceptance")
        self.assertEqual(wheel.suffix, ".whl")
        wheel_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()

        with tempfile.TemporaryDirectory(prefix="mark-wheel-e2e-") as temporary:
            root = Path(temporary)
            env = _private_environment(root)
            venv = root / "venv"
            created = _command(
                sys.executable, "-m", "venv", str(venv), cwd=root, env=env,
                timeout=45,
            )
            self.assertEqual(created.returncode, 0, created.stderr[-1600:])
            python = venv / "bin" / "python"
            installed = _command(
                str(python), "-m", "pip", "install", "--no-index", "--no-deps",
                "--disable-pip-version-check", str(wheel), cwd=root, env=env,
                timeout=60,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr[-1600:])
            imported = _command(
                str(python), "-I", "-c",
                "import mark_api,sys;"
                "assert 'site-packages' in mark_api.__file__;"
                "assert not any(n.startswith(('mark_api.private_web','mark_api.monkrel')) "
                "for n in sys.modules);"
                "print(mark_api.__file__)",
                cwd=root, env=env,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertIn(str(venv), imported.stdout)

            database = root / "mark.sqlite"
            missing = root / "not-created.sqlite"
            refused = _command(
                str(python), "-I", "-m", "mark_api", "--db", str(missing),
                cwd=root, env=env,
            )
            self.assertEqual(refused.returncode, 2)
            self.assertFalse(missing.exists(), "missing database was created without --init-db")

            # When the installed wheel lacks the optional Private-Web
            # dependencies, the full product launcher must fail before any
            # browser/CDP or SQLite side effect, regardless of env switches.
            forbidden_db = root / "denied-platform" / "must-not-create.sqlite"
            pretend_entitlement = {
                **env,
                "MARK_PLATFORM_WRITES_ENABLED": "true",
                "MARK_PRIVATE_WEB_APPROVED": "true",
                "MARK_PROSELLERS_API_ENTITLEMENT_CONFIRMED": "true",
            }
            denied_launcher = _command(
                str(venv / "bin" / "mark-api-launch"),
                "--db", str(forbidden_db), "--init-db",
                "--cdp-port", "9222",
                "--email", str(root / "nonexistent-private-email.eml"),
                cwd=root, env=pretend_entitlement,
            )
            self.assertEqual(denied_launcher.returncode, 2, denied_launcher.stderr)
            self.assertIn("private Web runtime requires the private-web optional dependency set", denied_launcher.stderr)
            self.assertEqual(denied_launcher.stdout, "")
            self.assertFalse(forbidden_db.parent.exists())
            self.assertFalse(forbidden_db.exists())

            email_a = root / "synthetic-a.eml"
            email_b = root / "synthetic-b.eml"
            email_a.write_bytes(_notification("wheel-msg-1", "Thu, 24 Sep 2026 10:37:48 +0000"))
            email_b.write_bytes(_notification("wheel-msg-2", "Thu, 24 Sep 2026 10:38:48 +0000"))
            email_files = (email_a, email_b)
            catalog = root / "explicit-local-catalog.json"
            catalog.write_text(json.dumps({
                "schema": "mark-local-catalog-v1",
                "ads": [{
                    "ad_id": "1234567890",
                    "observed_at": "2026-10-22T02:01:00Z",
                    "title": "Locally supplied, unverified listing",
                    "views": 23,
                    "watch_count": 0,
                }],
            }), encoding="utf-8")

            with _offline_server(
                python, root, env, database, email_files,
                initialize=True, catalogs=(catalog,),
            ) as address:
                code, _, payload = _request(address, "/healthz")
                self.assertEqual((code, json.loads(payload)), (200, {"status": "ok"}))
                code, _, payload = _request(address, "/readyz")
                self.assertEqual((code, json.loads(payload)), (200, {"status": "ready"}))
                code, _, payload = _request(address, "/api/dashboard/config")
                self.assertEqual((code, json.loads(payload)), (200, {"write_ui_available": False}))
                code, _, payload = _request(address, "/")
                self.assertEqual(code, 200)
                self.assertIn(b"dashboard.js", payload)

                code, _, payload = _request(address, "/api/ads")
                self.assertEqual(code, 200)
                ads = json.loads(payload)
                self.assertEqual(len(ads), 1)
                self.assertEqual(ads[0]["ad_id"], "1234567890")
                self.assertEqual(ads[0]["views"], 23)
                self.assertEqual(ads[0]["lifecycle_state"], "unknown")
                self.assertFalse(ads[0]["present"], "unverified catalog cannot claim current owner")
                self.assertEqual(ads[0]["source"], "user-supplied-local-catalog")
                code, _, payload = _request(address, "/api/summary")
                self.assertEqual(code, 200)
                summary = json.loads(payload)
                self.assertEqual(summary["tracked_ads"], 1)
                self.assertEqual(summary["current_ads"], 0)
                self.assertEqual(summary["unknown_state_ads"], 1)
                code, _, payload = _request(address, "/api/analytics/ads?metric=views")
                self.assertEqual(code, 200)
                self.assertEqual(
                    [(row["ad_id"], row["value"]) for row in json.loads(payload)],
                    [("1234567890", 23)],
                )

                code, _, payload = _request(address, "/api/email-reactions")
                reactions = json.loads(payload)
                self.assertEqual(code, 200)
                self.assertEqual(len(reactions), 1)
                self.assertEqual(reactions[0]["ad_id"], "1234567890")
                self.assertEqual(reactions[0]["inbound_message_count"], 2)
                self.assertEqual(reactions[0]["conversation_count"], 1)

                metric = "email_inbound_message_count"
                code, _, payload = _request(address, f"/api/analytics/ads?metric={metric}")
                ranks = json.loads(payload)
                self.assertEqual(code, 200)
                self.assertEqual([(row["ad_id"], row["value"]) for row in ranks], [("1234567890", 2)])

                code, headers, payload = _request(address, f"/api/analytics/ads.csv?metric={metric}")
                self.assertEqual(code, 200)
                self.assertTrue(headers.get("Content-Type", "").startswith("text/csv"))
                self.assertTrue(payload.startswith(b"\xef\xbb\xbf"))
                rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline="")))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["ad_id"], "1234567890")
                self.assertEqual(rows[0]["value"], "2")

                for method, path, data, expected in (
                    ("POST", "/api/email-reactions", b"{}", 405),
                    ("POST", "/api/write/ads", b"{}", 405),
                    ("GET", "/api/write/delete", None, 404),
                    ("GET", "/api/dashboard/pending-writes", None, 404),
                    ("HEAD", "/healthz", None, 405),
                ):
                    with self.subTest(method=method, path=path):
                        status, _, _ = _request(address, path, method=method, data=data)
                        self.assertEqual(status, expected)

            classified = _command(
                str(venv / "bin" / "mark-api-classify"),
                "--db", str(database), "--ad-id", "1234567890",
                "--city", "Berlin", cwd=root, env=env,
            )
            self.assertEqual(classified.returncode, 0, classified.stderr)

            # Reimporting the same local files must not double-count messages.
            with _offline_server(
                python, root, env, database, email_files, catalogs=(catalog,),
            ) as address:
                code, _, payload = _request(address, "/api/email-reactions")
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(payload)[0]["inbound_message_count"], 2)
                code, _, payload = _request(address, "/api/ads")
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(payload)[0]["views"], 23)
                groups_path = (
                    "/api/analytics/groups.csv?"
                    "dimension=city&metric=email_inbound_message_count"
                )
                code, _, payload = _request(address, groups_path)
                self.assertEqual(code, 200)
                groups = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline="")))
                self.assertEqual(len(groups), 1)
                self.assertEqual(groups[0]["label"], "Berlin")
                self.assertEqual(groups[0]["metric_sum"], "2")
                code, _, payload = _request(address, "/api/dashboard/config")
                self.assertEqual((code, json.loads(payload)), (200, {"write_ui_available": False}))

            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM ad_snapshots").fetchone()[0], 1,
                    "repeated installed catalog start duplicated the observation",
                )
            # This is a second, independent database. The installed wheel
            # (not the checkout) must keep catalog-only IDs unverified, retain
            # real prior-owner ABSENT transitions and label fallback metrics
            # last-known after a newer owner status omitted them.
            owner_db = root / "synthetic-owner-and-catalog.sqlite"
            owner_catalog = root / "synthetic-owner-catalog.json"
            owner_catalog.write_text(json.dumps({
                "schema": "mark-local-catalog-v1",
                "ads": [
                    {"ad_id": ad_id, "observed_at": "2026-10-22T02:01:00Z",
                     "views": 23}
                    for ad_id in ("1234567890", "1234567891", "1234567892")
                ],
            }), encoding="utf-8")
            synthetic_owner_setup = textwrap.dedent("""
                import sys
                from pathlib import Path
                from datetime import datetime, timezone
                from mark_api.domain import AdSnapshot, LifecycleState
                from mark_api.local_catalog_import import import_local_catalog_files
                from mark_api.results import ReadResult
                from mark_api.storage import SnapshotStore

                db = SnapshotStore(Path(sys.argv[1]))
                import_local_catalog_files(db, (Path(sys.argv[2]),))
                db.append_ad_snapshot(AdSnapshot(
                    ad_id="1234567892",
                    observed_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
                    source="synthetic-confirmed-owner",
                    lifecycle_state=LifecycleState.ACTIVE,
                    title="Confirmed owner title",
                ))
                current = AdSnapshot(
                    ad_id="1234567891",
                    observed_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
                    source="synthetic-confirmed-owner",
                    lifecycle_state=LifecycleState.ACTIVE,
                )
                count = db.append_inventory_result(
                    ReadResult.success_nonempty((current,)),
                    tracked_ad_ids=db.tracked_ad_ids(),
                    observed_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
                    source="synthetic-confirmed-owner",
                )
                assert count == 2, "catalog-only ID acquired forged ABSENT"
                assert len(db.ad_history("1234567890")) == 1
                assert db.ad_history("1234567892")[-1].lifecycle_state is LifecycleState.ABSENT
            """)
            owner_prepared = _command(
                str(python), "-I", "-c", synthetic_owner_setup,
                str(owner_db), str(owner_catalog), cwd=root, env=env,
            )
            self.assertEqual(owner_prepared.returncode, 0, owner_prepared.stderr)
            with _offline_server(python, root, env, owner_db, ()) as owner_address:
                code, _, payload = _request(owner_address, "/api/ads")
                self.assertEqual(code, 200)
                owner_rows = {item["ad_id"]: item for item in json.loads(payload)}
                self.assertEqual(len(owner_rows), 3)
                catalog_only = owner_rows["1234567890"]
                self.assertEqual(catalog_only["lifecycle_state"], "unknown")
                self.assertFalse(catalog_only["present"])
                self.assertEqual(catalog_only["source"], "user-supplied-local-catalog")
                owner_current = owner_rows["1234567891"]
                self.assertTrue(owner_current["present"])
                self.assertEqual(owner_current["lifecycle_state"], "active")
                self.assertEqual(owner_current["source"], "synthetic-confirmed-owner")
                self.assertEqual(owner_current["views"], 23)
                self.assertEqual(
                    owner_current["metric_evidence"]["views"]["source"],
                    "user-supplied-local-catalog",
                )
                self.assertTrue(owner_current["metric_evidence"]["views"]["last_known"])
                owner_absent = owner_rows["1234567892"]
                self.assertFalse(owner_absent["present"])
                self.assertEqual(owner_absent["lifecycle_state"], "absent")
                self.assertEqual(owner_absent["source"], "synthetic-confirmed-owner")
                code, _, payload = _request(owner_address, "/api/summary")
                self.assertEqual(code, 200)
                summary = json.loads(payload)
                self.assertEqual(
                    (summary["tracked_ads"], summary["current_ads"],
                     summary["absent_ads"], summary["unknown_state_ads"]),
                    (3, 1, 1, 1),
                )
            with closing(sqlite3.connect(owner_db)) as owner_connection:
                local_rows = owner_connection.execute(
                    "SELECT source, lifecycle_state FROM ad_snapshots WHERE ad_id=?",
                    ("1234567890",),
                ).fetchall()
                self.assertEqual(
                    local_rows,
                    [("user-supplied-local-catalog", "unknown")],
                    "unverified catalog must not generate a sourced owner absence",
                )

            malformed_catalog = root / "malformed-catalog.json"
            malformed_catalog.write_text(json.dumps({
                "schema": "mark-local-catalog-v1",
                "ads": [{"ad_id": "1234567890", "observed_at": "no-offset"}],
            }), encoding="utf-8")
            denied_catalog = _command(
                str(venv / "bin" / "mark-api-offline"),
                "--db", str(database), "--port", "0",
                "--catalog", str(malformed_catalog),
                cwd=root, env=env,
            )
            self.assertEqual(denied_catalog.returncode, 2)
            self.assertIn("local catalog import failed", denied_catalog.stderr)
            self.assertNotIn("no-offset", denied_catalog.stderr)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM ad_snapshots").fetchone()[0], 1,
                )

            # Optional generation is still a local draft-only capability. No
            # paid provider call may start in either mode without per-call consent.
            for mode in ("text", "image"):
                image_target = root / "should-not-create.png"
                request = [
                    str(venv / "bin" / "mark-api-generate"), mode,
                    "--prompt", "synthetic offline draft",
                ]
                if mode == "image":
                    request.extend(("--output", str(image_target)))
                denied = _command(*request, cwd=root, env=env)
                self.assertEqual(denied.returncode, 2, denied.stderr)
                self.assertIn("--acknowledge-provider-cost", denied.stderr)
                self.assertIn("https://developers.openai.com/api/docs/pricing", denied.stderr)
                self.assertEqual(denied.stdout, "")
                self.assertFalse(image_target.exists())

            # The installed wheel must also complete BOTH positive draft
            # generation paths, not merely refuse requests lacking consent.
            # A fake in-process SDK is injected only in this isolated test
            # subprocess. No OpenAI dependency, API key, browser, network
            # transport or publish operation is needed or permitted.
            simulated_generation = textwrap.dedent(r"""
                import base64
                import hashlib
                import io
                import json
                import os
                import stat
                import struct
                import sys
                import zlib
                from contextlib import redirect_stderr, redirect_stdout
                from pathlib import Path
                from types import SimpleNamespace
                from unittest.mock import patch

                import mark_api
                import mark_api.generation_cli as cli
                from mark_api.generation import AdvertisementGenerator

                assert "site-packages" in Path(mark_api.__file__).parts
                assert "site-packages" in Path(cli.__file__).parts
                assert not os.environ.get("OPENAI_API_KEY")
                assert not any(name == "openai" or name.startswith("openai.")
                               for name in sys.modules)

                def chunk(tag, data):
                    checksum = zlib.crc32(tag + data) & 0xffffffff
                    return (struct.pack(">I", len(data)) + tag + data
                            + struct.pack(">I", checksum))

                # Complete PNG with a decompressed 1024x1024 RGBA raster.
                raster = (b"\x00" + b"\x00" * (1024 * 4)) * 1024
                png = (b"\x89PNG\r\n\x1a\n"
                       + chunk(b"IHDR", struct.pack(">IIBBBBB",
                              1024, 1024, 8, 6, 0, 0, 0))
                       + chunk(b"IDAT", zlib.compress(raster))
                       + chunk(b"IEND", b""))

                class FakeResponses:
                    def __init__(self):
                        self.calls = []
                        self.fail = False

                    def create(self, **arguments):
                        # The user-facing cost notice must be printed BEFORE
                        # the provider interface can be invoked.
                        assert "gpt-5.6-terra" in sys.stderr.getvalue()
                        assert "Aktuelle Preise" in sys.stderr.getvalue()
                        self.calls.append(arguments)
                        if self.fail:
                            raise RuntimeError("secret provider diagnostic")
                        return SimpleNamespace(output_text=json.dumps({
                            "title": "Geprüfter Fahrrad-Entwurf",
                            "description": "Synthetisch erzeugter Textentwurf.",
                        }))

                class FakeImages:
                    def __init__(self):
                        self.calls = []

                    def generate(self, **arguments):
                        assert "gpt-image-2.5-flare" in sys.stderr.getvalue()
                        assert "Aktuelle Preise" in sys.stderr.getvalue()
                        self.calls.append(arguments)
                        return SimpleNamespace(data=[SimpleNamespace(
                            b64_json=base64.b64encode(png).decode("ascii"),
                        )])

                provider = SimpleNamespace(
                    responses=FakeResponses(), images=FakeImages(),
                )
                generator = AdvertisementGenerator(client=provider)

                def invoke(*arguments):
                    output, error = io.StringIO(), io.StringIO()
                    with (
                        patch.object(cli, "AdvertisementGenerator",
                                     return_value=generator),
                        redirect_stdout(output),
                        redirect_stderr(error),
                    ):
                        code = cli.main(list(arguments))
                    return code, output.getvalue(), error.getvalue()

                status, output, notice = invoke(
                    "text", "--prompt", "Synthetisches Fahrrad",
                    "--acknowledge-provider-cost",
                )
                assert status == 0, (status, notice)
                text_draft = json.loads(output)
                assert text_draft == {
                    "title": "Geprüfter Fahrrad-Entwurf",
                    "description": "Synthetisch erzeugter Textentwurf.",
                    "status": "draft",
                    "published": False,
                }
                assert len(provider.responses.calls) == 1
                assert provider.responses.calls[0]["input"] == "Synthetisches Fahrrad"
                assert provider.responses.calls[0]["model"] == "gpt-5.6-terra"
                assert provider.responses.calls[0]["text"]["format"]["type"] == "json_schema"

                # GitHub CI drops all capabilities as a real non-root UID;
                # locally, unprivileged userns maps only its inside UID 0.
                # In the latter mode, host-owned / and /tmp look foreign.
                # Use an owned private chroot ONLY in that rootless-userns
                # test environment; never weaken production path checks.
                # Both modes retain the kernel loopback-only network ns.
                private_root = Path(sys.argv[1])
                assert private_root.is_dir()
                assert private_root.stat().st_uid == os.geteuid()
                assert stat.S_IMODE(private_root.stat().st_mode) == 0o700
                uid_map = Path("/proc/self/uid_map").read_text().splitlines()
                fields = uid_map[0].split() if len(uid_map) == 1 else []
                rootless_userns = (
                    os.geteuid() == 0 and len(fields) == 3
                    and fields[0] == "0"
                    and fields[1].isdigit() and int(fields[1]) > 0
                    and fields[2] == "1"
                )
                if rootless_userns:
                    os.chdir(private_root)
                    os.chroot(private_root)
                    os.chdir("/")
                    assert Path("/").stat().st_uid == os.geteuid()
                    target = Path("/positive-synthetic-draft.png")
                else:
                    # Host root-owned ancestors have real UID 0, so the
                    # original strictly checked installed output path works.
                    assert os.geteuid() != 0
                    target = private_root / "positive-synthetic-draft.png"
                assert not target.exists()
                status, output, notice = invoke(
                    "image", "--prompt", "Synthetisches Fahrradbild",
                    "--acknowledge-provider-cost", "--output", str(target),
                )
                assert status == 0, (status, notice)
                image_draft = json.loads(output)
                assert image_draft["status"] == "draft"
                assert image_draft["published"] is False
                assert image_draft["image_sha256"] == hashlib.sha256(png).hexdigest()
                assert image_draft["image_path"] == str(target)
                assert image_draft["path_binding"] == "verified_at_write_only"
                assert image_draft["persistent_path_guarantee"] is False
                assert target.read_bytes() == png
                assert stat.S_IMODE(target.stat().st_mode) == 0o600
                assert len(provider.images.calls) == 1
                assert provider.images.calls[0]["model"] == "gpt-image-2.5-flare"
                assert provider.images.calls[0]["size"] == "1024x1024"
                assert provider.images.calls[0]["output_format"] == "png"

                # The next explicit request may not clobber an existing
                # destination or reach the provider before reservation.
                status, output, error = invoke(
                    "image", "--prompt", "No overwrite allowed",
                    "--acknowledge-provider-cost", "--output", str(target),
                )
                assert status == 2 and not output and "already exists" in error
                assert len(provider.images.calls) == 1
                assert target.read_bytes() == png

                # Injected provider failures are sanitized and never retried.
                provider.responses.fail = True
                status, output, error = invoke(
                    "text", "--prompt", "Failing synthetic text",
                    "--acknowledge-provider-cost",
                )
                assert status == 2 and not output
                assert "text generation request failed" in error
                assert "secret provider diagnostic" not in error
                assert len(provider.responses.calls) == 2

                assert not any(name == "openai" or name.startswith("openai.")
                               for name in sys.modules)
                print(json.dumps({
                    "installed_text_draft": True,
                    "installed_image_draft_png": True,
                    "private_create_only_image": True,
                    "fake_provider_called_once_per_user_request": True,
                    "provider_failure_sanitized": True,
                    "platform_writes": False,
                    "actual_provider_calls": 0,
                }, sort_keys=True))
            """)
            generation_root = root / "private-generation-root"
            generation_root.mkdir(mode=0o700)
            generation = _command(
                str(python), "-I", "-c", simulated_generation, str(generation_root),
                cwd=root, env=env, timeout=40,
            )
            self.assertEqual(generation.returncode, 0, generation.stderr[-2000:])
            self.assertEqual(json.loads(generation.stdout), {
                "installed_text_draft": True,
                "installed_image_draft_png": True,
                "private_create_only_image": True,
                "fake_provider_called_once_per_user_request": True,
                "provider_failure_sanitized": True,
                "platform_writes": False,
                "actual_provider_calls": 0,
            })

            private_generated = generation_root / "positive-synthetic-draft.png"
            self.assertTrue(private_generated.is_file())
            self.assertEqual(stat.S_IMODE(private_generated.stat().st_mode), 0o600)
            self.assertTrue(
                private_generated.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            )


            # Installed wheel must join the actual dashboard same-origin write
            # gateway to the Write API and persisted projection, using only a
            # wholly in-memory, synthetic platform double.
            simulated_dashboard_write = textwrap.dedent(r"""
                import json
                import sys
                from dataclasses import replace
                from datetime import datetime, timedelta, timezone
                from pathlib import Path
                from threading import Thread
                from urllib.error import HTTPError
                from urllib.request import ProxyHandler, Request, build_opener

                import mark_api
                from mark_api.application import MarkService
                from mark_api.dashboard import DashboardWriteProxy, create_server
                from mark_api.domain import AdSnapshot, LifecycleState
                from mark_api.results import ReadResult
                from mark_api.storage import SnapshotStore
                from mark_api.write_api import (
                    WriteApiAccess, WriteCapability, create_write_api_server,
                )

                assert "site-packages" in Path(mark_api.__file__).parts
                now = datetime(2026, 10, 4, tzinfo=timezone.utc)
                backend_token = "synthetic-installed-backend-token-0001"
                ui_token = "synthetic-installed-ui-token-0000001"

                class SyntheticPlatform:
                    def __init__(self):
                        self.ads = {"314": AdSnapshot(
                            ad_id="314", observed_at=now, source="synthetic-owner",
                            lifecycle_state=LifecycleState.ACTIVE, title="Existing",
                        )}
                        self.effects = []
                    def read_ads(self):
                        ads = tuple(self.ads.values())
                        return (ReadResult.success_nonempty(ads) if ads
                                else ReadResult.success_empty(()))
                    def update_content(self, ad_id, *, title=None, description=None):
                        prior = self.ads[ad_id]
                        self.ads[ad_id] = replace(
                            prior, title=prior.title if title is None else title,
                            description=prior.description if description is None else description,
                            observed_at=now + timedelta(seconds=len(self.effects)+1),
                        )
                        self.effects.append("update_content")
                    def set_state(self, ad_id, state):
                        self.ads[ad_id] = replace(
                            self.ads[ad_id], lifecycle_state=state,
                            observed_at=now + timedelta(seconds=len(self.effects)+1),
                        )
                        self.effects.append("set_state")
                    def create_ad(self, request):
                        self.ads["315"] = AdSnapshot(
                            ad_id="315", observed_at=now + timedelta(seconds=2),
                            source="synthetic-owner", lifecycle_state=LifecycleState.ACTIVE,
                            title=request.title, description=request.description,
                        )
                        self.effects.append("create")
                    def delete_ad(self, ad_id):
                        del self.ads[ad_id]
                        self.effects.append("delete")

                class FilterReader:
                    def __init__(self, platform, ad_id):
                        self.platform, self.ad_id = platform, ad_id
                    def read_ads(self):
                        items = tuple(x for x in self.platform.ads.values()
                                      if x.ad_id == self.ad_id)
                        return (ReadResult.success_nonempty(items) if items
                                else ReadResult.success_empty(()))

                platform = SyntheticPlatform()
                store = SnapshotStore(Path(sys.argv[1]))
                store.append_ad_snapshot(platform.ads["314"])
                service = MarkService(
                    owner_reader=platform, management_reader=platform,
                    delete_confirmation_reader=platform, reaction_reader=None,
                    state_writer=platform, delete_writer=platform,
                    content_writer=platform, create_writer=platform,
                    content_reader_factory=lambda ad_id: FilterReader(platform, ad_id),
                    store=store, writes_enabled=True, clock=lambda: now + timedelta(seconds=10),
                )
                access = WriteApiAccess(
                    principal="synthetic-wheel-only", bearer_token=backend_token,
                    capabilities=frozenset(WriteCapability) - {WriteCapability.CREATE_MEDIA}, writes_enabled=True,
                )
                backend = create_write_api_server(service, store, access, port=0)
                dashboard = create_server(
                    store, port=0, write_proxy=DashboardWriteProxy(
                        host="127.0.0.1", port=backend.server_port,
                        bearer_token=backend_token, ui_token=ui_token,
                        backend_principal=access.principal,
                        local_reconciliation_enabled=True,
                    ),
                )
                threads = []
                try:
                    for server in (backend, dashboard):
                        t = Thread(target=server.serve_forever, daemon=True)
                        t.start()
                        threads.append(t)
                    base = "http://127.0.0.1:" + str(dashboard.server_port)
                    opener = build_opener(ProxyHandler({}))
                    def call(path, *, method="GET", payload=None, key=None, authorized=True):
                        headers = {"Accept": "application/json"}
                        if method != "GET":
                            headers.update({
                                "Origin": base,
                                "X-Mark-Dashboard-Write": "1",
                                "Content-Type": "application/json",
                                "Idempotency-Key": key,
                            })
                            if authorized:
                                headers["X-Mark-Dashboard-Token"] = ui_token
                        req = Request(
                            base + path,
                            data=None if payload is None else json.dumps(payload).encode(),
                            method=method, headers=headers,
                        )
                        try:
                            res = opener.open(req, timeout=3)
                        except HTTPError as error:
                            res = error
                        with res:
                            return res.status, json.loads(res.read()), dict(res.headers)
                    assert call("/api/dashboard/config")[:2] == (
                        200, {"write_ui_available": True},
                    )
                    status, _, _ = call(
                        "/api/write/ads/314/pause", method="POST",
                        key="wheel-denied", authorized=False,
                    )
                    assert status == 403 and not platform.effects
                    data = {
                        "category_path": ["Freizeit", "Sammeln"],
                        "title": "Installed synthetic creation",
                        "description": "Only an in-memory simulated advertisement",
                        "price_eur": 10,
                    }
                    status, created, _ = call(
                        "/api/write/ads", method="POST", payload=data, key="wheel-create",
                    )
                    assert status == 200, (status, created)
                    assert created["operation_receipt"]["outcome"] == "confirmed"
                    rows = {r["ad_id"]: r for r in call("/api/ads")[1]}
                    assert rows["315"]["title"] == data["title"]
                    assert rows["314"]["lifecycle_state"] == "active"
                    status, paused, _ = call(
                        "/api/write/ads/315/pause", method="POST", key="wheel-pause",
                    )
                    assert status == 200 and paused["operation_receipt"]["outcome"] == "confirmed"
                    assert {r["ad_id"]: r for r in call("/api/ads")[1]}["315"]["lifecycle_state"] == "paused"
                    status, replay, headers = call(
                        "/api/write/ads/315/pause", method="POST", key="wheel-pause",
                    )
                    assert status == 200 and replay == paused
                    assert headers["Idempotency-Replayed"] == "true"
                    # Exact persisted replay above must not contact the backend.
                    # The installed UI then ACKs both local intents in two
                    # different, tightly bound persisted phases.
                    for key, scope in (
                        ("wheel-create", "create"),
                        ("wheel-pause", "ad:315:pause"),
                    ):
                        for finalized in (False, True):
                            ack_request = Request(
                                base + "/api/dashboard/pending-writes/ack",
                                data=json.dumps({
                                    "scope": scope, "idempotency_key": key,
                                }).encode("utf-8"),
                                method="POST",
                                headers={
                                    "Origin": base, "Content-Type": "application/json",
                                    "X-Mark-Dashboard-Write": "1",
                                    "X-Mark-Dashboard-Token": ui_token,
                                },
                            )
                            with opener.open(ack_request, timeout=3) as ack:
                                assert ack.status == 200
                                assert json.loads(ack.read()) == {
                                    "acknowledged": True, "finalized": finalized,
                                }
                    assert store.dashboard_pending_writes() == ()
                    assert platform.effects == ["create", "set_state"]
                    assert len(store.ad_history("315")) == 2
                finally:
                    for server in (dashboard, backend):
                        server.shutdown()
                        server.server_close()
                    for thread in threads:
                        thread.join(timeout=3)
                print(json.dumps({
                    "installed_dashboard_write_proxy": True,
                    "installed_write_create_and_pause": True,
                    "persisted_dashboard_projection": True,
                    "same_origin_ui_token_enforced": True,
                    "write_idempotency_replay": True,
                    "installed_local_two_phase_ack": True,
                    "synthetic_platform_only": True,
                }, sort_keys=True))
            """)
            write_ui_database = root / "synthetic-dashboard-write.sqlite"
            write_ui = _command(
                str(python), "-I", "-c", simulated_dashboard_write,
                str(write_ui_database), cwd=root, env=env, timeout=60,
            )
            self.assertEqual(write_ui.returncode, 0, write_ui.stderr[-2500:])
            self.assertEqual(json.loads(write_ui.stdout), {
                "installed_dashboard_write_proxy": True,
                "installed_write_create_and_pause": True,
                "persisted_dashboard_projection": True,
                "same_origin_ui_token_enforced": True,
                "write_idempotency_replay": True,
                "installed_local_two_phase_ack": True,
                "synthetic_platform_only": True,
            })

            # Run the actual full-CRUD/restart HTTP test against this wheel,
            # not checkout imports. This netns exposes loopback only and every
            # platform write is consumed by its stateful synthetic owner.
            crud_result = _command(
                str(python), "-I", "-m", "unittest", "discover",
                "-s", str(Path(__file__).resolve().parent),
                "-p", "test_dashboard_full_lifecycle_e2e.py", "-v",
                cwd=root, env=env, timeout=95,
            )
            self.assertEqual(crud_result.returncode, 0, crud_result.stderr[-2500:])
            self.assertRegex(
                crud_result.stderr, r"Ran [1-9][0-9]* tests? in [0-9.]+s",
            )
            self.assertIn("OK", crud_result.stderr)

            # Exercise the installed media path, not checkout imports:
            # actual loopback dashboard/upload proxy, private 0600 handle,
            # confirmed vs UNKNOWN media proof, persisted Write API receipt,
            # same-key restart replay and two-phase local ACK. The only
            # platform actor is the deterministic in-memory synthetic double.
            media_result = _command(
                str(python), "-I", "-m", "unittest", "discover",
                "-s", str(Path(__file__).resolve().parent),
                "-p", "test_dashboard_media_full_lifecycle_e2e.py", "-v",
                cwd=root, env=env, timeout=95,
            )
            self.assertEqual(media_result.returncode, 0, media_result.stderr[-2500:])
            self.assertRegex(media_result.stderr, r"Ran 3 tests? in [0-9.]+s")
            self.assertIn("OK", media_result.stderr)

            # Exercise the installed, not checkout-imported, fixed Owner
            # paginated history decoders against invalid timestamps, BLOB
            # source fields and hidden native BLOB ad IDs. This is not
            # hostile-UID, FD or MAP_SHARED host-cutover acceptance.
            owner_history = _command(
                str(python), "-I", "-m", "unittest", "discover",
                "-s", str(Path(__file__).resolve().parent),
                "-p", "test_storage_owner_paged_history_corruption.py", "-v",
                cwd=root, env=env, timeout=95,
            )
            self.assertEqual(owner_history.returncode, 0, owner_history.stderr[-2500:])
            self.assertRegex(owner_history.stderr, r"Ran 4 tests? in [0-9.]+s")
            # Local userns tests may map the runner to UID 0; the
            # storage-owner socket must then skip rather than weaken its
            # non-root UID requirement. Official CI drops capabilities and
            # executes this test under the actual unprivileged runner UID.
            if os.geteuid() == 0:
                self.assertIn("OK (skipped=1)", owner_history.stderr)
            else:
                self.assertIn("\nOK\n", owner_history.stderr)
                self.assertNotIn("skipped=", owner_history.stderr)

            # A corrupted native pending-fence identity or ACK bit can
            # silently hide an unresolved dashboard Write. Exercise the
            # installed store directly and the owner AF_UNIX framing where a
            # real non-root UID is available. These are not hostile UID tests.
            pending_native = _command(
                str(python), "-I", "-m", "unittest", "discover",
                "-s", str(Path(__file__).resolve().parent),
                "-p", "test_dashboard_pending_native_corruption.py", "-v",
                cwd=root, env=env, timeout=95,
            )
            self.assertEqual(
                pending_native.returncode, 0, pending_native.stderr[-2500:],
            )
            self.assertRegex(
                pending_native.stderr, r"Ran 7 tests? in [0-9.]+s",
            )
            if os.geteuid() == 0:
                self.assertIn("OK (skipped=1)", pending_native.stderr)
            else:
                self.assertIn("\nOK\n", pending_native.stderr)
                self.assertNotIn("skipped=", pending_native.stderr)

            # UTF-8 JSON contains only Unicode scalar values. Escaped
            # unpaired surrogates must not cross the installed Write-intent
            # boundary or become durable, including nested member names.
            scalar = _command(
                str(python), "-I", "-m", "unittest", "discover",
                "-s", str(Path(__file__).resolve().parent),
                "-p", "test_strict_json_unicode_scalars.py", "-v",
                cwd=root, env=env, timeout=60,
            )
            self.assertEqual(scalar.returncode, 0, scalar.stderr[-2000:])
            self.assertRegex(scalar.stderr, r"Ran 3 tests? in [0-9.]+s")
            self.assertIn("\nOK\n", scalar.stderr)

            # Inspect real Linux writable MAP_SHARED and old FD references
            # against pinned synthetic main/WAL/SHM inodes. The independent
            # installed detector remains negative-only: it cannot attest a
            # frozen process universe or host cutover without operator proof.
            host_refs = _command(
                str(python), "-I", "-m", "unittest", "discover",
                "-s", str(Path(__file__).resolve().parent),
                "-p", "test_a6_host_preflight.py", "-v",
                cwd=root, env=env, timeout=95,
            )
            self.assertEqual(host_refs.returncode, 0, host_refs.stderr[-2800:])
            self.assertRegex(host_refs.stderr, r"Ran 15 tests? in [0-9.]+s")
            self.assertIn("\nOK\n", host_refs.stderr)
            self.assertNotIn("skipped=", host_refs.stderr)

            # This is a create-only, same-user *offline* backup, NOT hostile
            # service-UID A6 recovery. Test installed CLI, receipt and manual
            # copy-to-new-path recovery; never overwrite the source or backup.
            backup = root / "verified-local-backup.sqlite"
            backup_bin = str(venv / "bin" / "mark-api-backup")
            backed = _command(
                backup_bin, "--db", str(database), "--backup", str(backup),
                cwd=root, env=env, timeout=90,
            )
            self.assertEqual(backed.returncode, 0, backed.stderr)
            backup_receipt = json.loads(backed.stdout)
            self.assertTrue(backup.is_file())
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o400)
            backup_sha256 = hashlib.sha256(backup.read_bytes()).hexdigest()
            self.assertEqual(backup_receipt["backup_sha256"], backup_sha256)
            self.assertEqual(backup_receipt["inbound_message_events"], 2)
            self.assertEqual(backup_receipt["pending_api_writes"], 0)
            self.assertEqual(backup_receipt["pending_dashboard_writes"], 0)
            with closing(sqlite3.connect(f"file:{backup}?mode=ro", uri=True)) as backup_db:
                self.assertEqual(backup_db.execute("PRAGMA quick_check(1)").fetchone()[0], "ok")
                self.assertEqual(
                    backup_db.execute("SELECT COUNT(*) FROM inbound_message_events").fetchone()[0],
                    2,
                )

            denied_overwrite = _command(
                backup_bin, "--db", str(database), "--backup", str(backup),
                cwd=root, env=env,
            )
            self.assertEqual(denied_overwrite.returncode, 2)
            self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), backup_sha256)

            restored = root / "restored-from-local-backup.sqlite"
            shutil.copyfile(backup, restored)
            os.chmod(restored, 0o600)
            with _offline_server(python, root, env, restored, ()) as address:
                code, _, payload = _request(address, "/api/ads")
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(payload)[0]["views"], 23)
                code, _, payload = _request(address, "/api/email-reactions")
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(payload)[0]["inbound_message_count"], 2)
                code, _, payload = _request(address, groups_path.replace(".csv", ""))
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(payload)[0]["label"], "Berlin")

            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("PRAGMA quick_check(1)").fetchone()[0], "ok")

        print(json.dumps({
            "result": "PASS", "scope": "installed-offline-wheel",
            "wheel_sha256": wheel_hash, "platform_access": False,
            "local_eml_events": 2, "local_catalog_snapshots": 1,
            "sqlite_quick_check": "ok",
            "loopback_http": True, "csv": True, "denied_writes": True,
            "restart_deduplication": True,
            "provider_consent_default_deny": True,
            "installed_synthetic_text_and_image_drafts": True,
            "installed_synthetic_dashboard_write_end_to_end": True,
            "installed_synthetic_media_write_end_to_end": True,
            "installed_owner_paged_history_fail_closed": True,
            "installed_dashboard_pending_corruption_fail_closed": True,
            "installed_unicode_scalar_json_rejection": True,
            "installed_a6_old_fd_mmap_negative_scan": True,
            "installed_synthetic_full_crud_restart_e2e": True,
            "image_draft_private_and_create_only": True,
            "backup_scope": "create-only-offline-not-a6",
            "backup_sha256": backup_sha256,
            "offline_restore_checked": True,
            "synthetic_owner_absence_and_provenance": True,
            "network_isolation": "linux-non-escalating-loopback-netns",
        }, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
