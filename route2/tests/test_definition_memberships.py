"""Template reuse must not turn reference variants into same-as assertions."""
from copy import deepcopy
from pathlib import Path

import pytest

from ontology_r2.definition_memberships import _semantic_columns, build_definition_memberships
from ontology_r2.group_incremental import ConceptBundleDecision, compile_concept
from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from ontology_r2.storage import Dataset, read_yaml
from test_semantic_cards import _table, _wide_reference_dataset

PROFILE = read_yaml(Path(__file__).resolve().parents[1] / "ontologies/internal_model.yaml")


def _fixture(tmp_path):
    root = tmp_path / 'input'
    _table(root, 'measure_definition', {
        'id': '记录主键', 'measure_name': '度量名称', 'definition': '度量定义',
        'formula': '计算公式', 'scope': '适用范围', 'unit': '单位', 'reference_code': '引用编码',
    }, [
        {'id': '1', 'measure_name': '收入', 'definition': '适用于不同经营对象的收入总额',
         'formula': 'sum(amount)', 'scope': '全年', 'unit': '元', 'reference_code': 'A'},
        {'id': '2', 'measure_name': '收入', 'definition': '适用于不同经营对象的收入总额',
         'formula': 'sum(amount)', 'scope': '全年', 'unit': '元', 'reference_code': 'B'},
        {'id': '3', 'measure_name': '收入', 'definition': '适用于不同经营对象的收入总额',
         'formula': 'sum(amount)', 'scope': '全年', 'unit': '元', 'reference_code': 'A'},
        {'id': '4', 'measure_name': '收入', 'definition': '适用于不同经营对象的收入总额',
         'formula': 'avg(amount)', 'scope': '全年', 'unit': '元', 'reference_code': 'A'},
        {'id': '5', 'measure_name': '收入', 'definition': '适用于不同经营对象的收入总额',
         'formula': 'sum(amount)', 'scope': '季度', 'unit': '元', 'reference_code': 'A'},
    ])
    work = tmp_path / 'work'
    work.mkdir()
    data = Dataset(root, work)
    built = build_semantic_cards(data, tmp_path / 'cards.sqlite')
    index = SemanticCardIndex(built['index_path'])
    card = next(c for c in index.all_cards(100)['cards'] if c['row_number'] == 1)
    decision = ConceptBundleDecision(status='proposed', label='收入', definition='适用于不同经营对象的收入总额',
        root_type='Measure', ontology_level='type', scope={'scope': '全年'},
        scope_roles={'scope': 'applicability'}, classification_basis='reusable_measure',
        classification_quote='适用于不同经营对象的收入总额',
        alignments=[{'record_id': card['record_id'], 'mapping_kind': 'exact', 'quote': '适用于不同经营对象的收入总额'}])
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


@pytest.mark.parametrize('first_reference', [None, 'reference_value_' + 'A' * 80])
def test_all_reference_columns_and_empty_representative_values_are_inspected(tmp_path, first_reference):
    data, references = _wide_reference_dataset(tmp_path, first_reference=first_reference)
    built = build_semantic_cards(data, tmp_path / 'cards.sqlite', max_field_chars=16)
    index = SemanticCardIndex(built['index_path'])
    try:
        card = next(c for c in index.all_cards(2)['cards'] if c['row_number'] == 1)
        decision = ConceptBundleDecision(status='proposed', label='收入', definition='适用于不同经营对象的收入总额',
            root_type='Measure', ontology_level='type', classification_basis='reusable_measure',
            classification_quote='适用于不同经营对象的收入总额',
            alignments=[{'record_id': card['record_id'], 'mapping_kind': 'exact', 'quote': '适用于不同经营对象的收入总额'}])
        concept, alignments = compile_concept(data, PROFILE, {'records': [card]}, decision, {})
        result = build_definition_memberships(data, index, {
            'snapshot_id': data.snapshot_id, 'concepts': [concept], 'record_alignments': alignments})
        assert len(result['memberships']) == 2
        assert set(result['templates'][0]['reference_values']) == set(references)
        source_row = next(row for row in data.rows('fruit.measure_definition') if row['__r2_row'] == 1)
        assert result['templates'][0]['reference_values']['ref9_code'] == source_row['ref9_code']
        pending = result['reference_variants_pending']
        assert len(pending) == 1 and pending[0]['row_number'] == 2
        assert set(pending[0]['reference_values']) == set(references)
        assert pending[0]['reference_values']['ref9_code'] == 'reference_value_' + 'B' * 80
        assert result['coverage']['reference_inspection_complete']
        assert result['coverage']['reference_linkage_complete'] is False
        assert result['coverage']['partial']
        assert result['coverage']['llm_calls'] == 0
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


