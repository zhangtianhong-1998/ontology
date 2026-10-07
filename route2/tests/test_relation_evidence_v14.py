"""False-positive and scheduling regressions, using local sources only."""
import asyncio
import copy
import json
import pytest
from ontology_r2.association_rules import build_association_rules, schedule_agent_candidates
from ontology_r2.configuration_relations import _relation_text_candidate
from ontology_r2.discovery import propose_candidates, validate_candidate
from ontology_r2.semantic_references import build_semantic_references, membership_declaration, verify_semantic_reference_assertion
from ontology_r2.storage import qi
from test_discovery import SmallDataset, find_candidate
from test_semantic_references import fixture


def test_unrelated_json_audit_branch_cannot_resolve_member_namespace(tmp_path):
    data, plan, concepts, alignments = fixture(tmp_path, duplicate=True)
    try:
        source = qi(data.tables['fruit.lexicon']['sql_name'])
        data.db.execute(f'UPDATE {source} SET payload=?', [json.dumps({
            'link': {'personnel_key': 'C7'}, 'audit_context': {'axis_ref': 'A3'}})])
        result = build_semantic_references(data, plan, concepts, alignments)
        assert not result['paths']
        assert result['pending'][0]['reason'] == 'member_code_missing_or_ambiguous_in_namespace'
        data.db.execute(f'UPDATE {source} SET payload=?', [json.dumps({
            'link': {'personnel_key': 'C7', 'axis_ref': 'A3'}, 'audit_context': {'axis_ref': 'A4'}})])
        result = build_semantic_references(data, plan, concepts, alignments)
        assert len(result['paths']) == 1
        assertion = copy.deepcopy(result['assertions'][0])
        assert assertion['proof']['member_namespace_binding']['source_path'] == ['link', 'axis_ref']
        assertion['proof']['member_namespace_binding']['source_path'] = ['audit_context', 'axis_ref']
        with pytest.raises(ValueError, match='namespace path'):
            verify_semantic_reference_assertion(data, assertion, result['instances'], result['plan'])
    finally:
        data.close()


@pytest.mark.parametrize('text', [
    'This is not an Employment status member.',
    'External worker is not a member of Employment status.',
    'If approved, External worker is a member of Employment status.',
    'External worker may be a member of Employment status.',
    'It is false that External worker is a member of Employment status.',
    'Employment status member: External worker. This applies only if approved.',
    '假设外聘人员属于用工类别成员', '外聘人员不是用工类别成员', '若已授权，外聘人员属于用工类别成员',
])
def test_keywords_are_not_an_affirmative_source_contract(text):
    assert not membership_declaration(text, 'Employment status', ['External worker'])
    assert not membership_declaration(text, '用工类别', ['外聘人员'])


@pytest.mark.parametrize('text,axis,label', [
    ('Employment status member: External worker', 'Employment status', 'External worker'),
    ('External worker is a member of Employment status.', 'Employment status', 'External worker'),
    ('用工类别成员：外聘人员', '用工类别', '外聘人员'),
    ('外聘人员属于用工类别的成员。', '用工类别', '外聘人员'),
])
def test_full_affirmative_declaration(text, axis, label):
    assert membership_declaration(text, axis, [label])


def test_negative_membership_stays_pending_end_to_end(tmp_path):
    data, plan, concepts, alignments = fixture(tmp_path)
    try:
        target = qi(data.tables['fruit.enumeration_rows']['sql_name'])
        data.db.execute(f'UPDATE {target} SET meaning=?', ['This is not an Employment status member.'])
        result = build_semantic_references(data, plan, concepts, alignments)
        assert not result['assertions'] and result['pending']
    finally:
        data.close()


def test_high_fanout_numeric_values_dropped_by_index_are_rechecked():
    data = SmallDataset({f'demo.{name}': (['item_code'], [('1',), ('2',)], [])
                         for name in ('source', 'target', 'noise')})
    try:
        recalled = propose_candidates(data, max_fields_per_common_value=2)
        candidate = find_candidate(recalled, 'demo.source', 'item_code', 'demo.target', 'item_code')
        assert candidate['numeric_overlap_only'] is None
        checked = validate_candidate(data, candidate)
        assert checked['checks']['numeric_overlap_only'] is True
        assert checked['checks']['numeric_matched_distinct_value_count'] == 2
        candidate['numeric_overlap_only'] = False
        report = asyncio.run(build_association_rules(data, {'candidates': [candidate], 'checks': [checked]}))
        assert report['rules'][0]['numeric_overlap_only'] is True
        assert report['rules'][0]['risk_flags'] == ['numeric_value_coincidence']
        data.db.execute('UPDATE src_1 SET item_code=\'unmatched\'')
        assert validate_candidate(data, candidate)['checks']['numeric_overlap_only'] is None
    finally:
        data.close()


