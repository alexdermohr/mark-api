from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ProSellersAccountType(StrEnum):
    PRIVATE = "private"
    PROFESSIONAL = "professional"


class ProSellersPlan(StrEnum):
    BASIC = "basic"
    POWER = "power"
    PREMIUM = "premium"


class ProSellersWriteAuthority(StrEnum):
    MANUAL_ONLY = "manual_only"
    API_ORIGINATED_ONLY = "api_originated_only"
    MIXED = "mixed"


class ProSellersAdmissionReason(StrEnum):
    ACCOUNT_NOT_PROFESSIONAL = "account_not_professional"
    PLAN_NOT_API_ELIGIBLE = "plan_not_api_eligible"
    API_ENTITLEMENT_NOT_CONFIRMED = "api_entitlement_not_confirmed"
    CLIENT_ID_MISSING = "client_id_missing"
    CLIENT_SECRET_MISSING = "client_secret_missing"
    WRITE_AUTHORITY_NOT_API_ORIGINATED_ONLY = (
        "write_authority_not_api_originated_only"
    )


@dataclass(frozen=True, slots=True)
class ProSellersRuntimeConfig:
    """Local inputs required before a future official API client may be built.

    Credential fields are intentionally excluded from repr/str output. The
    values remain opaque and are not normalized or persisted by this module.
    """

    account_type: ProSellersAccountType
    plan: ProSellersPlan | None
    api_entitlement_confirmed: bool
    client_id: str | None = field(repr=False)
    client_secret: str | None = field(repr=False)
    write_authority: ProSellersWriteAuthority | None


@dataclass(frozen=True, slots=True)
class ProSellersAdmissionDecision:
    allowed: bool
    reasons: tuple[ProSellersAdmissionReason, ...]

    def __post_init__(self) -> None:
        if self.allowed == bool(self.reasons):
            raise ValueError(
                "allowed must be true exactly when admission reasons are empty"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reasons": [reason.value for reason in self.reasons],
        }


class ProSellersAdmissionError(RuntimeError):
    def __init__(self, decision: ProSellersAdmissionDecision) -> None:
        self.decision = decision
        reason_text = ", ".join(reason.value for reason in decision.reasons)
        super().__init__(f"ProSellers admission denied: {reason_text}")


def _credential_present(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def assess_prosellers_admission(
    config: ProSellersRuntimeConfig,
) -> ProSellersAdmissionDecision:
    """Return a deterministic, secret-free decision without network access."""

    if not isinstance(config, ProSellersRuntimeConfig):
        raise TypeError("config must be ProSellersRuntimeConfig")

    reasons: list[ProSellersAdmissionReason] = []
    if config.account_type is not ProSellersAccountType.PROFESSIONAL:
        reasons.append(ProSellersAdmissionReason.ACCOUNT_NOT_PROFESSIONAL)
    if (
        config.plan is not ProSellersPlan.POWER
        and config.plan is not ProSellersPlan.PREMIUM
    ):
        reasons.append(ProSellersAdmissionReason.PLAN_NOT_API_ELIGIBLE)
    if config.api_entitlement_confirmed is not True:
        reasons.append(
            ProSellersAdmissionReason.API_ENTITLEMENT_NOT_CONFIRMED
        )
    if not _credential_present(config.client_id):
        reasons.append(ProSellersAdmissionReason.CLIENT_ID_MISSING)
    if not _credential_present(config.client_secret):
        reasons.append(ProSellersAdmissionReason.CLIENT_SECRET_MISSING)
    if (
        config.write_authority
        is not ProSellersWriteAuthority.API_ORIGINATED_ONLY
    ):
        reasons.append(
            ProSellersAdmissionReason.WRITE_AUTHORITY_NOT_API_ORIGINATED_ONLY
        )

    return ProSellersAdmissionDecision(
        allowed=not reasons,
        reasons=tuple(reasons),
    )


def require_prosellers_admission(
    config: ProSellersRuntimeConfig,
) -> ProSellersRuntimeConfig:
    """Fail closed unless every official ProSellers admission gate is met.

    This function performs no token request, Goods API request, persistence or
    logging. A future transport must call this gate before constructing any
    network-capable ProSellers client.
    """

    decision = assess_prosellers_admission(config)
    if not decision.allowed:
        raise ProSellersAdmissionError(decision)
    return config