@pytest.mark.parametrize('proposed_role', ['alias', 'name'])
def test_demoted_code_alias_keeps_independent_memberships_and_pending_references(tmp_path, proposed_role):
    root = tmp_path / 'input'
    _table(root, 'measure_definition', {'id': '记录主键', 'measure_name': '度量名称',
        'definition': '度量定义', 'measure_code': '度量编码'}, [
        {'id': '1', 'measure_name': '收入', 'definition': '适用于不同经营对象的收入总额', 'measure_code': 'M001'},
        {'id': '2', 'measure_name': '收入', 'definition': '适用于不同经营对象的收入总额', 'measure_code': 'M002'}])
    work = tmp_path / 'work'
    work.mkdir()
    data = Dataset(root, work)
    data.tables['fruit.measure_definition']['inferred_semantic_roles'] = [
        {'column': 'measure_code', 'role': proposed_role, 'status': 'source_verified_role_candidate'}]
    built = build_semantic_cards(data, tmp_path / 'cards.sqlite')
    index = SemanticCardIndex(built['index_path'])
    try:
        cards = index.all_cards(10)['cards']
        assert len(cards) == 2 and len({card['pattern_id'] for card in cards}) == 1
        card = next(card for card in cards if card['row_number'] == 1)
        assert card['binding_columns_excluded_from_semantic_pattern'] == ['measure_code']
        assert (proposed_role, 'measure_code') not in _semantic_columns(data, card)
        decision = ConceptBundleDecision(status='proposed', label='收入', definition='适用于不同经营对象的收入总额',
            root_type='Measure', ontology_level='type', classification_basis='reusable_measure',
            classification_quote='适用于不同经营对象的收入总额',
            alignments=[{'record_id': card['record_id'], 'mapping_kind': 'exact', 'quote': '适用于不同经营对象的收入总额'}])
        concept, alignments = compile_concept(data, PROFILE, {'records': [card]}, decision, {})
        result = build_definition_memberships(data, index, {
            'snapshot_id': data.snapshot_id, 'concepts': [concept], 'record_alignments': alignments})
        assert len(result['memberships']) == 2
        assert len({member['record_id'] for member in result['memberships']}) == 2
        assert result['templates'][0]['binding_columns_excluded_from_semantic_pattern'] == ['measure_code']
        assert result['reference_variants_pending'][0]['reference_values'] == {'measure_code': 'M002'}
        assert all(member['mapping_kind'] == 'shares_definition_type_template'
                   and not member['entity_identity_claim'] and not member['relationship_inheritance']
                   for member in result['memberships'])
        assert result['coverage']['llm_calls'] == 0
    finally:
        index.close()
        data.close()


