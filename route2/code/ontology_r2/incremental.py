"""Direct mappings and transactional, table-scoped semantic deltas."""
from copy import deepcopy
import re

from .models import BuildPlan, TablePlan
from .storage import digest
from .validation import validate_plan


def direct_mapping(data):
    tables, mappings = [], []
    for name, table in data.tables.items():
        tables.append(TablePlan(table=name, object_type="GeneralObject", identity_columns=table["pk"],
                                attributes={f"column:{name}.{c}": c for c in table["column_names"]},
                                evidence_ids=["schema:" + name]))
        mappings.append({"table": name, "status": "source_mapping_only", "object_root": "GeneralObject",
                         "columns": deepcopy(table["columns"]), "declared_primary_key": table["pk"],
                         "constraints": deepcopy(table["constraints"]), "foreign_keys": deepcopy(table["foreign_keys"]),
                         "evidence_ids": ["schema:" + name, *[f"schema:{name}:{c}" for c in table["column_names"]]]})
    return BuildPlan(tables=tables), {"tables": mappings, "coverage": "all_input_tables_and_columns", "business_semantics": "not_inferred"}


def traversal(data):
    parents = {name: {f["referenced_schema"] + "." + f["referenced_table"] for f in t["foreign_keys"]
                      if f["referenced_schema"] + "." + f["referenced_table"] in data.tables
                      and f["referenced_schema"] + "." + f["referenced_table"] != name}
               for name, t in data.tables.items()}
    pending, order = set(parents), []
    while pending:
        ready = sorted(n for n in pending if not parents[n].intersection(pending))
        if not ready:
            break
        order.extend(ready)
        pending.difference_update(ready)
    return order + sorted(pending), sorted(pending)


def unit_context(data, name):
    targets = {f["referenced_schema"] + "." + f["referenced_table"] for f in data.tables[name]["foreign_keys"]}
    relevant = {name} | targets.intersection(data.tables)
    context = data.context(relevant)
    context["table_catalog"] = [{"name": n, "comment": t.get("table_comment"), "columns": t["columns"]}
                                for n, t in data.tables.items()]
    return context


def core_context(core, unit, data):
    neighbors = {unit} | {f["referenced_schema"] + "." + f["referenced_table"] for f in data.tables[unit]["foreign_keys"]}
    for relation in core.relations:
        if unit in (relation.source_table, relation.target_table):
            neighbors.update((relation.source_table, relation.target_table))
    return {"object_types": [t.model_dump() for t in core.object_types], "relation_types": [t.model_dump() for t in core.relation_types],
            "tables": [t.model_dump() for t in core.tables if t.table in neighbors],
            "table_bindings": [{"table": t.table, "object_type": t.object_type} for t in core.tables],
            "relations": [r.model_dump() for r in core.relations if r.source_table in neighbors or r.target_table in neighbors],
            "context_scope": "all_type_definitions_and_selected_table_neighborhood", "full_core_hash": digest(core.model_dump())}


def merge_delta(core, delta, unit, data, profile):
    """No deletion, no mutation of core, and no overwrite of another unit."""
    errors = []
    if any(t.table != unit for t in delta.tables):
        errors.append("delta cannot change another table")
    if any(r.source_table != unit for r in delta.relations):
        errors.append("delta relation must originate in current table")
    candidate = core.model_copy(deep=True)
    for key in ("object_types", "relation_types", "relations"):
        existing = {t.id: t for t in getattr(candidate, key)}
        for item in getattr(delta, key):
            if item.id in existing and existing[item.id] != item:
                errors.append(f"conflicting {key} id: {item.id}")
            elif item.id not in existing:
                getattr(candidate, key).append(item.model_copy(deep=True))
                existing[item.id] = item
    for update in delta.tables:
        current = next((t for t in candidate.tables if t.table == update.table), None)
        if current is None:
            errors.append("unknown delta table: " + update.table)
            continue
        attrs = dict(current.attributes)
        for key, value in update.attributes.items():
            key = key if key.startswith(f"column:{unit}.") else f"attribute:{unit}:{key}"
            if key in attrs and attrs[key] != value:
                errors.append("cannot overwrite attribute mapping: " + key)
            else:
                attrs[key] = value
        replacement = update.model_copy(update={"attributes": attrs})
        candidate.tables[candidate.tables.index(current)] = replacement
    candidate.knowledge_questions = []
    errors += validate_plan(candidate, data, profile)
    for t in candidate.tables:
        if not set(data.tables[t.table]["column_names"]) <= set(t.attributes.values()):
            errors.append("direct column mapping lost: " + t.table)
    if errors:
        raise ValueError("; ".join(errors))
    return candidate


