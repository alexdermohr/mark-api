from __future__ import annotations

import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from mark_api.domain import AdCreateRequest, AdSnapshot, LifecycleState, OperationOutcome
from mark_api.orchestrator import SafeWriteOrchestrator
from mark_api.private_web import (
    PrivateWebContentWriter,
    PrivateWebCreateSnapshot,
    PrivateWebCreateWriter,
    PrivateWebDeleteSnapshot,
    PrivateWebDeleteWriter,
    PrivateWebEditorSnapshot,
    PrivateWebEditorState,
    PrivateWebInteractionError,
    PrivateWebPreconditionError,
    PrivateWebStateSnapshot,
    PrivateWebStateWriter,
    PrivateWebSubmitUnknownError,
    PrivateWebWriteNotAttemptedError,
)
from mark_api.results import ReadResult


AD_ID = "3521676801"
NOW = datetime(2026, 9, 28, 11, 0, tzinfo=timezone.utc)


def editor(
    *,
    state: PrivateWebEditorState = PrivateWebEditorState.READY,
    ad_id: str | None = AD_ID,
    title: str | None = "Old title",
    description: str | None = "Old description",
) -> PrivateWebEditorSnapshot:
    if state is not PrivateWebEditorState.READY:
        ad_id = None
        title = None
        description = None
    return PrivateWebEditorSnapshot(
        state=state,
        ad_id=ad_id,
        title=title,
        description=description,
    )


class FakePage:
    def __init__(self, *snapshots, fail_stage: str | None = None) -> None:
        self.snapshots = list(snapshots)
        self.fail_stage = fail_stage
        self.calls: list[tuple[object, ...]] = []

    def _maybe_fail(self, stage: str) -> None:
        if self.fail_stage == stage:
            raise RuntimeError(
                "provider details and content must not escape"
            )

    def open_editor(self, ad_id: str) -> None:
        self.calls.append(("open_editor", ad_id))
        self._maybe_fail("open_editor")

    def read_editor(self) -> PrivateWebEditorSnapshot:
        self.calls.append(("read_editor",))
        self._maybe_fail("read_editor")
        if not self.snapshots:
            raise AssertionError("unexpected read_editor")
        return self.snapshots.pop(0)

    def replace_title(self, value: str) -> None:
        self.calls.append(("replace_title", value))
        self._maybe_fail("replace_title")

    def replace_description(self, value: str) -> None:
        self.calls.append(("replace_description", value))
        self._maybe_fail("replace_description")

    def submit(self) -> None:
        self.calls.append(("submit",))
        self._maybe_fail("submit")


class FakeDeletePage:
    def __init__(
        self,
        *snapshots: PrivateWebDeleteSnapshot,
        fail_stage: str | None = None,
        submit_unknown: bool = False,
    ) -> None:
        self.snapshots = list(snapshots)
        self.fail_stage = fail_stage
        self.submit_unknown = submit_unknown
        self.calls: list[tuple[object, ...]] = []

    def _maybe_fail(self, stage: str) -> None:
        if self.fail_stage == stage:
            raise RuntimeError("provider details must not escape")

    def open_delete_controls(self, ad_id: str) -> None:
        self.calls.append(("open_delete_controls", ad_id))
        self._maybe_fail("open_delete_controls")

    def read_delete_controls(self) -> PrivateWebDeleteSnapshot:
        self.calls.append(("read_delete_controls",))
        self._maybe_fail("read_delete_controls")
        if not self.snapshots:
            raise AssertionError("unexpected delete read")
        return self.snapshots.pop(0)

    def open_delete_confirmation(self) -> None:
        self.calls.append(("open_delete_confirmation",))
        self._maybe_fail("open_delete_confirmation")

    def read_delete_confirmation(self) -> PrivateWebDeleteSnapshot:
        self.calls.append(("read_delete_confirmation",))
        self._maybe_fail("read_delete_confirmation")
        if not self.snapshots:
            raise AssertionError("unexpected confirmation read")
        return self.snapshots.pop(0)

    def submit_delete(self) -> None:
        self.calls.append(("submit_delete",))
        if self.submit_unknown:
            raise PrivateWebSubmitUnknownError("delete_submit")
        self._maybe_fail("submit_delete")


