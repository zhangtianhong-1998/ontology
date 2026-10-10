"""Small, source-grounded row examples for a checked field-pair candidate."""

from .storage import qi
from .discovery import association_match_sql


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
    """Return up to two uniquely matched original row pairs and one counterexample.

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
            or check.get("normalization", "identity") not in {"identity", "nfkc_whitespace_casefold", "source_alias_items", "target_alias_items", "both_alias_items"}
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

    transform = check.get("normalization", "identity")
    sql, params = association_match_sql(data, candidate, selector=selector,
                                         scope_bindings=scopes, transform=transform)
    # The matching SQL emits at most one target for each eligible source row.
    # Sample different observed targets before repeating one popular target;
    # this groups validated pairs in DuckDB, never expands a row/target product.
    pairs = data.db.execute(f"""WITH matches AS ({sql}), ranked AS (
        SELECT source_row_number, target_row_number,
               row_number() OVER (PARTITION BY target_row_number
                                  ORDER BY source_row_number) AS target_occurrence,
               count(DISTINCT target_row_number) OVER () AS target_witness_count
        FROM matches
    ) SELECT source_row_number, target_row_number, target_witness_count FROM ranked
      ORDER BY target_occurrence, source_row_number, target_row_number LIMIT ?""",
        [*params, min(limit, 2)]).fetchall()
    result["match_basis"] = transform
    target_count = pairs[0][2] if pairs else 0
    sampled_targets = len({pair[1] for pair in pairs})
    result["sampling"] = {"method": "distinct_observed_target_round_robin",
                          "unique_target_witnesses": target_count,
                          "sampled_unique_target_witnesses": sampled_targets,
                          "unique_target_witnesses_not_sampled": target_count - sampled_targets,
                          "ambiguous_targets_sampled_as_positive": False}
    for source_row, target_row, _ in pairs:
        result["matched_pairs"].append({
            "source_record": _row(data, source_table, source_row, source["field"], source_fields),
            "target_record": _row(data, target_table, target_row, target["field"], target_fields),
            "matching_raw_value": _row(data, source_table, source_row, source["field"], ())["fields"][source["field"]],
            "source_raw_value": _row(data, source_table, source_row, source["field"], ())["fields"][source["field"]],
            "target_raw_value": _row(data, target_table, target_row, target["field"], ())["fields"][target["field"]],
            "transform": transform,
        })
    for example in counts.get("counterexample_rows", [])[:1]:
        row_number = example["row_number"]
        result["counterexamples"].append({
            "reason": example["reason"],
            "source_record": _row(data, source_table, row_number, source["field"], source_fields),
        })
    result["status"] = "examples" if result["matched_pairs"] else "no_sample"
    return result
