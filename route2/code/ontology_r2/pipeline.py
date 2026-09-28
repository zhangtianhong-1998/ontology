"""Schema -> bounded planning/RAG -> deterministic extraction -> validated YAML."""
import asyncio
import json
import time
from contextlib import nullcontext
from pathlib import Path

from dotenv import load_dotenv

from .association_rules import build_association_rules
from .calculation_stage import compile_calculation_stage
from .column_roles import classify_columns
from .column_role_inference import infer_column_role_candidates
from .configuration_relations import (discover_configuration_relations,
                                      infer_configuration_specs)
from .configuration_relation_stage import adjudicate_configuration_relations
from .concept_candidates import recall_concept_candidates
from .metadata_graph import MetadataGraph, build_metadata_graph, write_metadata_graph
from .discovery import discover_and_check
from .definition_memberships import build_definition_memberships
from .embedding import LocalEmbedder, model_digest, settings as embedding_settings
from .fact_observations import build_fact_observation_candidates
from .fact_type_binding import bind_fact_observations
from .fact_schema_induction import induce_fact_schema
from .group_incremental import construct_from_bundles
from .incremental import construct, ontology_from_plan
from .instance_bundles import build_instance_bundles, validate_bundle_options
from .llm import BudgetExceeded, StructuredLLM, validate_call_reservation
from .progress import ProgressReporter
from .record_types import source_record_type
from .relations import Extractor
from .repair_stage import repair_settings, run_targeted_repair
from .semantic_cards import SemanticCardIndex, build_semantic_cards
from .semantic_state import restore_state, save_state
from .storage import Dataset, Sink, digest, read_yaml, write_yaml
from .type_generalization import GeneralizationDecision
from .type_generalization_stage import run_generalization
from .type_equivalence import EquivalenceDecision
from .type_equivalence_stage import run_type_equivalence
from .value_aliases import propose_value_alias_candidates


def load_config(path):
    path = Path(path).resolve()
    config = read_yaml(path)
    for key in ("dataset", "model_profile", "env_file", "resume_from", "fact_templates_from"):
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
    """Compatibility entry point for the offline metadata graph builder."""
    return build_metadata_graph(data)


def retrieval_contract(config):
    """Freeze effective retrieval settings, including environment overrides."""
    options = embedding_settings(config.get("embedding", {}))
    # Progress only affects display; weights, dtype and query configuration
    # affect candidate selection and must invalidate semantic checkpoints.
    options.pop("show_progress", None)
    if options["enabled"]:
        options["model_sha256"] = model_digest(options["model_path"])
    return options


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


def add_fact_value_attributes(ontology, field_bindings):
    """Add only value properties whose source field has an accepted type binding."""
    attributes = {}
    for binding in field_bindings:
        if (binding.get("status") != "accepted_field_binding"
                or binding.get("instances_created", 0) < 1):
            continue
        table, column, type_id = (binding["table"], binding["value_column"],
                                  binding["type_id"])
        attribute_id = "fact_value_property:" + digest([type_id, table, column])[:24]
        attributes[(type_id, table, column)] = attribute_id
        ontology["attributes"].append({
            "id": attribute_id, "label": column, "relation_type": "has",
            "domain": [type_id], "literal_type": "decimal",
            "storage_type": "string", "source_table": table,
            "source_column": column,
            "evidence_scope": "observed_fact_value_in_source_snapshot",
            "mapping_status": "observed_fact_value",
            "evidence_ids": [f"schema:{table}:{column}",
                             binding["definition_evidence_id"]],
        })
    return attributes


def put_fact_instances(sink, instances, value_attributes):
    """Store typed observations and their actual value assertions together."""
    for instance in instances:
        sink.put("objects", instance)
        attribute_id = value_attributes[(
            instance["type"], instance["source_ref"]["table"],
            instance["value_column"])]
        sink.put("assertions", {
            "id": "fact_value_assertion:" + digest(
                [instance["id"], attribute_id, instance["observed_value"]])[:24],
            "subject": instance["id"], "predicate": "has",
            "attribute": attribute_id,
            "literal": {"type": "decimal", "value": instance["observed_value"]},
            "condition": {"status": "not_applicable"},
            "evidence_ids": instance["evidence_ids"],
            "decision": {"status": "accepted",
                         "method": "field_type_binding_and_source_row_verification"},
        })


