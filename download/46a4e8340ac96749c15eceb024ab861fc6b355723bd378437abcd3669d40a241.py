"""Admission ceilings leave real capacity for downstream stages without new budget."""
import asyncio

import pytest
from pydantic import BaseModel

from ontology_r2.llm import BudgetExceeded, StructuredLLM


class Answer(BaseModel):
    value: str


def client(tmp_path, total=8):
    responses = tmp_path / 'responses.yaml'
    responses.write_text('probe: {value: recorded}\n')
    return StructuredLLM({'mode': 'mock', 'responses': str(responses),
                          'max_calls': total, 'max_reserved_tokens': 1000000}, tmp_path)


def test_tail_available_and_same_global_ceiling(tmp_path):
    llm = client(tmp_path)
    llm.admit({'task': 'roles'})
    with llm.reserve_calls(3, stage='groups'):
        for _ in range(4):
            llm.admit({'task': 'groups'})
        tokens = llm.reserved_tokens
        with pytest.raises(BudgetExceeded, match='reserved for later'):
            llm.admit({'task': 'groups'})
        assert llm.calls == 5 and llm.reserved_tokens == tokens
    for _ in range(3):
        llm.admit({'task': 'facts'})
    with pytest.raises(BudgetExceeded):
        llm.admit({'task': 'facts'})
    report = llm.metrics()['call_reservations'][0]
    assert report['calls_before'] == 1 and report['calls_after'] == 5
    assert report['blocked_admissions'] == 1


def test_cache_can_be_used_at_stage_ceiling(tmp_path):
    async def run():
        llm = client(tmp_path, total=2)
        with llm.reserve_calls(1, stage='groups'):
            first = await llm.ask('probe', {'n': 1}, Answer)
            second = await llm.ask('probe', {'n': 1}, Answer)
            assert first == second and llm.calls == 1 and llm.cached == 1
            with pytest.raises(BudgetExceeded):
                await llm.ask('probe', {'n': 2}, Answer)
        assert (await llm.ask('probe', {'n': 2}, Answer)).value == 'recorded'
        assert llm.calls == 2
    asyncio.run(run())


def test_nested_ceiling_and_exception_restore(tmp_path):
    llm = client(tmp_path)
    with llm.reserve_calls(3, stage='outer'):
        with pytest.raises(RuntimeError):
            with llm.reserve_calls(6, stage='inner'):
                llm.admit({})
                llm.admit({})
                with pytest.raises(BudgetExceeded):
                    llm.admit({})
                raise RuntimeError('cancel stage')
        for _ in range(3):
            llm.admit({})
        with pytest.raises(BudgetExceeded):
            llm.admit({})
    llm.admit({})
    assert llm.calls == 6 and not llm._call_ceilings


@pytest.mark.parametrize('value', [True, False, -1, 9, 1.0, '2', None])
def test_invalid_reservation_rejected_without_spending(tmp_path, value):
    llm = client(tmp_path)
    with pytest.raises(ValueError):
        with llm.reserve_calls(value, stage='bad'):
            raise AssertionError('must not enter')
    assert llm.calls == 0 and llm.call_reservations == []


def test_full_reservation_allows_no_group_calls(tmp_path):
    llm = client(tmp_path)
    with llm.reserve_calls(8, stage='groups'):
        with pytest.raises(BudgetExceeded):
            llm.admit({})
    llm.admit({})
    assert llm.calls == 1


def test_zero_preserves_global_budget(tmp_path):
    llm = client(tmp_path, total=1)
    with llm.reserve_calls(0, stage='groups'):
        llm.admit({})
        with pytest.raises(BudgetExceeded):
            llm.admit({})
    assert llm.calls == 1


