"""Budgeted LLM decision stage for evidence-grounded superclass induction.

Candidate retrieval and source-packet construction are deterministic. The
caller supplies the structured-model callback; a candidate is never accepted
until that callback proposes it and ``compile_generalization`` validates it.
"""

from __future__ import annotations

import inspect
import json

from .group_incremental import ROOTS
from .llm import BudgetExceeded
from .type_generalization import (
    GeneralizationDecision, _semantic_fragments, compile_generalization,
    propose_generalization_candidates,
)


def _packet(data, candidate, types):
    source_types = []
    for type_id in candidate["child_type_ids"]:
        item = types[type_id]
        fragments = []
        for evidence_id, role, value, record_id in _semantic_fragments(data, item):
            evidence = data.evidence[evidence_id]
            fragments.append({
                "evidence_id": evidence_id, "role": role, "value": value,
                "record_id": record_id, "source_ref": evidence["source_ref"],
                "truncated": False,
            })
        source_types.append({
            "id": item.id, "label": item.label, "definition": item.definition,
            "root_type": item.parent, "unit": item.unit,
            "applicability_scope": item.applicability_scope,
            "derivation_kind": item.derivation_kind,
            "definition_fragments": fragments,
        })
    return {
        "task_kind": "shared_supertype_decision",
        "candidate": candidate,
        "source_types": source_types,
        "constraints": {
            "candidate_similarity_is_not_identity": True,
            "quotes_must_be_from_complete_description_or_formula": True,
            "one_parent_per_child": True,
            "observation_coordinates_are_not_type_identity": True,
            "object_relations_are_not_lifted": True,
            "parent_scope_is_shared_applicability_only": True,
        },
    }


async def run_generalization(data, profile, core, decide, *, max_pairs=20,
                             max_decisions=10, max_packet_bytes=16000):
    """Return a candidate plan plus auditable per-pair and coverage results.

    ``decide(packet)`` may be async or sync and must return a
    ``GeneralizationDecision`` or its dict representation. The stage counts
    callback invocations, not provider retries or wire requests; the caller's
    global LLM budget remains authoritative. ``max_packet_bytes`` bounds only
    this evidence packet, so the caller must also bound its full request.
    """
    if any(type(value) is not int or value < 0
           for value in (max_pairs, max_decisions)):
        raise ValueError("Generalization pair and decision limits must be nonnegative integers")
    if type(max_packet_bytes) is not int or max_packet_bytes < 1:
        raise ValueError("max_packet_bytes must be a positive integer")
    if decide is None:
        raise ValueError("A structured decision callback is required")

    candidates = propose_generalization_candidates(core, data, max_pairs)
    types = {item.id: item for item in core.object_types}
    eligible = {item.id for item in core.object_types
                if item.category == "business_type" and item.parent in ROOTS
                and item.derivation_kind in (None, "exact_definition")
                and any(_semantic_fragments(data, item))}
    candidate_type_ids = {type_id for candidate in candidates
                          for type_id in candidate["child_type_ids"]}
    current = core.model_copy(deep=True)
    steps = []
    invocations = 0
    exhausted = False
    for candidate in candidates:
        step = {"candidate_id": candidate["candidate_id"],
                "child_type_ids": candidate["child_type_ids"],
                "retrieval_status": "candidate_only"}
        current_types = {item.id: item for item in current.object_types}
        if any(current_types[type_id].parent != candidate["root_type"]
               for type_id in candidate["child_type_ids"]):
            step.update(status="skipped_existing_parent",
                        reason="A child was already generalized by an earlier accepted pair")
            steps.append(step)
            continue
        if exhausted or invocations >= max_decisions:
            step.update(status="budget_exhausted", reason="max_decisions")
            steps.append(step)
            continue
        packet = _packet(data, candidate, current_types)
        packet_bytes = len(json.dumps(packet, ensure_ascii=False,
                                      separators=(",", ":"), default=str).encode("utf-8"))
        step["packet_bytes"] = packet_bytes
        if packet_bytes > max_packet_bytes:
            step.update(status="packet_over_budget", reason="Complete source packet exceeds limit")
            steps.append(step)
            continue
        invocations += 1
        try:
            result = decide(packet)
            if inspect.isawaitable(result):
                result = await result
            decision = GeneralizationDecision.model_validate(result)
            step["decision_status"] = decision.status
            if decision.status == "proposed":
                compiled = compile_generalization(data, profile, current, decision)
                if compiled is None:
                    raise ValueError("Proposed decision did not compile")
                current, parent = compiled
                step.update(status="accepted", parent_type_id=parent.id)
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

    statuses = {name: sum(step["status"] == name for step in steps) for name in
                ("accepted", "no_change", "unresolved", "rejected", "error",
                 "budget_exhausted", "packet_over_budget", "skipped_existing_parent")}
    unselected = sorted(eligible - candidate_type_ids)
    coverage = {
        "candidate_generation": "bounded_lexical_recall_candidate_only",
        "all_possible_pairs_enumerated": False,
        "eligible_source_type_count": len(eligible),
        "candidate_pairs_returned": len(candidates),
        "types_in_candidate_pairs": len(candidate_type_ids),
        "eligible_types_outside_candidate_pairs": len(unselected),
        "eligible_types_outside_candidate_pairs_sample": unselected[:20],
        "model_decision_invocations": invocations,
        "provider_wire_calls_including_retries": "tracked_by_caller",
        "statuses": statuses,
        "quality_status": "unjudged_without_independent_business_gold",
    }
    partial = bool(statuses["unresolved"] or statuses["rejected"]
                   or statuses["error"] or statuses["budget_exhausted"]
                   or statuses["packet_over_budget"] or unselected)
    return {"plan": current, "steps": steps, "coverage": coverage,
            "partial": partial}
