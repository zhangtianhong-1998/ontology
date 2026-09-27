"""Reuse accepted definition types by complete source equality, never entity identity.

A scheduling pattern only narrows the records to inspect. Every source row is
checked against a source-grounded template; identifiers and reference variants
remain independent and do not inherit the representative's business relations.
"""

from collections import Counter, defaultdict

from .concept_candidates import _field_roles
from .storage import digest, qi


_SEMANTIC_ROLES = {"name", "alias", "description", "formula", "unit", "scope", "unknown"}


def _read_rows(data, table_name, row_numbers, columns):
    """Fetch one fixed-size batch using original row positions and parameters."""
    info = data.tables[table_name]
    fields = list(dict.fromkeys([*info.get("pk", []), *columns]))
    if not set(fields) <= set(info["column_names"]):
        raise ValueError("Template refers to an unknown source column")
    placeholders = ",".join("?" for _ in row_numbers)
    selected = ", " + ", ".join(qi(field) for field in fields) if fields else ""
    cursor = data.db.execute(f"SELECT __r2_row{selected} FROM {qi(info['sql_name'])} "
                             f"WHERE __r2_row IN ({placeholders})", row_numbers)
    names = [item[0] for item in cursor.description]
    return {row[0]: dict(zip(names, row)) for row in cursor.fetchall()}


def _semantic_columns(data, card):
    """Respect compiled card roles and retain known empty semantic fields."""
    # Start from the card compiler's effective fields. Supplementing known
    # empty fields must never restore a model name/alias rejected by that
    # compiler as an identifier binding.
    pairs = {(role, entry["column"]) for role, entries in card["fields"].items()
             if role in _SEMANTIC_ROLES for entry in entries}
    roles = _field_roles(data.tables[card["table"]])
    pairs.update((role, field) for role, fields in roles.items() for field in fields
                 if role in _SEMANTIC_ROLES)
    conflicts = {(item.get("proposed_role"), item.get("column"))
                 for item in card.get("role_conflicts", [])
                 if item.get("binding_only") and item.get("effective_role") == "reference"
                 and item.get("proposed_role") in ("name", "alias")}
    # Older cards named only alias exclusions. Do not infer a name conflict
    # from a column-wide list; a valid name or scope on that column survives.
    if not card.get("role_conflicts"):
        conflicts.update(("alias", field) for field in card.get(
            "binding_columns_excluded_from_semantic_pattern", []))
    # Only the card's checked fragment contract can demote a formula role.
    # An ordinary scope annotation alone never removes a genuine formula.
    conflicts.update(("formula", item["column"]) for item in card.get("calculation_fragments", [])
                     if item.get("column") and item.get("formula_status") == "fragment"
                     and item.get("effective_role") in ("calculation_operator", "operand_reference"))
    pairs.difference_update(conflicts)
    return sorted(pairs)


def _raw_semantics(row, pairs):
    return [[role, field, row.get(field)] for role, field in pairs]


def _template(data, index, concept, alignment):
    card_id = alignment.get("source_card_id")
    source = index.db.execute("SELECT card_id, row_number FROM card_sources WHERE record_id=?",
                              (alignment["source_record_id"],)).fetchone()
    if source is None or (card_id and source["card_id"] != card_id):
        raise ValueError("accepted_source_not_in_current_index")
    card = index.get(source["card_id"])
    if card["kind"] != "definition" or card["snapshot_id"] != data.snapshot_id:
        raise ValueError("source_is_not_current_definition_card")
    if any(entry.get("truncated") for role, entries in card["fields"].items()
           if role in _SEMANTIC_ROLES for entry in entries):
        raise ValueError("semantic_preview_truncated")
    if index.coverage.get("by_table", {}).get(card["table"], {}).get("unknown_columns_not_examined"):
        raise ValueError("semantic_columns_unexamined")
    pairs = sorted(set(_semantic_columns(data, card)) | {
        ("unknown", field) for field in index.coverage.get("by_table", {}).get(card["table"], {}).get("unknown_columns_considered", [])})
    references = [entry["column"] for entry in card["fields"].get("reference", [])]
    rows = _read_rows(data, card["table"], [source["row_number"]],
                      [field for _, field in pairs] + references)
    row = rows.get(source["row_number"])
    if row is None or data.record_id(card["table"], row) != alignment["source_record_id"]:
        raise ValueError("accepted_source_identity_changed")
    for role, entries in card["fields"].items():
        if role in _SEMANTIC_ROLES:
            if any(row.get(entry["column"]) != entry["value"] for entry in entries):
                raise ValueError("accepted_source_text_changed")
    if not any(role in {"description", "formula"} and str(row.get(field) or "").strip()
               for role, field in pairs):
        raise ValueError("name_only_template_cannot_propagate")
    evidence_ids = sorted(set(concept.get("evidence_ids", [])) | set(alignment.get("evidence_ids", [])))
    if not evidence_ids or any(eid not in data.evidence for eid in evidence_ids):
        raise ValueError("accepted_template_evidence_missing")
    semantics = _raw_semantics(row, pairs)
    template_id = "definition_template:" + digest([
        data.snapshot_id, card["table"], concept["ontology_type_id"], semantics])[:24]
    return {"id": template_id, "type_id": concept["ontology_type_id"],
            "concept_id": concept["id"], "concept_reference_role": "accepted_type_template",
            "table": card["table"], "pattern_id": card["pattern_id"],
            "representative_record_id": alignment["source_record_id"],
            "representative_card_id": card["card_id"], "representative_row_number": source["row_number"],
            "semantic_fields": [{"role": role, "column": field, "value": value}
                                for role, field, value in semantics],
            "semantic_fingerprint": digest(semantics),
            "binding_columns_excluded_from_semantic_pattern": list(card.get(
                "binding_columns_excluded_from_semantic_pattern", [])),
            "role_conflicts": list(card.get("role_conflicts", [])),
            "calculation_fragments": list(card.get("calculation_fragments", [])),
            "reference_values": {field: row.get(field) for field in references},
            "scope": card["scope"], "unit": card["unit"],
            "evidence_ids": evidence_ids,
            "identity_claim": "none; template reuse does not merge source entities"}


