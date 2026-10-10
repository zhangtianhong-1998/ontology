"""Display labels and definition parameters cannot alter source meaning."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ontology_r2.group_incremental import (
    ConceptBundleDecision, _compiled_object_type, compile_concept, construct_from_bundles,
)
from ontology_r2.models import BuildPlan
from test_group_incremental import PROFILE


def _fixture(*, period="公历年", formula="收入 - 成本", region="全国", label="苹果经营利润"):
    data = SimpleNamespace(snapshot_id="snap", evidence={}, tables={"definitions": {"columns": [
        {"column_name": "period_scope", "column_comment": "适用期间粒度"},
        {"column_name": "region_scope", "column_comment": "适用地区范围"},
        {"column_name": "budget", "column_comment": "预算标记"},
    ]}})
    description = "苹果经营利润是苹果业务的收入减去成本。"
    scope = {"period_scope": period, "region_scope": region}
    record = {"record_id": "profit", "table": "definitions", "kind": "definition", "row_number": 1,
              "scope": scope, "unit": "元", "fields": {
                  "name": [{"column": "name", "value": "苹果经营利润"}],
                  "description": [{"column": "definition", "value": description}],
                  "formula": [{"column": "formula", "value": formula}],
                  "unit": [{"column": "unit", "value": "元"}],
                  "scope": [{"column": key, "value": value} for key, value in scope.items()],
              }}
    bundle = {"bundle_id": "bundle:profit", "task_kind": "concept_induction",
              "records": [record], "exact_alignment_record_ids": ["profit"]}
    decision = ConceptBundleDecision(status="proposed", label=label, definition=description,
        root_type="Metric", ontology_level="type", classification_basis="business_driven_metric",
        classification_quote=description, business_object_quote="苹果", aggregation_operator=None,
        scope_roles={"period_scope": "parameter", "region_scope": "applicability"},
        alignments=[{"record_id": "profit", "mapping_kind": "exact", "quote": "苹果经营利润"}])
    return data, bundle, decision


def test_unique_source_name_plus_annotation_is_display_only():
    data, bundle, decision = _fixture(label="苹果经营利润（仅华东）", region="仅华东")
    concept, _ = compile_concept(data, PROFILE, bundle, decision, {})
    assert concept["label"] == "苹果经营利润"
    assert concept["proposed_label"] == "苹果经营利润（仅华东）"
    proof = concept["label_normalization"]
    assert proof["source_name_witnesses"][0]["record_id"] == "profit"
    assert proof["annotation_is_semantic_evidence"] is False
    assert concept["applicability_scope"] == {"region_scope": "仅华东"}
    assert concept["definition_parameters"] == {"period_scope": "公历年"}
    assert concept["source_formulas"] == ["收入 - 成本"]


@pytest.mark.parametrize("label", ["苹果利润", "新业务苹果经营利润", "苹果经营利润-扣费后", "苹果经营利润（华东）（扣费后）"])
def test_unmatched_or_rewritten_names_are_not_normalized(label):
    data, bundle, decision = _fixture(label=label)
    with pytest.raises(ValueError, match="label is absent"):
        compile_concept(data, PROFILE, bundle, decision, {})


def test_name_cannot_be_borrowed_from_description_or_related_context():
    data, bundle, decision = _fixture(label="苹果经营利润（全国）")
    bundle["records"][0]["fields"].pop("name")
    other = deepcopy(bundle["records"][0])
    other.update(record_id="context", context_role="related_context")
    other["fields"]["name"] = [{"column": "name", "value": "苹果经营利润"}]
    bundle["records"].append(other)
    with pytest.raises(ValueError, match="label is absent"):
        compile_concept(data, PROFILE, bundle, decision, {})


def test_period_parameters_preserve_identity_without_equal_coordinate_filter():
    outputs = []
    for options in ({}, {"period": "季度"}, {"formula": "(收入 - 成本) * 0.9"}, {"region": "仅华东"}):
        data, bundle, decision = _fixture(**options)
        concept, _ = compile_concept(data, PROFILE, bundle, decision, {})
        item = _compiled_object_type(concept)
        outputs.append(item)
        assert "period_scope" not in item.applicability_scope
        assert item.definition_parameters["period_scope"] == options.get("period", "公历年")
        assert concept["parameter_evidence"]["period_scope"][0]["declaration"] == "适用期间粒度"
    assert len({item.id for item in outputs}) == 4
    assert {item.label for item in outputs} == {"苹果经营利润"}


@pytest.mark.parametrize("value", ["2025", "2025Q1", "2025-10", "2025年10月", ""])
def test_observation_period_is_not_a_definition_parameter(value):
    data, bundle, decision = _fixture(period=value)
    with pytest.raises(ValueError, match="Definition parameter requires") as error:
        compile_concept(data, PROFILE, bundle, decision, {})
    assert "field 'period_scope' lacks a checked declaration" in str(error.value)


def test_ordinary_scope_cannot_be_erased_as_a_parameter():
    data, bundle, decision = _fixture()
    decision.scope_roles["region_scope"] = "parameter"
    with pytest.raises(ValueError, match="Definition parameter requires"):
        compile_concept(data, PROFILE, bundle, decision, {})
    decision.scope_roles["region_scope"] = "applicability"
    data.tables["definitions"]["columns"][0]["column_comment"] = "来源值"
    with pytest.raises(ValueError, match="Definition parameter requires"):
        compile_concept(data, PROFILE, bundle, decision, {})


def test_parameter_flags_preserve_different_values_in_type_identity():
    ids = []
    for flag in ("0", "1"):
        data, bundle, decision = _fixture()
        record = bundle["records"][0]
        record["scope"]["budget"] = flag
        record["fields"]["scope"].append({"column": "budget", "value": flag})
        decision.scope_roles["budget"] = "parameter"
        concept, _ = compile_concept(data, PROFILE, bundle, decision, {})
        assert concept["definition_parameters"]["budget"] == flag
        ids.append(concept["ontology_type_id"])
    assert len(set(ids)) == 2


def test_packet_supplies_exact_names_and_parameter_source_declarations():
    data, bundle, decision = _fixture()
    class LLM:
        async def ask(self, task, payload, schema):
            assert payload["canonical_name_choices"] == [{"value": "苹果经营利润", "record_id": "profit", "column": "name", "role": "name"}]
            period = next(item for item in payload["scope_role_evidence"] if item["column"] == "period_scope")
            assert period["parameter_basis"]["declaration"] == "适用期间粒度"
            return decision
    result = asyncio.run(construct_from_bundles(data, PROFILE, BuildPlan(), [bundle], LLM(), review=False))
    assert result["steps"][0]["status"] == "accepted"
    assert result["plan"].object_types[0].definition_parameters == {"period_scope": "公历年"}


def test_metric_aggregation_operator_is_still_rejected():
    data, bundle, decision = _fixture()
    decision.aggregation_operator = "filter"
    with pytest.raises(ValueError, match="Metric calculation belongs"):
        compile_concept(data, PROFILE, bundle, decision, {})
    assert "Metric MUST use null" in ConceptBundleDecision.model_json_schema()["properties"]["aggregation_operator"]["description"]
