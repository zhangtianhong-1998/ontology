"""Genericity needs positive source evidence; empty model quotes prove nothing."""
from copy import deepcopy
from types import SimpleNamespace
import asyncio

import pytest

from ontology_r2.group_incremental import (
    ConceptBundleDecision, bundle_request_payload, compile_concept, construct_from_bundles,
)
from ontology_r2.models import BuildPlan
from test_group_incremental import PROFILE


def _fixture(name="收入", definition="收入是不绑定具体经营对象的金额口径。", *,
             scope=None, columns=()):
    scope = scope or {}
    data = SimpleNamespace(snapshot_id="snap", evidence={}, tables={"opaque.t": {"columns": list(columns)}})
    record = {"record_id": "r1", "table": "opaque.t", "row_number": 1,
              "kind": "definition", "scope": scope, "unit": "元", "fields": {
                  "name": [{"column": "name", "value": name}],
                  "description": [{"column": "definition", "value": definition}],
                  "scope": [{"column": key, "value": value} for key, value in scope.items()],
                  "unit": [{"column": "unit", "value": "元"}],
              }}
    bundle = {"bundle_id": "b1", "records": [record], "exact_alignment_record_ids": ["r1"]}
    decision = ConceptBundleDecision(status="proposed", label=name, definition=definition,
        root_type="Measure", ontology_level="type", classification_basis="reusable_measure",
        classification_quote=definition, scope_roles={key: "applicability" for key in scope},
        alignments=[{"record_id": "r1", "mapping_kind": "exact", "quote": definition}])
    return data, bundle, decision


def test_live_v6_empty_business_quote_cannot_accept_restricted_average_price():
    # Exact paid proposal from v6 trace line 63. Its quote is complete, but the
    # definition belongs to one operating object, not the short label's family.
    record_id = "fruit_market.fruit_measure_def:ae081329c812bc9ce91b7dd9"
    definition = "芒果均价，按非洲和会计期汇总。"
    data, bundle, _ = _fixture("均价", definition, scope={
        "domain_code": "IMPORT_EXPORT", "inference_type": "RATIO", "period": "M"})
    record = bundle["records"][0]
    record.update(record_id=record_id, table="fruit_market.fruit_measure_def", row_number=2825,
                  root_hint="Measure", unit="元/kg", calculation_fragments=[{
                      "column": "inference_type", "formula_status": "fragment",
                      "effective_role": "calculation_operator"}])
    record["fields"]["name"] = [{"column": column, "value": value} for column, value in (
        ("measure_name", "均价"), ("standard_name", "芒果均价"),
        ("parameter_name", "mango_0_value"), ("source_field", "avg_price_cny_per_kg"))]
    record["fields"]["description"][0]["column"] = "measure_description"
    record["fields"]["unit"][0]["value"] = "元/kg"
    bundle["exact_alignment_record_ids"] = [record_id]
    decision = ConceptBundleDecision.model_validate({
        "status": "proposed", "label": "均价", "definition": definition,
        "root_type": "Measure", "classification_basis": "reusable_measure",
        "classification_quote": definition, "business_object_quote": "",
        "aggregation_operator": None, "ontology_level": "type",
        "scope": {"domain_code": "IMPORT_EXPORT", "inference_type": "RATIO", "period": "M"},
        "scope_roles": {"domain_code": "applicability", "inference_type": "parameter", "period": "applicability"},
        "alignments": [{"record_id": record_id, "mapping_kind": "exact", "quote": definition}],
        "reason": "度量定义记录，measure_name 为均价，是可复用的平均价格口径。inference_type=RATIO 为已核验的计算片段参数，但 RATIO 不属于允许的聚合运算符，故 aggregation_operator 为 null。period=M 未核验为参数，按适用性处理。",
    })
    with pytest.raises(ValueError, match="Measure requires.*short_name_does_not_replace_standard_name"):
        compile_concept(data, PROFILE, bundle, decision, {})
    assessment = bundle_request_payload(data, PROFILE, BuildPlan(), bundle)["measure_reuse_assessment"][0]
    assert not assessment["supported"]
    assert assessment["reasons"] == ["missing_explicit_business_independence"]
    assert assessment["standard_names"][0]["value"] == "芒果均价"


