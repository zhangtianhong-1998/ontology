"""Later rounds target gaps without erasing first-pass coverage or the budget."""
import asyncio
import copy

import pytest

from ontology_r2.llm import BudgetExceeded, InputBudgetExceeded, StructuredLLM
from ontology_r2.models import BuildPlan
from ontology_r2.repair_stage import repair_settings, run_targeted_repair


def test_disabled_defaults_work_with_small_mock_budget():
    assert repair_settings(None, 3)['max_calls_per_round'] == 3


@pytest.mark.parametrize('options', [{'max_rounds': 0}, {'max_calls_per_round': -1},
    {'max_examples_per_task': True}, {'enabled': 'yes'}, {'unbounded': True}])
def test_bad_repair_config(options):
    with pytest.raises(ValueError):
        repair_settings(options, 30)


def test_targeted_round_preserves_core_skips_and_enforces_shared_budget(tmp_path, monkeypatch):
    import ontology_r2.template_reuse as reuse
    fixture=tmp_path/'mock.yaml'; fixture.write_text('{}')
    llm=StructuredLLM({'mode':'mock','responses':str(fixture),'max_calls':12},tmp_path)
    llm.calls=7
    initial={'plan':BuildPlan(), 'template_projections':[], 'steps':[{'bundle_id':'old','status':'accepted'}],
             'bundles_skipped':5,'coverage':{'bundles_not_attempted':5},'partial':True}
    def tasks(*args, **kwargs):
        return {'bundles':[{'bundle_id':'repair'}],'tasks':[{'id':'task'}],
                'coverage':{'partial':False}}
    monkeypatch.setattr(reuse,'build_targeted_repair_tasks',tasks)
    async def construct(data, profile, core, bundles, client, **kwargs):
        assert core is initial['plan'] and kwargs['prior_result'] is initial
        assert len(bundles)==1
        client.admit({});client.admit({})
        with pytest.raises(BudgetExceeded):client.admit({})
        value=copy.deepcopy(initial)
        value.update(bundles_skipped=0,partial=False,template_projections=[{'template_id':'new'}])
        return value
    monkeypatch.setattr('ontology_r2.repair_stage.construct_from_bundles',construct)
    result,report=asyncio.run(run_targeted_repair(None,{},[],initial,llm,
        {'enabled':True,'max_rounds':2,'max_calls_per_round':2,'reserve_calls_for_followup':2}))
    assert llm.calls==9 and not llm._call_ceilings
    assert result['bundles_skipped']==5 and result['partial']
    assert result['coverage']['first_pass']['bundles_not_attempted']==5
    assert report['coverage']['model_calls']==2
    assert report['coverage']['rounds_executed']==1
    llm.admit({})  # Later stages can use the released tail.


@pytest.mark.parametrize('prior_partial', [False, True])
def test_zero_round_budget_does_not_call_model(tmp_path,monkeypatch,prior_partial):
    import ontology_r2.template_reuse as reuse
    fixture=tmp_path/'mock.yaml'; fixture.write_text('{}')
    llm=StructuredLLM({'mode':'mock','responses':str(fixture),'max_calls':12},tmp_path)
    result={'plan':BuildPlan(),'partial':prior_partial,'bundles_skipped':0}
    monkeypatch.setattr(reuse,'build_targeted_repair_tasks',lambda *a,**kw:{
        'bundles':[{}],'tasks':[{}],'coverage':{}})
    async def forbidden(*a,**kw):pytest.fail('No model allowed')
    monkeypatch.setattr('ontology_r2.repair_stage.construct_from_bundles',forbidden)
    result,report=asyncio.run(run_targeted_repair(None,{},[],result,llm,
        {'enabled':True,'max_calls_per_round':0}))
    assert report['coverage']['stop_reason']=='budget_exhausted' and llm.calls==0
    assert report['coverage']['status']=='partial' and result['partial']
    assert result['coverage']['partial']
    assert report['coverage']['repair_tasks_not_attempted']==1
    assert report['rounds'][0]['repair_tasks_not_attempted']==1


@pytest.mark.parametrize('omitted', [None, 'groups_not_selected', 'observed_value_tasks_not_selected'])
def test_repair_completion_accounts_for_unselected_discovery(tmp_path, monkeypatch, omitted):
    import ontology_r2.template_reuse as reuse
    fixture = tmp_path / 'mock.yaml'; fixture.write_text('{}')
    llm = StructuredLLM({'mode': 'mock', 'responses': str(fixture), 'max_calls': 12}, tmp_path)
    initial = {'plan': BuildPlan(), 'partial': False, 'bundles_skipped': 0, 'steps': []}
    discovery = {omitted: 3} if omitted else {}
    monkeypatch.setattr(reuse, 'build_targeted_repair_tasks', lambda *a, **kw: {
        'bundles': [{'bundle_id': 'reviewed'}], 'tasks': [], 'coverage': discovery})

    async def construct(*a, **kw):
        return {**initial, 'steps': [{'bundle_id': 'reviewed', 'status': 'no_change'}]}

    monkeypatch.setattr('ontology_r2.repair_stage.construct_from_bundles', construct)
    result, report = asyncio.run(run_targeted_repair(None, {}, [], initial, llm,
        {'enabled': True, 'max_rounds': 2}))
    assert result['partial'] is bool(omitted)
    assert report['coverage']['partial'] is bool(omitted)
    assert report['coverage']['status'] == ('partial' if omitted else 'complete')
    assert report['rounds'][0]['status'] == ('partial' if omitted else 'complete')
    assert report['coverage']['stop_reason'] == ('discovery_limit' if omitted else 'no_semantic_change')
    assert report['coverage']['repair_tasks_not_attempted'] == 0
    if omitted:
        assert report['coverage'][omitted] == 3


