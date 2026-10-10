"""A value overlap and two names cannot establish definition ownership."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from ontology_r2.relation_evidence import (
    RelationEvidenceError, assess_definition_reference, validate_definition_reference,
)


def _case(source_comment="指标引用编码", target_comment="指标编码", table_comment="指标定义"):
    source = {"record_id": "s1", "table": "demo.consumer", "row_number": 1,
              "fields": {"reference": [{"column": "ref", "value": "K1"}],
                         "name": [{"column": "label", "value": "销售收入看板"}]}}
    target = {"record_id": "t1", "table": "demo.dictionary", "row_number": 1,
              "fields": {"reference": [{"column": "code", "value": "K1"}],
                         "name": [{"column": "label", "value": "销售收入"}]}}
    data = SimpleNamespace(snapshot_id="snap", tables={
        source["table"]: {"table_comment": "展示卡片", "columns": [
            {"column_name": "ref", "column_comment": source_comment}]},
        target["table"]: {"table_comment": table_comment, "columns": [
            {"column_name": "code", "column_comment": target_comment}]}})
    bundle = {"snapshot_id": "snap", "rule": {
        "snapshot_id": "snap", "rule_id": "rule1", "status": "checked_technical",
        "source": {"table": source["table"], "field": "ref"},
        "target": {"table": target["table"], "field": "code"},
        "verification": {"scan_scope": "full_input"}},
        "examples": {"positive": [{"source_record_id": "s1", "target_record_id": "t1"}]}}
    decision = {"parent_relation": "points_to", "predicate_name": "definition_reference",
                "source_quote": "销售收入看板", "target_quote": "销售收入",
                "definition": "卡片通过编码引用指标定义"}
    return data, bundle, decision, source, target


def test_matching_codes_and_names_alone_remain_unresolved():
    args = _case("", "", "")
    result = assess_definition_reference(*args)
    assert result["status"] == "unresolved"
    assert result["reason_codes"] == ["source_reference_intent_not_established"]
    # Model-authored definition prose is not independent source evidence.
    args[2]["definition"] = "源记录引用目标记录的公共指标定义，已完全核实"
    assert assess_definition_reference(*args) == result


def test_source_reference_does_not_make_an_anchor_a_definition():
    args = _case(table_comment="指标业务锚点")
    args[4]["fields"]["description"] = [{"column": "detail", "value": "指标的业务适用语境"}]
    assert assess_definition_reference(*args)["reason_codes"] == [
        "target_definition_ownership_not_established"]
    args[0].tables[args[4]["table"]]["table_comment"] = "指标定义使用日志"
    assert assess_definition_reference(*args)["status"] == "unresolved"


@pytest.mark.parametrize("comment", ["指标编码，对应指标详情和公共属性",
                                    "Metric code references metric definitions"])
def test_common_third_party_reference_rejects_direct_definition_reference(comment):
    args = _case(comment, comment, "指标业务锚点")
    result = assess_definition_reference(*args)
    assert result["status"] == "unresolved"
    assert result["reason_codes"] == ["both_endpoints_reference_third_party"]
    assert {item["evidence_id"] for item in result["evidence"]} == {
        "schema:demo.consumer:ref", "schema:demo.dictionary:code", "schema:demo.dictionary"}
    with pytest.raises(RelationEvidenceError) as error:
        validate_definition_reference(*args)
    assert error.value.audit == result


def test_outgoing_target_reference_cannot_be_overridden_by_name_or_definition_claim():
    args = _case(target_comment="编码，引用其他指标定义", table_comment="指标定义")
    assert assess_definition_reference(*args)["reason_codes"] == [
        "target_join_field_references_another_definition"]


def _declared_definition_body(args, value="华南蓝莓的水果销售总额；单位元，按会计期统计。"):
    args[4]["fields"]["description"] = [{"column": "detail", "value": value}]
    args[0].tables[args[4]["table"]]["columns"].append(
        {"column_name": "detail", "column_comment": "指标完整业务定义"})


def test_generic_key_correspondence_does_not_override_own_complete_definition():
    # Original rejected packet 83a649...: the target owns its metric definition;
    # its catalog-level key comment is not an explicit foreign owner.
    args = _case("被引用的编码、名称或参数字段，解释取决于 source_type",
                 "指标编码，对应指标详情和公共属性", "指标业务定义、来源与计算公式")
    _declared_definition_body(args)
    result = assess_definition_reference(*args)
    assert result["status"] == "supported"
    assert result["target_key_interpretation"] == "own_definition_with_weak_correspondence"
    assert any(item.get("column") == "detail" and item["origin"] == "observed_record"
               for item in result["evidence"])
    assert any(item.get("evidence_id") == "schema:demo.dictionary:detail"
               for item in result["evidence"])


@pytest.mark.parametrize("target_comment", [
    "编码，对应其他指标定义", "编码，对应风险定义表的 code",
    "Code references demo.external_definitions", "编码，对应 external_registry.code",
    "本表使用的外键，对应指标详情和公共属性",
])
def test_explicit_other_owner_wins_even_when_target_has_a_definition_body(target_comment):
    args = _case(target_comment=target_comment, table_comment="指标业务定义、计算公式")
    _declared_definition_body(args)
    assert assess_definition_reference(*args)["reason_codes"] == [
        "target_join_field_references_another_definition"]


@pytest.mark.parametrize("mutation", ["missing_body", "truncated_body", "anchor_purpose",
                                       "referenced_body", "only_names"])
def test_weak_correspondence_exception_requires_independent_ownership(mutation):
    shared = "指标编码，对应指标详情和公共属性"
    args = _case(shared, shared, "指标业务定义、来源与计算公式")
    _declared_definition_body(args)
    if mutation == "missing_body":
        args[4]["fields"].pop("description")
    elif mutation == "truncated_body":
        args[4]["fields"]["description"][0]["truncated"] = True
    elif mutation == "anchor_purpose":
        args[0].tables[args[4]["table"]]["table_comment"] = "指标业务锚点"
    elif mutation == "referenced_body":
        args[4]["fields"]["description"][0]["value"] = "引用指标详情中的定义。"
    else:
        args[4]["fields"].pop("description")
        args[0].tables[args[4]["table"]]["columns"][-1]["column_comment"] = "名称"
    assert assess_definition_reference(*args)["reason_codes"] == [
        "both_endpoints_reference_third_party"]


def test_owned_definition_does_not_replace_missing_source_reference_intent():
    args = _case("", "指标编码，对应指标详情和公共属性", "指标业务定义、计算公式")
    _declared_definition_body(args)
    assert assess_definition_reference(*args)["reason_codes"] == [
        "source_reference_intent_not_established"]


def test_observed_subset_keeps_the_same_witness_definition_reference():
    # The same DIM0002 record pair occurs in accepted a8b0f0... and rejected
    # 2d0098.... A different source_type failing the join is a coverage limit.
    args = _case("引用类别：维度编码、measure、metric、fixedValue、period 或 currency",
                 "维度头编码，对应维度定义表的 dim_code", "水果经营分析维度定义")
    args[3]["fields"]["description"] = [{
        "column": "purpose", "value": "API 参数引用 DIM0002 定义；按业务类型解释。"}]
    args[4]["fields"]["description"] = [{"column": "detail", "value": "产区用于水果经营分析。"}]
    args[2].update(source_quote=args[3]["fields"]["description"][0]["value"],
                   target_quote=args[4]["fields"]["description"][0]["value"])
    assert assess_definition_reference(*args)["status"] == "supported"
    args[1]["rule"]["status"] = "observed_subset"
    args[1]["examples"]["counterexamples"] = [{"record_id": "metric-reference", "reason": "missing_in_input"}]
    assert assess_definition_reference(*args)["status"] == "supported"


def test_declared_definition_target_and_reference_source_need_no_foreign_key():
    args = _case()
    assert all("foreign_keys" not in table for table in args[0].tables.values())
    assert validate_definition_reference(*args)["status"] == "supported"


def test_destination_declaration_may_name_its_own_definition_table():
    args = _case("维度编码，对应维度定义表的 code", "维度编码，对应维度定义表的 code", "经营分析维度定义")
    assert assess_definition_reference(*args)["status"] == "supported"
    args = _case("References demo.dictionary", "References demo.dictionary", "指标定义")
    assert assess_definition_reference(*args)["status"] == "supported"


def test_implicit_declared_code_and_full_usage_quote_support_definition_target():
    args = _case("region code，业务字段", "维度编码", "经营分析维度定义")
    args[3]["fields"]["description"] = [{"column": "detail", "value": "按地区和客户类型组合取数"}]
    args[2]["source_quote"] = "按地区和客户类型组合取数"
    assert assess_definition_reference(*args)["status"] == "supported"
    args[2]["source_quote"] = "销售收入看板"
    result = assess_definition_reference(*args)
    assert result["status"] == "supported"
    assert any(item["origin"] == "observed_record" and item.get("record_id") == "s1"
               and item["raw_fragment"] == "按地区和客户类型组合取数"
               for item in result["evidence"])


@pytest.mark.parametrize("mutation", ["missing", "truncated", "other_record", "no_key",
                                       "negated", "negated_english"])
def test_name_quote_cannot_borrow_missing_incomplete_or_foreign_usage(mutation):
    args = _case("region code，业务字段", "维度编码", "经营分析维度定义")
    entry = {"column": "detail", "value": "按地区和客户类型组合取数"}
    args[3]["fields"]["description"] = [entry]
    if mutation == "missing":
        entry["value"] = ""
    elif mutation == "truncated":
        entry["truncated"] = True
    elif mutation == "other_record":
        other = deepcopy(args[3])
        other["record_id"] = "s2"
        args[1]["records"] = [args[3], args[4], other]
        args[3]["fields"].pop("description")
    elif mutation == "no_key":
        args[0].tables[args[3]["table"]]["columns"][0]["column_comment"] = ""
    elif mutation == "negated":
        entry["value"] = "不按地区和客户类型组合取数"
    else:
        entry["value"] = "Does not use the region definition for lookup."
    result = assess_definition_reference(*args)
    assert result["reason_codes"] == ["source_reference_intent_not_established"]
    assert not any(item["origin"] == "observed_record" for item in result["evidence"])


def test_definition_text_supports_unannotated_target_and_reference_prose_supports_source():
    args = _case("", "", "")
    args[3]["fields"]["description"] = [{"column": "purpose", "value": "此卡片引用销售收入。"}]
    args[4]["fields"]["description"] = [{"column": "detail", "value": "销售收入定义为已支付订单金额合计。"}]
    result = assess_definition_reference(*args)
    assert result["status"] == "supported"
    assert len([item for item in result["evidence"] if item["origin"] == "observed_record"]) == 2


def test_truncated_definition_and_negative_reference_do_not_support_acceptance():
    args = _case(table_comment="")
    args[4]["fields"]["description"] = [{"column": "detail", "value": "销售收入定义为…", "truncated": True}]
    assert assess_definition_reference(*args)["status"] == "unresolved"
    args = _case("本字段不引用指标定义")
    assert assess_definition_reference(*args)["reason_codes"] == ["source_declaration_negates_reference"]


def test_actual_schema_wins_over_packet_comment_and_helper_has_no_side_effects():
    args = _case("指标编码，对应指标详情", "指标编码，对应指标详情", "业务锚点")
    args[4]["fields"]["reference"][0]["column_comment"] = "本指标定义的唯一编码"
    before = deepcopy(args)
    assert assess_definition_reference(*args)["status"] == "unresolved"
    assert args == before


@pytest.mark.parametrize("mutation, reason", [
    ("sample", "definition_reference_requires_current_full_input_check"),
    ("snapshot", "definition_reference_requires_current_full_input_check"),
    ("witness", "definition_reference_requires_checked_witness_pair"),
])
def test_technical_evidence_prerequisite(mutation, reason):
    args = _case()
    if mutation == "sample":
        args[1]["rule"]["verification"]["scan_scope"] = "sample"
    elif mutation == "snapshot":
        args[1]["rule"]["snapshot_id"] = "old"
    else:
        args[1]["examples"]["positive"] = []
    assert assess_definition_reference(*args)["reason_codes"] == [reason]


def test_root_pointing_does_not_claim_definition_ownership():
    args = _case("", "", "")
    args[2]["predicate_name"] = None
    assert validate_definition_reference(*args)["status"] == "not_applicable"


def test_audit_preserves_original_schema_and_record_text_while_matching_nfkc():
    schema_text = "　Ｍｅｔｒｉｃ　ｒｅｆｅｒｅｎｃｅ　ｃｏｄｅ　\n"
    source_text = "　此卡片引用销售收入。\n"
    target_text = "　销售收入定义：ＲＥＶＥＮＵＥ　－　ＣＯＳＴ\n"
    args = _case(schema_text, "　指标编码　", "")
    args[4]["fields"]["description"] = [{"column": "detail", "value": target_text}]
    result = assess_definition_reference(*args)
    assert result["status"] == "supported"
    assert next(item for item in result["evidence"] if item.get("evidence_id") ==
                "schema:demo.consumer:ref")["raw_fragment"] == schema_text
    assert next(item for item in result["evidence"] if item["origin"] ==
                "observed_record")["raw_fragment"] == target_text
    args[0].tables[args[3]["table"]]["columns"][0]["column_comment"] = ""
    args[3]["fields"]["description"] = [{"column": "purpose", "value": source_text}]
    result = assess_definition_reference(*args)
    assert result["status"] == "supported"
    assert {item["raw_fragment"] for item in result["evidence"]
            if item["origin"] == "observed_record"} == {source_text, target_text}
    # Rejected evidence must also retain its full punctuation and whitespace.
    shared = "　指标编码，对应指标详情和公共属性；Ｋ１　\n"
    result = assess_definition_reference(*_case(shared, shared, "业务锚点"))
    assert result["status"] == "unresolved"
    assert [item["raw_fragment"] for item in result["evidence"][:2]] == [shared, shared]


def _compiled_case(tmp_path, *, shared_reference=False, weak_correspondence=False, implicit_use=False):
    from ontology_r2.group_incremental import RelationBundleDecision
    from ontology_r2.incremental import direct_mapping
    from ontology_r2.storage import Dataset, read_yaml, write_yaml
    from test_semantic_cards import _table

    root = tmp_path / "input"
    shared = "　指标编码，对应指标详情和公共属性；Ｋ１　\n"
    source_decl = shared if shared_reference else "　Ｍｅｔｒｉｃ　ｒｅｆｅｒｅｎｃｅ　ｃｏｄｅ　\n"
    if implicit_use:
        source_decl = "指标编码"
    target_decl = shared if shared_reference or weak_correspondence else "指标编码"
    formula = "　ＲＥＶＥＮＵＥ　－　ＣＯＳＴ　\n"
    usage = "按地区和客户类型组合取数"
    source_columns = {"id": "记录 ID", "metric_ref": source_decl, "name": "卡片名称"}
    source_row = {"id": "s1", "metric_ref": "K1", "name": "经营利润卡片"}
    if implicit_use:
        source_columns["purpose"] = "用途说明"
        source_row["purpose"] = usage
    _table(root, "consumer", source_columns, [source_row])
    _table(root, "target", {"id": "记录 ID", "metric_code": target_decl,
                            "name": "指标名称", "formula": "　计算公式：Ｆ１　\n"},
           [{"id": "t1", "metric_code": "K1", "name": "经营利润", "formula": formula}])
    if weak_correspondence:
        path = root / "schema/tables/target.yaml"
        metadata = read_yaml(path)
        metadata["table_comment"] = "指标业务定义、来源与计算公式"
        write_yaml(path, metadata)
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    records = []
    for table, key in (("fruit.consumer", "metric_ref"), ("fruit.target", "metric_code")):
        row = next(data.rows(table))
        record = {"table": table, "record_id": data.record_id(table, row), "row_number": row["__r2_row"],
                  "fields": {"reference": [{"column": key, "value": row[key]}],
                             "name": [{"column": "name", "value": row["name"]}]}}
        if "formula" in row:
            record["fields"]["formula"] = [{"column": "formula", "value": row["formula"]}]
        if "purpose" in row:
            record["fields"]["description"] = [{"column": "purpose", "value": row["purpose"]}]
        records.append(record)
    source, target = records
    bundle = {"snapshot_id": data.snapshot_id, "records": records, "rule": {
        "rule_id": "rule-1", "snapshot_id": data.snapshot_id, "status": "checked_technical",
        "source": {"table": source["table"], "field": "metric_ref"},
        "target": {"table": target["table"], "field": "metric_code"},
        "transform": {"operator": "identity"}, "selector": {}, "scope_bindings": {},
        "verification": {"scan_scope": "full_input", "checks": {
            "eligible_references": 1, "unique_matches": 1}}},
        "examples": {"positive": [{"source_record_id": source["record_id"],
                                   "target_record_id": target["record_id"], "matching_raw_value": "K1"}]}}
    decision = RelationBundleDecision(status="proposed", parent_relation="points_to",
                                     predicate_name="definition_reference", label="points_to",
                                     definition="卡片引用指标定义", source_quote="经营利润卡片",
                                     target_quote="经营利润")
    core, _ = direct_mapping(data)
    profile = read_yaml(Path(__file__).resolve().parents[1] / "ontologies/internal_model.yaml")
    return data, profile, core, bundle, decision, formula


def test_compile_relation_rejects_shared_third_party_without_mutating_plan_or_evidence(tmp_path):
    from ontology_r2.group_incremental import compile_relation

    data, profile, core, bundle, decision, _ = _compiled_case(tmp_path, shared_reference=True)
    try:
        original_core, original_evidence = core.model_copy(deep=True), deepcopy(data.evidence)
        with pytest.raises(RelationEvidenceError) as error:
            compile_relation(data, profile, core, bundle, decision)
        assert error.value.audit["reason_codes"] == ["both_endpoints_reference_third_party"]
        assert core == original_core
        assert data.evidence == original_evidence
        assert not core.relations
        assert not core.relation_types
    finally:
        data.close()


@pytest.mark.parametrize("weak_correspondence", [False, True])
@pytest.mark.parametrize("implicit_use", [False, True])
def test_compile_relation_registers_definition_evidence_and_audit_with_original_text(tmp_path, weak_correspondence, implicit_use):
    from ontology_r2.group_incremental import compile_relation

    data, profile, core, bundle, decision, formula = _compiled_case(
        tmp_path, weak_correspondence=weak_correspondence, implicit_use=implicit_use)
    try:
        compiled, plan = compile_relation(data, profile, core, bundle, decision)
        assert len(compiled.relations) == 1
        assert not core.relations
        assert set(plan.evidence_ids) <= data.evidence.keys()
        checks = [data.evidence[eid] for eid in plan.evidence_ids
                  if data.evidence[eid]["origin"] == "automatic_contract_check"]
        assert len(checks) == 1
        audit = checks[0]
        assert audit["basis_evidence_ids"]
        assert set(audit["basis_evidence_ids"]) <= set(plan.evidence_ids)
        assert set(audit["basis_evidence_ids"]) <= data.evidence.keys()
        formula_evidence = [data.evidence[eid] for eid in audit["basis_evidence_ids"]
                            if data.evidence[eid].get("origin") == "observed_record"
                            and data.evidence[eid]["source_ref"]["column"] == "formula"]
        assert len(formula_evidence) == 1
        assert formula_evidence[0]["raw_fragment"] == formula
        assert not formula_evidence[0]["raw_fragment_truncated"]
        if implicit_use:
            assert decision.source_quote == "经营利润卡片"
            assert any(data.evidence[eid].get("source_ref", {}).get("column") == "purpose"
                       and data.evidence[eid]["raw_fragment"] == "按地区和客户类型组合取数"
                       for eid in audit["basis_evidence_ids"])
    finally:
        data.close()
