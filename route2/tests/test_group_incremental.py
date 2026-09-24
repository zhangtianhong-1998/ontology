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
from ontology_r2.incremental import direct_mapping
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
        status="proposed", parent_relation="contains", label="维度包含成员",
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


def test_related_dashboard_mention_cannot_create_a_measure_concept():
    data = SimpleNamespace(snapshot_id="snap", evidence={})
    record = _record("dashboard")
    record["table"] = "fruit.dashboard_card"
    with pytest.raises(ValueError, match="requires an exact source definition"):
        compile_concept(data, PROFILE, {"records": [record]},
                        _concept_decision(["dashboard"], kinds=["related"]), {})
    record["root_hint"] = "GeneralObject"
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
                            "scope_roles": {"region": self.scope_role}})
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
            status="proposed", parent_relation="points_to", label="引用指标",
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
        false_dependency = decision.model_copy(update={"parent_relation": "depends_on"})
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
            "parent_relation": "depends_on", "source_quote": "水果销售利润"})
        supported_plan, _ = compile_relation(data, PROFILE, core, bundle,
                                             supported_dependency)
        assert any("水果销售收入" in data.evidence[e]["raw_fragment"]
                   for e in supported_plan.relation_types[0].evidence_ids
                   if e.startswith("record:"))
    finally:
        data.close()
