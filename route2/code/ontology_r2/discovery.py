"""Bounded, deterministic field-pair discovery and exact input-level checks.

Candidates are technical leads, never inferred foreign keys or ontology facts.
All joins compare original CSV values; no normalization is applied implicitly.
"""

from collections import defaultdict
from contextlib import nullcontext
from itertools import combinations
import re

from .column_roles import classify_columns
from .storage import digest, qi


_GENERIC = {"id", "key", "code", "name", "ref", "fk", "value", "uuid"}


def _fields(data):
    return [(table, column) for table, info in sorted(data.tables.items())
            for column in info["column_names"]]


def _checked_field(data, table, field):
    if table not in data.tables or field not in data.tables[table]["column_names"]:
        raise ValueError(f"Unknown field: {table}.{field}")
    return qi(data.tables[table]["sql_name"]), qi(field)


def _tokens(name):
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return tuple(x for x in re.split(r"[^\w]+", name.casefold()) if x)


def _stem(name):
    return tuple(x for x in _tokens(name) if x not in _GENERIC)


def _table_stem(info):
    tokens = list(_tokens(info["table_name"]))
    if tokens and tokens[-1] == "t":
        tokens.pop()
    return tuple(tokens)


def _metadata_pair(source, target, data):
    source_table, source_col = source
    target_table, target_col = target
    if source == target:
        return False
    source_definition = next((c.get("column_comment") or ""
                              for c in data.tables[source_table].get("columns", [])
                              if c.get("column_name") == source_col), "").casefold()
    target_label = (data.tables[target_table]["table_name"] + "." + target_col).casefold()
    if target_label in source_definition or (target_table + "." + target_col).casefold() in source_definition:
        return True
    a, b = _stem(source_col), _stem(target_col)
    if source_col.casefold() == target_col.casefold() and a:
        return True
    target_name = _table_stem(data.tables[target_table])
    if _tokens(target_col) in (("id",), ("code",), ("key",), ("uuid",)):
        return bool(target_name and a and (a == target_name or a == target_name[:-1]))
    return bool(a and b and a == b and len(a) > 0)


def _sample_values(data, table, field, limit, max_length):
    sql_table, sql_field = _checked_field(data, table, field)
    # md5 makes this bounded sample stable under CSV row reordering. It is a
    # candidate-retrieval sample, never the denominator for exact coverage.
    rows = data.db.execute(
        f"SELECT DISTINCT {sql_field} FROM {sql_table} "
        f"WHERE {sql_field} IS NOT NULL AND {sql_field} <> '' "
        f"AND trim({sql_field}) <> '' AND length({sql_field}) <= ? "
        f"ORDER BY md5({sql_field}), {sql_field} LIMIT ?",
        [max_length, limit + 1],
    ).fetchall()
    return [row[0] for row in rows[:limit]], len(rows) > limit


