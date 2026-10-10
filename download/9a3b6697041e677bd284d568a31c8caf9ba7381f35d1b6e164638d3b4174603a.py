"""Accepted type equivalence requires complete bilateral source evidence."""

from types import SimpleNamespace

import pytest

from ontology_r2.models import BuildPlan, DerivedType, SourceProperty
from ontology_r2.type_equivalence import (
    compile_equivalence, propose_equivalence_candidates,
)


def _fixture(*, descriptions=None, formulas=None, roots=None, units=None,
             scopes=None, truncated=False):
    descriptions = descriptions or (
        "水果销售利润是水果销售收入扣除水果销售成本后的利润",
        "水果销售利润是水果销售收入扣除水果销售成本后的利润",
    )
    formulas = formulas if formulas is not None else (
        "水果销售利润 = 水果销售收入 - 水果销售成本",
        "水果销售利润 = 水果销售收入 - 水果销售成本",
    )
    roots = roots or tuple("Metric" for _ in descriptions)
    units = units or tuple("元" for _ in descriptions)
    scopes = scopes or tuple({} for _ in descriptions)
    evidence, types = {}, []
    for i, description in enumerate(descriptions):
        props, evidence_ids = [], []
        for role, value in (("description", description), ("formula", formulas[i])):
            if value is None:
                continue
            evidence_id = f"record:e{i}-{role}"
            evidence[evidence_id] = {
                "id": evidence_id, "origin": "observed_record",
                "raw_fragment": value,
                "raw_fragment_truncated": bool(truncated and i == 1 and role == "description"),
                "source_ref": {"table": f"fruit.metric_{i}", "column": role,
                               "record_id": f"r{i}", "snapshot_id": "snap"},
            }
            props.append(SourceProperty(role=role, source_table=f"fruit.metric_{i}",
                                        source_column=role, evidence_ids=[evidence_id]))
            evidence_ids.append(evidence_id)
        types.append(DerivedType(
            id=f"type:metric_{i}", parent=roots[i],
            label="水果销售利润", definition=description, category="business_type",
            unit=units[i], applicability_scope=scopes[i],
            derivation_kind="exact_definition", evidence_scope="definition_record",
            source_properties=props, evidence_ids=evidence_ids,
        ))
    return SimpleNamespace(snapshot_id="snap", evidence=evidence), BuildPlan(object_types=types)


def _decision(data, candidate):
    source_id, target_id = candidate["source_type_id"], candidate["target_type_id"]
    left, right = int(source_id.rsplit("_", 1)[1]), int(target_id.rsplit("_", 1)[1])
    result = {
        "status": "proposed", "source_type_id": source_id,
        "target_type_id": target_id,
        "source_description_evidence_id": f"record:e{left}-description",
        "target_description_evidence_id": f"record:e{right}-description",
        "source_description_quote": data.evidence[f"record:e{left}-description"]["raw_fragment"],
        "target_description_quote": data.evidence[f"record:e{right}-description"]["raw_fragment"],
        "semantic_equivalence_explanation": "双方指向同一销售利润计算口径和单位",
    }
    if f"record:e{left}-formula" in data.evidence:
        result.update(source_formula_evidence_id=f"record:e{left}-formula",
                      source_formula_quote=data.evidence[f"record:e{left}-formula"]["raw_fragment"])
    if f"record:e{right}-formula" in data.evidence:
        result.update(target_formula_evidence_id=f"record:e{right}-formula",
                      target_formula_quote=data.evidence[f"record:e{right}-formula"]["raw_fragment"])
    return result


def test_complete_bilateral_description_and_formula_support_one_pair():
    data, core = _fixture()
    candidates = propose_equivalence_candidates(core, data, 5)
    assert len(candidates) == 1 and candidates[0]["status"] == "candidate_only"
    assertion = compile_equivalence(data, core, candidates[0], _decision(data, candidates[0]))
    assert assertion["source_type_id"] == "type:metric_0"
    assert assertion["target_type_id"] == "type:metric_1"
    assert assertion["canonical_type_id"] == "type:metric_0"
    assert len(assertion["evidence_ids"]) == 4
    assert assertion["source_description_quote"] == data.evidence["record:e0-description"]["raw_fragment"]
    assert len(core.object_types) == 2


@pytest.mark.parametrize("left,right", [
    ({"period_scope": "公历年"}, {"period_scope": "公历月"}),
    ({"budget": "1"}, {"budget": "0"}),
    ({"period_scope": "公历年"}, {}),
    ({"operator": "SUM"}, {"operator": "sum"}),
])
def test_definition_parameters_prevent_recall_and_stale_pair_acceptance(left, right):
    data, core = _fixture()
    candidate = propose_equivalence_candidates(core, data, 1)[0]
    decision = _decision(data, candidate)
    core.object_types[0].definition_parameters = left
    core.object_types[1].definition_parameters = right
    assert propose_equivalence_candidates(core, data, 2) == []
    with pytest.raises(ValueError, match="Definition parameters differ"):
        compile_equivalence(data, core, candidate, decision)


def test_equal_parameters_are_explicit_and_changed_parameters_invalidate_pair():
    data, core = _fixture()
    for item in core.object_types:
        item.definition_parameters = {"period_scope": "公历年", "budget": "1"}
    candidate = propose_equivalence_candidates(core, data, 1)[0]
    assertion = compile_equivalence(data, core, candidate, _decision(data, candidate))
    assert assertion["definition_parameters"] == candidate["definition_parameters"] == {
        "period_scope": "公历年", "budget": "1"}
    for item in core.object_types:
        item.definition_parameters = {"period_scope": "公历月", "budget": "1"}
    with pytest.raises(ValueError, match="Candidate identity changed"):
        compile_equivalence(data, core, candidate, _decision(data, candidate))


