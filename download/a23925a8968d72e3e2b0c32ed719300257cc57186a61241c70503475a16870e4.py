"""Short wire identifiers must not weaken exact source evidence validation."""
import asyncio
from copy import deepcopy
import json

import pytest

from ontology_r2.group_incremental import ConceptBundleDecision
from ontology_r2.llm import StructuredLLM
from ontology_r2.models import Review
from ontology_r2.prompt_references import PromptReferences
from ontology_r2.template_projection import ProjectionQuote
from test_transport import provider


def record(rid, table='one.definitions', code='987654321012345', *, name='R1'):
    return {'table': table, 'record_id': rid, 'scope': {'tenant': '987654321012345'},
            'fields': {'reference': [{'column': 'code', 'value': code, 'truncated': False,
                                     'schema_evidence_id': f'schema:{table}:code'}],
                       'name': [{'column': 'name', 'value': name}],
                       'description': [{'column': 'definition', 'value': '编号987654321012345不适用于北京'}],
                       'formula': [{'column': 'formula', 'value': '987654321012345 / 100 + X'}],
                       'unit': [{'column': 'unit', 'value': '100'}],
                       'identity': [{'column': 'tenant', 'value': '987654321012345'}]}}


def payload(*records):
    return {'bundle': {'bundle_id': 'bundle:long-system-id', 'records': list(records),
                       'exact_alignment_record_ids': [r['record_id'] for r in records]},
            'current_types': [{'id': 'type:original-system-id', 'parent': 'Measure', 'label': 'R1'}]}


def test_only_addressed_identifiers_change_and_complete_semantics_survive():
    original = payload(record('record:long:1'))
    saved = deepcopy(original)
    refs = PromptReferences('concept_bundle', original)
    wire = refs.payload
    cell = wire['bundle']['records'][0]
    assert cell['record_id'].startswith('@r')
    assert wire['bundle']['exact_alignment_record_ids'] == [cell['record_id']]
    assert wire['current_types'][0]['id'].startswith('@t')
    assert wire['current_types'][0]['parent'] == 'Measure'
    assert cell['fields']['reference'][0]['value'].startswith('@v')
    assert cell['fields']['reference'][0]['schema_evidence_id'].startswith('@e')
    for key in ('name', 'description', 'formula', 'unit', 'identity'):
        assert cell['fields'][key] == original['bundle']['records'][0]['fields'][key]
    assert cell['scope'] == original['bundle']['records'][0]['scope']
    result = refs.response({'record_id': cell['record_id'], 'column': 'code',
                            'quote': cell['fields']['reference'][0]['value']})
    assert result == {'record_id': 'record:long:1', 'column': 'code', 'quote': '987654321012345'}
    assert original == saved
    assert refs.audit()['coverage']['embedded_reference_preserved'] == 3


def test_same_code_is_distinct_across_schema_rows_and_type_record_namespaces():
    original = payload(record('same', table='one.definitions'), record('other', table='two.definitions'),
                       record('third', table='one.definitions'))
    original['current_types'][0]['id'] = 'same'
    original['bundle']['records'][2]['scope']['tenant'] = 'other-tenant'
    refs = PromptReferences('concept_bundle', original)
    records = refs.payload['bundle']['records']
    assert len({r['fields']['reference'][0]['value'] for r in records}) == 3
    assert refs.payload['current_types'][0]['id'] != records[0]['record_id']
    quote = records[1]['fields']['reference'][0]['value']
    with pytest.raises(ValueError, match='source record/column'):
        refs.response({'record_id': records[0]['record_id'], 'column': 'code', 'quote': quote})


def test_known_pairs_and_their_copies_share_alias_but_selector_scope_stays_literal():
    original = payload(record('left'), record('right', table='two.definitions'))
    b = original['bundle']
    b['rule'] = {'rule_id': 'checked-rule', 'status': 'checked_technical',
                 'source': {'table': 'one.definitions', 'field': 'code'},
                 'target': {'table': 'two.definitions', 'field': 'code'},
                 'selector': {'code': '987654321012345', 'tenant': '987654321012345'},
                 'scope_bindings': {'tenant': 'tenant'}, 'transform': {'operator': 'identity'}}
    b['examples'] = {'positive': [{'source_record_id': 'left', 'target_record_id': 'right',
                                  'source_raw_value': '987654321012345', 'target_raw_value': '987654321012345',
                                  'matching_raw_value': '987654321012345'}]}
    refs = PromptReferences('relation_bundle', original)
    wire = refs.payload['bundle']
    short = wire['records'][0]['fields']['reference'][0]['value']
    assert short == wire['records'][1]['fields']['reference'][0]['value']
    assert wire['rule']['selector'] == {'code': short, 'tenant': '987654321012345'}
    assert wire['rule']['scope_bindings'] == {'tenant': 'tenant'}
    assert {wire['examples']['positive'][0][key] for key in
            ('source_raw_value', 'target_raw_value', 'matching_raw_value')} == {short}