def delete_snapshot(
    state: PrivateWebEditorState = PrivateWebEditorState.READY,
    *,
    ad_id: str | None = AD_ID,
) -> PrivateWebDeleteSnapshot:
    if state is not PrivateWebEditorState.READY:
        ad_id = None
    return PrivateWebDeleteSnapshot(state=state, ad_id=ad_id)


class SequenceReader:
    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls = 0

    def read_ads(self):
        self.calls += 1
        if not self.results:
            raise AssertionError("unexpected read_ads")
        return self.results.pop(0)


def ad_snapshot(
    *,
    title: str,
    description: str,
) -> AdSnapshot:
    return AdSnapshot(
        ad_id=AD_ID,
        observed_at=NOW,
        source="private-web",
        lifecycle_state=LifecycleState.ACTIVE,
        title=title,
        description=description,
    )


def create_snapshot(
    state: PrivateWebEditorState = PrivateWebEditorState.READY,
    *,
    title: str | None = "",
    description: str | None = "",
    price_amount: str | None = "",
) -> PrivateWebCreateSnapshot:
    if state is not PrivateWebEditorState.READY:
        title = None
        description = None
        price_amount = None
    return PrivateWebCreateSnapshot(
        state=state,
        title=title,
        description=description,
        price_amount=price_amount,
    )


class FakeCreatePage:
    def __init__(
        self,
        *snapshots: PrivateWebCreateSnapshot,
        fail_stage: str | None = None,
        submit_unknown: bool = False,
    ) -> None:
        self.snapshots = list(snapshots)
        self.fail_stage = fail_stage
        self.submit_unknown = submit_unknown
        self.calls: list[tuple[object, ...]] = []

    def _maybe_fail(self, stage: str) -> None:
        if self.fail_stage == stage:
            raise RuntimeError("provider details must not escape")

    def open_create_form(self, category_path: tuple[str, ...]) -> None:
        self.calls.append(("open_create_form", category_path))
        self._maybe_fail("open_create_form")

    def read_create_form(self) -> PrivateWebCreateSnapshot:
        self.calls.append(("read_create_form",))
        self._maybe_fail("read_create_form")
        if not self.snapshots:
            raise AssertionError("unexpected create read")
        return self.snapshots.pop(0)

    def replace_create_title(self, value: str) -> None:
        self.calls.append(("replace_create_title", value))
        self._maybe_fail("replace_create_title")

    def replace_create_description(self, value: str) -> None:
        self.calls.append(("replace_create_description", value))
        self._maybe_fail("replace_create_description")

    def replace_create_price(self, value: str) -> None:
        self.calls.append(("replace_create_price", value))
        self._maybe_fail("replace_create_price")

    def submit_create(self) -> None:
        self.calls.append(("submit_create",))
        if self.submit_unknown:
            raise PrivateWebSubmitUnknownError("create_submit")
        self._maybe_fail("submit_create")


def create_request() -> AdCreateRequest:
    return AdCreateRequest(
        category_path=("Haus & Garten", "Dekoration", "Weitere Dekoration"),
        title="Neue Vase",
        description="Beschreibung der neuen Vase",
        price_eur=12,
    )


