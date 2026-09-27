"""Association DSL and full-input verification stay separate from semantics."""

import asyncio
import copy
import json

import duckdb

from ontology_r2.association_rules import _select_agent_candidates, build_association_rules
from ontology_r2.discovery import discover_and_check, validate_candidate
from ontology_r2.storage import qi


class ReplayLLM:
    """Exercise the real AgentScope messages/formatter without a provider call."""

    mode = "mock"

    def __init__(self, rounds, input_limit=100000):
        self.config = {"max_input_bytes": input_limit, "max_output_tokens": 4096}
        self.responses = {"association_react_script": [{"calls": calls} for calls in rounds]}
        self.requests, self.events = [], []

    def admit(self, request):
        from ontology_r2.llm import BudgetExceeded
        if len(json.dumps(request, ensure_ascii=False, default=str).encode()) > self.config["max_input_bytes"]:
            raise BudgetExceeded("test input admission exceeded")
        self.requests.append(request)

    def trace(self, event):
        self.events.append(event)


def submit_empty():
    return {"name": "GenerateStructuredOutput", "input": {"proposals": [], "remaining_gaps": []}}


def describe(cid="lead-1"):
    return {"name": "describe_candidate", "input": {"candidate_id": cid}}


def test_eight_describes_fit_real_agentscope_context_without_dropping_full_checks():
    # The old tool repeated both 40-column schemas eight times, exceeding 100 KB.
    source = ["ref", *[f"source_col_{i}" for i in range(39)]]
    target = ["code", *[f"target_col_{i}" for i in range(39)]]
    data = Rows({"demo.source": (source, [("A", *["x"] * 39)]),
                 "demo.target": (target, [("A", *["x"] * 39)])})
    try:
        for table in data.tables.values():
            for col in table["columns"]:
                col.update(column_comment="字段定义及说明。" * 4, data_type="character varying(255)")
        leads = [{**candidate(), "candidate_id": f"lead-{i}"} for i in range(8)]
        checks = [validate_candidate(data, lead) for lead in leads]
        original_checks = copy.deepcopy(checks)
        llm = ReplayLLM([[describe(lead["candidate_id"]) for lead in leads], [submit_empty()]])
        result = asyncio.run(build_association_rules(data, {"candidates": leads, "checks": checks}, {
            "agent_enabled": True, "max_agent_candidates": 8,
            "max_agent_model_calls": 2, "max_agent_tool_calls": 8,
            "max_agent_tools_per_round": 8, "max_agent_tool_response_bytes": 16000}, llm))
        assert result["agent"]["status"] == "completed"
        assert result["agent"]["tool_calls"] == 8
        assert len(llm.requests) == 2
        assert max(result["agent"]["context_bytes"]) < 70000
        assert all(item["result_bytes"] <= 4096 for item in result["agent"]["tool_results"])
        request_text = json.dumps(llm.requests[-1], ensure_ascii=False, default=str)
        assert 'omitted_column_descriptions' in request_text
        assert 'association_checks.yaml' in request_text
        assert checks == original_checks
        assert all(rule["verification"]["checks"] == checks[i]["checks"]
                   for i, rule in enumerate(result["rules"]))
        assert [tool["function"]["name"] for tool in llm.requests[-1]["tools"]] == ["GenerateStructuredOutput"]
        assert 'auto' in llm.requests[-1]["tool_choice"]
    finally:
        data.close()


def test_three_round_budget_preserves_submit_and_persists_unproposed_full_check():
    data = Rows({"demo.source": (["ref"], [("A",), ("unknown",)]),
                 "demo.target": (["code"], [("A",)])})
    try:
        llm = ReplayLLM([[describe()], [{"name": "test_match", "input": {"candidate_id": "lead-1"}}, describe()],
                         [submit_empty()]])
        result = asyncio.run(build_association_rules(data, {"candidates": [candidate()], "checks": []}, {
            "agent_enabled": True, "max_agent_model_calls": 3, "max_agent_tool_calls": 2,
            "max_agent_full_scans": 1}, llm))
        assert result["agent"]["status"] == "completed"
        assert result["agent"]["model_calls"] == 3
        assert result["agent"]["tool_calls"] == 2
        assert result["agent"]["full_scans"] == 1
        assert result["agent"]["pending_tool_requests"][0]["reason"] == "tool_call_budget"
        assert result["coverage"]["partial"]
        check = next(iter(result["agent_checks"].values()))
        assert check["checks"]["eligible_references"] == 2
        assert check["checks"]["missing_in_input"] == 1
        assert check["checks"]["counterexample_rows"][0]["reason"] == "missing_in_input"
        assert [tool["function"]["name"] for tool in llm.requests[-1]["tools"]] == ["GenerateStructuredOutput"]
        assert 'auto' in llm.requests[-1]["tool_choice"]
        assert 'submission_required' in json.dumps(llm.requests[-1], ensure_ascii=False, default=str)
    finally:
        data.close()