def test_nonidentity_match_keeps_two_aliases_and_unrecognized_raw_match_is_audited():
    original = payload(record('left', code='001'), record('right', table='two.definitions', code='1'))
    b = original['bundle']
    b['rule'] = {'status': 'checked_technical', 'source': {'table': 'one.definitions', 'field': 'code'},
                 'target': {'table': 'two.definitions', 'field': 'code'}, 'transform': {'operator': 'numeric'}}
    b['examples'] = {'positive': [{'source_record_id': 'left', 'target_record_id': 'right',
                                  'source_raw_value': '001', 'target_raw_value': '1', 'matching_raw_value': '1'}]}
    wire = PromptReferences('relation_bundle', original).payload['bundle']
    pair = wire['examples']['positive'][0]
    assert pair['source_raw_value'] != pair['target_raw_value']
    assert wire['rule']['transform']['operator'] == 'numeric'


def test_alias_collision_and_unknown_outputs_fail_without_rewriting_labels():
    original = payload(record('record:a', code='@v1', name='@r1'))
    refs = PromptReferences('concept_bundle', original)
    wire = refs.payload['bundle']['records'][0]
    assert wire['record_id'] != '@r1'
    assert wire['fields']['reference'][0]['value'] != '@v1'
    assert refs.response({'label': '@r1', 'definition': '@v1'}) == {'label': '@r1', 'definition': '@v1'}
    with pytest.raises(ValueError, match='Unknown prompt alias'):
        refs.response({'record_id': '@r999'})
    with pytest.raises(ValueError, match='Unknown reference alias'):
        refs.response({'record_id': wire['record_id'], 'column': 'code', 'quote': '@v999'})
    with pytest.raises(ValueError, match='kind mismatch'):
        refs.response({'record_id': refs.payload['current_types'][0]['id']})


def test_conflicting_semantic_role_structured_values_and_role_inference_stay_original():
    original = payload(record('r'))
    fields = original['bundle']['records'][0]['fields']
    fields['reference'].extend([{'column': 'scope', 'value': '2026'},
                                {'column': 'config', 'value': '{"id":"123","formula":"X/100"}'}])
    fields['scope'] = [{'column': 'scope', 'value': '2026'}]
    refs = PromptReferences('concept_bundle', original)
    assert refs.payload['bundle']['records'][0]['fields']['reference'][1:] == fields['reference'][1:]
    assert refs.audit()['coverage']['protected_semantic_column'] == 1
    assert refs.audit()['coverage']['non_atomic_or_incomplete_reference'] == 1
    assert PromptReferences('column_role_inference', original).payload == original
    assert PromptReferences('concept_bundle', original, enabled=False).payload == original


def test_batch_review_candidates_restore_quotes_and_keep_literal_scope_maps():
    packet = payload(record('record:1'))
    packet['candidate'] = {'projection': {'witness_record_ids': ['record:1'],
                            'existing_type_id': 'type:original-system-id',
                            'slots': [{'name': 'code', 'role': 'reference', 'source_column': 'code',
                                       'evidence': {'record_id': 'record:1', 'column': 'code', 'quote': '987654321012345'}}],
                            'applicability_scope': {'record_id': 'record:1', 'id': 'type:original-system-id'}}}
    refs = PromptReferences('group_review_batch', {'packets': [packet, deepcopy(packet)]})
    left, right = refs.payload['packets']
    assert left == right
    projection = left['candidate']['projection']
    assert projection['slots'][0]['evidence']['quote'].startswith('@v')
    assert projection['applicability_scope'] == packet['candidate']['projection']['applicability_scope']
    restored = refs.response({'corrected_delta': left['candidate']})
    assert restored['corrected_delta'] == packet['candidate']


