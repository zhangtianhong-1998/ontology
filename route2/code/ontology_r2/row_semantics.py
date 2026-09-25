"""Conservative row-purpose and joint-distinct analysis of one CSV snapshot.

This only decides which evidence should be sent to concept induction. It does
not turn a distinct tuple into a concept, a primary key, or a business join.
The candidate column sets are bounded and never enumerate value products.
"""

from __future__ import annotations

import re

from .column_roles import classify_columns
from .concept_candidates import _field_roles
from .storage import qi


_TIME = re.compile(r"(?:^|_)(?:year|quarter|month|period|date|time|week)(?:_|$)|年|季|月|期间|账期|会计期", re.I)
_DIMENSION = re.compile(
    r"(?:^|_)(?:dim|dimension|region|area|country|city|fruit|product|"
    r"category|channel|customer|market|org)(?:_|$)|维度|地区|区域|国家|城市|水果|品类|渠道|客户|市场|组织", re.I)
_VALUE = re.compile(
    r"(?:^|_)(?:amount|revenue|profit|cost|quantity|qty|price|volume|"
    r"count|rate|ratio|total|sales|value)(?:_|$)|金额|收入|利润|成本|"
    r"产量|销量|均价|数量|比率|占比|总额|度量值|指标值", re.I)
_CONTROL = frozenset(("rule", "config", "anchor", "log", "reference", "ref",
                      "param", "input", "output", "tag", "connection", "member",
                      "field", "column"))
_DEFINITION = frozenset(("definition", "def", "detail", "common", "attribute", "attr"))


def _numeric_value(column, profile):
    sql_type = str(column.get("data_type") or "").casefold()
    declared_numeric = bool(re.match(r"^(?:smallint|integer|bigint|int\d*|decimal|numeric|real|double|float)", sql_type))
    usable = profile.get("usable_count") or 0
    numeric = profile.get("numeric_shape_count") or 0
    return declared_numeric or (usable >= 2 and numeric / usable >= 0.9)


def classify_row_purpose(table):
    """Describe a *table's likely row purpose*, leaving mixed cases unresolved.

    Strong fact evidence requires a numeric business value plus both a business
    time and a dimension coordinate. A definition/formula in the same table
    makes that inference ambiguous, so the table is not silently excluded.
    """
    roles = _field_roles(table)
    columns = {item["column_name"]: item for item in table["columns"]}
    profiles = {item["column"]: item for item in table.get("profiles", ())}
    safe = {item["column"] for item in classify_columns(table)
            if item["role"] not in ("sensitive", "empty", "audit_time", "audit_metadata")}
    pk = set(table.get("pk") or ())
    names = []
    times = []
    dimensions = []
    values = []
    for name in table["column_names"]:
        if name not in safe or name in pk:
            continue
        column = columns[name]
        text = name + " " + str(column.get("column_comment") or "")
        if _TIME.search(text):
            times.append(name)
        if _DIMENSION.search(text):
            dimensions.append(name)
        if _VALUE.search(text) and _numeric_value(column, profiles.get(name, {})):
            values.append(name)
        if name in roles.get("name", ()):
            names.append(name)
    tokens = set(table["table_name"].casefold().split("_"))
    has_definition_text = bool(roles.get("description") or roles.get("formula"))
    fact_shape = bool(times and dimensions and values)
    if tokens & _CONTROL and fact_shape:
        purpose = "unresolved"
        reason = "control_table_name_and_business_fact_evidence_coexist"
    elif tokens & _CONTROL:
        purpose = "configuration_data"
        reason = "control_or_reference_table_name"
    elif fact_shape and has_definition_text:
        purpose = "unresolved"
        reason = "business_fact_and_definition_evidence_coexist"
    elif fact_shape:
        purpose = "business_fact"
        reason = "numeric_business_value_with_time_and_dimension"
    elif names and (has_definition_text or tokens & _DEFINITION):
        purpose = "definition_data"
        reason = "named_definition_or_formula"
    else:
        purpose = "unresolved"
        reason = "insufficient_or_mixed_row_purpose_evidence"
    return {"purpose": purpose, "reason": reason,
            "evidence_columns": {"name": names, "definition": list(roles.get("description", ())),
                                 "formula": list(roles.get("formula", ())),
                                 "business_time": times, "dimension_coordinate": dimensions,
                                 "numeric_business_value": values},
            "scope": "imported_csv_snapshot_only",
            "classification_granularity": "table_level_heuristic",
            "authority": "heuristic_candidate_not_business_fact_proof"}


