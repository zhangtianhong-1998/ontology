"""Direct mappings and transactional, table-scoped semantic deltas."""
from copy import deepcopy
import json
import re

from .column_roles import classify_columns
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


def _mapping_only(table):
    """Large transient import tables retain physical mappings without a type call."""
    name = table["table_name"].casefold()
    return len(table["columns"]) >= 50 and bool(re.search(
        r"(?:import|staging).*temp|temp.*(?:import|staging)", name))


def _prompt_table(data, name, *, sample=False):
    table = data.tables[name]
    roles = classify_columns(table)
    semantic = {item["column"] for item in roles if item["include_in_semantic_prompt"]}
    profiles = {item["column"]: item for item in table["profiles"]}
    result = {
        "name": name, "comment": table.get("table_comment"),
        "columns": [column for column in table["columns"] if column["column_name"] in semantic],
        "deterministic_bindings": [item["deterministic_binding"] for item in roles
                                   if item["role"] in ("audit_time", "audit_metadata", "technical_identifier")],
        "empty_in_input": [item["column"] for item in roles if item["role"] == "empty"],
        # Schema/profile evidence remains in the local artifacts. Repeating its
        # per-column counters in every table packet adds no semantic context.
        "column_roles": [{"column": item["column"], "role": item["role"]}
                         for item in roles if item["include_in_semantic_prompt"]],
        "constraints": table["constraints"], "foreign_keys": table["foreign_keys"],
        "profiles": [{"column": column, "usable_count": profiles[column]["usable_count"],
                      "approx_distinct_usable": profiles[column]["approx_distinct_usable"]}
                     for column in table["column_names"] if column in semantic],
    }
    if sample:
        sample_columns = _example_columns(table, None, limit=12)
        result["sampled_columns"] = sample_columns
        result["sample_columns_not_shown"] = [column for column in table["column_names"]
                                              if column in semantic and column not in sample_columns]
        result["sample"] = [{
            "record_id": data.record_id(name, row), "row_number": row["__r2_row"],
            "values": {column: row[column][:160] if row[column] is not None else None
                       for column in sample_columns},
            "truncated_columns": [column for column in sample_columns
                                  if row[column] is not None and len(row[column]) > 160],
        } for row in data.rows(name, limit=3)]
    return result


def _example_columns(table, key, limit=4):
    roles = {item["column"]: item for item in classify_columns(table)}
    ranked = []
    for index, column in enumerate(table["columns"]):
        name = column["column_name"]
        if name == key or not roles[name]["include_in_semantic_prompt"]:
            continue
        description = name + " " + str(column.get("column_comment") or "")
        priority = 0 if re.search(r"name|alias|definition|description|formula|meaning|scope|unit|名称|别名|定义|描述|公式|口径|单位", description, re.I) else 1
        ranked.append((priority, index, name))
    return [name for _, _, name in sorted(ranked)[:limit]]


def unit_context(data, name, candidate_targets=()):
    targets = {f["referenced_schema"] + "." + f["referenced_table"] for f in data.tables[name]["foreign_keys"]}
    candidate_targets = [target for target in dict.fromkeys(candidate_targets)
                         if target != name and target in data.tables]
    relevant = {name} | targets.intersection(data.tables) | set(candidate_targets[:3]).intersection(data.tables)
    catalog = []
    for table_name, table in data.tables.items():
        hints = [column for column, role in zip(table["columns"], classify_columns(table))
                 if role["include_in_semantic_prompt"]]
        catalog.append({"name": table_name, "comment": table.get("table_comment"),
                        "column_hints": [{"name": col["column_name"], "comment": col.get("column_comment")}
                                         for col in hints[:2]],
                        "other_semantic_column_count": max(0, len(hints) - 2)})
    return {"tables": [_prompt_table(data, table_name, sample=table_name == name)
                       for table_name in sorted(relevant)],
            "table_catalog": catalog,
            "candidate_targets_not_expanded": candidate_targets[3:],
            "evidence": [{"id": item["id"]} for item in data.evidence.values()
                         if item.get("origin") != "observed_record"
                         and item["source_ref"].get("table") in relevant]}


