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
_NUMERIC_LITERAL = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?\Z")


def _numeric_literal(value):
    return bool(_NUMERIC_LITERAL.fullmatch(value))


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


def _value_index_eligibility(data, field, max_length):
    """Use observed shape to exclude clear payloads, not unfamiliar column names.

    Missing/incomplete profiles stay eligible: absence of profile evidence must
    not silently turn an opaque business key into an excluded field. The
    expensive DISTINCT index is still bounded by the caller's field budget.
    """
    table, column = field
    info = data.tables[table]
    role = next((item for item in classify_columns(info)
                 if item["column"] == column), {})
    tokens = set(_tokens(column))
    named_key = bool(role.get("join_eligible") or
                     tokens & {"id", "uuid", "guid", "key", "code", "ref"})
    profile = next((item for item in info.get("profiles", ())
                    if item.get("column") == column), None)
    if not profile or profile.get("scan_scope") != "full_input":
        return True, "profile_unavailable_or_incomplete"
    usable = profile.get("usable_count")
    if not isinstance(usable, int) or usable <= 0:
        return True, "value_shape_unknown"
    shortest = profile.get("min_chars_usable")
    if isinstance(shortest, int) and shortest > max_length:
        return False, "all_values_exceed_index_length"
    if not named_key:
        longest = profile.get("max_chars_usable")
        short = profile.get("short_text_count")
        if (isinstance(longest, int) and longest > max_length
                and isinstance(short, int) and short / usable < 0.05):
            return False, "predominantly_long_text"
        # A high-cardinality integer may be an undocumented business code.
        # Keep it in bounded value recall and mark numeric coincidence as risk.
    return True, "short_or_unknown_value_shape"


