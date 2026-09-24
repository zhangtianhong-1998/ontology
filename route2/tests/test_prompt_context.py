"""The model sees semantic evidence, while source mappings retain every field."""

import asyncio
import csv
from pathlib import Path

from ontology_r2.incremental import core_context, direct_mapping, unit_context
from ontology_r2.llm import StructuredLLM
from ontology_r2.pipeline import build
from ontology_r2.storage import Dataset, read_yaml, write_yaml
from test_pipeline import setup


def test_prompt_omits_empty_and_audit_values_but_keeps_physical_mapping(tmp_path):
    config = setup(tmp_path, mcp=False)
    root = Path(config["dataset"])
    schema = root / "schema/tables/records.yaml"
    table = read_yaml(schema)
    for name, data_type, comment in (
        ("creation_date", "date", "记录创建日期"),
        ("empty_column", "text", "备用字段"),
        ("transaction_date", "date", "交易日期"),
    ):
        table["columns"].append({"column_name": name, "ordinal_position": len(table["columns"]) + 1,
                                 "data_type": data_type, "column_comment": comment,
                                 "is_not_null": False, "default_value": None})
    write_yaml(schema, table)
    csv_file = root / "data/records.csv"
    with csv_file.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    with csv_file.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=[c["column_name"] for c in table["columns"]])
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "creation_date": "2026-01-01",
                             "empty_column": "", "transaction_date": "2026-01-02"})

    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        context = unit_context(data, "demo.records", ["demo.catalog"])
        current = next(item for item in context["tables"] if item["name"] == "demo.records")
        visible = {column["column_name"] for column in current["columns"]}
        assert "transaction_date" in visible
        assert "creation_date" not in visible and "empty_column" not in visible
        assert "empty_column" in current["empty_in_input"]
        assert any(binding["source_column"] == "creation_date"
                   for binding in current["deterministic_bindings"])
        assert all("creation_date" not in row["values"] for row in current["sample"])
        assert all("columns" not in item for item in context["table_catalog"])
        plan, mapping = direct_mapping(data)
        record_plan = next(item for item in plan.tables if item.table == "demo.records")
        assert {"creation_date", "empty_column", "transaction_date"} <= set(record_plan.attributes.values())
        assert len(mapping["tables"]) == 2
        compact = core_context(plan, "demo.records", data)
        assert all("attributes" not in item for item in compact["tables"])
        assert any(item["mapped_attribute_count"] == len(record_plan.attributes)
                   for item in compact["tables"])
    finally:
        data.close()


def test_timeout_does_not_repeat_the_same_plan_five_times(tmp_path, monkeypatch):
    config = setup(tmp_path, scenario="unrelated", mcp=False)
    config["llm"]["max_repairs"] = 4
    config["discovery"] = {"enabled": False}
    config["processing"]["materialize_all_objects"] = False

    async def timeout(self, task, payload, schema):
        raise TimeoutError("synthetic provider timeout")

    monkeypatch.setattr(StructuredLLM, "ask", timeout)
    result = asyncio.run(build(config, tmp_path / "run"))
    steps = read_yaml(tmp_path / "run/construction.yaml")["steps"]
    assert "demo.records" in read_yaml(tmp_path / "run/column_roles.yaml")
    assert result["status"] == "partial"
    assert "incremental_units_not_accepted" in result["partial_reasons"]
    assert all(step["status"] == "timeout" and len(step["attempts"]) == 1 for step in steps)
    assert all(step["stop_reason"] == "unchanged_request_would_repeat_timeout" for step in steps)


def test_bounded_concept_and_alias_recall_are_exported(tmp_path):
    config = setup(tmp_path, mcp=False)
    config["concept_recall"] = {"enabled": True, "max_records_per_table": 4,
                                "max_total_records": 8, "max_candidates_per_unit": 1}
    config["alias_recall"] = {"enabled": True, "max_rows_per_table": 8,
                              "max_fields": 8}
    result = asyncio.run(build(config, tmp_path / "run"))
    assert result["status"] == "complete", result
    for name in ("concept_candidates.yaml", "alias_candidates.yaml", "column_roles.yaml"):
        assert (tmp_path / "run" / name).exists()
    assert result["concept_recall"]["coverage"]["input_scope"] == "imported_csv_snapshot_only"
    assert result["alias_recall"]["coverage"]["input_scope"] == "imported_csv_snapshot"
