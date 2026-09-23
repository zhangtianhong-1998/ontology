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
        relevant = {f"schema:{p.source_table}", f"schema:{p.source_table}:{p.source_column}"}
        if not p.evidence_ids or not set(p.evidence_ids) <= data.evidence.keys() or not relevant.intersection(p.evidence_ids):
            errors.append("source reference-role evidence required: " + p.id)
        elif not any(data.evidence[e]["raw_fragment"].strip() for e in relevant.intersection(p.evidence_ids)):
            errors.append("empty reference-role evidence: " + p.id)
        if p.semantics in ("allowed_member", "observed_member") and p.mode != "members":
            errors.append("member semantics requires members extractor: " + p.id)
    return errors
