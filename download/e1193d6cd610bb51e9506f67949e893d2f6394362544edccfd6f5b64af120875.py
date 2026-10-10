"""Service subjects and quantity fields remain separate without model calls."""

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from ontology_r2.definition_subject import quantity_subject_assessment
from ontology_r2.storage import qi, read_yaml


@contextmanager
def _case(values, *, roles=None, comments=None, excluded=(), table='arbitrary.changed_source'):
    comments = comments or {}
    roles = roles or {'name': ['n'], 'description': ['d']}
    db = duckdb.connect(':memory:')
    db.execute('CREATE TABLE src (__r2_row BIGINT, ' + ', '.join(qi(c) + ' VARCHAR' for c in values) + ')')
    db.execute('INSERT INTO src VALUES (' + ','.join('?' for _ in range(len(values) + 1)) + ')',
               [1, *values.values()])

    class Recorder:
        queries = []

        def execute(self, sql, params):
            self.queries.append(sql)
            return db.execute(sql, params)

    recorder = Recorder()
    data = SimpleNamespace(db=recorder, tables={table: {
        'name': table, 'sql_name': 'src', 'semantic_excluded_columns': list(excluded),
        'columns': [{'column_name': c, 'column_comment': comments.get(c, '')} for c in values]}})
    record = {'table': table, 'record_id': 'source:1', 'row_number': 1,
              'fields': {role: [{'column': c, 'value': values[c], 'truncated': False} for c in cols]
                         for role, cols in roles.items()}}
    try:
        yield data, record, recorder
    finally:
        db.close()


def test_real_api_topic_does_not_define_the_quantity_it_queries():
    values = {'api_name': '橙子产量统计', 'api_description': '查询橙子在东南亚的产量统计结果。',
              'api_url': 'https://example.invalid/fruit/production', 'method': 'GET', 'protocol': 'HTTPS',
              'response_example': '{"avg_price": 5.2}'}
    with _case(values, roles={'name': ['api_name'], 'description': ['api_description']},
               comments={'api_name': '接口名称', 'api_description': '接口描述'}) as (data, record, _):
        result = quantity_subject_assessment(data, record, values['api_name'], values['api_description'])
    assert result['status'] == 'service_subject'
    assert result['blocks_quantity_definition'] and result['blocks_exact_record_identity']
    assert {e['value_shape'] for e in result['evidence'] if 'value_shape' in e} == {
        'service_address', 'http_method', 'service_protocol'}
    assert 'avg_price' not in json.dumps(result)


@pytest.mark.parametrize('label,description', [
    ('橙子产量统计', '查询橙子在东南亚的产量统计结果。'),
    ('Orange production', 'Retrieves orange production statistics for Southeast Asia.'),
    ('Sales overview', 'This dashboard displays sales totals by region.'),
])
def test_missing_comments_and_renamed_columns_retain_explicit_operation_purpose(label, description):
    with _case({'n': label, 'd': description, 'x': 'https://example.invalid/v1',
                'y': 'GET', 'z': 'HTTPS'}) as (data, record, _):
        result = quantity_subject_assessment(data, record, label, description)
    assert result['status'] == 'service_subject' and result['blocks_exact_record_identity']


def test_independent_definition_without_formula_is_allowed_even_in_api_named_table():
    description = '销售总额为已确认的商品销售金额，扣除取消订单。'
    with _case({'n': '销售总额', 'd': description}, table='x.market_api') as (data, record, _):
        result = quantity_subject_assessment(data, record, '销售总额', description)
    assert result['status'] == 'independent_quantity_definition'
    assert result['selected_definition_columns'] == ['d']
    assert not result['blocks_quantity_definition'] and not result['blocks_exact_record_identity']
    assert result['record_identity_supported'] is False


