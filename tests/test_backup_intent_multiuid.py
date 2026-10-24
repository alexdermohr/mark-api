"""Opt-in real Linux/Docker DAC test for the offline backup-intent operator.

Only synthetic SQLite/Witness fixtures are permitted here until the host
enforces exclusive Docker-daemon administration (or atomic immutable-identity
volume reclamation). The real-Docker test is intentionally fail-closed BEFORE
creating a container or named volume. Unique names do not make name-based
Docker deletion safe against competing administrators. This is not live
host-quiescence, historic mmap revocation, or platform-write acceptance.
The caller must select an immutable local image; no pulling is permitted.
"""
from __future__ import annotations

import io
import json
import os
import re
import tarfile
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("MARK_INTENT_MULTIUID_TEST_IMAGE", "")
IMAGE_OK = bool(re.fullmatch(r"sha256:[0-9a-f]{64}", IMAGE))
PYTHON = "/opt/mark-api/venv/bin/python"
if IMAGE and not IMAGE_OK:
    raise ValueError("MARK_INTENT_MULTIUID_TEST_IMAGE must be an immutable sha256 image ID")
if os.environ.get("MARK_REQUIRE_INTENT_MULTIUID") == "1" and not IMAGE_OK:
    raise ValueError("required real-UID test has no immutable Docker image")


def _require_exclusive_docker_admin_authority() -> None:
    """Never run the real-UID fixture on a shared Docker daemon.

    The installed test host has no independent exclusive-daemon attestation.
    A caller-controlled flag or a random resource name cannot supply one.
    Keep this guard until a host-enforced exclusive context is implemented
    and verified; a required real test must fail rather than silently skip.
    """
    message = (
        "exclusive Docker-admin authority not established: name-based "
        "volume/container cleanup can remove a foreign replacement"
    )
    if os.environ.get("MARK_REQUIRE_INTENT_MULTIUID") == "1":
        raise AssertionError(message)
    raise unittest.SkipTest(message)


def _docker(*args: str, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    # Enforce a positive, passive-only Docker command boundary. Docker CLI
    # aliases (volume remove) and other mutating families (system prune,
    # container run/rm, image rm, network remove) must never reach subprocess.
    # A TestCase can be invoked without setUpClass(), so this is essential.
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
            "Docker mutation forbidden before subprocess"
        )
    result = subprocess.run(
        ["/usr/bin/docker", *args], check=False, capture_output=True,
        text=True, timeout=60,
    )
    if result.returncode and not allow_failure:
        raise AssertionError("isolated Docker operation failed: " + result.stderr[-900:])
    return result


_SEAL = """
import hashlib,json,os,stat
from pathlib import Path
import mark_api
from mark_api.backup_intent_authority import BackupIntentAuthority
from mark_api.fence_witness import FenceLedger
from mark_api.storage import SnapshotStore
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
assert prepared['generation']==generation
result=op.seal_prepared(generation,Path('/output'),Path('/anchor'))
assert result['external_operator_intent_acknowledged'] is True
assert not (Path('/anchor')/'.inflight').exists()
# Parent permissions are intentionally set by the trusted test harness.
# Check every actual source and product-created artifact independently.
for name in ('source','output','anchor','intent'):
    directory=Path('/'+name)
    info=directory.lstat()
    assert info.st_uid==50043 and stat.S_ISDIR(info.st_mode)
    assert stat.S_IMODE(info.st_mode)==0o700,(name,info)
    for artifact in directory.rglob('*'):
        info=artifact.lstat()
        assert info.st_uid==50043,(artifact,info)
        if stat.S_ISDIR(info.st_mode):
            assert stat.S_IMODE(info.st_mode)==0o700,(artifact,info)
        else:
            assert stat.S_ISREG(info.st_mode),(artifact,info)
            assert stat.S_IMODE(info.st_mode)==0o600,(artifact,info)
pair=Path('/output')/generation
files=[
    Path('/intent/.intent.lock'),
    Path('/intent/.intent.bootstrap.json'),
    *(Path('/intent')/(generation+'.'+phase+'.json')
      for phase in ('prepared','committing','acknowledged')),
    Path('/anchor/latest.json'),
    Path('/anchor')/(generation+'.json'),
    *(pair/name for name in
      ('mark.sqlite','witness.sqlite','manifest.json','witness.sqlite.lock')),
]
for path in files:
    info=path.stat()
    assert info.st_uid==50043 and stat.S_IMODE(info.st_mode)==0o600,(path,info)
sealed_files={
    'head':Path('/anchor/latest.json'),
    'sqlite':pair/'mark.sqlite',
    'witness':pair/'witness.sqlite',
    'manifest':pair/'manifest.json',
}
sealed_hashes={
    label:hashlib.sha256(path.read_bytes()).hexdigest()
    for label,path in sealed_files.items()
}
print('MARK_UID_SEAL_BASELINE='+json.dumps(sealed_hashes,sort_keys=True),flush=True)
print('REAL_UID50043_COMPLETE_ARTIFACT_DAC_AND_INTENT_SEALED',flush=True)
"""

