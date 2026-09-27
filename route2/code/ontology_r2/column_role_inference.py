"""Bounded, source-checked column-role *candidates* for opaque source tables.

This is a recall stage, not an ontology classifier. A model may suggest which
otherwise unreadable column contains a name or definition, but it cannot make
that suggestion evidence without an exact value from a sampled source row.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .column_roles import classify_columns
from .concept_candidates import _field_roles
from .storage import qi


_ROLES = ("name", "alias", "description", "formula", "unit", "scope")
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
    role: Literal["name", "alias", "description", "formula", "unit", "scope"]
    observations: list[ObservedValue] = Field(default_factory=list)
    rationale: str = ""


class ColumnRoleInferenceResponse(_Strict):
    proposals: list[ColumnRoleProposal] = Field(default_factory=list)
    unresolved_columns: list[str] = Field(default_factory=list)


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
    already_named = {name for fields in _field_roles(table).values() for name in fields}
    selected = []
    for item in classify_columns(table):
        name = item["column"]
        profile = profiles.get(name, {})
        usable = profile.get("usable_count")
        numeric = profile.get("numeric_shape_count")
        if (item["role"] in blocked or name in already_named
                or type(usable) is not int or usable <= 0):
            continue
        if type(numeric) is int and numeric / usable >= 0.9:
            continue
        selected.append(name)
    return selected


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
        accepted.append({"column": column, "role": role,
                         "status": "source_verified_role_candidate",
                         "semantic_status": "unjudged",
                         "schema_evidence_id": f"schema:{table_name}:{column}",
                         "observations": evidence})
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
        if _field_roles(table).get("name"):
            continue
        eligible = _safe_columns(table)
        if not eligible:
            tables.append({"table": table_name, "status": "unresolved",
                           "reason": "no_safe_nonempty_unclassified_column", "candidates": []})
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
                "input_rows": table["rows"], "candidates": [], "rejected": []}
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
            "sample_rows": sampled, "allowed_roles": list(_ROLES),
            "contract": "proposals are uncertain column-role candidates, not business types; "
                        "cite exact sampled row_number and original value for each proposal",
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
        table["inferred_semantic_roles"] = accepted
        consumed_roles = _field_roles(table)
        for item in accepted:
            item["used_in_candidate_recall"] = item["column"] in consumed_roles.get(
                item["role"], ())
        all_candidates.extend({"table": table_name, **item} for item in accepted)
        tables.append({**base, "status": "source_verified_candidates" if accepted else "unresolved",
                       "reason": "sample_support_only_not_semantic_truth" if accepted else "no_valid_proposal",
                       "candidates": accepted, "rejected": rejected,
                       "model_unresolved_columns": [column for column in response.unresolved_columns
                                                    if column in selected]})
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
                                        any(not candidate["used_in_candidate_recall"]
                                            for candidate in item["candidates"])
                                        for item in tables),
                         "semantic_status": "unjudged; sample verification does not prove a column's role",
                         "reference_role": "not_supported_by_single_table_sample",
                         "limits": limits}}
