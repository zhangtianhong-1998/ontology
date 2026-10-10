"""Small synthetic checks for bounded alias and normalization recall."""

import duckdb
import pytest

from ontology_r2.storage import qi
from ontology_r2.value_aliases import propose_value_alias_candidates


class SmallDataset:
    def __init__(self, tables):
        self.db = duckdb.connect(":memory:")
        self.snapshot_id = "synthetic-alias-snapshot"
        self.tables = {}
        for index, (name, (columns, rows)) in enumerate(tables.items()):
            sql_name = f"src_{index}"
            fields = ", ".join(f"{qi(column)} VARCHAR" for column in columns)
            self.db.execute(f"CREATE TABLE {qi(sql_name)} (__r2_row BIGINT, {fields})")
            self.db.executemany(
                f"INSERT INTO {qi(sql_name)} VALUES ({','.join('?' for _ in range(len(columns) + 1))})",
                [(number, *row) for number, row in enumerate(rows, 1)],
            )
            self.tables[name] = {"sql_name": sql_name, "column_names": columns,
                                 "rows": len(rows)}

    def close(self):
        self.db.close()


def _pair(data, left, right, **limits):
    return propose_value_alias_candidates(data, fields=[left, right], **limits)


def test_standard_name_matches_alias_list_and_preserves_original_values():
    data = SmallDataset({
        "demo.metric": (["standard_name"], [("阿里云收入",), ("商业市场收入",)]),
        "demo.dictionary": (["metric_aliases"], [("阿里云收入；阿里云营收",)]),
    })
    try:
        result = _pair(data, ("demo.metric", "standard_name"),
                       ("demo.dictionary", "metric_aliases"))
        candidate = result["candidates"][0]
        assert candidate["orientation"] == "undetermined"
        assert candidate["decision"] == {"status": "proposed", "semantic_relation": "unresolved"}
        assert "alias_item_equal" in candidate["comparison_modes"]
        example = candidate["examples"][0]
        assert example["comparison_rule"] == "alias_item_equal"
        assert {example["source"]["raw_value"], example["target"]["raw_value"]} == {
            "阿里云收入", "阿里云收入；阿里云营收",
        }
        assert {example["source"]["rule"], example["target"]["rule"]} == {
            "raw_equal", "alias_list_item_nfkc_whitespace_casefold",
        }
        metric_side = ("source" if candidate["source"]["table"] == "demo.metric"
                       else "target")
        assert candidate["checks"][f"{metric_side}_raw_values_matched_in_sample"] == 1
        assert candidate["checks"][f"{metric_side}_distinct_raw_values_sampled"] == 2
        assert candidate["checks"][f"{metric_side}_sample_coverage"] == 0.5
        assert any(c["raw_value"] == "商业市场收入" for c in candidate["counterexamples"])
        assert any(c["matched_text"] == "阿里云营收" for c in candidate["counterexamples"])
    finally:
        data.close()


def test_unicode_normalization_collision_and_repeated_target_are_reported():
    data = SmallDataset({
        "demo.a": (["code"], [("ABC",)]),
        "demo.b": (["code"], [("ＡＢＣ",), ("abc",), ("abc",)]),
    })
    try:
        candidate = _pair(data, ("demo.a", "code"), ("demo.b", "code"))["candidates"][0]
        assert candidate["checks"]["shared_normalized_keys_in_sample"] == 1
        assert candidate["checks"]["normalization_collision_keys"] == 1
        assert candidate["checks"]["repeated_record_keys"] == 1
        assert candidate["checks"]["target_raw_values_matched_in_sample"] == 2
        assert "nfkc_whitespace_casefold" in candidate["retrieval_channels"]
        assert "normalized_equal" in candidate["comparison_modes"]
        assert {candidate["examples"][0]["source"]["raw_value"],
                candidate["examples"][0]["target"]["raw_value"]} & {"ＡＢＣ", "abc"}
    finally:
        data.close()


def test_sample_window_and_distinct_value_caps_are_explicit():
    data = SmallDataset({
        "demo.a": (["name"], [("A",), ("B",), ("C",), ("D",)]),
        "demo.b": (["name"], [("A",), ("X",)]),
    })
    try:
        result = _pair(data, ("demo.a", "name"), ("demo.b", "name"),
                       max_rows_per_table=3, max_values_per_field=2)
        candidate = result["candidates"][0]
        assert candidate["checks"]["source_sample_coverage"] == 0.5
        assert candidate["checks"]["target_sample_coverage"] == 0.5
        assert result["coverage"]["table_scans"]["demo.a"]["rows_scanned"] == 3
        assert result["coverage"]["table_scans"]["demo.a"]["value_cap_reached_fields"] == ["name"]
        assert result["coverage"]["partial"] is True
    finally:
        data.close()


def test_high_fanout_values_and_nonexplicit_synonyms_do_not_prove_relation():
    data = SmallDataset({
        "demo.a": (["name"], [("收入",)]),
        "demo.b": (["name"], [("收入",)]),
        "demo.c": (["name"], [("收入",)]),
    })
    try:
        result = propose_value_alias_candidates(data, max_common_key_fanout=2)
        assert result["candidates"] == []
        assert result["coverage"]["high_fanout_normalized_keys_skipped"] == 1
    finally:
        data.close()
    synonym_data = SmallDataset({
        "demo.a": (["name"], [("阿里云收入",)]),
        "demo.b": (["name"], [("阿里云营收",)]),
    })
    try:
        assert propose_value_alias_candidates(synonym_data)["candidates"] == []
    finally:
        synonym_data.close()


def test_unknown_fields_and_bad_limits_fail_before_sampling():
    data = SmallDataset({"demo.a": (["name"], [("A",)])})
    try:
        with pytest.raises(ValueError, match="Unknown alias candidate field"):
            propose_value_alias_candidates(data, fields=[("demo.a", "missing")])
        with pytest.raises(ValueError, match="positive integers"):
            propose_value_alias_candidates(data, max_rows_per_table=0)
    finally:
        data.close()


def test_pair_budget_bounds_output_and_reports_omitted_pair_key_events():
    data = SmallDataset({
        "demo.a": (["name"], [("shared",)]),
        "demo.b": (["name"], [("shared",)]),
        "demo.c": (["name"], [("shared",)]),
    })
    try:
        result = propose_value_alias_candidates(data, max_pairs=1)
        assert len(result["candidates"]) == 1
        assert result["coverage"]["pair_key_events_queued"] == 2
        assert result["coverage"]["partial"] is True
    finally:
        data.close()
