"""Stable names for evidence-bounded object relation types.

A model may choose one of the user-defined relation roots, but its prose must
never become a predicate identifier.  The ordered endpoint signature is part
of the identity; reversing a relation creates a different candidate.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata


RELATION_VERBS = {
    "contains": "contains",
    "depends_on": "depends_on",
    "related_to": "related_to",
    "points_to": "points_to",
}

_CUES = {
    "contains": ("包含", "含有", "contains"),
    "depends_on": ("依赖", "取决于", "depends on", "depends_on"),
    "related_to": ("关联", "相关于", "related to", "related_to"),
    "points_to": ("指向", "引用", "points to", "points_to", "references"),
}


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def canonical_relation_label(parent: str) -> str:
    """Use the user's English root predicate instead of model-authored prose."""
    try:
        return RELATION_VERBS[parent]
    except KeyError as exc:
        raise ValueError("Unknown object relation root") from exc


def canonical_relation_id(parent: str, domain: str, range_type: str, *,
                          namespace: str = "business_relation",
                          qualifier: list[str] | tuple[str, ...] = ()) -> str:
    """Generate a reproducible machine ID from a directed type signature.

    A qualifier is only for a source-record plan: distinct physical rules can
    connect the same table types. Business relations intentionally omit it, so
    two claimed meanings for one root/signature must conflict or be merged.
    """
    canonical_relation_label(parent)
    if namespace not in ("business_relation", "relation"):
        raise ValueError("Unknown relation namespace")
    if not domain or not range_type or not isinstance(domain, str) or not isinstance(range_type, str):
        raise ValueError("Relation requires one ordered endpoint on each side")
    if not isinstance(qualifier, (list, tuple)) or not all(
            isinstance(item, str) and item for item in qualifier):
        raise ValueError("Relation qualifier must be a list of nonempty strings")
    if namespace == "business_relation" and qualifier:
        raise ValueError("Business relation identity cannot depend on a source rule")
    payload = json.dumps([parent, domain, range_type, list(qualifier)],
                         ensure_ascii=False, separators=(",", ":"))
    token = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"{namespace}:{parent}:{token}"


def validate_proposed_relation_label(label: str, parent: str, *,
                                     subject_label: str | None = None,
                                     object_label: str | None = None) -> None:
    """Reject incompatible model labels before replacing them with a verb.

    With known business endpoints only a bare verb or the exact directed
    ``subject + verb + object`` form is accepted. This is deliberately stricter
    than matching a cue somewhere in a free-form sentence.
    """
    canonical_relation_label(parent)
    value = _normalized(label)
    if not value or len(value) > 96 or re.search(r"[\n\r:;；。]", value):
        raise ValueError("Relation label must be a short predicate phrase")
    allowed_cues = {_normalized(item) for item in _CUES[parent]}
    if subject_label is not None and object_label is not None:
        subject, object_ = _normalized(subject_label), _normalized(object_label)
        if not subject or not object_:
            raise ValueError("Relation label endpoint names are missing")
        allowed = allowed_cues | {
            subject + cue + object_ for cue in allowed_cues
        }
        if value not in allowed:
            raise ValueError("Relation label is not a canonical directed predicate")
        return
    if value not in allowed_cues:
        raise ValueError("Relation label must name the selected root predicate")
