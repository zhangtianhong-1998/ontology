"""Later rounds target gaps without erasing first-pass coverage or the budget."""
import asyncio
import copy

import pytest

from ontology_r2.llm import BudgetExceeded, StructuredLLM
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


def test_zero_round_budget_does_not_call_model(tmp_path,monkeypatch):
    import ontology_r2.template_reuse as reuse
    fixture=tmp_path/'mock.yaml'; fixture.write_text('{}')
    llm=StructuredLLM({'mode':'mock','responses':str(fixture),'max_calls':12},tmp_path)
    result={'plan':BuildPlan(),'partial':True,'bundles_skipped':0}
    monkeypatch.setattr(reuse,'build_targeted_repair_tasks',lambda *a,**kw:{
        'bundles':[{}],'tasks':[{}],'coverage':{}})
    async def forbidden(*a,**kw):pytest.fail('No model allowed')
    monkeypatch.setattr('ontology_r2.repair_stage.construct_from_bundles',forbidden)
    _,report=asyncio.run(run_targeted_repair(None,{},[],result,llm,
        {'enabled':True,'max_calls_per_round':0}))
    assert report['coverage']['stop_reason']=='budget_exhausted' and llm.calls==0
