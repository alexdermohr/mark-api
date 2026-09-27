from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


def _local_name(key: Any) -> str:
    text = str(key)
    return text.rsplit("}", 1)[-1] if "}" in text else text


def _direct(mapping: Any, local_name: str, *, label: str | None = None) -> Any:
    if not isinstance(mapping, dict):
        return None
    matches = [
        item
        for key, item in mapping.items()
        if _local_name(key) == local_name
    ]
    if len(matches) > 1:
        raise ValueError(
            f"owner ad has ambiguous {label or local_name}"
        )
    return matches[0] if matches else None


def _unwrap_value(value: Any) -> Any:
    current = value
    for _ in range(5):
        if not isinstance(current, dict) or len(current) != 1:
            break
        next_value = _direct(current, "value", label="value wrapper")
        if next_value is None:
            break
        current = next_value
    return current


def _mapping(value: Any, label: str) -> dict[str, Any]:
    current = _unwrap_value(value)
    if not isinstance(current, dict):
        raise ValueError(f"owner ad has malformed {label}")
    return current


def _as_sequence(value: Any) -> list[Any]:
    current = _unwrap_value(value)
    if current is None:
        return []
    if isinstance(current, list):
        return current
    return [current]


def _text(value: Any, label: str, *, required: bool = True) -> str | None:
    current = _unwrap_value(value)
    if current is None:
        if required:
            raise ValueError(f"owner ad is missing {label}")
        return None
    if isinstance(current, (dict, list)):
        raise ValueError(f"owner ad has non-scalar {label}")
    text = str(current)
    if required and not text.strip():
        raise ValueError(f"owner ad has blank {label}")
    if not required and not text.strip():
        return None
    return text


def _id(value: Any, label: str) -> str:
    block = _mapping(value, label)
    return str(_text(_direct(block, "id", label=f"{label} id"), f"{label} id"))


