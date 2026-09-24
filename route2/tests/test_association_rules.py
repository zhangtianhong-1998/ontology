"""Association DSL and full-input verification stay separate from semantics."""

import asyncio

import duckdb

from ontology_r2.association_rules import _select_agent_candidates, build_association_rules
from ontology_r2.discovery import validate_candidate
from ontology_r2.storage import qi


class Rows:
    def __init__(self, tables, snapshot="snapshot-A"):
        self.db = duckdb.connect(":memory:")
        self.snapshot_id = snapshot
        self.tables = {}
        for index, (name, (columns, values)) in enumerate(tables.items()):
            sql_name = f"src_{index}"
            fields = ", ".join(f"{qi(column)} VARCHAR" for column in columns)
            self.db.execute(f"CREATE TABLE {qi(sql_name)} (__r2_row BIGINT, {fields})")
            self.db.executemany(
                f"INSERT INTO {qi(sql_name)} VALUES ({','.join('?' for _ in range(len(columns) + 1))})",
                [(i, *row) for i, row in enumerate(values, 1)],
            )
            self.tables[name] = {
                "sql_name": sql_name,
                "column_names": columns,
                "columns": [{"column_name": c, "column_comment": ""} for c in columns],
            }

    def close(self):
        self.db.close()


def candidate(source_field="ref", target_field="code"):
    return {"candidate_id": "lead-1",
            "source": {"table": "demo.source", "field": source_field},
            "target": {"table": "demo.target", "field": target_field},
            "retrieval_channels": ["metadata", "value_overlap"]}


def test_agent_candidate_budget_prefers_checked_nonnumeric_and_spreads_tables():
    def lead(identifier, source_table, *, numeric=False, sample_count=1):
        return {"candidate_id": identifier,
                "source": {"table": source_table, "field": "ref"},
                "target": {"table": "demo.target", "field": "code"},
                "numeric_overlap_only": numeric,
                "shared_sample_value_count": sample_count,
                "target_declared_pk": True}

    # Deliberately put an unchecked and a numeric lead before useful checks.
    leads = [lead("unchecked", "demo.a", sample_count=100),
             lead("numeric", "demo.b", numeric=True, sample_count=99),
             lead("a-strong", "demo.a", sample_count=4),
             lead("a-second", "demo.a", sample_count=2),
             lead("b-strong", "demo.b", sample_count=3)]
    checked = {identifier: {} for identifier in
               ("numeric", "a-strong", "a-second", "b-strong")}
    selected = _select_agent_candidates(
        {item["candidate_id"]: item for item in leads}, checked, 3)
    assert list(selected) == ["a-strong", "b-strong", "a-second"]
    assert _select_agent_candidates({item["candidate_id"]: item for item in leads},
                                    checked, 0) == {}


def test_reuse_full_input_check_without_rescan_and_keep_semantics_unresolved():
    data = Rows({
        "demo.source": (["ref"], [("A",), ("B",)]),
        "demo.target": (["code"], [("A",), ("B",)]),
    })
    try:
        lead = candidate()
        check = validate_candidate(data, lead)
        result = asyncio.run(build_association_rules(
            data, {"candidates": [lead], "checks": [check]}))
        rule = result["rules"][0]
        assert result["coverage"]["new_full_input_validations"] == 0
        assert rule["status"] == "checked_technical"
        assert rule["semantic_relation"] == "unresolved"
        assert rule["scope_bindings_direction"] == "source_to_target"
        assert rule["verification"]["scan_scope"] == "full_input"
        assert rule["verification"]["checks"]["unique_matches"] == 2
    finally:
        data.close()


def test_selector_and_nonidentical_scope_fields_are_full_input_checked():
    data = Rows({
        "demo.source": (["kind", "ref", "market"], [
            ("API", "A", "CN"), ("API", "A", "EU"),
            ("API", "B", "CN"), ("CARD", "A", "CN")]),
        "demo.target": (["code", "market_area"], [
            ("A", "CN"), ("A", "EU"), ("B", "CN")]),
    })
    try:
        result = asyncio.run(build_association_rules(
            data, {"candidates": [candidate()], "checks": []},
            {"proposals": [{"candidate_id": "lead-1",
                            "selector": {"kind": "API"},
                            "scope_bindings": {"market": "market_area"}}]}))
        rule = next(r for r in result["rules"] if r["selector"])
        assert rule["status"] == "checked_technical"
        assert rule["scope_bindings"] == {"market": "market_area"}
        assert rule["verification"]["checks"]["unique_matches"] == 3
        assert rule["verification"]["checks"]["selector_false"] == 1
        assert rule["verification"]["checks"]["target_duplicate_key_groups"] == 0
        assert result["coverage"]["new_full_input_validations"] == 1
        assert next(r for r in result["rules"] if not r["selector"])["status"] == "unresolved"
    finally:
        data.close()


