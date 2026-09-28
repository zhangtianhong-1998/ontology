"""Bounded later rounds for identified gaps, preserving the first-pass result."""
from __future__ import annotations

from .group_incremental import construct_from_bundles
from .storage import digest


def repair_settings(options, total_calls):
    options = options or {}
    if not isinstance(options, dict):
        raise ValueError('targeted_repair must be a mapping')
    defaults = {'enabled': False, 'max_rounds': 2, 'max_tasks_per_round': 24,
                'max_examples_per_task': 3, 'max_calls_per_round': min(100, total_calls),
                'reserve_calls_for_followup': 0}
    if set(options) - set(defaults):
        raise ValueError('Unknown targeted_repair settings')
    result = {**defaults, **options}
    if type(result['enabled']) is not bool:
        raise ValueError('targeted_repair.enabled must be boolean')
    bounds = {'max_rounds': (1, 5), 'max_tasks_per_round': (1, 1000),
              'max_examples_per_task': (1, 10), 'max_calls_per_round': (0, total_calls),
              'reserve_calls_for_followup': (0, total_calls)}
    for key, (lo, hi) in bounds.items():
        if type(result[key]) is not int or not lo <= result[key] <= hi:
            raise ValueError(f'targeted_repair.{key} must be an integer in {lo}..{hi}')
    return result


def _semantic_digest(result):
    return digest({key: result.get(key, []) for key in
                   ('template_projections', 'concepts', 'concept_relations')})


async def run_targeted_repair(data, profile, bundles, result, llm, options, *,
                              bundle_options=None, progress=None, on_checkpoint=None, index=None):
    """Reuse the accepted core; only actual failed/partial patterns reach a model."""
    from .template_reuse import build_targeted_repair_tasks

    settings = repair_settings(options, llm.config.get('max_calls', 100))
    report = {'rounds': [], 'tasks': [], 'observed_value_tasks': [], 'coverage': {'status': 'disabled',
              'rounds_executed': 0, 'model_calls': 0, 'partial': False}}
    if not settings['enabled'] or settings['max_rounds'] == 1:
        return result, report
    start_calls = llm.calls
    bundle_options = bundle_options or {}
    first_coverage = dict(result.get('coverage', {}))
    first_skipped = result.get('bundles_skipped', 0)
    stop_reason = 'round_limit'
    for number in range(2, settings['max_rounds'] + 1):
        tasks = build_targeted_repair_tasks(
            data, bundles, result, max_tasks=settings['max_tasks_per_round'],
            max_examples=settings['max_examples_per_task'], round_number=number)
        report['tasks'].extend(tasks['tasks'])
        observed = {item['id']: item for item in report['observed_value_tasks']}
        observed.update({item['id']: item for item in tasks.get('observed_value_tasks', [])})
        report['observed_value_tasks'] = list(observed.values())
        if index is not None and report['observed_value_tasks']:
            from .template_reuse import lookup_observed_value_sources
            lookup = lookup_observed_value_sources(data, index, report['observed_value_tasks'])
            report['observed_value_tasks'] = lookup['tasks']
            report['observed_value_lookup'] = lookup['coverage']
        row = {'round': number, 'discovery': tasks['coverage'], 'calls_before': llm.calls}
        if not tasks['bundles']:
            stop_reason = 'no_targeted_tasks'
            break
        total = llm.config.get('max_calls', 100)
        ceiling = min(total - settings['reserve_calls_for_followup'],
                      llm.calls + settings['max_calls_per_round'])
        if llm.calls >= ceiling:
            report['rounds'].append({**row, 'status': 'budget_exhausted', 'model_calls': 0})
            stop_reason = 'budget_exhausted'
            break
        before = _semantic_digest(result)
        with llm.reserve_calls(total - ceiling, stage=f'targeted_repair_round_{number}'):
            result = await construct_from_bundles(
                data, profile, result['plan'], tasks['bundles'], llm, prior_result=result,
                max_bundles=len(tasks['bundles']), review=bundle_options.get('review', True),
                max_repairs_per_bundle=bundle_options.get('max_repairs_per_bundle', 0),
                enable_template_projection=True, concept_batch_size=1, relation_batch_size=1,
                progress=progress, on_checkpoint=on_checkpoint)
        row.update(status='partial' if result.get('partial') else 'complete',
                   model_calls=llm.calls - row['calls_before'], coverage=result.get('coverage', {}))
        report['rounds'].append(row)
        if _semantic_digest(result) == before:
            stop_reason = 'no_semantic_change'
            break
    # A later small queue must not erase the original input coverage limits.
    # New group checkpoints retain the original source inventory and may prove
    # formerly unattempted records through a repair. Legacy callers lack it.
    if 'source_bundles_not_attempted' not in result.get('coverage', {}):
        result['bundles_skipped'] = max(first_skipped, result.get('bundles_skipped', 0))
    result['partial'] = bool(result.get('partial') or result['bundles_skipped'])
    result.setdefault('coverage', {})['first_pass'] = first_coverage
    partial = bool(result['partial'] or report['observed_value_tasks'])
    report['coverage'] = {'status': 'partial' if partial else 'complete',
                         'rounds_executed': len(report['rounds']), 'stop_reason': stop_reason,
                         'model_calls': llm.calls - start_calls,
                         'first_pass_unattempted': first_skipped,
                         'observed_value_tasks': len(report['observed_value_tasks']),
                         'partial': partial}
    return result, report
