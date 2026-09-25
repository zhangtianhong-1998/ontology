"""Resolve explicit two-ended configuration references to accepted business types.

A configuration row is evidence for a possible relation, never a business
object or a relation endpoint.  Raw code equality and unique definition-row
lookup establish endpoints; they do not establish the predicate's meaning.
"""

from __future__ import annotations

from collections import Counter
from collections import defaultdict
from itertools import combinations
import re

from pydantic import Field

from .column_roles import is_sensitive_column
from .models import BuildPlan, Strict
from .row_semantics import classify_row_purpose
from .storage import digest, qi


class ConfigurationRelationSpec(Strict):
    """One explicit pair of source/target code references in a config table."""

    spec_id: str = Field(min_length=1)
    configuration_table: str
    source_code_column: str
    target_code_column: str
    source_definition_table: str
    source_definition_code_column: str
    target_definition_table: str
    target_definition_code_column: str
    # A literal value can be shown to a semantic judge, but is not itself a
    # validated predicate label such as depends_on or contains.
    relation_text_column: str | None = None


_RELATION_TEXT = re.compile(
    r"(?:^|_)(?:relation|relationship|dependency|depends|formula|description|rule)"
    r"(?:_|$)|关系|依赖|计算公式|关系说明|引用说明", re.I)


def _accepted_definition_roots(data, plan, concepts, alignments):
    accepted_by_record, _ = _type_alignments(plan, concepts, alignments)
    types = {item.id: item for item in plan.object_types}
    roots = defaultdict(set)
    for record_id, match in accepted_by_record.items():
        table_name = record_id.rpartition(":")[0]
        if table_name not in data.tables:
            continue
        item = types[match["type_id"]]
        parent = item.parent
        seen = set()
        while parent in types and parent not in seen:
            seen.add(parent)
            parent = types[parent].parent
        roots[table_name].add(parent)
    return roots


def _relation_text_candidate(data, table_name, reference_columns):
    table = data.tables[table_name]
    for column in table["columns"]:
        name = column["column_name"]
        if name in reference_columns:
            continue
        if not _RELATION_TEXT.search(name + " " + str(column.get("column_comment") or "")):
            continue
        try:
            _check_column(data, table_name, name)
        except ValueError:
            continue
        return name
    return None


