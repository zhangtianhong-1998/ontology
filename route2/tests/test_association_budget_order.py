"""Budget selection is technical recall, never semantic validation."""

import asyncio
import copy
import random

import pytest

from ontology_r2.association_rules import (RuleProposal, _order_alias_proposals,
                                          build_association_rules)
from ontology_r2.discovery import (_conditioned_order, _conditional_selection_coverage,
                                   discover_and_check)
from test_discovery import SmallDataset


def condition(branch, target, field='x', source=('u.a', 'opaque_ref')):
    return {'source': source, 'target': (target, field),
            'suggested_selector': {'opaque_partition': branch},
            'suggested_scope_bindings': {'opaque_partition': 'opaque_scope'},
            'target_role_priority': 1 if target == 'u.a_definition' else 2,
            'numeric_overlap_only': True, 'risk_flags': ['numeric_value_coincidence']}


def test_conditional_branches_and_target_families_precede_same_branch_variants():
    branches = ['01', '02', '甲', '乙', 'unknown']
    items = [condition(branch, target, field)
             for branch in branches
             for target in ['u.a_definition', 'u.b', 'u.z']
             for field in ['x', 'y', 'z']]
    before = copy.deepcopy(items)
    # A name hint alone must not displace an observed value-supported family.
    shared = {(('u.a', 'opaque_ref'), ('u.z', 'x')): (10, True)}
    ordered = _conditioned_order(items, shared)
    assert {r['suggested_selector']['opaque_partition'] for r in ordered[:5]} == set(branches)
    assert {r['target'] for r in ordered[:5]} == {('u.z', 'x')}
    for branch in branches:
        first_three = [r for r in ordered if r['suggested_selector']['opaque_partition'] == branch][:3]
        assert len({r['target'][0] for r in first_three}) == 3
    assert items == before  # Selectors, scope and numeric risk are unchanged.
    assert all(r['numeric_overlap_only'] for r in ordered)
    shuffled = items[:]
    random.Random(71).shuffle(shuffled)
    assert _conditioned_order(shuffled, shared) == ordered


def test_conditioned_source_fairness_and_honest_zero_budget_coverage():
    items = [condition(str(i), 'u.z') for i in range(20)]
    items += [condition('0', 'u.z', source=('v.feed', 'q'))]
    ordered = _conditioned_order(items, {})
    assert {r['source'][0] for r in ordered[:2]} == {'u.a', 'v.feed'}
    assert len({id(r) for r in ordered}) == len(items)
    empty = _conditional_selection_coverage(items, [])
    assert empty['selector_branches'] == {'available': 21, 'selected': 0, 'queued': 21}
    bounded = _conditional_selection_coverage(items, ordered[:2])
    assert bounded['source_fields']['selected'] == 2
    assert bounded['selector_branches']['queued'] == 19
    assert _conditioned_order([], {}) == []


def test_anonymous_columns_use_observed_chinese_branches_and_keep_unresolved_semantics():
    data = SmallDataset({
        'u.feed': (['opaque', 'bucket'], [('A', '甲'), ('A', '甲'), ('B', '乙'), ('B', '乙')], []),
        'u.left': (['z'], [('A',)], []),
        'u.right': (['w'], [('B',)], []),
    })
    try:
        result = discover_and_check(data, {'value_index_mode': 'full_distinct',
            'max_indexed_fields': 0, 'max_conditional_pair_checks': 100,
            'max_conditional_candidates': 100, 'max_candidates_total': 200,
            'max_candidate_validations': 200})
        relevant = [c for c in result['candidates'] if c['source'] ==
                    {'table': 'u.feed', 'field': 'opaque'} and c.get('suggested_selector')]
        assert {tuple(c['suggested_selector'].items()) for c in relevant} >= {
            (('bucket', '甲'),), (('bucket', '乙'),)}
        assert all(c['decision']['semantic_relation'] == 'unresolved' for c in relevant)
        assert result['coverage']['conditional_selection']['selector_branches']['queued'] == 0
    finally:
        data.close()


def alias_lead(cid, source, target, keys=1, target_raw=None, numeric=False):
    return {'candidate_id': cid, 'source': {'table': source[0], 'field': source[1]},
            'target': {'table': target[0], 'field': target[1]},
            'comparison_modes': ['alias_item_equal'],
            'target_transform_rules': ['alias_list_item_nfkc_whitespace_casefold'],
            'source_transform_rules': [], 'examples': [],
            'retrieval_channels': ['alias_list_item_nfkc_whitespace_casefold'],
            'checks': {'shared_normalized_keys_in_sample': keys,
                       'source_raw_values_matched_in_sample': keys,
                       'target_raw_values_matched_in_sample': target_raw or keys,
                       'source_sample_coverage': 1, 'target_sample_coverage': 1,
                       'numeric_overlap_only': numeric}}


