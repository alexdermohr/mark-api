from __future__ import annotations

import unittest
from datetime import datetime, timezone

from mark_api.adapters.monkrel_smoke import (
    MonkrelPrivateHttpContentSmoke,
    SmokeContent,
    SmokeOutcome,
)
from mark_api.domain import AdSnapshot
from mark_api.results import ReadResult, ReadStatus


NOW = datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc)
AD_ID = "fixture-private-http-smoke-ad"
BASELINE = SmokeContent(
    ad_id=AD_ID,
    title="Dekorativer Hirsch mit Geweih",
    description="beflockt, 20cm hoch",
)
TARGET = SmokeContent(
    ad_id=AD_ID,
    title=BASELINE.title,
    description="beflockt, 20cm hoch — private HTTP smoke",
)


def snapshot(content: SmokeContent, *, source: str) -> AdSnapshot:
    return AdSnapshot(
        ad_id=content.ad_id,
        observed_at=NOW,
        source=source,
        title=content.title,
        description=content.description,
    )


class FakeReader:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def read_ad(self, ad_id):
        self.calls.append(ad_id)
        if not self.results:
            raise AssertionError("unexpected read")
        return self.results.pop(0)


class FakeWriter:
    def __init__(self, *, failures=None):
        self.calls = []
        self.failures = dict(failures or {})

    def update_content(self, ad_id, *, title=None, description=None):
        call_number = len(self.calls) + 1
        self.calls.append((ad_id, title, description))
        exc = self.failures.get(call_number)
        if exc is not None:
            raise exc


def success(content: SmokeContent, *, source: str) -> ReadResult[AdSnapshot]:
    return ReadResult.success_nonempty(snapshot(content, source=source))


