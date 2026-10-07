"""Small, shared contracts for lossless semantic evidence checks.

Normalization is not a semantic rewrite. In particular, punctuation inside
numbers, ranges, dates, identifiers and formulas must survive comparison.
"""

from __future__ import annotations

import re
import unicodedata

_INSTANCE_TAIL = re.compile(
    r"^[\s。.;；]*(?:实例|样例|示例|instance|example)\s*[:：]\s*(?P<body>.+)$", re.I | re.S)
_SLOT = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def semantic_text_key(value: str) -> str:
    """Normalize presentation whitespace without deleting semantic symbols.

    Keep spaces between word/number tokens: ``1 0`` is not ``10`` and ``in
    come`` is not ``income``. Spaces adjacent to operators or punctuation are
    presentation only. NFC preserves superscripts and other numeric syntax.
    """
    text = unicodedata.normalize("NFC", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"\s*([=+*/%,.;:，。；：()（）\[\]<>≤≥-])\s*", r"\1", text)


def require_complete_definition(raw: str, quote: str, *, retained_values=(),
                                instance_template: str | None = None,
                                instance_values: dict[str, str] | None = None) -> None:
    """Reject omitted definition content unless it is retained as a contract.

    Retention means an explicit, complete source value in type parameters or
    applicability scope. A matcher's invariant or an evidence attachment alone
    is not a semantic constraint and does not authorize dropping a qualifier.
    """
    if not quote or quote not in raw:
        raise ValueError("Definition quote is absent from the complete source field")
    if quote == raw or raw in retained_values:
        return
    # A source may explicitly separate its class declaration and observed
    # examples. Only a literal instance section containing actual slot values
    # qualifies. Merely supplying a wildcard for "only channel {x}" does not.
    if instance_template and instance_template.startswith(quote):
        tail = _INSTANCE_TAIL.fullmatch(instance_template[len(quote):])
        values = instance_values or {}
        if tail:
            names = _SLOT.findall(tail["body"])
            remainder = _SLOT.sub("", tail["body"])
            if (names and set(names) <= set(values)
                    and not remainder.strip(" \t\r\n,，;；、|/()（）[]【】.。")):
                rendered = _SLOT.sub(lambda match: values[match[1]], instance_template)
                if rendered == raw:
                    return
    raise ValueError(
        "Definition must retain the complete source field; omitted qualifiers "
        "must be explicit definition parameters, applicability scope or source-declared instance bindings")
