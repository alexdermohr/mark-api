"""Operation-time binding of protected completion evidence, not host attestation.

A correct target and payload are insufficient when a supposedly fresh readback
actually predates this operation. These tests use no sockets or platform calls.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from mark_api.completion_authority import (
    CompletionAuthorityError, CompletionIntent, decode_evidence, encode_evidence,
)
from mark_api.domain import (
    AdSnapshot, CreateOperationReceipt, LifecycleState, OperationOutcome,
    OperationReceipt,
)
from mark_api.write_api import _request_fingerprint


START = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
END = START + timedelta(seconds=5)
PRINCIPAL = "synthetic-completion-chronology"


def _intent(action: str) -> CompletionIntent:
    target = None if action == "create" else "701"
    payload = ({"category_path": ["Haus & Garten", "Möbel & Wohnen"],
                "title": "Synthetic table", "description": "Synthetic description", "price_eur": 25}
               if action == "create" else {"title": "Updated table"}
               if action == "update_content" else {})
    path = "/api/write/ads" if target is None else "/api/write/ads/" + target
    if action in {"pause", "activate"}:
        path += "/" + action
    method = "PATCH" if action == "update_content" else "DELETE" if action == "delete" else "POST"
    return CompletionIntent.from_wire({
        "version": 1, "op": "execute", "key": "chronology:" + action,
        "sha": _request_fingerprint(principal=PRINCIPAL, method=method, path=path, payload=payload),
        "generation": "write-api-" + "c" * 32, "principal": PRINCIPAL,
        "action": action, "target": target, "payload": payload,
    }, expected_principal=PRINCIPAL)


def _snapshot(*, second: int, state=LifecycleState.ACTIVE,
              title="Synthetic table", description="Synthetic description") -> AdSnapshot:
    return AdSnapshot(ad_id="701", observed_at=START + timedelta(seconds=second),
                      source="synthetic-independent-owner", lifecycle_state=state,
                      title=title, description=description)


def _evidence(action: str):
    intent = _intent(action)
    common = dict(started_at=START, completed_at=END, outcome=OperationOutcome.CONFIRMED,
                  writer_invoked=True, authorization_by=PRINCIPAL,
                  authorization_reference="write-api:" + intent.key,
                  pre_read_status="success_nonempty", post_read_status="success_nonempty")
    if action == "create":
        receipt = CreateOperationReceipt(
            operation="create", created_ad_id="701", **common,
            confirmation_pre_read_status="success_nonempty",
            confirmation_post_read_status="success_nonempty", content_post_read_status="success_nonempty",
            post_snapshot=_snapshot(second=2), confirmation_post_snapshot=_snapshot(second=3),
            content_post_snapshot=_snapshot(second=4),
        )
    else:
        before = LifecycleState.PAUSED if action == "activate" else LifecycleState.ACTIVE
        after = LifecycleState.PAUSED if action == "pause" else LifecycleState.ACTIVE
        receipt = OperationReceipt(
            operation={"pause": "set_state:paused", "activate": "set_state:active"}.get(action, action),
            ad_id="701", **common,
            pre_snapshot=_snapshot(second=1, state=before),
            post_snapshot=None if action == "delete" else _snapshot(
                second=2, state=after,
                title="Updated table" if action == "update_content" else "Synthetic table",
            ),
        )
    return intent, encode_evidence(intent, receipt), receipt


class CompletionEvidenceChronologyTests(unittest.TestCase):
    def test_fresh_confirmed_evidence_roundtrips_for_every_nonmedia_operation(self):
        for action in ("create", "update_content", "pause", "activate", "delete"):
            with self.subTest(action=action):
                intent, evidence, receipt = _evidence(action)
                self.assertEqual(decode_evidence(evidence, intent), receipt)

    def test_stale_or_future_snapshots_cannot_confirm_this_operation(self):
        for action in ("create", "update_content", "pause", "activate", "delete"):
            intent, evidence, _ = _evidence(action)
            for name, snapshot in evidence["snapshots"].items():
                if snapshot is None:
                    continue
                for at in (START - timedelta(microseconds=1), END + timedelta(microseconds=1)):
                    with self.subTest(action=action, snapshot=name, at=at):
                        changed = deepcopy(evidence)
                        changed["snapshots"][name]["observed_at"] = at.isoformat()
                        with self.assertRaises(CompletionAuthorityError) as caught:
                            decode_evidence(changed, intent)
                        self.assertEqual(str(caught.exception), "independent completion not verified")

    def test_postread_cannot_precede_the_checked_precondition(self):
        for action in ("update_content", "pause", "activate"):
            with self.subTest(action=action):
                intent, evidence, _ = _evidence(action)
                evidence["snapshots"]["pre_snapshot"]["observed_at"] = (START + timedelta(seconds=3)).isoformat()
                with self.assertRaises(CompletionAuthorityError):
                    decode_evidence(evidence, intent)

    def test_timezone_offsets_compare_instants_not_clock_strings(self):
        intent, evidence, receipt = _evidence("pause")
        offset = timezone(timedelta(hours=2))
        for snapshot in evidence["snapshots"].values():
            at = datetime.fromisoformat(snapshot["observed_at"])
            snapshot["observed_at"] = at.astimezone(offset).isoformat()
        self.assertEqual(decode_evidence(evidence, intent), receipt)

    def test_clock_resolution_allows_equal_pre_and_post_instants(self):
        intent, evidence, _ = _evidence("pause")
        for snapshot in evidence["snapshots"].values():
            snapshot["observed_at"] = START.isoformat()
        evidence["receipt"]["completed_at"] = START.isoformat()
        self.assertIs(decode_evidence(evidence, intent).outcome, OperationOutcome.CONFIRMED)

    def test_uncertain_result_is_not_promoted_by_any_chronology(self):
        intent, evidence, _ = _evidence("pause")
        evidence["receipt"]["outcome"] = "ambiguous"
        evidence["snapshots"]["post_snapshot"] = None
        evidence["receipt"]["post_read_status"] = None
        result = decode_evidence(evidence, intent)
        self.assertIs(result.outcome, OperationOutcome.AMBIGUOUS)
        self.assertIsNone(result.post_snapshot)


if __name__ == "__main__":
    unittest.main()
