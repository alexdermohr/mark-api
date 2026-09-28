from __future__ import annotations

import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path

from mark_api.prosellers import (
    ProSellersAccountType,
    ProSellersAdmissionError,
    ProSellersAdmissionReason,
    ProSellersPlan,
    ProSellersRuntimeConfig,
    ProSellersWriteAuthority,
    assess_prosellers_admission,
    require_prosellers_admission,
)


CLIENT_ID = "client-id-do-not-print"
CLIENT_SECRET = "client-secret-do-not-print"


def allowed_config(
    *,
    plan: ProSellersPlan = ProSellersPlan.POWER,
) -> ProSellersRuntimeConfig:
    return ProSellersRuntimeConfig(
        account_type=ProSellersAccountType.PROFESSIONAL,
        plan=plan,
        api_entitlement_confirmed=True,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        write_authority=ProSellersWriteAuthority.API_ORIGINATED_ONLY,
    )


class ProSellersAdmissionTests(unittest.TestCase):
    def test_power_and_premium_are_admitted_with_every_gate_present(self) -> None:
        for plan in (ProSellersPlan.POWER, ProSellersPlan.PREMIUM):
            with self.subTest(plan=plan):
                config = allowed_config(plan=plan)
                decision = assess_prosellers_admission(config)

                self.assertTrue(decision.allowed)
                self.assertEqual(decision.reasons, ())
                self.assertEqual(
                    decision.to_dict(),
                    {"allowed": True, "reasons": []},
                )
                self.assertIs(require_prosellers_admission(config), config)

    def test_private_account_is_denied_even_with_other_gates_present(self) -> None:
        config = replace(
            allowed_config(),
            account_type=ProSellersAccountType.PRIVATE,
        )

        decision = assess_prosellers_admission(config)

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reasons,
            (ProSellersAdmissionReason.ACCOUNT_NOT_PROFESSIONAL,),
        )

    def test_basic_plan_is_denied(self) -> None:
        decision = assess_prosellers_admission(
            replace(allowed_config(), plan=ProSellersPlan.BASIC)
        )

        self.assertEqual(
            decision.reasons,
            (ProSellersAdmissionReason.PLAN_NOT_API_ELIGIBLE,),
        )

    def test_entitlement_must_be_explicit_true(self) -> None:
        for value in (False, 0, None, "yes"):
            with self.subTest(value=value):
                config = replace(
                    allowed_config(),
                    api_entitlement_confirmed=value,  # type: ignore[arg-type]
                )
                decision = assess_prosellers_admission(config)
                self.assertIn(
                    ProSellersAdmissionReason.API_ENTITLEMENT_NOT_CONFIRMED,
                    decision.reasons,
                )

    def test_both_credentials_must_be_nonblank_strings(self) -> None:
        cases = (
            (
                replace(allowed_config(), client_id=None),
                ProSellersAdmissionReason.CLIENT_ID_MISSING,
            ),
            (
                replace(allowed_config(), client_id="   "),
                ProSellersAdmissionReason.CLIENT_ID_MISSING,
            ),
            (
                replace(allowed_config(), client_secret=None),
                ProSellersAdmissionReason.CLIENT_SECRET_MISSING,
            ),
            (
                replace(allowed_config(), client_secret="\t"),
                ProSellersAdmissionReason.CLIENT_SECRET_MISSING,
            ),
        )
        for config, reason in cases:
            with self.subTest(reason=reason):
                self.assertIn(
                    reason,
                    assess_prosellers_admission(config).reasons,
                )

    def test_write_authority_must_be_api_originated_only(self) -> None:
        for authority in (
            None,
            ProSellersWriteAuthority.MANUAL_ONLY,
            ProSellersWriteAuthority.MIXED,
        ):
            with self.subTest(authority=authority):
                decision = assess_prosellers_admission(
                    replace(allowed_config(), write_authority=authority)
                )
                self.assertIn(
                    ProSellersAdmissionReason
                    .WRITE_AUTHORITY_NOT_API_ORIGINATED_ONLY,
                    decision.reasons,
                )

    def test_denial_accumulates_reasons_in_stable_order(self) -> None:
        config = ProSellersRuntimeConfig(
            account_type=ProSellersAccountType.PRIVATE,
            plan=None,
            api_entitlement_confirmed=False,
            client_id=None,
            client_secret=" ",
            write_authority=ProSellersWriteAuthority.MIXED,
        )

        decision = assess_prosellers_admission(config)

        self.assertEqual(
            decision.reasons,
            (
                ProSellersAdmissionReason.ACCOUNT_NOT_PROFESSIONAL,
                ProSellersAdmissionReason.PLAN_NOT_API_ELIGIBLE,
                ProSellersAdmissionReason.API_ENTITLEMENT_NOT_CONFIRMED,
                ProSellersAdmissionReason.CLIENT_ID_MISSING,
                ProSellersAdmissionReason.CLIENT_SECRET_MISSING,
                ProSellersAdmissionReason
                .WRITE_AUTHORITY_NOT_API_ORIGINATED_ONLY,
            ),
        )

    def test_repr_decision_and_error_do_not_expose_credentials(self) -> None:
        config = replace(
            allowed_config(),
            api_entitlement_confirmed=False,
        )

        rendered_config = repr(config)
        decision = assess_prosellers_admission(config)
        rendered_decision = repr(decision)
        with self.assertRaises(ProSellersAdmissionError) as caught:
            require_prosellers_admission(config)
        rendered_error = str(caught.exception)

        for secret in (CLIENT_ID, CLIENT_SECRET):
            self.assertNotIn(secret, rendered_config)
            self.assertNotIn(secret, rendered_decision)
            self.assertNotIn(secret, rendered_error)
            self.assertNotIn(secret, str(decision.to_dict()))

    def test_raw_strings_and_non_string_credentials_do_not_bypass_gate(self) -> None:
        raw_values = ProSellersRuntimeConfig(
            account_type="professional",  # type: ignore[arg-type]
            plan="power",  # type: ignore[arg-type]
            api_entitlement_confirmed=True,
            client_id=b"client-id",  # type: ignore[arg-type]
            client_secret=b"client-secret",  # type: ignore[arg-type]
            write_authority="api_originated_only",  # type: ignore[arg-type]
        )

        decision = assess_prosellers_admission(raw_values)

        self.assertEqual(
            decision.reasons,
            (
                ProSellersAdmissionReason.ACCOUNT_NOT_PROFESSIONAL,
                ProSellersAdmissionReason.PLAN_NOT_API_ELIGIBLE,
                ProSellersAdmissionReason.CLIENT_ID_MISSING,
                ProSellersAdmissionReason.CLIENT_SECRET_MISSING,
                ProSellersAdmissionReason
                .WRITE_AUTHORITY_NOT_API_ORIGINATED_ONLY,
            ),
        )

    def test_invalid_config_type_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            assess_prosellers_admission(object())  # type: ignore[arg-type]

    def test_import_does_not_load_historical_platform_adapters(self) -> None:
        code = (
            "import sys; import mark_api.prosellers; "
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


if __name__ == "__main__":
    unittest.main()