def _candidate_groups(table, purpose, *, max_columns):
    """Select a few meaningful sets; do not enumerate the power set."""
    roles = _field_roles(table)
    classified = classify_columns(table)
    safe = {item["column"] for item in classified if item["role"] not in
            ("sensitive", "empty", "audit_time", "audit_metadata")}
    valid = lambda fields: tuple(dict.fromkeys(field for field in fields if field in safe))[:max_columns]
    groups = []
    def add(label, fields):
        fields = valid(fields)
        if fields and fields not in [old[1] for old in groups]:
            groups.append((label, fields))
    add("declared_primary_key", table.get("pk") or ())
    codes = [item["column"] for item in classified
             if item["join_eligible"] and item["column"] not in table.get("pk", ())
             and item["column"] in safe]
    for field in codes[:2]:
        add("reference_key", [field])
    names = roles.get("name", ())
    scopes = roles.get("scope", ())
    if names:
        add("name", names[:1])
        if scopes:
            add("name_and_scope", [names[0], *scopes[:2]])
    evidence = purpose["evidence_columns"]
    if purpose["purpose"] == "business_fact":
        coordinates = [*codes[:1], *evidence["dimension_coordinate"][:2],
                       *evidence["business_time"][:1]]
        add("observation_coordinates", coordinates)
        if evidence["numeric_business_value"]:
            add("coordinates_and_value", [*coordinates[:max_columns - 1],
                                          evidence["numeric_business_value"][0]])
    return groups


def profile_joint_distinct(data, table_name, purpose=None, *, max_groups=6, max_columns=4):
    """Exact distinct counts for a bounded list of single and joint field sets.

    Rows with a blank in any member are excluded from that candidate's count.
    A 100% distinct ratio is only an observed uniqueness statement within the
    imported snapshot, not a stable key assertion about future data.
    """
    if any(type(value) is not int or value < 1 for value in (max_groups, max_columns)):
        raise ValueError("Joint-distinct limits must be positive integers")
    table = data.tables[table_name]
    purpose = purpose or classify_row_purpose(table)
    choices = _candidate_groups(table, purpose, max_columns=max_columns)
    selected = choices[:max_groups]
    if not selected:
        return {"table": table_name, "scan_scope": "full_input", "input_rows": table["rows"],
                "candidate_sets": [], "candidate_sets_omitted_limit": 0,
                "method": "bounded_exact_joint_distinct_no_cartesian_enumeration"}
    expressions = []
    for _, fields in selected:
        usable = " AND ".join(f"{qi(field)} IS NOT NULL AND length(trim({qi(field)})) > 0"
                              for field in fields)
        tuple_sql = qi(fields[0]) if len(fields) == 1 else "(" + ", ".join(map(qi, fields)) + ")"
        expressions.extend([f"count(*) FILTER (WHERE {usable})",
                            f"count(DISTINCT {tuple_sql}) FILTER (WHERE {usable})"])
    stats = data.db.execute(
        f"SELECT {', '.join(expressions)} FROM {qi(table['sql_name'])}").fetchone()
    candidate_sets = []
    for position, (label, fields) in enumerate(selected):
        complete, distinct = stats[2 * position:2 * position + 2]
        candidate_sets.append({"label": label, "columns": list(fields),
                               "complete_rows": complete, "distinct_tuples": distinct,
                               "duplicate_rows": complete - distinct,
                               "blank_or_null_rows": table["rows"] - complete,
                               "unique_within_complete_input_rows": complete > 0 and complete == distinct,
                               "exact": True})
    return {"table": table_name, "scan_scope": "full_input", "input_rows": table["rows"],
            "candidate_sets": candidate_sets,
            "candidate_sets_omitted_limit": max(0, len(choices) - len(selected)),
            "method": "bounded_exact_joint_distinct_no_cartesian_enumeration",
            "semantic_status": "candidate_grain_only_not_business_key_or_relation"}
