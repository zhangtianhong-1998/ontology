"""Explain partial template matches and schedule bounded, evidence-local repairs.

Matching a few fields is retrieval evidence, never type identity. Only the
complete projection matcher authorizes bindings. Repair groups are scheduling
groups: unsubmitted rows are not claimed to be semantically covered.
"""
from collections import Counter
from copy import deepcopy
import json
import re

from .models import BuildPlan
from .storage import digest
from .template_projection import _fields, _match_text, _SEMANTIC, _slot_target_mismatches, match_projection


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
    semantic = {key for key in values if roles[key] & _SEMANTIC
                and key not in template.get("instance_columns", {})}
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
    if template.get("class_definition_evidence"):
        class_supported = all(values.get(item["column"]) == item["quote"]
                              for item in template["class_definition_evidence"])
    for name in _slot_target_mismatches(template["slots"], captures):
        missing["slot:" + name] = "captured_value_not_supported_by_target_name_or_alias"
    protected = {column for column in values if roles[column] & {"formula", "unit", "identity"}}
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
            "class_definition", "class_definition_evidence", "definition_parameters", "identity_qualifiers", "applicability_scope", "semantic_columns", "invariants", "instance_columns", "field_templates")}
        report["accepted_template"]["slots"] = [{key: deepcopy(slot.get(key)) for key in (
            "name", "role", "label", "source_column", "fixed_value", "target_type_id", "target_component", "target_value_contract")
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
             "identity_qualifiers": item.identity_qualifiers,
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


def _class_declaration_candidates(record):
    """Recall whole, explicitly declarative clauses; never invent a class name.

    These source fragments are scheduling candidates only. The source fields
    remain complete in the packet and the normal compiler/reviewer decides
    whether a quoted clause really declares the proposed business class.
    """
    result = []
    for entry in record.get("fields", {}).get("description", []):
        if entry.get("truncated"):
            continue
        raw = str(entry["value"])
        for clause in re.split(r"[。！？；;\n]|(?<!\d)\.(?:\s+|$)|[!?](?:\s+|$)", raw):
            quote = clause.strip()
            chinese = re.fullmatch(r"([^，,]{1,80}?)(?:是一(?:类|种)|是用于|是用来).+", quote)
            english = re.fullmatch(r"(.{1,80}?)\s+is\s+(?:a|an)\s+(?:class|type|kind|category)\s+of\s+.+",
                                   quote, flags=re.I)
            matched = chinese or english
            if matched and not re.search(r"(?:不|并非|不是|非)$", matched[1]):
                result.append({"column": entry["column"], "quote": quote,
                               "label_fragment": matched[1].strip(), "status": "source_clause_candidate"})
    return result


def _repair_type_identity(data, record):
    """Keep error grouping separate from source-backed class identity."""
    from .template_projection import _instance_columns

    values, roles = _fields(record)
    clauses = _class_declaration_candidates(record)
    instances = _instance_columns(data, record)
    protected = {column: value for column, value in values.items()
                 if roles[column] & {"identity", "formula", "unit"}
                 or "scope" in roles[column] and column not in instances}
    if clauses:
        return {"table": record["table"], "class_clauses": clauses, "protected": protected}
    # Without an explicit class clause, require the complete semantic record
    # rather than grouping unrelated classes that happen to fail the same way.
    return {"table": record["table"], "protected": protected,
            "semantic_fields": {column: value for column, value in values.items()
                                if roles[column] & _SEMANTIC and column not in instances}}


def _repair_evidence_bytes(packet):
    """Bound complete record evidence, independently of scheduling provenance.

    Source bundle IDs and duplicate template context stay on the saved packet;
    the caller accounts for the actual serialized model request separately.
    """
    return len(json.dumps(packet.get("records", []), ensure_ascii=False,
                          default=str).encode("utf-8"))


def augment_template_repair_witnesses(data, index, repair_bundles, *, max_witnesses=2,
                                      max_candidates=16, max_bundle_bytes=24000):
    """Recall bounded variation witnesses from accepted or source-declared classes.

    Retrieval only enriches a field-gap or unresolved-definition repair. Every hit is refreshed from
    its source row, and neither shared prose nor a lookup hit grants exact type
    identity. In particular, the original exact-alignment allowlist is preserved.
    """
    from .definition_memberships import _read_rows
    from .template_projection import _instance_columns

    if type(max_witnesses) is not int or not 0 <= max_witnesses <= 2:
        raise ValueError("Variation witness limit must be in 0..2")
    if type(max_candidates) is not int or not 1 <= max_candidates <= 100:
        raise ValueError("Variation candidate limit must be in 1..100")
    if type(max_bundle_bytes) is not int or max_bundle_bytes < 1000:
        raise ValueError("Variation bundle byte limit must be at least 1000")
    if index.coverage.get("snapshot_id") != data.snapshot_id:
        raise ValueError("Variation witness index has a different snapshot")
    output, counts, search_cache = [], Counter(), {}
    for original in repair_bundles:
        packet = deepcopy(original)
        output.append(packet)
        task = packet.get("template_repair_task") or {}
        seed = representative_record(packet)
        if (not max_witnesses or task.get("scope") not in {
                "template_field_gaps", "unresolved_source_definition"} or seed is None):
            continue
        seed_values, seed_roles = _fields(seed)
        identities = {column: seed_values[column] for column in seed_values
                      if "identity" in seed_roles[column]}
        selected, cold_start = None, False
        for context in task.get("known_template_matches", []):
            template = context.get("accepted_template") or {}
            declaration = template.get("class_definition") or {}
            quote, column = declaration.get("quote"), declaration.get("column")
            if (context.get("status") == "partial_match" and context.get("unresolved_fields")
                    and not context.get("protected_fields_changed")
                    and template.get("source_table") == seed["table"]
                    and template.get("identity_qualifiers", {}) == identities
                    and isinstance(quote, str) and 0 < len(quote) <= 512
                    and "description" in seed_roles.get(column, set())
                    and quote in seed_values[column]):
                selected = template, quote, column, {item["column"] for item in context["unresolved_fields"]}
                break
        if selected is None:
            clauses = _class_declaration_candidates(seed)
            declaration = next((item for item in clauses if 0 < len(item["quote"]) <= 512), None)
            if declaration is None:
                continue
            # Cold start: shared explicit source text is enough to recall a
            # second witness, but grants neither a type nor an exact alignment.
            template = {"slots": [], "applicability_scope": {}}
            cold_start = True
            selected = template, declaration["quote"], declaration["column"], set(seed_values)
            task["class_definition_candidates"] = clauses
            task["witness_lookup_basis"] = "explicit_source_class_clause_without_accepted_template"
        template, quote, column, gap_columns = selected
        seed_identity = _repair_type_identity(data, seed)
        counts["bundles_queried"] += 1
        # Apply the stored scope filter before ranking so a common class quote
        # cannot spend the entire bounded hit window on another namespace.
        query_scope = seed.get("scope") if isinstance(seed.get("scope"), dict) else None
        excluded_pattern = seed.get("pattern_id") if cold_start else None
        query_key = (seed["table"], quote, json.dumps(query_scope, ensure_ascii=False, sort_keys=True),
                     excluded_pattern)
        if query_key not in search_cache:
            search_cache[query_key] = index.search(quote, limit=max_candidates,
                kind="definition", table=seed["table"], scope=query_scope,
                exclude_pattern_id=excluded_pattern)
            counts["index_queries"] += 1
            # Prefer another complete semantic pattern over repeated IDs of
            # the seed. If none exists, instance-only variation can still help.
            if not search_cache[query_key] and excluded_pattern:
                search_cache[query_key] = index.search(quote, limit=max_candidates,
                    kind="definition", table=seed["table"], scope=query_scope)
                counts["index_queries"] += 1
        hits = search_cache[query_key]
        counts["index_hits_examined"] += len(hits)
        seen = {record["record_id"] for record in packet["records"]}
        additions = 0
        for hit in hits:
            if hit["table"] != seed["table"] or hit["record_id"] in seen:
                continue
            columns = {entry["column"] for entries in hit.get("fields", {}).values() for entry in entries}
            row = _read_rows(data, hit["table"], [hit["row_number"]], columns).get(hit["row_number"])
            if row is None or data.record_id(hit["table"], row) != hit["record_id"]:
                counts["stale_source_hits"] += 1
                continue
            card = deepcopy(hit)
            for entries in card["fields"].values():
                for entry in entries:
                    entry.update(value=str(row.get(entry["column"]) or ""), truncated=False)
            values, roles = _fields(card)
            observed_identity = {field: values[field] for field in values if "identity" in roles[field]}
            if (observed_identity != identities or "description" not in roles.get(column, set())
                    or quote not in values[column]
                    or _repair_type_identity(data, card) != seed_identity
                    or any(values.get(field) != value for field, value in template.get("applicability_scope", {}).items())):
                counts["incompatible_source_hits"] += 1
                continue
            instance_fields = set(_instance_columns(data, card, template.get("slots", [])))
            instance_fields.update(field for field in values if roles[field] & {"name", "reference"})
            varied = sorted(field for field in instance_fields & seed_values.keys()
                            if values[field] != seed_values[field])
            # ID variation alone is not useful: require a different descriptive
            # instance field as well as the identical quoted class definition.
            if (not varied or not any(roles[field] & {"name", "scope", "description"} for field in varied)
                    or not any(field in values and field in seed_values and values[field] != seed_values[field]
                               for field in gap_columns)):
                continue
            card.update(context_role="repair_witness", candidate_status="unjudged",
                        identity_claim="none", variation_template_id=template.get("template_id"))
            proposed = deepcopy(packet)
            proposed["records"].append(card)
            proposed.setdefault("exact_alignment_record_ids", [seed["record_id"]])
            proposed["template_repair_task"].setdefault("variation_witnesses", []).append({
                "record_id": card["record_id"], "source_column": column,
                "class_definition_quote": quote, "changed_instance_fields": varied,
                "status": "candidate_only_not_exact_alignment"})
            if _repair_evidence_bytes(proposed) > max_bundle_bytes:
                counts["witnesses_omitted_by_byte_limit"] += 1
                continue
            packet = proposed
            output[-1] = packet
            seen.add(card["record_id"])
            additions += 1
            counts["witnesses_added"] += 1
            if additions >= max_witnesses:
                break
    return {"bundles": output, "coverage": {**dict(counts), "llm_calls": 0,
        "max_witnesses_per_bundle": max_witnesses, "max_candidates_per_bundle": max_candidates,
        "max_bundle_bytes": max_bundle_bytes, "exact_alignments_added": 0,
        "scope": "bounded_same_table_class_quote_candidates"}}


def build_targeted_repair_tasks(data, bundles, prior_result, *, max_tasks=24, max_examples=3, round_number=2,
                                max_value_tasks=64, max_bundle_bytes=24000):
    """Build new task IDs for gaps, preserving the first pass and its bindings."""
    if any(type(value) is not int or value < 1 for value in (max_examples, round_number)):
        raise ValueError("Repair examples and round number must be positive integers")
    if type(max_tasks) is not int or max_tasks < 0:
        raise ValueError("Repair task limit must be nonnegative")
    if type(max_value_tasks) is not int or max_value_tasks < 0:
        raise ValueError("Observed value task limit must be nonnegative")
    if type(max_bundle_bytes) is not int or max_bundle_bytes < 1000:
        raise ValueError("Repair bundle byte limit must be at least 1000")
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
    previous = {}
    # Repair IDs are new each round. Map the latest outcome back to every
    # original bundle it addressed, including failed/no-change repairs.
    for step in prior_result.get("steps", []):
        previous[step["bundle_id"]] = step
        sources = set(step.get("source_bundle_ids", [])) | set(step.get("resolved_source_bundle_ids", []))
        if step.get("source_bundle_id"):
            sources.add(step["source_bundle_id"])
        for source_id in sources:
            previous[source_id] = step
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
        # Complete projection proof already establishes one class while its
        # checked observation slots may vary. Unproven partial/error groups
        # still need a source identity boundary independent of their error.
        signature.append({"accepted_type_id": preserved} if scope == "template_slot_endpoints"
                         else _repair_type_identity(data, seed))
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
        added = []
        for item in group["examples"]:
            if item["record_id"] not in seen:
                added.append({**item, "context_role": "repair_witness"})
        seed = representative_record(packet)
        context = {"task_id": task_id, "round_number": round_number, "scope": group["scope"],
                   "source_bundle_ids": list(group["source_bundle_ids"]),
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
                      source_bundle_ids=list(group["source_bundle_ids"]),
                      template_repair_task=context, template_repair_sources=group["example_sources"])
        packet.setdefault("exact_alignment_record_ids", [seed["record_id"]])
        for record in added:
            proposed = {**packet, "records": [*packet["records"], record]}
            if _repair_evidence_bytes(proposed) > max_bundle_bytes:
                counts["group_witnesses_omitted_by_byte_limit"] += 1
                continue
            packet["records"].append(record)
        context["representatives_submitted"] = sum(
            item["record_id"] in {record["record_id"] for record in packet["records"]}
            for item in group["examples"])
        if _repair_evidence_bytes(packet) > max_bundle_bytes:
            # Existing input is retained for the caller's real request budget
            # check. This limit only prevents adding more source evidence.
            counts["original_evidence_over_byte_limit"] += 1
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
