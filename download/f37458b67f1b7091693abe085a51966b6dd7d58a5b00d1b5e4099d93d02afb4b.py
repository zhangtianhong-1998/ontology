"""Incremental relation evidence must not redefine types or widen row scope."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from ontology_r2.association_rules import build_association_rules
from ontology_r2.group_incremental import RelationBundleDecision, compile_relation
from ontology_r2.incremental import direct_mapping
from ontology_r2.instance_bundles import _relation_bundle, validate_bundle_options
from ontology_r2.models import DerivedType
from ontology_r2.relation_contract import canonical_relation_definition, merge_relation_type_evidence
from ontology_r2.relations import Extractor
from ontology_r2.storage import Dataset, Sink, read_yaml
from test_semantic_cards import _table

PROFILE = read_yaml(Path(__file__).parents[1] / "ontologies/internal_model.yaml")


@pytest.fixture
def checked_relations(tmp_path):
    root = tmp_path / "input"
    _table(root, "endpoint", {"id": "主键", "name": "参数名称", "ref": "引用标准编码",
                             "definition": "参数说明", "kind": "业务类型", "area": "地区"}, [
        {"id": "s1", "name": "收入输出", "ref": "K1", "definition": "收入输出引用收入定义", "kind": "API", "area": "CN"},
        {"id": "s2", "name": "成本输出", "ref": "K2", "definition": "成本输出引用成本定义", "kind": "CARD", "area": "CN"},
        {"id": "s3", "name": "利润输出", "ref": "K3", "definition": "利润输出引用利润定义", "kind": "API", "area": "CN"},
        {"id": "s4", "name": "库存输出", "ref": "K4", "definition": "库存输出引用库存定义", "kind": "API", "area": "CN"},
    ])
    _table(root, "dictionary", {"id": "主键", "name": "标准名称", "code": "标准编码",
                               "definition": "标准定义", "market": "地区"}, [
        {"id": f"t{i}", "name": name, "code": f"K{i}", "definition": definition, "market": "CN"}
        for i, (name, definition) in enumerate([
            ("收入", "收入定义为已确认的销售总金额"), ("成本", "成本定义为已确认的采购总金额"),
            ("利润", "利润定义为收入减去成本"), ("库存", "库存定义为尚未售出数量")], 1)
    ])
    work = tmp_path / "work"; work.mkdir()
    data = Dataset(root, work)
    try:
        proposals = [{"source": {"table": "fruit.endpoint", "field": "ref"},
                      "target": {"table": "fruit.dictionary", "field": "code"},
                      "selector": {"kind": kind}, "scope_bindings": {"area": "market"},
                      "transform": "identity"} for kind in ("API", "CARD")]
        found = asyncio.run(build_association_rules(
            data, {"candidates": [], "checks": []}, {"proposals": proposals}))
        bundles = {}
        for rule in found["rules"]:
            if rule.get("selector", {}).get("kind") not in ("API", "CARD"):
                continue
            assert rule["status"] == "checked_technical"
            bundle, reason = _relation_bundle(data, rule, validate_bundle_options({"max_bundle_bytes": 24000}))
            assert reason is None
            bundles[rule["selector"]["kind"]] = bundle
        core, _ = direct_mapping(data)
        yield data, core, bundles
    finally:
        data.close()


def decision_for(bundle, pair_index=0, explanation="这条参数引用目标标准定义"):
    pair = bundle["examples"]["positive"][pair_index]
    records = {item["record_id"]: item for item in bundle["records"]}
    source = records[pair["source_record_id"]]
    target = records[pair["target_record_id"]]
    return RelationBundleDecision(
        status="proposed", parent_relation="points_to", predicate_name="definition_reference",
        label="points to", definition=explanation, reason="字段声明和引文支持定义引用",
        source_quote=source["fields"]["description"][0]["value"],
        target_quote=target["fields"]["description"][0]["value"])


def execute(data, plan, tmp_path):
    class NoLLM:
        async def ask(self, *args, **kwargs):
            raise AssertionError("Reviewed identifier witnesses do not call models")
    (tmp_path / "work").mkdir(parents=True)
    sink = Sink(tmp_path)
    try:
        stats = asyncio.run(Extractor(data, sink, plan, NoLLM(), {}).execute())
        rows = [json.loads(row[0]) for row in sink.db.execute("SELECT body FROM items WHERE kind='assertions'")]
        return stats, {(row["subject"], row["object"]) for row in rows if "object" in row}
    finally:
        sink.close()


def witness(bundle, pair_index=0):
    pair = bundle["examples"]["positive"][pair_index]
    return pair["source_record_id"], pair["target_record_id"]


def test_two_conditions_share_type_and_preserve_each_model_explanation(checked_relations, tmp_path):
    data, core, bundles = checked_relations
    first = decision_for(bundles["API"], explanation="接口收入参数引用收入标准定义")
    second = decision_for(bundles["CARD"], explanation="卡片成本参数引用成本标准定义")
    a, a_plan = compile_relation(data, PROFILE, core, bundles["API"], first)
    b, b_plan = compile_relation(data, PROFILE, a, bundles["CARD"], second)
    assert len(b.relation_types) == 1 and len(b.relations) == 2
    assert a_plan.predicate == b_plan.predicate and a_plan.id != b_plan.id
    assert {item.selector.value for item in b.relations} == {"API", "CARD"}
    kind = b.relation_types[0]
    assert kind.definition == canonical_relation_definition("points_to", "definition_reference")
    assert set(a.relation_types[0].evidence_ids) <= set(kind.evidence_ids)
    explanations = [data.evidence[key] for key in kind.evidence_ids if key.startswith("relation_inference:")]
    assert {item["raw_fragment"] for item in explanations} == {first.definition, second.definition}
    assert {item["rule_binding"]["selector"]["kind"] for item in explanations} == {"API", "CARD"}
    assert all(item["basis_evidence_ids"] and item["witness_pair"]
               and item["rule_binding"]["snapshot_id"] == data.snapshot_id
               and item["rule_binding"]["scope_bindings"] == {"area": "market"}
               for item in explanations)
    stats, edges = execute(data, b, tmp_path / "two-branches")
    assert stats["accepted"] == 2
    assert edges == {witness(bundles["API"]), witness(bundles["CARD"])}
    assert len(core.relation_types) == len(core.relations) == 0


def test_same_plan_accumulates_only_distinct_reviewed_witnesses(checked_relations, tmp_path):
    data, core, bundles = checked_relations
    packet = bundles["API"]
    first, _ = compile_relation(data, PROFILE, core, packet, decision_for(packet))
    second, plan = compile_relation(data, PROFILE, first, packet, decision_for(packet, 1, "同类参数引用另一条定义"))
    third, repeated = compile_relation(data, PROFILE, second, packet, decision_for(packet, 1, "另一种说明措辞"))
    assert len(third.relation_types) == len(third.relations) == 1
    assert len(plan.witnessed_pairs) == len(repeated.witnessed_pairs) == 2
    assert len(first.relations[0].witnessed_pairs) == 1
    assert len([key for key in repeated.evidence_ids if key.startswith("relation_inference:")]) == 3
    stats, edges = execute(data, third, tmp_path / "merged")
    assert edges == {witness(packet), witness(packet, 1)}
    # One other API row and the CARD row are both outside this reviewed pair set.
    assert stats["accepted"] == 2 and stats["plans"][0]["outside_witness"] == 2


@pytest.mark.parametrize("change", [
    {"parent": "related_to"}, {"domain": ["another-type"]}, {"range": ["another-type"]},
    {"semantic_parameters": {"operand_role": "numerator"}},
    {"definition": "与固定契约含义不同的旧定义"}, {"endpoint_basis": "record_alignment"},
])
def test_conflicting_type_semantics_are_not_merged(change):
    existing = DerivedType(id="relation:x", parent="points_to", label="definition_reference",
        predicate_name="definition_reference", definition=canonical_relation_definition("points_to", "definition_reference"),
        evidence_ids=["old"], domain=["a"], range=["b"], endpoint_basis="table_binding")
    proposed = existing.model_copy(update={"evidence_ids": ["new"], **change})
    with pytest.raises(ValueError, match="Conflicting relation type"):
        merge_relation_type_evidence(existing, proposed)
    assert existing.evidence_ids == ["old"]


@pytest.mark.parametrize("field, value", [
    ("transform", {"operator": "nfkc_whitespace_casefold"}),
    ("scope_bindings", {}), ("witness_snapshot_id", "old-snapshot"),
    ("selector", None),
])
def test_same_plan_id_with_changed_execution_scope_is_rejected(checked_relations, field, value):
    data, core, bundles = checked_relations
    packet = bundles["API"]
    first, _ = compile_relation(data, PROFILE, core, packet, decision_for(packet))
    corrupted = first.model_copy(deep=True)
    setattr(corrupted.relations[0], field, value)
    before = corrupted.model_dump()
    with pytest.raises(ValueError, match="Conflicting relation plan ID"):
        compile_relation(data, PROFILE, corrupted, packet, decision_for(packet, 1))
    assert corrupted.model_dump() == before


def test_changed_technical_rule_check_cannot_share_old_plan(checked_relations):
    data, core, bundles = checked_relations
    packet = bundles["API"]
    first, _ = compile_relation(data, PROFILE, core, packet, decision_for(packet))
    changed = deepcopy(packet)
    changed["rule"]["verification"]["counterexamples"] = [{"reason": "changed_full_check"}]
    with pytest.raises(ValueError, match="Conflicting relation plan rule contract"):
        compile_relation(data, PROFILE, first, changed, decision_for(changed, 1))
