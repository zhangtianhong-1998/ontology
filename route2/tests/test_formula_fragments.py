"""A calculation parameter is evidence, not an invented complete expression."""
import asyncio
from copy import deepcopy

import pytest

from ontology_r2.column_role_inference import (
    classify_formula_fragment, formula_fragment_context, infer_column_role_candidates,
)
from ontology_r2.storage import Dataset
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