def test_legacy_empty_parameters_keep_equivalence_identity():
    data, core = _fixture()
    candidate = propose_equivalence_candidates(core, data, 1)[0]
    legacy = core.model_dump()
    for item in legacy["object_types"]:
        item.pop("definition_parameters")
    restored = BuildPlan.model_validate(legacy)
    assert propose_equivalence_candidates(restored, data, 1)[0] == candidate
    candidate.pop("definition_parameters")
    assert compile_equivalence(data, restored, candidate, _decision(data, candidate))["definition_parameters"] == {}


def test_shared_name_alone_cannot_recall_conflicting_root_unit_scope_or_truncation():
    for kwargs in (
        {"roots": ("Metric", "Measure")},
        {"units": ("元", "万元")},
        {"units": (None, None)},
        {"scopes": ({"region": "华东"}, {"region": "华南"})},
        {"truncated": True},
    ):
        data, core = _fixture(**kwargs)
        assert propose_equivalence_candidates(core, data, 5) == []


@pytest.mark.parametrize("change,error", [
    ("short_quote", "complete source fragment"),
    ("different_formula", "calculation formulas differ"),
    ("missing_formula", "only one side"),
    ("different_prose_without_formula", "Complete source descriptions differ"),
    ("tax_qualifier", "Complete source descriptions differ"),
    ("wrong_pair", "exact candidate pair"),
])
def test_compiler_rejects_unsupported_equivalence(change, error):
    kwargs = {}
    if change == "different_formula":
        kwargs["formulas"] = ("收入-成本", "收入+成本")
    elif change == "missing_formula":
        kwargs["formulas"] = ("收入-成本", None)
    elif change == "different_prose_without_formula":
        kwargs["formulas"] = (None, None)
        kwargs["descriptions"] = ("水果销售利润为水果营业所得", "水果销售利润为水果业务所得")
    elif change == "tax_qualifier":
        kwargs["descriptions"] = ("水果销售利润是含税营业利润", "水果销售利润是不含税营业利润")
    data, core = _fixture(**kwargs)
    candidate = propose_equivalence_candidates(core, data, 1)[0]
    decision = _decision(data, candidate)
    if change == "short_quote":
        decision["source_description_quote"] = "水果销售利润"
    elif change == "wrong_pair":
        decision["target_type_id"] = "type:unknown"
    with pytest.raises(ValueError, match=error):
        compile_equivalence(data, core, candidate, decision)


def test_no_change_does_not_compile_or_modify_types():
    data, core = _fixture()
    candidate = propose_equivalence_candidates(core, data, 1)[0]
    decision = _decision(data, candidate)
    decision["status"] = "no_change"
    assert compile_equivalence(data, core, candidate, decision) is None
    assert len(core.object_types) == 2


def test_same_formula_does_not_merge_different_description_or_business_scope():
    data, core = _fixture(descriptions=(
        "水果销售利润是水果销售收入扣除水果销售成本后的利润",
        "水果销售利润表示水果销售收入减去水果销售成本的金额",
    ))
    candidate = propose_equivalence_candidates(core, data, 1)[0]
    with pytest.raises(ValueError, match="Complete source descriptions differ"):
        compile_equivalence(data, core, candidate, _decision(data, candidate))
    data, core = _fixture(descriptions=(
        "水果销售利润仅含直营门店收入减成本",
        "水果销售利润仅含加盟门店收入减成本",
    ), formulas=("收入-成本", "收入-成本"))
    candidate = propose_equivalence_candidates(core, data, 1)[0]
    with pytest.raises(ValueError, match="Complete source descriptions differ"):
        compile_equivalence(data, core, candidate, _decision(data, candidate))


def test_formula_in_only_one_column_requires_full_expression_in_other_definition():
    data, core = _fixture(
        descriptions=("水果销售利润 = 水果销售收入 - 水果销售成本",
                      "水果销售利润=水果销售收入-水果销售成本"),
        formulas=("水果销售利润 = 水果销售收入 - 水果销售成本", None),
    )
    candidate = propose_equivalence_candidates(core, data, 1)[0]
    assertion = compile_equivalence(data, core, candidate, _decision(data, candidate))
    assert assertion["source_formula_evidence_ids"] == ["record:e0-formula"]
    assert assertion["target_formula_evidence_ids"] == []


def test_reusable_measure_equivalence_uses_complete_meaning_not_operator_only():
    data, core = _fixture(
        descriptions=("收入是可用于不同经营对象的营业所得金额", "收入是可用于不同经营对象的营业所得金额"),
        formulas=(None, None), roots=("Measure", "Measure"), units=("元", "元"),
    )
    core.object_types = [item.model_copy(update={"label": "收入", "aggregation_operator": None})
                         for item in core.object_types]
    candidate = propose_equivalence_candidates(core, data, 2)[0]
    assertion = compile_equivalence(data, core, candidate, _decision(data, candidate))
    assert assertion["root_type"] == "Measure" and assertion["unit"] == "元"
    core.object_types[1] = core.object_types[1].model_copy(update={"unit": "万元"})
    assert propose_equivalence_candidates(core, data, 2) == []


def test_measure_rank_may_be_unitless_but_operator_mismatch_blocks_recall():
    data, core = _fixture(
        descriptions=("当年预算排名按预算值降序给出序位", "当年预算排名按预算值降序给出序位"),
        formulas=(None, None), roots=("Measure", "Measure"), units=(None, None),
    )
    core.object_types = [item.model_copy(update={"label": "当年预算排名"})
                         for item in core.object_types]
    assert len(propose_equivalence_candidates(core, data, 2)) == 1
    core.object_types[1].aggregation_operator = "max"
    assert propose_equivalence_candidates(core, data, 2) == []