def test_reuse_slot_values_and_fixed_reference_values_encode_by_source_column():
    original = payload(record('r'))
    original['reuse'] = {'source_record_id': 'r', 'slot_values': {'code': '987654321012345', 'name': 'R1'},
                        'accepted_template': {'source_table': 'one.definitions', 'template_id': 'template:1',
                            'slots': [{'name': 'code', 'role': 'reference', 'source_column': 'code',
                                       'fixed_value': '987654321012345'},
                                      {'name': 'name', 'role': 'record_identity', 'source_column': 'name',
                                       'fixed_value': 'R1'}]}}
    wire = PromptReferences('concept_bundle', original).payload
    short = wire['bundle']['records'][0]['fields']['reference'][0]['value']
    assert wire['reuse']['slot_values'] == {'code': short, 'name': 'R1'}
    assert wire['reuse']['accepted_template']['slots'][0]['fixed_value'] == short
    assert wire['reuse']['accepted_template']['slots'][1]['fixed_value'] == 'R1'


@pytest.mark.parametrize('stream', [False, True])
def test_wire_cache_admission_and_format_repair_use_same_encoded_payload(tmp_path, monkeypatch, stream):
    original = payload(record('long:record:original'))
    observed = []

    def answer(body, number):
        content = body['messages'][1]['content']
        text = content if isinstance(content, str) else ''.join(item.get('text', '') for item in content)
        _, raw = text.split('\n', 1)
        sent = json.loads(raw)
        observed.append(sent)
        cell = sent['bundle']['records'][0]
        return 'submit_result', ('{"record_id":' if number == 1 else
                                {'record_id': cell['record_id'], 'column': 'code',
                                 'quote': cell['fields']['reference'][0]['value']})

    with provider(monkeypatch, answer) as requests:
        llm = StructuredLLM({'mode': 'agentscope', 'stream': stream, 'max_response_repairs': 1}, tmp_path)
        admitted = []
        admit = llm.admit
        def capture(request):
            admitted.append(deepcopy(request))
            admit(request)
        llm.admit = capture
        async def run():
            first = await llm.ask('concept_bundle', original, ProjectionQuote)
            # Reuse the wire cache with a different original ID; never replay stale source IDs.
            changed = deepcopy(original)
            changed['bundle']['records'][0]['record_id'] = 'long:record:new-source'
            changed['bundle']['exact_alignment_record_ids'] = ['long:record:new-source']
            second = await llm.ask('concept_bundle', changed, ProjectionQuote)
            await llm.close()
            return first, second
        first, second = asyncio.run(run())
    assert first.record_id == 'long:record:original' and second.record_id == 'long:record:new-source'
    assert first.quote == second.quote == '987654321012345'
    assert llm.calls == len(requests) == 2 and llm.cached == 1
    assert observed[0] == observed[1] == admitted[0]['input'] == admitted[1]['input']
    audits = [json.loads(line) for line in (tmp_path / 'prompt_reference_audit.jsonl').read_text().splitlines()]
    assert len(audits) == 2
    assert all(item['request']['input'] == observed[0] for item in audits)
    assert audits[1]['request']['format_corrections'] == admitted[1]['format_corrections']
    assert 'local-test-secret' not in (tmp_path / 'prompt_reference_audit.jsonl').read_text()
    assert 'long:record:original' in (tmp_path / 'trace.jsonl').read_text()
    wire_cache = json.loads(next(llm.cache.glob('*.json')).read_text())
    assert wire_cache['record_id'].startswith('@r') and wire_cache['quote'].startswith('@v')


def test_configuration_reference_citation_roundtrip():
    original = {'configuration_table': 'app.config', 'candidate_id': 'candidate:one',
                'reference_paths': [{'source': {'table': 'app.config', 'field': 'metric_code'}}],
                'evidence': [{'evidence_id': 'record:code-evidence', 'kind': 'configuration_value',
                              'column': 'metric_code', 'text': '123456789'},
                             {'evidence_id': 'record:description', 'kind': 'configuration_value',
                              'column': 'description', 'text': '不使用123456789'}]}
    refs = PromptReferences('configuration_purpose', original)
    wire = refs.payload['evidence'][0]
    assert wire['text'].startswith('@v')
    assert refs.payload['evidence'][1]['text'] == '不使用123456789'
    assert refs.response({'evidence_id': wire['evidence_id'], 'purpose_quote': wire['text']}) == {
        'evidence_id': 'record:code-evidence', 'purpose_quote': '123456789'}


