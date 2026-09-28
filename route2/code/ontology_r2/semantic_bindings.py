"""Compile verified definition slots into ontology edges without row identity claims.

Configuration/definition rows are witnesses. Selectors belong to binding
assertions, not type identity, and a measure binding never invents a formula.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, model_validator

from .configuration_relations import declared_reference_evidence
from .models import BuildPlan, Condition, DerivedType, Strict
from .relation_contract import canonical_relation_definition, canonical_relation_id
from .storage import digest


_ROLES = {
    "business_object": ("business_object_binding", "related_to", "GeneralObject"),
    "measure": ("measure_binding", "depends_on", "Measure"),
    "dimension": ("scope_constraint", "related_to", "Dimension"),
}


class OntologySlotBinding(Strict):
    """Typed member constraints are separate from an ontology relation's signature."""

    name: str = Field(min_length=1)
    role: Literal["business_object", "measure", "dimension"]
    target_type_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    selector: Condition | None = None
    binding_scope: Literal["template", "definition_instance"] = "template"

    @model_validator(mode="after")
    def check_selector(self):
        if self.selector is not None and self.role != "dimension":
            raise ValueError("Only dimension slots carry a member selector")
        return self


class ConfigurationPurposeDecision(Strict):
    """Meaning is proposed once from bounded source evidence, never from IDs."""

    status: Literal["proposed", "unresolved", "no_change"]
    role: Literal["business_object", "measure", "dimension"] | None = None
    evidence_id: str = ""
    purpose_quote: str = ""
    reason: str = ""


def _root(item, types):
    parent, seen = item.parent, set()
    while parent in types and parent not in seen:
        seen.add(parent)
        parent = types[parent].parent
    return parent


def _source_evidence(data, evidence_ids):
    """Only complete input evidence can support a slot; model prose cannot."""
    if not evidence_ids:
        raise ValueError("Binding lacks source evidence")
    for evidence_id in evidence_ids:
        proof = data.evidence.get(evidence_id)
        if not proof or proof.get("raw_fragment_truncated"):
            raise ValueError("Binding source evidence is missing or truncated")
        if proof.get("origin") not in ("observed_record", "schema", "source_schema", "metadata"):
            # Schema evidence from Dataset is identified by schema: IDs and
            # still must exist in this current Dataset's evidence registry.
            if not evidence_id.startswith("schema:"):
                raise ValueError("Binding requires source evidence, not an inferred claim")
        snapshot = proof.get("source_ref", {}).get("snapshot_id")
        if snapshot is not None and snapshot != data.snapshot_id:
            raise ValueError("Binding evidence belongs to another snapshot")


def _endpoints(types, source_id, target_id, role):
    source, target = types.get(source_id), types.get(target_id)
    if (source is None or target is None or source_id == target_id
            or source.category != "business_type" or target.category != "business_type"):
        raise ValueError("Binding requires two accepted business type endpoints")
    if _root(source, types) != "Metric" or _root(target, types) != _ROLES[role][2]:
        raise ValueError("Binding endpoint roots disagree with its semantic slot")
    common = source.applicability_scope.keys() & target.applicability_scope.keys()
    if any(source.applicability_scope[key] != target.applicability_scope[key] for key in common):
        raise ValueError("Binding endpoint applicability scopes conflict")


