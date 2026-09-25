"""A shared superclass needs two complete definition records, not shared names."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from ontology_r2.models import BuildPlan, DerivedType, SourceProperty
from ontology_r2.storage import read_yaml
from ontology_r2.type_generalization import (
    compile_generalization, propose_generalization_candidates,
)


PROFILE = read_yaml(Path(__file__).resolve().parents[1] / "ontologies/internal_model.yaml")


def _fixture(*, descriptions=None, formulas=None, units=("元", "元"),
             scopes=None, roots=("Measure", "Measure"), truncated=False):
    descriptions = descriptions or (
        "阿里云业务销售所得营业收入", "商业市场业务销售所得营业收入")
    scopes = scopes or ({"business": "阿里云"}, {"business": "商业市场"})
    evidence = {}
    children = []
    for i, (name, description) in enumerate(zip(("阿里云", "商业市场"), descriptions)):
        props = []
        sources = [("description", description)]
        if formulas is not None and formulas[i] is not None:
            sources.append(("formula", formulas[i]))
        for role, fragment in sources:
            evidence_id = f"record:e{i}-{role}"
            evidence[evidence_id] = {
                "id": evidence_id, "origin": "observed_record",
                "raw_fragment": fragment,
                "raw_fragment_truncated": truncated if role == "description" else False,
                "source_ref": {"table": "fruit.measure_def", "column": role,
                               "record_id": f"r{i}", "snapshot_id": "snap"},
            }
            props.append(SourceProperty(role=role, source_table="fruit.measure_def",
                                        source_column=role, evidence_ids=[evidence_id]))
        children.append(DerivedType(
            id=f"type:{name}", parent=roots[i], label=f"{name}收入",
            definition=description, category="business_type",
            unit=units[i], applicability_scope=scopes[i],
            derivation_kind="exact_definition", evidence_scope="definition_record",
            evidence_ids=[p.evidence_ids[0] for p in props],
            source_properties=props, source_concept_ids=[f"concept:{i}"],
        ))
    return SimpleNamespace(snapshot_id="snap", evidence=evidence, tables={}), BuildPlan(object_types=children)


def _decision():
    return {
        "status": "proposed", "label": "收入",
        "definition": "业务销售所得营业收入", "root_type": "Measure",
        "shared_anchor": "营业收入", "unit": "元", "parent_scope": {},
        "children": [
            {"type_id": "type:阿里云", "shared_quote": "业务销售所得营业收入",
             "specialization_quote": "阿里云业务销售所得营业收入",
             "specialization_term": "阿里云"},
            {"type_id": "type:商业市场", "shared_quote": "业务销售所得营业收入",
             "specialization_quote": "商业市场业务销售所得营业收入",
             "specialization_term": "商业市场"},
        ],
    }


def test_generalization_reparents_two_accepted_types_and_keeps_sources():
    data, core = _fixture()
    candidates = propose_generalization_candidates(core, data, 3)
    assert len(candidates) == 1 and candidates[0]["status"] == "candidate_only"
    compiled, parent = compile_generalization(data, PROFILE, core, _decision())
    assert parent.label == "收入" and parent.parent == "Measure"
    assert parent.derivation_kind == "shared_supertype"
    assert parent.induced_from_type_ids == ["type:商业市场", "type:阿里云"]
    assert parent.evidence_scope == "multiple_definition_records"
    assert len(parent.evidence_ids) == 2
    assert {item.parent for item in compiled.object_types if item.id != parent.id} == {parent.id}
    assert {item.parent for item in core.object_types} == {"Measure"}
    assert compiled.relation_types == core.relation_types
    repeated, same_parent = compile_generalization(data, PROFILE, compiled, _decision())
    assert same_parent.id == parent.id and len(repeated.object_types) == 3


def test_candidate_recall_does_not_use_name_alone_or_claim_identity():
    data, core = _fixture(descriptions=("云平台采摘记录", "线下渠道海运清单"))
    assert propose_generalization_candidates(core, data, 3) == []
    assert propose_generalization_candidates(core, data, 0) == []
    with pytest.raises(ValueError, match="Shared and specialization quotes"):
        compile_generalization(data, PROFILE, core, _decision())


def test_two_character_revenue_anchor_can_ground_rich_shared_definition():
    data, core = _fixture(descriptions=(
        "阿里云收入是业务销售形成的经营所得",
        "商业市场收入是业务销售形成的经营所得"))
    decision = _decision()
    decision.update(definition="收入是业务销售形成的经营所得", shared_anchor="收入")
    decision["children"][0].update(
        shared_quote="收入是业务销售形成的经营所得",
        specialization_quote="阿里云收入是业务销售形成的经营所得")
    decision["children"][1].update(
        shared_quote="收入是业务销售形成的经营所得",
        specialization_quote="商业市场收入是业务销售形成的经营所得")
    assert len(propose_generalization_candidates(core, data, 5)) == 1
    candidate, parent = compile_generalization(data, PROFILE, core, decision)
    assert parent.label == "收入" and len(candidate.object_types) == 3


def test_two_character_anchor_does_not_hide_different_accounting_bases():
    data, core = _fixture(descriptions=(
        "阿里云收入是含税营业收入",
        "商业市场收入是不含税营业收入"))
    decision = _decision()
    decision.update(definition="收入是本期营业收入总额", shared_anchor="收入")
    decision["children"][0].update(
        shared_quote="收入", specialization_quote="阿里云收入")
    decision["children"][1].update(
        shared_quote="收入", specialization_quote="商业市场收入")
    with pytest.raises(ValueError, match="definition templates"):
        compile_generalization(data, PROFILE, core, decision)


@pytest.mark.parametrize("change, error", [
    ("unit", "units conflict"),
    ("root", "mismatched root"),
    ("formula", "formulas are incompatible"),
    ("parent_qualifier", "Parent contains a child specialization"),
    ("scope", "common applicability scope"),
    ("truncated", "complete definition fragment"),
    ("same_child", "two distinct child types"),
    ("short_anchor", "too short"),
])
def test_rejects_unsupported_generalization(change, error):
    kwargs = {}
    if change == "unit":
        kwargs["units"] = ("元", "吨")
    elif change == "root":
        kwargs["roots"] = ("Measure", "Metric")
    elif change == "formula":
        kwargs["formulas"] = ("收入=销量+均价", "收入=销量-均价")
    elif change == "truncated":
        kwargs["truncated"] = True
    data, core = _fixture(**kwargs)
    decision = _decision()
    if change == "parent_qualifier":
        decision["definition"] = "阿里云业务销售所得营业收入"
    elif change == "scope":
        decision["parent_scope"] = {"business": "阿里云"}
    elif change == "same_child":
        decision["children"][1]["type_id"] = decision["children"][0]["type_id"]
    elif change == "short_anchor":
        decision["shared_anchor"] = "收"
    with pytest.raises(ValueError, match=error):
        compile_generalization(data, PROFILE, core, decision)


def test_existing_other_parent_and_observation_only_source_cannot_generalize():
    data, core = _fixture()
    other = DerivedType(id="type:other", parent="Measure", label="其他",
                        definition="其他类型", category="business_type",
                        evidence_ids=["record:e0-description"])
    core.object_types.append(other)
    core.object_types[0] = core.object_types[0].model_copy(update={"parent": other.id})
    with pytest.raises(ValueError, match="already has a derived parent"):
        compile_generalization(data, PROFILE, core, _decision())
    data, core = _fixture()
    data.evidence["record:e0-description"]["origin"] = "observed_value"
    with pytest.raises(ValueError, match="complete definition fragment"):
        compile_generalization(data, PROFILE, core, _decision())


def test_different_full_definitions_cannot_hide_conflict_with_short_quotes():
    data, core = _fixture(descriptions=(
        "阿里云业务销售所得营业收入（含税）",
        "商业市场业务销售所得营业收入（不含税）"))
    decision = _decision()
    decision["children"][0]["specialization_quote"] = "阿里云业务销售所得营业收入"
    decision["children"][1]["specialization_quote"] = "商业市场业务销售所得营业收入"
    with pytest.raises(ValueError, match="definition templates"):
        compile_generalization(data, PROFILE, core, decision)


def test_inconsistent_child_label_cannot_be_reparented():
    data, core = _fixture()
    core.object_types[0] = core.object_types[0].model_copy(update={"label": "阿里云利润"})
    with pytest.raises(ValueError, match="Parent label is absent"):
        compile_generalization(data, PROFILE, core, _decision())


def test_nonproposal_does_not_change_core():
    data, core = _fixture()
    decision = _decision()
    decision["status"] = "unresolved"
    before = deepcopy(core.model_dump())
    assert compile_generalization(data, PROFILE, core, decision) is None
    assert core.model_dump() == before