def test_reference_template_roundtrip_reaches_existing_compiler_and_review(tmp_path, monkeypatch):
    from ontology_r2.group_incremental import construct_from_bundles
    from ontology_r2.models import BuildPlan
    from test_template_projection import setup, decision, PROFILE

    data, bundle = setup()
    for item in bundle['records']:
        item['fields']['reference'].append({'column': 'shared_code', 'value': 'original:shared:reference:1001'})
    observed = []

    def answer(body, number):
        content = body['messages'][1]['content']
        text = content if isinstance(content, str) else ''.join(item.get('text', '') for item in content)
        task, raw = text.split('\n', 1)
        sent = json.loads(raw)
        observed.append((task, sent))
        if task == 'group_review':
            candidate = sent['candidate']['projection']
            assert candidate['slots'][-1]['evidence']['quote'].startswith('@v')
            # The two witnesses have different scopes: an unaddressed fixed
            # literal is conservatively retained for reviewer comparison.
            assert next(field for field in candidate['field_templates']
                        if field['column'] == 'shared_code')['template'] == 'original:shared:reference:1001'
            return 'submit_result', {'accepted': True}
        rows = sent['bundle']['records']
        projection = decision().model_dump()
        # Construct the provider's reply from model-visible source locations.
        def cited(node):
            if isinstance(node, list):
                for item in node:
                    cited(item)
            elif isinstance(node, dict):
                if 'record_id' in node:
                    node['record_id'] = rows[0]['record_id']
                for item in node.values():
                    cited(item)
        cited(projection)
        projection['witness_record_ids'] = [row['record_id'] for row in rows]
        projection['slots'].append({'name': 'record_code', 'role': 'record_identity', 'label': '记录编号',
                                    'source_column': 'x12', 'evidence': {
                                        'record_id': rows[0]['record_id'], 'column': 'x12',
                                        'quote': rows[0]['fields']['reference'][0]['value']}})
        projection['field_templates'].extend([
            {'column': 'x12', 'template': '{record_code}'},
            {'column': 'shared_code', 'template': rows[0]['fields']['reference'][1]['value']}])
        return 'submit_result', {'status': 'proposed', 'action': 'project_template', 'projection': projection}

    with provider(monkeypatch, answer):
        llm = StructuredLLM({'mode': 'agentscope', 'stream': False, 'max_calls': 2}, tmp_path)
        async def run():
            try:
                return await construct_from_bundles(data, PROFILE, BuildPlan(), [bundle], llm,
                                                    enable_template_projection=True)
            finally:
                await llm.close()
        result = asyncio.run(run())
    assert [task for task, _ in observed] == ['concept_bundle', 'group_review']
    assert result['coverage']['statuses']['accepted'] == 1
    assert [binding['slot_values']['record_code'] for binding in result['template_bindings']] == ['row1', 'row2']
    template = result['template_projections'][0]
    assert next(field for field in template['field_templates']
                if field['column'] == 'x12')['template'] == '{record_code}'
    # Reference values survive decoding on each binding, without becoming
    # immutable business-type literals merely because the witnesses agree.
    assert template['instance_columns']['shared_code'] == 'source_checked_record_binding'
    assert [binding['instance_values']['shared_code'] for binding in result['template_bindings']] == [
        'original:shared:reference:1001', 'original:shared:reference:1001']
    assert all('@v' not in json.dumps(item, ensure_ascii=False) for item in data.evidence.values())
    source = next(item for item in data.evidence.values()
                  if item.get('source_ref', {}).get('column') == 'x12')
    assert source['raw_fragment'] == 'row1' and source['source_ref']['record_id'] == 'row1'


def test_reference_alias_in_unaddressed_business_literal_is_rejected():
    refs = PromptReferences('concept_bundle', payload(record('r')))
    alias = refs.payload['bundle']['records'][0]['fields']['reference'][0]['value']
    for response in ({'label': alias}, {'definition_parameters': {'code': alias}},
                     {'formula': f'{alias} / 100'}, {'quote': alias}, {'value': '@v999'}):
        with pytest.raises(ValueError, match='alias'):
            refs.response(response)


