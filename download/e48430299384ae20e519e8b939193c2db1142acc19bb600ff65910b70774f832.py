"""Exercise scheduler provenance and failed-reference audit boundaries."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from ontology_r2.group_incremental import ConceptBundleDecision, construct_from_bundles
from ontology_r2.llm import StructuredLLM
from ontology_r2.models import BuildPlan
from ontology_r2.repair_stage import run_targeted_repair
from test_group_incremental import PROFILE, _record
from test_prompt_references import payload, record
from test_transport import provider


def test_request_retains_complete_records_without_repeating_scheduler_inventory():
    sources = [f'original:{i}' for i in range(1000)]
    bundle = {'bundle_id': 'repair', 'task_kind': 'concept_induction',
              'records': [_record('seed')], 'source_bundle_ids': sources,
              'template_repair_task': {'source_bundle_ids': sources, 'scope': 'gap',
                                       'known_template_matches': [], 'component_type_candidates': []}}
    original = deepcopy(bundle)
    class Capture:
        async def ask(self, task, sent, schema):
            assert sent['bundle']['records'] == original['records']
            assert sent['bundle']['source_group']['count'] == 1000
            assert sent['targeted_repair']['source_group']['count'] == 1000
            assert 'source_bundle_ids' not in sent['bundle']
            assert 'source_bundle_ids' not in sent['targeted_repair']
            return ConceptBundleDecision(status='unresolved')
    asyncio.run(construct_from_bundles(SimpleNamespace(snapshot_id='snap', evidence={}),
        PROFILE, BuildPlan(), [bundle], Capture(), review=False, enable_template_projection=True))
    assert bundle == original


def test_unchanged_input_block_is_not_resubmitted_after_unrelated_progress(tmp_path, monkeypatch):
    (tmp_path/'mock.yaml').write_text('{}')
    llm = StructuredLLM({'mode': 'mock', 'responses': str(tmp_path/'mock.yaml'), 'max_calls': 20}, tmp_path)
    initial = {'plan': BuildPlan(), 'partial': True, 'bundles_skipped': 0, 'steps': []}
    def discover(*args, round_number, **kwargs):
        return {'bundles': [{'bundle_id': f'repair:{round_number}', 'task_kind': 'concept_induction',
            'records': [{'record_id': 'large', 'fields': {}}],
            'template_repair_task': {'source_bundle_ids': ['b'], 'scope': 'gap'}}], 'tasks': [], 'coverage': {}}
    calls = []
    async def construct(*args, prior_result, **kwargs):
        calls.append(1)
        return {**prior_result, 'concepts': [{'id': 'unrelated'}],
                'steps': [{'bundle_id': 'repair:2', 'status': 'input_over_budget'}]}
    monkeypatch.setattr('ontology_r2.template_reuse.build_targeted_repair_tasks', discover)
    monkeypatch.setattr('ontology_r2.repair_stage.construct_from_bundles', construct)
    result, report = asyncio.run(run_targeted_repair(None, {}, [], initial, llm,
        {'enabled': True, 'max_rounds': 5}))
    assert len(calls) == 1
    assert report['coverage']['stop_reason'] == 'input_budget_blocked'
    assert report['coverage']['input_blocked_tasks_deferred'] == 1
    assert result['partial']


def test_live_transport_records_invalid_alias_without_caching_or_relaxing_evidence(tmp_path, monkeypatch):
    class Answer(BaseModel):
        name: str
    def answer(body, number):
        content = body['messages'][1]['content']
        text = content if isinstance(content, str) else ''.join(x['text'] for x in content)
        sent = json.loads(text.split('\n', 1)[1])
        return 'submit_result', {'name': sent['bundle']['records'][0]['fields']['reference'][0]['value']}
    with provider(monkeypatch, answer) as requests:
        llm = StructuredLLM({'mode': 'agentscope', 'max_response_repairs': 0}, tmp_path)
        async def run():
            try:
                with pytest.raises(ValueError, match='Unaddressed reference alias'):
                    await llm.ask('concept_bundle', payload(record('r')), Answer)
            finally:
                await llm.close()
        asyncio.run(run())
        assert len(requests) == 1
    events = [json.loads(line) for line in (tmp_path/'trace.jsonl').read_text().splitlines()]
    failure = next(x for x in events if x['stage'] == 'llm_alias_decode_error')
    assert failure['wire_result']['name'].startswith('@v')
    assert not list(llm.cache.glob('*.json'))


def test_new_paths_with_no_bound_endpoints_report_empty_without_model_calls(tmp_path):
    from test_pipeline import setup
    from ontology_r2.pipeline import build
    from ontology_r2.storage import read_yaml
    config = setup(tmp_path, scenario='unrelated', mcp=False)
    config['incremental'] = {'mode': 'source_mapping_only'}
    config['discovery'] = {'enabled': False}
    config['instance_bundles'] = {'enabled': True, 'max_llm_bundles': 0,
                                  'template_projection_enabled': True}
    config['configuration_relations'] = {'enabled': True, 'auxiliary_paths': True}
    output = tmp_path / 'run'
    result = asyncio.run(build(config, output))
    assert result['status'] != 'failed', result
    assert result['llm']['calls'] == 0
    assert result['configuration_relations']['auxiliary_endpoint_candidates'] == 0
    assert read_yaml(output / 'auxiliary_relation_proofs.yaml') == []
    coverage = read_yaml(output / 'definition_source_coverage.yaml')
    assert coverage['llm_calls'] == 0
    assert coverage['totals']['rows_with_type_binding'] == 0
