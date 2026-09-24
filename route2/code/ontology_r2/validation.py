"""Reject invented roots, nonexistent source fields and evidence-free plans."""
from .models import BuildPlan


def validate_plan(plan: BuildPlan, data, profile):
    errors = []
    types = {t["id"]: None for t in profile["object_roots"]}
    relations = {r["id"]: r["kind"] for r in profile["relation_roots"]}
    if ({t.id for t in plan.object_types} | set(types)) & ({r.id for r in plan.relation_types} | set(relations)):
        errors.append("object and relation types cannot share an id")
    for items, known in ((plan.object_types, types), (plan.relation_types, relations)):
        pending = {t.id: t for t in items}
        if len(pending) != len(items):
            errors.append("duplicate derived type id")
        while pending:
            progress = False
            for name, t in list(pending.items()):
                if name in known:
                    errors.append("cannot replace root/type: " + name)
                    del pending[name]
                    progress = True
                elif t.parent in known:
                    if not t.evidence_ids or not set(t.evidence_ids) <= data.evidence.keys():
                        errors.append("derived type lacks source evidence: " + name)
                    known[name] = known[t.parent]
                    del pending[name]
                    progress = True
            if not progress:
                errors.append("unknown/cyclic parent: " + ",".join(pending))
                break
    object_parents = {item.id: item.parent for item in plan.object_types}
    for item in plan.object_types:
        if item.domain or item.range or item.endpoint_basis:
            errors.append("object type cannot have relation endpoints: " + item.id)
    for item in plan.relation_types:
        if bool(item.domain) != bool(item.range):
            errors.append("relation domain and range must be declared together: " + item.id)
        if item.endpoint_basis and not (item.domain and item.range):
            errors.append("relation endpoint basis lacks a signature: " + item.id)
        if not set(item.domain + item.range) <= set(types):
            errors.append("relation domain/range names an unknown object type: " + item.id)
        if item.category == "business_relation_type":
            business_ids = {t.id for t in plan.object_types if t.category == "business_type"}
            if (not item.domain or not item.range
                    or not set(item.domain + item.range) <= business_ids
                    or item.endpoint_basis != "record_alignment"
                    or item.evidence_scope != "one_positive_pair_with_exact_type_alignments"):
                errors.append("business relation requires exact business type endpoints: " + item.id)
            if any(relation.predicate == item.id for relation in plan.relations):
                errors.append("one-pair business relation cannot execute as a table plan: " + item.id)

    def is_subtype(actual, declared):
        seen = set()
        while actual and actual not in seen:
            if actual == declared:
                return True
            seen.add(actual)
            actual = object_parents.get(actual)
        return False

    table_plans = {t.table: t for t in plan.tables}
    if len(table_plans) != len(plan.tables):
        errors.append("duplicate table plan")
    for p in plan.tables:
        if p.table not in data.tables:
            errors.append("unknown table: " + p.table)
            continue
        columns = set(data.tables[p.table]["column_names"])
        required = set(p.identity_columns) | set(p.attributes.values()) | ({p.label_column} if p.label_column else set())
        if not required <= columns or p.object_type not in types:
            errors.append("unknown field/type: " + p.table)
        if not p.evidence_ids or not set(p.evidence_ids) <= data.evidence.keys():
            errors.append("missing table evidence: " + p.table)
    if len({r.id for r in plan.relations}) != len(plan.relations):
        errors.append("duplicate relation plan id")
    def condition_columns(c):
        return ({c.field} if c.field else set()) | set().union(*(condition_columns(x) for x in c.children))
    for p in plan.relations:
        if p.source_table not in table_plans or p.target_table not in table_plans:
            errors.append("relation endpoints lack a table plan: " + p.id)
            continue
        if p.source_table not in data.tables or p.target_table not in data.tables:
            continue
        if (p.witnessed_pairs or p.witness_snapshot_id) and p.witness_snapshot_id != data.snapshot_id:
            errors.append("relation witness snapshot differs from input: " + p.id)
        if p.evidence_scope == "sample_semantic_with_full_technical_check" and not p.witnessed_pairs:
            errors.append("sample-supported relation requires witnessed record pair: " + p.id)
        if p.witnessed_pairs and p.mode != "identifier":
            errors.append("witnessed relation currently requires identifier mode: " + p.id)
        pairs = [(item.source_record_id, item.target_record_id) for item in p.witnessed_pairs]
        if len(pairs) != len(set(pairs)):
            errors.append("duplicate witnessed relation pair: " + p.id)
        source, target = data.tables[p.source_table], data.tables[p.target_table]
        if p.target_column in p.scope_bindings.values() or len(set(p.scope_bindings.values())) != len(p.scope_bindings):
            errors.append("conflicting target field bindings: " + p.id)
        cols = {p.source_column} | set(p.scope_bindings) | set(p.context_columns)
        if p.selector:
            cols |= condition_columns(p.selector)
        if not cols <= set(source["column_names"]) or not ({p.target_column} | set(p.scope_bindings.values())) <= set(target["column_names"]):
            errors.append("unknown relation fields: " + p.id)
        if relations.get(p.predicate) != "object":
            errors.append("object relation required: " + p.id)
        relation_type = next((item for item in plan.relation_types
                              if item.id == p.predicate), None)
        if relation_type and relation_type.domain and relation_type.range:
            source_type = table_plans[p.source_table].object_type
            target_type = table_plans[p.target_table].object_type
            if not any(is_subtype(source_type, domain) for domain in relation_type.domain):
                errors.append("relation source type violates domain: " + p.id)
            if not any(is_subtype(target_type, range_type) for range_type in relation_type.range):
                errors.append("relation target type violates range: " + p.id)
        if p.evidence_scope and (relation_type is None
                                 or relation_type.evidence_scope != p.evidence_scope):
            errors.append("relation evidence scope differs from predicate: " + p.id)
        relevant = {f"schema:{p.source_table}", f"schema:{p.source_table}:{p.source_column}"}
        if not p.evidence_ids or not set(p.evidence_ids) <= data.evidence.keys() or not relevant.intersection(p.evidence_ids):
            errors.append("source reference-role evidence required: " + p.id)
        elif not any(data.evidence[e]["raw_fragment"].strip() for e in relevant.intersection(p.evidence_ids)):
            errors.append("empty reference-role evidence: " + p.id)
        if p.semantics in ("allowed_member", "observed_member") and p.mode != "members":
            errors.append("member semantics requires members extractor: " + p.id)
    return errors