def test_round_tool_limit_reports_pending_and_does_not_abort_submission():
    data = Rows({"demo.source": (["ref"], [("A",)]), "demo.target": (["code"], [("A",)])})
    try:
        llm = ReplayLLM([[describe()] * 8, [submit_empty()]])
        result = asyncio.run(build_association_rules(data, {"candidates": [candidate()], "checks": []}, {
            "agent_enabled": True, "max_agent_model_calls": 3, "max_agent_tool_calls": 12}, llm))
        assert result["agent"]["status"] == "completed"
        assert result["agent"]["tool_calls"] == 4
        assert result["agent"]["tool_requests"] == 8
        assert result["agent"]["pending_tool_requests"][0]["reason"] == "round_tool_call_budget"
        assert len([r for r in result["agent"]["tool_results"] if r["summary_status"] == 'deferred_by_budget']) == 4
    finally:
        data.close()


def test_budget_failure_reports_reason_and_measured_bytes():
    data = Rows({"demo.source": (["ref"], [("A",)]), "demo.target": (["code"], [("A",)])})
    try:
        llm = ReplayLLM([[submit_empty()]], input_limit=100)
        result = asyncio.run(build_association_rules(data, {"candidates": [candidate()], "checks": []}, {
            "agent_enabled": True}, llm))
        assert result["agent"]["status"] == "budget_exhausted"
        error = next(event for event in llm.events if event['stage'] == 'association_react_error')
        assert 'bytes' in error['reason']
        assert error['context_bytes'][0] > error['max_input_bytes'] == 100
        assert result["agent"]["model_calls"] == 0
    finally:
        data.close()


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


def test_validation_progress_reports_counts_without_record_values():
    data = Rows({
        "demo.source": (["ref"], [("sensitive-example",)]),
        "demo.target": (["code"], [("sensitive-example",)]),
    })
    events = []
    try:
        result = asyncio.run(build_association_rules(data, {"candidates": [], "checks": []},
            {"proposals": [{"source": {"table": "demo.source", "field": "ref"},
                            "target": {"table": "demo.target", "field": "code"}}]},
            progress=events.append))
        assert result["rules"][0]["status"] == "checked_technical"
        assert [event["status"] for event in events] == ["started", "completed"]
        assert events[-1]["full_input_validations"] == 1
        assert "sensitive-example" not in str(events)
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


def test_discovered_polymorphic_branches_reuse_exact_checks_without_agent():
    data = Rows({
        "demo.business_tag": (["business_type", "business_id"],
                              [("API", "1"), ("CARD", "1"), ("API", "2")]),
        "demo.market_api": (["api_id"], [("1",), ("2",)]),
        "demo.dashboard_card": (["card_id"], [("1",)]),
    })
    data.tables["demo.market_api"]["pk"] = ["api_id"]
    data.tables["demo.dashboard_card"]["pk"] = ["card_id"]
    try:
        found = discover_and_check(data, {
            "value_index_mode": "full_distinct", "max_indexed_fields": 0,
            "max_candidate_validations": 12,
        })
        result = asyncio.run(build_association_rules(
            data, found, {"max_rules": 50, "agent_enabled": False}))
        branches = [r for r in result["rules"] if r["selector"]]
        assert {(r["selector"]["business_type"], r["target"]["table"])
                for r in branches} == {("API", "demo.market_api"),
                                      ("CARD", "demo.dashboard_card")}
        assert all(r["status"] == "checked_technical" and
                   r["semantic_relation"] == "unresolved" for r in branches)
        assert result["coverage"]["new_full_input_validations"] == 0
    finally:
        data.close()


