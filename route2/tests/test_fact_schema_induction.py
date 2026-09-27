import asyncio

import pytest

from ontology_r2.fact_observations import build_fact_observation_candidates
from ontology_r2.fact_schema_induction import induce_fact_schema
from ontology_r2.fact_type_binding import bind_fact_observations
from ontology_r2.models import BuildPlan
from ontology_r2.storage import Dataset, read_yaml, write_yaml
from test_semantic_cards import _table


def _dataset(tmp_path, *, field="水果销售收入", comment="", values=("100", "120"), units=None):
    root = tmp_path / "input"
    rows = [{"id": str(i + 1), "region_code": region, "period": period, field: value,
             **({"unit": units[i]} if units else {})}
            for i, (region, period, value) in enumerate(zip(("EAST", "SOUTH"), ("2025Q1", "2025Q2"), values))]
    _table(root, "sales_fact", {"id": "", "region_code": "", "period": "", field: comment,
                                 **({"unit": ""} if units else {})}, rows)
    work = tmp_path / "work"
    work.mkdir(parents=True)
    return Dataset(root, work)


class _LLM:
    def __init__(self, **overrides):
        self.calls = []
        self.overrides = overrides

    async def ask(self, task, payload, schema):
        assert task == "fact_schema_induction"
        self.calls.append(payload)
        declaration = payload["source_declaration"]
        return schema.model_validate({"status": "proposed", "label": "水果销售收入",
                                      "identity_evidence_id": declaration["evidence_id"],
                                      "identity_quote": declaration["value"],
                                      "business_object_quote": "水果", "quantity_quote": "销售收入",
                                      "scope_bindings": {"region": "region_code"},
                                      **self.overrides})


class _NoLLM:
    async def ask(self, *args, **kwargs):
        raise AssertionError("This path must not call a model")


def _induce(data, llm, **kwargs):
    observed = build_fact_observation_candidates(data)
    assert observed["tables"][0]["row_purpose"] == "business_fact"
    result = asyncio.run(induce_fact_schema(data, observed, BuildPlan(), llm, **kwargs))
    return observed, result


def test_named_no_comment_fact_field_proposes_and_compiles_one_reusable_template(tmp_path):
    data = _dataset(tmp_path)
    try:
        llm = _LLM()
        observed, result = _induce(data, llm)
        assert result["coverage"] == {"fields_considered": 1, "model_calls": 1, "accepted_templates": 1,
                                       "reused_templates": 0, "unresolved_fields": 0, "per_row_llm_calls": 0,
                                       "partial": False}
        metric = result["plan"].object_types[0]
        assert metric.parent == "Metric" and metric.unit is None
        assert metric.evidence_scope == "source_schema"
        assert metric.semantic_parameters["calculation_status"] == "unknown"
        assert "计算口径未提供" in metric.definition
        assert llm.calls[0]["source_declaration"]["kind"] == "column_name"
        assert [row["values"]["水果销售收入"] for row in llm.calls[0]["sample_rows"]] == ["100", "120"]
        bound = asyncio.run(bind_fact_observations(data, observed, result["plan"], _NoLLM(),
                                                  field_templates=result["field_templates"]))
        assert bound["coverage"]["binding_attempts"] == 0
        assert len(bound["instances"]) == 2
        assert {x["binding_decision"]["bound_scope_values"]["region"] for x in bound["instances"]} == {"EAST", "SOUTH"}
    finally:
        data.close()


def test_anonymous_numeric_field_cannot_be_named_from_values(tmp_path):
    data = _dataset(tmp_path, field="value")
    try:
        _, result = _induce(data, _NoLLM())
        assert result["plan"].object_types == []
        assert result["coverage"]["model_calls"] == 0
        assert result["steps"][0]["reason"] == "anonymous_numeric_field_has_no_business_identity"
    finally:
        data.close()


