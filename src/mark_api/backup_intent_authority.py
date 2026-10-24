"""Offline single-use recovery intents for a separately trusted operator.

The module is deliberately inaccessible from Mark's HTTP/browser/dashboard
paths. A host must enforce another UID, protected mounts, quiescent cold images,
and revoked writable descriptors/mappings. Same-UID files do not prove that.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from threading import local
from typing import Iterator

from .paired_backup_operator import (
    PairedBackupOperatorError, _check_generation_dir,
    _fsync_dir, _new_file, _private_root, _read_head,
    _validate_anchored_history, _reconcile_committed_inflight,
    seal_offline_pair,
)
from .paired_restore_preflight import (
    PairPreflightError, _canonical, _pinned_file, inspect_offline_pair,
    verify_offline_pair,
)


class BackupIntentError(RuntimeError):
    """An external operator intent cannot advance safely."""


_GEN = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ZERO_HEAD = "0" * 64
_MAX_RECORD = 4096
_BOOTSTRAP_NAME = ".intent.bootstrap.json"
# Only a thread already holding this *same inode* may nest the operation lock.
_OPERATION_HELD = local()


def _generation(value: str) -> str:
    if type(value) is not str or _GEN.fullmatch(value) is None:
        raise BackupIntentError("generation identity invalid")
    return value


def _sha(value: str, label: str) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise BackupIntentError(f"{label} SHA-256 invalid")
    return value


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record(root: Path, generation: str, phase: str) -> Path:
    return root / f"{_generation(generation)}.{phase}.json"


def _read(path: Path, keys: set[str]) -> tuple[dict[str, object], bytes]:
    with _pinned_file(path) as fd:
        size = os.fstat(fd).st_size
        if not 1 <= size <= _MAX_RECORD:
            raise BackupIntentError("operator intent length invalid")
        raw = os.pread(fd, size, 0)
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise BackupIntentError("operator intent JSON invalid") from exc
        if type(obj) is not dict or set(obj) != keys or _canonical(obj) != raw:
            raise BackupIntentError("operator intent not canonical")
        return obj, raw


def _expected_lock_identity(root: Path) -> tuple[int, int]:
    record, _ = _read(root / _BOOTSTRAP_NAME, {
        "schema_version", "lock_device", "lock_inode",
    })
    if (
        type(record["schema_version"]) is not int
        or record["schema_version"] != 1
        or type(record["lock_device"]) is not int
        or type(record["lock_inode"]) is not int
        or record["lock_device"] < 0 or record["lock_inode"] <= 0
    ):
        raise BackupIntentError("operator lock bootstrap record invalid")
    return record["lock_device"], record["lock_inode"]


def _bootstrap_lock_once(root: Path) -> None:
    """Only an absolutely empty operator root may initialize a new lock inode.

    Never silently recover deleted/replaced lock artifacts during normal use.
    Lock-inode binding is host-specific; moving a backup to a new filesystem
    requires controlled external recovery, not automatic reinitialization.
    """
    try:
        _private_root(root)
        if not any(root.iterdir()):
            lock = root / ".intent.lock"
            _new_file(lock, b"")
            _fsync_dir(root)
            info = lock.lstat()
            record = {
                "schema_version": 1,
                "lock_device": info.st_dev,
                "lock_inode": info.st_ino,
            }
            _new_file(root / _BOOTSTRAP_NAME, _canonical(record))
            _fsync_dir(root)
        expected = _expected_lock_identity(root)
        with _pinned_file(root / ".intent.lock") as fd:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != expected or info.st_size != 0:
                raise BackupIntentError("operator lock inode changed since bootstrap")
    except (OSError, PairPreflightError, PairedBackupOperatorError) as exc:
        raise BackupIntentError("operator lock bootstrap uncertain") from exc


@contextmanager
def _locked(root: Path) -> Iterator[None]:
    try:
        _private_root(root)
        expected = _expected_lock_identity(root)
        with _pinned_file(root / ".intent.lock") as fd:
            info = os.fstat(fd)
            if (
                (info.st_dev, info.st_ino) != expected
                or info.st_size != 0
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise BackupIntentError("operator lock inode or content invalid")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BackupIntentError("another independent operator is active") from exc
            try:
                # The anchored bootstrap and lock must remain the same while
                # a holder operates; later holders refuse a substituted inode.
                if _expected_lock_identity(root) != expected:
                    raise BackupIntentError("operator bootstrap changed during lock")
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    except (OSError, PairPreflightError, PairedBackupOperatorError) as exc:
        raise BackupIntentError("operator state or lock untrusted") from exc


@contextmanager
def _operation_lock(root: Path) -> Iterator[None]:
    """Serialize complete seals, manual abandons and external acknowledgements.

    The directory flock is separate from the short-lived intent record lock,
    which the sealer reacquires. It provides local cooperating-process safety,
    not revocation or protection against a compromised owner UID.
    """
    fd = -1
    try:
        try:
            _private_root(root)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            st = os.fstat(fd)
            original = (st.st_dev, st.st_ino)
            current = root.lstat()
            if not stat.S_ISDIR(st.st_mode) or original != (current.st_dev, current.st_ino):
                raise BackupIntentError("operator state root changed before operation")
            held = getattr(_OPERATION_HELD, "inodes", None)
            current_pid = os.getpid()
            # A forked child inherits thread-local memory but does not own
            # the parent's trusted operation. It must compete for the flock.
            if held is None or getattr(_OPERATION_HELD, "pid", None) != current_pid:
                held = set()
                _OPERATION_HELD.pid = current_pid
                _OPERATION_HELD.inodes = held
            # seal_prepared() calls its public acknowledge() hook while the
            # same trusted thread still owns the complete transaction.
            # Different threads/processes always compete for the real flock.
            nested = original in held
            if not nested:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise BackupIntentError(
                        "another trusted seal or abandon is in progress"
                    ) from exc
            current = root.lstat()
            if original != (current.st_dev, current.st_ino):
                raise BackupIntentError("operator state root changed while locking")
        except (OSError, PairPreflightError, PairedBackupOperatorError) as exc:
            raise BackupIntentError("operator operation lock could not be trusted") from exc
        if not nested:
            held.add(original)
        try:
            # Preserve original exception types after an uncertain seal.
            yield
        finally:
            if not nested:
                held.remove(original)
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if fd >= 0:
            os.close(fd)


def _state(root: Path) -> tuple[str, dict[str, dict[str, dict[str, object]]]]:
    """Read immutable transitions; reject replay, orphaned and forked history."""
    files: dict[str, dict[str, Path]] = {}
    entries = list(root.iterdir())
    if len(entries) > 100001:
        raise BackupIntentError("operator intent history too large")
    for path in entries:
        if path.name in {".intent.lock", _BOOTSTRAP_NAME}:
            continue
        match = re.fullmatch(
            r"([0-9a-f]{32})\.(prepared|committing|acknowledged|abandoned)\.json",
            path.name, re.ASCII,
        )
        if match is None:
            raise BackupIntentError("unrecognized operator intent artifact")
        files.setdefault(match[1], {})[match[2]] = path
    records: dict[str, dict[str, dict[str, object]]] = {}
    acknowledged: dict[str, dict[str, object]] = {}
    for gen, paths in files.items():
        if "prepared" not in paths:
            raise BackupIntentError("orphaned operator transition")
        prepared, pbytes = _read(paths["prepared"], {
            "schema_version", "generation", "previous_head_sha256",
            "sqlite_sha256", "witness_sha256", "sqlite_path", "witness_path",
        })
        if (
            type(prepared["schema_version"]) is not int
            or prepared["schema_version"] != 1
            or prepared["generation"] != gen
        ):
            raise BackupIntentError("prepared intent identity invalid")
        _sha(prepared["previous_head_sha256"], "previous HEAD")
        _sha(prepared["sqlite_sha256"], "SQLite")
        _sha(prepared["witness_sha256"], "Witness")
        for name in ("sqlite_path", "witness_path"):
            val = prepared[name]
            if (
                type(val) is not str or not val.startswith("/")
                or val != os.path.normpath(val)
            ):
                raise BackupIntentError("prepared image path invalid")
        if prepared["sqlite_path"] == prepared["witness_path"]:
            raise BackupIntentError("cold images identical")
        phases = {"prepared": prepared}
        if "committing" in paths:
            committing, cbytes = _read(paths["committing"], {
                "schema_version", "generation", "prepared_sha256",
            })
            if (
                type(committing["schema_version"]) is not int
                or committing["schema_version"] != 1
                or committing["generation"] != gen
                or committing["prepared_sha256"] != _digest(pbytes)
            ):
                raise BackupIntentError("committing intent not prepared-bound")
            phases["committing"] = committing
        else:
            cbytes = None
        if "acknowledged" in paths:
            if cbytes is None:
                raise BackupIntentError("acknowledgement lacks committing intent")
            ack, _ = _read(paths["acknowledged"], {
                "schema_version", "generation", "committing_sha256", "head_sha256",
            })
            if (
                type(ack["schema_version"]) is not int
                or ack["schema_version"] != 1
                or ack["generation"] != gen
                or ack["committing_sha256"] != _digest(cbytes)
            ):
                raise BackupIntentError("acknowledgement not committed-bound")
            _sha(ack["head_sha256"], "acknowledged HEAD")
            phases["acknowledged"] = ack
            acknowledged[gen] = ack
        if "abandoned" in paths:
            if "acknowledged" in paths:
                raise BackupIntentError("operator intent has conflicting terminal states")
            # No independent live host/process/FD quiescence authority exists
            # here. An old committing record cannot be cancelled by a
            # finite local inspection, even if its HEAD is not yet visible.
            if "committing" in paths:
                raise BackupIntentError("committing abandonment lacks independent quiescence")
            abandoned, _ = _read(paths["abandoned"], {
                "schema_version", "generation", "prepared_sha256",
                "committing_sha256", "previous_head_sha256",
            })
            if (
                type(abandoned["schema_version"]) is not int
                or abandoned["schema_version"] != 1
                or abandoned["generation"] != gen
                or abandoned["prepared_sha256"] != _digest(pbytes)
                or abandoned["committing_sha256"] != (
                    _ZERO_HEAD if cbytes is None else _digest(cbytes)
                )
                or abandoned["previous_head_sha256"] != prepared["previous_head_sha256"]
            ):
                raise BackupIntentError("abandoned intent not bound to immutable predecessor")
            phases["abandoned"] = abandoned
        records[gen] = phases

    # Every retained acknowledged generation must extend exactly one earlier
    # acknowledged HEAD. An independent latest-pointer is not trusted.
    tip = _ZERO_HEAD
    remaining = set(acknowledged)
    visited_heads: set[str] = set()
    while remaining:
        candidates = [
            gen for gen in remaining
            if records[gen]["prepared"]["previous_head_sha256"] == tip
        ]
        if len(candidates) != 1:
            raise BackupIntentError("operator acknowledgement chain not linear")
        gen = candidates[0]
        tip = acknowledged[gen]["head_sha256"]
        if tip in visited_heads:
            raise BackupIntentError("operator acknowledgement HEAD repeated")
        visited_heads.add(tip)
        remaining.remove(gen)

    known_heads = visited_heads | {_ZERO_HEAD}
    for gen, phases in records.items():
        if (
            "abandoned" in phases
            and phases["prepared"]["previous_head_sha256"] not in known_heads
        ):
            raise BackupIntentError("abandoned intent predecessor not acknowledged")
    pending = [
        gen for gen, phases in records.items()
        if "acknowledged" not in phases and "abandoned" not in phases
    ]
    if len(pending) > 1:
        raise BackupIntentError("multiple pending operator intents")
    if pending and records[pending[0]]["prepared"]["previous_head_sha256"] != tip:
        raise BackupIntentError("pending operator intent predecessor drift")
    return tip, records


def _verify_anchored_pairs(
    output_root: Path, anchor_root: Path, expected_head_sha256: str,
) -> dict[str, object]:
    """Verify every acknowledged historical image, never only the tip.

    This independent offline check is mandatory before a later seal and
    before acknowledgement. Finite hashes are not host-UID quiescence proof.
    """
    expected = _sha(expected_head_sha256, "anchored HEAD")
    try:
        output = _private_root(output_root)
        anchor = _private_root(anchor_root)
        if output == anchor or output in anchor.parents or anchor in output.parents:
            raise BackupIntentError("backup output and anchor overlap")
        head = _read_head(anchor, expected)
        if head is None:
            raise BackupIntentError("acknowledged ancestor HEAD absent")
        records = _validate_anchored_history(anchor, output, expected, head)
        for generation, record in records.values():
            root = output / generation
            checked = verify_offline_pair(
                root / "mark.sqlite",
                root / "witness.sqlite",
                root / "manifest.json",
                expected_manifest_sha256=record["manifest_sha256"],
            )
            if checked["generation"] != generation:
                raise BackupIntentError("historical generation image identity changed")
        if _read_head(anchor, expected) != head:
            raise BackupIntentError("anchored HEAD changed during history verification")
        return head
    except (OSError, PairPreflightError, PairedBackupOperatorError) as exc:
        raise BackupIntentError("acknowledged historical pair untrusted") from exc


class BackupIntentAuthority:
    """Durably consume independent one-shot intents, without trusted input SHAs."""

    def __init__(self, state_root: Path) -> None:
        if not isinstance(state_root, Path):
            raise BackupIntentError("operator root must be a Path")
        self.root = _private_root(state_root)
        _bootstrap_lock_once(self.root)

    def prepare(
        self, generation: str, sqlite_image: Path, witness_image: Path,
        *, previous_head_sha256: str | None,
    ) -> dict[str, str]:
        gen = _generation(generation)
        prev = _sha(previous_head_sha256, "previous HEAD") if previous_head_sha256 is not None else _ZERO_HEAD
        try:
            if not isinstance(sqlite_image, Path) or not isinstance(witness_image, Path):
                raise BackupIntentError("cold image paths required")
            if any(
                not p.is_absolute() or str(p) != os.path.normpath(p)
                for p in (sqlite_image, witness_image)
            ):
                raise BackupIntentError("cold image paths must be absolute normalized")
            with _locked(self.root):
                tip, records = _state(self.root)
                if gen in records or any(
                    "acknowledged" not in p and "abandoned" not in p
                    for p in records.values()
                ):
                    raise BackupIntentError("operator generation already used or pending")
                if tip != prev:
                    raise BackupIntentError("operator predecessor not last acknowledged HEAD")
                # The inspector itself holds the Witness exclusion lock and
                # pins/hashes BOTH FDs. Reacquiring the nonreentrant flock
                # around it would block every valid preparation.
                inspected = inspect_offline_pair(sqlite_image, witness_image)
                source_sql = inspected["sqlite_sha256"]
                source_wit = inspected["witness_sha256"]
                prepared = {
                    "schema_version": 1, "generation": gen,
                    "previous_head_sha256": prev,
                    "sqlite_sha256": source_sql, "witness_sha256": source_wit,
                    "sqlite_path": str(sqlite_image),
                    "witness_path": str(witness_image),
                }
                _new_file(_record(self.root, gen, "prepared"), _canonical(prepared))
                _fsync_dir(self.root)
                return {k: str(v) for k, v in prepared.items() if k != "schema_version"}
        except (OSError, PairPreflightError, PairedBackupOperatorError) as exc:
            raise BackupIntentError("independent cold image prepare failed") from exc

    def begin_commit(self, generation: str) -> dict[str, str]:
        """Do not preempt a seal/abandon transaction holding the operator root."""
        with _operation_lock(self.root):
            return self._begin_commit_exclusive(generation)

    def _begin_commit_exclusive(self, generation: str) -> dict[str, str]:
        """Persist a one-shot commit intent while holding transaction authority."""
        gen = _generation(generation)
        with _locked(self.root):
            _, records = _state(self.root)
            phases = records.get(gen)
            if (
                phases is None or "committing" in phases
                or "acknowledged" in phases or "abandoned" in phases
            ):
                raise BackupIntentError("generation not singly prepared; never retry commit")
            prepared = phases["prepared"]
            committing = {
                "schema_version": 1, "generation": gen,
                "prepared_sha256": _digest(_canonical(prepared)),
            }
            _new_file(_record(self.root, gen, "committing"), _canonical(committing))
            _fsync_dir(self.root)
            return {k: str(v) for k, v in prepared.items() if k != "schema_version"}

    def abandon(
        self, generation: str, output_root: Path, anchor_root: Path,
    ) -> dict[str, object]:
        """Retire a prepared-only identity before any commit may take effect."""
        with _operation_lock(self.root):
            return self._abandon_exclusive(generation, output_root, anchor_root)

    def _abandon_exclusive(
        self, generation: str, output_root: Path, anchor_root: Path,
    ) -> dict[str, object]:
        """Retire an uncommitted preparation without reclaiming its identity.

        Once a committing record exists, a separate sealer may have already
        started while leaving no filesystem effects. This library has no
        independent host-quiescence attestation and must preserve that fence.
        """
        gen = _generation(generation)
        try:
            with _locked(self.root):
                tip, records = _state(self.root)
                phases = records.get(gen)
                if phases is None or "acknowledged" in phases or "abandoned" in phases:
                    raise BackupIntentError("no safely abortable one-shot intent")
                if "committing" in phases:
                    raise BackupIntentError(
                        "committing intent requires independent host quiescence; "
                        "cannot abandon with local proof"
                    )
                prepared = phases["prepared"]
                previous = prepared["previous_head_sha256"]
                if previous != tip:
                    raise BackupIntentError("operator predecessor changed before abandon")
                output = _private_root(output_root)
                anchor = _private_root(anchor_root)
                if output == anchor or output in anchor.parents or anchor in output.parents:
                    raise BackupIntentError("operator backup roots overlap")

                def prove_no_effect() -> None:
                    if any(p.name == gen for p in output.iterdir()):
                        raise BackupIntentError("generation output may already have effects")
                    if any(p.name == gen + ".json" for p in anchor.iterdir()):
                        raise BackupIntentError("generation head record may already exist")
                    if any(p.name.startswith(".partial-") for p in output.iterdir()):
                        raise BackupIntentError("partial generation may contain effects")
                    if any(
                        p.name == ".inflight" or p.name.startswith(".next-")
                        for p in anchor.iterdir()
                    ):
                        raise BackupIntentError("crash fence or pending HEAD exists")
                    if previous == _ZERO_HEAD:
                        if (
                            any(p.name != ".authority.lock" for p in anchor.iterdir())
                            or any(output.iterdir())
                        ):
                            raise BackupIntentError("bootstrap state is ambiguous")
                    else:
                        prior = _read_head(anchor, previous)
                        if prior is None:
                            raise BackupIntentError("acknowledged predecessor missing")
                        _validate_anchored_history(anchor, output, previous, prior)

                prove_no_effect()
                _fsync_dir(output)
                _fsync_dir(anchor)
                prove_no_effect()
                abandoned = {
                    "schema_version": 1,
                    "generation": gen,
                    "prepared_sha256": _digest(_canonical(prepared)),
                    "committing_sha256": _ZERO_HEAD,
                    "previous_head_sha256": previous,
                }
                _new_file(_record(self.root, gen, "abandoned"), _canonical(abandoned))
                _fsync_dir(self.root)
                return {
                    "generation": gen, "abandoned_once": True,
                    "restore_authorized": False, "external_write_authorized": False,
                }
        except (OSError, PairPreflightError, PairedBackupOperatorError) as exc:
            raise BackupIntentError("effect-free abandon proof uncertain; preserve fences") from exc

    def acknowledge(
        self, generation: str, output_root: Path, anchor_root: Path,
    ) -> dict[str, object]:
        """Serialize external ACK with any active seal/abandon transaction."""
        with _operation_lock(self.root):
            return self._acknowledge_exclusive(generation, output_root, anchor_root)

    def _acknowledge_exclusive(
        self, generation: str, output_root: Path, anchor_root: Path,
    ) -> dict[str, object]:
        """Verify HEAD and consume one intent under the complete operation lock."""
        gen = _generation(generation)
        try:
            with _locked(self.root):
                _, records = _state(self.root)
                phases = records.get(gen)
                if (
                    phases is None or "committing" not in phases
                    or "acknowledged" in phases or "abandoned" in phases
                ):
                    raise BackupIntentError("no unconsumed external committing intent")
                prepared = phases["prepared"]
                output = _private_root(output_root)
                anchor = _private_root(anchor_root)
                if output == anchor or output in anchor.parents or anchor in output.parents:
                    raise BackupIntentError("backup output and anchor overlap")
                with _pinned_file(anchor / "latest.json") as fd:
                    size = os.fstat(fd).st_size
                    if not 1 <= size <= _MAX_RECORD:
                        raise BackupIntentError("published HEAD invalid")
                    head_sha = _digest(os.pread(fd, size, 0))
                head = _read_head(anchor, head_sha)
                if (
                    head is None or head["generation"] != gen
                    or head["predecessor_sha256"] != prepared["previous_head_sha256"]
                ):
                    raise BackupIntentError("HEAD not bound to unconsumed intent")
                _verify_anchored_pairs(output, anchor, head_sha)
                manifest = _check_generation_dir(output, gen, head["manifest_sha256"])
                if (
                    manifest["sqlite_sha256"] != prepared["sqlite_sha256"]
                    or manifest["witness_sha256"] != prepared["witness_sha256"]
                ):
                    raise BackupIntentError("HEAD images mismatch independent cold source")
                source = output / gen
                verified = verify_offline_pair(
                    source / "mark.sqlite", source / "witness.sqlite",
                    source / "manifest.json",
                    expected_manifest_sha256=head["manifest_sha256"],
                )
                if verified["generation"] != gen:
                    raise BackupIntentError("published generation invalid")
                reconciled = False
                inflight = anchor / ".inflight"
                try:
                    inflight_info = inflight.lstat()
                except FileNotFoundError:
                    inflight_info = None
                if inflight_info is not None:
                    if (
                        not stat.S_ISREG(inflight_info.st_mode)
                        or inflight_info.st_nlink != 1
                    ):
                        raise BackupIntentError("crash marker is not a regular file")
                    _reconcile_committed_inflight(
                        output, anchor,
                        expected_current_head_sha256=head_sha,
                        expected_generation=gen,
                        expected_previous_head_sha256=(
                            None if prepared["previous_head_sha256"] == _ZERO_HEAD
                            else prepared["previous_head_sha256"]
                        ),
                        expected_sqlite_sha256=prepared["sqlite_sha256"],
                        expected_witness_sha256=prepared["witness_sha256"],
                    )
                    reconciled = True
                if _read_head(anchor, head_sha) != head:
                    raise BackupIntentError("HEAD changed before external acknowledgement")
                # Last full-history verification is immediately before durable
                # external acknowledgement; this remains same-UID-race-limited.
                _verify_anchored_pairs(output, anchor, head_sha)
                _fsync_dir(anchor)
                if _read_head(anchor, head_sha) != head:
                    raise BackupIntentError("HEAD changed before durable external ACK")
                acknowledged = {
                    "schema_version": 1, "generation": gen,
                    "committing_sha256": _digest(_canonical(phases["committing"])),
                    "head_sha256": head_sha,
                }
                _new_file(_record(self.root, gen, "acknowledged"), _canonical(acknowledged))
                _fsync_dir(self.root)
                return {
                    "generation": gen, "head_sha256": head_sha,
                    "reconciled": reconciled, "acknowledged_once": True,
                    "restore_authorized": False, "external_write_authorized": False,
                }
        except (OSError, PairPreflightError, PairedBackupOperatorError) as exc:
            raise BackupIntentError("operator acknowledgement uncertain; preserve fences") from exc

    def seal_prepared(
        self, generation: str, output_root: Path, anchor_root: Path,
    ) -> dict[str, object]:
        """Serialize the complete preflight, commit, publication and ACK."""
        with _operation_lock(self.root):
            return self._seal_prepared_exclusive(generation, output_root, anchor_root)

    def _seal_prepared_exclusive(
        self, generation: str, output_root: Path, anchor_root: Path,
    ) -> dict[str, object]:
        """Preflight trusted cold inputs before consuming a one-shot commit."""
        gen = _generation(generation)
        with _locked(self.root):
            tip, records = _state(self.root)
            phases = records.get(gen)
            if (
                phases is None or "committing" in phases
                or "acknowledged" in phases or "abandoned" in phases
            ):
                raise BackupIntentError("operator intent not singly prepared")
            prepared = phases["prepared"]
            if tip != prepared["previous_head_sha256"]:
                raise BackupIntentError("preflight predecessor no longer acknowledged")
            cold = {k: str(v) for k, v in prepared.items() if k != "schema_version"}
        if cold["previous_head_sha256"] != _ZERO_HEAD:
            _verify_anchored_pairs(
                output_root, anchor_root, cold["previous_head_sha256"],
            )
        try:
            inspected = inspect_offline_pair(
                Path(cold["sqlite_path"]), Path(cold["witness_path"]),
            )
        except (OSError, PairPreflightError, PairedBackupOperatorError) as exc:
            raise BackupIntentError("cold source preflight uncertain") from exc
        if (
            inspected["sqlite_sha256"] != cold["sqlite_sha256"]
            or inspected["witness_sha256"] != cold["witness_sha256"]
        ):
            raise BackupIntentError("cold image changed since independent preparation")
        intent = self.begin_commit(gen)
        # A previously acknowledged pair that has since been damaged must
        # block the next seal *before* it publishes a successor HEAD.
        if intent["previous_head_sha256"] != _ZERO_HEAD:
            _verify_anchored_pairs(
                output_root, anchor_root, intent["previous_head_sha256"],
            )
        result = seal_offline_pair(
            Path(intent["sqlite_path"]), Path(intent["witness_path"]),
            output_root, anchor_root,
            expected_sqlite_sha256=intent["sqlite_sha256"],
            expected_witness_sha256=intent["witness_sha256"],
            expected_previous_head_sha256=(
                None if intent["previous_head_sha256"] == _ZERO_HEAD
                else intent["previous_head_sha256"]
            ),
            generation=generation,
        )
        ack = self.acknowledge(generation, output_root, anchor_root)
        if ack["head_sha256"] != result["head_sha256"]:
            raise BackupIntentError("operator HEAD disagrees with seal result")
        return {**result, "external_operator_intent_acknowledged": True}
