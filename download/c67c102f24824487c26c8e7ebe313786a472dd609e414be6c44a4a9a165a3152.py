"""Semantic evidence belongs in card identity before bounded model packets."""
import asyncio

from ontology_r2.column_role_inference import infer_column_role_candidates
from ontology_r2.concept_candidates import _field_roles
from ontology_r2.instance_bundles import build_instance_bundles
from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from ontology_r2.storage import Dataset
from test_column_role_inference import FakeLLM
from test_semantic_cards import _table


def _data(tmp_path, columns, rows, name="wide_table_def"):
    root = tmp_path / "input"
    _table(root, name, columns, rows)
    work = tmp_path / "work"
    work.mkdir()
    return Dataset(root, work)


def test_chinese_business_name_is_retained_after_two_technical_name_fields(tmp_path):
    data = _data(tmp_path, {"id": "", "physical_table_name": "", "schema_name": "",
                            "table_cn_name": "", "table_description": ""}, [
        {"id": "1", "physical_table_name": "fruit_sales_summary_001", "schema_name": "fruit_market",
         "table_cn_name": "水果销售汇总表", "table_description": "按水果、产区与会计期汇总"}])
    try:
        assert _field_roles(data.tables["fruit.wide_table_def"])["name"] == [
            "physical_table_name", "schema_name", "table_cn_name"]
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            cards = index.search("水果销售汇总表", kind="definition")
            assert len(cards) == 1 and cards[0]["name"] == "水果销售汇总表"
            assert {item["column"] for item in cards[0]["fields"]["name"]} == {
                "physical_table_name", "schema_name", "table_cn_name"}
        finally:
            index.close()
    finally:
        data.close()


def test_third_description_difference_keeps_separate_patterns(tmp_path):
    base = {"name": "水果收入", "definition": "水果销售收入", "description": "按会计期汇总"}
    data = _data(tmp_path, {"id": "", "name": "", "definition": "", "description": "", "caliber_description": ""}, [
        {"id": "1", **base, "caliber_description": "含税"},
        {"id": "2", **base, "caliber_description": "不含税"}])
    try:
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            cards = index.all_cards(10)["cards"]
            assert len(cards) == 2 and len({card["pattern_id"] for card in cards}) == 2
            assert all(len(card["fields"]["description"]) == 3 for card in cards)
        finally:
            index.close()
    finally:
        data.close()


def test_source_checked_flag_is_kept_after_existing_scope_fields(tmp_path):
    base = {"name": "水果收入", "definition": "水果销售收入", "period": "2025", "domain_code": "SALES"}
    data = _data(tmp_path, {"id": "", "name": "", "definition": "", "period": "",
                            "domain_code": "", "rank_flag": "排名标志"}, [
        {"id": "1", **base, "rank_flag": "0"}, {"id": "2", **base, "rank_flag": "1"}])
    try:
        llm = FakeLLM({"proposals": [{"column": "rank_flag", "role": "scope",
            "observations": [{"row_number": 1, "value": "0"}, {"row_number": 2, "value": "1"}]}]})
        report = asyncio.run(infer_column_role_candidates(data, llm, {"enabled": True}))
        assert report["candidates"][0]["used_in_candidate_recall"] is True
        assert _field_roles(data.tables["fruit.wide_table_def"])["scope"] == ["period", "domain_code", "rank_flag"]
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            cards = index.all_cards(10)["cards"]
            assert len({card["pattern_id"] for card in cards}) == 2
            assert {card["scope"]["rank_flag"] for card in cards} == {"0", "1"}
        finally:
            index.close()
    finally:
        data.close()


def test_checked_role_candidate_coexists_with_metadata_role(tmp_path):
    data = _data(tmp_path, {"id": "", "name": "", "definition": ""}, [
        {"id": "1", "name": "水果收入", "definition": "水果销售收入"}])
    try:
        table = data.tables["fruit.wide_table_def"]
        table["inferred_semantic_roles"] = [{"column": "name", "role": "scope",
                                             "status": "source_verified_role_candidate"}]
        roles = _field_roles(table)
        assert "name" in roles["name"] and "name" in roles["scope"]
    finally:
        data.close()


def test_full_card_evidence_over_packet_budget_is_explicitly_unprocessed(tmp_path):
    data = _data(tmp_path, {"id": "", "name": "", "definition": "", "description": "", "extra_description": ""}, [
        {"id": "1", "name": "水果收入", "definition": "水果销售收入", "description": "来源说明",
         "extra_description": "含税收入的完整说明。" * 400}])
    try:
        built = build_semantic_cards(data, tmp_path / "cards.sqlite", max_field_chars=10000)
        index = SemanticCardIndex(built["index_path"])
        try:
            result = build_instance_bundles(data, index, {"rules": []}, {"max_bundle_bytes": 1000})
            assert result["bundles"] == []
            assert result["coverage"]["partial"] is True
            assert result["coverage"]["definition_patterns_unprocessed"] == 1
            assert result["coverage"]["skipped"]
            assert any(item["column"] == "extra_description" for item in index.all_cards(1)["cards"][0]["fields"]["description"])
        finally:
            index.close()
    finally:
        data.close()
