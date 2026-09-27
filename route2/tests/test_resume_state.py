"""Accepted semantic state is reusable only against its evidence contract."""
import pytest
from ontology_r2.demo import make_demo
from ontology_r2.incremental import direct_mapping
from ontology_r2.semantic_state import save_state, restore_state, state_contract
from ontology_r2.storage import Dataset, read_yaml
from pathlib import Path
import json


IMPLEMENTATION_HASH = "a" * 64


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
        save_state(checkpoint, data, profile, state, implementation_code_hash=IMPLEMENTATION_HASH)
        restored = restore_state(checkpoint, data, profile, implementation_code_hash=IMPLEMENTATION_HASH)
        assert restored['plan'] == plan
        data.semantic_retrieval_contract = {'dtype': 'float32', 'model_sha256': 'new-weights'}
        with pytest.raises(ValueError, match='semantic contract'):
            restore_state(checkpoint, data, profile, implementation_code_hash=IMPLEMENTATION_HASH)
        del data.semantic_retrieval_contract
        # Old cached stages were compiled before parameter-aware type identity.
        old = json.loads(checkpoint.read_text())
        old['contract']['version'] = 2
        checkpoint.write_text(json.dumps(old))
        with pytest.raises(ValueError, match='semantic contract'):
            restore_state(checkpoint, data, profile, implementation_code_hash=IMPLEMENTATION_HASH)
        save_state(checkpoint, data, profile, state, implementation_code_hash=IMPLEMENTATION_HASH)
        monkeypatch.setenv('ONTOLOGY_LLM_MODEL', 'changed-model')
        with pytest.raises(ValueError, match='contract'):
            restore_state(checkpoint, data, profile, implementation_code_hash=IMPLEMENTATION_HASH)
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
                                        'stage_signature': 'options-hash'},
                   implementation_code_hash=IMPLEMENTATION_HASH)
        del data.column_role_report
        restored = restore_state(path, data, profile, implementation_code_hash=IMPLEMENTATION_HASH)
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
        save_state(path, first, profile, {'plan': plan}, implementation_code_hash=IMPLEMENTATION_HASH)
        with pytest.raises(ValueError, match='semantic contract'):
            restore_state(path, second, profile, implementation_code_hash=IMPLEMENTATION_HASH)
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
        save_state(path, data, profile, {'plan': plan}, implementation_code_hash=IMPLEMENTATION_HASH)
        state = json.loads(path.read_text())
        state['column_role_report'] = {'source_snapshot': 'old', 'coverage': {'partial': False}}
        path.write_text(json.dumps(state))
        with pytest.raises(ValueError, match='Column-role report'):
            restore_state(path, data, profile, implementation_code_hash=IMPLEMENTATION_HASH)
    finally:
        data.close()


@pytest.fixture
def checkpoint_case(tmp_path):
    root = tmp_path / "input"
    make_demo(root, 3, "unrelated")
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    profile = read_yaml(Path(__file__).parents[1] / "ontologies/internal_model.yaml")
    plan, _ = direct_mapping(data)
    try:
        yield data, profile, plan, tmp_path / "semantic_state.json"
    finally:
        data.close()


@pytest.mark.parametrize("checkpoint_hash, message", [
    (None, "lacks implementation_code_hash"),
    ("", "lacks implementation_code_hash"),
    ("b" * 64, "implementation_code_hash differs"),
])
def test_implementation_rejection_precedes_any_restoration(checkpoint_case, checkpoint_hash, message):
    from copy import deepcopy
    data, profile, plan, path = checkpoint_case
    save_state(path, data, profile, {"plan": plan},
               implementation_code_hash=IMPLEMENTATION_HASH)
    current_contract = state_contract(data, profile, implementation_code_hash=IMPLEMENTATION_HASH)
    saved = json.loads(path.read_text())
    assert saved["contract"] == current_contract
    if checkpoint_hash is None:
        del saved["contract"]["implementation_code_hash"]
    else:
        saved["contract"]["implementation_code_hash"] = checkpoint_hash
    # Every other contract field remains identical. None of these contents may
    # be restored merely because prompts/profile/snapshot still match.
    table = sorted(data.tables)[0]
    saved["evidence"] = {"old-acceptance": {"raw_fragment": "stale evidence"}}
    saved["column_roles"] = {table: [{"column": "old", "role": "name"}]}
    saved["column_role_report"] = {"source_snapshot": data.snapshot_id, "coverage": {"partial": False}}
    saved["plan"] = {"invalid_plan": "must never reach parsing"}
    path.write_text(json.dumps(saved))
    data.column_role_report = {"source_snapshot": data.snapshot_id, "coverage": {"partial": True}}
    before = deepcopy((data.evidence, data.tables, data.column_role_report))
    checkpoint_before = path.read_bytes()
    with pytest.raises(ValueError, match=message):
        restore_state(path, data, profile, implementation_code_hash=IMPLEMENTATION_HASH)
    assert (data.evidence, data.tables, data.column_role_report) == before
    assert path.read_bytes() == checkpoint_before


