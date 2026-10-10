"""Regression coverage for unchanged retries and scope mapping contracts."""
import asyncio
from types import SimpleNamespace

from ontology_r2.llm import StructuredLLM
from ontology_r2.models import BuildPlan, DerivedType, RelationPlan
from ontology_r2.repair_stage import run_targeted_repair
from ontology_r2.fact_type_binding import _scope_consistent, field_scope_constants
from ontology_r2.storage import Dataset
from test_semantic_cards import _table


def test_unrelated_progress_does_not_resubmit_same_repair_evidence(tmp_path, monkeypatch):
    responses = tmp_path / 'mock.yaml'
    responses.write_text('{}')
    llm = StructuredLLM({'mode': 'mock', 'responses': str(responses), 'max_calls': 20}, tmp_path)
    initial = {'plan': BuildPlan(), 'partial': True, 'bundles_skipped': 0, 'steps': []}

    def discover(*args, round_number, **kwargs):
        return {'bundles': [{'bundle_id': f'repair:{round_number}',
                            'task_kind': 'concept_induction',
                            'records': [{'record_id': 'unchanged', 'fields': {'name': 'same'}}],
                            'template_repair_task': {'source_bundle_ids': ['original'], 'scope': 'missing_endpoint'}}],
                'tasks': [], 'coverage': {}}

    async def construct(*args, prior_result, **kwargs):
        llm.admit({})
        return {**prior_result, 'concepts': [{'id': 'unrelated-new-concept'}],
                'steps': [{'bundle_id': 'repair:2', 'status': 'unresolved'}]}

    monkeypatch.setattr('ontology_r2.template_reuse.build_targeted_repair_tasks', discover)
    monkeypatch.setattr('ontology_r2.repair_stage.construct_from_bundles', construct)
    result, report = asyncio.run(run_targeted_repair(None, {}, [], initial, llm,
        {'enabled': True, 'max_rounds': 5}))
    assert llm.calls == 1
    assert report['coverage']['stop_reason'] == 'awaiting_new_evidence'
    assert report['coverage']['unchanged_tasks_deferred'] == 1
    assert result['partial']  # Avoiding a duplicate call does not resolve the gap.


def test_scope_constants_require_complete_nonblank_source_column(tmp_path):
    root = tmp_path / 'input'
    _table(root, 'observations', {'id': '主键', 'tenant': '租户标识', 'value': '数量'},
           [{'id': '1', 'tenant': 'A', 'value': '3'},
            {'id': '2', 'tenant': 'A', 'value': '4'}], pk='id')
    work = tmp_path / 'work'
    work.mkdir()
    data = Dataset(root, work)
    try:
        table = next(iter(data.tables.values()))
        constants = field_scope_constants(data, table, {'tenant'})
        item = SimpleNamespace(applicability_scope={}, identity_qualifiers={'tenant': 'A'})
        assert _scope_consistent(item, table, '', {}, {}, {'tenant': 'tenant'}, constants)
        assert not _scope_consistent(item, table, '', {}, {}, {'tenant': 'A'}, constants)
        assert not _scope_consistent(item, table, '', {}, {}, {'tenant': 'tenant'}, [])
        data.db.execute(f'UPDATE "{table["sql_name"]}" SET tenant=NULL WHERE __r2_row=2')
        assert field_scope_constants(data, table, {'tenant'}) == []
    finally:
        data.close()


def test_relation_replay_reacts_to_endpoint_changes_only(monkeypatch):
    from ontology_r2.group_incremental import replay_business_relations
    calls = []
    monkeypatch.setattr('ontology_r2.group_incremental.compile_business_relation',
                        lambda *a, **kw: (calls.append(1) or (None, None, 'missing_endpoint')))
    plan = BuildPlan(object_types=[DerivedType(id='t', parent='Term', definition='word',
                                               category='business_type', evidence_ids=[])])
    reference = RelationPlan(id='p', source_table='a', target_table='b', mode='identifier',
                             source_column='x', target_column='y', evidence_ids=[])
    result = {'plan': plan, 'concepts': [{'id': 'c', 'ontology_type_id': 't', 'ontology_level': 'type'}],
              'record_alignments': [{'source_record_id': 'r', 'concept_id': 'c'}],
              'pending_record_relations': [{'bundle': {'bundle_id': 'b', 'records': [{'record_id': 'r'}]},
                                           'decision': {'status': 'unresolved'}, 'plan': reference.model_dump()}]}
    replay_business_relations(None, {}, result)
    replay_business_relations(None, {}, result)
    assert len(calls) == 1
    result['plan'].object_types.append(DerivedType(id='unrelated', parent='Term', definition='other', evidence_ids=[]))
    replay_business_relations(None, {}, result)
    assert len(calls) == 1
    result['plan'].object_types[0].applicability_scope = {'namespace': 'changed'}
    replay_business_relations(None, {}, result)
    assert len(calls) == 2
