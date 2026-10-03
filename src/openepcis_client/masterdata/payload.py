# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG
"""Building the nested catalog document from path/value pairs.

The catalog wants nested JSON keyed by GS1 Web Vocabulary local names::

    brand.brandName                                  -> {"brand": {"brandName": ...}}
    countryOfOrigin.countryCode                      -> {"countryOfOrigin": {"countryCode": "DE"}}
    targetMarket[].targetMarketCountries.countryCode -> {"targetMarket": [{...}]}

A ``[]`` segment means "a single-element list", which is how the catalog
models target markets, contact points and referenced files.

Reading values out of the host system is the host adapter's job; this module
only knows how to place a value at a dotted path, how to pick one back out of a
catalog document (:func:`pick`, for synchronising the other way), and how the
catalog wants a few awkward value kinds shaped. The catalog treats ``PUT`` as a merge where an
absent key means "leave alone", so hosts skip empty values rather than sending
``null``; the shaping helpers answer ``None`` for a value that must be skipped.
"""

from typing import Any


def place(document: dict[str, Any], path: str, value: Any) -> None:
    """Write ``value`` into ``document`` at a dotted GS1 term path.

    :raises ValueError: for a path that cannot name anything (empty, leading or
        trailing dot, empty segment). The path usually comes from host-side
        configuration, so the fault is a configuration mistake to report.
    """
    parts = [p.strip() for p in (path or "").split(".")]
    if not all(parts):
        raise ValueError(f"{path!r} is not a usable GS1 term path")
    node = document
    for part in parts[:-1]:
        node = _descend(node, part)
    last = parts[-1]
    if last.endswith("[]"):
        node[last[:-2]] = [value]
    else:
        node[last] = value


def pick(document: dict[str, Any], path: str) -> Any:
    """The value at a dotted GS1 term path, or ``None`` when nothing is there.

    The reading counterpart of :func:`place`, with the same grammar: a ``[]``
    segment takes the first element of the list. A record a host reads back
    from the catalog may hold more than one contact point or target market;
    the first is the one :func:`place` would have written, and the only one a
    single host field can hold.

    :raises ValueError: for a path that cannot name anything, as :func:`place`.
    """
    parts = [p.strip() for p in (path or "").split(".")]
    if not all(parts):
        raise ValueError(f"{path!r} is not a usable GS1 term path")
    node: Any = document
    for part in parts:
        if not isinstance(node, dict):
            return None
        if part.endswith("[]"):
            items = node.get(part[:-2])
            node = items[0] if isinstance(items, list) and items else None
        else:
            node = node.get(part)
    return node


def pick_localized(value: Any, preferred: tuple[str, ...] = ()) -> str | None:
    """One text out of a language map, in the order of ``preferred`` tags.

    Falls back to English, then to whichever language the map holds first, so
    a record kept in a single language still reads. A plain string passes
    through: some catalog fields are language maps in one record and plain
    text in another, depending on who wrote them.
    """
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    for tag in (*preferred, "en"):
        text = value.get(tag)
        if isinstance(text, str) and text.strip():
            return text.strip()
    for text in value.values():
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def _descend(node: dict[str, Any], part: str) -> dict[str, Any]:
    """One step down the path, creating the container the segment implies."""
    if part.endswith("[]"):
        key = part[:-2]
        existing = node.get(key)
        if not isinstance(existing, list) or not existing or not isinstance(existing[0], dict):
            node[key] = [{}]
        first: dict[str, Any] = node[key][0]
        return first
    if not isinstance(node.get(part), dict):
        node[part] = {}
    child: dict[str, Any] = node[part]
    return child


def quantity(value: float, unit_code: str) -> dict[str, Any] | None:
    """A measurement as the catalog wants it: ``{"value": ..., "unitCode": ...}``.

    ``None`` when there is nothing to say. A value of zero is the common ERP
    default for "not filled in", and sending it would satisfy a requirement
    falsely. A value without a UN/CEFACT unit code is not a measurement, so it
    is dropped rather than sent half-formed; the host's readiness check should
    agree, so the omission is reported instead of silent.
    """
    number = float(value)
    if not number or not unit_code:
        return None
    return {"value": number, "unitCode": unit_code}


def boolean_text(flag: Any) -> str:
    """``"true"`` or ``"false"``, for catalog fields declared as strings.

    Several catalog fields hold ``"true"``/``"false"`` as text rather than as
    booleans; sending a real boolean fails validation.
    """
    return "true" if flag else "false"


def localized(values: dict[str, str]) -> dict[str, str] | None:
    """A language map, empty entries removed; ``None`` when nothing remains.

    Keys are BCP-47 primary subtags (``de``, ``en``); the host derives them
    from its locale codes, for example with
    :func:`openepcis_client.core.gs1.language_tag`.
    """
    kept = {tag: text.strip() for tag, text in values.items() if tag and text and text.strip()}
    return kept or None
