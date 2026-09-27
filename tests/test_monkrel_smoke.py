from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone

from mark_api.adapters.monkrel_invariant import OwnerAdInvariant
from mark_api.adapters.monkrel_smoke import (
    MonkrelPrivateHttpContentSmoke,
    SmokeContent,
    SmokeOutcome,
)
from mark_api.domain import AdSnapshot, LifecycleState
from mark_api.results import ReadResult, ReadStatus


NOW = datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc)
AD_ID = "fixture-private-http-smoke-ad"
BASELINE = SmokeContent(
    ad_id=AD_ID,
    title="Dekorativer Hirsch mit Geweih",
    description="beflockt, 20cm hoch",
)
TITLE_TARGET = SmokeContent(
    ad_id=AD_ID,
    title="Dekorativer Hirsch mit Geweih — HTTP smoke",
    description=BASELINE.description,
)
DESCRIPTION_TARGET = SmokeContent(
    ad_id=AD_ID,
    title=BASELINE.title,
    description="beflockt, 20cm hoch — HTTP smoke",
)
BASELINE_INVARIANT = OwnerAdInvariant(
    source="monkrel-private-owner-http",
    ad_id=AD_ID,
    lifecycle_state=LifecycleState.ACTIVE,
    title=BASELINE.title,
    description=BASELINE.description,
    category_id="246",
    location_id="3455",
    price_type="SPECIFIED_AMOUNT",
    price_amount="25",
    poster_type="PRIVATE",
    ad_type="OFFERED",
    contact_name="Mark",
    email="mark@example.invalid",
    phone=None,
    latitude="53.55",
    longitude="9.99",
    attributes=(("condition", ("USED",)),),
    pictures=(
        (
            ("XXL", "https://img/xxl.jpg"),
            ("teaser", "https://img/teaser.jpg"),
        ),
    ),
    shipping_option_ids=("DHL_001", "HERMES_001", "HERMES_002"),
    buy_now_selected=False,
    shipping_metadata_empty=True,
    medias_empty=True,
    product_safety_empty=True,
    show_full_address=False,
    imprint=None,
)
TITLE_TARGET_INVARIANT = BASELINE_INVARIANT.with_content(
    title=TITLE_TARGET.title,
    description=TITLE_TARGET.description,
)
DESCRIPTION_TARGET_INVARIANT = BASELINE_INVARIANT.with_content(
    title=DESCRIPTION_TARGET.title,
    description=DESCRIPTION_TARGET.description,
)


def snapshot(
    content: SmokeContent,
    *,
    source: str,
    include_description: bool = True,
    lifecycle_state: LifecycleState = LifecycleState.ACTIVE,
) -> AdSnapshot:
    return AdSnapshot(
        ad_id=content.ad_id,
        observed_at=NOW,
        source=source,
        lifecycle_state=lifecycle_state,
        title=content.title,
        description=content.description if include_description else None,
    )


class FakeIndependentReader:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def read_ad(self, ad_id):
        self.calls.append(ad_id)
        if not self.results:
            raise AssertionError("unexpected independent read")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeOwnerReader:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def read_invariant(self, ad_id):
        self.calls.append(ad_id)
        if not self.results:
            raise AssertionError("unexpected owner read")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


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


def success(
    content: SmokeContent,
    *,
    source: str,
    include_description: bool = True,
    lifecycle_state: LifecycleState = LifecycleState.ACTIVE,
) -> ReadResult[AdSnapshot]:
    return ReadResult.success_nonempty(
        snapshot(
            content,
            source=source,
            include_description=include_description,
            lifecycle_state=lifecycle_state,
        )
    )


def management_success(
    content: SmokeContent,
    *,
    lifecycle_state: LifecycleState = LifecycleState.ACTIVE,
) -> ReadResult[AdSnapshot]:
    return success(
        content,
        source="kleinanzeigen-management",
        include_description=False,
        lifecycle_state=lifecycle_state,
    )


def independent_content_success(content: SmokeContent) -> ReadResult[AdSnapshot]:
    return success(content, source="independent-content-reader")


