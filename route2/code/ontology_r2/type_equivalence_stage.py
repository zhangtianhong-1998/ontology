"""Bounded evidence-backed decisions for equivalence of accepted types."""

from __future__ import annotations

import inspect
import json
from itertools import combinations

from .llm import BudgetExceeded
from .type_equivalence import (
    EquivalenceDecision, _eligible_groups, compile_equivalence,
    propose_equivalence_candidates,
)


def _packet(data, candidate, types):
    source_types = []
    for type_id in (candidate["source_type_id"], candidate["target_type_id"]):
        item, fragments = types[type_id]
        source_types.append({
            "id": item.id, "root_type": candidate["root_type"],
            "label": item.label, "definition": item.definition,
            "unit": item.unit, "applicability_scope": item.applicability_scope,
            "complete_source_definitions": [
                {"role": role, "evidence_id": fragment["evidence_id"],
                 "value": fragment["value"], "source_ref": fragment["source_ref"]}
                for role in ("description", "formula")
                for fragment in fragments[role]
            ],
        })
    return {
        "task_kind": "accepted_type_equivalence",
        "candidate": candidate,
        "source_types": source_types,
        "constraints": {
            "same_name_is_only_a_candidate": True,
            "quote_the_full_original_description_on_both_sides": True,
            "quote_both_full_original_formulas_if_present": True,
            "formula_or_business_qualifier_conflict_means_no_equivalence": True,
            "do_not_merge_observation_coordinates": True,
            "do_not_invent_a_business_identity_from_a_shared_name": True,
        },
    }


async def run_type_equivalence(data, core, decide, *, max_pairs=20,
                               max_decisions=10, max_packet_bytes=16000):
    """Return canonical mappings only for completely verified same-name groups.

    ``decide(packet)`` returns an ``EquivalenceDecision`` or compatible dict.
    Candidate retrieval and each quote check are deterministic; one callback
    invocation is counted per considered pair, while provider retries remain
    the caller's global-budget responsibility.
    """
    if any(type(value) is not int or value < 0 for value in (max_pairs, max_decisions)):
        raise ValueError("Equivalence pair and decision limits must be nonnegative integers")
    if type(max_packet_bytes) is not int or max_packet_bytes < 1:
        raise ValueError("max_packet_bytes must be a positive integer")
    if decide is None:
        raise ValueError("A structured decision callback is required")
    groups = _eligible_groups(core, data)
    same_name_groups = [(key, members) for key, members in sorted(groups.items())
                        if len(members) > 1]
    possible_pairs = sum(len(members) * (len(members) - 1) // 2
                         for _, members in same_name_groups)
    candidates = propose_equivalence_candidates(core, data, max_pairs)
    by_type = {item.id: (item, fragments) for members in groups.values()
               for item, fragments in members}
    steps, assertions = [], []
    invocations = 0
    exhausted = False
    accepted_pairs = {}
    for candidate in candidates:
        pair = frozenset((candidate["source_type_id"], candidate["target_type_id"]))
        step = {"candidate_id": candidate["candidate_id"],
                "source_type_id": candidate["source_type_id"],
                "target_type_id": candidate["target_type_id"],
                "retrieval_status": "candidate_only"}
        if exhausted or invocations >= max_decisions:
            step.update(status="budget_exhausted", reason="max_decisions")
            steps.append(step)
            continue
        packet = _packet(data, candidate, by_type)
        packet_bytes = len(json.dumps(packet, ensure_ascii=False,
                                      separators=(",", ":"), default=str).encode("utf-8"))
        step["packet_bytes"] = packet_bytes
        if packet_bytes > max_packet_bytes:
            step.update(status="packet_over_budget",
                        reason="Complete source packet exceeds limit")
            steps.append(step)
            continue
        invocations += 1
        try:
            decision = decide(packet)
            if inspect.isawaitable(decision):
                decision = await decision
            decision = EquivalenceDecision.model_validate(decision)
            step["decision_status"] = decision.status
            if decision.status == "proposed":
                assertion = compile_equivalence(data, core, candidate, decision)
                if assertion is None:
                    raise ValueError("Proposed equivalence did not compile")
                accepted_pairs[pair] = assertion
                assertions.append(assertion)
                step.update(status="accepted", equivalence_assertion_id=assertion["id"])
            else:
                step.update(status=decision.status, reason=decision.reason)
        except BudgetExceeded as exc:
            exhausted = True
            step.update(status="budget_exhausted", reason=type(exc).__name__)
        except ValueError as exc:
            step.update(status="rejected", reason=str(exc))
        except Exception as exc:
            step.update(status="error", reason=type(exc).__name__)
        steps.append(step)
    canonical_map = {}
    accepted_group_count = 0
    incomplete_groups = []
    for _, members in same_name_groups:
        ids = [item.id for item, _ in members]
        pairs = [frozenset(pair) for pair in combinations(ids, 2)]
        if all(pair in accepted_pairs for pair in pairs):
            canonical = min(ids)
            canonical_map.update({type_id: canonical for type_id in ids})
            for pair in pairs:
                accepted_pairs[pair]["canonical_type_id"] = canonical
            accepted_group_count += 1
        else:
            incomplete_groups.append(ids)
    statuses = {name: sum(step["status"] == name for step in steps) for name in
                ("accepted", "no_change", "unresolved", "rejected", "error",
                 "budget_exhausted", "packet_over_budget")}
    coverage = {
        "candidate_generation": "bounded_same_root_name_unit_scope_with_complete_definitions",
        "eligible_source_type_count": sum(len(members) for members in groups.values()),
        "same_name_group_count": len(same_name_groups),
        "all_possible_pairs": possible_pairs,
        "candidate_pairs_returned": len(candidates),
        "candidate_pairs_not_returned": max(0, possible_pairs - len(candidates)),
        "model_decision_invocations": invocations,
        "provider_wire_calls_including_retries": "tracked_by_caller",
        "accepted_pair_assertions": len(assertions),
        "complete_equivalence_groups": accepted_group_count,
        "incomplete_group_count": len(incomplete_groups),
        "incomplete_group_sample": incomplete_groups[:20],
        "statuses": statuses,
        "quality_status": "source_checked_model_decision_without_independent_business_gold",
    }
    return {
        "canonical_map": canonical_map,
        "equivalence_assertions": assertions,
        "steps": steps,
        "coverage": coverage,
        # A fully checked no_change says the named types are distinct; that is
        # a complete negative decision, not an unfinished run. Missing or
        # failed decisions still leave an explicit partial coverage report.
        "partial": bool(possible_pairs > len(candidates)
                        or any(statuses[name] for name in (
                            "unresolved", "rejected", "error", "budget_exhausted",
                            "packet_over_budget"))),
    }