def ontology_from_plan(plan, profile, data, mapping, steps):
    attrs = []
    for table in plan.tables:
        columns = {c["column_name"]: c for c in data.tables[table.table]["columns"]}
        for attr, column in table.attributes.items():
            c = columns[column]
            sql_type = c["data_type"].lower()
            literal_type = "datetime" if "timestamp" in sql_type else "date" if sql_type == "date" else "boolean" if sql_type in ("bool", "boolean") else "integer" if re.match(r"^(smallint|integer|bigint|int\d*|serial\d*)$", sql_type) else "decimal" if re.match(r"^(numeric|decimal|real|double|float)", sql_type) else "string"
            attrs.append({"id": attr, "relation_type": "has", "domain": [table.object_type],
                          "literal_type": literal_type, "declared_data_type": c["data_type"], "storage_type": "string",
                          "source_table": table.table, "source_column": column,
                          "evidence_ids": [f"schema:{table.table}:{column}"]})
    return {**profile, "object_types": [x.model_dump() for x in plan.object_types],
            "relation_types": [x.model_dump() for x in plan.relation_types], "attributes": attrs,
            "source_mappings": mapping["tables"], "construction": {"mode": "rigor_adapted_incremental_yaml",
            "core_hash": digest(plan.model_dump()), "accepted_units": [s["unit"] for s in steps if s["status"] == "accepted"]}}