def core_context(core, unit, data, semantic_neighbors=(), candidate_targets=()):
    neighbors = {unit} | {f["referenced_schema"] + "." + f["referenced_table"] for f in data.tables[unit]["foreign_keys"]}
    neighbors.update(candidate_targets)
    for relation in core.relations:
        if unit in (relation.source_table, relation.target_table):
            neighbors.update((relation.source_table, relation.target_table))
    neighbors.update(hit["table"] for hit in semantic_neighbors)
    return {"object_types": [t.model_dump() for t in core.object_types], "relation_types": [t.model_dump() for t in core.relation_types],
            "tables": [{**t.model_dump(exclude={"attributes"}),
                        "mapped_attribute_count": len(t.attributes),
                        "attribute_mapping_hash": digest(t.attributes)}
                       for t in core.tables if t.table in neighbors],
            "table_bindings": [{"table": t.table, "object_type": t.object_type} for t in core.tables],
            "relations": [r.model_dump() for r in core.relations if r.source_table in neighbors or r.target_table in neighbors],
            "context_scope": "all_type_definitions_and_selected_table_summaries; full mappings validated locally",
            "semantic_neighbors": list(semantic_neighbors), "full_core_hash": digest(core.model_dump())}


def semantic_core_hits(core, data, unit, accepted, embedding, limit):
    """Recall previously accepted table definitions; similarity is context, not a relation."""
    from .embedding import top_cosine

    names = sorted(set(accepted) - {unit})
    if not names:
        return []

    def description(name):
        metadata = data.tables[name]
        model = next(t for t in core.tables if t.table == name)
        columns = " ".join((c.get("column_comment") or c["column_name"]) for c in metadata["columns"])
        return "\n".join((name, metadata.get("table_comment") or "", model.object_type, columns))

    current = data.tables[unit]
    query = "\n".join((unit, current.get("table_comment") or "",
                        " ".join((c.get("column_comment") or c["column_name"]) for c in current["columns"])))
    vectors = embedding.documents([description(name) for name in names])
    return [{"table": names[index], "cosine_similarity": score,
             "model_sha256": embedding.model_sha256}
            for index, score in top_cosine(vectors, embedding.query(query), limit)]


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
                    discovery_checks=(), discovery_candidates=(),
                    concept_candidates=(), progress=None):
    """RIGOR-style enrich/judge/validate/merge; failed deltas never replace core."""
    from contextlib import AsyncExitStack, nullcontext
    from .embedding import LocalEmbedder, settings as embedding_settings
    from .external import ExternalIndex
    from .knowledge import connect, retrieve
    from .llm import BudgetExceeded
    from .models import AlignmentDecision, ExternalQueries, Review
    from .row_bundles import joint_examples
    from .storage import write_yaml
    from openai import APITimeoutError

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
    joint_pair_limit = config.get("discovery", {}).get("max_joint_pairs_per_candidate", 1)
    joint_byte_limit = config.get("discovery", {}).get("max_joint_example_bytes", 16000)
    if type(joint_pair_limit) is not int or not 0 <= joint_pair_limit <= 2:
        raise ValueError("discovery.max_joint_pairs_per_candidate must be 0..2")
    if type(joint_byte_limit) is not int or joint_byte_limit <= 0:
        raise ValueError("discovery.max_joint_example_bytes must be positive")
    max_concepts_per_unit = config.get("concept_recall", {}).get("max_candidates_per_unit", 2)
    if type(max_concepts_per_unit) is not int or not 0 <= max_concepts_per_unit <= 5:
        raise ValueError("concept_recall.max_candidates_per_unit must be 0..5")
    concept_by_table = {}
    for candidate in concept_candidates:
        for table in {record["table"] for record in candidate["records"]}:
            concept_by_table.setdefault(table, []).append(candidate)
    max_repairs = config["llm"].get("max_repairs", 2)
    if isinstance(max_repairs, bool) or not isinstance(max_repairs, int) or max_repairs < 0:
        raise ValueError("llm.max_repairs must be a nonnegative integer")
    max_attempts = max_repairs + 1
    iteration_policy = {"table_passes": 1, "units": len(order),
                        "max_attempts_per_unit": max_attempts,
                        "revisit_rejected_units": False}
    manifest["incremental_iteration_policy"] = iteration_policy
    write_yaml(output / "direct_mapping.yaml", mapping)
    write_yaml(output / "ontology.yaml", ontology_from_plan(core, profile, data, mapping, steps))
    mcp = config.get("mcp", {})
    embedding_config = embedding_settings(config.get("embedding", {}))
    embedding = None
    if embedding_config["enabled"]:
        model_task = progress.task("加载向量模型", 1) if progress else nullcontext(None)
        with model_task as stage:
            embedding = LocalEmbedder(embedding_config)
            if stage:
                stage.advance()
    manifest["embedding"] = embedding.report() if embedding is not None else {"enabled": False}
    manifest["source_channels"]["embedding"] = embedding is not None
    async with AsyncExitStack() as stack:
        session, unavailable, ext = None, None, None
        if mcp.get("enabled"):
            try:
                session = await stack.enter_async_context(connect(mcp))
            except Exception as exc:
                unavailable = type(exc).__name__
        if config.get("external", {}).get("enabled"):
            import_task = progress.task("载入外部本体", 1) if progress else nullcontext(None)
            with import_task as stage:
                ext = ExternalIndex(config["external"], output / "work", embedding=embedding)
                if stage:
                    stage.advance(detail=f"{ext.report['cards']} 张卡")
            stack.callback(ext.close)
            write_yaml(output / "external_import.yaml", ext.report)
            manifest["external_import_incomplete"] = not ext.report["complete"]
        unit_stage = stack.enter_context(progress.task("本体增量构建", len(order))) if progress else None
        for index, unit in enumerate(order):
            before = digest(core.model_dump())
            step = {"unit": unit, "index": index, "core_before": before, "status": "rejected", "attempts": []}
            if _mapping_only(data.tables[unit]):
                step.update(status="mapping_only", reason="large_transient_import_table",
                            core_after=before)
                steps.append(step)
                write_yaml(output / "incremental" / f"step-{index:04}.yaml", step)
                llm.trace({"stage": "incremental_unit", "unit": unit,
                           "status": "mapping_only", "core_before": before,
                           "core_after": before})
                if unit_stage:
                    unit_stage.advance(detail=f"{unit}: mapping_only")
                continue
            core_hits = semantic_core_hits(core, data, unit,
                                           [s["unit"] for s in steps if s["status"] == "accepted"],
                                           embedding, embedding_config["core_top_k"]) if embedding is not None else []
            if embedding is not None:
                step["semantic_core_neighbors"] = core_hits
            checked_pairs = checks_by_source.get(unit, [])[:max_evidence_per_unit]
            candidate_targets = [candidate["target"]["table"] for candidate, _ in checked_pairs]
            context = unit_context(data, unit, candidate_targets)
            field_association_evidence = []
            for candidate, check in checked_pairs:
                item = {
                    "candidate_id": candidate["candidate_id"],
                    "source": candidate["source"], "target": candidate["target"],
                    "retrieval_channels": candidate["retrieval_channels"],
                    "numeric_overlap_only": candidate["numeric_overlap_only"],
                    "checks": {key: check["checks"][key] for key in (
                        "eligible_references", "unique_matches", "ambiguous_matches",
                        "missing_in_input", "distinct_eligible_keys",
                        "distinct_keys_matched", "whole_column_distinct_value_inclusion_ratio")},
                    "semantic_relation": "unresolved",
                }
                bundle = joint_examples(data, candidate, check,
                    limit=joint_pair_limit,
                    source_fields=_example_columns(data.tables[candidate["source"]["table"]], candidate["source"]["field"]),
                    target_fields=_example_columns(data.tables[candidate["target"]["table"]], candidate["target"]["field"]))
                size = len(json.dumps(bundle, ensure_ascii=False, default=str).encode())
                item["joint_examples"] = (bundle if size <= joint_byte_limit
                                          else {"status": "over_budget", "bytes": size, "semantic_relation": "unresolved"})
                field_association_evidence.append(item)
            step["field_association_checks_presented"] = len(field_association_evidence)
            local_knowledge, external_context = [], []
            knowledge_state = "disabled"
            if mcp.get("enabled"):
                if unavailable:
                    result = {"unit": unit, "status": "unavailable", "claims": [], "error_type": unavailable}
                elif index >= mcp.get("max_units", mcp.get("max_questions", 10)):
                    result = {"unit": unit, "status": "budget_exhausted", "claims": [], "stop_reason": "max_knowledge_units"}
                else:
                    if unit_stage:
                        unit_stage.note(f"{unit} 企业文档检索")
                    source = {"unit": unit, **context, "current_core": core_context(core, unit, data, core_hits, candidate_targets), "root_model": profile}
                    result = await retrieve("补充当前源定义在本体增量构建中尚缺少的定义、关系含义及适用条件；已有证据足够时不重复。", source, session, mcp, llm, embedding=embedding)
                knowledge_state = result["status"]
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
                    if unit_stage:
                        unit_stage.note(f"{unit} 外部本体对齐")
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
            packet = {"unit": unit, "sources": context, "root_model": profile, "current_core": core_context(core, unit, data, core_hits, candidate_targets),
                      "knowledge_state": knowledge_state, "knowledge": local_knowledge,
                      "external_context": external_context,
                      "concept_candidate_evidence": concept_by_table.get(unit, [])[:max_concepts_per_unit],
                      "concept_candidate_limits": "Sampled names or aliases only; every pair remains unjudged. Compare definitions, units and scope before proposing a shared concept.",
                      "field_association_evidence": field_association_evidence,
                      "field_association_limits": "Raw-value equality on this input only. No selector or scope was inferred; overlap is not a declared FK or business relation. Candidate pairs without exact checks are omitted."}
            errors, previous = [], None
            for attempt in range(max_attempts):
                record = {"number": attempt + 1}
                try:
                    if unit_stage:
                        unit_stage.note(f"{unit} 生成 {attempt + 1}/{max_attempts}")
                    task = "plan" if attempt == 0 else "final_plan"
                    record["phase"] = task
                    delta = await llm.ask(task, {**packet, "errors": errors, "previous_delta": previous}, BuildPlan)
                    record["delta"] = previous = delta.model_dump()
                    candidate = merge_delta(core, delta, unit, data, profile)
                    if unit_stage:
                        unit_stage.note(f"{unit} 校验 {attempt + 1}/{max_attempts}")
                    record["phase"] = "review"
                    review = await llm.ask("review", {**packet, "delta": delta.model_dump(), "candidate_core": core_context(candidate, unit, data, core_hits, candidate_targets)}, Review)
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
                    if isinstance(exc, (TimeoutError, APITimeoutError)):
                        step["status"] = "timeout"
                        step["stop_reason"] = "unchanged_request_would_repeat_timeout"
                        break
                    if isinstance(exc, BudgetExceeded):
                        step["status"] = "budget_exhausted"
                        break
            step["core_after"] = digest(core.model_dump())
            steps.append(step)
            write_yaml(output / "incremental" / f"step-{index:04}.yaml", step)
            llm.trace({"stage": "incremental_unit", "unit": unit, "status": step["status"], "core_before": before, "core_after": step["core_after"]})
            write_yaml(output / "extraction_plan.yaml", core.model_dump())
            write_yaml(output / "ontology.yaml", ontology_from_plan(core, profile, data, mapping, steps))
            if unit_stage:
                unit_stage.advance(detail=f"{unit}: {step['status']}")
    errors = validate_plan(core, data, profile)
    if errors:
        raise ValueError("Final core validation failed: " + "; ".join(errors))
    write_yaml(output / "knowledge.yaml", knowledge)
    write_yaml(output / "alignments.yaml", alignments)
    write_yaml(output / "construction.yaml", {"table_order": order, "cyclic_or_blocked_dependencies": cyclic,
        "iteration_policy": iteration_policy,
        "steps": steps, "direct_mapping_tables": len(mapping["tables"]), "direct_mapping_columns": sum(len(t["columns"]) for t in mapping["tables"]),
        "final_core_valid": True, "final_core_hash": digest(core.model_dump())})
    manifest["incremental_units_not_accepted"] = [s["unit"] for s in steps
                                                   if s["status"] not in ("accepted", "mapping_only")]
    manifest["mapping_only_units"] = [s["unit"] for s in steps if s["status"] == "mapping_only"]
    manifest["external_alignment_errors"] = [s["unit"] for s in steps if s.get("external_error")]
    if embedding is not None:
        manifest["embedding"] = embedding.report()
        write_yaml(output / "embedding_report.yaml", manifest["embedding"])
    manifest["construction_mode"] = "rigor_adapted_incremental_yaml"
    return core, ontology_from_plan(core, profile, data, mapping, steps), knowledge