_ATTACK = """
import errno,os
from pathlib import Path
assert os.geteuid()==50044
# Positive control: RW Docker volume access actually works for this UID.
control=Path('/control/probe')
control.write_bytes(b'UID50044-RW-ALLOWED')
assert control.read_bytes()==b'UID50044-RW-ALLOWED'
# The trusted harness has made its protected roots traversable (0711).
# Before denial, this UID can stat concrete product files through them.
for name in ('source','output','anchor','intent'):
    assert os.access('/'+name,os.X_OK),name
for item in ('/source/mark.sqlite','/anchor/latest.json','/intent/.intent.lock'):
    assert Path(item).stat().st_size>=0,item
assert Path('/output/'+'1'*32).is_dir()
# All protected volumes are ALSO intentionally mounted RW to this UID.
protected=(
    '/intent/.intent.lock',
    '/intent/'+'1'*32+'.acknowledged.json',
    '/source/mark.sqlite',
    '/source/witness.sqlite',
    '/output/'+'1'*32+'/mark.sqlite',
    '/anchor/latest.json',
)
for item in protected:
    for flags in (os.O_RDONLY,os.O_WRONLY):
        try:
            fd=os.open(item,flags|os.O_CLOEXEC|os.O_NOFOLLOW)
        except OSError as exc:
            assert exc.errno in (errno.EACCES,errno.EPERM),(item,flags,exc)
        else:
            os.close(fd)
            raise AssertionError('hostile UID accessed protected path: '+item)
    try:Path(item).read_bytes()
    except PermissionError:pass
    else:raise AssertionError('hostile UID read protected image: '+item)
print('REAL_UID50044_PRODUCT_FILE_OPEN_DAC_DENIED',flush=True)

# Namespace mutations are denied by harness-provisioned 0711 root directories.
# The nested generation directory is product-created with mode 0700.
# These checks do NOT demonstrate product control of root mount policy.
for item in protected:
    try:os.unlink(item)
    except OSError as exc:
        assert exc.errno in (errno.EACCES,errno.EPERM),(item,'unlink',exc)
    else:raise AssertionError('hostile UID unlinked protected artifact: '+item)
for path in ('/intent/forged.json','/anchor/.inflight','/output/attacker'):
    try:
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|
                   os.O_CLOEXEC|os.O_NOFOLLOW,0o600)
    except OSError as exc:
        assert exc.errno in (errno.EACCES,errno.EPERM),(path,exc)
    else:
        os.close(fd)
        raise AssertionError('hostile UID created protected artifact: '+path)
try:os.rename('/anchor/latest.json','/anchor/rolled-back.json')
except OSError as exc:
    assert exc.errno in (errno.EACCES,errno.EPERM),exc
else:raise AssertionError('hostile UID replaced the anchored HEAD')
print('REAL_UID50044_HARNESS_ROOT_NAMESPACE_DAC_DENIED',flush=True)
print('REAL_UID50044_DAC_DENIED_WITH_RW_VOLUME_POSITIVE_CONTROL',flush=True)
"""


_RELAX_ROOTS = """
import os,stat
from pathlib import Path
assert os.geteuid()==50043
for name in ('source','output','anchor','intent'):
    root=Path('/'+name)
    assert stat.S_IMODE(root.stat().st_mode)==0o700
    root.chmod(0o711)
print('TRUSTED_ROOTS_TRAVERSABLE_ARTIFACT_DAC_UNCHANGED',flush=True)
"""

