"""An explicit unrestricted scope is evidence, not a literal join condition."""
from types import SimpleNamespace

import pytest

from ontology_r2.calculation_contracts import enrich_calculation_contracts
from ontology_r2.group_incremental import (
    ConceptBundleDecision, RecordAlignmentDecision, _compiled_object_type, compile_concept,
)
from ontology_r2.models import BuildPlan
from test_group_incremental import PROFILE


def _compile(data, key, name, scope, roles, *, metric=False, formula=None, source_scope=True):
    definition = name + ("是苹果经营业务的收入减去成本。" if metric else "是可用于不同经营对象的金额合计。")
    fields = {
        "name": [{"column": "name", "value": name}],
        "description": [{"column": "definition", "value": definition}],
        "unit": [{"column": "unit", "value": "元"}],
        "scope": [{"column": column, "value": value} for column, value in scope.items()]
        if source_scope else [],
    }
    if formula:
        fields["formula"] = [{"column": "formula", "value": formula}]
    record = {"record_id": key, "table": "definitions", "kind": "definition",
              "row_number": 1, "scope": scope, "unit": "元", "fields": fields}
    decision = ConceptBundleDecision(
        status="proposed", label=name, definition=definition,
        root_type="Metric" if metric else "Measure", ontology_level="type",
        classification_basis="business_driven_metric" if metric else "reusable_measure",
        classification_quote=definition, business_object_quote="苹果" if metric else "",
        scope_roles=roles,
        alignments=[RecordAlignmentDecision(record_id=key, mapping_kind="exact", quote=name)],
    )
    concept, alignments = compile_concept(data, PROFILE, {"records": [record]}, decision, {})
    return concept, alignments, _compiled_object_type(concept)


@pytest.mark.parametrize("value", ["不限", "不限制", "不限定经营对象、地区或年份", "无限制", "all", "UNRESTRICTED"])
def test_complete_unrestricted_declaration_retains_evidence_without_equality_condition(value):
    data = SimpleNamespace(snapshot_id="snap", evidence={}, tables={})
    concept, _, item = _compile(data, "revenue", "收入", {"limit": value, "currency": "CNY"},
                                {"limit": "unrestricted", "currency": "applicability"})
    assert item.applicability_scope == {"currency": "CNY"}
    assert concept["scope"] == {"limit": value, "currency": "CNY"}
    assert concept["scope_roles"]["limit"] == "unrestricted"
    assert concept["unrestricted_scope"] == {"limit": value}
    source = next(source for source in item.source_properties if source.source_column == "limit")
    assert data.evidence[source.evidence_ids[0]]["raw_fragment"] == value


@pytest.mark.parametrize("value", [
    "", "全国", "公历年", "仅华东", "不限但仅华东", "不限制地区，排除北京", "不限地区，2025年",
    "不限于华东", "all except China", "not unrestricted", "unrestricted but only China",
])
def test_scope_bounds_exceptions_and_unknown_values_cannot_be_unrestricted(value):
    data = SimpleNamespace(snapshot_id="snap", evidence={}, tables={})
    with pytest.raises(ValueError, match="Unrestricted scope requires"):
        _compile(data, "revenue", "收入", {"limit": value}, {"limit": "unrestricted"})


def test_unrestricted_claim_requires_complete_original_scope_field():
    data = SimpleNamespace(snapshot_id="snap", evidence={}, tables={})
    with pytest.raises(ValueError, match="Unrestricted scope requires"):
        _compile(data, "revenue", "收入", {"limit": "all"}, {"limit": "unrestricted"}, source_scope=False)


def test_metric_calculation_binds_unrestricted_measures_without_erasing_real_scopes():
    data = SimpleNamespace(snapshot_id="snap", evidence={}, tables={})
    groups = {"concepts": [], "record_alignments": []}
    types = []
    for key, name in (("revenue", "收入"), ("cost", "成本")):
        concept, alignments, item = _compile(data, key, name,
            {"limit": "不限定经营对象、地区或年份"}, {"limit": "unrestricted"})
        groups["concepts"].append(concept)
        groups["record_alignments"].extend(alignments)
        types.append(item)
    concept, alignments, metric = _compile(data, "profit", "苹果经营利润",
        {"business_object": "苹果", "region_scope": "全国", "period_scope": "公历年"},
        {"business_object": "applicability", "region_scope": "applicability", "period_scope": "applicability"},
        metric=True, formula="收入 - 成本")
    groups["concepts"].append(concept)
    groups["record_alignments"].extend(alignments)
    plan = BuildPlan(object_types=[*types, metric])
    result = enrich_calculation_contracts(data, plan, groups)
    assert result["calculations"][0]["status"] == "accepted"
    assert {edge["target_type_id"] for edge in result["dependencies"]} == {item.id for item in types}
    assert metric.applicability_scope["region_scope"] == "全国"
    assert metric.applicability_scope["period_scope"] == "公历年"
    # A genuinely narrower operand still cannot become a global dependency.
    types[0].applicability_scope = {"region_scope": "仅华东"}
    rejected = enrich_calculation_contracts(data, plan, groups)
    assert rejected["calculations"][0]["status"] == "unresolved"
    assert any(item.get("reason") == "scope_not_proven" for item in rejected["calculations"][0]["bindings"])