class MonkrelPrivateHttpContentSmokeTests(unittest.TestCase):
    def test_happy_path_confirms_target_then_rolls_back_once(self):
        owner = FakeReader([success(BASELINE, source="monkrel-mobile-api")])
        independent = FakeReader(
            [
                success(BASELINE, source="kleinanzeigen-management"),
                success(TARGET, source="kleinanzeigen-management"),
                success(BASELINE, source="kleinanzeigen-management"),
            ]
        )
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        result = smoke.run(baseline=BASELINE, target=TARGET)

        self.assertEqual(result.outcome, SmokeOutcome.VERIFIED_AND_ROLLED_BACK)
        self.assertTrue(result.target_confirmed)
        self.assertTrue(result.rollback_confirmed)
        self.assertIsNone(result.write_error)
        self.assertIsNone(result.rollback_error)
        self.assertEqual(
            writer.calls,
            [
                (AD_ID, None, TARGET.description),
                (AD_ID, None, BASELINE.description),
            ],
        )

    def test_write_exception_never_retries_but_readback_can_confirm_effect(self):
        owner = FakeReader([success(BASELINE, source="monkrel-mobile-api")])
        independent = FakeReader(
            [
                success(BASELINE, source="kleinanzeigen-management"),
                success(TARGET, source="kleinanzeigen-management"),
                success(BASELINE, source="kleinanzeigen-management"),
            ]
        )
        writer = FakeWriter(failures={1: TimeoutError("unknown outcome")})
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        result = smoke.run(baseline=BASELINE, target=TARGET)

        self.assertEqual(result.outcome, SmokeOutcome.VERIFIED_AND_ROLLED_BACK)
        self.assertEqual(result.write_error, "TimeoutError")
        self.assertEqual(len(writer.calls), 2)
        self.assertEqual(writer.calls[1], (AD_ID, None, BASELINE.description))

    def test_unconfirmed_write_stops_without_rollback_or_retry(self):
        owner = FakeReader([success(BASELINE, source="monkrel-mobile-api")])
        independent = FakeReader(
            [
                success(BASELINE, source="kleinanzeigen-management"),
                success(BASELINE, source="kleinanzeigen-management"),
            ]
        )
        writer = FakeWriter(failures={1: TimeoutError("unknown outcome")})
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        result = smoke.run(baseline=BASELINE, target=TARGET)

        self.assertEqual(result.outcome, SmokeOutcome.WRITE_UNCONFIRMED)
        self.assertFalse(result.target_confirmed)
        self.assertFalse(result.rollback_confirmed)
        self.assertEqual(result.write_error, "TimeoutError")
        self.assertEqual(
            writer.calls,
            [(AD_ID, None, TARGET.description)],
        )

    def test_failed_independent_readback_stops_without_rollback(self):
        owner = FakeReader([success(BASELINE, source="monkrel-mobile-api")])
        independent = FakeReader(
            [
                success(BASELINE, source="kleinanzeigen-management"),
                ReadResult.failure(
                    ReadStatus.TRANSPORT_ERROR,
                    error="redacted",
                ),
            ]
        )
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        result = smoke.run(baseline=BASELINE, target=TARGET)

        self.assertEqual(result.outcome, SmokeOutcome.WRITE_UNCONFIRMED)
        self.assertEqual(result.readback_error, "RuntimeError")
        self.assertEqual(len(writer.calls), 1)

    def test_rollback_exception_never_retries_when_final_readback_is_wrong(self):
        wrong_final = SmokeContent(
            ad_id=AD_ID,
            title=BASELINE.title,
            description="unexpected final state",
        )
        owner = FakeReader([success(BASELINE, source="monkrel-mobile-api")])
        independent = FakeReader(
            [
                success(BASELINE, source="kleinanzeigen-management"),
                success(TARGET, source="kleinanzeigen-management"),
                success(wrong_final, source="kleinanzeigen-management"),
            ]
        )
        writer = FakeWriter(failures={2: TimeoutError("unknown rollback")})
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        result = smoke.run(baseline=BASELINE, target=TARGET)

        self.assertEqual(result.outcome, SmokeOutcome.ROLLBACK_UNCONFIRMED)
        self.assertTrue(result.target_confirmed)
        self.assertFalse(result.rollback_confirmed)
        self.assertEqual(result.rollback_error, "TimeoutError")
        self.assertEqual(len(writer.calls), 2)

    def test_rollback_exception_can_be_resolved_only_by_independent_readback(self):
        owner = FakeReader([success(BASELINE, source="monkrel-mobile-api")])
        independent = FakeReader(
            [
                success(BASELINE, source="kleinanzeigen-management"),
                success(TARGET, source="kleinanzeigen-management"),
                success(BASELINE, source="kleinanzeigen-management"),
            ]
        )
        writer = FakeWriter(failures={2: TimeoutError("unknown rollback")})
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        result = smoke.run(baseline=BASELINE, target=TARGET)

        self.assertEqual(result.outcome, SmokeOutcome.VERIFIED_AND_ROLLED_BACK)
        self.assertEqual(result.rollback_error, "TimeoutError")
        self.assertEqual(len(writer.calls), 2)

    def test_precondition_requires_exact_baseline_on_both_readers(self):
        other = SmokeContent(
            ad_id=AD_ID,
            title=BASELINE.title,
            description="changed elsewhere",
        )
        owner = FakeReader([success(BASELINE, source="monkrel-mobile-api")])
        independent = FakeReader(
            [success(other, source="kleinanzeigen-management")]
        )
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        with self.assertRaisesRegex(
            ValueError,
            "independent read does not match",
        ):
            smoke.run(baseline=BASELINE, target=TARGET)

        self.assertEqual(writer.calls, [])

    def test_precondition_requires_distinct_read_sources(self):
        owner = FakeReader([success(BASELINE, source="same-source")])
        independent = FakeReader([success(BASELINE, source="same-source")])
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        with self.assertRaisesRegex(ValueError, "distinct source"):
            smoke.run(baseline=BASELINE, target=TARGET)

        self.assertEqual(writer.calls, [])

    def test_target_must_change_exactly_one_field(self):
        both_changed = SmokeContent(
            ad_id=AD_ID,
            title="Neuer Titel",
            description="Neue Beschreibung",
        )
        owner = FakeReader([])
        independent = FakeReader([])
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        with self.assertRaisesRegex(ValueError, "exactly one"):
            smoke.run(baseline=BASELINE, target=both_changed)

        self.assertEqual(owner.calls, [])
        self.assertEqual(independent.calls, [])
        self.assertEqual(writer.calls, [])


if __name__ == "__main__":
    unittest.main()