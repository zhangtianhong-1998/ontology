import asyncio

from ontology_r2.column_role_inference import infer_column_role_candidates
from ontology_r2.concept_candidates import _field_roles, recall_concept_candidates
from ontology_r2.llm import BudgetExceeded
from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from ontology_r2.storage import Dataset
from test_semantic_cards import _table


class FakeLLM:
    def __init__(self, answer=None, error=None):
        self.answer = answer or {}
        self.error = error
        self.packets = []

    async def ask(self, task, packet, schema):
        assert task == "column_role_inference"
        self.packets.append(packet)
        if self.error:
            raise self.error
        return schema.model_validate(self.answer)


def _opaque_data(tmp_path, *, two=False):
    root = tmp_path / "input"
    columns = {"id": "", "c1": "", "c2": "", "creation_date": "", "password": "", "c3": ""}
    rows = [
        {"id": "1", "c1": "苹果销售额", "c2": "苹果销售产生的收入", "creation_date": "2025-01-01", "password": "sk_SECRET_MARKER", "c3": ""},
        {"id": "2", "c1": "香蕉销售额", "c2": "香蕉销售产生的收入", "creation_date": "2025-01-02", "password": "sk_SECRET_MARKER", "c3": ""},
    ]
    _table(root, "opaque_one", columns, rows)
    if two:
        _table(root, "opaque_two", columns, rows)
    work = tmp_path / "work"
    work.mkdir()
    return Dataset(root, work)


def test_no_comments_source_checked_roles_enter_definition_cards(tmp_path):
    data = _opaque_data(tmp_path)
    try:
        assert not _field_roles(data.tables["fruit.opaque_one"]).get("name")
        llm = FakeLLM({"proposals": [
            {"column": "c1", "role": "name", "observations": [
                {"row_number": 1, "value": "苹果销售额"}], "rationale": "人可读名称"},
            {"column": "c2", "role": "description", "observations": [
                {"row_number": 1, "value": "苹果销售产生的收入"}], "rationale": "解释性文字"},
        ], "unresolved_columns": []})
        result = asyncio.run(infer_column_role_candidates(data, llm, {"enabled": True}))
        table = data.tables["fruit.opaque_one"]
        assert _field_roles(table) == {"name": ["c1"], "description": ["c2"]}
        assert result["coverage"]["source_verified_candidates"] == 2
        assert result["coverage"]["model_calls_attempted"] == 1
        assert result["candidates"][0]["observations"][0]["record_id"].startswith("fruit.opaque_one:")
        assert "password" not in str(llm.packets)
        assert "creation_date" not in str(llm.packets)
        assert "sk_SECRET_MARKER" not in str(llm.packets)
        assert "c3" not in str(llm.packets)
        assert llm.packets[0]["sample_rows"][0]["values"]["c1"] == "苹果销售额"
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            cards = index.search("苹果销售额", kind="uncertain")
            assert any(card["name"] == "苹果销售额" for card in cards)
            assert next(card for card in cards if card["name"] == "苹果销售额")[
                "fields"]["description"][0]["value"] == "苹果销售产生的收入"
            assert index.search("苹果销售额", kind="definition") == []
            assert built["coverage"]["by_table"]["fruit.opaque_one"]["row_purpose"][
                "reason"] == "inferred_column_role_is_candidate_not_definition_proof"
        finally:
            index.close()
        recall = recall_concept_candidates(data)
        assert recall["coverage"]["definition_tables_considered"] == 1
    finally:
        data.close()


def test_hallucinated_values_columns_and_conflicting_roles_stay_unresolved(tmp_path):
    data = _opaque_data(tmp_path)
    try:
        llm = FakeLLM({"proposals": [
            {"column": "c1", "role": "name", "observations": [
                {"row_number": 1, "value": "梨销售额"}], "rationale": ""},
            {"column": "password", "role": "name", "observations": [
                {"row_number": 1, "value": "sk_SECRET_MARKER"}], "rationale": ""},
            {"column": "made_up", "role": "formula", "observations": [
                {"row_number": 1, "value": "x/y"}], "rationale": ""},
            {"column": "c2", "role": "description", "observations": [], "rationale": ""},
        ], "unresolved_columns": ["c1", "made_up"]})
        result = asyncio.run(infer_column_role_candidates(data, llm, {"enabled": True}))
        assert result["candidates"] == []
        assert len(result["tables"][0]["rejected"]) == 4
        assert result["tables"][0]["model_unresolved_columns"] == ["c1"]
        assert _field_roles(data.tables["fruit.opaque_one"]) == {}
    finally:
        data.close()


def test_disabled_missing_model_and_table_budget_report_scope(tmp_path):
    data = _opaque_data(tmp_path, two=True)
    try:
        llm = FakeLLM(error=RuntimeError("model unavailable"))
        off = asyncio.run(infer_column_role_candidates(data, llm, {}))
        assert off["coverage"]["status"] == "disabled"
        assert llm.packets == []
        on = asyncio.run(infer_column_role_candidates(
            data, llm, {"enabled": True, "max_tables": 1}))
        assert on["coverage"]["model_calls_attempted"] == 1
        assert on["coverage"]["partial"] is True
        assert {item["reason"] for item in on["tables"]} == {
            "model_unavailable_or_error", "table_budget"}
        assert all(item["status"] == "unresolved" for item in on["tables"])
        budget = FakeLLM(error=BudgetExceeded("shared budget exhausted"))
        capped = asyncio.run(infer_column_role_candidates(
            data, budget, {"enabled": True, "max_tables": 1}))
        assert capped["tables"][0]["error_type"] == "BudgetExceeded"
        assert capped["candidates"] == []
    finally:
        data.close()
