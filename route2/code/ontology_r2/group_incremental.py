"""Evidence-bundle decisions, separate from table types and physical join rules."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .models import BuildPlan, Condition, DerivedType, RelationPlan, Strict
from .storage import digest
from .validation import validate_plan


ROOTS = ("GeneralObject", "Measure", "Metric", "Dimension", "Term")
OBJECT_RELATIONS = ("contains", "depends_on", "related_to", "points_to")


class RecordAlignmentDecision(Strict):
    record_id: str
    mapping_kind: Literal["exact", "narrower", "related", "unresolved"]
    quote: str


class ConceptBundleDecision(Strict):
    status: Literal["proposed", "no_change", "unresolved"]
    label: str = ""
    definition: str = ""
    root_type: Literal["GeneralObject", "Measure", "Metric", "Dimension", "Term"] | None = None
    scope: dict[str, str] = Field(default_factory=dict)
    alignments: list[RecordAlignmentDecision] = Field(default_factory=list)
    reason: str = ""


class RelationBundleDecision(Strict):
    status: Literal["proposed", "no_change", "unresolved"]
    parent_relation: Literal["contains", "depends_on", "related_to", "points_to"] | None = None
    label: str = ""
    definition: str = ""
    source_quote: str = ""
    target_quote: str = ""
    reason: str = ""


class BundleReview(Strict):
    accepted: bool
    errors: list[str] = Field(default_factory=list)


def _entries(record):
    for role, values in record.get("fields", {}).items():
        for item in values:
            if isinstance(item, dict) and item.get("value") is not None:
                yield role, item


def _quote_evidence(data, record, quote, *, allowed_roles=None, excluded_columns=(),
                    require_complete=False):
    """Require a verbatim, visible source fragment before adding record evidence."""
    if not quote or not quote.strip():
        raise ValueError("Bundle decision lacks a nonempty source quote")
    for role, entry in _entries(record):
        value = str(entry["value"])
        column = entry["column"]
        if column in excluded_columns or (allowed_roles is not None and role not in allowed_roles):
            continue
        if quote not in value:
            continue
        if require_complete and entry.get("truncated"):
            continue
        evidence_id = "record:" + digest([data.snapshot_id, record["record_id"], column])[:24]
        data.evidence[evidence_id] = {
            "id": evidence_id,
            "origin": "observed_record",
            "raw_fragment": value,
            "raw_fragment_truncated": bool(entry.get("truncated")),
            "source_ref": {"table": record["table"], "record_id": record["record_id"],
                           "row": record.get("row_number"), "column": column,
                           "snapshot_id": data.snapshot_id},
        }
        return evidence_id
    raise ValueError("Quote is absent from the cited bundle record")


def _record_map(bundle):
    records = bundle.get("records", [])
    by_id = {record["record_id"]: record for record in records}
    if len(by_id) != len(records):
        raise ValueError("Duplicate record in evidence bundle")
    return by_id


def _member_or_field_record(record):
    tokens = set(record.get("table", "").casefold().replace(".", "_").split("_"))
    return bool(tokens & {"member", "field", "column"})


def _child_surface(table_name):
    tokens = set(table_name.casefold().replace(".", "_").split("_"))
    return bool(tokens & {"member", "field", "column", "param", "item", "value"})


_RELATION_QUOTE_ROLES = frozenset(("name", "alias", "description", "formula", "scope", "unit", "unknown", "context"))


def _supports_quote(record, quote, key_field):
    return bool(quote) and any(
        role in _RELATION_QUOTE_ROLES and entry["column"] != key_field
        and not entry.get("truncated")
        and quote in str(entry["value"])
        for role, entry in _entries(record))


def compile_concept(data, profile, bundle, decision, accepted_exact):
    """Validate a proposed business object and record-to-concept source mappings."""
    if decision.status != "proposed":
        return None
    if not decision.label.strip() or not decision.definition.strip() or decision.root_type is None:
        raise ValueError("Proposed concept lacks label, definition or root type")
    roots = {item["id"] for item in profile["object_roots"]}
    if decision.root_type not in roots:
        raise ValueError("Unknown concept root type")
    records = _record_map(bundle)
    if not records or not decision.alignments:
        raise ValueError("Concept has no source records")
    for key, value in decision.scope.items():
        if not any(record.get("scope", {}).get(key) == value for record in records.values()):
            raise ValueError("Concept scope is absent from the bundle records")
    if len({item.record_id for item in decision.alignments}) != len(decision.alignments):
        raise ValueError("Duplicate concept alignment for one record")
    exact_records, selected = [], []
    for item in decision.alignments:
        record = records.get(item.record_id)
        if record is None:
            raise ValueError("Concept alignment refers outside the evidence bundle")
        if item.mapping_kind == "unresolved":
            continue
        if item.mapping_kind == "exact":
            if record.get("root_hint") and record["root_hint"] != decision.root_type:
                raise ValueError("Exact source record root differs from proposed concept root")
            if _member_or_field_record(record):
                raise ValueError("Member or field record cannot be exact to its parent concept")
            if any(entry.get("truncated") for role, entry in _entries(record)
                   if role in ("name", "alias", "description", "formula", "unit", "scope")):
                raise ValueError("Exact concept alignment has truncated semantic fields")
            if any(record.get("scope", {}).get(key) != value
                   for key, value in decision.scope.items()):
                raise ValueError("Concept scope conflicts with an exact source record")
            exact_records.append(record)
        selected.append((item, record))
    if not exact_records:
        raise ValueError("New concept requires an exact source definition record")
    exact_scope = {}
    for record in exact_records:
        for key, value in (record.get("scope") or {}).items():
            if key in exact_scope and exact_scope[key] != value:
                raise ValueError("Conflicting scopes cannot share an exact concept")
            exact_scope[key] = value
    exact_units = {str(record.get("unit") or "").strip().casefold()
                   for record in exact_records if str(record.get("unit") or "").strip()}
    if len(exact_units) > 1:
        raise ValueError("Conflicting units cannot share an exact concept")
    unit = next(iter(exact_units), "")
    effective_scope = {**exact_scope, **decision.scope}
    normalized_label = " ".join(decision.label.casefold().split())
    concept_id = "concept:" + digest([decision.root_type, normalized_label,
                                       effective_scope, unit])[:24]
    alignments, all_evidence = [], set()
    for item, record in selected:
        if item.mapping_kind == "exact":
            prior = accepted_exact.get(item.record_id)
            if prior and prior != concept_id:
                raise ValueError("Record already has a conflicting exact concept")
        evidence_id = _quote_evidence(data, record, item.quote,
                                      allowed_roles=("name", "alias", "description", "formula",
                                                     "unit", "scope", "unknown"),
                                      require_complete=item.mapping_kind == "exact")
        all_evidence.add(evidence_id)
        alignments.append({
            "id": "alignment:" + digest([data.snapshot_id, item.record_id, concept_id, item.mapping_kind])[:24],
            "source_record_id": item.record_id,
            "concept_id": concept_id,
            "mapping_kind": item.mapping_kind,
            "scope": effective_scope,
            "evidence_ids": [evidence_id],
            "decision": "accepted_by_automatic_checks",
        })
    if not alignments:
        raise ValueError("All concept alignments are unresolved")
    concept = {"id": concept_id, "type": decision.root_type, "label": decision.label.strip(),
               "definition": decision.definition.strip(), "identity_scope": "snapshot_only",
               "scope": effective_scope, "unit": unit or None,
               "source_refs": [{"record_id": item["source_record_id"]}
                                                      for item in alignments],
               "evidence_ids": sorted(all_evidence),
               "decision": "accepted_by_automatic_checks"}
    return concept, alignments


def compile_relation(data, profile, core, bundle, decision):
    """Turn a technically checked rule and quoted meaning into a validated plan."""
    if decision.status != "proposed":
        return None
    rule = bundle.get("rule") or {}
    if rule.get("status") != "checked_technical":
        raise ValueError("Relation bundle has no checked technical rule")
    if (bundle.get("snapshot_id") != data.snapshot_id or
            rule.get("snapshot_id") != data.snapshot_id or
            rule.get("verification", {}).get("scan_scope") != "full_input"):
        raise ValueError("Relation bundle is not verified on this complete snapshot")
    if rule.get("transform", {}).get("operator") != "identity":
        raise ValueError("Relation compiler only supports identity transforms")
    if decision.parent_relation not in OBJECT_RELATIONS or not decision.label.strip() or not decision.definition.strip():
        raise ValueError("Proposed relation lacks a supported kind, label or definition")
    source, target = rule["source"], rule["target"]
    if (decision.parent_relation == "contains"
            and _child_surface(source["table"])
            and not _child_surface(target["table"])):
        raise ValueError("Contains direction is reversed for child-to-parent source")
    source_field, target_field = source.get("field"), target.get("field")
    if not source_field or not target_field:
        raise ValueError("Relation compiler only supports one key field")
    records = _record_map(bundle)
    source_record = target_record = None
    for pair in bundle.get("examples", {}).get("positive", []):
        left = records.get(pair.get("source_record_id"))
        right = records.get(pair.get("target_record_id"))
        raw = pair.get("matching_raw_value")
        if (left is None or right is None or left.get("table") != source["table"]
                or right.get("table") != target["table"] or raw in (None, "")):
            continue
        left_values = [str(entry["value"]) for _, entry in _entries(left)
                       if entry.get("column") == source_field]
        right_values = [str(entry["value"]) for _, entry in _entries(right)
                        if entry.get("column") == target_field]
        if (str(raw) in left_values and str(raw) in right_values
                and _supports_quote(left, decision.source_quote, source_field)
                and _supports_quote(right, decision.target_quote, target_field)):
            source_record, target_record = left, right
            break
    if source_record is None:
        raise ValueError("No validated positive pair supports both cited quotes")
    evidence_ids = list(dict.fromkeys([
        f"schema:{source['table']}:{source_field}",
        f"schema:{target['table']}:{target_field}",
        _quote_evidence(data, source_record, decision.source_quote,
                        allowed_roles=_RELATION_QUOTE_ROLES, excluded_columns={source_field},
                        require_complete=True),
        _quote_evidence(data, target_record, decision.target_quote,
                        allowed_roles=_RELATION_QUOTE_ROLES, excluded_columns={target_field},
                        require_complete=True),
    ]))
    relation_id = "relation:" + digest([decision.parent_relation, decision.label.casefold().strip(),
                                        source["table"], target["table"]])[:24]
    relation_type = DerivedType(id=relation_id, parent=decision.parent_relation,
                                definition=decision.definition.strip(), evidence_ids=evidence_ids,
                                label=decision.label.strip())
    selectors = [Condition(op="eq", field=field, value=str(value))
                 for field, value in sorted((rule.get("selector") or {}).items())]
    selector = (selectors[0] if len(selectors) == 1 else
                Condition(op="and", children=selectors) if selectors else None)
    plan = RelationPlan(
        id="plan:" + digest([rule["rule_id"], relation_id, data.snapshot_id])[:24],
        source_table=source["table"], target_table=target["table"], mode="identifier",
        source_column=source_field, target_column=target_field,
        scope_bindings=rule.get("scope_bindings") or {}, selector=selector,
        predicate=relation_id, semantics="reference", evidence_ids=evidence_ids,
    )
    candidate = core.model_copy(deep=True)
    existing_types = {item.id: item for item in candidate.relation_types}
    existing_plans = {item.id: item for item in candidate.relations}
    if relation_id in existing_types and existing_types[relation_id] != relation_type:
        raise ValueError("Conflicting relation type ID")
    if plan.id in existing_plans and existing_plans[plan.id] != plan:
        raise ValueError("Conflicting relation plan ID")
    if relation_id not in existing_types:
        candidate.relation_types.append(relation_type)
    if plan.id not in existing_plans:
        candidate.relations.append(plan)
    errors = validate_plan(candidate, data, profile)
    if errors:
        raise ValueError("Group relation plan invalid: " + "; ".join(errors))
    return candidate, plan


def _balanced_selection(bundles, limit):
    """Reserve bounded model calls for both concept and relation packets."""
    queues = {kind: [item for item in bundles if item.get("task_kind") == kind]
              for kind in ("concept_induction", "relation_meaning")}
    other = [item for item in bundles if item.get("task_kind") not in queues]
    selected = []
    while len(selected) < limit and any(queues.values()):
        for kind in queues:
            if queues[kind] and len(selected) < limit:
                selected.append(queues[kind].pop(0))
    return (selected + other[:max(0, limit - len(selected))])[:limit]


async def construct_from_bundles(data, profile, core: BuildPlan, bundles, llm, *, review=True,
                                 max_bundles=20, progress=None):
    """One bounded group pass; failures do not change accepted plan or objects."""
    from .llm import BudgetExceeded

    if type(max_bundles) is not int or max_bundles < 0:
        raise ValueError("max_bundles must be nonnegative")
    accepted_exact, concepts, alignments, steps = {}, {}, [], []
    selected = _balanced_selection(bundles, max_bundles)
    skipped = max(0, len(bundles) - len(selected))
    stage = progress.task("语义组增量抽取", len(selected)) if progress else None
    if stage:
        stage.__enter__()
    try:
        for bundle in selected:
            step = {"bundle_id": bundle["bundle_id"], "task_kind": bundle["task_kind"],
                    "status": "unresolved", "core_before": digest(core.model_dump())}
            try:
                payload = {"bundle": bundle, "root_model": {
                    "object_roots": profile["object_roots"], "relation_roots": profile["relation_roots"]},
                    "current_types": [{"id": item.id, "parent": item.parent, "definition": item.definition}
                                      for item in core.object_types[-12:]],
                    "current_relations": [{"id": item.id, "parent": item.parent}
                                          for item in core.relation_types[-12:]]}
                if bundle["task_kind"] == "concept_induction":
                    decision = await llm.ask("concept_bundle", payload, ConceptBundleDecision)
                    compiled = compile_concept(data, profile, bundle, decision, accepted_exact)
                    if compiled:
                        concept, new_alignments = compiled
                        old = concepts.get(concept["id"])
                        if old and (old["definition"] != concept["definition"] or old["type"] != concept["type"]):
                            raise ValueError("Conflicting definitions for one concept ID")
                        if review:
                            check = await llm.ask("group_review", {**payload, "candidate": decision.model_dump()}, BundleReview)
                            if not check.accepted or check.errors:
                                raise ValueError("Group review rejected: " + "; ".join(check.errors))
                        if old:
                            refs = {item["record_id"] for item in old["source_refs"]}
                            old["source_refs"].extend(ref for ref in concept["source_refs"]
                                                      if ref["record_id"] not in refs)
                            old["evidence_ids"] = sorted(set(old["evidence_ids"])
                                                         | set(concept["evidence_ids"]))
                        else:
                            concepts[concept["id"]] = concept
                        existing_alignments = {item["id"] for item in alignments}
                        alignments.extend(item for item in new_alignments
                                          if item["id"] not in existing_alignments)
                        for item in new_alignments:
                            if item["mapping_kind"] == "exact":
                                accepted_exact[item["source_record_id"]] = concept["id"]
                        step.update(status="accepted", concept_id=concept["id"],
                                    alignment_count=len(new_alignments))
                    else:
                        step.update(status=decision.status, reason=decision.reason)
                elif bundle["task_kind"] == "relation_meaning":
                    decision = await llm.ask("relation_bundle", payload, RelationBundleDecision)
                    compiled = compile_relation(data, profile, core, bundle, decision)
                    if compiled:
                        candidate, plan = compiled
                        if review:
                            check = await llm.ask("group_review", {**payload, "candidate": decision.model_dump()}, BundleReview)
                            if not check.accepted or check.errors:
                                raise ValueError("Group review rejected: " + "; ".join(check.errors))
                        core = candidate
                        step.update(status="accepted", relation_plan_id=plan.id)
                    else:
                        step.update(status=decision.status, reason=decision.reason)
                else:
                    raise ValueError("Unknown bundle task kind")
            except BudgetExceeded as exc:
                step.update(status="budget_exhausted", error_type=type(exc).__name__)
                steps.append(step)
                skipped += len(selected) - len(steps)
                break
            except Exception as exc:
                step.update(status="unresolved", error_type=type(exc).__name__,
                            reason=str(exc) if isinstance(exc, ValueError) else "See trace")
            step["core_after"] = digest(core.model_dump())
            steps.append(step)
            if stage:
                stage.advance(detail=step["status"])
    finally:
        if stage:
            stage.__exit__(None, None, None)
    statuses = {status: sum(item["status"] == status for item in steps)
                for status in ("accepted", "no_change", "unresolved", "budget_exhausted")}
    coverage = {"bundles_available": len(bundles), "bundles_selected": len(selected),
                "bundles_not_attempted": skipped, "statuses": statuses,
                "partial": bool(skipped or statuses["unresolved"] or statuses["budget_exhausted"])}
    return {"plan": core, "concepts": list(concepts.values()), "record_alignments": alignments,
            "steps": steps, "bundles_selected": len(selected), "bundles_skipped": skipped,
            "coverage": coverage, "partial": coverage["partial"]}
