"""Opt-in real Docker UID/DAC negative: raw sealer cannot bypass protected intent.

All data and volumes are synthetic and uniquely named. The real-Docker suite
fails closed before resource creation because this host has no independently
attested exclusive Docker-admin context: deleting a named volume after an
inspect is not atomic. The suite cannot be claimed as passing or executed on
a shared daemon until that security boundary is implemented and verified.
A compromised executor UID 50044 receives even RW mounts of every protected
directory and the *correct* independent input hashes, but never becomes UID
50043. The installed image is selected by immutable sha256 ID and never pulled.

This is not a host FD/MAP_SHARED revocation, real source quiescence, privileged
operator admission, live restore, or Kleinanzeigen platform-write test.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
from uuid import uuid4
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("MARK_RAW_SEALER_UID_TEST_IMAGE", "")
IMAGE_OK = bool(re.fullmatch(r"sha256:[0-9a-f]{64}", IMAGE))
PYTHON = "/opt/mark-api/venv/bin/python"
if IMAGE and not IMAGE_OK:
    raise ValueError("MARK_RAW_SEALER_UID_TEST_IMAGE must be an immutable image ID")
if os.environ.get("MARK_REQUIRE_RAW_SEALER_UID") == "1" and not IMAGE_OK:
    raise ValueError("required raw-sealer UID test has no pinned local image")


def _docker(*args: str, allow_failure: bool = False,
            timeout_seconds: float = 90) -> subprocess.CompletedProcess[str]:
    # Docker exposes only name-based deletion for volumes. An administrator
    # can replace a correctly labelled volume after inspect but before rm;
    # neither a second inspect nor a final 404 prevents foreign deletion.
    # This suite has no independent exclusive-daemon attestation yet.
    if args[:2] in (("volume", "rm"), ("volume", "prune")):
        raise AssertionError(
            "name-based Docker volume deletion is unsafe without an "
            "independently enforced exclusive Docker-admin authority"
        )
    # Only passive daemon metadata is admissible on a shared Docker socket.
    # A nested TestCase.run() or a direct test method can bypass setUpClass,
    # so protect the subprocess boundary in addition to unittest setup.
    if not (
        args[:1] == ("info",)
        or args[:2] in (
            ("image", "inspect"),
            ("container", "inspect"),
            ("volume", "inspect"),
        )
    ):
        raise AssertionError(
            "exclusive Docker-admin authority not established: "
            "Docker resource mutation forbidden before subprocess"
        )
    result = subprocess.run(
        ["/usr/bin/docker", *args], capture_output=True, text=True,
        check=False, timeout=timeout_seconds,
    )
    if result.returncode and not allow_failure:
        raise AssertionError("isolated Docker operation failed: " + result.stderr[-900:])
    return result


def _docker_reports_missing(
    result: subprocess.CompletedProcess[str], kind: str, name: str,
) -> bool:
    """Only exact Docker not-found replies establish resource absence."""
    if result.returncode == 0 or result.stdout.strip() not in ("", "[]"):
        return False
    if kind == "volume":
        accepted = (
            f"Error response from daemon: get {name}: no such volume",
            f"Error: No such volume: {name}",
        )
    elif kind == "container":
        accepted = (
            f"Error response from daemon: No such container: {name}",
            f"Error: No such container: {name}",
            f"Error: No such object: {name}",
        )
    else:
        raise ValueError("unknown Docker resource kind")
    return result.stderr.strip() in accepted


def _remove_owned_synthetic_container(
    name: str, run_suffix: str, original_create_id: str, docker,
) -> None:
    """Best-effort cleanup of one test container, never by its mutable name.

    This is NOT proof that a previously timed-out daemon-side run is terminal.
    A completed original create ACK must pin this test-owned immutable ID;
    merely matching the name and run label may identify a foreign decoy.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", original_create_id):
        raise AssertionError("original synthetic create identity not proven")
    observed = docker(
        "container", "inspect", "--format",
        '{{.Id}} {{index .Config.Labels "mark.a6.rawseal.run"}}',
        name, allow_failure=True,
    )
    if observed.returncode:
        if _docker_reports_missing(observed, "container", name):
            return
        raise AssertionError("synthetic container identity not proven")
    fields = observed.stdout.strip().split()
    if (len(fields) != 2
            or not re.fullmatch(r"[0-9a-f]{64}", fields[0])
            or fields[0] != original_create_id
            or fields[1] != run_suffix):
        raise AssertionError("refuse to remove foreign synthetic container")
    docker("rm", "-f", fields[0], allow_failure=True)
    final = docker("container", "inspect", name, allow_failure=True)
    if not _docker_reports_missing(final, "container", name):
        raise AssertionError("synthetic container removal not proven absent")


_TRUSTED_SEAL = r"""
import json,os,stat
from pathlib import Path
import mark_api
from mark_api.storage import SnapshotStore
from mark_api.fence_witness import FenceLedger
from mark_api.backup_intent_authority import BackupIntentAuthority

assert os.geteuid()==50043
assert Path(mark_api.__file__).as_posix().startswith('/src/mark_api/')
os.umask(0o022)
source=Path('/source')
sqlite=source/'mark.sqlite'
witness=source/'witness.sqlite'
SnapshotStore(sqlite)
with FenceLedger(witness,initialize=True):
    pass
op=BackupIntentAuthority(Path('/intent'))
generation='1'*32
prepared=op.prepare(generation,sqlite,witness,previous_head_sha256=None)
result=op.seal_prepared(generation,Path('/output'),Path('/anchor'))
assert result['external_operator_intent_acknowledged'] is True
assert result['restore_authorized'] is False
assert not (Path('/anchor')/'.inflight').exists()
for path in (sqlite,witness,Path('/anchor/latest.json'),
             Path('/intent/.intent.lock'),
             Path('/intent')/(generation+'.acknowledged.json')):
    st=path.stat()
    assert st.st_uid==50043 and stat.S_IMODE(st.st_mode)==0o600,(path,st)
print('PINNED_TRUSTED_SEAL='+json.dumps({
    'sqlite_sha256':prepared['sqlite_sha256'],
    'witness_sha256':prepared['witness_sha256'],
    'head_sha256':result['head_sha256'],
},sort_keys=True),flush=True)
"""


_RELAX = r"""
import os,stat
from pathlib import Path
assert os.geteuid()==50043
for name in ('source','output','anchor','intent'):
    p=Path('/'+name)
    assert stat.S_IMODE(p.stat().st_mode)==0o700
    p.chmod(0o711)
print('TRUSTED_TEST_ROOTS_TRAVERSABLE_NO_FILE_DAC_CHANGE',flush=True)
"""


_DAC_PROBE = r"""
import errno,os
from pathlib import Path
assert os.geteuid()==50044

# Positive control: UID50044 can really mutate a separately owned RW volume.
control=Path('/control/raw-sealer-attacker-probe')
control.write_bytes(b'RW_UID50044_CONFIRMED')
assert control.read_bytes()==b'RW_UID50044_CONFIRMED'

# Protected volumes are ALSO mounted RW, and the trusted harness has enabled
# traversal. Permission denial must come from actual owner-created artifacts.
for name in ('source','output','anchor','intent'):
    assert os.access('/'+name,os.X_OK),name
for item in ('/source/mark.sqlite','/source/witness.sqlite',
             '/anchor/latest.json','/intent/.intent.lock'):
    assert Path(item).stat().st_size>=0,item
    for flags in (os.O_RDONLY,os.O_WRONLY):
        try:
            fd=os.open(item,flags|os.O_NOFOLLOW|os.O_CLOEXEC)
        except OSError as exc:
            assert exc.errno in (errno.EACCES,errno.EPERM),(item,exc)
        else:
            os.close(fd)
            raise AssertionError('foreign UID opened protected file '+item)

print('ROOTS_0711_FILE_DAC_DENIED_WITH_RW_POSITIVE_CONTROL',flush=True)
"""


