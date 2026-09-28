"""Template reuse binds observed records without creating dimensional products."""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from ontology_r2.group_incremental import (BundleReview, ConceptBundleDecision,
                                          construct_from_bundles)
from ontology_r2.models import BuildPlan
from ontology_r2.storage import read_yaml
from ontology_r2.template_projection import (TemplateProjectionDecision,
                                             ProjectionQuote, compile_projection, reuse_projection)


PROFILE = read_yaml(Path(__file__).resolve().parents[1] / "ontologies/internal_model.yaml")


def record(identifier, fruit="苹果", region="华东", formula="passed / inspected", period="Q"):
    # Deliberately neutral column names and no column comments.
    return {"record_id": identifier, "card_id": "card:" + identifier,
            "table": "data.definitions", "row_number": int(identifier[-1]),
            "kind": "definition", "unit": "%", "scope": {"x7": period},
            "fields": {
                "name": [{"column": "x1", "value": fruit + "合格率"},
                         {"column": "x2", "value": "合格率"}],
                "description": [{"column": "x3", "value": fruit + "合格率按" + region + "汇总"},
                                {"column": "x4", "value": "水果合格率统计质量合格比例。"},
                                {"column": "x5", "value": "水果是一类经营对象。"},
                                {"column": "x6", "value": "合格率是合格数量与检验数量之比。可用于不同经营对象。"}],
                "scope": [{"column": "x7", "value": period},
                          {"column": "x8", "value": fruit}, {"column": "x9", "value": region}],
                "formula": [{"column": "x10", "value": formula}],
                "unit": [{"column": "x11", "value": "%"}],
                "reference": [{"column": "x12", "value": identifier}],
            }}


def quote(column, text, record_id="row1"):
    return {"record_id": record_id, "column": column, "quote": text}


def decision():
    return TemplateProjectionDecision.model_validate({
        "label": "水果合格率", "root_type": "Metric", "definition": "水果合格率统计质量合格比例。",
        "label_evidence": [quote("x4", "水果合格率")],
        "witness_record_ids": ["row1", "row2"],
        "components": [
            {"name": "operating_object", "role": "business_object", "label": "水果",
             "root_type": "GeneralObject", "definition": "水果是一类经营对象。",
             "label_evidence": quote("x5", "水果"), "definition_evidence": quote("x5", "水果是一类经营对象。")},
            {"name": "quantity", "role": "measure", "label": "合格率", "root_type": "Measure",
             "definition": "合格率是合格数量与检验数量之比。",
             "label_evidence": quote("x2", "合格率"), "definition_evidence": quote("x6", "合格率是合格数量与检验数量之比。")},
        ],
        "slots": [
            {"name": "product", "role": "business_object", "label": "经营对象", "source_column": "x8",
             "target_component": "operating_object", "evidence": quote("x8", "苹果")},
            {"name": "region", "role": "dimension", "label": "地区", "source_column": "x9",
             "evidence": quote("x9", "华东")},
            {"name": "quantity", "role": "measure", "label": "度量", "target_component": "quantity",
             "evidence": quote("x2", "合格率")},
        ],
        "field_templates": [
            {"column": "x1", "template": "{product}合格率"},
            {"column": "x3", "template": "{product}合格率按{region}汇总"},
            {"column": "x8", "template": "{product}"}, {"column": "x9", "template": "{region}"},
        ],
    })


def setup():
    a, b = record("row1"), record("row2", "橙子", "华南")
    data = SimpleNamespace(snapshot_id="snapshot", evidence={}, tables={})
    bundle = {"bundle_id": "b1", "task_kind": "concept_induction",
              "records": [a, b], "exact_alignment_record_ids": ["row1"]}
    return data, bundle


def test_projection_separates_quantity_object_and_observed_bindings():
    data, bundle = setup()
    plan, template, bindings = compile_projection(data, PROFILE, BuildPlan(), bundle, decision())
    assert {item.parent for item in plan.object_types} == {"Metric", "Measure", "GeneralObject"}
    assert len(bindings) == 2
    assert bindings[0]["slot_values"]["product"] == "苹果"
    assert bindings[1]["slot_values"]["product"] == "橙子"
    assert all(item["mapping_kind"] == "template_instance" for item in bindings)
    assert all(item["identity_claim"] == "none" for item in bindings)
    assert template["slots"][0]["target_type_id"] in {item.id for item in plan.object_types}
    assert not any(item.label in {"苹果合格率", "橙子合格率"} for item in plan.object_types)
    # No invented combination apple/south or orange/east was emitted.
    assert {(x["slot_values"]["product"], x["slot_values"]["region"]) for x in bindings} == {
        ("苹果", "华东"), ("橙子", "华南")}


