"""Conservative equivalence of accepted business types from full source definitions.

Equal names are candidate retrieval only.  The compiler rechecks both complete
definition records and never edits the accepted ontology plan; callers may use
the returned canonical map only for pairs whose entire same-name group passed.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from itertools import combinations
from typing import Literal

from .models import BuildPlan, DerivedType, Strict
from .storage import digest


ROOTS = frozenset(("GeneralObject", "Measure", "Metric", "Dimension", "Term"))
_SPACE = re.compile(r"\s+")
_PROSE_PUNCT = re.compile(r"[\s，,。.;；:：、]+")


class EquivalenceDecision(Strict):
    status: Literal["proposed", "no_change", "unresolved"]
    source_type_id: str = ""
    target_type_id: str = ""
    source_description_evidence_id: str = ""
    target_description_evidence_id: str = ""
    # Full, byte-for-byte source descriptions.  Short substrings could hide a
    # qualifier or negation in the omitted portion.
    source_description_quote: str = ""
    target_description_quote: str = ""
    source_formula_evidence_id: str | None = None
    target_formula_evidence_id: str | None = None
    source_formula_quote: str | None = None
    target_formula_quote: str | None = None
    semantic_equivalence_explanation: str = ""
    reason: str = ""


def _norm(value):
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _description_key(value):
    return _PROSE_PUNCT.sub("", _norm(value))


def _formula_key(value):
    expression = _SPACE.sub("", _norm(value))
    # The written name left of an assignment is not a calculation operand.
    assignment = re.fullmatch(r"[\w\u3400-\u9fff.]+=([^=].*)", expression)
    return assignment.group(1) if assignment else expression


def _root_of(item: DerivedType, by_id):
    parent, seen = item.parent, {item.id}
    while parent in by_id:
        if parent in seen:
            return None
        seen.add(parent)
        parent = by_id[parent].parent
    return parent if parent in ROOTS else None


def _fragments(data, item):
    """Return all complete description/formula fragments or ``None``.

    One invalid fragment makes the type ineligible; we cannot selectively hide
    a second, contradictory source definition from the comparison packet.
    """
    result = {"description": [], "formula": []}
    cited = set(item.evidence_ids)
    for prop in item.source_properties:
        if prop.role not in result:
            continue
        if not prop.evidence_ids:
            return None
        for evidence_id in prop.evidence_ids:
            evidence = data.evidence.get(evidence_id) or {}
            ref = evidence.get("source_ref") or {}
            raw = evidence.get("raw_fragment")
            if (evidence_id not in cited
                    or evidence.get("origin") != "observed_record"
                    or evidence.get("raw_fragment_truncated")
                    or not isinstance(raw, str) or not raw.strip()
                    or ref.get("table") != prop.source_table
                    or ref.get("column") != prop.source_column
                    or ref.get("snapshot_id") != data.snapshot_id
                    or not ref.get("record_id")):
                return None
            result[prop.role].append({
                "evidence_id": evidence_id, "value": raw,
                "record_id": ref["record_id"], "source_ref": ref,
            })
    if not result["description"]:
        return None
    for role in result:
        result[role].sort(key=lambda entry: entry["evidence_id"])
    return result


def _eligible_groups(plan, data):
    by_id = {item.id: item for item in plan.object_types}
    groups = defaultdict(list)
    for item in plan.object_types:
        if (item.category != "business_type"
                or item.derivation_kind != "exact_definition"
                or item.evidence_scope != "definition_record"
                or not item.label or not item.definition):
            continue
        root = _root_of(item, by_id)
        if root is None:
            continue
        unit = _norm(item.unit)
        # Two unknown units do not establish a compatible quantitative unit.
        if root in ("Metric", "Measure") and not unit:
            continue
        fragments = _fragments(data, item)
        if fragments is None:
            continue
        scope = tuple(sorted((_norm(k), _norm(v))
                              for k, v in item.applicability_scope.items()))
        key = (root, _norm(item.label), unit, scope)
        groups[key].append((item, fragments))
    for values in groups.values():
        values.sort(key=lambda pair: pair[0].id)
    return groups


def _candidate(data, group_key, source, target):
    left, right = source[0], target[0]
    return {
        "candidate_id": "equivalence:" + digest([data.snapshot_id, left.id, right.id])[:24],
        "source_type_id": left.id, "target_type_id": right.id,
        "root_type": group_key[0], "label": left.label,
        "unit": left.unit, "applicability_scope": dict(group_key[3]),
        "status": "candidate_only",
    }


def propose_equivalence_candidates(plan: BuildPlan, data, max_pairs: int):
    """Bounded same-name recall; a candidate is never an identity assertion."""
    if type(max_pairs) is not int or max_pairs < 0:
        raise ValueError("max_pairs must be a nonnegative integer")
    groups = _eligible_groups(plan, data)
    result = []
    for key, members in sorted(groups.items()):
        for source, target in combinations(members, 2):
            if len(result) >= max_pairs:
                return result
            result.append(_candidate(data, key, source, target))
    return result


def _selected_fragment(fragments, role, evidence_id, quote):
    if not evidence_id or not quote:
        raise ValueError(f"Missing full {role} source quotation")
    found = next((fragment for fragment in fragments[role]
                  if fragment["evidence_id"] == evidence_id), None)
    if found is None or found["value"] != quote:
        raise ValueError(f"{role} quotation must equal its complete source fragment")
    return found


def _single_meaning(fragments, role, key):
    values = {key(fragment["value"]) for fragment in fragments[role]}
    if len(values) > 1:
        raise ValueError(f"A type has conflicting complete {role} sources")
    return next(iter(values), None)


def compile_equivalence(data, core: BuildPlan, candidate, decision):
    """Validate one model decision and emit source-backed pair evidence.

    No plan mutation occurs.  The stage decides whether a whole same-name
    group forms a complete equivalence class and may publish a canonical map.
    """
    decision = EquivalenceDecision.model_validate(decision)
    if decision.status != "proposed":
        return None
    source_id, target_id = candidate.get("source_type_id"), candidate.get("target_type_id")
    if (not source_id or not target_id or source_id == target_id
            or decision.source_type_id != source_id
            or decision.target_type_id != target_id):
        raise ValueError("Decision does not name the exact candidate pair")
    groups = _eligible_groups(core, data)
    group = next((members for members in groups.values()
                  if {source_id, target_id} <= {item.id for item, _ in members}), None)
    if group is None:
        raise ValueError("Pair lacks same-root, same-name, same-unit, same-scope complete definitions")
    by_id = {item.id: (item, fragments) for item, fragments in group}
    source, source_fragments = by_id[source_id]
    target, target_fragments = by_id[target_id]
    group_key = next(key for key, members in groups.items()
                     if {source_id, target_id} <= {item.id for item, _ in members})
    expected = _candidate(data, group_key, by_id[source_id], by_id[target_id])
    if candidate.get("candidate_id") != expected["candidate_id"]:
        raise ValueError("Candidate identity changed")
    left = _selected_fragment(source_fragments, "description",
                              decision.source_description_evidence_id,
                              decision.source_description_quote)
    right = _selected_fragment(target_fragments, "description",
                               decision.target_description_evidence_id,
                               decision.target_description_quote)
    if len(decision.semantic_equivalence_explanation.strip()) < 6:
        raise ValueError("Equivalence requires a substantive semantic explanation")
    source_description = _single_meaning(source_fragments, "description", _description_key)
    target_description = _single_meaning(target_fragments, "description", _description_key)
    source_formula = _single_meaning(source_fragments, "formula", _formula_key)
    target_formula = _single_meaning(target_fragments, "formula", _formula_key)
    one_formula_missing = (source_formula is None) != (target_formula is None)
    if one_formula_missing:
        # A formula written in a *description* is still source text.  Accept
        # this narrow case only if the missing-side complete description is
        # exactly the same assignment/expression after whitespace removal.
        present_formula = source_formula or target_formula
        missing_descriptions = (target_fragments["description"] if source_formula
                                else source_fragments["description"])
        if (not present_formula or not all(
                _formula_key(x["value"]) == present_formula
                for x in missing_descriptions)):
            raise ValueError("A formula exists on only one side without the same full expression in its definition")
    if source_formula is not None:
        _selected_fragment(source_fragments, "formula",
                           decision.source_formula_evidence_id,
                           decision.source_formula_quote)
    elif decision.source_formula_evidence_id or decision.source_formula_quote:
        raise ValueError("Formula quotation supplied without source formula")
    if target_formula is not None:
        _selected_fragment(target_fragments, "formula",
                           decision.target_formula_evidence_id,
                           decision.target_formula_quote)
    elif decision.target_formula_evidence_id or decision.target_formula_quote:
        raise ValueError("Formula quotation supplied without target formula")
    if source_formula is not None and target_formula is not None and source_formula != target_formula:
        raise ValueError("Complete calculation formulas differ")
    if source_description != target_description:
        # No fixed qualifier vocabulary can cover all scope distinctions.  An
        # identical formula with e.g. 直营 vs 加盟 is still a different type.
        # Paraphrase alignment needs a separate, evidence-backed mechanism;
        # this canonical map accepts only equal complete descriptions.
        raise ValueError("Complete source descriptions differ")
    source_records = {(x["source_ref"]["table"], x["record_id"])
                      for x in source_fragments["description"]}
    target_records = {(x["source_ref"]["table"], x["record_id"])
                      for x in target_fragments["description"]}
    if source_records & target_records:
        raise ValueError("Two types cite the same definition record; classification conflict")
    canonical = min(source_id, target_id)
    return {
        "id": "type_equivalence:" + digest([data.snapshot_id, source_id, target_id])[:24],
        "source_type_id": source_id, "target_type_id": target_id,
        "canonical_type_id": canonical,
        "root_type": group_key[0], "label": source.label,
        "unit": source.unit, "applicability_scope": source.applicability_scope,
        "source_description_evidence_ids": [x["evidence_id"] for x in source_fragments["description"]],
        "target_description_evidence_ids": [x["evidence_id"] for x in target_fragments["description"]],
        "source_formula_evidence_ids": [x["evidence_id"] for x in source_fragments["formula"]],
        "target_formula_evidence_ids": [x["evidence_id"] for x in target_fragments["formula"]],
        "source_description_quote": left["value"],
        "target_description_quote": right["value"],
        "source_formula_quote": decision.source_formula_quote,
        "target_formula_quote": decision.target_formula_quote,
        "semantic_equivalence_explanation": decision.semantic_equivalence_explanation.strip(),
        "evidence_ids": sorted({x["evidence_id"] for values in
                                (source_fragments, target_fragments)
                                for role in ("description", "formula") for x in values[role]}),
        "identity_scope": "accepted_type_equivalence_in_source_snapshot",
    }