def _publish_binding(data, plan, source_id, slot, contract):
    """One checked contract adds one type edge plus a scoped binding assertion."""
    slot = OntologySlotBinding.model_validate(slot)
    types = {item.id: item for item in plan.object_types}
    _endpoints(types, source_id, slot.target_type_id, slot.role)
    _source_evidence(data, slot.evidence_ids)
    predicate, parent, _ = _ROLES[slot.role]
    proof_body = {
        "snapshot_id": data.snapshot_id, "source_type_id": source_id,
        "slot": slot.model_dump(), "contract": contract,
    }
    proof_id = "semantic_binding_proof:" + digest(proof_body)[:24]
    data.evidence[proof_id] = {
        "id": proof_id, "origin": "automatic_contract_check",
        "inference_kind": "checked_structured_binding", "binding_proof": proof_body,
        "basis_evidence_ids": slot.evidence_ids,
        "source_ref": {"snapshot_id": data.snapshot_id},
    }
    relation_id = canonical_relation_id(parent, source_id, slot.target_type_id, predicate_name=predicate)
    proposed = DerivedType(
        id=relation_id, parent=parent, label=predicate, predicate_name=predicate,
        definition=canonical_relation_definition(parent, predicate),
        category="business_relation_type", domain=[source_id], range=[slot.target_type_id],
        endpoint_basis="semantic_binding", evidence_scope="checked_structured_binding",
        evidence_ids=sorted(set(slot.evidence_ids + [proof_id])),
    )
    old = next((item for item in plan.relation_types if item.id == relation_id), None)
    if old:
        if old.model_dump(exclude={"evidence_ids"}) != proposed.model_dump(exclude={"evidence_ids"}):
            raise ValueError("Binding conflicts with an existing relation contract")
        proposed.evidence_ids = sorted(set(old.evidence_ids + proposed.evidence_ids))
        plan.relation_types[plan.relation_types.index(old)] = proposed
    else:
        plan.relation_types.append(proposed)
    return {
        "id": "semantic_binding:" + digest(proof_body)[:24],
        "relation_type_id": relation_id, "source_type_id": source_id,
        "target_type_id": slot.target_type_id, "slot": slot.model_dump(),
        "contract": contract, "snapshot_id": data.snapshot_id,
        "status": "accepted", "evidence_ids": proposed.evidence_ids,
        "identity_claim": "none", "relationship_scope": "checked_definition_slot",
    }


def binding_relation_errors(data, relation, object_types):
    """Replay validates endpoint and source proofs, not just a saved status flag."""
    types = {item.id: item for item in object_types}
    if len(relation.domain) != 1 or len(relation.range) != 1:
        return ["structured binding requires one source and target type"]
    found = False
    for evidence_id in relation.evidence_ids:
        evidence = data.evidence.get(evidence_id, {})
        body = evidence.get("binding_proof")
        if evidence.get("inference_kind") != "checked_structured_binding" or not isinstance(body, dict):
            continue
        try:
            if body.get("snapshot_id") != data.snapshot_id:
                raise ValueError("Binding contract belongs to another snapshot")
            if evidence_id != "semantic_binding_proof:" + digest(body)[:24]:
                raise ValueError("Binding contract hash differs")
            slot = OntologySlotBinding.model_validate(body["slot"])
            _endpoints(types, body["source_type_id"], slot.target_type_id, slot.role)
            _source_evidence(data, slot.evidence_ids)
            predicate, parent, _ = _ROLES[slot.role]
            if (relation.domain != [body["source_type_id"]] or relation.range != [slot.target_type_id]
                    or relation.predicate_name != predicate or relation.parent != parent):
                raise ValueError("Binding proof path differs from relation endpoints")
            if not set(slot.evidence_ids) <= set(relation.evidence_ids):
                raise ValueError("Binding relation omits its source proofs")
            found = True
        except (ValueError, KeyError) as exc:
            return [str(exc)]
    return [] if found else ["structured binding lacks a checked source contract"]


def measure_slot_value_supported(data, target, value):
    """A varying slot may use a concrete measure only by exact name or alias."""
    from .calculation_contracts import _aliases, _norm
    names = {_norm(target.label)}
    for prop in target.source_properties:
        if prop.role != "alias":
            continue
        for evidence_id in prop.evidence_ids:
            proof = data.evidence.get(evidence_id, {})
            ref = proof.get("source_ref", {})
            if (proof.get("origin") == "observed_record" and not proof.get("raw_fragment_truncated")
                    and ref.get("snapshot_id") == data.snapshot_id
                    and ref.get("table") == prop.source_table and ref.get("column") == prop.source_column):
                names.update(_norm(alias) for alias in _aliases(proof.get("raw_fragment", "")))
    return _norm(value) in names


