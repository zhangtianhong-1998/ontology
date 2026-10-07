"""Offline V14 compiler and cross-snapshot fact-mapping acceptance.

All semantic proposals are deterministic local fixtures. This is an engineering
acceptance exercise, not a model quality or unannotated-dataset benchmark.
"""
import argparse
import asyncio
import csv
import json
from pathlib import Path
from ontology_r2.configuration_relation_stage import ConfigurationRelationDecision, compile_configuration_relation
from ontology_r2.configuration_relations import discover_configuration_relations
from ontology_r2.cross_snapshot_reuse import export_reuse_bundle, reuse_on_snapshot
from ontology_r2.fact_observations import build_fact_observation_candidates
from ontology_r2.fact_schema_induction import induce_fact_schema
from ontology_r2.fact_type_binding import bind_fact_observations
from ontology_r2.group_incremental import ConceptBundleDecision, compile_concept, _compiled_object_type
from ontology_r2.models import BuildPlan
from ontology_r2.storage import Dataset, read_yaml, write_yaml
PROFILE = read_yaml(Path(__file__).resolve().parents[1] / 'ontologies/internal_model.yaml')

def _table(root, name, columns, rows):
    base = {'schema': 'energy', 'table_name': name}
    write_yaml(root / 'schema/tables' / (name + '.yaml'), {**base,
        'table_comment': '设备经营定义' if 'definition' in name else '按设备地区和期间记录的业务事实',
        'columns': [{'column_name': key, 'column_comment': value, 'data_type': 'text',
                     'is_not_null': False, 'ordinal_position': number}
                    for number, (key, value) in enumerate(columns.items(), 1)]})
    write_yaml(root / 'schema/constraints' / (name + '.yaml'), {**base,
        'constraints': [{'definition': 'PRIMARY KEY (id)', 'constraint_type': 'p'}]})
    write_yaml(root / 'schema/foreign_keys' / (name + '.yaml'), {**base, 'foreign_keys': []})
    (root / 'data').mkdir(exist_ok=True)
    with (root / 'data' / (name + '.csv')).open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _input(tmp_path, *, count=2, tenant='A', changed_definition=False, include_confuser=False):
    root = tmp_path / 'input'
    definitions = [
        {'id': 'Q1', 'name': '设备发电量', 'definition': '设备发电量衡量设备发出的电能总量', 'unit': 'kWh', 'tenant': 'A'},
        {'id': 'Q2', 'name': '发出电能', 'definition': '发出电能是可用于不同业务对象的电能总量', 'unit': 'kWh', 'tenant': 'A'},
        {'id': 'Q3', 'name': '设备', 'definition': '设备是发出电能的经营对象', 'unit': '', 'tenant': 'A'},
        {'id': 'Q4', 'name': '地区', 'definition': '地区划分设备的地理位置', 'unit': '', 'tenant': 'A'},
    ]
    if include_confuser:
        definitions.append({'id': 'Q5', 'name': '耗电量',
            'definition': '耗电量是可用于不同业务对象的电能消耗量', 'unit': 'kWh', 'tenant': 'A'})
    if changed_definition: definitions[0]['definition'] += '，仅限自有设备'
    _table(root, 'quantity_definition', {'id': '定义 ID', 'name': '名称', 'definition': '完整定义',
                                        'unit': '计量单位', 'tenant': '适用租户'}, definitions)
    _table(root, 'relation_definition', {'id': '关系 ID', 'source_ref': '源定义编码',
        'target_ref': '目标定义编码', 'relation_text': '配置关系声明'}, [
        {'id': 'R1', 'source_ref': 'Q1', 'target_ref': 'Q2', 'relation_text': '设备发电量依赖发出电能'},
        {'id': 'R2', 'source_ref': 'Q1', 'target_ref': 'Q3', 'relation_text': '设备发电量关联设备'}])
    rows = [{'id': str(i + 1), 'device_code': 'GEN-' + str(i + 1),
             'region_code': ('NORTH', 'SOUTH', 'WEST')[i % 3], 'period': '2026Q' + str(i + 1),
             'tenant': tenant, 'generation_kwh': str(100 + i * 20)} for i in range(count)]
    _table(root, 'generation_fact', {'id': '记录 ID', 'device_code': '设备编码', 'region_code': '地区编码',
                                   'period': '会计期', 'tenant': '适用租户',
                                   'generation_kwh': '设备发电量指标值，单位 kWh'}, rows)
    work = tmp_path / 'work'
    work.mkdir(parents=True)
    return Dataset(root, work)