async def construct(data, profile, config, output, llm, manifest,
                    discovery_checks=(), discovery_candidates=()):
    """RIGOR-style enrich/judge/validate/merge; failed deltas never replace core."""
    from contextlib import AsyncExitStack
    from .external import ExternalIndex
    from .knowledge import connect, retrieve
    from .llm import BudgetExceeded
    from .models import AlignmentDecision, ExternalQueries, Review
    from .storage import write_yaml

    core, mapping = direct_mapping(data)
    order, cyclic = traversal(data)
    steps, knowledge, alignments = [], [], []
    candidate_by_id = {c["candidate_id"]: c for c in discovery_candidates}
    checks_by_source = {}
    for check in discovery_checks:
        if check.get("decision", {}).get("status") != "checked":
            continue
        candidate = candidate_by_id.get(check["candidate_id"])
        if candidate:
            checks_by_source.setdefault(candidate["source"]["table"], []).append((candidate, check))
    max_evidence_per_unit = config.get("discovery", {}).get("max_evidence_per_unit", 3)
    if not isinstance(max_evidence_per_unit, int) or max_evidence_per_unit < 0:
        raise ValueError("discovery.max_evidence_per_unit must be nonnegative")
    write_yaml(output / "direct_mapping.yaml", mapping)
    write_yaml(output / "ontology.yaml", ontology_from_plan(core, profile, data, mapping, steps))
    mcp = config.get("mcp", {})
    async with AsyncExitStack() as stack:
        session, unavailable, ext = None, None, None
        if mcp.get("enabled"):
            try:
                session = await stack.enter_async_context(connect(mcp))
            except Exception as exc:
                unavailable = type(exc).__name__
        if config.get("external", {}).get("enabled"):
            ext = ExternalIndex(config["external"], output / "work")
            stack.callback(ext.close)
            write_yaml(output / "external_import.yaml", ext.report)
            manifest["external_import_incomplete"] = not ext.report["complete"]
        for index, unit in enumerate(order):
            before = digest(core.model_dump())
            context = unit_context(data, unit)
            step = {"unit": unit, "index": index, "core_before": before, "status": "rejected", "attempts": []}
            checked_pairs = checks_by_source.get(unit, [])[:max_evidence_per_unit]
            field_association_evidence = [{
                "candidate_id": candidate["candidate_id"],
                "source": candidate["source"], "target": candidate["target"],
                "retrieval_channels": candidate["retrieval_channels"],
                "numeric_overlap_only": candidate["numeric_overlap_only"],
                "checks": {key: check["checks"][key] for key in (
                    "eligible_references", "unique_matches", "ambiguous_matches",
                    "missing_in_input", "distinct_eligible_keys",
                    "distinct_keys_matched", "whole_column_distinct_value_inclusion_ratio")},
                "semantic_relation": "unresolved",
            } for candidate, check in checked_pairs]
            step["field_association_checks_presented"] = len(field_association_evidence)
            local_knowledge, external_context = [], []
            if mcp.get("enabled"):
                if unavailable:
                    result = {"unit": unit, "status": "unavailable", "claims": [], "error_type": unavailable}
                elif index >= mcp.get("max_units", mcp.get("max_questions", 10)):
                    result = {"unit": unit, "status": "budget_exhausted", "claims": [], "stop_reason": "max_knowledge_units"}
                else:
                    source = {"unit": unit, **context, "current_core": core_context(core, unit, data), "root_model": profile}
                    result = await retrieve("补充当前源定义在本体增量构建中尚缺少的定义、关系含义及适用条件；已有证据足够时不重复。", source, session, mcp, llm)
                knowledge.append(result)
                for claim in result.get("claims", []):
                    data.evidence[claim["id"]] = {"id": claim["id"], "origin": "enterprise_document", "raw_fragment": claim["quote"],
                        "source_ref": {"document_id": claim["document_id"], "version": claim["version"]},
                        "scope": claim["source_scope"], "claim_scope": claim["scope"], "polarity": claim["polarity"],
                        "source_evidence_ids": claim["source_evidence_ids"], "contribution": claim["contribution"]}
                if result.get("claims"):
                    local_knowledge = [{"status": result["status"], "claims": result["claims"]}]
            if ext is not None:
                try:
                    name = data.tables[unit].get("table_comment") or unit
                    queries = await llm.ask("external_queries", {"internal_term": name, "columns": data.tables[unit]["columns"]}, ExternalQueries)
                    cards = ext.search_many([name, *queries.queries], 5)
                    if cards:
                        decision = await llm.ask("alignment", {"internal_source_term": name, "candidates": cards}, AlignmentDecision)
                        selected = next((c for c in cards if c["uri"] == decision.external_uri), None) if decision.mapping_kind != "unmapped" else None
                        if decision.mapping_kind != "unmapped":
                            if not selected:
                                raise ValueError("Alignment references a noncandidate URI")
                            text = selected["definition"] if decision.mapping_kind == "exact" else selected["label"] + "\n" + selected["definition"]
                            if not decision.internal_quote or decision.internal_quote not in name or not decision.external_quote or decision.external_quote not in text:
                                raise ValueError("Alignment lacks verbatim internal/external support")
                        item = {"internal_id": unit, **decision.model_dump(), "source_card": selected}
                        alignments.append(item)
                        if selected:
                            external_context.append(item)
                    else:
                        alignments.append({"internal_id": unit, "mapping_kind": "unmapped", "reason": "no_candidate"})
                except Exception as exc:
                    step["external_error"] = type(exc).__name__
                    alignments.append({"internal_id": unit, "mapping_kind": "unmapped", "reason": "retrieval_error", "error_type": type(exc).__name__})
            packet = {"unit": unit, "sources": context, "root_model": profile, "current_core": core_context(core, unit, data),
                      "knowledge": local_knowledge, "external_context": external_context,
                      "field_association_evidence": field_association_evidence,
                      "field_association_limits": "Raw-value equality on this input only. No selector or scope was inferred; overlap is not a declared FK or business relation. Candidate pairs without exact checks are omitted."}
            errors, previous = [], None
            for attempt in range(config["llm"].get("max_repairs", 2) + 1):
                record = {"number": attempt + 1}
                try:
                    task = "plan" if attempt == 0 else "final_plan"
                    delta = await llm.ask(task, {**packet, "errors": errors, "previous_delta": previous}, BuildPlan)
                    record["delta"] = previous = delta.model_dump()
                    candidate = merge_delta(core, delta, unit, data, profile)
                    review = await llm.ask("review", {**packet, "delta": delta.model_dump(), "candidate_core": core_context(candidate, unit, data)}, Review)
                    record["judge"] = review.model_dump()
                    if not review.accepted or review.errors:
                        raise ValueError("Judge rejected: " + "; ".join(review.errors or ["insufficient_semantic_evidence"]))
                    if review.corrected_delta is not None:
                        candidate = merge_delta(core, review.corrected_delta, unit, data, profile)
                        record["accepted_delta"] = review.corrected_delta.model_dump()
                    step["changes"] = {
                        "object_types_added": sorted({x.id for x in candidate.object_types} - {x.id for x in core.object_types}),
                        "relation_types_added": sorted({x.id for x in candidate.relation_types} - {x.id for x in core.relation_types}),
                        "relation_plans_added": sorted({x.id for x in candidate.relations} - {x.id for x in core.relations}),
                        "table_type": next(t.object_type for t in candidate.tables if t.table == unit),
                        "attributes_added": sum(len(t.attributes) for t in candidate.tables) - sum(len(t.attributes) for t in core.tables),
                    }
                    core, step["status"] = candidate, "accepted"
                    record["validation"] = "passed"
                    step["attempts"].append(record)
                    break
                except Exception as exc:
                    errors = [str(exc) if isinstance(exc, (ValueError, BudgetExceeded)) else type(exc).__name__]
                    record.update(errors=errors, error_type=type(exc).__name__)
                    step["attempts"].append(record)
                    if isinstance(exc, BudgetExceeded):
                        step["status"] = "budget_exhausted"
                        break
            step["core_after"] = digest(core.model_dump())
            steps.append(step)
            write_yaml(output / "incremental" / f"step-{index:04}.yaml", step)
            llm.trace({"stage": "incremental_unit", "unit": unit, "status": step["status"], "core_before": before, "core_after": step["core_after"]})
            write_yaml(output / "extraction_plan.yaml", core.model_dump())
            write_yaml(output / "ontology.yaml", ontology_from_plan(core, profile, data, mapping, steps))
    errors = validate_plan(core, data, profile)
    if errors:
        raise ValueError("Final core validation failed: " + "; ".join(errors))
    write_yaml(output / "knowledge.yaml", knowledge)
    write_yaml(output / "alignments.yaml", alignments)
    write_yaml(output / "construction.yaml", {"table_order": order, "cyclic_or_blocked_dependencies": cyclic,
        "steps": steps, "direct_mapping_tables": len(mapping["tables"]), "direct_mapping_columns": sum(len(t["columns"]) for t in mapping["tables"]),
        "final_core_valid": True, "final_core_hash": digest(core.model_dump())})
    manifest["incremental_units_not_accepted"] = [s["unit"] for s in steps if s["status"] != "accepted"]
    manifest["external_alignment_errors"] = [s["unit"] for s in steps if s.get("external_error")]
    manifest["construction_mode"] = "rigor_adapted_incremental_yaml"
    return core, ontology_from_plan(core, profile, data, mapping, steps), knowledge
