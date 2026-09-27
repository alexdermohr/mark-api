from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol
from xml.sax.saxutils import quoteattr

from .monkrel_invariant import OwnerAdInvariant, owner_ad_invariant


API_HOST = "https://api.kleinanzeigen.de"
_AD_NAMESPACE_SUFFIX = "}ad"
_OWNER_INVARIANT_SOURCE = "monkrel-private-owner-http"


class MonkrelHttpResponse(Protocol):
    def json(self) -> Any:
        ...


class RawMonkrelClient(Protocol):
    """Narrow contract intentionally pinned to the current monkrel client.

    The upstream library does not expose an in-place content update method yet.
    This bridge reuses its authenticated transport and its current create-payload
    XML builder while keeping mark-api free of a hard runtime dependency.
    """

    max_retries: int

    @property
    def user_id(self) -> str:
        ...

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        data: str | None = None,
        content_type: str | None = None,
        authed: bool = True,
        gateway: bool = False,
    ) -> MonkrelHttpResponse:
        ...

    def _build_ad_xml(self, **kwargs: Any) -> str:
        ...


def _unwrap_value(value: Any) -> Any:
    while isinstance(value, Mapping) and set(value) == {"value"}:
        value = value["value"]
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    value = _unwrap_value(value)
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _required_text(value: Any, field: str) -> str:
    value = _unwrap_value(value)
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} is missing")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} is blank")
    return text


def _required_content_text(value: Any, field: str) -> str:
    value = _unwrap_value(value)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not value.strip():
        raise ValueError(f"{field} is blank")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    value = _unwrap_value(value)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    return value


def _optional_scalar(value: Any, field: str) -> Any:
    if value is None:
        return None
    value = _unwrap_value(value)
    if value is None:
        return None
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return value
    raise ValueError(f"{field} must be a scalar or null")