def build_definition_memberships(data, index, group_result, *, max_records=100000):
    """Compile accepted templates and inspect bounded source records with zero LLM calls.

    Input ``index`` is an open SemanticCardIndex. The return keeps independent
    source record identities and pending reference variants. ``max_records``
    bounds inspected records, including rejected records; all remainder counts
    refer to the complete indexed definition population, not the whole database.
    """
    if type(max_records) is not int or max_records < 0:
        raise ValueError("definition_memberships.max_records must be nonnegative")
    if (group_result.get("snapshot_id") != data.snapshot_id
            or index.coverage.get("snapshot_id") != data.snapshot_id):
        raise ValueError("Definition membership snapshots differ")
    concepts = {item["id"]: item for item in group_result.get("concepts", [])
                if item.get("ontology_level") == "type" and item.get("ontology_type_id")
                and item.get("decision") == "accepted_by_automatic_checks"}
    exact = defaultdict(set)
    for alignment in group_result.get("record_alignments", []):
        if alignment.get("mapping_kind") == "exact":
            exact[alignment["source_record_id"]].add(alignment["concept_id"])
    templates, errors = {}, []
    for alignment in group_result.get("record_alignments", []):
        concept = concepts.get(alignment.get("concept_id"))
        if not concept or alignment.get("mapping_kind") != "exact":
            continue
        if exact[alignment["source_record_id"]] != {concept["id"]}:
            errors.append({"record_id": alignment["source_record_id"], "reason": "conflicting_exact_alignment"})
            continue
        try:
            template = _template(data, index, concept, alignment)
        except ValueError as exc:
            errors.append({"record_id": alignment["source_record_id"], "reason": str(exc)})
            continue
        key = (template["pattern_id"], template["semantic_fingerprint"], template["type_id"])
        templates.setdefault(key, template)
    by_pattern = defaultdict(list)
    for template in templates.values():
        by_pattern[template["pattern_id"]].append(template)
    total = index.db.execute("SELECT count(*) FROM card_sources s JOIN cards c ON c.card_id=s.card_id "
                             "WHERE c.kind='definition'").fetchone()[0]
    eligible = 0
    for pattern in by_pattern:
        eligible += index.db.execute("SELECT count(*) FROM card_sources s JOIN cards c ON c.card_id=s.card_id "
                                      "WHERE c.pattern_id=? AND c.kind='definition'", (pattern,)).fetchone()[0]
    memberships, pending, rejections = [], [], []
    counts = Counter()
    for pattern, pattern_templates in sorted(by_pattern.items()):
        if counts["records_scanned"] >= max_records:
            break
        table = pattern_templates[0]["table"]
        pairs = sorted({(entry["role"], entry["column"]) for template in pattern_templates
                        for entry in template["semantic_fields"]})
        card_cache = {}
        cursor = index.db.execute("SELECT s.card_id, s.record_id, s.row_number FROM card_sources s "
                                  "JOIN cards c ON c.card_id=s.card_id WHERE c.pattern_id=? "
                                  "AND c.kind='definition' ORDER BY s.row_number, s.record_id", (pattern,))
        while counts["records_scanned"] < max_records:
            batch = cursor.fetchmany(min(256, max_records-counts["records_scanned"]))
            if not batch:
                break
            for source in batch:
                if source["card_id"] not in card_cache:
                    card_cache[source["card_id"]] = index.get(source["card_id"])
            reference_columns = sorted({entry["column"] for card in card_cache.values()
                                        for entry in card["fields"].get("reference", [])})
            rows = _read_rows(data, table, [source["row_number"] for source in batch],
                              [field for _, field in pairs] + reference_columns)
            for source in batch:
                counts["records_scanned"] += 1
                row = rows.get(source["row_number"])
                reason = None
                card = card_cache[source["card_id"]]
                if row is None or data.record_id(table, row) != source["record_id"]:
                    reason = "source_record_identity_changed"
                    matching = []
                else:
                    signature = digest(_raw_semantics(row, pairs))
                    matching = [template for template in pattern_templates
                                if template["table"] == card["table"]
                                and template["semantic_fingerprint"] == signature
                                and template["scope"] == card["scope"]
                                and template["unit"] == card["unit"]]
                    if not matching:
                        reason = "full_semantic_values_differ"
                accepted_concepts = exact.get(source["record_id"], set())
                if matching and accepted_concepts:
                    matching = [template for template in matching if accepted_concepts == {template["concept_id"]}]
                    if not matching:
                        reason = "conflicting_exact_alignment"
                if len({template["type_id"] for template in matching}) > 1:
                    reason = "conflicting_type_templates"
                if reason:
                    counts[reason] += 1
                    rejections.append({"record_id": source["record_id"], "row_number": source["row_number"],
                                       "card_id": source["card_id"], "reason": reason})
                    continue
                template = min(matching, key=lambda item: item["id"])
                refs = {column: row.get(column) for column in reference_columns}
                # Preserve the complete variant. It is deliberately not an
                # inferred relation, exact alignment, or same-as assertion.
                variant = refs != {column: template["reference_values"].get(column) for column in reference_columns}
                member = {"id": "definition_membership:" + digest([
                    data.snapshot_id, source["record_id"], template["type_id"]])[:24],
                    "kind": "definition_type_membership", "type_id": template["type_id"],
                    "concept_id": template["concept_id"], "concept_reference_role": "accepted_type_template",
                    "template_id": template["id"], "record_id": source["record_id"],
                    "table": table, "row_number": source["row_number"], "card_id": source["card_id"],
                    "pattern_id": pattern, "snapshot_id": data.snapshot_id,
                    "reference_values": refs, "reference_variants_pending": variant,
                    "mapping_kind": "shares_definition_type_template", "entity_identity_claim": False,
                    "status": "definition_template_match",
                    "verification_scope": "complete_source_semantics_only; reference_meaning_requires_separate_check",
                    "relationship_inheritance": False, "evidence_ids": template["evidence_ids"],
                    "verification": {"method": "complete_original_source_field_equality",
                                     "semantic_fingerprint": template["semantic_fingerprint"],
                                     "columns": [field for _, field in pairs]}}
                memberships.append(member)
                if variant:
                    pending.append({"record_id": source["record_id"], "table": table,
                                    "row_number": source["row_number"], "card_id": source["card_id"],
                                    "template_id": template["id"], "reference_values": refs,
                                    "status": "unresolved_reference_variant",
                                    "reason": "shared_definition_type_does_not_establish_reference_relations"})
    missing = total-eligible
    unscanned = eligible-counts["records_scanned"]
    index_partial = bool(index.coverage.get("partial") or index.coverage.get("rows_omitted_cap", 0))
    inspection_complete = not (missing or unscanned or rejections or errors or index_partial)
    return {"snapshot_id": data.snapshot_id, "templates": list(templates.values()),
            "memberships": memberships, "reference_variants_pending": pending,
            "rejections": rejections, "template_errors": errors,
            "coverage": {"scope": "indexed_definition_source_records_only", "llm_calls": 0,
                         "indexed_definition_records": total, "records_with_accepted_template": eligible,
                         "records_without_accepted_template": missing, "max_records": max_records,
                         "records_scanned": counts["records_scanned"], "records_not_scanned_due_to_limit": unscanned,
                         "memberships_emitted": len(memberships), "templates_compiled": len(templates),
                         "reference_variants_pending": len(pending), "rejected_records": len(rejections),
                         "rejection_reasons": {key: value for key, value in counts.items() if key != "records_scanned"},
                         "type_membership_complete": inspection_complete,
                         "reference_inspection_complete": inspection_complete,
                         "reference_linkage_complete": inspection_complete and not pending,
                         "reference_linkage_scope": "complete indexed definition inspection with no pending reference variants; does not certify business relations",
                         "source_index_partial": index_partial,
                         "index_rows_omitted_cap": index.coverage.get("rows_omitted_cap", 0),
                         "partial": not inspection_complete or bool(pending)}}