def annotate_type_equivalences(ontology, result):
    """Expose only complete, source-checked equivalence groups in the ontology."""
    canonical_map = result["canonical_map"]
    groups = {}
    for type_id, canonical_id in canonical_map.items():
        groups.setdefault(canonical_id, set()).add(type_id)
    for item in ontology.get("object_types", []):
        if item["id"] not in canonical_map:
            continue
        canonical_id = canonical_map[item["id"]]
        item["canonical_type_id"] = canonical_id
        item["equivalent_type_ids"] = sorted(groups[canonical_id] - {item["id"]})
    ontology["type_equivalences"] = [
        assertion for assertion in result["equivalence_assertions"]
        if canonical_map.get(assertion["source_type_id"])
        == canonical_map.get(assertion["target_type_id"])
        == assertion["canonical_type_id"]
    ]


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
        repair_settings(config.get("targeted_repair"), config["llm"].get("max_calls", 100))
        instance_options = config.get("ontology_instantiation", {})
        if (not isinstance(instance_options, dict)
                or set(instance_options) - {"enabled", "max_records", "max_assertions"}
                or type(instance_options.get("enabled", False)) is not bool):
            raise ValueError("Invalid ontology_instantiation settings")
        for key in ("max_records", "max_assertions"):
            if key in instance_options and (type(instance_options[key]) is not int or instance_options[key] < 0):
                raise ValueError("ontology_instantiation limits must be nonnegative integers")
        if config.get("visualization", {}).get("enabled", True):
            from .visualization import validate_viewer_limit
            validate_viewer_limit(config.get("visualization", {}).get("max_nodes", 200))
        if config.get("instance_bundles", {}).get("enabled", False):
            validate_bundle_options(config["instance_bundles"])
            validate_call_reservation(
                config["instance_bundles"].get("reserve_calls_for_followup", 0),
                config["llm"].get("max_calls", 100))
        load_dotenv(config.get("env_file"), override=False)
        profile = read_yaml(config["model_profile"])
        profiling = {**config.get("profiling", {}), "input_scope": config.get("data_scope", "unknown")}
        data = Dataset(config["dataset"], output / "work", config.get("memory_limit", "1GB"),
                       profiling, progress=progress, privacy_config=config.get("privacy"))
        data.semantic_retrieval_contract = retrieval_contract(config)
        resumed = restore_state(config.get("resume_from"), data, profile,
                                implementation_code_hash=code_hash)
        sink = Sink(output, config.get("shard_size", 5000))
        llm = StructuredLLM(config["llm"], output)
        manifest.update(snapshot_id=data.snapshot_id, input_files=data.files, input_tables=len(data.tables), input_records=sum(t["rows"] for t in data.tables.values()), model_profile_hash=digest(profile))
        meta_graph = technical_graph(data)
        write_yaml(output / "meta_graph.yaml", meta_graph)
        write_yaml(output / "profiles.yaml", {name: t["profiles"] for name, t in data.tables.items()})
        write_yaml(output / "column_roles.yaml", {name: classify_columns(table)
                                                   for name, table in data.tables.items()})
        prior_roles = (resumed or {}).get("column_role_report", {})
        roles_reused = bool(prior_roles and not prior_roles.get("coverage", {}).get("partial", True)
                            and prior_roles.get("coverage", {}).get("status") != "disabled")
        role_candidates = (prior_roles if roles_reused else
                           await infer_column_role_candidates(
                               data, llm, config.get("column_role_inference", {})))
        # Restored candidates stay in table metadata; unresolved columns alone
        # are reconsidered. Never relabel an incomplete checkpoint as complete.
        data.column_role_report = role_candidates
        write_yaml(output / "column_role_candidates.yaml", role_candidates)
        manifest["column_role_inference"] = role_candidates["coverage"]
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
        association = {"rules": [], "coverage": {"status": "disabled", "partial": False},
                       "agent": {"status": "disabled"}}
        if config.get("association_rules", {}).get("enabled", False):
            stage = progress.task("关联规则全量核验") if progress else nullcontext(None)
            with stage as task:
                association = await build_association_rules(
                    data, discovery, config["association_rules"], llm=llm,
                    alias_candidates=alias_result,
                    progress=(lambda event: task.advance(
                        int(event.get("status") == "completed"),
                        detail=f"{event.get('origin', '')} {event.get('requested_position', 0)}/{event.get('requested_total', 0)}"))
                    if task else None)
        write_yaml(output / "association_rules.yaml", association)
        manifest["association_rules"] = {"rules": len(association["rules"]),
                                          "statuses": association["coverage"].get("statuses", {}),
                                          "agent": association["agent"],
                                          "partial": association["coverage"].get("partial", False)}
        meta_graph, export_report = write_metadata_graph(data, output, association)
        data.metadata_graph = MetadataGraph(meta_graph)
        manifest["metadata_graph"] = {
            "backend": "local_queryable_snapshot", "datahub": export_report,
            "nodes": len(meta_graph["nodes"]), "edges": len(meta_graph["edges"]),
            "technical_links": meta_graph["association_coverage"]["included_links"],
            "used_for": ["source_neighborhood", "evidence_packet_schema", "visualization"],
        }
        fact_options = config.get("fact_observations", {})
        if not isinstance(fact_options, dict):
            raise ValueError("fact_observations must be a mapping")
        unknown_fact_options = set(fact_options) - {
            "enabled", "max_candidates_per_table", "max_dimension_columns",
            "max_time_columns", "max_value_columns", "max_exact_group_tuples"}
        if unknown_fact_options:
            raise ValueError("Unknown fact_observations settings: " +
                             ", ".join(sorted(unknown_fact_options)))
        if type(fact_options.get("enabled", True)) is not bool:
            raise ValueError("fact_observations.enabled must be a boolean")
        fact_result = {"scope": "imported_csv_snapshot_only", "candidate_status": "candidate_only",
                       "business_type_binding": "unresolved", "tables": [],
                       "coverage": {"status": "disabled", "partial": False,
                                    "reason": "fact_observations.enabled=false"}}
        if fact_options.get("enabled", True):
            stage = progress.task("业务事实去重候选", 1) if progress else nullcontext(None)
            with stage as task:
                fact_result = build_fact_observation_candidates(
                    data, verified_technical_links=association["rules"],
                    **{key: value for key, value in fact_options.items() if key != "enabled"})
                if task:
                    task.advance(detail=f"{fact_result['coverage']['emitted_candidates']} 个候选")
            partial_tables = [item["table"] for item in fact_result["tables"]
                              if item["row_purpose"] == "business_fact" and (
                                  item["scan_scope"] != "full_input_for_selected_value_columns"
                                  or item["omitted_observed_tuples"] > 0
                                  or item.get("omitted_observed_tuples_status") != "exact"
                                  or item.get("coordinate_grain_status") not in
                                  (None, "candidate_grain_not_business_key_proof")
                                  or bool(item.get("value_columns_not_scanned_due_to_candidate_limit"))
                                  or any(item["selected_columns"].get(key)
                                         for key in ("omitted_dimensions", "omitted_business_times",
                                                     "omitted_values")))]
            partial = bool(partial_tables or fact_result["coverage"].get("partial"))
            fact_result["coverage"].update(
                status="partial" if partial else "complete",
                partial=partial, partial_table_names=partial_tables,
                unresolved_table_names=[item["table"] for item in fact_result["tables"]
                                        if item["row_purpose"] == "unresolved"],
                semantic_status="candidate_only_not_business_instance_or_type")
        write_yaml(output / "fact_observation_candidates.yaml", {
            key: value for key, value in fact_result.items() if key != "coverage"})
        write_yaml(output / "fact_observation_coverage.yaml", fact_result["coverage"])
        manifest["fact_observations"] = {
            "status": fact_result["coverage"]["status"],
            "candidate_status": "candidate_only", "business_type_binding": "unresolved",
            "business_fact_tables": fact_result["coverage"].get("business_fact_tables", 0),
            "emitted_candidates": fact_result["coverage"].get("emitted_candidates", 0),
            "omitted_observed_tuples": fact_result["coverage"].get("omitted_observed_tuples", 0),
            "unresolved_tables": fact_result["coverage"].get("unresolved_tables", 0),
            "partial": fact_result["coverage"]["partial"],
        }
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
        plan, ontology, knowledge = await construct(data, profile, config, output, llm, manifest,
                                                    discovery_checks=discovery["checks"],
                                                    discovery_candidates=discovery["candidates"],
                                                    concept_candidates=concept_result["candidates"],
                                                    progress=progress)
        if resumed:
            plan = resumed["plan"]
            manifest["resumed_from"] = str(config["resume_from"])
        card_report = {"status": "disabled", "partial": False}
        bundle_report = {"status": "disabled", "partial": False}
        group_result = {"concepts": [], "record_alignments": [],
                        "concept_relations": [], "concept_relation_derivations": [], "steps": [],
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
                                                       embedding=vector_model, progress=progress)
            finally:
                index.close()
            bundle_report = packet_result["coverage"]
            if vector_model is not None:
                manifest["embedding"] = vector_model.report()
                manifest["source_channels"]["embedding"] = True
            write_yaml(output / "evidence_bundles.yaml", packet_result["bundles"])
            write_yaml(output / "instance_bundle_coverage.yaml", bundle_report)
            with llm.reserve_calls(options.get("reserve_calls_for_followup", 0),
                                   stage="group_incremental"):
                group_result = await construct_from_bundles(
                    data, profile, plan, packet_result["bundles"], llm,
                    review=options.get("review", True),
                    max_bundles=options.get("max_llm_bundles", 20),
                    max_repairs_per_bundle=options.get("max_repairs_per_bundle", 0),
                    progress=progress, prior_result=resumed,
                    concept_batch_size=options.get("concept_batch_size", 1),
                    relation_batch_size=options.get("relation_batch_size", 1),
                    enable_template_projection=options.get("template_projection_enabled", False),
                    on_checkpoint=lambda result: save_state(
                        output / "semantic_state.json", data, profile, result,
                        implementation_code_hash=code_hash))
            # Check actual rows before the second round: pattern seeds alone
            # may omit values that invalidate an otherwise plausible template.
            repair_index = None
            repair_bundles = packet_result["bundles"]
            try:
                if config.get("targeted_repair", {}).get("enabled", False):
                    from .definition_memberships import build_projection_bindings
                    repair_index = SemanticCardIndex(output / "work" / "semantic_cards.sqlite")
                    checked_members = build_projection_bindings(
                        data, repair_index, group_result.get("template_projections", []),
                        max_records=options.get("max_definition_memberships", 100000))
                    bound = {item["id"]: item for item in group_result.get("template_bindings", [])}
                    bound.update({item["id"]: item for item in checked_members["bindings"]})
                    group_result["template_bindings"] = list(bound.values())
                    repair_bundles = [*repair_bundles, *checked_members["repair_bundles"]]
                    write_yaml(output / "template_pre_repair_coverage.yaml", checked_members["coverage"])
                group_result, repair_report = await run_targeted_repair(
                    data, profile, repair_bundles, group_result, llm,
                    config.get("targeted_repair"), bundle_options=options, progress=progress,
                    index=repair_index,
                    on_checkpoint=lambda result: save_state(
                        output / "semantic_state.json", data, profile, result,
                        implementation_code_hash=code_hash))
            finally:
                if repair_index is not None:
                    repair_index.close()
            write_yaml(output / "targeted_repair_tasks.yaml", repair_report["tasks"])
            write_yaml(output / "observed_value_tasks.yaml", repair_report["observed_value_tasks"])
            write_yaml(output / "observed_value_lookup_coverage.yaml", repair_report.get("observed_value_lookup", {}))
            write_yaml(output / "targeted_repair_rounds.yaml", repair_report["rounds"])
            manifest["targeted_repair"] = repair_report["coverage"]
            plan = group_result["plan"]
            from .semantic_bindings import compile_template_relations
            template_relations = compile_template_relations(
                data, profile, plan, group_result.get("template_projections", []),
                group_result.get("template_bindings", []))
            plan = template_relations.pop("plan")
            group_result["plan"] = plan
            write_yaml(output / "ontology_bindings.yaml", template_relations)
            manifest["template_relations"] = template_relations["coverage"]
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
        memberships = {"templates": [], "memberships": [], "reference_variants_pending": [],
                       "rejections": [], "template_errors": [], "coverage": {"status": "disabled", "partial": False}}
        if config.get("instance_bundles", {}).get("enabled", False):
            index = SemanticCardIndex(output / "work" / "semantic_cards.sqlite")
            try:
                memberships = build_definition_memberships(
                    data, index, group_result,
                    max_records=config["instance_bundles"].get("max_definition_memberships", 100000))
                from .definition_memberships import build_projection_bindings
                projection_members = build_projection_bindings(
                    data, index, group_result.get("template_projections", []),
                    max_records=config["instance_bundles"].get("max_definition_memberships", 100000))
                bound = {item["id"]: item for item in group_result.get("template_bindings", [])}
                bound.update({item["id"]: item for item in projection_members["bindings"]})
                group_result["template_bindings"] = list(bound.values())
                manifest["template_bindings"] = projection_members["coverage"]
                manifest["template_relations"]["template_instance_bindings"] = len(bound)
                template_relations["coverage"]["template_instance_bindings"] = len(bound)
                write_yaml(output / "ontology_bindings.yaml", template_relations)
                write_yaml(output / "template_binding_coverage.yaml", projection_members["coverage"])
                write_yaml(output / "template_uncovered_examples.yaml", projection_members["repair_bundles"])
            finally:
                index.close()
            for member in memberships["memberships"]:
                sink.put("definition_memberships", member)
        for key in ("templates", "reference_variants_pending", "rejections", "template_errors"):
            write_yaml(output / ("definition_" + key + ".yaml"), memberships[key])
        write_yaml(output / "definition_membership_coverage.yaml", memberships["coverage"])
        manifest["definition_memberships"] = memberships["coverage"]
        stage_settings = {key: config.get(key, {}) for key in (
            "type_generalization", "configuration_relations", "type_equivalence",
            "fact_schema_induction", "fact_type_binding", "fact_templates_from",
            "fact_observations", "discovery", "association_rules", "alias_recall",
            "instance_bundles", "column_role_inference")}
        stage_settings["embedding"] = data.semantic_retrieval_contract
        stage_signature = digest(stage_settings)
        saved_stages = ((resumed or {}).get("stage_outputs", {})
                        if roles_reused
                        and (resumed or {}).get("stage_signature") == stage_signature
                        and group_result.get("coverage", {}).get("steps_this_run", 1) == 0
                        else {})

        def completed_stage(name):
            """Reuse completed semantic decisions, never a budget-truncated stage."""
            result = saved_stages.get(name)
            if result and not result.get("partial", False) and not result.get("coverage", {}).get("partial", True):
                return result
            return None
        generalization = {"steps": [], "coverage": {"status": "disabled"},
                          "partial": False}
        generalization_options = config.get("type_generalization", {})
        if not isinstance(generalization_options, dict):
            raise ValueError("type_generalization must be a mapping")
        if type(generalization_options.get("enabled", False)) is not bool:
            raise ValueError("type_generalization.enabled must be a boolean")
        if generalization_options.get("enabled", False):
            stage = progress.task("业务上位类型归纳", 1) if progress else nullcontext(None)
            with stage as task:
                generalization = completed_stage("generalization") or await run_generalization(
                    data, profile, plan,
                    lambda packet: llm.ask("type_generalization", packet,
                                           GeneralizationDecision),
                    max_pairs=generalization_options.get("max_pairs", 40),
                    max_decisions=generalization_options.get("max_decisions", 10),
                    max_packet_bytes=generalization_options.get("max_packet_bytes", 16000),
                )
                if task:
                    task.advance(detail=str(generalization["coverage"]["statuses"]["accepted"]))
            plan = generalization.get("plan", plan)
            write_yaml(output / "extraction_plan.yaml", plan.model_dump())
            construction = read_yaml(output / "construction.yaml")
            construction["final_core_hash"] = digest(plan.model_dump())
            construction["type_generalization_steps"] = generalization["steps"]
            write_yaml(output / "construction.yaml", construction)
            ontology = ontology_from_plan(
                plan, profile, data, read_yaml(output / "direct_mapping.yaml"),
                construction["steps"])
            write_yaml(output / "ontology.yaml", ontology)
        write_yaml(output / "type_generalization_steps.yaml", generalization["steps"])
        write_yaml(output / "type_generalization_coverage.yaml", generalization["coverage"])
        manifest["type_generalization"] = {
            "accepted": generalization["coverage"].get("statuses", {}).get("accepted", 0),
            "model_decision_invocations": generalization["coverage"].get(
                "model_decision_invocations", 0),
            "partial": generalization["partial"],
            "coverage": generalization["coverage"],
        }
        config_relation_options = config.get("configuration_relations", {})
        if not isinstance(config_relation_options, dict):
            raise ValueError("configuration_relations must be a mapping")
        if type(config_relation_options.get("enabled", False)) is not bool:
            raise ValueError("configuration_relations.enabled must be a boolean")
        configuration_specs = {"spec_candidates": [],
                               "coverage": {"status": "disabled", "partial": False}}
        configuration_relations = {"candidates": [],
                                   "coverage": {"status": "disabled", "partial": False}}
        configuration_members = memberships["memberships"] + group_result.get("template_bindings", [])
        if config_relation_options.get("enabled", False):
            configuration_specs = completed_stage("configuration_specs") or infer_configuration_specs(
                data, plan, group_result["concepts"], group_result["record_alignments"],
                association["rules"],
                max_specs=config_relation_options.get("max_specs", 20),
                memberships=configuration_members)
            configuration_relations = completed_stage("configuration_relations") or discover_configuration_relations(
                data, plan, group_result["concepts"], group_result["record_alignments"],
                [item["spec"] for item in configuration_specs["spec_candidates"]],
                max_pairs_per_spec=config_relation_options.get("max_pairs_per_spec", 200),
                memberships=configuration_members)
        config_decisions = {"assertions": [], "steps": [],
                            "coverage": {"status": "disabled", "partial": False,
                                         "accepted": 0, "attempted": 0, "not_attempted": 0}}
        if config_relation_options.get("enabled", False):
            stage = progress.task("配置关系语义裁决", 1) if progress else nullcontext(None)
            with stage as task:
                config_decisions = completed_stage("config_decisions") or await adjudicate_configuration_relations(
                    data, profile, plan, configuration_relations["candidates"],
                    group_result["concepts"], group_result["record_alignments"], llm,
                    max_candidates=config_relation_options.get("max_semantic_decisions", 10),
                    checked_rules=association["rules"], memberships=configuration_members)
                if task:
                    task.advance(detail=str(config_decisions["coverage"]["accepted"]))
            plan = config_decisions.get("plan", plan)
            prior_assertions = {item["id"] for item in group_result["concept_relations"]}
            group_result["concept_relations"].extend(
                item for item in config_decisions["assertions"] if item["id"] not in prior_assertions)
            write_yaml(output / "extraction_plan.yaml", plan.model_dump())
            construction = read_yaml(output / "construction.yaml")
            construction["final_core_hash"] = digest(plan.model_dump())
            construction["configuration_relation_steps"] = config_decisions["steps"]
            write_yaml(output / "construction.yaml", construction)
            ontology = ontology_from_plan(
                plan, profile, data, read_yaml(output / "direct_mapping.yaml"),
                construction["steps"])
            write_yaml(output / "ontology.yaml", ontology)
        write_yaml(output / "configuration_bindings.yaml", config_decisions.get("bindings", []))
        if config_decisions.get("bindings"):
            binding_artifact = read_yaml(output / "ontology_bindings.yaml") if (output / "ontology_bindings.yaml").exists() else {"bindings": []}
            binding_artifact["bindings"].extend(config_decisions["bindings"])
            binding_artifact["configuration_coverage"] = config_decisions["coverage"]
            binding_artifact["combined_bindings_count"] = len(binding_artifact["bindings"])
            write_yaml(output / "ontology_bindings.yaml", binding_artifact)
        write_yaml(output / "configuration_relation_specs.yaml", configuration_specs)
        write_yaml(output / "configuration_relation_candidates.yaml", configuration_relations)
        write_yaml(output / "configuration_relation_steps.yaml", config_decisions["steps"])
        write_yaml(output / "configuration_relation_coverage.yaml", config_decisions["coverage"])
        manifest["configuration_relations"] = {
            "spec_candidates": len(configuration_specs["spec_candidates"]),
            "endpoint_verified_candidates": configuration_relations["coverage"].get(
                "endpoint_verified_candidates", 0),
            "semantic_decisions": config_decisions["coverage"].get("attempted", 0),
            "accepted_business_relations": config_decisions["coverage"].get("accepted", 0),
            "predicate_status": "accepted_per_witness_or_unjudged",
            "partial": bool(configuration_specs["coverage"].get("partial")
                            or configuration_relations["coverage"].get("partial")
                            or config_decisions["coverage"].get("partial")),
        }
        calculations = {"calculations": [], "dependencies": [], "coverage": {"status": "disabled", "partial": False}}
        if config.get("instance_bundles", {}).get("enabled", False):
            index = SemanticCardIndex(output / "work" / "semantic_cards.sqlite")
            try:
                calculations = compile_calculation_stage(data, profile, plan, group_result, index)
            finally:
                index.close()
            plan = calculations.pop("plan")
            ontology = ontology_from_plan(plan, profile, data, read_yaml(output / "direct_mapping.yaml"),
                                          read_yaml(output / "construction.yaml")["steps"])
            ontology["calculations"] = calculations["calculations"]
            ontology["calculation_dependencies"] = calculations["dependencies"]
            write_yaml(output / "extraction_plan.yaml", plan.model_dump())
            write_yaml(output / "ontology.yaml", ontology)
        write_yaml(output / "calculation_contracts.yaml", calculations)
        manifest["calculations"] = calculations["coverage"]
        equivalence_options = config.get("type_equivalence", {})
        if not isinstance(equivalence_options, dict):
            raise ValueError("type_equivalence must be a mapping")
        unknown_equivalence_options = set(equivalence_options) - {
            "enabled", "max_pairs", "max_decisions", "max_packet_bytes"}
        if unknown_equivalence_options:
            raise ValueError("Unknown type_equivalence settings: "
                             + ", ".join(sorted(unknown_equivalence_options)))
        if type(equivalence_options.get("enabled", False)) is not bool:
            raise ValueError("type_equivalence.enabled must be a boolean")
        equivalence = {
            "canonical_map": {}, "equivalence_assertions": [], "steps": [],
            "coverage": {"status": "disabled", "partial": False},
            "partial": False,
        }
        if equivalence_options.get("enabled", False):
            stage = progress.task("同名业务类型等价核验", 1) if progress else nullcontext(None)
            with stage as task:
                equivalence = completed_stage("equivalence") or await run_type_equivalence(
                    data, plan,
                    lambda packet: llm.ask("type_equivalence", packet,
                                           EquivalenceDecision),
                    max_pairs=equivalence_options.get("max_pairs", 20),
                    max_decisions=equivalence_options.get("max_decisions", 10),
                    max_packet_bytes=equivalence_options.get("max_packet_bytes", 16000),
                )
                if task:
                    task.advance(detail=str(equivalence["coverage"][
                        "complete_equivalence_groups"]))
        annotate_type_equivalences(ontology, equivalence)
        write_yaml(output / "ontology.yaml", ontology)
        write_yaml(output / "type_equivalence_steps.yaml", equivalence["steps"])
        write_yaml(output / "type_equivalence_assertions.yaml",
                   equivalence["equivalence_assertions"])
        write_yaml(output / "type_equivalence_coverage.yaml", equivalence["coverage"])
        manifest["type_equivalence"] = {
            "accepted_pairs": len(equivalence["equivalence_assertions"]),
            "complete_groups": equivalence["coverage"].get(
                "complete_equivalence_groups", 0),
            "canonicalized_types": len(equivalence["canonical_map"]),
            "model_decision_invocations": equivalence["coverage"].get(
                "model_decision_invocations", 0),
            "partial": equivalence["partial"],
        }
        fact_schema = {"field_templates": [], "steps": [], "coverage": {"status": "disabled", "partial": False}}
        schema_options = config.get("fact_schema_induction", {})
        if schema_options.get("enabled", False):
            reusable_templates = []
            if config.get("fact_templates_from"):
                reusable_templates = read_yaml(config["fact_templates_from"])
            elif resumed:
                reusable_templates = resumed.get("stage_outputs", {}).get("fact_schema", {}).get("field_templates", [])
            fact_schema = completed_stage("fact_schema") or await induce_fact_schema(
                data, fact_result, plan, llm, association_context=data.metadata_graph,
                max_calls=schema_options.get("max_calls", 50),
                max_samples_per_field=schema_options.get("max_samples_per_field", 5),
                max_packet_bytes=schema_options.get("max_packet_bytes", 16000),
                reusable_templates=reusable_templates)
            plan = fact_schema.pop("plan", plan)
            ontology = ontology_from_plan(plan, profile, data, read_yaml(output / "direct_mapping.yaml"),
                                          read_yaml(output / "construction.yaml")["steps"])
            ontology["calculations"] = calculations["calculations"]
            ontology["calculation_dependencies"] = calculations["dependencies"]
            annotate_type_equivalences(ontology, equivalence)
            write_yaml(output / "extraction_plan.yaml", plan.model_dump())
            write_yaml(output / "ontology.yaml", ontology)
        write_yaml(output / "fact_field_templates.yaml", fact_schema["field_templates"])
        write_yaml(output / "fact_schema_steps.yaml", fact_schema["steps"])
        write_yaml(output / "fact_schema_coverage.yaml", fact_schema["coverage"])
        manifest["fact_schema_induction"] = fact_schema["coverage"]
        binding_options = config.get("fact_type_binding", {})
        if not isinstance(binding_options, dict):
            raise ValueError("fact_type_binding must be a mapping")
        unknown_binding_options = set(binding_options) - {
            "enabled", "max_type_candidates_per_field", "max_source_rows_per_instance",
            "max_binding_calls", "max_definition_chars"}
        if unknown_binding_options:
            raise ValueError("Unknown fact_type_binding settings: "
                             + ", ".join(sorted(unknown_binding_options)))
        if type(binding_options.get("enabled", False)) is not bool:
            raise ValueError("fact_type_binding.enabled must be a boolean")
        fact_binding = {"field_bindings": [], "instances": [],
                        "coverage": {"status": "disabled", "partial": False,
                                     "reason": "fact_type_binding.enabled=false"}}
        if binding_options.get("enabled", False):
            stage = progress.task("业务事实类型绑定", 1) if progress else nullcontext(None)
            with stage as task:
                fact_binding = completed_stage("fact_binding") or await bind_fact_observations(
                    data, fact_result, plan, llm,
                    **{key: value for key, value in binding_options.items()
                       if key != "enabled"},
                    field_templates=fact_schema["field_templates"],
                    canonical_type_map=equivalence["canonical_map"],
                    equivalence_assertions=equivalence["equivalence_assertions"])
                if task:
                    task.advance(detail=str(fact_binding["coverage"]["instances_created"]))
            fact_binding["coverage"]["partial"] = bool(
                fact_binding["coverage"]["candidate_tuples_not_instantiated"])
            fact_binding["coverage"]["status"] = (
                "partial" if fact_binding["coverage"]["partial"] else "complete")
        write_yaml(output / "business_fact_field_bindings.yaml", fact_binding["field_bindings"])
        write_yaml(output / "business_fact_instances.yaml", fact_binding["instances"])
        write_yaml(output / "business_fact_binding_coverage.yaml", fact_binding["coverage"])
        manifest["fact_type_binding"] = {
            "status": fact_binding["coverage"]["status"],
            "binding_attempts": fact_binding["coverage"].get("binding_attempts", 0),
            "accepted_fields": fact_binding["coverage"].get("accepted_fields", 0),
            "instances_created": len(fact_binding["instances"]),
            "candidate_tuples_not_instantiated": fact_binding["coverage"].get(
                "candidate_tuples_not_instantiated", 0),
            "partial": fact_binding["coverage"]["partial"],
        }
        fact_value_attributes = add_fact_value_attributes(
            ontology, fact_binding["field_bindings"])
        if fact_value_attributes:
            write_yaml(output / "ontology.yaml", ontology)
        manifest["fact_type_binding"]["value_attributes"] = len(fact_value_attributes)
        manifest["semantic_cards"] = {"cards_indexed": card_report.get("cards_indexed", 0),
                                      "rows_scanned": card_report.get("rows_scanned", 0),
                                      "partial": card_report.get("partial", False)}
        manifest["instance_bundles"] = {"built": bundle_report.get("bundles_built", 0),
                                         "coverage": bundle_report, "partial": bundle_report.get("partial", False)}
        manifest["group_incremental"] = {"accepted": sum(s["status"] == "accepted" for s in group_result["steps"]),
                                         "steps": len(group_result["steps"]),
                                         "skipped": group_result["bundles_skipped"],
                                         "business_relation_types": sum(
                                             item.category == "business_relation_type"
                                             for item in plan.relation_types),
                                         "business_relation_assertions": len(
                                             group_result["concept_relations"]),
                                         "relation_derivations_not_promoted": sum(
                                             item["status"] == "not_promoted" for item in
                                             group_result["concept_relation_derivations"]),
                                         "partial": group_result["partial"]}
        if config.get("instance_bundles", {}).get("enabled", False):
            save_state(output / "semantic_state.json", data, profile,
                       {**group_result, "plan": plan, "stage_signature": stage_signature,
                        "stage_outputs": {name: {key: value for key, value in result.items() if key != "plan"}
                                          for name, result in {
                                              "generalization": generalization,
                                              "configuration_specs": configuration_specs,
                                              "configuration_relations": configuration_relations,
                                              "config_decisions": config_decisions,
                                              "equivalence": equivalence,
                                              "fact_schema": fact_schema,
                                              "fact_binding": fact_binding}.items()}},
                       implementation_code_hash=code_hash)
        write_yaml(output / "business_concepts.yaml", group_result["concepts"])
        write_yaml(output / "template_projections.yaml", group_result.get("template_projections", []))
        write_yaml(output / "template_bindings.yaml", group_result.get("template_bindings", []))
        write_yaml(output / "record_alignments.yaml", group_result["record_alignments"])
        write_yaml(output / "concept_relations.yaml", group_result["concept_relations"])
        write_yaml(output / "concept_relation_derivations.yaml",
                   group_result["concept_relation_derivations"])
        instance_graph = {"objects": [], "assertions": [], "source_mappings": [], "pending": [],
                          "coverage": {"status": "disabled", "partial": False}}
        if instance_options.get("enabled", False):
            from .ontology_instances import materialize_ontology_instances
            stage = progress.task("本体来源实例化", 1) if progress else nullcontext(None)
            with stage as task:
                instance_graph = materialize_ontology_instances(
                    data, plan, template_projections=group_result.get("template_projections", []),
                    template_bindings=group_result.get("template_bindings", []),
                    ontology_bindings=(template_relations.get("bindings", [])
                                       if config.get("instance_bundles", {}).get("enabled", False) else [])
                                      + config_decisions.get("bindings", []),
                    calculation_contracts=calculations, concepts=group_result["concepts"],
                    record_alignments=group_result["record_alignments"], memberships=memberships["memberships"],
                    fact_instances=fact_binding["instances"],
                    **{key: value for key, value in instance_options.items() if key != "enabled"})
                if task:
                    task.advance(detail=f"{len(instance_graph['objects'])} 节点 / {len(instance_graph['assertions'])} 关系")
        manifest["ontology_instantiation"] = instance_graph["coverage"]
        write_yaml(output / "ontology_instance_coverage.yaml", instance_graph["coverage"])
        write_yaml(output / "ontology_instance_mappings.yaml", instance_graph["source_mappings"])
        write_yaml(output / "ontology_instance_pending.yaml", instance_graph["pending"])
        for obj in instance_graph["objects"]:
            sink.put("objects", obj)
        for assertion in instance_graph["assertions"]:
            sink.put("assertions", assertion)
        for ev in data.evidence.values():
            sink.put("evidence", ev)
        for concept in group_result["concepts"]:
            sink.put("objects", concept)
        put_fact_instances(sink, fact_binding["instances"], fact_value_attributes)
        for alignment in group_result["record_alignments"]:
            sink.put("record_alignments", alignment)
        for relation in group_result["concept_relations"]:
            sink.put("assertions", relation)
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
            "column_roles_not_fully_inferred": bool(manifest["column_role_inference"]["partial"]),
            "fact_observation_candidates_partial": bool(manifest["fact_observations"]["partial"]),
            "fact_type_binding_partial": bool(manifest["fact_type_binding"]["partial"]),
            "fact_schema_induction_partial": bool(fact_schema["coverage"].get("partial")),
            "association_rules_partial": bool(manifest["association_rules"]["partial"]),
            "semantic_cards_partial": bool(manifest["semantic_cards"]["partial"]),
            "instance_bundles_partial": bool(manifest["instance_bundles"]["partial"]),
            "definition_membership_or_reference_pending": bool(memberships["coverage"].get("partial")),
            "template_binding_incomplete": bool(manifest.get("template_bindings", {}).get("partial")),
            "template_relations_unresolved": bool(manifest.get("template_relations", {}).get("partial")),
            "ontology_instances_incomplete": bool(manifest["ontology_instantiation"].get("partial")),
            "targeted_repair_gaps": bool(manifest.get("targeted_repair", {}).get("partial")),
            "group_incremental_partial": bool(manifest["group_incremental"]["partial"]),
            "calculation_operands_unresolved": bool(calculations["coverage"].get("partial")),
            "type_generalization_partial": bool(manifest["type_generalization"]["partial"]),
            "type_equivalence_partial": bool(manifest["type_equivalence"]["partial"]),
            "configuration_relation_candidates_partial": bool(
                manifest["configuration_relations"]["partial"]),
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