def test_partial_matches_keep_counterexamples_and_no_business_predicate():
    data = Rows({
        "demo.source": (["ref"], [("A",), ("B",), ("C",)]),
        "demo.target": (["code"], [("A",), ("B",), ("B",)]),
    })
    try:
        check = validate_candidate(data, candidate())
        result = asyncio.run(build_association_rules(
            data, {"candidates": [candidate()], "checks": [check]}))
        rule = result["rules"][0]
        assert rule["status"] == "observed_subset"
        assert rule["semantic_relation"] == "unresolved"
        assert {x["reason"] for x in rule["verification"]["counterexamples"]} == {
            "ambiguous_target", "missing_in_input"}
    finally:
        data.close()


def test_other_snapshot_check_is_not_reused():
    data = Rows({
        "demo.source": (["ref"], [("A",)]),
        "demo.target": (["code"], [("A",)]),
    })
    try:
        check = validate_candidate(data, candidate())
        check["snapshot_id"] = "older-snapshot"
        result = asyncio.run(build_association_rules(
            data, {"candidates": [candidate()], "checks": [check]}))
        assert result["rules"][0]["status"] == "unresolved"
        assert result["coverage"]["candidate_checks_reused"] == 0
    finally:
        data.close()


def test_conditional_check_cannot_certify_unconditional_rule():
    data = Rows({
        "demo.source": (["kind", "ref"], [("API", "A"), ("CARD", "B")]),
        "demo.target": (["code"], [("A",)]),
    })
    try:
        check = validate_candidate(data, candidate(), selector={"kind": "API"})
        result = asyncio.run(build_association_rules(
            data, {"candidates": [candidate()], "checks": [check]}))
        assert result["rules"][0]["status"] == "unresolved"
        assert result["coverage"]["candidate_checks_reused"] == 0
    finally:
        data.close()


def test_unsupported_transform_and_scope_collision_fail_closed():
    data = Rows({
        "demo.source": (["ref", "a", "b"], [("A", "X", "Y")]),
        "demo.target": (["code", "scope"], [("A", "X")]),
    })
    try:
        result = asyncio.run(build_association_rules(
            data, {"candidates": [candidate()], "checks": []},
            {"proposals": [
                {"candidate_id": "lead-1", "transform": "casefold"},
                {"candidate_id": "lead-1", "scope_bindings": {"a": "scope", "b": "scope"}},
            ]}))
        assert result["coverage"]["partial"]
        assert result["coverage"]["errors"][0]["error_type"] == "ValidationError"
        collision = next(r for r in result["rules"] if r["scope_bindings"])
        assert collision["status"] == "unresolved"
        assert collision["verification"]["error"] == "ValueError"
    finally:
        data.close()


def test_explicit_field_pair_can_survive_bounded_discovery_omission():
    data = Rows({
        "demo.source": (["ref"], [("A",)]),
        "demo.target": (["code"], [("A",)]),
    })
    try:
        result = asyncio.run(build_association_rules(
            data, {"candidates": [], "checks": []},
            {"proposals": [{"source": {"table": "demo.source", "field": "ref"},
                            "target": {"table": "demo.target", "field": "code"}}]}))
        assert len(result["rules"]) == 1
        assert result["rules"][0]["status"] == "checked_technical"
        assert result["rules"][0]["retrieval_channels"] == ["explicit_rule"]
    finally:
        data.close()


def test_scripted_agentscope_react_uses_read_only_tools_then_program_verifies():
    class ScriptLLM:
        mode = "mock"
        config = {"max_input_bytes": 100000, "max_output_tokens": 4096}
        responses = {"association_react_script": [
            {"calls": [{"name": "describe_candidate", "input": {"candidate_id": "lead-1"}}]},
            {"calls": [{"name": "test_match", "input": {
                "candidate_id": "lead-1", "selector": {"kind": "API"},
                "scope_bindings": {"market": "market_area"}}}]},
            {"calls": [{"name": "GenerateStructuredOutput", "input": {
                "proposals": [{"candidate_id": "lead-1", "selector": {"kind": "API"},
                               "scope_bindings": {"market": "market_area"},
                               "rationale": "仅在 API 条件及产区范围内匹配"}],
                "remaining_gaps": ["业务关系含义待判定"]}}]},
        ]}

        def __init__(self):
            self.calls = 0

        def admit(self, request):
            self.calls += 1

        def trace(self, event):
            pass

    data = Rows({
        "demo.source": (["kind", "ref", "market"], [
            ("API", "A", "CN"), ("CARD", "A", "EU")]),
        "demo.target": (["code", "market_area"], [
            ("A", "CN"), ("A", "EU")]),
    })
    try:
        llm = ScriptLLM()
        result = asyncio.run(build_association_rules(
            data, {"candidates": [candidate()], "checks": []},
            {"agent_enabled": True, "max_agent_model_calls": 3,
             "max_agent_full_scans": 1, "max_explicit_validations": 0}, llm))
        assert result["agent"]["mode"] == "agentscope_react_mock"
        assert result["agent"]["status"] == "completed"
        assert result["agent"]["model_calls"] == llm.calls == 3
        assert result["agent"]["full_scans"] == 1
        rule = next(r for r in result["rules"] if r["selector"])
        assert rule["origin"] == "agentscope_react"
        assert rule["status"] == "checked_technical"
        assert rule["semantic_relation"] == "unresolved"
    finally:
        data.close()