def propose_candidates(data, *, max_candidates_total=2000,
                       max_per_source_per_channel=20,
                       max_indexed_fields=64,
                       max_indexed_values_per_field=128,
                       max_value_length=96,
                       max_fields_per_common_value=32, on_step=None, on_note=None):
    """Recall directional field pairs from declarations, names and raw values.

    Returns ``{"candidates": [...], "coverage": {...}}``. The value index is
    bounded per field and high-fanout values are skipped. A result can be
    absent even when a real link exists; exact validation must precede use.
    """
    limits = (max_candidates_total, max_per_source_per_channel,
              max_indexed_fields,
              max_indexed_values_per_field, max_value_length,
              max_fields_per_common_value)
    if any(not isinstance(x, int) or x <= 0 for x in limits):
        raise ValueError("Discovery limits must be positive integers")
    all_fields = _fields(data)
    excluded = {(table, item["column"])
                for table, info in data.tables.items() if info.get("columns")
                for item in classify_columns(info)
                if item["role"] in ("empty", "audit_time", "audit_metadata", "sensitive")}
    fields = [field for field in all_fields if field not in excluded]
    proposals = defaultdict(lambda: {"channels": set(), "shared_sample_values": set()})
    declared = set()
    for table, info in sorted(data.tables.items()):
        for fk in info.get("foreign_keys", []):
            source = (table, fk["column_name"])
            target = (f"{fk['referenced_schema']}.{fk['referenced_table']}",
                      fk["referenced_column"])
            if source in all_fields and target in all_fields:
                declared.add((source, target))
                proposals[(source, target)]["channels"].add("declared_fk")

    metadata_by_source = defaultdict(list)
    for source in fields:
        for target in fields:
            if _metadata_pair(source, target, data):
                metadata_by_source[source].append(target)
        if on_step is not None:
            on_step(f"字段名 {source[0]}.{source[1]}")
    omitted_metadata = 0
    for source, targets in metadata_by_source.items():
        # A declared key and an observed primary key are useful *ranking*
        # signals. Neither is proof that the source references that target.
        targets.sort(key=lambda target: (
            target[1] not in data.tables[target[0]].get("pk", []),
            target[0], target[1]))
        for target in targets[:max_per_source_per_channel]:
            proposals[(source, target)]["channels"].add("metadata")
        omitted_metadata += max(0, len(targets) - max_per_source_per_channel)

    # Index a bounded set across tables, prioritizing declared keys and
    # generic identifier-like names. Metadata recall still sees every field.
    by_table = defaultdict(list)
    for table, column in fields:
        info = data.tables[table]
        tokens = set(_tokens(column))
        priority = (column not in info.get("pk", []),
                    not bool(tokens & {"id", "code", "key", "uuid", "ref", "fk"}),
                    info["column_names"].index(column), column)
        by_table[table].append((priority, (table, column)))
    for columns in by_table.values():
        columns.sort()
    indexed_fields = []
    rank = 0
    while len(indexed_fields) < min(max_indexed_fields, len(fields)):
        added = False
        for table in sorted(by_table):
            if rank < len(by_table[table]):
                indexed_fields.append(by_table[table][rank][1])
                added = True
                if len(indexed_fields) >= max_indexed_fields:
                    break
        if not added:
            break
        rank += 1
    inverted = defaultdict(list)
    truncated_fields = []
    for field in indexed_fields:
        if on_note is not None:
            on_note(f"值样本 {field[0]}.{field[1]} 查询中")
        values, truncated = _sample_values(data, *field,
                                           max_indexed_values_per_field,
                                           max_value_length)
        if on_step is not None:
            on_step(f"值样本 {field[0]}.{field[1]}")
        if truncated:
            truncated_fields.append(f"{field[0]}.{field[1]}")
        for value in values:
            inverted[value].append(field)
    shared = defaultdict(set)
    high_fanout_values = 0
    for value, holders in inverted.items():
        if len(holders) > max_fields_per_common_value:
            high_fanout_values += 1
            continue
        for left, right in combinations(holders, 2):
            shared[(left, right)].add(value)
            shared[(right, left)].add(value)
    overlap_by_source = defaultdict(list)
    for (source, target), values in shared.items():
        overlap_by_source[source].append((target, values))
    omitted_value_pairs = 0
    for source, targets in overlap_by_source.items():
        targets.sort(key=lambda item: (
            -len(item[1]),
            item[0][1] not in data.tables[item[0][0]].get("pk", []),
            item[0]))
        for target, values in targets[:max_per_source_per_channel]:
            entry = proposals[(source, target)]
            entry["channels"].add("value_overlap")
            entry["shared_sample_values"].update(values)
        omitted_value_pairs += max(0, len(targets) - max_per_source_per_channel)

    ranked = sorted(proposals, key=lambda pair: (
        pair not in declared,
        "metadata" not in proposals[pair]["channels"],
        -len(proposals[pair]["shared_sample_values"]),
        pair))
    retained = ranked[:max_candidates_total]
    # Never silently lose a declared FK behind a global candidate budget.
    for pair in sorted(declared):
        if pair not in retained:
            retained.append(pair)
    candidates = []
    for source, target in retained:
        entry = proposals[(source, target)]
        values = entry["shared_sample_values"]
        numeric_only = bool(values) and all(v.isdecimal() for v in values)
        candidates.append({
            "candidate_id": digest([source, target, data.snapshot_id])[:24],
            "source": {"table": source[0], "field": source[1]},
            "target": {"table": target[0], "field": target[1]},
            "retrieval_channels": sorted(entry["channels"]),
            "shared_sample_value_count": len(values),
            "numeric_overlap_only": numeric_only,
            "target_declared_pk": target[1] in data.tables[target[0]].get("pk", []),
            "decision": {"status": "proposed", "semantic_relation": "unresolved"},
        })
    indexed_set = set(indexed_fields)
    return {"candidates": candidates, "coverage": {
        "input_scope": "unknown", "scan_scope": "full_input_for_selected_field_samples",
        "fields_considered": len(all_fields),
        "candidate_fields_considered": len(fields),
        "candidate_fields_excluded_empty_or_audit": [f"{table}.{column}"
                                                      for table, column in sorted(excluded)],
        "value_index_fields": len(indexed_fields),
        "fields_not_value_indexed": [f"{t}.{c}" for t, c in fields
                                     if (t, c) not in indexed_set],
        "fields_with_truncated_value_samples": truncated_fields,
        "high_fanout_values_skipped": high_fanout_values,
        "metadata_pairs_queued": omitted_metadata,
        "value_pairs_queued": omitted_value_pairs,
        "candidates_queued_global": len(ranked) - min(len(ranked), max_candidates_total),
        "candidate_count": len(candidates),
    }}


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def validate_candidate(data, candidate, *, selector=None, scope_bindings=None,
                       sample_limit=5):
    """Exactly check one raw-value field pair over the imported CSV snapshot.

    ``selector`` is a mapping of source fields to literal values, combined by
    SQL three-valued AND. ``scope_bindings`` maps target columns to source
    columns. Identity comparison only; no transformation or semantic claim.
    """
    source, target = candidate["source"], candidate["target"]
    source_table, source_key = _checked_field(data, source["table"], source["field"])
    target_table, target_key = _checked_field(data, target["table"], target["field"])
    selector = selector or {}
    scope_bindings = scope_bindings or {}
    if not isinstance(selector, dict) or not isinstance(scope_bindings, dict):
        raise ValueError("selector and scope_bindings must be mappings")
    if not isinstance(sample_limit, int) or sample_limit < 0:
        raise ValueError("sample_limit must be nonnegative")
    for field in selector.values():
        if field is not None and not isinstance(field, str):
            raise ValueError("selector values must be strings or null")
    for source_scope in scope_bindings.values():
        _checked_field(data, source["table"], source_scope)
    for target_scope in scope_bindings:
        _checked_field(data, target["table"], target_scope)
    for source_condition in selector:
        _checked_field(data, source["table"], source_condition)

    scope_pairs = list(sorted(scope_bindings.items()))
    source_extra = ", ".join(
        f"s.{qi(source_scope)} AS {qi(f'scope_{i}')}"
        for i, (_, source_scope) in enumerate(scope_pairs))
    target_extra = ", ".join(
        f"t.{qi(target_scope)} AS {qi(f'scope_{i}')}"
        for i, (target_scope, _) in enumerate(scope_pairs))
    select_expr = " AND ".join(f"s.{qi(field)} = ?" for field in selector) or "TRUE"
    params = list(selector.values())
    source_scope_usable = " AND ".join(
        f"s.scope_{i} IS NOT NULL AND s.scope_{i} <> '' AND trim(s.scope_{i}) <> ''"
        for i in range(len(scope_pairs))) or "TRUE"
    target_scope_usable = " AND ".join(
        f"scope_{i} IS NOT NULL AND scope_{i} <> '' AND trim(scope_{i}) <> ''"
        for i in range(len(scope_pairs))) or "TRUE"
    scope_group = ", " + ", ".join(f"scope_{i}" for i in range(len(scope_pairs))) if scope_pairs else ""
    join_scope = " AND ".join(f"s.scope_{i} = t.scope_{i}" for i in range(len(scope_pairs)))
    join_scope = " AND " + join_scope if join_scope else ""
    ctes = f"""
    WITH source_rows AS (
      SELECT s.__r2_row AS row_number, s.{source_key} AS ref,
             ({select_expr}) AS selected
             {', ' + source_extra if source_extra else ''}
      FROM {source_table} s
    ), target_rows AS (
      SELECT t.{target_key} AS ref
             {', ' + target_extra if target_extra else ''}
      FROM {target_table} t
    ), target_keys AS (
      SELECT ref{scope_group}, count(*) AS multiplicity
      FROM target_rows
      WHERE ref IS NOT NULL AND ref <> '' AND trim(ref) <> ''
        AND {target_scope_usable}
      GROUP BY ref{scope_group}
    ), joined AS (
      SELECT s.*, t.multiplicity,
             (s.ref IS NOT NULL AND s.ref <> '' AND trim(s.ref) <> '') AS usable,
             ({source_scope_usable}) AS scope_usable
      FROM source_rows s LEFT JOIN target_keys t
        ON s.ref = t.ref{join_scope}
    )
    """
    counts = data.db.execute(ctes + """
      SELECT count(*) AS source_rows,
             count(*) FILTER (WHERE selected IS TRUE) AS selector_true,
             count(*) FILTER (WHERE selected IS FALSE) AS selector_false,
             count(*) FILTER (WHERE selected IS NULL) AS selector_unknown,
             count(*) FILTER (WHERE selected IS TRUE AND ref IS NULL) AS null_references,
             count(*) FILTER (WHERE selected IS TRUE AND ref = '') AS empty_references,
             count(*) FILTER (WHERE selected IS TRUE AND ref <> '' AND trim(ref) = '') AS whitespace_references,
             count(*) FILTER (WHERE selected IS TRUE AND usable AND NOT scope_usable) AS missing_scope,
             count(*) FILTER (WHERE selected IS TRUE AND usable AND scope_usable) AS eligible_references,
             count(*) FILTER (WHERE selected IS TRUE AND usable AND scope_usable AND multiplicity > 0) AS matched_references,
             count(*) FILTER (WHERE selected IS TRUE AND usable AND scope_usable AND multiplicity = 1) AS unique_matches,
             count(*) FILTER (WHERE selected IS TRUE AND usable AND scope_usable AND multiplicity > 1) AS ambiguous_matches,
             count(*) FILTER (WHERE selected IS TRUE AND usable AND scope_usable AND multiplicity IS NULL) AS missing_in_input,
             count(*) FILTER (WHERE selected IS FALSE AND usable AND scope_usable) AS outside_eligible_references,
             count(*) FILTER (WHERE selected IS FALSE AND usable AND scope_usable AND multiplicity > 0) AS outside_matched_references
      FROM joined
    """, params).fetchone()
    names = ("source_rows", "selector_true", "selector_false", "selector_unknown",
             "null_references", "empty_references", "whitespace_references",
             "missing_scope", "eligible_references", "matched_references",
             "unique_matches", "ambiguous_matches", "missing_in_input",
             "outside_eligible_references", "outside_matched_references")
    result = dict(zip(names, counts))
    distinct = data.db.execute(ctes + f"""
      SELECT count(*) AS distinct_eligible_keys,
             count(*) FILTER (WHERE multiplicity > 0) AS distinct_keys_matched
      FROM (SELECT DISTINCT ref{scope_group}, multiplicity FROM joined
            WHERE selected IS TRUE AND usable AND scope_usable)
    """, params).fetchone()
    result["distinct_eligible_keys"], result["distinct_keys_matched"] = distinct
    whole = data.db.execute(ctes + """
      SELECT count(*) AS distinct_source_values,
             count(*) FILTER (WHERE has_target) AS distinct_source_values_in_target
      FROM (
        SELECT DISTINCT s.ref, EXISTS (
          SELECT 1 FROM target_keys t WHERE t.ref = s.ref
        ) AS has_target
        FROM source_rows s
        WHERE s.ref IS NOT NULL AND s.ref <> '' AND trim(s.ref) <> ''
      )
    """, params).fetchone()
    result["distinct_source_values"], result["distinct_source_values_in_target"] = whole
    target_stats = data.db.execute(ctes + """
      SELECT coalesce(sum(multiplicity), 0),
             count(*),
             count(*) FILTER (WHERE multiplicity > 1),
             coalesce(max(multiplicity), 0)
      FROM target_keys
    """, params).fetchone()
    result["target_rows_with_complete_key"], result["target_distinct_keys"], result[
        "target_duplicate_key_groups"], result["target_max_multiplicity"] = target_stats
    examples = data.db.execute(ctes + """
      SELECT row_number, CASE WHEN multiplicity IS NULL THEN 'missing_in_input'
                              ELSE 'ambiguous_target' END AS reason
      FROM joined
      WHERE selected IS TRUE AND usable AND scope_usable
        AND (multiplicity IS NULL OR multiplicity > 1)
      ORDER BY row_number LIMIT ?
    """, [*params, sample_limit]).fetchall()
    result["counterexample_rows"] = [
        {"row_number": row, "reason": reason} for row, reason in examples]
    result["distinct_key_inclusion_ratio"] = _ratio(
        result["distinct_keys_matched"], result["distinct_eligible_keys"])
    result["whole_column_distinct_value_inclusion_ratio"] = _ratio(
        result["distinct_source_values_in_target"], result["distinct_source_values"])
    for numerator, name in (("matched_references", "reference_hit_ratio"),
                            ("unique_matches", "unique_match_ratio"),
                            ("ambiguous_matches", "ambiguous_match_ratio"),
                            ("missing_in_input", "missing_in_input_ratio")):
        result[name] = _ratio(result[numerator], result["eligible_references"])
    return {
        "candidate_id": candidate["candidate_id"],
        "snapshot_id": data.snapshot_id,
        "source": source, "target": target,
        "selector": selector, "scope_bindings": scope_bindings,
        "normalization": "identity", "scan_scope": "full_input",
        "checks": result,
        "decision": {"status": "checked", "semantic_relation": "unresolved"},
    }


