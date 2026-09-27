"""Batch envelopes use the same local JSON/SSE auto-tool transport."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from ontology_r2.group_incremental import ConceptBundleDecision, RelationBundleDecision, construct_from_bundles
from ontology_r2.configuration_relation_stage import ConfigurationRelationDecision
from ontology_r2.llm import StructuredLLM
from ontology_r2.models import BuildPlan, DerivedType
from test_group_incremental import PROFILE, _batch_packets, _concept_decision
from test_transport import provider


@pytest.mark.parametrize("stream", [False, True])
def test_batch_contract_roundtrips_over_local_auto_tool_transport(tmp_path, monkeypatch, stream):
    for name in ("STREAM", "THINKING_MODE", "THINKING_PARAMETER", "REASONING_EFFORT"):
        monkeypatch.delenv("ONTOLOGY_LLM_" + name, raising=False)

    def answer(body, number):
        text = body["messages"][-1]["content"]
        if isinstance(text, list):
            text = "".join(item.get("text", "") for item in text)
        task, raw = text.split("\n", 1)
        packets = json.loads(raw)["packets"]
        if task == "concept_batch":
            results = []
            for packet in packets:
                decision = _concept_decision([packet["bundle"]["records"][0]["record_id"]]).model_dump()
                # Compatible providers sometimes encode nested maps as strings.
                decision["scope"] = "{}"
                decision["aggregation_operator"] = "null"
                results.append({"bundle_id": packet["bundle"]["bundle_id"], "decision": decision})
            return "submit_result", {"decisions": results}
        assert task == "group_review_batch"
        return "submit_result", {"reviews": [{"bundle_id": packet["bundle"]["bundle_id"],
                                               "review": {"accepted": True}}
                                              for packet in packets]}

    with provider(monkeypatch, answer) as requests:
        llm = StructuredLLM({"mode": "agentscope", "stream": stream, "max_calls": 5,
                            "thinking": {"mode": "disabled", "parameter": "thinking"}}, tmp_path)
        async def run():
            try:
                return await construct_from_bundles(
                    SimpleNamespace(snapshot_id="snap", evidence={}), PROFILE,
                    BuildPlan(),
                    _batch_packets(3), llm, concept_batch_size=3)
            finally:
                await llm.close()
        result = asyncio.run(run())
    assert len(requests) == llm.calls == 2
    assert all(item["tool_choice"] == "auto" for item in requests)
    assert all(item["thinking"] == {"type": "disabled"} for item in requests)
    parameters = requests[0]["tools"][0]["function"]["parameters"]
    assert "decisions" in parameters["properties"]
    # AgentScope expands Pydantic $defs before emitting the wire schema.
    decision = parameters["properties"]["decisions"]["items"]["properties"]["decision"]
    assert "classification_quote" in decision["properties"]
    assert "record_id" in decision["properties"]["alignments"]["items"]["properties"]
    assert result["coverage"]["statuses"]["accepted"] == 3
    assert not result["partial"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("schema,response,fields", [
    (ConceptBundleDecision, {"status": "unresolved", "root_type": "null", "aggregation_operator": "null"},
     ("root_type", "aggregation_operator")),
    (RelationBundleDecision, {"status": "unresolved", "parent_relation": "null", "predicate_name": "null"},
     ("parent_relation", "predicate_name")),
    (ConfigurationRelationDecision, {"status": "unresolved", "direction": "null", "parent_relation": "null", "predicate_name": "null"},
     ("direction", "parent_relation", "predicate_name")),
    (DerivedType, {"id": "type:x", "parent": "Measure", "definition": "来源定义", "evidence_ids": [],
                   "unit": "null", "aggregation_operator": "null", "predicate_name": "null"},
     ("unit", "aggregation_operator", "predicate_name")),
])
def test_literal_json_null_tool_strings_are_normalized_only_on_nullable_fields(
        tmp_path, monkeypatch, stream, schema, response, fields):
    for name in ("STREAM", "THINKING_MODE", "THINKING_PARAMETER", "REASONING_EFFORT"):
        monkeypatch.delenv("ONTOLOGY_LLM_" + name, raising=False)
    with provider(monkeypatch, lambda body, number: ("submit_result", response)) as requests:
        llm = StructuredLLM({"mode": "agentscope", "stream": stream}, tmp_path)
        async def run():
            try:
                return await llm.ask("concept_bundle", {"purpose": "local nullable contract test"}, schema)
            finally:
                await llm.close()
        result = asyncio.run(run())
    assert all(getattr(result, field) is None for field in fields)
    assert requests[0]["tool_choice"] == "auto" and llm.calls == 1


@pytest.mark.parametrize("value", ["None", "NULL", "unknown", "空", "n/a"])
def test_arbitrary_unknown_operator_strings_are_not_silently_converted(value):
    with pytest.raises(ValueError):
        ConceptBundleDecision(status="unresolved", aggregation_operator=value)
    assert DerivedType(id="x", parent="Measure", definition="d", evidence_ids=[], unit=value).unit == value
