"""Non-fruit source->compiler->facts->new snapshot checks, using mock decisions."""
import asyncio
import csv
from pathlib import Path
import pytest
from ontology_r2.cross_snapshot_reuse import export_reuse_bundle, reuse_on_snapshot
from ontology_r2.fact_observations import build_fact_observation_candidates
from ontology_r2.fact_schema_induction import induce_fact_schema
from ontology_r2.fact_type_binding import bind_fact_observations
from ontology_r2.models import BuildPlan
from scripts.verify_correctness_v14 import _input, _compile_definition_plan, _BindingModel, _NoModel


def test_configuration_to_types_relations_facts_then_appended_snapshot_reuse(tmp_path):
    first, second = _input(tmp_path / 'first'), _input(tmp_path / 'second', count=3)
    try:
        plan, assertions = _compile_definition_plan(first)
        assert len(plan.object_types) == 4
        assert len(plan.relation_types) == len(assertions) == 2
        observed = build_fact_observation_candidates(first)
        model = _BindingModel()
        bound = asyncio.run(bind_fact_observations(first, observed, plan, model))
        assert len(model.calls) == 1
        assert len(bound['instances']) == 2, bound['coverage']
        for instance in bound['instances']:
            assert set(instance['binding_decision']['bound_scope_values']) >= {'device', 'region', 'period'}
            assert instance['observed_value'] in {'100', '120'}
            assert instance['unit'] == 'kWh'
            assert instance['source_ref']['record_ids']
            assert all(eid in first.evidence for eid in instance['evidence_ids'])
        bundle = export_reuse_bundle(first, plan, field_bindings=bound['field_bindings'], fact_observations=observed)
        replay = asyncio.run(reuse_on_snapshot(second, build_fact_observation_candidates(second), bundle))
        assert replay['coverage']['model_calls'] == 0
        assert replay['coverage']['instances_created'] == 3, replay
        assert len(replay['plan'].object_types) == 4
        assert replay['plan'].relation_types == []
        assert sum(item['kind'] == 'relation' for item in replay['pending']) == 2
        assert {item['id'] for item in bound['instances']}.isdisjoint(
            item['id'] for item in replay['fact_binding']['instances'])
        for instance in replay['fact_binding']['instances']:
            assert instance['source_ref']['snapshot_id'] == second.snapshot_id
            assert all(eid in second.evidence for eid in instance['evidence_ids'])
        assert first.snapshot_id != second.snapshot_id
    finally:
        first.close(); second.close()


@pytest.mark.parametrize('change', ['scope', 'definition'])
def test_cross_snapshot_scope_or_definition_change_is_not_false_success(tmp_path, change):
    first = _input(tmp_path / 'first')
    second = _input(tmp_path / 'second', count=3, tenant='B' if change == 'scope' else 'A',
                    changed_definition=change == 'definition')
    try:
        plan, _ = _compile_definition_plan(first)
        observed = build_fact_observation_candidates(first)
        bound = asyncio.run(bind_fact_observations(first, observed, plan, _BindingModel()))
        bundle = export_reuse_bundle(first, plan, field_bindings=bound['field_bindings'], fact_observations=observed)
        replay = asyncio.run(reuse_on_snapshot(second, build_fact_observation_candidates(second), bundle))
        assert replay['fact_binding']['instances'] == []
        assert replay['coverage']['model_calls'] == 0
        assert replay['coverage']['partial'] and replay['pending']
    finally:
        first.close(); second.close()


def test_unrelated_lexical_type_does_not_suppress_fact_induction(tmp_path):
    data = _input(tmp_path, include_confuser=True)
    try:
        accepted, _ = _compile_definition_plan(data)
        core = BuildPlan(object_types=[item for item in accepted.object_types if item.label == '耗电量'])
        observed = build_fact_observation_candidates(data)
        class Model:
            def __init__(self): self.calls = []
            async def ask(self, task, payload, schema):
                assert task == 'fact_schema_induction'
                self.calls.append(task)
                declaration = payload['source_declaration']
                return schema.model_validate({'status': 'proposed', 'label': '设备发电量',
                    'identity_evidence_id': declaration['evidence_id'], 'identity_quote': declaration['value'],
                    'business_object_quote': '设备', 'quantity_quote': '发电量', 'unit': 'kWh',
                    'unit_quote': declaration['value']})
        outcomes = []
        for prior in (BuildPlan(), core):
            llm = Model()
            inducted = asyncio.run(induce_fact_schema(data, observed, prior, llm))
            bound = asyncio.run(bind_fact_observations(data, observed, inducted['plan'], _NoModel(),
                                                       field_templates=inducted['field_templates']))
            outcomes.append(len(bound['instances']))
            assert len(llm.calls) == 1
        assert outcomes == [2, 2]
    finally:
        data.close()


def test_reuse_retains_omitted_fact_population_in_partial_coverage(tmp_path):
    first = _input(tmp_path / 'first')
    second = _input(tmp_path / 'second', count=3)
    try:
        plan, _ = _compile_definition_plan(first)
        plan.relation_types = []
        observed = build_fact_observation_candidates(first)
        bound = asyncio.run(bind_fact_observations(first, observed, plan, _BindingModel()))
        bundle = export_reuse_bundle(first, plan, field_bindings=bound['field_bindings'], fact_observations=observed)
        sampled = build_fact_observation_candidates(second, max_candidates_per_table=1)
        replay = asyncio.run(reuse_on_snapshot(second, sampled, bundle))
        assert replay['pending'] == []
        coverage = replay['coverage']
        assert coverage['instances_created'] == 1
        assert coverage['fact_observation_coverage']['omitted_observed_tuples'] == 2
        assert coverage['fact_observation_limits']['max_candidates_per_table'] == 1
        assert coverage['partial']
        assert 'fact_observation_coverage_partial' in coverage['partial_reasons']
    finally:
        first.close(); second.close()


def test_reuse_rejects_old_semantic_validation_code_even_with_valid_artifact_digest(tmp_path):
    from ontology_r2.storage import digest
    first = _input(tmp_path)
    try:
        plan, _ = _compile_definition_plan(first)
        observed = build_fact_observation_candidates(first)
        bound = asyncio.run(bind_fact_observations(first, observed, plan, _BindingModel()))
        bundle = export_reuse_bundle(first, plan, field_bindings=bound['field_bindings'], fact_observations=observed)
        bundle['semantic_code_hash'] = '0' * 64
        bundle['bundle_id'] = 'reuse_bundle:' + digest({key: value for key, value in bundle.items() if key != 'bundle_id'})
        with pytest.raises(ValueError, match='semantic validation contract differs'):
            asyncio.run(reuse_on_snapshot(first, observed, bundle))
    finally:
        first.close()


def test_reuse_rejects_changed_internal_ontology_contract(tmp_path):
    from copy import deepcopy
    from scripts.verify_correctness_v14 import PROFILE
    data = _input(tmp_path)
    try:
        plan, _ = _compile_definition_plan(data)
        observed = build_fact_observation_candidates(data)
        bound = asyncio.run(bind_fact_observations(data, observed, plan, _BindingModel()))
        bundle = export_reuse_bundle(data, plan, field_bindings=bound['field_bindings'], fact_observations=observed)
        changed = deepcopy(PROFILE)
        changed['object_roots'][1]['definition'] = 'changed semantic definition'
        with pytest.raises(ValueError, match='internal ontology profile differs'):
            asyncio.run(reuse_on_snapshot(data, observed, bundle, profile=changed))
    finally:
        data.close()