def test_negative_reference_stays_subset_until_an_explicit_selector_excludes_it():
    data = Rows({
        "demo.ref_rule": (["source_type", "source_field", "record_state"],
                          [("metric", "M1", "valid"),
                           ("metric", "UNKNOWN_SYNTHETIC_REFERENCE", "invalid")]),
        "demo.metric_detail": (["metric_code"], [("M1",)]),
    })
    try:
        found = discover_and_check(data, {
            "value_index_mode": "full_distinct", "max_indexed_fields": 0,
            "max_candidate_validations": 3,
        })
        lead = next(c for c in found["candidates"]
                    if c.get("suggested_selector") == {"source_type": "metric"}
                    and c["target"]["field"] == "metric_code")
        result = asyncio.run(build_association_rules(data, found, {
            "max_rules": 20,
            "proposals": [{"candidate_id": lead["candidate_id"],
                           "selector": {"source_type": "metric", "record_state": "valid"}}],
        }))
        rules = [r for r in result["rules"] if r["candidate_id"] == lead["candidate_id"]]
        broad = next(r for r in rules if "record_state" not in r["selector"])
        narrow = next(r for r in rules if r["selector"].get("record_state") == "valid")
        assert broad["status"] == "observed_subset"
        assert broad["verification"]["checks"]["missing_in_input"] == 1
        assert broad["verification"]["counterexamples"][0]["reason"] == "missing_in_input"
        assert narrow["status"] == "checked_technical"
        assert narrow["selector"] == {"record_state": "valid", "source_type": "metric"}
        assert narrow["semantic_relation"] == broad["semantic_relation"] == "unresolved"
    finally:
        data.close()


def test_missing_scope_prevents_global_technical_status():
    data = Rows({
        "demo.source": (["kind", "ref", "market"],
                        [("linked", "A", "north"), ("linked", "A", "")]),
        "demo.target": (["code", "market_area"], [("A", "north")]),
    })
    try:
        result = asyncio.run(build_association_rules(
            data, {"candidates": [candidate()], "checks": []},
            {"proposals": [{"candidate_id": "lead-1", "selector": {"kind": "linked"},
                            "scope_bindings": {"market": "market_area"}}]}))
        rule = next(r for r in result["rules"] if r["scope_bindings"])
        assert rule["verification"]["checks"]["unique_matches"] == 1
        assert rule["verification"]["checks"]["missing_scope"] == 1
        assert rule["status"] == "observed_subset"
    finally:
        data.close()


def test_alias_transform_validates_all_rows_and_keeps_normalization_collisions():
    from ontology_r2.discovery import association_match_sql
    data = Rows({
        "demo.source": (["ref"], [(" 销售额 ",), ("A",), ("Ｂ",), ("unknown",)]),
        "demo.target": (["code"], [("收入;销售额",), ("a;b",), ("A",)]),
    })
    try:
        check = validate_candidate(data, candidate(), transform="target_alias_items")
        assert check["normalization"] == "target_alias_items"
        assert check["checks"]["unique_matches"] == 2
        assert check["checks"]["ambiguous_matches"] == 1
        assert check["checks"]["missing_in_input"] == 1
        query, params = association_match_sql(data, candidate(), transform="target_alias_items")
        assert set(data.db.execute(query, params).fetchall()) == {(1, 1), (3, 2)}
    finally:
        data.close()


def test_alias_recall_compiles_reusable_transforms_not_identity_certificates():
    from ontology_r2.value_aliases import propose_value_alias_candidates
    data = Rows({
        "demo.source": (["ref"], [("销售额",), ("收入",)]),
        "demo.target": (["code"], [("营收;销售额;收入",)]),
    })
    for info in data.tables.values():
        info["rows"] = data.db.execute(f"SELECT count(*) FROM {qi(info['sql_name'])}").fetchone()[0]
    try:
        aliases = propose_value_alias_candidates(data)
        assert aliases["candidates"]
        found = {"candidates": [], "checks": []}
        result = asyncio.run(build_association_rules(data, found, alias_candidates=aliases))
        forward = next(r for r in result["rules"] if r["source"]["table"] == "demo.source")
        assert forward["transform"] == {"operator": "target_alias_items"}
        assert forward["verification"]["scan_scope"] == "full_input"
        assert forward["verification"]["checks"]["unique_matches"] == 2
        assert forward["semantic_relation"] == "unresolved"
        reverse = next(r for r in result["rules"] if r["source"]["table"] == "demo.target")
        assert reverse["verification"]["checks"]["ambiguous_matches"] == 1
        assert result["coverage"]["partial"]
    finally:
        data.close()


