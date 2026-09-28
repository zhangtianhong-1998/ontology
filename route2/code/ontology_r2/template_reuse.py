"""Explain partial template matches and schedule bounded, evidence-local repairs.

Matching a few fields is retrieval evidence, never type identity. Only the
complete projection matcher authorizes bindings. Repair groups are scheduling
groups: unsubmitted rows are not claimed to be semantically covered.
"""
from collections import Counter
from copy import deepcopy
import json

from .models import BuildPlan
from .storage import digest
from .template_projection import _fields, _match_text, _SEMANTIC, match_projection


SEMANTIC_SLOTS = {"business_object", "measure", "dimension"}


def representative_record(bundle):
    allowed = set(bundle.get("exact_alignment_record_ids") or ())
    return next((item for item in bundle.get("records", [])
                 if item.get("context_role") != "related_context"
                 and (not allowed or item["record_id"] in allowed)), None)


def projection_match_report(template, record):
    """Describe checked fields without promoting a partial match to identity."""
    if record.get("table") != template.get("source_table"):
        return None
    values, roles = _fields(record)
    semantic = {key for key in values if roles[key] & _SEMANTIC}
    matched, missing = set(), {}
    for column in set(template["semantic_columns"]) ^ semantic:
        missing[column] = "semantic_field_set_changed"
    for column, expected in template["invariants"].items():
        if values.get(column) == expected:
            matched.add(column)
        else:
            missing[column] = "invariant_changed"
    verified_invariants = sorted(matched)
    slots = {item["name"]: item for item in template["slots"]}
    captures = {name: slot["fixed_value"] for name, slot in slots.items() if "fixed_value" in slot}
    pending = list(template["field_templates"])
    while pending:
        progressed = False
        for field in pending[:]:
            column = field["column"]
            try:
                found = (_match_text(field["template"], values[column], slots, values, captures)
                         if column in values else None)
            except ValueError as error:
                if str(error) == "Adjacent unbound slots are ambiguous":
                    continue
                found = None
            pending.remove(field)
            progressed = True
            if found is None:
                missing[column] = "field_contract_mismatch"
            else:
                matched.add(column)
                captures = found
        if not progressed:
            for field in pending:
                missing[field["column"]] = "unbound_slot_ambiguity"
            break
    try:
        complete = match_projection(template, record)
    except ValueError:
        complete = None
    class_quote = (template.get("class_definition") or {}).get("quote") or template.get("definition", "")
    class_supported = bool(class_quote and any(class_quote in values[column]
                           for column in values if "description" in roles[column]))
    protected = {column for column in values if roles[column] & {"formula", "unit"}}
    unresolved_slots = [{"name": slot["name"], "role": slot["role"], "label": slot["label"],
                         "observed_value": captures.get(slot["name"])}
                        for slot in template["slots"]
                        if slot["role"] in SEMANTIC_SLOTS and not slot.get("target_type_id")]
    return {"template_id": template["template_id"], "object_type_id": template["object_type_id"],
            "status": "complete_match" if complete else "partial_match",
            "identity_claim": "complete_contract_only" if complete else "none",
            "source_record_id": record["record_id"], "matched_columns": sorted(matched - set(missing)),
            "unresolved_fields": [{"column": key, "reason": value} for key, value in sorted(missing.items())],
            "verified_invariant_columns": verified_invariants,
            "protected_fields_changed": sorted(column for column in protected
                                               if column in template["invariants"]
                                               and values.get(column) != template["invariants"][column]),
            "class_quote_supported": class_supported, "unresolved_slots": unresolved_slots,
            "slot_values": complete["slot_values"] if complete else captures}


