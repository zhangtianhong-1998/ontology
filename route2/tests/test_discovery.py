"""Synthetic checks for technical candidate recall and exact input statistics."""

import asyncio
import json
from pathlib import Path

import duckdb
import pytest

from ontology_r2.discovery import discover_and_check, propose_candidates, validate_candidate
from ontology_r2.demo import make_demo
from ontology_r2.pipeline import build
from ontology_r2.storage import qi, read_yaml


class SmallDataset:
    def __init__(self, tables):
        self.db = duckdb.connect(":memory:")
        self.snapshot_id = "synthetic-snapshot"
        self.tables = {}
        for index, (name, spec) in enumerate(tables.items()):
            columns, rows, pk = spec
            sql_name = f"src_{index}"
            fields = ", ".join(f"{qi(col)} VARCHAR" for col in columns)
            self.db.execute(f"CREATE TABLE {qi(sql_name)} (__r2_row BIGINT, {fields})")
            placeholders = ",".join("?" for _ in range(len(columns) + 1))
            self.db.executemany(f"INSERT INTO {qi(sql_name)} VALUES ({placeholders})",
                                [(i, *row) for i, row in enumerate(rows, 1)])
            self.tables[name] = {
                "schema": name.split(".", 1)[0],
                "table_name": name.split(".", 1)[1],
                "sql_name": sql_name, "column_names": columns,
                "pk": pk, "foreign_keys": [],
            }

    def close(self):
        self.db.close()


def find_candidate(result, source_table, source_field, target_table, target_field):
    return next(c for c in result["candidates"]
                if c["source"] == {"table": source_table, "field": source_field}
                and c["target"] == {"table": target_table, "field": target_field})


def test_coincident_auto_ids_are_unresolved_even_with_perfect_overlap():
    data = SmallDataset({
        "demo.source": (["id"], [(str(i),) for i in range(1, 4)], ["id"]),
        "demo.target": (["id"], [(str(i),) for i in range(1, 4)], ["id"]),
    })
    try:
        found = propose_candidates(data)
        candidate = find_candidate(found, "demo.source", "id", "demo.target", "id")
        assert candidate["retrieval_channels"] == ["value_overlap"]
        assert candidate["numeric_overlap_only"] is True
        checked = validate_candidate(data, candidate)
        assert checked["checks"]["unique_match_ratio"] == 1.0
        assert checked["decision"] == {"status": "checked", "semantic_relation": "unresolved"}
    finally:
        data.close()


def test_explicit_column_comment_recalls_pair_when_value_budget_skips_target():
    data = SmallDataset({
        "demo.source": (["ref", "other"], [("K17", "x")], []),
        "demo.target": (["code"], [("K17",)], []),
    })
    data.tables["demo.source"]["columns"] = [
        {"column_name": "ref", "column_comment": "引用 target.code"},
        {"column_name": "other", "column_comment": ""},
    ]
    try:
        found = propose_candidates(data, max_indexed_fields=1)
        candidate = find_candidate(found, "demo.source", "ref", "demo.target", "code")
        assert candidate["retrieval_channels"] == ["metadata"]
        assert found["coverage"]["value_index_fields"] == 1
    finally:
        data.close()


def test_audit_value_overlap_does_not_consume_candidate_budget():
    data = SmallDataset({
        "demo.source": (["creation_date", "ref_code"], [("2026-01-01", "K17")], []),
        "demo.target": (["creation_date", "code"], [("2026-01-01", "K17")], ["code"]),
    })
    for table in data.tables.values():
        table["columns"] = [
            {"column_name": name, "column_comment": "审计创建时间" if name == "creation_date"
             else "指标引用编码" if name == "ref_code" else "指标编码"}
            for name in table["column_names"]]
    try:
        found = propose_candidates(data, max_candidates_total=2)
        assert all("creation_date" not in (item["source"]["field"], item["target"]["field"])
                   for item in found["candidates"])
        find_candidate(found, "demo.source", "ref_code", "demo.target", "code")
        assert len(found["coverage"]["candidate_fields_excluded_empty_or_audit"]) == 2
    finally:
        data.close()


