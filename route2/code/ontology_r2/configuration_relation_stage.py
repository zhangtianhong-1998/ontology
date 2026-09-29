"""Conservative semantic adjudication of two-ended configuration references.

Code equality can identify two accepted definition records. It does not name
their business relation. This stage needs an explicit, directional phrase in
the configuration row plus complete definition quotes from both endpoints.
The configuration record stays a witness, never an object endpoint.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import Field, field_validator

from .configuration_relations import _check_column, _type_alignments
from .models import BuildPlan, DerivedType, Strict, json_null_placeholder
from .relation_contract import (canonical_relation_id, canonical_relation_label,
                                relation_semantic_parameters,
                                validate_calculation_parameter_evidence,
                                validate_proposed_relation_label)
from .storage import digest, qi
from .validation import validate_plan


_CUES = {
    "contains": ("包含", "含有", "contains"),
    "depends_on": ("依赖", "取决于", "depends on"),
    "related_to": ("关联", "相关于", "related to"),
    "points_to": ("指向", "引用", "points to", "references"),
}


class ConfigurationRelationDecision(Strict):
    status: Literal["proposed", "no_change", "unresolved"]
    direction: Literal["source_to_target", "target_to_source"] | None = None
    parent_relation: Literal["contains", "depends_on", "related_to", "points_to"] | None = None
    predicate_name: str | None = None
    semantic_parameters: dict[str, str] = Field(default_factory=dict)
    assertion_status: Literal["affirmed", "negated", "conditional", "inactive", "uncertain"] = "affirmed"
    qualifier_quote: str = ""
    label: str = ""
    definition: str = ""
    configuration_quote: str = ""
    source_definition_quote: str = ""
    target_definition_quote: str = ""
    reason: str = ""

    @field_validator("direction", "parent_relation", "predicate_name", mode="before")
    @classmethod
    def parse_nullable_tool_fields(cls, value):
        return json_null_placeholder(value)


def _norm(value):
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _fragment_for_exact_record(data, business_type, record_id, quote):
    if not quote or not quote.strip():
        raise ValueError("Endpoint decision lacks a full-definition quote")
    for prop in business_type.source_properties:
        if prop.role not in ("description", "formula"):
            continue
        for evidence_id in prop.evidence_ids:
            evidence = data.evidence.get(evidence_id) or {}
            source = evidence.get("source_ref") or {}
            fragment = evidence.get("raw_fragment")
            if (evidence.get("origin") == "observed_record"
                    and not evidence.get("raw_fragment_truncated")
                    and source.get("snapshot_id") == data.snapshot_id
                    and source.get("record_id") == record_id
                    and source.get("table") == prop.source_table
                    and source.get("column") == prop.source_column
                    and isinstance(fragment, str) and quote.strip() == fragment.strip()):
                return evidence_id
    raise ValueError("Endpoint quote is absent from its exact complete definition record")


def _explicit_direction(quote, first_anchors, second_anchors, relation, direction,
                        predicate_name=None):
    """Accept a complete positive sentence; unmatched modifiers stay unresolved.

    Substring endpoint ordering loses negation and conditions. Until condition
    compilation exists, accepting only a full supported clause is deliberate.
    """
    compact = lambda value: re.sub(r"\s+", "", _norm(value)).rstrip("。.!！")
    phrase = compact(quote)
    name = canonical_relation_label(relation, predicate_name)
    from .relation_contract import _DERIVED_CUES
    cues = _DERIVED_CUES[name] if name != relation else _CUES[relation]
    pairs = ((first_anchors, second_anchors) if direction == "source_to_target"
             else (second_anchors, first_anchors))
    allowed = {compact(first) + compact(cue) + compact(second)
               for first in pairs[0] for second in pairs[1] for cue in cues
               if first and second and len(compact(first)) >= 2 and len(compact(second)) >= 2}
    return phrase in allowed


def _read_witness(data, candidate):
    table_name = candidate.get("configuration_table")
    table = data.tables.get(table_name)
    row_number = candidate.get("configuration_witness_row_number")
    if table is None or type(row_number) is not int or row_number < 1:
        raise ValueError("Configuration witness table or row is invalid")
    cursor = data.db.execute(
        f"SELECT * FROM {qi(table['sql_name'])} WHERE __r2_row=?", [row_number])
    names = [item[0] for item in cursor.description]
    raw = cursor.fetchone()
    if raw is None:
        raise ValueError("Configuration witness row is absent")
    row = dict(zip(names, raw))
    if data.record_id(table_name, row) != candidate.get("configuration_record_id"):
        raise ValueError("Configuration witness record identity differs from the snapshot")
    for side in ("source", "target"):
        column = candidate.get(side + "_code_column")
        if column not in table["column_names"] or row[column] != candidate.get(side + "_code"):
            raise ValueError("Configuration witness code differs from its candidate")
        _check_column(data, table_name, column)
    return row


def _endpoint(data, plan, candidate, side, aligned):
    stated = candidate.get(side + "_definition") or {}
    table_name, code_column = stated.get("table"), stated.get("code_column")
    code = candidate.get(side + "_code")
    if (table_name not in data.tables or code_column not in data.tables[table_name]["column_names"]
            or code in (None, "")):
        raise ValueError("Definition endpoint lacks a valid code binding")
    _check_column(data, table_name, code_column)
    rows = data.lookup(table_name, ((code_column, code), *sorted(stated.get("lookup_scope", {}).items())))
    if len(rows) != 1:
        raise ValueError("Definition code is missing or ambiguous in the full snapshot")
    record_id = data.record_id(table_name, rows[0])
    matched = aligned.get(record_id)
    type_id = candidate.get(side + "_type_id")
    if (record_id != stated.get("record_id") or matched is None
            or matched["type_id"] != type_id
            or matched.get("snapshot_id") not in (None, data.snapshot_id)
            or matched["concept_id"] != stated.get("concept_id")):
        raise ValueError("Definition row lacks the cited current accepted business type binding")
    business_type = next((item for item in plan.object_types
                          if item.id == type_id and item.category == "business_type"), None)
    if business_type is None:
        raise ValueError("Configuration endpoint is not an accepted business type")
    return business_type, matched, record_id


def compile_configuration_relation(data, profile, core: BuildPlan, candidate,
                                   decision: ConfigurationRelationDecision,
                                   concepts, alignments):
    """Atomically add one business relation and one witnessed concept assertion."""
    decision = ConfigurationRelationDecision.model_validate(decision)
    if decision.status != "proposed":
        return None
    if candidate.get("status") != "endpoint_verified_candidate":
        raise ValueError("Only endpoint-verified configuration candidates can be compiled")
    if candidate.get("snapshot_id") != data.snapshot_id:
        raise ValueError("Configuration candidate snapshot differs from the input")
    if candidate.get("predicate_status") != "unjudged":
        raise ValueError("Configuration candidate predicate status is not unjudged")
    if (decision.direction not in ("source_to_target", "target_to_source")
            or decision.parent_relation not in _CUES
            or not decision.label.strip() or not decision.definition.strip()):
        raise ValueError("Configuration relation lacks direction, root, label or definition")
    relation_roots = {item["id"] for item in profile["relation_roots"]
                      if item.get("kind") == "object"}
    if decision.parent_relation not in relation_roots:
        raise ValueError("Configuration relation root is not in the profile")
    core = BuildPlan.model_validate(core)
    aligned, conflicting = _type_alignments(core, concepts, alignments)
    source_type, source_match, source_record = _endpoint(data, core, candidate, "source", aligned)
    target_type, target_match, target_record = _endpoint(data, core, candidate, "target", aligned)
    if source_record in conflicting or target_record in conflicting:
        raise ValueError("An endpoint has conflicting exact type alignments")
    source_definition_evidence = _fragment_for_exact_record(
        data, source_type, source_record, decision.source_definition_quote)
    target_definition_evidence = _fragment_for_exact_record(
        data, target_type, target_record, decision.target_definition_quote)
    common = source_type.applicability_scope.keys() & target_type.applicability_scope.keys()
    if any(source_type.applicability_scope[key] != target_type.applicability_scope[key]
           for key in common):
        raise ValueError("Configuration endpoints have conflicting applicability scopes")

    witness = _read_witness(data, candidate)
    text_column = candidate.get("relation_text_column")
    if not text_column or text_column not in data.tables[candidate["configuration_table"]]["column_names"]:
        raise ValueError("Configuration row has no explicit relationship text")
    _check_column(data, candidate["configuration_table"], text_column)
    literal = witness.get(text_column)
    if (not isinstance(literal, str) or not literal.strip()
            or len(literal) > 1024 or literal != candidate.get("predicate_literal")
            or not decision.configuration_quote.strip()
            or decision.configuration_quote not in literal):
        raise ValueError("Configuration relationship text is absent, changed or over budget")
    if decision.assertion_status != "affirmed" or decision.qualifier_quote:
        raise ValueError("Configuration negation, condition or inactive status requires an explicit supported constraint")
    if _norm(decision.configuration_quote) != _norm(literal):
        raise ValueError("Configuration quote must preserve the entire statement and its qualifiers")
    if not _explicit_direction(
        literal,
        (source_type.label, candidate.get("source_code")),
        (target_type.label, candidate.get("target_code")),
        decision.parent_relation, decision.direction, decision.predicate_name,
    ):
        raise ValueError("Configuration text does not prove the proposed predicate and direction")
    config_evidence = "record:" + digest([
        data.snapshot_id, candidate["configuration_record_id"], text_column])[:24]
    saved = data.evidence.get(config_evidence) or {}
    if (saved.get("origin") != "observed_record"
            or saved.get("raw_fragment_truncated")
            or saved.get("raw_fragment") != literal
            or saved.get("source_ref", {}).get("record_id") != candidate["configuration_record_id"]):
        raise ValueError("Configuration phrase lacks complete witness evidence")

    ordered = ((source_type, target_type, source_match, target_match,
                source_record, target_record)
               if decision.direction == "source_to_target" else
               (target_type, source_type, target_match, source_match,
                target_record, source_record))
    subject_type, object_type, subject_match, object_match, subject_record, object_record = ordered
    validate_proposed_relation_label(
        decision.label, decision.parent_relation,
        subject_label=subject_type.label, object_label=object_type.label,
        predicate_name=decision.predicate_name)
    semantic_parameters = relation_semantic_parameters(
        decision.parent_relation, decision.predicate_name, decision.semantic_parameters)
    if semantic_parameters:
        formulas = [data.evidence[evidence_id]["raw_fragment"]
                    for prop in subject_type.source_properties if prop.role == "formula"
                    for evidence_id in prop.evidence_ids if evidence_id in data.evidence
                    and not data.evidence[evidence_id].get("raw_fragment_truncated")]
        validate_calculation_parameter_evidence(semantic_parameters, formulas,
                                                [object_type.label])
    evidence_ids = sorted(set(candidate.get("evidence_ids", [])
                              + [config_evidence, source_definition_evidence,
                                 target_definition_evidence]
                              + source_match["evidence_ids"] + target_match["evidence_ids"]))
    if not set(evidence_ids) <= data.evidence.keys():
        raise ValueError("Configuration relation evidence is missing")
    relation_id = canonical_relation_id(
        decision.parent_relation, subject_type.id, object_type.id,
        predicate_name=decision.predicate_name, semantic_parameters=semantic_parameters)
    relation_type = DerivedType(
        id=relation_id, parent=decision.parent_relation,
        label=canonical_relation_label(decision.parent_relation, decision.predicate_name),
        predicate_name=decision.predicate_name, semantic_parameters=semantic_parameters,
        definition=decision.definition.strip(),
        category="business_relation_type", domain=[subject_type.id],
        range=[object_type.id], endpoint_basis="configuration_reference",
        evidence_scope="one_configuration_witness_with_exact_type_alignments",
        evidence_ids=evidence_ids,
    )
    proposed = core.model_copy(deep=True)
    existing = next((item for item in proposed.relation_types if item.id == relation_id), None)
    if existing:
        if (existing.parent != relation_type.parent or existing.label != relation_type.label
                or existing.definition != relation_type.definition
                or existing.domain != relation_type.domain or existing.range != relation_type.range
                or existing.endpoint_basis != relation_type.endpoint_basis
                or existing.evidence_scope != relation_type.evidence_scope):
            raise ValueError("Configuration relation conflicts with an existing type")
        proposed.relation_types[proposed.relation_types.index(existing)] = existing.model_copy(update={
            "evidence_ids": sorted(set(existing.evidence_ids) | set(evidence_ids))})
    else:
        proposed.relation_types.append(relation_type)
    errors = validate_plan(proposed, data, profile)
    if errors:
        raise ValueError("Configuration relation plan invalid: " + "; ".join(errors))
    assertion = {
        "id": "concept_relation:" + digest([
            data.snapshot_id, relation_id, subject_match["concept_id"],
            object_match["concept_id"], candidate["configuration_record_id"]])[:24],
        "subject": subject_match["concept_id"], "predicate": relation_id,
        "object": object_match["concept_id"],
        "subject_type": subject_type.id, "object_type": object_type.id,
        "source_record_pair": {"source": subject_record, "target": object_record},
        "configuration_witness_record_id": candidate["configuration_record_id"],
        "configuration_candidate_id": candidate["id"],
        "configuration_text": literal, "assertion_status": "affirmed",
        "scope": {**object_type.applicability_scope, **subject_type.applicability_scope},
        "identity_scope": "input_snapshot", "evidence_ids": evidence_ids,
        "decision": {"status": "accepted", "method": "explicit_configuration_phrase_and_exact_type_alignments",
                     "direction": decision.direction,
                     "evidence_scope": "one_configuration_witness_with_exact_type_alignments"},
    }
    return proposed, assertion


async def adjudicate_configuration_relations(data, profile, core, candidates,
                                             concepts, alignments, llm, *, max_candidates=20,
                                             checked_rules=(), memberships=()):
    """Spend at most one model decision per candidate, retaining every failure."""
    if type(max_candidates) is not int or max_candidates < 0:
        raise ValueError("max_candidates must be a nonnegative integer")
    from .llm import BudgetExceeded

    selected = [item for item in candidates if item.get("status") == "endpoint_verified_candidate"]
    chosen = selected[:max_candidates]
    steps, assertions, bindings = [], [], []
    plan = BuildPlan.model_validate(core)
    for item in chosen:
        step = {"candidate_id": item["id"], "status": "unresolved",
                "configuration_text": item.get("predicate_literal")}
        try:
            if item.get("snapshot_id") != data.snapshot_id:
                raise ValueError("Configuration candidate snapshot differs from the input")
            witness = _read_witness(data, item)
            if any(witness.get(field) != value for field, value in item.get("selector", {}).items()):
                raise ValueError("Configuration witness does not satisfy its condition")
            # Structured purposes describe the config's function, independently
            # of endpoint names. No natural-language A-depends-on-B column is needed.
            if checked_rules:
                from .semantic_bindings import (structured_configuration_purpose,
                                                compile_structured_configuration_binding,
                                                configuration_purpose_packet, checked_purpose_decision,
                                                ConfigurationPurposeDecision)
                purpose = structured_configuration_purpose(data, item, witness)
                packet = configuration_purpose_packet(data, item, witness, plan, checked_rules)
                if purpose is None:
                    if packet:
                        decision = await llm.ask("configuration_purpose", packet, ConfigurationPurposeDecision)
                        step["llm_calls"] = 1
                        purpose = checked_purpose_decision(packet, decision)
                        step["purpose_decision"] = decision.model_dump()
                        if purpose is None:
                            step.update(status=decision.status, reason=decision.reason)
                            steps.append(step)
                            continue
                    elif not item.get("relation_text_column"):
                        raise ValueError("No eligible checked semantic reference paths; numeric overlap, "
                                         "configuration identities or unsupported endpoint roots are not binding evidence")
                if purpose:
                    aligned, _ = _type_alignments(plan, concepts, alignments, memberships)
                    plan, binding = compile_structured_configuration_binding(
                        data, profile, plan, item, witness, purpose, checked_rules, aligned)
                    bindings.append(binding)
                    step.update(status="accepted", relation_type_id=binding["relation_type_id"],
                                binding_id=binding["id"], method="checked_structured_configuration",
                                llm_calls=step.get("llm_calls", 0))
                    steps.append(step)
                    continue
            literal = item.get("predicate_literal")
            text_column = item.get("relation_text_column")
            if not text_column or text_column not in data.tables[item["configuration_table"]]["column_names"]:
                raise ValueError("No explicit configuration relationship text column")
            _check_column(data, item["configuration_table"], text_column)
            if (not isinstance(literal, str) or not literal.strip() or len(literal) > 1024
                    or witness[text_column] != literal):
                raise ValueError("No bounded explicit configuration relationship text")
            types = {value.id: value for value in plan.object_types}
            source_type = types[item["source_type_id"]]
            target_type = types[item["target_type_id"]]
            payload = {
                "candidate_id": item["id"], "configuration_text": literal,
                "configuration_witness": item["configuration_record_id"],
                "endpoint_a": {"type_id": source_type.id, "label": source_type.label,
                               "definition": source_type.definition[:1024],
                               "definition_evidence": _definition_previews(data, source_type,
                                   item["source_definition"]["record_id"])},
                "endpoint_b": {"type_id": target_type.id, "label": target_type.label,
                               "definition": target_type.definition[:1024],
                               "definition_evidence": _definition_previews(data, target_type,
                                   item["target_definition"]["record_id"])},
                "object_relation_roots": [value for value in profile["relation_roots"]
                                          if value.get("kind") == "object"],
                "contract": "Configuration row is a witness, never a business endpoint. "
                            "Quote the entire statement, retaining negation, conditions and validity. "
                            "Return unresolved for negated, conditional, expired, inactive or uncertain "
                            "claims; only a complete positive unqualified clause can be compiled. "
                            "Choose a registered predicate name, never free-form prose; operand roles "
                            "must be proved by an exact formula, not inferred from endpoint order.",
            }
            if not payload["endpoint_a"]["definition_evidence"] or not payload["endpoint_b"]["definition_evidence"]:
                raise ValueError("An endpoint has no complete definition evidence in the current snapshot")
            decision = await llm.ask("configuration_relation", payload,
                                     ConfigurationRelationDecision)
            compiled = compile_configuration_relation(
                data, profile, plan, item, decision, concepts, alignments)
            if compiled:
                plan, assertion = compiled
                assertions.append(assertion)
                step.update(status="accepted", relation_type_id=assertion["predicate"],
                            assertion_id=assertion["id"])
            else:
                step.update(status=decision.status, reason=decision.reason)
        except BudgetExceeded as exc:
            step.update(status="budget_exhausted", error_type=type(exc).__name__)
            steps.append(step)
            break
        except Exception as exc:
            step.update(status="unresolved", error_type=type(exc).__name__,
                        reason=str(exc) if isinstance(exc, ValueError) else "See trace")
        steps.append(step)
    not_attempted = len(selected) - len(steps)
    errors = {}
    for step in steps:
        kind = step.get("error_type")
        if kind:
            errors[kind] = errors.get(kind, 0) + 1
    return {"plan": plan, "assertions": assertions, "bindings": bindings, "steps": steps,
            "coverage": {"input_candidates": len(candidates),
                         "not_endpoint_verified": len(candidates) - len(selected),
                         "endpoint_verified_candidates": len(selected),
                         "attempted": len(steps), "not_attempted": not_attempted,
                         "accepted": sum(item["status"] == "accepted" for item in steps),
                         "unresolved": sum(item["status"] == "unresolved" for item in steps),
                         "budget_exhausted": sum(item["status"] == "budget_exhausted" for item in steps),
                         "error_types": errors,
                         "partial": bool(not_attempted or any(item["status"] in
                                 ("unresolved", "budget_exhausted") for item in steps))}}


def _definition_previews(data, business_type, record_id):
    previews = []
    for prop in business_type.source_properties:
        if prop.role not in ("description", "formula"):
            continue
        for evidence_id in prop.evidence_ids:
            evidence = data.evidence.get(evidence_id) or {}
            if (evidence.get("origin") == "observed_record"
                    and not evidence.get("raw_fragment_truncated")
                    and evidence.get("source_ref", {}).get("record_id") == record_id):
                value = evidence.get("raw_fragment")
                if isinstance(value, str) and value and len(value) <= 1024:
                    previews.append({"role": prop.role, "quote": value,
                                     "evidence_id": evidence_id})
    return previews[:4]
