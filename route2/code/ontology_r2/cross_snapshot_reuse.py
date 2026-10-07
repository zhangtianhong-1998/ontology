"""Versioned fact-mapping replay, independent of interrupted-run recovery.

Only previously accepted field bindings and source-checked type definitions are
executable. Relation schemas are preserved for inspection but queued for their
own compiler replay; this entry never infers or executes them from shared IDs.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .column_roles import is_sensitive_column
from .fact_observations import build_fact_observation_candidates
from .fact_schema_induction import field_contract_fingerprint
from .fact_type_binding import FactFieldBindingDecision, bind_fact_observations
from .models import BuildPlan
from .semantic_state import implementation_fingerprints
from .storage import Dataset, digest, read_yaml, write_yaml

FORMAT = "ontology_fact_reuse/v1"


def _profile_hash(profile):
    if profile is None:
        profile = read_yaml(Path(__file__).resolve().parents[2] / "ontologies/internal_model.yaml")
    if not isinstance(profile, dict) or not profile.get("object_roots") or not profile.get("relation_roots"):
        raise ValueError("Reuse requires an explicit valid internal ontology profile")
    return digest(profile)


def _schema_contract(table):
    return digest({key: table.get(key) for key in (
        "name", "table_comment", "columns", "pk", "semantic_excluded_columns")})


def _evidence_ids(value):
    ids = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "evidence_ids" and isinstance(item, list):
                ids.update(item)
            else:
                ids.update(_evidence_ids(item))
    elif isinstance(value, list):
        for item in value:
            ids.update(_evidence_ids(item))
    return ids


def _record_content(table, row):
    return {name: row.get(name) for name in table["column_names"]}


def _save_evidence(data, evidence_id):
    evidence = deepcopy(data.evidence.get(evidence_id) or {})
    ref = evidence.get("source_ref") or {}
    table = data.tables.get(ref.get("table"))
    if not table or evidence.get("raw_fragment_truncated"):
        return {"status": "unsupported", "reason": "source_evidence_missing_or_incomplete"}
    saved = {"status": "supported", "evidence": evidence,
             "schema_contract": _schema_contract(table)}
    if evidence.get("origin") == "declared_metadata":
        return saved
    if evidence.get("origin") != "observed_record" or type(ref.get("row")) is not int:
        return {"status": "unsupported", "reason": "non_literal_evidence_requires_compiler_replay"}
    try:
        rows = list(data.rows_at(table["name"], [ref["row"]]))
    except ValueError:
        return {"status": "unsupported", "reason": "record_locator_invalid"}
    if len(rows) != 1 or ref.get("column") not in table["column_names"]:
        return {"status": "unsupported", "reason": "record_locator_invalid"}
    row = rows[0]
    if (data.record_id(table["name"], row) != ref.get("record_id")
            or str(row[ref["column"]]) != str(evidence.get("raw_fragment"))):
        return {"status": "unsupported", "reason": "record_evidence_not_complete_literal_cell"}
    keys = table["pk"] or [ref["column"]]
    hidden = set(table.get("semantic_excluded_columns", ())) | {
        column["column_name"] for column in table["columns"] if is_sensitive_column(column)}
    if set(keys) & hidden or ref["column"] in hidden:
        return {"status": "unsupported", "reason": "replay_key_or_evidence_excluded_by_privacy"}
    selector = [(name, row[name]) for name in keys]
    if len(data.lookup(table["name"], tuple(selector))) != 1:
        return {"status": "unsupported", "reason": "definition_has_no_unique_replay_key"}
    saved.update(selector=selector, record_content_digest=digest(_record_content(table, row)))
    return saved


def export_reuse_bundle(data, core, field_templates=(), field_bindings=(), *,
                        fact_observations=None, profile=None):
    """Freeze accepted mappings and their complete source witnesses locally.

    The content digest detects accidental edits; it is not an authenticity
    signature. Consumers must use an artifact produced by a trusted local run.
    """
    plan = BuildPlan.model_validate(core)
    reports = {report["table"]: report for report in
               (fact_observations or {}).get("tables", [])}
    types = {item.id: item for item in plan.object_types}
    mappings, pending = [], []
    existing = {(item.get("table"), item.get("value_column")): item
                for item in field_templates}
    for field in field_bindings:
        if field.get("status") != "accepted_field_binding":
            continue
        table, name = field["table"], field["value_column"]
        coordinates = list(reports.get(table, {}).get("coordinate_columns") or [])
        type_id = field.get("selected_type_id") or field.get("type_id")
        item = types.get(type_id)
        if not item or not coordinates or field.get("equivalence_assertion_ids"):
            pending.append({"table": table, "value_column": name,
                            "reason": "mapping_coordinates_or_equivalence_replay_unsupported"})
            continue
        prior = existing.get((table, name))
        decision = FactFieldBindingDecision(
            status="bind", type_id=type_id,
            source_column_quote=field["column_comment_quote"],
            type_definition_quote=field["type_definition_quote"],
            source_definition_evidence_id=field["definition_evidence_id"],
            type_source_quote=field["type_source_quote"],
            scope_bindings=field.get("scope_bindings", {}),
            constant_scope_bindings=field.get("constant_scope_bindings", {}),
            unit_column=field.get("unit_column"))
        mappings.append({"table": table, "value_column": name, "type_id": type_id,
            "coordinate_columns": coordinates,
            "schema_fingerprint": field_contract_fingerprint(data.tables[table], name, coordinates),
            "binding_decision": decision.model_dump(),
            "prior_template_id": (prior or {}).get("id")})
    ids = set().union(*(_evidence_ids(item.model_dump()) for item in plan.object_types))
    ids.update(item["binding_decision"]["source_definition_evidence_id"] for item in mappings)
    bundle = {"format": FORMAT, "source_snapshot_id": data.snapshot_id,
        "semantic_code_hash": implementation_fingerprints()["semantic_code_hash"],
        "profile_hash": _profile_hash(profile),
        "ontology": plan.model_dump(), "field_mappings": mappings,
        "evidence_contracts": {eid: _save_evidence(data, eid) for eid in sorted(ids)},
        "export_pending": pending,
        "execution_scope": "verified_fact_field_bindings_only",
        "relation_reuse": "retained_schema_requires_separate_relation_compiler_replay"}
    bundle["bundle_id"] = "reuse_bundle:" + digest(bundle)
    return bundle


def _replay_evidence(data, saved):
    if saved.get("status") != "supported":
        raise ValueError(saved.get("reason", "unsupported_source_evidence"))
    evidence = saved["evidence"]
    ref = evidence["source_ref"]
    table = data.tables.get(ref.get("table"))
    if not table or saved["schema_contract"] != _schema_contract(table):
        raise ValueError("source_schema_contract_changed")
    current = deepcopy(evidence)
    if evidence.get("origin") == "declared_metadata":
        if evidence["id"] in data.evidence:
            actual = data.evidence[evidence["id"]]
            if actual.get("raw_fragment") != evidence.get("raw_fragment"):
                raise ValueError("source_metadata_meaning_changed")
            current = deepcopy(actual)
        else:
            column = next((item for item in table["columns"]
                           if item["column_name"] == ref.get("column")), None)
            raw = ((column.get("column_comment") or "").strip()
                   if column and evidence.get("declaration_kind") == "column_comment"
                   else column["column_name"] if column and evidence.get("declaration_kind") == "column_name"
                   else None)
            if raw != evidence.get("raw_fragment"):
                raise ValueError("source_field_declaration_changed")
        current["source_ref"]["snapshot_id"] = data.snapshot_id
    else:
        rows = data.lookup(table["name"], tuple(tuple(pair) for pair in saved["selector"]))
        if len(rows) != 1:
            raise ValueError("source_definition_missing_or_ambiguous")
        row = rows[0]
        if (digest(_record_content(table, row)) != saved["record_content_digest"]
                or str(row[ref["column"]]) != str(evidence["raw_fragment"])):
            raise ValueError("source_definition_record_changed")
        record_id = data.record_id(table["name"], row)
        current["id"] = "record:" + digest([data.snapshot_id, record_id, ref["column"]])[:24]
        current["source_ref"] = {**ref, "snapshot_id": data.snapshot_id,
                                 "record_id": record_id, "row": row["__r2_row"]}
    data.evidence[current["id"]] = current
    return current["id"]


def _replace_ids(value, mapping):
    if isinstance(value, dict):
        return {key: _replace_ids(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_ids(item, mapping) for item in value]
    return mapping.get(value, value) if isinstance(value, str) else value


class _NoModel:
    async def ask(self, *args, **kwargs):
        raise RuntimeError("cross_snapshot_reuse_does_not_call_llm")


async def reuse_on_snapshot(data, fact_observations, bundle, *, association_context=None, profile=None):
    """Recheck old contracts against new source; never relax resume checks."""
    signature = {key: value for key, value in bundle.items() if key != "bundle_id"}
    if bundle.get("format") != FORMAT or bundle.get("bundle_id") != "reuse_bundle:" + digest(signature):
        raise ValueError("Invalid or edited cross-snapshot reuse bundle")
    if bundle.get("semantic_code_hash") != implementation_fingerprints()["semantic_code_hash"]:
        raise ValueError("Reuse bundle semantic validation contract differs from this implementation")
    if bundle.get("profile_hash") != _profile_hash(profile):
        raise ValueError("Reuse bundle internal ontology profile differs from this run")
    prior = BuildPlan.model_validate(bundle["ontology"])
    evidence_map, failed = {}, {}
    for eid, contract in bundle.get("evidence_contracts", {}).items():
        try:
            evidence_map[eid] = _replay_evidence(data, contract)
        except (ValueError, KeyError) as exc:
            failed[eid] = str(exc)
    plan, pending = BuildPlan(), []
    for item in prior.object_types:
        ids = _evidence_ids(item.model_dump())
        if not ids or any(eid not in evidence_map for eid in ids):
            pending.append({"kind": "type", "type_id": item.id,
                            "reason": "type_source_contract_not_revalidated",
                            "evidence_failures": {eid: failed.get(eid, "evidence_contract_missing")
                                                  for eid in ids if eid not in evidence_map}})
        else:
            refreshed = type(item).model_validate(_replace_ids(item.model_dump(), evidence_map))
            # Historical concept instances belong to the previous snapshot.
            # The immutable bundle retains them; the current type cites fresh
            # record evidence and must not expose stale concept endpoints.
            refreshed.source_concept_ids = []
            plan.object_types.append(refreshed)
    # A derived type may depend on an unavailable parent. Invalidate the whole
    # dependent chain rather than silently attach it to a root.
    roots = {"GeneralObject", "Measure", "Metric", "Dimension", "Term"}
    while True:
        valid = roots | {item.id for item in plan.object_types}
        rejected = [item for item in plan.object_types if item.parent not in valid]
        if not rejected:
            break
        for item in rejected:
            plan.object_types.remove(item)
            pending.append({"kind": "type", "type_id": item.id, "reason": "parent_type_not_revalidated"})
    for relation in prior.relation_types:
        pending.append({"kind": "relation", "type_id": relation.id,
                        "reason": "relation_contract_requires_separate_compiler_replay"})
    by_id = {item.id: item for item in plan.object_types}
    reports = {item["table"]: item for item in fact_observations.get("tables", [])}
    templates, mapped = [], set()
    for mapping in bundle.get("field_mappings", []):
        table, name = mapping["table"], mapping["value_column"]
        coordinates = reports.get(table, {}).get("coordinate_columns", [])
        item = by_id.get(mapping["type_id"])
        if (table not in data.tables or item is None or coordinates != mapping["coordinate_columns"]
                or field_contract_fingerprint(data.tables[table], name, coordinates)
                != mapping["schema_fingerprint"]):
            pending.append({"kind": "field", "table": table, "value_column": name,
                            "reason": "field_or_type_contract_changed"})
            continue
        decision = _replace_ids(mapping["binding_decision"], evidence_map)
        templates.append({"id": "replayed_field:" + digest([bundle["bundle_id"], table, name])[:24],
            "status": "accepted_field_template", "table": table, "value_column": name,
            "type_id": item.id, "source_snapshot_id": data.snapshot_id,
            "schema_fingerprint": mapping["schema_fingerprint"],
            "type_signature": digest(item.model_dump()), "binding_decision": decision,
            "reuse_bundle_id": bundle["bundle_id"]})
        mapped.add((table, name))
    binding = await bind_fact_observations(data, fact_observations, plan, _NoModel(),
                                           max_binding_calls=0, field_templates=templates)
    # No-model replay is intentional, not an exhausted execution budget.
    for field in binding["field_bindings"]:
        if field.get("reason") == "binding_call_budget_exhausted":
            field["reason"] = "no_revalidated_mapping"
    skipped = binding["coverage"]["skipped_reasons"]
    if "binding_call_budget_exhausted" in skipped:
        skipped["no_revalidated_mapping"] = skipped.pop("binding_call_budget_exhausted")
    for report in fact_observations.get("tables", []):
        if report.get("row_purpose") == "business_fact":
            for field in report.get("value_fields", []):
                if (report["table"], field["column"]) not in mapped:
                    pending.append({"kind": "new_semantic", "table": report["table"],
                                    "value_column": field["column"], "reason": "no_revalidated_mapping"})
    pending.extend({"kind": "field", **field} for field in binding["field_bindings"]
                   if field.get("status") != "accepted_field_binding")
    observed_coverage = deepcopy(fact_observations.get("coverage") or {})
    unbound = binding["coverage"]["candidate_tuples_not_instantiated"]
    partial_reasons = [reason for reason, present in (
        ("new_or_invalidated_contracts", bool(pending)),
        ("fact_observation_coverage_partial", bool(observed_coverage.get("partial"))),
        ("uninstantiated_observation_candidates", bool(unbound)),
        ("unresolved_input_table_purpose", bool(observed_coverage.get("unresolved_tables")))) if present]
    return {"plan": plan, "field_templates": templates, "fact_binding": binding,
        "pending": pending, "coverage": {"source_bundle_id": bundle["bundle_id"],
            "source_snapshot_id": data.snapshot_id, "prior_snapshot_id": bundle["source_snapshot_id"],
            "revalidated_types": len(plan.object_types), "revalidated_mapping_candidates": len(templates),
            "instances_created": len(binding["instances"]), "model_calls": 0,
            "candidate_tuples": binding["coverage"]["candidate_tuples"],
            "candidate_tuples_not_instantiated": unbound,
            "fact_observation_coverage": observed_coverage,
            "fact_observation_limits": deepcopy(fact_observations.get("limits") or {}),
            "table_scopes": [{key: report.get(key) for key in (
                "table", "row_purpose", "input_rows", "scan_scope", "emitted_candidates",
                "omitted_observed_tuples", "omitted_observed_tuples_status", "selected_columns")}
                for report in fact_observations.get("tables", [])],
            "new_or_invalidated_items": len(pending), "partial": bool(partial_reasons),
            "partial_reasons": partial_reasons}}


async def run_reuse(dataset, bundle_path, output, *, profile=None):
    """Local offline command; output must be a new directory."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    data = Dataset(dataset, output)
    try:
        facts = build_fact_observation_candidates(data)
        result = await reuse_on_snapshot(data, facts, read_yaml(bundle_path),
                                         profile=read_yaml(profile) if profile else None)
        write_yaml(output / "reused_plan.yaml", result["plan"].model_dump())
        write_yaml(output / "fact_observation_candidates.yaml", facts)
        write_yaml(output / "business_fact_instances.yaml", result["fact_binding"]["instances"])
        write_yaml(output / "business_fact_field_bindings.yaml", result["fact_binding"]["field_bindings"])
        write_yaml(output / "reuse_pending.yaml", result["pending"])
        write_yaml(output / "reuse_coverage.yaml", result["coverage"])
        write_yaml(output / "evidence.yaml", list(data.evidence.values()))
        return result["coverage"]
    finally:
        data.close()
