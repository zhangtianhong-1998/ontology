"""Regression evidence for endpoint-safe reuse and cross-column declarations."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ontology_r2.models import BuildPlan, SourceProperty
from ontology_r2.template_projection import (
    ProjectionField, ProjectionSlot, TemplateProjectionDecision, compile_projection, match_projection,
)
from ontology_r2.template_reuse import projection_match_report, build_targeted_repair_tasks
from test_template_projection import PROFILE, setup, decision, quote, record


def variable_measure_decision():
    proposed = decision()
    proposed.field_templates.append(ProjectionField(column="x2", template="{quantity}"))
    return proposed


def test_later_record_cannot_bind_a_different_measure_to_a_fixed_endpoint():
    data, bundle = setup()
    _, template, _ = compile_projection(data, PROFILE, BuildPlan(), bundle, variable_measure_decision())
    later = record("row3")
    later["fields"]["name"][1]["value"] = "损耗率"
    assert match_projection(template, later) is None
    report = projection_match_report(template, later)
    assert report["status"] == "partial_match"
    assert {"column": "slot:quantity", "reason": "captured_value_not_supported_by_target_name_or_alias"} in report["unresolved_fields"]
    # Pre-v12 contracts cannot authorize new variable measure captures.
    legacy = deepcopy(template)
    next(s for s in legacy["slots"] if s["role"] == "measure").pop("target_value_contract")
    assert match_projection(legacy, record("row3")) is None


def test_literal_source_alias_can_vary_without_changing_the_measure():
    data, bundle = setup()
    plan, _, _ = compile_projection(data, PROFILE, BuildPlan(), bundle, decision())
    measure = next(t for t in plan.object_types if t.parent == "Measure")
    eid = "record:quantity-alias"
    data.evidence[eid] = {"id": eid, "origin": "observed_record", "raw_fragment": '["质量通过率"]',
                          "raw_fragment_truncated": False,
                          "source_ref": {"snapshot_id": data.snapshot_id, "record_id": "alias-row",
                                         "table": "data.quantity_aliases", "column": "aliases"}}
    measure.evidence_ids.append(eid)
    measure.source_properties.append(SourceProperty(role="alias", source_table="data.quantity_aliases",
                                                    source_column="aliases", evidence_ids=[eid]))
    proposed = variable_measure_decision()
    proposed.components = [c for c in proposed.components if c.role != "measure"]
    proposed.slots[2].target_component = None
    proposed.slots[2].target_type_id = measure.id
    _, template, _ = compile_projection(data, PROFILE, plan, bundle, proposed)
    later = record("row3")
    later["fields"]["name"][1]["value"] = "质量通过率"
    assert match_projection(template, later)["slot_values"]["quantity"] == "质量通过率"
    later["fields"]["name"][1]["value"] = "quality guess"
    assert match_projection(template, later) is None


def resource_fixture():
    rec = {"record_id": "r1", "table": "data.resources", "row_number": 1,
           "fields": {"name": [{"column": "resource_name", "value": "区域汇总资源"}],
                      "description": [{"column": "description", "value": "按地区和期间汇总已核准记录"}],
                      "scope": [{"column": "grain", "value": "area,period"}],
                      "unknown": [{"column": "request_example", "value": '{"area":"EAST"}'},
                                  {"column": "policy_json", "value": '{"minimum":100}'}],
                      "reference": [{"column": "resource_id", "value": "1"}]}}
    data = SimpleNamespace(snapshot_id="s", tables={}, evidence={})
    proposed = TemplateProjectionDecision(
        root_type="GeneralObject", label="区域汇总资源", definition="按地区和期间汇总已核准记录\narea,period",
        label_evidence=[quote("resource_name", "区域汇总资源", "r1")], witness_record_ids=["r1"],
        class_definition_evidence=[quote("description", "按地区和期间汇总已核准记录", "r1"),
                                   quote("grain", "area,period", "r1")])
    return data, {"records": [rec]}, proposed


def test_cross_column_definition_keeps_grain_and_does_not_freeze_payload_examples():
    data, bundle, proposed = resource_fixture()
    plan, template, _ = compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    assert plan.object_types[0].definition == proposed.definition
    assert template["invariants"]["grain"] == "area,period"
    assert "request_example" not in template["invariants"]
    later = deepcopy(bundle["records"][0])
    later["record_id"] = "r2"
    later["fields"]["unknown"][0]["value"] = '{"area":"WEST"}'
    later["fields"]["reference"][0]["value"] = "2"
    matched = match_projection(template, later)
    assert matched["instance_values"]["request_example"] == '{"area":"WEST"}'
    # Arbitrary structured business constraints retain their exact contract.
    later["fields"]["unknown"][1]["value"] = '{"minimum":200}'
    assert match_projection(template, later) is None
    later = deepcopy(bundle["records"][0])
    later["fields"]["scope"][0]["value"] = "area"
    assert match_projection(template, later) is None


def test_cross_column_composition_rejects_unquoted_prose_and_other_row_labels():
    data, bundle, proposed = resource_fixture()
    proposed.definition += "；全量覆盖"
    with pytest.raises(ValueError, match="Composed definition"):
        compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    data, bundle, proposed = resource_fixture()
    other = deepcopy(bundle["records"][0])
    other["record_id"] = "r2"
    bundle["records"].append(other)
    proposed.label_evidence[0].record_id = "r2"
    with pytest.raises(ValueError, match="Composed definition"):
        compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)


def test_observed_member_slot_captures_each_row_instead_of_freezing_its_witness():
    data, bundle, proposed = resource_fixture()
    bundle["records"][0]["fields"]["scope"].append({"column": "member_name", "value": "北区"})
    proposed.slots.append(ProjectionSlot(name="area", role="dimension", label="地区",
                                         evidence=quote("member_name", "北区", "r1")))
    proposed.field_templates.append(ProjectionField(column="member_name", template="北区"))
    _, template, _ = compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    assert "member_name" not in template["invariants"]
    later = deepcopy(bundle["records"][0])
    later["record_id"] = "r2"
    later["fields"]["scope"][-1]["value"] = "南区"
    assert match_projection(template, later)["slot_values"]["area"] == "南区"
    # Declaring the member as class applicability still fails closed.
    proposed.applicability_scope = {"member_name": "北区"}
    with pytest.raises(ValueError, match="applicability scope"):
        compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)


def test_single_description_quote_accepts_name_in_another_cell_of_the_same_row():
    data, bundle, proposed = resource_fixture()
    proposed.definition = proposed.class_definition_evidence[0].quote
    proposed.class_definition = proposed.class_definition_evidence[0]
    proposed.class_definition_evidence = []
    _, template, _ = compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    assert template["class_definition"]["column"] == "description"


def test_projection_reuses_exact_type_but_does_not_merge_changed_definitions():
    data, bundle, proposed = resource_fixture()
    plan, first, _ = compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    exact = plan.object_types[0]
    exact.id = "type:already-exact"
    exact.derivation_kind, exact.evidence_scope = "exact_definition", "definition_record"
    compiled, reused, _ = compile_projection(data, PROFILE, plan, bundle, proposed)
    assert reused["object_type_id"] == exact.id
    assert len(compiled.object_types) == 1
    exact.definition += "（另一口径）"
    compiled, separate, _ = compile_projection(data, PROFILE, plan, bundle, proposed)
    assert separate["object_type_id"] != exact.id
    assert len(compiled.object_types) == 2


def test_repair_uses_latest_error_for_every_original_bundle_in_a_group():
    data, bundle, _ = resource_fixture()
    bundles = [{**deepcopy(bundle), "bundle_id": key, "task_kind": "concept_induction"} for key in ["b1", "b2"]]
    prior = {"snapshot_id": data.snapshot_id, "steps": [
        {"bundle_id": "b1", "status": "unresolved", "reason": "old error"},
        {"bundle_id": "b2", "status": "unresolved", "reason": "old error"},
        {"bundle_id": "repair1", "source_bundle_ids": ["b1", "b2"],
         "status": "unresolved", "reason": "new compiler error"}]}
    result = build_targeted_repair_tasks(data, bundles, prior)
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["previous_error"] == "new compiler error"
    assert result["tasks"][0]["source_bundle_ids"] == ["b1", "b2"]
