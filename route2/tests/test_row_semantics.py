"""Fact/config separation and bounded exact grain statistics."""

from ontology_r2.instance_bundles import _exact_evidence_signature, build_instance_bundles
from ontology_r2.row_semantics import classify_row_purpose, profile_joint_distinct
from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from ontology_r2.storage import Dataset
from test_semantic_cards import _table


def test_business_fact_is_not_a_concept_seed_and_joint_distinct_does_not_expand_rows(tmp_path):
    root = tmp_path / "input"
    _table(root, "fruit_business_fact", {
        "fruit_fact_id": "记录 ID", "fruit_code": "水果编码", "region_code": "经营地区编码",
        "period": "会计期", "profit_amount": "经营利润金额",
    }, [
        {"fruit_fact_id": "1", "fruit_code": "APPLE", "region_code": "EAST", "period": "2025Q1", "profit_amount": "100"},
        {"fruit_fact_id": "2", "fruit_code": "APPLE", "region_code": "EAST", "period": "2025Q1", "profit_amount": "110"},
        {"fruit_fact_id": "3", "fruit_code": "APPLE", "region_code": "SOUTH", "period": "2025Q1", "profit_amount": "120"},
    ], pk="fruit_fact_id")
    _table(root, "fruit_metric_definition", {
        "id": "记录 ID", "metric_name": "指标名称", "definition": "指标定义",
    }, [{"id": "1", "metric_name": "苹果经营利润", "definition": "苹果销售收入减去经营成本"}])
    _table(root, "fruit_api_input_param", {
        "id": "记录 ID", "param_name": "入参名称", "description": "参数说明",
    }, [{"id": "1", "param_name": "region_code", "description": "查询地区参数"}])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        fact = data.tables["fruit.fruit_business_fact"]
        assert classify_row_purpose(fact)["purpose"] == "business_fact"
        joint = profile_joint_distinct(data, "fruit.fruit_business_fact")
        assert joint["input_rows"] == 3
        grain = next(item for item in joint["candidate_sets"]
                     if item["label"] == "observation_coordinates")
        assert grain["distinct_tuples"] == 2
        assert grain["duplicate_rows"] == 1
        assert all(item["distinct_tuples"] <= 3 for item in joint["candidate_sets"])
        assert classify_row_purpose(data.tables["fruit.fruit_api_input_param"])["purpose"] == "configuration_data"

        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        coverage = built["coverage"]
        assert coverage["rows_skipped_business_fact"] == 3
        assert coverage["by_table"]["fruit.fruit_business_fact"]["joint_distinct"]["input_rows"] == 3
        index = SemanticCardIndex(built["index_path"])
        try:
            assert index.db.execute("SELECT count(*) FROM cards WHERE kind='definition'").fetchone()[0] == 1
            assert index.db.execute("SELECT kind FROM cards WHERE table_name='fruit.fruit_api_input_param'").fetchone()[0] == "reference"
            packets = build_instance_bundles(data, index, {"rules": []}, {
                "max_concept_bundles": 1, "max_relation_bundles": 0})
            assert len(packets["bundles"]) == 1
            assert packets["bundles"][0]["input_mode"] == "single_definition"
            assert packets["coverage"]["novelty"]["single_definition_bundles"] == 1
        finally:
            index.close()
    finally:
        data.close()


def test_exact_evidence_signature_preserves_definition_and_scope():
    base = {"kind": "definition", "root_hint": "Metric", "fields": {
        "name": [{"column": "metric_name", "value": "利润"}],
        "description": [{"column": "definition", "value": "收入减成本"}],
        "scope": [{"column": "region", "value": "华东"}],
    }}
    changed = {**base, "fields": {**base["fields"],
                                "scope": [{"column": "region", "value": "华南"}]}}
    assert _exact_evidence_signature(base) != _exact_evidence_signature(changed)
    assert _exact_evidence_signature(base) == _exact_evidence_signature(base)
    assert _exact_evidence_signature({**base, "table": "fruit.a"}) != _exact_evidence_signature(
        {**base, "table": "fruit.b"})
    truncated_a = {**base, "card_id": "card:a", "fields": {
        **base["fields"], "description": [{"column": "definition", "value": "same prefix",
                                        "truncated": True}]}}
    truncated_b = {**truncated_a, "card_id": "card:b"}
    assert _exact_evidence_signature(truncated_a) != _exact_evidence_signature(truncated_b)


def test_control_name_conflicting_with_fact_shape_remains_unresolved():
    fields = [("region_code", "地区编码", "text"),
              ("period", "会计期", "text"),
              ("profit_amount", "经营利润", "numeric")]
    table = {"schema": "fruit", "table_name": "fruit_rule", "pk": [],
             "column_names": [name for name, _, _ in fields],
             "columns": [{"column_name": name, "column_comment": comment, "data_type": dtype}
                         for name, comment, dtype in fields]}
    purpose = classify_row_purpose(table)
    assert purpose["purpose"] == "unresolved"
    assert purpose["reason"] == "control_table_name_and_business_fact_evidence_coexist"
    assert purpose["classification_granularity"] == "table_level_heuristic"