def test_components_require_explicit_same_role_slot_connections():
    data, bundle = setup()
    proposed = decision()
    proposed.slots[0].target_component = None
    with pytest.raises(ValueError, match="Orphan projected components: operating_object") as error:
        compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    assert "same-role slot.target_component" in str(error.value)
    # An explicit reference to the wrong semantic role does not remove the gap.
    proposed.slots[1].target_component = "operating_object"
    with pytest.raises(ValueError, match="different semantic role"):
        compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    proposed.slots[1].target_component = None
    proposed.slots[0].target_component = "operating_object"
    _, template, _ = compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    assert all(slot["target_type_id"] for slot in template["slots"] if slot["target_component"])
    # Removing an ungrounded component is also valid; an untyped slot remains pending.
    proposed.slots[0].target_component = None
    proposed.components = [component for component in proposed.components if component.name != "operating_object"]
    _, template, _ = compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    assert template["slots"][0]["target_type_id"] is None


def test_reuse_requires_formula_unit_grain_and_complete_definition_compatibility():
    data, bundle = setup()
    _, template, _ = compile_projection(data, PROFILE, BuildPlan(), bundle, decision())
    other = record("row3", "芒果", "非洲")
    packet = {"records": [other], "exact_alignment_record_ids": ["row3"]}
    assert reuse_projection(data, [template], packet)["slot_values"]["product"] == "芒果"
    for column, replacement in (("x10", "failed / inspected"), ("x11", "吨"), ("x7", "Y"),
                                 ("x4", "水果合格率使用另外一种业务口径。")):
        changed = deepcopy(other)
        next(entry for entries in changed["fields"].values() for entry in entries
             if entry["column"] == column)["value"] = replacement
        assert reuse_projection(data, [template], {"records": [changed]}) is None


def test_no_wildcard_formulas_or_unsupported_single_witness():
    data, bundle = setup()
    proposed = decision()
    proposed.witness_record_ids = ["row1"]
    with pytest.raises(ValueError, match="observed variation"):
        compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    proposed = decision().model_dump()
    proposed["field_templates"].append({"column": "x10", "template": "{product}"})
    with pytest.raises(ValueError, match="cannot be wildcarded"):
        compile_projection(data, PROFILE, BuildPlan(), bundle, TemplateProjectionDecision.model_validate(proposed))


def test_explicit_source_class_can_compile_one_observed_witness():
    data, bundle = setup()
    proposed = decision()
    proposed.witness_record_ids = ["row1"]
    proposed.class_definition = ProjectionQuote.model_validate(quote("x4", proposed.definition))
    proposed = TemplateProjectionDecision.model_validate(proposed.model_dump())
    _, _, bindings = compile_projection(data, PROFILE, BuildPlan(), bundle, proposed)
    assert len(bindings) == 1


def test_variable_slot_citation_must_prove_the_actual_witness_capture():
    data, bundle = setup()
    # Both a code and a broad class occur in the source, but neither proves
    # the literal name captured by the product placeholder.
    bundle["records"][0]["fields"]["reference"].append({"column": "fruit_code", "value": "APPLE"})
    for evidence in (quote("fruit_code", "APPLE"), quote("x5", "水果")):
        proposed = decision().model_dump()
        proposed["slots"][0]["source_column"] = None
        proposed["slots"][0]["evidence"] = evidence
        with pytest.raises(ValueError, match="captured value in the cited witness"):
            compile_projection(data, PROFILE, BuildPlan(), bundle,
                               TemplateProjectionDecision.model_validate(proposed))

    # A matching name on a third related record is not a projection witness.
    bundle["records"].append(record("row3"))
    proposed = decision().model_dump()
    proposed["slots"][0]["evidence"] = quote("x8", "苹果", "row3")
    with pytest.raises(ValueError, match="captured value in the cited witness"):
        compile_projection(data, PROFILE, BuildPlan(), bundle,
                           TemplateProjectionDecision.model_validate(proposed))

    # Evidence may use any real witness; do not assume its value is the first.
    proposed["slots"][0]["evidence"] = quote("x8", "橙子", "row2")
    _, _, bindings = compile_projection(data, PROFILE, BuildPlan(), bundle,
                                        TemplateProjectionDecision.model_validate(proposed))
    assert [item["slot_values"]["product"] for item in bindings] == ["苹果", "橙子"]


def test_online_reuse_avoids_second_model_request_and_keeps_separate_instances():
    data, bundle = setup()
    later = {"bundle_id": "b2", "task_kind": "concept_induction", "records": [record("row3", "芒果", "非洲")],
             "exact_alignment_record_ids": ["row3"]}
    class LLM:
        def __init__(self):
            self.calls = []

        async def ask(self, task, payload, schema):
            self.calls.append(task)
            if task == "concept_bundle":
                return ConceptBundleDecision(status="proposed", action="project_template", projection=decision())
            assert task == "group_review"
            return BundleReview(accepted=True)

    llm = LLM()
    output = asyncio.run(construct_from_bundles(data, PROFILE, BuildPlan(), [bundle, later], llm,
                                              max_bundles=2, enable_template_projection=True))
    assert llm.calls == ["concept_bundle", "group_review"]
    assert len(output["template_projections"]) == 1
    assert len(output["template_bindings"]) == 3
    assert output["coverage"]["template_reused_before_llm"] == 1
    assert output["record_alignments"] == []
    assert output["concepts"] == []
    assert output["steps"][1]["action"] == "reuse_template"