@pytest.mark.parametrize('reserved', [-1, 9, True, '2'])
def test_invalid_configuration_fails_before_import_or_model(tmp_path, monkeypatch, reserved):
    from ontology_r2 import pipeline
    def forbidden(*args, **kwargs):
        raise AssertionError('invalid budget must fail before data or model setup')
    monkeypatch.setattr(pipeline, 'Dataset', forbidden)
    monkeypatch.setattr(pipeline, 'StructuredLLM', forbidden)
    config = {'dataset': str(tmp_path/'not-read'), 'llm': {'max_calls': 8},
              'instance_bundles': {'enabled': True, 'reserve_calls_for_followup': reserved},
              'progress': {'enabled': False}, 'visualization': {'enabled': False}}
    result = asyncio.run(pipeline.build(config, tmp_path/'result'))
    assert result['status'] == 'failed'
    assert result['error']['type'] == 'ValueError'
    assert 'reserve_calls_for_followup' in result['error']['message']
    assert 'llm' not in result


@pytest.mark.parametrize('failure', ['retry', 'format'])
def test_real_transport_retry_and_format_repair_obey_stage_ceiling(tmp_path, monkeypatch, failure):
    from test_transport import provider
    async def run():
        def answer(body, number):
            return 'submit_result', ('{invalid' if failure == 'format' and number == 1 else {'value':'recorded'})
        reject = (lambda body, number: 503 if number == 1 else None) if failure == 'retry' else False
        with provider(monkeypatch, answer, reject=reject) as requests:
            llm = StructuredLLM({'mode':'agentscope', 'stream':False, 'max_calls':2,
                                 'max_reserved_tokens':1000000, 'max_output_tokens':64,
                                 'max_retries':1, 'max_response_repairs':1}, tmp_path)
            try:
                with llm.reserve_calls(1, stage='groups'):
                    with pytest.raises(BudgetExceeded, match='reserved for later'):
                        await llm.ask('probe', {'n':1}, Answer)
                assert llm.calls == 1 and len(requests) == 1
                assert (await llm.ask('probe', {'n':2}, Answer)).value == 'recorded'
                assert llm.calls == 2 and len(requests) == 2
            finally:
                await llm.close()
    asyncio.run(run())


def test_pipeline_followup_can_spend_reserved_tail(tmp_path, monkeypatch):
    from ontology_r2 import pipeline
    from test_pipeline import setup
    config = setup(tmp_path, scenario='unrelated', mcp=False, rows=2)
    config.update(incremental={'mode':'source_mapping_only'}, discovery={'enabled':False},
                  progress={'enabled':False}, visualization={'enabled':False},
                  instance_bundles={'enabled':True, 'reserve_calls_for_followup':1},
                  type_generalization={'enabled':True})
    config['llm']['max_calls'] = 3
    active = []
    class RecordingLLM(StructuredLLM):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            active.append(self)
    monkeypatch.setattr(pipeline, 'StructuredLLM', RecordingLLM)

    async def group_stage(data, profile, plan, bundles, llm, **kwargs):
        llm.admit({'task':'concept_batch'})
        llm.admit({'task':'group_review_batch'})
        with pytest.raises(BudgetExceeded, match='reserved for later'):
            llm.admit({'task':'concept_batch'})
        return {'snapshot_id':data.snapshot_id, 'plan':plan, 'concepts':[], 'record_alignments':[],
                'concept_relations':[], 'concept_relation_derivations':[],
                'steps':[{'status':'budget_exhausted'}], 'bundles_selected':1,
                'bundles_skipped':1, 'partial':True}

    async def following_stage(data, profile, plan, ask, **kwargs):
        assert active[0].calls == 2 and not active[0]._call_ceilings
        active[0].admit({'task':'type_generalization'})
        return {'plan':plan, 'steps':[], 'partial':False,
                'coverage':{'status':'complete', 'statuses':{'accepted':0},
                            'model_decision_invocations':1}}

    monkeypatch.setattr(pipeline, 'construct_from_bundles', group_stage)
    monkeypatch.setattr(pipeline, 'run_generalization', following_stage)
    result = asyncio.run(pipeline.build(config, tmp_path/'result'))
    assert 'error' not in result, result.get('error')
    assert result['status'] == 'partial'
    assert result['llm']['calls'] == 3
    assert result['type_generalization']['model_decision_invocations'] == 1
    assert result['llm']['call_reservations'][0]['calls_after'] == 2