def test_low_prevalence_conditional_link_is_not_lost_to_whole_column_ratio():
    source = [("linked" if i in (7, 41) else "other",
               f"K{i:03}" if i in (7, 41) else f"U{i:03}")
              for i in range(100)]
    data = SmallDataset({
        "demo.source": (["kind", "ref_code"], source, []),
        "demo.target": (["code"], [("K007",), ("K041",)], ["code"]),
    })
    try:
        found = propose_candidates(data)
        candidate = find_candidate(found, "demo.source", "ref_code", "demo.target", "code")
        checked = validate_candidate(data, candidate, selector={"kind": "linked"})
        counts = checked["checks"]
        assert counts["eligible_references"] == 2
        assert counts["unique_matches"] == 2
        assert counts["unique_match_ratio"] == 1.0
        assert counts["whole_column_distinct_value_inclusion_ratio"] == 0.02
        assert counts["outside_eligible_references"] == 98
        assert counts["outside_matched_references"] == 0
    finally:
        data.close()


def test_ambiguous_target_and_blank_and_unknown_selector_are_separate():
    data = SmallDataset({
        "demo.source": (["kind", "ref"], [
            ("linked", "A"), ("linked", "B"), ("linked", ""),
            ("linked", "   "), ("linked", None), (None, "A"),
            ("other", "A")], []),
        "demo.target": (["code"], [("A",), ("A",), ("B",)], []),
    })
    try:
        candidate = {
            "candidate_id": "test", "source": {"table": "demo.source", "field": "ref"},
            "target": {"table": "demo.target", "field": "code"},
        }
        counts = validate_candidate(data, candidate, selector={"kind": "linked"})["checks"]
        assert counts["selector_true"] == 5
        assert counts["selector_false"] == 1
        assert counts["selector_unknown"] == 1
        assert counts["null_references"] == counts["empty_references"] == counts["whitespace_references"] == 1
        assert counts["eligible_references"] == 2
        assert counts["unique_matches"] == 1
        assert counts["ambiguous_matches"] == 1
        assert counts["target_duplicate_key_groups"] == 1
        assert counts["target_max_multiplicity"] == 2
        assert counts["outside_eligible_references"] == 1
        assert counts["outside_matched_references"] == 1
        assert counts["counterexample_rows"] == [{"row_number": 1, "reason": "ambiguous_target"}]
    finally:
        data.close()


def test_raw_composite_scope_resolves_duplicates_and_rejects_unknown_fields():
    data = SmallDataset({
        "demo.source": (["ref", "source_scope"], [("A", "north"), ("A", "south"),
                                                   ("A", "west"), ("A", "")], []),
        "demo.target": (["code", "target_scope"], [("A", "north"), ("A", "south")], []),
    })
    candidate = {"candidate_id": "test", "source": {"table": "demo.source", "field": "ref"},
                 "target": {"table": "demo.target", "field": "code"}}
    try:
        counts = validate_candidate(data, candidate,
                                    scope_bindings={"target_scope": "source_scope"})["checks"]
        assert counts["eligible_references"] == 3
        assert counts["unique_matches"] == 2
        assert counts["missing_in_input"] == 1
        assert counts["missing_scope"] == 1
        assert counts["distinct_key_inclusion_ratio"] == pytest.approx(2 / 3)
        with pytest.raises(ValueError, match="Unknown field"):
            validate_candidate(data, candidate, selector={"missing_column": "x"})
    finally:
        data.close()


def test_zero_eligible_reference_has_null_ratios():
    data = SmallDataset({
        "demo.source": (["ref"], [(None,), ("",)], []),
        "demo.target": (["code"], [("A",)], ["code"]),
    })
    candidate = {"candidate_id": "test", "source": {"table": "demo.source", "field": "ref"},
                 "target": {"table": "demo.target", "field": "code"}}
    try:
        checks = validate_candidate(data, candidate)["checks"]
        assert checks["eligible_references"] == 0
        assert checks["unique_match_ratio"] is None
        assert checks["whole_column_distinct_value_inclusion_ratio"] is None
    finally:
        data.close()