@pytest.mark.parametrize('omitted_values', [0, 2])
def test_no_model_queue_still_reports_unselected_observed_values(tmp_path, monkeypatch, omitted_values):
    import ontology_r2.template_reuse as reuse
    fixture = tmp_path / 'mock.yaml'; fixture.write_text('{}')
    llm = StructuredLLM({'mode': 'mock', 'responses': str(fixture), 'max_calls': 12}, tmp_path)
    initial = {'plan': BuildPlan(), 'partial': False, 'bundles_skipped': 0}
    monkeypatch.setattr(reuse, 'build_targeted_repair_tasks', lambda *a, **kw: {
        'bundles': [], 'tasks': [], 'coverage': {'observed_value_tasks_not_selected': omitted_values}})

    async def forbidden(*a, **kw):
        pytest.fail('No model queue exists')

    monkeypatch.setattr('ontology_r2.repair_stage.construct_from_bundles', forbidden)
    result, report = asyncio.run(run_targeted_repair(None, {}, [], initial, llm,
        {'enabled': True, 'max_calls_per_round': 0}))
    assert report['coverage']['stop_reason'] == 'no_targeted_tasks'
    assert report['coverage']['partial'] is bool(omitted_values)
    assert result['partial'] is bool(omitted_values)
    assert report['coverage']['observed_value_tasks_not_selected'] == omitted_values
    assert llm.calls == 0


def test_later_discovery_clears_earlier_unselected_groups(tmp_path, monkeypatch):
    import ontology_r2.template_reuse as reuse
    fixture = tmp_path / 'mock.yaml'; fixture.write_text('{}')
    llm = StructuredLLM({'mode': 'mock', 'responses': str(fixture), 'max_calls': 12}, tmp_path)
    initial = {'plan': BuildPlan(), 'partial': False, 'bundles_skipped': 0, 'steps': []}

    def discover(*a, round_number, **kw):
        return {'bundles': [{'bundle_id': str(round_number)}], 'tasks': [],
                'coverage': {'groups_not_selected': 2 if round_number == 2 else 0}}

    async def construct(*a, prior_result, **kw):
        return {**prior_result, 'concepts': [{'id': 'newly_resolved'}]}

    monkeypatch.setattr(reuse, 'build_targeted_repair_tasks', discover)
    monkeypatch.setattr('ontology_r2.repair_stage.construct_from_bundles', construct)
    result, report = asyncio.run(run_targeted_repair(None, {}, [], initial, llm,
        {'enabled': True, 'max_rounds': 3}))
    assert [row['status'] for row in report['rounds']] == ['partial', 'complete']
    assert report['coverage']['groups_not_selected'] == 0
    assert report['coverage']['status'] == 'complete' and not result['partial']


def test_request_size_failure_does_not_consume_shared_admission(tmp_path):
    fixture = tmp_path / 'mock.yaml'; fixture.write_text('{}')
    llm = StructuredLLM({'mode': 'mock', 'responses': str(fixture),
                         'max_calls': 2, 'max_input_bytes': 30}, tmp_path)
    with llm.reserve_calls(1, stage='repair'):
        with pytest.raises(InputBudgetExceeded) as caught:
            llm.admit({'evidence': 'x' * 100})
        assert not isinstance(caught.value, BudgetExceeded)
        assert llm.calls == llm.reserved_tokens == 0
        llm.admit({})
    assert llm.calls == 1 and llm.call_reservations[0]['blocked_admissions'] == 0


def test_input_blocked_round_does_not_report_semantic_convergence(tmp_path, monkeypatch):
    import ontology_r2.template_reuse as reuse
    fixture = tmp_path / 'mock.yaml'; fixture.write_text('{}')
    llm = StructuredLLM({'mode': 'mock', 'responses': str(fixture), 'max_calls': 12}, tmp_path)
    initial = {'plan': BuildPlan(), 'partial': True, 'bundles_skipped': 0, 'steps': []}
    monkeypatch.setattr(reuse, 'build_targeted_repair_tasks', lambda *a, **kw: {
        'bundles': [{'bundle_id': 'large'}, {'bundle_id': 'small'}],
        'tasks': [], 'coverage': {}})

    async def construct(*args, **kwargs):
        llm.admit({})  # The smaller task was still reviewed.
        return {**initial, 'steps': [{'bundle_id': 'large', 'status': 'input_over_budget'},
                                     {'bundle_id': 'small', 'status': 'no_change'}]}

    monkeypatch.setattr('ontology_r2.repair_stage.construct_from_bundles', construct)
    _, report = asyncio.run(run_targeted_repair(None, {}, [], initial, llm,
        {'enabled': True, 'max_rounds': 3}))
    assert report['coverage']['stop_reason'] == 'input_budget_blocked'
    assert report['coverage']['model_calls'] == 1
    assert report['rounds'][0]['input_over_budget_tasks'] == 1
