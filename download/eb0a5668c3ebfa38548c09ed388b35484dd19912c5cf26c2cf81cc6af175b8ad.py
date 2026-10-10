"""Viewer configuration must fail before spending the extraction budget."""
import asyncio
from pathlib import Path

import pytest

from ontology_r2 import pipeline
from ontology_r2.visualization import validate_viewer_limit


@pytest.mark.parametrize('limit', [2000, 9, True, '200', 20.5])
def test_invalid_viewer_limit_precedes_data_and_model(tmp_path, monkeypatch, limit):
    def forbidden(*args, **kwargs):
        raise AssertionError('viewer configuration must be checked first')
    monkeypatch.setattr(pipeline, 'Dataset', forbidden)
    monkeypatch.setattr(pipeline, 'StructuredLLM', forbidden)
    config = {'dataset': 'unused', 'llm': {}, 'progress': {'enabled': False},
              'visualization': {'enabled': True, 'max_nodes': limit}}
    result = asyncio.run(pipeline.build(config, tmp_path/'out'))
    assert result['status'] == 'failed'
    assert 'max_nodes' in result['error']['message']


def test_experiment_viewer_limits_are_valid():
    root = Path(__file__).resolve().parents[1]
    for name in ('runtime.fruit-templates.yaml', 'runtime.fruit-templates-smoke.yaml'):
        config = pipeline.load_config(root/'config'/name)
        validate_viewer_limit(config['visualization']['max_nodes'])