def discover_and_check(data, options=None, progress=None):
    """Run bounded recall and exact checks; retain every unverified candidate.

    This stage does not create a RelationPlan. It can run without an LLM and
    reports which field samples and candidate checks were outside its budget.
    """
    options = options or {}
    allowed = ("max_candidates_total", "max_per_source_per_channel",
               "max_indexed_fields", "max_indexed_values_per_field",
               "max_value_length", "max_fields_per_common_value")
    fields = len(_fields(data))
    index_limit = options.get("max_indexed_fields", 64)
    total = fields + min(fields, index_limit) if isinstance(index_limit, int) and index_limit > 0 else None
    recall_task = progress.task("字段候选召回", total) if progress else nullcontext(None)
    with recall_task as stage:
        found = propose_candidates(data, **{key: options[key] for key in allowed
                                          if key in options},
                                   on_step=(lambda detail: stage.advance(detail=detail)) if stage else None,
                                   on_note=stage.note if stage else None)
    candidates = found["candidates"]
    max_validations = options.get("max_candidate_validations", 12)
    max_per_unit = options.get("max_validations_per_source_table", 3)
    if not isinstance(max_validations, int) or max_validations < 0:
        raise ValueError("max_candidate_validations must be nonnegative")
    if not isinstance(max_per_unit, int) or max_per_unit <= 0:
        raise ValueError("max_validations_per_source_table must be positive")

    def rank(candidate):
        channels = set(candidate["retrieval_channels"])
        return ("declared_fk" not in channels,
                candidate["numeric_overlap_only"],
                not {"metadata", "value_overlap"} <= channels,
                not candidate["target_declared_pk"],
                -candidate["shared_sample_value_count"],
                candidate["candidate_id"])

    by_source = defaultdict(list)
    for candidate in candidates:
        by_source[candidate["source"]["table"]].append(candidate)
    for values in by_source.values():
        values.sort(key=rank)
    ordered = []
    for index in range(max_per_unit):
        for table in sorted(by_source):
            if index < len(by_source[table]):
                ordered.append(by_source[table][index])
    selected = ordered[:max_validations]
    checks = []
    check_task = progress.task("字段关联核验", len(selected)) if progress else nullcontext(None)
    with check_task as stage:
        for candidate in selected:
            if stage:
                stage.note(f"{candidate['source']['table']}.{candidate['source']['field']} → "
                           f"{candidate['target']['table']}.{candidate['target']['field']}")
            try:
                checked = validate_candidate(data, candidate,
                                             sample_limit=options.get("max_counterexamples", 5))
                candidate["decision"] = checked["decision"]
                checks.append(checked)
            except Exception as exc:
                candidate["decision"] = {"status": "check_error",
                                         "semantic_relation": "unresolved"}
                checks.append({"candidate_id": candidate["candidate_id"],
                               "decision": candidate["decision"],
                               "error_type": type(exc).__name__})
            if stage:
                stage.advance(detail=candidate["candidate_id"])
    coverage = found["coverage"]
    omitted_fields = set(coverage["fields_not_value_indexed"])
    indexed = [(table, column) for table, column in _fields(data)
               if f"{table}.{column}" not in omitted_fields]
    logical_cells = (sum(data.tables[table]["rows"] for table, _ in indexed)
                     if all("rows" in data.tables[table] for table, _ in indexed)
                     else None)
    coverage.update({
        "value_index_strategy": "one DISTINCT plus MD5 ordering per selected field; each may scan its full input table",
        "value_index_field_scans": coverage["value_index_fields"],
        "value_index_logical_cells_lower_bound": logical_cells,
        "max_candidate_validations": max_validations,
        "max_validations_per_source_table": max_per_unit,
        "candidates_checked": sum(x["decision"]["status"] == "checked" for x in checks),
        "candidate_check_errors": sum(x["decision"]["status"] == "check_error" for x in checks),
        "candidates_not_attempted": len(candidates) - len(selected),
        "candidates_not_checked": sum(x["decision"]["status"] != "checked" for x in candidates),
        "candidate_ids_not_checked": [x["candidate_id"] for x in candidates
                                      if x["decision"]["status"] != "checked"],
        "candidate_scope": "raw_field_equality_only; selectors, JSON paths and semantic relation unverified",
    })
    coverage["partial"] = bool(
        coverage["fields_not_value_indexed"] or
        coverage["fields_with_truncated_value_samples"] or
        coverage["metadata_pairs_queued"] or coverage["value_pairs_queued"] or
        coverage["candidates_queued_global"] or
        coverage["candidates_not_checked"] or coverage["candidate_check_errors"])
    return {"candidates": candidates, "checks": checks, "coverage": coverage}
