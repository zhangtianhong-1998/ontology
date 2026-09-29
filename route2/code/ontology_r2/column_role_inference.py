"""Bounded, source-checked column-role *candidates* for opaque source tables.

This is a recall stage, not an ontology classifier. A model may suggest which
otherwise unreadable column contains a name or definition, but it cannot make
that suggestion evidence without an exact value from a sampled source row.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .column_roles import CONTEXT_ROLES, classify_columns, declared_context_role
from .concept_candidates import _field_roles, field_role_conflicts
from .storage import qi


_ROLES = ("name", "alias", "description", "formula", "unit", "scope",
          "provenance", "metadata", "identity",
          "calculation_operator", "operand_reference",
          "business_time", "dimension_coordinate", "numeric_business_value")
_FRAGMENT_ROLES = frozenset(("calculation_operator", "operand_reference"))
_OPERATOR_VALUES = frozenset((
    "sum", "avg", "average", "mean", "count", "count_distinct", "distinct_count",
    "min", "max", "ratio", "rank", "add", "subtract", "multiply", "divide", "filter",
    "求和", "平均", "均值", "计数", "去重计数", "最小值", "最大值", "比率", "排名", "过滤",
))
_IDENTIFIER_VALUE = re.compile(r"[^\W\d]\w*(?:\.[^\W\d]\w*)*", re.UNICODE)
_EXPLICIT_FORMULA_NAME = re.compile(r"(?:^|_)(?:formula|expression|expr)(?:_|$)")
_EXPLICIT_FORMULA_COMMENT = re.compile(r"完整公式|完整表达式|计算公式|计算表达式|\b(?:formula|expression)\b", re.I)
_OPERATOR_NAME = re.compile(
    r"(?:^|_)(?:operator|operation)(?:_|$)|"
    r"(?:^|_)(?:calculation|calc|aggregation|aggregate|inference|derivation|arithmetic)"
    r"_(?:type|kind|mode|method)(?:_|$)")
_OPERATOR_COMMENT = re.compile(
    r"(?:聚合|汇总|计算|运算|推导|推断|操作)(?:符|类型|方式|方法|种类)|"
    r"\b(?:operator|(?:calculation|aggregation|aggregate|inference|arithmetic)\s+(?:type|kind|mode|method))\b", re.I)
_OPERAND_NAME = re.compile(
    r"(?:^|_)(?:source|input|operand|argument|parameter|measure|metric)"
    r"_(?:field|column|ref|reference)(?:_|$)|"
    r"(?:^|_)(?:field|column)_(?:ref|reference)(?:_|$)|(?:^|_)operand(?:_|$)")
_OPERAND_COMMENT = re.compile(
    r"字段引用|引用字段|来源字段|源字段|输入字段|操作数|字段标识|引用列|来源列|源列|输入列|"
    r"\b(?:(?:source|input|referenced)\s+(?:field|column)|(?:field|column)\s+reference|operand)\b", re.I)
_SENSITIVE_VALUE = re.compile(
    r"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b|"
    r"\b(?:\+?\d[ -]?){11,15}\b|"
    r"\b(?:sk|pk|AKIA)[_-]?[A-Za-z0-9_-]{16,}\b)", re.I,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ObservedValue(_Strict):
    row_number: int
    value: str


class ColumnRoleProposal(_Strict):
    column: str
    role: Literal["name", "alias", "description", "formula", "unit", "scope",
                  "provenance", "metadata", "identity",
                  "calculation_operator", "operand_reference",
                  "business_time", "dimension_coordinate", "numeric_business_value"]
    observations: list[ObservedValue] = Field(default_factory=list)
    rationale: str = ""


class ColumnRoleInferenceResponse(_Strict):
    proposals: list[ColumnRoleProposal] = Field(default_factory=list)
    unresolved_columns: list[str] = Field(default_factory=list)


def classify_formula_fragment(table, candidate):
    """Keep source-backed calculation fragments distinct from full formulas.

    A legacy formula proposal is reclassified only when both the declaration
    and observed values support a fragment. Identifier-shaped values alone do
    not disprove a direct mapping in an explicitly declared formula column.
    This does not resolve operand targets or construct an expression.
    """
    profile_candidate = candidate.get("status") == "profile_supported_role_candidate"
    if candidate.get("status") != "source_verified_role_candidate" and not profile_candidate:
        return None
    proposed = candidate.get("role")
    if proposed not in {"formula", *_FRAGMENT_ROLES}:
        return None
    column = next((item for item in table.get("columns", ())
                   if item.get("column_name") == candidate.get("column")), None)
    observations = candidate.get("observations") or []
    if column is None or not observations:
        return None
    values = [str(item.get("value", "")).strip() for item in observations]
    if any(not value for value in values):
        return None
    effective = proposed
    reason = "source_checked_fragment_role_candidate"
    validation_scope = "source_checked_observations"
    # An operator proposal must also survive all already available source
    # samples. A model citing only SUM cannot hide SUM(amount) in the same field.
    profile = next((item for item in table.get("profiles", ())
                    if item.get("column") == candidate["column"]), {})
    values.extend(str(value).strip() for value in profile.get("distinct_sample", ())
                  if value is not None and str(value).strip())
    if proposed in _FRAGMENT_ROLES and not all(
            formula_fragment_value(proposed, value) for value in values):
        return None
    if proposed == "formula":
        name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", column["column_name"]).casefold()
        comment = str(column.get("column_comment") or "")
        if (_EXPLICIT_FORMULA_NAME.search(name)
                or name in {"calculation", "caliber"}
                or _EXPLICIT_FORMULA_COMMENT.search(comment)):
            return None
        # Use every available profile sample as a contradiction check, not
        # just the few values cited by the model. This remains sample-scoped.
        validation_scope = "source_checked_observations_and_available_profile_samples"
        if ((_OPERATOR_NAME.search(name) or _OPERATOR_COMMENT.search(comment))
                and all(value.casefold() in _OPERATOR_VALUES for value in values)):
            effective = "calculation_operator"
            reason = "declared_operator_field_with_observed_operator_values"
        elif ((_OPERAND_NAME.search(name) or _OPERAND_COMMENT.search(comment))
                and all(_IDENTIFIER_VALUE.fullmatch(value) for value in values)):
            effective = "operand_reference"
            reason = "declared_operand_field_with_observed_identifier_values"
        else:
            return None
    if profile_candidate:
        validation_scope = "source_declaration_and_available_profile_samples"
    return {**candidate, "column": candidate["column"], "proposed_role": proposed,
            "effective_role": effective, "formula_status": "fragment", "reason": reason,
            "validation_scope": validation_scope,
            "expression_constructed": False, "operand_target_verified": False}


def formula_fragment_value(role, value):
    """Check each indexed row too; profile samples are not full-column proof."""
    text = str(value or "").strip()
    return bool(text) and (text.casefold() in _OPERATOR_VALUES if role == "calculation_operator"
                           else bool(_IDENTIFIER_VALUE.fullmatch(text)))


def formula_fragment_context(table):
    """Keep declared operators out of formulas without new scans/model calls."""
    fragments = {}
    profiles = {item["column"]: item for item in table.get("profiles", ())}
    safe = {item["column"] for item in classify_columns(table)
            if item["include_in_semantic_prompt"]}
    table_name = table.get("name") or ".".join(filter(None, (
        table.get("schema"), table.get("table_name"))))
    for column in table.get("columns", ()):
        name = column["column_name"]
        profile = profiles.get(name, {})
        samples = profile.get("distinct_sample") or []
        if name not in safe or profile.get("scan_scope") != "full_input" or not samples:
            continue
        # Profile values are genuine source observations, but do not have
        # record identities. Preserve that distinction instead of inventing rows.
        candidate = {"column": name, "role": "formula",
                     "status": "profile_supported_role_candidate", "semantic_status": "unjudged",
                     "schema_evidence_id": f"schema:{table_name}:{name}",
                     "evidence_kind": "source_declaration_and_profile_values",
                     "profile_field_id": profile.get("field_id"),
                     "profile_sample_scope": profile.get("sample_scope"),
                     "sample_exhaustive_in_input": profile.get("sample_exhaustive_in_input", False),
                     "observations": [{"column": name, "value": str(value),
                                       "source": "profile_distinct_sample"}
                                      for value in samples if value is not None and str(value).strip()]}
        fragment = classify_formula_fragment(table, candidate)
        if fragment is not None and fragment["effective_role"] == "calculation_operator":
            fragments[name] = fragment
    for candidate in table.get("inferred_semantic_roles", ()):
        fragment = classify_formula_fragment(table, candidate)
        if fragment is not None:
            fragments[fragment["column"]] = fragment
    return fragments


def _positive_int(config, key, default, *, maximum):
    value = config.get(key, default)
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"column_role_inference.{key} must be 1..{maximum}")
    return value


def _safe_columns(table):
    """Return only source columns eligible for sampled semantic inspection."""
    blocked = {"sensitive", "empty", "audit_time", "audit_metadata",
               "technical_identifier"}
    profiles = {item["column"]: item for item in table.get("profiles", ())}
    roles = _field_roles(table)
    settled_fragments = formula_fragment_context(table)
    checked_roles = {item["column"] for item in table.get("inferred_semantic_roles", ())
                     if item.get("status") == "source_verified_role_candidate"}
    # A metadata cue saying "calculation" is not a verified formula role.
    # Reuse the existing bounded table packet to review these columns first.
    review_formulas = set(roles.get("formula", ())) - set(settled_fragments) - checked_roles
    already_named = {name for fields in roles.values() for name in fields} - review_formulas
    already_named.update(settled_fragments)
    # Strong identifier/content conflicts are already resolved to bindings;
    # do not spend another model call trying to rename a known source key.
    already_named.update(item["column"] for item in field_role_conflicts(table)
                         if item["binding_only"])
    selected = []
    for item in classify_columns(table):
        name = item["column"]
        profile = profiles.get(name, {})
        usable = profile.get("usable_count")
        if (item["role"] in blocked or name in already_named
                or type(usable) is not int or usable <= 0):
            continue
        selected.append(name)
    return sorted(selected, key=lambda name: name not in review_formulas)


def _sample(data, table_name, columns, *, max_rows, max_value_chars):
    """Read a fixed spread of actual row positions, never a value product."""
    table = data.tables[table_name]
    total = table["rows"]
    count = min(total, max_rows)
    positions = sorted({1 + ((total - 1) * index) // max(1, count - 1)
                        for index in range(count)})
    if not positions:
        return [], {}, {"over_length": 0, "sensitive_shape": 0}
    placeholders = ", ".join("?" for _ in positions)
    select = ", ".join(qi(name) for name in columns)
    sql = (f"SELECT __r2_row, {select} FROM {qi(table['sql_name'])} "
           f"WHERE __r2_row IN ({placeholders}) ORDER BY __r2_row")
    rows = data.db.execute(sql, positions).fetchall()
    packet_rows, observed = [], {}
    omissions = {"over_length": 0, "sensitive_shape": 0}
    for values in rows:
        row_number = values[0]
        fragments = {}
        for column, raw in zip(columns, values[1:]):
            if raw is None or not str(raw).strip():
                continue
            text = str(raw)
            if len(text) > max_value_chars:
                omissions["over_length"] += 1
                continue
            if _SENSITIVE_VALUE.search(text):
                omissions["sensitive_shape"] += 1
                continue
            fragments[column] = text
            observed[(column, row_number)] = text
        if fragments:
            packet_rows.append({"row_number": row_number, "values": fragments})
    return packet_rows, observed, omissions


def _validate_response(data, table_name, response, eligible, observed):
    accepted, rejected = [], []
    seen = set()
    for proposal in response.proposals:
        column, role = proposal.column, proposal.role
        reason = None
        if column not in eligible:
            reason = "column_not_eligible_or_not_in_source"
        elif (column, role) in seen or any(item["column"] == column for item in accepted):
            reason = "duplicate_or_conflicting_role"
        elif not proposal.observations:
            reason = "no_source_observation"
        elif any(observed.get((column, item.row_number)) != item.value
                 for item in proposal.observations):
            reason = "sample_value_or_row_not_in_source"
        if reason:
            rejected.append({"column": column, "role": role, "reason": reason})
            continue
        seen.add((column, role))
        evidence = []
        for item in proposal.observations[:2]:
            info = data.tables[table_name]
            cursor = data.db.execute(
                f"SELECT * FROM {qi(info['sql_name'])} WHERE __r2_row = ?",
                [item.row_number])
            names = [part[0] for part in cursor.description]
            values = cursor.fetchone()
            row = dict(zip(names, values)) if values is not None else {}
            if row.get(column) != item.value:
                reason = "sample_changed_before_validation"
                break
            evidence.append({"row_number": item.row_number,
                             "record_id": data.record_id(table_name, row),
                             "value": item.value,
                             "column": column})
        if reason:
            rejected.append({"column": column, "role": role, "reason": reason})
            continue
        candidate = {"column": column, "role": role,
                     "status": "source_verified_role_candidate",
                     "semantic_status": "unjudged",
                     "schema_evidence_id": f"schema:{table_name}:{column}",
                     "evidence_kind": "source_values_supported_role_candidate",
                     "observations": evidence}
        context_role = declared_context_role(next(
            item for item in data.tables[table_name]["columns"]
            if item["column_name"] == column))
        if role == "scope" and context_role:
            candidate.update(role=context_role, proposed_role=role,
                             role_resolution="explicit_context_declaration_not_business_scope")
        fragment = classify_formula_fragment(data.tables[table_name], candidate)
        if role in _FRAGMENT_ROLES and fragment is None:
            rejected.append({"column": column, "role": role,
                             "reason": "fragment_role_conflicts_with_observed_values"})
            continue
        accepted.append(fragment or candidate)
    return accepted, rejected


async def infer_column_role_candidates(data, llm, config=None):
    """At most one shared-budget model call per unresolved table.

    Accepted source-checked *role candidates* are added to the in-memory table
    metadata so subsequent semantic-card recall can use them. The output
    reports both checked and omitted scope; it does not assert role truth.
    """
    config = config or {}
    if not isinstance(config, dict):
        raise ValueError("column_role_inference must be a mapping")
    allowed = {"enabled", "max_tables", "max_columns_per_table",
               "max_sample_rows", "max_value_chars"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError("Unknown column_role_inference settings: " + ", ".join(sorted(unknown)))
    enabled = config.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("column_role_inference.enabled must be a boolean")
    limits = {
        "max_tables": _positive_int(config, "max_tables", 12, maximum=1000),
        "max_columns_per_table": _positive_int(config, "max_columns_per_table", 16, maximum=64),
        "max_sample_rows": _positive_int(config, "max_sample_rows", 8, maximum=32),
        "max_value_chars": _positive_int(config, "max_value_chars", 128, maximum=512),
    }
    if not enabled:
        return {"source_snapshot": data.snapshot_id, "candidates": [], "tables": [], "coverage": {
            "status": "disabled", "partial": False, "reason": "column_role_inference.enabled=false",
            "reference_role": "not_supported_by_single_table_sample",
            "limits": limits}}

    tables = []
    all_candidates = []
    attempted = 0
    calls = 0
    for table_name, table in sorted(data.tables.items()):
        # Existing names do not settle other columns: opaque formulas, aliases
        # and scope fields still need bounded inspection.
        eligible = _safe_columns(table)
        resolved_conflicts = field_role_conflicts(table)
        if not eligible:
            tables.append({"table": table_name, "status": "no_unclassified_columns",
                           "reason": "no_safe_nonempty_unclassified_column", "candidates": [],
                           "role_conflicts": resolved_conflicts})
            continue
        if attempted >= limits["max_tables"]:
            tables.append({"table": table_name, "status": "unresolved",
                           "reason": "table_budget", "eligible_columns": eligible,
                           "candidates": []})
            continue
        attempted += 1
        selected = eligible[:limits["max_columns_per_table"]]
        sampled, observed, omissions = _sample(
            data, table_name, selected, max_rows=limits["max_sample_rows"],
            max_value_chars=limits["max_value_chars"])
        base = {"table": table_name, "status": "unresolved",
                "eligible_columns": selected,
                "eligible_columns_omitted": eligible[len(selected):],
                "sampled_row_numbers": [item["row_number"] for item in sampled],
                "sample_selection": "fixed_evenly_spaced_source_row_numbers",
                "sample_fragment_omissions": omissions,
                "input_rows": table["rows"], "candidates": [], "rejected": [],
                "known_roles": _field_roles(table), "role_conflicts": resolved_conflicts}
        if not observed:
            tables.append({**base, "reason": "no_safe_observed_value_in_bounded_rows"})
            continue
        packet = {
            "source_snapshot": data.snapshot_id, "table": table_name,
            "table_comment": table.get("table_comment") or "",
            "columns": [{"column": column,
                         "declared_data_type": next(c.get("data_type") for c in table["columns"]
                                                    if c["column_name"] == column),
                         "column_comment": next(c.get("column_comment") or "" for c in table["columns"]
                                                if c["column_name"] == column),
                         "usable_count": next(p["usable_count"] for p in table["profiles"]
                                              if p["column"] == column),
                         "approx_distinct": next(p.get("approx_distinct") for p in table["profiles"]
                                                 if p["column"] == column)}
                        for column in selected],
            "known_column_roles": _field_roles(table),
            "role_review_columns": [column for column in selected
                                    if column in _field_roles(table).get("formula", ())],
            "sample_rows": sampled, "allowed_roles": list(_ROLES),
            "context_role_contract": {
                "provenance": "source system, import or lineage context; not business applicability",
                "metadata": "lexical language, display or administrative context; not definition identity",
                "identity": "definition tenant, namespace or version; never row IDs or business observation keys",
                "scope": "business applicability or declared calculation grain; not a catch-all for constant fields",
                "reference_discriminator": "A value selecting a reference namespace is neither a description nor a formula. Leave it unresolved when its target is not visible.",
            },
            "contract": "proposals are uncertain column-role candidates, not business types; "
                        "cite exact sampled row_number and original value for each proposal; "
                        "known_column_roles are metadata recall hints, not verified roles; "
                        "review heuristic formula columns: an operator code such as SUM/RATIO/FILTER "
                        "is calculation_operator, not a complete formula",
        }
        try:
            calls += 1
            response = await llm.ask("column_role_inference", packet,
                                     ColumnRoleInferenceResponse)
        except Exception as exc:
            # A missing provider, exhausted shared budget, timeout or malformed
            # response must not turn a source column into an invented role.
            tables.append({**base, "reason": "model_unavailable_or_error",
                           "error_type": type(exc).__name__})
            continue
        accepted, rejected = _validate_response(data, table_name, response,
                                                 set(selected), observed)
        prior = table.get("inferred_semantic_roles", [])
        table["inferred_semantic_roles"] = [item for item in prior
            if item.get("column") not in {new["column"] for new in accepted}] + accepted
        consumed_roles = _field_roles(table)
        effective_conflicts = {(item["column"], item["proposed_role"]): item
                               for item in field_role_conflicts(table)}
        for item in accepted:
            conflict = effective_conflicts.get((item["column"], item["role"]))
            if conflict:
                item["effective_role"] = conflict["effective_role"]
                item["role_conflict"] = conflict
            item["used_in_candidate_recall"] = item["column"] in consumed_roles.get(
                item["role"], ()) or conflict is not None or item["role"] in {
                    "business_time", "dimension_coordinate", "numeric_business_value",
                    "calculation_operator", "operand_reference", *CONTEXT_ROLES}
            item["consumer"] = ("row_semantics" if item["role"] in {
                "business_time", "dimension_coordinate", "numeric_business_value"}
                else "semantic_card_recall")
        all_candidates.extend({"table": table_name, **item} for item in accepted)
        tables.append({**base, "status": "source_verified_candidates" if accepted else "unresolved",
                       "reason": "sample_support_only_not_semantic_truth" if accepted else "no_valid_proposal",
                       "candidates": accepted, "rejected": rejected,
                       "model_unresolved_columns": [column for column in response.unresolved_columns if column in selected],
                       "unresolved_columns": sorted(set(
                           [column for column in response.unresolved_columns if column in selected]
                           + [column for column in selected
                              if column not in {item["column"] for item in accepted}]))})
    return {"source_snapshot": data.snapshot_id,
            "candidates": all_candidates, "tables": tables,
            "coverage": {"status": "candidate_only", "scope": "imported_csv_snapshot_only",
                         "tables_considered": len(tables), "model_calls_attempted": calls,
                         "source_verified_candidates": len(all_candidates),
                         "candidates_used_in_recall": sum(item["used_in_candidate_recall"]
                                                          for item in all_candidates),
                         "unresolved_tables": sum(item["status"] == "unresolved" for item in tables),
                         "partial": any(item["status"] == "unresolved" or
                                        item.get("eligible_columns_omitted") or
                                        item.get("unresolved_columns") or item.get("rejected") or
                                        any(item.get("sample_fragment_omissions", {}).values()) or
                                        any(not candidate["used_in_candidate_recall"]
                                            for candidate in item["candidates"])
                                        for item in tables),
                         "semantic_status": "unjudged; sample verification does not prove a column's role",
                         "reference_role": "not_supported_by_single_table_sample",
                         "limits": limits}}