class PrivateWebCreateWriterTests(unittest.TestCase):
    def test_exact_create_form_is_filled_and_submitted_once(self) -> None:
        request = create_request()
        page = FakeCreatePage(
            create_snapshot(),
            create_snapshot(
                title=request.title,
                description=request.description,
                price_amount="12",
            ),
        )

        PrivateWebCreateWriter(page).create_ad(request)

        self.assertEqual(
            page.calls,
            [
                ("open_create_form", request.category_path),
                ("read_create_form",),
                ("replace_create_title", request.title),
                ("replace_create_description", request.description),
                ("replace_create_price", "12"),
                ("read_create_form",),
                ("submit_create",),
            ],
        )

    def test_create_challenge_fails_before_form_mutation(self) -> None:
        for state in (
            PrivateWebEditorState.LOGIN_REQUIRED,
            PrivateWebEditorState.MFA_REQUIRED,
            PrivateWebEditorState.CAPTCHA_REQUIRED,
            PrivateWebEditorState.SECURITY_CHALLENGE,
            PrivateWebEditorState.UNKNOWN,
        ):
            with self.subTest(state=state):
                page = FakeCreatePage(create_snapshot(state))

                with self.assertRaises(PrivateWebPreconditionError):
                    PrivateWebCreateWriter(page).create_ad(create_request())

                self.assertEqual(
                    page.calls,
                    [
                        ("open_create_form", create_request().category_path),
                        ("read_create_form",),
                    ],
                )

    def test_create_drift_immediately_before_submit_fails_closed(self) -> None:
        request = create_request()
        page = FakeCreatePage(
            create_snapshot(),
            create_snapshot(
                title=request.title,
                description="unexpected",
                price_amount="12",
            ),
        )

        with self.assertRaisesRegex(
            PrivateWebPreconditionError,
            "create_values_drift",
        ):
            PrivateWebCreateWriter(page).create_ad(request)

        self.assertNotIn(("submit_create",), page.calls)

    def test_create_pre_submit_provider_error_is_write_not_attempted(self) -> None:
        page = FakeCreatePage(
            create_snapshot(),
            fail_stage="replace_create_title",
        )

        with self.assertRaises(PrivateWebWriteNotAttemptedError) as caught:
            PrivateWebCreateWriter(page).create_ad(create_request())

        self.assertEqual(caught.exception.stage, "replace_create_title")
        self.assertNotIn(("submit_create",), page.calls)

    def test_create_unmarked_submit_failure_is_unknown_and_not_retried(self) -> None:
        request = create_request()
        page = FakeCreatePage(
            create_snapshot(),
            create_snapshot(
                title=request.title,
                description=request.description,
                price_amount="12",
            ),
            fail_stage="submit_create",
        )

        with self.assertRaises(PrivateWebSubmitUnknownError) as caught:
            PrivateWebCreateWriter(page).create_ad(request)

        self.assertEqual(caught.exception.stage, "submit_create")
        self.assertEqual(page.calls.count(("submit_create",)), 1)

    def test_create_submit_unknown_is_preserved_and_not_retried(self) -> None:
        request = create_request()
        page = FakeCreatePage(
            create_snapshot(),
            create_snapshot(
                title=request.title,
                description=request.description,
                price_amount="12",
            ),
            submit_unknown=True,
        )

        with self.assertRaises(PrivateWebSubmitUnknownError) as caught:
            PrivateWebCreateWriter(page).create_ad(request)

        self.assertEqual(caught.exception.stage, "create_submit")
        self.assertEqual(page.calls.count(("submit_create",)), 1)


