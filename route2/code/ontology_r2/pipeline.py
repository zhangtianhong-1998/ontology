"""Schema -> bounded planning/RAG -> deterministic extraction -> validated YAML."""
import asyncio
import json
import time
from contextlib import nullcontext
from pathlib import Path

from dotenv import load_dotenv

from .association_rules import build_association_rules
from .column_roles import classify_columns
from .concept_candidates import recall_concept_candidates
from .discovery import discover_and_check
from .embedding import LocalEmbedder, settings as embedding_settings
from .group_incremental import construct_from_bundles
from .incremental import construct, ontology_from_plan
from .instance_bundles import build_instance_bundles
from .llm import BudgetExceeded, StructuredLLM
from .progress import ProgressReporter
from .relations import Extractor
from .semantic_cards import SemanticCardIndex, build_semantic_cards
from .storage import Dataset, Sink, digest, read_yaml, write_yaml
from .value_aliases import propose_value_alias_candidates


def load_config(path):
    path = Path(path).resolve()
    config = read_yaml(path)
    for key in ("dataset", "model_profile", "env_file"):
        if key in config:
            config[key] = str((path.parent / config[key]).resolve())
    for key in ("responses", "cache_dir"):
        if config.get("llm", {}).get(key):
            config["llm"][key] = str((path.parent / config["llm"][key]).resolve())
    for source in config.get("external", {}).get("sources", []):
        source["path"] = str((path.parent / source["path"]).resolve())
    embedding = config.get("embedding", {})
    if embedding.get("model_path"):
        model_path = Path(embedding["model_path"]).expanduser()
        embedding["model_path"] = str((path.parent / model_path).resolve())
    mcp = config.get("mcp", {})
    if mcp.get("mock_documents"):
        import sys
        mcp["command"] = sys.executable
        mcp["args"] = ["-m", "ontology_r2.mock_mcp", "--documents", str((path.parent / mcp["mock_documents"]).resolve())]
    return config


def technical_graph(data):
    nodes, edges = [{"id": "snapshot:" + data.snapshot_id, "kind": "DatasetSnapshot"}], []
    for path, sha in sorted(data.files.items()):
        nodes.append({"id": "source:" + path, "kind": "Source", "path": path, "sha256": sha})
        edges.append({"source": "snapshot:" + data.snapshot_id, "type": "includes_source", "target": "source:" + path})
    for name, t in data.tables.items():
        metadata_file = data.evidence["schema:" + name]["source_ref"]["file"]
        nodes.append({"id": name, "kind": "Table", **{k: t.get(k) for k in ("schema", "table_name", "table_comment", "relkind", "estimated_rows", "total_size", "data_size")}, "observed_rows": t["rows"]})
        edges.append({"source": name, "type": "documented_by", "target": "source:" + metadata_file})
        edges.append({"source": name, "type": "sample_from", "target": "source:" + str(Path(t["csv_path"]).relative_to(data.root))})
        for index, constraint in enumerate(t["constraints"]):
            cid = name + ":constraint:" + str(index)
            nodes.append({"id": cid, "kind": "Constraint", **constraint})
            edges.append({"source": name, "type": "has_declared_constraint", "target": cid})
            edges.append({"source": cid, "type": "documented_by", "target": "source:" + metadata_file.replace("schema/tables/", "schema/constraints/", 1)})
        for c in t["columns"]:
            cid = name + "." + c["column_name"]
            nodes.append({"id": cid, "kind": "Column", **c})
            edges.append({"source": name, "type": "table_has_column", "target": cid})
        for fk in t["foreign_keys"]:
            edges.append({"source": name + "." + fk["column_name"], "type": "declared_fk", "target": fk["referenced_schema"] + "." + fk["referenced_table"] + "." + fk["referenced_column"], "raw": fk})
    known = {node["id"] for node in nodes}
    for edge in edges:
        if edge["target"] not in known:
            nodes.append({"id": edge["target"], "kind": "ExternalColumnReference", "availability": "not_in_input"})
            known.add(edge["target"])
    return {"nodes": nodes, "edges": edges, "lineage_source_available": False}