@pytest.mark.parametrize("name,definition", [
    ("Average charge", "Average charge for the Oryx division in Zone K, grouped by accounting period."),
    ("流转额", "砾石工坊的流转额，依北岭业务线统计。"),
    ("Dépense moyenne", "Dépense moyenne de l’activité Aster, par territoire."),
    ("Revenue", "Revenue is a reusable amount."),  # Reusable alone is not independence.
])
@pytest.mark.parametrize("level", ["type", "instance"])
def test_unknown_business_vocabulary_and_empty_quote_never_establish_genericity(name, definition, level):
    data, bundle, decision = _fixture(name, definition)
    decision.ontology_level = level
    with pytest.raises(ValueError, match="missing_explicit_business_independence"):
        compile_concept(data, PROFILE, bundle, decision, {})


@pytest.mark.parametrize("level", ["type", "instance", "unresolved"])
def test_api_metric_cannot_evade_quantity_definition_checks_by_changing_level(level):
    data, bundle, decision = _fixture("橙子产量统计", "查询橙子在东南亚的产量统计结果。")
    decision.root_type = "Metric"
    decision.classification_basis = "business_driven_metric"
    decision.ontology_level = level
    decision.classification_quote = ""
    decision.business_object_quote = "橙子"
    with pytest.raises(ValueError, match="classification quote must be a complete exact definition"):
        compile_concept(data, PROFILE, bundle, decision, {})


@pytest.mark.parametrize("name,definition", [
    ("收入", "收入是可用于不同经营对象的营业所得金额"),
    ("成本", "成本是不限定具体经营对象的耗费金额。"),
    ("Revenue", "Revenue is a monetary quantity independent of business objects."),
])
def test_complete_generic_definitions_are_accepted_and_keep_positive_proof(name, definition):
    data, bundle, decision = _fixture(name, definition)
    concept, _ = compile_concept(data, PROFILE, bundle, decision, {})
    proof = concept["classification_evidence"]["measure_reuse_assessment"][0]
    assert proof["supported"] and proof["proofs"][0]["value"] == definition
    assert concept["definition"] == definition and concept["type"] == "Measure"
    # Even a literally present generic word is not an operating-object proof.
    decision.root_type = "Metric"
    decision.classification_basis = "business_driven_metric"
    decision.business_object_quote = name
    with pytest.raises(ValueError, match="Metric conflicts with an explicit business-independent"):
        compile_concept(data, PROFILE, bundle, decision, {})


@pytest.mark.parametrize("name,period", [("10月年预算", "M"), ("当年预算排名", "Y")])
def test_time_budget_and_rank_parameters_do_not_bind_a_business_object(name, period):
    data, bundle, decision = _fixture(name, name + "是不绑定具体经营对象的通用口径。",
        scope={"period": period, "budget": "1"}, columns=[
            {"column_name": "period", "column_comment": "计算期间粒度"},
            {"column_name": "budget", "column_comment": "预算标记"}])
    decision.scope_roles = {"period": "parameter", "budget": "parameter"}
    concept, _ = compile_concept(data, PROFILE, bundle, decision, {})
    assert concept["definition_parameters"] == {"period": period, "budget": "1"}
    assert concept["applicability_scope"] == {}
    changed = deepcopy(bundle)
    changed["records"][0]["scope"]["budget"] = "0"
    changed["records"][0]["fields"]["scope"][1]["value"] = "0"
    other, _ = compile_concept(data, PROFILE, changed, decision, {})
    assert other["ontology_type_id"] != concept["ontology_type_id"]