def _compile_definition_plan(data):
    concepts, alignments, plan = [], [], BuildPlan()
    roots = {'Q1': 'Metric', 'Q2': 'Measure', 'Q3': 'GeneralObject', 'Q4': 'Dimension', 'Q5': 'Measure'}
    for row in data.rows('energy.quantity_definition'):
        record = {'record_id': data.record_id('energy.quantity_definition', row),
            'table': 'energy.quantity_definition', 'row_number': row['__r2_row'], 'kind': 'definition',
            'scope': {'tenant': 'A'}, 'unit': row['unit'], 'fields': {
                role: [{'column': column, 'value': row[column]}]
                for role, column in [('name', 'name'), ('description', 'definition'),
                                     ('unit', 'unit'), ('scope', 'tenant')]}}
        root = roots[row['id']]
        decision = ConceptBundleDecision(status='proposed', ontology_level='type',
            root_type=root, label=row['name'], definition=row['definition'],
            scope={'tenant': 'A'}, scope_roles={'tenant': 'applicability'},
            classification_basis=('business_driven_metric' if root == 'Metric' else
                                  'reusable_measure' if root == 'Measure' else 'other'),
            classification_quote=row['definition'] if root in ('Metric', 'Measure') else '',
            business_object_quote='设备' if root == 'Metric' else '',
            alignments=[{'record_id': record['record_id'], 'mapping_kind': 'exact', 'quote': row['definition']}])
        concept, mappings = compile_concept(data, PROFILE, {'records': [record]}, decision, {})
        concepts.append(concept)
        alignments.extend(mappings)
        plan.object_types.append(_compiled_object_type(concept))
    spec = {'spec_id': 'quantity-definition-link', 'configuration_table': 'energy.relation_definition',
        'source_code_column': 'source_ref', 'target_code_column': 'target_ref',
        'source_definition_table': 'energy.quantity_definition', 'source_definition_code_column': 'id',
        'target_definition_table': 'energy.quantity_definition', 'target_definition_code_column': 'id',
        'relation_text_column': 'relation_text'}
    discovered = discover_configuration_relations(data, plan, concepts, alignments, [spec])
    assertions = []
    for candidate in discovered['candidates']:
        assert candidate['status'] == 'endpoint_verified_candidate'
        source = next(item for item in plan.object_types if item.id == candidate['source_type_id'])
        target = next(item for item in plan.object_types if item.id == candidate['target_type_id'])
        relation = 'depends_on' if target.parent == 'Measure' else 'related_to'
        decision = ConfigurationRelationDecision(status='proposed', direction='source_to_target',
            parent_relation=relation, label=relation, definition=candidate['predicate_literal'],
            configuration_quote=candidate['predicate_literal'], source_definition_quote=source.definition,
            target_definition_quote=target.definition)
        plan, assertion = compile_configuration_relation(data, PROFILE, plan, candidate, decision, concepts, alignments)
        assertions.append(assertion)
    return plan, assertions


class _BindingModel:
    def __init__(self): self.calls = []
    async def ask(self, task, payload, schema):
        assert task == 'fact_type_binding'
        self.calls.append(task)
        item = next(value for value in payload['type_candidates'] if value['label'] == '设备发电量')
        evidence = next(value for value in item['full_source_definitions'] if value['role'] == 'description')
        return schema.model_validate({'status': 'bind', 'type_id': item['id'],
            'source_column_quote': payload['source_declaration'], 'type_definition_quote': item['definition'],
            'type_source_quote': evidence['value'], 'source_definition_evidence_id': evidence['evidence_id'],
            'scope_bindings': {'device': 'device_code', 'region': 'region_code', 'period': 'period'},
            'constant_scope_bindings': {'tenant': 'tenant'}})


class _NoModel:
    async def ask(self, *args, **kwargs): raise AssertionError('No LLM call is permitted')




class _SchemaModel:
    def __init__(self): self.calls = []
    async def ask(self, task, payload, schema):
        if task != 'fact_schema_induction':
            raise AssertionError('Unexpected model task')
        self.calls.append(task)
        declaration = payload['source_declaration']
        return schema.model_validate({'status': 'proposed', 'label': '设备发电量',
            'identity_evidence_id': declaration['evidence_id'], 'identity_quote': declaration['value'],
            'business_object_quote': '设备', 'quantity_quote': '发电量',
            'unit': 'kWh', 'unit_quote': declaration['value']})


def _save_binding(output, data, plan, observations, bound):
    write_yaml(output / 'extraction_plan.yaml', plan.model_dump())
    write_yaml(output / 'fact_observations.yaml', observations)
    write_yaml(output / 'business_fact_instances.yaml', bound['instances'])
    write_yaml(output / 'business_fact_field_bindings.yaml', bound['field_bindings'])
    write_yaml(output / 'business_fact_binding_coverage.yaml', bound['coverage'])
    write_yaml(output / 'evidence.yaml', list(data.evidence.values()))


