"""Operational changes preserve semantic caches; stage budgets count repairs."""
import asyncio

import pytest

from ontology_r2.llm import BudgetExceeded, StructuredLLM
from ontology_r2.models import Review
from ontology_r2.semantic_state import implementation_fingerprints
from test_transport import ask_once, provider


def test_cache_survives_operational_changes_but_not_generation_changes(tmp_path, monkeypatch):
    base = {"mode": "agentscope", "stream": False,
            "thinking": {"mode": "provider_default"}, "cache_dir": str(tmp_path / "cache")}
    with provider(monkeypatch, lambda body, number: ("submit_result", {"accepted": True})) as requests:
        first = StructuredLLM(base, tmp_path)
        assert asyncio.run(ask_once(first)).accepted
        operational = {**base, "max_calls": 0, "max_reserved_tokens": 0,
                       "max_input_bytes": 1, "timeout_seconds": 300,
                       "max_retries": 0, "max_response_repairs": 2,
                       "request_interval_seconds": .2, "stream": True}
        replay = StructuredLLM(operational, tmp_path)
        assert asyncio.run(ask_once(replay)).accepted
        assert replay.cached == 1 and replay.calls == 0 and len(requests) == 1
        changed = StructuredLLM({**base, "max_output_tokens": 1024}, tmp_path)
        assert asyncio.run(ask_once(changed)).accepted
        assert changed.cached == 0 and len(requests) == 2


def test_stage_budget_prevents_format_repair_from_sending_second_request(tmp_path, monkeypatch):
    with provider(monkeypatch, lambda body, number: (
            "submit_result", {"accepted": "invalid"} if number == 1 else {"accepted": True})) as requests:
        llm = StructuredLLM({"mode": "agentscope", "max_calls": 10,
                             "max_response_repairs": 1}, tmp_path)
        async def run():
            try:
                with llm.call_budget(1, stage="one_call_stage"):
                    with pytest.raises(BudgetExceeded, match="one_call_stage"):
                        await llm.ask("review", {"unit": "source"}, Review)
            finally:
                await llm.close()
        asyncio.run(run())
        assert len(requests) == llm.calls == 1
        assert llm.call_reservations[-1]["blocked_admissions"] == 1


def test_cache_supports_omitted_temperature(tmp_path, monkeypatch):
    config = {"mode": "agentscope", "temperature": None, "cache_dir": str(tmp_path / "cache")}
    with provider(monkeypatch, lambda body, number: ("submit_result", {"accepted": True})) as requests:
        assert asyncio.run(ask_once(StructuredLLM(config, tmp_path))).accepted
        replay = StructuredLLM({**config, "max_calls": 0}, tmp_path)
        assert asyncio.run(ask_once(replay)).accepted
        assert len(requests) == 1 and replay.calls == 0
        assert requests[0].get("temperature") is None


def test_presentation_edit_keeps_semantic_fingerprint_but_core_edit_invalidates(tmp_path):
    core = tmp_path / "compiler.py"
    core.write_text("CONTRACT = 1\n")
    viewer = tmp_path / "viewer.html"
    viewer.write_text("<p>first</p>")
    before = implementation_fingerprints(tmp_path)
    viewer.write_text("<p>second</p>")
    after = implementation_fingerprints(tmp_path)
    assert after["implementation_code_hash"] != before["implementation_code_hash"]
    assert after["semantic_code_hash"] == before["semantic_code_hash"]
    core.write_text("CONTRACT = 2\n")
    assert implementation_fingerprints(tmp_path)["semantic_code_hash"] != after["semantic_code_hash"]