def _compatible_value_pair(data, source, target, *, nonnumeric_shared=False):
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
    if source_tokens & {"name", "alias", "synonym"} and target_tokens & {"name", "alias", "synonym"}:
        return True
    # Distinct, nonnumeric raw values can recall differently named fields.
    # This is only a technical lead; validate_candidate checks full rows and
    # leaves its semantic_relation unresolved.
    return source[0] != target[0]


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
                   bool_and(regexp_full_match(a.value, '[+-]?[0-9]+(\\.[0-9]+)?')) AS numeric_only
            FROM usable v
            JOIN {index_name} a ON a.value = v.value
            JOIN {index_name} b ON b.value = v.value AND a.field_no < b.field_no
            GROUP BY a.field_no, b.field_no
        """, [max_fanout]).fetchall()
        pairs = {}
        for left, right, count, numeric in matches:
            a, b = fields[left], fields[right]
            for source, target in ((a, b), (b, a)):
                if _compatible_value_pair(data, source, target,
                                          nonnumeric_shared=not bool(numeric)):
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


def _fair_source_order(items, source_of, quality):
    """Interleave source tables and fields before applying a global budget."""
    tables = defaultdict(lambda: defaultdict(list))
    for item in items:
        table, field = source_of(item)
        tables[table][field].append(item)
    for fields in tables.values():
        for values in fields.values():
            values.sort(key=quality, reverse=True)
    uses = defaultdict(int)
    ordered = []
    while tables:
        for table in sorted(list(tables)):
            fields = tables[table]
            field = min(fields, key=lambda f: (uses[(table, f)], quality(fields[f][-1]), f))
            ordered.append(fields[field].pop())
            uses[(table, field)] += 1
            if not fields[field]:
                del fields[field]
            if not fields:
                del tables[table]
    return ordered


def _conditioned_order(items, shared):
    """Cover source, discriminator branch and target table before variants.

    Existing value evidence orders families within each branch; it does not
    validate the branch or erase the original selector, scope or risk flags.
    """
    grouped = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    def quality(item):
        count = shared.get((item['source'], item['target']), (0, False))[0]
        evidence = item.get('condition_evidence', {})
        rows, matches = evidence.get('branch_rows', 0), evidence.get('branch_matches', 0)
        return (not bool(count or matches), -(matches / rows if rows else 0),
                -count, item['target_role_priority'], item['target'])
    for item in items:
        branch = tuple(sorted(item['suggested_selector'].items()))
        grouped[item['source']][branch][item['target'][0]].append(item)
    local_rank = {}
    for branches in grouped.values():
        branch_queues = {}
        for branch, families in branches.items():
            for values in families.values():
                values.sort(key=quality)
            family_order = sorted(families, key=lambda key: (quality(families[key][0]), key))
            queue = []
            # A second field of one target table cannot consume another table's turn.
            for offset in range(max(map(len, families.values()), default=0)):
                queue.extend(families[key][offset] for key in family_order
                             if offset < len(families[key]))
            branch_queues[branch] = queue
        rank = 0
        for offset in range(max(map(len, branch_queues.values()), default=0)):
            for branch in sorted(branch_queues):
                if offset < len(branch_queues[branch]):
                    local_rank[id(branch_queues[branch][offset])] = rank
                    rank += 1
    return _fair_source_order(items, lambda item: item['source'],
                              lambda item: (local_rank[id(item)],))


def _conditional_selection_coverage(items, selected):
    def groups(values):
        sources, branches, families = set(), set(), set()
        for item in values:
            source = item['source']
            branch = (source, tuple(sorted(item['suggested_selector'].items())))
            sources.add(source)
            branches.add(branch)
            families.add((branch, item['target'][0]))
        return sources, branches, families
    full, kept = groups(items), groups(selected)
    return {'policy': 'source_discriminator_branch_target_table_round_robin',
            **{name: {'available': len(all_), 'selected': len(some),
                      'queued': len(all_ - some)}
               for name, all_, some in zip(('source_fields', 'selector_branches',
                                           'target_families'), full, kept)}}


def _value_conditioned_pairs(data, fields, shared, *, max_values, max_pairs=64,
                             max_selector_fields=4):
    """Find discriminator branches from observed containment, without a literal vocabulary.

    Candidate pairs come from the inverted value index, not all field products.
    A branch is proposed only when its hit rate exceeds the remaining rows.
    Numeric/Chinese discriminators are treated as opaque original literals.
    """
    fields_by_table = defaultdict(list)
    for table, field in fields:
        fields_by_table[table].append(field)
    selectors, selector_omissions = {}, []
    for table, names in fields_by_table.items():
        info = data.tables[table]
        choices = []
        profiles = {p['column']: p for p in info.get('profiles', [])}
        for field in names:
            if field in info.get('pk', []):
                continue
            profile = profiles.get(field, {})
            approximate = profile.get('approx_distinct_usable')
            if isinstance(approximate, (int, float)) and approximate > max_values * 2:
                continue
            st, sf = _checked_field(data, table, field)
            values = data.db.execute(f"SELECT DISTINCT {sf} FROM {st} WHERE {sf} IS NOT NULL "
                                     f"AND trim({sf}) <> '' LIMIT ?", [max_values + 1]).fetchall()
            if 2 <= len(values) <= max_values:
                usable = profile.get('usable_count')
                if type(usable) is not int:
                    usable = data.db.execute(f"SELECT count(*) FROM {st} WHERE {sf} IS NOT NULL AND trim({sf}) <> ''").fetchone()[0]
                # A unique ID/value would select individual rows, not a reusable
                # discriminator. Explicit type names remain bounded hints.
                if len(values) < usable or set(_tokens(field)) & {'type', 'kind', 'category'}:
                    choices.append(field)
        choices.sort(key=lambda name: (not bool(set(_tokens(name)) & {'type', 'kind', 'category'}), name))
        selectors[table] = choices[:max_selector_fields]
        selector_omissions.extend(f'{table}.{field}' for field in choices[max_selector_fields:])
    ranked = _fair_source_order(
        [(source, target) for source, target in shared
         if source[0] != target[0] and selectors[source[0]]], lambda pair: pair[0],
        lambda pair: (pair[0][1] in data.tables[pair[0][0]].get('pk', []),
                      -shared[pair][0], pair))
    results = []
    for source, target in ranked[:max_pairs]:
        st, sk = _checked_field(data, *source)
        tt, tk = _checked_field(data, *target)
        for discriminator in selectors[source[0]]:
            if discriminator == source[1]:
                continue
            rows = data.db.execute(f"""WITH keys AS (
                SELECT DISTINCT {tk} AS ref FROM {tt} WHERE {tk} IS NOT NULL AND trim({tk}) <> ''
            ) SELECT s.{qi(discriminator)}, count(*), count(t.ref)
              FROM {st} s LEFT JOIN keys t ON s.{sk}=t.ref
              WHERE s.{sk} IS NOT NULL AND trim(s.{sk}) <> ''
              GROUP BY s.{qi(discriminator)}""").fetchall()
            total, matches = sum(r[1] for r in rows), sum(r[2] for r in rows)
            for literal, count, matched in rows:
                outside, outside_match = total - count, matches - matched
                if (literal is None or not str(literal).strip() or not matched or not outside
                        or matched / count <= outside_match / outside):
                    continue
                results.append({'source': source, 'target': target,
                    'suggested_selector': {discriminator: literal}, 'suggested_scope_bindings': {},
                    'target_role_priority': 1, 'condition_discovery': 'observed_value_containment',
                    'condition_evidence': {'branch_rows': count, 'branch_matches': matched,
                                           'outside_rows': outside, 'outside_matches': outside_match}})
    return results, {'conditional_pair_checks': min(len(ranked), max_pairs),
                     'conditional_pairs_not_probed': max(0, len(ranked)-max_pairs),
                     'conditional_selector_fields_not_probed': selector_omissions}


def propose_candidates(data, *, max_candidates_total=2000,
                       max_per_source_per_channel=20,
                       max_indexed_fields=64,
                       max_indexed_values_per_field=128,
                       max_value_length=96,
                       max_fields_per_common_value=32,
                       value_index_mode="sample", max_condition_values_per_field=64,
                       max_conditional_candidates=128, max_conditional_pair_checks=64,
                       max_condition_fields_per_table=4,
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
              max_conditional_candidates, max_conditional_pair_checks, max_condition_fields_per_table)
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

    # Blocking indexes avoid an all-fields squared metadata comparison. Only
    # exact/stem blocks and explicitly mentioned qualified targets are checked.
    by_stem, by_name, generic_by_table = defaultdict(set), defaultdict(set), defaultdict(set)
    qualified = defaultdict(set)
    for target in fields:
        by_stem[_stem(target[1])].add(target)
        by_name[target[1].casefold()].add(target)
        info = data.tables[target[0]]
        qualified[(info.get("table_name", target[0].rsplit(".", 1)[-1]) + "." + target[1]).casefold()].add(target)
        qualified[(target[0] + "." + target[1]).casefold()].add(target)
        if _tokens(target[1]) in (("id",), ("code",), ("key",), ("uuid",)):
            name = _table_stem(info)
            generic_by_table[name].add(target)
            generic_by_table[name[:-1]].add(target)
    metadata_by_source = defaultdict(list)
    for source in fields:
        stem = _stem(source[1])
        block = set(by_stem.get(stem, ())) if stem else set()
        block.update(by_name.get(source[1].casefold(), ()))
        if stem:
            block.update(generic_by_table.get(stem, ()))
        comment = next((c.get("column_comment") or "" for c in data.tables[source[0]].get("columns", [])
                        if c.get("column_name") == source[1]), "").casefold()
        for ref in re.findall(r"\w+(?:\.\w+){1,2}", comment):
            if ref in qualified:
                block.update(qualified[ref])
        metadata_by_source[source] = [target for target in block
                                      if _metadata_pair(source, target, data)]
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
    eligibility = ({field: _value_index_eligibility(data, field, max_value_length)
                    for field in fields} if value_index_mode == "full_distinct" else {})
    indexable = ([field for field in fields if eligibility[field][0]]
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
                         all(_numeric_literal(value) for value in values))
                  for pair, values in sampled.items()
                  if _compatible_value_pair(data, pair[0], pair[1],
                                            nonnumeric_shared=any(not _numeric_literal(value)
                                                                  for value in values))}
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
    learned, learned_coverage = (_value_conditioned_pairs(
        data, fields, shared, max_values=max_condition_values_per_field,
        max_pairs=max_conditional_pair_checks, max_selector_fields=max_condition_fields_per_table)
        if value_index_mode == "full_distinct" else ([], {}))
    seen_conditions = {(item["source"], item["target"], tuple(sorted(item["suggested_selector"].items())))
                       for item in conditional}
    conditional.extend(item for item in learned if
        (item["source"], item["target"], tuple(sorted(item["suggested_selector"].items()))) not in seen_conditions)
    conditional = _conditioned_order(conditional, shared)
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
            "risk_flags": (["numeric_value_coincidence"] if shared.get((source, target), (0, False))[1] else []),
            "target_declared_pk": target[1] in data.tables[target[0]].get("pk", []),
            "target_role_priority": item["target_role_priority"],
            "condition_discovery": item.get("condition_discovery", "name_hint_and_observed_literal"),
            "condition_evidence": item.get("condition_evidence", {}),
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
            "risk_flags": (["numeric_value_coincidence"] if entry.get("numeric_overlap_only") else []),
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
        "value_index_exclusion_reasons": {
            f"{table}.{column}": eligibility[(table, column)][1]
            for table, column in fields
            if eligibility and not eligibility[(table, column)][0]},
        "value_index_eligibility_policy": (
            "all nonempty nonsensitive nonaudit fields unless complete profile proves "
            "oversized text; numeric coincidences remain risk-marked candidates"
            if value_index_mode == "full_distinct" else "sampled field budget"),
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
        "conditional_selection": _conditional_selection_coverage(conditional, retained_conditioned),
        **learned_coverage,
        "candidate_count": len(candidates),
    }}


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


TRANSFORMS = {"identity", "nfkc_whitespace_casefold", "source_alias_items",
              "target_alias_items", "both_alias_items"}

# Python str.strip() also removes control and Unicode whitespace which DuckDB's
# one-argument trim does not. This controlled literal only tests emptiness: the
# identity key itself remains the complete, unmodified source string.
_PYTHON_STRIP_CHARACTERS = ("\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
                            "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
                            "\u2028\u2029\u202f\u205f\u3000")


def _match_keys(value, operator):
    """The executable meaning of the allowlisted transform DSL."""
    from .value_aliases import _forms
    if value is None:
        return []
    if operator == "identity":
        return [value] if value.strip() else []
    return sorted({part["canonical"] for part in _forms(value, operator == "alias_items")})


def _matching_ctes(data, candidate, selector=None, scope_bindings=None, transform="identity"):
    """Build fixed SQL; caller inputs select fields/literals, never SQL text.

    scope_bindings uses target->source as in validate_candidate. Multiple alias
    tokens matching one target row count once; two target rows remain ambiguous.
    """
    if transform not in TRANSFORMS:
        raise ValueError("Unsupported association transform")
    selector, scope_bindings = selector or {}, scope_bindings or {}
    if not isinstance(selector, dict) or not isinstance(scope_bindings, dict):
        raise ValueError("selector and scope_bindings must be mappings")
    source, target = candidate["source"], candidate["target"]
    st, sk = _checked_field(data, source["table"], source["field"])
    tt, tk = _checked_field(data, target["table"], target["field"])
    for field, literal in selector.items():
        _checked_field(data, source["table"], field)
        if literal is not None and not isinstance(literal, str):
            raise ValueError("selector values must be strings or null")
    pairs = sorted(scope_bindings.items())
    for target_col, source_col in pairs:
        _checked_field(data, source["table"], source_col)
        _checked_field(data, target["table"], target_col)
    if transform != "identity" and not data.db.execute(
            "SELECT count(*) FROM duckdb_functions() WHERE function_name='__r2_match_keys'").fetchone()[0]:
        data.db.create_function("__r2_match_keys", _match_keys,
                                ["VARCHAR", "VARCHAR"], "VARCHAR[]")
    source_op = "alias_items" if transform in {"source_alias_items", "both_alias_items"} else (
        "identity" if transform == "identity" else "normalized")
    target_op = "alias_items" if transform in {"target_alias_items", "both_alias_items"} else (
        "identity" if transform == "identity" else "normalized")
    if transform == "identity":
        # Avoid Python list conversion for the common equality-only path.
        tokens = {side: f"SELECT *, ref AS key FROM {side}_values "
                        f"WHERE trim(ref, '{_PYTHON_STRIP_CHARACTERS}') <> ''"
                  for side in ("source", "target")}
    else:
        tokens = {side: f"SELECT *, unnest(__r2_match_keys(ref, '{operator}')) AS key FROM {side}_values"
                  for side, operator in (("source", source_op), ("target", target_op))}
    source_scope = ''.join(f', s.{qi(sc)} AS scope_{i}' for i, (_, sc) in enumerate(pairs))
    target_scope = ''.join(f', t.{qi(tc)} AS scope_{i}' for i, (tc, _) in enumerate(pairs))
    source_usable = ' AND '.join(f"s.{qi(sc)} IS NOT NULL AND trim(s.{qi(sc)}) <> ''"
                                 for _, sc in pairs) or 'TRUE'
    target_usable = ' AND '.join(f"t.{qi(tc)} IS NOT NULL AND trim(t.{qi(tc)}) <> ''"
                                 for tc, _ in pairs) or 'TRUE'
    selected = ' AND '.join(f's.{qi(col)} = ?' for col in selector) or 'TRUE'
    scope_group = ''.join(f', scope_{i}' for i in range(len(pairs)))
    join_scope = ''.join(f' AND s.scope_{i} = t.scope_{i}' for i in range(len(pairs)))
    source_groups = ''.join(f', s.scope_{i}' for i in range(len(pairs)))
    join_match_scope = ''.join(f' AND s.scope_{i} = m.scope_{i}' for i in range(len(pairs)))
    sql = f"""WITH source_rows AS (
        SELECT s.__r2_row AS row_number, s.{sk} AS ref,
               ({selected}) AS selected, ({source_usable}) AS scope_usable,
               (s.{sk} IS NOT NULL AND trim(s.{sk}) <> '') AS usable {source_scope}
        FROM {st} s
    ), target_rows AS (
        SELECT t.__r2_row AS row_number, t.{tk} AS ref,
               ({target_usable}) AS scope_usable {target_scope} FROM {tt} t
    ), source_values AS MATERIALIZED (
        SELECT DISTINCT ref {scope_group}, TRUE AS usable, TRUE AS scope_usable
        FROM source_rows WHERE usable AND scope_usable
    ), target_values AS MATERIALIZED (
        SELECT ref {scope_group}, count(*) AS multiplicity, min(row_number) AS row_number
        FROM target_rows WHERE scope_usable AND ref IS NOT NULL AND trim(ref) <> ''
        GROUP BY ref {scope_group}
    ), source_tokens AS (
        {tokens['source']}
    ), target_tokens AS (
        {tokens['target']}
    ), target_keys AS (
        SELECT key {scope_group}, sum(multiplicity) AS multiplicity,
               min(row_number) AS target_row_number
        FROM target_tokens WHERE key <> '' GROUP BY key {scope_group}
    ), value_matches AS (
        SELECT s.ref {source_groups},
               CASE WHEN max(t.multiplicity) > 1 THEN 2
                    ELSE nullif(count(DISTINCT t.target_row_number), 0) END AS multiplicity,
               min(t.target_row_number) AS matched_target_row
        FROM source_tokens s LEFT JOIN target_keys t ON s.key = t.key {join_scope}
        GROUP BY s.ref {source_groups}
    ), joined AS (
        SELECT s.*, m.multiplicity, m.matched_target_row FROM source_rows s
        LEFT JOIN value_matches m ON s.ref = m.ref {join_match_scope}
    ), links AS (
        SELECT row_number AS source_row_number, matched_target_row AS target_row_number
        FROM joined WHERE multiplicity = 1
    ) """
    return sql, list(selector.values()), scope_group


def association_match_sql(data, candidate, *, selector=None, scope_bindings=None,
                          transform="identity"):
    """Return a bounded-consumer-compatible SQL query for unique checked links.

    Returns ``(sql, parameters)`` with source_row_number,target_row_number.
    Consumer may append ORDER BY/LIMIT or paginate original source row numbers.
    Ambiguous targets are excluded. No semantic relation is implied.
    """
    ctes, params, _ = _matching_ctes(data, candidate, selector, scope_bindings, transform)
    return ctes + """SELECT l.source_row_number, l.target_row_number
        FROM links l JOIN joined s ON s.row_number = l.source_row_number
        WHERE s.selected IS TRUE AND s.multiplicity = 1""", params


def _validate_transformed_candidate(data, candidate, *, selector, scope_bindings,
                                    sample_limit, transform):
    ctes, params, scope_group = _matching_ctes(data, candidate, selector, scope_bindings, transform)
    names = ("source_rows", "selector_true", "selector_false", "selector_unknown",
             "null_references", "empty_references", "whitespace_references", "missing_scope",
             "eligible_references", "matched_references", "unique_matches", "ambiguous_matches",
             "missing_in_input", "outside_eligible_references", "outside_matched_references")
    conditions = [None, 'selected IS TRUE', 'selected IS FALSE', 'selected IS NULL',
        'selected IS TRUE AND ref IS NULL', "selected IS TRUE AND ref = ''",
        "selected IS TRUE AND ref <> '' AND trim(ref) = ''",
        'selected IS TRUE AND usable AND NOT scope_usable',
        'selected IS TRUE AND usable AND scope_usable',
        'selected IS TRUE AND usable AND scope_usable AND multiplicity > 0',
        'selected IS TRUE AND usable AND scope_usable AND multiplicity = 1',
        'selected IS TRUE AND usable AND scope_usable AND multiplicity > 1',
        'selected IS TRUE AND usable AND scope_usable AND multiplicity IS NULL',
        'selected IS FALSE AND usable AND scope_usable',
        'selected IS FALSE AND usable AND scope_usable AND multiplicity > 0']
    aggregates = ', '.join('count(*)' + (f' FILTER (WHERE {c})' if c else '') for c in conditions)
    result = dict(zip(names, data.db.execute(ctes + 'SELECT ' + aggregates + ' FROM joined', params).fetchone()))
    distinct = data.db.execute(ctes + f"""SELECT count(*), count(*) FILTER (WHERE multiplicity > 0)
        FROM (SELECT DISTINCT ref{scope_group}, multiplicity FROM joined
              WHERE selected IS TRUE AND usable AND scope_usable)""", params).fetchone()
    result['distinct_eligible_keys'], result['distinct_keys_matched'] = distinct
    whole = data.db.execute(ctes + """SELECT count(*), count(*) FILTER (WHERE matched)
        FROM (SELECT ref, bool_or(EXISTS(SELECT 1 FROM target_tokens t WHERE t.key=s.key)) AS matched
              FROM source_tokens s WHERE usable GROUP BY ref)""", params).fetchone()
    result['distinct_source_values'], result['distinct_source_values_in_target'] = whole
    target = data.db.execute(ctes + """SELECT
        (SELECT count(*) FROM target_rows WHERE scope_usable AND ref IS NOT NULL AND trim(ref) <> ''),
        count(*), count(*) FILTER (WHERE multiplicity > 1), coalesce(max(multiplicity), 0)
        FROM target_keys""", params).fetchone()
    for name, value in zip(('target_rows_with_complete_key', 'target_distinct_keys',
                            'target_duplicate_key_groups', 'target_max_multiplicity'), target):
        result[name] = value
    examples = data.db.execute(ctes + """SELECT row_number,
        CASE WHEN multiplicity IS NULL THEN 'missing_in_input' ELSE 'ambiguous_target' END
        FROM joined WHERE selected IS TRUE AND usable AND scope_usable
        AND (multiplicity IS NULL OR multiplicity > 1) ORDER BY row_number LIMIT ?""",
        [*params, sample_limit]).fetchall()
    result['counterexample_rows'] = [{'row_number': row, 'reason': reason} for row, reason in examples]
    result['distinct_key_inclusion_ratio'] = _ratio(result['distinct_keys_matched'], result['distinct_eligible_keys'])
    result['whole_column_distinct_value_inclusion_ratio'] = _ratio(result['distinct_source_values_in_target'], result['distinct_source_values'])
    result['unique_match_ratio'] = _ratio(result['unique_matches'], result['eligible_references'])
    return {'candidate_id': candidate['candidate_id'], 'snapshot_id': data.snapshot_id,
            'source': candidate['source'], 'target': candidate['target'],
            'selector': selector or {}, 'scope_bindings': scope_bindings or {},
            'normalization': transform, 'scan_scope': 'full_input', 'checks': result,
            'decision': {'status': 'checked', 'semantic_relation': 'unresolved'}}


def validate_candidate(data, candidate, *, selector=None, scope_bindings=None,
                       sample_limit=5, transform="identity"):
    """Exactly check one raw-value field pair over the imported CSV snapshot.

    ``selector`` is a mapping of source fields to literal values, combined by
    SQL three-valued AND. ``scope_bindings`` maps target columns to source
    columns. Identity comparison only; no transformation or semantic claim.
    """
    if type(sample_limit) is not int or sample_limit < 0:
        raise ValueError("sample_limit must be nonnegative")
    if transform != "identity":
        return _validate_transformed_candidate(data, candidate, selector=selector,
            scope_bindings=scope_bindings, sample_limit=sample_limit, transform=transform)
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
               "max_conditional_candidates", "max_conditional_pair_checks",
               "max_condition_fields_per_table")
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
    # Recall already interleaves complete selectors and target families. Keep
    # that order: grouping literals by an alphabetic prefix would collapse
    # distinct branches such as A01/A02 and undo bounded recall coverage.
    conditioned_order = conditioned
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
        coverage["fields_not_value_indexed"] or coverage["fields_not_index_eligible"] or
        coverage["high_fanout_values_skipped"] or
        coverage["oversized_distinct_values_by_field"] or
        coverage["fields_with_truncated_value_samples"] or
        coverage["condition_values_truncated"] or
        coverage.get("conditional_pairs_not_probed") or
        coverage.get("conditional_selector_fields_not_probed") or
        coverage["conditional_candidates_queued"] or
        coverage["metadata_pairs_queued"] or coverage["value_pairs_queued"] or
        coverage["candidates_queued_global"] or
        coverage["candidates_not_checked"] or coverage["candidate_check_errors"])
    return {"candidates": candidates, "checks": checks, "coverage": coverage}