def test_alias_risk_uses_matched_tokens_instead_of_whole_cell():
    data = SmallDataset({'demo.s': (['names'], [('1;2',), ('not matched',)], []),
                         'demo.t': (['code'], [('1',), ('2',)], [])})
    try:
        candidate = {'candidate_id': 'a', 'source': {'table': 'demo.s', 'field': 'names'},
                     'target': {'table': 'demo.t', 'field': 'code'}}
        checked = validate_candidate(data, candidate, transform='source_alias_items')
        assert checked['checks']['numeric_overlap_only'] is True
        assert checked['checks']['matched_distinct_value_count'] == 2
    finally:
        data.close()


def test_relation_text_after_first_16_rows_survives_reorder_and_reports_gaps():
    rows = [(str(i), '', 'L', 'R') for i in range(16)] + [('16', 'Equipment references capacity', 'L', 'R')]
    data = SmallDataset({'demo.links': (['id', 'opaque', 'left', 'right'], rows, ['id'])})
    info = data.tables['demo.links']
    info['columns'] = [{'column_name': name, 'column_comment': ''} for name in info['column_names']]
    try:
        report = {}
        assert _relation_text_candidate(data, 'demo.links', {'left', 'right'}, coverage=report) == 'opaque'
        assert not report['partial']
        data.db.execute('UPDATE src_0 SET __r2_row=100-__r2_row')
        reordered = {}
        assert _relation_text_candidate(data, 'demo.links', {'left', 'right'}, coverage=reordered) == 'opaque'
        assert report == reordered
        data.db.executemany('INSERT INTO src_0 VALUES (?,?,?,?,?)', [
            (200+i, str(200+i), 'ordinary text ' + str(i), 'L', 'R') for i in range(30)])
        limited = {}
        assert _relation_text_candidate(data, 'demo.links', {'left', 'right'}, coverage=limited) == 'opaque'
        assert limited['partial'] and limited['columns'][0]['distinct_values_not_examined'] > 0
    finally:
        data.close()


def test_agent_visits_evidence_gaps_before_zero_match_and_continues_cursor():
    candidates, checked = {}, {}
    for name, counts, channels in [
        *[(f'zero-{i}', {'eligible_references': 2, 'unique_matches': 0}, []) for i in range(12)],
        ('ambiguous', {'eligible_references': 2, 'unique_matches': 0, 'ambiguous_matches': 2}, []),
        ('partial', {'eligible_references': 2, 'unique_matches': 1, 'missing_in_input': 1}, []),
        ('conflict', {'eligible_references': 2, 'unique_matches': 0, 'missing_in_input': 2}, ['declared_fk']),
        ('unchecked', None, []),
    ]:
        candidates[name] = {'candidate_id': name, 'source': {'table': 'demo.s', 'field': name},
            'target': {'table': 'demo.t', 'field': 'key'}, 'retrieval_channels': channels,
            'numeric_overlap_only': False}
        if counts is not None:
            checked[name] = {'scan_scope': 'full_input', 'checks': counts}
    selected, report = schedule_agent_candidates(candidates, checked, 3)
    assert list(selected) == ['ambiguous', 'partial', 'conflict']
    next_, more = schedule_agent_candidates(candidates, checked, 3, report['next_cursor'])
    assert 'unchecked' in next_ and not set(selected) & set(next_)
    assert report['queues']['zero_match']['selected'] == 0
    changed = copy.deepcopy(checked)
    changed['zero-0']['checks']['ambiguous_matches'] = 2
    _, reset = schedule_agent_candidates(candidates, changed, 3, more['next_cursor'])
    assert reset['next_cursor']['signature'] != more['next_cursor']['signature']


