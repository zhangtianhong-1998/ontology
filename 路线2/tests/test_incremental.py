import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ontology_r2.incremental import direct_mapping, merge_delta, traversal
from ontology_r2.knowledge import retrieve
from ontology_r2.llm import StructuredLLM
from ontology_r2.models import BuildPlan, Review
from ontology_r2.pipeline import build
from ontology_r2.storage import Dataset, read_yaml, write_yaml
from ontology_r2.visualization import render_viewer
from test_pipeline import setup, items


def test_rejected_deltas_keep_every_declared_field_and_original_core(tmp_path):
    config = setup(tmp_path, mcp=False)
    responses = read_yaml(config['llm']['responses'])
    responses['review'] = {'accepted': False, 'errors': ['semantic claim unsupported']}
    write_yaml(config['llm']['responses'], responses)
    output = tmp_path / 'run'
    result = asyncio.run(build(config, output))
    assert result['status'] == 'partial'
    plan = read_yaml(output / 'extraction_plan.yaml')
    for table in plan['tables']:
        original = read_yaml(Path(config['dataset']) / 'schema/tables' / (table['table'].split('.')[-1] + '.yaml'))
        assert set(table['attributes'].values()) == {c['column_name'] for c in original['columns']}
    assert plan['relations'] == []
    steps = read_yaml(output / 'construction.yaml')['steps']
    assert all(s['core_before'] == s['core_after'] for s in steps)
    assert all(len(s['attempts']) == 2 for s in steps)
    assert (output / 'viewer.html').exists()


def test_later_units_receive_accepted_core_and_judge_corrections(tmp_path, monkeypatch):
    config = setup(tmp_path, mcp=False)
    original = StructuredLLM.ask
    seen = []
    async def ask(self, task, payload, schema):
        seen.append((task, copy.deepcopy(payload)))
        if task == 'review' and payload['unit'] == 'demo.catalog':
            corrected = copy.deepcopy(payload['delta'])
            corrected['object_types'] = [{'id': 'CatalogEntry', 'parent': 'GeneralObject', 'definition': '合成定义条目', 'evidence_ids': ['schema:demo.catalog']}]
            corrected['tables'][0]['object_type'] = 'CatalogEntry'
            return Review(accepted=True, corrected_delta=BuildPlan.model_validate(corrected))
        return await original(self, task, payload, schema)
    monkeypatch.setattr(StructuredLLM, 'ask', ask)
    output = tmp_path / 'run'
    result = asyncio.run(build(config, output))
    assert result['status'] == 'complete'
    later = next(p for task, p in seen if task == 'plan' and p['unit'] == 'demo.records')
    assert any(t['id'] == 'CatalogEntry' for t in later['current_core']['object_types'])
    assert read_yaml(output / 'construction.yaml')['steps'][0]['attempts'][0]['accepted_delta']['object_types'][0]['id'] == 'CatalogEntry'


def test_corrected_delta_is_revalidated(tmp_path, monkeypatch):
    config = setup(tmp_path, mcp=False)
    original = StructuredLLM.ask
    async def ask(self, task, payload, schema):
        if task == 'review':
            corrected = copy.deepcopy(payload['delta'])
            corrected['tables'][0]['label_column'] = 'invented_column'
            return Review(accepted=True, corrected_delta=BuildPlan.model_validate(corrected))
        return await original(self, task, payload, schema)
    monkeypatch.setattr(StructuredLLM, 'ask', ask)
    output = tmp_path / 'run'
    result = asyncio.run(build(config, output))
    assert result['status'] == 'partial'
    assert all(t['label_column'] is None for t in read_yaml(output / 'extraction_plan.yaml')['tables'])


