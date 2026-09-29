"""Replay one accepted relation witness without expanding its source table."""
import json

from .models import evaluate
from .relation_contract import canonical_relation_id


def verify_record_relation(data, plan, relation, assertion, source_node, target_node, rows, relations):
    """Validate schema provenance, witness identity and its current executable link."""
    pair = assertion.get("source_record_pair") or {}
    if (assertion.get("decision", {}).get("status") != "accepted"
            or assertion.get("identity_scope") != "input_snapshot"
            or plan is None or relation is None
            or plan.witness_snapshot_id != data.snapshot_id
            or plan.evidence_scope != "sample_semantic_with_full_technical_check"
            or (pair.get("source"), pair.get("target")) not in {
                (item.source_record_id, item.target_record_id) for item in plan.witnessed_pairs}):
        raise ValueError("Record relation lacks an accepted current witnessed plan")
    base = relations.get(plan.predicate)
    if (base is None or base.evidence_scope != plan.evidence_scope
            or relation.category != "business_relation_type"
            or (relation.parent, relation.predicate_name, relation.semantic_parameters) !=
               (base.parent, base.predicate_name, base.semantic_parameters)
            or relation.id != canonical_relation_id(base.parent, assertion.get("subject_type"),
                assertion.get("object_type"), predicate_name=base.predicate_name,
                semantic_parameters=base.semantic_parameters)
            or not set(base.evidence_ids) <= set(assertion.get("evidence_ids", []))
            or not set(plan.evidence_ids) <= set(assertion.get("evidence_ids", []))):
        raise ValueError("Record relation predicate differs from its accepted schema evidence")
    source_ref, target_ref = source_node["source_ref"], target_node["source_ref"]
    if (source_ref.get("table") != plan.source_table or target_ref.get("table") != plan.target_table
            or source_ref.get("record_id") != pair.get("source")
            or target_ref.get("record_id") != pair.get("target")):
        raise ValueError("Record relation instance source differs from its witness")
    source = rows.get((plan.source_table, source_ref["row"]))
    target = rows.get((plan.target_table, target_ref["row"]))
    if (not source or not target or evaluate(plan.selector, source) is not True
            or any(source.get(field) in (None, "") for field in set(plan.scope_bindings) | set(plan.context_columns))):
        raise ValueError("Record relation witness condition or scope is not satisfied")
    for evidence_id in assertion.get("evidence_ids", []):
        proof = data.evidence.get(evidence_id) or {}
        ref = proof.get("source_ref") or {}
        if proof.get("origin") != "observed_record" or ref.get("record_id") not in pair.values():
            continue
        witnessed_row = source if ref.get("record_id") == pair["source"] else target
        witnessed_table = plan.source_table if ref.get("record_id") == pair["source"] else plan.target_table
        if (proof.get("raw_fragment_truncated") or ref.get("snapshot_id") != data.snapshot_id
                or ref.get("table") != witnessed_table or ref.get("row") != witnessed_row["__r2_row"]
                or ref.get("column") not in witnessed_row
                or witnessed_row[ref["column"]] != proof.get("raw_fragment")):
            raise ValueError("Record relation semantic witness changed")
    if plan.mode == "identifier" and not plan.source_path:
        from .discovery import association_match_sql
        sql, params = association_match_sql(data, {
            "source": {"table": plan.source_table, "field": plan.source_column},
            "target": {"table": plan.target_table, "field": plan.target_column}},
            scope_bindings={remote: local for local, remote in plan.scope_bindings.items()},
            transform=plan.transform.get("operator", "identity"))
        matches = data.db.execute(sql + " AND l.source_row_number=?", [*params, source_ref["row"]]).fetchall()
        if matches != [(source_ref["row"], target_ref["row"])]:
            raise ValueError("Record relation reference no longer resolves uniquely to its witness")
        return
    if plan.transform.get("operator", "identity") != "identity" or plan.mode == "text":
        raise ValueError("Record relation mode requires an independently replayable reference")
    value = source.get(plan.source_column)
    try:
        if plan.source_path:
            value = json.loads(value)
            for key in plan.source_path:
                value = value[key]
        if plan.mode == "formula":
            from .relations import formula_symbols
            values = formula_symbols(value)
        elif plan.mode == "members":
            values = (value if isinstance(value, list) else json.loads(value)
                      if value.lstrip().startswith("[") else value.split(plan.delimiter)
                      if plan.delimiter else [value])
        else:
            values = [value]
        if not all(isinstance(item, str) and item for item in values):
            raise ValueError("Reference must resolve to nonempty strings")
        matches = [data.lookup(plan.target_table, ((plan.target_column, item),
                   *[(remote, source[local]) for local, remote in plan.scope_bindings.items()]))
                   for item in values]
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError("Record relation source reference cannot be replayed") from exc
    if not any(len(found) == 1 and data.record_id(plan.target_table, found[0]) == pair["target"]
               for found in matches):
        raise ValueError("Record relation target is absent or ambiguous in its witnessed reference")
