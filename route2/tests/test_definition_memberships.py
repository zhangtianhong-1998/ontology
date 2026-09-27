"""Template reuse must not turn reference variants into same-as assertions."""
from copy import deepcopy
from pathlib import Path

from ontology_r2.definition_memberships import build_definition_memberships
from ontology_r2.group_incremental import ConceptBundleDecision, compile_concept
from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from ontology_r2.storage import Dataset, read_yaml
from test_semantic_cards import _table

PROFILE = read_yaml(Path(__file__).resolve().parents[1] / "ontologies/internal_model.yaml")


def _fixture(tmp_path):
    root = tmp_path / 'input'
    _table(root, 'measure_definition', {
        'id': '记录主键', 'measure_name': '度量名称', 'definition': '度量定义',
        'formula': '计算公式', 'scope': '适用范围', 'unit': '单位', 'reference_code': '引用编码',
    }, [
        {'id': '1', 'measure_name': '收入', 'definition': '可复用的收入总额',
         'formula': 'sum(amount)', 'scope': '全年', 'unit': '元', 'reference_code': 'A'},
        {'id': '2', 'measure_name': '收入', 'definition': '可复用的收入总额',
         'formula': 'sum(amount)', 'scope': '全年', 'unit': '元', 'reference_code': 'B'},
        {'id': '3', 'measure_name': '收入', 'definition': '可复用的收入总额',
         'formula': 'sum(amount)', 'scope': '全年', 'unit': '元', 'reference_code': 'A'},
        {'id': '4', 'measure_name': '收入', 'definition': '可复用的收入总额',
         'formula': 'avg(amount)', 'scope': '全年', 'unit': '元', 'reference_code': 'A'},
        {'id': '5', 'measure_name': '收入', 'definition': '可复用的收入总额',
         'formula': 'sum(amount)', 'scope': '季度', 'unit': '元', 'reference_code': 'A'},
    ])
    work = tmp_path / 'work'
    work.mkdir()
    data = Dataset(root, work)
    built = build_semantic_cards(data, tmp_path / 'cards.sqlite')
    index = SemanticCardIndex(built['index_path'])
    card = next(c for c in index.all_cards(100)['cards'] if c['row_number'] == 1)
    decision = ConceptBundleDecision(status='proposed', label='收入', definition='可复用的收入总额',
        root_type='Measure', ontology_level='type', scope={'scope': '全年'},
        scope_roles={'scope': 'applicability'}, classification_basis='reusable_measure',
        classification_quote='可复用的收入总额',
        alignments=[{'record_id': card['record_id'], 'mapping_kind': 'exact', 'quote': '可复用的收入总额'}])
    concept, alignments = compile_concept(data, PROFILE, {'records': [card]}, decision, {})
    group = {'snapshot_id': data.snapshot_id, 'concepts': [concept], 'record_alignments': alignments}
    return data, index, group


def test_full_value_template_reuses_type_without_entity_merge_or_relation_propagation(tmp_path):
    data, index, group = _fixture(tmp_path)
    try:
        before = deepcopy(group)
        result = build_definition_memberships(data, index, group)
        assert group == before
        assert result['coverage']['llm_calls'] == 0
        assert len(result['templates']) == 1
        members = result['memberships']
        assert {item['row_number'] for item in members} == {1, 2, 3}
        assert len({item['record_id'] for item in members}) == 3
        assert len({item['type_id'] for item in members}) == 1
        assert all(not item['entity_identity_claim'] and not item['relationship_inheritance'] for item in members)
        assert all(item['mapping_kind'] == 'shares_definition_type_template' for item in members)
        assert result['reference_variants_pending'][0]['row_number'] == 2
        assert result['reference_variants_pending'][0]['reference_values'] == {'reference_code': 'B'}
        # Formula/scope differences form separate patterns and have no accepted template.
        assert result['coverage']['records_without_accepted_template'] == 2
        assert result['coverage']['partial']
    finally:
        index.close()
        data.close()


def test_conflicting_exact_alignment_is_not_overwritten(tmp_path):
    data, index, group = _fixture(tmp_path)
    try:
        row2 = next(row for row in data.rows('fruit.measure_definition') if row['__r2_row'] == 2)
        record_id = data.record_id('fruit.measure_definition', row2)
        group['record_alignments'].append({'source_record_id': record_id, 'concept_id': 'other-concept',
                                           'mapping_kind': 'exact'})
        result = build_definition_memberships(data, index, group)
        assert record_id not in {item['record_id'] for item in result['memberships']}
        assert any(item['record_id'] == record_id and item['reason'] == 'conflicting_exact_alignment'
                   for item in result['rejections'])
    finally:
        index.close()
        data.close()


