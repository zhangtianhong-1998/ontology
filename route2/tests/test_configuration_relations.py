"""Configuration rows cite two business definitions; they are not endpoints."""

import pytest

from ontology_r2.configuration_relations import (
    discover_configuration_relations, infer_configuration_specs,
)
from ontology_r2.models import BuildPlan, DerivedType
from ontology_r2.storage import Dataset, digest
from test_semantic_cards import _table


def _input(tmp_path):
    root = tmp_path / "input"
    _table(root, "business_metric_definition", {
        "id": "定义记录 ID", "metric_code": "指标编码", "metric_name": "指标名称",
        "definition": "指标定义",
    }, [
        {"id": "1", "metric_code": "I1", "metric_name": "经营利润",
         "definition": "经营利润用于考察收入减成本的经营成果"},
    ])
    _table(root, "business_measure_definition", {
        "id": "定义记录 ID", "measure_code": "度量编码", "measure_name": "度量名称",
        "definition": "度量定义",
    }, [
        {"id": "11", "measure_code": "M1", "measure_name": "收入",
         "definition": "收入是销售额的数值聚合"},
        {"id": "12", "measure_code": "M2", "measure_name": "成本",
         "definition": "成本是投入额的数值聚合"},
        {"id": "13", "measure_code": "M_DUP", "measure_name": "重复口径甲",
         "definition": "重复口径甲"},
        {"id": "14", "measure_code": "M_DUP", "measure_name": "重复口径乙",
         "definition": "重复口径乙"},
    ])
    _table(root, "business_relation_config", {
        "id": "配置记录 ID", "metric_ref": "指标引用编码",
        "measure_ref": "度量引用编码", "relation_text": "配置关系说明",
        "secret_token": "接入令牌",
    }, [
        {"id": "21", "metric_ref": "I1", "measure_ref": "M1", "relation_text": "经营利润依赖收入"},
        {"id": "22", "metric_ref": "I1", "measure_ref": "M1", "relation_text": "经营利润依赖收入"},
        {"id": "23", "metric_ref": "I1", "measure_ref": "M2", "relation_text": "经营利润依赖成本"},
        {"id": "24", "metric_ref": "I1", "measure_ref": "M_DUP", "relation_text": "依赖"},
        {"id": "25", "metric_ref": "I_MISSING", "measure_ref": "M1", "relation_text": "依赖"},
        {"id": "26", "metric_ref": "", "measure_ref": "M1", "relation_text": "依赖"},
    ])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    metric = "fruit.business_metric_definition"
    measure = "fruit.business_measure_definition"
    config = "fruit.business_relation_config"
    m_record = next(data.rows(metric))
    v_record = next(data.rows(measure))
    m_record_id = data.record_id(metric, m_record)
    v_record_id = data.record_id(measure, v_record)
    m_ev = "record:" + digest([data.snapshot_id, m_record_id, "definition"])[:24]
    v_ev = "record:" + digest([data.snapshot_id, v_record_id, "definition"])[:24]
    for table_name, row, record_id, evidence_id in (
        (metric, m_record, m_record_id, m_ev),
        (measure, v_record, v_record_id, v_ev),
    ):
        data.evidence[evidence_id] = {
            "id": evidence_id, "origin": "observed_record",
            "raw_fragment": row["definition"], "raw_fragment_truncated": False,
            "source_ref": {"table": table_name, "column": "definition",
                           "record_id": record_id, "row": row["__r2_row"],
                           "snapshot_id": data.snapshot_id},
        }
    plan = BuildPlan(object_types=[
        DerivedType(id="type:profit", parent="Metric", category="business_type",
                    label="经营利润", definition="经营利润指标",
                    evidence_ids=[m_ev], source_properties=[{
                        "role": "description", "source_table": metric,
                        "source_column": "definition", "evidence_ids": [m_ev]}]),
        DerivedType(id="type:revenue", parent="Measure", category="business_type",
                    label="收入", definition="收入度量",
                    evidence_ids=[v_ev], source_properties=[{
                        "role": "description", "source_table": measure,
                        "source_column": "definition", "evidence_ids": [v_ev]}]),
    ])
    concepts = [
        {"id": "concept:profit", "ontology_level": "type", "ontology_type_id": "type:profit"},
        {"id": "concept:revenue", "ontology_level": "type", "ontology_type_id": "type:revenue"},
    ]
    alignments = [
        {"id": "alignment:profit", "mapping_kind": "exact",
         "source_record_id": data.record_id(metric, m_record), "concept_id": "concept:profit",
         "evidence_ids": [f"schema:{metric}:metric_name"]},
        {"id": "alignment:revenue", "mapping_kind": "exact",
         "source_record_id": data.record_id(measure, v_record), "concept_id": "concept:revenue",
         "evidence_ids": [f"schema:{measure}:measure_name"]},
    ]
    spec = {"spec_id": "profit-measure", "configuration_table": config,
            "source_code_column": "metric_ref", "target_code_column": "measure_ref",
            "source_definition_table": metric, "source_definition_code_column": "metric_code",
            "target_definition_table": measure, "target_definition_code_column": "measure_code",
            "relation_text_column": "relation_text"}
    return data, plan, concepts, alignments, spec