_ATTACK = r"""
import errno,os,re,stat
from pathlib import Path
import mark_api
from mark_api.paired_backup_operator import seal_offline_pair,PairedBackupOperatorError
from mark_api.backup_intent_authority import BackupIntentAuthority

assert os.geteuid()==50044
assert Path(mark_api.__file__).as_posix().startswith('/src/mark_api/')
sha_sql=os.environ['MARK_TEST_SQLITE_SHA']
sha_witness=os.environ['MARK_TEST_WITNESS_SHA']
sha_head=os.environ['MARK_TEST_HEAD_SHA']
for value in (sha_sql,sha_witness,sha_head):
    assert re.fullmatch(r'[0-9a-f]{64}',value),value

# The raw API must be rejected when the actual trusted private roots
# have their original owner/mode (50043/0700), not merely because the trusted
# harness temporarily relaxed them to 0711 for direct file-DAC probing.
for name in ('source','output','anchor','intent'):
    info=Path('/'+name).stat()
    assert info.st_uid==50043,(name,info)
    assert stat.S_IMODE(info.st_mode)==0o700,(name,info)
assert Path('/control/raw-sealer-attacker-probe').read_bytes()==b'RW_UID50044_CONFIRMED'

print('ACTUAL_PRODUCT_RAW_SEALER_IMPORTED_WITH_CORRECT_SOURCE_DIGESTS',flush=True)
# Crucial: the test is not just checking a bad digest or missing code path.
# Run the exact installed Python implementation, with all valid digests and
# writable mounts. An untrusted UID must have no authority to publish HEAD.
try:
    seal_offline_pair(
        Path('/source/mark.sqlite'),Path('/source/witness.sqlite'),
        Path('/output'),Path('/anchor'),
        expected_sqlite_sha256=sha_sql,
        expected_witness_sha256=sha_witness,
        expected_previous_head_sha256=sha_head,
        generation='2'*32,
    )
except PairedBackupOperatorError as exc:
    assert 'operator directory is not privately owned' in str(exc),str(exc)
else:
    raise AssertionError('hostile UID published raw second generation')
print('RAW_SEALER_REFUSED_FOREIGN_UID_WITH_REAL_DIGESTS',flush=True)

# A forged external intent is equally unavailable to the compromised UID.
try:
    BackupIntentAuthority(Path('/intent'))
except PairedBackupOperatorError as exc:
    assert 'operator directory is not privately owned' in str(exc),str(exc)
else:
    raise AssertionError('hostile UID acquired protected operator authority')
print('OPERATOR_INTENT_AUTHORITY_DENIED_TO_EXECUTOR_UID',flush=True)

for path in ('/intent/22222222222222222222222222222222.prepared.json',
             '/anchor/.inflight','/output/22222222222222222222222222222222'):
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except OSError as exc:
        assert exc.errno in (errno.EACCES,errno.EPERM),(path,exc)
    else:
        os.close(fd)
        raise AssertionError('hostile UID created recovery authority '+path)
print('HOSTILE_UID50044_RAW_SEALER_AND_INTENT_WRITE_DENIED',flush=True)
"""


_RESTORE = r"""
import os,stat
from pathlib import Path
assert os.geteuid()==50043
for name in ('source','output','anchor','intent'):
    p=Path('/'+name)
    assert stat.S_IMODE(p.stat().st_mode)==0o711
    p.chmod(0o700)
print('TRUSTED_TEST_ROOTS_RESET_0700',flush=True)
"""


_RECHECK = r"""
import hashlib,json,os
from pathlib import Path
from mark_api.paired_restore_preflight import verify_offline_pair
from mark_api.backup_intent_authority import BackupIntentAuthority,BackupIntentError
assert os.geteuid()==50043
expected=os.environ['MARK_TEST_HEAD_SHA']
head=Path('/anchor/latest.json')
assert hashlib.sha256(head.read_bytes()).hexdigest()==expected
record=json.loads(head.read_bytes())
generation='1'*32
assert record['generation']==generation
assert not (Path('/output')/('2'*32)).exists()
assert not (Path('/anchor')/(('2'*32)+'.json')).exists()
assert not (Path('/anchor')/'.inflight').exists()
assert not (Path('/intent')/(('2'*32)+'.prepared.json')).exists()
root=Path('/output')/generation
pair=verify_offline_pair(
    root/'mark.sqlite',root/'witness.sqlite',root/'manifest.json',
    expected_manifest_sha256=record['manifest_sha256'],
)
assert pair['generation']==generation
op=BackupIntentAuthority(Path('/intent'))
try:
    op.acknowledge(generation,Path('/output'),Path('/anchor'))
except BackupIntentError as exc:
    assert 'no unconsumed' in str(exc),str(exc)
else:
    raise AssertionError('acknowledged external intent replayed')
print('TRUSTED_ANCHOR_PAIR_AND_ONE_SHOT_INTENT_UNCHANGED',flush=True)
"""


def _require_exclusive_docker_volume_authority() -> None:
    """Never create named test volumes if safe cleanup cannot be guaranteed.

    No independently enforced Docker-daemon exclusivity or immutable
    volume-delete primitive has been verified for this test harness.
    An opt-in image or environment variable is NOT security attestation.
    """
    message = (
        "exclusive Docker-admin authority not established: named volume "
        "cleanup would be subject to foreign name-reuse deletion"
    )
    if os.environ.get("MARK_REQUIRE_RAW_SEALER_UID") == "1":
        raise AssertionError(message)
    raise unittest.SkipTest(message)


