"""Conservative superclass induction from accepted, source-grounded types.

This is intentionally narrower than semantic similarity. A pair becomes a
subclass assertion only when its complete definition fragments differ by an
explicitly cited specialization term. Other pairs remain retrieval candidates.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Literal

from pydantic import Field

from .models import BuildPlan, DerivedType, Strict
from .storage import digest
from .validation import validate_plan


ROOTS = frozenset(("GeneralObject", "Measure", "Metric", "Dimension", "Term"))
_HAN = re.compile(r"[\u3400-\u9fff]+")
_WORD = re.compile(r"[a-z0-9]{4,}")
_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)


class ChildSupport(Strict):
    type_id: str
    shared_quote: str
    specialization_quote: str
    specialization_term: str


class GeneralizationDecision(Strict):
    status: Literal["proposed", "no_change", "unresolved"]
    label: str = ""
    definition: str = ""
    root_type: Literal["GeneralObject", "Measure", "Metric", "Dimension", "Term"] | None = None
    shared_anchor: str = ""
    parent_scope: dict[str, str] = Field(default_factory=dict)
    unit: str | None = None
    children: list[ChildSupport] = Field(default_factory=list)
    reason: str = ""


def _norm(value):
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _plain(value):
    return _PUNCT.sub("", _norm(value))


def _template(value):
    # Keep arithmetic, comparison and tax/ratio punctuation: a purported
    # shared definition must not turn x+y and x-y into the same template.
    return re.sub(r"\s+", "", _norm(value))


def _semantic_fragments(data, item):
    """Yield complete, same-snapshot description/formula evidence only."""
    for prop in item.source_properties:
        if prop.role not in ("description", "formula"):
            continue
        for evidence_id in prop.evidence_ids:
            evidence = data.evidence.get(evidence_id) or {}
            source = evidence.get("source_ref") or {}
            if (evidence.get("origin") != "observed_record"
                    or evidence.get("raw_fragment_truncated")
                    or source.get("table") != prop.source_table
                    or source.get("column") != prop.source_column
                    or source.get("snapshot_id") != data.snapshot_id):
                continue
            fragment = evidence.get("raw_fragment")
            if isinstance(fragment, str) and fragment.strip():
                yield evidence_id, prop.role, fragment, source.get("record_id")


def _terms(value):
    terms = set()
    for run in _HAN.findall(_norm(value)):
        for size in (2, 3):
            terms.update(run[i:i + size] for i in range(len(run) - size + 1))
    terms.update(_WORD.findall(_norm(value)))
    return terms


def propose_generalization_candidates(plan: BuildPlan, data, max_pairs: int):
    """Recall bounded same-root pairs; this never asserts a superclass.

    Only accepted business types with complete definition/formula evidence
    participate. Common n-grams are retrieval signals, not identity evidence.
    """
    if type(max_pairs) is not int or max_pairs < 0:
        raise ValueError("max_pairs must be a nonnegative integer")
    if not max_pairs:
        return []
    eligible = [item for item in plan.object_types
                if item.category == "business_type" and item.parent in ROOTS
                and item.derivation_kind in (None, "exact_definition")]
    by_group = defaultdict(list)
    for item in eligible:
        fragments = list(_semantic_fragments(data, item))
        if fragments:
            by_group[(item.parent, _norm(item.unit))].append((item, fragments))
    ranked = []
    for (root, unit), entries in by_group.items():
        inverted = defaultdict(list)
        for index, (_, fragments) in enumerate(entries):
            for term in _terms(" ".join(fragment for _, _, fragment, _ in fragments)):
                inverted[term].append(index)
        scores = defaultdict(int)
        pair_budget = max(64, max_pairs * 64)
        # Rare terms rank first, but even a lone two-Han-character term such as
        # 收入 may recall a pair. Per-posting fanout and the global budget avoid
        # building a quadratic number of pairs when that word is widespread.
        for _, indexes in sorted(inverted.items(), key=lambda item: (len(item[1]), item[0])):
            if len(scores) >= pair_budget:
                break
            for position, left in enumerate(indexes):
                if len(scores) >= pair_budget:
                    break
                for right in indexes[position + 1:position + 9]:
                    if len(scores) >= pair_budget:
                        break
                    scores[(left, right)] += 1
        for (left, right), score in scores.items():
            a, b = entries[left][0], entries[right][0]
            ids = sorted((a.id, b.id))
            ranked.append({"child_type_ids": ids, "root_type": root,
                           "unit": unit or None, "lexical_overlap": score,
                           "status": "candidate_only",
                           "candidate_id": "generalization:" + digest(ids)[:24]})
    return sorted(ranked, key=lambda x: (-x["lexical_overlap"], x["child_type_ids"]))[:max_pairs]


def _supporting_fragment(data, child, support):
    """Both quotes must occur in one complete definition record fragment."""
    for evidence_id, role, fragment, record_id in _semantic_fragments(data, child):
        if (support.shared_quote in fragment
                and support.specialization_quote in fragment
                and support.specialization_term in support.specialization_quote):
            return evidence_id, role, fragment, record_id
    raise ValueError("Shared and specialization quotes lack one complete definition fragment")


def _formula_templates(data, child, term):
    # Operators and parentheses are meaningful here: x+y != x-y.
    formulas = {re.sub(r"\s+", "", _norm(fragment).replace(_norm(term), ""))
                for _, role, fragment, _ in _semantic_fragments(data, child)
                if role == "formula"}
    return formulas


def compile_generalization(data, profile, core: BuildPlan, decision):
    """Atomically add one superclass and reparent its cited children.

    Returns ``(candidate_plan, parent_type)`` or ``None`` for a nonproposal.
    Passing checks records source support, not independently verified truth.
    """
    decision = GeneralizationDecision.model_validate(decision)
    if decision.status != "proposed":
        return None
    if (not decision.label.strip() or not decision.definition.strip()
            or not decision.shared_anchor.strip() or decision.root_type not in ROOTS):
        raise ValueError("Generalization lacks label, definition, shared anchor or root")
    if len(decision.children) < 2 or len({x.type_id for x in decision.children}) != len(decision.children):
        raise ValueError("Generalization needs two distinct child types")
    anchor = _plain(decision.shared_anchor)
    if len(anchor) < 2 or (len(anchor) < 4 and not _HAN.fullmatch(anchor)):
        raise ValueError("Shared anchor is too short to ground generalization")
    # Two-character anchors are allowed, but the parent still needs a real
    # definition. The bare label 收入, even quoted twice, proves no superclass.
    if len(_plain(decision.definition)) < max(8, len(anchor) + 4):
        raise ValueError("Shared parent definition lacks semantic detail")
    existing = {item.id: item for item in core.object_types}
    children = []
    for support in decision.children:
        child = existing.get(support.type_id)
        if child is None or child.category != "business_type":
            raise ValueError("Generalization child is not an accepted business type")
        children.append(child)
    units = {_norm(item.unit) for item in children}
    if len(units) != 1 or _norm(decision.unit) not in units:
        raise ValueError("Child units conflict with the proposed parent unit")
    if decision.root_type in ("Metric", "Measure") and not next(iter(units)):
        raise ValueError("Quantitative generalization requires known compatible units")
    common_scope = {key: value for key, value in children[0].applicability_scope.items()
                    if all(item.applicability_scope.get(key) == value for item in children[1:])}
    if decision.parent_scope != common_scope:
        raise ValueError("Parent scope must equal the common applicability scope")
    scope_keys = {frozenset(item.applicability_scope) for item in children}
    if len(scope_keys) != 1:
        raise ValueError("Child applicability scope keys differ")

    label = " ".join(_norm(decision.label).split())
    definition = " ".join(_norm(decision.definition).split())
    parent_id = "type:" + digest(["shared_supertype", decision.root_type, label,
                                   definition, common_scope, _norm(decision.unit)])[:24]
    old_parent = existing.get(parent_id)
    if old_parent and (old_parent.category != "business_type"
                       or old_parent.derivation_kind != "shared_supertype"
                       or old_parent.parent != decision.root_type
                       or old_parent.definition != decision.definition.strip()):
        raise ValueError("Generalized parent ID conflicts with an existing type")

    evidence_ids, templates, terms, record_ids = set(), [], set(), set()
    for child, support in zip(children, decision.children):
        if child.parent not in (decision.root_type, parent_id if old_parent else decision.root_type):
            # The one-parent model cannot silently add a second business parent.
            raise ValueError("Child already has a derived parent or mismatched root")
        if not support.shared_quote.strip() or not support.specialization_quote.strip():
            raise ValueError("Generalization lacks child source quotes")
        term = support.specialization_term.strip()
        if not _plain(term) or _plain(term) in terms:
            raise ValueError("Specialization terms must be distinct")
        terms.add(_plain(term))
        if (_plain(term) in _plain(decision.label)
                or _plain(term) in _plain(decision.definition)):
            raise ValueError("Parent contains a child specialization")
        if (_plain(term) not in _plain(child.label or "")
                and not any(_plain(term) in _plain(value)
                            for value in child.applicability_scope.values())):
            raise ValueError("Specialization term is not a child label or scope qualifier")
        if _plain(decision.label) not in _plain(_norm(child.label or "").replace(_norm(term), "")):
            raise ValueError("Parent label is absent from a specialized child label")
        for key, value in child.applicability_scope.items():
            if key not in common_scope and _plain(term) not in _plain(value):
                raise ValueError("A varying scope value lacks the cited specialization")
        evidence_id, _, fragment, record_id = _supporting_fragment(data, child, support)
        if not record_id or record_id in record_ids:
            raise ValueError("Generalization needs distinct source definition records")
        record_ids.add(record_id)
        if _plain(decision.shared_anchor) not in _plain(support.shared_quote):
            raise ValueError("Shared anchor is absent from a child semantic quote")
        template = _template(_norm(fragment).replace(_norm(term), ""))
        if not template or _plain(decision.shared_anchor) not in template:
            raise ValueError("Child fragment loses shared meaning after specialization removal")
        templates.append(template)
        evidence_ids.add(evidence_id)
    if len(set(templates)) != 1 or _template(decision.definition) != templates[0]:
        raise ValueError("Child definition templates or proposed parent definition conflict")
    formula_sets = [_formula_templates(data, child, support.specialization_term.strip())
                    for child, support in zip(children, decision.children)]
    if any(len(formulas) > 1 for formulas in formula_sets) or any(
            formulas != formula_sets[0] for formulas in formula_sets[1:]):
        raise ValueError("Child calculation formulas are incompatible")
    evidence_ids.update(evidence_id for child in children
                        for evidence_id, role, _, _ in _semantic_fragments(data, child)
                        if role == "formula")

    child_ids = {item.id for item in children}
    if old_parent:
        if (set(old_parent.induced_from_type_ids) == child_ids
                and all(item.parent == parent_id for item in children)
                and set(old_parent.evidence_ids) == evidence_ids):
            return core.model_copy(deep=True), old_parent
        raise ValueError("Existing generalized parent has different children or evidence")
    parent = DerivedType(
        id=parent_id, parent=decision.root_type, label=decision.label.strip(),
        definition=decision.definition.strip(), category="business_type",
        unit=decision.unit, derivation_kind="shared_supertype",
        induced_from_type_ids=sorted(item.id for item in children),
        applicability_scope=common_scope,
        evidence_scope="multiple_definition_records",
        evidence_ids=sorted(evidence_ids),
        source_concept_ids=sorted({concept_id for item in children
                                   for concept_id in item.source_concept_ids}),
    )
    candidate = core.model_copy(deep=True)
    candidate.object_types.append(parent)
    candidate.object_types = [item.model_copy(update={"parent": parent_id})
                              if item.id in child_ids else item
                              for item in candidate.object_types]
    errors = validate_plan(candidate, data, profile)
    if errors:
        raise ValueError("Generalized plan invalid: " + "; ".join(errors))
    return candidate, parent
