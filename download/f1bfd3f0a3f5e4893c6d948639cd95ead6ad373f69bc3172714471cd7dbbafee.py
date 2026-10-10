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
    "denotes": "points_to",
    "member_of": "related_to",
    "term_dimension_context": "related_to",
    "resource_measure_binding": "related_to",
    "resource_object_binding": "related_to",
    "resource_scope_binding": "related_to",
}
_DERIVED_CUES = {
    "calculation_dependency": ("calculation_dependency", "计算依赖"),
    "measure_binding": ("measure_binding", "度量绑定"),
    "business_object_binding": ("business_object_binding", "经营对象绑定"),
    "scope_constraint": ("scope_constraint", "维度约束", "范围约束"),
    "definition_reference": ("definition_reference", "定义引用", "引用定义"),
    "has_member": ("has_member", "包含成员"),
    "denotes": ("denotes", "指称"),
    "member_of": ("member_of", "属于成员集合"),
    "term_dimension_context": ("term_dimension_context", "指称成员所属维度"),
    "resource_measure_binding": ("resource_measure_binding", "配置度量"),
    "resource_object_binding": ("resource_object_binding", "配置经营对象"),
    "resource_scope_binding": ("resource_scope_binding", "配置维度"),
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
    "denotes": "源术语在声明的语境中指称目标对象；不声明两端身份相同。",
    "member_of": "源对象是目标维度或集合中的一个成员；不声明类继承。",
    "term_dimension_context": "源术语通过其指称的成员关联到该成员所属的目标维度；不声明术语等于维度或属于维度类型。",
    "resource_measure_binding": "源资源的定义或配置引用目标度量；不声明源资源是度量或具有该计算公式。",
    "resource_object_binding": "源资源的定义或配置明确绑定目标经营对象；不声明类继承或身份相同。",
    "resource_scope_binding": "源资源的定义或配置采用目标维度限制所引用内容的观察范围。",
}

# These restrictions apply even when a derived predicate is proposed through
# the generic record-relation path rather than its usual dedicated compiler.
_PREDICATE_ENDPOINTS = {
    "denotes": ({"Term"}, {"GeneralObject", "Measure", "Metric", "Dimension"}),
    "member_of": ({"GeneralObject", "Term", "Dimension"}, {"GeneralObject", "Dimension"}),
    "term_dimension_context": ({"Term"}, {"Dimension"}),
    "resource_measure_binding": ({"GeneralObject"}, {"Measure"}),
    "resource_object_binding": ({"GeneralObject"}, {"GeneralObject"}),
    "resource_scope_binding": ({"GeneralObject"}, {"Dimension"}),
}


def predicate_endpoint_errors(relation, object_types):
    """Validate the endpoint meaning of registered cross-root predicates."""
    contract = _PREDICATE_ENDPOINTS.get(relation.predicate_name)
    if contract is None:
        return []
    types = {item.id: item for item in object_types}

    def root(key):
        seen = set()
        while key in types and key not in seen:
            seen.add(key)
            key = types[key].parent
        return key

    if (len(relation.domain) != 1 or len(relation.range) != 1
            or root(relation.domain[0]) not in contract[0]
            or root(relation.range[0]) not in contract[1]):
        return ["Predicate endpoint roots disagree with the registered relation contract"]
    return []


def _classification_scope_property(item, key, data):
    """Only original field metadata can identify a classification attribute.

    A matching field name or a model-written type label is insufficient. The
    value must have a complete source proof and its schema must explicitly say
    category/classification. Tenant, version and unknown scopes stay strict.
    """
    if data is None:
        return False
    for prop in item.source_properties:
        if prop.role != "scope" or prop.source_column != key:
            continue
        table = data.tables.get(prop.source_table, {})
        column = next((value for value in table.get("columns", [])
                       if value.get("column_name") == key), {})
        description = str(column.get("column_comment") or "")
        if not re.search(r"分类|类别|\b(?:category|classification)\b", description, re.I):
            continue
        if re.search(r"租户|命名空间|版本|tenant|namespace|version", description, re.I):
            continue
        for eid in prop.evidence_ids:
            proof = data.evidence.get(eid, {})
            ref = proof.get("source_ref", {})
            if (proof.get("origin") == "observed_record" and not proof.get("raw_fragment_truncated")
                    and ref.get("snapshot_id") == data.snapshot_id
                    and ref.get("table") == prop.source_table and ref.get("column") == key
                    and str(proof.get("raw_fragment")) == str(item.applicability_scope[key])):
                return True
    return False


def relation_scope_conflicts(source, target, data=None):
    """Return incompatible scope keys without treating categories as namespaces.

    This permits relations between differently classified objects. It never
    merges their definitions, relaxes identity qualifiers, or proves a relation.
    """
    conflicts = []
    for key in source.applicability_scope.keys() & target.applicability_scope.keys():
        if source.applicability_scope[key] == target.applicability_scope[key]:
            continue
        if all(_classification_scope_property(item, key, data) for item in (source, target)):
            continue
        conflicts.append(key)
    for key in source.identity_qualifiers.keys() & target.identity_qualifiers.keys():
        if source.identity_qualifiers[key] != target.identity_qualifiers[key]:
            conflicts.append("identity:" + key)
    return sorted(conflicts)

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