def test_quantity_name_starting_with_query_verb_is_not_an_operation_purpose():
    description = '查询次数为统计周期内已完成的数据查询请求数量。'
    with _case({'n': '查询次数', 'd': description}) as (data, record, _):
        result = quantity_subject_assessment(data, record, '查询次数', description)
    assert result['status'] == 'independent_quantity_definition'
    assert not result['blocks_exact_record_identity']


def test_mixed_service_record_requires_projection_of_independent_quantity_definition():
    description = '销售总额为已确认的商品销售金额，扣除取消订单。'
    # The service description is intentionally absent from the card preview;
    # the guard reads only this one safe row to check the record's subject.
    with _case({'n': '销售总额', 'd': description, 'api_description': '查询各地区的销售结果。',
                'api_url': 'https://example.invalid/sales', 'method': 'GET'}) as (data, record, _):
        result = quantity_subject_assessment(data, record, '销售总额', description)
    assert result['status'] == 'independent_quantity_definition'
    assert not result['blocks_quantity_definition']
    assert result['requires_definition_projection'] and result['blocks_exact_record_identity']
    assert 'mixed_record_requires_definition_projection' in result['reasons']
    assert result['selected_definition_columns'] == ['d']


def test_reference_url_alone_does_not_change_quantity_record_to_service():
    description = 'Sales revenue is the recognized amount from completed product sales.'
    with _case({'n': 'Sales revenue', 'd': description,
                'reference_url': 'https://example.invalid/definition?token=DO_NOT_COPY'}) as (data, record, _):
        result = quantity_subject_assessment(data, record, 'Sales revenue', description)
    assert result['status'] == 'independent_quantity_definition'
    assert not result['requires_definition_projection']
    assert 'DO_NOT_COPY' not in json.dumps(result)


def test_unknown_text_or_truncated_quote_is_not_reported_as_definition_proof():
    with _case({'n': '收入', 'd': '历史信息，含义尚未说明。'}) as (data, record, _):
        result = quantity_subject_assessment(data, record, '收入', '历史信息，含义尚未说明。')
        assert result['status'] == 'unknown' and not result['record_identity_supported']
        record['fields']['description'][0]['truncated'] = True
        incomplete = quantity_subject_assessment(data, record, '收入', '历史信息，含义尚未说明。')
        assert incomplete['status'] == 'unknown'
        assert incomplete['reasons'] == ['complete_source_definition_quote_not_available']


def test_excluded_and_authentication_fields_are_never_read_or_returned():
    description = '销售总额为已确认的商品销售金额。'
    with _case({'n': '销售总额', 'd': description, 'hidden_url': 'https://SECRET.invalid/',
                'hidden_method': 'GET', 'password': 'DO_NOT_READ', 'auth': 'DO_NOT_READ',
                'renamed_secret': 'DO_NOT_READ'},
               comments={'renamed_secret': '认证凭据'},
               excluded=('hidden_url', 'hidden_method')) as (data, record, recorder):
        result = quantity_subject_assessment(data, record, '销售总额', description)
        assert recorder.queries
        assert all(name not in recorder.queries[0] for name in (
            'hidden_url', 'hidden_method', 'password', 'auth', 'renamed_secret'))
    assert result['status'] == 'independent_quantity_definition'
    assert 'DO_NOT_READ' not in json.dumps(result) and 'SECRET' not in json.dumps(result)


def test_missing_table_metadata_stays_unknown_for_lightweight_callers():
    result = quantity_subject_assessment(SimpleNamespace(snapshot_id='s', evidence={}),
        {'table': 't', 'fields': {'description': [{'column': 'd', 'value': '销售总额为商品销售金额。'}]}},
        '销售总额', '销售总额为商品销售金额。')
    assert result['status'] == 'unknown' and result['record_identity_supported'] is False