def infer_configuration_specs(
    data, plan: BuildPlan, concepts, alignments, checked_rules, *, max_specs=100,
):
    """Pair two checked config→definition code references, never assert a relation.

    Each input rule must have exact raw-code matching over the full snapshot,
    no missing/ambiguous referenced rows, and no selector/scope condition that
    this simple dual-code contract cannot represent. Distinct accepted target
    business roots block shared-key alignment from becoming a relation lead.
    The returned `spec` mappings can feed `discover_configuration_relations`;
    their evidence remains candidate-level until both endpoints and the
    predicate have been judged separately.
    """
    if type(max_specs) is not int or max_specs < 1:
        raise ValueError("max_specs must be a positive integer")
    plan = BuildPlan.model_validate(plan)
    target_roots = _accepted_definition_roots(data, plan, concepts, alignments)
    by_table = defaultdict(list)
    skipped = Counter()
    seen = 0
    for rule in checked_rules:
        seen += 1
        if rule.get("status") != "checked_technical":
            skipped["not_checked_technical"] += 1
            continue
        if (rule.get("transform", {}).get("operator") != "identity"
                or rule.get("verification", {}).get("scan_scope") != "full_input"):
            skipped["not_full_input_raw_identity"] += 1
            continue
        checks = rule.get("verification", {}).get("checks") or {}
        eligible, unique = checks.get("eligible_references"), checks.get("unique_matches")
        if (type(eligible) is not int or eligible <= 0 or type(unique) is not int
                or unique != eligible or any(checks.get(key, 0) for key in
                                          ("ambiguous_matches", "missing_in_input", "missing_scope"))):
            skipped["incomplete_or_ambiguous_full_input_check"] += 1
            continue
        if rule.get("selector") or rule.get("scope_bindings"):
            skipped["condition_not_supported_by_dual_code_spec"] += 1
            continue
        source, target = rule.get("source") or {}, rule.get("target") or {}
        source_table, target_table = source.get("table"), target.get("table")
        source_field, target_field = source.get("field"), target.get("field")
        if not all((source_table, target_table, source_field, target_field)):
            skipped["missing_rule_endpoint"] += 1
            continue
        try:
            _check_column(data, source_table, source_field)
            _check_column(data, target_table, target_field)
        except ValueError:
            skipped["invisible_or_unknown_column"] += 1
            continue
        if classify_row_purpose(data.tables[source_table])["purpose"] != "configuration_data":
            skipped["source_not_configuration_data"] += 1
            continue
        roots = target_roots.get(target_table, set())
        if len(roots) != 1:
            skipped["target_lacks_single_accepted_business_root"] += 1
            continue
        by_table[source_table].append((rule, next(iter(roots))))

    output, total_pairs = [], 0
    for table_name in sorted(by_table):
        rules = sorted(by_table[table_name], key=lambda pair: str(pair[0].get("rule_id")))
        for (left, left_root), (right, right_root) in combinations(rules, 2):
            total_pairs += 1
            left_source, right_source = left["source"], right["source"]
            if left_source["field"] == right_source["field"]:
                skipped["same_configuration_column"] += 1
                continue
            if left_root == right_root:
                # Two code paths into records of one business root are often
                # alternate descriptions of the same type, not a predicate.
                skipped["same_business_root_alignment_lead"] += 1
                continue
            if left["target"]["table"] == right["target"]["table"]:
                skipped["same_definition_table_alignment_lead"] += 1
                continue
            if len(output) >= max_specs:
                skipped["spec_limit"] += 1
                continue
            text_column = _relation_text_candidate(
                data, table_name, {left_source["field"], right_source["field"]})
            spec = ConfigurationRelationSpec(
                spec_id="config_spec:" + digest([
                    data.snapshot_id, table_name, left["rule_id"], right["rule_id"]])[:24],
                configuration_table=table_name,
                source_code_column=left_source["field"],
                target_code_column=right_source["field"],
                source_definition_table=left["target"]["table"],
                source_definition_code_column=left["target"]["field"],
                target_definition_table=right["target"]["table"],
                target_definition_code_column=right["target"]["field"],
                relation_text_column=text_column,
            )
            output.append({
                "spec": spec.model_dump(), "status": "candidate_only",
                "direction_status": "unjudged",
                "rule_ids": [left["rule_id"], right["rule_id"]],
                "target_root_hints": [left_root, right_root],
                "relation_text_basis": (
                    "visible_column_name_or_comment_candidate" if text_column
                    else "none_predicate_remains_unjudged"),
                "evidence_ids": sorted({
                    f"schema:{table_name}:{left_source['field']}",
                    f"schema:{table_name}:{right_source['field']}",
                    f"schema:{left['target']['table']}:{left['target']['field']}",
                    f"schema:{right['target']['table']}:{right['target']['field']}",
                }),
                "semantic_status": "two_verified_reference_rules_are_not_a_business_predicate",
            })
    return {
        "method": "pair_full_input_checked_config_references_to_distinct_accepted_definition_roots",
        "snapshot_id": data.snapshot_id,
        "spec_candidates": output,
        "coverage": {"rules_inspected": seen, "eligible_rule_groups": len(by_table),
                     "rule_pairs_considered": total_pairs, "specs_emitted": len(output),
                     "specs_omitted_limit": skipped["spec_limit"],
                     "skipped_reasons": dict(skipped),
                     "partial": skipped["spec_limit"] > 0},
    }


def _check_column(data, table_name, column_name):
    table = data.tables.get(table_name)
    if table is None or column_name not in table["column_names"]:
        raise ValueError(f"Unknown configuration reference column: {table_name}.{column_name}")
    if column_name in table.get("semantic_excluded_columns", ()):
        raise ValueError(f"Excluded configuration reference column: {table_name}.{column_name}")
    column = next(item for item in table["columns"] if item["column_name"] == column_name)
    if is_sensitive_column(column):
        raise ValueError(f"Sensitive configuration reference column: {table_name}.{column_name}")


def _type_alignments(plan, concepts, alignments):
    accepted = {item.id for item in plan.object_types if item.category == "business_type"}
    by_concept = {item["id"]: item for item in concepts}
    by_record = {}
    conflicting = set()
    for alignment in alignments:
        if alignment.get("mapping_kind") != "exact":
            continue
        concept = by_concept.get(alignment.get("concept_id"))
        if not concept or concept.get("ontology_level") != "type":
            continue
        type_id = concept.get("ontology_type_id")
        if type_id not in accepted:
            continue
        record_id = alignment.get("source_record_id")
        if not record_id:
            continue
        old = by_record.get(record_id)
        if old and old["type_id"] != type_id:
            conflicting.add(record_id)
        else:
            by_record[record_id] = {
                "type_id": type_id, "concept_id": concept["id"],
                "alignment_id": alignment.get("id"),
                "evidence_ids": alignment.get("evidence_ids", []),
            }
    for record_id in conflicting:
        by_record.pop(record_id, None)
    return by_record, conflicting