@pytest.mark.parametrize("value", [" 1 ", ".5", "1e3", "1.0%", "-2.5E-2"])
def test_numeric_risk_covers_numeric_notation_without_changing_join_identity(value):
    data = SmallDataset({'demo.s': (['value'], [(value,)], []),
                         'demo.t': (['value'], [(value,)], [])})
    try:
        candidate = {'candidate_id': 'n', 'source': {'table': 'demo.s', 'field': 'value'},
                     'target': {'table': 'demo.t', 'field': 'value'}}
        check = validate_candidate(data, candidate)
        assert check['checks']['numeric_overlap_only'] is True
        assert check['normalization'] == 'identity'
    finally:
        data.close()


def test_alias_recall_and_exact_rule_share_numeric_risk_for_decimal_values():
    from ontology_r2.value_aliases import propose_value_alias_candidates
    from test_association_rules import Rows
    data = Rows({'demo.source': (['value'], [('1.0',)]),
                 'demo.target': (['value'], [('１.０',)])})
    try:
        recalled = propose_value_alias_candidates(data,
            fields=[('demo.source', 'value'), ('demo.target', 'value')])
        assert recalled['candidates'][0]['checks']['numeric_overlap_only'] is True
        result = asyncio.run(build_association_rules(data, {'candidates': [], 'checks': []},
            alias_candidates=recalled))
        assert result['rules'] and all(rule['numeric_overlap_only'] is True for rule in result['rules'])
        assert all(rule['verification']['checks']['numeric_evidence_scope'] ==
                   'full_input_selected_matched_keys' for rule in result['rules'])
    finally:
        data.db.close()


def test_per_table_prompt_uses_exact_risk_not_stale_candidate_hint(tmp_path, monkeypatch):
    from ontology_r2.pipeline import build
    from ontology_r2.discovery import discover_and_check
    from ontology_r2.storage import read_yaml
    from test_pipeline import setup
    config = setup(tmp_path, mcp=False)
    def stale_hint(*args, **kwargs):
        report = discover_and_check(*args, **kwargs)
        checks = {item['candidate_id']: item for item in report['checks']}
        for candidate in report['candidates']:
            checked = checks.get(candidate['candidate_id'], {}).get('checks', {})
            candidate['numeric_overlap_only'] = checked.get('numeric_overlap_only') is not True
        return report
    monkeypatch.setattr('ontology_r2.pipeline.discover_and_check', stale_hint)
    output = tmp_path / 'run'
    asyncio.run(build(config, output))
    checks = {item['candidate_id']: item['checks'].get('numeric_overlap_only')
              for item in read_yaml(output / 'association_checks.yaml')}
    trace = [json.loads(line) for line in (output / 'trace.jsonl').read_text().splitlines()]
    shown = [item for event in trace if event.get('stage') == 'llm_request'
             and event.get('task') == 'plan'
             for item in event['input'].get('field_association_evidence', [])]
    assert shown and all(item['numeric_overlap_only'] is checks[item['candidate_id']] for item in shown)


@pytest.mark.parametrize('limit,expected_calls,expected_status', [(1, 1, 'budget_exhausted'), (2, 2, 'completed')])
def test_association_agent_budget_counts_provider_retries(tmp_path, monkeypatch, limit, expected_calls, expected_status):
    from ontology_r2.llm import StructuredLLM
    from test_association_rules import Rows, candidate
    from test_transport import provider
    data = Rows({'demo.source': (['ref'], [('A',)]), 'demo.target': (['code'], [('A',)])})
    try:
        with provider(monkeypatch, lambda body, number: (
                'GenerateStructuredOutput', {'proposals': [], 'remaining_gaps': []}),
                reject=lambda body, number: 500 if number == 1 else None) as requests:
            llm = StructuredLLM({'mode': 'agentscope', 'stream': False, 'max_calls': 10,
                                 'max_retries': 1, 'max_reserved_tokens': 1000000}, tmp_path)
            async def run():
                try:
                    return await build_association_rules(data, {'candidates': [candidate()], 'checks': []},
                        {'agent_enabled': True, 'max_agent_model_calls': limit}, llm)
                finally:
                    await llm.close()
            result = asyncio.run(run())
            assert len(requests) == llm.calls == result['agent']['model_calls'] == expected_calls
            assert result['agent']['status'] == expected_status
    finally:
        data.db.close()
