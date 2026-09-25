"""Superclass stage must not confuse bounded recall with accepted ontology."""

import asyncio

import pytest

from ontology_r2.llm import BudgetExceeded
from ontology_r2.type_generalization_stage import run_generalization
from test_type_generalization import PROFILE, _decision, _fixture


def test_stage_calls_structured_callback_and_compiles_only_accepted_decision():
    data, core = _fixture()
    packets = []

    async def decide(packet):
        packets.append(packet)
        return _decision()

    result = asyncio.run(run_generalization(
        data, PROFILE, core, decide, max_pairs=3, max_decisions=1))
    assert len(packets) == 1
    assert packets[0]["candidate"]["status"] == "candidate_only"
    assert all(item["definition_fragments"] for item in packets[0]["source_types"])
    assert all(fragment["source_ref"]["snapshot_id"] == "snap"
               for item in packets[0]["source_types"]
               for fragment in item["definition_fragments"])
    assert result["steps"][0]["status"] == "accepted"
    assert len(result["plan"].object_types) == 3
    assert len(core.object_types) == 2
    assert result["coverage"]["model_decision_invocations"] == 1
    assert result["coverage"]["quality_status"].startswith("unjudged")
    assert result["partial"] is False


def test_candidate_is_not_accepted_when_model_returns_no_change():
    data, core = _fixture()
    result = asyncio.run(run_generalization(
        data, PROFILE, core, lambda packet: {"status": "no_change", "reason": "口径不明"},
        max_pairs=2, max_decisions=1))
    assert result["steps"][0]["status"] == "no_change"
    assert len(result["plan"].object_types) == 2
    assert result["coverage"]["statuses"]["accepted"] == 0


@pytest.mark.parametrize("max_decisions,max_packet_bytes,status", [
    (0, 16000, "budget_exhausted"),
    (1, 1, "packet_over_budget"),
])
def test_stage_budget_omits_model_calls(max_decisions, max_packet_bytes, status):
    data, core = _fixture()
    calls = []
    result = asyncio.run(run_generalization(
        data, PROFILE, core, lambda packet: calls.append(packet),
        max_pairs=1, max_decisions=max_decisions,
        max_packet_bytes=max_packet_bytes))
    assert calls == []
    assert result["steps"][0]["status"] == status
    assert result["coverage"]["model_decision_invocations"] == 0
    assert result["partial"] is True


def test_stage_keeps_failed_decision_out_of_accepted_core():
    data, core = _fixture()
    rejected = asyncio.run(run_generalization(
        data, PROFILE, core, lambda packet: {"status": "proposed"},
        max_pairs=1, max_decisions=1))
    assert rejected["steps"][0]["status"] == "rejected"
    assert rejected["coverage"]["statuses"]["rejected"] == 1
    assert len(rejected["plan"].object_types) == 2


def test_global_budget_exception_stops_further_decisions():
    data, core = _fixture()

    def decide(packet):
        raise BudgetExceeded("global model budget")

    result = asyncio.run(run_generalization(
        data, PROFILE, core, decide, max_pairs=3, max_decisions=3))
    assert result["steps"][0]["status"] == "budget_exhausted"
    assert result["coverage"]["model_decision_invocations"] == 1
    assert result["partial"] is True


def test_eligible_types_outside_bounded_recall_remain_unjudged():
    data, core = _fixture(descriptions=("云平台采摘记录", "线下渠道海运清单"))
    result = asyncio.run(run_generalization(
        data, PROFILE, core, lambda packet: pytest.fail("must not call"),
        max_pairs=5, max_decisions=5))
    assert result["steps"] == []
    assert result["coverage"]["eligible_source_type_count"] == 2
    assert result["coverage"]["eligible_types_outside_candidate_pairs"] == 2
    assert result["partial"] is True