@unittest.skipUnless(IMAGE_OK, "Explicit immutable MARK_RAW_SEALER_UID_TEST_IMAGE required")
class RawSealerHostileUidDockerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _require_exclusive_docker_volume_authority()

    def setUp(self) -> None:
        # Unlike setUpClass, this always runs for direct TestCase.run() calls.
        super().setUp()
        _require_exclusive_docker_volume_authority()

    def test_hostile_uid_with_all_rw_mounts_cannot_invoke_raw_sealer(self) -> None:
        # Guard direct/nested TestCase.run() calls which skip setUpClass().
        _require_exclusive_docker_volume_authority()
        suffix = uuid4().hex[:12]
        volumes = {
            key: "mark-rawseal-" + suffix + "-" + key
            for key in ("source", "output", "anchor", "intent", "control")
        }
        created: list[str] = []
        named_containers: list[str] = []
        # A CLI timeout/error does not terminate an in-flight daemon request.
        # Never infer terminality solely from later inspection 404 replies.
        unsettled: set[tuple[str, str]] = set()
        with tempfile.TemporaryDirectory(prefix="mark-rawseal-real-uid-") as tmp:
            root = Path(tmp)
            head = subprocess.run(
                ["git", "-C", str(REPO), "rev-parse", "--verify", "HEAD"],
                check=True, text=True, capture_output=True, timeout=10,
            ).stdout.strip()
            self.assertRegex(head, r"^[0-9a-f]{40}$")
            source_clean = subprocess.run(
                ["git", "-C", str(REPO), "status", "--porcelain", "--",
                 "src/mark_api"],
                check=True, text=True, capture_output=True, timeout=10,
            )
            self.assertFalse(source_clean.stdout.strip(),
                             "product source differs from committed HEAD")
            # Never install mutable, ignored or untracked product code into
            # the test container: exact tracked modules only.
            archive = subprocess.run(
                ["git", "-C", str(REPO), "archive", "--format=tar",
                 head, "src/mark_api"],
                check=True, capture_output=True, timeout=30,
            ).stdout
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as pack:
                pack.extractall(root, filter="data")
            product = root / "src" / "mark_api"
            self.assertTrue((product / "__init__.py").is_file())
            for path in (root, product.parent, *product.parent.rglob("*")):
                if path.is_dir():
                    path.chmod(0o755)
                elif path.is_file():
                    path.chmod(0o644)

            label_key = "mark.a6.rawseal.run"
            label_value = label_key + "=" + suffix
            mounts: list[str] = []
            for key, volume in volumes.items():
                mounts += [
                    "--mount", f"type=volume,source={volume},target=/{key},volume-nocopy",
                ]
            module_bind = [
                "--mount", f"type=bind,source={product.parent},target=/src,readonly",
                "-e", "PYTHONPATH=/src",
            ]

            def run_python(label: str, uid: int, script: str,
                           *, evidence: dict[str, str] | None = None
                           ) -> subprocess.CompletedProcess[str]:
                name = "mark-rawseal-" + suffix + "-" + label
                prior = _docker("container", "inspect", name, allow_failure=True)
                self.assertTrue(
                    _docker_reports_missing(prior, 'container', name),
                    'synthetic container name not proven absent or Docker unavailable',
                )
                named_containers.append(name)  # before a potentially uncertain Docker run
                unsettled.add(("container", name))
                args = [
                    "run", "--rm", "--pull", "never", "--name", name,
                    "--label", label_value,
                    "--network", "none", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true",
                    "--user", f"{uid}:{uid}", "--workdir", "/",
                    "-e", "PYTHONDONTWRITEBYTECODE=1",
                    *module_bind,
                ]
                if evidence:
                    args.extend(item for k, v in evidence.items()
                                for item in ("-e", k + "=" + v))
                completed = _docker(
                    *args, "--entrypoint", PYTHON, *mounts,
                    IMAGE, "-B", "-c", script,
                )
                if completed.returncode:
                    raise AssertionError("docker run did not complete successfully")
                unsettled.remove(("container", name))
                return completed

            try:
                self.assertEqual(
                    _docker("image", "inspect", "--format", "{{.Id}}",
                            IMAGE).stdout.strip(), IMAGE,
                )
                security = _docker(
                    "info", "--format", "{{json .SecurityOptions}}",
                ).stdout.lower()
                self.assertNotIn("name=userns", security)
                self.assertNotIn("name=rootless", security)
                for volume in volumes.values():
                    prior = _docker("volume", "inspect", volume,
                                    allow_failure=True)
                    self.assertTrue(
                        _docker_reports_missing(prior, 'volume', volume),
                        'synthetic volume name not proven absent or Docker unavailable',
                    )
                    # Register the unique name BEFORE Docker starts. A daemon
                    # may have created it even when the CLI later times out.
                    created.append(volume)
                    unsettled.add(("volume", volume))
                    complete = _docker("volume", "create", "--label", label_value, volume)
                    if complete.returncode:
                        raise AssertionError("docker volume create did not complete")
                    unsettled.remove(("volume", volume))
                    driver = _docker(
                        "volume", "inspect", "--format",
                        '{{.Driver}} {{json .Options}} '
                        '{{index .Labels "mark.a6.rawseal.run"}}', volume,
                    ).stdout.strip()
                    self.assertIn(driver,
                                  ("local null " + suffix, "local {} " + suffix))
                # Root touches *only* the disposable volume directory ownership.
                # No host UID, global Docker options or services are changed.
                seed_name = "mark-rawseal-" + suffix + "-seed"
                prior = _docker("container", "inspect", seed_name,
                                allow_failure=True)
                self.assertTrue(
                    _docker_reports_missing(prior, 'container', seed_name),
                    'synthetic seed container not proven absent or Docker unavailable',
                )
                named_containers.append(seed_name)  # before uncertain run
                unsettled.add(("container", seed_name))
                seeded = _docker(
                    "run", "--rm", "--pull", "never", "--name", seed_name,
                    "--label", label_value,
                    "--network", "none", "--read-only", "--user", "0:0",
                    "--cap-drop", "ALL", "--cap-add", "CHOWN",
                    *mounts, "--entrypoint", "/bin/sh", IMAGE, "-ec",
                    "for dir in /source /output /anchor /intent /control; do "
                    "test -z \"$(ls -A \"$dir\")\" || exit 1; "
                    "done; "
                    "chmod 0700 /source /output /anchor /intent /control;"
                    "chown 50043:50043 /source /output /anchor /intent; "
                    "chown 50044:50044 /control; "
                )
                if seeded.returncode:
                    raise AssertionError("seed docker run did not complete")
                unsettled.remove(("container", seed_name))
                trusted = run_python("seal", 50043, _TRUSTED_SEAL)
                matched = re.search(
                    r"^PINNED_TRUSTED_SEAL=(\{[^\n]+\})$",
                    trusted.stdout, flags=re.MULTILINE,
                )
                self.assertIsNotNone(matched, trusted.stdout)
                prepared = json.loads(matched.group(1))
                self.assertEqual(set(prepared),
                                 {"sqlite_sha256", "witness_sha256", "head_sha256"})
                self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", v)
                                    for v in prepared.values()))
                relaxed = run_python("relax", 50043, _RELAX)
                self.assertIn("TRUSTED_TEST_ROOTS_TRAVERSABLE", relaxed.stdout)
                env = {
                    "MARK_TEST_SQLITE_SHA": prepared["sqlite_sha256"],
                    "MARK_TEST_WITNESS_SHA": prepared["witness_sha256"],
                    "MARK_TEST_HEAD_SHA": prepared["head_sha256"],
                }
                try:
                    direct = run_python("attack-dac", 50044, _DAC_PROBE)
                    self.assertIn(
                        "ROOTS_0711_FILE_DAC_DENIED_WITH_RW_POSITIVE_CONTROL",
                        direct.stdout,
                    )
                finally:
                    restored = run_python("restore", 50043, _RESTORE)
                    self.assertIn("TRUSTED_TEST_ROOTS_RESET_0700", restored.stdout)
                attacked = run_python("attack-raw", 50044, _ATTACK, evidence=env)
                self.assertIn(
                    "HOSTILE_UID50044_RAW_SEALER_AND_INTENT_WRITE_DENIED",
                    attacked.stdout,
                )
                self.assertIn(
                    "ACTUAL_PRODUCT_RAW_SEALER_IMPORTED_WITH_CORRECT_SOURCE_DIGESTS",
                    attacked.stdout,
                )
                checked = run_python("recheck", 50043, _RECHECK, evidence=env)
                self.assertIn(
                    "TRUSTED_ANCHOR_PAIR_AND_ONE_SHOT_INTENT_UNCHANGED",
                    checked.stdout,
                )
                final_head = subprocess.run(
                    ["git", "-C", str(REPO), "rev-parse", "--verify", "HEAD"],
                    check=True, text=True, capture_output=True, timeout=10,
                ).stdout.strip()
                self.assertEqual(final_head, head)
                print("RAW_SEALER_DAC_EXACT_COMMITTED_HEAD=" + head, flush=True)
            finally:
                cleanup_errors: list[str] = []

                def reconcile(kind: str, name: str) -> None:
                    # Only exact names previously checked absent, plus matching
                    # run labels, authorize removal. No wildcard or foreign rm.
                    expr = ('{{.Id}} {{index .Config.Labels "mark.a6.rawseal.run"}}'
                            if kind == "container"
                            else '{{index .Labels "mark.a6.rawseal.run"}}')
                    try:
                        observed = _docker(kind, "inspect", "--format", expr,
                                           name, allow_failure=True,
                                           timeout_seconds=5)
                        if observed.returncode:
                            if _docker_reports_missing(observed, kind, name):
                                return  # absence NOW; not daemon-request completion
                            cleanup_errors.append(
                                name + ": ownership inspect failed, not 404: " +
                                (observed.stdout + observed.stderr)[-180:]
                            )
                            return
                        if kind == "container":
                            # The daemon returns a stable immutable container
                            # ID and label together. Remove ONLY that ID: a
                            # foreign same-name replacement must never be rm'd.
                            identity = observed.stdout.strip().split()
                            if (len(identity) != 2
                                    or not re.fullmatch(r"[a-f0-9]{64}", identity[0])
                                    or identity[1] != suffix):
                                cleanup_errors.append(
                                    name + ": ownership label mismatch; refuse cleanup"
                                )
                                return
                            # The client can time out before the
                            # daemon creates *its* container. A second Docker
                            # actor can create a decoy with the same name and
                            # label, so even seeing/removing the pinned ID
                            # cannot prove the original daemon request ended.
                            # Leave the name and mounts unresolved/NO-GO.
                            if ("container", name) in unsettled:
                                return
                            _docker("rm", "-f", identity[0], allow_failure=True,
                                    timeout_seconds=5)
                        else:
                            if observed.stdout.strip() != suffix:
                                cleanup_errors.append(
                                    name + ": ownership label mismatch; refuse cleanup"
                                )
                                return
                            cleanup_errors.append(
                                name + ": named volume intentionally retained; "
                                "independent exclusive Docker-admin authority "
                                "is required before deletion"
                            )
                            return
                        final = _docker(kind, "inspect", name,
                                        allow_failure=True, timeout_seconds=5)
                        if not _docker_reports_missing(final, kind, name):
                            cleanup_errors.append(
                                name + ": not proven absent after rm: " +
                                (final.stdout + final.stderr)[-180:]
                            )
                    except Exception as exc:
                        cleanup_errors.append(name + ": " + repr(exc))

                for name in reversed(named_containers):
                    reconcile("container", name)

                def bounded_retry(kind: str) -> None:
                    # Never promote finite negative inspection to proof that
                    # the original daemon request has actually terminated.
                    if not any(k == kind for k, _ in unsettled):
                        return
                    deadline = time.monotonic() + 1.5
                    while time.monotonic() < deadline:
                        for k, name in sorted(unsettled):
                            if k == kind:
                                reconcile(k, name)
                        if not any(k == kind for k, _ in unsettled):
                            break
                        remaining = deadline - time.monotonic()
                        if remaining > 0:
                            time.sleep(min(0.12, remaining))

                # Resolve pending Docker runs FIRST. Deleting named volumes
                # while a daemon run can still begin would let a delayed run
                # re-create the names without the private owner run label.
                bounded_retry("container")
                unsettled_runs = sorted(
                    name for kind, name in unsettled if kind == "container"
                )
                if unsettled_runs:
                    # Never delete the volumes still needed by an unknown run.
                    cleanup_errors.append(
                        "named volumes intentionally retained pending "
                        "independent completion of uncertain Docker run: " +
                        repr(unsettled_runs)
                    )
                else:
                    for name in reversed(created):
                        reconcile("volume", name)
                    # The original volume-create request can also complete
                    # after a first exact 404; detect late arrivals but keep
                    # the final verdict NO-GO absent proof of completion.
                    bounded_retry("volume")

                for kind, name in sorted(unsettled):
                    cleanup_errors.append(
                        kind + " " + name +
                        ": unresolved Docker create/run after unclear CLI "
                        "outcome; daemon completion not proven; even "
                        "repeated 404 is not final cleanup evidence"
                    )
                self.assertFalse(
                    cleanup_errors,
                    "unconfirmed cleanup of synthetic Docker resources: " +
                    repr(cleanup_errors),
                )

    def test_created_volume_is_cleaned_when_docker_client_times_out(self) -> None:
        # A CLI TimeoutExpired does not mean the daemon failed to create the
        # requested volume. Reproduce the effect *after* the first create.
        original = _docker
        created: str | None = None
        injected = False

        def lose_create_ack(*args: str, **kwargs: object
                            ) -> subprocess.CompletedProcess[str]:
            nonlocal created, injected
            if args[:2] == ("volume", "create") and not injected:
                response = original(*args, **kwargs)
                self.assertEqual(response.returncode, 0)
                created = args[-1]
                injected = True
                raise subprocess.TimeoutExpired(
                    cmd=["/usr/bin/docker", *args], timeout=90,
                )
            return original(*args, **kwargs)

        try:
            with patch(__name__ + "._docker", side_effect=lose_create_ack):
                nested = type(self)(
                    "test_hostile_uid_with_all_rw_mounts_cannot_invoke_raw_sealer",
                )
                result = unittest.TestResult()
                nested.run(result)
            self.assertTrue(injected)
            self.assertIsNotNone(created)
            self.assertFalse(result.errors, result.errors)
            self.assertEqual(len(result.failures), 1, result.failures)
            self.assertIn("unresolved Docker create/run", result.failures[0][1])
            absent = original("volume", "inspect", created,
                              allow_failure=True)
            self.assertTrue(
                _docker_reports_missing(absent, "volume", created),
                "uncertain created volume not proven absent or Docker unreachable",
            )
            print("DOCKER_VOLUME_CREATE_TIMEOUT_CLEANED", flush=True)
        finally:
            # A future broken cleanup must not leak even this negative test.
            if created is not None:
                owned = original(
                    "volume", "inspect", "--format",
                    '{{index .Labels "mark.a6.rawseal.run"}}',
                    created, allow_failure=True,
                )
                if owned.returncode == 0:
                    expected_suffix = created.split("-")[2]
                    self.assertEqual(
                        owned.stdout.strip(), expected_suffix,
                        "refuse to remove a non-test Docker volume",
                    )
                    original("volume", "rm", created)
                    self.fail("orphaned test volume required emergency cleanup")


    def test_uncertain_late_run_must_retain_named_volumes(self) -> None:
        # If a run times out *before* daemon-side container creation, a first
        # container 404 is not permission to remove its named mount volumes.
        # Simulate the delayed daemon command and then actually issue that
        # command after cleanup; no unlabeled volume may be auto-created.
        original = _docker
        volumes: list[str] = []
        delayed: tuple[str, ...] | None = None
        replay_submitted = False
        replay_completed = False

        def defer_seed_run(*args: str, **kwargs: object
                           ) -> subprocess.CompletedProcess[str]:
            nonlocal delayed
            if args[:2] == ("volume", "create"):
                result = original(*args, **kwargs)
                volumes.append(args[-1])
                return result
            if args and args[0] == "run" and "--name" in args:
                name = args[args.index("--name") + 1]
                if name.endswith("-seed") and delayed is None:
                    delayed = args
                    raise subprocess.TimeoutExpired(
                        cmd=["/usr/bin/docker", *args], timeout=90,
                    )
            return original(*args, **kwargs)

        try:
            nested = type(self)(
                "test_hostile_uid_with_all_rw_mounts_cannot_invoke_raw_sealer",
            )
            result = unittest.TestResult()
            with patch(__name__ + "._docker", side_effect=defer_seed_run):
                nested.run(result)
            self.assertIsNotNone(delayed)
            self.assertEqual(len(volumes), 5)
            self.assertFalse(result.errors, result.errors)
            self.assertEqual(len(result.failures), 1, result.failures)
            self.assertIn("unresolved Docker create/run", result.failures[0][1])
            # The original request is not known to have finished; keep all
            # five existing run-labelled mounts until externally reconciled.
            for volume in volumes:
                label = original(
                    "volume", "inspect", "--format",
                    '{{index .Labels "mark.a6.rawseal.run"}}',
                    volume, allow_failure=True,
                )
                self.assertEqual(label.returncode, 0,
                                 "pending docker run lost its named mount")
                self.assertEqual(label.stdout.strip(), volume.split("-")[2],
                                 "named mount ownership changed")
            # Now perform the delayed real Docker run against the retained,
            # already correctly labelled volumes.
            replay_submitted = True
            completed = original(*delayed)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            replay_completed = True
            for volume in volumes:
                label = original(
                    "volume", "inspect", "--format",
                    '{{index .Labels "mark.a6.rawseal.run"}}',
                    volume, allow_failure=True,
                )
                self.assertEqual(label.returncode, 0)
                self.assertEqual(label.stdout.strip(), volume.split("-")[2])
        finally:
            # If the replayed Docker CLI times out, it may create the named
            # mounts after this process exits. A 404 or a retained owner label
            # does not prove termination. Do not remove their names here.
            if replay_submitted and not replay_completed:
                self.fail(
                    "unresolved replayed docker run; retain labelled named "
                    "mount volumes until independent daemon completion proof: "
                    + repr(volumes)
                )
            # A real delayed run is executed only if the prior mounts stayed
            # owned. Perform guarded emergency cleanup *after* its completion.
            for volume in reversed(volumes):
                owned = original(
                    "volume", "inspect", "--format",
                    '{{index .Labels "mark.a6.rawseal.run"}}',
                    volume, allow_failure=True,
                )
                if owned.returncode == 0:
                    self.assertEqual(owned.stdout.strip(), volume.split("-")[2],
                                     "never remove an unowned replacement")
                    original("volume", "rm", volume)
                else:
                    self.assertTrue(
                        _docker_reports_missing(owned, "volume", volume),
                        "foreign or uncertain Docker volume: no removal",
                    )
                final = original("volume", "inspect", volume,
                                 allow_failure=True)
                self.assertTrue(_docker_reports_missing(final, "volume", volume))
        print("LATE_RUN_NAMED_MOUNTS_RETAINED_UNTIL_DAEMON_COMPLETED", flush=True)

    def test_replayed_docker_run_timeout_retains_named_mounts(self) -> None:
        # The replay CLI may itself lose an acknowledgement. Simulate that
        # failure *without dispatching the Docker run*: the outer harness can
        # then safely reclaim exactly labelled disposable volumes afterward.
        real = _docker
        created: list[str] = []

        def lose_replay_ack(*args: str, **kwargs: object
                            ) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("volume", "create"):
                result = real(*args, **kwargs)
                created.append(args[-1])
                return result
            if args and args[0] == "run" and "--name" in args:
                name = args[args.index("--name") + 1]
                if name.endswith("-seed"):
                    raise subprocess.TimeoutExpired(
                        cmd=["/usr/bin/docker", *args], timeout=90,
                    )
            return real(*args, **kwargs)

        try:
            nested = type(self)(
                "test_uncertain_late_run_must_retain_named_volumes",
            )
            result = unittest.TestResult()
            with patch(__name__ + "._docker", side_effect=lose_replay_ack):
                nested.run(result)
            self.assertEqual(len(created), 5)
            self.assertFalse(result.errors, result.errors)
            self.assertEqual(len(result.failures), 1, result.failures)
            self.assertIn(
                "unresolved replayed docker run", result.failures[0][1],
            )
            for volume in created:
                label = real(
                    "volume", "inspect", "--format",
                    '{{index .Labels "mark.a6.rawseal.run"}}',
                    volume, allow_failure=True,
                )
                self.assertEqual(label.returncode, 0)
                self.assertEqual(label.stdout.strip(), volume.split("-")[2])
        finally:
            # This simulation knows it did not submit the replay command.
            # Such a guarantee is NOT available to the actual timed-out path.
            for volume in reversed(created):
                label = real(
                    "volume", "inspect", "--format",
                    '{{index .Labels "mark.a6.rawseal.run"}}',
                    volume, allow_failure=True,
                )
                if label.returncode == 0:
                    self.assertEqual(label.stdout.strip(), volume.split("-")[2])
                    real("volume", "rm", volume)
                else:
                    self.assertTrue(
                        _docker_reports_missing(label, "volume", volume),
                    )
                gone = real("volume", "inspect", volume, allow_failure=True)
                self.assertTrue(
                    _docker_reports_missing(gone, "volume", volume),
                )
        print("P2_GREEN_REPLAY_TIMEOUT_RETAINS_LABELLED_MOUNTS", flush=True)

    def test_late_container_after_404_cannot_settle_daemon_request(self) -> None:
        # Docker run has timed out. The *first* cleanup inspect accurately
        # returns 404, then the daemon finishes creating the exact named
        # container. Later bounded rechecks must settle this single run,
        # and only then clean the labelled mount volumes.
        original = _docker
        volumes: list[str] = []
        seed: str | None = None
        created_seed_id: str | None = None
        delayed = False

        def finish_after_first_404(*args: str, **kwargs: object
                                   ) -> subprocess.CompletedProcess[str]:
            nonlocal seed, delayed, created_seed_id
            if args[:2] == ("volume", "create"):
                result = original(*args, **kwargs)
                volumes.append(args[-1])
                return result
            if args and args[0] == "run" and "--name" in args:
                name = args[args.index("--name") + 1]
                if name.endswith("-seed") and seed is None:
                    seed = name
                    return_code = subprocess.TimeoutExpired(
                        cmd=["/usr/bin/docker", *args], timeout=90,
                    )
                    raise return_code
            if (seed is not None and not delayed
                    and args[:2] == ("container", "inspect")
                    and "--format" in args and args[-1] == seed):
                prior = original(*args, **kwargs)
                self.assertTrue(
                    _docker_reports_missing(prior, "container", seed),
                    "first cleanup response must be a real Docker 404",
                )
                suffix = seed.split("-")[2]
                created = original(
                    "create", "--pull", "never", "--name", seed,
                    "--label", "mark.a6.rawseal.run=" + suffix,
                    "--network", "none", "--read-only", "--cap-drop", "ALL",
                    "--entrypoint", "/bin/sh", IMAGE, "-c", "true",
                )
                self.assertEqual(created.returncode, 0, created.stderr)
                created_seed_id = created.stdout.strip()
                self.assertRegex(created_seed_id, r"^[0-9a-f]{64}$")
                delayed = True
                return prior
            return original(*args, **kwargs)

        try:
            trial = type(self)(
                "test_hostile_uid_with_all_rw_mounts_cannot_invoke_raw_sealer",
            )
            result = unittest.TestResult()
            with patch(__name__ + "._docker",
                       side_effect=finish_after_first_404):
                trial.run(result)
            self.assertTrue(delayed)
            self.assertEqual(len(volumes), 5)
            self.assertFalse(result.errors, result.errors)
            self.assertEqual(len(result.failures), 1, result.failures)
            self.assertIn("unresolved Docker create/run", result.failures[0][1])
            for volume in volumes:
                retained = original(
                    "volume", "inspect", "--format",
                    '{{index .Labels "mark.a6.rawseal.run"}}',
                    volume, allow_failure=True,
                )
                self.assertEqual(retained.returncode, 0,
                                 "do not drop mounts for a possibly spoofed run")
                self.assertEqual(retained.stdout.strip(), volume.split("-")[2])
        finally:
            if seed is not None:
                self.assertIsNotNone(created_seed_id, "no completed create identity")
                _remove_owned_synthetic_container(
                    seed, seed.split("-")[2], created_seed_id, original,
                )
            for volume in reversed(volumes):
                observed = original(
                    "volume", "inspect", "--format",
                    '{{index .Labels "mark.a6.rawseal.run"}}',
                    volume, allow_failure=True,
                )
                if observed.returncode == 0:
                    self.assertEqual(
                        observed.stdout.strip(), volume.split("-")[2],
                        "never remove an untrusted Docker volume",
                    )
                    original("volume", "rm", volume)
                else:
                    self.assertTrue(
                        _docker_reports_missing(observed, "volume", volume),
                        "cannot attest synthetic volume for emergency cleanup",
                    )
                final = original("volume", "inspect", volume,
                                 allow_failure=True)
                self.assertTrue(_docker_reports_missing(final, "volume", volume))
        print("P2_GREEN_LATE_DECOY_AFTER_404_STILL_UNRESOLVED", flush=True)

    def test_recognized_timeout_run_decoy_stays_no_go(self) -> None:
        # A timed-out run may have created its named, owned container.
        # Exact positive label+terminal removal proves that *this* request
        # produced the container, allowing the five mounts to be reclaimed.
        original = _docker
        volumes: list[str] = []
        seed: str | None = None
        created_seed_id: str | None = None
        intercepted = False

        def create_then_lose_ack(*args: str, **kwargs: object
                                 ) -> subprocess.CompletedProcess[str]:
            nonlocal seed, intercepted, created_seed_id
            if args[:2] == ("volume", "create"):
                response = original(*args, **kwargs)
                volumes.append(args[-1])
                return response
            if args and args[0] == "run" and "--name" in args:
                name = args[args.index("--name") + 1]
                if name.endswith("-seed") and not intercepted:
                    self.assertIsNone(seed)
                    seed = name
                    label_value = args[args.index("--label") + 1]
                    created = original(
                        "create", "--pull", "never", "--name", name,
                        "--label", label_value, "--network", "none",
                        "--read-only", "--cap-drop", "ALL",
                        "--entrypoint", "/bin/sh", IMAGE, "-c", "true",
                    )
                    self.assertEqual(created.returncode, 0, created.stderr)
                    created_seed_id = created.stdout.strip()
                    self.assertRegex(created_seed_id, r"^[0-9a-f]{64}$")
                    intercepted = True
                    raise subprocess.TimeoutExpired(
                        cmd=["/usr/bin/docker", *args], timeout=90,
                    )
            return original(*args, **kwargs)

        try:
            trial = type(self)(
                "test_hostile_uid_with_all_rw_mounts_cannot_invoke_raw_sealer",
            )
            result = unittest.TestResult()
            with patch(__name__ + "._docker", side_effect=create_then_lose_ack):
                trial.run(result)
            self.assertTrue(intercepted)
            self.assertEqual(len(volumes), 5)
            self.assertFalse(result.errors, result.errors)
            self.assertEqual(len(result.failures), 1, result.failures)
            self.assertIn("unresolved Docker create/run", result.failures[0][1])
            for volume in volumes:
                retained = original(
                    "volume", "inspect", "--format",
                    '{{index .Labels "mark.a6.rawseal.run"}}',
                    volume, allow_failure=True,
                )
                self.assertEqual(retained.returncode, 0,
                                 "a decoy container cannot authorize mount rm")
                self.assertEqual(retained.stdout.strip(), volume.split("-")[2])
        finally:
            # RED mutation must not leave any real synthetic Docker resource.
            if seed:
                self.assertIsNotNone(created_seed_id, "no completed create identity")
                _remove_owned_synthetic_container(
                    seed, seed.split("-")[2], created_seed_id, original,
                )
            for volume in reversed(volumes):
                probe = original(
                    "volume", "inspect", "--format",
                    '{{index .Labels "mark.a6.rawseal.run"}}',
                    volume, allow_failure=True,
                )
                if probe.returncode == 0:
                    self.assertEqual(probe.stdout.strip(), volume.split("-")[2])
                    original("volume", "rm", volume)
                else:
                    self.assertTrue(
                        _docker_reports_missing(probe, "volume", volume),
                        "unexpected volume identity; refuse foreign deletion",
                    )
                final = original("volume", "inspect", volume, allow_failure=True)
                self.assertTrue(_docker_reports_missing(final, "volume", volume))
        print("P2_GREEN_DECOY_CONTAINER_DID_NOT_SETTLE_DAEMON_RUN", flush=True)

    def test_late_docker_volume_create_after_exact_404_stays_no_go(self) -> None:
        # Real Docker creates the volume only AFTER cleanup's first 404.
        # This was the independently reproduced P2 at the old PR HEAD.
        original = _docker
        release = threading.Event()
        state: dict[str, object] = {
            "name": None, "worker": None, "result": None, "first_404": False,
        }

        def delayed_create(args: tuple[str, ...]) -> None:
            if not release.wait(30):
                state["result"] = "daemon simulation did not receive release"
                return
            time.sleep(0.08)  # definitely after the first negative inspection
            try:
                state["result"] = original(*args, timeout_seconds=25)
            except Exception as exc:
                state["result"] = exc

        def late_daemon(*args: str, **kwargs: object
                        ) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("volume", "create") and state["name"] is None:
                name = args[-1]
                self.assertRegex(name, r"^mark-rawseal-[0-9a-f]{12}-source$")
                state["name"] = name
                worker = threading.Thread(target=delayed_create, args=(args,),
                                          daemon=True)
                state["worker"] = worker
                worker.start()
                raise subprocess.TimeoutExpired(
                    cmd=["/usr/bin/docker", *args], timeout=90,
                )
            if (state["name"] is not None and
                    args[:2] == ("volume", "inspect") and
                    args[-1] == state["name"] and "--format" in args and
                    not state["first_404"]):
                prior = original(*args, **kwargs)
                self.assertTrue(
                    _docker_reports_missing(prior, "volume", str(state["name"])),
                )
                state["first_404"] = True
                release.set()
                return prior
            return original(*args, **kwargs)

        leaked = False
        try:
            trial = type(self)(
                "test_hostile_uid_with_all_rw_mounts_cannot_invoke_raw_sealer",
            )
            result = unittest.TestResult()
            with patch(__name__ + "._docker", side_effect=late_daemon):
                trial.run(result)
            self.assertTrue(state["first_404"])
            self.assertFalse(result.errors, result.errors)
            self.assertEqual(len(result.failures), 1, result.failures)
            self.assertIn("unresolved Docker create/run", result.failures[0][1])
            release.set()
            worker = state["worker"]
            self.assertIsNotNone(worker)
            worker.join(timeout=35)
            self.assertFalse(worker.is_alive(), "late Docker worker is stuck")
            self.assertIsInstance(state["result"], subprocess.CompletedProcess)
            self.assertEqual(state["result"].returncode, 0)
            name = str(state["name"])
            final = original("volume", "inspect", name, allow_failure=True)
            leaked = not _docker_reports_missing(final, "volume", name)
            self.assertFalse(leaked, "late volume not removed by bounded recheck")
        finally:
            # Emergency cleanup is only permitted for our exact label.
            release.set()
            worker = state["worker"]
            if isinstance(worker, threading.Thread):
                worker.join(timeout=35)
            if state["name"] is not None:
                name = str(state["name"])
                owned = original(
                    "volume", "inspect", "--format",
                    '{{index .Labels "mark.a6.rawseal.run"}}',
                    name, allow_failure=True,
                )
                if owned.returncode == 0:
                    self.assertEqual(owned.stdout.strip(), name.split("-")[2])
                    original("volume", "rm", name)
                    leaked = True
                else:
                    self.assertTrue(
                        _docker_reports_missing(owned, "volume", name),
                        "cannot establish late test-volume identity",
                    )
                absent = original("volume", "inspect", name, allow_failure=True)
                self.assertTrue(_docker_reports_missing(absent, "volume", name))
        self.assertFalse(leaked, "emergency cleanup had to remove late volume")
        print("P2_GREEN_LATE_CREATE_NO_FALSE_CLEAN_AND_VOLUME_ABSENT", flush=True)

    def test_failed_create_without_volume_still_requires_completion_proof(self) -> None:
        original = _docker
        state: dict[str, str] = {}

        def rejected_create(*args: str, **kwargs: object
                            ) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("volume", "create"):
                state["name"] = args[-1]
                raise OSError("injected ambiguous Docker create rejection")
            return original(*args, **kwargs)

        trial = type(self)(
            "test_hostile_uid_with_all_rw_mounts_cannot_invoke_raw_sealer",
        )
        result = unittest.TestResult()
        with patch(__name__ + "._docker", side_effect=rejected_create):
            trial.run(result)
        self.assertFalse(result.errors, result.errors)
        self.assertEqual(len(result.failures), 1, result.failures)
        self.assertIn("unresolved Docker create/run", result.failures[0][1])
        self.assertIn("name", state)
        absent = original("volume", "inspect", state["name"],
                          allow_failure=True)
        self.assertTrue(_docker_reports_missing(absent, "volume", state["name"]))

    def test_foreign_label_is_never_deleted_after_uncertain_create(self) -> None:
        original = _docker
        state: dict[str, object] = {"name": None, "removed": False}

        def forged_foreign(*args: str, **kwargs: object
                           ) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("volume", "create"):
                state["name"] = args[-1]
                raise subprocess.TimeoutExpired(
                    cmd=["/usr/bin/docker", *args], timeout=90,
                )
            if (state["name"] and args[:2] == ("volume", "inspect") and
                    args[-1] == state["name"] and "--format" in args):
                return subprocess.CompletedProcess(
                    args=args, returncode=0,
                    stdout="another-owner\n", stderr="",
                )
            if args[:2] == ("volume", "rm") and args[-1] == state["name"]:
                state["removed"] = True
                raise AssertionError("test attempted to remove foreign volume")
            return original(*args, **kwargs)

        trial = type(self)(
            "test_hostile_uid_with_all_rw_mounts_cannot_invoke_raw_sealer",
        )
        result = unittest.TestResult()
        with patch(__name__ + "._docker", side_effect=forged_foreign):
            trial.run(result)
        self.assertEqual(len(result.failures), 1, result.failures)
        self.assertFalse(result.errors, result.errors)
        self.assertIn("ownership label mismatch", result.failures[0][1])
        self.assertIn("unresolved Docker create/run", result.failures[0][1])
        self.assertFalse(state["removed"])
        self.assertIsNotNone(state["name"])
        absent = original("volume", "inspect", str(state["name"]),
                          allow_failure=True)
        self.assertTrue(_docker_reports_missing(
            absent, "volume", str(state["name"]),
        ))

    def test_docker_missing_evidence_rejects_daemon_failures(self) -> None:
        name = "mark-rawseal-fake-test-resource"
        cases = (
            ("volume", f"Error response from daemon: get {name}: no such volume", True),
            ("container", f"Error response from daemon: No such container: {name}", True),
            ("volume", f"Error response from daemon: 500: no such volume {name}", False),
            ("container", "permission denied while reaching Docker daemon", False),
            ("container", f"Error response from daemon: No such container: {name}-other", False),
            ("volume", "unexpected daemon timeout", False),
        )
        for kind, message, absent in cases:
            with self.subTest(kind=kind, message=message):
                result = subprocess.CompletedProcess(
                    args=["docker", kind, "inspect", name],
                    returncode=1, stdout="[]", stderr=message,
                )
                self.assertEqual(_docker_reports_missing(result, kind, name), absent)
                success = subprocess.CompletedProcess(
                    args=result.args, returncode=0, stdout="", stderr=message,
                )
                self.assertFalse(_docker_reports_missing(success, kind, name))

    def test_auto_rm_container_after_inspect_does_not_fail_cleanup(self) -> None:
        # Synchronously run a disposable --rm seed; its Docker container is
        # already gone at the end. Inject an outdated positive inspect result
        # once during cleanup to emulate auto-removal before docker rm -f.
        original = _docker
        seed: str | None = None
        observed_positive = False
        attempted_remove = False

        def just_exited_after_inspection(*args: str, **kwargs: object
                                          ) -> subprocess.CompletedProcess[str]:
            nonlocal seed, observed_positive, attempted_remove
            if args and args[0] == "run" and "--name" in args:
                candidate = args[args.index("--name") + 1]
                result = original(*args, **kwargs)
                if candidate.endswith("-seed"):
                    seed = candidate
                return result
            if (seed is not None and args[:2] == ("container", "inspect")
                    and args[-1] == seed and "--format" in args
                    and not observed_positive):
                observed_positive = True
                suffix = seed.split("-")[2]
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=("a" * 64 + " " + suffix),
                    stderr="",
                )
            if (seed is not None and args[:2] == ("rm", "-f")
                    and args[-1] == "a" * 64):
                attempted_remove = True
                # Force the documented losing race even if this local Docker
                # version returns 0 for an already auto-removed --rm seed:
                # real post-removal inspect is absent, rm reports a synthetic
                # nonzero "No such container", and only final strict 404 wins.
                original(*args, **kwargs)
                absent = original("container", "inspect", seed,
                                  allow_failure=True)
                self.assertTrue(_docker_reports_missing(absent, "container", seed))
                return subprocess.CompletedProcess(
                    args=args, returncode=1, stdout="",
                    stderr="Error response from daemon: No such container: " + seed,
                )
            return original(*args, **kwargs)

        with patch(__name__ + "._docker", side_effect=just_exited_after_inspection):
            nested = type(self)(
                "test_hostile_uid_with_all_rw_mounts_cannot_invoke_raw_sealer",
            )
            result = unittest.TestResult()
            nested.run(result)
        self.assertTrue(observed_positive)
        self.assertTrue(attempted_remove)
        self.assertFalse(result.errors, result.errors)
        self.assertFalse(result.failures, result.failures)


    def test_create_timeout_rejects_inspection_500_not_false_404(self) -> None:
        # A daemon 500 after a successfully cleaned create is not evidence of
        # absence. This tests the full post-timeout verification contract, not
        # just the stand-alone parser.
        original = _docker
        created: str | None = None
        injected = False
        volume_removed = False
        cleanup_inspect_succeeded = False

        def wrong_daemon_reply(*args: str, **kwargs: object
                                ) -> subprocess.CompletedProcess[str]:
            nonlocal created, injected, volume_removed, cleanup_inspect_succeeded
            if args[:2] == ("volume", "create") and created is None:
                outcome = original(*args, **kwargs)
                created = args[-1]
                return outcome
            if (created is not None and args[:2] == ("volume", "rm")
                    and args[-1] == created):
                result = original(*args, **kwargs)
                volume_removed = True
                return result
            if (created is not None and args == ("volume", "inspect", created)
                    and volume_removed):
                if not cleanup_inspect_succeeded:
                    # The nested normal cleanup must prove a real 404 first.
                    real = original(*args, **kwargs)
                    self.assertTrue(
                        _docker_reports_missing(real, "volume", created),
                    )
                    cleanup_inspect_succeeded = True
                    return real
                if not injected:
                    # Only the later *test verdict* inspect receives 500.
                    injected = True
                    return subprocess.CompletedProcess(
                        args=args, returncode=1, stdout="[]",
                        stderr="Error response from daemon: 500 Internal Server Error",
                    )
            return original(*args, **kwargs)

        with patch(__name__ + "._docker", side_effect=wrong_daemon_reply):
            nested = type(self)(
                "test_created_volume_is_cleaned_when_docker_client_times_out",
            )
            result = unittest.TestResult()
            nested.run(result)
        self.assertTrue(volume_removed)
        self.assertTrue(cleanup_inspect_succeeded)
        self.assertTrue(injected)
        self.assertFalse(result.errors, result.errors)
        self.assertEqual(len(result.failures), 1, result.failures)
        self.assertIn("not proven absent", result.failures[0][1])


