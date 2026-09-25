import asyncio
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from openai import APITimeoutError, BadRequestError

from ontology_r2.knowledge import retrieve
from ontology_r2.llm import StructuredLLM, scope_mock_plan
from ontology_r2.models import Review
from ontology_r2.pipeline import build
from ontology_r2.storage import read_yaml
from test_incremental import FakeMCP, claim
from test_pipeline import setup


@pytest.fixture(autouse=True)
def transport_environment(monkeypatch):
    for key in ("STREAM", "THINKING_MODE", "THINKING_PARAMETER", "REASONING_EFFORT"):
        monkeypatch.delenv("ONTOLOGY_LLM_" + key, raising=False)


@contextmanager
def provider(monkeypatch, answer, *, finish="tool_calls", delay=0, reject=False, report_usage=True):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            rejection = reject(body, len(requests)) if callable(reject) else 400 if reject else None
            if rejection:
                self.send_response(rejection)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"unsupported thinking parameter","type":"invalid_request_error"}}')
                return
            name, result = answer(body, len(requests))
            arguments = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            common = {"id": "response-" + str(len(requests)), "created": 1, "model": "local-test"}
            usage = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
            if not body["stream"]:
                message = {"role": "assistant", "content": arguments} if name is None else {
                    "role": "assistant", "content": None, "reasoning_content": "hidden-reasoning-marker",
                    "tool_calls": [{"id": "call-" + str(len(requests)), "type": "function", "function": {"name": name, "arguments": arguments}}]}
                payload = {**common, "object": "chat.completion", "choices": [{"index": 0, "finish_reason": finish,
                    "message": message}], "usage": usage if report_usage else None}
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def send(choices, **extra):
                event = {**common, "object": "chat.completion.chunk", "choices": choices, **extra}
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()

            try:
                send([{"index": 0, "delta": {"reasoning_content": "hidden-reasoning-marker"}, "finish_reason": None}])
                for index, offset in enumerate(range(0, len(arguments), 9)):
                    call = {"index": 0, "function": {"arguments": arguments[offset:offset + 9]}}
                    if index == 0:
                        call.update(id="call-" + str(len(requests)), type="function")
                        call["function"]["name"] = name
                    send([{"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": None}])
                    if index == 0 and delay:
                        time.sleep(delay)
                if finish is not None:
                    send([{"index": 0, "delta": {}, "finish_reason": finish}])
                if report_usage:
                    send([], usage=usage)
                self.wfile.write(b"data: [DONE]\n\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("ONTOLOGY_LLM_MODEL", "local-test")
    monkeypatch.setenv("ONTOLOGY_LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("ONTOLOGY_LLM_API_KEY", "local-test-secret")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    try:
        yield requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


CONTROLS = [
    ("chat_template_kwargs", {"chat_template_kwargs": {"enable_thinking": False}}),
    ("enable_thinking", {"enable_thinking": False}),
    ("thinking", {"thinking": {"type": "disabled"}}),
    ("reasoning_effort", {"reasoning_effort": "none"}),
]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("parameter,expected", CONTROLS)
def test_full_extraction_json_and_sse_with_explicit_thinking_disabled(tmp_path, monkeypatch, stream, parameter, expected):
    config = setup(tmp_path, "unrelated", mcp=False)
    responses = read_yaml(config["llm"]["responses"])
    config["llm"].update(mode="agentscope", stream=stream, thinking={"mode": "disabled", "parameter": parameter})

    def answer(body, number):
        text = [m["content"] for m in body["messages"] if m["role"] == "user"][-1]
        if isinstance(text, list):
            text = "".join(block.get("text", "") for block in text)
        task, payload = text.split("\n", 1)
        result = scope_mock_plan(responses[task], json.loads(payload)["unit"]) if task in ("plan", "final_plan") else responses[task]
        return "submit_result", result

    with provider(monkeypatch, answer) as requests:
        result = asyncio.run(build(config, tmp_path / "run"))
    assert result["status"] == "complete", result
    assert len(requests) == result["llm"]["calls"] == 4
    assert result["llm"]["provider_reported_tokens"] == 80
    assert all(b["stream"] is stream and all(b[k] == v for k, v in expected.items()) for b in requests)
    assert all(b["tool_choice"] == "auto" for b in requests)
    assert all("企业知识检索已关闭" in json.dumps(b["messages"], ensure_ascii=False) for b in requests)
    events = (tmp_path / "run/trace.jsonl").read_text()
    assert "hidden-reasoning-marker" not in events and "local-test-secret" not in events
    assert ("llm_stream_delta" in events) is stream
    assert read_yaml(tmp_path / "run/validation.yaml")["passed"]


@pytest.mark.parametrize("stream", [False, True])
def test_react_uses_the_same_transport_and_thinking_controls(tmp_path, monkeypatch, stream):
    actions = [("search_documents", {"query": "first-query"}), ("read_document", {"document_id": "a"}),
               ("GenerateStructuredOutput", {"claims": [claim()], "remaining_gaps": [], "stop_reason": "done"})]
    with provider(monkeypatch, lambda body, number: actions[number - 1]) as requests:
        llm = StructuredLLM({"mode": "agentscope", "stream": stream, "thinking": {"mode": "disabled", "parameter": "thinking"}}, tmp_path)

        async def run():
            try:
                return await retrieve("定义", {"unit": "demo.records", "evidence": [{"id": "schema:demo.records:ref"}]}, FakeMCP(), {"max_rounds": 3}, llm)
            finally:
                await llm.close()

        result = asyncio.run(run())
    assert result["status"] == "useful", result
    assert result["model_calls"] == llm.calls == len(requests) == 3 and llm.actual_tokens == 60
    assert all(b["stream"] is stream and b["thinking"] == {"type": "disabled"} for b in requests)
    assert any(m["role"] == "tool" for m in requests[-1]["messages"])
    assert "hidden-reasoning-marker" not in (tmp_path / "trace.jsonl").read_text()


async def ask_once(llm):
    try:
        return await llm.ask("review", {"source": "测试"}, Review)
    finally:
        await llm.close()


@pytest.mark.parametrize("stream,finish,result", [
    (True, None, {"accepted": True}), (True, "length", {"accepted": True}),
    (False, "length", {"accepted": True}), (False, "content_filter", {"accepted": True}),
    (True, "tool_calls", '{"accepted":'), (False, "tool_calls", '{"accepted":'),
])
def test_incomplete_outputs_are_rejected_without_cache(tmp_path, monkeypatch, stream, finish, result):
    with provider(monkeypatch, lambda body, number: ("submit_result", result), finish=finish) as requests:
        llm = StructuredLLM({"mode": "agentscope", "stream": stream}, tmp_path)
        with pytest.raises(ValueError):
            asyncio.run(ask_once(llm))
    assert len(requests) == llm.calls == 1
    assert not list(llm.cache.glob("*.json"))
    metrics = llm.metrics()
    assert metrics["provider_reported_tokens"] == 20
    assert metrics["provider_wire_responses"] == metrics["provider_usage_reported_responses"] == 1
    assert metrics["provider_finish_reasons"] == {finish or "missing": 1}
    assert metrics["provider_incomplete_responses"] == int(finish not in ("stop", "tool_calls"))
    trace = (tmp_path / "trace.jsonl").read_text()
    wire = [event for event in map(json.loads, trace.splitlines()) if event["stage"] == "llm_wire_response"]
    assert wire == [{"stage": "llm_wire_response", "task": "review", "attempt": 1,
                     "finish_reason": finish or "missing", "usage": {"input_tokens": 10, "output_tokens": 10}}]
    assert "hidden-reasoning-marker" not in trace and "local-test-secret" not in trace


def test_incomplete_response_without_usage_reports_unknown_tokens(tmp_path, monkeypatch):
    with provider(monkeypatch, lambda body, number: ("submit_result", {"accepted": True}),
                  finish="length", report_usage=False):
        llm = StructuredLLM({"mode": "agentscope", "stream": True}, tmp_path)
        with pytest.raises(ValueError, match="Incomplete provider response"):
            asyncio.run(ask_once(llm))
    metrics = llm.metrics()
    assert metrics["provider_reported_tokens"] == 0
    assert metrics["provider_wire_responses"] == 1
    assert metrics["provider_usage_reported_responses"] == 0
    assert metrics["provider_finish_reasons"] == {"length": 1}
    assert not list(llm.cache.glob("*.json"))


@pytest.mark.parametrize("name,finish", [(None, "stop"), ("unexpected_tool", "tool_calls")])
def test_auto_choice_rejects_non_result_responses(tmp_path, monkeypatch, name, finish):
    with provider(monkeypatch, lambda body, number: (name, {"accepted": True}), finish=finish) as requests:
        llm = StructuredLLM({"mode": "agentscope", "stream": False}, tmp_path)
        with pytest.raises(ValueError, match="Expected exactly one submit_result call"):
            asyncio.run(ask_once(llm))
    assert requests[0]["tool_choice"] == "auto"
    assert llm.calls == 1 and not list(llm.cache.glob("*.json"))


def test_stream_consumption_deadline_and_provider_rejection_do_not_fallback(tmp_path, monkeypatch):
    with provider(monkeypatch, lambda body, number: ("submit_result", {"accepted": True}), delay=0.3) as requests:
        llm = StructuredLLM({"mode": "agentscope", "stream": True, "timeout_seconds": 0.1}, tmp_path)
        with pytest.raises((TimeoutError, APITimeoutError)):
            asyncio.run(ask_once(llm))
        assert len(requests) == 1 and not list(llm.cache.glob("*.json"))
    with provider(monkeypatch, lambda body, number: ("submit_result", {}), reject=True) as requests:
        llm = StructuredLLM({"mode": "agentscope", "stream": True, "thinking": {"mode": "disabled"}}, tmp_path)
        with pytest.raises(BadRequestError):
            asyncio.run(ask_once(llm))
        assert len(requests) == 1 and requests[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_transient_provider_retry_counts_each_wire_call(tmp_path, monkeypatch):
    reject_first = lambda body, number: 503 if number == 1 else None
    with provider(monkeypatch, lambda body, number: ("submit_result", {"accepted": True}),
                  reject=reject_first) as requests:
        llm = StructuredLLM({"mode": "agentscope", "max_retries": 1, "max_calls": 2}, tmp_path)
        assert asyncio.run(ask_once(llm)).accepted
        assert len(requests) == llm.calls == 2
        assert len(list(llm.cache.glob("*.json"))) == 1
        assert "llm_retry" in (tmp_path / "trace.jsonl").read_text()

    with provider(monkeypatch, lambda body, number: ("submit_result", {"accepted": True}),
                  reject=reject_first) as requests:
        llm = StructuredLLM({"mode": "agentscope", "max_retries": 1, "max_calls": 1}, tmp_path / "capped")
        with pytest.raises(Exception, match="budget exhausted"):
            asyncio.run(ask_once(llm))
        assert len(requests) == llm.calls == 1
        assert not list(llm.cache.glob("*.json"))


def test_environment_overrides_invalidate_cache_and_provider_default_omits_controls(tmp_path, monkeypatch):
    config = {"mode": "agentscope", "stream": False, "thinking": {"mode": "provider_default"}}
    with provider(monkeypatch, lambda body, number: ("submit_result", {"accepted": True})) as requests:
        first = StructuredLLM(config, tmp_path)
        assert asyncio.run(ask_once(first)).accepted
        assert not any(k in requests[0] for k in ("thinking", "enable_thinking", "chat_template_kwargs", "reasoning_effort"))
        monkeypatch.setenv("ONTOLOGY_LLM_STREAM", "true")
        monkeypatch.setenv("ONTOLOGY_LLM_THINKING_MODE", "enabled")
        monkeypatch.setenv("ONTOLOGY_LLM_THINKING_PARAMETER", "reasoning_effort")
        monkeypatch.setenv("ONTOLOGY_LLM_REASONING_EFFORT", "high")
        second = StructuredLLM(config, tmp_path)
        assert asyncio.run(ask_once(second)).accepted
        assert len(requests) == 2 and second.cached == 0 and requests[1]["stream"] is True
        assert requests[1]["reasoning_effort"] == "high"
        third = StructuredLLM(config, tmp_path)
        assert asyncio.run(ask_once(third)).accepted
        assert len(requests) == 2 and third.cached == 1 and third.calls == 0


def test_explicit_cache_location_does_not_change_semantic_cache_key(tmp_path, monkeypatch):
    config = {"mode": "agentscope", "stream": False,
              "thinking": {"mode": "provider_default"}}
    with provider(monkeypatch, lambda body, number: ("submit_result", {"accepted": True})) as requests:
        first = StructuredLLM(config, tmp_path)
        assert asyncio.run(ask_once(first)).accepted
        same_location = {**config, "cache_dir": str(first.cache)}
        replay_output = tmp_path / "replay"
        replay_output.mkdir()
        replay = StructuredLLM(same_location, replay_output)
        assert asyncio.run(ask_once(replay)).accepted
        assert len(requests) == 1
        assert replay.cached == 1 and replay.calls == 0


@pytest.mark.parametrize("config", [{"stream": "sometimes"}, {"thinking": False}, {"thinking": {"mode": "off"}}, {"thinking": {"parameter": "unknown"}}])
def test_invalid_transport_config_fails_before_request(tmp_path, config):
    with pytest.raises(ValueError):
        StructuredLLM(config, tmp_path)