def test_failed_exact_check_stays_in_unchecked_coverage(monkeypatch):
    data = SmallDataset({
        "demo.source": (["shared_code"], [("A",)], []),
        "demo.target": (["shared_code"], [("A",)], []),
    })
    try:
        def fail(*args, **kwargs):
            raise RuntimeError("synthetic failure")
        monkeypatch.setattr("ontology_r2.discovery.validate_candidate", fail)
        result = discover_and_check(data, {"max_candidate_validations": 1})
        coverage = result["coverage"]
        assert coverage["candidate_check_errors"] == 1
        assert coverage["candidates_not_attempted"] == 1
        assert coverage["candidates_not_checked"] == 2
        assert len(coverage["candidate_ids_not_checked"]) == 2
        assert coverage["partial"] is True
    finally:
        data.close()


def test_pipeline_exports_bounded_checks_and_presents_only_checked_pairs(tmp_path):
    input_root = tmp_path / "input"
    make_demo(input_root, 8, "linked")
    project = Path(__file__).resolve().parents[1]
    config = {
        "dataset": str(input_root),
        "model_profile": str(project / "ontologies/internal_model.yaml"),
        "synthetic": True,
        "llm": {"mode": "mock", "responses": str(input_root / "mock_llm.yaml"),
                "max_calls": 30, "max_reserved_tokens": 2000000},
        "mcp": {"enabled": False}, "external": {"enabled": False},
        "processing": {"materialize_all_objects": True},
        "discovery": {"max_indexed_fields": 8, "max_candidate_validations": 4,
                      "max_evidence_per_unit": 2},
    }
    output = tmp_path / "run"
    result = asyncio.run(build(config, output))
    assert result["status"] == "complete", result
    candidates = read_yaml(output / "field_candidates.yaml")
    checks = read_yaml(output / "association_checks.yaml")
    coverage = read_yaml(output / "discovery_coverage.yaml")
    assert len(candidates) == len(checks) == coverage["candidates_checked"] == 4
    assert all(c["decision"]["semantic_relation"] == "unresolved" for c in candidates)
    assert all(check["scan_scope"] == "full_input" for check in checks)
    trace = [json.loads(line) for line in (output / "trace.jsonl").read_text().splitlines()]
    plan_call = next(x for x in trace if x.get("stage") == "llm_request"
                     and x.get("task") == "plan" and x["input"].get("unit") == "demo.records")
    evidence = plan_call["input"]["field_association_evidence"]
    assert 0 < len(evidence) <= 2
    assert {x["candidate_id"] for x in evidence} <= {x["candidate_id"] for x in checks}
    assert all(x["semantic_relation"] == "unresolved" for x in evidence)


def test_pipeline_reports_unindexed_fields_and_unchecked_candidates(tmp_path):
    input_root = tmp_path / "input"
    make_demo(input_root, 8, "linked")
    project = Path(__file__).resolve().parents[1]
    config = {
        "dataset": str(input_root),
        "model_profile": str(project / "ontologies/internal_model.yaml"),
        "synthetic": True,
        "llm": {"mode": "mock", "responses": str(input_root / "mock_llm.yaml"),
                "max_calls": 30, "max_reserved_tokens": 2000000},
        "mcp": {"enabled": False}, "external": {"enabled": False},
        "discovery": {"max_indexed_fields": 2, "max_candidate_validations": 1},
    }
    output = tmp_path / "run"
    result = asyncio.run(build(config, output))
    assert result["status"] == "partial", result
    coverage = read_yaml(output / "discovery_coverage.yaml")
    assert coverage["value_index_fields"] == 2
    assert len(coverage["fields_not_value_indexed"]) == 6
    assert coverage["candidates_not_checked"] > 0
    assert coverage["partial"] is True