def test_declared_unit_packet_requests_complete_quote_and_rejects_substring(tmp_path):
    declaration = "水果销售收入，单位元"
    data = _dataset(tmp_path, comment=declaration)
    try:
        llm = _LLM(unit="元", unit_quote=declaration)
        _, accepted = _induce(data, llm)
        contract = llm.calls[0]["declared_unit_quote_contract"]
        assert contract["recognized_units"] == ["元"]
        assert contract["required_unit_quote_if_declared"] == declaration
        assert accepted["coverage"]["accepted_templates"] == 1
        _, rejected = _induce(data, _LLM(unit="元", unit_quote="单位元"))
        assert rejected["coverage"]["accepted_templates"] == 0
        assert rejected["steps"][0]["reason"] == "Unit must appear explicitly in the complete field declaration"
    finally:
        data.close()


def test_template_round_trip_reuses_schema_with_new_numbers_and_refreshes_evidence(tmp_path):
    first = _dataset(tmp_path / "first", units=("元", "元"))
    second = _dataset(tmp_path / "second", values=("210", "320"), units=("元", "元"))
    try:
        _, initial = _induce(first, _LLM(unit="元", unit_column="unit", unit_quote="元"))
        assert initial["coverage"]["accepted_templates"] == 1
        path = tmp_path / "templates.yaml"
        write_yaml(path, initial["field_templates"])
        saved = read_yaml(path)
        assert first.snapshot_id != second.snapshot_id
        observed, replay = _induce(second, _NoLLM(), reusable_templates=saved, max_calls=0)
        assert replay["coverage"]["model_calls"] == 0
        assert replay["coverage"]["reused_templates"] == 1
        assert replay["plan"].object_types[0].id == initial["plan"].object_types[0].id
        current = replay["field_templates"][0]
        assert current["source_snapshot_id"] == second.snapshot_id
        assert all(second.evidence[key]["source_ref"]["snapshot_id"] == second.snapshot_id
                   for key in current["evidence_ids"])
        old_unit_ids = {x for x in saved[0]["evidence_ids"] if x.startswith("unit_observation:")}
        assert old_unit_ids.isdisjoint(current["evidence_ids"])
        bound = asyncio.run(bind_fact_observations(second, observed, replay["plan"], _NoLLM(),
                                                  field_templates=replay["field_templates"]))
        assert {x["observed_value"] for x in bound["instances"]} == {"210", "320"}
        assert all(x["unit"] == "元" for x in bound["instances"])
        stale = asyncio.run(bind_fact_observations(second, observed, replay["plan"], _NoLLM(),
                                                  field_templates=saved))
        assert stale["instances"] == []
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("change", ["declaration", "role", "table_declaration"])
def test_semantic_contract_change_invalidates_saved_decision(tmp_path, change):
    first = _dataset(tmp_path / "first")
    second = _dataset(tmp_path / "second", values=("210", "320"))
    try:
        _, initial = _induce(first, _LLM())
        table = second.tables["fruit.sales_fact"]
        if change == "declaration":
            next(x for x in table["columns"] if x["column_name"] == "水果销售收入")["column_comment"] = "水果销售收入，已入账"
        elif change == "role":
            table["inferred_semantic_roles"] = [{"column": "水果销售收入", "role": "numeric_business_value",
                                                 "status": "source_verified_role_candidate"}]
        else:
            table["table_comment"] = "按 region_code 和 period 统计的水果销售收入"
        llm = _LLM()
        _, result = _induce(second, llm, reusable_templates=initial["field_templates"])
        assert result["coverage"]["model_calls"] == 1
        assert result["coverage"]["reused_templates"] == 0
        assert len(llm.calls) == 1
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("decision", [
    {"unit": "元", "unit_quote": "水果销售收入"},
    {"label": "水果预算利润", "quantity_quote": "预算利润"},
    {"scope_bindings": {"region": "not_a_coordinate"}},
])
def test_unsupported_unit_identity_or_scope_is_not_compiled(tmp_path, decision):
    data = _dataset(tmp_path)
    try:
        _, result = _induce(data, _LLM(**decision))
        assert result["plan"].object_types == []
        assert result["coverage"]["unresolved_fields"] == 1
    finally:
        data.close()


