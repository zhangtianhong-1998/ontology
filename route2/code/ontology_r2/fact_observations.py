"""Bounded, source-backed candidates for rows that look like business facts.

An observation candidate is a distinct tuple *present in the imported CSV*.
It is not an ontology type, a business-key assertion, or a Metric/Measure
instance.  The SQL groups only observed rows; it never produces a Cartesian
product of coordinate values.  Definition and unresolved tables remain in
the coverage report rather than disappearing from the extraction pipeline.
"""

from __future__ import annotations

from .column_roles import classify_columns
from .row_semantics import _DIMENSION, _TIME, _VALUE, classify_row_purpose
from .storage import digest, qi


def _positive_limit(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _accepted_dimension_evidence(data, links):
    """Use only caller-accepted, snapshot-matched Dimension links for ranking.

    ``links`` is an optional iterable of mappings with source_table,
    source_column, target_table, target_column, target_root_type='Dimension',
    status='accepted', witness_snapshot_id, evidence_ids, and
    full_input_verified=True. This function cannot itself prove semantic
    acceptance; the caller must pass only accepted ontology relations.
    """
    accepted = {}
    ignored = 0
    for link in links or ():
        if not isinstance(link, dict):
            ignored += 1
            continue
        table = link.get("source_table")
        field = link.get("source_column")
        target = link.get("target_table")
        target_field = link.get("target_column")
        valid = (link.get("status") == "accepted"
                 and link.get("target_root_type") == "Dimension"
                 and link.get("witness_snapshot_id") == data.snapshot_id
                 and link.get("full_input_verified") is True
                 and bool(link.get("evidence_ids"))
                 and table in data.tables and target in data.tables
                 and field in data.tables[table]["column_names"]
                 and target_field in data.tables[target]["column_names"])
        if not valid:
            ignored += 1
            continue
        accepted.setdefault(table, {}).setdefault(field, []).append(link["evidence_ids"])
    return accepted, ignored


def _verified_technical_fields(data, links):
    """Rank only: a checked raw-value match does not establish a Dimension."""
    verified = {}
    ignored = 0
    for link in links or ():
        if not isinstance(link, dict):
            ignored += 1
            continue
        source, target = link.get("source"), link.get("target")
        verification = link.get("verification") or {}
        checks = verification.get("checks") or {}
        eligible = checks.get("eligible_references")
        valid = (link.get("status") == "checked_technical"
                 and link.get("snapshot_id") == data.snapshot_id
                 and verification.get("scan_scope") == "full_input"
                 and isinstance(eligible, int) and eligible > 0
                 and checks.get("unique_matches") == eligible
                 and checks.get("missing_scope", 0) == 0
                 and isinstance(source, dict) and isinstance(target, dict))
        if not valid:
            ignored += 1
            continue
        ends = ((source.get("table"), source.get("field")),
                (target.get("table"), target.get("field")))
        if any(table not in data.tables or field not in data.tables[table]["column_names"]
               for table, field in ends):
            ignored += 1
            continue
        for table, field in ends:
            verified.setdefault(table, set()).add(field)
    return verified, ignored


def _field_priority(table, name, cue, *, accepted=(), technical=()):
    profile = next((item for item in table.get("profiles", ())
                    if item.get("column") == name), {})
    count = profile.get("approx_distinct_usable")
    # Sorting only schedules a bounded candidate scan; it is not a semantic
    # assertion or a proof that a selected set is the complete fact grain.
    return (name not in accepted, name not in technical,
            not bool(cue.search(name)),
            name in table.get("pk", ()),
            count if isinstance(count, (int, float)) else float("inf"),
            name)


def _selected_fields(table, purpose, *, max_dimensions, max_times, max_values,
                     accepted_dimensions=(), technical_links=()):
    evidence = purpose["evidence_columns"]
    all_times = sorted(set(evidence["business_time"]),
                       key=lambda name: _field_priority(
                           table, name, _TIME, technical=technical_links))
    # A value column comment may mention the business object (for example
    # "水果销售利润（元）") and therefore also match a dimension cue. Numeric
    # value evidence takes precedence over that lexical mention.
    all_values = sorted((name for name in set(evidence["numeric_business_value"])
                         if name not in all_times),
                        key=lambda name: _field_priority(table, name, _VALUE))
    all_dimensions = sorted((name for name in set(evidence["dimension_coordinate"])
                             if name not in all_times and name not in all_values),
                            key=lambda name: _field_priority(
                                table, name, _DIMENSION, accepted=accepted_dimensions,
                                technical=technical_links))
    # Time and dimension are disjoint; a column that matches both cues has the
    # time role.  This is a selection rule, not a semantic claim about the DB.
    dimensions = all_dimensions[:max_dimensions]
    times = all_times[:max_times]
    values = all_values[:max_values]
    return {
        "dimensions": dimensions,
        "business_times": times,
        "values": values,
        "omitted_dimensions": all_dimensions[max_dimensions:],
        "omitted_business_times": all_times[max_times:],
        "omitted_values": all_values[max_values:],
        "selection_method": "accepted_dimension_link_then_verified_technical_hint_then_name_and_profile_rank",
        "accepted_dimension_fields_selected": [name for name in dimensions
                                               if name in accepted_dimensions],
        "technical_link_fields_selected": [name for name in [*dimensions, *times]
                                           if name in technical_links],
    }


def _query_observed(data, table, coordinates, value_column, limit):
    """One bounded result set from exact GROUP BY over actual rows only."""
    fields = [*coordinates, value_column]
    complete = " AND ".join(
        f"{qi(field)} IS NOT NULL AND length(trim({qi(field)})) > 0"
        for field in fields
    )
    coordinate_sql = ", ".join(qi(field) for field in coordinates)
    select_sql = ", ".join(f"{qi(field)} AS {qi(field)}" for field in coordinates)
    using_sql = ", ".join(qi(field) for field in coordinates)
    order_sql = "md5(concat_ws(chr(31), " + ", ".join(
        [*(f"o.{qi(field)}" for field in coordinates), "o.observation_value"]) + "))"
    sql = f"""
        WITH observed AS (
            SELECT {select_sql}, {qi(value_column)} AS observation_value,
                   count(*) AS source_row_count,
                   min(__r2_row) AS source_row_min,
                   max(__r2_row) AS source_row_max
            FROM {qi(table['sql_name'])}
            WHERE {complete}
            GROUP BY {coordinate_sql}, {qi(value_column)}
        ), coordinates AS (
            SELECT {coordinate_sql}, count(*) AS distinct_value_count
            FROM observed GROUP BY {coordinate_sql}
        )
        SELECT o.*, c.distinct_value_count,
               count(*) OVER () AS all_observed_tuples,
               sum(o.source_row_count) OVER () AS all_complete_rows
        FROM observed o JOIN coordinates c USING ({using_sql})
        ORDER BY {order_sql}, o.observation_value
        LIMIT ?
    """
    cursor = data.db.execute(sql, [limit])
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _estimate_tuple_count(data, table, coordinates, value_column):
    fields = [*coordinates, value_column]
    complete = " AND ".join(
        f"{qi(field)} IS NOT NULL AND length(trim({qi(field)})) > 0"
        for field in fields)
    args = ", ".join(qi(field) for field in fields)
    return data.db.execute(
        f"SELECT approx_count_distinct(hash({args})) FROM {qi(table['sql_name'])} "
        f"WHERE {complete}").fetchone()[0] or 0


def _query_seeded(data, table, coordinates, value_column, limit):
    """Search a bounded set of observed tuples, then verify their whole grain.

    A selected coordinate is checked against every matching source row. Min/max
    detects conflicting raw values without materializing all distinct values
    of a high-cardinality coordinate. Coverage stays partial because other
    source coordinates were not selected.
    """
    fields = [*coordinates, value_column]
    complete = " AND ".join(
        f"{qi(field)} IS NOT NULL AND length(trim({qi(field)})) > 0"
        for field in fields)
    coordinate_sql = ", ".join(qi(field) for field in coordinates)
    raw_fields = ", ".join(qi(field) for field in fields)
    priority_sql = f"md5(concat_ws(chr(31), {raw_fields}))"
    matches_coord = " AND ".join(f"r.{qi(field)} = s.{qi(field)}" for field in coordinates)
    joins_coord = " AND ".join(f"m.{qi(field)} = c.{qi(field)}" for field in coordinates)
    grouped = ", ".join(f"s.{qi(field)}" for field in coordinates)
    projection = ", ".join(f"m.{qi(field)}" for field in coordinates)
    sql = f"""
        WITH sampled AS (
            SELECT {coordinate_sql}, {qi(value_column)} AS observation_value,
                   {priority_sql} AS candidate_rank
            FROM {qi(table['sql_name'])}
            WHERE {complete}
            ORDER BY candidate_rank, __r2_row LIMIT ?
        ), seeds AS (
            SELECT DISTINCT {coordinate_sql}, observation_value, candidate_rank
            FROM sampled ORDER BY candidate_rank LIMIT ?
        ), seed_coordinates AS (
            SELECT DISTINCT {coordinate_sql} FROM seeds
        ), scoped AS (
            SELECT {', '.join('r.' + qi(field) for field in coordinates)},
                   r.{qi(value_column)} AS observation_value, r.__r2_row
            FROM {qi(table['sql_name'])} r JOIN seed_coordinates s
              ON {matches_coord}
            WHERE r.{qi(value_column)} IS NOT NULL
              AND length(trim(r.{qi(value_column)})) > 0
        ), coordinate_status AS (
            SELECT {coordinate_sql}, min(observation_value) AS min_value,
                   max(observation_value) AS max_value
            FROM scoped GROUP BY {coordinate_sql}
        ), matches AS (
            SELECT {grouped}, s.observation_value,
                   count(*) AS source_row_count,
                   min(r.__r2_row) AS source_row_min,
                   max(r.__r2_row) AS source_row_max
            FROM seeds s JOIN scoped r ON {matches_coord}
              AND s.observation_value = r.observation_value
            GROUP BY {grouped}, s.observation_value
        )
        SELECT {projection}, m.observation_value, m.source_row_count,
               m.source_row_min, m.source_row_max,
               CASE WHEN c.min_value = c.max_value THEN 1 ELSE 2 END
                 AS distinct_value_count
        FROM matches m JOIN coordinate_status c ON {joins_coord}
        ORDER BY md5(concat_ws(chr(31), {projection}, m.observation_value))
        LIMIT ?
    """
    cursor = data.db.execute(sql, [max(limit * 4, limit), limit, limit])
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _unmodeled_grain_fields(table, selected):
    known = set(table.get("pk", ())) | set(selected["dimensions"]) | set(selected["business_times"])
    known.update(selected["omitted_dimensions"])
    known.update(selected["omitted_business_times"])
    known.update(selected["values"])
    known.update(selected["omitted_values"])
    profiles = {item["column"]: item for item in table.get("profiles", ())}
    possible, unprofiled = [], []
    for item in classify_columns(table):
        name = item["column"]
        if name in known or item["role"] not in ("semantic", "unknown"):
            continue
        profile = profiles.get(name)
        if not profile or profile.get("scan_scope") != "full_input":
            unprofiled.append(name)
        elif (profile.get("approx_distinct_usable") or 0) > 1:
            possible.append(name)
    return possible, unprofiled


def build_fact_observation_candidates(
    data, *, max_candidates_per_table=200, max_dimension_columns=4,
    max_time_columns=2, max_value_columns=8,
    max_exact_group_tuples=50000, accepted_dimension_links=None,
    verified_technical_links=None,
):
    """Return distinct observed fact tuples and explicit coverage boundaries.

    Every business-fact table is scanned in full for each selected value field,
    then output is capped.  The cap limits emitted tuples, not input scanning.
    A table with mixed/uncertain purpose is reported as unresolved and is never
    silently promoted to a fact. When the estimated observed-tuple population
    is too large for exact grouping, bounded hash seeds are reverse-checked
    against every row at those coordinates; the unvisited remainder is unknown.
    """
    for name, value in (
        ("max_candidates_per_table", max_candidates_per_table),
        ("max_dimension_columns", max_dimension_columns),
        ("max_time_columns", max_time_columns),
        ("max_value_columns", max_value_columns),
        ("max_exact_group_tuples", max_exact_group_tuples),
    ):
        _positive_limit(value, name)

    accepted_by_table, ignored_links = _accepted_dimension_evidence(
        data, accepted_dimension_links)
    technical_by_table, ignored_technical = _verified_technical_fields(
        data, verified_technical_links)

    reports = []
    for table_name in sorted(data.tables):
        table = data.tables[table_name]
        accepted_dimensions = accepted_by_table.get(table_name, {})
        technical_fields = technical_by_table.get(table_name, set())
        purpose = classify_row_purpose(
            table, accepted_dimension_fields=accepted_dimensions)
        report = {
            "table": table_name,
            "row_purpose": purpose["purpose"],
            "row_purpose_reason": purpose["reason"],
            "input_rows": table["rows"],
            "candidate_status": "candidate_only",
            "business_type_binding": "unresolved",
            "scan_scope": "not_applicable_for_row_purpose",
            "selected_columns": None,
            "value_fields": [],
            "candidates": [],
            "emitted_candidates": 0,
            "omitted_observed_tuples": 0,
            "omitted_observed_tuples_status": "exact",
            "accepted_dimension_link_fields": sorted(accepted_dimensions),
            "verified_technical_link_fields": sorted(technical_fields),
        }
        if purpose["purpose"] != "business_fact":
            report["reason"] = (
                "mixed_or_insufficient_row_purpose_evidence"
                if purpose["purpose"] == "unresolved" else
                "definition_or_configuration_rows_are_not_business_fact_observations"
            )
            reports.append(report)
            continue

        selected = _selected_fields(
            table, purpose, max_dimensions=max_dimension_columns,
            max_times=max_time_columns, max_values=max_value_columns,
            accepted_dimensions=accepted_dimensions,
            technical_links=technical_fields,
        )
        report["selected_columns"] = selected
        possible_grain, unprofiled_grain = _unmodeled_grain_fields(table, selected)
        report["possible_unmodeled_grain_fields"] = possible_grain
        report["unprofiled_unassigned_fields"] = unprofiled_grain
        report["coordinate_grain_status"] = (
            "unresolved_omitted_coordinate_columns" if selected["omitted_dimensions"]
            or selected["omitted_business_times"] else
            "unresolved_possible_unmodeled_grain" if possible_grain else
            "unresolved_unprofiled_unassigned_fields" if unprofiled_grain else
            "candidate_grain_not_business_key_proof")
        dimensions, times, values = (selected["dimensions"],
                                    selected["business_times"], selected["values"])
        if not dimensions or not times or not values:
            report["scan_scope"] = "not_scanned_ambiguous_column_roles"
            report["reason"] = "selected_fact_columns_do_not_cover_dimension_time_and_value"
            reports.append(report)
            continue

        coordinates = [*dimensions, *times]
        report["scan_scope"] = "full_input_for_selected_value_columns"
        report["reason"] = "observed_tuples_grouped_without_cartesian_expansion"
        report["source_snapshot_id"] = data.snapshot_id
        report["coordinate_columns"] = coordinates
        # Round-robin quotas give each value field at least one chance rather
        # than letting the first high-cardinality field consume the full cap.
        covered_values = values[:max_candidates_per_table]
        report["value_columns_not_scanned_due_to_candidate_limit"] = values[len(covered_values):]
        quotient, remainder = divmod(max_candidates_per_table, len(covered_values))
        for index, value_column in enumerate(covered_values):
            limit = quotient + (index < remainder)
            estimated_tuples = _estimate_tuple_count(
                data, table, coordinates, value_column)
            seeded = estimated_tuples > max_exact_group_tuples
            rows = (_query_seeded(data, table, coordinates, value_column, limit)
                    if seeded else _query_observed(
                        data, table, coordinates, value_column, limit))
            all_tuples = rows[0]["all_observed_tuples"] if rows and not seeded else None if seeded else 0
            complete_rows = rows[0]["all_complete_rows"] if rows and not seeded else None if seeded else 0
            emitted_rows = sum(item["source_row_count"] for item in rows)
            field_report = {
                "column": value_column,
                "scan_scope": ("full_input_with_exact_selected_coordinate_reverse_check"
                               if seeded else "full_input"),
                "input_rows": table["rows"],
                "complete_rows": complete_rows,
                "blank_or_null_coordinate_or_value_rows": (
                    table["rows"] - complete_rows if complete_rows is not None else None),
                "observed_tuples": all_tuples,
                "observed_tuples_estimate": estimated_tuples,
                "observed_tuples_count_status": "unknown" if seeded else "exact",
                "selection_method": ("stable_hash_source_seeds_with_exact_reverse_check"
                                     if seeded else "stable_hash_of_full_observed_groups"),
                "emitted_tuples": len(rows),
                "omitted_observed_tuples": (all_tuples - len(rows)
                                            if all_tuples is not None else None),
                "source_rows_in_emitted_tuples": emitted_rows,
                "complete_source_rows_not_emitted": (complete_rows - emitted_rows
                                                    if complete_rows is not None else None),
            }
            report["value_fields"].append(field_report)
            if seeded:
                report["scan_scope"] = "exact_selected_coordinates_only"
                report["reason"] = "hash_seeded_observed_tuples_with_exact_coordinate_reverse_check"
                report["omitted_observed_tuples_status"] = "unknown_high_cardinality_seeded"
            else:
                report["omitted_observed_tuples"] += field_report["omitted_observed_tuples"]
            template_id = "fact_template:" + digest(
                [table_name, dimensions, times, value_column])[:24]
            for row in rows:
                coordinate_values = {field: row[field] for field in coordinates}
                ambiguity = []
                if row["distinct_value_count"] > 1:
                    ambiguity.append("multiple_values_at_selected_coordinates")
                if selected["omitted_dimensions"] or selected["omitted_business_times"]:
                    ambiguity.append("coordinate_columns_omitted_by_limit")
                candidate = {
                    "id": "fact_candidate:" + digest(
                        [data.snapshot_id, table_name, value_column,
                         coordinate_values, row["observation_value"]])[:24],
                    "template_id": template_id,
                    "table": table_name,
                    "candidate_status": "candidate_only",
                    "selection_scope": ("hash_seeded_exact_reverse_checked"
                                        if seeded else "full_observed_group"),
                    "coordinate_grain_status": report["coordinate_grain_status"],
                    "business_type_binding": "unresolved",
                    "coordinate_values": coordinate_values,
                    "value_column": value_column,
                    "observed_value": row["observation_value"],
                    "source_row_count": row["source_row_count"],
                    "duplicate_source_rows": row["source_row_count"] - 1,
                    "source_row_min": row["source_row_min"],
                    "source_row_max": row["source_row_max"],
                    "source_row_range_is_exact_membership": False,
                    "distinct_values_at_selected_coordinates": row["distinct_value_count"],
                    "ambiguity": ambiguity,
                    "evidence": {
                        "source": "imported_csv_snapshot",
                        "snapshot_id": data.snapshot_id,
                        "table": table_name,
                        "columns": [*coordinates, value_column],
                        "source_row_min": row["source_row_min"],
                        "source_row_max": row["source_row_max"],
                        "source_row_count": row["source_row_count"],
                    },
                }
                report["candidates"].append(candidate)
        report["emitted_candidates"] = len(report["candidates"])
        reports.append(report)

    return {
        "scope": "imported_csv_snapshot_only",
        "method": "bounded_observed_group_or_seeded_reverse_check_no_cartesian_expansion",
        "candidate_status": "candidate_only",
        "business_type_binding": "unresolved",
        "limits": {
            "max_candidates_per_table": max_candidates_per_table,
            "max_dimension_columns": max_dimension_columns,
            "max_time_columns": max_time_columns,
            "max_value_columns": max_value_columns,
            "max_exact_group_tuples": max_exact_group_tuples,
        },
        "tables": reports,
        "coverage": {
            "tables_total": len(reports),
            "business_fact_tables": sum(r["row_purpose"] == "business_fact" for r in reports),
            "unresolved_tables": sum(r["row_purpose"] == "unresolved" for r in reports),
            "non_fact_tables": sum(r["row_purpose"] in
                                   ("definition_data", "configuration_data") for r in reports),
            "emitted_candidates": sum(r["emitted_candidates"] for r in reports),
            "omitted_observed_tuples": sum(r["omitted_observed_tuples"] for r in reports),
            "unknown_omitted_observed_tuples_fields": sum(
                field["observed_tuples_count_status"] == "unknown"
                for report in reports for field in report["value_fields"]),
            "accepted_dimension_links_ignored": ignored_links,
            "verified_technical_links_ignored": ignored_technical,
            "partial": bool(ignored_links or ignored_technical or any(
                report.get("coordinate_grain_status") not in
                (None, "candidate_grain_not_business_key_proof")
                or report["omitted_observed_tuples_status"] != "exact"
                or report["omitted_observed_tuples"] > 0
                or (report.get("selected_columns") or {}).get("omitted_values")
                for report in reports)),
        },
    }
