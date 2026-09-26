from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ..domain import AdSnapshot
from ..results import ReadResult


class SmokeAdReader(Protocol):
    def read_ad(self, ad_id: str) -> ReadResult[AdSnapshot]:
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
class _ObservedContent:
    content: SmokeContent
    source: str


def _observed_content(
    reader: SmokeAdReader,
    ad_id: str,
) -> _ObservedContent:
    result = reader.read_ad(ad_id)
    if not result.is_success:
        raise RuntimeError(f"read failed with status {result.status.value}")
    snapshot = result.value
    if snapshot is None:
        raise ValueError("read did not return the requested ad")
    if snapshot.ad_id != ad_id:
        raise ValueError("read returned a different ad id")
    if not isinstance(snapshot.title, str) or not snapshot.title.strip():
        raise ValueError("read returned a blank or missing title")
    if (
        not isinstance(snapshot.description, str)
        or not snapshot.description.strip()
    ):
        raise ValueError("read returned a blank or missing description")
    return _ObservedContent(
        content=SmokeContent(
            ad_id=snapshot.ad_id,
            title=snapshot.title,
            description=snapshot.description,
        ),
        source=snapshot.source,
    )


def _readback(
    reader: SmokeAdReader,
    ad_id: str,
) -> tuple[_ObservedContent | None, str | None]:
    try:
        return _observed_content(reader, ad_id), None
    except Exception as exc:  # noqa: BLE001 - external read boundary.
        return None, type(exc).__name__


def _changed_field(
    baseline: SmokeContent,
    target: SmokeContent,
) -> str:
    if baseline.ad_id != target.ad_id:
        raise ValueError("baseline and target must bind the same ad id")
    changed: list[str] = []
    if baseline.title != target.title:
        changed.append("title")
    if baseline.description != target.description:
        changed.append("description")
    if len(changed) != 1:
        raise ValueError("smoke target must change exactly one content field")
    return changed[0]


class MonkrelPrivateHttpContentSmoke:
    """Run one reversible, independently verified content-write smoke.

    The harness deliberately performs no retries. It requires two read channels
    with distinct source labels to agree on the exact baseline before the first
    write. A rollback is attempted only after the independent reader confirms
    the target content on the same ad id.

    A transport exception from either write never authorizes a replay. The
    independent readback may establish that the effect happened despite that
    exception; otherwise the run stops fail-closed.
    """

    def __init__(
        self,
        *,
        owner_reader: SmokeAdReader,
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
    ) -> SmokeResult:
        changed_field = _changed_field(baseline, target)

        owner_before = _observed_content(self._owner_reader, baseline.ad_id)
        independent_before = _observed_content(
            self._independent_reader,
            baseline.ad_id,
        )
        if owner_before.source == independent_before.source:
            raise ValueError("independent reader must use a distinct source")
        if owner_before.content != baseline:
            raise ValueError("owner read does not match the bound baseline")
        if independent_before.content != baseline:
            raise ValueError("independent read does not match the bound baseline")

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

        target_after, target_read_error = _readback(
            self._independent_reader,
            baseline.ad_id,
        )
        target_confirmed = bool(
            target_after is not None
            and target_after.source == independent_before.source
            and target_after.content == target
        )
        if not target_confirmed:
            return SmokeResult(
                outcome=SmokeOutcome.WRITE_UNCONFIRMED,
                ad_id=baseline.ad_id,
                target_confirmed=False,
                rollback_confirmed=False,
                write_error=write_error,
                readback_error=target_read_error,
            )

        rollback_kwargs = {
            "title": baseline.title if changed_field == "title" else None,
            "description": (
                baseline.description if changed_field == "description" else None
            ),
        }
        rollback_error: str | None = None
        try:
            self._writer.update_content(
                baseline.ad_id,
                **rollback_kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - rollback outcome can be ambiguous.
            rollback_error = type(exc).__name__

        final_after, final_read_error = _readback(
            self._independent_reader,
            baseline.ad_id,
        )
        rollback_confirmed = bool(
            final_after is not None
            and final_after.source == independent_before.source
            and final_after.content == baseline
        )
        if not rollback_confirmed:
            return SmokeResult(
                outcome=SmokeOutcome.ROLLBACK_UNCONFIRMED,
                ad_id=baseline.ad_id,
                target_confirmed=True,
                rollback_confirmed=False,
                write_error=write_error,
                rollback_error=rollback_error,
                readback_error=final_read_error,
            )

        return SmokeResult(
            outcome=SmokeOutcome.VERIFIED_AND_ROLLED_BACK,
            ad_id=baseline.ad_id,
            target_confirmed=True,
            rollback_confirmed=True,
            write_error=write_error,
            rollback_error=rollback_error,
        )