def check_output(sink):
    checks = {}
    for field in ("subject", "object"):
        sql = "SELECT count(*) FROM items a WHERE a.kind='assertions' AND json_extract(a.body, ?) IS NOT NULL AND NOT EXISTS (SELECT 1 FROM items o WHERE o.kind='objects' AND o.id=json_extract(a.body, ?))"
        checks["dangling_" + field] = sink.db.execute(sql, ("$." + field, "$." + field)).fetchone()[0]
    checks["missing_evidence"] = sink.db.execute("SELECT count(*) FROM items a, json_each(a.body, '$.evidence_ids') e WHERE a.kind IN ('assertions','objects','rules','record_alignments') AND NOT EXISTS (SELECT 1 FROM items v WHERE v.kind='evidence' AND v.id=e.value)").fetchone()[0]
    checks["dangling_rule_member"] = sink.db.execute("SELECT count(*) FROM items a, json_each(a.body, '$.members') e WHERE a.kind='rules' AND NOT EXISTS (SELECT 1 FROM items v WHERE v.kind='objects' AND v.id=e.value)").fetchone()[0]
    checks["dangling_concept_alignment"] = sink.db.execute("SELECT count(*) FROM items a WHERE a.kind='record_alignments' AND NOT EXISTS (SELECT 1 FROM items o WHERE o.kind='objects' AND o.id=json_extract(a.body,'$.concept_id'))").fetchone()[0]
    checks["mixed_literal_object"] = sink.db.execute("SELECT count(*) FROM items WHERE kind='assertions' AND (json_type(body,'$.object') IS NOT NULL) = (json_type(body,'$.literal') IS NOT NULL)").fetchone()[0]
    return {"passed": not any(checks.values()), "checks": checks, "semantic_quality": "requires_independent_evaluation"}


def attach_concept_evidence(data, result):
    """Register sampled definition fragments so any derived type can cite a row."""
    for candidate in result["candidates"]:
        for record in candidate["records"]:
            for entries in record["fields"].values():
                for entry in entries:
                    evidence_id = "record:" + digest([record["record_id"], entry["column"]])[:24]
                    entry["record_evidence_id"] = evidence_id
                    data.evidence[evidence_id] = {
                        "id": evidence_id, "origin": "observed_record",
                        "raw_fragment": entry["value"],
                        "raw_fragment_truncated": entry["truncated"],
                        "source_ref": {"table": record["table"], "record_id": record["record_id"],
                                       "row": record["row_number"], "column": entry["column"],
                                       "snapshot_id": data.snapshot_id},
                    }


