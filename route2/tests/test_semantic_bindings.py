"""Type bindings reuse a proved template and keep coordinates out of type IDs."""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from ontology_r2.configuration_relation_stage import adjudicate_configuration_relations
from ontology_r2.configuration_relations import discover_configuration_relations, infer_configuration_specs
from ontology_r2.models import BuildPlan, DerivedType
from ontology_r2.semantic_bindings import compile_template_relations
from ontology_r2.storage import read_yaml, qi
from ontology_r2.validation import validate_plan
from test_configuration_relations import _input, _rule


PROFILE = read_yaml(Path(__file__).resolve().parents[1] / "ontologies/internal_model.yaml")


def _projection_case():
    evidence = {"source": {"origin": "observed_record", "raw_fragment": "检查对象采用合格率",
                          "source_ref": {"snapshot_id": "snap", "record_id": "r1"}}}
    types = [DerivedType(id=key, parent=root, label=label, definition=label,
                         category="business_type", evidence_ids=["source"])
             for key, root, label in (("metric", "Metric", "检查对象合格率"),
                 ("object", "GeneralObject", "检查对象"), ("measure", "Measure", "合格率"),
                 ("dimension", "Dimension", "地区"))]
    slots = [{"name": role, "role": role, "target_type_id": target, "evidence_ids": ["source"]}
             for role, target in (("business_object", "object"), ("measure", "measure"),
                                  ("dimension", "dimension"))]
    projection = {"template_id": "template", "object_type_id": "metric", "root_type": "Metric",
                  "status": "accepted", "snapshot_id": "snap", "contract_hash": "contract",
                  "source_table": "definitions", "slots": slots}
    return SimpleNamespace(snapshot_id="snap", evidence=evidence, tables={}), BuildPlan(object_types=types), projection


def test_metric_binds_object_measure_dimension_and_member_constraints_stay_separate():
    data, plan, projection = _projection_case()
    projection["slots"][-1]["selector"] = {"op": "in", "field": "member_code", "values": ["E", "S"]}
    first = compile_template_relations(data, PROFILE, plan, [projection])
    assert {item.predicate_name for item in first["plan"].relation_types} == {
        "business_object_binding", "measure_binding", "scope_constraint"}
    assert all(item.evidence_scope == "checked_structured_binding" for item in first["plan"].relation_types)
    assert first["bindings"][-1]["slot"]["selector"]["values"] == ["E", "S"]
    assert all(item.predicate_name != "calculation_dependency" for item in first["plan"].relation_types)
    changed = deepcopy(projection)
    changed["slots"][-1]["selector"]["values"] = ["N"]
    second = compile_template_relations(data, PROFILE, first["plan"], [changed], [{"record_id": "r2"}])
    assert len(second["plan"].relation_types) == 3
    assert second["coverage"]["llm_calls"] == 0
    assert first["bindings"][-1]["id"] != second["bindings"][-1]["id"]


def test_unknown_component_is_pending_and_forged_or_stale_proof_is_rejected():
    data, plan, projection = _projection_case()
    projection["slots"][0]["target_type_id"] = None
    result = compile_template_relations(data, PROFILE, plan, [projection])
    assert len(result["pending"]) == 1
    assert len(result["plan"].relation_types) == 2
    proof_id = next(key for key in result["plan"].relation_types[0].evidence_ids
                    if key.startswith("semantic_binding_proof:"))
    data.evidence[proof_id]["binding_proof"]["source_type_id"] = "object"
    assert any("hash differs" in error for error in validate_plan(result["plan"], data, PROFILE))
    data, plan, projection = _projection_case()
    projection["snapshot_id"] = "stale"
    stale = compile_template_relations(data, PROFILE, plan, [projection])
    assert not stale["plan"].relation_types