def test_live_full_source_change_cannot_hide_behind_same_pattern_or_preview(tmp_path):
    data, index, group = _fixture(tmp_path)
    try:
        sql = data.tables['fruit.measure_definition']['sql_name']
        data.db.execute(f'UPDATE "{sql}" SET formula=? WHERE __r2_row=2', ['sum(amount)-tax'])
        result = build_definition_memberships(data, index, group)
        assert {item['row_number'] for item in result['memberships']} == {1, 3}
        assert result['rejections'][0]['reason'] == 'full_semantic_values_differ'
    finally:
        index.close()
        data.close()


def test_record_budget_reports_remaining_source_records(tmp_path):
    data, index, group = _fixture(tmp_path)
    try:
        result = build_definition_memberships(data, index, group, max_records=1)
        assert result['coverage']['records_scanned'] == 1
        assert result['coverage']['records_not_scanned_due_to_limit'] == 2
        assert result['coverage']['partial']
        assert result['coverage']['reference_linkage_complete'] is False
        assert result['coverage']['reference_inspection_complete'] is False
    finally:
        index.close()
        data.close()


def test_zero_record_budget_never_claims_reference_inspection_complete(tmp_path):
    data, index, group = _fixture(tmp_path)
    try:
        result = build_definition_memberships(data, index, group, max_records=0)
        assert result['reference_variants_pending'] == []
        assert result['coverage']['records_scanned'] == 0
        assert result['coverage']['records_not_scanned_due_to_limit'] == 3
        assert result['coverage']['reference_linkage_complete'] is False
        assert result['coverage']['reference_inspection_complete'] is False
        assert result['coverage']['type_membership_complete'] is False
    finally:
        index.close()
        data.close()


def test_partial_source_index_remains_incomplete_even_with_no_pending_variants(tmp_path):
    data, index, group = _fixture(tmp_path)
    try:
        # Limit this index to the exact representative; all inspected values
        # match, but an upstream cap omitted four records.
        index.db.execute('DELETE FROM card_sources WHERE row_number != 1')
        index.coverage.update(rows_omitted_cap=4, partial=True)
        result = build_definition_memberships(data, index, group)
        assert result['coverage']['memberships_emitted'] == 1
        assert result['coverage']['reference_variants_pending'] == 0
        assert result['coverage']['source_index_partial']
        assert result['coverage']['reference_linkage_complete'] is False
        assert result['coverage']['type_membership_complete'] is False
        assert result['coverage']['partial']
    finally:
        index.close()
        data.close()


def test_complete_inspection_is_only_about_variants_not_business_relations(tmp_path):
    data, index, group = _fixture(tmp_path)
    try:
        index.db.execute('DELETE FROM card_sources WHERE row_number != 1')
        index.coverage.update(rows_omitted_cap=0, partial=False)
        result = build_definition_memberships(data, index, group)
        assert result['coverage']['reference_inspection_complete']
        assert result['coverage']['reference_linkage_complete']
        assert 'does not certify business relations' in result['coverage']['reference_linkage_scope']
        assert all(not member['relationship_inheritance'] for member in result['memberships'])
    finally:
        index.close()
        data.close()


def test_only_matching_labels_are_insufficient_to_compile_a_reusable_template(tmp_path):
    root = tmp_path / 'input'
    _table(root, 'object_definition', {'id': '', 'name': '对象名称', 'ref_code': '引用编码'}, [
        {'id': '1', 'name': '收入', 'ref_code': 'A'}, {'id': '2', 'name': '收入', 'ref_code': 'B'},
    ])
    work = tmp_path / 'work'
    work.mkdir()
    data = Dataset(root, work)
    built = build_semantic_cards(data, tmp_path / 'cards.sqlite')
    index = SemanticCardIndex(built['index_path'])
    try:
        card = index.all_cards(100)['cards'][0]
        decision = ConceptBundleDecision(status='proposed', label='收入', definition='收入',
            root_type='GeneralObject', ontology_level='type', classification_basis='other',
            alignments=[{'record_id': card['record_id'], 'mapping_kind': 'exact', 'quote': '收入'}])
        concept, alignments = compile_concept(data, PROFILE, {'records': [card]}, decision, {})
        result = build_definition_memberships(data, index, {
            'snapshot_id': data.snapshot_id, 'concepts': [concept], 'record_alignments': alignments})
        assert result['memberships'] == []
        assert result['template_errors'][0]['reason'] == 'name_only_template_cannot_propagate'
        assert result['coverage']['records_without_accepted_template'] == 2
    finally:
        index.close()
        data.close()