def _bool(value: Any, label: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    current = _unwrap_value(value)
    if isinstance(current, bool):
        return current
    if isinstance(current, int) and current in (0, 1):
        return bool(current)
    if isinstance(current, str):
        normalized = current.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no", ""}:
            return False
    raise ValueError(f"owner ad has unsupported {label} boolean")


def _has_nonempty_value(value: Any) -> bool:
    current = _unwrap_value(value)
    if current is None:
        return False
    if isinstance(current, dict):
        return any(_has_nonempty_value(item) for item in current.values())
    if isinstance(current, list):
        return any(_has_nonempty_value(item) for item in current)
    if isinstance(current, str):
        return bool(current.strip())
    return bool(current)


def _attributes(ad: dict[str, Any]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    raw_container = _direct(ad, "attributes", label="attributes")
    if raw_container is None:
        return ()
    container = _mapping(raw_container, "attributes")
    items = _as_sequence(_direct(container, "attribute", label="attribute"))
    parsed: list[tuple[str, tuple[str, ...]]] = []
    for index, item in enumerate(items):
        attr = _mapping(item, f"attribute[{index}]")
        name = str(
            _text(
                _direct(attr, "name", label=f"attribute[{index}] name"),
                f"attribute[{index}] name",
            )
        )
        values = tuple(
            value
            for raw in _as_sequence(
                _direct(attr, "value", label=f"attribute {name} value")
            )
            if (
                value := _text(
                    raw,
                    f"attribute {name} value",
                    required=False,
                )
            )
            is not None
        )
        if not values:
            raise ValueError(f"owner ad attribute {name} has no value")
        parsed.append((name, values))
    return tuple(parsed)


def _pictures(ad: dict[str, Any]) -> tuple[tuple[tuple[str, str], ...], ...]:
    raw_container = _direct(ad, "pictures", label="pictures")
    if raw_container is None:
        return ()
    container = _mapping(raw_container, "pictures")
    raw_pictures = _as_sequence(_direct(container, "picture", label="picture"))
    pictures: list[tuple[tuple[str, str], ...]] = []
    for picture_index, item in enumerate(raw_pictures):
        picture = _mapping(item, f"picture[{picture_index}]")
        links: list[tuple[str, str]] = []
        for link_index, link_item in enumerate(
            _as_sequence(_direct(picture, "link", label="picture link"))
        ):
            link = _mapping(
                link_item,
                f"picture[{picture_index}].link[{link_index}]",
            )
            rel = str(
                _text(
                    _direct(link, "rel", label="picture rel"),
                    f"picture[{picture_index}].link[{link_index}].rel",
                )
            )
            href = str(
                _text(
                    _direct(link, "href", label="picture href"),
                    f"picture[{picture_index}].link[{link_index}].href",
                )
            )
            links.append((rel, href))
        if not links:
            raise ValueError("owner ad picture has no links")
        pictures.append(tuple(links))
    return tuple(pictures)


def _shipping_option_ids(ad: dict[str, Any]) -> tuple[str, ...]:
    raw_container = _direct(ad, "shipping-options", label="shipping-options")
    if raw_container is None:
        return ()
    container = _mapping(raw_container, "shipping-options")
    options = _as_sequence(
        _direct(container, "shipping-option", label="shipping-option")
    )
    ids = tuple(_id(option, "shipping-option") for option in options)
    if len(ids) != len(set(ids)):
        raise ValueError("owner ad has duplicate shipping-option ids")
    return ids


@dataclass(frozen=True, slots=True)
class OwnerAdInvariant:
    """Canonical owner-visible state that a full-payload content PUT must preserve."""

    source: str
    ad_id: str
    title: str
    description: str
    category_id: str
    location_id: str
    price_type: str
    price_amount: str | None
    poster_type: str
    ad_type: str
    contact_name: str | None
    email: str | None
    phone: str | None
    latitude: str | None
    longitude: str | None
    attributes: tuple[tuple[str, tuple[str, ...]], ...]
    pictures: tuple[tuple[tuple[str, str], ...], ...]
    shipping_option_ids: tuple[str, ...]
    buy_now_selected: bool
    shipping_metadata_empty: bool
    medias_empty: bool
    product_safety_empty: bool
    show_full_address: bool
    imprint: str | None

    def with_content(self, *, title: str, description: str) -> "OwnerAdInvariant":
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be a non-blank string")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("description must be a non-blank string")
        return replace(self, title=title, description=description)

    def single_value_attributes(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for name, values in self.attributes:
            if len(values) != 1:
                raise ValueError(
                    f"attribute {name} is multi-value and not update-safe"
                )
            if name in result:
                raise ValueError(f"duplicate attribute {name} is not update-safe")
            result[name] = values[0]
        return result

    def xxl_picture_urls(self) -> list[str]:
        urls: list[str] = []
        for picture in self.pictures:
            xxl = [href for rel, href in picture if rel.upper() == "XXL"]
            if len(xxl) != 1:
                raise ValueError(
                    "owner ad picture does not expose exactly one XXL link"
                )
            urls.append(xxl[0])
        return urls

    def require_content_update_safe(self) -> None:
        if self.email is None:
            raise ValueError(
                "owner ad email is unavailable; content update would not preserve owner state"
            )
        if self.buy_now_selected:
            raise ValueError(
                "buy-now=true is not update-safe without direct wire-format proof"
            )
        if not self.shipping_metadata_empty:
            raise ValueError("shipping metadata is not update-safe yet")
        if not self.medias_empty:
            raise ValueError("medias is not update-safe yet")
        if not self.product_safety_empty:
            raise ValueError("product-safety is not update-safe yet")
        if self.show_full_address:
            raise ValueError("show-full-address=true is not update-safe yet")
        if self.imprint is not None:
            raise ValueError("imprint is not update-safe yet")
        self.single_value_attributes()
        self.xxl_picture_urls()


def owner_ad_invariant(
    ad: dict[str, Any],
    *,
    source: str,
) -> OwnerAdInvariant:
    if not isinstance(ad, dict):
        raise ValueError("owner ad payload must be an object")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("owner invariant source must be non-blank")

    raw_locations = _direct(ad, "locations", label="locations")
    if raw_locations is not None:
        locations = _mapping(raw_locations, "locations")
        raw_location_items = _direct(locations, "location", label="location")
    else:
        raw_location_items = _direct(ad, "location", label="location")
    location_items = _as_sequence(raw_location_items)
    if len(location_items) != 1:
        raise ValueError("owner ad must expose exactly one location")

    price = _mapping(_direct(ad, "price", label="price"), "price")
    address_raw = _direct(ad, "ad-address", label="ad-address")
    address = {} if address_raw is None else _mapping(address_raw, "ad-address")

    buy_now_raw = _direct(ad, "buy-now", label="buy-now")
    buy_now_selected = False
    if buy_now_raw is not None:
        buy_now = _mapping(buy_now_raw, "buy-now")
        buy_now_selected = _bool(
            _direct(buy_now, "selected", label="buy-now selected"),
            "buy-now selected",
        )

    show_full_address = _bool(
        _direct(address, "show-full-address", label="show-full-address"),
        "show-full-address",
        default=False,
    )

    product_safety_empty = not any(
        _has_nonempty_value(_direct(ad, key, label=key))
        for key in ("productsafety", "product-safety")
    )

    return OwnerAdInvariant(
        source=source,
        ad_id=str(_text(_direct(ad, "id", label="ad id"), "ad id")),
        title=str(_text(_direct(ad, "title", label="title"), "title")),
        description=str(
            _text(_direct(ad, "description", label="description"), "description")
        ),
        category_id=_id(_direct(ad, "category", label="category"), "category"),
        location_id=_id(location_items[0], "location"),
        price_type=str(
            _text(
                _direct(price, "price-type", label="price type"),
                "price type",
            )
        ),
        price_amount=_text(
            _direct(price, "amount", label="price amount"),
            "price amount",
            required=False,
        ),
        poster_type=str(
            _text(
                _direct(ad, "poster-type", label="poster type"),
                "poster type",
            )
        ),
        ad_type=str(_text(_direct(ad, "ad-type", label="ad type"), "ad type")),
        contact_name=_text(
            _direct(ad, "contact-name", label="contact name"),
            "contact name",
            required=False,
        ),
        email=_text(
            _direct(ad, "email", label="email"),
            "email",
            required=False,
        ),
        phone=_text(
            _direct(ad, "phone", label="phone"),
            "phone",
            required=False,
        ),
        latitude=_text(
            _direct(address, "latitude", label="latitude"),
            "latitude",
            required=False,
        ),
        longitude=_text(
            _direct(address, "longitude", label="longitude"),
            "longitude",
            required=False,
        ),
        attributes=_attributes(ad),
        pictures=_pictures(ad),
        shipping_option_ids=_shipping_option_ids(ad),
        buy_now_selected=buy_now_selected,
        shipping_metadata_empty=not _has_nonempty_value(
            _direct(ad, "shipping", label="shipping")
        ),
        medias_empty=not _has_nonempty_value(
            _direct(ad, "medias", label="medias")
        ),
        product_safety_empty=product_safety_empty,
        show_full_address=show_full_address,
        imprint=_text(
            _direct(ad, "imprint", label="imprint"),
            "imprint",
            required=False,
        ),
    )