def _configuration_case(tmp_path, wording):
    data, plan, concepts, alignments, spec = _input(tmp_path)
    table = data.tables[spec["configuration_table"]]
    data.db.execute(f"UPDATE {qi(table['sql_name'])} SET relation_text=?", [wording])
    rules = [_rule("metric-ref", spec, "metric_ref", spec["source_definition_table"], "metric_code"),
             _rule("measure-ref", spec, "measure_ref", spec["target_definition_table"], "measure_code")]
    for rule in rules:
        rule["snapshot_id"] = data.snapshot_id
    inferred = infer_configuration_specs(data, plan, concepts, alignments, rules)
    candidate = next(item for item in discover_configuration_relations(
        data, plan, concepts, alignments, [inferred["spec_candidates"][0]["spec"]])["candidates"]
        if item["status"] == "endpoint_verified_candidate")
    return data, plan, concepts, alignments, candidate, rules


class _PurposeModel:
    def __init__(self):
        self.calls = 0

    async def ask(self, stage, packet, model):
        self.calls += 1
        assert stage == "configuration_purpose"
        evidence = next(item for item in packet["evidence"]
                        if item.get("column") == "relation_text" and item["kind"] == "configuration_value")
        return model.model_validate({"status": "proposed", "role": "measure",
            "evidence_id": evidence["evidence_id"], "purpose_quote": evidence["text"],
            "reason": "The configuration declares which reusable quantity the performance definition uses."})


def test_unfamiliar_configuration_wording_uses_bounded_judgement_and_exact_paths(tmp_path):
    case = _configuration_case(tmp_path, "This mapping assigns the reusable quantity used to quantify the performance definition.")
    data, plan, concepts, alignments, candidate, rules = case
    try:
        llm = _PurposeModel()
        result = asyncio.run(adjudicate_configuration_relations(
            data, PROFILE, plan, [candidate], concepts, alignments, llm, checked_rules=rules))
        assert llm.calls == 1
        assert result["coverage"]["accepted"] == 1
        relation = result["plan"].relation_types[0]
        assert relation.domain == ["type:profit"] and relation.range == ["type:revenue"]
        assert relation.predicate_name == "measure_binding"
        assert result["bindings"][0]["contract"]["configuration_record_id"] == candidate["configuration_record_id"]
        assert result["assertions"] == []  # A template binding does not create same-as concepts.
        assert validate_plan(result["plan"], data, PROFILE) == []
    finally:
        data.close()


def test_declared_config_shortcut_has_no_llm_and_wrong_join_path_is_not_accepted(tmp_path):
    data, plan, concepts, alignments, candidate, rules = _configuration_case(tmp_path, "度量提供指标的量定义")
    try:
        llm = _PurposeModel()
        result = asyncio.run(adjudicate_configuration_relations(
            data, PROFILE, plan, [candidate], concepts, alignments, llm, checked_rules=rules))
        assert llm.calls == 0 and result["coverage"]["accepted"] == 1
        forged = deepcopy(rules)
        forged[0]["source"]["field"] = "id"
        wrong = asyncio.run(adjudicate_configuration_relations(
            data, PROFILE, plan, [candidate], concepts, alignments, llm, checked_rules=forged))
        assert wrong["coverage"]["accepted"] == 0
        assert "paths differ" in wrong["steps"][0]["reason"]
    finally:
        data.close()


def test_checked_membership_can_bind_definition_but_never_claims_exact_identity(tmp_path):
    data, plan, concepts, alignments, candidate, rules = _configuration_case(tmp_path, "度量提供指标的量定义")
    try:
        measure_alignment = alignments.pop()
        member = {"id": "membership", "record_id": measure_alignment["source_record_id"],
                  "type_id": "type:revenue", "concept_id": "concept:revenue",
                  "status": "definition_template_match", "evidence_ids": measure_alignment["evidence_ids"]}
        result = asyncio.run(adjudicate_configuration_relations(
            data, PROFILE, plan, [candidate], concepts, alignments, _PurposeModel(),
            checked_rules=rules, memberships=[member]))
        assert result["coverage"]["accepted"] == 1
        assert result["bindings"][0]["identity_claim"] == "none"
    finally:
        data.close()