def test_generic_context_cannot_supply_reuse_evidence_for_a_restricted_exact_record():
    data, bundle, decision = _fixture("收入", "Oryx 部门的收入。")
    context = _fixture()[1]["records"][0]
    context.update(record_id="generic", context_role="related_context")
    bundle["records"].append(context)
    with pytest.raises(ValueError, match="missing_explicit_business_independence"):
        compile_concept(data, PROFILE, bundle, decision, {})
    # One proven generic source must not launder another exact source either.
    context.pop("context_role")
    bundle["exact_alignment_record_ids"].append("generic")
    decision.alignments.append(type(decision.alignments[0])(
        record_id="generic", mapping_kind="exact", quote="收入"))
    with pytest.raises(ValueError, match="missing_explicit_business_independence"):
        compile_concept(data, PROFILE, bundle, decision, {})


def test_independent_generic_definition_can_be_seed_without_absorbing_restricted_record():
    data, bundle, decision = _fixture()
    restricted = _fixture("Oryx收入", "Oryx 业务的收入金额。")[1]["records"][0]
    restricted.update(record_id="restricted", context_role="related_context")
    bundle["records"].append(restricted)
    decision.alignments.append(type(decision.alignments[0])(
        record_id="restricted", mapping_kind="related", quote="Oryx收入"))
    concept, alignments = compile_concept(data, PROFILE, bundle, decision, {})
    assert concept["definition"] == "收入是不绑定具体经营对象的金额口径。"
    assert [item["mapping_kind"] for item in alignments] == ["exact", "related"]
    assert {item["record_id"] for item in concept["classification_evidence"]["measure_reuse_assessment"]} == {"r1"}


@pytest.mark.parametrize("scope,column,accepted", [
    ("all", "region", False), ("all", "business_object", True),
    ("不限定经营对象、地区或年份", "applicability", True),
    ("all except Oryx", "business_object", False),
])
def test_unrestricted_must_explicitly_cover_business_objects(scope, column, accepted):
    data, bundle, decision = _fixture(definition="收入金额的合计。", scope={column: scope})
    if accepted:
        decision.scope_roles = {column: "unrestricted"}
        assert compile_concept(data, PROFILE, bundle, decision, {})[0]["type"] == "Measure"
    else:
        with pytest.raises(ValueError, match="Measure requires"):
            compile_concept(data, PROFILE, bundle, decision, {})


def test_generic_claim_does_not_erase_a_concrete_business_scope_or_standard_name():
    data, bundle, decision = _fixture(scope={"business_object": "Oryx"})
    with pytest.raises(ValueError, match="explicit_business_object_binding"):
        compile_concept(data, PROFILE, bundle, decision, {})
    data, bundle, decision = _fixture()
    bundle["records"][0]["fields"]["name"].append({"column": "standard_name", "value": "Oryx收入"})
    with pytest.raises(ValueError, match="short_name_does_not_replace_standard_name"):
        compile_concept(data, PROFILE, bundle, decision, {})


def test_rejected_genericity_keeps_source_audit_in_checkpoint_step():
    data, bundle, decision = _fixture("流转额", "砾石工坊的流转额，依北岭业务线统计。")
    bundle["task_kind"] = "concept_induction"

    class MockLLM:
        async def ask(self, task, payload, schema):
            assert task == "concept_bundle"
            return decision

    result = asyncio.run(construct_from_bundles(
        data, PROFILE, BuildPlan(), [bundle], MockLLM(), review=False))
    step = result["steps"][0]
    assert step["status"] == "unresolved"
    assessment = step["compiler_evidence_audit"]["measure_reuse_assessment"][0]
    assert assessment["record_id"] == "r1"
    assert assessment["reasons"] == ["missing_explicit_business_independence"]
    assert not result["concepts"] and not result["plan"].object_types


@pytest.mark.parametrize("definition", [
    "收入适用于不同经营对象，但仅供 Oryx。", "收入并非不绑定具体经营对象。",
    "Revenue is independent of business objects but only for Oryx.",
    "Revenue is not independent of business objects.",
    "收入不能保证不绑定具体经营对象。",
])
def test_conditional_or_negated_reuse_claim_is_not_positive_evidence(definition):
    data, bundle, decision = _fixture("收入" if "收入" in definition else "Revenue", definition)
    with pytest.raises(ValueError, match="missing_explicit_business_independence"):
        compile_concept(data, PROFILE, bundle, decision, {})
