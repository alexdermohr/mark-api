"""Bounded JSON admission shared by all HTTP write-intent entrypoints.

The input is capped by each HTTP handler separately. This decoder also bounds
container nesting *before* Python allocates deep trees or downstream code
canonicalizes them. All wire object members must be unambiguous and numbers
must stay finite when represented by Python.
"""

from __future__ import annotations

import json
import math


_MAX_JSON_NESTING = 64


def _check_nesting(raw: str) -> None:
    depth = 0
    quoted = False
    escaped = False
    for character in raw:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":  # backslash
                escaped = True
            elif character == '"':  # quote
                quoted = False
        elif character == '"':
            quoted = True
        elif character in ("[", "{"):  # [ or {
            depth += 1
            if depth > _MAX_JSON_NESTING:
                raise ValueError("invalid_json")
        elif character in ("]", "}"):  # ] or }
            depth -= 1
    # The JSON decoder below checks matching delimiters and string syntax.


def _unique_object_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for name, value in pairs:
        if name in values:
            raise ValueError("duplicate JSON object member")
        values[name] = value
    return values


def _finite_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("nonfinite JSON number")
    return value


def _reject_constant(_raw: str) -> object:
    raise ValueError("nonfinite JSON constant")


def strict_json_object(raw: bytes) -> dict[str, object]:
    """Read a bounded, unambiguous JSON object or raise a stable client error."""
    try:
        text = raw.decode("utf-8")
        _check_nesting(text)
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object_members,
            parse_float=_finite_float,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid_json") from exc
    if not isinstance(payload, dict):
        raise ValueError("json_body_must_be_object")
    # Python's json decoder accepts a lone \\uD800/\\uDFFF escape. Those
    # are not Unicode scalar values: their later UTF-8 encoding can fail
    # after a durable pending Write has already been claimed. Validate every
    # decoded value AND object key (also in nested arrays) before admission.
    # The parser already bounds nesting; a worklist avoids recursion and
    # preserves valid surrogate *pairs* decoded as one Unicode scalar.
    remaining: list[object] = [payload]
    while remaining:
        item = remaining.pop()
        if type(item) is str:
            try:
                item.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("invalid_unicode_text") from exc
        elif type(item) is dict:
            remaining.extend(item.keys())
            remaining.extend(item.values())
        elif type(item) is list:
            remaining.extend(item)
    return payload
