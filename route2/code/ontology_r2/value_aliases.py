"""Bounded, explainable recall of cross-table value and alias coincidences.

This module proposes *undirected* field pairs.  NFKC, whitespace folding and
alias-list splitting are recall rules, not proof of identity or a foreign key.
Original CSV values and row numbers are retained in the examples.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations
import re
import unicodedata

from .storage import digest, qi


_ALIAS_NAME = re.compile(r"(?:^|_)(?:alias|aliases|synonym|synonyms)(?:_|$)|别名", re.I)
_ALIAS_SPLIT = re.compile(r"[;,；，、|]")
_IMPORTANT_NAME = re.compile(r"(?:^|_)(?:name|alias|aliases|code|id|key|title|label)(?:_|$)", re.I)


def _canonical(value):
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _forms(value, alias_field):
    canonical = _canonical(value)
    if canonical:
        yield {"canonical": canonical, "matched_text": value,
               "rule": "raw_equal" if value == canonical else "nfkc_whitespace_casefold"}
    if alias_field and _ALIAS_SPLIT.search(value):
        for index, part in enumerate(_ALIAS_SPLIT.split(value)):
            canonical = _canonical(part)
            if canonical:
                yield {"canonical": canonical, "matched_text": part.strip(),
                       "rule": "alias_list_item_nfkc_whitespace_casefold",
                       "alias_item_index": index}


def _fields(data, fields, limit):
    all_fields = {(table, column) for table, info in data.tables.items()
                  for column in info["column_names"]}
    if fields is not None:
        requested = list(dict.fromkeys(tuple(field) for field in fields))
        if any(field not in all_fields for field in requested):
            raise ValueError("Unknown alias candidate field")
        return requested[:limit], max(0, len(requested) - limit)
    by_table = {}
    for table, info in sorted(data.tables.items()):
        by_table[table] = sorted(
            info["column_names"], key=lambda col: (
                not bool(_ALIAS_NAME.search(col)),
                not bool(_IMPORTANT_NAME.search(col)),
                info["column_names"].index(col), col,
            ),
        )
    selected = []
    for position in range(max(map(len, by_table.values()), default=0)):
        for table in by_table:
            if position < len(by_table[table]):
                selected.append((table, by_table[table][position]))
                if len(selected) == limit:
                    return selected, len(all_fields) - len(selected)
    return selected, 0


def _sample(data, selected, max_rows, max_values, max_chars):
    by_table = defaultdict(list)
    for table, column in selected:
        by_table[table].append(column)
    samples = {(table, column): {} for table, column in selected}
    coverage = {}
    for table, columns in by_table.items():
        info = data.tables[table]
        sql = (f"SELECT __r2_row, {', '.join(qi(col) for col in columns)} "
               f"FROM {qi(info['sql_name'])} WHERE __r2_row <= ? ORDER BY __r2_row")
        cursor = data.db.cursor().execute(sql, [max_rows])
        rows_scanned = 0
        skipped_long = 0
        fields_at_cap = set()
        try:
            while batch := cursor.fetchmany(1000):
                for row in batch:
                    rows_scanned += 1
                    row_number = row[0]
                    for column, value in zip(columns, row[1:]):
                        if value is None or not str(value).strip():
                            continue
                        raw = str(value)
                        if len(raw) > max_chars:
                            skipped_long += 1
                            continue
                        observed = samples[(table, column)]
                        if raw in observed:
                            observed[raw]["occurrences"] += 1
                        elif len(observed) < max_values:
                            observed[raw] = {"raw_value": raw, "row_number": row_number,
                                             "occurrences": 1}
                        else:
                            fields_at_cap.add(column)
        finally:
            cursor.close()
        coverage[table] = {
            "rows_scanned": rows_scanned,
            "input_rows": info.get("rows"),
            "row_window_truncated": info.get("rows") is None or info["rows"] > rows_scanned,
            "value_cap_reached_fields": sorted(fields_at_cap),
            "long_values_skipped": skipped_long,
        }
    return samples, coverage


def propose_value_alias_candidates(
    data, *, fields=None, max_fields=64, max_rows_per_table=10000,
    max_values_per_field=128, max_value_chars=96, max_pairs=500,
    max_examples_per_pair=5, max_common_key_fanout=8,
):
    """Recall bounded field pairs from raw values, safe normalization and aliases.

    ``fields`` can explicitly restrict work to ``(table, column)`` tuples.
    All coverage and ambiguity numbers refer only to the scanned row window
    and retained distinct values.  Full-input validation remains necessary.
    """
    limits = (max_fields, max_rows_per_table, max_values_per_field,
              max_value_chars, max_pairs, max_examples_per_pair,
              max_common_key_fanout)
    if any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in limits):
        raise ValueError("Alias recall limits must be positive integers")
    selected, fields_queued = _fields(data, fields, max_fields)
    samples, scan_coverage = _sample(data, selected, max_rows_per_table,
                                     max_values_per_field, max_value_chars)
    inverted = defaultdict(lambda: defaultdict(list))
    forms_by_field = defaultdict(list)
    for field in selected:
        for raw, observation in samples[field].items():
            for form in _forms(raw, bool(_ALIAS_NAME.search(field[1]))):
                item = {**observation, **form}
                inverted[form["canonical"]][field].append(item)
                forms_by_field[field].append(item)

    pairs = defaultdict(lambda: {"keys": set(), "left_values": set(),
                                 "right_values": set(), "examples": [],
                                 "collision_keys": set(), "repeated_record_keys": set(),
                                 "rules": set(), "comparison_modes": set()})
    skipped_common_keys = 0
    queued_pair_key_events = 0
    for key, holders in sorted(inverted.items()):
        if len(holders) > max_common_key_fanout:
            skipped_common_keys += 1
            continue
        for left, right in combinations(sorted(holders), 2):
            if left[0] == right[0]:
                continue
            pair = (left, right)
            if pair not in pairs and len(pairs) >= max_pairs:
                queued_pair_key_events += 1
                continue
            bucket = pairs[pair]
            left_items, right_items = holders[left], holders[right]
            bucket["keys"].add(key)
            bucket["left_values"].update(item["raw_value"] for item in left_items)
            bucket["right_values"].update(item["raw_value"] for item in right_items)
            bucket["rules"].update(item["rule"] for item in left_items + right_items)
            raw_equal = bool({item["raw_value"] for item in left_items} & {
                item["raw_value"] for item in right_items})
            alias_equal = any(item["rule"].startswith("alias_list_item")
                              for item in left_items + right_items)
            if raw_equal:
                bucket["comparison_modes"].add("raw_value_equal")
            if alias_equal:
                bucket["comparison_modes"].add("alias_item_equal")
            elif not raw_equal:
                bucket["comparison_modes"].add("normalized_equal")
            if len({item["raw_value"] for item in left_items}) > 1 or len(
                    {item["raw_value"] for item in right_items}) > 1:
                bucket["collision_keys"].add(key)
            if (sum({item["raw_value"]: item["occurrences"] for item in left_items}.values()) > 1 or
                    sum({item["raw_value"]: item["occurrences"] for item in right_items}.values()) > 1):
                bucket["repeated_record_keys"].add(key)
            if len(bucket["examples"]) < max_examples_per_pair:
                left_example = min(left_items, key=lambda item: (item["raw_value"], item["row_number"]))
                right_example = min(right_items, key=lambda item: (item["raw_value"], item["row_number"]))
                bucket["examples"].append({
                    "normalized_value": key,
                    "comparison_rule": (
                        "raw_value_equal" if left_example["raw_value"] == right_example["raw_value"]
                        else "alias_item_equal" if any(item["rule"].startswith("alias_list_item")
                                                       for item in (left_example, right_example))
                        else "normalized_equal"
                    ),
                    "source": {k: left_example[k] for k in (
                        "raw_value", "row_number", "matched_text", "rule")},
                    "target": {k: right_example[k] for k in (
                        "raw_value", "row_number", "matched_text", "rule")},
                })

    candidates = []
    for (left, right), bucket in sorted(pairs.items()):
        left_unmatched = [item for item in forms_by_field[left]
                          if item["canonical"] not in bucket["keys"]]
        right_unmatched = [item for item in forms_by_field[right]
                           if item["canonical"] not in bucket["keys"]]
        counterexamples = []
        for side, items in (("source", left_unmatched), ("target", right_unmatched)):
            for item in items[:max_examples_per_pair]:
                counterexamples.append({"side": side, "reason": "form_not_matched_in_sample",
                                        "raw_value": item["raw_value"],
                                        "matched_text": item["matched_text"],
                                        "normalized_value": item["canonical"],
                                        "row_number": item["row_number"]})
        left_count, right_count = len(samples[left]), len(samples[right])
        candidates.append({
            "candidate_id": digest([getattr(data, "snapshot_id", None), left, right,
                                     "value_aliases_v1"])[:24],
            "source": {"table": left[0], "field": left[1]},
            "target": {"table": right[0], "field": right[1]},
            "orientation": "undetermined",
            "retrieval_channels": sorted(bucket["rules"]),
            "comparison_modes": sorted(bucket["comparison_modes"]),
            "checks": {
                "shared_normalized_keys_in_sample": len(bucket["keys"]),
                "source_distinct_raw_values_sampled": left_count,
                "target_distinct_raw_values_sampled": right_count,
                "source_raw_values_matched_in_sample": len(bucket["left_values"]),
                "target_raw_values_matched_in_sample": len(bucket["right_values"]),
                "source_sample_coverage": len(bucket["left_values"]) / left_count if left_count else None,
                "target_sample_coverage": len(bucket["right_values"]) / right_count if right_count else None,
                "normalization_collision_keys": len(bucket["collision_keys"]),
                "repeated_record_keys": len(bucket["repeated_record_keys"]),
                "numeric_overlap_only": all(key.isdecimal() for key in bucket["keys"]),
            },
            "examples": bucket["examples"],
            "counterexamples": counterexamples,
            "decision": {"status": "proposed", "semantic_relation": "unresolved"},
            "scope": "bounded_imported_csv_window; not full-input validation",
        })
    return {"candidates": candidates, "coverage": {
        "input_scope": "imported_csv_snapshot",
        "scan_scope": "first_N_rows_per_selected_table",
        "selected_fields": [{"table": table, "field": field} for table, field in selected],
        "fields_queued": fields_queued,
        "max_rows_per_table": max_rows_per_table,
        "max_values_per_field": max_values_per_field,
        "max_value_chars": max_value_chars,
        "table_scans": scan_coverage,
        "high_fanout_normalized_keys_skipped": skipped_common_keys,
        "pair_key_events_queued": queued_pair_key_events,
        "candidate_count": len(candidates),
        "partial": bool(fields_queued or skipped_common_keys or queued_pair_key_events or any(
            item["row_window_truncated"] or item["value_cap_reached_fields"] or
            item["long_values_skipped"] for item in scan_coverage.values())),
    }}
