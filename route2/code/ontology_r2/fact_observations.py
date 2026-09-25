"""Bounded, source-backed candidates for rows that look like business facts.

An observation candidate is a distinct tuple *present in the imported CSV*.
It is not an ontology type, a business-key assertion, or a Metric/Measure
instance.  The SQL groups only observed rows; it never produces a Cartesian
product of coordinate values.  Definition and unresolved tables remain in
the coverage report rather than disappearing from the extraction pipeline.
"""

from __future__ import annotations

from .row_semantics import classify_row_purpose
from .storage import digest, qi


def _positive_limit(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _selected_fields(purpose, *, max_dimensions, max_times, max_values):
    evidence = purpose["evidence_columns"]
    all_times = list(dict.fromkeys(evidence["business_time"]))
    # A value column comment may mention the business object (for example
    # "水果销售利润（元）") and therefore also match a dimension cue. Numeric
    # value evidence takes precedence over that lexical mention.
    all_values = [name for name in dict.fromkeys(evidence["numeric_business_value"])
                  if name not in all_times]
    all_dimensions = [name for name in dict.fromkeys(evidence["dimension_coordinate"])
                      if name not in all_times and name not in all_values]
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
    order_sql = ", ".join(f"o.{qi(field)}" for field in coordinates)
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


def build_fact_observation_candidates(
    data, *, max_candidates_per_table=200, max_dimension_columns=4,
    max_time_columns=2, max_value_columns=8,
):
    """Return distinct observed fact tuples and explicit coverage boundaries.

    Every business-fact table is scanned in full for each selected value field,
    then output is capped.  The cap limits emitted tuples, not input scanning.
    A table with mixed/uncertain purpose is reported as unresolved and is never
    silently promoted to a fact.  Repeated values with one coordinate are
    marked ambiguous rather than collapsed into a single fact.
    """
    for name, value in (
        ("max_candidates_per_table", max_candidates_per_table),
        ("max_dimension_columns", max_dimension_columns),
        ("max_time_columns", max_time_columns),
        ("max_value_columns", max_value_columns),
    ):
        _positive_limit(value, name)

    reports = []
    for table_name in sorted(data.tables):
        table = data.tables[table_name]
        purpose = classify_row_purpose(table)
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
            purpose, max_dimensions=max_dimension_columns,
            max_times=max_time_columns, max_values=max_value_columns,
        )
        report["selected_columns"] = selected
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
            rows = _query_observed(data, table, coordinates, value_column, limit)
            all_tuples = rows[0]["all_observed_tuples"] if rows else 0
            complete_rows = rows[0]["all_complete_rows"] if rows else 0
            emitted_rows = sum(item["source_row_count"] for item in rows)
            field_report = {
                "column": value_column,
                "scan_scope": "full_input",
                "input_rows": table["rows"],
                "complete_rows": complete_rows,
                "blank_or_null_coordinate_or_value_rows": table["rows"] - complete_rows,
                "observed_tuples": all_tuples,
                "emitted_tuples": len(rows),
                "omitted_observed_tuples": all_tuples - len(rows),
                "source_rows_in_emitted_tuples": emitted_rows,
                "complete_source_rows_not_emitted": complete_rows - emitted_rows,
            }
            report["value_fields"].append(field_report)
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
        "method": "bounded_exact_observed_tuple_grouping_no_cartesian_expansion",
        "candidate_status": "candidate_only",
        "business_type_binding": "unresolved",
        "limits": {
            "max_candidates_per_table": max_candidates_per_table,
            "max_dimension_columns": max_dimension_columns,
            "max_time_columns": max_time_columns,
            "max_value_columns": max_value_columns,
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
        },
    }