def test_config_row_is_only_evidence_and_two_endpoints_are_exact(tmp_path):
    data, plan, concepts, alignments, spec = _input(tmp_path)
    try:
        result = discover_configuration_relations(data, plan, concepts, alignments, [spec])
        by_codes = {(item["source_code"], item["target_code"]): item
                    for item in result["candidates"]}
        assert result["coverage"]["specs"][0]["input_rows"] == 6
        assert result["coverage"]["specs"][0]["distinct_code_pairs"] == 5
        accepted = by_codes[("I1", "M1")]
        assert accepted["status"] == "endpoint_verified_candidate"
        assert accepted["source_type_id"] == "type:profit"
        assert accepted["target_type_id"] == "type:revenue"
        assert accepted["configuration_rows_with_same_code_pair"] == 2
        assert accepted["configuration_record_id"] not in {
            accepted["source_definition"]["record_id"],
            accepted["target_definition"]["record_id"],
        }
        assert accepted["predicate_status"] == "unjudged"
        assert accepted["predicate_literal"] == "经营利润依赖收入"
        assert accepted["semantic_status"] == "code_references_do_not_prove_relation_meaning"
        assert any(data.evidence[e]["source_ref"].get("column") == "metric_ref"
                   for e in accepted["evidence_ids"] if e in data.evidence)
        assert by_codes[("I1", "M2")]["target_definition"]["status"] == (
            "definition_without_exact_accepted_type")
        assert by_codes[("I1", "M_DUP")]["target_definition"]["status"] == (
            "ambiguous_definition")
        assert by_codes[("I_MISSING", "M1")]["source_definition"]["status"] == (
            "missing_definition")
        assert by_codes[(None, "M1")]["source_definition"]["status"] == "blank_code"
        assert all(item["source_type_id"] is None and item["target_type_id"] is None
                   for item in by_codes.values() if item["status"] == "unresolved")
    finally:
        data.close()


def test_cap_is_explicit_and_does_not_skip_full_input_count(tmp_path):
    data, plan, concepts, alignments, spec = _input(tmp_path)
    try:
        result = discover_configuration_relations(
            data, plan, concepts, alignments, [spec], max_pairs_per_spec=1)
        report = result["coverage"]["specs"][0]
        assert report["scan_scope"] == "full_input"
        assert report["input_rows"] == 6
        assert report["distinct_code_pairs"] == 5
        assert report["pairs_emitted"] == 1
        assert report["pairs_omitted_limit"] == 4
        assert report["partial"] is True
        assert result["candidates"][0]["configuration_rows_with_same_code_pair"] == 2
    finally:
        data.close()


def test_invalid_or_sensitive_spec_is_rejected(tmp_path):
    data, plan, concepts, alignments, spec = _input(tmp_path)
    try:
        with pytest.raises(ValueError, match="Unknown configuration reference column"):
            discover_configuration_relations(
                data, plan, concepts, alignments, [{**spec, "source_code_column": "missing"}])
        with pytest.raises(ValueError, match="must differ"):
            discover_configuration_relations(
                data, plan, concepts, alignments, [{**spec, "source_code_column": "measure_ref"}])
        with pytest.raises(ValueError, match="Sensitive configuration reference column"):
            discover_configuration_relations(
                data, plan, concepts, alignments, [{**spec, "relation_text_column": "secret_token"}])
        with pytest.raises(ValueError, match="positive integer"):
            discover_configuration_relations(
                data, plan, concepts, alignments, [spec], max_pairs_per_spec=0)
    finally:
        data.close()


def _rule(rule_id, spec, source_field, target_table, target_field):
    return {
        "rule_id": rule_id, "status": "checked_technical",
        "source": {"table": spec["configuration_table"], "field": source_field},
        "target": {"table": target_table, "field": target_field},
        "transform": {"operator": "identity"}, "selector": {}, "scope_bindings": {},
        "verification": {"scan_scope": "full_input", "checks": {
            "eligible_references": 3, "unique_matches": 3,
            "ambiguous_matches": 0, "missing_in_input": 0, "missing_scope": 0,
        }},
    }


def test_infer_spec_from_two_checked_rules_and_keep_direction_unjudged(tmp_path):
    data, plan, concepts, alignments, spec = _input(tmp_path)
    try:
        rules = [
            _rule("a-metric", spec, "metric_ref", spec["source_definition_table"],
                  "metric_code"),
            _rule("b-measure", spec, "measure_ref", spec["target_definition_table"],
                  "measure_code"),
        ]
        inferred = infer_configuration_specs(data, plan, concepts, alignments, rules)
        assert inferred["coverage"]["rules_inspected"] == 2
        assert inferred["coverage"]["specs_emitted"] == 1
        item = inferred["spec_candidates"][0]
        assert item["rule_ids"] == ["a-metric", "b-measure"]
        assert item["direction_status"] == "unjudged"
        assert item["spec"]["relation_text_column"] == "relation_text"
        assert item["semantic_status"] == (
            "two_verified_reference_rules_are_not_a_business_predicate")
        resolved = discover_configuration_relations(
            data, plan, concepts, alignments, [item["spec"]])
        verified = next(x for x in resolved["candidates"]
                        if x["status"] == "endpoint_verified_candidate")
        assert verified["direction_status"] == verified["predicate_status"] == "unjudged"

        # Two rules to the same accepted root are alignment leads, even if
        # their raw-key checks happen to pass.
        same_root = [rules[0], _rule(
            "c-metric-alias", spec, "measure_ref", spec["source_definition_table"],
            "metric_code")]
        blocked = infer_configuration_specs(data, plan, concepts, alignments, same_root)
        assert blocked["spec_candidates"] == []
        assert blocked["coverage"]["skipped_reasons"]["same_business_root_alignment_lead"] == 1

        conditioned = [{**rules[0], "selector": {"business_type": "API"}}, rules[1]]
        rejected = infer_configuration_specs(data, plan, concepts, alignments, conditioned)
        assert rejected["spec_candidates"] == []
        assert rejected["coverage"]["skipped_reasons"][
            "condition_not_supported_by_dual_code_spec"] == 1
    finally:
        data.close()