def template_reuse_context(templates, bundle, *, limit=4):
    """Rank all accepted contracts by source checks, not insertion recency."""
    record = representative_record(bundle)
    if record is None or limit <= 0:
        return []
    reports = []
    for template in templates:
        if template.get("status") != "accepted":
            continue
        try:
            report = projection_match_report(template, record)
        except ValueError:
            continue
        if report is None:
            continue
        # Keep partial evidence visible but do not call shared units or words
        # an identity match. The full contract remains available for review.
        report["accepted_template"] = {key: deepcopy(template.get(key)) for key in (
            "template_id", "object_type_id", "source_table", "root_type", "label", "definition",
            "class_definition", "definition_parameters", "applicability_scope", "semantic_columns", "invariants", "field_templates")}
        report["accepted_template"]["slots"] = [{key: deepcopy(slot.get(key)) for key in (
            "name", "role", "label", "source_column", "fixed_value", "target_type_id", "target_component")
            if key in slot} for slot in template["slots"]]
        reports.append(report)
    complete_ids = sorted({item["object_type_id"] for item in reports if item["status"] == "complete_match"})
    endpoint_targets = {}
    for item in reports:
        if item["status"] == "complete_match":
            for slot in item["accepted_template"]["slots"]:
                if slot.get("target_type_id"):
                    endpoint_targets.setdefault((slot["role"], slot["name"]), set()).add(slot["target_type_id"])
    conflicts = any(len(targets) > 1 for targets in endpoint_targets.values())
    for item in reports:
        item["complete_match_type_ids"] = complete_ids
        item["conflicting_accepted_endpoints"] = conflicts
    return sorted(reports, key=lambda item: (
        item["status"] != "complete_match", not item["class_quote_supported"],
        bool(item["protected_fields_changed"]), len(item["unresolved_fields"]),
        len(item["unresolved_slots"]), -len(item["matched_columns"]), item["template_id"]))[:limit]


def component_type_candidates(core, record, *, limit=24):
    """Literal source mentions recall candidates; they do not bind endpoints."""
    text = "\n".join(str(entry["value"]) for entries in record.get("fields", {}).values() for entry in entries)
    candidates = [item for item in core.object_types if item.category == "business_type"
                  and item.parent in {"Measure", "Dimension", "GeneralObject"}
                  and item.label and item.label in text]
    return [{"id": item.id, "parent": item.parent, "label": item.label, "definition": item.definition,
             "unit": item.unit, "definition_parameters": item.definition_parameters,
             "applicability_scope": item.applicability_scope,
             "retrieval_basis": "literal_source_mention_only_not_endpoint_identity"}
            for item in sorted(candidates, key=lambda item: (-len(item.label), item.id))[:limit]]


def lookup_observed_value_sources(data, index, tasks, *, max_tasks=64, max_candidates_per_task=5):
    """Recall bounded source candidates, then check complete literal cell values.

    An index hit is not a member/type assertion. Re-read its actual source row
    so truncated previews or stale row identities cannot become evidence.
    """
    from .definition_memberships import _read_rows

    if type(max_tasks) is not int or max_tasks < 0:
        raise ValueError("Observed value lookup task limit must be nonnegative")
    if type(max_candidates_per_task) is not int or not 1 <= max_candidates_per_task <= 25:
        raise ValueError("Observed value candidate limit must be in 1..25")
    if index.coverage.get("snapshot_id") != data.snapshot_id:
        raise ValueError("Observed value lookup index has a different snapshot")
    output, counts = [], Counter()
    for task in tasks[:max_tasks]:
        value = str(task.get("value") or "")
        result = {**deepcopy(task), "source_candidates": [], "candidate_scope": "bounded_existing_semantic_index"}
        if not value or len(value) > 512:
            result["lookup_status"] = "unsupported_query_length"
            output.append(result)
            continue
        hits = index.search(value, limit=max_candidates_per_task * 4)
        counts["index_hits_examined"] += len(hits)
        for hit in hits:
            columns = list({entry["column"] for entries in hit["fields"].values() for entry in entries})
            row = _read_rows(data, hit["table"], [hit["row_number"]], columns).get(hit["row_number"])
            if row is None or data.record_id(hit["table"], row) != hit["record_id"]:
                counts["stale_source_hits"] += 1
                continue
            card, matches = deepcopy(hit), []
            for role, entries in card["fields"].items():
                for entry in entries:
                    raw = str(row.get(entry["column"]) or "")
                    entry.update(value=raw, truncated=False)
                    aliases = []
                    if role == "alias":
                        try:
                            aliases = json.loads(raw)
                        except (ValueError, TypeError):
                            pass
                    if (role in {"name", "alias", "reference", "scope"}
                            and (raw == value or isinstance(aliases, list) and value in aliases)):
                        matches.append({"column": entry["column"], "role": role,
                                        "quote": raw, "match": "complete_literal_or_alias_item"})
            if not matches:
                continue  # Mere mentions in a description or BM25 similarity do not qualify.
            result["source_candidates"].append({
                "record": card, "matched_fields": matches, "snapshot_id": data.snapshot_id,
                "candidate_status": "unjudged", "identity_claim": "none", "membership_asserted": False})
            if len(result["source_candidates"]) >= max_candidates_per_task:
                break
        result["lookup_status"] = "source_candidates_found" if result["source_candidates"] else "no_checked_source_candidate"
        counts["tasks_with_candidates"] += bool(result["source_candidates"])
        output.append(result)
    examined = len(output)
    output.extend({**deepcopy(task), "source_candidates": [], "lookup_status": "not_attempted_due_to_limit"}
                  for task in tasks[max_tasks:])
    return {"tasks": output, "coverage": {
        **dict(counts), "tasks_examined": examined, "tasks_not_examined": max(0, len(tasks) - examined),
        "max_tasks": max_tasks, "max_candidates_per_task": max_candidates_per_task,
        "llm_calls": 0, "created_types": 0, "asserted_memberships": 0,
        "scope": "bounded_candidate_recall_not_exhaustive_member_discovery"}}


