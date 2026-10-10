"""Formatting compatibility never changes the source facts a matcher accepts."""
from copy import deepcopy

import pytest

from ontology_r2.models import BuildPlan
from ontology_r2.template_projection import (
    ProjectionField, ProjectionQuote, compile_projection, reuse_projection,
)
from test_template_projection import PROFILE, decision, quote, record, setup


def test_fixed_formula_unit_fields_remain_exact_invariants_and_parameters():
    data, bundle = setup()
    proposed = decision()
    proposed.field_templates.extend([
        ProjectionField(column="x10", template="passed / inspected"),
        ProjectionField(column="x11", template="%")])
    proposed.definition_parameters = {"formula": "passed / inspected", "unit": "%"}
    _, template, bindings = compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    assert len(bindings) == 2
    assert template["invariants"]["x10"] == "passed / inspected"
    assert template["invariants"]["x11"] == "%"
    changed = deepcopy(bundle)
    changed["records"][1]["fields"]["formula"][0]["value"] = "failed / inspected"
    with pytest.raises(ValueError, match="Every witness must match"):
        compile_projection(type(data)(snapshot_id="changed", evidence={}, tables={}), PROFILE,
                           BuildPlan(), changed, proposed)
    assert reuse_projection(data, [template], {"records": [record("row3", formula="failed / inspected")]}) is None
    proposed.field_templates[-2] = ProjectionField(column="x10", template="{product}")
    with pytest.raises(ValueError, match="cannot be wildcarded"):
        compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)


def test_full_quote_supports_contained_label_but_not_an_absent_generalization():
    data, bundle = setup()
    proposed = decision()
    proposed.label_evidence = [ProjectionQuote.model_validate(quote("x4", "水果合格率统计质量合格比例。"))]
    compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    proposed.label_evidence = [ProjectionQuote.model_validate(quote("x1", "苹果合格率")),
                              ProjectionQuote.model_validate(quote("x1", "橙子合格率", "row2"))]
    with pytest.raises(ValueError, match="Every projected label fragment"):
        compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)


def test_slot_context_quote_must_support_its_actual_matching_path():
    data, bundle = setup()
    proposed = decision()
    proposed.slots[0].evidence = ProjectionQuote.model_validate(quote("x3", "苹果合格率按华东汇总"))
    _, template, bindings = compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    product = next(slot for slot in template["slots"] if slot["name"] == "product")
    assert product["witness_value"] == "苹果"
    assert product["value_evidence"]["quote"] == "苹果合格率按华东汇总"
    assert bindings[0]["slot_values"]["product"] == "苹果"
    # Equal text on an unrelated field does not prove the executed path.
    bundle["records"][0]["fields"]["reference"].append({"column": "unrelated_note", "value": "苹果"})
    proposed.slots[0].evidence = ProjectionQuote.model_validate(quote("unrelated_note", "苹果"))
    with pytest.raises(ValueError, match="matching field path"):
        compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)


def test_anchored_fields_bind_adjacent_slots_before_matching_title():
    data, bundle = setup()
    proposed = decision()
    for rec in bundle["records"]:
        fruit = next(e["value"] for e in rec["fields"]["scope"] if e["column"] == "x8")
        region = next(e["value"] for e in rec["fields"]["scope"] if e["column"] == "x9")
        rec["fields"]["name"][0]["value"] = region + fruit + "合格率"
    proposed.slots[0].source_column = None
    proposed.slots[1].source_column = None
    proposed.field_templates[0] = ProjectionField(column="x1", template="{region}{product}合格率")
    _, _, bindings = compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    assert bindings[1]["slot_values"]["product"] == "橙子"
    # With no anchored field and no source-column binding, ambiguity remains.
    broken = decision()
    broken.slots = broken.slots[:2]
    broken.components = broken.components[:1]
    for slot in broken.slots:
        slot.source_column = None
    broken.field_templates = [ProjectionField(column="x1", template="{region}{product}合格率")]
    broken.witness_record_ids = ["row1"]
    with pytest.raises(ValueError, match="Adjacent unbound"):
        compile_projection(data, PROFILE, BuildPlan(), bundle, broken)
