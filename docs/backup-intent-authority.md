# Independent offline backup intent — implementation candidate

**Status: Not host-deployed, not reviewed, not authorized for production or restore.**
The implementation in src/mark_api/backup_intent_authority.py is for a
separately trusted offline operator, never the Mark HTTP, dashboard or browser.

## Contract

The operator creates immutable, generation-specific, fsynced records in an
operator-owned, separately protected private directory:

- GEN.prepared.json: original generation, previous independently acknowledged
  HEAD and SHA-256 hashes observed by the existing pinned SQLite/Witness
  offline inspector directly from cold source files.
- GEN.committing.json: exact prepared-record SHA-256, persisted **before**
  any seal/HEAD publication effect.
- GEN.acknowledged.json: exact committing-record SHA-256 plus the actual
  independently reverified published HEAD digest.
- GEN.abandoned.json: terminal create-only, fsynced proof bound to the exact
  prepared record, a zero committing-record digest and original predecessor HEAD.
  This is neither an ACK nor a reusable generation; records are never deleted.

Every retained acknowledgement is checked to reconstruct a single linear
history. Gaps, forks, orphaned or noncanonical phases, reused generations and
more than one pending intent fail closed. There is no mutable state pointer
whose replacement could silently erase a consumed intent.

The public seal_prepared method will commit the external intent durably, invoke
the existing offline paired sealer using *journal-bound* source SHA values,
then independently reverify published HEAD, manifest, pair images and chain
before a once-only acknowledgement.

The acknowledge method is strictly for an already persisted committing intent.
It handles a crash after HEAD publication (with or without an existing
.inflight marker) by exact offline pair verification, optional anchored
reconciliation, and durable once-only acknowledgement. An already acknowledged
generation is rejected even if someone reconstructs .inflight using the
correct original generation/predecessor/image digests.

## Lock bootstrap and historical integrity

On first construction, the intent root must be **empty** and privately owned by
the separate operator UID. The one-time bootstrap creates a durable
`.intent.lock` plus `.intent.bootstrap.json`, binding the device and inode
of the lock. Subsequent operations and operator restarts only reopen the
original pinned lock inode. Removal, recreation or replacement of either
bootstrap artifact is an **operator incident**, not a reason to regenerate
an apparently clean lock. Do not remove the lock even after a crash.

The bootstrap is deliberately filesystem-specific. A restored operator state
on another filesystem with different device/inode identities requires an
independently authorized offline migration and full forensic reconciliation.
It must not auto-rebootstrap. Incomplete initial bootstrap must be handled
manually, preserving all records and fences.

Before a successor seal and again before an external acknowledgement, **all**
retained acknowledged SQLite/Witness image pairs are checked against their
immutable chained manifests. A damaged earlier generation denies the next
seal (preserving the committing intent and old HEAD) or a delayed ACK.
A clean current generation never makes a corrupted ancestor safe.

The checks here remain finite same-UID observations, **not** revocation of
writable mappings, historical descriptors or permission to deploy. An
independent operator-owned mount, process and UID security boundary is still
mandatory. The lock-inode tests and history tests are synthetic only.

## Mandatory host trust boundary

Same-UID 0700/0600 permissions alone are not sufficient. Before operational
use the host MUST independently demonstrate all of:

1. A distinct protected operator UID; neither Mark browser, dashboard nor
   storage-access principals may modify operator intent records, cold image
   files, backup generations, HEAD anchors, or any related mount.
2. Source quiescence and revocation of all writable SQLite main/WAL/SHM
   file descriptors, MAP_SHARED mappings, stale inodes and namespace aliases,
   including separately protected Witness state.
3. Operator-only invocation. No untrusted Mark RPC parameter may invent an
   external source digest, operator intent, acknowledgement or clearance.
4. Actually durable fsync/rename/order on the installed storage and explicit
   power-failure/restart recovery, not solely synthetic Python test results.
5. Full preservation of unresolved SQLite and Witness write/Create/media
   recovery fences, and an installed owner/Witness start-stop-restore runbook.

This library does not create host users or services. No such host installation
or platform API authorization is established by these files.

## Example — trusted operator only

~~~python
from pathlib import Path
from mark_api.backup_intent_authority import BackupIntentAuthority