def compile_template_relations(data, profile, plan, template_projections, template_bindings=()):
    """Publish reusable type edges once; observed member values remain bindings."""
    from .validation import validate_plan

    candidate = BuildPlan.model_validate(plan).model_copy(deep=True)
    assertions, pending, seen = [], [], set()
    for projection in template_projections:
        if projection.get("status") != "accepted" or projection.get("root_type") != "Metric":
            continue
        for raw_slot in projection.get("slots", []):
            if raw_slot.get("role") not in _ROLES:
                continue
            try:
                if projection.get("snapshot_id") != data.snapshot_id:
                    raise ValueError("Template projection snapshot differs")
                if not raw_slot.get("target_type_id"):
                    raise ValueError("Slot requires a targeted definition endpoint")
                variable = any("{" + raw_slot["name"] + "}" in field.get("template", "")
                               for field in projection.get("field_templates", []))
                if raw_slot["role"] == "measure" and variable:
                    target = next((item for item in candidate.object_types if item.id == raw_slot["target_type_id"]), None)
                    values = [binding.get("slot_values", {}).get(raw_slot["name"])
                              for binding in template_bindings if binding.get("template_id") == projection["template_id"]]
                    if (target is None or not values or any(value is None or not
                            measure_slot_value_supported(data, target, value) for value in values)):
                        raise ValueError("Varying measure slot values do not all identify the proposed target definition")
                slot = {key: raw_slot[key] for key in OntologySlotBinding.model_fields if key in raw_slot}
                contract = {"kind": "template_projection", "template_id": projection["template_id"],
                            "contract_hash": projection["contract_hash"],
                            "source_table": projection.get("source_table")}
                assertion = _publish_binding(data, candidate, projection["object_type_id"], slot, contract)
                if assertion["id"] not in seen:
                    assertions.append(assertion)
                    seen.add(assertion["id"])
            except (ValueError, KeyError) as exc:
                pending.append({"template_id": projection.get("template_id"), "slot": raw_slot,
                                "status": "unresolved", "reason": str(exc)})
    errors = validate_plan(candidate, data, profile)
    if errors:
        raise ValueError("Invalid structured ontology bindings: " + "; ".join(errors))
    return {"plan": candidate, "bindings": assertions, "pending": pending,
            "coverage": {"accepted": len(assertions), "pending": len(pending),
                         "template_instance_bindings": len(template_bindings), "llm_calls": 0,
                         "partial": bool(pending)}}


_PURPOSE_DECLARATIONS = {
    "measure": ("度量提供指标的量定义", "指标采用度量", "指标绑定度量", "measure_binding"),
    "business_object": ("指标绑定经营对象", "指标适用于经营对象", "business_object_binding"),
    "dimension": ("维度约束指标的适用范围", "指标受维度约束", "scope_constraint"),
}


def configuration_purpose_packet(data, candidate, witness, plan, checked_rules):
    """Gate and package a semantic judgement on this exact reference pattern."""
    rules = {item["rule_id"]: item for item in checked_rules}
    types = {item.id: item for item in plan.object_types}
    left, right = types.get(candidate.get("source_type_id")), types.get(candidate.get("target_type_id"))
    if left is None or right is None:
        return None
    roots = (_root(left, types), _root(right, types))
    if "Metric" not in roots or not ({"Measure", "Dimension", "GeneralObject"} & set(roots)):
        return None
    for side in ("source", "target"):
        rule = rules.get(candidate.get(side + "_rule_id"))
        if (rule is None or rule.get("status") != "checked_technical"
                or rule.get("snapshot_id") != data.snapshot_id
                or rule.get("verification", {}).get("scan_scope") != "full_input"):
            return None
        if ((rule.get("numeric_overlap_only") or rule.get("source", {}).get("field")
                in data.tables[candidate["configuration_table"]].get("pk", []))
                and declared_reference_evidence(data, rule) is None):
            return None
    from .column_roles import is_sensitive_column
    table_name = candidate["configuration_table"]
    table = data.tables[table_name]
    evidence = []
    table_id = "schema:" + table_name
    if table.get("table_comment") and table_id in data.evidence:
        evidence.append({"evidence_id": table_id, "kind": "table_description",
                         "text": str(table["table_comment"])})
    omitted = []
    for column in table["columns"]:
        name = column["column_name"]
        if is_sensitive_column(column) or name in table.get("semantic_excluded_columns", ()):
            continue
        schema_id = f"schema:{table_name}:{name}"
        if column.get("column_comment") and schema_id in data.evidence:
            evidence.append({"evidence_id": schema_id, "kind": "column_description", "column": name,
                             "text": str(column["column_comment"])})
        value = witness.get(name)
        if not isinstance(value, str) or not value.strip():
            continue
        if len(value) > 1024:
            omitted.append(name)
            continue
        evidence_id = "record:" + digest([data.snapshot_id, candidate["configuration_record_id"], name])[:24]
        data.evidence[evidence_id] = {"id": evidence_id, "origin": "observed_record", "raw_fragment": value,
            "raw_fragment_truncated": False, "source_ref": {"table": table_name, "column": name,
                "record_id": candidate["configuration_record_id"], "row": witness["__r2_row"],
                "snapshot_id": data.snapshot_id}}
        evidence.append({"evidence_id": evidence_id, "kind": "configuration_value", "column": name, "text": value})
    return {"candidate_id": candidate["id"], "snapshot_id": data.snapshot_id,
            "configuration_table": table_name, "evidence": evidence, "omitted_overlong_fields": omitted,
            "endpoints": [{"type_id": item.id, "root": root, "label": item.label,
                            "definition": item.definition, "scope": item.applicability_scope}
                           for item, root in zip((left, right), roots)],
            "reference_paths": [{**{key: rules[candidate[side + "_rule_id"]].get(key)
                                  for key in ("rule_id", "source", "target", "selector", "scope_bindings")},
                                  "declaration": declared_reference_evidence(data, rules[candidate[side + "_rule_id"]])}
                                 for side in ("source", "target")],
            "contract": "Judge a configuration purpose, not a formula or row identity. Code equality and "
                        "co-occurrence are insufficient. Quote one complete source item that explains the "
                        "metric's measure, business object, or dimension binding. Return unresolved when absent."}