def test_declared_fk_order_and_atomic_cross_unit_rejection(tmp_path):
    config = setup(tmp_path, mcp=False)
    work = tmp_path / 'work'; work.mkdir()
    data = Dataset(config['dataset'], work)
    try:
        data.tables['demo.catalog']['foreign_keys'] = [{'referenced_schema': 'demo', 'referenced_table': 'records'}]
        assert traversal(data) == (['demo.records', 'demo.catalog'], [])
        data.tables['demo.records']['foreign_keys'] = [{'referenced_schema': 'demo', 'referenced_table': 'catalog'}]
        assert traversal(data)[1] == ['demo.catalog', 'demo.records']
        core, _ = direct_mapping(data)
        before = core.model_dump()
        delta = BuildPlan.model_validate({'tables': [{'table': 'demo.catalog', 'object_type': 'Metric', 'evidence_ids': ['schema:demo.catalog']}]})
        with pytest.raises(ValueError, match='another table'):
            merge_delta(core, delta, 'demo.records', data, read_yaml(config['model_profile']))
        assert core.model_dump() == before
    finally:
        data.close()


class FakeMCP:
    def __init__(self):
        self.calls = []
    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == 'search':
            result = {'hits': [{'id': 'b' if args['query'] == 'new-query' else 'a'}]}
        else:
            key = args['document_id']
            result = {'id': key, 'text': '定义甲' if key == 'a' else '定义乙', 'scope': '测试范围', 'version': '1'}
        return SimpleNamespace(isError=False, structuredContent=result)


def call(name, **kwargs):
    return {'calls': [{'name': name, 'input': kwargs}]}


def run_agent(tmp_path, script, **limits):
    file = tmp_path / 'responses.yaml'
    write_yaml(file, {'react_script': script})
    llm = StructuredLLM({'mode': 'mock', 'responses': str(file), 'max_calls': 10}, tmp_path)
    mcp = FakeMCP()
    result = asyncio.run(retrieve('当前字段定义', {'unit': 'demo.records', 'evidence': [{'id': 'schema:demo.records:ref'}]}, mcp, {'max_rounds': 5, **limits}, llm))
    return result, mcp.calls


def claim(doc='a', quote='定义甲', **extra):
    return {'document_id': doc, 'quote': quote, 'statement': quote, 'scope': '测试范围', 'source_evidence_ids': ['schema:demo.records:ref'], 'contribution': 'definition', **extra}


def test_native_react_rewrites_query_reads_and_summarizes(tmp_path):
    script = [call('search_documents', query='first-query'), call('read_document', document_id='a'), call('search_documents', query='new-query'), call('read_document', document_id='b'), {'summary': {'claims': [claim(), claim('b', '定义乙', polarity='contradicts')], 'stop_reason': '检索到冲突定义'}}]
    result, calls = run_agent(tmp_path, script)
    assert result['agent'] == 'agentscope.agent.Agent/ReActConfig'
    assert result['model_calls'] == 5 and result['tool_calls'] == 4
    assert result['queries'] == ['first-query', 'new-query']
    assert result['status'] == 'conflict' and len(result['claims']) == 2
    assert [c[0] for c in calls] == ['search', 'fetch', 'search', 'fetch']
    trace = (tmp_path / 'trace.jsonl').read_text()
    assert 'react_model_request' in trace and 'mcp_response' in trace


@pytest.mark.parametrize('bad_claim', [claim(quote='不在原文'), claim(doc='unread'), claim(source_evidence_ids=['schema:other:field'])])
def test_react_rejects_unread_or_unquoted_or_unrelated_claims(tmp_path, bad_claim):
    result, _ = run_agent(tmp_path, [call('search_documents', query='q'), call('read_document', document_id='a'), {'summary': {'claims': [bad_claim], 'stop_reason': 'done'}}])
    assert result['claims'] == [] and result['status'] == 'no_evidence'
    assert 'rejected_summary' in (tmp_path / 'trace.jsonl').read_text()


def test_react_tool_budget_and_unknown_tool_are_not_bypassed(tmp_path):
    script = [call('search_documents', query='q'), call('read_document', document_id='a'), call('delete_database'), {'summary': {'claims': [claim()], 'stop_reason': 'done'}}]
    result, calls = run_agent(tmp_path, script, max_tool_calls=1)
    assert result['status'] == 'budget_exhausted' and result['claims'] == []
    assert [c[0] for c in calls] == ['search']


