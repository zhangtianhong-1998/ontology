"""Stable names for evidence-bounded object relation types.

A model may choose one of the user-defined relation roots, but its prose must
never become a predicate identifier.  The ordered endpoint signature is part
of the identity; reversing a relation creates a different candidate.
"""

from __future__ import annotations

import hashlib
import json
import re
import ast
import unicodedata


RELATION_VERBS = {
    "contains": "contains",
    "depends_on": "depends_on",
    "related_to": "related_to",
    "points_to": "points_to",
}

# Derived names are part of a small contract, not model-authored predicate prose.
DERIVED_PREDICATES = {
    "calculation_dependency": "depends_on",
    "measure_binding": "depends_on",
    "business_object_binding": "related_to",
    "scope_constraint": "related_to",
    "definition_reference": "points_to",
    "has_member": "contains",
}
_DERIVED_CUES = {
    "calculation_dependency": ("calculation_dependency", "计算依赖"),
    "measure_binding": ("measure_binding", "度量绑定"),
    "business_object_binding": ("business_object_binding", "经营对象绑定"),
    "scope_constraint": ("scope_constraint", "维度约束", "范围约束"),
    "definition_reference": ("definition_reference", "定义引用", "引用定义"),
    "has_member": ("has_member", "包含成员"),
}
_OPERAND_ROLES = frozenset(("minuend", "subtrahend", "numerator", "denominator",
                           "addend", "factor"))
_PREDICATE_DEFINITIONS = {
    "contains": "源对象包含目标对象。",
    "depends_on": "源对象依赖目标对象。",
    "related_to": "源对象与目标对象有关联。",
    "points_to": "源对象指向目标对象。",
    "calculation_dependency": "源对象的计算依赖目标操作数。",
    "measure_binding": "指标采用目标通用度量；此关系本身不声明计算公式。",
    "business_object_binding": "指标绑定目标经营对象类型。",
    "scope_constraint": "源对象与目标对象之间存在范围约束。",
    "definition_reference": "源对象引用目标对象的定义。",
    "has_member": "目标对象是源对象的成员。",
}

_CUES = {
    "contains": ("包含", "含有", "contains"),
    "depends_on": ("依赖", "取决于", "depends on", "depends_on"),
    "related_to": ("关联", "相关于", "related to", "related_to"),
    "points_to": ("指向", "引用", "points to", "points_to", "references"),
}


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def canonical_relation_label(parent: str, predicate_name: str | None = None) -> str:
    """Validate a root or a registered derived predicate and return its name."""
    try:
        root = RELATION_VERBS[parent]
    except KeyError as exc:
        raise ValueError("Unknown object relation root") from exc
    if predicate_name in (None, "", root):
        return root
    if DERIVED_PREDICATES.get(predicate_name) != parent:
        raise ValueError("Unknown derived predicate or incompatible relation root")
    return predicate_name


def relation_semantic_parameters(parent, predicate_name=None, parameters=None):
    """Only reproducible calculation roles currently qualify relation identity."""
    name = canonical_relation_label(parent, predicate_name)
    parameters = parameters or {}
    if not isinstance(parameters, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in parameters.items()):
        raise ValueError("Relation semantic parameters must be a string mapping")
    if parameters and (name != "calculation_dependency"
                       or set(parameters) != {"operand_role"}
                       or parameters["operand_role"] not in _OPERAND_ROLES):
        raise ValueError("Unsupported predicate semantic parameters")
    return dict(sorted(parameters.items()))


def canonical_relation_definition(parent, predicate_name=None, parameters=None):
    """Keep type semantics stable; individual model explanations are evidence."""
    name = canonical_relation_label(parent, predicate_name)
    parameters = relation_semantic_parameters(parent, predicate_name, parameters)
    definition = _PREDICATE_DEFINITIONS[name]
    if parameters:
        definition += " 运算角色：" + parameters["operand_role"] + "。"
    return definition


def merge_relation_type_evidence(existing, proposed):
    """Only evidence may accumulate; every declared semantic field must agree."""
    if existing.model_dump(exclude={"evidence_ids"}) != proposed.model_dump(exclude={"evidence_ids"}):
        raise ValueError("Conflicting relation type ID")
    return existing.model_copy(update={
        "evidence_ids": sorted(set(existing.evidence_ids) | set(proposed.evidence_ids))})


def validate_calculation_parameter_evidence(parameters, formulas, target_names):
    """Prove an operand role from a parsed binary expression, never prose."""
    if not parameters:
        return
    wanted = parameters["operand_role"]
    side_roles = {ast.Sub: ("minuend", "subtrahend"),
                  ast.Div: ("numerator", "denominator"),
                  ast.Add: ("addend", "addend"), ast.Mult: ("factor", "factor")}
    names = {str(value).strip() for value in target_names}
    for raw in formulas:
        expression = unicodedata.normalize("NFKC", raw).strip()
        if "=" in expression:
            expression = expression.split("=", 1)[1].strip()
        try:
            tree = ast.parse(expression, mode="eval")
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or type(node.op) not in side_roles:
                continue
            for side, role in zip((node.left, node.right), side_roles[type(node.op)]):
                if role == wanted and isinstance(side, ast.Name) and side.id in names:
                    return
    raise ValueError("Calculation operand role lacks parsed source formula evidence")


def canonical_relation_id(parent: str, domain: str, range_type: str, *,
                          namespace: str = "business_relation",
                          qualifier: list[str] | tuple[str, ...] = (),
                          predicate_name: str | None = None,
                          semantic_parameters: dict[str, str] | None = None) -> str:
    """Generate a reproducible machine ID from a directed type signature.

    A qualifier is only for a source-record plan. Business predicate identity
    instead distinguishes registered derived names and evidenced operand roles.
    Legacy root-only signatures retain their IDs.
    """
    name = canonical_relation_label(parent, predicate_name)
    parameters = relation_semantic_parameters(parent, predicate_name, semantic_parameters)
    if namespace not in ("business_relation", "relation"):
        raise ValueError("Unknown relation namespace")
    if not domain or not range_type or not isinstance(domain, str) or not isinstance(range_type, str):
        raise ValueError("Relation requires one ordered endpoint on each side")
    if not isinstance(qualifier, (list, tuple)) or not all(
            isinstance(item, str) and item for item in qualifier):
        raise ValueError("Relation qualifier must be a list of nonempty strings")
    if namespace == "business_relation" and qualifier:
        raise ValueError("Business relation identity cannot depend on a source rule")
    signature = [parent, domain, range_type, list(qualifier)]
    if name != parent or parameters:
        signature.extend([name, parameters])
    payload = json.dumps(signature,
                         ensure_ascii=False, separators=(",", ":"))
    token = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"{namespace}:{parent}:{token}"


def validate_proposed_relation_label(label: str, parent: str, *,
                                     subject_label: str | None = None,
                                     object_label: str | None = None,
                                     predicate_name: str | None = None) -> None:
    """Reject incompatible model labels before replacing them with a verb.

    With known business endpoints only a bare verb or the exact directed
    ``subject + verb + object`` form is accepted. This is deliberately stricter
    than matching a cue somewhere in a free-form sentence.
    """
    name = canonical_relation_label(parent, predicate_name)
    value = _normalized(label)
    if not value or len(value) > 96 or re.search(r"[\n\r:;；。]", value):
        raise ValueError("Relation label must be a short predicate phrase")
    # The machine predicate is already validated above. A root's controlled
    # display verb may describe its registered subtype without renaming it.
    cues = (*_DERIVED_CUES[name], *_CUES[parent]) if name != parent else _CUES[parent]
    allowed_cues = {_normalized(item) for item in cues}
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