class SyntheticContainerCleanupContractTests(unittest.TestCase):
    """No Docker daemon required: positive identity, swap and error controls."""

    def test_emergency_cleanup_removes_only_pinned_id(self) -> None:
        name = "mark-rawseal-abc123abc123-seed"
        pinned = "a" * 64
        calls: list[tuple[str, ...]] = []

        def fake(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args[:2] == ("container", "inspect") and "--format" in args:
                return subprocess.CompletedProcess(args, 0, pinned + " abc123abc123\n", "")
            if args[:2] == ("rm", "-f"):
                self.assertEqual(args[-1], pinned)
                return subprocess.CompletedProcess(args, 0, name + "\n", "")
            if args == ("container", "inspect", name):
                return subprocess.CompletedProcess(
                    args, 1, "[]",
                    "Error response from daemon: No such container: " + name,
                )
            raise AssertionError("unexpected Docker operation " + repr(args))

        _remove_owned_synthetic_container(name, "abc123abc123", "a" * 64, fake)
        self.assertEqual([a for a in calls if a[:2] == ("rm", "-f")],
                         [("rm", "-f", pinned)])

    def test_emergency_cleanup_same_name_foreign_swap_fails_closed(self) -> None:
        name = "mark-rawseal-abc123abc123-seed"
        pinned = "a" * 64
        foreign = "b" * 64
        removed: list[str] = []

        def fake(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("container", "inspect") and "--format" in args:
                return subprocess.CompletedProcess(args, 0, pinned + " abc123abc123\n", "")
            if args[:2] == ("rm", "-f"):
                removed.append(args[-1])
                if args[-1] == name:
                    raise AssertionError("FOREIGN_CONTAINER_DELETE_BY_NAME")
                self.assertEqual(args[-1], pinned)
                # A second administrator already replaced the container:
                # the pinned ID no longer exists and rm reports nonzero.
                return subprocess.CompletedProcess(
                    args, 1, "", "Error: No such container: " + pinned,
                )
            if args == ("container", "inspect", name):
                return subprocess.CompletedProcess(args, 0, foreign, "")
            raise AssertionError("unexpected Docker operation " + repr(args))

        with self.assertRaisesRegex(AssertionError,
                                    "removal not proven absent"):
            _remove_owned_synthetic_container(name, "abc123abc123", "a" * 64, fake)
        self.assertEqual(removed, [pinned])

    def test_emergency_cleanup_rejects_preinspection_decoy_with_matching_label(self) -> None:
        name = "mark-rawseal-abc123abc123-seed"
        original_id = "a" * 64  # original, separately acknowledged create
        foreign_id = "b" * 64   # decoy before inspection
        remove_calls: list[str] = []

        def fake(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("container", "inspect") and "--format" in args:
                return subprocess.CompletedProcess(
                    args, 0, foreign_id + " abc123abc123\n", "",
                )
            if args[:2] == ("rm", "-f"):
                remove_calls.append(args[-1])
                raise AssertionError("foreign decoy removal attempted")
            raise AssertionError("unexpected Docker operation " + repr(args))

        with self.assertRaisesRegex(
            AssertionError, "refuse to remove foreign synthetic container",
        ):
            _remove_owned_synthetic_container(
                name, "abc123abc123", original_id, fake,
            )
        self.assertFalse(remove_calls)

    def test_emergency_cleanup_refuses_mismatch_or_daemon_500(self) -> None:
        name = "mark-rawseal-abc123abc123-seed"
        for result, message in (
            (subprocess.CompletedProcess((), 0, "b" * 64 + " foreign\n", ""),
             "refuse to remove foreign"),
            (subprocess.CompletedProcess((), 1, "[]", "500 Internal Server Error"),
             "identity not proven"),
        ):
            removed: list[tuple[str, ...]] = []

            def fake(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
                if args[:2] == ("rm", "-f"):
                    removed.append(args)
                    raise AssertionError("must not delete")
                self.assertEqual(args[:2], ("container", "inspect"))
                return result

            with self.assertRaisesRegex(AssertionError, message):
                _remove_owned_synthetic_container(name, "abc123abc123", "a" * 64, fake)
            self.assertFalse(removed)


class VolumeNameReuseSafetyTests(unittest.TestCase):
    """Guard against label-inspect then name-rm on a shared Docker daemon."""

    def test_name_based_volume_deletion_rejected_before_docker(self) -> None:
        for args in (
            ("volume", "rm", "mark-rawseal-abc123abc123-source"),
            ("volume", "prune", "--force"),
        ):
            with self.subTest(args=args):
                with patch(__name__ + ".subprocess.run") as run:
                    with self.assertRaisesRegex(
                        AssertionError, "name-based Docker volume deletion",
                    ):
                        _docker(*args, allow_failure=True)
                    run.assert_not_called()

    def test_all_resource_mutations_rejected_before_subprocess(self) -> None:
        # The class-wide guard is not the actual Docker command boundary.
        for args in (
            ("run", "--rm", "untrusted"),
            ("volume", "create", "mark-rawseal-test"),
            ("volume", "rm", "mark-rawseal-test"),
            ("volume", "prune", "--force"),
            ("rm", "-f", "a" * 64),
            ("container", "create", "untrusted"),
            ("network", "create", "mark-rawseal-test"),
        ):
            with self.subTest(args=args):
                with patch(__name__ + ".subprocess.run") as run:
                    with self.assertRaisesRegex(
                        AssertionError, "Docker-admin authority",
                    ):
                        _docker(*args, allow_failure=True)
                    run.assert_not_called()

    def test_direct_testcase_run_cannot_bypass_class_level_guard(self) -> None:
        # A direct TestCase.run() does NOT call setUpClass(). Both optional
        # and required opt-in modes must still stop before any Docker access.
        test_names = (
            "test_docker_missing_evidence_rejects_daemon_failures",
            "test_created_volume_is_cleaned_when_docker_client_times_out",
        )
        with patch.object(RawSealerHostileUidDockerTests, "__unittest_skip__", False):
            for required in ("0", "1"):
                for name in test_names:
                    with self.subTest(required=required, method=name):
                        with patch.dict(
                            os.environ, {"MARK_REQUIRE_RAW_SEALER_UID": required},
                        ):
                            result = unittest.TestResult()
                            with patch(__name__ + ".subprocess.run") as run:
                                RawSealerHostileUidDockerTests(name).run(result)
                            run.assert_not_called()
                            self.assertEqual(result.testsRun, 1)
                            if required == "0":
                                self.assertEqual(len(result.skipped), 1)
                                self.assertFalse(result.failures or result.errors)
                                self.assertIn(
                                    "exclusive Docker-admin authority",
                                    result.skipped[0][1],
                                )
                            else:
                                self.assertEqual(len(result.failures), 1)
                                self.assertFalse(result.skipped or result.errors)
                                self.assertIn(
                                    "exclusive Docker-admin authority",
                                    result.failures[0][1],
                                )

    def test_required_real_uid_test_fails_before_resource_creation(self) -> None:
        with patch(__name__ + ".IMAGE_OK", True):
            with patch.dict(os.environ, {"MARK_REQUIRE_RAW_SEALER_UID": "1"}):
                with self.assertRaisesRegex(
                    AssertionError, "exclusive Docker-admin authority",
                ):
                    RawSealerHostileUidDockerTests.setUpClass()

    def test_optional_real_uid_test_skips_without_host_authority(self) -> None:
        with patch(__name__ + ".IMAGE_OK", True):
            with patch.dict(os.environ, {"MARK_REQUIRE_RAW_SEALER_UID": "0"}):
                with self.assertRaisesRegex(
                    unittest.SkipTest, "exclusive Docker-admin authority",
                ):
                    RawSealerHostileUidDockerTests.setUpClass()


if __name__ == "__main__":
    unittest.main()
