"""A calculation parameter is evidence, not an invented complete expression."""
import asyncio
from copy import deepcopy

import pytest

from ontology_r2.column_role_inference import (
    classify_formula_fragment, formula_fragment_context, infer_column_role_candidates,
)
from ontology_r2.storage import Dataset
from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from test_column_role_inference import FakeLLM
from test_semantic_cards import _table


def _candidate(column, value, role="formula"):
    return {"column": column, "role": role, "status": "source_verified_role_candidate",
            "semantic_status": "unjudged", "schema_evidence_id": f"schema:sample:{column}",
            "observations": [{"column": column, "row_number": 1,
                              "record_id": "sample:row1", "value": value}]}


@pytest.mark.parametrize("name,comment,value,expected", [
    ("aggregate_kind", "", "SUM", "calculation_operator"),
    ("calculationMode", "", "RATIO", "calculation_operator"),
    ("c1", "聚合类型", "AVG", "calculation_operator"),
    ("input_column", "", "sales.amount", "operand_reference"),
    ("c2", "来源字段", "销售收入", "operand_reference"),
])
def test_declared_formula_fragment_retains_original_source_evidence(name, comment, value, expected):
    table = {"columns": [{"column_name": name, "column_comment": comment}]}
    candidate = _candidate(name, value)
    before = deepcopy(candidate)
    fragment = classify_formula_fragment(table, candidate)
    assert candidate == before
    assert fragment["role"] == fragment["proposed_role"] == "formula"
    assert fragment["effective_role"] == expected
    assert fragment["formula_status"] == "fragment"
    assert fragment["observations"] == before["observations"]
    assert fragment["schema_evidence_id"] == before["schema_evidence_id"]
    assert fragment["expression_constructed"] is False
    assert fragment["operand_target_verified"] is False


@pytest.mark.parametrize("name,comment,value", [
    ("formula_column", "", "收入"),
    ("expression", "", "revenue"),
    ("c1", "计算公式", "收入"),
    ("c1", "", "SUM"),
    ("metric_code", "", "revenue"),
    ("aggregate_kind", "", "SUM(revenue)"),
    ("input_column", "", "收入-成本"),
])
def test_identifier_or_operator_shape_alone_does_not_disprove_formula(name, comment, value):
    table = {"columns": [{"column_name": name, "column_comment": comment}]}
    assert classify_formula_fragment(table, _candidate(name, value)) is None


def test_profile_contradiction_and_unverified_candidate_are_not_reclassified():
    table = {"columns": [{"column_name": "aggregate_kind"}],
             "profiles": [{"column": "aggregate_kind", "distinct_sample": ["SUM", "SUM(revenue)"]}]}
    candidate = _candidate("aggregate_kind", "SUM")
    assert classify_formula_fragment(table, candidate) is None
    candidate["status"] = "unverified"
    assert classify_formula_fragment({"columns": table["columns"]}, candidate) is None


def test_restored_candidates_use_the_same_fragment_classification():
    candidate = _candidate("aggregate_kind", "SUM")
    table = {"columns": [{"column_name": "aggregate_kind"}],
             "inferred_semantic_roles": [candidate]}
    assert formula_fragment_context(table)["aggregate_kind"]["effective_role"] == "calculation_operator"
    assert candidate["role"] == "formula"


def test_new_fragment_roles_are_source_checked_and_reported_as_card_context(tmp_path):
    root = tmp_path / "input"
    _table(root, "definition", {"id": "", "name": "", "description": "", "c1": "", "c2": ""}, [
        {"id": "1", "name": "销售额", "description": "销售收入的统计定义", "c1": "SUM", "c2": "sales.amount"},
        {"id": "2", "name": "均价", "description": "价格的统计定义", "c1": "RATIO", "c2": "unit_price"},
    ])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        llm = FakeLLM({"proposals": [
            {"column": "c1", "role": "calculation_operator",
             "observations": [{"row_number": 1, "value": "SUM"}]},
            {"column": "c2", "role": "operand_reference",
             "observations": [{"row_number": 1, "value": "sales.amount"}]},
        ]})
        report = asyncio.run(infer_column_role_candidates(data, llm, {"enabled": True}))
        assert {"calculation_operator", "operand_reference"} <= set(llm.packets[0]["allowed_roles"])
        assert report["coverage"]["source_verified_candidates"] == 2
        assert report["coverage"]["candidates_used_in_recall"] == 2
        assert not report["coverage"]["partial"]
        for candidate in report["candidates"]:
            assert candidate["formula_status"] == "fragment"
            assert candidate["consumer"] == "semantic_card_recall"
            assert candidate["observations"][0]["record_id"].startswith("fruit.definition:")
        context = formula_fragment_context(data.tables["fruit.definition"])
        assert context["c1"]["effective_role"] == "calculation_operator"
        assert context["c2"]["effective_role"] == "operand_reference"
    finally:
        data.close()


