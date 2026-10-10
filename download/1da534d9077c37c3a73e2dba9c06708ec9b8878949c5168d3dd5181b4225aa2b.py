"""Source-grounded class recall and resource identity without domain fixtures."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ontology_r2.models import BuildPlan
from ontology_r2.template_projection import ProjectionField, compile_projection, match_projection
from ontology_r2.template_reuse import (
    _class_declaration_candidates, augment_template_repair_witnesses, build_targeted_repair_tasks,
)
from test_projection_contract_v12 import resource_fixture
from test_template_projection import PROFILE
from test_template_variation_witnesses import fixture


def test_first_class_can_recall_other_instances_without_an_accepted_template(tmp_path):
    data, index, packet = fixture(tmp_path)
    try:
        packet["template_repair_task"] = {"scope": "unresolved_source_definition"}
        result = augment_template_repair_witnesses(data, index, [packet])
        enriched = result["bundles"][0]
        assert {record["row_number"] for record in enriched["records"]} == {1, 2, 3}
        assert enriched["exact_alignment_record_ids"] == packet["exact_alignment_record_ids"]
        assert enriched["template_repair_task"]["class_definition_candidates"][0]["quote"] == (
            "数据接口是一类提供区域查询的服务资源")
        assert result["coverage"]["llm_calls"] == result["coverage"]["exact_alignments_added"] == 0
    finally:
        index.close()
        data.close()


def test_large_scheduling_provenance_does_not_consume_witness_evidence_budget(tmp_path):
    data, index, packet = fixture(tmp_path)
    try:
        packet["source_bundle_ids"] = [f"bundle:{number:024d}" for number in range(749)]
        packet["template_repair_task"]["source_bundle_ids"] = list(packet["source_bundle_ids"])
        result = augment_template_repair_witnesses(data, index, [packet], max_bundle_bytes=24000)
        enriched = result["bundles"][0]
        assert len(enriched["records"]) == 3
        assert enriched["source_bundle_ids"] == packet["source_bundle_ids"]
        assert enriched["template_repair_task"]["source_bundle_ids"] == packet["source_bundle_ids"]
    finally:
        index.close()
        data.close()


def test_declaration_recall_preserves_full_clauses_and_skips_negation_and_truncation():
    record = {"fields": {"description": [
        {"column": "definition", "value": "Readings for site A. Report is a class of published summaries; this is one report."},
        {"column": "negative", "value": "报表不是一类经营主体。"},
        {"column": "preview", "value": "设备是一种运行", "truncated": True}]}}
    assert _class_declaration_candidates(record) == [{"column": "definition",
        "quote": "Report is a class of published summaries", "label_fragment": "Report",
        "status": "source_clause_candidate"}]


def dimension_packets():
    data = SimpleNamespace(snapshot_id="s", tables={}, evidence={})
    records = [{"record_id": str(i), "table": "ops.dimensions", "row_number": i,
                "fields": {"name": [{"column": "name", "value": label}],
                           "description": [{"column": "definition", "value": f"{label}是用于区分记录分类的维度定义"}],
                           "scope": [{"column": "kind", "value": kind}],
                           "reference": [{"column": "id", "value": str(i)}]}}
               for i, label, kind in [(1, "风险等级", "RISK"), (2, "服务级别", "SERVICE"), (3, "计费类别", "BILLING")]]
    bundles = [{"bundle_id": "bundle:" + record["record_id"], "task_kind": "concept_induction",
                "records": [record], "exact_alignment_record_ids": [record["record_id"]]}
               for record in records]
    prior = {"snapshot_id": "s", "plan": BuildPlan(), "steps": [
        {"bundle_id": bundle["bundle_id"], "status": "unresolved", "error_type": "ValueError",
         "reason": "same parameter role error"} for bundle in bundles]}
    return data, bundles, prior


def test_same_error_does_not_group_distinct_dimension_definitions():
    data, bundles, prior = dimension_packets()
    result = build_targeted_repair_tasks(data, bundles, prior)
    assert len(result["bundles"]) == 3
    assert all(len(packet["records"]) == 1 for packet in result["bundles"])
    assert {tuple(task["source_bundle_ids"]) for task in result["tasks"]} == {
        ("bundle:1",), ("bundle:2",), ("bundle:3",)}
    # A misleading partial match to one existing dimension also cannot merge
    # the two other definitions simply because their missing fields coincide.
    prior["template_projections"] = [{"template_id": "projection:risk", "object_type_id": "risk",
        "source_table": "ops.dimensions", "status": "accepted", "snapshot_id": "s", "root_type": "Dimension",
        "label": "风险等级", "definition": "风险等级是用于区分记录分类的维度定义",
        "semantic_columns": ["name", "definition", "kind"], "invariants": {"kind": "RISK"},
        "field_templates": [{"column": "name", "template": "风险等级"},
                            {"column": "definition", "template": "风险等级是用于区分记录分类的维度定义"}],
        "slots": []}]
    result = build_targeted_repair_tasks(data, bundles, prior)
    assert len(result["bundles"]) == 2
    assert all(len(packet["records"]) == 1 for packet in result["bundles"])


def test_grouping_byte_limit_preserves_oversized_original_evidence_for_request_gate():
    data, bundles, prior = dimension_packets()
    bundles[0]["records"][0]["fields"]["description"][0]["value"] += "完整证据" * 500
    original = deepcopy(bundles)
    result = build_targeted_repair_tasks(data, bundles, prior, max_bundle_bytes=1000)
    assert result["coverage"]["original_evidence_over_byte_limit"] >= 1
    assert bundles == original
    assert len(result["bundles"]) == 3
    assert all(len(packet["records"]) == 1 for packet in result["bundles"])
    retained = next(packet for packet in result["bundles"] if packet["source_bundle_id"] == "bundle:1")
    assert retained["records"][0]["fields"] == original[0]["records"][0]["fields"]


def test_shared_class_clause_does_not_override_declared_scope():
    data, bundles, prior = dimension_packets()
    for packet in bundles:
        packet["records"][0]["fields"]["description"][0]["value"] = "分类是一类用于区分记录的维度"
    result = build_targeted_repair_tasks(data, bundles, prior)
    assert len(result["bundles"]) == 3
    assert all(len(packet["records"]) == 1 for packet in result["bundles"])


@pytest.mark.parametrize("declaration", ["physical table name", "物理数据表名称"])
def test_declared_physical_name_is_an_instance_value_without_a_model_slot(declaration):
    data, bundle, proposed = resource_fixture()
    original = bundle["records"][0]
    original["fields"]["name"].append({"column": "storage_name", "value": "audit_001"})
    data.tables[original["table"]] = {"columns": [{"column_name": "storage_name", "column_comment": declaration}]}
    proposed.field_templates.append(ProjectionField(column="storage_name", template="audit_001"))
    _, template, _ = compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    later = deepcopy(original)
    later["record_id"] = "r2"
    later["fields"]["name"][-1]["value"] = "audit_002"
    assert "storage_name" not in template["invariants"]
    assert match_projection(template, later)["instance_values"]["storage_name"] == "audit_002"
    later["fields"]["scope"][0]["value"] = "another grain"
    assert match_projection(template, later) is None


@pytest.mark.parametrize("declaration", ["physical object name", "业务对象名称", "物理表名或业务对象名称"])
def test_ambiguous_names_remain_business_invariants(declaration):
    data, bundle, proposed = resource_fixture()
    source = bundle["records"][0]
    source["fields"]["name"].append({"column": "storage_name", "value": "policy_001"})
    data.tables[source["table"]] = {"columns": [{"column_name": "storage_name", "column_comment": declaration}]}
    _, template, _ = compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    later = deepcopy(source)
    later["fields"]["name"][-1]["value"] = "policy_002"
    assert template["invariants"]["storage_name"] == "policy_001"
    assert match_projection(template, later) is None