def test_identifier_conflict_removes_name_and_alias_but_not_scope(tmp_path):
    data, index, group = _fixture(tmp_path)
    try:
        table = data.tables['fruit.measure_definition']
        table['inferred_semantic_roles'] = [
            {'column': field, 'role': role, 'status': 'source_verified_role_candidate'}
            for field, role in [('reference_code', 'alias'), ('reference_code', 'name'), ('reference_code', 'scope')]]
        card = index.all_cards(10)['cards'][0]
        card['binding_columns_excluded_from_semantic_pattern'] = ['reference_code']
        pairs = _semantic_columns(data, card)
        assert ('alias', 'reference_code') not in pairs
        # The source identifier declaration now resolves both conflicting
        # roles before card-specific legacy exclusions are considered.
        assert ('name', 'reference_code') not in pairs
        assert ('scope', 'reference_code') in pairs
        card['role_conflicts'] = [{'column': 'reference_code', 'proposed_role': role,
                                   'effective_role': 'reference', 'binding_only': True}
                                  for role in ('name', 'alias')]
        pairs = _semantic_columns(data, card)
        assert ('name', 'reference_code') not in pairs
        assert ('alias', 'reference_code') not in pairs
        assert ('scope', 'reference_code') in pairs
    finally:
        index.close()
        data.close()


def test_checked_calculation_fragments_keep_scope_without_reintroducing_formula(tmp_path):
    root = tmp_path / 'input'
    _table(root, 'measure_definition', {'id': '记录主键', 'name': '名称', 'definition': '定义',
        'inference_type': '聚合操作类型', 'source_field': '来源字段', 'calculation_formula': '完整公式',
        'reference_code': '关联编码'}, [
        {'id': str(i), 'name': '数量', 'definition': '数量的可复用计算口径',
         'inference_type': operator, 'source_field': 'sales.amount',
         'calculation_formula': 'amount', 'reference_code': ref}
        for i, operator, ref in [(1, 'SUM', 'A'), (2, 'SUM', 'B'), (3, 'AVG', 'C')]])
    work = tmp_path / 'work'
    work.mkdir()
    data = Dataset(root, work)
    data.tables['fruit.measure_definition']['inferred_semantic_roles'] = [
        {'column': column, 'role': 'formula', 'status': 'source_verified_role_candidate',
         'schema_evidence_id': 'schema:fruit.measure_definition:' + column,
         'observations': [{'column': column, 'row_number': 1, 'value': value}]}
        for column, value in [('inference_type', 'SUM'), ('source_field', 'sales.amount')]]
    built = build_semantic_cards(data, tmp_path / 'cards.sqlite')
    index = SemanticCardIndex(built['index_path'])
    try:
        card = next(card for card in index.all_cards(10)['cards'] if card['row_number'] == 1)
        pairs = _semantic_columns(data, card)
        assert ('formula', 'inference_type') not in pairs
        assert ('formula', 'source_field') not in pairs
        assert ('scope', 'inference_type') in pairs and ('scope', 'source_field') in pairs
        assert ('formula', 'calculation_formula') in pairs
        # Scope alone does not have authority to erase a formula role.
        no_fragment_proof = {**card, 'calculation_fragments': []}
        assert ('formula', 'inference_type') in _semantic_columns(data, no_fragment_proof)
        decision = ConceptBundleDecision(status='proposed', label='数量', definition='数量的可复用计算口径',
            root_type='GeneralObject', ontology_level='type',
            scope_roles={column: 'applicability' for column in card['scope']},
            alignments=[{'record_id': card['record_id'], 'mapping_kind': 'exact', 'quote': '数量的可复用计算口径'}])
        concept, alignments = compile_concept(data, PROFILE, {'records': [card]}, decision, {})
        result = build_definition_memberships(data, index, {
            'snapshot_id': data.snapshot_id, 'concepts': [concept], 'record_alignments': alignments})
        assert {member['row_number'] for member in result['memberships']} == {1, 2}
        assert result['reference_variants_pending'][0]['row_number'] == 2
        assert result['coverage']['records_without_accepted_template'] == 1
        assert len(result['templates'][0]['calculation_fragments']) == 2
        assert all(not member['entity_identity_claim'] and not member['relationship_inheritance']
                   for member in result['memberships'])
    finally:
        index.close()
        data.close()
