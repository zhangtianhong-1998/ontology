"""Strong source-key declarations must override semantic-looking name tokens."""
import asyncio
from unittest.mock import patch

from ontology_r2.column_role_inference import infer_column_role_candidates
from ontology_r2.concept_candidates import _field_roles, field_role_conflicts
from ontology_r2.definition_memberships import _semantic_columns
from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from ontology_r2.storage import Dataset
from test_column_role_inference import FakeLLM
from test_semantic_cards import _table


def test_static_rule_ids_stay_as_bindings_while_rule_contents_and_parameters_survive(tmp_path):
    root = tmp_path / "input"
    columns = {"business_rule_id": "合成业务字段", "rule_name": "规则名称", "definition": "规则定义",
               "name_id": "", "alias_key": "", "expr_code": "", "rule_type": "",
               "rule_json": "", "field_rule": "允许取值或过滤规则 JSON", "formula_column": "计算公式"}
    base = {"rule_name": "库存阈值", "definition": "低于阈值提示库存风险", "rule_type": "THRESHOLD",
            "rule_json": '{"min":10}', "field_rule": '["A"]', "formula_column": "收入"}
    rows = [{**base, "business_rule_id": str(i), "name_id": "N" + str(i),
             "alias_key": "A" + str(i), "expr_code": "E" + str(i)} for i in range(1, 6)]
    rows[2]["rule_json"] = '{"min":20}'
    rows[3]["field_rule"] = '["B"]'
    rows[4]["rule_type"] = "RANGE"
    _table(root, "definition", columns, rows, pk="business_rule_id")
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        table = data.tables["fruit.definition"]
        roles = _field_roles(table)
        assert roles["name"] == ["rule_name"]
        assert not roles.get("alias")
        assert set(roles["formula"]) == {"rule_json", "field_rule", "formula_column"}
        assert roles["scope"] == ["rule_type"]
        # Keys remain excluded, but heuristic rule/formula contents still need
        # bounded review. An unresolved review must not discard these fields.
        review_columns = {"rule_json", "field_rule", "formula_column"}
        llm = FakeLLM({"unresolved_columns": sorted(review_columns)})
        limits = {"enabled": True, "max_tables": 1, "max_columns_per_table": 3,
                  "max_sample_rows": 3}
        with patch.object(data, "db", wraps=data.db) as db:
            report = asyncio.run(infer_column_role_candidates(data, llm, limits))
            # Role review reads only the fixed source rows, without a new
            # full scan or one query per column.
            assert db.execute.call_count == 1
            sql, positions = db.execute.call_args.args
            assert "WHERE __r2_row IN (?, ?, ?)" in sql
            assert positions == [1, 3, 5]
        assert len(llm.packets) == report["coverage"]["model_calls_attempted"] == 1
        packet = llm.packets[0]
        assert {column["column"] for column in packet["columns"]} == review_columns
        assert set(packet["role_review_columns"]) == review_columns
        assert {row["row_number"] for row in packet["sample_rows"]} == {1, 3, 5}
        assert all(set(row["values"]) == review_columns for row in packet["sample_rows"])
        assert report["candidates"] == []
        assert report["coverage"]["partial"]
        assert _field_roles(table) == roles
        assert len(report["tables"][0]["role_conflicts"]) == 5
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        assert built["coverage"]["definition_patterns_indexed"] == 4
        index = SemanticCardIndex(built["index_path"])
        try:
            cards = {card["row_number"]: card for card in index.all_cards(10)["cards"]}
            assert len(cards) == 5
            assert cards[1]["pattern_id"] == cards[2]["pattern_id"]
            assert cards[1]["card_id"] != cards[2]["card_id"]
            for card in cards.values():
                assert {entry["column"] for entry in card["fields"]["reference"]} == {
                    "business_rule_id", "name_id", "alias_key", "expr_code"}
                assert {entry["column"] for entry in card["fields"]["formula"]} == {
                    "rule_json", "field_rule", "formula_column"}
                assert card["scope"]["rule_type"] in {"THRESHOLD", "RANGE"}
                pairs = _semantic_columns(data, card)
                assert ("formula", "business_rule_id") not in pairs
                assert ("formula", "rule_type") not in pairs
                assert ("scope", "rule_type") in pairs
                assert all(item.get("schema_evidence_id") for item in card["role_conflicts"])
        finally:
            index.close()
    finally:
        data.close()


def test_primary_key_alone_does_not_remove_a_business_name_or_direct_formula(tmp_path):
    root = tmp_path / "input"
    _table(root, "definition", {"rule_name": "业务名称", "definition": "定义", "formula_column": ""}, [
        {"rule_name": "收入", "definition": "销售收入", "formula_column": "收入"}], pk="rule_name")
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        table = data.tables["fruit.definition"]
        table["inferred_semantic_roles"] = [
            {"column": "rule_name", "role": "name", "status": "source_verified_role_candidate"}]
        assert field_role_conflicts(table) == []
        assert _field_roles(table)["name"] == ["rule_name"]
        assert _field_roles(table)["formula"] == ["formula_column"]
    finally:
        data.close()


def test_restored_model_roles_cannot_reintroduce_identifier_as_formula_or_name(tmp_path):
    root = tmp_path / "input"
    _table(root, "definition", {"id": "", "name": "", "definition": "", "source_key": ""}, [
        {"id": "1", "name": "收入", "definition": "销售收入", "source_key": "M001"}])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        table = data.tables["fruit.definition"]
        table["inferred_semantic_roles"] = [
            {"column": "source_key", "role": role, "status": "source_verified_role_candidate"}
            for role in ("name", "alias", "formula", "scope")]
        roles = _field_roles(table)
        assert roles["name"] == ["name"]
        assert not roles.get("alias") and not roles.get("formula")
        assert roles["scope"] == ["source_key"]
        assert {item["proposed_role"] for item in field_role_conflicts(table)} == {"name", "alias", "formula"}
    finally:
        data.close()