def _unwrap_ad_payload(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("owner ad response root must be an object")

    direct = payload.get("ad")
    if direct is not None:
        if isinstance(direct, Mapping) and "value" in direct:
            direct = direct["value"]
        return _mapping(direct, "ad")

    for key, value in payload.items():
        if isinstance(key, str) and key.endswith(_AD_NAMESPACE_SUFFIX):
            if isinstance(value, Mapping) and "value" in value:
                value = value["value"]
            return _mapping(value, "ad")

    return payload


def _ensure_supported_optional_features(ad: Mapping[str, Any]) -> OwnerAdInvariant:
    state = owner_ad_invariant(dict(ad), source=_OWNER_INVARIANT_SOURCE)
    state.require_content_update_safe()
    return state


def _inject_shipping_options(xml: str, option_ids: tuple[str, ...]) -> str:
    if not option_ids:
        return xml
    if 'xmlns:shipping="http://www.ebayclassifiedsgroup.com/schema/shipping/v1"' not in xml:
        raise RuntimeError("monkrel XML builder does not declare the shipping namespace")
    if "<shipping:shipping-options" in xml:
        raise RuntimeError("monkrel XML builder already emits shipping options")
    marker = '<payment:buy-now selected="false"/>'
    if xml.count(marker) != 1:
        raise RuntimeError(
            "monkrel XML builder does not expose the expected buy-now=false marker"
        )
    block = (
        "<shipping:shipping-options>"
        + "".join(
            f"<shipping:shipping-option id={quoteattr(option_id)}/>"
            for option_id in option_ids
        )
        + "</shipping:shipping-options>"
    )
    return xml.replace(marker, block + marker, 1)


class MonkrelPrivateHttpContentClient:
    """Add a conservative update_ad() primitive to the current monkrel client.

    Evidence for the owner-scoped PUT comes from the current Android-CAPI map
    and the historical CAPI contract, which specifies the same write payload as
    create. The current monkrel create XML builder is therefore reused instead
    of maintaining a second copy of the wire schema here.

    A dedicated upstream client configured with max_retries=1 is required so a
    write is attempted at most once. Reads and payload reconstruction happen
    before that single PUT.
    """

    def __init__(
        self,
        client: RawMonkrelClient,
        *,
        api_host: str = API_HOST,
        contact_email_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self._client = client
        self._api_host = api_host.rstrip("/")
        self._contact_email_provider = contact_email_provider

    def _email(self, ad: Mapping[str, Any]) -> str:
        current = _optional_text(ad.get("email"), "email")
        if current is not None and current.strip():
            return current
        if self._contact_email_provider is None:
            raise ValueError("owner ad email is unavailable")
        fallback = self._contact_email_provider()
        if not isinstance(fallback, str) or not fallback.strip():
            raise ValueError("owner ad email is unavailable")
        return fallback

    def _owner_ad(self, target_id: str) -> tuple[str, Mapping[str, Any]]:
        uid = _required_text(self._client.user_id, "user_id")
        response = self._client._request(
            "GET",
            f"{self._api_host}/api/users/{uid}/ads/{target_id}.json",
            authed=True,
        )
        ad = _unwrap_ad_payload(response.json())
        returned_id = _required_text(ad.get("id"), "owner_ad.id")
        if returned_id != target_id:
            raise ValueError("owner ad id does not match requested ad_id")
        return uid, ad

    def read_invariant(self, ad_id: str) -> OwnerAdInvariant:
        target_id = _required_text(ad_id, "ad_id")
        _, ad = self._owner_ad(target_id)
        return owner_ad_invariant(dict(ad), source=_OWNER_INVARIANT_SOURCE)

    def update_ad(
        self,
        ad_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        target_id = _required_text(ad_id, "ad_id")
        if title is not None and not isinstance(title, str):
            raise TypeError("title must be a string or None")
        if description is not None and not isinstance(description, str):
            raise TypeError("description must be a string or None")
        if title is None and description is None:
            raise ValueError("at least one content field must be provided")

        retries = getattr(self._client, "max_retries", None)
        if type(retries) is not int or retries != 1:
            raise RuntimeError(
                "monkrel write client must be configured with max_retries=1"
            )

        uid, ad = self._owner_ad(target_id)
        invariant = _ensure_supported_optional_features(ad)

        current_title = _required_content_text(ad.get("title"), "title")
        current_description = _required_content_text(
            ad.get("description"),
            "description",
        )
        new_title = current_title if title is None else title
        new_description = current_description if description is None else description
        if new_title == current_title and new_description == current_description:
            raise ValueError("content update would be a no-op")

        category = _mapping(ad.get("category"), "category")
        price = _mapping(ad.get("price"), "price")
        address = (
            _mapping(ad.get("ad-address"), "ad-address")
            if ad.get("ad-address") is not None
            else {}
        )

        price_type = _required_text(price.get("price-type"), "price.price-type")
        amount = _optional_scalar(price.get("amount"), "price.amount")
        normalized_price_type = price_type.strip().upper()
        if normalized_price_type not in {
            "FIXED",
            "SPECIFIED_AMOUNT",
            "NEGOTIABLE",
            "PLEASE_CONTACT",
            "FREE",
            "GIVE_AWAY",
        }:
            raise ValueError("unsupported price type")
        if normalized_price_type in {"FIXED", "SPECIFIED_AMOUNT", "NEGOTIABLE"} and amount is None:
            raise ValueError("price amount is required for this price type")

        xml = self._client._build_ad_xml(
            title=new_title,
            description=new_description,
            category_id=_required_text(category.get("id"), "category.id"),
            location_id=invariant.location_id,
            price=amount,
            price_type=price_type,
            poster_type=_required_text(ad.get("poster-type"), "poster-type"),
            ad_type=_required_text(ad.get("ad-type"), "ad-type"),
            contact_name=_optional_text(ad.get("contact-name"), "contact-name") or "",
            email=self._email(ad),
            phone=_optional_text(ad.get("phone"), "phone"),
            attributes=invariant.single_value_attributes(),
            picture_urls=invariant.xxl_picture_urls(),
            latitude=_optional_scalar(address.get("latitude"), "ad-address.latitude"),
            longitude=_optional_scalar(address.get("longitude"), "ad-address.longitude"),
        )
        if not isinstance(xml, str) or not xml.strip():
            raise RuntimeError("monkrel XML builder returned an empty payload")
        xml = _inject_shipping_options(xml, invariant.shipping_option_ids)

        self._client._request(
            "PUT",
            f"{self._api_host}/api/users/{uid}/ads/{target_id}",
            data=xml,
            content_type="application/xml",
            authed=True,
        )