def test_same_namespace_equal_reference_cells_share_alias_and_reuse_maps_roundtrip():
    original = payload(record('r1'), record('r2'))
    original['reuse'] = {'source_record_id': 'r1', 'slot_values': {'code': '987654321012345'},
                        'accepted_template': {'source_table': 'one.definitions',
                            'invariants': {'code': '987654321012345'},
                            'slots': [{'name': 'code', 'role': 'reference', 'source_column': 'code',
                                       'fixed_value': '987654321012345'}]}}
    refs = PromptReferences('concept_bundle', original)
    records = refs.payload['bundle']['records']
    assert records[0]['fields']['reference'][0]['value'] == records[1]['fields']['reference'][0]['value']
    assert refs.response(refs.payload['reuse']) == original['reuse']


def test_unknown_alias_fails_without_format_repair_or_cache(tmp_path, monkeypatch):
    with provider(monkeypatch, lambda body, number: ('submit_result', {
            'record_id': '@r999', 'column': 'code', 'quote': 'bad'})) as requests:
        llm = StructuredLLM({'mode': 'agentscope', 'stream': False, 'max_response_repairs': 2}, tmp_path)
        async def run():
            try:
                return await llm.ask('concept_bundle', payload(record('r')), ProjectionQuote)
            finally:
                await llm.close()
        with pytest.raises(ValueError, match='Unknown prompt alias'):
            asyncio.run(run())
    assert len(requests) == llm.calls == 1
    assert not list(llm.cache.glob('*.json'))


def test_disabled_config_and_role_inference_request_keep_original_schema_and_payload(tmp_path):
    with pytest.raises(ValueError, match='boolean'):
        StructuredLLM({'prompt_reference_aliases': 'true'}, tmp_path)
    original = payload(record('r'))
    llm = StructuredLLM({'prompt_reference_aliases': False}, tmp_path)
    assert llm.structured_request('concept_bundle', original, Review)['input'] == original
    role_llm = StructuredLLM({}, tmp_path)
    assert role_llm.structured_request('column_role_inference', original, Review)['input'] == original


@pytest.mark.parametrize('protected', ['scope', 'identity_qualifiers', 'definition_parameters', 'applicability_scope'])
def test_protected_business_maps_cannot_trigger_reference_overrides(protected):
    original = payload(record('r'))
    business = {'record_id': 'r', 'column': 'code', 'value': '987654321012345',
                'nested': {'source_record_id': 'r', 'column': 'code', 'quote': '987654321012345'}}
    original['current_types'][0][protected] = business
    refs = PromptReferences('concept_bundle', original)
    assert refs.payload['current_types'][0][protected] == business
    assert refs.response({protected: business}) == {protected: business}


def test_selector_map_and_condition_roundtrip_are_addressed_by_source_column():
    original = payload(record('r'))
    original['rule'] = {'source': {'table': 'one.definitions', 'field': 'code'},
                        'selector': {'code': '987654321012345', 'tenant': '987654321012345'}}
    refs = PromptReferences('concept_bundle', original)
    wire = refs.payload['rule']
    alias = wire['selector']['code']
    assert alias.startswith('@v')
    assert wire['selector']['tenant'] == '987654321012345'
    assert refs.response(wire) == original['rule']
    response = {'source_table': 'one.definitions', 'selector': {
        'op': 'and', 'children': [
            {'op': 'eq', 'field': 'code', 'value': alias},
            {'op': 'in', 'field': 'code', 'values': [alias, 'unseen-original']},
            {'op': 'range', 'field': 'code', 'values': ['100', '200']},
            {'op': 'eq', 'field': 'tenant', 'value': '987654321012345'}]}}
    restored = refs.response(response)
    children = restored['selector']['children']
    assert children[0]['value'] == children[1]['values'][0] == '987654321012345'
    assert children[1]['values'][1] == 'unseen-original'
    assert children[2:] == response['selector']['children'][2:]
    wrong = deepcopy(response)
    wrong['source_table'] = 'other.definitions'
    with pytest.raises(ValueError, match='source record/column'):
        refs.response(wrong)
    wrong = deepcopy(response)
    wrong['selector']['children'][0]['field'] = 'tenant'
    with pytest.raises(ValueError, match='source record/column'):
        refs.response(wrong)


