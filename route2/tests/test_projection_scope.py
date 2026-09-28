"""Class applicability requires an explicit, invariant source scope claim."""
from types import SimpleNamespace

import pytest

from ontology_r2.group_incremental import _compile_checked_projection
from ontology_r2.models import BuildPlan, DerivedType
from ontology_r2.template_projection import TemplateProjectionDecision, compile_projection
from test_template_projection import PROFILE, decision, setup


def scoped_source(value="FRUIT"):
    data, bundle = setup()
    for record in bundle["records"]:
        record["fields"]["scope"].append({"column": "domain", "value": value})
        record["scope"]["domain"] = value
    proposed = decision()
    proposed.applicability_scope = {"domain": value}
    return data, bundle, proposed


def test_explicit_invariant_scope_is_preserved_and_part_of_type_identity():
    data, bundle, proposed = scoped_source()
    plan, template, _ = _compile_checked_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    subject = next(item for item in plan.object_types if item.id == template["object_type_id"])
    assert subject.applicability_scope == template["applicability_scope"] == {"domain": "FRUIT"}
    other_data, other_bundle, other = scoped_source("OTHER")
    _, changed, _ = _compile_checked_projection(other_data, PROFILE, BuildPlan(), other_bundle, other)
    assert changed["object_type_id"] != template["object_type_id"]
    other.existing_type_id = template["object_type_id"]
    with pytest.raises(ValueError, match="applicability scope"):
        _compile_checked_projection(other_data, PROFILE, plan, other_bundle, other)


def test_scope_is_not_inferred_and_name_or_variable_field_cannot_supply_it():
    data, bundle, proposed = scoped_source()
    proposed.applicability_scope = {}
    plan, template, _ = _compile_checked_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    assert next(item for item in plan.object_types if item.id == template["object_type_id"]).applicability_scope == {}
    for bad_scope, error in (({"x2": "合格率"}, "scope field"), ({"x8": "苹果"}, "invariant scope")):
        data, bundle, proposed = scoped_source()
        proposed.applicability_scope = bad_scope
        with pytest.raises(ValueError, match=error):
            _compile_checked_projection(data, PROFILE, BuildPlan(), bundle, proposed)


def test_observed_period_or_declared_coordinate_cannot_be_class_scope():
    for value, declaration in (("2025Q1", ""), ("EAST", "观测地区维度坐标")):
        data, bundle, proposed = scoped_source(value)
        data.tables = {"data.definitions": {"columns": [{"column_name": "domain", "column_comment": declaration}]}}
        with pytest.raises(ValueError, match="Observed period or coordinate"):
            _compile_checked_projection(data, PROFILE, BuildPlan(), bundle, proposed)


def test_varying_measure_on_general_object_cannot_target_one_concrete_quantity():
    records = [{"record_id": f"r{i}", "row_number": i, "table": "x.widgets", "fields": {
        "name": [{"column": "name", "value": quantity + "展示"}, {"column": "q", "value": quantity}],
        "description": [{"column": "definition", "value": "展示是一类数据呈现对象。"}]}}
        for i, quantity in enumerate(("收入", "利润"), 1)]
    evidence = lambda column, quote: {"record_id": "r1", "column": column, "quote": quote}
    proposed = TemplateProjectionDecision.model_validate({
        "label": "展示", "root_type": "GeneralObject", "definition": "展示是一类数据呈现对象。",
        "label_evidence": [evidence("definition", "展示")],
        "class_definition": evidence("definition", "展示是一类数据呈现对象。"),
        "witness_record_ids": ["r1", "r2"],
        "slots": [{"name": "quantity", "role": "measure", "label": "量", "source_column": "q",
                   "target_type_id": "type:income", "evidence": evidence("q", "收入")}],
        "field_templates": [{"column": "q", "template": "{quantity}"},
                            {"column": "name", "template": "{quantity}展示"}],
    })
    data = SimpleNamespace(snapshot_id="snapshot", evidence={})
    core = BuildPlan(object_types=[DerivedType(id="type:income", parent="Measure", label="收入",
                                               definition="通用收入量", evidence_ids=[])])
    with pytest.raises(ValueError, match="varying measure slot"):
        compile_projection(data, PROFILE, core, {"records": records}, proposed)
    proposed.slots[0].target_type_id = None
    _, template, _ = compile_projection(data, PROFILE, core, {"records": records}, proposed)
    assert template["slots"][0]["target_type_id"] is None