async def build(config, output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "work").mkdir()
    start = time.monotonic()
    data = sink = llm = progress = None
    code_hash = digest({p.name: p.read_text() for p in sorted(p for p in Path(__file__).parent.iterdir() if p.suffix in (".py", ".html"))})
    manifest = {"status": "failed", "implementation": "rigor_adapted_incremental_yaml_prototype", "implementation_code_hash": code_hash, "config_hash": digest(config), "data_scope": config.get("data_scope", "sample"), "synthetic": config.get("synthetic", False)}
    manifest.update(input_root=config["dataset"], experiment_profile=config.get("experiment_profile"), source_channels={"mcp": config.get("mcp", {}).get("enabled", False), "external": config.get("external", {}).get("enabled", False)}, runtime_limits={"llm": {k: v for k, v in config["llm"].items() if k.startswith("max_") or k in ("mode", "timeout_seconds")}, "processing": config.get("processing", {}), "memory_limit": config.get("memory_limit", "1GB")})
    try:
        progress = ProgressReporter(config.get("progress"))
        load_dotenv(config.get("env_file"), override=False)
        profile = read_yaml(config["model_profile"])
        profiling = {**config.get("profiling", {}), "input_scope": config.get("data_scope", "unknown")}
        data = Dataset(config["dataset"], output / "work", config.get("memory_limit", "1GB"),
                       profiling, progress=progress, privacy_config=config.get("privacy"))
        sink = Sink(output, config.get("shard_size", 5000))
        llm = StructuredLLM(config["llm"], output)
        manifest.update(snapshot_id=data.snapshot_id, input_files=data.files, input_tables=len(data.tables), input_records=sum(t["rows"] for t in data.tables.values()), model_profile_hash=digest(profile))
        write_yaml(output / "meta_graph.yaml", technical_graph(data))
        write_yaml(output / "profiles.yaml", {name: t["profiles"] for name, t in data.tables.items()})
        write_yaml(output / "column_roles.yaml", {name: classify_columns(table)
                                                   for name, table in data.tables.items()})
        discovery = {"candidates": [], "checks": [], "coverage": {"status": "disabled", "partial": False}}
        if config.get("discovery", {}).get("enabled", True):
            discovery = discover_and_check(data, config.get("discovery", {}), progress=progress)
            discovery["coverage"]["input_scope"] = config.get("data_scope", "unknown")
        write_yaml(output / "field_candidates.yaml", discovery["candidates"])
        write_yaml(output / "association_checks.yaml", discovery["checks"])
        write_yaml(output / "discovery_coverage.yaml", discovery["coverage"])
        manifest["field_discovery"] = {"candidate_count": len(discovery["candidates"]),
                                       "checked_count": discovery["coverage"].get("candidates_checked", 0),
                                       "partial": discovery["coverage"]["partial"]}
        association = {"rules": [], "coverage": {"status": "disabled", "partial": False},
                       "agent": {"status": "disabled"}}
        if config.get("association_rules", {}).get("enabled", False):
            association = await build_association_rules(
                data, discovery, config["association_rules"], llm=llm)
        write_yaml(output / "association_rules.yaml", association)
        manifest["association_rules"] = {"rules": len(association["rules"]),
                                          "statuses": association["coverage"].get("statuses", {}),
                                          "agent": association["agent"],
                                          "partial": association["coverage"].get("partial", False)}
        concept_result = {"candidates": [], "coverage": {"status": "disabled"}}
        if config.get("concept_recall", {}).get("enabled", False):
            options = {key: value for key, value in config["concept_recall"].items()
                       if key not in ("enabled", "max_candidates_per_unit")}
            stage = progress.task("定义记录候选召回", 1) if progress else nullcontext(None)
            with stage as task:
                concept_result = recall_concept_candidates(data, **options)
                if task:
                    task.advance(detail=f"{len(concept_result['candidates'])} 对")
            attach_concept_evidence(data, concept_result)
        write_yaml(output / "concept_candidates.yaml", concept_result)
        manifest["concept_recall"] = {"candidate_count": len(concept_result["candidates"]),
                                       "coverage": concept_result["coverage"]}
        alias_result = {"candidates": [], "coverage": {"status": "disabled"}}
        if config.get("alias_recall", {}).get("enabled", False):
            options = {key: value for key, value in config["alias_recall"].items() if key != "enabled"}
            stage = progress.task("别名字段候选召回", 1) if progress else nullcontext(None)
            with stage as task:
                alias_result = propose_value_alias_candidates(data, **options)
                if task:
                    task.advance(detail=f"{len(alias_result['candidates'])} 对")
        write_yaml(output / "alias_candidates.yaml", alias_result)
        manifest["alias_recall"] = {"candidate_count": len(alias_result["candidates"]),
                                     "coverage": alias_result["coverage"]}
        plan, ontology, knowledge = await construct(data, profile, config, output, llm, manifest,
                                                    discovery_checks=discovery["checks"],
                                                    discovery_candidates=discovery["candidates"],
                                                    concept_candidates=concept_result["candidates"],
                                                    progress=progress)
        card_report = {"status": "disabled", "partial": False}
        bundle_report = {"status": "disabled", "partial": False}
        group_result = {"concepts": [], "record_alignments": [], "steps": [],
                        "bundles_selected": 0, "bundles_skipped": 0, "partial": False}
        if config.get("instance_bundles", {}).get("enabled", False):
            options = config["instance_bundles"]
            indexed = build_semantic_cards(
                data, output / "work" / "semantic_cards.sqlite",
                max_cards=options.get("max_cards", 200000),
                max_field_chars=options.get("max_field_chars", 512),
                max_unknown_fields_per_table=options.get("max_unknown_fields_per_table", 4),
                progress=progress)
            card_report = indexed["coverage"]
            write_yaml(output / "semantic_card_coverage.yaml", card_report)
            index = SemanticCardIndex(indexed["index_path"])
            try:
                vector_model = None
                if options.get("vector_enabled", False):
                    vector_settings = embedding_settings(config.get("embedding", {}))
                    if vector_settings["enabled"]:
                        vector_model = LocalEmbedder(vector_settings)
                packet_result = build_instance_bundles(data, index, association, options,
                                                       embedding=vector_model)
            finally:
                index.close()
            bundle_report = packet_result["coverage"]
            write_yaml(output / "evidence_bundles.yaml", packet_result["bundles"])
            write_yaml(output / "instance_bundle_coverage.yaml", bundle_report)
            group_result = await construct_from_bundles(
                data, profile, plan, packet_result["bundles"], llm,
                review=options.get("review", True),
                max_bundles=options.get("max_llm_bundles", 20), progress=progress)
            plan = group_result["plan"]
            construction = read_yaml(output / "construction.yaml")
            construction["group_steps"] = group_result["steps"]
            construction["final_core_hash"] = digest(plan.model_dump())
            write_yaml(output / "construction.yaml", construction)
            write_yaml(output / "group_steps.yaml", group_result["steps"])
            write_yaml(output / "extraction_plan.yaml", plan.model_dump())
            ontology = ontology_from_plan(
                plan, profile, data, read_yaml(output / "direct_mapping.yaml"),
                construction["steps"])
            write_yaml(output / "ontology.yaml", ontology)
        manifest["semantic_cards"] = {"cards_indexed": card_report.get("cards_indexed", 0),
                                      "rows_scanned": card_report.get("rows_scanned", 0),
                                      "partial": card_report.get("partial", False)}
        manifest["instance_bundles"] = {"built": bundle_report.get("bundles_built", 0),
                                         "coverage": bundle_report, "partial": bundle_report.get("partial", False)}
        manifest["group_incremental"] = {"accepted": sum(s["status"] == "accepted" for s in group_result["steps"]),
                                         "steps": len(group_result["steps"]),
                                         "skipped": group_result["bundles_skipped"],
                                         "partial": group_result["partial"]}
        write_yaml(output / "business_concepts.yaml", group_result["concepts"])
        write_yaml(output / "record_alignments.yaml", group_result["record_alignments"])
        for ev in data.evidence.values():
            sink.put("evidence", ev)
        for concept in group_result["concepts"]:
            sink.put("objects", concept)
        for alignment in group_result["record_alignments"]:
            sink.put("record_alignments", alignment)
        extractor = Extractor(data, sink, plan, llm, config.get("processing", {}), progress=progress)
        materialized = 0
        preview_materialized = 0
        preview_per_table = config.get("processing", {}).get("preview_objects_per_table", 0)
        if type(preview_per_table) is not int or not 0 <= preview_per_table <= 100:
            raise ValueError("processing.preview_objects_per_table must be 0..100")
        if config.get("processing", {}).get("materialize_all_objects", True):
            cap = config.get("processing", {}).get("max_object_records", 1000000)
            total = min(cap, sum(data.tables[t.table]["rows"] for t in plan.tables))
            object_task = progress.task("对象物化", total) if progress else nullcontext(None)
            with object_task as stage:
                pending = 0
                for t in plan.tables:
                    if stage:
                        stage.note(t.table)
                    for row in data.rows(t.table):
                        if materialized >= cap:
                            manifest["object_materialization_partial"] = True
                            break
                        extractor.object(t.table, row)
                        materialized += 1
                        pending += 1
                        if stage and pending >= 1000:
                            stage.advance(pending)
                            pending = 0
                if stage and pending:
                    stage.advance(pending)
        else:
            if preview_per_table:
                total = sum(min(preview_per_table, data.tables[t.table]["rows"])
                            for t in plan.tables)
                preview_task = progress.task("记录预览物化", total) if progress else nullcontext(None)
                with preview_task as stage:
                    for table in plan.tables:
                        for row in data.rows(table.table, limit=preview_per_table):
                            extractor.object(table.table, row)
                            preview_materialized += 1
                            if stage:
                                stage.advance(detail=table.table)
                materialized += preview_materialized
        manifest["object_preview"] = {"per_table_first_rows": preview_per_table if preview_materialized else 0,
                                      "records_materialized": preview_materialized,
                                      "scope": "first_rows_per_table_only; not representative" if preview_materialized else "none"}
        stats = await extractor.execute(progress=progress)
        write_yaml(output / "coverage.yaml", {"tables": [{"table": name, "input_records": t["rows"], "object_plan": name in extractor.tables, "source_relation_plans": [p.id for p in plan.relations if p.source_table == name]} for name, t in data.tables.items()], "relation_plans": stats["plans"], "implicit_relation_recall": "unknown; absent plans do not prove absence of business relations"})
        unmodeled_tables = sorted(data.tables.keys() - {t.table for t in plan.tables})
        manifest["unmodeled_tables"] = unmodeled_tables
        for table in unmodeled_tables:
            sink.put("unresolved", {"id": "unmodeled:" + table, "reason": "unmodeled_table", "table": table, "count": data.tables[table]["rows"]})
        for table in manifest.get("incremental_units_not_accepted", []):
            sink.put("unresolved", {"id": "semantic_unit:" + table, "reason": "semantic_increment_not_accepted", "table": table, "count": data.tables[table]["rows"]})
        counts = sink.export(progress=progress)
        check_task = progress.task("结果校验", 1) if progress else nullcontext(None)
        with check_task as stage:
            validation = check_output(sink)
            if stage:
                stage.advance()
        write_yaml(output / "validation.yaml", validation)
        write_yaml(output / "metrics.yaml", {"counts": counts, "extraction": stats,
                                             "materialized_records": materialized,
                                             "preview_materialized_records": preview_materialized,
                                             "llm": llm.metrics(), "semantic_quality": "synthetic_only" if config.get("synthetic") else "unjudged"})
        partial_causes = {
            "unmodeled_tables": bool(unmodeled_tables),
            "relation_records_unprocessed": bool(stats["unprocessed_records"]),
            "semantic_budget_exhausted": bool(stats["semantic_budget_exhausted"]),
            "object_materialization_cap": bool(manifest.get("object_materialization_partial")),
            "knowledge_questions_unprocessed": bool(manifest.get("knowledge_questions_not_processed")),
            "knowledge_failed_or_capped": any(k["status"] in ("error", "unavailable", "budget_exhausted") for k in knowledge),
            "external_import_incomplete": bool(manifest.get("external_import_incomplete")),
            "incremental_units_not_accepted": bool(manifest.get("incremental_units_not_accepted")),
            "external_alignment_errors": bool(manifest.get("external_alignment_errors")),
            "field_candidates_not_fully_checked": bool(manifest["field_discovery"]["partial"]),
            "association_rules_partial": bool(manifest["association_rules"]["partial"]),
            "semantic_cards_partial": bool(manifest["semantic_cards"]["partial"]),
            "instance_bundles_partial": bool(manifest["instance_bundles"]["partial"]),
            "group_incremental_partial": bool(manifest["group_incremental"]["partial"]),
        }
        manifest["partial_reasons"] = [name for name, present in partial_causes.items() if present]
        partial = bool(manifest["partial_reasons"])
        manifest["status"] = "failed" if not validation["passed"] else "partial" if partial else "complete"
        manifest["ontology_hash"] = digest(ontology)
        manifest["semantic_completeness"] = "unknown; complete describes execution only"
    except Exception as exc:
        manifest["status"] = "partial" if isinstance(exc, BudgetExceeded) else "failed"
        manifest["error"] = {"type": type(exc).__name__, "message": str(exc) if isinstance(exc, (ValueError, BudgetExceeded, FileNotFoundError)) else "See stage trace; provider/tool details are not echoed"}
        if llm:
            llm.trace({"stage": "run_error", **manifest["error"]})
        if sink:
            sink.export(progress=progress)
    finally:
        if llm:
            manifest["llm"] = llm.metrics()
            try:
                await llm.close()
            except Exception as exc:
                manifest["cleanup_error"] = type(exc).__name__
        manifest["elapsed_seconds"] = round(time.monotonic() - start, 3)
        write_yaml(output / "manifest.yaml", manifest)
        for item in (sink, data):
            if item:
                item.close()
        if config.get("visualization", {}).get("enabled", True):
            from .visualization import render_viewer
            try:
                viewer_task = progress.task("生成可视化", 1) if progress else nullcontext(None)
                with viewer_task as stage:
                    render_viewer(output, config.get("visualization", {}).get("max_nodes", 200),
                                  manifest_override={**manifest, "viewer": "viewer.html"})
                    if stage:
                        stage.advance()
                manifest["viewer"] = "viewer.html"
            except Exception as exc:
                manifest["viewer_error"] = type(exc).__name__
                if manifest["status"] == "complete":
                    manifest["status"] = "partial"
                manifest.setdefault("partial_reasons", []).append("viewer_error")
            write_yaml(output / "manifest.yaml", manifest)
    return manifest
