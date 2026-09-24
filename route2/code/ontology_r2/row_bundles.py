"""Small, source-grounded row examples for a checked field-pair candidate."""

from .storage import qi


def _field(data, table, field):
    if table not in data.tables or field not in data.tables[table]["column_names"]:
        raise ValueError(f"Unknown field: {table}.{field}")
    return qi(field)


def _row(data, table, row_number, key, fields):
    info = data.tables[table]
    shown = list(dict.fromkeys([key, *fields]))
    needed = list(dict.fromkeys([*shown, *info["pk"]]))
    columns = ", ".join(qi(field) for field in needed)
    cursor = data.db.execute(
        f"SELECT __r2_row, {columns} FROM {qi(info['sql_name'])} WHERE __r2_row = ?",
        [row_number],
    )
    values = cursor.fetchone()
    if values is None:
        raise ValueError(f"Validated row is absent from snapshot: {table}:{row_number}")
    record = dict(zip(["__r2_row", *needed], values))
    return {"table": table, "row_number": row_number,
            "record_id": data.record_id(table, record),
            "fields": {field: record[field] for field in shown}}


def joint_examples(data, candidate, check, *, limit=2, source_fields=(), target_fields=()):
    """Return up to two unique raw-equality pairs and one checked counterexample.

    These rows illustrate a verified *field comparison*, not an accepted
    business relation. Selector and scope predicates follow the exact check.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a nonnegative integer")
    if isinstance(source_fields, (str, bytes)) or isinstance(target_fields, (str, bytes)):
        raise ValueError("projection fields must be sequences of column names")
    source, target = candidate["source"], candidate["target"]
    source_table, target_table = source["table"], target["table"]
    source_key = _field(data, source_table, source["field"])
    target_key = _field(data, target_table, target["field"])
    source_fields, target_fields = tuple(source_fields), tuple(target_fields)
    for field in source_fields:
        _field(data, source_table, field)
    for field in target_fields:
        _field(data, target_table, field)
    if (check.get("candidate_id") != candidate["candidate_id"]
            or check.get("snapshot_id") != data.snapshot_id
            or check.get("source") != source or check.get("target") != target):
        raise ValueError("Candidate and check must refer to the same snapshot field pair")

    counts = check.get("checks", {})
    result = {
        "candidate_id": candidate["candidate_id"], "snapshot_id": data.snapshot_id,
        "source": source, "target": target,
        "match_basis": "raw_value_identity", "semantic_relation": "unresolved",
        "validation_ref": {
            "candidate_id": check["candidate_id"], "snapshot_id": check["snapshot_id"],
            "scan_scope": check.get("scan_scope"), "normalization": check.get("normalization"),
            "selector": check.get("selector") or {},
            "scope_bindings": check.get("scope_bindings") or {},
            "eligible_references": counts.get("eligible_references"),
            "unique_matches": counts.get("unique_matches"),
            "ambiguous_matches": counts.get("ambiguous_matches"),
            "missing_in_input": counts.get("missing_in_input"),
        },
        "matched_pairs": [], "counterexamples": [],
    }
    if (source_table == target_table or check.get("decision", {}).get("status") != "checked"
            or check.get("normalization") != "identity"
            or not isinstance(counts.get("unique_matches"), int)
            or counts["unique_matches"] <= 0):
        result["status"] = "not_applicable"
        return result
    if limit == 0:
        result["status"] = "disabled_by_limit"
        return result

    selector = check.get("selector") or {}
    scopes = check.get("scope_bindings") or {}
    if not isinstance(selector, dict) or not isinstance(scopes, dict):
        raise ValueError("selector and scope_bindings must be mappings")
    for field in selector:
        _field(data, source_table, field)
    for target_field, source_field in scopes.items():
        _field(data, target_table, target_field)
        _field(data, source_table, source_field)

    scope_pairs = sorted(scopes.items())
    source_scope = " AND ".join(
        f"s.{qi(source_field)} IS NOT NULL AND s.{qi(source_field)} <> '' "
        f"AND trim(s.{qi(source_field)}) <> ''"
        for _, source_field in scope_pairs) or "TRUE"
    target_scope = " AND ".join(
        f"t.{qi(target_field)} IS NOT NULL AND t.{qi(target_field)} <> '' "
        f"AND trim(t.{qi(target_field)}) <> ''"
        for target_field, _ in scope_pairs) or "TRUE"
    selector_sql = " AND ".join(f"s.{qi(field)} = ?" for field in sorted(selector)) or "TRUE"
    target_scope_select = "".join(
        f", t.{qi(target_field)} AS {qi(f'scope_{i}')}"
        for i, (target_field, _) in enumerate(scope_pairs))
    group_positions = ", ".join(str(i) for i in range(1, len(scope_pairs) + 2))
    scope_join = "".join(
        f" AND s.{qi(source_field)} = u.{qi(f'scope_{i}')}"
        for i, (_, source_field) in enumerate(scope_pairs))
    sql = f"""
      WITH unique_target AS (
        SELECT t.{target_key} AS ref{target_scope_select},
               min(t.__r2_row) AS target_row_number
        FROM {qi(data.tables[target_table]['sql_name'])} t
        WHERE t.{target_key} IS NOT NULL AND t.{target_key} <> ''
          AND trim(t.{target_key}) <> '' AND {target_scope}
        GROUP BY {group_positions} HAVING count(*) = 1
      )
      SELECT s.__r2_row AS source_row_number, u.target_row_number,
             s.{source_key} AS raw_value
      FROM {qi(data.tables[source_table]['sql_name'])} s
      JOIN unique_target u ON s.{source_key} = u.ref{scope_join}
      WHERE s.{source_key} IS NOT NULL AND s.{source_key} <> ''
        AND trim(s.{source_key}) <> '' AND {source_scope}
        AND ({selector_sql}) IS TRUE
      ORDER BY s.__r2_row, u.target_row_number LIMIT ?
    """
    pairs = data.db.execute(sql, [*(selector[field] for field in sorted(selector)), min(limit, 2)]).fetchall()
    for source_row, target_row, raw_value in pairs:
        result["matched_pairs"].append({
            "source_record": _row(data, source_table, source_row, source["field"], source_fields),
            "target_record": _row(data, target_table, target_row, target["field"], target_fields),
            "matching_raw_value": raw_value,
        })
    for example in counts.get("counterexample_rows", [])[:1]:
        row_number = example["row_number"]
        result["counterexamples"].append({
            "reason": example["reason"],
            "source_record": _row(data, source_table, row_number, source["field"], source_fields),
        })
    result["status"] = "examples" if result["matched_pairs"] else "no_sample"
    return result