def checked_purpose_decision(packet, decision):
    decision = ConfigurationPurposeDecision.model_validate(decision)
    if decision.status != "proposed":
        return None
    source = next((item for item in packet["evidence"] if item["evidence_id"] == decision.evidence_id), None)
    if (source is None or not decision.role or not decision.purpose_quote.strip()
            or decision.purpose_quote != source["text"] or not decision.reason.strip()):
        raise ValueError("Configuration purpose requires a complete source quote and explanation")
    return {"role": decision.role, "evidence_id": decision.evidence_id, "quote": source["text"],
            "declaration": decision.reason, "method": "bounded_model_purpose_judgement",
            "packet_hash": digest(packet)}


def structured_configuration_purpose(data, candidate, witness):
    """Recognize a declared config purpose; code coincidence is never a purpose.

The declaration may live in schema or row metadata. It need not spell out
both endpoint names. Unknown wording is left for a future semantic decision.
"""
    from .column_roles import is_sensitive_column

    table_name = candidate["configuration_table"]
    table = data.tables[table_name]
    sources = [("schema:" + table_name, str(table.get("table_comment") or ""))]
    for column in table["columns"]:
        name = column["column_name"]
        if is_sensitive_column(column) or name in table.get("semantic_excluded_columns", ()):
            continue
        sources.append((f"schema:{table_name}:{name}", str(column.get("column_comment") or "")))
        value = witness.get(name)
        if isinstance(value, str) and value.strip() and len(value) <= 1024:
            evidence_id = "record:" + digest([data.snapshot_id, candidate["configuration_record_id"], name])[:24]
            sources.append((evidence_id, value))
    found = []
    for evidence_id, value in sources:
        for clause in re.split(r"[;；。\n,，]", value):
            clause = clause.strip()
            if re.search(r"不|无|未|禁止|假设|如果|可能|取消|失效|停用|\b(?:not|never|if|inactive)\b", clause, re.I):
                continue
            for role, declarations in _PURPOSE_DECLARATIONS.items():
                if clause in declarations:
                    if evidence_id.startswith("record:"):
                        name = next(column["column_name"] for column in table["columns"]
                                    if evidence_id == "record:" + digest([
                                        data.snapshot_id, candidate["configuration_record_id"], column["column_name"]])[:24])
                        data.evidence[evidence_id] = {
                            "id": evidence_id, "origin": "observed_record", "raw_fragment": value,
                            "raw_fragment_truncated": False, "source_ref": {
                                "table": table_name, "record_id": candidate["configuration_record_id"],
                                "row": witness["__r2_row"], "column": name, "snapshot_id": data.snapshot_id}}
                    found.append({"role": role, "evidence_id": evidence_id, "quote": value,
                                  "declaration": clause})
    return found[0] if len({item["role"] for item in found}) == 1 else None