def directions(leads):
    candidates, proposals = {}, []
    for lead in leads:
        for reverse in (False, True):
            cid = lead['candidate_id'] + ('r' if reverse else 'f')
            source, target = (lead['target'], lead['source']) if reverse else (lead['source'], lead['target'])
            candidates[cid] = {'source': source, 'target': target,
                               'alias_recall_candidate_id': lead['candidate_id']}
            proposals.append((RuleProposal(candidate_id=cid), 'value_alias_recall'))
    return candidates, proposals


def test_alias_order_covers_tables_fields_and_independent_directions():
    leads = [alias_lead(str(i), ('u.a', 'q'), ('u.b', f'p{i}')) for i in range(20)]
    leads += [alias_lead('good', ('u.z', 'opaque'), ('u.a', 'other'), keys=8)]
    candidates, proposals = directions(leads)
    ordered = _order_alias_proposals(proposals, candidates, leads)
    first = [candidates[p.candidate_id] for p, _ in ordered[:3]]
    assert {c['source']['table'] for c in first} == {'u.a', 'u.b', 'u.z'}
    assert 'goodr' in {p.candidate_id for p, _ in ordered[:3]}
    assert 'goodf' in {p.candidate_id for p, _ in ordered[:3]}
    assert len({p.candidate_id for p, _ in ordered}) == len(proposals)
    assert _order_alias_proposals(list(reversed(proposals)), candidates, leads) == ordered


def test_alias_sample_collision_and_numeric_risk_are_only_ordering_hints():
    leads = [alias_lead('noisy', ('u.a', 'p'), ('u.b', 'x'), keys=8, target_raw=16),
             alias_lead('clear', ('u.a', 'p'), ('u.c', 'y'), keys=3),
             alias_lead('numeric', ('u.a', 'p'), ('u.d', 'z'), keys=20, numeric=True)]
    candidates, proposals = directions(leads)
    ordered = _order_alias_proposals(proposals, candidates, leads)
    forwards = [p.candidate_id for p, _ in ordered if candidates[p.candidate_id]['source']['table'] == 'u.a']
    assert forwards == ['clearf', 'noisyf', 'numericf']
    assert all(p in proposals for p in ordered)  # No lead is silently excluded.


def test_json_object_fragments_do_not_displace_plain_alias_evidence():
    payload = alias_lead('payload', ('u.a', 'opaque_payload'), ('u.b', 'opaque_json'), keys=20)
    payload['examples'] = [{'source': {'raw_value': '{"x":"apple","flag":true}'},
                            'target': {'raw_value': '{"y":"apple","flag":true}'}}]
    plain = alias_lead('plain', ('u.a', 'opaque_lexical'), ('u.b', 'opaque_list'), keys=2)
    plain['examples'] = [{'source': {'raw_value': 'apple'},
                          'target': {'raw_value': 'apple;苹果'}}]
    candidates, proposals = directions([payload, plain])
    ordered = _order_alias_proposals(proposals, candidates, [payload, plain])
    assert {p.candidate_id for p, _ in ordered[:2]} == {'plainf', 'plainr'}
    assert len(ordered) == 4  # Structured leads remain queued, not erased.


@pytest.mark.parametrize('budget', [0, 1, 2])
@pytest.mark.parametrize('ambiguous', [False, True])
def test_alias_budget_does_not_bypass_full_input_uniqueness_or_semantics(budget, ambiguous):
    target = [('alpha;first',), ('alpha;second' if ambiguous else 'beta;second',)]
    data = SmallDataset({'u.a': (['opaque'], [('ALPHA ',), ('Beta',), ('missing',)], []),
                         'u.b': (['payload'], target, [])})
    lead = alias_lead('a', ('u.a', 'opaque'), ('u.b', 'payload'), keys=2)
    try:
        result = asyncio.run(build_association_rules(data, {'candidates': [], 'checks': []},
            {'max_alias_validations': budget}, alias_candidates={'candidates': [lead]}))
        assert result['coverage']['alias_full_input_validations'] == budget
        assert len(result['rules']) == 2
        assert all(r['semantic_relation'] == 'unresolved' for r in result['rules'])
        assert len([r for r in result['rules'] if r['verification']['error'] ==
                    'validation_budget_exhausted']) == 2 - budget
        forward = next(r for r in result['rules'] if r['source']['table'] == 'u.a')
        if budget:
            checks = forward['verification']['checks']
            assert checks['ambiguous_matches'] == int(ambiguous)
            assert checks['unique_matches'] == (0 if ambiguous else 2)
            assert checks['missing_in_input'] == (2 if ambiguous else 1)
            assert forward['status'] == ('unresolved' if ambiguous else 'observed_subset')
        else:
            assert result['coverage']['alias_selection']['source_tables'] == {
                'available': 2, 'verified': 0, 'unverified': 2}
    finally:
        data.close()


