"""Publish uniquely bound calculation contracts as typed ontology dependencies."""
from .calculation_contracts import calculation_relation_errors, enrich_calculation_contracts
from .models import DerivedType
from .relation_contract import canonical_relation_id
from .validation import validate_plan


def compile_calculation_stage(data, profile, plan, groups, index=None):
    result = enrich_calculation_contracts(data, plan, groups, index)
    candidate = plan.model_copy(deep=True)
    # Recompute only this stage's published edges. New accepted definitions can
    # make an old operand ambiguous; an earlier proof must not survive that change.
    previous_calculations = {item.id: item for item in candidate.relation_types
        if item.endpoint_basis == 'calculation_binding'
        and item.evidence_scope == 'complete_calculation_definition'
        and item.predicate_name == 'calculation_dependency'}
    existing = {item.id: item for item in candidate.relation_types
                if item.id not in previous_calculations}
    published_ids = set()
    proof_updates = []
    supported_roles = {'minuend', 'subtrahend', 'numerator', 'denominator', 'addend', 'factor'}
    for dependency in result['dependencies']:
        role = dependency['operand_role']
        params = {'operand_role': role} if role in supported_roles else {}
        relation_id = canonical_relation_id(
            'depends_on', dependency['source_type_id'], dependency['target_type_id'],
            predicate_name='calculation_dependency', semantic_parameters=params)
        dependency['relation_type_id'] = relation_id
        proposed = DerivedType(
            id=relation_id, label='calculation_dependency', parent='depends_on',
            predicate_name='calculation_dependency', semantic_parameters=params,
            category='business_relation_type',
            definition='The source calculation uses the target definition as an operand; the original expression preserves its role.',
            domain=[dependency['source_type_id']], range=[dependency['target_type_id']],
            endpoint_basis='calculation_binding', evidence_scope='complete_calculation_definition',
            evidence_ids=dependency['evidence_ids'])
        if relation_id in existing:
            old = existing[relation_id]
            if (old.parent != proposed.parent or old.predicate_name != proposed.predicate_name
                    or old.semantic_parameters != proposed.semantic_parameters
                    or old.domain != proposed.domain or old.range != proposed.range):
                raise ValueError('Existing calculation relation has a conflicting signature')
            if relation_id in published_ids:
                proposed.evidence_ids = sorted(set(old.evidence_ids + proposed.evidence_ids))
            if old.endpoint_basis != proposed.endpoint_basis or old.evidence_scope != proposed.evidence_scope:
                proof_updates.append({'relation_type_id': relation_id,
                    'previous_endpoint_basis': old.endpoint_basis, 'previous_evidence_scope': old.evidence_scope,
                    'previous_evidence_ids': old.evidence_ids,
                    'new_endpoint_basis': proposed.endpoint_basis, 'new_evidence_scope': proposed.evidence_scope})
            existing[relation_id] = proposed
        else:
            existing[relation_id] = proposed
        published_ids.add(relation_id)
    retracted = []
    current_complete = {item['source_type_id'] for item in result['calculations']
                        if item['status'] == 'accepted'}
    for relation_id in sorted(previous_calculations.keys() - published_ids):
        old = previous_calculations[relation_id]
        retracted.append({'relation_type_id': relation_id,
            'previous_endpoint_basis': old.endpoint_basis, 'previous_evidence_scope': old.evidence_scope,
            'previous_evidence_ids': old.evidence_ids,
            'reasons': calculation_relation_errors(data, old, candidate.object_types)
                       or ['not_reproduced_by_current_definition_bindings']})
        if not old.domain or old.domain[0] not in current_complete:
            result['coverage']['partial'] = True
    result['coverage']['retracted_calculation_relations'] = len(retracted)
    candidate.relation_types = list(existing.values())
    errors = validate_plan(candidate, data, profile)
    if errors:
        raise ValueError('Invalid calculation contracts: ' + '; '.join(errors))
    return {**result, 'plan': candidate, 'proof_updates': proof_updates,
            'retracted_relations': retracted}