def compile_structured_configuration_binding(data, profile, core, candidate, witness, purpose,
                                           checked_rules, aligned):
    """Lift two checked reference paths, keeping their config row as a witness."""
    from .configuration_relation_stage import _endpoint
    from .validation import validate_plan

    if candidate.get("snapshot_id") != data.snapshot_id:
        raise ValueError("Configuration binding snapshot differs")
    rules = {item["rule_id"]: item for item in checked_rules}
    plan = BuildPlan.model_validate(core).model_copy(deep=True)
    types = {item.id: item for item in plan.object_types}
    role = purpose["role"]
    endpoints, rule_proofs = {}, []
    for side in ("source", "target"):
        rule = rules.get(candidate.get(side + "_rule_id"))
        expected = candidate[side + "_definition"]
        if (rule is None or rule.get("status") != "checked_technical"
                or rule.get("snapshot_id") != data.snapshot_id
                or rule.get("verification", {}).get("scan_scope") != "full_input"
                or rule.get("transform", {}).get("operator", "identity") != "identity"):
            raise ValueError("Structured binding lacks a complete checked reference rule")
        if (rule.get("source") != {"table": candidate["configuration_table"],
                                  "field": candidate[side + "_code_column"]}
                or rule.get("target") != {"table": expected["table"], "field": expected["code_column"]}):
            raise ValueError("Structured purpose and execution reference paths differ")
        declaration = declared_reference_evidence(data, rule)
        if rule.get("numeric_overlap_only") and declaration is None:
            raise ValueError("Numeric overlap lacks a declared reference path")
        if (rule["source"]["field"] in data.tables[candidate["configuration_table"]].get("pk", [])
                and declaration is None):
            raise ValueError("A configuration row identity is not a business reference")
        counts = rule["verification"].get("checks", {})
        if (not counts.get("eligible_references")
                or counts.get("unique_matches") != counts["eligible_references"]
                or any(counts.get(key, 0) for key in ("ambiguous_matches", "missing_in_input", "missing_scope"))):
            raise ValueError("Reference rule does not uniquely resolve its applicable input")
        if any(witness.get(field) != value for field, value in (rule.get("selector") or {}).items()):
            raise ValueError("Configuration witness does not satisfy reference selector")
        scope = {remote: witness.get(local) for local, remote in (rule.get("scope_bindings") or {}).items()}
        if scope != expected.get("lookup_scope", {}):
            raise ValueError("Configuration reference lookup scope differs from its checked rule")
        endpoints[side], _, _ = _endpoint(data, plan, candidate, side, aligned)
        rule_proofs.append({"rule_id": rule["rule_id"], "source": rule["source"], "target": rule["target"],
                            "selector": rule.get("selector") or {}, "scope_bindings": rule.get("scope_bindings") or {},
                            "declaration": declaration,
                            "verification_hash": digest(rule["verification"])})
    left, right = endpoints["source"], endpoints["target"]
    source, target = (left, right) if _root(left, types) == "Metric" else (right, left)
    _endpoints(types, source.id, target.id, role)
    evidence_ids = sorted(set(candidate["evidence_ids"] + [purpose["evidence_id"]]))
    # Endpoint proof may include earlier automatic audits; binding proof keeps
    # the original records/schema and the exact two executable reference paths.
    evidence_ids = [key for key in evidence_ids if key.startswith("schema:")
                    or data.evidence.get(key, {}).get("origin") == "observed_record"]
    slot = {"name": role, "role": role, "target_type_id": target.id,
            "evidence_ids": evidence_ids, "binding_scope": "definition_instance"}
    contract = {"kind": "configuration_references", "candidate_id": candidate["id"],
                "configuration_record_id": candidate["configuration_record_id"],
                "configuration_table": candidate["configuration_table"],
                "configuration_row_number": witness["__r2_row"],
                "endpoints": {
                    "source": {"type_id": source.id, **candidate[
                        "source_definition" if source.id == left.id else "target_definition"]},
                    "target": {"type_id": target.id, **candidate[
                        "target_definition" if target.id == right.id else "source_definition"]}},
                "declaration": purpose, "reference_paths": rule_proofs,
                "selector": candidate.get("selector", {}),
                "support_scope": "checked_reference_pattern_and_observed_definition_pair"}
    assertion = _publish_binding(data, plan, source.id, slot, contract)
    errors = validate_plan(plan, data, profile)
    if errors:
        raise ValueError("Invalid configuration binding: " + "; ".join(errors))
    return plan, assertion
