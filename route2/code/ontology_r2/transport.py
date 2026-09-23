"""Provider controls and complete responses for both JSON and SSE transports."""
import asyncio
import os

from agentscope.model import OpenAIChatModel


def visible(value):
    """Serialize public messages and tool events without reasoning blocks."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if isinstance(value, dict):
        return {k: visible(v) for k, v in value.items() if k not in ("thinking", "reasoning", "reasoning_content")}
    if isinstance(value, list):
        return [visible(v) for v in value if getattr(v, "type", None) != "thinking" and not (isinstance(v, dict) and v.get("type") == "thinking")]
    return value


def transport_settings(config):
    def setting(name, fallback):
        return os.getenv(name) or fallback

    stream = setting("ONTOLOGY_LLM_STREAM", config.get("stream", False))
    if isinstance(stream, str) and stream.lower() in ("true", "false"):
        stream = stream.lower() == "true"
    if not isinstance(stream, bool):
        raise ValueError("llm.stream / ONTOLOGY_LLM_STREAM must be true or false")
    thinking = config.get("thinking", {})
    if not isinstance(thinking, dict):
        raise ValueError("llm.thinking must be a mapping")
    mode = setting("ONTOLOGY_LLM_THINKING_MODE", thinking.get("mode", "provider_default"))
    parameter = setting("ONTOLOGY_LLM_THINKING_PARAMETER", thinking.get("parameter", "chat_template_kwargs"))
    effort = setting("ONTOLOGY_LLM_REASONING_EFFORT", thinking.get("effort", "medium"))
    if mode not in ("provider_default", "disabled", "enabled"):
        raise ValueError("thinking.mode must be provider_default, disabled or enabled")
    if parameter not in ("enable_thinking", "chat_template_kwargs", "thinking", "reasoning_effort"):
        raise ValueError("Unknown thinking.parameter")
    if effort not in ("minimal", "low", "medium", "high", "xhigh"):
        raise ValueError("thinking.effort must be minimal, low, medium, high or xhigh")
    return {"stream": stream, "thinking": {"mode": mode, "parameter": parameter, "effort": effort}}


def thinking_body(settings):
    options = settings["thinking"]
    if options["mode"] == "provider_default":
        return None
    enabled = options["mode"] == "enabled"
    return {
        "enable_thinking": {"enable_thinking": enabled},
        "chat_template_kwargs": {"chat_template_kwargs": {"enable_thinking": enabled}},
        "thinking": {"thinking": {"type": options["mode"]}},
        "reasoning_effort": {"reasoning_effort": options["effort"] if enabled else "none"},
    }[options["parameter"]]


def check_finish(reason):
    if reason not in ("stop", "tool_calls"):
        raise ValueError("Incomplete provider response: missing or unsuccessful finish_reason")


class CheckedStream:
    """Observe wire completion before the SDK drops finish_reason metadata."""
    def __init__(self, stream):
        self.stream, self.reason = stream, None

    async def __aenter__(self):
        await self.stream.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.stream.__aexit__(*args)

    async def __aiter__(self):
        async for chunk in self.stream:
            if chunk.choices and chunk.choices[0].finish_reason is not None:
                self.reason = chunk.choices[0].finish_reason
            yield chunk


class CheckedChatModel(OpenAIChatModel):
    """Keep native parsing/accumulation, but reject truncated provider output."""
    async def _parse_stream_response(self, start_datetime, response):
        checked = CheckedStream(response)
        async for chunk in super()._parse_stream_response(start_datetime, checked):
            yield chunk
        check_finish(checked.reason)

    def _parse_completion_response(self, start_datetime, response, audio_format="wav"):
        if len(response.choices) != 1:
            raise ValueError("Expected exactly one completion choice")
        check_finish(response.choices[0].finish_reason)
        return super()._parse_completion_response(start_datetime, response, audio_format)


async def complete(model, messages, *, timeout, on_delta, **kwargs):
    """A single deadline includes opening and consuming the entire SSE stream."""
    stream, final = None, None
    async with asyncio.timeout(timeout) as deadline:
        try:
            response = await model(messages, **kwargs)
            if hasattr(response, "__aiter__"):
                stream = response
                async for chunk in stream:
                    if chunk.is_last:
                        final = chunk
                    else:
                        content = visible(chunk.content)
                        if content:
                            on_delta(content)
            else:
                final = response
            # AgentScope can turn task cancellation into an interrupted response.
            if deadline.expired():
                raise TimeoutError("LLM response exceeded its complete-response deadline")
            if final is None or not final.is_last or final.finished_reason != "completed":
                raise ValueError("LLM response was interrupted or has no complete result")
            return final
        finally:
            if stream is not None and hasattr(stream, "aclose"):
                await stream.aclose()
