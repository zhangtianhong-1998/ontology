"""Accepted semantic state is reusable only against its evidence contract."""
import pytest
from ontology_r2.demo import make_demo
from ontology_r2.incremental import direct_mapping
from ontology_r2.semantic_state import save_state, restore_state
from ontology_r2.storage import Dataset, read_yaml
from pathlib import Path
import json


def test_snapshot_checkpoint_validates_and_reuses_plan(tmp_path, monkeypatch):
    root = tmp_path/'input'
    make_demo(root, 3, 'unrelated')
    work = tmp_path/'work'; work.mkdir()
    data = Dataset(root, work)
    profile = read_yaml(Path(__file__).parents[1]/'ontologies/internal_model.yaml')
    try:
        plan, mapping = direct_mapping(data)
        checkpoint = tmp_path/'semantic_state.json'
        state = {'plan': plan, 'snapshot_id': data.snapshot_id, 'concepts': [], 'steps': []}
        save_state(checkpoint, data, profile, state)
        restored = restore_state(checkpoint, data, profile)
        assert restored['plan'] == plan
        data.semantic_retrieval_contract = {'dtype': 'float32', 'model_sha256': 'new-weights'}
        with pytest.raises(ValueError, match='semantic contract'):
            restore_state(checkpoint, data, profile)
        del data.semantic_retrieval_contract
        # Old cached stages were compiled before parameter-aware type identity.
        old = json.loads(checkpoint.read_text())
        old['contract']['version'] = 2
        checkpoint.write_text(json.dumps(old))
        with pytest.raises(ValueError, match='semantic contract'):
            restore_state(checkpoint, data, profile)
        save_state(checkpoint, data, profile, state)
        monkeypatch.setenv('ONTOLOGY_LLM_MODEL', 'changed-model')
        with pytest.raises(ValueError, match='contract'):
            restore_state(checkpoint, data, profile)
    finally:
        data.close()


def test_retrieval_contract_uses_environment_dtype_but_not_progress(monkeypatch):
    from ontology_r2.pipeline import retrieval_contract
    monkeypatch.setenv('ONTOLOGY_EMBEDDING_ENABLED', 'false')
    monkeypatch.setenv('ONTOLOGY_EMBEDDING_DTYPE', 'float32')
    first = retrieval_contract({'embedding': {'dtype': 'auto'}})
    monkeypatch.setenv('ONTOLOGY_EMBEDDING_SHOW_PROGRESS', 'true')
    assert retrieval_contract({'embedding': {'dtype': 'auto'}}) == first
    monkeypatch.setenv('ONTOLOGY_EMBEDDING_DTYPE', 'bfloat16')
    assert retrieval_contract({'embedding': {'dtype': 'auto'}}) != first


def test_resume_preserves_incomplete_column_role_report_and_stage_outputs(tmp_path):
    root = tmp_path / 'input'
    make_demo(root, 3, 'unrelated')
    work = tmp_path / 'work'; work.mkdir()
    data = Dataset(root, work)
    profile = read_yaml(Path(__file__).parents[1] / 'ontologies/internal_model.yaml')
    try:
        plan, _ = direct_mapping(data)
        report = {'source_snapshot': data.snapshot_id, 'candidates': [],
                  'tables': [{'table': 'demo.records', 'status': 'unresolved',
                              'unresolved_columns': ['value'], 'reason': 'model_unavailable_or_error'}],
                  'coverage': {'status': 'candidate_only', 'partial': True,
                               'model_calls_attempted': 1, 'unresolved_tables': 1}}
        data.column_role_report = report
        stage_outputs = {'fact_schema': {'field_templates': [{'id': 'template:one'}]},
                         'calculations': {'coverage': {'partial': True}},
                         'generalization': {'steps': [{'status': 'unresolved'}]}}
        path = tmp_path / 'semantic_state.json'
        save_state(path, data, profile, {'plan': plan, 'snapshot_id': data.snapshot_id,
                                        'stage_outputs': stage_outputs,
                                        'stage_signature': 'options-hash'})
        del data.column_role_report
        restored = restore_state(path, data, profile)
        assert restored['column_role_report'] == report
        assert data.column_role_report['coverage']['partial'] is True
        assert restored['stage_outputs'] == stage_outputs
        assert restored['stage_signature'] == 'options-hash'
        assert restored['column_role_report']['tables'][0]['unresolved_columns'] == ['value']
    finally:
        data.close()


def test_new_semantic_exclusion_invalidates_checkpoint_before_evidence_is_restored(tmp_path):
    root = tmp_path / 'input'
    make_demo(root, 3, 'unrelated')
    first_work = tmp_path / 'first'; first_work.mkdir()
    second_work = tmp_path / 'second'; second_work.mkdir()
    first = Dataset(root, first_work)
    table_name = sorted(first.tables)[0]
    excluded_field = table_name + '.' + first.tables[table_name]['column_names'][-1]
    second = Dataset(root, second_work, privacy_config={'exclude_columns': [excluded_field]})
    profile = read_yaml(Path(__file__).parents[1] / 'ontologies/internal_model.yaml')
    try:
        assert first.snapshot_id == second.snapshot_id
        plan, _ = direct_mapping(first)
        first.evidence['old-sensitive-evidence'] = {'raw_fragment': 'old data'}
        path = tmp_path / 'semantic_state.json'
        save_state(path, first, profile, {'plan': plan})
        with pytest.raises(ValueError, match='semantic contract'):
            restore_state(path, second, profile)
        assert 'old-sensitive-evidence' not in second.evidence
    finally:
        first.close()
        second.close()


def test_role_report_from_another_snapshot_is_rejected(tmp_path):
    root = tmp_path / 'input'
    make_demo(root, 3, 'unrelated')
    work = tmp_path / 'work'; work.mkdir()
    data = Dataset(root, work)
    profile = read_yaml(Path(__file__).parents[1] / 'ontologies/internal_model.yaml')
    try:
        plan, _ = direct_mapping(data)
        path = tmp_path / 'semantic_state.json'
        save_state(path, data, profile, {'plan': plan})
        state = json.loads(path.read_text())
        state['column_role_report'] = {'source_snapshot': 'old', 'coverage': {'partial': False}}
        path.write_text(json.dumps(state))
        with pytest.raises(ValueError, match='Column-role report'):
            restore_state(path, data, profile)
    finally:
        data.close()
