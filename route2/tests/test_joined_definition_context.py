"""Direct verified neighbors supply context, never an extra exact-alignment seed."""
import asyncio
from pathlib import Path

import pytest

from ontology_r2.association_rules import build_association_rules
from ontology_r2.group_incremental import ConceptBundleDecision, compile_concept
from ontology_r2.instance_bundles import build_instance_bundles
from ontology_r2.joined_definition_context import JoinedDefinitionContext
from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from ontology_r2.storage import Dataset, read_yaml
from test_semantic_cards import _table

PROFILE = read_yaml(Path(__file__).resolve().parents[1] / 'ontologies/internal_model.yaml')


def _fixture(tmp_path, count=1):
    root = tmp_path / 'input'
    _table(root, 'metric_definition', {'id': '主键', 'metric_code': '编码', 'name': '名称', 'definition': '指标定义'}, [
        {'id': 'm1', 'metric_code': 'M1', 'name': '水果利润', 'definition': '水果利润按确认的收入扣减成本计算'},
    ])
    _table(root, 'binding_config', {'id': '主键', 'opaque_ref': '绑定编码', 'formula': '计算公式', 'description': '口径说明'}, [
        {'id': f'c{i}', 'opaque_ref': 'M1', 'formula': '收入-成本', 'description': '仅绑定配置指定的经营对象'}
        for i in range(count)
    ])
    work = tmp_path / 'work'
    work.mkdir()
    data = Dataset(root, work)
    built = build_semantic_cards(data, tmp_path / 'cards.sqlite')
    index = SemanticCardIndex(built['index_path'])
    associations = asyncio.run(build_association_rules(data, {'candidates': [], 'checks': []}, {
        'proposals': [{'source': {'table': 'fruit.binding_config', 'field': 'opaque_ref'},
                       'target': {'table': 'fruit.metric_definition', 'field': 'metric_code'}}]}))
    return data, index, associations


def test_reverse_unique_reference_supplies_formula_without_granting_exact_alignment(tmp_path):
    data, index, association = _fixture(tmp_path)
    try:
        result = build_instance_bundles(data, index, association, {'max_concept_bundles': 5,
            'max_relation_bundles': 0, 'max_joined_context_records': 3})
        bundle = next(b for b in result['bundles'] if b['records'][0]['table'] == 'fruit.metric_definition')
        contexts = [record for record in bundle['records'] if record.get('context_role') == 'related_context']
        assert len(contexts) == 1
        context = contexts[0]
        assert context['table'] == 'fruit.binding_config'
        assert context['fields']['formula'][0]['value'] == '收入-成本'
        assert context['fields']['formula'][0]['truncated'] is False
        assert context['connection']['direction'] == 'reverse'
        assert context['eligible_for_exact_alignment'] is False
        assert context['record_id'] not in bundle['exact_alignment_record_ids']
        assert context['card_id'] in bundle['examples']['related_context']
        decision = ConceptBundleDecision(status='proposed', root_type='GeneralObject',
            label='配置', definition='收入-成本', alignments=[{'record_id': context['record_id'],
                'mapping_kind': 'exact', 'quote': '收入-成本'}])
        with pytest.raises(ValueError, match='related_context cannot be an exact|representative record allowlist'):
            compile_concept(data, PROFILE, bundle, decision, {})
    finally:
        index.close()
        data.close()


def test_reverse_many_source_rows_are_bounded_and_omissions_reported(tmp_path):
    data, index, association = _fixture(tmp_path, count=12)
    try:
        seed = index.all_cards(100)['cards'][0]
        joined = JoinedDefinitionContext(data, index, association, [seed], max_records_per_seed=3)
        try:
            contexts, coverage = joined.for_seed(seed)
            assert [record['row_number'] for record in contexts] == [1, 2, 3]
            assert coverage['neighbor_edges_available'] == 12
            assert coverage['neighbor_edges_not_expanded'] == 9
            assert coverage['partial']
            assert joined.coverage()['scan_strategy'] == 'once_per_checked_rule_for_current_seed_window'
        finally:
            joined.close()
    finally:
        index.close()
        data.close()


