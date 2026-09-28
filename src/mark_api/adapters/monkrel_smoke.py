from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from ..domain import AdSnapshot, LifecycleState
from ..results import ReadResult
from .monkrel_invariant import OwnerAdInvariant


ContentField = Literal["title", "description"]


class SmokeAdReader(Protocol):
    def read_ad(self, ad_id: str) -> ReadResult[AdSnapshot]:
        ...


class SmokeOwnerReader(Protocol):
    def read_invariant(self, ad_id: str) -> OwnerAdInvariant:
        ...


class SmokeContentWriter(Protocol):
    def update_content(
        self,
        ad_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        ...


@dataclass(frozen=True, slots=True)
class SmokeContent:
    ad_id: str
    title: str
    description: str

    def __post_init__(self) -> None:
        if not isinstance(self.ad_id, str) or not self.ad_id.strip():
            raise ValueError("ad_id must be a non-blank string")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("title must be a non-blank string")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("description must be a non-blank string")


class SmokeOutcome(StrEnum):
    VERIFIED_AND_ROLLED_BACK = "verified_and_rolled_back"
    WRITE_UNCONFIRMED = "write_unconfirmed"
    TARGET_CONTENT_UNCONFIRMED = "target_content_unconfirmed"
    ROLLBACK_UNCONFIRMED = "rollback_unconfirmed"


@dataclass(frozen=True, slots=True)
class SmokeResult:
    outcome: SmokeOutcome
    ad_id: str
    target_confirmed: bool
    rollback_confirmed: bool
    write_error: str | None = None
    rollback_error: str | None = None
    readback_error: str | None = None


@dataclass(frozen=True, slots=True)
class _ObservedField:
    field: ContentField
    value: str
    source: str
    lifecycle_state: LifecycleState


def _snapshot(
    reader: SmokeAdReader,
    ad_id: str,
) -> AdSnapshot:
    result = reader.read_ad(ad_id)
    if not result.is_success:
        raise RuntimeError(f"read failed with status {result.status.value}")
    snapshot = result.value
    if snapshot is None:
        raise ValueError("read did not return the requested ad")
    if snapshot.ad_id != ad_id:
        raise ValueError("read returned a different ad id")
    return snapshot


def _content_field(
    snapshot: AdSnapshot,
    field: ContentField,
) -> str:
    value = getattr(snapshot, field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"read returned a blank or missing {field}")
    return value


def _observed_field(
    reader: SmokeAdReader,
    ad_id: str,
    field: ContentField,
) -> _ObservedField:
    snapshot = _snapshot(reader, ad_id)
    return _ObservedField(
        field=field,
        value=_content_field(snapshot, field),
        source=snapshot.source,
        lifecycle_state=snapshot.lifecycle_state,
    )


def _owner_readback(
    reader: SmokeOwnerReader,
    ad_id: str,
) -> tuple[OwnerAdInvariant | None, str | None]:
    try:
        observed = reader.read_invariant(ad_id)
    except Exception as exc:  # noqa: BLE001 - external read boundary.
        return None, type(exc).__name__
    if observed.ad_id != ad_id:
        return None, "owner_ad_id_mismatch"
    return observed, None


def _field_readback(
    reader: SmokeAdReader,
    ad_id: str,
    field: ContentField,
) -> tuple[_ObservedField | None, str | None]:
    try:
        return _observed_field(reader, ad_id, field), None
    except Exception as exc:  # noqa: BLE001 - external read boundary.
        return None, type(exc).__name__


def _changed_field(
    baseline: SmokeContent,
    target: SmokeContent,
) -> ContentField:
    if baseline.ad_id != target.ad_id:
        raise ValueError("baseline and target must bind the same ad id")
    changed: list[ContentField] = []
    if baseline.title != target.title:
        changed.append("title")
    if baseline.description != target.description:
        changed.append("description")
    if len(changed) != 1:
        raise ValueError("smoke target must change exactly one content field")
    return changed[0]


def _bind_full_baseline(
    baseline: SmokeContent,
    baseline_invariant: OwnerAdInvariant,
) -> None:
    if baseline_invariant.ad_id != baseline.ad_id:
        raise ValueError("full baseline binds a different ad id")
    if baseline_invariant.title != baseline.title:
        raise ValueError("full baseline title does not match bound content")
    if baseline_invariant.description != baseline.description:
        raise ValueError("full baseline description does not match bound content")


class MonkrelPrivateHttpContentSmoke:
    """Run one reversible content smoke with full owner-state invariants.

    The caller supplies a previously observed full owner baseline. The harness
    immediately re-reads that state and requires exact equality before any
    write. This turns price, category, pictures, attributes, shipping options,
    buy-now state and the other reconstructed fields into explicit preconditions
    rather than merely post-hoc diagnostics.

    The changed field must then be observed through a distinct independent
    source before one recovery write is authorized. That independent source
    must expose a concrete lifecycle state; the current Monkrel public Listing
    parser drops ad-status, so MonkrelMobileApiAdapter is not suitable for this
    role. ManagementReadAdapter is the supported current-owner lifecycle reader.
    Full owner readback must
    match the target invariant before success is possible. Recovery restores
    both bound content fields exactly once, and completion requires both the
    independent field read and the complete owner invariant to match baseline.

    Neither the initial write nor recovery is retried after an exception.
    """

    def __init__(
        self,
        *,
        owner_reader: SmokeOwnerReader,
        independent_reader: SmokeAdReader,
        writer: SmokeContentWriter,
    ) -> None:
        self._owner_reader = owner_reader
        self._independent_reader = independent_reader
        self._writer = writer

    def run(
        self,
        *,
        baseline: SmokeContent,
        target: SmokeContent,
        baseline_invariant: OwnerAdInvariant,
    ) -> SmokeResult:
        changed_field = _changed_field(baseline, target)
        _bind_full_baseline(baseline, baseline_invariant)

        owner_before = self._owner_reader.read_invariant(baseline.ad_id)
        if owner_before != baseline_invariant:
            raise ValueError("owner read does not match the bound full baseline")

        independent_before = _observed_field(
            self._independent_reader,
            baseline.ad_id,
            changed_field,
        )
        if owner_before.source == independent_before.source:
            raise ValueError("independent reader must use a distinct source")
        if independent_before.lifecycle_state is LifecycleState.UNKNOWN:
            raise ValueError(
                "independent reader must provide a concrete lifecycle state"
            )
        if independent_before.lifecycle_state != baseline_invariant.lifecycle_state:
            raise ValueError(
                "independent lifecycle does not match the bound full baseline"
            )
        if independent_before.value != getattr(baseline, changed_field):
            raise ValueError(
                f"independent read does not match the bound baseline {changed_field}"
            )

        # This is a pre-write gate. In particular, buy-now=true remains blocked
        # until a direct wire-format contract is proven.
        owner_before.require_content_update_safe()

        target_invariant = baseline_invariant.with_content(
            title=target.title,
            description=target.description,
        )
        target_kwargs = {
            "title": target.title if changed_field == "title" else None,
            "description": (
                target.description if changed_field == "description" else None
            ),
        }

        write_error: str | None = None
        try:
            self._writer.update_content(
                baseline.ad_id,
                **target_kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - write outcome can be ambiguous.
            write_error = type(exc).__name__

        target_field, target_field_error = _field_readback(
            self._independent_reader,
            baseline.ad_id,
            changed_field,
        )
        target_field_confirmed = bool(
            target_field is not None
            and target_field.source == independent_before.source
            and target_field.value == getattr(target, changed_field)
        )
        target_lifecycle_confirmed = bool(
            target_field is not None
            and target_field.lifecycle_state == baseline_invariant.lifecycle_state
        )
        if not target_field_confirmed:
            return SmokeResult(
                outcome=SmokeOutcome.WRITE_UNCONFIRMED,
                ad_id=baseline.ad_id,
                target_confirmed=False,
                rollback_confirmed=False,
                write_error=write_error,
                readback_error=target_field_error,
            )

        owner_target, owner_target_error = _owner_readback(
            self._owner_reader,
            baseline.ad_id,
        )
        owner_target_confirmed = owner_target == target_invariant
        target_content_confirmed = (
            owner_target_confirmed and target_lifecycle_confirmed
        )
        target_content_error = owner_target_error
        if owner_target is not None and not owner_target_confirmed:
            target_content_error = "owner_target_invariant_mismatch"
        elif owner_target is not None and not target_lifecycle_confirmed:
            target_content_error = "independent_target_lifecycle_mismatch"

        rollback_error: str | None = None
        try:
            self._writer.update_content(
                baseline.ad_id,
                title=baseline.title,
                description=baseline.description,
            )
        except Exception as exc:  # noqa: BLE001 - rollback outcome can be ambiguous.
            rollback_error = type(exc).__name__

        final_field, final_field_error = _field_readback(
            self._independent_reader,
            baseline.ad_id,
            changed_field,
        )
        owner_final, owner_final_error = _owner_readback(
            self._owner_reader,
            baseline.ad_id,
        )
        final_field_confirmed = bool(
            final_field is not None
            and final_field.source == independent_before.source
            and final_field.value == getattr(baseline, changed_field)
            and final_field.lifecycle_state == baseline_invariant.lifecycle_state
        )
        final_invariant_confirmed = owner_final == baseline_invariant
        rollback_confirmed = final_field_confirmed and final_invariant_confirmed

        if not rollback_confirmed:
            final_error = final_field_error or owner_final_error
            if final_error is None:
                final_error = "rollback_invariant_mismatch"
            return SmokeResult(
                outcome=SmokeOutcome.ROLLBACK_UNCONFIRMED,
                ad_id=baseline.ad_id,
                target_confirmed=target_content_confirmed,
                rollback_confirmed=False,
                write_error=write_error,
                rollback_error=rollback_error,
                readback_error=final_error,
            )

        if not target_content_confirmed:
            return SmokeResult(
                outcome=SmokeOutcome.TARGET_CONTENT_UNCONFIRMED,
                ad_id=baseline.ad_id,
                target_confirmed=False,
                rollback_confirmed=True,
                write_error=write_error,
                rollback_error=rollback_error,
                readback_error=target_content_error,
            )

        return SmokeResult(
            outcome=SmokeOutcome.VERIFIED_AND_ROLLED_BACK,
            ad_id=baseline.ad_id,
            target_confirmed=True,
            rollback_confirmed=True,
            write_error=write_error,
            rollback_error=rollback_error,
        )
