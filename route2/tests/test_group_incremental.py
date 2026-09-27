"""Accepted group deltas need source quotes and must remain locally consistent."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ontology_r2.group_incremental import (
    BundleReview, ConceptBundleDecision, RecordAlignmentDecision,
    RelationBundleDecision, compile_business_relation, compile_concept, compile_relation,
    construct_from_bundles,
)
from ontology_r2.incremental import direct_mapping, ontology_from_plan
from ontology_r2.models import BuildPlan, DerivedType, TablePlan
from ontology_r2.relations import Extractor
from ontology_r2.storage import Dataset, Sink, digest, read_yaml
from ontology_r2.validation import validate_plan
from test_semantic_cards import _table


PROFILE = read_yaml(Path(__file__).resolve().parents[1] / "ontologies/internal_model.yaml")


def test_provider_json_string_scope_is_decoded_but_other_strings_are_rejected():
    accepted = ConceptBundleDecision.model_validate({
        "status": "unresolved", "scope": "{}", "scope_roles": "{}"})
    assert accepted.scope == {} and accepted.scope_roles == {}
    with pytest.raises(ValueError):
        ConceptBundleDecision.model_validate({"status": "unresolved", "scope": "[]"})


def _record(record_id, *, region="华东", unit="元", name="水果收入"):
    return {
        "record_id": record_id, "table": "fruit.metric", "row_number": 1,
        "scope": {"region": region}, "unit": unit,
        "fields": {
            "name": [{"column": "metric_name", "value": name}],
            "description": [{"column": "definition", "value": f"{region}{name}"}],
            "unit": [{"column": "unit", "value": unit}],
        },
    }


def _concept_decision(record_ids, *, scope=None, kinds=None, quotes=None):
    kinds = kinds or ["exact"] * len(record_ids)
    quotes = quotes or ["水果收入"] * len(record_ids)
    return ConceptBundleDecision(
        status="proposed", label="水果收入", definition="水果业务收入",
        root_type="Metric", scope=scope or {},
        alignments=[RecordAlignmentDecision(record_id=record_id,
                                            mapping_kind=kind, quote=quote)
                    for record_id, kind, quote in zip(record_ids, kinds, quotes)],
    )


def test_concept_rejects_unquoted_and_conflicting_exact_mappings():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    records = [_record("east"), _record("south", region="华南")]
    bundle = {"records": records}
    with pytest.raises(ValueError, match="Quote is absent"):
        compile_concept(data, PROFILE, bundle,
                        _concept_decision(["east"], quotes=["不存在的原文"]), {})
    with pytest.raises(ValueError, match="Conflicting scopes"):
        compile_concept(data, PROFILE, bundle,
                        _concept_decision(["east", "south"]), {})
    with pytest.raises(ValueError, match="Concept scope"):
        compile_concept(data, PROFILE, bundle,
                        _concept_decision(["south", "east"],
                                          scope={"region": "华东"},
                                          kinds=["exact", "related"]), {})
    other_unit = [_record("east"), _record("tonnes", unit="吨")]
    with pytest.raises(ValueError, match="Conflicting units"):
        compile_concept(data, PROFILE, {"records": other_unit},
                        _concept_decision(["east", "tonnes"]), {})


def test_exact_concept_rejects_truncated_definition():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    record = _record("long")
    record["fields"]["description"][0]["truncated"] = True
    with pytest.raises(ValueError, match="truncated semantic fields"):
        compile_concept(data, PROFILE, {"records": [record]},
                        _concept_decision(["long"]), {})


def test_pattern_packet_does_not_exactly_align_a_nonrepresentative_record():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    records = [_record("representative"), _record("same-text-different-code")]
    bundle = {"records": records,
              "exact_alignment_record_ids": ["representative"]}
    with pytest.raises(ValueError, match="representative record allowlist"):
        compile_concept(data, PROFILE, bundle,
                        _concept_decision(["same-text-different-code"]), {})
    concept, alignments = compile_concept(
        data, PROFILE, bundle,
        _concept_decision(["representative", "same-text-different-code"],
                          kinds=["exact", "related"]), {})
    assert concept["source_refs"][0]["record_id"] == "representative"
    assert [item["mapping_kind"] for item in alignments] == ["exact", "related"]


def test_concept_quote_cannot_be_only_an_identifier():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    record = _record("metric")
    record["fields"]["reference"] = [{"column": "metric_code", "value": "MET001"}]
    with pytest.raises(ValueError, match="Quote is absent"):
        compile_concept(data, PROFILE, {"records": [record]},
                        _concept_decision(["metric"], quotes=["MET001"]), {})


def test_contains_rejects_child_to_parent_relation_direction():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    bundle = {"snapshot_id": "snap", "rule": {
        "snapshot_id": "snap", "status": "checked_technical",
        "verification": {"scan_scope": "full_input"},
        "transform": {"operator": "identity"},
        "source": {"table": "fruit.dim_member", "field": "dim_code"},
        "target": {"table": "fruit.dim_definition", "field": "dim_code"},
    }}
    decision = RelationBundleDecision(
        status="proposed", parent_relation="contains", label="contains",
        definition="维度有成员", source_quote="苹果", target_quote="水果类别")
    with pytest.raises(ValueError, match="direction is reversed"):
        compile_relation(data, PROFILE, BuildPlan(), bundle, decision)


def test_dimension_member_is_not_exactly_the_dimension():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    member = _record("member")
    member["table"] = "fruit.dim_member"
    with pytest.raises(ValueError, match="Member or field record"):
        compile_concept(data, PROFILE, {"records": [member]},
                        _concept_decision(["member"]), {})


def test_related_record_cannot_create_concept_but_weak_root_hint_does_not_block():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    record = _record("dashboard")
    record["table"] = "fruit.dashboard_card"
    with pytest.raises(ValueError, match="requires an exact source definition"):
        compile_concept(data, PROFILE, {"records": [record]},
                        _concept_decision(["dashboard"], kinds=["related"]), {})
    record["root_hint"] = "GeneralObject"
    assert compile_concept(data, PROFILE, {"records": [record]},
                           _concept_decision(["dashboard"]), {})[0]["type"] == "Metric"
    record["root_hint_authoritative"] = True
    with pytest.raises(ValueError, match="root differs"):
        compile_concept(data, PROFILE, {"records": [record]},
                        _concept_decision(["dashboard"]), {})


class _ConceptLLM:
    def __init__(self, *, accept=True):
        self.accept = accept

    async def ask(self, task, payload, schema):
        if task == "concept_bundle":
            record_id = payload["bundle"]["records"][0]["record_id"]
            return _concept_decision([record_id])
        if task == "group_review":
            return BundleReview(accepted=self.accept,
                                errors=[] if self.accept else ["证据不足"])
        raise AssertionError(task)


class _StatusLLM:
    def __init__(self, status):
        self.status = status

    async def ask(self, task, payload, schema):
        assert task == "concept_bundle"
        return ConceptBundleDecision(status=self.status, reason="没有足够证据")


def test_group_repair_rechecks_one_invalid_model_alignment_without_inventing_it():
    class RepairLLM:
        def __init__(self):
            self.calls = 0

        async def ask(self, task, payload, schema):
            assert task == "concept_bundle"
            self.calls += 1
            if self.calls == 1:
                return _concept_decision(["other"], kinds=["related"])
            assert payload["compiler_error"] == (
                "New concept requires an exact source definition record")
            return _concept_decision(["seed"])

    llm = RepairLLM()
    bundle = {"bundle_id": "repair", "task_kind": "concept_induction",
              "records": [_record("seed"), _record("other")],
              "exact_alignment_record_ids": ["seed"]}
    result = asyncio.run(construct_from_bundles(
        SimpleNamespace(snapshot_id="snap", evidence={}), PROFILE, BuildPlan(),
        [bundle], llm, review=False, max_repairs_per_bundle=1))
    assert llm.calls == 2
    assert result["steps"][0]["status"] == "accepted"
    assert result["steps"][0]["repair_calls"] == 1
    assert [item["source_record_id"] for item in result["record_alignments"]] == ["seed"]


def test_incremental_reuses_concept_without_losing_second_source():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    bundles = [
        {"bundle_id": "b1", "task_kind": "concept_induction", "records": [_record("r1")]},
        {"bundle_id": "b2", "task_kind": "concept_induction", "records": [_record("r2")]},
    ]
    result = asyncio.run(construct_from_bundles(data, PROFILE, BuildPlan(), bundles,
                                                _ConceptLLM(), max_bundles=2))
    assert [step["status"] for step in result["steps"]] == ["accepted", "accepted"]
    assert len(result["concepts"]) == 1
    assert {ref["record_id"] for ref in result["concepts"][0]["source_refs"]} == {"r1", "r2"}
    assert {item["source_record_id"] for item in result["record_alignments"]} == {"r1", "r2"}
    rejected = asyncio.run(construct_from_bundles(
        SimpleNamespace(snapshot_id="snap", evidence={}), PROFILE, BuildPlan(),
        bundles[:1], _ConceptLLM(accept=False), max_bundles=1))
    assert rejected["concepts"] == [] and rejected["record_alignments"] == []
    assert rejected["steps"][0]["status"] == "unresolved"
    assert rejected["partial"] is True


def test_reviewed_definition_becomes_a_business_type_but_observation_does_not():
    class LevelLLM:
        def __init__(self, level, scope_role):
            self.level, self.scope_role = level, scope_role

        async def ask(self, task, payload, schema):
            if task == "concept_bundle":
                record_id = payload["bundle"]["records"][0]["record_id"]
                return _concept_decision([record_id], scope={"region": "华东"}).model_copy(
                    update={"ontology_level": self.level,
                            "scope_roles": {"region": self.scope_role},
                            "classification_basis": "business_driven_metric",
                            "classification_quote": "华东水果收入",
                            "business_object_quote": "水果"})
            if task == "group_review":
                return BundleReview(accepted=True)
            raise AssertionError(task)

    bundle = {"bundle_id": "definition", "task_kind": "concept_induction",
              "records": [{**_record("metric-def"), "kind": "definition",
                           "root_hint": "Metric"}]}
    typed = asyncio.run(construct_from_bundles(
        SimpleNamespace(snapshot_id="snap", evidence={}), PROFILE, BuildPlan(),
        [bundle], LevelLLM("type", "applicability")))
    assert typed["steps"][0]["status"] == "accepted"
    derived = typed["plan"].object_types[0]
    assert derived.parent == "Metric" and derived.category == "business_type"
    assert derived.evidence_scope == "definition_record"
    assert derived.applicability_scope == {"region": "华东"}
    assert typed["concepts"][0]["ontology_type_id"] == derived.id
    assert typed["steps"][0]["object_type_id"] == derived.id
    assert {source.role for source in derived.source_properties} == {
        "name", "description", "unit"}
    business_attributes = [item for item in ontology_from_plan(
        typed["plan"], PROFILE, SimpleNamespace(tables={}), {"tables": []}, [])
        ["attributes"] if item["domain"] == [derived.id]]
    assert {item["label"] for item in business_attributes} == {
        "name", "description", "unit"}
    assert all(item["mapping_status"] == "definition_record_only"
               and item["evidence_ids"] for item in business_attributes)
    observed = asyncio.run(construct_from_bundles(
        SimpleNamespace(snapshot_id="snap", evidence={}), PROFILE, BuildPlan(),
        [bundle], LevelLLM("instance", "observation")))
    assert observed["plan"].object_types == []
    assert observed["concepts"][0]["observation_coordinates"] == {"region": "华东"}
    assert observed["concepts"][0]["ontology_type_id"] is None
    unsupported = asyncio.run(construct_from_bundles(
        SimpleNamespace(snapshot_id="snap", evidence={}), PROFILE, BuildPlan(),
        [bundle], LevelLLM("type", "observation")))
    assert unsupported["steps"][0]["status"] == "unresolved"
    assert unsupported["plan"].object_types == []


def test_metric_measure_type_boundary_uses_definition_not_table_hint():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    record = {
        "record_id": "measure-def", "table": "fruit.metric", "row_number": 1,
        "kind": "definition", "root_hint": "Metric", "scope": {}, "unit": "元",
        "fields": {
            "name": [{"column": "measure_name", "value": "收入"}],
            "description": [{"column": "definition", "value": "收入是可用于不同经营对象的营业所得金额"}],
            "unit": [{"column": "unit", "value": "元"}],
        },
    }
    decision = ConceptBundleDecision(
        status="proposed", label="收入", definition="可复用的营业所得金额",
        root_type="Measure", ontology_level="type",
        classification_basis="reusable_measure",
        classification_quote="收入是可用于不同经营对象的营业所得金额",
        alignments=[RecordAlignmentDecision(record_id="measure-def",
                                            mapping_kind="exact", quote="收入")],
    )
    compiled = compile_concept(data, PROFILE, {"records": [record]}, decision, {})
    assert compiled[0]["type"] == "Measure"
    assert compiled[0]["ontology_type_id"]
    assert compiled[0]["aggregation_operator"] is None
    assert compiled[0]["unit"] == "元"
    with pytest.raises(ValueError, match="classification basis"):
        compile_concept(data, PROFILE, {"records": [record]},
                        decision.model_copy(update={"classification_basis": "business_driven_metric"}), {})
    with pytest.raises(ValueError, match="classification quote"):
        compile_concept(data, PROFILE, {"records": [record]},
                        decision.model_copy(update={"classification_quote": "收入"}), {})
    operator_record = {**record, "record_id": "sum", "unit": "", "fields": {
        "name": [{"column": "operation", "value": "SUM"}],
        "description": [{"column": "definition", "value": "SUM(x) 对输入数值求和"}],
    }}
    with pytest.raises(ValueError, match="operator alone"):
        compile_concept(data, PROFILE, {"records": [operator_record]},
                        decision.model_copy(update={
                            "label": "SUM", "classification_quote": "SUM(x) 对输入数值求和",
                            "aggregation_operator": "sum",
                            "alignments": [RecordAlignmentDecision(record_id="sum", mapping_kind="exact", quote="SUM")],
                        }), {})
    business_record = {**_record("business-revenue", name="水果销售收入"),
                       "kind": "definition", "root_hint": "Measure"}
    metric = decision.model_copy(update={
        "label": "水果销售收入", "root_type": "Metric",
        "classification_basis": "business_driven_metric",
        "classification_quote": "华东水果销售收入",
        "business_object_quote": "水果", "scope_roles": {"region": "applicability"},
        "alignments": [RecordAlignmentDecision(
            record_id="business-revenue", mapping_kind="exact", quote="水果销售收入")],
    })
    assert compile_concept(data, PROFILE, {"records": [business_record]}, metric, {})[0]["type"] == "Metric"
    with pytest.raises(ValueError, match="named business object"):
        compile_concept(data, PROFILE, {"records": [business_record]},
                        metric.model_copy(update={"root_type": "Measure",
                                                  "classification_basis": "reusable_measure"}), {})


def test_measure_operator_is_an_optional_evidenced_property_not_type_name():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    record = {
        "record_id": "income", "table": "fruit.measure", "kind": "definition",
        "row_number": 1, "root_hint": "Measure", "scope": {}, "unit": "元",
        "fields": {
            "name": [{"column": "name", "value": "收入"}],
            "description": [{"column": "definition", "value": "收入按 SUM(x) 汇总各期间金额"}],
            "unit": [{"column": "unit", "value": "元"}],
        },
    }
    decision = ConceptBundleDecision(
        status="proposed", label="收入", definition="可复用的期间收入金额",
        root_type="Measure", ontology_level="type",
        classification_basis="reusable_measure",
        classification_quote="收入按 SUM(x) 汇总各期间金额",
        aggregation_operator="sum",
        alignments=[RecordAlignmentDecision(record_id="income", mapping_kind="exact", quote="收入")],
    )
    concept, _ = compile_concept(data, PROFILE, {"records": [record]}, decision, {})
    assert concept["aggregation_operator"] == "sum" and concept["unit"] == "元"
    with pytest.raises(ValueError, match="operator lacks exact source evidence"):
        compile_concept(data, PROFILE, {"records": [record]},
                        decision.model_copy(update={"aggregation_operator": "avg"}), {})
    distinct_record = {**record, "fields": {
        **record["fields"], "description": [
            {"column": "definition", "value": "收入按 COUNT(DISTINCT x) 去重计数"}]}}
    with pytest.raises(ValueError, match="operator lacks exact source evidence"):
        compile_concept(data, PROFILE, {"records": [distinct_record]},
                        decision.model_copy(update={
                            "classification_quote": "收入按 COUNT(DISTINCT x) 去重计数",
                            "aggregation_operator": "count"}), {})
    assert compile_concept(data, PROFILE, {"records": [distinct_record]},
                           decision.model_copy(update={
                               "classification_quote": "收入按 COUNT(DISTINCT x) 去重计数",
                               "aggregation_operator": "distinct_count"}), {})[0]["aggregation_operator"] == "distinct_count"


def test_measure_definition_may_have_time_applicability_but_not_observation_identity():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    record = {
        "record_id": "budget", "table": "fruit.measure", "kind": "definition",
        "row_number": 1, "root_hint": "Measure", "scope": {"month": "10"}, "unit": "元",
        "fields": {
            "name": [{"column": "name", "value": "10月年预算"}],
            "description": [{"column": "definition", "value": "10月年预算适用于不同经营对象的预算金额"}],
            "unit": [{"column": "unit", "value": "元"}],
            "scope": [{"column": "month", "value": "10"}],
        },
    }
    decision = ConceptBundleDecision(
        status="proposed", label="10月年预算", definition="10月年度预算金额",
        root_type="Measure", ontology_level="type",
        classification_basis="reusable_measure",
        classification_quote="10月年预算适用于不同经营对象的预算金额",
        scope_roles={"month": "applicability"},
        alignments=[RecordAlignmentDecision(record_id="budget", mapping_kind="exact", quote="10月年预算")],
    )
    concept, _ = compile_concept(data, PROFILE, {"records": [record]}, decision, {})
    assert concept["type"] == "Measure"
    assert concept["applicability_scope"] == {"month": "10"}
    with pytest.raises(ValueError, match="Observation coordinates"):
        compile_concept(data, PROFILE, {"records": [record]},
                        decision.model_copy(update={"scope_roles": {"month": "observation"}}), {})
    rank = {**record, "record_id": "rank", "scope": {}, "unit": "", "fields": {
        "name": [{"column": "name", "value": "当年预算排名"}],
        "description": [{"column": "definition", "value": "当年预算排名按预算值降序给出序位"}],
    }}
    rank_decision = decision.model_copy(update={
        "label": "当年预算排名", "definition": "通用预算序位",
        "classification_quote": "当年预算排名按预算值降序给出序位",
        "scope_roles": {},
        "alignments": [RecordAlignmentDecision(record_id="rank", mapping_kind="exact", quote="当年预算排名")],
    })
    assert compile_concept(data, PROFILE, {"records": [rank]}, rank_decision, {})[0]["unit"] is None


def test_same_name_measure_merges_equal_definitions_and_separates_distinct_meanings():
    class MeasureLLM:
        async def ask(self, task, payload, schema):
            record = payload["bundle"]["records"][0]
            return ConceptBundleDecision(
                status="proposed", label="收入", definition=record["fields"]["description"][0]["value"],
                root_type="Measure", ontology_level="type",
                classification_basis="reusable_measure",
                classification_quote=record["fields"]["description"][0]["value"],
                alignments=[RecordAlignmentDecision(record_id=record["record_id"],
                                                    mapping_kind="exact", quote="收入")],
            )

    def packet(index, meaning):
        record = {"record_id": f"r{index}", "table": f"fruit.measure_{index}",
                  "kind": "definition", "root_hint": "Measure", "row_number": 1,
                  "scope": {}, "unit": "元", "fields": {
                      "name": [{"column": "name", "value": "收入"}],
                      "description": [{"column": "definition", "value": meaning}],
                      "unit": [{"column": "unit", "value": "元"}],
                  }}
        return {"bundle_id": f"b{index}", "task_kind": "concept_induction",
                "records": [record], "exact_alignment_record_ids": [record["record_id"]]}

    same = asyncio.run(construct_from_bundles(
        SimpleNamespace(snapshot_id="snap", evidence={}), PROFILE, BuildPlan(),
        [packet(1, "收入为营业所得金额"), packet(2, "收入为营业所得金额")],
        MeasureLLM(), review=False))
    assert [step["status"] for step in same["steps"]] == ["accepted", "accepted"]
    assert len([item for item in same["plan"].object_types if item.parent == "Measure"]) == 1
    assert len(same["concepts"][0]["source_refs"]) == 2
    different = asyncio.run(construct_from_bundles(
        SimpleNamespace(snapshot_id="snap", evidence={}), PROFILE, BuildPlan(),
        [packet(1, "收入为含税营业所得金额"), packet(2, "收入为不含税营业所得金额")],
        MeasureLLM(), review=False))
    assert [step["status"] for step in different["steps"]] == ["accepted", "accepted"]
    assert len([item for item in different["plan"].object_types if item.parent == "Measure"]) == 2


def test_no_change_is_complete_but_unresolved_remains_partial():
    bundle = {"bundle_id": "b1", "task_kind": "concept_induction",
              "records": [_record("r1")]}
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    no_change = asyncio.run(construct_from_bundles(
        data, PROFILE, BuildPlan(), [bundle], _StatusLLM("no_change"), review=False))
    unresolved = asyncio.run(construct_from_bundles(
        data, PROFILE, BuildPlan(), [bundle], _StatusLLM("unresolved"), review=False))
    assert no_change["partial"] is False
    assert no_change["coverage"]["statuses"]["no_change"] == 1
    assert unresolved["partial"] is True
    assert unresolved["coverage"]["statuses"]["unresolved"] == 1


def test_group_budget_reserves_relation_and_concept_calls():
    class NeutralLLM:
        async def ask(self, task, payload, schema):
            if task == "concept_bundle":
                return ConceptBundleDecision(status="no_change")
            if task == "relation_bundle":
                return RelationBundleDecision(status="no_change")
            raise AssertionError(task)

    bundles = [{"bundle_id": f"concept-{index}", "task_kind": "concept_induction"}
               for index in range(3)] + [
                   {"bundle_id": f"relation-{index}", "task_kind": "relation_meaning"}
                   for index in range(3)]
    result = asyncio.run(construct_from_bundles(
        SimpleNamespace(snapshot_id="snap", evidence={}), PROFILE, BuildPlan(),
        bundles, NeutralLLM(), max_bundles=2, review=False))
    assert [step["task_kind"] for step in result["steps"]] == [
        "concept_induction", "relation_meaning"]
    assert result["bundles_skipped"] == 4


def test_group_context_excludes_unrelated_physical_record_types():
    class CaptureLLM:
        async def ask(self, task, payload, schema):
            self.payload = payload
            return ConceptBundleDecision(status="no_change")

    physical = [DerivedType(id=f"source_record_type:t{index}", parent="GeneralObject",
                            definition="source record", evidence_ids=["schema"],
                            category="source_record_type") for index in range(20)]
    business = DerivedType(id="type:revenue", parent="Metric", definition="Revenue metric",
                           evidence_ids=["source"], category="business_type")
    core = BuildPlan(object_types=[*physical, business], tables=[
        TablePlan(table=f"t{index}", object_type=item.id, evidence_ids=["schema"])
        for index, item in enumerate(physical)])
    llm = CaptureLLM()
    asyncio.run(construct_from_bundles(
        SimpleNamespace(snapshot_id="snap", evidence={}), PROFILE, core,
        [{"bundle_id": "b1", "task_kind": "concept_induction",
          "records": [{**_record("r1"), "table": "t3"}]}],
        llm, review=False))
    assert {item["id"] for item in llm.payload["current_types"]} == {
        "type:revenue", "source_record_type:t3"}


def test_same_label_with_different_exact_unit_or_scope_keeps_distinct_identity():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    east_yuan = compile_concept(data, PROFILE, {"records": [_record("r1")]},
                                _concept_decision(["r1"]), {})[0]
    east_tonne = compile_concept(data, PROFILE, {"records": [_record("r2", unit="吨")]},
                                 _concept_decision(["r2"]), {})[0]
    south_yuan = compile_concept(data, PROFILE,
                                 {"records": [_record("r3", region="华南")]},
                                 _concept_decision(["r3"]), {})[0]
    assert len({east_yuan["id"], east_tonne["id"], south_yuan["id"]}) == 3


def _relation_dataset(tmp_path):
    root = tmp_path / "input"
    _table(root, "source", {"id": "记录 ID", "metric_ref": "指标引用编码",
                            "param_name": "参数名称"}, [
        {"id": "s1", "metric_ref": "K1", "param_name": "水果销售收入"},
        {"id": "s2", "metric_ref": "K2", "param_name": "香蕉进口量"},
    ])
    _table(root, "target", {"id": "记录 ID", "metric_code": "指标编码",
                            "metric_name": "指标名称"}, [
        {"id": "t1", "metric_code": "K1", "metric_name": "水果销售收入"},
        {"id": "t2", "metric_code": "K2", "metric_name": "香蕉进口量"},
    ])
    work = tmp_path / "work"
    work.mkdir()
    return Dataset(root, work)


def _relation_record(data, table, row, key, name):
    return {
        "record_id": data.record_id(table, row), "table": table,
        "row_number": row["__r2_row"],
        "fields": {"reference": [{"column": key, "value": row[key]}],
                   "name": [{"column": name, "value": row[name]}]},
    }


def test_relation_quotes_must_come_from_one_validated_positive_pair(tmp_path):
    data = _relation_dataset(tmp_path)
    try:
        source, target = "fruit.source", "fruit.target"
        s1, s2 = list(data.rows(source))
        t1 = next(data.rows(target))
        r1 = _relation_record(data, source, s1, "metric_ref", "param_name")
        r2 = _relation_record(data, source, s2, "metric_ref", "param_name")
        r3 = _relation_record(data, target, t1, "metric_code", "metric_name")
        rule = {
            "rule_id": "rule-1", "candidate_id": "pair-1",
            "snapshot_id": data.snapshot_id,
            "source": {"table": source, "field": "metric_ref"},
            "target": {"table": target, "field": "metric_code"},
            "status": "checked_technical", "transform": {"operator": "identity"},
            "selector": {}, "scope_bindings": {},
            "verification": {"scan_scope": "full_input",
                             "checks": {"eligible_references": 2, "unique_matches": 2}},
        }
        bundle = {
            "snapshot_id": data.snapshot_id, "rule": rule,
            # The first source record is deliberately *not* the validated pair.
            "records": [r2, r1, r3],
            "examples": {"positive": [{"source_record_id": r1["record_id"],
                                       "target_record_id": r3["record_id"],
                                       "matching_raw_value": "K1"}]},
        }
        decision = RelationBundleDecision(
            status="proposed", parent_relation="points_to", label="points_to",
            definition="参数指向指标定义", source_quote="水果销售收入",
            target_quote="水果销售收入")
        core, _ = direct_mapping(data)
        compiled, plan = compile_relation(data, PROFILE, core, bundle, decision)
        assert len(compiled.relations) == 1
        assert plan.witness_snapshot_id == data.snapshot_id
        assert [(item.source_record_id, item.target_record_id)
                for item in plan.witnessed_pairs] == [(r1["record_id"], r3["record_id"])]
        output = tmp_path / "witnessed-output"
        (output / "work").mkdir(parents=True)
        sink = Sink(output)
        try:
            for item in data.evidence.values():
                sink.put("evidence", item)
            stats = asyncio.run(Extractor(
                data, sink, compiled, _ConceptLLM(),
                {"max_relation_records": 10}).execute())
            assert stats["accepted"] == 1
            assert stats["plans"][0]["outside_witness"] == 1
            edge = sink.db.execute(
                "SELECT json_extract(body,'$.subject'), json_extract(body,'$.object') "
                "FROM items WHERE kind='assertions' AND json_extract(body,'$.object') IS NOT NULL"
            ).fetchall()
            assert edge == [(r1["record_id"], r3["record_id"])]
        finally:
            sink.close()
        # A witnessed source row alone is insufficient: the target must be the
        # very record reviewed as the positive semantic example.
        wrong_target = compiled.model_copy(deep=True)
        wrong_target.relations[0].witnessed_pairs[0].target_record_id = data.record_id(
            target, list(data.rows(target))[1])
        mismatch_output = tmp_path / "mismatch-output"
        (mismatch_output / "work").mkdir(parents=True)
        mismatch_sink = Sink(mismatch_output)
        try:
            mismatch_stats = asyncio.run(Extractor(
                data, mismatch_sink, wrong_target, _ConceptLLM(),
                {"max_relation_records": 10}).execute())
            assert mismatch_stats["accepted"] == 0
            assert mismatch_sink.db.execute(
                "SELECT count(*) FROM items WHERE kind='assertions'"
            ).fetchone()[0] == 0
            assert mismatch_sink.db.execute(
                "SELECT count(*) FROM items WHERE kind='unresolved' "
                "AND json_extract(body,'$.reason')='witness_target_mismatch'"
            ).fetchone()[0] == 1
        finally:
            mismatch_sink.close()
        bad_counts = {**bundle, "rule": {**rule, "verification": {
            "scan_scope": "full_input",
            "checks": {"eligible_references": 2, "unique_matches": 1,
                       "missing_in_input": 1}}}}
        with pytest.raises(ValueError, match="incomplete or inconsistent"):
            compile_relation(data, PROFILE, core, bad_counts, decision)
        relation = compiled.relation_types[0]
        assert relation.domain == [next(item.object_type for item in core.tables
                                        if item.table == source)]
        assert relation.range == [next(item.object_type for item in core.tables
                                       if item.table == target)]
        assert relation.endpoint_basis == "table_binding"
        assert relation.evidence_scope == plan.evidence_scope == (
            "sample_semantic_with_full_technical_check")
        typed = compiled.model_copy(deep=True)
        typed.object_types.extend([
            DerivedType(id="type:profit", parent="Metric", definition="利润指标",
                        category="business_type", evidence_ids=[f"schema:{source}"]),
            DerivedType(id="type:revenue", parent="Measure", definition="收入度量",
                        category="business_type", evidence_ids=[f"schema:{target}"]),
        ])
        concepts = [{"id": "concept:profit", "ontology_level": "type",
                     "ontology_type_id": "type:profit", "applicability_scope": {}},
                    {"id": "concept:revenue", "ontology_level": "type",
                     "ontology_type_id": "type:revenue", "applicability_scope": {}}]
        only_one_exact = [{"source_record_id": r1["record_id"],
                           "concept_id": "concept:profit", "mapping_kind": "exact",
                           "evidence_ids": [plan.evidence_ids[2]]}]
        lifted, assertion, reason = compile_business_relation(
            data, PROFILE, typed, bundle, decision, plan, concepts, only_one_exact)
        assert lifted is assertion is None
        assert reason == "both_positive_records_require_exact_type_alignment"
        mistyped = compiled.model_copy(deep=True)
        mistyped.relation_types[0].domain = ["Metric"]
        assert any("violates domain" in error
                   for error in validate_plan(mistyped, data, PROFILE))
        mislabeled = compiled.model_copy(deep=True)
        mislabeled.relation_types[0].evidence_scope = None
        assert any("evidence scope differs" in error
                   for error in validate_plan(mislabeled, data, PROFILE))
        assert "record:" + digest(
            [data.snapshot_id, r1["record_id"], "param_name"])[:24] in plan.evidence_ids
        invalid = {**bundle, "examples": {"positive": [{"source_record_id": r2["record_id"],
                                                        "target_record_id": r3["record_id"],
                                                        "matching_raw_value": "K1"}]}}
        with pytest.raises(ValueError, match="positive pair"):
            compile_relation(data, PROFILE, core, invalid, decision)
        t2 = list(data.rows(target))[1]
        r4 = _relation_record(data, target, t2, "metric_code", "metric_name")
        both = {**bundle, "records": [r1, r2, r3, r4],
                "examples": {"positive": [
                    {"source_record_id": r1["record_id"], "target_record_id": r3["record_id"],
                     "matching_raw_value": "K1"},
                    {"source_record_id": r2["record_id"], "target_record_id": r4["record_id"],
                     "matching_raw_value": "K2"}]}}
        second = decision.model_copy(update={"source_quote": "香蕉进口量",
                                             "target_quote": "香蕉进口量"})
        _, second_plan = compile_relation(data, PROFILE, core, both, second)
        assert "record:" + digest(
            [data.snapshot_id, r2["record_id"], "param_name"])[:24] in second_plan.evidence_ids
        r1["fields"]["reference"][0]["column_comment"] = "指标引用编码"
        r3["fields"]["reference"][0]["column_comment"] = "指标编码"
        schema_quotes = decision.model_copy(update={"source_quote": "指标引用编码",
                                                     "target_quote": "指标编码"})
        with pytest.raises(ValueError, match="positive pair"):
            compile_relation(data, PROFILE, core, bundle, schema_quotes)
        key_values = decision.model_copy(update={"source_quote": "K1",
                                                 "target_quote": "K1"})
        with pytest.raises(ValueError, match="positive pair"):
            compile_relation(data, PROFILE, core, bundle, key_values)
        false_dependency = decision.model_copy(update={"parent_relation": "depends_on",
                                                "label": "depends_on"})
        with pytest.raises(ValueError, match="source formula"):
            compile_relation(data, PROFILE, core, bundle, false_dependency)
        # The model no longer chooses executable endpoint types at all.
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            RelationBundleDecision.model_validate({**decision.model_dump(),
                                                   "source_type": "Metric"})
        r1["fields"]["name"][0]["value"] = "水果销售利润"
        r1["fields"]["formula"] = [{"column": "formula", "value":
                                     "水果销售利润 = 水果销售收入 - 水果销售成本"}]
        supported_dependency = decision.model_copy(update={
            "parent_relation": "depends_on", "label": "depends_on",
            "source_quote": "水果销售利润"})
        supported_plan, _ = compile_relation(data, PROFILE, core, bundle,
                                             supported_dependency)
        assert any("水果销售收入" in data.evidence[e]["raw_fragment"]
                   for e in supported_plan.relation_types[0].evidence_ids
                   if e.startswith("record:"))
    finally:
        data.close()


def test_formula_identity_and_source_definition_cannot_be_overwritten_by_model():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    first, second = _record("r1"), _record("r2")
    for record, expression in ((first, "收入 - 成本"), (second, "收入 + 成本")):
        record["fields"]["formula"] = [{"column": "formula", "value": expression}]
    generated = _concept_decision(["r1"]).model_copy(update={
        "definition": "模型无依据地声称此指标符合外部会计法规"})
    a, _ = compile_concept(data, PROFILE, {"records": [first]}, generated, {})
    b, _ = compile_concept(data, PROFILE, {"records": [second]},
                           _concept_decision(["r2"]), {})
    assert a["id"] != b["id"]
    assert a["definition"] == "华东水果收入"
    assert a["proposed_definition"] == generated.definition
    assert a["source_formulas"] == ["收入 - 成本"]
    with pytest.raises(ValueError, match="Conflicting source formulas"):
        compile_concept(data, PROFILE, {"records": [first, second]},
                        _concept_decision(["r1", "r2"]), {})


def test_metric_business_object_can_be_in_definition_without_repeating_in_label():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    record = _record("profit", name="利润")
    record.update(kind="definition", root_hint="GeneralObject")
    record["fields"]["description"][0]["value"] = "水果经营利润按收入减成本计算"
    decision = _concept_decision(["profit"], quotes=["利润"]).model_copy(update={
        "label": "利润", "ontology_level": "type",
        "classification_basis": "business_driven_metric",
        "classification_quote": "水果经营利润按收入减成本计算",
        "business_object_quote": "水果", "scope_roles": {"region": "applicability"}})
    result, _ = compile_concept(data, PROFILE, {"records": [record]}, decision, {})
    assert result["type"] == "Metric" and result["ontology_type_id"]
    with pytest.raises(ValueError, match="source-quoted business object"):
        compile_concept(data, PROFILE, {"records": [record]},
                        decision.model_copy(update={"business_object_quote": "互联网"}), {})


def test_incremental_checkpoint_skips_accepted_bundles_and_accumulates_sources():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    first = {"bundle_id": "a", "task_kind": "concept_induction", "records": [_record("a")]}
    second = {"bundle_id": "b", "task_kind": "concept_induction", "records": [_record("b")]}
    checkpoints = []
    prior = asyncio.run(construct_from_bundles(
        data, PROFILE, BuildPlan(), [first], _ConceptLLM(), review=False,
        on_checkpoint=lambda state: checkpoints.append(len(state["steps"]))))
    assert checkpoints == [1, 1]
    class NewOnly(_ConceptLLM):
        async def ask(self, task, payload, schema):
            assert payload["bundle"]["bundle_id"] == "b"
            return await super().ask(task, payload, schema)
    resumed = asyncio.run(construct_from_bundles(
        data, PROFILE, prior["plan"], [first, second], NewOnly(), review=False,
        prior_result=prior))
    assert resumed["coverage"]["bundles_reused"] == 1
    assert resumed["coverage"]["steps_this_run"] == 1
    assert len(resumed["concepts"]) == 1 and len(resumed["record_alignments"]) == 2
    assert {item["record_id"] for item in resumed["concepts"][0]["source_refs"]} == {"a", "b"}
    with pytest.raises(ValueError, match="snapshot differs"):
        asyncio.run(construct_from_bundles(
            SimpleNamespace(snapshot_id="changed", evidence={}), PROFILE, prior["plan"],
            [first], NewOnly(), prior_result=prior))


def test_type_context_recalls_matching_old_type_instead_of_only_recent_types():
    from ontology_r2.group_incremental import _type_context
    types = [DerivedType(id="old-income", parent="Measure", definition="收入汇总金额",
                         label="收入", category="business_type", evidence_ids=[])]
    types.extend(DerivedType(id=f"new-{i}", parent="GeneralObject", definition=f"其它业务对象{i}",
                             label=f"其它对象{i}", category="business_type", evidence_ids=[])
                 for i in range(20))
    selected = _type_context(BuildPlan(object_types=types), {"records": [_record("r", name="收入")]})
    assert len(selected) == 12
    assert selected[0]["id"] == "old-income"


class _BatchConceptLLM:
    def __init__(self, *, corrupt_first=False, fail_batch_review=False):
        self.calls = []
        self.corrupt_first = corrupt_first
        self.fail_batch_review = fail_batch_review
        self.config = {"max_input_bytes": 100000}

    async def ask(self, task, payload, schema):
        self.calls.append(task)
        if task == "concept_batch":
            decisions = []
            for i, packet in enumerate(payload["packets"]):
                record_id = packet["bundle"]["records"][0]["record_id"]
                if self.corrupt_first and i == 0:
                    record_id = payload["packets"][1]["bundle"]["records"][0]["record_id"]
                decisions.append({"bundle_id": packet["bundle"]["bundle_id"],
                                  "decision": _concept_decision([record_id]).model_dump()})
            return schema.model_validate({"decisions": decisions})
        if task == "group_review_batch":
            if self.fail_batch_review:
                from ontology_r2.llm import BudgetExceeded
                raise BudgetExceeded("budget")
            return schema.model_validate({"reviews": [
                {"bundle_id": packet["bundle"]["bundle_id"], "review": {"accepted": True}}
                for packet in payload["packets"]]})
        if task == "concept_bundle":
            return _concept_decision([payload["bundle"]["records"][0]["record_id"]])
        if task == "group_review":
            return BundleReview(accepted=True)
        raise AssertionError(task)


def _batch_packets(size):
    return [{"bundle_id": f"b{i}", "task_kind": "concept_induction",
             "records": [_record(f"r{i}")], "exact_alignment_record_ids": [f"r{i}"]}
            for i in range(size)]


def test_batching_reduces_requests_without_dropping_independent_decisions():
    llm = _BatchConceptLLM()
    result = asyncio.run(construct_from_bundles(
        SimpleNamespace(snapshot_id="snap", evidence={}), PROFILE, BuildPlan(),
        _batch_packets(6), llm, concept_batch_size=3))
    assert llm.calls == ["concept_batch", "group_review_batch"] * 2
    assert result["coverage"]["statuses"]["accepted"] == 6
    assert len(result["record_alignments"]) == 6
    assert not result["partial"]
    assert all(step["proposal_batch_size"] == 3 for step in result["steps"])


def test_batch_cross_packet_alignment_is_repaired_only_with_its_own_source():
    llm = _BatchConceptLLM(corrupt_first=True)
    result = asyncio.run(construct_from_bundles(
        SimpleNamespace(snapshot_id="snap", evidence={}), PROFILE, BuildPlan(),
        _batch_packets(3), llm, concept_batch_size=3, max_repairs_per_bundle=1))
    assert llm.calls == ["concept_batch", "group_review_batch", "concept_bundle", "group_review"]
    assert result["steps"][0]["repair_calls"] == 1
    assert len(result["record_alignments"]) == 3
    assert not result["partial"]


def test_resume_preserves_paid_batch_decisions_when_review_budget_stops_run():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    packets = _batch_packets(3)
    failed_llm = _BatchConceptLLM(fail_batch_review=True)
    prior = asyncio.run(construct_from_bundles(
        data, PROFILE, BuildPlan(), packets, failed_llm, concept_batch_size=3))
    assert len(prior["pending_concept_decisions"]) == 3
    assert prior["coverage"]["statuses"]["budget_exhausted"] == 1
    resumed_llm = _BatchConceptLLM()
    result = asyncio.run(construct_from_bundles(
        data, PROFILE, prior["plan"], packets, resumed_llm,
        concept_batch_size=3, prior_result=prior))
    assert resumed_llm.calls == ["group_review"] * 3
    assert result["coverage"]["statuses"]["accepted"] == 3
    assert result["coverage"]["statuses"]["budget_exhausted"] == 0
    assert not result["partial"] and result["pending_concept_decisions"] == {}


def test_relation_batch_keeps_per_rule_compilation_and_reviews(tmp_path):
    data = _relation_dataset(tmp_path)
    try:
        source, target = "fruit.source", "fruit.target"
        left = _relation_record(data, source, next(data.rows(source)), "metric_ref", "param_name")
        right = _relation_record(data, target, next(data.rows(target)), "metric_code", "metric_name")
        packets = []
        for i in range(2):
            packets.append({"bundle_id": f"rel{i}", "task_kind": "relation_meaning",
                "snapshot_id": data.snapshot_id, "rule": {
                    "rule_id": f"rule-{i}", "snapshot_id": data.snapshot_id,
                    "source": {"table": source, "field": "metric_ref"},
                    "target": {"table": target, "field": "metric_code"},
                    "status": "checked_technical", "transform": {"operator": "identity"},
                    "verification": {"scan_scope": "full_input", "checks": {
                        "eligible_references": 2, "unique_matches": 2}}},
                "records": [left, right], "examples": {"positive": [{
                    "source_record_id": left["record_id"], "target_record_id": right["record_id"],
                    "matching_raw_value": "K1"}]}})
        class RelationBatchLLM:
            def __init__(self):
                self.calls = []
            async def ask(self, task, payload, schema):
                self.calls.append(task)
                if task == "relation_batch":
                    return schema.model_validate({"decisions": [{
                        "bundle_id": packet["bundle"]["bundle_id"], "decision": {
                            "status": "proposed", "parent_relation": "points_to", "label": "points_to",
                            "definition": "参数引用指标定义", "source_quote": "水果销售收入",
                            "target_quote": "水果销售收入"}}
                        for packet in payload["packets"]]})
                assert task == "group_review_batch"
                return schema.model_validate({"reviews": [{"bundle_id": packet["bundle"]["bundle_id"],
                                                          "review": {"accepted": True}}
                                                         for packet in payload["packets"]]})
        core, _ = direct_mapping(data)
        llm = RelationBatchLLM()
        result = asyncio.run(construct_from_bundles(
            data, PROFILE, core, packets, llm, relation_batch_size=3))
        assert llm.calls == ["relation_batch", "group_review_batch"]
        assert result["coverage"]["statuses"]["accepted"] == 2
        assert len(result["plan"].relations) == 2
        assert all(step["proposal_batch_size"] == 2 for step in result["steps"])
        assert result["pending_relation_decisions"] == {}
    finally:
        data.close()


def test_related_context_does_not_supply_exact_identity_or_seed_formula():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    seed = _record("seed")
    context = _record("context")
    context["context_role"] = "related_context"
    context["fields"]["formula"] = [{"column": "formula", "value": "收入 - 成本"}]
    bundle = {"records": [seed, context], "exact_alignment_record_ids": ["seed", "context"]}
    # The explicit technical-context role remains binding even with a bad allowlist.
    with pytest.raises(ValueError, match="related_context cannot be an exact"):
        compile_concept(data, PROFILE, bundle, _concept_decision(["context"]), {})
    decision = _concept_decision(["seed"]).model_copy(update={"definition": "收入 - 成本"})
    concept, _ = compile_concept(data, PROFILE, bundle, decision, {})
    assert concept["definition"] == "华东水果收入"
    assert concept["source_formulas"] == []
    typed = decision.model_copy(update={"ontology_level": "type", "classification_basis": "business_driven_metric",
                                       "classification_quote": "收入 - 成本", "business_object_quote": "水果"})
    with pytest.raises(ValueError, match="classification quote"):
        compile_concept(data, PROFILE, bundle, typed, {})