def test_mixed_unit_column_cannot_be_promoted_from_one_example(tmp_path):
    data = _dataset(tmp_path, units=("元", "万元"))
    try:
        _, result = _induce(data, _LLM(unit="元", unit_column="unit", unit_quote="元"))
        assert result["plan"].object_types == []
        assert "complete current-snapshot unit column" in result["steps"][0]["reason"]
    finally:
        data.close()


def test_explicit_unit_in_field_declaration_is_used_without_a_unit_column(tmp_path):
    data = _dataset(tmp_path, comment="水果销售收入（元）")
    try:
        observed, result = _induce(data, _LLM(unit="元", unit_quote="水果销售收入（元）"))
        assert result["plan"].object_types[0].unit == "元"
        bound = asyncio.run(bind_fact_observations(data, observed, result["plan"], _NoLLM(),
                                                  field_templates=result["field_templates"]))
        assert len(bound["instances"]) == 2
    finally:
        data.close()


def test_unit_change_invalidates_template_and_requires_new_decision(tmp_path):
    first = _dataset(tmp_path / "first", units=("元", "元"))
    second = _dataset(tmp_path / "second", units=("万元", "万元"))
    try:
        _, initial = _induce(first, _LLM(unit="元", unit_column="unit", unit_quote="元"))
        llm = _LLM(unit="万元", unit_column="unit", unit_quote="万元")
        _, replay = _induce(second, llm, reusable_templates=initial["field_templates"])
        assert len(llm.calls) == 1 and replay["coverage"]["reused_templates"] == 0
        assert replay["plan"].object_types[0].unit == "万元"
    finally:
        first.close()
        second.close()


def test_incomplete_packet_or_no_budget_is_explicitly_unresolved(tmp_path):
    data = _dataset(tmp_path)
    try:
        _, oversized = _induce(data, _NoLLM(), max_packet_bytes=5)
        assert oversized["steps"][0]["reason"] == "fact_schema_packet_exceeds_byte_budget"
        _, no_budget = _induce(data, _NoLLM(), max_calls=0)
        assert no_budget["steps"][0]["reason"] == "fact_schema_call_budget_exhausted"
    finally:
        data.close()


def test_changed_checked_association_condition_invalidates_template(tmp_path):
    data = _dataset(tmp_path)
    try:
        def graph(region):
            return {"edges": [
                {"type": "table_has_column", "source": "fruit.sales_fact", "target": "fruit.sales_fact.region_code"},
                {"type": "table_has_column", "source": "fruit.definitions", "target": "fruit.definitions.region"},
                {"type": "technical_link", "status": "checked_technical", "snapshot_id": data.snapshot_id,
                 "source": "fruit.sales_fact.region_code", "target": "fruit.definitions.region",
                 "selector": {"source": {"region_code": region}}, "transform": "identity"}]}

        _, initial = _induce(data, _LLM(), association_context=graph("EAST"))
        llm = _LLM()
        _, changed = _induce(data, llm, reusable_templates=initial["field_templates"],
                             association_context=graph("SOUTH"))
        assert len(llm.calls) == 1 and changed["coverage"]["reused_templates"] == 0
        assert llm.calls[0]["checked_association_conditions"][0]["selector"] == {"source": {"region_code": "SOUTH"}}
    finally:
        data.close()


def test_tampered_accepted_type_does_not_use_saved_binding_template(tmp_path):
    data = _dataset(tmp_path)
    try:
        observed, initial = _induce(data, _LLM())
        initial["plan"].object_types[0].definition = "无证据的收入减成本"
        bound = asyncio.run(bind_fact_observations(data, observed, initial["plan"], _NoLLM(),
                                                  field_templates=initial["field_templates"]))
        assert bound["instances"] == []
        assert bound["coverage"]["binding_attempts"] == 0
        assert bound["coverage"]["skipped_reasons"]["binding_call_failed:ValueError"] == 2
    finally:
        data.close()