@pytest.mark.parametrize('transform,selector,verified', [
    ('target_alias_items', {}, 1),
    ('identity', {}, 0),
    ('target_alias_items', {'partition': 'one'}, 0),
])
def test_alias_coverage_counts_same_rule_verified_by_other_origin(transform, selector, verified):
    data = SmallDataset({'u.a': (['ref', 'partition'], [('alpha', 'one')], []),
                         'u.b': (['names'], [('alpha;first',)], [])})
    lead = alias_lead('x', ('u.a', 'ref'), ('u.b', 'names'))
    try:
        result = asyncio.run(build_association_rules(data, {'candidates': [], 'checks': []}, {
            'max_alias_validations': 0, 'max_explicit_validations': 1,
            'proposals': [{'source': lead['source'], 'target': lead['target'],
                           'transform': transform, 'selector': selector}]},
            alias_candidates={'candidates': [lead]}))
        assert result['coverage']['alias_full_input_validations'] == 0
        assert result['coverage']['new_full_input_validations'] == 1
        explicit = next(r for r in result['rules'] if r['origin'] == 'explicit_rule')
        assert explicit['verification']['scan_scope'] == 'full_input'
        for family in ('source_tables', 'source_fields', 'target_families'):
            assert result['coverage']['alias_selection'][family] == {
                'available': 2, 'verified': verified, 'unverified': 2 - verified}
        assert all(r['semantic_relation'] == 'unresolved' for r in result['rules'])
    finally:
        data.close()


@pytest.mark.parametrize('branches', [('A01', 'A02'), ('01', '02'), ('甲一', '甲二')])
@pytest.mark.parametrize('budget', [2, 4])
def test_validation_keeps_complete_selector_order_and_full_checks(monkeypatch, branches, budget):
    """Do not collapse distinct literals into one prefix during validation."""
    from ontology_r2 import discovery
    data = SmallDataset({
        'u.feed': (['opaque', 'bucket'], [('101', branches[0]), ('202', branches[1]),
                                          ('999', 'other')] * 2, []),
        'u.left': (['x'], [('101',), ('202',)], ['x']),
        'u.right': (['y'], [('101',), ('202',)], ['y']),
    })
    try:
        found = discovery.propose_candidates(data, value_index_mode='full_distinct',
            max_indexed_fields=0, max_conditional_pair_checks=100,
            max_conditional_candidates=100, max_candidates_total=200)
        # Real recall output, with ordinary pairs removed to isolate the bounded
        # conditional validation queue. Values, selectors and risks are untouched.
        found['candidates'] = [c for c in found['candidates'] if c['source'] ==
            {'table': 'u.feed', 'field': 'opaque'} and c.get('suggested_selector')]
        recalled = copy.deepcopy(found['candidates'])
        assert len(recalled) == 4
        assert {c['suggested_selector']['bucket'] for c in recalled[:2]} == set(branches)
        expected = [discovery.validate_candidate(data, c, selector=c['suggested_selector'],
            scope_bindings={target: source for source, target in
                            c['suggested_scope_bindings'].items()}) for c in recalled]
        monkeypatch.setattr(discovery, 'propose_candidates', lambda *a, **k: copy.deepcopy(found))
        result = discovery.discover_and_check(data, {'max_candidate_validations': budget})
        assert result['checks'] == expected[:budget]
        assert result['coverage']['candidates_not_attempted'] == 4 - budget
        assert result['coverage']['candidates_checked'] == budget
        assert result['coverage']['candidates_not_checked'] == 4 - budget
        assert all(c['numeric_overlap_only'] and c['risk_flags'] == ['numeric_value_coincidence']
                   for c in result['candidates'])
        assert all(c['decision']['semantic_relation'] == 'unresolved' for c in result['candidates'])
        assert all(c['checks']['eligible_references'] == c['checks']['unique_matches'] == 2
                   for c in result['checks'])
        if budget == 4:
            # Untruncated validation produces the same complete checks, not a
            # looser approximation. Only the bounded queue order was changed.
            assert {c['candidate_id'] for c in result['checks']} == {
                c['candidate_id'] for c in recalled}
    finally:
        data.close()
