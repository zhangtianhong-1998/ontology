"""A transformed match must survive evidence packaging, compilation and execution."""
import asyncio
import json
from pathlib import Path

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
