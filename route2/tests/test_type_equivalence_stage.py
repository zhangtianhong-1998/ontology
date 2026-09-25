"""A partial same-name group must never enter the canonical type mapping."""

import asyncio

import pytest

from ontology_r2.llm import BudgetExceeded
from ontology_r2.type_equivalence_stage import run_type_equivalence
from test_type_equivalence import _decision, _fixture


def test_one_verified_pair_maps_both_endpoints_and_carries_source_packet():
    data, core = _fixture()
    packets = []

    def decide(packet):
        packets.append(packet)
        return _decision(data, packet["candidate"])

    result = asyncio.run(run_type_equivalence(data, core, decide))
    assert result["canonical_map"] == {
        "type:metric_0": "type:metric_0",
        "type:metric_1": "type:metric_0",
    }
    assert len(result["equivalence_assertions"]) == 1
    assert all(fragment["source_ref"]["snapshot_id"] == "snap"
               for item in packets[0]["source_types"]
               for fragment in item["complete_source_definitions"])
    assert result["coverage"]["model_decision_invocations"] == 1
    assert result["partial"] is False


def test_rejected_or_unresolved_pair_remains_separate():
    data, core = _fixture(formulas=("a-b", "a+b"))
    result = asyncio.run(run_type_equivalence(
        data, core, lambda packet: _decision(data, packet["candidate"])))
    assert result["steps"][0]["status"] == "rejected"
    assert result["canonical_map"] == {}
    assert result["equivalence_assertions"] == []
    assert result["partial"] is True
    no_change = asyncio.run(run_type_equivalence(
        data, core, lambda packet: {"status": "unresolved", "reason": "业务口径不明"}))
    assert no_change["steps"][0]["status"] == "unresolved"
    assert no_change["canonical_map"] == {}


def test_explicit_no_change_is_complete_negative_decision():
    data, core = _fixture()
    result = asyncio.run(run_type_equivalence(
        data, core, lambda packet: {
            "status": "no_change", "reason": "完整定义反映不同经营口径"}))
    assert result["canonical_map"] == {}
    assert result["steps"][0]["status"] == "no_change"
    assert result["partial"] is False


def test_three_types_need_complete_pairwise_acceptance_before_group_map():
    data, core = _fixture(
        descriptions=tuple("水果销售利润是收入扣除成本后的利润" for _ in range(3)),
        formulas=tuple("收入-成本" for _ in range(3)),
    )
    one = asyncio.run(run_type_equivalence(
        data, core, lambda packet: _decision(data, packet["candidate"]),
        max_pairs=3, max_decisions=1))
    assert len(one["equivalence_assertions"]) == 1
    assert one["canonical_map"] == {}
    assert one["partial"] is True
    all_pairs = asyncio.run(run_type_equivalence(
        data, core, lambda packet: _decision(data, packet["candidate"]),
        max_pairs=3, max_decisions=3))
    assert all_pairs["canonical_map"] == {
        "type:metric_0": "type:metric_0",
        "type:metric_1": "type:metric_0",
        "type:metric_2": "type:metric_0",
    }
    assert all(a["canonical_type_id"] == "type:metric_0"
               for a in all_pairs["equivalence_assertions"])


@pytest.mark.parametrize("max_decisions,max_packet_bytes,status", [
    (0, 16000, "budget_exhausted"),
    (1, 1, "packet_over_budget"),
])
def test_decision_and_packet_budgets_never_silently_accept(max_decisions, max_packet_bytes, status):
    data, core = _fixture()
    result = asyncio.run(run_type_equivalence(
        data, core, lambda packet: pytest.fail("must not call"),
        max_decisions=max_decisions, max_packet_bytes=max_packet_bytes))
    assert result["steps"][0]["status"] == status
    assert result["canonical_map"] == {}
    assert result["coverage"]["model_decision_invocations"] == 0


def test_global_budget_exception_does_not_publish_mapping():
    data, core = _fixture()

    def decide(packet):
        raise BudgetExceeded("budget")

    result = asyncio.run(run_type_equivalence(data, core, decide))
    assert result["steps"][0]["status"] == "budget_exhausted"
    assert result["canonical_map"] == {}
