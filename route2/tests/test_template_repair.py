"""Reuse proof, bounded second-pass repairs, and unresolved observed values."""
import asyncio
from copy import deepcopy

import pytest

from ontology_r2.group_incremental import ConceptBundleDecision, construct_from_bundles
from ontology_r2.models import BuildPlan, DerivedType
from ontology_r2.template_projection import compile_projection, reuse_projection
from ontology_r2.template_reuse import build_targeted_repair_tasks, template_reuse_context, lookup_observed_value_sources
from test_template_projection import PROFILE, decision, record, setup


def accepted_state():
    data, bundle = setup()
    plan, template, bindings = compile_projection(data, PROFILE, BuildPlan(), bundle, decision())
    # A separately evidenced Dimension is available before the second pass.
    eid = next(iter(data.evidence))
    plan.object_types.append(DerivedType(id="type:region", label="地区", parent="Dimension",
                                       definition="地区划分的维度定义", category="business_type", evidence_ids=[eid]))
    later = {"bundle_id": "b2", "task_kind": "concept_induction", "records": [record("row3", "香蕉", "非洲")],
             "exact_alignment_record_ids": ["row3"]}
    prior = {"snapshot_id": data.snapshot_id, "plan": plan, "template_projections": [template],
             "template_bindings": bindings, "steps": [
                 {"bundle_id": "b1", "task_kind": "concept_induction", "status": "unresolved", "reason": "missing endpoint"}],
             "source_bundle_ids": ["b1", "b2", "b_missing"],
             "coverage": {"bundles_not_attempted": 2}, "bundles_skipped": 2}
    return data, [bundle, later], prior


def test_partial_match_keeps_checked_fields_without_claiming_identity():
    data, bundles, prior = accepted_state()
    changed = deepcopy(bundles[1])
    changed["records"][0]["fields"]["formula"][0]["value"] = "failed / inspected"
    report = template_reuse_context(prior["template_projections"], changed)[0]
    assert report["status"] == "partial_match" and report["identity_claim"] == "none"
    assert report["protected_fields_changed"] == ["x10"]
    assert "x11" in report["matched_columns"]
    assert reuse_projection(data, prior["template_projections"], changed) is None
    assert report["accepted_template"]["invariants"]["x10"] == "passed / inspected"


def test_repair_groups_are_bounded_and_new_banana_value_does_not_mint_type():
    data, bundles, prior = accepted_state()
    before = prior["plan"].model_dump()
    tasks = build_targeted_repair_tasks(data, bundles * 5, prior, max_tasks=1, max_examples=2)
    assert len(tasks["bundles"]) == 1
    assert tasks["tasks"][0]["scope"] == "template_slot_endpoints"
    assert tasks["tasks"][0]["representatives_submitted"] == 2
    assert tasks["coverage"]["source_records_in_gap_groups"] == 2
    assert any(item["label"] == "合格率" for item in tasks["tasks"][0]["component_type_candidates"])
    unknown = next(item for item in tasks["observed_value_tasks"] if item["value"] == "香蕉")
    assert unknown["identity_claim"] == "none" and not unknown["creates_ontology_type"]
    assert prior["plan"].model_dump() == before
    assert build_targeted_repair_tasks(data, bundles, prior, max_tasks=0)["coverage"]["groups_not_selected"] == 1


def test_second_pass_preserves_core_and_resolves_only_checked_source_gaps():
    data, bundles, prior = accepted_state()
    tasks = build_targeted_repair_tasks(data, bundles, prior)
    original_type = prior["template_projections"][0]["object_type_id"]

    class RepairLLM:
        calls = 0

        async def ask(self, task, payload, schema):
            self.calls += 1
            assert task == "concept_bundle"
            assert payload["targeted_repair"]["preserve_type_id"] == original_type
            assert any(item["label"] == "合格率" for item in payload["component_type_candidates"])
            proposed = decision()
            proposed.existing_type_id = original_type
            proposed.slots[1].target_type_id = "type:region"
            return ConceptBundleDecision(status="proposed", action="project_template", projection=proposed)

    llm = RepairLLM()
    result = asyncio.run(construct_from_bundles(
        data, PROFILE, prior["plan"], tasks["bundles"], llm, prior_result=prior,
        enable_template_projection=True, review=False))
    assert llm.calls == 1  # A full existing match must not skip its endpoint repair.
    assert {item.id for item in prior["plan"].object_types} <= {item.id for item in result["plan"].object_types}
    assert len(result["template_bindings"]) >= len(prior["template_bindings"]) + 1
    old = next(item for item in result["steps"] if item["bundle_id"] == "b1")
    assert old["superseded"] and old["resolved_by"] == tasks["bundles"][0]["bundle_id"]
    assert result["coverage"]["statuses"]["unresolved"] == 0
    assert result["coverage"]["source_bundles_not_attempted"] == 1
    assert result["bundles_skipped"] == 1 and result["partial"]
    chosen = reuse_projection(data, result["template_projections"], bundles[1])
    chosen_template = next(item for item in result["template_projections"] if item["template_id"] == chosen["template_id"])
    assert next(slot for slot in chosen_template["slots"] if slot["name"] == "region")["target_type_id"] == "type:region"
    assert build_targeted_repair_tasks(data, bundles, result)["bundles"] == []


def test_repair_cannot_replace_the_accepted_type_or_remove_unresolved_slot():
    for change in ("type", "slot"):
        data, bundles, prior = accepted_state()
        tasks = build_targeted_repair_tasks(data, bundles, prior)

        class BadLLM:
            async def ask(self, task, payload, schema):
                proposed = decision()
                if change == "type":
                    proposed.definition_parameters = {"period": "Q"}
                    proposed.slots[1].target_type_id = "type:region"
                else:
                    proposed.slots[1].role = "reference"
                return ConceptBundleDecision(status="proposed", action="project_template", projection=proposed)

        result = asyncio.run(construct_from_bundles(
            data, PROFILE, prior["plan"], tasks["bundles"], BadLLM(), prior_result=prior,
            enable_template_projection=True, review=False))
        assert result["steps"][-1]["status"] == "unresolved"
        assert result["plan"].model_dump() == prior["plan"].model_dump()
        assert not result["steps"][0].get("superseded")


def test_conflicting_template_endpoints_never_choose_arbitrarily():
    data, bundles, prior = accepted_state()
    original = prior["template_projections"][0]
    left, right = deepcopy(original), deepcopy(original)
    left["template_id"], right["template_id"] = "left", "right"
    left["slots"][1]["target_type_id"] = "type:region_a"
    right["slots"][1]["target_type_id"] = "type:region_b"
    assert reuse_projection(data, [left, right], bundles[0]) is None


def test_observed_value_lookup_returns_checked_source_candidates_without_type_assertion(tmp_path):
    from test_definition_memberships import _fixture

    data, index, _ = _fixture(tmp_path)
    try:
        result = lookup_observed_value_sources(data, index, [
            {"value": "收入", "kind": "observed_value_resolution"},
            {"value": "香蕉", "kind": "observed_value_resolution"}], max_candidates_per_task=2)
        candidates = result["tasks"][0]["source_candidates"]
        assert 1 <= len(candidates) <= 2
        assert candidates[0]["record"]["record_id"]
        assert any(field["quote"] == "收入" for field in candidates[0]["matched_fields"])
        assert candidates[0]["identity_claim"] == "none" and not candidates[0]["membership_asserted"]
        assert result["tasks"][1]["lookup_status"] == "no_checked_source_candidate"
        assert result["coverage"]["llm_calls"] == result["coverage"]["created_types"] == 0
    finally:
        index.close()
        data.close()
