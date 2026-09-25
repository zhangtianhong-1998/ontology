"""Evidence-bundle decisions, separate from table types and physical join rules."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, field_validator

from .models import BuildPlan, Condition, DerivedType, RelationPlan, Strict
from .storage import digest
from .validation import validate_plan


ROOTS = ("GeneralObject", "Measure", "Metric", "Dimension", "Term")
OBJECT_RELATIONS = ("contains", "depends_on", "related_to", "points_to")


class RecordAlignmentDecision(Strict):
    record_id: str
    mapping_kind: Literal["exact", "narrower", "related", "unresolved"]
    quote: str


class ConceptBundleDecision(Strict):
    status: Literal["proposed", "no_change", "unresolved"]
    label: str = ""
    definition: str = ""
    root_type: Literal["GeneralObject", "Measure", "Metric", "Dimension", "Term"] | None = None
    # The table name is only a retrieval hint. A quantitative business type
    # needs a source-grounded reason for its Metric/Measure boundary.
    classification_basis: Literal["business_driven_metric", "aggregation_or_filter_measure",
                                  "other", "unresolved"] = "unresolved"
    classification_quote: str = ""
    # An accepted source-grounded concept is not automatically a class.
    # Existing responses without this field remain instance-level candidates.
    ontology_level: Literal["type", "instance", "unresolved"] = "unresolved"
    scope: dict[str, str] = Field(default_factory=dict)
    scope_roles: dict[str, Literal["applicability", "observation"]] = Field(default_factory=dict)
    alignments: list[RecordAlignmentDecision] = Field(default_factory=list)
    reason: str = ""

    @field_validator("scope", "scope_roles", mode="before")
    @classmethod
    def parse_object_string(cls, value):
        # Some OpenAI-compatible tool endpoints encode an object argument as
        # a JSON string. Decode only an actual JSON object, then retain strict
        # field validation; arbitrary prose is never accepted as a scope.
        if isinstance(value, str):
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise ValueError("Scope argument must be a JSON object")
            return parsed
        return value


class RelationBundleDecision(Strict):
    status: Literal["proposed", "no_change", "unresolved"]
    parent_relation: Literal["contains", "depends_on", "related_to", "points_to"] | None = None
    label: str = ""
    definition: str = ""
    source_quote: str = ""
    target_quote: str = ""
    reason: str = ""


class BundleReview(Strict):
    accepted: bool
    errors: list[str] = Field(default_factory=list)


def _entries(record):
    for role, values in record.get("fields", {}).items():
        for item in values:
            if isinstance(item, dict) and item.get("value") is not None:
                yield role, item


def _quote_evidence(data, record, quote, *, allowed_roles=None, excluded_columns=(),
                    require_complete=False):
    """Require a verbatim, visible source fragment before adding record evidence."""
    if not quote or not quote.strip():
        raise ValueError("Bundle decision lacks a nonempty source quote")
    for role, entry in _entries(record):
        value = str(entry["value"])
        column = entry["column"]
        if column in excluded_columns or (allowed_roles is not None and role not in allowed_roles):
            continue
        if quote not in value:
            continue
        if require_complete and entry.get("truncated"):
            continue
        evidence_id = "record:" + digest([data.snapshot_id, record["record_id"], column])[:24]
        data.evidence[evidence_id] = {
            "id": evidence_id,
            "origin": "observed_record",
            "raw_fragment": value,
            "raw_fragment_truncated": bool(entry.get("truncated")),
            "source_ref": {"table": record["table"], "record_id": record["record_id"],
                           "row": record.get("row_number"), "column": column,
                           "snapshot_id": data.snapshot_id},
        }
        return evidence_id
    raise ValueError("Quote is absent from the cited bundle record")


def _record_map(bundle):
    records = bundle.get("records", [])
    by_id = {record["record_id"]: record for record in records}
    if len(by_id) != len(records):
        raise ValueError("Duplicate record in evidence bundle")
    return by_id


def _member_or_field_record(record):
    tokens = set(record.get("table", "").casefold().replace(".", "_").split("_"))
    return bool(tokens & {"member", "field", "column"})


def _child_surface(table_name):
    tokens = set(table_name.casefold().replace(".", "_").split("_"))
    return bool(tokens & {"member", "field", "column", "param", "item", "value"})


_RELATION_QUOTE_ROLES = frozenset(("name", "alias", "description", "formula", "scope", "unit", "unknown", "context"))


def _supports_quote(record, quote, key_field):
    return bool(quote) and any(
        role in _RELATION_QUOTE_ROLES and entry["column"] != key_field
        and not entry.get("truncated")
        and quote in str(entry["value"])
        for role, entry in _entries(record))


def compile_concept(data, profile, bundle, decision, accepted_exact):
    """Validate a proposed business object and record-to-concept source mappings."""
    if decision.status != "proposed":
        return None
    if not decision.label.strip() or not decision.definition.strip() or decision.root_type is None:
        raise ValueError("Proposed concept lacks label, definition or root type")
    roots = {item["id"] for item in profile["object_roots"]}
    if decision.root_type not in roots:
        raise ValueError("Unknown concept root type")
    records = _record_map(bundle)
    if not records or not decision.alignments:
        raise ValueError("Concept has no source records")
    exact_allowlist = bundle.get("exact_alignment_record_ids")
    if exact_allowlist is not None:
        if (not isinstance(exact_allowlist, list) or not exact_allowlist
                or len(exact_allowlist) != len(set(exact_allowlist))
                or any(record_id not in records for record_id in exact_allowlist)):
            raise ValueError("Invalid exact alignment record allowlist")
    for key, value in decision.scope.items():
        if not any(record.get("scope", {}).get(key) == value for record in records.values()):
            raise ValueError("Concept scope is absent from the bundle records")
    if len({item.record_id for item in decision.alignments}) != len(decision.alignments):
        raise ValueError("Duplicate concept alignment for one record")
    exact_records, selected = [], []
    for item in decision.alignments:
        record = records.get(item.record_id)
        if record is None:
            raise ValueError("Concept alignment refers outside the evidence bundle")
        if item.mapping_kind == "unresolved":
            continue
        if item.mapping_kind == "exact":
            if exact_allowlist is not None and item.record_id not in exact_allowlist:
                raise ValueError("Exact alignment is outside the representative record allowlist")
            # Metric and Measure are distinguished by the definition's meaning,
            # not by a table-name hint. Other root mismatches remain guarded.
            hint = record.get("root_hint")
            if hint and hint != decision.root_type and not (
                    hint in ("Metric", "Measure")
                    and decision.root_type in ("Metric", "Measure")):
                raise ValueError("Exact source record root differs from proposed concept root")
            if _member_or_field_record(record):
                raise ValueError("Member or field record cannot be exact to its parent concept")
            if any(entry.get("truncated") for role, entry in _entries(record)
                   if role in ("name", "alias", "description", "formula", "unit", "scope")):
                raise ValueError("Exact concept alignment has truncated semantic fields")
            if any(record.get("scope", {}).get(key) != value
                   for key, value in decision.scope.items()):
                raise ValueError("Concept scope conflicts with an exact source record")
            exact_records.append(record)
        selected.append((item, record))
    if not exact_records:
        raise ValueError("New concept requires an exact source definition record")
    if decision.ontology_level == "type" and decision.root_type in ("Metric", "Measure"):
        expected = ("business_driven_metric" if decision.root_type == "Metric"
                    else "aggregation_or_filter_measure")
        if decision.classification_basis != expected:
            raise ValueError("Metric/Measure type requires a matching classification basis")
        if not decision.classification_quote.strip() or not any(
                role in ("description", "formula")
                and not entry.get("truncated")
                and decision.classification_quote in str(entry["value"])
                for record in exact_records for role, entry in _entries(record)):
            raise ValueError("Metric/Measure classification quote is absent from an exact definition")
    exact_scope = {}
    for record in exact_records:
        for key, value in (record.get("scope") or {}).items():
            if key in exact_scope and exact_scope[key] != value:
                raise ValueError("Conflicting scopes cannot share an exact concept")
            exact_scope[key] = value
    exact_units = {str(record.get("unit") or "").strip().casefold()
                   for record in exact_records if str(record.get("unit") or "").strip()}
    if len(exact_units) > 1:
        raise ValueError("Conflicting units cannot share an exact concept")
    unit = next(iter(exact_units), "")
    effective_scope = {**exact_scope, **decision.scope}
    if not set(decision.scope_roles) <= set(effective_scope):
        raise ValueError("Scope role names a field absent from exact source definitions")
    if decision.ontology_level == "type":
        if not all(record.get("kind") == "definition" for record in exact_records):
            raise ValueError("Type induction requires exact definition cards")
        if set(decision.scope_roles) != set(effective_scope):
            raise ValueError("Type induction requires every scope field to be classified")
        if "observation" in decision.scope_roles.values():
            raise ValueError("Observation coordinates cannot become type identity")
    normalized_label = " ".join(decision.label.casefold().split())
    normalized_definition = " ".join(decision.definition.casefold().split())
    concept_id = "concept:" + digest([data.snapshot_id, decision.root_type, normalized_label,
                                       normalized_definition, effective_scope, unit])[:24]
    applicability = {key: value for key, value in effective_scope.items()
                     if decision.scope_roles.get(key) == "applicability"}
    coordinates = {key: value for key, value in effective_scope.items()
                   if decision.scope_roles.get(key) == "observation"}
    type_id = ("type:" + digest([decision.root_type, normalized_label,
                                  normalized_definition, applicability, unit])[:24]
               if decision.ontology_level == "type" else None)
    alignments, all_evidence = [], set()
    for item, record in selected:
        if item.mapping_kind == "exact":
            prior = accepted_exact.get(item.record_id)
            if prior and prior != concept_id:
                raise ValueError("Record already has a conflicting exact concept")
        evidence_id = _quote_evidence(data, record, item.quote,
                                      allowed_roles=("name", "alias", "description", "formula",
                                                     "unit", "scope", "unknown"),
                                      require_complete=item.mapping_kind == "exact")
        all_evidence.add(evidence_id)
        alignments.append({
            "id": "alignment:" + digest([data.snapshot_id, item.record_id, concept_id, item.mapping_kind])[:24],
            "source_record_id": item.record_id,
            "source_card_id": record.get("card_id"),
            "concept_id": concept_id,
            "mapping_kind": item.mapping_kind,
            "scope": effective_scope,
            "evidence_ids": [evidence_id],
            "decision": "accepted_by_automatic_checks",
        })
    if not alignments:
        raise ValueError("All concept alignments are unresolved")
    property_sources = {}
    for record in exact_records:
        for role, entry in _entries(record):
            if role not in ("name", "alias", "description", "formula", "unit", "scope"):
                continue
            column = entry["column"]
            evidence_id = "record:" + digest([data.snapshot_id, record["record_id"], column])[:24]
            data.evidence[evidence_id] = {
                "id": evidence_id, "origin": "observed_record",
                "raw_fragment": str(entry["value"]), "raw_fragment_truncated": False,
                "source_ref": {"table": record["table"], "record_id": record["record_id"],
                               "row": record.get("row_number"), "column": column,
                               "snapshot_id": data.snapshot_id},
            }
            key = (role, record["table"], column)
            property_sources.setdefault(key, set()).add(evidence_id)
            all_evidence.add(evidence_id)
    source_properties = [
        {"role": role, "source_table": table, "source_column": column,
         "evidence_ids": sorted(evidence_ids)}
        for (role, table, column), evidence_ids in sorted(property_sources.items())
    ]
    concept = {"id": concept_id, "type": decision.root_type, "label": decision.label.strip(),
               "definition": decision.definition.strip(), "identity_scope": "snapshot_only",
               "ontology_level": decision.ontology_level,
               "ontology_type_id": type_id,
               "scope": effective_scope, "applicability_scope": applicability,
               "observation_coordinates": coordinates,
               "unclassified_scope": {key: value for key, value in effective_scope.items()
                                      if key not in decision.scope_roles},
               "unit": unit or None,
               "source_properties": source_properties,
               "source_refs": [{"record_id": item["source_record_id"],
                                "card_id": item.get("source_card_id"),
                                "scope": "representative_record; additional indexed rows remain in card_sources"}
                               for item in alignments],
               "evidence_ids": sorted(all_evidence),
               "decision": "accepted_by_automatic_checks"}
    return concept, alignments


def _compiled_object_type(concept):
    if not concept["ontology_type_id"]:
        return None
    return DerivedType(
        id=concept["ontology_type_id"], parent=concept["type"],
        label=concept["label"], definition=concept["definition"],
        category="business_type",
        evidence_ids=concept["evidence_ids"],
        evidence_scope="definition_record",
        applicability_scope=concept["applicability_scope"],
        unit=concept["unit"], derivation_kind="exact_definition",
        source_concept_ids=[concept["id"]],
        source_properties=concept["source_properties"],
    )


def _quoted_positive_pair(bundle, decision, source, target, source_field, target_field):
    records = _record_map(bundle)
    for pair in bundle.get("examples", {}).get("positive", []):
        left = records.get(pair.get("source_record_id"))
        right = records.get(pair.get("target_record_id"))
        raw = pair.get("matching_raw_value")
        if (left is None or right is None or left.get("table") != source["table"]
                or right.get("table") != target["table"] or raw in (None, "")):
            continue
        left_values = [str(entry["value"]) for _, entry in _entries(left)
                       if entry.get("column") == source_field]
        right_values = [str(entry["value"]) for _, entry in _entries(right)
                        if entry.get("column") == target_field]
        if (str(raw) in left_values and str(raw) in right_values
                and _supports_quote(left, decision.source_quote, source_field)
                and _supports_quote(right, decision.target_quote, target_field)):
            return left, right
    raise ValueError("No validated positive pair supports both cited quotes")


def compile_relation(data, profile, core, bundle, decision):
    """Turn a technically checked rule and quoted meaning into a validated plan."""
    if decision.status != "proposed":
        return None
    rule = bundle.get("rule") or {}
    if rule.get("status") != "checked_technical":
        raise ValueError("Relation bundle has no checked technical rule")
    if (bundle.get("snapshot_id") != data.snapshot_id or
            rule.get("snapshot_id") != data.snapshot_id or
            rule.get("verification", {}).get("scan_scope") != "full_input"):
        raise ValueError("Relation bundle is not verified on this complete snapshot")
    if rule.get("transform", {}).get("operator") != "identity":
        raise ValueError("Relation compiler only supports identity transforms")
    if decision.parent_relation not in OBJECT_RELATIONS or not decision.label.strip() or not decision.definition.strip():
        raise ValueError("Proposed relation lacks a supported kind, label or definition")
    source, target = rule["source"], rule["target"]
    if (decision.parent_relation == "contains"
            and _child_surface(source["table"])
            and not _child_surface(target["table"])):
        raise ValueError("Contains direction is reversed for child-to-parent source")
    counts = rule.get("verification", {}).get("checks") or {}
    eligible, unique = counts.get("eligible_references"), counts.get("unique_matches")
    if (type(eligible) is not int or eligible <= 0 or type(unique) is not int
            or unique != eligible or any(counts.get(key, 0) for key in (
                "ambiguous_matches", "missing_in_input", "missing_scope"))):
        raise ValueError("Checked technical rule has incomplete or inconsistent full-input counts")
    source_field, target_field = source.get("field"), target.get("field")
    if not source_field or not target_field:
        raise ValueError("Relation compiler only supports one key field")
    source_record, target_record = _quoted_positive_pair(
        bundle, decision, source, target, source_field, target_field)
    dependency_evidence = []
    if decision.parent_relation == "depends_on":
        # A key match and two names do not prove a calculation dependency.
        target_names = [str(entry["value"]) for role, entry in _entries(target_record)
                        if role in ("name", "alias") and not entry.get("truncated")]
        formula_text = [str(entry["value"]) for role, entry in _entries(source_record)
                        if role == "formula" and not entry.get("truncated")]
        supported = next(((name, formula) for name in target_names for formula in formula_text
                          if name and name in formula), None)
        if supported is None:
            raise ValueError("Dependency requires a source formula naming the target")
        dependency_evidence = [
            _quote_evidence(data, source_record, supported[1],
                            allowed_roles=("formula",), require_complete=True),
            _quote_evidence(data, target_record, supported[0],
                            allowed_roles=("name", "alias"), require_complete=True),
        ]

    known_types = ({item["id"] for item in profile["object_roots"]}
                   | {item.id for item in core.object_types})
    table_types = {item.table: item.object_type for item in core.tables}

    def endpoint_type(record):
        bound = table_types.get(record["table"])
        # The model judges meaning, not physical endpoint types. The source
        # mapping supplies the only executable table-wide endpoint binding.
        if bound not in known_types:
            raise ValueError("Relation endpoint type lacks a table-wide binding")
        return bound

    domain = endpoint_type(source_record)
    range_type = endpoint_type(target_record)
    evidence_ids = list(dict.fromkeys([
        f"schema:{source['table']}:{source_field}",
        f"schema:{target['table']}:{target_field}",
        _quote_evidence(data, source_record, decision.source_quote,
                        allowed_roles=_RELATION_QUOTE_ROLES, excluded_columns={source_field},
                        require_complete=True),
        _quote_evidence(data, target_record, decision.target_quote,
                        allowed_roles=_RELATION_QUOTE_ROLES, excluded_columns={target_field},
                        require_complete=True),
        *dependency_evidence,
    ]))
    relation_id = "relation:" + digest([decision.parent_relation, decision.label.casefold().strip(),
                                        source["table"], target["table"], domain, range_type])[:24]
    relation_type = DerivedType(id=relation_id, parent=decision.parent_relation,
                                definition=decision.definition.strip(), evidence_ids=evidence_ids,
                                label=decision.label.strip(), domain=[domain], range=[range_type],
                                endpoint_basis="table_binding",
                                evidence_scope="sample_semantic_with_full_technical_check")
    selectors = [Condition(op="eq", field=field, value=str(value))
                 for field, value in sorted((rule.get("selector") or {}).items())]
    selector = (selectors[0] if len(selectors) == 1 else
                Condition(op="and", children=selectors) if selectors else None)
    plan = RelationPlan(
        id="plan:" + digest([rule["rule_id"], relation_id, data.snapshot_id])[:24],
        source_table=source["table"], target_table=target["table"], mode="identifier",
        source_column=source_field, target_column=target_field,
        scope_bindings=rule.get("scope_bindings") or {}, selector=selector,
        predicate=relation_id, semantics="reference", evidence_ids=evidence_ids,
        evidence_scope="sample_semantic_with_full_technical_check",
        witness_snapshot_id=data.snapshot_id,
        witnessed_pairs=[{"source_record_id": source_record["record_id"],
                          "target_record_id": target_record["record_id"]}],
    )
    candidate = core.model_copy(deep=True)
    existing_types = {item.id: item for item in candidate.relation_types}
    existing_plans = {item.id: item for item in candidate.relations}
    if relation_id in existing_types and existing_types[relation_id] != relation_type:
        raise ValueError("Conflicting relation type ID")
    if plan.id in existing_plans and existing_plans[plan.id] != plan:
        raise ValueError("Conflicting relation plan ID")
    if relation_id not in existing_types:
        candidate.relation_types.append(relation_type)
    if plan.id not in existing_plans:
        candidate.relations.append(plan)
    errors = validate_plan(candidate, data, profile)
    if errors:
        raise ValueError("Group relation plan invalid: " + "; ".join(errors))
    return candidate, plan


def compile_business_relation(data, profile, core, bundle, decision, plan,
                              concepts, alignments):
    """Lift one reviewed record pair, never the whole-table execution plan."""
    rule = bundle["rule"]
    source, target = rule["source"], rule["target"]
    left, right = _quoted_positive_pair(
        bundle, decision, source, target, source["field"], target["field"])
    if (plan.witness_snapshot_id != data.snapshot_id
            or (left["record_id"], right["record_id"]) not in {
                (item.source_record_id, item.target_record_id)
                for item in plan.witnessed_pairs}):
        return None, None, "concept_pair_is_not_in_executable_relation_witnesses"
    exact = {item["source_record_id"]: item for item in alignments
             if item["mapping_kind"] == "exact"}
    source_alignment = exact.get(left["record_id"])
    target_alignment = exact.get(right["record_id"])
    if source_alignment is None or target_alignment is None:
        return None, None, "both_positive_records_require_exact_type_alignment"
    concept_by_id = {item["id"]: item for item in concepts}
    source_concept = concept_by_id.get(source_alignment["concept_id"])
    target_concept = concept_by_id.get(target_alignment["concept_id"])
    if (source_concept is None or target_concept is None
            or source_concept["id"] == target_concept["id"]
            or source_concept["ontology_level"] != "type"
            or target_concept["ontology_level"] != "type"):
        return None, None, "positive_record_alignment_lacks_distinct_business_types"
    object_types = {item.id: item for item in core.object_types}
    source_type = source_concept["ontology_type_id"]
    target_type = target_concept["ontology_type_id"]
    if (source_type not in object_types or target_type not in object_types
            or object_types[source_type].category != "business_type"
            or object_types[target_type].category != "business_type"):
        return None, None, "aligned_business_type_is_not_in_accepted_core"
    source_scope = source_concept["applicability_scope"]
    target_scope = target_concept["applicability_scope"]
    if any(source_scope[key] != target_scope[key] for key in source_scope.keys() & target_scope.keys()):
        return None, None, "business_type_applicability_scopes_conflict"
    base_type = next((item for item in core.relation_types if item.id == plan.predicate), None)
    if (base_type is None or plan.evidence_scope != "sample_semantic_with_full_technical_check"
            or base_type.evidence_scope != plan.evidence_scope):
        return None, None, "record_relation_lacks_accepted_semantic_evidence"
    evidence_ids = sorted(set(base_type.evidence_ids)
                          | set(source_alignment["evidence_ids"])
                          | set(target_alignment["evidence_ids"]))
    relation_id = "business_relation:" + digest([
        base_type.parent, base_type.label, base_type.definition,
        source_type, target_type])[:24]
    relation_type = DerivedType(
        id=relation_id, parent=base_type.parent, label=base_type.label,
        definition=base_type.definition, evidence_ids=evidence_ids,
        category="business_relation_type", domain=[source_type], range=[target_type],
        endpoint_basis="record_alignment",
        evidence_scope="one_positive_pair_with_exact_type_alignments",
    )
    candidate = core.model_copy(deep=True)
    existing = next((item for item in candidate.relation_types
                     if item.id == relation_id), None)
    if existing is None:
        candidate.relation_types.append(relation_type)
    elif (existing.parent != relation_type.parent
          or existing.definition != relation_type.definition
          or existing.domain != relation_type.domain
          or existing.range != relation_type.range
          or existing.category != relation_type.category):
        raise ValueError("Conflicting business relation type ID")
    else:
        merged = existing.model_copy(update={
            "evidence_ids": sorted(set(existing.evidence_ids) | set(evidence_ids))})
        candidate.relation_types[candidate.relation_types.index(existing)] = merged
    errors = validate_plan(candidate, data, profile)
    if errors:
        raise ValueError("Business relation type invalid: " + "; ".join(errors))
    assertion = {
        "id": "concept_relation:" + digest([
            data.snapshot_id, relation_id, source_concept["id"], target_concept["id"]])[:24],
        "subject": source_concept["id"], "predicate": relation_id,
        "object": target_concept["id"],
        "subject_type": source_type, "object_type": target_type,
        "source_record_pair": {"source": left["record_id"],
                               "target": right["record_id"]},
        "source_relation_plan_id": plan.id,
        "scope": dict(sorted({**target_scope, **source_scope}.items())),
        "identity_scope": "input_snapshot",
        "evidence_ids": evidence_ids,
        "decision": {"status": "accepted", "method": "same_positive_pair_exact_type_alignments",
                     "evidence_scope": "one_positive_pair_with_exact_type_alignments"},
    }
    return candidate, assertion, None


def _balanced_selection(bundles, limit):
    """Reserve bounded model calls for both concept and relation packets."""
    queues = {kind: [item for item in bundles if item.get("task_kind") == kind]
              for kind in ("concept_induction", "relation_meaning")}
    other = [item for item in bundles if item.get("task_kind") not in queues]
    selected = []
    while len(selected) < limit and any(queues.values()):
        for kind in queues:
            if queues[kind] and len(selected) < limit:
                selected.append(queues[kind].pop(0))
    return (selected + other[:max(0, limit - len(selected))])[:limit]


def _type_context(core, bundle, limit=12):
    """Prefer learned business types; only show physical types for packet tables."""
    tables = {record.get("table") for record in bundle.get("records", [])}
    rule = bundle.get("rule") or {}
    tables.add((rule.get("source") or {}).get("table"))
    tables.add((rule.get("target") or {}).get("table"))
    table_types = {item.object_type for item in core.tables if item.table in tables}
    related = [item for item in core.object_types if item.id in table_types]
    learned = [item for item in core.object_types if item.category == "business_type"
               and item.id not in table_types]
    slots = max(0, limit - len(related))
    chosen = [*(learned[-slots:] if slots else []), *related[:limit]]
    return [{"id": item.id, "parent": item.parent, "definition": item.definition,
             "category": item.category} for item in chosen]


async def construct_from_bundles(data, profile, core: BuildPlan, bundles, llm, *, review=True,
                                 max_bundles=20, max_repairs_per_bundle=0, progress=None):
    """One bounded group pass; failures do not change accepted plan or objects."""
    from .llm import BudgetExceeded

    if type(max_bundles) is not int or max_bundles < 0:
        raise ValueError("max_bundles must be nonnegative")
    if type(max_repairs_per_bundle) is not int or not 0 <= max_repairs_per_bundle <= 2:
        raise ValueError("max_repairs_per_bundle must be an integer in 0..2")
    accepted_exact, concepts, alignments, steps = {}, {}, [], []
    accepted_record_relations = []
    selected = _balanced_selection(bundles, max_bundles)
    skipped = max(0, len(bundles) - len(selected))
    stage = progress.task("语义组增量抽取", len(selected)) if progress else None
    if stage:
        stage.__enter__()
    try:
        for bundle in selected:
            step = {"bundle_id": bundle["bundle_id"], "task_kind": bundle["task_kind"],
                    "status": "unresolved", "core_before": digest(core.model_dump())}
            try:
                payload = {"bundle": bundle, "root_model": {
                    "object_roots": profile["object_roots"], "relation_roots": profile["relation_roots"]},
                    "current_types": _type_context(core, bundle),
                    "current_relations": [{"id": item.id, "parent": item.parent}
                                          for item in core.relation_types[-12:]]}
                if bundle["task_kind"] == "concept_induction":
                    concept_payload = payload
                    for attempt in range(max_repairs_per_bundle + 1):
                        decision = await llm.ask(
                            "concept_bundle", concept_payload, ConceptBundleDecision)
                        try:
                            compiled = compile_concept(
                                data, profile, bundle, decision, accepted_exact)
                            break
                        except ValueError as exc:
                            if attempt >= max_repairs_per_bundle:
                                raise
                            concept_payload = {
                                **payload, "previous_decision": decision.model_dump(),
                                "compiler_error": str(exc),
                                "repair_instruction": (
                                    "Correct only the cited validation error using this same "
                                    "evidence bundle. A proposed new concept requires an exact "
                                    "alignment to one of exact_alignment_record_ids with a "
                                    "verbatim quote; otherwise return unresolved. Do not invent "
                                    "records, scope, units, or facts."),
                            }
                            step["repair_calls"] = attempt + 1
                    if compiled:
                        concept, new_alignments = compiled
                        old = concepts.get(concept["id"])
                        if old and (old["definition"] != concept["definition"] or old["type"] != concept["type"]):
                            raise ValueError("Conflicting definitions for one concept ID")
                        if (old and old["ontology_level"] in ("type", "instance")
                                and concept["ontology_level"] in ("type", "instance")
                                and old["ontology_level"] != concept["ontology_level"]):
                            raise ValueError("Conflicting ontology levels for one concept ID")
                        if review:
                            check = await llm.ask("group_review", {**payload, "candidate": decision.model_dump()}, BundleReview)
                            if not check.accepted or check.errors:
                                raise ValueError("Group review rejected: " + "; ".join(check.errors))
                        candidate = core.model_copy(deep=True)
                        new_type = _compiled_object_type(concept)
                        if new_type is not None:
                            existing = next((item for item in candidate.object_types
                                             if item.id == new_type.id), None)
                            if existing is None:
                                candidate.object_types.append(new_type)
                            elif (existing.parent != new_type.parent
                                  or existing.definition != new_type.definition
                                  or existing.applicability_scope != new_type.applicability_scope):
                                raise ValueError("Conflicting derived object type ID")
                            else:
                                sources = {(item.role, item.source_table, item.source_column):
                                           item.model_dump() for item in existing.source_properties}
                                for item in new_type.source_properties:
                                    key = (item.role, item.source_table, item.source_column)
                                    if key in sources:
                                        sources[key]["evidence_ids"] = sorted(
                                            set(sources[key]["evidence_ids"]) | set(item.evidence_ids))
                                    else:
                                        sources[key] = item.model_dump()
                                merged = DerivedType.model_validate({**existing.model_dump(),
                                    "evidence_ids": sorted(set(existing.evidence_ids)
                                                           | set(new_type.evidence_ids)),
                                    "source_concept_ids": sorted(set(existing.source_concept_ids)
                                                                 | set(new_type.source_concept_ids)),
                                    "source_properties": [sources[key] for key in sorted(sources)],
                                })
                                candidate.object_types[candidate.object_types.index(existing)] = merged
                        errors = validate_plan(candidate, data, profile)
                        if errors:
                            raise ValueError("Group concept plan invalid: " + "; ".join(errors))
                        if old:
                            refs = {item["record_id"] for item in old["source_refs"]}
                            old["source_refs"].extend(ref for ref in concept["source_refs"]
                                                      if ref["record_id"] not in refs)
                            old["evidence_ids"] = sorted(set(old["evidence_ids"])
                                                         | set(concept["evidence_ids"]))
                            properties = {(item["role"], item["source_table"], item["source_column"]):
                                          item for item in old.get("source_properties", [])}
                            for item in concept["source_properties"]:
                                key = (item["role"], item["source_table"], item["source_column"])
                                if key in properties:
                                    properties[key]["evidence_ids"] = sorted(
                                        set(properties[key]["evidence_ids"]) | set(item["evidence_ids"]))
                                else:
                                    properties[key] = item
                            old["source_properties"] = [properties[key] for key in sorted(properties)]
                            if old["ontology_level"] == "unresolved":
                                old["ontology_level"] = concept["ontology_level"]
                                old["ontology_type_id"] = concept["ontology_type_id"]
                                old["applicability_scope"] = concept["applicability_scope"]
                                old["observation_coordinates"] = concept["observation_coordinates"]
                                old["unclassified_scope"] = concept["unclassified_scope"]
                        else:
                            concepts[concept["id"]] = concept
                        existing_alignments = {item["id"] for item in alignments}
                        alignments.extend(item for item in new_alignments
                                          if item["id"] not in existing_alignments)
                        for item in new_alignments:
                            if item["mapping_kind"] == "exact":
                                accepted_exact[item["source_record_id"]] = concept["id"]
                        core = candidate
                        step.update(status="accepted", concept_id=concept["id"],
                                    alignment_count=len(new_alignments))
                        if concept["ontology_type_id"]:
                            step["object_type_id"] = concept["ontology_type_id"]
                    else:
                        step.update(status=decision.status, reason=decision.reason)
                elif bundle["task_kind"] == "relation_meaning":
                    decision = await llm.ask("relation_bundle", payload, RelationBundleDecision)
                    compiled = compile_relation(data, profile, core, bundle, decision)
                    if compiled:
                        candidate, plan = compiled
                        if review:
                            check = await llm.ask("group_review", {**payload, "candidate": decision.model_dump()}, BundleReview)
                            if not check.accepted or check.errors:
                                raise ValueError("Group review rejected: " + "; ".join(check.errors))
                        core = candidate
                        accepted_record_relations.append((bundle, decision, plan))
                        step.update(status="accepted", relation_plan_id=plan.id)
                    else:
                        step.update(status=decision.status, reason=decision.reason)
                else:
                    raise ValueError("Unknown bundle task kind")
            except BudgetExceeded as exc:
                step.update(status="budget_exhausted", error_type=type(exc).__name__)
                steps.append(step)
                skipped += len(selected) - len(steps)
                break
            except Exception as exc:
                step.update(status="unresolved", error_type=type(exc).__name__,
                            reason=str(exc) if isinstance(exc, ValueError) else "See trace")
            step["core_after"] = digest(core.model_dump())
            steps.append(step)
            if stage:
                stage.advance(detail=step["status"])
    finally:
        if stage:
            stage.__exit__(None, None, None)
    concept_relations, relation_derivations = {}, []
    for bundle, decision, plan in accepted_record_relations:
        derivation = {"bundle_id": bundle["bundle_id"], "source_relation_plan_id": plan.id,
                      "status": "not_promoted", "core_before": digest(core.model_dump())}
        try:
            candidate, assertion, reason = compile_business_relation(
                data, profile, core, bundle, decision, plan,
                list(concepts.values()), alignments)
            if candidate is None:
                derivation["reason"] = reason
            else:
                core = candidate
                existing = concept_relations.get(assertion["id"])
                if existing:
                    existing["evidence_ids"] = sorted(set(existing["evidence_ids"])
                                                       | set(assertion["evidence_ids"]))
                else:
                    concept_relations[assertion["id"]] = assertion
                derivation.update(status="accepted", assertion_id=assertion["id"],
                                  business_relation_type_id=assertion["predicate"])
        except ValueError as exc:
            derivation["reason"] = str(exc)
        derivation["core_after"] = digest(core.model_dump())
        relation_derivations.append(derivation)
    statuses = {status: sum(item["status"] == status for item in steps)
                for status in ("accepted", "no_change", "unresolved", "budget_exhausted")}
    coverage = {"bundles_available": len(bundles), "bundles_selected": len(selected),
                "bundles_not_attempted": skipped, "statuses": statuses,
                "partial": bool(skipped or statuses["unresolved"] or statuses["budget_exhausted"])}
    return {"plan": core, "concepts": list(concepts.values()), "record_alignments": alignments,
            "concept_relations": list(concept_relations.values()),
            "concept_relation_derivations": relation_derivations,
            "steps": steps, "bundles_selected": len(selected), "bundles_skipped": skipped,
            "coverage": coverage, "partial": coverage["partial"]}