def test_viewer_preview_is_bounded_and_source_text_cannot_inject_html(tmp_path):
    config = setup(tmp_path, mcp=False, rows=100)
    output = tmp_path / 'run'
    assert asyncio.run(build(config, output))['status'] == 'complete'
    import sqlite3
    with sqlite3.connect(output / 'work/results.sqlite') as db:
        raw = db.execute("SELECT id,body FROM items WHERE kind='objects' LIMIT 1").fetchone()
        item = json.loads(raw[1]); item['label'] = '</script><script>window.BAD=1</script>'
        db.execute("UPDATE items SET body=? WHERE kind='objects' AND id=?", (json.dumps(item), raw[0]))
    html = render_viewer(output, 10).read_text()
    data = json.loads(html.split('<script id="result-data" type="application/json">', 1)[1].split('</script>', 1)[0])
    assert len(data['objects']) <= 10 and data['counts']['objects'] == 105
    assert '</script><script>window.BAD=1</script>' not in html
    assert data['manifest']['synthetic'] is True
    assert 'https://' not in html


def test_native_react_with_agentscope_sdk_and_http_provider(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    requests = []
    actions = [('search_documents', {'query': 'first-query'}), ('read_document', {'document_id': 'a'}), ('GenerateStructuredOutput', {'claims': [claim()], 'remaining_gaps': [], 'stop_reason': '有原文支持'})]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            name, args = actions[len(requests) - 1]
            response = {'id': 'local-test', 'object': 'chat.completion', 'created': 1, 'model': 'local-test', 'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call-' + str(len(requests)), 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]}}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}
            raw = json.dumps(response).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setenv('ONTOLOGY_LLM_MODEL', 'local-test')
    monkeypatch.setenv('ONTOLOGY_LLM_BASE_URL', f'http://127.0.0.1:{server.server_port}/v1')
    monkeypatch.setenv('ONTOLOGY_LLM_API_KEY', 'local-test-secret')
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    llm = StructuredLLM({'mode': 'agentscope', 'max_calls': 5}, tmp_path)
    async def run():
        try:
            return await retrieve('定义', {'unit': 'demo.records', 'evidence': [{'id': 'schema:demo.records:ref'}]}, FakeMCP(), {'max_rounds': 3}, llm)
        finally:
            await llm.close()
    try:
        result = asyncio.run(run())
        assert result['status'] == 'useful', result
        assert len(requests) == llm.calls == 3 and llm.actual_tokens == 60
        assert {'search_documents', 'read_document', 'GenerateStructuredOutput'} == {t['function']['name'] for t in requests[0]['tools']}
        assert any(m['role'] == 'tool' for m in requests[2]['messages'])
        assert 'local-test-secret' not in (tmp_path / 'trace.jsonl').read_text()
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_23_table_build_uses_table_budget_not_row_calls(tmp_path):
    config = setup(tmp_path, mcp=False)
    root = Path(config['dataset'])
    for index in range(21):
        name = 'aux_' + str(index).zfill(2)
        for folder in ('tables', 'constraints', 'foreign_keys'):
            info = read_yaml(root / 'schema' / folder / 'catalog.yaml')
            info['table_name'] = name
            write_yaml(root / 'schema' / folder / (name + '.yaml'), info)
        (root / 'data' / (name + '.csv')).write_bytes((root / 'data/catalog.csv').read_bytes())
    config['llm']['max_calls'] = 64
    output = tmp_path / 'run'
    result = asyncio.run(build(config, output))
    assert result['status'] == 'complete', result
    assert result['input_tables'] == 23 and result['llm']['calls'] == 46
    report = read_yaml(output / 'construction.yaml')
    assert len(report['steps']) == 23 and report['direct_mapping_columns'] == 71
    assert all(s['status'] == 'accepted' for s in report['steps'])
