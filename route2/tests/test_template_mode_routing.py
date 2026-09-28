"""Template mode cannot promote a configured instance through exact identity."""
import asyncio

import pytest

from ontology_r2.group_incremental import (
    BundleReview, ConceptBatchDecision, ConceptBundleDecision,
    _check_concept_action, construct_from_bundles,
)
from ontology_r2.models import BuildPlan
from ontology_r2.template_projection import ProjectionQuote, compile_projection
from test_template_projection import PROFILE, decision, quote, setup


@pytest.mark.parametrize("root", ["Metric", "GeneralObject", "Measure", "Dimension", "Term"])
def test_root_routing_is_explicit_and_default_mode_remains_compatible(root):
    proposed = ConceptBundleDecision(status="proposed", root_type=root)
    _check_concept_action(proposed, False)
    if root in {"Metric", "GeneralObject"}:
        with pytest.raises(ValueError, match="requires project_template"):
            _check_concept_action(proposed, True)
    else:
        _check_concept_action(proposed, True)
    _check_concept_action(ConceptBundleDecision(status="unresolved", root_type=root), True)


@pytest.mark.parametrize("batch_size", [1, 2])
def test_exact_metric_response_is_repaired_before_any_review_or_acceptance(batch_size):
    data, bundle = setup()
    # Mirrors the live failure: the explanation promises a class and slots,
    # but the actual action still attempts to create a type from an instance.
    wrong = ConceptBundleDecision(
        status="proposed", root_type="Metric", action="exact_definition",
        label="苹果合格率", definition="苹果合格率按华东汇总",
        ontology_level="type", classification_basis="business_driven_metric",
        classification_quote="水果合格率统计质量合格比例。", business_object_quote="水果",
        scope_roles={"x7": "applicability"},
        alignments=[{"record_id": "row1", "mapping_kind": "exact", "quote": "苹果合格率"}],
        reason="通用类与配置实例应分离，保留对象与度量槽位")

    class LLM:
        def __init__(self):
            self.tasks = []

        async def ask(self, task, payload, schema):
            self.tasks.append(task)
            if task == "concept_batch":
                return ConceptBatchDecision.model_validate({"decisions": [
                    {"bundle_id": packet["bundle"]["bundle_id"], "decision": wrong.model_dump()}
                    for packet in payload["packets"]]})
            if task == "concept_bundle":
                if "compiler_error" not in payload:
                    return wrong
                assert "requires project_template" in payload["compiler_error"]
                assert "Metric and GeneralObject require project_template" in payload["repair_instruction"]
                return ConceptBundleDecision(status="proposed", action="project_template", projection=decision())
            # Invalid batch or single proposals must never reach the reviewer.
            assert task == "group_review"
            assert payload["candidate"]["action"] == "project_template"
            return BundleReview(accepted=True)

    bundles = [bundle]
    if batch_size > 1:
        bundles.append({**bundle, "bundle_id": "b2"})
    llm = LLM()
    result = asyncio.run(construct_from_bundles(
        data, PROFILE, BuildPlan(), bundles, llm, enable_template_projection=True,
        concept_batch_size=batch_size, max_repairs_per_bundle=1))
    assert result["steps"][0]["status"] == "accepted"
    assert result["steps"][0]["repair_calls"] == 1
    assert len(result["template_projections"]) == 1
    assert not result["concepts"] and not result["record_alignments"]
    assert "苹果合格率" not in {item.label for item in result["plan"].object_types}
    assert "group_review_batch" not in llm.tasks


@pytest.mark.parametrize("root", ["Metric", "GeneralObject"])
def test_empty_single_record_projection_cannot_bypass_class_evidence(root):
    data, bundle = setup()
    proposed = decision()
    proposed.root_type = root
    proposed.witness_record_ids = ["row1"]
    proposed.slots = []
    proposed.field_templates = []
    with pytest.raises(ValueError, match="requires a quoted class_definition"):
        compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    proposed.class_definition = ProjectionQuote.model_validate(quote("x4", proposed.definition))
    plan, _, bindings = compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    assert proposed.label in {item.label for item in plan.object_types}
    assert len(bindings) == 1


def test_varying_records_need_shared_source_definition_without_class_quote():
    data, bundle = setup()
    proposed = decision()
    proposed.definition = "水果合格率由模型概括得出但没有源文。"
    with pytest.raises(ValueError, match="requires a quoted class_definition"):
        compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