def test_projection_repair_keeps_projection_contract_instead_of_forcing_exact_identity():
    data, bundle = setup()

    class RepairLLM:
        calls = 0

        async def ask(self, task, payload, schema):
            assert task == "concept_bundle"
            self.calls += 1
            proposed = decision()
            if self.calls == 1:
                assert "exact_definition only" in payload["template_projection"]["name_policy"]
                proposed.slots[0].evidence = ProjectionQuote.model_validate(quote("x5", "水果"))
            else:
                assert "captured value in the cited witness" in payload["compiler_error"]
                assert "do not add exact record alignments" in payload["repair_instruction"]
                assert "A proposed new concept requires an exact" not in payload["repair_instruction"]
                assert payload["previous_decision"]["action"] == "project_template"
            return ConceptBundleDecision(status="proposed", action="project_template", projection=proposed)

    llm = RepairLLM()
    result = asyncio.run(construct_from_bundles(
        data, PROFILE, BuildPlan(), [bundle], llm, review=False,
        max_repairs_per_bundle=1, enable_template_projection=True))
    assert llm.calls == 2
    assert result["steps"][0]["status"] == "accepted"
    assert len(result["template_projections"]) == 1
    assert len(result["template_bindings"]) == 2
    assert result["record_alignments"] == []


def test_overlap_between_different_templates_never_silently_binds():
    data, bundle = setup()
    _, template, _ = compile_projection(data, PROFILE, BuildPlan(), bundle, decision())
    conflict = deepcopy(template)
    conflict["object_type_id"] = "type:other"
    assert reuse_projection(data, [template, conflict], bundle) is None


def test_type_identity_is_independent_of_matcher_column_names_and_checks_reuse_formula():
    data, bundle = setup()
    plan, first, _ = compile_projection(data, PROFILE, BuildPlan(), bundle, decision())
    renamed = deepcopy(bundle)
    proposed = decision().model_dump()
    # Different physical name column, same definition template and quantity.
    for record_ in renamed["records"]:
        for entry in record_["fields"]["name"]:
            if entry["column"] == "x1":
                entry["column"] = "display_text"
    proposed["field_templates"][0]["column"] = "display_text"
    _, second, _ = compile_projection(data, PROFILE, plan, renamed,
                                      TemplateProjectionDecision.model_validate(proposed))
    assert first["object_type_id"] == second["object_type_id"]
    assert first["template_id"] != second["template_id"]
    proposed["existing_type_id"] = first["object_type_id"]
    for rec in renamed["records"]:
        rec["fields"]["formula"][0]["value"] = "failed / inspected"
        rec["record_id"] = rec["record_id"].replace("1", "3").replace("2", "4")
    def rename_citations(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "record_id":
                    value[key] = item.replace("1", "3").replace("2", "4")
                else:
                    rename_citations(item)
        elif isinstance(value, list):
            for item in value:
                rename_citations(item)
    rename_citations(proposed)
    proposed["witness_record_ids"] = ["row3", "row4"]
    with pytest.raises(ValueError, match="formula"):
        compile_projection(data, PROFILE, plan, renamed, TemplateProjectionDecision.model_validate(proposed))


def test_offline_binding_checks_actual_rows_and_reports_uncovered_scope(tmp_path):
    from ontology_r2.definition_memberships import build_projection_bindings
    from test_definition_memberships import _fixture
    data, index, _ = _fixture(tmp_path)
    try:
        first = next(card for card in index.all_cards(100)["cards"] if card["row_number"] == 1)
        projection = TemplateProjectionDecision(
            label="收入", root_type="Measure", definition="适用于不同经营对象的收入总额",
            label_evidence=[ProjectionQuote(record_id=first["record_id"], column="measure_name", quote="收入")],
            witness_record_ids=[first["record_id"]])
        _, template, _ = compile_projection(data, PROFILE, BuildPlan(), {"records": [first]}, projection)
        result = build_projection_bindings(data, index, [template])
        assert {item["row_number"] for item in result["bindings"]} == {1, 2, 3}
        assert result["coverage"]["records_scanned"] == 5
        assert result["coverage"]["uncovered_or_ambiguous"] == 2
        assert result["coverage"]["llm_calls"] == 0
        limited = build_projection_bindings(data, index, [template], max_records=1)
        assert limited["coverage"]["records_not_scanned"] == 4
        assert limited["coverage"]["partial"]
    finally:
        index.close()
        data.close()
