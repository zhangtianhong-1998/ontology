"""Bounded field summaries over the imported CSV snapshot.

The CSV syntax can distinguish unquoted blanks from quoted empty strings. It
cannot establish whether either form was a database NULL before export.
"""

from __future__ import annotations

from pathlib import Path


MAX_COLUMNS_PER_GROUP = 32
SAMPLE_ROWS = 2048
SAMPLE_VALUES = 16
WHITESPACE_POLICY = "ascii_whitespace_nbsp_ideographic_space_v1"
_TRIM_CHARS = "' ' || chr(9) || chr(10) || chr(11) || chr(12) || chr(13) || chr(160) || chr(12288)"


def _identifier(name):
    return '"' + name.replace('"', '""') + '"'


def _stat(value, *, exact=True, method="count", population="all_input_rows", rows_scanned=0):
    return {"value": value, "exact": exact, "method": method,
            "population": population, "rows_scanned": rows_scanned, "status": "complete"}


def _column_expressions(column):
    c = _identifier(column)
    usable = f"{c} IS NOT NULL AND length(trim({c}, {_TRIM_CHARS})) > 0"
    stripped = f"ltrim({c}, {_TRIM_CHARS})"
    clauses = {
        "null_count": f"count(*) FILTER (WHERE {c} IS NULL)",
        "empty_count": f"count(*) FILTER (WHERE {c} = '')",
        "whitespace_only_count": f"count(*) FILTER (WHERE {c} IS NOT NULL AND {c} <> '' AND length(trim({c}, {_TRIM_CHARS})) = 0)",
        "usable_count": f"count(*) FILTER (WHERE {usable})",
        "non_null": f"count({c})",
        # Retained for the existing profiles.yaml/context contract.
        "approx_distinct": f"approx_count_distinct({c})",
        "approx_distinct_usable": f"approx_count_distinct({c}) FILTER (WHERE {usable})",
        "min_chars_usable": f"min(length({c})) FILTER (WHERE {usable})",
        "max_chars_usable": f"max(length({c})) FILTER (WHERE {usable})",
        "min_bytes_usable": f"min(octet_length(encode({c}))) FILTER (WHERE {usable})",
        "max_bytes_usable": f"max(octet_length(encode({c}))) FILTER (WHERE {usable})",
        "numeric_shape_count": f"count(*) FILTER (WHERE {usable} AND regexp_full_match({c}, '[+-]?[0-9]+(\\.[0-9]+)?'))",
        "leading_zero_shape_count": f"count(*) FILTER (WHERE {usable} AND regexp_full_match({c}, '0[0-9]+'))",
        "uuid_shape_count": f"count(*) FILTER (WHERE {usable} AND regexp_full_match({c}, '[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}'))",
        "json_start_count": f"count(*) FILTER (WHERE {usable} AND left({stripped}, 1) IN ('{{', '['))",
        "short_text_count": f"count(*) FILTER (WHERE {usable} AND length({c}) <= 64)",
    }
    return clauses


def profile_fields(data, config=None, on_group=None, on_group_start=None):
    """Profile every imported field; one aggregate and one bounded sample per group.

    Returns the legacy table -> list-of-fields shape, with additional P01
    statistics. All counts cover the imported CSV, not an unobserved database.
    """
    config = config or {}
    requested = int(config.get("sql_columns_per_group", MAX_COLUMNS_PER_GROUP))
    if requested <= 0:
        raise ValueError("sql_columns_per_group must be positive")
    group_size = min(requested, MAX_COLUMNS_PER_GROUP)
    input_scope = config.get("input_scope", "unknown")
    if input_scope not in ("unknown", "sample", "complete_export"):
        raise ValueError("input_scope must be unknown, sample, or complete_export")
    result = {}
    for table_name, table in data.tables.items():
        columns = table["column_names"]
        sql_table = _identifier(table["sql_name"])
        profiles = []
        for start in range(0, len(columns), group_size):
            group = columns[start:start + group_size]
            if on_group_start is not None:
                on_group_start(table_name, start, start + len(group))
            measures = ["count(*)"]
            names = []
            for column in group:
                expressions = _column_expressions(column)
                names.append(tuple(expressions))
                measures.extend(expressions.values())
            row = data.db.execute(f"SELECT {', '.join(measures)} FROM {sql_table}").fetchone()
            rows = row[0]
            if rows != table["rows"]:
                raise ValueError(f"Row count changed during profiling: {table_name}")
            # This bounded read preserves the old distinct_sample field without
            # a separate full-domain DISTINCT scan for every column.
            sample_sql = f"SELECT {', '.join(map(_identifier, group))} FROM {sql_table} ORDER BY __r2_row LIMIT {SAMPLE_ROWS}"
            sample_rows = data.db.execute(sample_sql).fetchall()
            width = len(names[0]) if names else 0
            for index, column in enumerate(group):
                stats = dict(zip(names[index], row[1 + index * width:1 + (index + 1) * width]))
                values = sorted({r[index] for r in sample_rows if r[index] is not None})
                sample_exhaustive = rows <= SAMPLE_ROWS and len(values) <= SAMPLE_VALUES
                nested = {"row_count": _stat(rows, rows_scanned=rows)}
                for name, value in stats.items():
                    approximate = name.startswith("approx_distinct")
                    method = "hll" if approximate else ("min" if name.startswith("min_") else "max" if name.startswith("max_") else "count")
                    nested[name] = _stat(value, exact=not approximate, method=method,
                                         population="usable_input_values" if name.endswith("_usable") else "all_input_rows",
                                         rows_scanned=rows)
                profile = {
                    "column": column,
                    "field_id": f"{table_name}.{column}",
                    "source_path": str(Path(table["csv_path"]).relative_to(data.root)),
                    "source_hash": table["csv_hash"],
                    "row_count": rows,
                    "input_scope": input_scope,
                    "scan_scope": "full_input",
                    "csv_null_semantics": "syntax_only_original_database_unknown",
                    "csv_null_policy": "unquoted_empty_null_quoted_empty_string",
                    "whitespace_policy": WHITESPACE_POLICY,
                    "statistics": nested,
                    **stats,
                    "distinct_sample": values[:SAMPLE_VALUES],
                    "sample_exhaustive_in_input": sample_exhaustive,
                    "sample_scope": f"first_{min(rows, SAMPLE_ROWS)}_input_rows",
                }
                profiles.append(profile)
            if on_group is not None:
                on_group(table_name)
        result[table_name] = profiles
    return result
