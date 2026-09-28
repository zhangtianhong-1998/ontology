"""Batch envelopes use the same local JSON/SSE auto-tool transport."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ontology_r2.group_incremental import (BundleBatchReview, ConceptBatchDecision, ConceptBundleDecision,
                                         RelationBundleDecision, bundle_request_payload, construct_from_bundles)
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


def _bounded_decision(packet):
    decision = _concept_decision([packet['bundle']['records'][0]['record_id']]).model_dump()
    # The reviewer must see this complete decision, even though its source
    # bundle already fits in the proposal request.
    decision['reason'] = '核验说明' * 500
    return decision


def _bounded_packets_and_limit(llm):
    bundles = _batch_packets(3)
    for bundle in bundles:
        bundle['evidence_padding'] = '原始证据' * 1000
    data = SimpleNamespace(snapshot_id='snap', evidence={})
    # Budget the same complete payload sent in production, including names and
    # role evidence. A duplicate fixture would drift when the contract grows.
    packets = [bundle_request_payload(data, PROFILE, BuildPlan(), bundle) for bundle in bundles]
    reviews = [{**packet, 'candidate': _bounded_decision(packet)} for packet in packets]
    proposal_bytes = llm.request_bytes('concept_batch', {'packets': packets}, ConceptBatchDecision)
    review_pair_bytes = llm.request_bytes('group_review_batch', {'packets': reviews[:2]}, BundleBatchReview)
    limit = max(proposal_bytes, review_pair_bytes) + 1200
    assert llm.request_bytes('group_review_batch', {'packets': reviews}, BundleBatchReview) > limit
    # This is the actual regression boundary: the old 65% payload heuristic
    # would unnecessarily break the valid three-proposal request apart.
    assert len(json.dumps({'packets': packets}, ensure_ascii=False).encode()) > int(limit * .65)
    return bundles, limit


def _bounded_answer(calls):
    def answer(body, number):
        text = body['messages'][-1]['content']
        if isinstance(text, list):
            text = ''.join(item.get('text', '') for item in text)
        task, raw = text.split('\n', 1)
        payload = json.loads(raw)
        calls.append((task, payload))
        if task == 'concept_batch':
            return 'submit_result', {'decisions': [
                {'bundle_id': packet['bundle']['bundle_id'], 'decision': _bounded_decision(packet)}
                for packet in reversed(payload['packets'])]}
        if task == 'group_review_batch':
            return 'submit_result', {'reviews': [
                {'bundle_id': packet['bundle']['bundle_id'], 'review': {'accepted': True}}
                for packet in reversed(payload['packets'])]}
        assert task == 'group_review'
        return 'submit_result', {'accepted': True}
    return answer


def _assert_all_bundles_accepted(result):
    assert result['coverage']['statuses']['accepted'] == 3
    assert {step['bundle_id'] for step in result['steps'] if step['status'] == 'accepted'} == {'b0', 'b1', 'b2'}
    assert {alignment['source_record_id'] for alignment in result['record_alignments']} == {'r0', 'r1', 'r2'}
    assert all(step['proposal_batch_size'] == 3 for step in result['steps'] if step['status'] == 'accepted')
    assert result['pending_concept_decisions'] == result['pending_concept_reviews'] == {}
    assert result['partial'] is False


@pytest.mark.parametrize('stream', [False, True])
def test_exact_request_budget_keeps_proposals_together_and_splits_complete_reviews(tmp_path, monkeypatch, stream):
    for name in ('STREAM', 'THINKING_MODE', 'THINKING_PARAMETER', 'REASONING_EFFORT'):
        monkeypatch.delenv('ONTOLOGY_LLM_' + name, raising=False)
    calls, checkpoints = [], []
    with provider(monkeypatch, _bounded_answer(calls)) as requests:
        llm = StructuredLLM({'mode': 'agentscope', 'stream': stream, 'max_calls': 3}, tmp_path)
        bundles, limit = _bounded_packets_and_limit(llm)
        llm.config['max_input_bytes'] = limit

        async def run():
            try:
                return await construct_from_bundles(SimpleNamespace(snapshot_id='snap', evidence={}),
                    PROFILE, BuildPlan(), bundles, llm, concept_batch_size=3,
                    on_checkpoint=lambda state: checkpoints.append(deepcopy(state)))
            finally:
                await llm.close()

        result = asyncio.run(run())
    assert llm.calls == len(requests) == 3
    assert [(task, len(packet['packets'])) for task, packet in calls] == [
        ('concept_batch', 3), ('group_review_batch', 2), ('group_review_batch', 1)]
    assert [packet['bundle']['bundle_id'] for _, payload in calls[1:] for packet in payload['packets']] == ['b0', 'b1', 'b2']
    assert all(packet['candidate']['reason'] == '核验说明' * 500
               for _, payload in calls[1:] for packet in payload['packets'])
    assert any(len(state['pending_concept_decisions']) == 3 and not state['pending_concept_reviews']
               for state in checkpoints)
    assert any(len(state['pending_concept_reviews']) == 2 for state in checkpoints)
    _assert_all_bundles_accepted(result)
    budgets = [json.loads(line) for line in (tmp_path / 'trace.jsonl').read_text().splitlines()
               if json.loads(line).get('stage') == 'llm_input_budget']
    assert len(budgets) == 3 and all(event['request_bytes'] <= limit - 1200 for event in budgets)
    assert llm.reserved_tokens == sum(event['request_bytes'] + 4096 for event in budgets)


def test_split_review_budget_and_resume_preserve_paid_decisions_and_independent_ids(tmp_path, monkeypatch):
    for name in ('STREAM', 'THINKING_MODE', 'THINKING_PARAMETER', 'REASONING_EFFORT'):
        monkeypatch.delenv('ONTOLOGY_LLM_' + name, raising=False)
    calls, checkpoints = [], []
    with provider(monkeypatch, _bounded_answer(calls)) as requests:
        llm = StructuredLLM({'mode': 'agentscope', 'stream': False, 'max_calls': 2}, tmp_path)
        bundles, limit = _bounded_packets_and_limit(llm)
        llm.config['max_input_bytes'] = limit
        data = SimpleNamespace(snapshot_id='snap', evidence={})

        async def run():
            try:
                first = await construct_from_bundles(data, PROFILE, BuildPlan(), bundles, llm,
                    concept_batch_size=3, on_checkpoint=lambda state: checkpoints.append(deepcopy(state)))
                assert llm.calls == 2
                assert set(first['pending_concept_decisions']) == {'b0', 'b1', 'b2'}
                assert set(first['pending_concept_reviews']) == {'b0', 'b1'}
                assert first['coverage']['statuses']['budget_exhausted'] == 1
                # Re-enter with the same adapter and unchanged shared budget:
                # paid reviews are reused, but no new request is admitted.
                second = await construct_from_bundles(data, PROFILE, first['plan'], bundles, llm,
                    concept_batch_size=3, prior_result=first)
                assert llm.calls == 2
                assert second['coverage']['statuses']['accepted'] == 2
                assert second['coverage']['statuses']['budget_exhausted'] == 1
                assert set(second['pending_concept_decisions']) == {'b2'}
                assert second['pending_concept_reviews'] == {}
                llm.config['max_calls'] = 3  # An explicit added call budget, not a reset.
                third = await construct_from_bundles(data, PROFILE, second['plan'], bundles, llm,
                    concept_batch_size=3, prior_result=second)
                assert third['coverage']['bundles_reused'] == 2
                return third
            finally:
                await llm.close()

        result = asyncio.run(run())
    assert llm.calls == len(requests) == 3
    assert [task for task, _ in calls] == ['concept_batch', 'group_review_batch', 'group_review']
    assert calls[-1][1]['bundle']['bundle_id'] == 'b2'
    assert checkpoints and any(set(state['pending_concept_reviews']) == {'b0', 'b1'} for state in checkpoints)
    _assert_all_bundles_accepted(result)
