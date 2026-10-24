"""Seal a trusted *offline* SQLite/Witness pair as one generation.

This is a build block for an independent operator, NOT the live backup authority.
The caller must independently prove source quiescence, process/FD/mmap revocation,
source ownership and trusted provenance before these two cold images exist.
The routine never stops services, touches Kleinanzeigen or authorizes restore.
There is deliberately no live/Compose entrypoint.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Iterator

from .fence_witness import FenceWitnessError, _private_directory
from .paired_restore_preflight import (
    PairPreflightError, _MAX_IMAGE_BYTES, _MAX_MANIFEST, _canonical,
    _manifest, _no_sidecars, _pinned_file, _sha256_fd,
    _witness_owner_absent, inspect_offline_pair, verify_offline_pair,
)


class PairedBackupOperatorError(RuntimeError):
    """The cold pair is not an independently anchored backup generation."""


_SHA = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_GENERATION = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_MAX_HEAD = 4096
_ZERO = "0" * 64


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _private_root(path: Path) -> Path:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or path != Path(os.path.normpath(path))
    ):
        raise PairedBackupOperatorError("operator directory must be absolute and normalized")
    try:
        _private_directory(path, os.geteuid(), mode=0o700)
    except FenceWitnessError as exc:
        raise PairedBackupOperatorError("operator directory is not privately owned") from exc
    return path


def _separate_roots(source: Path, output: Path, anchor: Path) -> None:
    roots = (source, output, anchor)
    for i, first in enumerate(roots):
        for second in roots[i + 1:]:
            if first == second or first in second.parents or second in first.parents:
                raise PairedBackupOperatorError("offline inputs, outputs and anchor overlap")


def _require_digest(value: str, label: str) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise PairedBackupOperatorError(f"{label} SHA-256 anchor is invalid")
    return value


def _unoccupied(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise PairedBackupOperatorError("generation artifact already exists; no blind retry")


def _fsync_dir(directory: Path) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _new_file(path: Path, data: bytes) -> None:
    fd = os.open(
        path, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
        os.O_CLOEXEC | os.O_NOFOLLOW, 0o600,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise PairedBackupOperatorError("cold pair file write failed")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_pinned(fd: int, destination: Path) -> str:
    info = os.fstat(fd)
    if not 100 <= info.st_size <= 512 * 1024 * 1024:
        raise PairedBackupOperatorError("source cold image size is unsafe")
    out = os.open(
        destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
        os.O_CLOEXEC | os.O_NOFOLLOW, 0o600,
    )
    sha = hashlib.sha256()
    try:
        offset = 0
        while offset < info.st_size:
            block = os.pread(fd, min(1024 * 1024, info.st_size - offset), offset)
            if not block:
                raise PairedBackupOperatorError("source cold image was shortened")
            sha.update(block)
            view = memoryview(block)
            while view:
                count = os.write(out, view)
                if count <= 0:
                    raise PairedBackupOperatorError("cold image copy failed")
                view = view[count:]
            offset += len(block)
        os.fsync(out)
    finally:
        os.close(out)
    return sha.hexdigest()


def _read_head(anchor: Path, previous_sha: str | None) -> dict[str, object] | None:
    head_file = anchor / "latest.json"
    try:
        head_file.lstat()
    except FileNotFoundError:
        if previous_sha is not None:
            raise PairedBackupOperatorError("previous anchored generation is missing")
        return None
    if previous_sha is None:
        raise PairedBackupOperatorError("an existing generation requires its pinned head digest")
    _require_digest(previous_sha, "previous head")
    with _pinned_file(head_file) as fd:
        size = os.fstat(fd).st_size
        if not 1 <= size <= _MAX_HEAD:
            raise PairedBackupOperatorError("head anchor length invalid")
        raw = os.pread(fd, size, 0)
    if _digest(raw) != previous_sha:
        raise PairedBackupOperatorError("external previous generation digest mismatches")
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PairedBackupOperatorError("previous generation record is invalid") from exc
    if (
        type(obj) is not dict
        or set(obj) != {
            "schema_version", "generation", "manifest_sha256", "predecessor_sha256",
        }
        or type(obj["schema_version"]) is not int
        or obj["schema_version"] != 1
        or type(obj["generation"]) is not str
        or _GENERATION.fullmatch(obj["generation"]) is None
        or type(obj["manifest_sha256"]) is not str
        or _SHA.fullmatch(obj["manifest_sha256"]) is None
        or type(obj["predecessor_sha256"]) is not str
        or _SHA.fullmatch(obj["predecessor_sha256"]) is None
        or _canonical(obj) != raw
    ):
        raise PairedBackupOperatorError("previous generation schema is not canonical")
    record_file = anchor / (obj["generation"] + ".json")
    with _pinned_file(record_file) as fd:
        if os.fstat(fd).st_size != len(raw) or os.pread(fd, len(raw), 0) != raw:
            raise PairedBackupOperatorError("head anchor disagrees with immutable record")
    return obj


def _assert_no_ambiguous_artifacts(
    anchor: Path, output: Path, *, has_head: bool,
) -> None:
    """Fail closed on interrupted generations before creating a new chain tip.

    A missing HEAD alone never proves first initialization: an old valid
    source pair may have been published already and only the mutable pointer
    rolled back or was lost in a crash. Never silently re-bootstrap around
    retained history or other generations.
    """
    if not has_head:
        if any(p.name != ".authority.lock" for p in anchor.iterdir()):
            raise PairedBackupOperatorError(
                "head is absent but anchored generation history remains"
            )
        if any(output.iterdir()):
            raise PairedBackupOperatorError(
                "head is absent but a cold generation or partial copy remains"
            )
        return
    # A previous generation has an externally pinned HEAD. A retained
    # unpublished staging image or incomplete atomic-head handoff is UNKNOWN,
    # not harmless: inspect and reconcile before another generation.
    if any(p.name == ".inflight" for p in anchor.iterdir()):
        raise PairedBackupOperatorError("unsettled generation requires operator recovery")
    if any(p.name.startswith(".next-") for p in anchor.iterdir()):
        raise PairedBackupOperatorError("unsettled head update requires operator recovery")
    if any(p.name.startswith(".partial-") for p in output.iterdir()):
        raise PairedBackupOperatorError("unsettled partial generation requires recovery")


def _check_generation_dir(
    output: Path, generation: str, manifest_sha256: str,
) -> dict[str, object]:
    """Require every file of a sealed generation, not just its directory name.

    This is a conservative offline check, not a same-UID quiescence proof.
    """
    if type(generation) is not str or _GENERATION.fullmatch(generation) is None:
        raise PairedBackupOperatorError("retained generation identity is invalid")
    _require_digest(manifest_sha256, "retained manifest")
    generation_dir = _private_root(output / generation)
    expected_names = frozenset({
        "mark.sqlite", "witness.sqlite", "witness.sqlite.lock", "manifest.json",
    })
    if {entry.name for entry in generation_dir.iterdir()} != expected_names:
        raise PairedBackupOperatorError("generation artifact set is incomplete")
    for name in ("mark.sqlite", "witness.sqlite", "witness.sqlite.lock"):
        with _pinned_file(generation_dir / name) as fd:
            size = os.fstat(fd).st_size
            if name == "witness.sqlite.lock":
                if size != 0:
                    raise PairedBackupOperatorError("generation Witness lock is invalid")
            elif not 100 <= size <= _MAX_IMAGE_BYTES:
                raise PairedBackupOperatorError("generation SQLite image size is unsafe")
    with _pinned_file(generation_dir / "manifest.json") as fd:
        size = os.fstat(fd).st_size
        if not 0 < size <= _MAX_MANIFEST:
            raise PairedBackupOperatorError("generation manifest length is invalid")
        anchored_manifest = _manifest(
            os.pread(fd, size, 0), manifest_sha256,
        )
        if anchored_manifest["generation"] != generation:
            raise PairedBackupOperatorError(
                "generation manifest disagrees with independent anchor"
            )
        return anchored_manifest


def _validate_anchored_history(
    anchor: Path, output: Path, head_sha: str, head: dict[str, object],
    *, pending_generation: str | None = None,
) -> dict[str, tuple[str, dict[str, object]]]:
    """Fail closed when a committed ancestor or output artifact is missing.

    Called under the independent anchor lock; only identities and canonical
    chain records are checked here. Crash reconciliation verifies every image.
    """
    # A stray staging image, next-HEAD, unpublished generation or
    # unlinked immutable record is an unresolved operation. Do not
    # retire the fence just because latest.json looks consistent.
    anchor_records: dict[str, tuple[str, dict[str, object]]] = {}
    for entry in anchor.iterdir():
        name = entry.name
        if name in {".authority.lock", ".inflight", "latest.json"}:
            continue
        if not name.endswith(".json") or _GENERATION.fullmatch(name[:-5]) is None:
            raise PairedBackupOperatorError("unexpected independent anchor artifact")
        with _pinned_file(entry) as fd:
            size = os.fstat(fd).st_size
            if not 1 <= size <= _MAX_HEAD:
                raise PairedBackupOperatorError("generation record length invalid")
            raw = os.pread(fd, size, 0)
        record = json.loads(raw)
        if (
            type(record) is not dict
            or set(record) != {
                "schema_version", "generation", "manifest_sha256",
                "predecessor_sha256",
            }
            or type(record["schema_version"]) is not int
            or record["schema_version"] != 1
            or record["generation"] != name[:-5]
            or type(record["manifest_sha256"]) is not str
            or _SHA.fullmatch(record["manifest_sha256"]) is None
            or type(record["predecessor_sha256"]) is not str
            or _SHA.fullmatch(record["predecessor_sha256"]) is None
            or _canonical(record) != raw
        ):
            raise PairedBackupOperatorError("generation history record is not canonical")
        digest = _digest(raw)
        if digest in anchor_records:
            raise PairedBackupOperatorError("generation history has duplicate head hash")
        anchor_records[digest] = (name[:-5], record)

    generations: set[str] = set()
    for entry in output.iterdir():
        if _GENERATION.fullmatch(entry.name) is None:
            raise PairedBackupOperatorError("unsettled or unexpected backup artifact")
        _private_root(entry)
        generations.add(entry.name)

    # During a new seal, its completely copied but not yet HEAD-anchored
    # directory is the one permitted additional output artifact. It must not
    # shadow or replace any already committed generation.
    if pending_generation is not None:
        if (
            _GENERATION.fullmatch(pending_generation) is None
            or pending_generation not in generations
            or any(generation == pending_generation for generation, _ in anchor_records.values())
        ):
            raise PairedBackupOperatorError("pending generation is not uniquely staged")
        generations.remove(pending_generation)

    visited: set[str] = set()
    link = head_sha
    while link != _ZERO:
        if link in visited or link not in anchor_records:
            raise PairedBackupOperatorError("backup head chain is broken or cyclic")
        visited.add(link)
        generation, record = anchor_records[link]
        if generation not in generations:
            raise PairedBackupOperatorError("committed generation is missing")
        link = record["predecessor_sha256"]
    if visited != set(anchor_records) or len(visited) != len(generations):
        raise PairedBackupOperatorError("unlinked or unpublished backup generation remains")
    if head["manifest_sha256"] != anchor_records[head_sha][1]["manifest_sha256"]:
        raise PairedBackupOperatorError("current manifest anchor mismatches")

    # Keep the same canonical artifact grammar for retained ancestors and
    # the freshly published, not-yet-anchored pair.
    for digest in visited:
        generation, record = anchor_records[digest]
        _check_generation_dir(output, generation, record["manifest_sha256"])

    return anchor_records


@contextmanager
def _authority_lock(
    anchor: Path, *, create_if_missing: bool = True,
) -> Iterator[None]:
    path = anchor / ".authority.lock"
    if create_if_missing:
        try:
            _new_file(path, b"")
            _fsync_dir(anchor)
        except FileExistsError:
            pass
    # Reconciliation must reuse the existing operator-lock inode. Recreating
    # a missing lock could coexist with a still-running holder of the old
    # (unlinked) inode, falsely claiming exclusive authority.
    with _pinned_file(path) as fd:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            raise PairedBackupOperatorError("operator lock is untrusted")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PairedBackupOperatorError("another operator is sealing a generation") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)



def _reconcile_committed_inflight(
    output_root: Path,
    anchor_root: Path,
    *,
    expected_current_head_sha256: str,
    expected_generation: str,
    expected_previous_head_sha256: str | None,
    expected_sqlite_sha256: str,
    expected_witness_sha256: str,
) -> dict[str, object]:
    """Release an inflight intent only with independently pinned source provenance.

    The previous acknowledged HEAD and both cold source hashes MUST come
    from an operator-protected, durable PRE-SEAL intent. The current HEAD
    digest alone can be self-attested after a crash and proves nothing about
    the provenance of its images. These inputs are not host-quiescence proof
    and do not authorize restore, platform writes or service cutover.
    """
    output = _private_root(output_root)
    anchor = _private_root(anchor_root)
    if output == anchor or output in anchor.parents or anchor in output.parents:
        raise PairedBackupOperatorError("backup and independent anchors overlap")
    head_sha = _require_digest(expected_current_head_sha256, "current head")
    previous_head_sha = (
        _require_digest(expected_previous_head_sha256, "previous head")
        if expected_previous_head_sha256 is not None else None
    )
    expected_sqlite_sha = _require_digest(
        expected_sqlite_sha256, "independently attested SQLite image"
    )
    expected_witness_sha = _require_digest(
        expected_witness_sha256, "independently attested Witness image"
    )
    if previous_head_sha == head_sha:
        raise PairedBackupOperatorError(
            "reconciliation cannot clear an already acknowledged HEAD"
        )
    if (
        type(expected_generation) is not str
        or _GENERATION.fullmatch(expected_generation) is None
    ):
        raise PairedBackupOperatorError("committed generation identity is invalid")

    try:
        with _authority_lock(anchor, create_if_missing=False):
            head = _read_head(anchor, head_sha)
            if head is None or head["generation"] != expected_generation:
                raise PairedBackupOperatorError("independently pinned HEAD is not the inflight generation")
            if head["predecessor_sha256"] != (previous_head_sha or _ZERO):
                raise PairedBackupOperatorError(
                    "recovered HEAD does not extend the independently acknowledged predecessor"
                )

            inflight = anchor / ".inflight"
            with _pinned_file(inflight) as fd:
                info = os.fstat(fd)
                if (
                    info.st_size != 32
                    or os.pread(fd, 33, 0) != expected_generation.encode("ascii")
                ):
                    raise PairedBackupOperatorError("persisted generation intent disagrees with HEAD")
                marker_identity = (info.st_dev, info.st_ino)

            # Reuse the exact committed-chain validation used at seal time.
            anchor_records = _validate_anchored_history(
                anchor, output, head_sha, head,
            )
            current_manifest = _check_generation_dir(
                output, expected_generation, head["manifest_sha256"],
            )
            if (
                current_manifest["sqlite_sha256"] != expected_sqlite_sha
                or current_manifest["witness_sha256"] != expected_witness_sha
            ):
                raise PairedBackupOperatorError(
                    "recovered pair differs from independently attested cold source images"
                )
            visited = set(anchor_records)

            # Chain links alone do not prove historical image integrity.
            # Every retained generation must still match its immutable
            # manifest before clearing the only crash-intent marker. This
            # infrequent explicit offline operation is deliberately thorough.
            verified: dict[str, object] | None = None
            for digest in visited:
                generation, record = anchor_records[digest]
                image_dir = output / generation
                inspected = verify_offline_pair(
                    image_dir / "mark.sqlite",
                    image_dir / "witness.sqlite",
                    image_dir / "manifest.json",
                    expected_manifest_sha256=record["manifest_sha256"],
                )
                if inspected["generation"] != generation:
                    raise PairedBackupOperatorError("historical pair generation drifted")
                if generation == expected_generation:
                    verified = inspected
            if verified is None:
                raise PairedBackupOperatorError("committed pair was not fully verified")
            # Validate the pointer once more under the operator lock. No
            # historically opened write mapping is revoked by this operation.
            if _read_head(anchor, head_sha) != head:
                raise PairedBackupOperatorError("committed HEAD changed during reconciliation")
            # Pathname identities may change after an earlier verification
            # while another same-UID process modifies retained artifacts.
            # Reconcile is exceptional: reverify the entire retained pair
            # history immediately before consuming the crash intent.
            _validate_anchored_history(anchor, output, head_sha, head)
            for digest in visited:
                generation, record = anchor_records[digest]
                image_dir = output / generation
                final = verify_offline_pair(
                    image_dir / "mark.sqlite",
                    image_dir / "witness.sqlite",
                    image_dir / "manifest.json",
                    expected_manifest_sha256=record["manifest_sha256"],
                )
                if final["generation"] != generation:
                    raise PairedBackupOperatorError("committed pair changed before intent clearance")
            # A readable latest.json may have been renamed into place just
            # before the original process crashed without fsyncing the parent
            # directory. Persist the checked HEAD and immutable record FIRST.
            # If this barrier fails, the intent marker must remain in place.
            _fsync_dir(anchor)
            # Re-reading only before the potentially long historical pair
            # verification is insufficient: an older HEAD may be replaced
            # while verification is in progress. Preserve the crash intent.
            if _read_head(anchor, head_sha) != head:
                raise PairedBackupOperatorError(
                    "committed HEAD changed before intent clearance"
                )
            info = inflight.lstat()
            if (info.st_dev, info.st_ino) != marker_identity:
                raise PairedBackupOperatorError("generation intent inode changed")
            os.unlink(inflight)
            _fsync_dir(anchor)
            return {
                "generation": expected_generation,
                "head_sha256": head_sha,
                "manifest_sha256": head["manifest_sha256"],
                "reconciled": True,
                "offline_only": True,
                "live_source_quiescence_attested": False,
                "restore_authorized": False,
                "external_write_authorized": False,
                "fences": verified,
            }
    except PairedBackupOperatorError:
        raise
    except (
        OSError, ValueError, TypeError, KeyError, UnicodeError,
        RecursionError, PairPreflightError, FenceWitnessError,
    ) as exc:
        raise PairedBackupOperatorError(
            "committed generation not reconciled; preserve recovery evidence"
        ) from exc


def reconcile_committed_inflight(
    output_root: Path,
    anchor_root: Path,
    *,
    expected_current_head_sha256: str,
    expected_generation: str,
    expected_previous_head_sha256: str | None,
    expected_sqlite_sha256: str,
    expected_witness_sha256: str,
) -> dict[str, object]:
    """Reject direct marker clearance without a durable external one-shot intent.

    The caller-controlled hashes and a reconstructed .inflight marker cannot
    prove that an independent operator has an unconsumed committing record.
    BackupIntentAuthority.acknowledge validates and consumes that record under
    the protected operator lock before reporting success. The internal helper
    above is only usable inside that locked, trusted operator transaction.
    This does not replace the separate host UID/mount/FD security boundary.
    """
    raise PairedBackupOperatorError(
        "direct reconciliation forbidden: use BackupIntentAuthority.acknowledge "
        "with an unconsumed external committing intent"
    )


def seal_offline_pair(
    source_sqlite: Path,
    source_witness: Path,
    output_root: Path,
    anchor_root: Path,
    *,
    expected_sqlite_sha256: str,
    expected_witness_sha256: str,
    expected_previous_head_sha256: str | None,
    generation: str | None = None,
) -> dict[str, object]:
    """Create-only, digest-bound cold generation under a separate operator anchor.

    Inputs MUST be trusted cold images already staged into an owner-private
    directory after an independently attested host cutover. The supplied
    source digests MUST come from a trusted, separate attestation channel.
    No caller-provided boolean or self-asserted proof is accepted as a host
    attestation. A successful result remains explicitly NOT restore authority.

    The anchor lives outside output_root; callers must preserve/pin its latest
    SHA independently. Failures leave any uncertain, newly written artifacts
    in place for operator inspection, never retry/overwrite automatically.
    """
    sql = source_sqlite
    witness = source_witness
    if not isinstance(sql, Path) or not isinstance(witness, Path):
        raise PairedBackupOperatorError("cold input paths must be exact Path objects")
    if sql == witness or sql.name.endswith(("-wal", "-shm", "-journal")):
        raise PairedBackupOperatorError("cold source identities are invalid")
    source_dir = _private_root(sql.parent)
    _private_root(witness.parent)
    if witness.parent != source_dir:
        raise PairedBackupOperatorError("trusted cold images require one private source root")
    output = _private_root(output_root)
    anchor = _private_root(anchor_root)
    _separate_roots(source_dir, output, anchor)
    sqlite_sha = _require_digest(expected_sqlite_sha256, "SQLite image")
    witness_sha = _require_digest(expected_witness_sha256, "Witness image")
    if expected_previous_head_sha256 is not None:
        _require_digest(expected_previous_head_sha256, "previous head")
    if generation is None:
        generation = secrets.token_hex(16)
    if type(generation) is not str or _GENERATION.fullmatch(generation) is None:
        raise PairedBackupOperatorError("generation must be 32 lowercase hex digits")
    temporary = output / (".partial-" + generation)
    published = output / generation

    try:
        # The operator's anchor locks serialize HEAD publication. They do NOT
        # quiesce a live SQLite writer or revoke historical mmap descriptors.
        with _authority_lock(anchor):
            prior = _read_head(anchor, expected_previous_head_sha256)
            _assert_no_ambiguous_artifacts(anchor, output, has_head=prior is not None)
            if prior is not None:
                _validate_anchored_history(
                    anchor, output, expected_previous_head_sha256, prior,
                )
            _unoccupied(temporary)
            _unoccupied(published)
            _unoccupied(anchor / (generation + ".json"))
            _unoccupied(anchor / (".next-" + generation))
            _no_sidecars(sql)
            _no_sidecars(witness)
            with ExitStack() as stack:
                stack.enter_context(_witness_owner_absent(witness))
                sqlite_fd = stack.enter_context(_pinned_file(sql))
                witness_fd = stack.enter_context(_pinned_file(witness))
                if (
                    os.fstat(sqlite_fd).st_dev, os.fstat(sqlite_fd).st_ino
                ) == (
                    os.fstat(witness_fd).st_dev, os.fstat(witness_fd).st_ino
                ):
                    raise PairedBackupOperatorError("cold images share one source inode")
                if (
                    _sha256_fd(sqlite_fd) != sqlite_sha
                    or _sha256_fd(witness_fd) != witness_sha
                ):
                    raise PairedBackupOperatorError(
                        "cold source bytes disagree with independent image anchors"
                    )
                # Crash fence: persist generation intent BEFORE any output
                # effect. If publication stops at any later phase (including
                # after rename but before its head record), the marker stays
                # in the independent owner root and prohibits a new chain tip.
                inflight = anchor / ".inflight"
                _new_file(inflight, generation.encode("ascii"))
                _fsync_dir(anchor)
                os.mkdir(temporary, 0o700)
                staged_sqlite = temporary / "mark.sqlite"
                staged_witness = temporary / "witness.sqlite"
                if (
                    _copy_pinned(sqlite_fd, staged_sqlite) != sqlite_sha
                    or _copy_pinned(witness_fd, staged_witness) != witness_sha
                    or _sha256_fd(sqlite_fd) != sqlite_sha
                    or _sha256_fd(witness_fd) != witness_sha
                ):
                    raise PairedBackupOperatorError("cold input changed during staging")
                _new_file(temporary / "witness.sqlite.lock", b"")
                observed = inspect_offline_pair(
                    staged_sqlite, staged_witness,
                    expected_sqlite_sha256=sqlite_sha,
                    expected_witness_sha256=witness_sha,
                )
                manifest = _canonical({
                    "schema_version": 5, "generation": generation, **observed,
                })
                _new_file(temporary / "manifest.json", manifest)
                _fsync_dir(temporary)
            # Re-check source path identities and sidecars before publishing.
            _no_sidecars(sql)
            _no_sidecars(witness)
            # A rename within the private output directory publishes one
            # complete image pair. Never mutate either image after publication.
            _unoccupied(published)
            os.rename(temporary, published)
            _fsync_dir(output)
            manifest_sha = _digest(manifest)
            verified = verify_offline_pair(
                published / "mark.sqlite", published / "witness.sqlite",
                published / "manifest.json",
                expected_manifest_sha256=manifest_sha,
            )
            if verified["generation"] != generation:
                raise PairedBackupOperatorError("published pair generation drifted")
            # An earlier HEAD read is not a reservation. Before publishing
            # a new chain tip, check that the independently pinned previous
            # pointer has not been replaced during the expensive pair scan.
            if _read_head(anchor, expected_previous_head_sha256) != prior:
                raise PairedBackupOperatorError(
                    "previous backup HEAD changed before publication"
                )
            if prior is None:
                # Bootstrap has no committed ancestors, but after the
                # durable intent and output rename exactly one new pair must
                # exist. Ignore neither orphan anchors nor foreign outputs.
                # Keeping this separate avoids treating a new generation as
                # a committed predecessor.
                if {
                    entry.name for entry in anchor.iterdir()
                } != {".authority.lock", ".inflight"} or {
                    entry.name for entry in output.iterdir()
                } != {generation}:
                    raise PairedBackupOperatorError(
                        "first generation has unexpected committed artifacts"
                    )
                _private_root(published)
            else:
                # Source image verification can be expensive. Reject changes
                # in any previously anchored generation which occurred since
                # the initial admission check, before publishing a new HEAD.
                _validate_anchored_history(
                    anchor, output, expected_previous_head_sha256, prior,
                    pending_generation=generation,
                )
            # A valid SHA of the pair is insufficient if a late local
            # operation created a sidecar, removed the Witness lock, or
            # swapped a pinned image after the first verification.
            _check_generation_dir(output, generation, manifest_sha)
            _no_sidecars(published / "mark.sqlite")
            _no_sidecars(published / "witness.sqlite")
            # Pin all published artifacts while preparing durable HEAD records.
            # ExitStack must close before the latest.json replace, since its
            # context-exit identity check can still reject an inode swap.
            with ExitStack() as pinned_published:
                pinned_sqlite = pinned_published.enter_context(
                    _pinned_file(published / "mark.sqlite")
                )
                pinned_witness = pinned_published.enter_context(
                    _pinned_file(published / "witness.sqlite")
                )
                pinned_manifest = pinned_published.enter_context(
                    _pinned_file(published / "manifest.json")
                )
                pinned_lock = pinned_published.enter_context(
                    _pinned_file(published / "witness.sqlite.lock")
                )
                head = _canonical({
                    "schema_version": 1,
                    "generation": generation,
                    "manifest_sha256": manifest_sha,
                    "predecessor_sha256": expected_previous_head_sha256 or _ZERO,
                })
                # Persisted create-only records remain for manual investigation
                # if validation fails; .inflight must remain unresolved.
                _new_file(anchor / (generation + ".json"), head)
                _fsync_dir(anchor)
                _new_file(anchor / (".next-" + generation), head)
                if (
                    _sha256_fd(pinned_sqlite) != sqlite_sha
                    or _sha256_fd(pinned_witness) != witness_sha
                    or os.fstat(pinned_lock).st_size != 0
                    or os.fstat(pinned_manifest).st_size != len(manifest)
                    or os.pread(pinned_manifest, len(manifest), 0) != manifest
                ):
                    raise PairedBackupOperatorError(
                        "published image changed during HEAD preparation"
                    )
                _no_sidecars(published / "mark.sqlite")
                _no_sidecars(published / "witness.sqlite")
            # Do not publish HEAD if the predecessor changed during preparation.
            if _read_head(anchor, expected_previous_head_sha256) != prior:
                raise PairedBackupOperatorError(
                    "previous backup HEAD changed during HEAD preparation"
                )
            # A malicious same-UID actor may still race the last check-to-HEAD
            # interval. Host trust requires independent operator authority.
            os.replace(anchor / (".next-" + generation), anchor / "latest.json")
            _fsync_dir(anchor)
            # Only a durably committed head releases this serial operator
            # intent. Errors never clear the marker: recovery is manual.
            os.unlink(inflight)
            _fsync_dir(anchor)
            return {
                "generation": generation,
                "manifest_sha256": manifest_sha,
                "head_sha256": _digest(head),
                "sqlite_sha256": sqlite_sha,
                "witness_sha256": witness_sha,
                "offline_only": True,
                "live_source_quiescence_attested": False,
                "restore_authorized": False,
                "external_write_authorized": False,
                "fences": verified,
            }
    except PairedBackupOperatorError:
        raise
    except (
        OSError, PairPreflightError, FenceWitnessError, ValueError,
        TypeError, OverflowError, RecursionError,
    ) as exc:
        raise PairedBackupOperatorError(
            "cold pair was not safely sealed; inspect artifacts before retry"
        ) from exc