def test_context_respects_selector_scope_and_transform(tmp_path):
    data, index, association = _fixture(tmp_path, count=3)
    try:
        # Recompile a checked rule selecting exactly one actual source row.
        association = asyncio.run(build_association_rules(data, {'candidates': [], 'checks': []}, {
            'proposals': [{'source': {'table': 'fruit.binding_config', 'field': 'opaque_ref'},
                          'target': {'table': 'fruit.metric_definition', 'field': 'metric_code'},
                          'selector': {'id': 'c1'}, 'transform': 'nfkc_whitespace_casefold'}]}))
        seed = index.all_cards(100)['cards'][0]
        joined = JoinedDefinitionContext(data, index, association, [seed])
        try:
            contexts, coverage = joined.for_seed(seed)
            assert [record['row_number'] for record in contexts] == [2]
            assert contexts[0]['connection']['selector'] == {'id': 'c1'}
            assert contexts[0]['connection']['transform']['operator'] == 'nfkc_whitespace_casefold'
            assert coverage['neighbor_edges_available'] == 1
        finally:
            joined.close()
    finally:
        index.close()
        data.close()


def test_scoped_context_never_crosses_same_code_in_another_tenant(tmp_path):
    root = tmp_path / 'input'
    _table(root, 'metric_definition', {'id': '主键', 'code': '编码', 'area': '适用范围',
           'name': '名称', 'definition': '定义'}, [
        {'id': 'm1', 'code': 'M1', 'area': 'CN', 'name': '中国利润', 'definition': '中国收入减去成本'},
        {'id': 'm2', 'code': 'M1', 'area': 'EU', 'name': '欧洲利润', 'definition': '欧洲收入减去成本'}])
    _table(root, 'config', {'id': '主键', 'ref': '引用编码', 'territory': '适用范围', 'formula': '计算公式'}, [
        {'id': 'c1', 'ref': ' m1 ', 'territory': 'CN', 'formula': 'cn_income-cn_cost'},
        {'id': 'c2', 'ref': 'm1', 'territory': 'EU', 'formula': 'eu_income-eu_cost'}])
    work = tmp_path / 'work'
    work.mkdir()
    data = Dataset(root, work)
    built = build_semantic_cards(data, tmp_path / 'cards.sqlite')
    index = SemanticCardIndex(built['index_path'])
    try:
        association = asyncio.run(build_association_rules(data, {'candidates': [], 'checks': []}, {
            'proposals': [{'source': {'table': 'fruit.config', 'field': 'ref'},
                          'target': {'table': 'fruit.metric_definition', 'field': 'code'},
                          'scope_bindings': {'territory': 'area'},
                          'transform': 'nfkc_whitespace_casefold'}]}))
        seeds = [card for card in index.all_cards(100)['cards'] if card['table'] == 'fruit.metric_definition']
        assert len(seeds) == 2
        joined = JoinedDefinitionContext(data, index, association, seeds)
        try:
            for seed in seeds:
                context, coverage = joined.for_seed(seed)
                assert len(context) == 1
                assert context[0]['row_number'] == seed['row_number']
                assert context[0]['connection']['scope_bindings'] == {'territory': 'area'}
                assert coverage['neighbor_edges_available'] == 1
        finally:
            joined.close()
    finally:
        index.close()
        data.close()


def test_oversized_formula_is_reported_whole_not_silently_truncated(tmp_path):
    data, index, association = _fixture(tmp_path)
    try:
        seed = index.all_cards(100)['cards'][0]
        joined = JoinedDefinitionContext(data, index, association, [seed], max_value_chars=4)
        try:
            context, coverage = joined.for_seed(seed)
            assert 'formula' not in context[0]['fields']
            assert any(item['column'] == 'formula' for item in context[0]['omitted_fields'])
            assert coverage['fields_not_expanded'] > 0
            assert coverage['partial']
        finally:
            joined.close()
    finally:
        index.close()
        data.close()