@pytest.mark.parametrize('stream', [False, True])
def test_relation_plan_selector_condition_roundtrips_through_transport_schema(tmp_path, monkeypatch, stream):
    from ontology_r2.models import RelationPlan
    original = payload(record('r'))
    original['rule'] = {'rule_id': 'rule:1001', 'source': {'table': 'one.definitions', 'field': 'code'},
                        'selector': {'code': '987654321012345'}}
    observed = []
    def answer(body, number):
        content = body['messages'][1]['content']
        text = content if isinstance(content, str) else ''.join(item.get('text', '') for item in content)
        sent = json.loads(text.split('\n', 1)[1])
        observed.append(sent)
        return 'submit_result', {
            'id': 'plan:new', 'source_table': 'one.definitions', 'target_table': 'other.definitions',
            'source_column': 'code', 'target_column': 'id', 'mode': 'identifier', 'evidence_ids': [],
            'selector': {'op': 'eq', 'field': 'code', 'value': sent['rule']['selector']['code']}}
    with provider(monkeypatch, answer):
        llm = StructuredLLM({'mode': 'agentscope', 'stream': stream}, tmp_path)
        async def run():
            try:
                return await llm.ask('plan', original, RelationPlan)
            finally:
                await llm.close()
        result = asyncio.run(run())
    assert observed[0]['rule']['selector']['code'].startswith('@v')
    assert result.selector.value == '987654321012345'
    assert result.selector.op == 'eq' and result.selector.field == 'code'


def test_numeric_reference_cells_and_readable_enum_literals_remain_original():
    original = payload(record('r', code=123))
    fields = original['bundle']['records'][0]['fields']['reference']
    fields.extend([{'column': 'object_kind', 'value': 'Metric'},
                   {'column': 'aggregation', 'value': 'SUM'},
                   {'column': 'status', 'value': 'PENDING_APPROVAL'}])
    refs = PromptReferences('concept_bundle', original)
    wire = refs.payload['bundle']['records'][0]
    assert wire['fields']['reference'][0]['value'] == 123
    assert [item['value'] for item in wire['fields']['reference'][1:]] == ['Metric', 'SUM', 'PENDING_APPROVAL']
    assert refs.audit()['coverage']['non_string_reference_preserved'] == 1
    assert refs.audit()['coverage']['readable_reference_literal_preserved'] == 3
    quote = ProjectionQuote.model_validate(refs.response({'record_id': wire['record_id'], 'column': 'code', 'quote': '123'}))
    assert quote.quote == '123'


def test_known_system_aliases_restore_only_in_model_explanations_unknown_always_rejects():
    refs = PromptReferences('concept_bundle', payload(record('r')))
    rid = refs.payload['bundle']['records'][0]['record_id']
    result = refs.response({'reason': f'依据 {rid}', 'errors': [f'检查 {rid}'], 'explanation': rid})
    assert result == {'reason': '依据 r', 'errors': ['检查 r'], 'explanation': 'r'}
    for response in ({'label': rid}, {'definition': f'uses {rid}'}, {'label': '@r999'},
                     {'reason': '@r999'}, {'definition': 'uses @e999'},
                     {'scope': {'reason': rid}}, {'definition_parameters': {'@r999': 'value'}}):
        with pytest.raises(ValueError, match='alias'):
            refs.response(response)
    # A known reference may be mentioned in prose, but cannot bypass an
    # addressed evidence quote by appearing in an alignment.
    code = refs.payload['bundle']['records'][0]['fields']['reference'][0]['value']
    assert refs.response({'reason': f'Only reference {code}; no semantic evidence.'}) == {
        'reason': 'Only reference 987654321012345; no semantic evidence.'}
    with pytest.raises(ValueError, match='alias'):
        refs.response({'alignments': [{'record_id': rid, 'mapping_kind': 'unresolved', 'quote': code}]})


@pytest.mark.parametrize('selector', [
    {'op': 'eq', 'field': 'code'},
    {'op': 'eq', 'field': 'code', 'value': '987654321012345'},
    {'op': 'in', 'field': 'code', 'values': 'literal-business-values'},
])
def test_rule_selector_business_columns_never_masquerade_as_condition(selector):
    original = payload(record('r'))
    original['rule'] = {'source': {'table': 'one.definitions', 'field': 'code'},
                        'selector': selector}
    refs = PromptReferences('concept_bundle', original)
    assert refs.payload['rule']['selector'] == selector
    assert refs.response(refs.payload['rule']) == original['rule']
