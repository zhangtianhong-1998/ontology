import csv

import pytest

from ontology_r2.concept_candidates import _field_roles, recall_concept_candidates
from ontology_r2.pipeline import attach_concept_evidence
from ontology_r2.storage import Dataset, write_yaml


def definition_table(root, name, rows):
    columns = {
        "id": "记录编号", "display_name": "定义名称", "alias": "别名",
        "description": "含义说明", "calculation_rule": "计算口径",
        "unit": "单位", "scope": "适用范围",
    }
    base = {"schema": "demo", "table_name": name}
    write_yaml(root / "schema/tables" / f"{name}.yaml", {
        **base, "table_comment": "合成定义记录", "columns": [
            {"column_name": field, "ordinal_position": position + 1,
             "data_type": "text", "is_not_null": False, "default_value": None,
             "column_comment": comment}
            for position, (field, comment) in enumerate(columns.items())],
    })
    write_yaml(root / "schema/constraints" / f"{name}.yaml", {
        **base, "constraints": [{"constraint_name": "id_pk", "constraint_type": "p",
                                "definition": 'PRIMARY KEY ("id")'}],
    })
    write_yaml(root / "schema/foreign_keys" / f"{name}.yaml", {**base, "foreign_keys": []})
    (root / "data").mkdir(exist_ok=True)
    with (root / "data" / f"{name}.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def dataset(tmp_path):
    root = tmp_path / "input"
    definition_table(root, "definitions_one", [{
        "id": "1", "display_name": "一区库存量", "alias": "",
        "description": "一区仓库内的库存数量", "calculation_rule": "按日末余额统计",
        "unit": "件", "scope": "一区",
    }])
    definition_table(root, "definitions_two", [{
        "id": "2", "display_name": "二区库存量", "alias": "",
        "description": "二区渠道中的库存数量", "calculation_rule": "按可售余额统计",
        "unit": "箱", "scope": "二区",
    }, {
        "id": "3", "display_name": "设备运行时长", "alias": "",
        "description": "设备运行的持续时间", "calculation_rule": "结束减开始",
        "unit": "小时", "scope": "设备",
    }])
    work = tmp_path / "work"
    work.mkdir()
    return Dataset(root, work)


def test_shared_name_recalls_evidence_without_merging_concepts(tmp_path):
    data = dataset(tmp_path)
    try:
        result = recall_concept_candidates(data)
        pair = next(item for item in result["candidates"]
                    if {record["fields"]["name"][0]["value"] for record in item["records"]}
                    == {"一区库存量", "二区库存量"})
        assert pair["retrieval_channels"] == ["name_or_alias_fragment"]
        assert pair["unit_observation"] == "different"
        assert pair["decision"] == {"status": "proposed", "same_concept": "unresolved"}
        assert pair["source_snapshot"] == data.snapshot_id
        for record in pair["records"]:
            assert all(role in record["fields"] for role in
                       ("name", "description", "formula", "unit", "scope"))
            assert record["fields"]["description"][0]["schema_evidence_id"].startswith("schema:demo.")
            assert record["record_id"].startswith(record["table"] + ":")
        assert result["coverage"]["semantic_status"].startswith("unjudged")
    finally:
        data.close()


def test_sql_record_limit_reports_unseen_definition_rows(tmp_path):
    data = dataset(tmp_path)
    try:
        result = recall_concept_candidates(data, max_records_per_table=1,
                                           max_total_records=2, max_field_chars=8)
        assert result["coverage"]["definition_records_sampled"] == 2
        assert result["coverage"]["partial"] is True
        assert any(table["sample_truncated"] for table in result["coverage"]["tables"])
        assert all(len(value["value"]) <= 8 for item in result["candidates"]
                   for record in item["records"] for fields in record["fields"].values()
                   for value in fields)
        with pytest.raises(ValueError, match="positive integers"):
            recall_concept_candidates(data, max_candidates=0)
    finally:
        data.close()


def test_code_comment_mentioning_name_is_not_used_as_definition_name():
    table = {"columns": [{"column_name": "metric_code", "column_comment": "指标名称编码"}],
             "profiles": [{"column": "metric_code", "scan_scope": "full_input",
                           "usable_count": 10}]}
    assert _field_roles(table) == {}


def test_sampled_definition_fragments_are_citable_without_accepting_identity(tmp_path):
    data = dataset(tmp_path)
    try:
        result = recall_concept_candidates(data)
        attach_concept_evidence(data, result)
        candidate = result["candidates"][0]
        assert candidate["decision"]["same_concept"] == "unresolved"
        for record in candidate["records"]:
            for entries in record["fields"].values():
                for entry in entries:
                    evidence = data.evidence[entry["record_evidence_id"]]
                    assert evidence["source_ref"]["record_id"] == record["record_id"]
                    assert evidence["raw_fragment"] == entry["value"]
    finally:
        data.close()