def test_same_implementation_contract_is_saved_and_reusable(checkpoint_case):
    data, profile, plan, path = checkpoint_case
    save_state(path, data, profile, {"plan": plan, "steps": [{"status": "accepted"}]},
               implementation_code_hash=IMPLEMENTATION_HASH)
    assert json.loads(path.read_text())["contract"]["implementation_code_hash"] == IMPLEMENTATION_HASH
    restored = restore_state(path.parent, data, profile, implementation_code_hash=IMPLEMENTATION_HASH)
    assert restored["plan"] == plan
    assert restored["steps"] == [{"status": "accepted"}]
    assert restore_state(None, data, profile, implementation_code_hash=IMPLEMENTATION_HASH) is None


@pytest.mark.parametrize("bad_hash", [None, "", "not-a-hash", "a" * 63, "g" * 64, 7])
def test_invalid_implementation_hash_cannot_overwrite_checkpoint(checkpoint_case, bad_hash):
    data, profile, plan, path = checkpoint_case
    path.write_text("existing checkpoint")
    before = path.read_bytes()
    for operation in (
        lambda: state_contract(data, profile, implementation_code_hash=bad_hash),
        lambda: restore_state(path, data, profile, implementation_code_hash=bad_hash),
        lambda: save_state(path, data, profile, {"plan": plan}, implementation_code_hash=bad_hash),
    ):
        with pytest.raises(ValueError, match="pipeline SHA-256"):
            operation()
    assert path.read_bytes() == before
    assert not path.with_suffix(".tmp").exists()


def test_checkpoint_implementation_hash_is_a_required_argument(checkpoint_case):
    data, profile, plan, path = checkpoint_case
    for operation in (
        lambda: state_contract(data, profile),
        lambda: restore_state(None, data, profile),
        lambda: save_state(path, data, profile, {"plan": plan}),
    ):
        with pytest.raises(TypeError, match="implementation_code_hash"):
            operation()
    assert not path.exists()


def test_pipeline_passes_its_manifest_code_hash_to_every_checkpoint_call():
    import ast
    import inspect
    from ontology_r2 import pipeline
    tree = ast.parse(inspect.getsource(pipeline.build))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id in {"restore_state", "save_state"}]
    assert sorted(node.func.id for node in calls) == ["restore_state", "save_state", "save_state"]
    for node in calls:
        argument = next(item.value for item in node.keywords if item.arg == "implementation_code_hash")
        assert isinstance(argument, ast.Name) and argument.id == "code_hash"
    # The same value is already recorded in the run manifest; no parallel
    # implementation-fingerprint algorithm is introduced by this patch.
    manifest = next(node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "manifest" for target in node.targets))
    code_hash = next(value for key, value in zip(manifest.keys, manifest.values)
                     if isinstance(key, ast.Constant) and key.value == "implementation_code_hash")
    assert isinstance(code_hash, ast.Name) and code_hash.id == "code_hash"



def test_mock_pipeline_uses_one_hash_for_restore_progress_and_final_save(tmp_path, monkeypatch):
    import asyncio
    from ontology_r2 import pipeline
    dataset = tmp_path / "input"
    make_demo(dataset, 3, "unrelated")
    config = {
        "dataset": str(dataset), "synthetic": True,
        "model_profile": str(Path(__file__).parents[1] / "ontologies/internal_model.yaml"),
        "env_file": str(tmp_path / ".env"),
        "llm": {"mode": "mock", "responses": str(dataset / "mock_llm.yaml"), "max_calls": 10},
        "mcp": {"enabled": False}, "external": {"enabled": False},
        "discovery": {"enabled": False}, "column_role_inference": {"enabled": False},
        "incremental": {"mode": "source_mapping_only"},
        "instance_bundles": {"enabled": True, "max_llm_bundles": 0, "vector_enabled": False},
        "visualization": {"enabled": False}, "progress": {"enabled": False},
    }
    calls = []
    async def never_call_model(*args, **kwargs):
        pytest.fail("Checkpoint integration must not call a model")
    monkeypatch.setattr(pipeline.StructuredLLM, "ask", never_call_model)
    def save(*args, **kwargs):
        calls.append(("save", kwargs["implementation_code_hash"]))
        return save_state(*args, **kwargs)
    def restore(*args, **kwargs):
        calls.append(("restore", kwargs["implementation_code_hash"]))
        return restore_state(*args, **kwargs)
    monkeypatch.setattr(pipeline, "save_state", save)
    monkeypatch.setattr(pipeline, "restore_state", restore)
    first = asyncio.run(pipeline.build(config, tmp_path / "first"))
    assert first["status"] in {"complete", "partial"}, first
    config["resume_from"] = str(tmp_path / "first")
    second = asyncio.run(pipeline.build(config, tmp_path / "second"))
    assert second["status"] in {"complete", "partial"}, second
    assert second["resumed_from"] == str(tmp_path / "first")
    assert first["llm"]["calls"] == second["llm"]["calls"] == 0
    expected_hash = first["implementation_code_hash"]
    assert expected_hash == second["implementation_code_hash"]
    assert calls == [("restore", expected_hash), ("save", expected_hash), ("save", expected_hash)] * 2
    for name in ("first", "second"):
        state = json.loads((tmp_path / name / "semantic_state.json").read_text())
        assert state["contract"]["implementation_code_hash"] == expected_hash
