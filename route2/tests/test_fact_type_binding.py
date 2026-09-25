import asyncio
from copy import deepcopy
from types import SimpleNamespace

from ontology_r2.fact_observations import build_fact_observation_candidates
from ontology_r2.fact_type_binding import (
    _source_definition_evidence, bind_fact_observations,
)
from ontology_r2.models import BuildPlan, DerivedType, SourceProperty
from ontology_r2.pipeline import (add_fact_value_attributes, check_output,
                                  put_fact_instances)
from ontology_r2.storage import Dataset, Sink, digest
from test_semantic_cards import _table


def _fixture(tmp_path, *, comment="经营利润金额（元）", conflicting=False,
             duplicate_type=False, second_definition="经营利润等于收入减成本",
             hidden_coordinate=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "input"
    values = [
        {"fact_id": "1", "fruit_code": "APPLE", "region_code": "EAST",
         "period": "2025Q1", "profit_amount": "100"},
        {"fact_id": "2", "fruit_code": "APPLE", "region_code": "EAST",
         "period": "2025Q1", "profit_amount": "100"},
        {"fact_id": "3", "fruit_code": "BANANA", "region_code": "SOUTH",
         "period": "2025Q2", "profit_amount": "120"},
    ]
    if conflicting:
        values.append({"fact_id": "4", "fruit_code": "APPLE",
                       "region_code": "EAST", "period": "2025Q1",
                       "profit_amount": "110"})
    if hidden_coordinate:
        for index, value in enumerate(values):
            value["shipment_mode"] = "SEA" if index == 0 else "AIR"
    _table(root, "fruit_profit_fact", {
        "fact_id": "记录 ID", "fruit_code": "水果编码",
        "region_code": "地区编码", "period": "会计期",
        "profit_amount": comment,
        **({"shipment_mode": "运输方式"} if hidden_coordinate else {}),
    }, values, pk="fact_id")
    _table(root, "fruit_metric_definition", {
        "id": "记录 ID", "metric_name": "指标名称",
        "definition": "指标定义", "unit": "计量单位",
    }, [{"id": "1", "metric_name": "经营利润",
         "definition": "经营利润等于收入减成本", "unit": "元"},
        *([{"id": "2", "metric_name": "经营利润",
            "definition": second_definition, "unit": "元"}]
          if duplicate_type else [])])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    source_table = "fruit.fruit_metric_definition"
    row = next(data.rows(source_table))
    record_id = data.record_id(source_table, row)
    evidence_id = "record:" + digest([data.snapshot_id, record_id, "definition"])[:24]
    data.evidence[evidence_id] = {
        "id": evidence_id, "origin": "observed_record",
        "raw_fragment": row["definition"], "raw_fragment_truncated": False,
        "source_ref": {"table": source_table, "record_id": record_id,
                       "row": row["__r2_row"], "column": "definition",
                       "snapshot_id": data.snapshot_id},
    }
    core = BuildPlan(object_types=[DerivedType(
        id="type:operating_profit", parent="Metric", label="经营利润",
        definition="水果经营利润，收入减成本", category="business_type",
        evidence_ids=[evidence_id], evidence_scope="definition_record",
        derivation_kind="exact_definition", unit="元",
        source_properties=[SourceProperty(
            role="description", source_table=source_table,
            source_column="definition", evidence_ids=[evidence_id])],
    )])
    if duplicate_type:
        second = next(row for row in data.rows(source_table) if row["id"] == "2")
        second_id = data.record_id(source_table, second)
        second_evidence_id = "record:" + digest([
            data.snapshot_id, second_id, "definition"])[:24]
        data.evidence[second_evidence_id] = {
            "id": second_evidence_id, "origin": "observed_record",
            "raw_fragment": second["definition"],
            "raw_fragment_truncated": False,
            "source_ref": {"table": source_table, "record_id": second_id,
                           "row": second["__r2_row"], "column": "definition",
                           "snapshot_id": data.snapshot_id},
        }
        duplicate = core.object_types[0].model_copy(deep=True)
        duplicate.id = "type:operating_profit_2"
        duplicate.evidence_ids = [second_evidence_id]
        duplicate.source_properties[0].evidence_ids = [second_evidence_id]
        core.object_types.append(duplicate)
    return data, core


class _LLM:
    def __init__(self, decision=None):
        self.calls = []
        self.decision = decision

    async def ask(self, task, payload, schema):
        self.calls.append((task, payload))
        assert task == "fact_type_binding"
        assert "observed_value" not in str(payload)
        return schema.model_validate(self.decision or {
            "status": "bind", "type_id": "type:operating_profit",
            "source_column_quote": payload["column_comment"],
            "type_definition_quote": payload["type_candidates"][0]["definition"],
            "source_definition_evidence_id": payload["type_candidates"][0][
                "full_source_definitions"][0]["evidence_id"],
            "type_source_quote": payload["type_candidates"][0][
                "full_source_definitions"][0]["value"],
        })


def _bind(data, core, llm, **options):
    observed = build_fact_observation_candidates(data, **options.pop("observation_options", {}))
    return asyncio.run(bind_fact_observations(data, observed, core, llm, **options))


def test_one_field_call_instantiates_only_exact_observed_tuples_with_full_source_rows(tmp_path):
    data, core = _fixture(tmp_path)
    try:
        llm = _LLM()
        result = _bind(data, core, llm)
        assert result["coverage"]["binding_attempts"] == 1
        assert result["coverage"]["accepted_fields"] == 1
        assert result["coverage"]["candidate_tuples"] == 2
        assert result["coverage"]["instances_created"] == 2
        assert result["coverage"]["per_row_llm_calls"] == 0
        assert len(llm.calls) == 1
        assert all(item["type"] == "type:operating_profit"
                   and item["identity_scope"] == "source_snapshot_observed_tuple_only"
                   and item["evidence_ids"] for item in result["instances"])
        apple = next(item for item in result["instances"]
                     if item["observation_coordinates"]["fruit_code"] == "APPLE")
        assert apple["source_ref"]["source_row_count"] == 2
        assert apple["source_ref"]["row_numbers"] == [1, 2]
        assert len(apple["source_ref"]["record_ids"]) == 2
        assert "schema:fruit.fruit_profit_fact:profit_amount" in apple["evidence_ids"]
        assert apple["binding_decision"]["source_definition_evidence_id"] in apple["evidence_ids"]
        assert len(apple["evidence_ids"]) == 4
        assert not any(item["observation_coordinates"]["fruit_code"] == "APPLE"
                       and item["observation_coordinates"]["region_code"] == "SOUTH"
                       for item in result["instances"])
    finally:
        data.close()


def test_accepted_fact_binding_materializes_type_property_and_value_assertions(tmp_path):
    data, core = _fixture(tmp_path)
    try:
        bound = _bind(data, core, _LLM())
        ontology = {"attributes": []}
        value_attributes = add_fact_value_attributes(
            ontology, bound["field_bindings"])
        assert len(value_attributes) == len(ontology["attributes"]) == 1
        prop = ontology["attributes"][0]
        assert prop["domain"] == ["type:operating_profit"]
        assert prop["mapping_status"] == "observed_fact_value"
        sink = Sink(tmp_path)
        try:
            for item in data.evidence.values():
                sink.put("evidence", item)
            put_fact_instances(sink, bound["instances"], value_attributes)
            assert sink.count("objects") == sink.count("assertions") == 2
            assert check_output(sink)["passed"] is True
        finally:
            sink.close()
    finally:
        data.close()


def test_field_decision_without_instances_does_not_claim_observed_value_property():
    ontology = {"attributes": []}
    result = add_fact_value_attributes(ontology, [{
        "status": "accepted_field_binding", "table": "fruit.fact",
        "value_column": "profit", "type_id": "type:profit",
        "definition_evidence_id": "record:one", "instances_created": 0,
    }])
    assert result == {}
    assert ontology["attributes"] == []


def test_same_name_types_require_complete_verified_equivalence_for_fact_binding(tmp_path):
    data, core = _fixture(tmp_path, duplicate_type=True)
    try:
        without = _bind(data, core, _LLM())
        assert without["instances"] == []
        assert without["coverage"]["skipped_reasons"][
            "multiple_compatible_types_share_label"] == 2
        canonical = "type:operating_profit"
        mapping = {item.id: canonical for item in core.object_types}
        without_assertion = _bind(data, core, _LLM(), canonical_type_map=mapping)
        assert without_assertion["instances"] == []
        with_assertion = _bind(
            data, core, _LLM(), canonical_type_map=mapping,
            equivalence_assertions=[{
                "id": "type_equivalence:fixture",
                "source_type_id": core.object_types[0].id,
                "target_type_id": core.object_types[1].id,
                "canonical_type_id": canonical,
            }])
        assert len(with_assertion["instances"]) == 2
        assert all(item["type"] == canonical for item in with_assertion["instances"])
        assert all(item["binding_decision"]["equivalence_assertion_ids"]
                   == ["type_equivalence:fixture"] for item in with_assertion["instances"])
    finally:
        data.close()


def test_conflicting_value_at_same_coordinates_stays_candidate_only(tmp_path):
    data, core = _fixture(tmp_path, conflicting=True)
    try:
        result = _bind(data, core, _LLM())
        assert result["coverage"]["candidate_tuples"] == 3
        assert result["coverage"]["instances_created"] == 1
        assert result["coverage"]["skipped_reasons"][
            "ambiguous_or_incomplete_coordinates"] == 2
        assert result["instances"][0]["observation_coordinates"]["fruit_code"] == "BANANA"
    finally:
        data.close()


def test_unrecognized_extra_coordinate_cannot_merge_different_source_rows(tmp_path):
    data, core = _fixture(tmp_path, hidden_coordinate=True)
    try:
        result = _bind(data, core, _LLM())
        assert result["coverage"]["instances_created"] == 1
        assert result["coverage"]["skipped_reasons"][
            "unmodeled_source_fields_vary_within_observation"] == 1
        assert result["instances"][0]["observation_coordinates"]["fruit_code"] == "BANANA"
    finally:
        data.close()


def test_coordinate_cap_and_candidate_lineage_mismatch_do_not_create_instances(tmp_path):
    data, core = _fixture(tmp_path)
    try:
        llm = _LLM()
        capped = _bind(data, core, llm, observation_options={"max_dimension_columns": 1})
        assert capped["instances"] == []
        assert capped["coverage"]["binding_attempts"] == 0
        assert llm.calls == []
        assert capped["coverage"]["skipped_reasons"][
            "coordinate_columns_omitted_by_limit"] == 2
        observed = build_fact_observation_candidates(data)
        observed = deepcopy(observed)
        # Select the table by name; sorted table order is implementation detail.
        fact = next(report for report in observed["tables"]
                    if report["table"] == "fruit.fruit_profit_fact")
        fact["candidates"][0]["source_row_count"] = 999
        result = asyncio.run(bind_fact_observations(data, observed, core, _LLM()))
        assert result["coverage"]["instances_created"] == 1
        assert result["coverage"]["skipped_reasons"][
            "source_rows_changed_or_incomplete"] + result["coverage"]["skipped_reasons"].get(
                "source_row_count_exceeds_limit_or_invalid", 0) == 1
    finally:
        data.close()


def test_llm_proposal_fails_when_comment_has_no_full_name_or_unit_conflicts(tmp_path):
    data, core = _fixture(tmp_path, comment="利润金额（万元）")
    try:
        result = _bind(data, core, _LLM())
        assert result["instances"] == []
        assert result["coverage"]["binding_attempts"] == 1
        assert result["coverage"]["skipped_reasons"][
            "column_comment_lacks_business_type_name"] == 2
    finally:
        data.close()

    data, core = _fixture(tmp_path / "second", comment="经营利润金额（万元）")
    try:
        result = _bind(data, core, _LLM())
        assert result["instances"] == []
        assert result["coverage"]["skipped_reasons"]["unit_missing_or_conflicting"] == 2
    finally:
        data.close()


def test_absent_full_source_definition_and_scope_mismatch_are_not_silently_bound(tmp_path):
    data, core = _fixture(tmp_path)
    try:
        no_source = core.model_copy(deep=True)
        no_source.object_types[0].source_properties = []
        llm = _LLM()
        result = _bind(data, no_source, llm)
        assert result["instances"] == []
        assert result["coverage"]["binding_attempts"] == 0
        assert llm.calls == []
        scoped = core.model_copy(deep=True)
        scoped.object_types[0].applicability_scope = {"region_code": "EAST"}
        result = _bind(data, scoped, _LLM())
        assert result["coverage"]["instances_created"] == 1
        assert result["coverage"]["skipped_reasons"][
            "applicability_scope_unverified_or_conflicting"] == 1
    finally:
        data.close()


def test_wrong_definition_quote_is_rejected_even_if_type_id_matches(tmp_path):
    data, core = _fixture(tmp_path)
    try:
        llm = _LLM({"status": "bind", "type_id": "type:operating_profit",
                    "source_column_quote": "经营利润金额（元）",
                    "type_definition_quote": "不存在的定义",
                    "source_definition_evidence_id": "record:fake",
                    "type_source_quote": "收入减成本"})
        result = _bind(data, core, llm)
        assert result["instances"] == []
        assert result["coverage"]["skipped_reasons"]["type_definition_quote_invalid"] == 2
        short = _LLM({
            "status": "bind", "type_id": "type:operating_profit",
            "source_column_quote": "经营利润金额（元）",
            "type_definition_quote": core.object_types[0].definition,
            "source_definition_evidence_id": core.object_types[0].evidence_ids[0],
            "type_source_quote": "利润",
        })
        result = _bind(data, core, short)
        assert result["coverage"]["skipped_reasons"][
            "full_source_definition_quote_invalid"] == 2
    finally:
        data.close()


def test_unusable_or_conflicting_source_meanings_block_binding_before_llm(tmp_path):
    data, core = _fixture(tmp_path)
    try:
        llm = _LLM()
        result = _bind(data, core, llm, max_definition_chars=5)
        assert result["instances"] == [] and llm.calls == []
        report = result["field_bindings"][0]
        assert report["excluded_type_reasons"] == {
            "source_definition_evidence_over_budget": 1}
    finally:
        data.close()

    data, core = _fixture(
        tmp_path / "conflict", duplicate_type=True,
        second_definition="经营利润仅含直营店收入减成本")
    try:
        first, second = core.object_types
        other_evidence = second.evidence_ids[0]
        first.evidence_ids.append(other_evidence)
        first.source_properties[0].evidence_ids.append(other_evidence)
        core.object_types = [first]
        llm = _LLM()
        result = _bind(data, core, llm)
        assert result["instances"] == [] and llm.calls == []
        assert result["field_bindings"][0]["excluded_type_reasons"] == {
            "multiple_source_meanings": 1}
    finally:
        data.close()


def test_formula_assignment_alias_is_not_a_second_calculation_meaning():
    evidence = {}
    for evidence_id, role, value in (
        ("description", "description", "销售收入扣除销售成本后的利润"),
        ("formula_a", "formula", "profit = revenue - cost"),
        ("formula_b", "formula", "operating_profit = revenue - cost"),
    ):
        evidence[evidence_id] = {
            "origin": "observed_record", "raw_fragment": value,
            "raw_fragment_truncated": False,
            "source_ref": {"table": "fruit.def", "column": role,
                           "snapshot_id": "snap", "record_id": evidence_id},
        }
    item = DerivedType(
        id="type:profit", parent="Metric", label="利润",
        definition="销售利润", category="business_type",
        evidence_ids=list(evidence),
        source_properties=[
            SourceProperty(role="description", source_table="fruit.def",
                           source_column="description", evidence_ids=["description"]),
            SourceProperty(role="formula", source_table="fruit.def",
                           source_column="formula", evidence_ids=["formula_a", "formula_b"]),
        ],
    )
    data = SimpleNamespace(snapshot_id="snap", evidence=evidence)
    fragments, reason = _source_definition_evidence(item, data, max_chars=2048)
    assert reason is None and len(fragments) == 3
    evidence["formula_b"]["raw_fragment"] = "operating_profit = revenue + cost"
    fragments, reason = _source_definition_evidence(item, data, max_chars=2048)
    assert fragments == [] and reason == "multiple_source_meanings"


def test_truncated_type_recall_does_not_hide_a_same_label_competitor(tmp_path):
    data, core = _fixture(tmp_path)
    try:
        for index in range(9):
            peer = core.object_types[0].model_copy(deep=True)
            peer.id = f"type:peer_{index}"
            peer.definition += f"；口径 {index}"
            core.object_types.append(peer)
        llm = _LLM()
        result = _bind(data, core, llm, max_type_candidates_per_field=8)
        assert result["instances"] == []
        assert result["coverage"]["binding_attempts"] == 0
        assert result["coverage"]["skipped_reasons"][
            "type_candidate_recall_limit_hit"] == 2
        assert llm.calls == []
    finally:
        data.close()
