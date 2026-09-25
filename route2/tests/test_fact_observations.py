"""Observed fact tuples are candidates, not generated ontology instances."""

import pytest

from ontology_r2.fact_observations import build_fact_observation_candidates
from ontology_r2.storage import Dataset
from test_semantic_cards import _table


def _dataset(tmp_path):
    root = tmp_path / "input"
    _table(root, "fruit_profit_fact", {
        "fact_id": "记录 ID", "fruit_code": "水果编码", "region_code": "地区编码",
        "period": "会计期", "profit_amount": "经营利润金额",
    }, [
        {"fact_id": "1", "fruit_code": "APPLE", "region_code": "EAST",
         "period": "2025Q1", "profit_amount": "100"},
        {"fact_id": "2", "fruit_code": "APPLE", "region_code": "EAST",
         "period": "2025Q1", "profit_amount": "100"},
        {"fact_id": "3", "fruit_code": "APPLE", "region_code": "EAST",
         "period": "2025Q1", "profit_amount": "110"},
        {"fact_id": "4", "fruit_code": "BANANA", "region_code": "SOUTH",
         "period": "2025Q2", "profit_amount": "120"},
    ], pk="fact_id")
    _table(root, "fruit_metric_definition", {
        "id": "记录 ID", "metric_name": "指标名称", "definition": "指标定义",
    }, [{"id": "1", "metric_name": "经营利润", "definition": "收入减成本"}])
    _table(root, "opaque_table", {"id": "记录 ID", "payload": "附加文本"},
           [{"id": "1", "payload": "未知"}])
    work = tmp_path / "work"
    work.mkdir()
    return Dataset(root, work)


def test_only_observed_tuples_are_emitted_with_conflict_and_lineage(tmp_path):
    data = _dataset(tmp_path)
    try:
        output = build_fact_observation_candidates(data)
        reports = {item["table"]: item for item in output["tables"]}
        fact = reports["fruit.fruit_profit_fact"]
        assert fact["row_purpose"] == "business_fact"
        assert fact["scan_scope"] == "full_input_for_selected_value_columns"
        assert fact["emitted_candidates"] == 3
        assert fact["omitted_observed_tuples"] == 0
        assert fact["value_fields"][0]["complete_rows"] == 4
        assert fact["value_fields"][0]["observed_tuples"] == 3
        assert all(item["candidate_status"] == "candidate_only"
                   and item["business_type_binding"] == "unresolved"
                   for item in fact["candidates"])
        same = next(item for item in fact["candidates"]
                    if item["observed_value"] == "100")
        assert same["source_row_count"] == 2
        assert same["duplicate_source_rows"] == 1
        assert (same["source_row_min"], same["source_row_max"]) == (1, 2)
        assert same["source_row_range_is_exact_membership"] is False
        assert same["distinct_values_at_selected_coordinates"] == 2
        assert "multiple_values_at_selected_coordinates" in same["ambiguity"]
        # APPLE/EAST and BANANA/SOUTH occur; the unobserved cross-products do not.
        assert not any(item["coordinate_values"].get("fruit_code") == "APPLE"
                       and item["coordinate_values"].get("region_code") == "SOUTH"
                       for item in fact["candidates"])
        assert reports["fruit.fruit_metric_definition"]["row_purpose"] == "definition_data"
        assert reports["fruit.fruit_metric_definition"]["candidates"] == []
        assert reports["fruit.opaque_table"]["row_purpose"] == "unresolved"
        assert reports["fruit.opaque_table"]["reason"] == "mixed_or_insufficient_row_purpose_evidence"
        assert output["coverage"]["unresolved_tables"] == 1
    finally:
        data.close()


def test_candidate_cap_reports_exact_omission_and_is_repeatable(tmp_path):
    data = _dataset(tmp_path)
    try:
        first = build_fact_observation_candidates(data, max_candidates_per_table=2)
        second = build_fact_observation_candidates(data, max_candidates_per_table=2)
        assert first == second
        fact = next(item for item in first["tables"]
                    if item["table"] == "fruit.fruit_profit_fact")
        field = fact["value_fields"][0]
        assert field["observed_tuples"] == 3
        assert field["emitted_tuples"] == 2
        assert field["omitted_observed_tuples"] == 1
        assert field["complete_source_rows_not_emitted"] == 1
        assert fact["emitted_candidates"] == 2
        assert first["coverage"]["omitted_observed_tuples"] == 1
    finally:
        data.close()


def test_numeric_value_column_does_not_become_a_dimension_from_business_comment(tmp_path):
    data = _dataset(tmp_path)
    try:
        table = data.tables["fruit.fruit_profit_fact"]
        next(column for column in table["columns"]
             if column["column_name"] == "profit_amount")["column_comment"] = "水果销售利润（元）"
        result = build_fact_observation_candidates(data)
        fact = next(item for item in result["tables"]
                    if item["table"] == "fruit.fruit_profit_fact")
        assert fact["selected_columns"]["dimensions"] == ["fruit_code", "region_code"]
        assert fact["selected_columns"]["values"] == ["profit_amount"]
        assert fact["emitted_candidates"] == 3
    finally:
        data.close()


@pytest.mark.parametrize("kwargs", [
    {"max_candidates_per_table": 0},
    {"max_dimension_columns": True},
    {"max_time_columns": -1},
    {"max_value_columns": 1.5},
])
def test_invalid_limits_fail_explicitly(tmp_path, kwargs):
    data = _dataset(tmp_path)
    try:
        with pytest.raises(ValueError, match="positive integer"):
            build_fact_observation_candidates(data, **kwargs)
    finally:
        data.close()