def _grouped_pairs(data, spec, limit):
    table = data.tables[spec.configuration_table]
    cols = [spec.source_code_column, spec.target_code_column]
    if spec.relation_text_column:
        cols.append(spec.relation_text_column)
    names = ["source_code", "target_code", "relation_literal"][:len(cols)]
    select = ", ".join(f"{qi(column)} AS {qi(alias)}" for column, alias in zip(cols, names))
    group = ", ".join(qi(column) for column in cols)
    order = ", ".join(f"{qi(alias)} NULLS LAST" for alias in names)
    query = f"""
        WITH grouped AS (
            SELECT {select}, count(*) AS configuration_rows,
                   min(__r2_row) AS witness_row_number
            FROM {qi(table['sql_name'])}
            GROUP BY {group}
        )
        SELECT grouped.*, count(*) OVER () AS all_distinct_code_pairs,
               sum(configuration_rows) OVER () AS all_configuration_rows
        FROM grouped
        ORDER BY configuration_rows DESC, {order}
        LIMIT ?
    """
    cursor = data.db.execute(query, [limit])
    fields = [item[0] for item in cursor.description]
    return [dict(zip(fields, row)) for row in cursor.fetchall()]


def _witness(data, spec, row_number):
    table = data.tables[spec.configuration_table]
    cursor = data.db.execute(
        f"SELECT * FROM {qi(table['sql_name'])} WHERE __r2_row=?", [row_number])
    names = [item[0] for item in cursor.description]
    row = cursor.fetchone()
    if row is None:
        raise ValueError("Grouped configuration witness is missing from the snapshot")
    return dict(zip(names, row))


def _record_evidence(data, spec, witness, columns):
    table_name = spec.configuration_table
    record_id = data.record_id(table_name, witness)
    evidence_ids = []
    for column in columns:
        evidence_id = "record:" + digest([data.snapshot_id, record_id, column])[:24]
        data.evidence[evidence_id] = {
            "id": evidence_id, "origin": "observed_record",
            "raw_fragment": str(witness[column]) if witness[column] is not None else "",
            "raw_fragment_truncated": False,
            "source_ref": {"table": table_name, "record_id": record_id,
                           "row": witness["__r2_row"], "column": column,
                           "snapshot_id": data.snapshot_id},
        }
        evidence_ids.extend((f"schema:{table_name}:{column}", evidence_id))
    return record_id, list(dict.fromkeys(evidence_ids))


