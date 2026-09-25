"""Bind bounded fact observations to accepted types without row-wise model calls.

This stage is deliberately narrower than entity resolution: an accepted result
is one observed tuple in one imported snapshot, not a durable business key or
an inferred Cartesian combination of dimensions.  Lexical matching recalls
type candidates only; a structured field-level decision and source checks are
required before any tuple becomes a typed observation.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from decimal import Decimal, InvalidOperation
from itertools import combinations
from typing import Literal

from .column_roles import classify_columns
from .models import BuildPlan, DerivedType, Strict
from .storage import digest, qi
from .type_equivalence import _formula_key


class FactFieldBindingDecision(Strict):
    status: Literal["bind", "unresolved"]
    type_id: str | None = None
    # Full, unabridged column comment.  A fragment could hide a conflicting
    # qualifier or unit, so the compiler requires the whole original comment.
    source_column_quote: str = ""
    type_definition_quote: str = ""
    source_definition_evidence_id: str = ""
    type_source_quote: str = ""
    reason: str = ""


_UNIT_MARKER = re.compile(
    r"(?:单位\s*[：:]\s*([^，。；;、\s]+)|[（(]\s*([^（）()]+?)\s*[）)])"
)
_WORD = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]{2,}", re.I)
_HAN = re.compile(r"[\u3400-\u9fff]")


def _norm(value):
    return "".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _unit_from_comment(comment):
    """Only an explicit parenthesized or `单位:` annotation counts as a unit."""
    units = {_norm(match.group(1) or match.group(2))
             for match in _UNIT_MARKER.finditer(str(comment or ""))}
    return units


def _root_of(item: DerivedType, by_id):
    parent = item.parent
    seen = {item.id}
    while parent in by_id:
        if parent in seen:
            return None
        seen.add(parent)
        parent = by_id[parent].parent
    return parent


def _source_definition_evidence(item, data, *, max_chars):
    """Return every complete source meaning, or reject the entire type."""
    result = []
    cited = set(item.evidence_ids)
    for source in item.source_properties:
        if source.role not in ("description", "formula"):
            continue
        if not source.evidence_ids:
            return [], "source_definition_evidence_incomplete"
        for evidence_id in source.evidence_ids:
            evidence = data.evidence.get(evidence_id)
            raw = (evidence or {}).get("raw_fragment")
            ref = (evidence or {}).get("source_ref") or {}
            if (evidence_id not in cited or not evidence
                    or evidence.get("origin") != "observed_record"
                    or evidence.get("raw_fragment_truncated")
                    or not isinstance(raw, str) or not raw.strip()
                    or ref.get("table") != source.source_table
                    or ref.get("column") != source.source_column
                    or ref.get("snapshot_id") != data.snapshot_id
                    or not ref.get("record_id")):
                return [], "source_definition_evidence_incomplete"
            if len(raw) > max_chars:
                return [], "source_definition_evidence_over_budget"
            result.append({"evidence_id": evidence_id, "role": source.role,
                           "value": raw, "source_table": source.source_table,
                           "source_column": source.source_column})
    if not any(item["role"] == "description" for item in result):
        return [], "source_definition_description_missing"
    if len(result) > 8:
        return [], "source_definition_evidence_over_budget"
    for role in ("description", "formula"):
        normalize = _formula_key if role == "formula" else _norm
        if len({normalize(item["value"]) for item in result
                if item["role"] == role}) > 1:
            return [], "multiple_source_meanings"
    return sorted(result, key=lambda item: item["evidence_id"]), None


def _terms(value):
    words = set(_WORD.findall(_norm(value).replace("_", " ")))
    grams = set()
    for word in words:
        if _HAN.search(word):
            grams.update(word[i:i + 2] for i in range(len(word) - 1))
    return words | grams


def _recall_types(data, core, table, column, *, max_candidates, max_definition_chars):
    by_id = {item.id: item for item in core.object_types}
    comment = str(column.get("column_comment") or "").strip()
    source = f"{column['column_name']} {comment}"
    source_norm, source_terms = _norm(source), _terms(source)
    found = []
    excluded = Counter()
    for item in core.object_types:
        if (item.category != "business_type" or item.derivation_kind == "shared_supertype"
                or _root_of(item, by_id) not in ("Metric", "Measure")
                or not item.label or not item.definition):
            continue
        label = _norm(item.label)
        if not label:
            continue
        overlap = source_terms & _terms(item.label)
        score = (100 if label in source_norm else 0) + 2 * len(overlap)
        if score == 0:
            continue
        evidence, invalid_reason = _source_definition_evidence(
            item, data, max_chars=max_definition_chars)
        if invalid_reason:
            # An exact-label peer with hidden or conflicting source meanings
            # could change the identity decision. Do not silently ignore it.
            if label in source_norm:
                excluded[invalid_reason] += 1
            continue
        found.append((score, item, evidence))
    found.sort(key=lambda entry: (-entry[0], entry[1].id))
    return found[:max_candidates], max(0, len(found) - max_candidates), dict(excluded)


def _scope_consistent(item, table, column_comment, coordinates):
    context = _norm((table.get("table_comment") or "") + " " + column_comment)
    for key, value in item.applicability_scope.items():
        if key in coordinates:
            if _norm(coordinates[key]) != _norm(value):
                return False
        elif _norm(value) not in context:
            return False
    return True


def _unit_consistent(item, column_comment):
    observed = _unit_from_comment(column_comment)
    expected = _norm(item.unit)
    if expected:
        return observed == {expected}
    return not observed


def _business_name_in_comment(item, comment, data):
    surface = _norm(comment)
    if not surface:
        return False
    names = [item.label]
    for source in item.source_properties:
        if source.role not in ("name", "alias"):
            continue
        for evidence_id in source.evidence_ids:
            evidence = data.evidence.get(evidence_id)
            if evidence and not evidence.get("raw_fragment_truncated"):
                names.append(evidence.get("raw_fragment") or "")
    return any(len(_norm(name)) >= 2 and _norm(name) in surface for name in names)


def _validate_binding(decision, choices, data, table, column_comment,
                      canonical_type_map, verified_equivalence_pairs):
    if decision.status != "bind":
        return None, "llm_unresolved"
    matches = [(item, evidence) for _, item, evidence in choices
               if item.id == decision.type_id]
    if len(matches) != 1:
        return None, "unknown_or_unrecalled_type"
    item, evidence = matches[0]
    if not column_comment or decision.source_column_quote != column_comment:
        return None, "column_comment_quote_missing_or_not_full"
    if not _business_name_in_comment(item, column_comment, data):
        return None, "column_comment_lacks_business_type_name"
    if (not decision.type_definition_quote.strip()
            or decision.type_definition_quote != item.definition):
        return None, "type_definition_quote_invalid"
    original = next((value for value in evidence
                     if value["evidence_id"] == decision.source_definition_evidence_id), None)
    if (original is None or not decision.type_source_quote.strip()
            or decision.type_source_quote != original["value"]):
        return None, "full_source_definition_quote_invalid"
    if not _unit_consistent(item, column_comment):
        return None, "unit_missing_or_conflicting"
    # Equal labels, units and applicability among two accepted exact types are
    # still ambiguous when their definitions differ; the field cannot resolve
    # them with only its name/comment.
    same_label = [other for _, other, _ in choices if other.id != item.id
                  and _norm(other.label) == _norm(item.label)
                  and _norm(other.unit) == _norm(item.unit)
                  and other.applicability_scope == item.applicability_scope]
    canonical_id = canonical_type_map.get(item.id, item.id)
    peers = [item, *same_label]
    if (any(canonical_type_map.get(other.id, other.id) != canonical_id
            for other in peers)
            or any(frozenset((left.id, right.id)) not in verified_equivalence_pairs
                   for left, right in combinations(peers, 2))):
        return None, "multiple_compatible_types_share_label"
    return item, None


def _source_rows(data, table, candidate, coordinate_columns, *, limit):
    bindings = [(field, candidate["coordinate_values"][field])
                for field in coordinate_columns]
    bindings.append((candidate["value_column"], candidate["observed_value"]))
    sql = (f"SELECT * FROM {qi(table['sql_name'])} WHERE "
           + " AND ".join(f"{qi(field)} = ?" for field, _ in bindings)
           + " ORDER BY __r2_row LIMIT ?")
    cursor = data.db.execute(sql, [*[value for _, value in bindings], limit + 1])
    names = [part[0] for part in cursor.description]
    return [dict(zip(names, values)) for values in cursor.fetchall()]


def _instantiate(data, table, item, candidate, coordinate_columns, decision, *,
                 max_source_rows, selected_type_id=None,
                 equivalence_assertion_ids=()):
    if candidate.get("candidate_status") != "candidate_only":
        return None, "unexpected_candidate_state"
    if candidate.get("business_type_binding") != "unresolved":
        return None, "candidate_already_bound"
    if candidate.get("ambiguity") or candidate.get("distinct_values_at_selected_coordinates") != 1:
        return None, "ambiguous_or_incomplete_coordinates"
    if candidate.get("table") != table["name"]:
        return None, "candidate_table_mismatch"
    if set(candidate.get("coordinate_values") or {}) != set(coordinate_columns):
        return None, "candidate_coordinate_columns_mismatch"
    if candidate["value_column"] not in table["column_names"]:
        return None, "candidate_value_column_unknown"
    try:
        number = Decimal(str(candidate["observed_value"]))
        if not number.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError):
        return None, "non_numeric_observed_value"
    count = candidate.get("source_row_count")
    if type(count) is not int or count < 1 or count > max_source_rows:
        return None, "source_row_count_exceeds_limit_or_invalid"
    try:
        rows = _source_rows(data, table, candidate, coordinate_columns, limit=max_source_rows)
    except (KeyError, TypeError):
        return None, "invalid_observation_candidate"
    actual_numbers = [row["__r2_row"] for row in rows]
    if (len(rows) != count or min(actual_numbers) != candidate.get("source_row_min")
            or max(actual_numbers) != candidate.get("source_row_max")):
        return None, "source_rows_changed_or_incomplete"
    # Column-role heuristics can miss a business coordinate. If source rows
    # collapsed into this tuple disagree on any remaining nontechnical field,
    # the observed grain is unsafe and must remain unresolved.
    excluded_roles = {"empty", "audit_time", "audit_metadata",
                      "technical_identifier", "sensitive"}
    residual_columns = [entry["column"] for entry in classify_columns(table)
                        if entry["role"] not in excluded_roles
                        and entry["column"] not in coordinate_columns
                        and entry["column"] != candidate["value_column"]]
    if any(len({str(row[column]) for row in rows}) > 1
           for column in residual_columns):
        return None, "unmodeled_source_fields_vary_within_observation"
    record_ids = [data.record_id(table["name"], row) for row in rows]
    evidence_ids = []
    for record_id, row in zip(record_ids, rows):
        evidence_id = "record:" + digest([data.snapshot_id, record_id,
                                          candidate["value_column"]])[:24]
        data.evidence[evidence_id] = {
            "id": evidence_id, "origin": "observed_record",
            "raw_fragment": str(row[candidate["value_column"]]),
            "raw_fragment_truncated": False,
            "source_ref": {"table": table["name"], "record_id": record_id,
                           "row": row["__r2_row"], "column": candidate["value_column"],
                           "snapshot_id": data.snapshot_id},
        }
        evidence_ids.append(evidence_id)
    coordinates = candidate["coordinate_values"]
    instance_id = "fact_observation:" + digest([
        data.snapshot_id, table["name"], item.id, candidate["value_column"],
        coordinates, candidate["observed_value"]])[:24]
    label = (item.label or item.id) + " | " + ", ".join(
        f"{field}={coordinates[field]}" for field in coordinate_columns)
    return {
        "id": instance_id, "type": item.id, "label": label,
        "observation_coordinates": coordinates,
        "observed_value": candidate["observed_value"],
        "observation_candidate_id": candidate.get("id"),
        "fact_template_id": candidate.get("template_id"),
        "unit": item.unit, "value_column": candidate["value_column"],
        "identity_scope": "source_snapshot_observed_tuple_only",
        "source_ref": {"table": table["name"], "snapshot_id": data.snapshot_id,
                       "record_ids": record_ids, "row_numbers": actual_numbers,
                       "source_row_count": count,
                       "coordinate_columns": coordinate_columns,
                       "value_column": candidate["value_column"]},
        "evidence_ids": sorted(set(evidence_ids) | {
            f"schema:{table['name']}:{candidate['value_column']}",
            decision.source_definition_evidence_id,
        }),
        "binding_decision": {
            "selected_type_id": selected_type_id or item.id,
            "canonical_type_id": item.id,
            "equivalence_assertion_ids": list(equivalence_assertion_ids),
            "source_column_quote": decision.source_column_quote,
            "type_definition_quote": decision.type_definition_quote,
            "type_source_quote": decision.type_source_quote,
            "source_definition_evidence_id": decision.source_definition_evidence_id,
        },
    }, None


async def bind_fact_observations(
    data, fact_observations, core: BuildPlan, llm, *,
    max_type_candidates_per_field=8, max_source_rows_per_instance=1000,
    max_binding_calls=50, max_definition_chars=2048,
    canonical_type_map=None, equivalence_assertions=(),
):
    """Return verified typed observations plus explicit unresolved coverage.

    At most one structured LLM request is made for each selected value field.
    Every emitted tuple is checked against the original imported rows before
    constructing a source-snapshot-local instance. No per-row model call occurs.
    """
    for name, value in (("max_type_candidates_per_field", max_type_candidates_per_field),
                        ("max_source_rows_per_instance", max_source_rows_per_instance),
                        ("max_definition_chars", max_definition_chars)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if type(max_binding_calls) is not int or max_binding_calls < 0:
        raise ValueError("max_binding_calls must be a nonnegative integer")
    if fact_observations.get("scope") != "imported_csv_snapshot_only":
        raise ValueError("Unsupported fact observation scope")
    if canonical_type_map is None:
        canonical_type_map = {}
    if not isinstance(canonical_type_map, dict):
        raise ValueError("canonical_type_map must be a mapping")
    by_type_id = {item.id: item for item in core.object_types}
    if any(source not in by_type_id or target not in by_type_id
           for source, target in canonical_type_map.items()):
        raise ValueError("canonical_type_map references unknown ontology types")
    equivalence_by_canonical = {}
    verified_equivalence_pairs = set()
    for assertion in equivalence_assertions:
        canonical = assertion.get("canonical_type_id")
        source_id, target_id = (assertion.get("source_type_id"),
                                assertion.get("target_type_id"))
        if (canonical in by_type_id and source_id in by_type_id
                and target_id in by_type_id
                and canonical_type_map.get(source_id) == canonical
                and canonical_type_map.get(target_id) == canonical):
            equivalence_by_canonical.setdefault(canonical, []).append(assertion["id"])
            verified_equivalence_pairs.add(frozenset((source_id, target_id)))

    reports = []
    instances = []
    skipped = Counter()
    attempts = accepted_fields = candidate_tuples = 0
    for report in fact_observations.get("tables", []):
        if report.get("row_purpose") != "business_fact":
            continue
        table_name = report.get("table")
        table = data.tables.get(table_name)
        if table is None or report.get("source_snapshot_id") != data.snapshot_id:
            raise ValueError("Fact observations do not match the imported snapshot")
        selected = report.get("selected_columns") or {}
        coordinate_columns = list(report.get("coordinate_columns") or [])
        blocked_coordinates = bool(selected.get("omitted_dimensions")
                                   or selected.get("omitted_business_times"))
        columns = {entry["column_name"]: entry for entry in table["columns"]}
        by_field = {}
        for candidate in report.get("candidates", []):
            candidate_tuples += 1
            by_field.setdefault(candidate.get("value_column"), []).append(candidate)
        for field in report.get("value_fields", []):
            name = field["column"]
            candidates = by_field.get(name, [])
            status = {"table": table_name, "value_column": name,
                      "candidate_tuples": len(candidates), "status": "candidate_only"}
            reason = None
            if blocked_coordinates:
                reason = "coordinate_columns_omitted_by_limit"
            elif not coordinate_columns or name not in columns:
                reason = "missing_coordinate_or_value_column"
            elif not candidates:
                reason = "no_emitted_tuples"
            elif not str(columns[name].get("column_comment") or "").strip():
                reason = "missing_value_column_comment"
            elif attempts >= max_binding_calls:
                reason = "binding_call_budget_exhausted"
            if reason:
                status["reason"] = reason
                skipped[reason] += len(candidates)
                reports.append(status)
                continue
            comment = str(columns[name]["column_comment"]).strip()
            choices, omitted_types, excluded_types = _recall_types(
                data, core, table, columns[name],
                max_candidates=max_type_candidates_per_field,
                max_definition_chars=max_definition_chars)
            if excluded_types:
                status["reason"] = "type_candidate_has_unusable_source_evidence"
                status["excluded_type_reasons"] = excluded_types
                skipped[status["reason"]] += len(candidates)
                reports.append(status)
                continue
            if not choices:
                status["reason"] = "no_type_with_lexical_and_full_definition_evidence"
                skipped[status["reason"]] += len(candidates)
                reports.append(status)
                continue
            if omitted_types:
                # Hidden lexical peers may have the same name but a different
                # definition. A bounded prompt cannot certify uniqueness when
                # its candidate set was truncated.
                status["reason"] = "type_candidate_recall_limit_hit"
                status["type_candidates_omitted_by_limit"] = omitted_types
                skipped[status["reason"]] += len(candidates)
                reports.append(status)
                continue
            payload = {
                "contract": (
                    "One decision for this value field, not per row. Only choose an exact "
                    "accepted Metric/Measure business type whose full definition fits the "
                    "column's business meaning. A lexical match is candidate recall only. "
                    "If the column comment lacks the type's complete business name, unit or "
                    "scope is uncertain, or several types remain plausible, return unresolved. "
                    "For bind, copy the ENTIRE column comment, the ENTIRE type definition "
                    "and the ENTIRE source definition from one listed original fragment "
                    "with its evidence ID. Do not infer type identity from observed values."),
                "table": table_name, "table_comment": table.get("table_comment") or "",
                "value_column": name, "column_comment": comment,
                "coordinate_columns": coordinate_columns,
                "type_candidates_omitted_by_limit": omitted_types,
                "type_candidates": [
                    {"id": item.id, "label": item.label, "root_type": _root_of(
                        item, {entry.id: entry for entry in core.object_types}),
                     "canonical_type_id": canonical_type_map.get(item.id, item.id),
                     "definition": item.definition,
                     "unit": item.unit, "applicability_scope": item.applicability_scope,
                     "full_source_definitions": evidence}
                    for _, item, evidence in choices],
            }
            attempts += 1
            try:
                decision = await llm.ask("fact_type_binding", payload,
                                         FactFieldBindingDecision)
                item, reason = _validate_binding(
                    decision, choices, data, table, comment, canonical_type_map,
                    verified_equivalence_pairs)
            except Exception as exc:
                item, reason = None, "binding_call_failed:" + type(exc).__name__
            if item is None:
                status["reason"] = reason
                skipped[reason] += len(candidates)
                reports.append(status)
                continue
            accepted_fields += 1
            canonical_id = canonical_type_map.get(item.id, item.id)
            canonical_item = by_type_id[canonical_id]
            if (_norm(canonical_item.label) != _norm(item.label)
                    or _norm(canonical_item.unit) != _norm(item.unit)
                    or canonical_item.applicability_scope != item.applicability_scope):
                status["reason"] = "canonical_type_business_signature_conflict"
                skipped[status["reason"]] += len(candidates)
                accepted_fields -= 1
                reports.append(status)
                continue
            equivalence_ids = sorted(set(equivalence_by_canonical.get(canonical_id, [])))
            status.update(status="accepted_field_binding", type_id=canonical_id,
                          selected_type_id=item.id,
                          equivalence_assertion_ids=equivalence_ids,
                          definition_evidence_id=decision.source_definition_evidence_id,
                          type_definition_quote=decision.type_definition_quote,
                          type_source_quote=decision.type_source_quote,
                          column_comment_quote=decision.source_column_quote)
            for candidate in candidates:
                if not _scope_consistent(item, table, comment,
                                         candidate.get("coordinate_values") or {}):
                    rejection = "applicability_scope_unverified_or_conflicting"
                    instance = None
                else:
                    instance, rejection = _instantiate(
                        data, table, canonical_item, candidate, coordinate_columns,
                        decision, max_source_rows=max_source_rows_per_instance,
                        selected_type_id=item.id,
                        equivalence_assertion_ids=equivalence_ids)
                if instance is None:
                    skipped[rejection] += 1
                else:
                    instances.append(instance)
            status["instances_created"] = sum(
                item["source_ref"]["table"] == table_name
                and item["value_column"] == name for item in instances)
            status["candidate_tuples_not_instantiated"] = (
                len(candidates) - status["instances_created"])
            reports.append(status)

    return {
        "method": "field_level_structured_binding_then_exact_source_row_verification",
        "identity_scope": "source_snapshot_observed_tuple_only",
        "field_bindings": reports,
        "instances": instances,
        "coverage": {
            "binding_attempts": attempts,
            "accepted_fields": accepted_fields,
            "candidate_tuples": candidate_tuples,
            "instances_created": len(instances),
            "candidate_tuples_not_instantiated": candidate_tuples - len(instances),
            "skipped_reasons": dict(sorted(skipped.items())),
            "per_row_llm_calls": 0,
            "binding_call_limit": max_binding_calls,
            "source_snapshot_id": data.snapshot_id,
        },
    }