_RESTORE_ROOTS = """
import os,stat
from pathlib import Path
assert os.geteuid()==50043
for name in ('source','output','anchor','intent'):
    root=Path('/'+name)
    assert stat.S_IMODE(root.stat().st_mode)==0o711
    root.chmod(0o700)
print('TRUSTED_ROOTS_RESTRICTED_AGAIN',flush=True)
"""

_RECHECK = """
import os,hashlib,json
from pathlib import Path
import mark_api
from mark_api.backup_intent_authority import BackupIntentAuthority,BackupIntentError
from mark_api.paired_restore_preflight import verify_offline_pair
assert os.geteuid()==50043
assert Path(mark_api.__file__).as_posix().startswith('/src/mark_api/')
generation='1'*32
root=Path('/output')/generation
anchor=Path('/anchor')
expected={
    'head':os.environ['MARK_UID_SEAL_HEAD_SHA'],
    'sqlite':os.environ['MARK_UID_SEAL_SQLITE_SHA'],
    'witness':os.environ['MARK_UID_SEAL_WITNESS_SHA'],
    'manifest':os.environ['MARK_UID_SEAL_MANIFEST_SHA'],
}
for label,digest in expected.items():
    assert len(digest)==64 and all(c in '0123456789abcdef' for c in digest),(label,digest)
sealed_files={
    'head':anchor/'latest.json',
    'sqlite':root/'mark.sqlite',
    'witness':root/'witness.sqlite',
    'manifest':root/'manifest.json',
}
for label,path in sealed_files.items():
    assert hashlib.sha256(path.read_bytes()).hexdigest()==expected[label],(label,path)
head_bytes=(anchor/'latest.json').read_bytes()
head_sha=hashlib.sha256(head_bytes).hexdigest()
head=json.loads(head_bytes)
assert head['generation']==generation
pairs=[root/name for name in ('mark.sqlite','witness.sqlite','manifest.json')]
original={str(path):hashlib.sha256(path.read_bytes()).hexdigest()
          for path in pairs}
checked=verify_offline_pair(
    pairs[0],pairs[1],pairs[2],
    expected_manifest_sha256=head['manifest_sha256'],
)
assert checked['generation']==generation
op=BackupIntentAuthority(Path('/intent'))
checks=(
    (lambda:op.prepare(generation,Path('/source/mark.sqlite'),
        Path('/source/witness.sqlite'),previous_head_sha256=head_sha),
     'operator generation already used or pending'),
    (lambda:op.seal_prepared(generation,Path('/output'),anchor),
     'generation not singly prepared; never retry commit'),
    (lambda:op.acknowledge(generation,Path('/output'),anchor),
     'no unconsumed external committing intent'),
)
for action,message in checks:
    try:action()
    except BackupIntentError as exc:
        # Both variants reject replay of an already consumed generation.
        # An integrated backup authority can fail earlier in preflight.
        assert (
            message in str(exc)
            or (
                message == 'generation not singly prepared; never retry commit'
                and str(exc) == 'operator intent not singly prepared'
            )
        ),(message,str(exc))
    else:raise AssertionError('original acknowledged intent was replayed')
assert (anchor/'latest.json').read_bytes()==head_bytes
for path in pairs:
    assert hashlib.sha256(path.read_bytes()).hexdigest()==original[str(path)]
for label,path in sealed_files.items():
    assert hashlib.sha256(path.read_bytes()).hexdigest()==expected[label],(label,path)
assert not (anchor/'.inflight').exists()
assert (Path('/intent')/(generation+'.acknowledged.json')).is_file()
print('REAL_UID50043_EXACT_CHAIN_AND_SINGLE_USE_REPLAY_DENIAL',flush=True)
"""


