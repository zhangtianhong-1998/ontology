"""A transformed match must survive evidence packaging, compilation and execution."""
import asyncio
import json
from pathlib import Path
import pytest

from ontology_r2.association_rules import build_association_rules
from ontology_r2.group_incremental import RelationBundleDecision, compile_relation
from ontology_r2.incremental import direct_mapping
from ontology_r2.instance_bundles import _limits, _relation_bundle
from ontology_r2.relations import Extractor
from ontology_r2.storage import Dataset, Sink, read_yaml
from ontology_r2.value_aliases import propose_value_alias_candidates
from test_semantic_cards import _table

PROFILE = read_yaml(Path(__file__).resolve().parents[1] / 'ontologies/internal_model.yaml')


class NoLLM:
    async def ask(self, *args, **kwargs):
        raise AssertionError('No per-record model call is allowed')


def test_alias_to_checked_rule_to_witness_bundle_to_actual_assertion(tmp_path):
    root = tmp_path / 'input'
    _table(root, 'endpoint', {'id': '主键', 'name': '参数名称', 'ref_value': '引用名称',
                            'definition': '引用用途定义', 'kind': '应用类型'}, [
        {'id': 's1', 'name': '金额输出', 'ref_value': ' SALES ', 'definition': '金额输出引用销售额定义', 'kind': '甲'},
        {'id': 's2', 'name': '其他输出', 'ref_value': 'sales', 'definition': '其他输出用途尚未核实', 'kind': '乙'},
    ])
    _table(root, 'dictionary', {'id': '主键', 'name': '标准名称', 'aliases': '同义名',
                              'definition': '标准定义'}, [
        {'id': 't1', 'name': '销售额', 'aliases': '营收；sales', 'definition': '销售额定义为已确认的商品销售总金额'},
    ])
    work = tmp_path / 'work'
    work.mkdir()
    data = Dataset(root, work)
    output = tmp_path / 'out'
    (output / 'work').mkdir(parents=True)
    sink = Sink(output)
    try:
        aliases = propose_value_alias_candidates(data, fields=[
            ('fruit.endpoint', 'ref_value'), ('fruit.dictionary', 'aliases')])
        rules = asyncio.run(build_association_rules(data, {'candidates': [], 'checks': []},
                                                   alias_candidates=aliases))
        rule = next(r for r in rules['rules'] if r['source']['table'] == 'fruit.endpoint')
        assert rule['status'] == 'checked_technical'
        assert rule['transform']['operator'] == 'target_alias_items'
        bundle, reason = _relation_bundle(data, rule, _limits({}))
        assert reason is None
        positive = bundle['examples']['positive'][0]
        assert positive['source_raw_value'] == ' SALES '
        assert positive['target_raw_value'] == '营收；sales'
        core, _ = direct_mapping(data)
        decision = RelationBundleDecision(status='proposed', parent_relation='points_to',
            predicate_name='definition_reference', label='definition_reference',
            definition='输出参数引用标准销售额定义',
            source_quote='金额输出引用销售额定义', target_quote='销售额定义为已确认的商品销售总金额')
        plan, relation = compile_relation(data, PROFILE, core, bundle, decision)
        assert relation.transform == {'operator': 'target_alias_items'}
        expected_source = positive['source_record_id']
        expected_target = positive['target_record_id']
        stats = asyncio.run(Extractor(data, sink, plan, NoLLM(), {}).execute())
        assertions = [json.loads(row[0]) for row in sink.db.execute("SELECT body FROM items WHERE kind='assertions'")]
        assertions = [item for item in assertions if item['predicate'] == relation.predicate]
        assert stats['accepted'] == 1
        assert len(assertions) == 1
        assert assertions[0]['subject'] == expected_source
        assert assertions[0]['object'] == expected_target
        assert assertions[0]['decision']['method'] == 'checked_transform:target_alias_items'
        assert stats['plans'][0]['outside_witness'] == 1
    finally:
        sink.db.close()
        data.close()