@pytest.mark.parametrize('mixed', [False, True])
def test_metric_type_compiler_rejects_whole_service_record_even_with_quantity_field(mixed):
    from ontology_r2.group_incremental import ConceptBundleDecision, compile_concept

    label = '水果销售总额' if mixed else '橙子产量统计'
    description = '水果销售总额为已确认的商品销售金额。' if mixed else '查询橙子在东南亚的产量统计结果。'
    values = {'n': label, 'd': description, 'api_url': 'https://example.invalid/v1', 'method': 'GET'}
    if mixed:
        values['api_description'] = '查询各地区的水果销售结果。'
    with _case(values) as (data, record, _):
        data.snapshot_id, data.evidence = 'snapshot', {}
        record.update(kind='definition', scope={}, unit='')
        decision = ConceptBundleDecision(
            status='proposed', root_type='Metric', ontology_level='type', label=label,
            definition=description, classification_basis='business_driven_metric',
            classification_quote=description, business_object_quote='水果' if mixed else '橙子',
            alignments=[{'record_id': record['record_id'], 'mapping_kind': 'exact', 'quote': label}])
        profile = read_yaml(Path(__file__).parents[1] / 'ontologies/internal_model.yaml')
        with pytest.raises(ValueError) as error:
            compile_concept(data, profile, {'records': [record]}, decision, {})
        reason = ('mixed_record_requires_definition_projection' if mixed else
                  'service_or_display_topic_is_not_quantity_definition')
        assert reason in str(error.value)


@pytest.mark.parametrize('quote', ['', '水果销售总额为已确认的商品销售金额。'])
def test_service_subject_is_checked_when_classification_quote_is_empty_or_from_another_record(quote):
    with _case({'n': '水果销售总额', 'd': '查询各地区的水果销售总额。',
                'u': 'https://example.invalid/v1', 'm': 'GET'}) as (data, record, _):
        result = quantity_subject_assessment(data, record, '水果销售总额', quote)
    assert result['status'] == 'service_subject'
    assert result['blocks_exact_record_identity']
    assert result['selected_definition_columns'] == []
    assert 'service_or_display_topic_is_not_quantity_definition' in result['reasons']
    assert any(e.get('quote') == '查询各地区的水果销售总额。' for e in result['evidence'])


def test_two_exact_records_cannot_hide_service_subject_behind_quantity_definition():
    from ontology_r2.group_incremental import ConceptBundleDecision, compile_concept

    label, definition = '水果销售总额', '水果销售总额为已确认的商品销售金额。'
    with _case({'n': label, 'd': definition}, table='s.quantity') as (quantity_data, quantity, _), \
            _case({'n': label, 'd': '查询各地区的水果销售总额。'}, table='s.arbitrary_service') as (service_data, service, _):
        # Distinct records and tables, matching labels, one shared proposal.
        # Record-local visible purpose is sufficient even without URL/comments.
        data = SimpleNamespace(snapshot_id='snapshot', evidence={},
                               tables={**quantity_data.tables, **service_data.tables})
        quantity.update(record_id='quantity:1', kind='definition', scope={}, unit='')
        service.update(record_id='service:1', kind='definition', scope={}, unit='')
        decision = ConceptBundleDecision(
            status='proposed', root_type='Metric', ontology_level='type', label=label,
            definition=definition, classification_basis='business_driven_metric',
            classification_quote=definition, business_object_quote='水果',
            alignments=[{'record_id': record['record_id'], 'mapping_kind': 'exact', 'quote': label}
                        for record in (quantity, service)])
        profile = read_yaml(Path(__file__).parents[1] / 'ontologies/internal_model.yaml')
        with pytest.raises(ValueError, match='service_or_display_topic_is_not_quantity_definition'):
            compile_concept(data, profile, {'records': [quantity, service]}, decision, {})


def test_unquoted_nonservice_record_stays_unknown_without_borrowing_other_definition():
    with _case({'n': '收入', 'd': '该字段的数量含义尚未确定。'}) as (data, record, _):
        result = quantity_subject_assessment(data, record, '收入', '收入为已确认的销售金额。')
    assert result['status'] == 'unknown'
    assert not result['blocks_exact_record_identity']
    assert result['selected_definition_columns'] == []
    assert result['record_identity_supported'] is False