class MonkrelPrivateHttpContentSmokeTests(unittest.TestCase):
    def test_prewrite_rejects_independent_lifecycle_mismatch(self):
        owner = FakeOwnerReader([BASELINE_INVARIANT])
        independent = FakeIndependentReader(
            [management_success(BASELINE, lifecycle_state=LifecycleState.PAUSED)]
        )
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        with self.assertRaisesRegex(ValueError, "independent lifecycle"):
            smoke.run(
                baseline=BASELINE,
                target=TITLE_TARGET,
                baseline_invariant=BASELINE_INVARIANT,
            )

        self.assertEqual(writer.calls, [])

    def test_target_lifecycle_drift_is_not_success_and_still_rolls_back(self):
        owner = FakeOwnerReader(
            [BASELINE_INVARIANT, TITLE_TARGET_INVARIANT, BASELINE_INVARIANT]
        )
        independent = FakeIndependentReader(
            [
                management_success(BASELINE),
                management_success(
                    TITLE_TARGET,
                    lifecycle_state=LifecycleState.PAUSED,
                ),
                management_success(BASELINE),
            ]
        )
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        result = smoke.run(
            baseline=BASELINE,
            target=TITLE_TARGET,
            baseline_invariant=BASELINE_INVARIANT,
        )

        self.assertEqual(result.outcome, SmokeOutcome.TARGET_CONTENT_UNCONFIRMED)
        self.assertFalse(result.target_confirmed)
        self.assertTrue(result.rollback_confirmed)
        self.assertEqual(
            result.readback_error,
            "independent_target_lifecycle_mismatch",
        )
        self.assertEqual(len(writer.calls), 2)

    def test_final_lifecycle_drift_blocks_rollback_confirmation(self):
        owner = FakeOwnerReader(
            [BASELINE_INVARIANT, TITLE_TARGET_INVARIANT, BASELINE_INVARIANT]
        )
        independent = FakeIndependentReader(
            [
                management_success(BASELINE),
                management_success(TITLE_TARGET),
                management_success(
                    BASELINE,
                    lifecycle_state=LifecycleState.PAUSED,
                ),
            ]
        )
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        result = smoke.run(
            baseline=BASELINE,
            target=TITLE_TARGET,
            baseline_invariant=BASELINE_INVARIANT,
        )

        self.assertEqual(result.outcome, SmokeOutcome.ROLLBACK_UNCONFIRMED)
        self.assertTrue(result.target_confirmed)
        self.assertFalse(result.rollback_confirmed)
        self.assertEqual(len(writer.calls), 2)

    def test_title_smoke_requires_full_owner_invariant_and_independent_field(self):
        owner = FakeOwnerReader(
            [BASELINE_INVARIANT, TITLE_TARGET_INVARIANT, BASELINE_INVARIANT]
        )
        independent = FakeIndependentReader(
            [
                management_success(BASELINE),
                management_success(TITLE_TARGET),
                management_success(BASELINE),
            ]
        )
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        result = smoke.run(
            baseline=BASELINE,
            target=TITLE_TARGET,
            baseline_invariant=BASELINE_INVARIANT,
        )

        self.assertEqual(result.outcome, SmokeOutcome.VERIFIED_AND_ROLLED_BACK)
        self.assertTrue(result.target_confirmed)
        self.assertTrue(result.rollback_confirmed)
        self.assertIsNone(result.write_error)
        self.assertIsNone(result.rollback_error)
        self.assertEqual(
            writer.calls,
            [
                (AD_ID, TITLE_TARGET.title, None),
                (AD_ID, BASELINE.title, BASELINE.description),
            ],
        )

    def test_write_exception_never_retries_but_readback_can_confirm_effect(self):
        owner = FakeOwnerReader(
            [BASELINE_INVARIANT, TITLE_TARGET_INVARIANT, BASELINE_INVARIANT]
        )
        independent = FakeIndependentReader(
            [
                management_success(BASELINE),
                management_success(TITLE_TARGET),
                management_success(BASELINE),
            ]
        )
        writer = FakeWriter(failures={1: TimeoutError("unknown outcome")})
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        result = smoke.run(
            baseline=BASELINE,
            target=TITLE_TARGET,
            baseline_invariant=BASELINE_INVARIANT,
        )

        self.assertEqual(result.outcome, SmokeOutcome.VERIFIED_AND_ROLLED_BACK)
        self.assertEqual(result.write_error, "TimeoutError")
        self.assertEqual(len(writer.calls), 2)

    def test_unconfirmed_changed_field_stops_without_rollback_or_retry(self):
        owner = FakeOwnerReader([BASELINE_INVARIANT])
        independent = FakeIndependentReader(
            [management_success(BASELINE), management_success(BASELINE)]
        )
        writer = FakeWriter(failures={1: TimeoutError("unknown outcome")})
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        result = smoke.run(
            baseline=BASELINE,
            target=TITLE_TARGET,
            baseline_invariant=BASELINE_INVARIANT,
        )

        self.assertEqual(result.outcome, SmokeOutcome.WRITE_UNCONFIRMED)
        self.assertFalse(result.target_confirmed)
        self.assertFalse(result.rollback_confirmed)
        self.assertEqual(result.write_error, "TimeoutError")
        self.assertEqual(writer.calls, [(AD_ID, TITLE_TARGET.title, None)])
        self.assertEqual(len(owner.calls), 1)

    def test_failed_independent_target_readback_stops_without_rollback(self):
        owner = FakeOwnerReader([BASELINE_INVARIANT])
        independent = FakeIndependentReader(
            [
                management_success(BASELINE),
                ReadResult.failure(ReadStatus.TRANSPORT_ERROR, error="redacted"),
            ]
        )
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        result = smoke.run(
            baseline=BASELINE,
            target=TITLE_TARGET,
            baseline_invariant=BASELINE_INVARIANT,
        )

        self.assertEqual(result.outcome, SmokeOutcome.WRITE_UNCONFIRMED)
        self.assertEqual(result.readback_error, "RuntimeError")
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(len(owner.calls), 1)

    def test_any_target_invariant_corruption_is_recovered_but_never_success(self):
        corruptions = {
            "price": {"price_amount": "26"},
            "category": {"category_id": "999"},
            "shipping": {"shipping_option_ids": ("DHL_001",)},
            "buy_now": {"buy_now_selected": True},
            "attributes": {"attributes": (("condition", ("NEW",)),)},
            "pictures": {
                "pictures": (
                    (("XXL", "https://img/changed.jpg"),),
                )
            },
        }
        for label, change in corruptions.items():
            with self.subTest(label=label):
                corrupted = replace(TITLE_TARGET_INVARIANT, **change)
                owner = FakeOwnerReader(
                    [BASELINE_INVARIANT, corrupted, BASELINE_INVARIANT]
                )
                independent = FakeIndependentReader(
                    [
                        management_success(BASELINE),
                        management_success(TITLE_TARGET),
                        management_success(BASELINE),
                    ]
                )
                writer = FakeWriter()
                smoke = MonkrelPrivateHttpContentSmoke(
                    owner_reader=owner,
                    independent_reader=independent,
                    writer=writer,
                )

                result = smoke.run(
                    baseline=BASELINE,
                    target=TITLE_TARGET,
                    baseline_invariant=BASELINE_INVARIANT,
                )

                self.assertEqual(
                    result.outcome,
                    SmokeOutcome.TARGET_CONTENT_UNCONFIRMED,
                )
                self.assertFalse(result.target_confirmed)
                self.assertTrue(result.rollback_confirmed)
                self.assertEqual(
                    result.readback_error,
                    "owner_target_invariant_mismatch",
                )
                self.assertEqual(len(writer.calls), 2)

    def test_failed_full_target_read_is_recovered_but_not_counted_as_proof(self):
        owner = FakeOwnerReader(
            [
                BASELINE_INVARIANT,
                RuntimeError("owner read failed"),
                BASELINE_INVARIANT,
            ]
        )
        independent = FakeIndependentReader(
            [
                management_success(BASELINE),
                management_success(TITLE_TARGET),
                management_success(BASELINE),
            ]
        )
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        result = smoke.run(
            baseline=BASELINE,
            target=TITLE_TARGET,
            baseline_invariant=BASELINE_INVARIANT,
        )

        self.assertEqual(result.outcome, SmokeOutcome.TARGET_CONTENT_UNCONFIRMED)
        self.assertFalse(result.target_confirmed)
        self.assertTrue(result.rollback_confirmed)
        self.assertEqual(result.readback_error, "RuntimeError")
        self.assertEqual(len(writer.calls), 2)

    def test_rollback_exception_never_retries_when_final_invariant_is_wrong(self):
        wrong_final = replace(BASELINE_INVARIANT, price_amount="99")
        owner = FakeOwnerReader(
            [BASELINE_INVARIANT, TITLE_TARGET_INVARIANT, wrong_final]
        )
        independent = FakeIndependentReader(
            [
                management_success(BASELINE),
                management_success(TITLE_TARGET),
                management_success(BASELINE),
            ]
        )
        writer = FakeWriter(failures={2: TimeoutError("unknown rollback")})
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        result = smoke.run(
            baseline=BASELINE,
            target=TITLE_TARGET,
            baseline_invariant=BASELINE_INVARIANT,
        )

        self.assertEqual(result.outcome, SmokeOutcome.ROLLBACK_UNCONFIRMED)
        self.assertTrue(result.target_confirmed)
        self.assertFalse(result.rollback_confirmed)
        self.assertEqual(result.rollback_error, "TimeoutError")
        self.assertEqual(result.readback_error, "rollback_invariant_mismatch")
        self.assertEqual(len(writer.calls), 2)

    def test_rollback_exception_can_be_resolved_only_by_both_readbacks(self):
        owner = FakeOwnerReader(
            [BASELINE_INVARIANT, TITLE_TARGET_INVARIANT, BASELINE_INVARIANT]
        )
        independent = FakeIndependentReader(
            [
                management_success(BASELINE),
                management_success(TITLE_TARGET),
                management_success(BASELINE),
            ]
        )
        writer = FakeWriter(failures={2: TimeoutError("unknown rollback")})
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        result = smoke.run(
            baseline=BASELINE,
            target=TITLE_TARGET,
            baseline_invariant=BASELINE_INVARIANT,
        )

        self.assertEqual(result.outcome, SmokeOutcome.VERIFIED_AND_ROLLED_BACK)
        self.assertEqual(result.rollback_error, "TimeoutError")
        self.assertEqual(len(writer.calls), 2)

    def test_owner_final_invariant_mismatch_blocks_success(self):
        owner_wrong = replace(BASELINE_INVARIANT, shipping_option_ids=())
        owner = FakeOwnerReader(
            [BASELINE_INVARIANT, TITLE_TARGET_INVARIANT, owner_wrong]
        )
        independent = FakeIndependentReader(
            [
                management_success(BASELINE),
                management_success(TITLE_TARGET),
                management_success(BASELINE),
            ]
        )
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        result = smoke.run(
            baseline=BASELINE,
            target=TITLE_TARGET,
            baseline_invariant=BASELINE_INVARIANT,
        )

        self.assertEqual(result.outcome, SmokeOutcome.ROLLBACK_UNCONFIRMED)
        self.assertFalse(result.rollback_confirmed)
        self.assertEqual(result.readback_error, "rollback_invariant_mismatch")

    def test_precondition_requires_exact_independent_changed_field(self):
        other = SmokeContent(
            ad_id=AD_ID,
            title="changed elsewhere",
            description=BASELINE.description,
        )
        owner = FakeOwnerReader([BASELINE_INVARIANT])
        independent = FakeIndependentReader([management_success(other)])
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        with self.assertRaisesRegex(
            ValueError,
            "independent read does not match the bound baseline title",
        ):
            smoke.run(
                baseline=BASELINE,
                target=TITLE_TARGET,
                baseline_invariant=BASELINE_INVARIANT,
            )

        self.assertEqual(writer.calls, [])

    def test_precondition_requires_exact_full_owner_baseline(self):
        owner_other = replace(BASELINE_INVARIANT, price_amount="30")
        owner = FakeOwnerReader([owner_other])
        independent = FakeIndependentReader([management_success(BASELINE)])
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        with self.assertRaisesRegex(ValueError, "bound full baseline"):
            smoke.run(
                baseline=BASELINE,
                target=TITLE_TARGET,
                baseline_invariant=BASELINE_INVARIANT,
            )

        self.assertEqual(writer.calls, [])
        self.assertEqual(independent.calls, [])

    def test_precondition_requires_distinct_read_sources(self):
        same_source = success(
            BASELINE,
            source=BASELINE_INVARIANT.source,
            include_description=False,
        )
        owner = FakeOwnerReader([BASELINE_INVARIANT])
        independent = FakeIndependentReader([same_source])
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        with self.assertRaisesRegex(ValueError, "distinct source"):
            smoke.run(
                baseline=BASELINE,
                target=TITLE_TARGET,
                baseline_invariant=BASELINE_INVARIANT,
            )

        self.assertEqual(writer.calls, [])

    def test_target_must_change_exactly_one_field(self):
        both_changed = SmokeContent(
            ad_id=AD_ID,
            title="Neuer Titel",
            description="Neue Beschreibung",
        )
        owner = FakeOwnerReader([])
        independent = FakeIndependentReader([])
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        with self.assertRaisesRegex(ValueError, "exactly one"):
            smoke.run(
                baseline=BASELINE,
                target=both_changed,
                baseline_invariant=BASELINE_INVARIANT,
            )

        self.assertEqual(owner.calls, [])
        self.assertEqual(independent.calls, [])
        self.assertEqual(writer.calls, [])

    def test_description_smoke_fails_closed_when_independent_reader_lacks_description(self):
        owner = FakeOwnerReader([BASELINE_INVARIANT])
        independent = FakeIndependentReader([management_success(BASELINE)])
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        with self.assertRaisesRegex(ValueError, "missing description"):
            smoke.run(
                baseline=BASELINE,
                target=DESCRIPTION_TARGET,
                baseline_invariant=BASELINE_INVARIANT,
            )

        self.assertEqual(writer.calls, [])

    def test_description_smoke_works_with_independent_description_surface(self):
        owner = FakeOwnerReader(
            [
                BASELINE_INVARIANT,
                DESCRIPTION_TARGET_INVARIANT,
                BASELINE_INVARIANT,
            ]
        )
        independent = FakeIndependentReader(
            [
                independent_content_success(BASELINE),
                independent_content_success(DESCRIPTION_TARGET),
                independent_content_success(BASELINE),
            ]
        )
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        result = smoke.run(
            baseline=BASELINE,
            target=DESCRIPTION_TARGET,
            baseline_invariant=BASELINE_INVARIANT,
        )

        self.assertEqual(result.outcome, SmokeOutcome.VERIFIED_AND_ROLLED_BACK)
        self.assertEqual(
            writer.calls,
            [
                (AD_ID, None, DESCRIPTION_TARGET.description),
                (AD_ID, BASELINE.title, BASELINE.description),
            ],
        )

    def test_buy_now_true_baseline_blocks_before_writer_call(self):
        unsafe = replace(BASELINE_INVARIANT, buy_now_selected=True)
        owner = FakeOwnerReader([unsafe])
        independent = FakeIndependentReader([management_success(BASELINE)])
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        with self.assertRaisesRegex(ValueError, "buy-now=true"):
            smoke.run(
                baseline=BASELINE,
                target=TITLE_TARGET,
                baseline_invariant=unsafe,
            )

        self.assertEqual(writer.calls, [])
        self.assertEqual(len(owner.calls), 1)
        self.assertEqual(len(independent.calls), 1)

    def test_bound_invariant_content_must_match_smoke_content(self):
        mismatched = replace(BASELINE_INVARIANT, title="other")
        owner = FakeOwnerReader([])
        independent = FakeIndependentReader([])
        writer = FakeWriter()
        smoke = MonkrelPrivateHttpContentSmoke(
            owner_reader=owner,
            independent_reader=independent,
            writer=writer,
        )

        with self.assertRaisesRegex(ValueError, "full baseline title"):
            smoke.run(
                baseline=BASELINE,
                target=TITLE_TARGET,
                baseline_invariant=mismatched,
            )

        self.assertEqual(writer.calls, [])
        self.assertEqual(owner.calls, [])
        self.assertEqual(independent.calls, [])


if __name__ == "__main__":
    unittest.main()