def build_targeted_repair_tasks(data, bundles, prior_result, *, max_tasks=24, max_examples=3, round_number=2,
                                max_value_tasks=64):
    """Build new task IDs for gaps, preserving the first pass and its bindings."""
    if any(type(value) is not int or value < 1 for value in (max_examples, round_number)):
        raise ValueError("Repair examples and round number must be positive integers")
    if type(max_tasks) is not int or max_tasks < 0:
        raise ValueError("Repair task limit must be nonnegative")
    if type(max_value_tasks) is not int or max_value_tasks < 0:
        raise ValueError("Observed value task limit must be nonnegative")
    if prior_result.get("snapshot_id") != data.snapshot_id:
        raise ValueError("Repair state must belong to the current source snapshot")
    core = prior_result.get("plan", BuildPlan())
    if isinstance(core, dict):
        core = BuildPlan.model_validate(core)
    templates = prior_result.get("template_projections", [])
    template_by_id = {item["template_id"]: item for item in templates}
    known_values, new_values = set(), {}
    for binding in prior_result.get("template_bindings", []):
        template = template_by_id.get(binding.get("template_id"))
        if template:
            for slot in template["slots"]:
                value = binding.get("slot_values", {}).get(slot["name"])
                if slot["role"] in SEMANTIC_SLOTS and value is not None:
                    known_values.add((template["source_table"], slot["role"], slot["name"], value))
    previous = {step["bundle_id"]: step for step in prior_result.get("steps", [])}
    grouped, counts = {}, Counter()
    for bundle in bundles:
        if bundle.get("task_kind") != "concept_induction":
            continue
        seed = representative_record(bundle)
        if seed is None:
            continue
        counts["concept_bundles_examined"] += 1
        contexts = template_reuse_context(templates, bundle)
        best = contexts[0] if contexts else None
        if best and best["status"] == "complete_match":
            for slot in best["accepted_template"]["slots"]:
                value = best["slot_values"].get(slot["name"])
                key = (seed["table"], slot["role"], slot["name"], value)
                if (slot["role"] in SEMANTIC_SLOTS and "fixed_value" not in slot
                        and value is not None and key not in known_values):
                    new_values.setdefault(key, {
                        "kind": "observed_value_resolution", "status": "needs_definition_or_membership_evidence",
                        "source_table": seed["table"], "source_record_id": seed["record_id"],
                        "slot": slot["name"], "role": slot["role"], "value": value,
                        "template_id": best["template_id"], "target_type_id": slot.get("target_type_id"),
                        "identity_claim": "none", "creates_ontology_type": False})
        complete_ids = set(best.get("complete_match_type_ids", [])) if best else set()
        old = previous.get(bundle["bundle_id"], {})
        if len(complete_ids) > 1 or (best and best["conflicting_accepted_endpoints"]):
            scope, preserved = "conflicting_template_matches", None
            signature = [scope, sorted(complete_ids)]
        elif len(complete_ids) == 1 and best and best["status"] == "complete_match":
            if not best["unresolved_slots"]:
                counts["fully_covered_without_model"] += 1
                continue
            scope, preserved = "template_slot_endpoints", best["object_type_id"]
            gaps = tuple((slot["role"], slot["name"], slot["label"]) for slot in best["unresolved_slots"])
            signature = [scope, preserved, gaps]
        elif best:
            scope, preserved = "template_field_gaps", None
            signature = [scope, best["object_type_id"], best["template_id"],
                         best["unresolved_fields"], best["protected_fields_changed"]]
        elif old.get("status") in {"accepted", "no_change"}:
            counts["accepted_exact_or_no_change"] += 1
            continue
        else:
            scope, preserved = "unresolved_source_definition", None
            signature = [scope, seed["table"], old.get("error_type"), old.get("reason")]
        key = digest(signature)
        if key not in grouped:
            grouped[key] = {"scope": scope, "preserve_type_id": preserved, "contexts": contexts,
                            "bundle": bundle, "examples": [], "example_sources": [], "source_bundle_ids": [], "records": set(),
                            "previous_error": old.get("reason") or "not_attempted_in_first_pass"}
        group = grouped[key]
        group["source_bundle_ids"].append(bundle["bundle_id"])
        if seed["record_id"] not in group["records"]:
            group["records"].add(seed["record_id"])
            if len(group["examples"]) < max_examples:
                group["examples"].append(deepcopy(seed))
                group["example_sources"].append({"bundle_id": bundle["bundle_id"], "record_id": seed["record_id"]})
    tasks, repair_bundles = [], []
    # Endpoint gaps have proven type matches and therefore take priority over
    # rebuilding classes whose source definition is still unknown.
    ordered = sorted(grouped.items(), key=lambda pair: (
        pair[1]["scope"] != "template_slot_endpoints", -len(pair[1]["records"]), pair[0]))
    for key, group in ordered[:max_tasks]:
        packet = deepcopy(group["bundle"])
        task_id = "template_repair:" + digest([data.snapshot_id, round_number, key])[:24]
        seen = {item["record_id"] for item in packet["records"]}
        packet["records"].extend({**item, "context_role": "repair_witness"} for item in group["examples"]
                                 if item["record_id"] not in seen)
        seed = representative_record(packet)
        context = {"task_id": task_id, "round_number": round_number, "scope": group["scope"],
                   "preserve_type_id": group["preserve_type_id"], "previous_error": group["previous_error"],
                   "known_template_matches": group["contexts"],
                   "component_type_candidates": component_type_candidates(core, seed),
                   "grouped_source_bundles": len(group["source_bundle_ids"]),
                   "representatives_submitted": len(group["examples"]),
                   "unsubmitted_records_are_not_semantically_verified": True,
                   "instruction": (
                       "Repair only the listed gap using these observed records. Preserve accepted definitions, "
                       "formulas and parameters. A candidate name is retrieval evidence, not an endpoint binding. "
                       "Unknown slot values require source definitions or membership evidence; do not invent a new type.")}
        packet.update(bundle_id=task_id, source_bundle_id=group["bundle"]["bundle_id"],
                      template_repair_task=context, template_repair_sources=group["example_sources"])
        tasks.append(context)
        repair_bundles.append(packet)
    observed_value_tasks = list(new_values.values())[:max_value_tasks]
    return {"bundles": repair_bundles, "tasks": tasks, "observed_value_tasks": observed_value_tasks, "coverage": {
        **dict(counts), "round_number": round_number, "gap_groups_found": len(grouped),
        "tasks_selected": len(tasks), "groups_not_selected": max(0, len(grouped) - len(tasks)),
        "representatives_submitted": sum(item["representatives_submitted"] for item in tasks),
        "source_records_in_gap_groups": sum(len(item["records"]) for item in grouped.values()),
        "max_tasks": max_tasks, "max_examples": max_examples, "llm_calls": 0,
        "new_observed_values": len(new_values), "observed_value_tasks_selected": len(observed_value_tasks),
        "observed_value_tasks_not_selected": max(0, len(new_values) - len(observed_value_tasks)),
        "scope": "gap_group_representatives_only_not_all_grouped_rows"}}
