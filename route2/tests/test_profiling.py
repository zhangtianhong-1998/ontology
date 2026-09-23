from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from ontology_r2.profiling import profile_fields
from ontology_r2.storage import Dataset, write_yaml


def _dataset(tmp_path):
    root = tmp_path / "input"
    for folder in ("data", "schema/tables", "schema/constraints", "schema/foreign_keys"):
        (root / folder).mkdir(parents=True)
    columns = [dict(column_name=name, ordinal_position=index + 1, data_type="text", is_not_null=False,
                    default_value=None, column_comment="") for index, name in enumerate(("id", "value"))]
    write_yaml(root / "schema/tables/values.yaml", dict(schema="demo", table_name="values", columns=columns))
    write_yaml(root / "schema/constraints/values.yaml", dict(schema="demo", table_name="values", constraints=[]))
    write_yaml(root / "schema/foreign_keys/values.yaml", dict(schema="demo", table_name="values", foreign_keys=[]))
    (root / "data/values.csv").write_text(
        'id,value\n1,\n2,""\n3," \t"\n4,001\n5,"[1]"\n6,"中"\n7,N/A\n', encoding="utf-8"
    )
    work = tmp_path / "work"
    work.mkdir()
    return Dataset(root, work)


def test_csv_syntax_null_empty_whitespace_and_usable_are_distinct(tmp_path):
    data = _dataset(tmp_path)
    try:
        item = next(p for p in data.tables["demo.values"]["profiles"] if p["column"] == "value")
        assert item["row_count"] == 7
        assert (item["null_count"], item["empty_count"], item["whitespace_only_count"], item["usable_count"]) == (1, 1, 1, 4)
        assert item["non_null"] == 6
        assert item["leading_zero_shape_count"] == 1
        assert item["numeric_shape_count"] == 1
        assert item["json_start_count"] == 1
        assert item["min_chars_usable"] == 1
        assert item["max_bytes_usable"] >= 3  # UTF-8 value 中
        assert item["csv_null_semantics"] == "syntax_only_original_database_unknown"
        assert item["scan_scope"] == "full_input" and item["input_scope"] == "unknown"
        assert item["statistics"]["approx_distinct"]["exact"] is False
        assert item["statistics"]["usable_count"] == {
            "value": 4, "exact": True, "method": "count", "population": "all_input_rows",
            "rows_scanned": 7, "status": "complete",
        }
        assert item["sample_exhaustive_in_input"]
        assert "" in item["distinct_sample"] and "001" in item["distinct_sample"]
        assert "statistics" not in data.context()["tables"][0]["profiles"][0]
    finally:
        data.close()


class _CountingDb:
    def __init__(self, db):
        self.db, self.queries = db, []

    def execute(self, sql):
        self.queries.append(sql)
        return self.db.execute(sql)


def test_40_fields_use_two_aggregate_queries_and_bounded_samples(tmp_path):
    db = duckdb.connect()
    columns = [f"c{i}" for i in range(40)]
    db.execute("CREATE TABLE src AS SELECT row_number() OVER () AS __r2_row, * FROM (SELECT " + ", ".join(f"'v{i}' AS {name}" for i, name in enumerate(columns)) + ")")
    counted = _CountingDb(db)
    data = SimpleNamespace(root=tmp_path, db=counted, tables={"demo.wide": {
        "sql_name": "src", "column_names": columns, "rows": 1,
        "csv_path": str(tmp_path / "wide.csv"), "csv_hash": "synthetic",
    }})
    profiles = profile_fields(data, {"sql_columns_per_group": 64, "input_scope": "sample"})["demo.wide"]
    assert len(profiles) == 40
    assert len([q for q in counted.queries if "count(*)" in q]) == 2
    assert len([q for q in counted.queries if "ORDER BY __r2_row LIMIT 2048" in q]) == 2
    assert all(p["input_scope"] == "sample" and p["sample_exhaustive_in_input"] for p in profiles)
    with pytest.raises(ValueError, match="positive"):
        profile_fields(data, {"sql_columns_per_group": 0})
    db.close()
