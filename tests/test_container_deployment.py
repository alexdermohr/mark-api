"""Fail-closed structural and shell regressions for the optional Mark container.

No Docker daemon, external network, user files, or platform account is touched.
Actual root-owned UID, mapping and recovery verification is an additional
live deployment gate, not a result of these static checks.
"""
from __future__ import annotations

from pathlib import Path
import io
import os
import shutil
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import unittest
import zlib


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def _read(filename: str) -> str:
    return (DOCS / filename).read_text(encoding="utf-8")


class ContainerDeploymentTests(unittest.TestCase):
    def test_independent_storage_uid_has_exclusive_raw_sqlite_mounts(self) -> None:
        # This is a *draft* structural security gate. It does not authorize
        # running the incomplete normal Write launcher or offline restore.
        compose = _read("mark-api-container.compose.yaml")
        executor = compose.split("\n  mark-api-owner:\n", 1)[1].split(
            "\n  mark-api-witness:\n", 1,
        )[0]
        storage = compose.split("\n  mark-api-storage:\n", 1)[1].split(
            "\n# Never automatically create", 1,
        )[0]
        backup = compose.split("\n  mark-api-backup:\n", 1)[1].split(
            "\n  mark-api-storage:\n", 1,
        )[0]
        self.assertEqual(compose.count("image: sha256:"), 6)
        self.assertEqual(compose.count('user: "50046:50046"'), 2)
        self.assertEqual(compose.count('user: "50044:50044"'), 1)
        self.assertNotIn("source: mark_data", executor)
        self.assertNotIn("source: mark_backups", executor)
        self.assertIn("source: mark_media", executor)
        self.assertIn("source: mark_storage_socket", executor)
        self.assertIn("read_only: true", executor)
        self.assertIn('user: "50046:50046"', storage)
        self.assertIn("source: mark_data", storage)
        self.assertIn("source: mark_storage_socket", storage)
        self.assertIn('network_mode: none', storage)
        self.assertIn('entrypoint: ["/bin/sh", "/etc/mark-api/storage.sh"]', storage)
        self.assertNotIn("source: mark_fence_ledger", storage)
        self.assertNotIn("source: mark_executor_socket", storage)
        self.assertIn('user: "50046:50046"', backup)
        self.assertIn("source: mark_data", backup)
        self.assertNotIn("source: mark_media", backup)
        self.assertEqual(compose.count("external: true"), 9)
        self.assertEqual(compose.count("nocopy: true"), 14)
        self.assertEqual(compose.count("- type: volume"), 14)
        script = _read("mark-api-container-storage.sh")
        self.assertIn("independent SQLite owner live deployment not attested", script)
        self.assertLess(script.index("exit 1"), script.index("mark_api.storage_owner"))
        self.assertIn("--runtime-uid 50044", script)
        self.assertIn("--runtime-gid 50044", script)
        self.assertNotIn("--init-db", script)
        self.assertNotIn("rm -f", script)
        result = subprocess.run(
            ["/bin/sh", str(DOCS / "mark-api-container-storage.sh")],
            capture_output=True, text=True, check=False, timeout=5,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("not attested", result.stderr)
        dockerfile = _read("mark-api-container.Dockerfile")
        self.assertIn('groupadd --gid 50046 mark-api-storage', dockerfile)
        self.assertIn('useradd --uid 50046 --gid 50046', dockerfile)
        self.assertIn("mark-api-container-storage.sh /etc/mark-api/storage.sh",
                      dockerfile)
        self.assertIn("mark-api-container-storage.sh", _read("mark-api-container-build.sh"))
        control = _read("mark-api-container-control.sh")
        self.assertIn("attest_existing_volume mark-api-data-v1 50046:50046:700", control)
        self.assertIn("attest_existing_volume mark-api-storage-socket-v1 50046:50044:2710", control)
        self.assertIn("attest_existing_volume mark-api-media-v1 50044:50044:700", control)
        self.assertLess(
            control.index("up --no-deps --no-build --pull never --detach mark-api-storage"),
            control.index("up --no-deps --no-build --pull never --detach mark-api-owner"),
        )

    def test_scripts_are_valid_posix_shell(self) -> None:
        for filename in (
            "mark-api-container-start.sh",
            "mark-api-container-frontend.sh",
            "mark-api-container-ingress.sh",
            "mark-api-container-witness.sh",
            "mark-api-container-storage.sh",
            "mark-api-container-backup.sh",
            "mark-api-container-build.sh",
            "mark-api-container-control.sh",
        ):
            with self.subTest(filename=filename):
                result = subprocess.run(
                    ["/bin/sh", "-n", str(DOCS / filename)],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_image_has_attested_distro_and_no_mutable_code_owner(self) -> None:
        image = _read("mark-api-container.Dockerfile")
        self.assertIn("FROM ubuntu:24.04@sha256:", image)
        self.assertIn("python3-minimal python3.12 python3.12-venv", image)
        self.assertIn("RUN /usr/bin/python3.12 -m venv /opt/mark-api/venv", image)
        self.assertIn('COPY --chmod=0644 mark-api-bootstrap.sh', image)
        self.assertIn('COPY --chmod=0644 mark-api-preflight.py', image)
        self.assertIn('COPY --chmod=0644 mark-api-container-healthcheck.py', image)
        self.assertIn('RUN /bin/sh /etc/mark-api/bootstrap.sh --help', image)
        self.assertIn('USER mark-api:mark-api', image)
        self.assertIn('ENTRYPOINT ["/usr/bin/false"]', image)
        self.assertNotIn('USER root', image)
        self.assertIn('ARG MARK_UID=50042', image)
        self.assertIn('ARG MARK_GID=50042', image)
        self.assertIn('! getent passwd "$MARK_UID"', image)
        self.assertIn('! getent group "$MARK_GID"', image)
        self.assertIn('install -d -o mark-api-owner -g mark-api-owner -m 0700', image)

    def test_storage_preflight_rejects_executor_db_ownership(self) -> None:
        """UID 50044 may not attest raw SQLite; 50046 owns DB and backups."""
        dockerfile = _read("mark-api-container.Dockerfile")
        start = _read("mark-api-container-start.sh")
        backup = _read("mark-api-container-backup.sh")
        storage = _read("mark-api-container-storage.sh")
        preflight = _read("mark-api-preflight.py")
        self.assertIn('! getent passwd 50044', dockerfile)
        self.assertIn('! getent group 50044', dockerfile)
        self.assertIn('groupadd --gid 50044 mark-api-owner', dockerfile)
        self.assertIn('useradd --uid 50044 --gid 50044', dockerfile)
        self.assertIn('groupadd --gid 50046 mark-api-storage', dockerfile)
        self.assertIn('useradd --uid 50046 --gid 50046', dockerfile)
        # The legacy executor entrypoint remains hard-stopped until its
        # raw-SQLite arguments are replaced by storage-owner RPC.
        self.assertIn("\nexit 1\n", start)
        self.assertLess(
            start.index("\nexit 1\n"),
            start.index("exec /opt/mark-api/venv/bin/python -I -m mark_api.launcher"),
        )
        self.assertIn('--service-account mark-api-storage', backup)
        # The storage service's own _attested_database validates private UID50046
        # data; it does not mount the backup directory or need backup preflight.
        self.assertIn('mark_api.storage_owner', storage)
        self.assertNotIn('--service-account mark-api-owner', backup + storage)
        self.assertIn('service_account != "mark-api-storage"', preflight)
        self.assertIn('choices=("mark-api-storage",)', preflight)
        self.assertNotIn('service_account not in ("mark-api", "mark-api-owner")', preflight)
        self.assertLess(storage.index("exit 1"),
                        storage.index("exec /opt/mark-api/venv/bin/python"))
        self.assertNotIn('--service-account root', start + backup + storage)

    def test_release_wheel_is_noneditable_and_deps_explicit(self) -> None:
        image = _read("mark-api-container.Dockerfile")
        self.assertIn('set -- /opt/mark-api-wheels/*.whl', image)
        self.assertIn('[ "$#" -eq 1 ]', image)
        self.assertIn('pip install --no-index --no-deps "$1"', image)
        self.assertIn("Pillow==12.3.0 websocket-client==1.9.2", image)
        self.assertNotIn('pip install -e', image)
        self.assertNotIn('COPY . /', image)

    def test_product_start_checks_deployment_before_default_on_write_launcher(self) -> None:
        script = _read("mark-api-container-start.sh")
        preflight = '/bin/sh /etc/mark-api/bootstrap.sh'
        launcher = 'exec /opt/mark-api/venv/bin/python -I -m mark_api.launcher'
        self.assertLess(script.index(preflight), script.index(launcher))
        for flag in ("--db /var/lib/mark-api/mark.sqlite", "--cdp-port 9222",
                     "--dashboard-port 18875", "--write-port 18876"):
            self.assertIn(flag, script)
        self.assertNotIn("--init-db", script)
        self.assertNotIn("--disable-writes", script)
        self.assertIn("> /run/mark-api/launcher.log", script)
        self.assertIn("umask 077", script)

    def test_all_draft_entrypoints_refuse_direct_compose_even_with_no_deps(self) -> None:
        # This is a Draft safety gate, not a production-ready deployment.
        # Compose up bypasses control.sh; --no-deps bypasses service_healthy.
        # Even the frontend alone must never serve HTTP.
        # All image-owned entrypoints reject before any sensitive operation.
        for filename, args, marker in (
            (
                "mark-api-container-start.sh",
                (),
                "mark-api: trusted browser executor live deployment not attested",
            ),
            (
                "mark-api-container-frontend.sh",
                (),
                "mark-api: independent frontend live deployment not attested",
            ),
            (
                "mark-api-container-ingress.sh",
                (),
                "mark-api: public dashboard ingress live deployment not attested",
            ),
            (
                "mark-api-container-backup.sh",
                ("synthetic",),
                "mark-api: independent witness ledger paired backup unavailable",
            ),
            (
                "mark-api-container-storage.sh",
                (),
                "mark-api: independent SQLite owner live deployment not attested",
            ),
            (
                "mark-api-container-witness.sh",
                (),
                "mark-api: independent witness live deployment not attested",
            ),
        ):
            with self.subTest(filename=filename):
                script = _read(filename)
                first_sensitive_operation = {
                    "mark-api-container-start.sh": "/bin/sh /etc/mark-api/bootstrap.sh",
                    "mark-api-container-frontend.sh":
                        "exec /opt/mark-api/venv/bin/python -I -m mark_api.dashboard_relay",
                    "mark-api-container-ingress.sh":
                        "exec /usr/bin/python3 -I -S /etc/mark-api/ingress-landlock.py",
                    "mark-api-container-backup.sh": "/bin/sh /etc/mark-api/bootstrap.sh",
                    "mark-api-container-storage.sh":
                        "exec /opt/mark-api/venv/bin/python -I -m mark_api.storage_owner",
                    "mark-api-container-witness.sh":
                        "exec /opt/mark-api/venv/bin/python -I -m mark_api.fence_witness",
                }[filename]
                self.assertLess(
                    script.index(marker), script.index(first_sensitive_operation),
                )
                result = subprocess.run(
                    ["/bin/sh", str(DOCS / filename), *args],
                    capture_output=True, text=True, check=False, timeout=5,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(marker, result.stderr)
                self.assertNotIn("/etc/mark-api/bootstrap.sh:", result.stderr)

    def test_backup_is_create_only_and_bounded_to_a_valid_instance(self) -> None:
        script = _read("mark-api-container-backup.sh")
        self.assertIn('instance=$1', script)
        self.assertIn("*[!A-Za-z0-9_-]*", script)
        self.assertIn('[ "${#instance}" -le 48 ]', script)
        self.assertLess(
            script.index("/bin/sh /etc/mark-api/bootstrap.sh"),
            script.index("exec /opt/mark-api/venv/bin/python"),
        )
        self.assertIn("-m mark_api.backup_cli", script)
        self.assertIn('mark-${instance}.sqlite', script)
        for args in ((), ("../escape",), ("",), ("a/b",), ("x" * 49,)):
            with self.subTest(args=args):
                run = subprocess.run(
                    ["/bin/sh", str(DOCS / "mark-api-container-backup.sh"), *args],
                    capture_output=True, check=False,
                )
                self.assertEqual(run.returncode, 64, run.stderr)

    def test_attested_image_contains_draft_witness_entrpoint_guard(self) -> None:
        compose = _read("mark-api-container.compose.yaml")
        witness_service = compose.split("\n  mark-api-witness:\n",1)[1].split(
            "\n  mark-api-backup:\n",1
        )[0]
        self.assertIn('entrypoint: ["/bin/sh", "/etc/mark-api/witness.sh"]', witness_service)
        self.assertIn("COPY --chmod=0644 mark-api-container-witness.sh /etc/mark-api/witness.sh",
                      _read("mark-api-container.Dockerfile"))
        build = _read("mark-api-container-build.sh")
        self.assertGreaterEqual(build.count("mark-api-container-witness.sh"), 2)
        witness_guard = _read("mark-api-container-witness.sh")
        self.assertNotIn("--init-ledger", witness_guard)
        self.assertIn("exit 1", witness_guard)
        self.assertLess(
            witness_guard.index("exit 1"),
            witness_guard.index("mark_api.fence_witness"),
        )

    def test_compose_requires_preexisting_private_volumes_and_does_not_autostart(self) -> None:
        compose = _read("mark-api-container.compose.yaml")
        self.assertIn("MARK_API_IMAGE_DIGEST:?Verified immutable image digest required", compose)
        self.assertEqual(compose.count("image: sha256:"), 6)
        self.assertNotIn("image: ${MARK_API_IMAGE:", compose)
        self.assertEqual(compose.count('user: "50042:50042"'), 1)
        self.assertEqual(compose.count('user: "50044:50044"'), 1)
        self.assertEqual(compose.count('user: "50045:50045"'), 1)
        self.assertEqual(compose.count("read_only: true\n    cap_drop:"), 6)
        self.assertEqual(compose.count('cap_drop: ["ALL"]'), 6)
        self.assertEqual(compose.count('security_opt: ["no-new-privileges:true"]'), 6)
        self.assertEqual(compose.count('restart: "no"'), 6)
        self.assertIn('profiles: ["live"]', compose)
        self.assertIn('profiles: ["backup"]', compose)
        # A socket file without a listening process cannot report healthy.
        self.assertIn('/etc/mark-api/healthcheck.py", "owner"', compose)
        self.assertIn('/etc/mark-api/healthcheck.py", "witness"', compose)
        self.assertIn('/etc/mark-api/healthcheck.py", "storage"', compose)
        self.assertIn('/etc/mark-api/healthcheck.py", "frontend"', compose)
        self.assertIn('/etc/mark-api/healthcheck.py", "ingress"', compose)
        self.assertNotIn('test -S /run/mark-', compose)
        self.assertIn('network_mode: host', compose)
        self.assertIn('network_mode: none', compose)
        self.assertIn('target: /var/lib/mark-api', compose)
        self.assertIn('target: /var/lib/mark-api-backups', compose)
        self.assertIn('read_only: true', compose)
        self.assertIn("/run/mark-api:rw,nosuid,nodev,noexec", compose)
        self.assertEqual(compose.count("external: true"), 9)
        # Raw Compose must not seed an empty attested external volume.
        self.assertEqual(compose.count("- type: volume"), 14)
        self.assertEqual(compose.count("nocopy: true"), 14)
        self.assertNotIn("privileged: true", compose)
        self.assertNotIn("pid: host", compose)
        self.assertNotIn("userns_mode: host", compose)


    def test_witness_service_is_separate_uid_and_fail_closed_on_absent_ledger(self) -> None:
        compose = _read("mark-api-container.compose.yaml")
        start = _read("mark-api-container-start.sh")
        self.assertEqual(compose.count("image: sha256:"), 6)
        self.assertIn('  mark-api-witness:', compose)
        witness = compose.split('  mark-api-witness:', 1)[1].split('  mark-api-backup:', 1)[0]
        self.assertIn('user: "50043:50043"', witness)
        self.assertIn('network_mode: none', witness)
        self.assertIn('cap_drop: ["ALL"]', witness)
        self.assertIn('no-new-privileges:true', witness)
        self.assertIn('--runtime-uid', witness)
        self.assertIn('50044', witness)
        self.assertIn('--runtime-gid', witness)
        self.assertIn('--ledger', witness)
        self.assertIn('--runtime-socket', witness)
        self.assertIn('--operator-socket', witness)
        self.assertNotIn('--init-ledger', witness)
        self.assertIn('mark_fence_ledger:', compose)
        self.assertIn('mark_fence_runtime_socket:', compose)
        self.assertIn('mark_fence_operator_socket:', compose)
        self.assertIn('--witness-socket /run/mark-api-witness/runtime.sock', start)
        self.assertIn('--witness-owner-uid 50043', start)
        self.assertIn("No write-disable flags", start)
        self.assertNotIn("--disable-writes", start)

    def test_planned_live_release_sequence_requires_witness_first_after_cdp_gate(self) -> None:
        # Static future ordering only; live currently fails before both starts.
        control = _read("mark-api-container-control.sh")
        self.assertIn('getent passwd 50043', control)
        self.assertIn('getent group 50043', control)
        self.assertIn('getent passwd 50044', control)
        self.assertIn('getent group 50044', control)
        self.assertIn('mark-api-fence-ledger-v1', control)
        self.assertIn('mark-api-fence-runtime-socket-v1', control)
        self.assertIn('mark-api-fence-operator-socket-v1', control)
        self.assertIn('mark-api-ingress-socket-v1', control)
        self.assertIn('getent passwd 50045', control)
        self.assertIn('getent passwd 50046', control)
        self.assertIn('getent group 50046', control)
        self.assertLess(
            control.index('up --no-deps --no-build --pull never --detach mark-api-witness'),
            control.index('up --no-deps --no-build --pull never --detach mark-api-storage'))
        self.assertLess(
            control.index('up --no-deps --no-build --pull never --detach mark-api-storage'),
            control.index('up --no-deps --no-build --pull never --detach mark-api-owner'))
        self.assertLess(control.index('up --no-deps --no-build --pull never --detach mark-api-witness'),
                        control.index('up --no-deps --no-build --pull never --detach mark-api-owner'))
        self.assertLess(control.index('up --no-deps --no-build --pull never --detach mark-api-owner'),
                        control.index('up --no-deps --no-build --pull never --detach mark-api ||'))
        self.assertLess(control.index('up --no-deps --no-build --pull never --detach mark-api ||'),
                        control.index('up --no-deps --no-build --pull never --detach mark-api-ingress'))
        self.assertIn('Health.Status', control)
        self.assertNotIn('--init-ledger', control)


    def test_release_guard_rejects_userns_remapping_and_attests_product_identity(self) -> None:
        control = _read("mark-api-container-control.sh")
        self.assertIn("docker_cli info --format '{{json .SecurityOptions}}'", control)
        self.assertIn("name=userns", control)
        self.assertIn("name=rootless", control)
        self.assertIn("product_id=$(docker_cli compose", control)
        self.assertIn("observed_product_user=", control)
        self.assertIn("observed_product_image=", control)
        self.assertIn('[ "$observed_product_network" = none ]', control)
        self.assertIn("ingress_user=", control)
        self.assertIn("ingress_network=", control)
        self.assertIn("observed_product_running=", control)
        self.assertIn("'50042:50042'", control)
        self.assertIn("{{.State.Running}}", control)
        self.assertLess(control.index("observed_user="),
                        control.index("observed_product_user="))
        self.assertIn("stop_grace_period: 60s",
                      _read("mark-api-container.compose.yaml"))


    def test_cutover_guard_denies_external_volume_driver_bind_aliases(self) -> None:
        # Distinct Compose volume *names* alone cannot prove distinct inodes:
        # a malicious/misconfigured driver_opts.device can alias SQLite into
        # the executor's apparently separate media volume.
        control = _read("mark-api-container-control.sh")
        function = control.split("attest_existing_volume() {", 1)[1].split(
            "\n}\n", 1,
        )[0]
        self.assertIn("volume inspect --format '{{.Driver}}'", function)
        self.assertIn("volume inspect --format '{{json .Options}}'", function)
        self.assertIn("expected a local Docker volume with no driver options",
                      function)
        self.assertLess(
            function.index("{{json .Options}}"),
            function.index("--mount \"type=volume,source=${volume_name}"),
        )

    def test_live_guard_attests_existing_volume_directory_and_ledger_identity(self) -> None:
        control = _read("mark-api-container-control.sh")
        self.assertIn("attest_existing_volume() {", control)
        self.assertIn("--mount \"type=volume,source=${volume_name},target=/volume,readonly,volume-nocopy\"", control)
        self.assertIn("--entrypoint /usr/bin/stat", control)
        self.assertIn("-c '%u:%g:%a'", control)
        for item in (
            "attest_existing_volume mark-api-data-v1 50046:50046:700",
            "attest_existing_volume mark-api-backups-v1 50046:50046:700",
            "attest_existing_volume mark-api-media-v1 50044:50044:700",
            "attest_existing_volume mark-api-storage-socket-v1 50046:50044:2710",
            "attest_existing_volume mark-api-fence-ledger-v1 50043:50043:700",
            "attest_existing_volume mark-api-fence-runtime-socket-v1 50043:50044:2710",
            "attest_existing_volume mark-api-executor-socket-v1 50044:50042:2710",
            "attest_existing_volume mark-api-ingress-socket-v1 50042:50045:2710",
            "attest_existing_volume mark-api-fence-operator-socket-v1 50043:50043:700",
            "/volume/ledger.sqlite",
            "50043:50043:600:1",
        ):
            self.assertIn(item, control)


    def test_live_guard_blocks_unmediated_runtime_cdp_bypass(self) -> None:
        control = _read("mark-api-container-control.sh")
        # This draft separates the network and data authority but still
        # lacks real UID/CDP/old-mapping/paired-ledger proof. Keep live gated.
        stop = "fail 'unmediated host CDP bypass blocks product live deployment'"
        self.assertIn(stop, control)
        self.assertLess(control.index(stop),
                        control.index("up --no-deps --no-build --pull never --detach mark-api-witness"))
        compose = _read("mark-api-container.compose.yaml")
        runtime = compose.split("\n  mark-api:\n", 1)[1].split("\n  mark-api-ingress:\n", 1)[0]
        owner = compose.split("\n  mark-api-owner:\n", 1)[1].split("\n  mark-api-witness:\n", 1)[0]
        self.assertNotIn("network_mode: host", runtime)
        self.assertNotIn("source: mark_data", runtime)
        self.assertIn("network_mode: host", owner)


    def test_runtime_uid_has_only_dashboard_transport_without_cdp_or_sqlite(self) -> None:
        compose = _read("mark-api-container.compose.yaml")
        runtime = compose.split("\n  mark-api:\n", 1)[1].split("\n  mark-api-ingress:\n", 1)[0]
        owner = compose.split("\n  mark-api-owner:\n", 1)[1].split("\n  mark-api-witness:\n", 1)[0]
        witness = compose.split("\n  mark-api-witness:\n", 1)[1].split("\n  mark-api-backup:\n", 1)[0]
        self.assertIn('user: "50042:50042"', runtime)
        self.assertIn("working_dir: /", runtime)
        self.assertNotIn("network_mode: host", runtime)
        self.assertNotIn("network_mode: container:", runtime)
        self.assertNotIn("source: mark_data", runtime)
        self.assertNotIn("source: mark_backups", runtime)
        self.assertNotIn("source: mark_fence_ledger", runtime)
        self.assertNotIn("source: mark_fence_runtime_socket", runtime)
        self.assertNotIn("source: mark_fence_operator_socket", runtime)
        self.assertIn("source: mark_executor_socket", runtime)
        self.assertIn("read_only: true", runtime)
        self.assertIn("network_mode: none", runtime)
        self.assertIn("source: mark_ingress_socket", runtime)
        self.assertNotIn("ports:", runtime)
        self.assertNotIn("frontend_isolated", runtime)
        self.assertIn('user: "50044:50044"', owner)
        self.assertIn('entrypoint: ["/bin/sh", "/etc/mark-api/start.sh"]', owner)
        self.assertIn("network_mode: host", owner)
        self.assertNotIn("source: mark_data", owner)
        self.assertNotIn("source: mark_backups", owner)
        self.assertIn("source: mark_storage_socket", owner)
        self.assertIn("source: mark_media", owner)
        self.assertIn("source: mark_fence_runtime_socket", owner)
        self.assertIn("source: mark_executor_socket", owner)
        self.assertIn('network_mode: none', witness)
        self.assertIn('"--runtime-uid", "50044"', witness)
        self.assertIn('"--runtime-gid", "50044"', witness)
        # No IPv4, IPv6, bridge gateway or LAN routing for UID50042.
        self.assertNotIn("driver: bridge", compose)
        self.assertNotIn("frontend_isolated:", compose)
        ingress = compose.split("\n  mark-api-ingress:\n", 1)[1].split(
            "\n  mark-api-owner:\n", 1,
        )[0]
        self.assertIn('user: "50045:50045"', ingress)
        self.assertIn("network_mode: host", ingress)
        self.assertNotIn("source: mark_executor_socket", ingress)
        self.assertNotIn("source: mark_data", ingress)
        self.assertNotIn("source: mark_fence", ingress)
        self.assertIn("source: mark_ingress_socket", ingress)
        self.assertNotIn("ports:", ingress)
        self.assertNotIn("privileged: true", compose)
        self.assertNotIn("privileged: true", compose)

    def test_trusted_executor_and_frontend_use_only_bound_dashboard_ipc(self) -> None:
        owner = _read("mark-api-container-start.sh")
        frontend = _read("mark-api-container-frontend.sh")
        build = _read("mark-api-container-build.sh")
        dockerfile = _read("mark-api-container.Dockerfile")
        self.assertIn("--frontend-socket /run/mark-api-executor/dashboard.sock", owner)
        self.assertIn("--frontend-uid 50042", owner)
        self.assertIn("--frontend-gid 50042", owner)
        self.assertIn("--witness-owner-uid 50043", owner)
        self.assertIn("--cdp-port 9222", owner)
        self.assertIn("--dashboard-port 18875", owner)
        self.assertIn("--write-port 18876", owner)
        self.assertNotIn("--cdp-port", frontend)
        self.assertNotIn("--db", frontend)
        self.assertNotIn("--init-db", frontend)
        self.assertNotIn("--witness", frontend)
        self.assertIn("--expected-owner-uid 50044", frontend)
        self.assertIn("mark-api-container-frontend.sh", build)
        self.assertIn("mark-api-container-frontend.sh", dockerfile)
        self.assertIn("mark-api-container-ingress.sh", build)
        self.assertIn("mark-api-container-ingress.sh", dockerfile)
        self.assertIn("netless-frontend", frontend)
        self.assertIn("--ingress-uid 50045", frontend)
        ingress = _read("mark-api-container-ingress.sh")
        guard = _read("mark-api-container-ingress-landlock.py")
        self.assertIn("ingress-landlock.py", ingress)
        self.assertNotIn("-m mark_api.dashboard_relay", ingress)
        self.assertIn("install_tcp_ingress_policy(bind_port=8875)", guard)
        self.assertIn("public-ingress", guard)
        self.assertIn("_LANDLOCK_ACCESS_NET_CONNECT_TCP", guard)
        self.assertIn("mark-api-container-ingress-landlock.py", build)
        self.assertIn("mark-api-container-ingress-landlock.py", dockerfile)
        self.assertLess(
            guard.index("install_tcp_ingress_policy(bind_port=8875)"),
            guard.index("os.execv("),
        )

    def test_release_backup_refuses_unpaired_sqlite_only_witness_state(self) -> None:
        control = _read("mark-api-container-control.sh")
        blocked = "fail 'independent witness ledger backup is not implemented'"
        self.assertIn(blocked, control)
        self.assertLess(
            control.index(blocked),
            control.index('docker_cli compose -f "$compose" --project-name mark-api --profile backup'),
        )
        # An operator cannot treat a valid SQLite snapshot as an independent
        # recovery-fence snapshot or delete the original witness receipts.

    def test_release_image_build_uses_only_exact_committed_archive(self) -> None:
        build = _read("mark-api-container-build.sh")
        self.assertIn('rev-parse --verify HEAD', build)
        self.assertIn('[ "$head" = "$expected" ]', build)
        self.assertIn('status --porcelain --untracked-files=normal', build)
        self.assertNotIn('attested_git archive "$expected"', build)
        self.assertIn('private_git archive "$expected"', build)
        self.assertIn("mark-api-container-healthcheck.py", build)
        self.assertIn('attested_git rev-list --objects --no-walk "$expected"', build)
        self.assertIn('private_git hash-object -t "$object_type" --stdin', build)
        self.assertIn('private_git hash-object -w -t "$object_type" --stdin', build)
        self.assertIn('attested_git status --porcelain', build)
        self.assertIn('--git-dir="$temp/git"', build)
        self.assertIn('GIT_ALTERNATE_OBJECT_DIRECTORIES="$common/objects"', build)
        self.assertNotIn('safe_git -C "$repo" status --porcelain', build)
        self.assertIn('build --wheel --offline', build)
        self.assertIn('org.opencontainers.image.revision=$expected', build)
        self.assertIn('image inspect', build)
        self.assertIn('--host unix:///var/run/docker.sock', build)
        self.assertNotIn('docker push', build)
        self.assertNotIn('docker run', build)
        for script in (build, _read("mark-api-container-control.sh")):
            self.assertIn("safe_git() {", script)
            self.assertIn("/usr/bin/env -i", script)
            self.assertIn("GIT_CONFIG_NOSYSTEM=1", script)
            self.assertIn("GIT_CONFIG_GLOBAL=/dev/null", script)
            self.assertIn("-c core.fsmonitor=false", script)
            self.assertIn("-c core.hooksPath=/dev/null", script)
            self.assertIn("GIT_ALTERNATE_OBJECT_DIRECTORIES=", script)
            self.assertIn("attested_git status --porcelain", script)
            self.assertIn('attested_git read-tree "$expected"', script)
            self.assertNotIn('cp -- "$gitdir/index"', script)
            self.assertNotIn('safe_git -C "$repo" status --porcelain', script)


    def _fake_git_repo(self, root: Path) -> tuple[Path, str]:
        repo = root / "synthetic"
        repo.mkdir()
        (repo / "docs").mkdir()
        (repo / "src/mark_api").mkdir(parents=True)
        shutil.copyfile(DOCS / "mark-api-container-build.sh", repo / "docs/mark-api-container-build.sh")
        shutil.copyfile(DOCS / "mark-api-container-control.sh", repo / "docs/mark-api-container-control.sh")
        (repo / "src/mark_api/__init__.py").write_text("# mark\n")
        (repo / "pyproject.toml").write_text("[project]\nname='synthetic'\nversion='0.0.1'\n")
        (repo / "README.md").write_text("attested-version\n")
        def git(*args: str) -> str:
            p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            return p.stdout.strip()
        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        git("add", ".")
        git("commit", "-qm", "attested")
        expected = git("rev-parse", "HEAD")
        return repo, expected

    def test_rejects_replacement_commit_even_when_head_sha_looks_valid(self) -> None:
        with TemporaryDirectory(prefix="mark-git-replace-") as tmp:
            repo, expected = self._fake_git_repo(Path(tmp))
            def git(*args: str) -> str:
                p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                return p.stdout.strip()
            (repo / "README.md").write_text("untrusted-replacement\n")
            git("add", "README.md")
            git("commit", "-qm", "replacement")
            replacement = git("rev-parse", "HEAD")
            git("replace", expected, replacement)
            # HEAD still reports the original ID; normal Git archive does not.
            git("checkout", "--detach", "--force", expected)
            self.assertEqual(git("rev-parse", "HEAD"), expected)
            archived = subprocess.run(
                ["git", "-C", str(repo), "archive", expected, "--", "README.md"],
                capture_output=True, check=True,
            )
            with tarfile.open(fileobj=io.BytesIO(archived.stdout)) as archive:
                self.assertEqual(archive.extractfile("README.md").read(),
                                 b"untrusted-replacement\n")
            env = {**os.environ, "MARK_UV": "/usr/bin/false"}
            env.pop("GIT_NO_REPLACE_OBJECTS", None)
            p = subprocess.run(
                ["/bin/sh", str(repo / "docs/mark-api-container-build.sh"), expected],
                capture_output=True, text=True, env=env,
            )
            self.assertNotEqual(p.returncode, 0)
            build = _read("mark-api-container-build.sh")
            self.assertIn("export GIT_NO_REPLACE_OBJECTS=1", build)
            self.assertIn("refs/replace", build)
            self.assertTrue(
                "mark-api: source checkout is dirty" in p.stderr
                or "mark-api: Git replacement references are not allowed" in p.stderr,
                p.stderr,
            )

    def test_rejects_unused_replacement_ref_in_clean_repo(self) -> None:
        with TemporaryDirectory(prefix="mark-git-unused-replace-") as tmp:
            repo, expected = self._fake_git_repo(Path(tmp))
            (repo / "README.md").write_text("a second commit\n")
            for cmd in (
                ("add", "README.md"), ("commit", "-qm", "unrelated"),
            ):
                p = subprocess.run(["git", "-C", str(repo), *cmd],
                                   capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
            alt = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
            ).strip()
            p = subprocess.run(
                ["git", "-C", str(repo), "checkout", "--detach", expected],
                capture_output=True, text=True,
            )
            self.assertEqual(p.returncode, 0, p.stderr)
            p = subprocess.run(
                ["git", "-C", str(repo), "replace", alt, expected],
                capture_output=True, text=True,
            )
            self.assertEqual(p.returncode, 0, p.stderr)
            clean = subprocess.check_output(
                ["git", "-C", str(repo), "status", "--porcelain"], text=True
            )
            self.assertEqual(clean, "")
            result = subprocess.run(
                ["/bin/sh", str(repo / "docs/mark-api-container-build.sh"), expected],
                capture_output=True, text=True,
                env={**os.environ, "MARK_UV": "/usr/bin/false"},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("mark-api: Git replacement references are not allowed",
                          result.stderr)

    def test_rejects_git_graft_even_if_branch_and_index_look_clean(self) -> None:
        with TemporaryDirectory(prefix="mark-git-graft-") as tmp:
            repo, expected = self._fake_git_repo(Path(tmp))
            graft = repo / ".git/info/grafts"
            graft.parent.mkdir(parents=True, exist_ok=True)
            graft.write_text(expected + "\n")
            p = subprocess.run(
                ["/bin/sh", str(repo / "docs/mark-api-container-build.sh"), expected],
                capture_output=True, text=True,
                env={**os.environ, "MARK_UV": "/usr/bin/false"},
            )
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("mark-api: Git grafts are not allowed", p.stderr)

    def test_untrusted_git_fsmonitor_cannot_execute(self) -> None:
        # Both environment-injected and checkout-local executable config must
        # be inert before any git status or revision validation runs.
        with TemporaryDirectory(prefix="mark-git-fsmonitor-") as tmp:
            root = Path(tmp)
            repo, expected = self._fake_git_repo(root)
            # Force a tracked-content refresh: fsmonitor is not called on a cache hit.
            (repo / "README.md").write_text("dirty tracked content\n")
            marker = root / "fsmonitor-executed"
            hook = root / "fsmonitor.sh"
            hook.write_text(
                "#!/bin/sh\n"
                + "printf triggered > '" + str(marker) + "'\n"
                + "printf 'token\\000/\\000'\n",
                encoding="utf-8",
            )
            hook.chmod(0o700)
            paths = (
                (repo / "docs/mark-api-container-build.sh", [expected]),
                (repo / "docs/mark-api-container-control.sh",
                 ["verify", "sha256:" + "a" * 64, expected]),
            )
            for script, arguments in paths:
                for source in ("environment", "checkout-local"):
                    with self.subTest(script=script.name, source=source):
                        marker.unlink(missing_ok=True)
                        env = {**os.environ, "MARK_UV": "/usr/bin/false"}
                        env.pop("GIT_CONFIG_COUNT", None)
                        for key in list(env):
                            if key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
                                env.pop(key)
                        conf = subprocess.run(
                            ["git", "-C", str(repo), "config", "--unset-all", "core.fsmonitor"],
                            capture_output=True, text=True, env=os.environ.copy(),
                        )
                        self.assertIn(conf.returncode, (0, 5), conf.stderr)
                        if source == "environment":
                            env.update(
                                GIT_CONFIG_COUNT="1",
                                GIT_CONFIG_KEY_0="core.fsmonitor",
                                GIT_CONFIG_VALUE_0=str(hook),
                            )
                        else:
                            conf = subprocess.run(
                                ["git", "-C", str(repo), "config", "core.fsmonitor", str(hook)],
                                capture_output=True, text=True, env=os.environ.copy(),
                            )
                            self.assertEqual(conf.returncode, 0, conf.stderr)
                        completed = subprocess.run(
                            ["/bin/sh", str(script), *arguments],
                            capture_output=True, text=True, env=env,
                        )
                        self.assertNotEqual(completed.returncode, 0)
                        self.assertFalse(marker.exists(),
                            f"{script.name} executed {source} core.fsmonitor")

    def test_release_status_does_not_trust_assume_unchanged_index(self) -> None:
        with TemporaryDirectory(prefix="mark-git-assume-unchanged-") as tmp:
            repo, _ = self._fake_git_repo(Path(tmp))
            compose = repo / "docs/mark-api-container.compose.yaml"
            compose.write_text("trusted compose\n", encoding="utf-8")

            def git(*args: str) -> str:
                p = subprocess.run(
                    ["git", "-C", str(repo), *args],
                    capture_output=True, text=True,
                )
                self.assertEqual(p.returncode, 0, p.stderr)
                return p.stdout.strip()

            git("add", "docs/mark-api-container.compose.yaml")
            git("commit", "-qm", "trusted compose")
            expected = git("rev-parse", "HEAD")
            git("update-index", "--assume-unchanged",
                "docs/mark-api-container.compose.yaml")
            compose.write_text(
                "services:\n  mark-api:\n    image: untrusted-latest\n",
                encoding="utf-8",
            )
            self.assertEqual(git("status", "--porcelain"), "")
            for name, argv, expected_message in (
                ("mark-api-container-build.sh", [expected],
                 "source checkout is dirty"),
                ("mark-api-container-control.sh",
                 ["verify", "sha256:" + "a" * 64, expected],
                 "dirty release checkout"),
            ):
                with self.subTest(script=name):
                    env = {**os.environ, "MARK_UV": "/usr/bin/true"}
                    p = subprocess.run(
                        ["/bin/sh", str(repo / "docs" / name), *argv],
                        capture_output=True, text=True, env=env,
                    )
                    self.assertNotEqual(p.returncode, 0)
                    self.assertIn(expected_message, p.stderr)

    def test_release_status_rebuilds_clean_split_index(self) -> None:
        with TemporaryDirectory(prefix="mark-git-split-index-") as tmp:
            repo, expected = self._fake_git_repo(Path(tmp))
            p = subprocess.run(
                ["git", "-C", str(repo), "update-index", "--split-index"],
                capture_output=True, text=True,
            )
            self.assertEqual(p.returncode, 0, p.stderr)
            # The source checkout is genuinely clean; a copied split index
            # without sharedindex.<hash> must not reject this release.
            p = subprocess.run(
                ["/bin/sh", str(repo / "docs/mark-api-container-build.sh"),
                 expected],
                capture_output=True, text=True,
                env={**os.environ, "MARK_UV": "/usr/bin/true"},
            )
            self.assertNotEqual(p.returncode, 0)
            # The clean split index must advance through source attestation
            # to the offline build-backend admission gate.
            self.assertIn("pinned setuptools wheel is required", p.stderr)
            self.assertNotIn("source checkout is dirty", p.stderr)
            p = subprocess.run(
                ["/bin/sh", str(repo / "docs/mark-api-container-control.sh"),
                 "verify", "sha256:" + "a" * 64, expected],
                capture_output=True, text=True,
            )
            self.assertNotEqual(p.returncode, 0)
            self.assertNotIn("Git snapshot status unavailable", p.stderr)
            self.assertNotIn("dirty release checkout", p.stderr)
            self.assertNotIn("index file open failed", p.stderr)

    def test_git_filter_injected_after_preflight_is_inert(self) -> None:
        # A one-time config allowlist is insufficient. Mutate .git/config
        # AFTER that preflight, immediately before the release status check.
        with TemporaryDirectory(prefix="mark-git-config-race-") as tmp:
            root = Path(tmp)
            repo, _ = self._fake_git_repo(root)
            (repo / ".gitattributes").write_text(
                "README.md filter=malicious\n", encoding="utf-8",
            )
            for command in (("add", ".gitattributes"),
                            ("commit", "-qm", "trusted attributes")):
                p = subprocess.run(
                    ["git", "-C", str(repo), *command],
                    capture_output=True, text=True,
                )
                self.assertEqual(p.returncode, 0, p.stderr)
            expected = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
            ).strip()
            marker = root / "late-injected-filter-executed"
            hook = root / "late-filter.sh"
            hook.write_text(
                "#!/bin/sh\n"
                + "printf executed > '" + str(marker) + "'\n"
                + "exec /bin/cat\n",
                encoding="utf-8",
            )
            hook.chmod(0o700)

            for name, anchor in (
                ("mark-api-container-build.sh",
                 'head=$(safe_git -C "$repo" rev-parse --verify HEAD)'),
                ("mark-api-container-control.sh",
                 'actual=$(safe_git -C "$repo" rev-parse --verify HEAD) || fail \'HEAD unavailable\''),
            ):
                with self.subTest(script=name):
                    marker.unlink(missing_ok=True)
                    script = repo / "docs" / name
                    source = (DOCS / name).read_text(encoding="utf-8")
                    self.assertEqual(source.count(anchor), 1)
                    # Fixture injection runs after allowlist (if present).
                    injection = (
                        '\n/usr/bin/git -C "$repo" config filter.malicious.clean "'
                        + str(hook) + '"\n'
                    )
                    script.write_text(
                        source.replace(anchor, anchor + injection, 1),
                        encoding="utf-8",
                    )
                    config = subprocess.run(
                        ["git", "-C", str(repo), "config", "--unset-all",
                         "filter.malicious.clean"],
                        capture_output=True, text=True,
                    )
                    self.assertIn(config.returncode, (0, 5), config.stderr)
                    readme = repo / "README.md"
                    readme.write_text("attested-changes\n", encoding="utf-8")
                    stamp = readme.stat()
                    os.utime(readme, (stamp.st_atime, stamp.st_mtime + 90))
                    p = subprocess.run(
                        ["/bin/sh", str(script), *(
                            [expected] if name.endswith("-build.sh") else
                            ["verify", "sha256:" + "a" * 64, expected]
                        )],
                        capture_output=True, text=True,
                        env={**os.environ, "MARK_UV": "/usr/bin/false"},
                    )
                    self.assertNotEqual(p.returncode, 0)
                    self.assertFalse(
                        marker.exists(),
                        f"{name} executed a Git filter introduced after preflight",
                    )
                    # Prove this is a working executable filter fixture.
                    marker.unlink(missing_ok=True)
                    stamp = readme.stat()
                    os.utime(readme, (stamp.st_atime, stamp.st_mtime + 90))
                    check = subprocess.run(
                        ["git", "-C", str(repo), "status", "--porcelain"],
                        capture_output=True, text=True,
                    )
                    self.assertEqual(check.returncode, 0, check.stderr)
                    self.assertTrue(
                        marker.exists(),
                        f"race fixture was not executable: script_stderr={p.stderr!r}; "
                        f"status={check.stdout!r}",
                    )

    def test_untrusted_git_clean_filter_cannot_execute(self) -> None:
        with TemporaryDirectory(prefix="mark-git-clean-filter-") as tmp:
            root = Path(tmp)
            repo, _ = self._fake_git_repo(root)
            (repo / ".gitattributes").write_text(
                "README.md filter=malicious\n", encoding="utf-8",
            )

            def git(*args: str) -> str:
                result = subprocess.run(
                    ["git", "-C", str(repo), *args],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            git("add", ".gitattributes")
            git("commit", "-qm", "trusted attributes")
            expected = git("rev-parse", "HEAD")
            marker = root / "filter-executed"
            hook = root / "filter.sh"
            hook.write_text(
                "#!/bin/sh\n"
                + "printf executed > '" + str(marker) + "'\n"
                + "exec /bin/cat\n",
                encoding="utf-8",
            )
            hook.chmod(0o700)
            git("config", "filter.malicious.clean", str(hook))
            for script, arguments in (
                (repo / "docs/mark-api-container-build.sh", [expected]),
                (repo / "docs/mark-api-container-control.sh",
                 ["verify", "sha256:" + "a" * 64, expected]),
            ):
                with self.subTest(script=script.name):
                    marker.unlink(missing_ok=True)
                    readme = repo / "README.md"
                    stamp = readme.stat()
                    # Force Git to compare a tracked worktree file to its index.
                    os.utime(readme, (stamp.st_atime, stamp.st_mtime + 90))
                    # Prove the fixture is executable before testing isolation.
                    check = subprocess.run(
                        ["git", "-C", str(repo), "status", "--porcelain"],
                        capture_output=True, text=True,
                    )
                    self.assertEqual(check.returncode, 0, check.stderr)
                    self.assertTrue(marker.exists(), "clean-filter fixture never executed")
                    marker.unlink()
                    stamp = readme.stat()
                    os.utime(readme, (stamp.st_atime, stamp.st_mtime + 90))
                    completed = subprocess.run(
                        ["/bin/sh", str(script), *arguments],
                        capture_output=True, text=True,
                        env={**os.environ, "MARK_UV": "/usr/bin/false"},
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertFalse(marker.exists(),
                        f"{script.name} executed checkout-local filter.clean")

    def test_build_never_uses_forged_loose_source_blob(self) -> None:
        with TemporaryDirectory(prefix="mark-forged-build-blob-") as tmp:
            repo, expected = self._fake_git_repo(Path(tmp))
            marker = Path(tmp) / "wheel-builder-invoked"
            wheel = Path(tmp) / "fake-wheel-builder.sh"
            wheel.write_text("#!/bin/sh\ntouch " + str(marker) + "\nexit 77\n")
            wheel.chmod(0o700)
            blob_id = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", expected + ":README.md"],
                text=True,
            ).strip()
            loose = repo / ".git/objects" / blob_id[:2] / blob_id[2:]
            self.assertTrue(loose.is_file())
            loose.chmod(0o600)
            forged = b"forged build source bytes\n"
            loose.write_bytes(zlib.compress(
                b"blob " + str(len(forged)).encode("ascii") + b"\0" + forged,
            ))
            (repo / "README.md").write_bytes(forged)
            result = subprocess.run(
                ["/bin/sh", str(repo / "docs/mark-api-container-build.sh"),
                 expected],
                capture_output=True, text=True,
                env={**os.environ, "MARK_UV": str(wheel)},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists(),
                "wheel builder must not process unverified source bytes")
            self.assertTrue(
                "source checkout is dirty" in result.stderr
                or "attested Git object content mismatch" in result.stderr,
                result.stderr,
            )
            self.assertNotIn("pinned setuptools wheel is required", result.stderr)
            self.assertNotIn("untrusted uv binary override", result.stderr)

    def test_build_verified_source_reaches_pinned_backend_admission(self) -> None:
        with TemporaryDirectory(prefix="mark-build-verified-source-") as tmp:
            repo, expected = self._fake_git_repo(Path(tmp))
            result = subprocess.run(
                ["/bin/sh", str(repo / "docs/mark-api-container-build.sh"),
                 expected],
                capture_output=True, text=True,
                env={**os.environ, "MARK_SETUPTOOLS_WHEEL": ""},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("pinned setuptools wheel is required", result.stderr)
            self.assertNotIn("attested Git object content mismatch", result.stderr)
            self.assertNotIn("source checkout is dirty", result.stderr)

    def test_control_rejects_forged_loose_compose_blob(self) -> None:
        with TemporaryDirectory(prefix="mark-forged-git-blob-") as tmp:
            repo, _ = self._fake_git_repo(Path(tmp))
            compose = repo / "docs/mark-api-container.compose.yaml"
            compose.write_bytes(b"services:\n  mark-api:\n    image: sha256:trusted\n")

            def git(*args: str) -> str:
                p = subprocess.run(
                    ["git", "-C", str(repo), *args],
                    capture_output=True, text=True,
                )
                self.assertEqual(p.returncode, 0, p.stderr)
                return p.stdout.strip()

            git("add", "docs/mark-api-container.compose.yaml")
            git("commit", "-qm", "trusted compose blob")
            expected = git("rev-parse", "HEAD")
            blob_id = git(
                "rev-parse", expected + ":docs/mark-api-container.compose.yaml",
            )
            self.assertEqual(len(blob_id), 40)
            loose = repo / ".git/objects" / blob_id[:2] / blob_id[2:]
            self.assertTrue(loose.is_file())
            loose.chmod(0o600)
            untrusted = b"services:\n  mark-api:\n    privileged: true\n"
            loose.write_bytes(zlib.compress(
                b"blob " + str(len(untrusted)).encode("ascii")
                + b"\0" + untrusted,
            ))
            # This current-Git fixture is a real hash/content mismatch:
            # cat-file/show will display the forged bytes under the old OID.
            shown = subprocess.run(
                ["git", "-C", str(repo), "show",
                 expected + ":docs/mark-api-container.compose.yaml"],
                capture_output=True,
            )
            # Git versions differ: this host's Git 2.34 rejects the forged
            # loose object, while the reviewed Git 2.43 can return its bytes.
            if shown.returncode == 0:
                self.assertEqual(shown.stdout, untrusted)
            else:
                self.assertTrue(shown.stderr)

            result = subprocess.run(
                ["/bin/sh", str(repo / "docs/mark-api-container-control.sh"),
                 "verify", "sha256:" + "a" * 64, expected],
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "attested Git object content mismatch", result.stderr,
            )

    def test_control_uses_committed_compose_bytes_not_mutable_worktree(self) -> None:
        control = _read("mark-api-container-control.sh")
        for statement in (
            'attest_object commit "$expected"',
            'attest_object tree "$root_tree"',
            'attest_object tree "$docs_tree"',
            'attest_object blob "$compose_blob" "$compose"',
            'private_git hash-object -t "$object_type" --stdin',
            'private_git hash-object -w -t "$object_type" --stdin',
        ):
            self.assertIn(statement, control)
        self.assertNotIn(
            'attested_git show "$expected:docs/mark-api-container.compose.yaml"',
            control,
        )
        self.assertIn('private_git ls-tree "$root_tree" -- docs', control)
        self.assertIn('private_git ls-tree "$docs_tree" -- mark-api-container.compose.yaml', control)
        self.assertNotIn('docs_tree=$(attested_git ls-tree', control)
        self.assertNotIn('compose_blob=$(attested_git ls-tree', control)
        self.assertIn('compose="$attest_dir/mark-api-container.compose.yaml"', control)
        self.assertNotIn('compose="$repo/docs/mark-api-container.compose.yaml"', control)
        self.assertIn("--project-name mark-api", control)
        self.assertNotIn('exec "$docker"', control)
        self.assertIn(
            "up --no-deps --no-build --pull never --detach mark-api ||\n"
            "                fail 'product frontend failed to start'\n"
            "            product_id=$(docker_cli compose",
            control,
        )

        with TemporaryDirectory(prefix="mark-compose-after-check-") as tmp:
            repo, _ = self._fake_git_repo(Path(tmp))
            compose = repo / "docs/mark-api-container.compose.yaml"
            trusted = b"services:\n  mark-api:\n    image: sha256:trusted\n"
            compose.write_bytes(trusted)
            for args in (("add", "docs/mark-api-container.compose.yaml"),
                         ("commit", "-qm", "trusted compose")):
                result = subprocess.run(
                    ["git", "-C", str(repo), *args],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            expected = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
            ).strip()
            compose.write_bytes(b"services:\n  mark-api:\n    privileged: true\n")
            committed = subprocess.run(
                ["git", "-C", str(repo), "show",
                 expected + ":docs/mark-api-container.compose.yaml"],
                capture_output=True,
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)
            self.assertEqual(committed.stdout, trusted)
            self.assertNotEqual(compose.read_bytes(), trusted)

    def test_documented_product_run_uses_only_guarded_image_actions(self) -> None:
        runbook = _read("operations-runbook.md")
        self.assertIn('sh docs/mark-api-container-control.sh verify', runbook)
        self.assertIn('sh docs/mark-api-container-control.sh live', runbook)
        self.assertIn('sh docs/mark-api-container-control.sh backup', runbook)
        self.assertNotIn(
            "docker compose -f docs/mark-api-container.compose.yaml --profile live up",
            runbook,
        )
        guard = _read("mark-api-container-control.sh")
        self.assertIn("getent passwd 50042", guard)
        self.assertIn("getent group 50042", guard)
        self.assertIn("volume inspect", guard)
        self.assertIn("org.opencontainers.image.revision", guard)

    def test_control_never_executes_caller_selected_compose_plugin(self) -> None:
        control = _read("mark-api-container-control.sh")
        self.assertIn("docker_cli() {", control)
        self.assertIn('DOCKER_CONFIG="$attest_dir/docker-config"', control)
        self.assertIn('/usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent', control)
        self.assertNotIn('"$docker" --host unix:///var/run/docker.sock compose', control)
        start = control.index("docker_cli() {")
        end = control.index("\n}", start) + 2
        wrapper = control[start:end]
        with TemporaryDirectory(prefix="mark-compose-plugin-p1-") as tmp:
            root = Path(tmp)
            private = root / "attested"
            (private / "docker-config").mkdir(parents=True, mode=0o700)
            attacker = root / "attacker-config"
            plugins = attacker / "cli-plugins"
            plugins.mkdir(parents=True)
            marker = root / "plugin-executed"
            malicious = plugins / "docker-compose"
            malicious.write_text(
                "#!/bin/sh\n"
                + "printf exploited > " + str(marker) + "\n"
                + "exit 77\n", encoding="utf-8",
            )
            malicious.chmod(0o755)
            observed = root / "docker-env"
            fake_docker = root / "docker-stub"
            fake_docker.write_text(
                "#!/bin/sh\n"
                + "printf '%s\n' \"$DOCKER_CONFIG\" \"${DOCKER_HOST-unset}\" "
                + "> " + str(observed) + "\n"
                + 'if [ -x "$DOCKER_CONFIG/cli-plugins/docker-compose" ]; then\n'
                + '  exec "$DOCKER_CONFIG/cli-plugins/docker-compose"\n'
                + "fi\n"
                + "exit 0\n", encoding="utf-8",
            )
            fake_docker.chmod(0o700)
            script = (
                "set -eu\n"
                + "attest_dir='" + str(private) + "'\n"
                + "digest=" + "a" * 64 + "\n"
                + wrapper.replace("/usr/bin/docker", str(fake_docker))
                + "\ndocker_cli compose version\n"
            )
            result = subprocess.run(
                ["/bin/sh", "-c", script],
                env={
                    **os.environ,
                    "DOCKER_CONFIG": str(attacker),
                    "DOCKER_HOST": "tcp://localhost:1234",
                },
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())
            self.assertEqual(
                observed.read_text(encoding="utf-8").splitlines(),
                [str(private / "docker-config"), "unset"],
            )