def test_declared_operator_and_complete_expression_do_not_compete_as_formulas(tmp_path):
    """Reproduce the real smoke failure without injected model role decisions."""
    root = tmp_path / "input"
    _table(root, "quantity_definition", {
        "id": "主键", "name": "名称", "definition": "定义",
        "inference_type": "计算方法，例如 SUM、AVG、RATIO；是计算属性",
        "source_field": "量的计算来源；表达式表示该量的计算公式，空值表示基础聚合量",
    }, [
        {"id": "1", "name": "合格率", "definition": "合格数量与检测数量之比",
         "inference_type": "RATIO", "source_field": "qualified_quantity / tested_quantity * 100"},
        {"id": "2", "name": "有效数量", "definition": "通过条件过滤后的数量",
         "inference_type": "FILTER", "source_field": ""},
    ])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        table = data.tables["fruit.quantity_definition"]
        fragments = formula_fragment_context(table)
        assert set(fragments) == {"inference_type"}
        proof = fragments["inference_type"]
        assert proof["evidence_kind"] == "source_declaration_and_profile_values"
        assert proof["sample_exhaustive_in_input"]
        assert all("row_number" not in observation for observation in proof["observations"])
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            cards = index.all_cards(10)["cards"]
            card = next(card for card in cards if card["name"] == "合格率")
            assert [(field["column"], field["value"]) for field in card["fields"]["formula"]] == [
                ("source_field", "qualified_quantity / tested_quantity * 100")]
            assert card["scope"]["inference_type"] == "RATIO"
            assert card["calculation_fragments"][0]["effective_role"] == "calculation_operator"
            from ontology_r2.definition_memberships import _semantic_columns
            assert ("formula", "inference_type") not in _semantic_columns(data, card)
            assert ("formula", "source_field") in _semantic_columns(data, card)
        finally:
            index.close()
    finally:
        data.close()


def test_heuristic_formula_is_reviewed_within_existing_table_budget(tmp_path):
    root = tmp_path / "input"
    _table(root, "quantity_definition", {
        "id": "主键", "name": "名称", "description": "定义", "method_detail": "计算内容",
    }, [{"id": "1", "name": "合格率", "description": "合格数量与检测数量之比",
         "method_detail": "RATIO"}])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        llm = FakeLLM({"proposals": [{"column": "method_detail", "role": "calculation_operator",
            "observations": [{"row_number": 1, "value": "RATIO"}]}]})
        report = asyncio.run(infer_column_role_candidates(data, llm, {
            "enabled": True, "max_tables": 1, "max_columns_per_table": 1, "max_sample_rows": 1}))
        assert len(llm.packets) == report["coverage"]["model_calls_attempted"] == 1
        assert llm.packets[0]["role_review_columns"] == ["method_detail"]
        assert report["candidates"][0]["effective_role"] == "calculation_operator"
        assert not report["coverage"]["partial"]
    finally:
        data.close()


def test_operator_role_cannot_hide_an_expression_or_ordinary_reference():
    table = {"schema": "s", "table_name": "neutral", "columns": [
        {"column_name": "calculation_operator", "column_comment": "计算方法"},
        {"column_name": "source_field", "column_comment": "引用字段"},
    ], "profiles": [
        {"column": "calculation_operator", "scan_scope": "full_input", "usable_count": 2,
         "distinct_sample": ["RATIO", "quantity / population"]},
        {"column": "source_field", "scan_scope": "full_input", "usable_count": 2,
         "distinct_sample": ["CODE001", "CODE002"]},
    ]}
    assert formula_fragment_context(table) == {}
    assert classify_formula_fragment(table, _candidate(
        "calculation_operator", "RATIO", "calculation_operator")) is None


def test_expression_outside_profile_sample_survives_existing_index_scan(tmp_path):
    root = tmp_path / "input"
    _table(root, "quantity_definition", {"id": "主键", "name": "名称", "description": "定义",
        "inference_type": "计算方法"}, [
        {"id": "1", "name": "总量", "description": "可复用量定义", "inference_type": "SUM"},
        {"id": "2", "name": "比率", "description": "可复用量定义", "inference_type": "a / b"},
    ])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        # Simulate a bounded profile which did not see the later expression.
        profile = next(p for p in data.tables["fruit.quantity_definition"]["profiles"]
                       if p["column"] == "inference_type")
        profile.update(distinct_sample=["SUM"], sample_exhaustive_in_input=False,
                       sample_scope="first_1_input_rows")
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            cards = {card["name"]: card for card in index.all_cards(10)["cards"]}
            assert cards["总量"]["scope"]["inference_type"] == "SUM"
            assert cards["总量"]["calculation_fragments"]
            assert cards["比率"]["fields"]["formula"][0]["value"] == "a / b"
            assert "inference_type" not in cards["比率"]["scope"]
            assert cards["比率"]["calculation_fragments"] == []
        finally:
            index.close()
    finally:
        data.close()
