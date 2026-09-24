"""Deterministic field-pair discovery and exact input-level checks.

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
    return tuple(x for x in re.split(r"[_\W]+", name.casefold()) if x)


def _stem(name):
    return tuple(x for x in _tokens(name) if x not in _GENERIC)


def _table_stem(info):
    tokens = list(_tokens(info.get("table_name", "")))
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
    target_label = (data.tables[target_table].get("table_name", target_table.rsplit(".", 1)[-1])
                    + "." + target_col).casefold()
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


def _indexable_field(data, field):
    """Keep the full-value index on keys and names, not descriptions or payloads."""
    table, column = field
    role = next((item for item in classify_columns(data.tables[table])
                 if item["column"] == column), {})
    tokens = set(_tokens(column))
    return bool(role.get("join_eligible") or
                tokens & {"id", "uuid", "guid"} or
                tokens & {"name", "alias", "synonym", "field", "ref", "key", "code"})


def _compatible_value_pair(data, source, target):
    """Value equality is worth recalling only for plausible field roles."""
    if source == target:
        return False
    if _metadata_pair(source, target, data):
        return True
    source_tokens, target_tokens = set(_tokens(source[1])), set(_tokens(target[1]))
    source_key = bool(source_tokens & {"id", "key", "code", "ref", "field"})
    target_key = bool(target_tokens & {"id", "key", "code", "ref", "field"})
    if source_key and target_key:
        return True
    return bool(source_tokens & {"name", "alias", "synonym"} and
                target_tokens & {"name", "alias", "synonym"})


def _full_value_pairs(data, fields, max_length, max_fanout, *, on_step=None,
                      on_note=None):
    """Build an on-disk DISTINCT dictionary; never load all values into Python.

    DuckDB groups every eligible value from the imported snapshot.  The only
    loss here is explicitly reported: blank/oversized values and values shared
    by too many fields.  Matching source rows are checked later by
    ``validate_candidate``; the dictionary is solely a recall structure.
    """
    index_name = qi("__r2_discovery_values")
    data.db.execute(f"DROP TABLE IF EXISTS {index_name}")
    data.db.execute(f"CREATE TEMP TABLE {index_name} (field_no INTEGER, value VARCHAR)")
    oversized = {}
    try:
        for field_no, (table, column) in enumerate(fields):
            if on_note is not None:
                on_note(f"完整 distinct {table}.{column} 查询中")
            sql_table, sql_column = _checked_field(data, table, column)
            value_sql = (f"{sql_column} IS NOT NULL AND {sql_column} <> '' "
                         f"AND trim({sql_column}) <> ''")
            oversized[f"{table}.{column}"] = data.db.execute(
                f"SELECT count(*) FROM (SELECT DISTINCT {sql_column} "
                f"FROM {sql_table} WHERE {value_sql} AND length({sql_column}) > ?)",
                [max_length]).fetchone()[0]
            data.db.execute(
                f"INSERT INTO {index_name} SELECT ?, {sql_column} "
                f"FROM {sql_table} WHERE {value_sql} AND length({sql_column}) <= ? "
                f"GROUP BY {sql_column}", [field_no, max_length])
            if on_step is not None:
                on_step(f"完整 distinct {table}.{column}")
        common = data.db.execute(
            f"SELECT count(*) FROM (SELECT value FROM {index_name} "
            f"GROUP BY value HAVING count(*) > ?)", [max_fanout]).fetchone()[0]
        matches = data.db.execute(f"""
            WITH usable AS (
              SELECT value FROM {index_name} GROUP BY value
              HAVING count(*) BETWEEN 2 AND ?
            )
            SELECT a.field_no, b.field_no, count(*) AS common_values,
                   bool_and(regexp_matches(a.value, '^[0-9]+$')) AS numeric_only
            FROM usable v
            JOIN {index_name} a ON a.value = v.value
            JOIN {index_name} b ON b.value = v.value AND a.field_no < b.field_no
            GROUP BY a.field_no, b.field_no
        """, [max_fanout]).fetchall()
        pairs = {}
        for left, right, count, numeric in matches:
            a, b = fields[left], fields[right]
            for source, target in ((a, b), (b, a)):
                if _compatible_value_pair(data, source, target):
                    pairs[(source, target)] = (count, bool(numeric))
        return pairs, common, oversized
    finally:
        data.db.execute(f"DROP TABLE IF EXISTS {index_name}")


def _conditioned_pairs(data, fields, max_values):
    """Recall type-discriminated references as *candidates*, not foreign keys.

    ``business_type/business_id`` and ``source_type/source_field`` are generic
    patterns.  Only discriminator literals observed in this snapshot are used;
    a matching table/column token is a recall hint, never a semantic decision.
    """
    available = set(fields)
    proposals = []
    truncated = []
    for table, type_field in fields:
        if not type_field.endswith("_type"):
            continue
        prefix = type_field[:-5]
        reference = next((name for name in (prefix + "_id", prefix + "_field")
                          if (table, name) in available), None)
        if reference is None:
            continue
        source_table, source_type = _checked_field(data, table, type_field)
        raw_values = data.db.execute(
            f"SELECT DISTINCT {source_type} FROM {source_table} "
            f"WHERE {source_type} IS NOT NULL AND trim({source_type}) <> '' "
            f"ORDER BY {source_type} LIMIT ?", [max_values + 1]).fetchall()
        if len(raw_values) > max_values:
            truncated.append(f"{table}.{type_field}")
        for (literal,) in raw_values[:max_values]:
            cue = re.match(r"[a-zA-Z]+", literal)
            if not cue:
                continue
            cue = cue.group().casefold()
            for target_table, info in sorted(data.tables.items()):
                target_tokens = _tokens(info.get("table_name", target_table.rsplit(".", 1)[-1]))
                if target_table == table or cue not in target_tokens:
                    continue
                columns = set(info["column_names"])
                if reference.endswith("_id"):
                    targets = info.get("pk") or []
                elif cue == "dim":
                    targets = [name for name in info["column_names"]
                               if name.endswith("_code") and
                               ("dim" in _tokens(name) or name == "member_code")]
                else:
                    targets = [name for name in info["column_names"]
                               if name == cue + "_code" or name == cue + "_name"]
                for target_field in targets:
                    if (target_table, target_field) not in available:
                        continue
                    scope_field = ("dim_code" if "dim_code" in columns else
                                   "dim_head_code" if "dim_head_code" in columns else None)
                    scope = ({type_field: scope_field}
                             if cue == "dim" and target_field == "member_code" and
                             scope_field else {})
                    proposals.append({
                        "source": (table, reference),
                        "target": (target_table, target_field),
                        "suggested_selector": {type_field: literal},
                        "suggested_scope_bindings": scope,
                        "target_role_priority": (0 if target_tokens[-1] == cue else
                                                 1 if target_tokens[-1] in
                                                 {"def", "definition", "detail"} else 2),
                    })
    return proposals, truncated


def propose_candidates(data, *, max_candidates_total=2000,
                       max_per_source_per_channel=20,
                       max_indexed_fields=64,
                       max_indexed_values_per_field=128,
                       max_value_length=96,
                       max_fields_per_common_value=32,
                       value_index_mode="sample", max_condition_values_per_field=64,
                       max_conditional_candidates=128,
                       on_step=None, on_note=None):
    """Recall directional field pairs from declarations, names and raw values.

    Returns ``{"candidates": [...], "coverage": {...}}``. The value index is
    sampled or fully distinct by field. High-fanout values are skipped. A
    result can be absent even when a real link exists; exact validation must
    precede use.
    """
    if value_index_mode not in ("sample", "full_distinct"):
        raise ValueError("value_index_mode must be sample or full_distinct")
    if not isinstance(max_indexed_fields, int) or max_indexed_fields < 0 or (
            value_index_mode == "sample" and max_indexed_fields == 0):
        raise ValueError("max_indexed_fields must be positive, or zero in full_distinct mode")
    limits = (max_candidates_total, max_per_source_per_channel,
              max_indexed_values_per_field, max_value_length,
              max_fields_per_common_value, max_condition_values_per_field,
              max_conditional_candidates)
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
    indexable = ([field for field in fields if _indexable_field(data, field)]
                 if value_index_mode == "full_distinct" else fields)
    by_table = defaultdict(list)
    for table, column in indexable:
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
    index_limit = max_indexed_fields or len(indexable)
    while len(indexed_fields) < min(index_limit, len(indexable)):
        added = False
        for table in sorted(by_table):
            if rank < len(by_table[table]):
                indexed_fields.append(by_table[table][rank][1])
                added = True
                if len(indexed_fields) >= index_limit:
                    break
        if not added:
            break
        rank += 1
    shared = {}
    truncated_fields = []
    oversized_values = {}
    if value_index_mode == "full_distinct":
        shared, high_fanout_values, oversized_values = _full_value_pairs(
            data, indexed_fields, max_value_length, max_fields_per_common_value,
            on_step=on_step, on_note=on_note)
    else:
        inverted = defaultdict(list)
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
        sampled = defaultdict(set)
        high_fanout_values = 0
        for value, holders in inverted.items():
            if len(holders) > max_fields_per_common_value:
                high_fanout_values += 1
                continue
            for left, right in combinations(holders, 2):
                sampled[(left, right)].add(value)
                sampled[(right, left)].add(value)
        shared = {pair: (len(values), bool(values) and
                         all(value.isdecimal() for value in values))
                  for pair, values in sampled.items()}
    overlap_by_source = defaultdict(list)
    for (source, target), summary in shared.items():
        overlap_by_source[source].append((target, summary))
    omitted_value_pairs = 0
    for source, targets in overlap_by_source.items():
        targets.sort(key=lambda item: (
            -item[1][0],
            item[0][1] not in data.tables[item[0][0]].get("pk", []),
            item[0]))
        for target, (count, numeric_only) in targets[:max_per_source_per_channel]:
            entry = proposals[(source, target)]
            entry["channels"].add("value_overlap")
            entry["shared_value_count"] = count
            entry["numeric_overlap_only"] = numeric_only
        omitted_value_pairs += max(0, len(targets) - max_per_source_per_channel)

    ranked = sorted(proposals, key=lambda pair: (
        pair not in declared,
        "metadata" not in proposals[pair]["channels"],
        -proposals[pair].get("shared_value_count", 0),
        pair))
    conditional, truncated_conditions = (
        _conditioned_pairs(data, fields, max_condition_values_per_field)
        if value_index_mode == "full_distinct" else ([], []))
    conditional.sort(key=lambda item: (
        item["source"], tuple(sorted(item["suggested_selector"].items())),
        item["target_role_priority"], item["target"]))
    retained_conditioned = conditional[:min(max_conditional_candidates, max_candidates_total)]
    retained = ranked[:max(0, max_candidates_total - len(retained_conditioned))]
    # Never silently lose a declared FK behind a global candidate budget.
    for pair in sorted(declared):
        if pair not in retained:
            retained.append(pair)
    candidates = []
    for item in retained_conditioned:
        source, target = item["source"], item["target"]
        selector = item["suggested_selector"]
        scope = item["suggested_scope_bindings"]
        candidates.append({
            "candidate_id": digest([source, target, selector, scope, data.snapshot_id])[:24],
            "source": {"table": source[0], "field": source[1]},
            "target": {"table": target[0], "field": target[1]},
            "retrieval_channels": ["conditioned_structure"],
            "suggested_selector": selector,
            "suggested_scope_bindings": scope,
            "shared_sample_value_count": shared.get((source, target), (0, False))[0],
            "shared_value_count_scope": value_index_mode,
            "numeric_overlap_only": shared.get((source, target), (0, False))[1],
            "target_declared_pk": target[1] in data.tables[target[0]].get("pk", []),
            "target_role_priority": item["target_role_priority"],
            "decision": {"status": "proposed", "semantic_relation": "unresolved"},
        })
    for source, target in retained:
        entry = proposals[(source, target)]
        count = entry.get("shared_value_count", 0)
        candidates.append({
            "candidate_id": digest([source, target, data.snapshot_id])[:24],
            "source": {"table": source[0], "field": source[1]},
            "target": {"table": target[0], "field": target[1]},
            "retrieval_channels": sorted(entry["channels"]),
            "shared_sample_value_count": count,
            "shared_value_count_scope": value_index_mode,
            "numeric_overlap_only": entry.get("numeric_overlap_only", False),
            "target_declared_pk": target[1] in data.tables[target[0]].get("pk", []),
            "decision": {"status": "proposed", "semantic_relation": "unresolved"},
        })
    indexed_set = set(indexed_fields)
    return {"candidates": candidates, "coverage": {
        "input_scope": "unknown", "scan_scope": ("full_input_for_selected_field_distinct"
                                           if value_index_mode == "full_distinct" else
                                           "full_input_for_selected_field_samples"),
        "fields_considered": len(all_fields),
        "candidate_fields_considered": len(fields),
        "candidate_fields_excluded_empty_or_audit": [f"{table}.{column}"
                                                      for table, column in sorted(excluded)],
        "value_index_fields": len(indexed_fields),
        "fields_not_value_indexed": [f"{t}.{c}" for t, c in indexable
                                     if (t, c) not in indexed_set],
        "fields_not_index_eligible": [f"{t}.{c}" for t, c in fields
                                      if (t, c) not in set(indexable)],
        "value_index_mode": value_index_mode,
        "oversized_distinct_values_by_field": {key: count for key, count in oversized_values.items()
                                               if count},
        "fields_with_truncated_value_samples": truncated_fields,
        "high_fanout_values_skipped": high_fanout_values,
        "metadata_pairs_queued": omitted_metadata,
        "value_pairs_queued": omitted_value_pairs,
        "candidates_queued_global": len(ranked) - len(retained),
        "condition_values_truncated": truncated_conditions,
        "conditional_candidates_queued": len(conditional) - len(retained_conditioned),
        "conditional_candidates_recalled": len(retained_conditioned),
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
               "max_value_length", "max_fields_per_common_value",
               "value_index_mode", "max_condition_values_per_field",
               "max_conditional_candidates")
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
        target = candidate["target"]
        table_info = data.tables[target["table"]]
        table_tokens = _tokens(table_info.get("table_name", target["table"].rsplit(".", 1)[-1]))
        target_aligned = bool(set(_stem(target["field"])) & set(table_tokens))
        target_definition = bool(table_tokens and table_tokens[-1] in
                                 {"def", "definition", "detail", "common"})
        return ("declared_fk" not in channels,
                "conditioned_structure" not in channels,
                candidate["numeric_overlap_only"],
                not {"metadata", "value_overlap"} <= channels,
                not target_aligned,
                not target_definition,
                not candidate["target_declared_pk"],
                -candidate["shared_sample_value_count"],
                candidate["candidate_id"])

    conditioned = [c for c in candidates if "conditioned_structure" in c["retrieval_channels"]]
    ordinary = [c for c in candidates if "conditioned_structure" not in c["retrieval_channels"]]
    # Give different observed discriminator branches a fair share before
    # spending the remaining budget on ordinary value/name pairs.
    conditioned_by_reference = defaultdict(lambda: defaultdict(list))
    for candidate in conditioned:
        source = candidate["source"]
        literal = next(iter(candidate["suggested_selector"].values()))
        cue = re.match(r"[a-zA-Z]+", literal)
        branch = cue.group().casefold() if cue else literal.casefold()
        conditioned_by_reference[(source["table"], source["field"])][branch].append(candidate)
    conditioned_order = []
    by_reference = {}
    for reference, branches in conditioned_by_reference.items():
        for values in branches.values():
            values.sort(key=lambda c: (
                -c["shared_sample_value_count"] if reference[1].endswith("_field") else 0,
                c.get("target_role_priority", 2), rank(c)))
        order = []
        for index in range(max((len(x) for x in branches.values()), default=0)):
            for branch in sorted(branches):
                if index < len(branches[branch]):
                    order.append(branches[branch][index])
        by_reference[reference] = order
    reference_order = sorted(by_reference, key=lambda ref: (
        not ref[1].endswith("_field"), ref))
    for index in range(max((len(x) for x in by_reference.values()), default=0)):
        for reference in reference_order:
            if index < len(by_reference[reference]):
                conditioned_order.append(by_reference[reference][index])
    reserved = min(len(conditioned_order), max(1, max_validations // 3)) if max_validations else 0
    ordinary_budget = max_validations - reserved
    pending = list(ordinary)
    ordered = []
    source_table_uses = defaultdict(int)
    source_fields_used = set()
    while pending and len(ordered) < ordinary_budget:
        eligible = [c for c in pending
                    if source_table_uses[c["source"]["table"]] < max_per_unit]
        if not eligible:
            break
        candidate = min(eligible, key=lambda c: (
            rank(c)[:4],
            (c["source"]["table"], c["source"]["field"]) in source_fields_used,
            rank(c)[4:-1], source_table_uses[c["source"]["table"]],
            c["candidate_id"]))
        pending.remove(candidate)
        ordered.append(candidate)
        source_table_uses[candidate["source"]["table"]] += 1
        source_fields_used.add((candidate["source"]["table"], candidate["source"]["field"]))
    selected = [*conditioned_order[:reserved], *ordered]
    if len(selected) < max_validations:
        selected.extend(conditioned_order[reserved:reserved + max_validations - len(selected)])
    checks = []
    check_task = progress.task("字段关联核验", len(selected)) if progress else nullcontext(None)
    with check_task as stage:
        for candidate in selected:
            if stage:
                stage.note(f"{candidate['source']['table']}.{candidate['source']['field']} → "
                           f"{candidate['target']['table']}.{candidate['target']['field']}")
            try:
                checked = validate_candidate(data, candidate,
                                             selector=candidate.get("suggested_selector"),
                                             scope_bindings={target: source for source, target in
                                                             candidate.get("suggested_scope_bindings", {}).items()},
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
        "value_index_strategy": ("complete DISTINCT of eligible nonblank values within "
                                 "max_value_length in DuckDB, then exact row-level joins "
                                 "for selected candidates"
                                 if coverage["value_index_mode"] == "full_distinct" else
                                 "one DISTINCT plus MD5 ordering per selected field; each may scan its full input table"),
        "max_value_length": options.get("max_value_length", 96),
        "value_index_field_scans": coverage["value_index_fields"],
        "value_index_logical_cells_lower_bound": logical_cells,
        "max_candidate_validations": max_validations,
        "max_validations_per_source_table": max_per_unit,
        "candidates_checked": sum(x["decision"]["status"] == "checked" for x in checks),
        "conditioned_candidates_checked": sum(
            x["decision"]["status"] == "checked" and
            "conditioned_structure" in next((c["retrieval_channels"] for c in candidates
                                              if c["candidate_id"] == x["candidate_id"]), [])
            for x in checks),
        "candidate_check_errors": sum(x["decision"]["status"] == "check_error" for x in checks),
        "candidates_not_attempted": len(candidates) - len(selected),
        "candidates_not_checked": sum(x["decision"]["status"] != "checked" for x in candidates),
        "candidate_ids_not_checked": [x["candidate_id"] for x in candidates
                                      if x["decision"]["status"] != "checked"],
        "candidate_scope": ("raw_field_equality_only; observed discriminator literals and suggested "
                            "scope are exact-checked where selected; JSON paths and semantic relation unverified"),
    })
    coverage["partial"] = bool(
        coverage["fields_not_value_indexed"] or
        coverage["oversized_distinct_values_by_field"] or
        coverage["fields_with_truncated_value_samples"] or
        coverage["condition_values_truncated"] or
        coverage["conditional_candidates_queued"] or
        coverage["metadata_pairs_queued"] or coverage["value_pairs_queued"] or
        coverage["candidates_queued_global"] or
        coverage["candidates_not_checked"] or coverage["candidate_check_errors"])
    return {"candidates": candidates, "checks": checks, "coverage": coverage}