def discover_configuration_relations(
    data, plan: BuildPlan, concepts, alignments, specs, *, max_pairs_per_spec=1000,
):
    """Return bounded, evidence-backed relation *candidates* from explicit codes.

    The SQL scans every configuration row and groups only observed code tuples.
    Each definition code must match exactly one row in the full imported table,
    and that row must have an accepted exact type alignment.  Missing, multiple,
    or unaligned endpoints remain unresolved.  Even two verified endpoints do
    not authorize a business predicate without a separate semantic decision.
    """
    if type(max_pairs_per_spec) is not int or max_pairs_per_spec < 1:
        raise ValueError("max_pairs_per_spec must be a positive integer")
    plan = BuildPlan.model_validate(plan)
    parsed = [ConfigurationRelationSpec.model_validate(item) for item in specs]
    if len({item.spec_id for item in parsed}) != len(parsed):
        raise ValueError("Duplicate configuration relation spec ID")
    align_by_record, conflicting_alignments = _type_alignments(plan, concepts, alignments)
    candidates, reports = [], []
    lookup_cache = {}

    def endpoint(table_name, code_column, code):
        if code is None or str(code).strip() == "":
            return {"status": "blank_code"}
        cache_key = (table_name, code_column, code)
        if cache_key in lookup_cache:
            return lookup_cache[cache_key]
        matches = data.lookup(table_name, ((code_column, code),))
        if not matches:
            result = {"status": "missing_definition"}
        elif len(matches) > 1:
            result = {"status": "ambiguous_definition", "matching_rows_at_least": 2}
        else:
            record_id = data.record_id(table_name, matches[0])
            alignment = align_by_record.get(record_id)
            result = ({"status": "accepted_type", "record_id": record_id,
                       **alignment} if alignment else
                      {"status": "definition_without_exact_accepted_type",
                       "record_id": record_id,
                       "conflicting_exact_alignments": record_id in conflicting_alignments})
        lookup_cache[cache_key] = result
        return result

    for spec in parsed:
        for table, column in (
            (spec.configuration_table, spec.source_code_column),
            (spec.configuration_table, spec.target_code_column),
            (spec.source_definition_table, spec.source_definition_code_column),
            (spec.target_definition_table, spec.target_definition_code_column),
        ):
            _check_column(data, table, column)
        if spec.relation_text_column:
            _check_column(data, spec.configuration_table, spec.relation_text_column)
        if spec.source_code_column == spec.target_code_column:
            raise ValueError("Configuration source and target code columns must differ")
        rows = _grouped_pairs(data, spec, max_pairs_per_spec)
        total_groups = rows[0]["all_distinct_code_pairs"] if rows else 0
        total_rows = rows[0]["all_configuration_rows"] if rows else 0
        statuses = Counter()
        for item in rows:
            witness = _witness(data, spec, item["witness_row_number"])
            columns = [spec.source_code_column, spec.target_code_column]
            if spec.relation_text_column:
                columns.append(spec.relation_text_column)
            config_record_id, config_evidence = _record_evidence(data, spec, witness, columns)
            source = endpoint(spec.source_definition_table, spec.source_definition_code_column,
                              item["source_code"])
            target = endpoint(spec.target_definition_table, spec.target_definition_code_column,
                              item["target_code"])
            verified = source["status"] == target["status"] == "accepted_type"
            status = "endpoint_verified_candidate" if verified else "unresolved"
            statuses[status] += 1
            candidate = {
                "id": "configuration_relation_candidate:" + digest([
                    data.snapshot_id, spec.spec_id, item["source_code"], item["target_code"],
                    item.get("relation_literal")])[:24],
                "spec_id": spec.spec_id, "status": status,
                "snapshot_id": data.snapshot_id,
                "predicate_status": "unjudged", "direction_status": "unjudged",
                "predicate_literal": item.get("relation_literal"),
                "configuration_table": spec.configuration_table,
                "source_code_column": spec.source_code_column,
                "target_code_column": spec.target_code_column,
                "relation_text_column": spec.relation_text_column,
                "configuration_record_id": config_record_id,
                "configuration_witness_row_number": item["witness_row_number"],
                "configuration_rows_with_same_code_pair": item["configuration_rows"],
                "source_code": item["source_code"], "target_code": item["target_code"],
                "source_definition": {"table": spec.source_definition_table,
                                      "code_column": spec.source_definition_code_column, **source},
                "target_definition": {"table": spec.target_definition_table,
                                      "code_column": spec.target_definition_code_column, **target},
                "source_type_id": source.get("type_id") if verified else None,
                "target_type_id": target.get("type_id") if verified else None,
                "evidence_ids": sorted(set(config_evidence + source.get("evidence_ids", [])
                                           + target.get("evidence_ids", []))),
                "evidence_scope": "one_configuration_witness_with_full_snapshot_unique_code_lookup",
                "semantic_status": "code_references_do_not_prove_relation_meaning",
            }
            candidates.append(candidate)
        reports.append({"spec_id": spec.spec_id, "configuration_table": spec.configuration_table,
                        "scan_scope": "full_input", "input_rows": total_rows,
                        "distinct_code_pairs": total_groups, "pairs_emitted": len(rows),
                        "pairs_omitted_limit": total_groups - len(rows),
                        "configuration_rows_in_emitted_pairs": sum(item["configuration_rows"] for item in rows),
                        "statuses_in_emitted_pairs": dict(statuses),
                        "partial": total_groups > len(rows)})
    return {"method": "explicit_dual_code_full_snapshot_unique_lookup",
            "semantic_status": "endpoint_candidates_only_predicate_unjudged",
            "snapshot_id": data.snapshot_id,
            "limits": {"max_pairs_per_spec": max_pairs_per_spec},
            "candidates": candidates,
            "coverage": {"specs": reports, "candidates_emitted": len(candidates),
                         "endpoint_verified_candidates": sum(
                             item["status"] == "endpoint_verified_candidate" for item in candidates),
                         "partial": any(item["partial"] for item in reports)}}
