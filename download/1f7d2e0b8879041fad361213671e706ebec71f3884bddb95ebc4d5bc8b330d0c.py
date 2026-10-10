"""Read-only coverage of indexed definition source rows across binding paths.

Coverage counts existing accepted artifacts; it does not rejudge their semantics
or certify the source classifier. A template-only denominator is reported beside
the full definition-card inventory, never substituted for it.
"""

from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def _mapping(item):
    return item.model_dump() if hasattr(item, "model_dump") else item


def summarize_definition_coverage(index, group, *, definition_memberships=(),
                                  template_bindings=None, template_projections=None,
                                  plan=None, data=None):
    """Return per-table/global unique-(table,row) coverage without source writes.

    ``index`` is a SemanticCardIndex or its SQLite path; ``group`` supplies
    concepts/record_alignments and, by default, template bindings/projections.
    Iterables may stream memberships and bindings. Pass the final BuildPlan (or
    its dict) to exclude types no longer accepted. ``data`` optionally verifies
    the snapshot; no source values, models, or network calls are needed.

    Missing template scope is not a failure: an accepted exact alignment or
    membership can cover the same row. Counts measure binding presence only;
    reference variants and inherited business relations remain unverified here.
    """
    path = Path(getattr(index, "path", index)).resolve()
    bindings = group.get("template_bindings", ()) if template_bindings is None else template_bindings
    projections = group.get("template_projections", ()) if template_projections is None else template_projections
    concepts = {c["id"]: c for c in group.get("concepts", ())}
    if plan is not None:
        accepted_types = {t["id"] for value in _mapping(plan).get("object_types", ())
                          if (t := _mapping(value)).get("category") == "business_type"}
    else:
        accepted_types = None

    # Temporary disk-backed SQL state bounds Python memory. ATTACH uses mode=ro
    # even when the caller passes a live index object; its connection is untouched.
    with closing(sqlite3.connect("", uri=True)) as db:
        try:
            db.execute("ATTACH DATABASE ? AS source", (path.as_uri() + "?mode=ro",))
            meta = db.execute("SELECT value FROM source.metadata WHERE key='coverage'").fetchone()
            if meta is None:
                raise ValueError("Definition coverage requires completed index metadata")
            source_coverage = json.loads(meta[0])
            snapshot = source_coverage.get("snapshot_id")
            for supplied in (group.get("snapshot_id"), getattr(data, "snapshot_id", None)):
                if supplied is not None and supplied != snapshot:
                    raise ValueError("Definition coverage snapshot differs from its source index")
            if not snapshot:
                raise ValueError("Definition coverage index has no snapshot")
            if db.execute("SELECT 1 FROM source.cards WHERE snapshot_id<>? LIMIT 1", (snapshot,)).fetchone():
                raise ValueError("Definition coverage index mixes snapshots")
            db.executescript("""
                CREATE TABLE inventory (
                    table_name TEXT NOT NULL, row_number INTEGER NOT NULL,
                    channels INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(table_name,row_number));
                INSERT OR IGNORE INTO inventory(table_name,row_number)
                    SELECT c.table_name,s.row_number FROM source.card_sources s
                    JOIN source.cards c ON c.card_id=s.card_id WHERE c.kind='definition';
                CREATE TABLE template_scope(table_name TEXT PRIMARY KEY);
            """)
            rejected, artifacts = Counter(), Counter()

            def allowed_type(type_id):
                return bool(type_id) and (accepted_types is None or type_id in accepted_types)

            def valid_snapshot(item):
                return item.get("snapshot_id", snapshot) == snapshot

            for projection in projections:
                if (projection.get("status") == "accepted" and valid_snapshot(projection)
                        and allowed_type(projection.get("object_type_id"))
                        and projection.get("source_table")):
                    db.execute("INSERT OR IGNORE INTO template_scope VALUES (?)", (projection["source_table"],))
                else:
                    rejected["template_scope:not_accepted_or_current_type"] += 1

            def mark(item, channel, type_id):
                artifacts[channel + "_seen"] += 1
                if not allowed_type(type_id):
                    rejected[channel + ":no_accepted_business_type"] += 1
                    return
                if not valid_snapshot(item):
                    rejected[channel + ":snapshot_mismatch"] += 1
                    return
                record_id = item.get("source_record_id", item.get("record_id"))
                if not record_id:
                    rejected[channel + ":missing_record_id"] += 1
                    return
                clauses, parameters = ["s.record_id=?", "c.kind='definition'"], [record_id]
                for column, value in (("c.card_id", item.get("source_card_id", item.get("card_id"))),
                                      ("c.table_name", item.get("source_table", item.get("table"))),
                                      ("s.row_number", item.get("row_number"))):
                    if value is not None:
                        clauses.append(column + "=?")
                        parameters.append(value)
                rows = db.execute("SELECT DISTINCT c.table_name,s.row_number FROM source.card_sources s "
                                  "JOIN source.cards c ON c.card_id=s.card_id WHERE " + " AND ".join(clauses)
                                  + " LIMIT 2", parameters).fetchall()
                if len(rows) != 1:
                    reason = "ambiguous_source_row" if rows else "source_row_not_in_definition_index"
                    rejected[channel + ":" + reason] += 1
                    return
                bit = {"exact": 1, "membership": 2, "template": 4}[channel]
                db.execute("UPDATE inventory SET channels=channels|? WHERE table_name=? AND row_number=?",
                           (bit, *rows[0]))
                artifacts[channel + "_accepted"] += 1

            for alignment in group.get("record_alignments", ()):
                concept = concepts.get(alignment.get("concept_id"), {})
                if (alignment.get("mapping_kind") != "exact"
                        or alignment.get("decision") not in {"accepted", "accepted_by_automatic_checks"}
                        or concept.get("ontology_level") != "type"
                        or concept.get("decision") not in {"accepted", "accepted_by_automatic_checks"}):
                    rejected["exact:not_accepted_type_alignment"] += 1
                    continue
                mark(alignment, "exact", concept.get("ontology_type_id"))
            for membership in definition_memberships:
                if (membership.get("mapping_kind") != "shares_definition_type_template"
                        or membership.get("status") != "definition_template_match"):
                    rejected["membership:not_accepted"] += 1
                    continue
                mark(membership, "membership", membership.get("type_id"))
            for binding in bindings:
                if binding.get("status") != "accepted" or binding.get("mapping_kind") != "template_instance":
                    rejected["template:not_accepted"] += 1
                    continue
                mark(binding, "template", binding.get("object_type_id"))

            by_table = {}
            # Channel counts overlap; union is counted once per source row.
            for row in db.execute("""
                SELECT i.table_name, count(*), sum(channels&1 != 0), sum(channels&2 != 0),
                       sum(channels&4 != 0), sum(channels != 0), sum(channels = 0),
                       sum(channels IN (3,5,6,7)), t.table_name IS NOT NULL
                FROM inventory i LEFT JOIN template_scope t ON t.table_name=i.table_name
                GROUP BY i.table_name ORDER BY i.table_name
            """):
                table, total, exact, membership, template, bound, unbound, overlap, in_scope = row
                by_table[table] = {
                    "indexed_definition_rows": total, "exact_alignment_rows": exact,
                    "membership_rows": membership, "template_binding_rows": template,
                    "rows_with_type_binding": bound, "rows_without_type_binding": unbound,
                    "rows_with_multiple_binding_paths": overlap,
                    "type_binding_fraction": bound / total if total else None,
                    "in_template_scope": bool(in_scope),
                    "rows_outside_template_scope": 0 if in_scope else total,
                    "bound_rows_outside_template_scope": 0 if in_scope else bound,
                    "template_scope_rows_without_type_binding": unbound if in_scope else 0,
                }
            count_fields = [key for key in next(iter(by_table.values()), {})
                            if key not in {"type_binding_fraction", "in_template_scope"}]
            totals = {key: sum(report[key] for report in by_table.values()) for key in count_fields}
            # Stable zero-valued fields even for an empty index.
            for key in ("indexed_definition_rows", "exact_alignment_rows", "membership_rows",
                        "template_binding_rows", "rows_with_type_binding", "rows_without_type_binding",
                        "rows_with_multiple_binding_paths", "rows_outside_template_scope",
                        "bound_rows_outside_template_scope", "template_scope_rows_without_type_binding"):
                totals.setdefault(key, 0)
            scoped = totals["indexed_definition_rows"] - totals["rows_outside_template_scope"]
            scoped_template = sum(r["template_binding_rows"] for r in by_table.values() if r["in_template_scope"])
            totals.update({
                "definition_tables": len(by_table),
                "template_scope_definition_tables": sum(r["in_template_scope"] for r in by_table.values()),
                "template_scope_rows": scoped,
                "type_binding_fraction": (totals["rows_with_type_binding"] / totals["indexed_definition_rows"]
                                          if totals["indexed_definition_rows"] else None),
                "template_binding_fraction_within_template_scope": scoped_template / scoped if scoped else None,
            })
            scope_tables = [r[0] for r in db.execute("SELECT table_name FROM template_scope ORDER BY table_name")]
            return {
                "scope": "unique_source_table_and_row_in_definition_card_index",
                "snapshot_id": snapshot, "llm_calls": 0,
                "type_acceptance_basis": "final_plan_business_types" if plan is not None else "supplied_accepted_artifacts",
                "semantic_validation": "not_rejudged; binding_presence_only; no_reference_or_relation_completeness_claim",
                "source_index_partial": bool(source_coverage.get("partial")),
                "source_index_rows_omitted_cap": source_coverage.get("rows_omitted_cap", 0),
                "source_index_rows_without_card": source_coverage.get("rows_without_card", 0),
                "definition_classification_scope": "index_definition_cards_only; excludes_reference_uncertain_and_business_fact_rows",
                "totals": totals, "by_table": by_table,
                "template_scope_tables": scope_tables,
                "template_scope_tables_without_indexed_definitions": sorted(set(scope_tables) - set(by_table)),
                "tables_outside_template_scope": [t for t, r in by_table.items() if not r["in_template_scope"]],
                "artifact_counts": dict(artifacts), "excluded_artifact_counts": dict(rejected),
            }
        finally:
            # A sqlite connection context manager commits/rolls back, but does
            # not close it; close explicitly so its temporary database vanishes.
            db.close()
