"""Changing the mapping action must not evade quantity semantics checks."""
from copy import deepcopy

import pytest

from ontology_r2.group_incremental import _compile_checked_projection
from ontology_r2.models import BuildPlan
from test_template_projection import PROFILE, decision, setup


def test_operator_cannot_be_a_primary_or_component_measure():
    data, bundle = setup()
    proposed = decision()
    proposed.root_type, proposed.label = "Measure", "SUM"
    with pytest.raises(ValueError, match="operator alone"):
        _compile_checked_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    proposed = decision()
    proposed.components[1].label = "SUM"
    with pytest.raises(ValueError, match="operator alone"):
        _compile_checked_projection(data, PROFILE, BuildPlan(), bundle, proposed)


def test_scoped_source_does_not_turn_into_generic_measure_by_projection():
    data, bundle = setup()
    for record in bundle["records"]:
        next(entry for entry in record["fields"]["description"] if entry["column"] == "x6")["value"] = "苹果合格率仅用于苹果经营。"
    proposed = decision()
    with pytest.raises(ValueError, match="independent generic source"):
        _compile_checked_projection(data, PROFILE, BuildPlan(), bundle, proposed)


def test_declared_period_grain_cannot_be_erased_by_slot():
    data, bundle = setup()
    data.tables = {"data.definitions": {"columns": [{"column_name": "x7", "column_comment": "期间粒度"}]}}
    proposed = decision()
    from ontology_r2.template_projection import ProjectionField, ProjectionSlot, ProjectionQuote
    proposed.slots.append(ProjectionSlot(name="period", role="dimension", label="期间",
        source_column="x7", evidence=ProjectionQuote(record_id="row1", column="x7", quote="Q")))
    proposed.field_templates.append(ProjectionField(column="x7", template="{period}"))
    with pytest.raises(ValueError, match="calculation parameter"):
        _compile_checked_projection(data, PROFILE, BuildPlan(), bundle, proposed)


def test_valid_generic_measure_and_business_composition_remain_allowed():
    data, bundle = setup()
    plan, _, _ = _compile_checked_projection(data, PROFILE, BuildPlan(), bundle, decision())
    assert {item.parent for item in plan.object_types} == {"GeneralObject", "Measure", "Metric"}