class PrivateWebContentWriterTests(unittest.TestCase):
    def test_exact_editor_is_filled_and_submitted_once(self) -> None:
        page = FakePage(
            editor(),
            editor(title="New title", description="New description"),
        )

        PrivateWebContentWriter(page).update_content(
            AD_ID,
            title="New title",
            description="New description",
        )

        self.assertEqual(
            page.calls,
            [
                ("open_editor", AD_ID),
                ("read_editor",),
                ("replace_title", "New title"),
                ("replace_description", "New description"),
                ("read_editor",),
                ("submit",),
            ],
        )

    def test_noop_does_not_submit(self) -> None:
        page = FakePage(editor())

        PrivateWebContentWriter(page).update_content(
            AD_ID,
            title="Old title",
        )

        self.assertEqual(
            page.calls,
            [("open_editor", AD_ID), ("read_editor",)],
        )

    def test_auth_and_security_challenges_fail_before_field_mutation(self) -> None:
        for state in (
            PrivateWebEditorState.LOGIN_REQUIRED,
            PrivateWebEditorState.MFA_REQUIRED,
            PrivateWebEditorState.CAPTCHA_REQUIRED,
            PrivateWebEditorState.SECURITY_CHALLENGE,
            PrivateWebEditorState.UNKNOWN,
        ):
            with self.subTest(state=state):
                page = FakePage(editor(state=state))

                with self.assertRaises(PrivateWebPreconditionError) as caught:
                    PrivateWebContentWriter(page).update_content(
                        AD_ID,
                        title="New title",
                    )

                self.assertEqual(
                    caught.exception.reason,
                    f"before:{state.value}",
                )
                self.assertEqual(
                    page.calls,
                    [("open_editor", AD_ID), ("read_editor",)],
                )

    def test_wrong_ad_id_fails_before_field_mutation(self) -> None:
        page = FakePage(editor(ad_id="1234567890"))

        with self.assertRaisesRegex(
            PrivateWebPreconditionError,
            "ad_id_mismatch",
        ):
            PrivateWebContentWriter(page).update_content(
                AD_ID,
                description="New description",
            )

        self.assertEqual(
            page.calls,
            [("open_editor", AD_ID), ("read_editor",)],
        )

    def test_challenge_after_fill_prevents_submit(self) -> None:
        page = FakePage(
            editor(),
            editor(state=PrivateWebEditorState.CAPTCHA_REQUIRED),
        )

        with self.assertRaises(PrivateWebPreconditionError) as caught:
            PrivateWebContentWriter(page).update_content(
                AD_ID,
                title="New title",
            )

        self.assertEqual(
            caught.exception.reason,
            "before_submit:captcha_required",
        )
        self.assertNotIn(("submit",), page.calls)

    def test_ad_id_change_after_fill_prevents_submit(self) -> None:
        page = FakePage(
            editor(),
            editor(ad_id="1234567890", title="New title"),
        )

        with self.assertRaisesRegex(
            PrivateWebPreconditionError,
            "before_submit:ad_id_mismatch",
        ):
            PrivateWebContentWriter(page).update_content(
                AD_ID,
                title="New title",
            )

        self.assertNotIn(("submit",), page.calls)

    def test_collateral_field_change_before_submit_fails_closed(self) -> None:
        page = FakePage(
            editor(),
            editor(
                title="New title",
                description="Unexpected description",
            ),
        )

        with self.assertRaisesRegex(
            PrivateWebPreconditionError,
            "editor_values_drift",
        ):
            PrivateWebContentWriter(page).update_content(
                AD_ID,
                title="New title",
            )

        self.assertNotIn(("submit",), page.calls)

    def test_interaction_errors_are_sanitized(self) -> None:
        page = FakePage(editor(), fail_stage="replace_title")

        with self.assertRaises(PrivateWebInteractionError) as caught:
            PrivateWebContentWriter(page).update_content(
                AD_ID,
                title="sensitive listing text",
            )

        self.assertEqual(caught.exception.stage, "replace_title")
        self.assertEqual(
            str(caught.exception),
            "private web interaction failed at replace_title",
        )
        self.assertNotIn("provider", str(caught.exception))
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertNotIn(("submit",), page.calls)

    def test_submit_failure_is_not_retried_and_is_sanitized(self) -> None:
        page = FakePage(
            editor(),
            editor(title="New title"),
            fail_stage="submit",
        )

        with self.assertRaises(PrivateWebInteractionError) as caught:
            PrivateWebContentWriter(page).update_content(
                AD_ID,
                title="New title",
            )

        self.assertEqual(caught.exception.stage, "submit")
        self.assertEqual(page.calls.count(("submit",)), 1)

    def test_invalid_id_and_missing_update_fail_before_page_access(self) -> None:
        page = FakePage()

        with self.assertRaises(ValueError):
            PrivateWebContentWriter(page).update_content(
                "1; rm -rf /",
                title="New title",
            )
        with self.assertRaises(TypeError):
            PrivateWebContentWriter(page).update_content(
                3521676801,  # type: ignore[arg-type]
                title="New title",
            )
        with self.assertRaises(ValueError):
            PrivateWebContentWriter(page).update_content(AD_ID)

        self.assertEqual(page.calls, [])

    def test_raw_non_string_content_is_rejected(self) -> None:
        page = FakePage()

        with self.assertRaises(TypeError):
            PrivateWebContentWriter(page).update_content(
                AD_ID,
                title=123,  # type: ignore[arg-type]
            )

        self.assertEqual(page.calls, [])

    def test_snapshot_contract_hides_ad_data_when_not_ready(self) -> None:
        with self.assertRaises(ValueError):
            PrivateWebEditorSnapshot(
                state=PrivateWebEditorState.CAPTCHA_REQUIRED,
                ad_id=AD_ID,
                title="must not escape",
                description=None,
            )

    def test_import_does_not_load_historical_platform_adapters(self) -> None:
        code = (
            "import sys; import mark_api.private_web; "
            "loaded=sorted(name for name in sys.modules "
            "if name == 'mark_api.adapters' "
            "or name.startswith('mark_api.adapters.')); "
            "print(loaded); raise SystemExit(0 if not loaded else 1)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr,
        )
        self.assertEqual(result.stdout.strip(), "[]")

    def test_orchestrator_confirms_matching_post_read_with_one_submit(self) -> None:
        page = FakePage(
            editor(),
            editor(title="New title"),
        )
        writer = PrivateWebContentWriter(page)
        reader = SequenceReader(
            ReadResult.success_nonempty(
                (
                    ad_snapshot(
                        title="Old title",
                        description="Old description",
                    ),
                )
            ),
            ReadResult.success_nonempty(
                (
                    ad_snapshot(
                        title="New title",
                        description="Old description",
                    ),
                )
            ),
        )

        receipt = SafeWriteOrchestrator(
            writes_enabled=True,
            clock=lambda: NOW,
        ).update_content(
            ad_id=AD_ID,
            reader=reader,
            writer=writer,
            title="New title",
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(page.calls.count(("submit",)), 1)
        self.assertEqual(reader.calls, 2)

    def test_orchestrator_does_not_retry_ambiguous_submit_failure(self) -> None:
        page = FakePage(
            editor(),
            editor(title="New title"),
            fail_stage="submit",
        )
        writer = PrivateWebContentWriter(page)
        reader = SequenceReader(
            ReadResult.success_nonempty(
                (
                    ad_snapshot(
                        title="Old title",
                        description="Old description",
                    ),
                )
            ),
            ReadResult.success_nonempty(
                (
                    ad_snapshot(
                        title="Old title",
                        description="Old description",
                    ),
                )
            ),
        )

        receipt = SafeWriteOrchestrator(
            writes_enabled=True,
            clock=lambda: NOW,
        ).update_content(
            ad_id=AD_ID,
            reader=reader,
            writer=writer,
            title="New title",
        )

        self.assertEqual(receipt.outcome, OperationOutcome.AMBIGUOUS)
        self.assertEqual(
            receipt.writer_error,
            "PrivateWebInteractionError",
        )
        self.assertEqual(page.calls.count(("submit",)), 1)
        self.assertEqual(reader.calls, 2)


def state_snapshot(
    lifecycle_state: LifecycleState = LifecycleState.ACTIVE,
    *,
    state: PrivateWebEditorState = PrivateWebEditorState.READY,
    ad_id: str | None = AD_ID,
) -> PrivateWebStateSnapshot:
    if state is not PrivateWebEditorState.READY:
        ad_id = None
        lifecycle = None
    else:
        lifecycle = lifecycle_state
    return PrivateWebStateSnapshot(
        state=state,
        ad_id=ad_id,
        lifecycle_state=lifecycle,
    )


class FakeStatePage:
    def __init__(
        self,
        *snapshots: PrivateWebStateSnapshot,
        fail_stage: str | None = None,
        submit_unknown: bool = False,
    ) -> None:
        self.snapshots = list(snapshots)
        self.fail_stage = fail_stage
        self.submit_unknown = submit_unknown
        self.calls: list[tuple[object, ...]] = []

    def _maybe_fail(self, stage: str) -> None:
        if self.fail_stage == stage:
            raise RuntimeError("provider details must not escape")

    def open_state_controls(self, ad_id: str) -> None:
        self.calls.append(("open_state_controls", ad_id))
        self._maybe_fail("open_state_controls")

    def read_state_controls(self) -> PrivateWebStateSnapshot:
        self.calls.append(("read_state_controls",))
        self._maybe_fail("read_state_controls")
        if not self.snapshots:
            raise AssertionError("unexpected read_state_controls")
        return self.snapshots.pop(0)

    def submit_state(self, state: LifecycleState) -> None:
        self.calls.append(("submit_state", state))
        if self.submit_unknown:
            raise PrivateWebSubmitUnknownError("state_submit")
        self._maybe_fail("submit_state")


class PrivateWebStateWriterTests(unittest.TestCase):
    def test_active_paused_transitions_submit_once(self) -> None:
        cases = (
            (LifecycleState.ACTIVE, LifecycleState.PAUSED),
            (LifecycleState.PAUSED, LifecycleState.ACTIVE),
        )
        for current, target in cases:
            with self.subTest(current=current, target=target):
                page = FakeStatePage(
                    state_snapshot(current),
                    state_snapshot(current),
                )

                PrivateWebStateWriter(page).set_state(AD_ID, target)

                self.assertEqual(
                    page.calls,
                    [
                        ("open_state_controls", AD_ID),
                        ("read_state_controls",),
                        ("read_state_controls",),
                        ("submit_state", target),
                    ],
                )

    def test_same_state_is_noop_without_submit(self) -> None:
        for state in (LifecycleState.ACTIVE, LifecycleState.PAUSED):
            with self.subTest(state=state):
                page = FakeStatePage(state_snapshot(state))

                PrivateWebStateWriter(page).set_state(AD_ID, state)

                self.assertEqual(
                    page.calls,
                    [
                        ("open_state_controls", AD_ID),
                        ("read_state_controls",),
                    ],
                )

    def test_auth_and_security_states_fail_before_submit(self) -> None:
        for state in (
            PrivateWebEditorState.LOGIN_REQUIRED,
            PrivateWebEditorState.MFA_REQUIRED,
            PrivateWebEditorState.CAPTCHA_REQUIRED,
            PrivateWebEditorState.SECURITY_CHALLENGE,
            PrivateWebEditorState.UNKNOWN,
        ):
            with self.subTest(state=state):
                page = FakeStatePage(state_snapshot(state=state))

                with self.assertRaises(PrivateWebPreconditionError) as caught:
                    PrivateWebStateWriter(page).set_state(
                        AD_ID,
                        LifecycleState.PAUSED,
                    )

                self.assertEqual(caught.exception.reason, f"before:{state.value}")
                self.assertNotIn(
                    ("submit_state", LifecycleState.PAUSED),
                    page.calls,
                )

    def test_wrong_target_id_fails_closed_before_submit(self) -> None:
        page = FakeStatePage(
            state_snapshot(LifecycleState.ACTIVE, ad_id="1234567890")
        )

        with self.assertRaisesRegex(
            PrivateWebPreconditionError,
            "ad_id_mismatch",
        ):
            PrivateWebStateWriter(page).set_state(
                AD_ID,
                LifecycleState.PAUSED,
            )

        self.assertFalse(
            any(call[0] == "submit_state" for call in page.calls)
        )

    def test_challenge_or_target_change_immediately_before_submit_fails_closed(self) -> None:
        cases = (
            state_snapshot(state=PrivateWebEditorState.CAPTCHA_REQUIRED),
            state_snapshot(
                LifecycleState.ACTIVE,
                ad_id="1234567890",
            ),
        )
        for second in cases:
            with self.subTest(second=second):
                page = FakeStatePage(
                    state_snapshot(LifecycleState.ACTIVE),
                    second,
                )

                with self.assertRaises(PrivateWebPreconditionError):
                    PrivateWebStateWriter(page).set_state(
                        AD_ID,
                        LifecycleState.PAUSED,
                    )

                self.assertFalse(
                    any(call[0] == "submit_state" for call in page.calls)
                )

    def test_invalid_id_and_target_state_fail_before_page_access(self) -> None:
        page = FakeStatePage()

        with self.assertRaises(ValueError):
            PrivateWebStateWriter(page).set_state(
                "1;bad",
                LifecycleState.PAUSED,
            )
        with self.assertRaises(TypeError):
            PrivateWebStateWriter(page).set_state(
                3521676801,  # type: ignore[arg-type]
                LifecycleState.PAUSED,
            )
        with self.assertRaises(TypeError):
            PrivateWebStateWriter(page).set_state(
                AD_ID,
                "paused",  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            PrivateWebStateWriter(page).set_state(
                AD_ID,
                LifecycleState.PENDING,
            )

        self.assertEqual(page.calls, [])

    def test_pre_submit_interaction_failure_is_sanitized_without_submit(self) -> None:
        page = FakeStatePage(
            state_snapshot(LifecycleState.ACTIVE),
            fail_stage="read_state_controls",
        )

        with self.assertRaises(PrivateWebInteractionError) as caught:
            PrivateWebStateWriter(page).set_state(
                AD_ID,
                LifecycleState.PAUSED,
            )

        self.assertEqual(caught.exception.stage, "read_state_before")
        self.assertNotIn("provider", str(caught.exception))
        self.assertFalse(
            any(call[0] == "submit_state" for call in page.calls)
        )

    def test_submit_unknown_is_preserved_and_never_retried(self) -> None:
        page = FakeStatePage(
            state_snapshot(LifecycleState.ACTIVE),
            state_snapshot(LifecycleState.ACTIVE),
            submit_unknown=True,
        )

        with self.assertRaises(PrivateWebSubmitUnknownError) as caught:
            PrivateWebStateWriter(page).set_state(
                AD_ID,
                LifecycleState.PAUSED,
            )

        self.assertEqual(caught.exception.stage, "state_submit")
        self.assertEqual(
            sum(call[0] == "submit_state" for call in page.calls),
            1,
        )

    def test_orchestrator_confirms_only_matching_management_post_read(self) -> None:
        page = FakeStatePage(
            state_snapshot(LifecycleState.ACTIVE),
            state_snapshot(LifecycleState.ACTIVE),
        )
        reader = SequenceReader(
            ReadResult.success_nonempty(
                (
                    AdSnapshot(
                        ad_id=AD_ID,
                        observed_at=NOW,
                        source="management",
                        lifecycle_state=LifecycleState.ACTIVE,
                    ),
                )
            ),
            ReadResult.success_nonempty(
                (
                    AdSnapshot(
                        ad_id=AD_ID,
                        observed_at=NOW,
                        source="management",
                        lifecycle_state=LifecycleState.PAUSED,
                    ),
                )
            ),
        )

        receipt = SafeWriteOrchestrator(
            writes_enabled=True,
            clock=lambda: NOW,
        ).set_state(
            ad_id=AD_ID,
            target_state=LifecycleState.PAUSED,
            reader=reader,
            writer=PrivateWebStateWriter(page),
        )

        self.assertEqual(receipt.outcome, OperationOutcome.CONFIRMED)
        self.assertEqual(
            sum(call[0] == "submit_state" for call in page.calls),
            1,
        )
        self.assertEqual(reader.calls, 2)


    def test_orchestrator_classifies_pre_submit_interaction_failure_as_safe(self) -> None:
        page = FakeStatePage(
            state_snapshot(LifecycleState.ACTIVE),
            fail_stage="read_state_controls",
        )
        reader = SequenceReader(
            ReadResult.success_nonempty(
                (
                    AdSnapshot(
                        ad_id=AD_ID,
                        observed_at=NOW,
                        source="management",
                        lifecycle_state=LifecycleState.ACTIVE,
                    ),
                )
            ),
        )

        receipt = SafeWriteOrchestrator(
            writes_enabled=True,
            clock=lambda: NOW,
        ).set_state(
            ad_id=AD_ID,
            target_state=LifecycleState.PAUSED,
            reader=reader,
            writer=PrivateWebStateWriter(page),
        )

        self.assertEqual(
            receipt.outcome,
            OperationOutcome.PRECONDITION_FAILED,
        )
        self.assertEqual(
            receipt.writer_error,
            "PrivateWebWriteNotAttemptedError",
        )
        self.assertIsNone(receipt.post_read_status)
        self.assertEqual(reader.calls, 1)
        self.assertFalse(
            any(call[0] == "submit_state" for call in page.calls)
        )

    def test_orchestrator_classifies_challenge_before_submit_as_safe(self) -> None:
        page = FakeStatePage(
            state_snapshot(state=PrivateWebEditorState.CAPTCHA_REQUIRED),
        )
        reader = SequenceReader(
            ReadResult.success_nonempty(
                (
                    AdSnapshot(
                        ad_id=AD_ID,
                        observed_at=NOW,
                        source="management",
                        lifecycle_state=LifecycleState.ACTIVE,
                    ),
                )
            ),
        )

        receipt = SafeWriteOrchestrator(
            writes_enabled=True,
            clock=lambda: NOW,
        ).set_state(
            ad_id=AD_ID,
            target_state=LifecycleState.PAUSED,
            reader=reader,
            writer=PrivateWebStateWriter(page),
        )

        self.assertEqual(
            receipt.outcome,
            OperationOutcome.PRECONDITION_FAILED,
        )
        self.assertEqual(receipt.writer_error, "PrivateWebPreconditionError")
        self.assertIsNone(receipt.post_read_status)
        self.assertEqual(reader.calls, 1)
        self.assertFalse(
            any(call[0] == "submit_state" for call in page.calls)
        )

    def test_orchestrator_treats_unmarked_submit_failure_as_ambiguous(self) -> None:
        page = FakeStatePage(
            state_snapshot(LifecycleState.ACTIVE),
            state_snapshot(LifecycleState.ACTIVE),
            fail_stage="submit_state",
        )
        active = AdSnapshot(
            ad_id=AD_ID,
            observed_at=NOW,
            source="management",
            lifecycle_state=LifecycleState.ACTIVE,
        )
        reader = SequenceReader(
            ReadResult.success_nonempty((active,)),
            ReadResult.success_nonempty((active,)),
        )

        receipt = SafeWriteOrchestrator(
            writes_enabled=True,
            clock=lambda: NOW,
        ).set_state(
            ad_id=AD_ID,
            target_state=LifecycleState.PAUSED,
            reader=reader,
            writer=PrivateWebStateWriter(page),
        )

        self.assertEqual(receipt.outcome, OperationOutcome.AMBIGUOUS)
        self.assertEqual(receipt.writer_error, "PrivateWebInteractionError")
        self.assertEqual(
            sum(call[0] == "submit_state" for call in page.calls),
            1,
        )
        self.assertEqual(reader.calls, 2)

    def test_orchestrator_does_not_retry_submit_unknown(self) -> None:
        page = FakeStatePage(
            state_snapshot(LifecycleState.ACTIVE),
            state_snapshot(LifecycleState.ACTIVE),
            submit_unknown=True,
        )
        active = AdSnapshot(
            ad_id=AD_ID,
            observed_at=NOW,
            source="management",
            lifecycle_state=LifecycleState.ACTIVE,
        )
        reader = SequenceReader(
            ReadResult.success_nonempty((active,)),
            ReadResult.success_nonempty((active,)),
        )

        receipt = SafeWriteOrchestrator(
            writes_enabled=True,
            clock=lambda: NOW,
        ).set_state(
            ad_id=AD_ID,
            target_state=LifecycleState.PAUSED,
            reader=reader,
            writer=PrivateWebStateWriter(page),
        )

        self.assertEqual(receipt.outcome, OperationOutcome.AMBIGUOUS)
        self.assertEqual(
            receipt.writer_error,
            "PrivateWebSubmitUnknownError",
        )
        self.assertEqual(
            sum(call[0] == "submit_state" for call in page.calls),
            1,
        )
        self.assertEqual(reader.calls, 2)


class PrivateWebDeleteWriterTests(unittest.TestCase):
    def test_exact_target_requires_confirmation_and_submits_once(self) -> None:
        page = FakeDeletePage(delete_snapshot(), delete_snapshot())

        PrivateWebDeleteWriter(page).delete_ad(AD_ID)

        self.assertEqual(
            page.calls,
            [
                ("open_delete_controls", AD_ID),
                ("read_delete_controls",),
                ("open_delete_confirmation",),
                ("read_delete_confirmation",),
                ("submit_delete",),
            ],
        )

    def test_challenge_stops_before_confirmation_input(self) -> None:
        for state in (
            PrivateWebEditorState.LOGIN_REQUIRED,
            PrivateWebEditorState.MFA_REQUIRED,
            PrivateWebEditorState.CAPTCHA_REQUIRED,
            PrivateWebEditorState.SECURITY_CHALLENGE,
            PrivateWebEditorState.UNKNOWN,
        ):
            with self.subTest(state=state):
                page = FakeDeletePage(delete_snapshot(state))

                with self.assertRaises(PrivateWebPreconditionError):
                    PrivateWebDeleteWriter(page).delete_ad(AD_ID)

                self.assertEqual(
                    page.calls,
                    [
                        ("open_delete_controls", AD_ID),
                        ("read_delete_controls",),
                    ],
                )

    def test_confirmation_must_remain_exact_target(self) -> None:
        page = FakeDeletePage(
            delete_snapshot(),
            delete_snapshot(ad_id="1234567890"),
        )

        with self.assertRaisesRegex(
            PrivateWebPreconditionError,
            "confirmation:ad_id_mismatch",
        ):
            PrivateWebDeleteWriter(page).delete_ad(AD_ID)

        self.assertNotIn(("submit_delete",), page.calls)

    def test_submit_unknown_is_preserved_and_never_retried(self) -> None:
        page = FakeDeletePage(
            delete_snapshot(),
            delete_snapshot(),
            submit_unknown=True,
        )

        with self.assertRaises(PrivateWebSubmitUnknownError):
            PrivateWebDeleteWriter(page).delete_ad(AD_ID)

        self.assertEqual(page.calls.count(("submit_delete",)), 1)


if __name__ == "__main__":
    unittest.main()