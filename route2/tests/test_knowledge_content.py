import asyncio
from types import SimpleNamespace

import pytest

from ontology_r2.knowledge import invalid_original_text, retrieve
from ontology_r2.llm import StructuredLLM
from ontology_r2.storage import write_yaml


class MCPWithBody:
    def __init__(self, body):
        self.body = body

    async def call_tool(self, name, args):
        if name == "search":
            result = {"hits": [{"id": "doc-1", "title": "定义"}]}
        else:
            result = {"id": args["document_id"], "text": self.body,
                      "scope": "测试范围", "version": "1"}
        return SimpleNamespace(isError=False, structuredContent=result)


@pytest.mark.parametrize("body,reason", [
    (None, "null_text"), ("", "empty_text"), (" \t\n ", "empty_text"),
    ("None", "placeholder_text"), (" null ", "placeholder_text"),
    ("UNDEFINED", "placeholder_text"),
])
def test_fetch_placeholder_cannot_support_claim(tmp_path, body, reason):
    script = [
        {"calls": [{"name": "search_documents", "input": {"query": "定义"}}]},
        {"calls": [{"name": "read_document", "input": {"document_id": "doc-1"}}]},
        {"summary": {"claims": [{"document_id": "doc-1", "quote": "None", "statement": "错误定义",
                                "scope": "测试范围", "source_evidence_ids": ["schema:example:field"],
                                "contribution": "definition"}],
                     "remaining_gaps": [], "stop_reason": "done"}},
    ]
    responses = tmp_path / "responses.yaml"
    write_yaml(responses, {"react_script": script})
    llm = StructuredLLM({"mode": "mock", "responses": str(responses), "max_calls": 10}, tmp_path)
    result = asyncio.run(retrieve("定义", {"unit": "example", "evidence": [{"id": "schema:example:field"}]},
                                  MCPWithBody(body), {"max_rounds": 3}, llm))
    assert result["status"] == "error"
    assert result["claims"] == result["documents"] == []
    assert result["document_errors"] == [{"document_id": "doc-1", "reason": reason}]
    assert "mcp_document_rejected" in (tmp_path / "trace.jsonl").read_text()


def test_placeholder_words_inside_real_text_are_allowed():
    assert invalid_original_text("None of these values is an identifier.") is None
