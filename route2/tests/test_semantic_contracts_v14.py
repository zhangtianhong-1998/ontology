"""Counterexamples must fail at compilation and on later contract replay."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from ontology_r2.models import BuildPlan, DerivedType
from ontology_r2.semantic_validation import semantic_text_key
from ontology_r2.template_projection import (
    TemplateProjectionDecision, compile_projection, match_projection,
)
from ontology_r2.type_equivalence import compile_equivalence, propose_equivalence_candidates
from test_template_projection import PROFILE
from test_type_equivalence import _fixture, _decision


def projection_case(description="客户是购买产品的主体。仅限直营渠道。", *, composed=False):
    record = {"record_id": "r1", "table": "sample.definitions", "row_number": 1,
              "fields": {"name": [{"column": "n", "value": "客户"}],
                         "description": [{"column": "d", "value": description}]}}
    quote = lambda column, value: {"record_id": "r1", "column": column, "quote": value}
    definition = "客户是购买产品的主体。"
    kwargs = {"class_definition_evidence": [quote("d", definition)]} if composed else {
        "class_definition": quote("d", definition)}
    decision = TemplateProjectionDecision(
        root_type="GeneralObject", label="客户", definition=definition,
        label_evidence=[quote("n", "客户")], witness_record_ids=["r1"], **kwargs)
    return SimpleNamespace(snapshot_id="snap", tables={}, evidence={}), {"records": [record]}, decision


@pytest.mark.parametrize("composed", [False, True])
@pytest.mark.parametrize("restriction", ["仅限直营渠道。", "仅限加盟渠道。", "不得用于生产环境。"])
def test_partial_class_definition_cannot_drop_source_restrictions(composed, restriction):
    data, bundle, decision = projection_case("客户是购买产品的主体。" + restriction, composed=composed)
    with pytest.raises(ValueError, match="complete source field"):
        compile_projection(data, PROFILE, BuildPlan(), bundle, decision)


@pytest.mark.parametrize("composed", [False, True])
def test_complete_source_or_retained_contract_preserves_distinct_identities(composed):
    plan, identities = BuildPlan(), []
    for restriction in ("仅限直营渠道。", "仅限加盟渠道。"):
        data, bundle, decision = projection_case("客户是购买产品的主体。" + restriction, composed=composed)
        raw = bundle["records"][0]["fields"]["description"][0]["value"]
        decision.definition_parameters = {"source_definition_constraint": raw}
        plan, template, _ = compile_projection(data, PROFILE, plan, bundle, decision)
        identities.append(template["object_type_id"])
        assert next(t for t in plan.object_types if t.id == identities[-1]).definition_parameters == {
            "source_definition_constraint": raw}
    assert len(set(identities)) == 2


def test_full_definition_is_supported_without_model_invented_scope():
    data, bundle, decision = projection_case()
    raw = bundle["records"][0]["fields"]["description"][0]["value"]
    decision.definition = raw
    decision.class_definition.quote = raw
    plan, _, _ = compile_projection(data, PROFILE, BuildPlan(), bundle, decision)
    assert plan.object_types[0].definition == raw
    assert plan.object_types[0].applicability_scope == {}


@pytest.mark.parametrize("composed", [False, True])
def test_source_delimited_instances_preserve_class_and_actual_coordinates(composed):
    data, bundle, decision = projection_case("客户是购买产品的主体。实例：华东", composed=composed)
    for number, coordinate in enumerate(("华东", "华南"), 1):
        record = deepcopy(bundle["records"][0])
        record.update(record_id=f"r{number}", row_number=number)
        record["fields"]["description"][0]["value"] = "客户是购买产品的主体。实例：" + coordinate
        record["fields"]["scope"] = [{"column": "axis_value", "value": coordinate}]
        if number == 1:
            bundle["records"][0] = record
        else:
            bundle["records"].append(record)
    data.tables = {"sample.definitions": {"columns": [{"column_name": "axis_value", "column_comment": "维度成员取值"}]}}
    payload = decision.model_dump()
    payload.update(witness_record_ids=["r1", "r2"], slots=[{
        "name": "area", "role": "dimension", "label": "地区", "source_column": "axis_value",
        "evidence": {"record_id": "r1", "column": "axis_value", "quote": "华东"}}],
        field_templates=[{"column": "d", "template": "客户是购买产品的主体。实例：{area}"},
                         {"column": "axis_value", "template": "{area}"}])
    plan, template, bindings = compile_projection(data, PROFILE, BuildPlan(), bundle,
                                                   TemplateProjectionDecision.model_validate(payload))
    assert len(plan.object_types) == 1
    assert [item["slot_values"] for item in bindings] == [{"area": "华东"}, {"area": "华南"}]
    assert template["definition_instance_contracts"][0]["column"] == "d"
    later = deepcopy(bundle["records"][0])
    later["fields"]["scope"][0]["value"] = "北区"
    later["fields"]["description"][0]["value"] = "客户是购买产品的主体。实例：北区"
    assert match_projection(template, later)["slot_values"] == {"area": "北区"}
    # A class restriction cannot be waived by describing it as a slot.
    for row in bundle["records"]:
        row["fields"]["description"][0]["value"] = row["fields"]["description"][0]["value"].replace("实例：", "仅限")
    payload["field_templates"][0]["template"] = "客户是购买产品的主体。仅限{area}"
    data.evidence = {}
    with pytest.raises(ValueError, match="complete source field"):
        compile_projection(data, PROFILE, BuildPlan(), bundle, TemplateProjectionDecision.model_validate(payload))


def test_old_partial_definition_template_cannot_bypass_new_replay_contract():
    data, bundle, decision = projection_case("客户是购买产品的主体。")
    _, template, _ = compile_projection(data, PROFILE, BuildPlan(), bundle, decision)
    bundle["records"][0]["fields"]["description"][0]["value"] += "仅限直营渠道。"
    template["invariants"]["d"] += "仅限直营渠道。"
    assert match_projection(template, bundle["records"][0]) is None


def measure_case(*, captured="合格率", name="合格率", alias=None, variable=False):
    data, bundle, decision = projection_case("客户是购买产品的主体。")
    bundle["records"][0]["fields"]["name"].append({"column": "m", "value": captured})
    eid = "record:m-name"
    evidence = {"id": eid, "origin": "observed_record", "raw_fragment": name,
                "source_ref": {"table": "sample.measures", "column": "n", "record_id": "m1",
                               "snapshot_id": data.snapshot_id}}
    data.evidence[eid] = evidence
    props = [{"role": "name", "source_table": "sample.measures", "source_column": "n", "evidence_ids": [eid]}]
    eids = [eid]
    if alias:
        aid = "record:m-alias"
        data.evidence[aid] = {**evidence, "id": aid, "raw_fragment": alias,
                              "source_ref": {**evidence["source_ref"], "column": "aliases"}}
        props.append({"role": "alias", "source_table": "sample.measures", "source_column": "aliases", "evidence_ids": [aid]})
        eids.append(aid)
    target = DerivedType(id="type:measure", parent="Measure", label=name, definition=name + "的定义",
                         category="business_type", evidence_ids=eids, source_properties=props)
    payload = decision.model_dump()
    payload["slots"] = [{"name": "quantity", "label": "度量", "role": "measure",
                         "target_type_id": target.id,
                         "evidence": {"record_id": "r1", "column": "m", "quote": captured}}]
    if variable:
        payload["field_templates"] = [{"column": "m", "template": "{quantity}"}]
    return data, bundle, TemplateProjectionDecision.model_validate(payload), BuildPlan(object_types=[target])


@pytest.mark.parametrize("variable", [False, True])
def test_fixed_and_variable_targets_reject_different_meaning(variable):
    data, bundle, decision, core = measure_case(name="时长", variable=variable)
    with pytest.raises(ValueError, match="Every witness must match"):
        compile_projection(data, PROFILE, core, bundle, decision)


@pytest.mark.parametrize("variable", [False, True])
def test_full_source_name_or_verified_alias_authorizes_both_slot_modes(variable):
    for alias in (None, '["合格率"]'):
        data, bundle, decision, core = measure_case(name="质量通过率" if alias else "合格率", alias=alias, variable=variable)
        _, template, _ = compile_projection(data, PROFILE, core, bundle, decision)
        assert match_projection(template, bundle["records"][0])
        legacy = deepcopy(template)
        legacy["slots"][0].pop("target_value_contract")
        assert match_projection(legacy, bundle["records"][0]) is None


@pytest.mark.parametrize("left,right", [
    ("服务费按销售金额的1.0%计收", "服务费按销售金额的10%计收"),
    ("范围为1.2至3.4", "范围为12至34"),
    ("有效期为2025.01.01", "有效期为20250101"),
    ("阈值为1e-3", "阈值为1e3"),
    ("仅包含1,2号", "仅包含12号"),
    ("阈值为1 0", "阈值为10"),
    ("条件为x²", "条件为x2"),
])
def test_numeric_symbols_are_not_removed_by_equivalence(left, right):
    assert semantic_text_key(left) != semantic_text_key(right)
    data, core = _fixture(descriptions=(left, right), formulas=(None, None))
    candidate = propose_equivalence_candidates(core, data, 1)[0]
    with pytest.raises(ValueError, match="Complete source descriptions differ"):
        compile_equivalence(data, core, candidate, _decision(data, candidate))


def test_harmless_formula_spacing_still_compares_equal():
    assert semantic_text_key("费率 = 收入 * 1.0%") == semantic_text_key("费率=收入*1.0%")
