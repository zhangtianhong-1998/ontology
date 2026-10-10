"""Calculation proof is checked independently of its claimed scope label."""
from pathlib import Path

import pytest

from ontology_r2.calculation_stage import compile_calculation_stage
from ontology_r2.models import DerivedType
from ontology_r2.relation_contract import canonical_relation_id
from ontology_r2.storage import read_yaml
from ontology_r2.validation import validate_plan
from test_calculation_contracts import _case

PROFILE = read_yaml(Path(__file__).resolve().parents[1] / "ontologies/internal_model.yaml")


def test_stage_publishes_evidenced_dependencies_without_mutating_input():
    data, plan, groups = _case()
    result = compile_calculation_stage(data, PROFILE, plan, groups)
    assert len(result["plan"].relation_types) == 2
    assert plan.relation_types == []
    assert validate_plan(result["plan"], data, PROFILE) == []
    assert all(item.endpoint_basis == "calculation_binding" for item in result["plan"].relation_types)


def test_canonical_name_and_complete_scope_cannot_replace_source_formula_proof():
    data, plan, groups = _case()
    forged = DerivedType(
        id=canonical_relation_id("depends_on", "profit", "revenue", predicate_name="calculation_dependency"),
        parent="depends_on", label="calculation_dependency", predicate_name="calculation_dependency",
        category="business_relation_type", definition="an unsupported claim", domain=["profit"], range=["revenue"],
        endpoint_basis="calculation_binding", evidence_scope="complete_calculation_definition",
        evidence_ids=["profit:name", "revenue:name"])
    plan.relation_types.append(forged)
    assert any("independent complete source formula" in error for error in validate_plan(plan, data, PROFILE))
    forged.evidence_ids.append("profit:formula")
    forged.semantic_parameters = {"operand_role": "subtrahend"}
    forged.id = canonical_relation_id("depends_on", "profit", "revenue", predicate_name="calculation_dependency",
                                      semantic_parameters=forged.semantic_parameters)
    assert any("operand role is absent" in error for error in validate_plan(plan, data, PROFILE))


def test_existing_one_pair_proof_is_explicitly_upgraded_and_old_plan_stays_unchanged():
    data, plan, groups = _case()
    calculated = compile_calculation_stage(data, PROFILE, plan, groups)["plan"]
    old = calculated.relation_types[0]
    old.endpoint_basis = "record_alignment"
    old.evidence_scope = "one_positive_pair_with_exact_type_alignments"
    upgraded = compile_calculation_stage(data, PROFILE, calculated, groups)
    assert old.endpoint_basis == "record_alignment"
    assert old.evidence_scope == "one_positive_pair_with_exact_type_alignments"
    assert len(upgraded["proof_updates"]) == 1
    relation = next(item for item in upgraded["plan"].relation_types if item.id == old.id)
    assert relation.endpoint_basis == "calculation_binding"
    assert relation.evidence_scope == "complete_calculation_definition"
    assert upgraded["proof_updates"][0]["previous_evidence_scope"] == old.evidence_scope


def test_restored_calculation_edges_are_retracted_if_original_formula_is_now_missing():
    data, plan, groups = _case()
    calculated = compile_calculation_stage(data, PROFILE, plan, groups)["plan"]
    data.evidence["profit:formula"]["raw_fragment_truncated"] = True
    resumed = compile_calculation_stage(data, PROFILE, calculated, groups)
    assert resumed["plan"].relation_types == []
    assert len(resumed["retracted_relations"]) == 2
    assert resumed["coverage"]["partial"] is True
    assert len(calculated.relation_types) == 2


def test_new_operand_ambiguity_retracts_only_calculation_stage_edges():
    data, plan, groups = _case()
    original = compile_calculation_stage(data, PROFILE, plan, groups)["plan"]
    expanded_data, expanded_plan, expanded_groups = _case(duplicate=True)
    expanded_plan.relation_types = original.relation_types
    other = DerivedType(
        id=canonical_relation_id("related_to", "profit", "revenue"), parent="related_to",
        label="related_to", definition="a separately reviewed relation", category="business_relation_type",
        domain=["profit"], range=["revenue"], endpoint_basis="record_alignment",
        evidence_scope="one_positive_pair_with_exact_type_alignments", evidence_ids=["profit:name", "revenue:name"])
    expanded_plan.relation_types.append(other)
    resumed = compile_calculation_stage(expanded_data, PROFILE, expanded_plan, expanded_groups)
    assert [item.id for item in resumed["plan"].relation_types] == [other.id]
    assert len(resumed["retracted_relations"]) == 2
    assert resumed["dependencies"] == [] and resumed["coverage"]["partial"]


def test_failed_stage_is_atomic_even_when_existing_relation_evidence_would_merge():
    data, plan, groups = _case()
    calculated = compile_calculation_stage(data, PROFILE, plan, groups)["plan"]
    data.evidence["second-proof"] = dict(data.evidence["profit:formula"])
    calculated.object_types[0].source_properties[1].evidence_ids.append("second-proof")
    calculated.object_types.append(DerivedType(id="bad", parent="missing", definition="bad", evidence_ids=["profit:name"]))
    old_evidence = [list(item.evidence_ids) for item in calculated.relation_types]
    with pytest.raises(ValueError, match="unknown/cyclic"):
        compile_calculation_stage(data, PROFILE, calculated, groups)
    assert [item.evidence_ids for item in calculated.relation_types] == old_evidence