def test_different_transform_cannot_reuse_raw_check():
    data = Rows({"demo.source": (["ref"], [("APPLE",)]),
                 "demo.target": (["code"], [("apple",)])})
    try:
        lead = candidate()
        check = validate_candidate(data, lead)
        result = asyncio.run(build_association_rules(data, {"candidates": [lead], "checks": [check]},
            {"proposals": [{"candidate_id": "lead-1", "transform": "nfkc_whitespace_casefold"}]}))
        transformed = next(r for r in result["rules"] if r["transform"]["operator"] != "identity")
        assert transformed["verification"]["checks"]["unique_matches"] == 1
        assert result["coverage"]["new_full_input_validations"] == 1
    finally:
        data.close()


def test_duplicate_alias_tokens_in_one_target_are_one_row_not_ambiguous():
    data = Rows({"demo.source": (["ref"], [("a",)]),
                 "demo.target": (["code"], [("A;a;Ａ",)])})
    try:
        check = validate_candidate(data, candidate(), transform="target_alias_items")
        assert check["checks"]["unique_matches"] == 1
        assert check["checks"]["ambiguous_matches"] == 0
    finally:
        data.close()


def test_agent_counterexamples_does_not_reuse_other_selector_cache():
    import json
    class ScriptLLM:
        mode = 'mock'
        config = {'max_input_bytes': 100000, 'max_output_tokens': 4096}
        responses = {'association_react_script': [
            {'calls': [{'name': 'counterexamples', 'input': {'candidate_id': 'lead-1'}}]},
            {'calls': [{'name': 'GenerateStructuredOutput', 'input': {
                'proposals': [], 'remaining_gaps': ['unconditional check not run']}}]},
        ]}
        def __init__(self):
            self.requests = []
        def admit(self, request):
            self.requests.append(request)
        def trace(self, event):
            pass
    data = Rows({'demo.source': (['kind', 'ref'], [('API', 'A'), ('CARD', 'B')]),
                 'demo.target': (['code'], [('A',)])})
    try:
        lead = {**candidate(), 'suggested_selector': {'kind': 'API'}}
        checked = validate_candidate(data, lead, selector={'kind': 'API'})
        llm = ScriptLLM()
        result = asyncio.run(build_association_rules(data, {'candidates': [lead], 'checks': [checked]},
            {'agent_enabled': True, 'max_agent_model_calls': 2}, llm))
        assert result['agent']['status'] == 'completed'
        assert 'test_match_required' in json.dumps(llm.requests, ensure_ascii=False, default=str)
    finally:
        data.close()


def test_transform_many_repeated_values_never_joins_record_cartesian_product():
    """10k equal sources and 10k equal targets are 10k ambiguous rows, not 100M links."""
    from time import monotonic
    from ontology_r2.discovery import association_match_sql
    data = Rows({'demo.source': (['ref'], [(' apple ',)]),
                 'demo.target': (['code'], [('苹果;APPLE',)])})
    try:
        for table, column, value in [('demo.source', 'ref', ' apple '),
                                     ('demo.target', 'code', '苹果;APPLE')]:
            sql = data.tables[table]['sql_name']
            data.db.execute(f'DELETE FROM {qi(sql)}')
            data.db.execute(f'INSERT INTO {qi(sql)} SELECT i, ? FROM range(1,10001) t(i)', [value])
        started = monotonic()
        result = validate_candidate(data, candidate(), transform='target_alias_items')
        assert result['checks']['ambiguous_matches'] == 10000
        assert result['checks']['target_max_multiplicity'] == 10000
        assert result['checks']['unique_matches'] == 0
        sql, params = association_match_sql(data, candidate(), transform='target_alias_items')
        assert data.db.execute('SELECT count(*) FROM (' + sql + ')', params).fetchone()[0] == 0
        # This generous bound is a complexity regression, not a speed target.
        assert monotonic()-started < 10
    finally:
        data.close()
