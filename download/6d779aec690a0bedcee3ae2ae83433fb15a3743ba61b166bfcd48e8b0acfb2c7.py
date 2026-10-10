"""Projection provenance is eligible evidence, not a reason to hide a type."""
import asyncio

from ontology_r2.type_equivalence import propose_equivalence_candidates, compile_equivalence
from ontology_r2.type_generalization import propose_generalization_candidates
from ontology_r2.type_generalization_stage import run_generalization
from test_type_equivalence import _fixture as equivalence_fixture, _decision as equivalence_decision
from test_type_generalization import _fixture as generalization_fixture, PROFILE


def test_exact_and_projected_type_can_be_compared_with_full_definitions():
    data, plan = equivalence_fixture(roots=("Dimension", "Dimension"), units=(None, None), formulas=(None, None))
    plan.object_types[1].derivation_kind = "template_projection"
    plan.object_types[1].evidence_scope = "projected_definition_template"
    candidates = propose_equivalence_candidates(plan, data, 3)
    assert len(candidates) == 1
    assert compile_equivalence(data, plan, candidates[0], equivalence_decision(data, candidates[0]))
    # A changed definition must not become equivalent just because its name matches.
    data.evidence["record:e1-description"]["raw_fragment"] += "不含退款"
    import pytest
    with pytest.raises(ValueError, match="description"):
        compile_equivalence(data, plan, candidates[0], equivalence_decision(data, candidates[0]))


def test_projected_definitions_enter_generalization_but_measure_exclusion_is_honest():
    data, plan = generalization_fixture()
    for item in plan.object_types:
        item.derivation_kind = "template_projection"
        item.evidence_scope = "projected_definition_template"
    assert len(propose_generalization_candidates(plan, data, 3)) == 1
    result = asyncio.run(run_generalization(data, PROFILE, plan,
                                           lambda packet: {"status": "no_change"}, max_decisions=1))
    assert result["coverage"]["eligible_source_type_count"] == 2
    for item in plan.object_types:
        item.parent = "Measure"
    result = asyncio.run(run_generalization(data, PROFILE, plan,
                                           lambda packet: (_ for _ in ()).throw(AssertionError("must not call"))))
    assert result["coverage"]["eligible_source_type_count"] == 0
    assert result["coverage"]["excluded_measure_type_count"] == 2
    assert result["coverage"]["model_decision_invocations"] == 0