def run_correctness(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    first = _input(output / 'initial')
    try:
        plan, relations = _compile_definition_plan(first)
        observed = build_fact_observation_candidates(first)
        model = _BindingModel()
        bound = asyncio.run(bind_fact_observations(first, observed, plan, model))
        assert len(plan.object_types) == 4 and len(relations) == 2
        assert len(bound['instances']) == 2
        assert all(set(row['binding_decision']['bound_scope_values']) >= {'device', 'region', 'period'}
                   and row['unit'] == 'kWh' and row['source_ref']['record_ids']
                   and all(eid in first.evidence for eid in row['evidence_ids']) for row in bound['instances'])
        _save_binding(output / 'initial', first, plan, observed, bound)
        write_yaml(output / 'initial/configuration_relation_assertions.yaml', relations)
        bundle = export_reuse_bundle(first, plan, field_bindings=bound['field_bindings'], fact_observations=observed)
        write_yaml(output / 'ontology_reuse_bundle.yaml', bundle)
        report = {'validation_kind': 'offline_mock_decisions_and_real_source_compilers',
            'external_llm_calls': 0, 'initial': {'types': len(plan.object_types), 'relations': len(relations),
                'facts': len(bound['instances']), 'mock_field_decisions': len(model.calls)},
            'source_snapshot': first.snapshot_id, 'scenarios': {}}
        for name, options in [('append', {'count': 3}), ('bounded_append', {'count': 3}), ('scope_change', {'count': 3, 'tenant': 'B'}),
                              ('definition_change', {'count': 3, 'changed_definition': True})]:
            current = _input(output / name, **options)
            try:
                observations = build_fact_observation_candidates(current, **({'max_candidates_per_table': 1}
                    if name == 'bounded_append' else {}))
                replay = asyncio.run(reuse_on_snapshot(current, observations, bundle))
                expected = 3 if name == 'append' else 1 if name == 'bounded_append' else 0
                assert replay['coverage']['instances_created'] == expected
                assert replay['coverage']['model_calls'] == 0
                if name == 'bounded_append':
                    assert replay['coverage']['fact_observation_coverage']['omitted_observed_tuples'] == 2
                    assert replay['coverage']['partial']
                    assert 'fact_observation_coverage_partial' in replay['coverage']['partial_reasons']
                if name == 'append':
                    assert {row['id'] for row in bound['instances']}.isdisjoint(
                        row['id'] for row in replay['fact_binding']['instances'])
                    assert all(row['source_ref']['snapshot_id'] == current.snapshot_id
                        and all(eid in current.evidence for eid in row['evidence_ids'])
                        for row in replay['fact_binding']['instances'])
                _save_binding(output / name, current, replay['plan'], observations, replay['fact_binding'])
                write_yaml(output / name / 'reuse_pending.yaml', replay['pending'])
                write_yaml(output / name / 'reuse_coverage.yaml', replay['coverage'])
                report['scenarios'][name] = {'facts': expected, 'external_llm_calls': 0,
                    'revalidated_types': replay['coverage']['revalidated_types'],
                    'pending_count': len(replay['pending']),
                    'pending_reasons': sorted({item.get('reason', '') for item in replay['pending']}),
                    'snapshot': current.snapshot_id, 'partial': replay['coverage']['partial'],
                    'omitted_observed_tuples': replay['coverage']['fact_observation_coverage']['omitted_observed_tuples'],
                    'partial_reasons': replay['coverage']['partial_reasons']}
            finally:
                current.close()
        data = _input(output / 'lexical_confuser', include_confuser=True)
        try:
            old, _ = _compile_definition_plan(data)
            confuser = BuildPlan(object_types=[item for item in old.object_types if item.label == '耗电量'])
            observed = build_fact_observation_candidates(data)
            outcomes = []
            for name, core in [('empty_core', BuildPlan()), ('nearby_existing_type', confuser)]:
                llm = _SchemaModel()
                schema = asyncio.run(induce_fact_schema(data, observed, core, llm))
                facts = asyncio.run(bind_fact_observations(data, observed, schema['plan'], _NoModel(),
                                                           field_templates=schema['field_templates']))
                outcomes.append(len(facts['instances']))
                report['scenarios'][name] = {'facts': len(facts['instances']),
                    'mock_schema_decisions': len(llm.calls), 'external_llm_calls': 0}
                _save_binding(output / 'lexical_confuser' / name, data, schema['plan'], observed, facts)
                write_yaml(output / 'lexical_confuser' / name / 'schema_steps.yaml', schema['steps'])
            assert outcomes == [2, 2]
        finally:
            data.close()
        report['status'] = 'engineering_acceptance_passed'
        report['limits'] = [
            'No external LLM or embedding model was invoked; this does not validate model extraction quality.',
            'Reused relation schemas require separate compiler replay and remain pending, not executable.',
            'Fact coordinates retain observed source values; this does not resolve durable object identities.',
            'Observed values are source values; no numerical formula evaluation was performed.',
            'Unannotated or arbitrary enterprise schemas remain a separate generalization evaluation.']
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return report
    finally:
        first.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='New directory; existing results are never overwritten')
    args = parser.parse_args()
    print(json.dumps(run_correctness(args.output), ensure_ascii=False, indent=2))