@pytest.mark.parametrize('transform', ['identity', 'target_alias_items'])
def test_observed_subset_emits_only_reviewed_unique_pair_with_scope_and_selector(tmp_path, transform):
    """Missing, ambiguous, out-of-scope and merely unreviewed rows remain unasserted."""
    root = tmp_path / 'input'
    a, b = ('a', 'b') if transform == 'identity' else (' ALPHA ', ' BETA ')
    ta, tb = ('a', 'b') if transform == 'identity' else ('a;alpha', 'b;beta')
    _table(root, 'endpoint', {'id': '主键', 'name': '参数名称', 'ref_value': '引用编码',
                            'definition': '参数定义', 'kind': '应用类型', 'area': '地区范围'}, [
        {'id': 's1', 'name': '金额输出', 'ref_value': a, 'definition': '金额输出引用销售额定义', 'kind': 'yes', 'area': 'CN'},
        {'id': 's2', 'name': '歧义输出', 'ref_value': b, 'definition': '歧义目标未经语义审核', 'kind': 'yes', 'area': 'CN'},
        {'id': 's3', 'name': '缺失输出', 'ref_value': 'missing', 'definition': '目标不存在', 'kind': 'yes', 'area': 'CN'},
        {'id': 's4', 'name': '未审输出', 'ref_value': a, 'definition': '唯一匹配但未审查语义', 'kind': 'yes', 'area': 'CN'},
        {'id': 's5', 'name': '条件外输出', 'ref_value': a, 'definition': '条件外记录', 'kind': 'no', 'area': 'CN'},
        {'id': 's6', 'name': '范围缺失输出', 'ref_value': a, 'definition': '缺少范围', 'kind': 'yes', 'area': ''},
        {'id': 's7', 'name': '其他地区输出', 'ref_value': a, 'definition': '其他地区未经审核', 'kind': 'yes', 'area': 'EU'},
    ])
    _table(root, 'dictionary', {'id': '主键', 'name': '标准名称', 'code': '标准编码',
                              'definition': '标准定义', 'market': '地区范围'}, [
        {'id': 't1', 'name': '销售额', 'code': ta, 'definition': '销售额定义为已确认的商品销售总金额', 'market': 'CN'},
        {'id': 't2', 'name': '歧义甲', 'code': tb, 'definition': '同码定义甲', 'market': 'CN'},
        {'id': 't3', 'name': '歧义乙', 'code': tb, 'definition': '同码定义乙', 'market': 'CN'},
        {'id': 't4', 'name': '欧洲销售额', 'code': ta, 'definition': '欧洲范围销售额定义', 'market': 'EU'},
    ])
    work = tmp_path / 'work'; work.mkdir()
    data = Dataset(root, work)
    try:
        found = asyncio.run(build_association_rules(data, {'candidates': [], 'checks': []}, {
            'proposals': [{'source': {'table': 'fruit.endpoint', 'field': 'ref_value'},
                           'target': {'table': 'fruit.dictionary', 'field': 'code'},
                           'selector': {'kind': 'yes'}, 'scope_bindings': {'area': 'market'},
                           'transform': transform}]}))
        rule = found['rules'][0]
        assert rule['status'] == 'observed_subset'
        counts = rule['verification']['checks']
        assert (counts['eligible_references'], counts['unique_matches'],
                counts['ambiguous_matches'], counts['missing_in_input'], counts['missing_scope']) == (5, 3, 1, 1, 1)
        bundle, reason = _relation_bundle(data, rule, _limits({}))
        assert reason is None
        assert bundle['rule']['verification']['checks'] == counts
        assert bundle['technical_coverage']['unique_matches_not_in_bundle'] == 1
        assert bundle['technical_coverage']['all_unique_matches_semantically_accepted'] is False
        assert bundle['examples']['counterexamples']
        core, _ = direct_mapping(data)
        decision = RelationBundleDecision(status='proposed', parent_relation='points_to',
            predicate_name='definition_reference', label='points to',
            definition='此输出参数引用标准销售额定义',
            source_quote='金额输出引用销售额定义', target_quote='销售额定义为已确认的商品销售总金额')
        compiled, plan = compile_relation(data, PROFILE, core, bundle, decision)
        assert compiled.relation_types[0].label == 'definition_reference'
        assert plan.selector.field == 'kind' and plan.selector.value == 'yes'
        assert plan.scope_bindings == {'area': 'market'}
        assert len(plan.witnessed_pairs) == 1
        source_rows, target_rows = list(data.rows('fruit.endpoint')), list(data.rows('fruit.dictionary'))

        def execute(test_plan, directory):
            output = tmp_path / directory; (output / 'work').mkdir(parents=True)
            sink = Sink(output)
            try:
                stats = asyncio.run(Extractor(data, sink, test_plan, NoLLM(), {}).execute())
                edges = [json.loads(row[0]) for row in sink.db.execute("SELECT body FROM items WHERE kind='assertions'")]
                edges = [edge for edge in edges if edge['predicate'] == plan.predicate]
                return stats, edges
            finally:
                sink.db.close()

        stats, edges = execute(compiled, 'accepted')
        assert stats['accepted'] == len(edges) == 1
        assert edges[0]['subject'] == data.record_id('fruit.endpoint', source_rows[0])
        assert edges[0]['object'] == data.record_id('fruit.dictionary', target_rows[0])
        assert stats['plans'][0]['outside_witness'] == 6
        # Even a corrupted persisted witness cannot bypass the executor's
        # target uniqueness, selector, scope or exact reviewed-target checks.
        for position in (1, 2, 4, 5, 6):
            wrong = compiled.model_copy(deep=True)
            pair = wrong.relations[0].witnessed_pairs[0]
            pair.source_record_id = data.record_id('fruit.endpoint', source_rows[position])
            pair.target_record_id = data.record_id('fruit.dictionary', target_rows[1 if position == 1 else 0])
            stats, edges = execute(wrong, f'rejected-{position}')
            assert stats['accepted'] == len(edges) == 0
    finally:
        data.close()
