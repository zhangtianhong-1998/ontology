"""A shared wire boundary must serialize all callers and space completed calls."""
import asyncio
import contextvars
import time
from types import SimpleNamespace

import pytest

from ontology_r2.llm import StructuredLLM


@pytest.mark.parametrize('value', [-0.1, True, '0.2', float('nan'), float('inf'), 61])
def test_reject_invalid_interval_before_request(tmp_path, value):
    with pytest.raises(ValueError, match='request_interval_seconds'):
        StructuredLLM({'request_interval_seconds': value}, tmp_path)


@pytest.mark.parametrize('interval', [0, 0.025])
def test_structured_and_react_share_serial_wire_boundary(tmp_path, monkeypatch, interval):
    llm = StructuredLLM({'request_interval_seconds': interval, 'max_retries': 0}, tmp_path)
    model = SimpleNamespace(wire_observer=contextvars.ContextVar('observer', default=None))
    monkeypatch.setattr(llm, 'get_model', lambda: model)
    events, active = [], 0

    async def transport(model, messages, **kwargs):
        nonlocal active
        assert active == 0, 'Concurrent caller bypassed the wire lock'
        active += 1
        events.append(('start', time.monotonic()))
        await asyncio.sleep(0.004)
        active -= 1
        events.append(('end', time.monotonic()))
        return SimpleNamespace(usage=None)

    monkeypatch.setattr('ontology_r2.llm.complete', transport)

    async def run():
        await asyncio.gather(*(llm.complete([], task=task) for task in
                               ('concept_bundle', 'react', 'association_react')))
    asyncio.run(run())
    assert [kind for kind, _ in events] == ['start', 'end'] * 3
    for index in (2, 4):
        assert events[index][1] - events[index - 1][1] >= interval - 0.002


def test_failed_call_also_spaces_the_next_call(tmp_path, monkeypatch):
    llm = StructuredLLM({'request_interval_seconds': 0.025, 'max_retries': 0}, tmp_path)
    model = SimpleNamespace(wire_observer=contextvars.ContextVar('observer', default=None))
    monkeypatch.setattr(llm, 'get_model', lambda: model)
    times = []

    async def transport(*args, **kwargs):
        times.append(time.monotonic())
        if len(times) == 1:
            raise TimeoutError('local test')
        return SimpleNamespace(usage=None)
    monkeypatch.setattr('ontology_r2.llm.complete', transport)

    async def run():
        with pytest.raises(TimeoutError):
            await llm.complete([], task='concept_bundle')
        await llm.complete([], task='react')
    asyncio.run(run())
    assert times[1] - times[0] >= 0.023
