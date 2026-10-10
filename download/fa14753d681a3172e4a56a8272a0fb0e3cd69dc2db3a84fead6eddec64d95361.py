"""The synthetic fixture tests input contracts, not business ontology quality."""
import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/generate_fruit_data.py"
spec = importlib.util.spec_from_file_location("generate_fruit_data", SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_full_scale_target_and_declared_columns():
    assert len(module.TABLES) == 23
    assert sum(table.audit for table in module.TABLES) == 21
    assert sum(module.scaled_counts(1.0).values()) == 1_102_238
    assert all(len(table.fields) == table.expected_columns for table in module.TABLES)
    assert module.TABLE_BY_NAME["fruit_import_staging_temp"].pk is None


def test_small_fixture_schema_refs_and_formula_ratio(tmp_path):
    root = tmp_path / "fruit_data"
    result = module.generate(root, scale=0.001)
    assert result["table_count"] == 23
    assert result["row_count"] == 1213
    assert result["parseable_formulas"] / result["metric_detail_rows"] >= 0.1
    assert set(module.SOURCE_TYPES) == set(result["source_types"])
    assert set(result["business_types"]) == {"API", "CARD", "TABLE"}
    assert result["valid_metric_reference_codes"] > 0
    assert result["valid_measure_reference_codes"] > 0
    assert result["polymorphic_business_targets_checked"] > 0
    assert result["parameter_references_checked"] == module.scaled_counts(0.001)["fruit_param_ref_rule"]
    assert result["dimension_rule_values_checked"] > 0

    for table in module.TABLES:
        schema = yaml.safe_load((root / "schema/tables" / f"{table.name}.yaml").read_text())
        with (root / "data" / f"{table.name}.csv").open(newline="", encoding="utf-8") as stream:
            assert next(csv.reader(stream)) == [c["column_name"] for c in schema["columns"]]
        fk = yaml.safe_load((root / "schema/foreign_keys" / f"{table.name}.yaml").read_text())
        assert fk["foreign_keys"] == []
    with (root / "data/fruit_dim_definition.csv").open(newline="", encoding="utf-8") as stream:
        dim_rows = list(csv.DictReader(stream))
    assert len({row["dim_code"] for row in dim_rows}) == len(dim_rows)
    with (root / "data/fruit_dim_member.csv").open(newline="", encoding="utf-8") as stream:
        members = list(csv.DictReader(stream))
    assert len({(row["dim_code"], row["member_code"]) for row in members}) == len(members)
    member_keys = {(row["dim_code"], row["member_code"]) for row in members}
    param_targets = {}
    for table, key in (("fruit_api_input_param", "fruit_input_param_id"),
                       ("fruit_api_output_param", "fruit_output_param_id")):
        with (root / "data" / f"{table}.csv").open(newline="", encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                assert row[key] not in param_targets
                param_targets[row[key]] = (table, row["business_type"], row["business_id"])
    referenced_param_tables = set()
    with (root / "data/fruit_param_ref_rule.csv").open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            target_table, kind, business_id = param_targets[row["param_id"]]
            referenced_param_tables.add(target_table)
            assert (kind, business_id) == (row["business_type"], row["business_id"])
            if row["source_type"].startswith("DIM"):
                assert all((row["source_type"], value) in member_keys
                           for value in json.loads(row["field_rule"]))
    assert referenced_param_tables == {"fruit_api_input_param", "fruit_api_output_param"}
    with (root / "data/fruit_wide_table_column.csv").open(newline="", encoding="utf-8") as stream:
        wide_fields = list(csv.DictReader(stream))
    assert len({(row["fruit_wide_table_id"], row["physical_field_name"])
                for row in wide_fields}) == len(wide_fields)
    manifest = yaml.safe_load((root / "synthetic_manifest.yaml").read_text())
    assert manifest["synthetic"] is True
    assert "22 tables" in manifest["assumption"]


def test_generator_refuses_overwrite_and_bad_scale(tmp_path):
    root = tmp_path / "fruit_data"
    module.generate(root, scale=0.001)
    with pytest.raises(FileExistsError):
        module.generate(root, scale=0.001)
    with pytest.raises(ValueError, match="positive"):
        module.scaled_counts(0)
    with pytest.raises(ValueError, match="positive"):
        module.scaled_counts(float("nan"))


def _replace_first_csv_row(path, **changes):
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        fields, rows = reader.fieldnames, list(reader)
    rows[0].update(changes)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_validator_rejects_unknown_dimension_allowed_member(tmp_path):
    root = tmp_path / "fruit_data"
    module.generate(root, scale=0.001)
    _replace_first_csv_row(root / "data/fruit_param_ref_rule.csv",
                           field_rule='["UNKNOWN_MEMBER"]')
    with pytest.raises(ValueError, match="Dimension field_rule contains unknown member"):
        module.validate(root)


def test_validator_rejects_parameter_scope_mismatch(tmp_path):
    root = tmp_path / "fruit_data"
    module.generate(root, scale=0.001)
    _replace_first_csv_row(root / "data/fruit_param_ref_rule.csv", business_id="2")
    with pytest.raises(ValueError, match="Parameter reference missing or scope mismatch"):
        module.validate(root)