op = BackupIntentAuthority(Path("/protected/operator-intents"))
op.prepare(
    "11111111111111111111111111111111",
    Path("/protected/cold/mark.sqlite"),
    Path("/protected/cold/witness.sqlite"),
    previous_head_sha256=None,
)
result = op.seal_prepared(
    "11111111111111111111111111111111",
    Path("/protected/backup-generations"),
    Path("/protected/head-anchors"),
)
~~~

A later generation must use the independently acknowledged HEAD as previous
identity. After **any uncertain seal result, do not call seal_prepared again**.
Inspect immutable operator state and the protected published HEAD, and only
if there is a durable unconsumed committing record, call:

~~~python
op.acknowledge(
    "11111111111111111111111111111111",
    Path("/protected/backup-generations"),
    Path("/protected/head-anchors"),
)
~~~

Failure or mismatch preserves the pending intent and recovery fences.
Never delete .inflight to force a retry.

## Prepared-only cancellation; committing intents stay fenced

A generation in **prepared** state has not been authorized to start a seal.
If the independent operator confirms that no committing record was persisted,
the private `abandon(generation, output, anchor)` method may irreversibly retire
that prepared-only generation. It retains a create-only, fsynced
`GEN.abandoned.json` bound to the prepared record and its independently
acknowledged predecessor; `committing_sha256` is the all-zero sentinel.
The identifier can never be used again. A different new generation may
then be prepared against the same acknowledged predecessor.

This limited operation checks absence of generation output/HEAD records,
`.inflight`, `.next-*` and `.partial-*`, validates the acknowledged chain,
fsyncs both output and anchor roots, and repeats the effect check before
recording an abandonment. Negative cases include after a *previously
acknowledged* generation and adversarial changes during directory fsync.

**A durable committing intent cannot be abandoned by this library, even if
there is no visible output.** An external `seal_offline_pair()` may already
hold its anchor lock and be paused before its first file effect. A finite
filesystem scan and the private operator directory `flock` cannot certify
that it has terminated; a concurrent external sealer can otherwise publish
the same generation *after* an abandoned record. Any `committing` intent
therefore remains pending, with all crash/recovery fences preserved. The
module rejects a forged/legacy `GEN.abandoned.json` paired with a committing
record on restart as an unsafe state.

Do not delete `committing`, `.inflight`, generation files, or locks to force
progress. A future recovery mechanism must have an independently enforced
operator/host quiescence and a separate revisions-bound security review
before it could retire such an intent. A caller-supplied hash, statement,
PID listing, filesystem permission or passed Python test is **not** proof
of FD/mmap/namespace revocation. No such release mechanism is provided here.

All cooperating `seal_prepared` calls and prepared-only abandons coordinate
through an exclusive directory flock on the protected operator root.
The lock cannot revoke already running or malicious out-of-process sealer
access. The library, without a securely installed host boundary, is not a
production backup/restore release authorization. No Mark platform write is
authorized by an abandoned record.

The lock bootstrap also binds device/inode identity. Inode numbers may be
reused after all descriptors close; absence of a matching inode is
detectable, but recreation with inode reuse is not a cryptographic
guarantee. Lock-state migration and hostile-UID assurance remain
explicit deployment gates.

## Adversarial matrix

| Failure point | Required result |
| --- | --- |
| Before committing | Prepared only, new generations blocked |
| Prepared-only, verified effect-free | One-shot abandonment may retire the unused identifier; no committing record |
| Committing before HEAD | No blind retry or local abandon; retain pending intent and all fences |
| Abandoned generation replay | Original identity permanently consumed; only a fresh generation may start |
| Inflight symlink or unsynced anchor | No ACK; retained committing intent requires investigation |
| HEAD published, .inflight remains | Only the unconsumed external intent permits verified reconciliation |
| HEAD published, .inflight cleared, ack missing | Reverify actual pair and record one ack |
| Recreated .inflight after an ack | Reject duplicate acknowledgement |
| Source or published image modified | No external ack |
| Journal history fork, record corruption or arbitrary artifact | Fail closed |

**Limit:** finite SHA/stat rechecks cannot guarantee stability against a live
compromised same-UID writer or preexisting writable mappings. Issue #76 and
PR #82 P1 remain OPEN pending actual UID/mount/FD isolation and crash/restore
acceptance. The synthetic tests do not close those gates. No Kleinanzeigen
writes are authorized by this module.