@unittest.skipUnless(IMAGE_OK, "Set immutable MARK_INTENT_MULTIUID_TEST_IMAGE")
class BackupIntentMultiUidDockerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _require_exclusive_docker_admin_authority()

    def setUp(self) -> None:
        # A direct TestCase.run() does not execute setUpClass().
        super().setUp()
        _require_exclusive_docker_admin_authority()

    def test_hostile_executor_cannot_access_independent_offline_intent(self) -> None:
        # Direct TestCase.method() calls do not execute setUpClass().
        _require_exclusive_docker_admin_authority()
        suffix = uuid4().hex[:12]
        names = {name: "mark-intent-" + suffix + "-" + name
                 for name in ("source", "output", "anchor", "intent", "control")}
        created: list[str] = []
        launched: list[str] = []
        with tempfile.TemporaryDirectory(prefix="mark-intent-realuid-") as temp:
            # A green Docker run must use the exact committed product modules,
            # not silently import a locally modified or untracked source tree.
            head = subprocess.run(
                ["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"],
                check=True, capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            self.assertRegex(head, r"^[0-9a-f]{40}$")
            source_status = subprocess.run(
                ["git", "-C", str(ROOT), "status", "--porcelain",
                 "--untracked-files=all", "--", "src/mark_api"],
                check=True, capture_output=True, text=True, timeout=10,
            )
            self.assertFalse(source_status.stdout.strip(),
                             "Docker source differs from committed HEAD")
            package = Path(temp) / "src" / "mark_api"
            # Extract exactly HEAD's archived tracked modules: an ignored
            # local .so, legacy .pyc or other build artifact cannot enter /src.
            product_archive = subprocess.run(
                ["git", "-C", str(ROOT), "archive", "--format=tar",
                 head, "src/mark_api"],
                check=True, capture_output=True, timeout=30,
            ).stdout
            with tarfile.open(fileobj=io.BytesIO(product_archive), mode="r:") as archive:
                archive.extractall(temp, filter="data")
            self.assertTrue((package / "__init__.py").is_file())
            for item in (Path(temp), package.parent, *package.parent.rglob("*")):
                if item.is_dir():
                    item.chmod(0o755)
                elif item.is_file():
                    item.chmod(0o644)
            volume_mounts: list[str] = []
            for name, volume in names.items():
                volume_mounts.extend([
                    "--mount", f"type=volume,source={volume},target=/{name},volume-nocopy",
                ])
            source_mount = [
                "--mount",
                f"type=bind,source={package.parent},target=/src,readonly",
            ]

            def run_python(role: str, uid: int, script: str,
                           *, source_visible: bool,
                           evidence: dict[str, str] | None = None
                           ) -> subprocess.CompletedProcess[str]:
                name = f"mark-intent-{suffix}-{role}"
                launched.append(name)
                opts = [*source_mount, "-e", "PYTHONPATH=/src"] if source_visible else []
                if evidence:
                    for key, value in evidence.items():
                        if not re.fullmatch(r"MARK_UID_SEAL_[A-Z]+_SHA", key):
                            raise AssertionError("untrusted Docker evidence name")
                        if not re.fullmatch(r"[0-9a-f]{64}", value):
                            raise AssertionError("untrusted Docker evidence digest")
                        opts.extend(("-e", key + "=" + value))
                return _docker(
                    "run", "--rm", "--pull", "never", "--name", name,
                    "--network", "none", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true",
                    "--user", f"{uid}:{uid}", "--workdir", "/",
                    "-e", "PYTHONDONTWRITEBYTECODE=1",
                    *opts, "--entrypoint", PYTHON, *volume_mounts,
                    IMAGE, "-B", "-c", script,
                )

            try:
                for volume in names.values():
                    # Register before a possibly timed-out Docker create.
                    created.append(volume)
                    _docker("volume", "create", volume)
                seed_name = f"mark-intent-{suffix}-seed"
                launched.append(seed_name)
                _docker(
                    "run", "--rm", "--pull", "never", "--name", seed_name,
                    "--network", "none", "--read-only", "--user", "0:0",
                    "--cap-drop", "ALL", "--cap-add", "CHOWN",
                    *volume_mounts, "--entrypoint", "/bin/sh", IMAGE, "-ec",
                    "for d in /source /output /anchor /intent /control; do "
                    "out=$(ls -A \"$d\") || exit 1; "
                    "[ -z \"$out\" ] || exit 1; done; "
                    "chmod 0700 /source /output /anchor /intent /control; "
                    "chown 50043:50043 /source /output /anchor /intent; "
                    "chown 50044:50044 /control",
                )
                first = run_python("seal", 50043, _SEAL, source_visible=True)
                self.assertIn("REAL_UID50043_COMPLETE_ARTIFACT_DAC_AND_INTENT_SEALED",
                              first.stdout)
                captured = re.search(
                    r"^MARK_UID_SEAL_BASELINE=(\{[^\n]+\})$",
                    first.stdout, flags=re.MULTILINE,
                )
                self.assertIsNotNone(captured, first.stdout)
                seal_hashes = json.loads(captured.group(1))
                self.assertEqual(set(seal_hashes),
                                 {"head", "sqlite", "witness", "manifest"})
                self.assertTrue(all(
                    isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                    for value in seal_hashes.values()
                ))
                # 0711 harness roots grant traversal. 0600 file-open denials
                # prove artifact DAC; namespace denials prove harness root DAC.

                relaxed = run_python("relax", 50043, _RELAX_ROOTS, source_visible=False)
                self.assertIn("TRUSTED_ROOTS_TRAVERSABLE_ARTIFACT_DAC_UNCHANGED",
                              relaxed.stdout)
                attack = run_python("attack", 50044, _ATTACK, source_visible=False)
                self.assertIn("REAL_UID50044_PRODUCT_FILE_OPEN_DAC_DENIED",
                              attack.stdout)
                self.assertIn("REAL_UID50044_HARNESS_ROOT_NAMESPACE_DAC_DENIED",
                              attack.stdout)
                self.assertIn(
                    "REAL_UID50044_DAC_DENIED_WITH_RW_VOLUME_POSITIVE_CONTROL",
                    attack.stdout,
                )
                restored = run_python("restore", 50043, _RESTORE_ROOTS,
                                      source_visible=False)
                self.assertIn("TRUSTED_ROOTS_RESTRICTED_AGAIN", restored.stdout)
                verify = run_python(
                    "verify", 50043, _RECHECK, source_visible=True,
                    evidence={
                        "MARK_UID_SEAL_HEAD_SHA": seal_hashes["head"],
                        "MARK_UID_SEAL_SQLITE_SHA": seal_hashes["sqlite"],
                        "MARK_UID_SEAL_WITNESS_SHA": seal_hashes["witness"],
                        "MARK_UID_SEAL_MANIFEST_SHA": seal_hashes["manifest"],
                    },
                )
                self.assertIn(
                    "REAL_UID50043_EXACT_CHAIN_AND_SINGLE_USE_REPLAY_DENIAL",
                    verify.stdout,
                )
                self.assertEqual(
                    subprocess.run(
                        ["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"],
                        check=True, capture_output=True, text=True, timeout=10,
                    ).stdout.strip(), head,
                    "committed product HEAD changed during the Docker test",
                )
                print("MARK_UID_TEST_EXACT_COMMITTED_HEAD=" + head, flush=True)
            finally:
                cleanup_errors: list[str] = []
                # Even a Docker CLI timeout cannot orphan an anonymous
                # container: every launch has an independently known name.
                for name in reversed(launched):
                    try:
                        result = _docker("rm", "-f", name, allow_failure=True)
                        if result.returncode and "No such container" not in (
                            result.stderr + result.stdout
                        ):
                            cleanup_errors.append("container " + name + ": " + result.stderr[-200:])
                    except Exception as exc:
                        cleanup_errors.append("container " + name + ": " + repr(exc))
                for volume in reversed(created):
                    try:
                        result = _docker("volume", "rm", volume, allow_failure=True)
                        if result.returncode:
                            cleanup_errors.append("volume " + volume + ": " + result.stderr[-200:])
                    except Exception as exc:
                        cleanup_errors.append("volume " + volume + ": " + repr(exc))
                if cleanup_errors:
                    message = "synthetic Docker cleanup incomplete: " + "; ".join(cleanup_errors)
                    if sys.exc_info()[0] is None:
                        self.fail(message)
                    else:
                        print(message, file=sys.stderr)


class SharedDockerAuthorityContractTests(unittest.TestCase):
    """Unsafe cleanup cannot rely on mutable Docker resource names."""

    def test_shared_daemon_mutations_are_blocked_before_client_dispatch(self) -> None:
        # A blacklist of volume rm/run would miss Docker CLI aliases.
        for args in (
            ("volume", "create", "mark-intent-abc123abc123-source"),
            ("volume", "rm", "mark-intent-abc123abc123-source"),
            ("volume", "remove", "mark-intent-abc123abc123-source"),
            ("volume", "prune", "--force"),
            ("system", "prune", "--volumes"),
            ("container", "run", "--rm", "synthetic"),
            ("container", "rm", "-f", "synthetic"),
            ("container", "create", "synthetic"),
            ("run", "--rm", "--name", "mark-intent-abc123abc123-seed"),
            ("rm", "-f", "mark-intent-abc123abc123-seed"),
            ("image", "rm", "synthetic"),
            ("network", "remove", "synthetic"),
            ("network", "create", "synthetic"),
        ):
            with self.subTest(args=args):
                with patch(__name__ + ".subprocess.run") as launched:
                    with self.assertRaisesRegex(
                        AssertionError, "exclusive Docker-admin authority",
                    ):
                        _docker(*args, allow_failure=True)
                    launched.assert_not_called()

    def test_explicit_passive_commands_are_the_only_dispatched_forms(self) -> None:
        for args in (
            ("info", "--format", "{{json .SecurityOptions}}"),
            ("image", "inspect", "sha256:" + "a" * 64),
            ("container", "inspect", "synthetic"),
            ("volume", "inspect", "synthetic"),
        ):
            with self.subTest(args=args):
                fake = subprocess.CompletedProcess(
                    args=["/usr/bin/docker", *args],
                    returncode=0, stdout="MOCK_ONLY", stderr="",
                )
                with patch(__name__ + ".subprocess.run", return_value=fake) as dispatched:
                    self.assertIs(_docker(*args), fake)
                    dispatched.assert_called_once()

    def test_direct_testcase_run_still_enforces_authority(self) -> None:
        # unittest skips setUpClass for a directly instantiated TestCase.
        name = "test_hostile_executor_cannot_access_independent_offline_intent"
        with patch.object(BackupIntentMultiUidDockerTests, "__unittest_skip__", False):
            for required in ("0", "1"):
                with self.subTest(required=required):
                    trial = BackupIntentMultiUidDockerTests(name)
                    result = unittest.TestResult()
                    with patch.dict(os.environ, {"MARK_REQUIRE_INTENT_MULTIUID": required}):
                        with patch(__name__ + ".uuid4") as allocate:
                            with patch(__name__ + ".subprocess.run") as called:
                                trial.run(result)
                            called.assert_not_called()
                            allocate.assert_not_called()
                    self.assertEqual(result.testsRun, 1)
                    self.assertFalse(result.errors)
                    if required == "0":
                        self.assertEqual(len(result.skipped), 1)
                        self.assertFalse(result.failures)
                        self.assertIn("exclusive Docker-admin authority", result.skipped[0][1])
                    else:
                        self.assertEqual(len(result.failures), 1)
                        self.assertFalse(result.skipped)
                        self.assertIn("exclusive Docker-admin authority", result.failures[0][1])

    def test_required_real_uid_suite_blocks_before_name_allocation(self) -> None:
        trial = BackupIntentMultiUidDockerTests(
            "test_hostile_executor_cannot_access_independent_offline_intent"
        )
        with patch(__name__ + ".IMAGE_OK", True):
            with patch.dict(os.environ, {"MARK_REQUIRE_INTENT_MULTIUID": "1"}):
                with patch(
                    __name__ + ".uuid4",
                    side_effect=AssertionError("unsafe name allocation before authority"),
                ) as allocate:
                    with self.assertRaisesRegex(
                        AssertionError, "exclusive Docker-admin authority",
                    ):
                        trial.test_hostile_executor_cannot_access_independent_offline_intent()
                    allocate.assert_not_called()

    def test_optional_real_uid_suite_skips_before_name_allocation(self) -> None:
        trial = BackupIntentMultiUidDockerTests(
            "test_hostile_executor_cannot_access_independent_offline_intent"
        )
        with patch(__name__ + ".IMAGE_OK", True):
            with patch.dict(os.environ, {"MARK_REQUIRE_INTENT_MULTIUID": "0"}):
                with patch(
                    __name__ + ".uuid4",
                    side_effect=AssertionError("unsafe name allocation before authority"),
                ) as allocate:
                    with self.assertRaisesRegex(
                        unittest.SkipTest, "exclusive Docker-admin authority",
                    ):
                        trial.test_hostile_executor_cannot_access_independent_offline_intent()
                    allocate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
