"""The path used to explain a relation must be the path executed by the plan."""
from copy import deepcopy
from pathlib import Path

import pytest

from ontology_r2.group_incremental import RelationBundleDecision, compile_relation
from ontology_r2.incremental import direct_mapping
from ontology_r2.storage import Dataset, read_yaml
from test_semantic_cards import _table


PROFILE = read_yaml(Path(__file__).resolve().parents[1] / "ontologies/internal_model.yaml")


def case(tmp_path, *, declaration="记录主键", description="通过 dim_code 引用第三方地区维度"):
    root = tmp_path / "input"
    for table in ("source", "target"):
        _table(root, table, {"id": declaration if table == "source" else "记录主键",
                            "name": "业务名称", "description": "说明", "dim_code": "第三方维度编码"},
               [{"id": "1", "name": table + "对象", "description": description,
                 "dim_code": "DIM0001"}])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    records = []
    for table in ("fruit.source", "fruit.target"):
        row = next(data.rows(table))
        records.append({"table": table, "record_id": data.record_id(table, row), "row_number": 1,
                        "fields": {"reference": [{"column": "id", "value": row["id"]},
                                                  {"column": "dim_code", "value": row["dim_code"]}],
                                   "name": [{"column": "name", "value": row["name"]}],
                                   "description": [{"column": "description", "value": row["description"]}]}})
    rule = {"rule_id": "numeric-pair", "snapshot_id": data.snapshot_id,
            "source": {"table": "fruit.source", "field": "id"},
            "target": {"table": "fruit.target", "field": "id"},
            "status": "checked_technical", "transform": {"operator": "identity"},
            "selector": {}, "scope_bindings": {}, "risk_flags": ["numeric_value_coincidence"],
            "verification": {"scan_scope": "full_input", "checks": {"eligible_references": 1, "unique_matches": 1}}}
    bundle = {"snapshot_id": data.snapshot_id, "rule": rule, "records": records,
              "examples": {"positive": [{"source_record_id": records[0]["record_id"],
                                         "target_record_id": records[1]["record_id"], "matching_raw_value": "1"}]}}
    decision = RelationBundleDecision(status="proposed", parent_relation="related_to", label="related_to",
                                      definition="两个对象共享地区维度因此关联", source_quote=description,
                                      target_quote=description)
    return data, bundle, decision


@pytest.mark.parametrize("risk_flag", [True, False])
def test_shared_third_party_dimension_cannot_justify_numeric_primary_key_join(tmp_path, risk_flag):
    data, bundle, decision = case(tmp_path)
    try:
        if not risk_flag:
            bundle["rule"].pop("risk_flags")
        core, _ = direct_mapping(data)
        with pytest.raises(ValueError, match="executed join path"):
            compile_relation(data, PROFILE, core, bundle, decision)
        assert not core.relations
    finally:
        data.close()


@pytest.mark.parametrize("support", ["foreign_key", "field_declaration", "record_definition"])
def test_numerical_reference_with_the_same_proven_and_executed_path_is_allowed(tmp_path, support):
    declaration = "引用 fruit.target.id" if support == "field_declaration" else "记录主键"
    description = "id 引用 fruit.target.id；用于对象配置" if support == "record_definition" else "对象配置"
    data, bundle, decision = case(tmp_path, declaration=declaration, description=description)
    try:
        if support == "foreign_key":
            data.tables["fruit.source"]["foreign_keys"] = [{"fk_name": "source_target", "column_name": "id",
                "referenced_schema": "fruit", "referenced_table": "target", "referenced_column": "id"}]
        core, _ = direct_mapping(data)
        compiled, plan = compile_relation(data, PROFILE, core, bundle, decision)
        assert len(compiled.relations) == 1
        assert (plan.source_column, plan.target_column) == ("id", "id")
        assert plan.witnessed_pairs[0].source_record_id == bundle["records"][0]["record_id"]
    finally:
        data.close()


def test_schema_reference_to_different_field_or_negated_reference_cannot_transfer(tmp_path):
    data, bundle, decision = case(tmp_path, declaration="不引用 fruit.target.id；引用 fruit.target.dim_code")
    try:
        core, _ = direct_mapping(data)
        with pytest.raises(ValueError, match="executed join path"):
            compile_relation(data, PROFILE, core, bundle, decision)
    finally:
        data.close()
