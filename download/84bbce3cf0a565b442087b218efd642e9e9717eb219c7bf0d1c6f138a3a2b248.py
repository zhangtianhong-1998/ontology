"""Coverage denominators include every indexed definition table, not only templates."""

import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from ontology_r2.definition_coverage import summarize_definition_coverage


def _index(tmp_path, *, partial=False, rows=None):
    path = tmp_path / "cards.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE cards(card_id TEXT PRIMARY KEY, table_name TEXT, kind TEXT, snapshot_id TEXT);
            CREATE TABLE card_sources(card_id TEXT, record_id TEXT, row_number INTEGER);
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
            CREATE INDEX by_record ON card_sources(record_id);
        """)
        rows = rows if rows is not None else [
            ("api", 1), ("api", 2), ("api", 3),
            ("measure", 1), ("measure", 2), ("measure", 3),
            ("wide", 1), ("wide", 2), ("zero_binding", 1),
        ]
        for i, (table, row) in enumerate(rows):
            db.execute("INSERT INTO cards VALUES (?,?,?,?)", (f"c{i}", table, "definition", "s"))
            db.execute("INSERT INTO card_sources VALUES (?,?,?)", (f"c{i}", f"{table}:{row}", row))
        # A second card for the same source row must not increase the denominator.
        if rows:
            table, row = rows[0]
            db.execute("INSERT INTO cards VALUES ('duplicate',?,'definition','s')", (table,))
            db.execute("INSERT INTO card_sources VALUES ('duplicate',?,?)", (f"{table}:{row}", row))
        db.execute("INSERT INTO cards VALUES ('reference','reference','reference','s')")
        db.execute("INSERT INTO card_sources VALUES ('reference','reference:1',1)")
        db.execute("INSERT INTO metadata VALUES ('coverage',?)", (json.dumps({
            "snapshot_id": "s", "partial": partial, "rows_omitted_cap": 7 if partial else 0,
            "rows_without_card": 19,
        }),))
    return path


def _group():
    return {"snapshot_id": "s", "concepts": [
        {"id": "concept", "ontology_level": "type", "ontology_type_id": "type",
         "decision": "accepted_by_automatic_checks"},
        {"id": "instance", "ontology_level": "instance", "ontology_type_id": "type",
         "decision": "accepted_by_automatic_checks"},
    ], "record_alignments": [
        {"source_record_id": "api:1", "concept_id": "concept", "mapping_kind": "exact",
         "decision": "accepted_by_automatic_checks"},
        {"source_record_id": "measure:1", "concept_id": "concept", "mapping_kind": "exact",
         "decision": "accepted_by_automatic_checks"},
        {"source_record_id": "api:3", "concept_id": "instance", "mapping_kind": "exact",
         "decision": "accepted_by_automatic_checks"},
    ]}


def _binding(table, row, **kwargs):
    return {"record_id": f"{table}:{row}", "source_table": table, "row_number": row,
            "object_type_id": "type", "snapshot_id": "s", "status": "accepted",
            "mapping_kind": "template_instance", **kwargs}


def _projection(table, **kwargs):
    return {"source_table": table, "object_type_id": "type", "snapshot_id": "s",
            "status": "accepted", **kwargs}


PLAN = {"object_types": [{"id": "type", "category": "business_type"}]}


def test_unique_source_union_includes_exact_only_api_and_zero_binding_template_tables(tmp_path):
    path = _index(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    memberships = ({"record_id": f"measure:{i}", "table": "measure", "row_number": i,
                    "type_id": "type", "mapping_kind": "shares_definition_type_template",
                    "status": "definition_template_match", "reference_variants_pending": True}
                   for i in (1, 2))
    result = summarize_definition_coverage(path, _group(), plan=PLAN,
        definition_memberships=memberships,
        template_bindings=iter([_binding("measure", 2), _binding("measure", 2),
                                _binding("wide", 1), _binding("measure", 3, status="rejected")]),
        template_projections=[_projection(t) for t in ("measure", "wide", "zero_binding")])
    total = result["totals"]
    assert total["indexed_definition_rows"] == 9
    assert total["exact_alignment_rows"] == total["membership_rows"] == total["template_binding_rows"] == 2
    assert total["rows_with_type_binding"] == 4
    assert total["rows_without_type_binding"] == 5
    assert total["rows_with_multiple_binding_paths"] == 2
    assert total["type_binding_fraction"] == 4 / 9
    assert total["template_scope_rows"] == 6
    assert total["template_binding_fraction_within_template_scope"] == 2 / 6
    assert total["rows_outside_template_scope"] == 3
    assert total["bound_rows_outside_template_scope"] == 1
    assert result["tables_outside_template_scope"] == ["api"]
    assert result["by_table"]["api"]["rows_with_type_binding"] == 1
    assert result["by_table"]["zero_binding"]["in_template_scope"] is True
    assert result["by_table"]["zero_binding"]["rows_without_type_binding"] == 1
    assert result["artifact_counts"]["template_accepted"] == 3  # duplicate evidence, one source row
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_removed_types_stale_bindings_wrong_row_and_reference_cards_are_not_coverage(tmp_path):
    result = summarize_definition_coverage(_index(tmp_path, partial=True), _group(), plan=PLAN,
        template_bindings=[_binding("wide", 1, object_type_id="removed"),
                           _binding("wide", 1, snapshot_id="old"),
                           _binding("wide", 1, row_number=2),
                           _binding("reference", 1)],
        template_projections=[_projection("api", object_type_id="removed"),
                              _projection("unindexed")])
    assert result["totals"]["rows_with_type_binding"] == 2
    assert result["totals"]["template_binding_rows"] == 0
    assert result["template_scope_tables_without_indexed_definitions"] == ["unindexed"]
    assert result["source_index_partial"] is True
    assert result["source_index_rows_omitted_cap"] == 7
    rejected = result["excluded_artifact_counts"]
    assert rejected["template:no_accepted_business_type"] == 1
    assert rejected["template:snapshot_mismatch"] == 1
    assert rejected["template:source_row_not_in_definition_index"] == 2


def test_snapshot_mismatch_fails_and_live_index_path_is_read_only(tmp_path):
    path = _index(tmp_path)
    index = SimpleNamespace(path=path)
    with pytest.raises(ValueError, match="snapshot"):
        summarize_definition_coverage(index, _group(), data=SimpleNamespace(snapshot_id="old"))
    assert summarize_definition_coverage(index, _group())["totals"]["indexed_definition_rows"] == 9


def test_ambiguous_record_requires_card_or_row_witness(tmp_path):
    path = _index(tmp_path, rows=[("api", 1), ("api", 2)])
    with sqlite3.connect(path) as db:
        db.execute("UPDATE card_sources SET record_id='same'")
    group = _group()
    alignment = group["record_alignments"][0]
    alignment["source_record_id"] = "same"
    group["record_alignments"] = [alignment]
    ambiguous = summarize_definition_coverage(path, group)
    assert ambiguous["totals"]["rows_with_type_binding"] == 0
    assert ambiguous["excluded_artifact_counts"]["exact:ambiguous_source_row"] == 1
    alignment["source_card_id"] = "c0"
    exact = summarize_definition_coverage(path, group)
    assert exact["totals"]["rows_with_type_binding"] == 1


def test_empty_inventory_keeps_stable_counts_and_unknown_fraction(tmp_path):
    result = summarize_definition_coverage(_index(tmp_path, rows=[]), {"concepts": []})
    assert result["totals"]["indexed_definition_rows"] == 0
    assert result["totals"]["type_binding_fraction"] is None
    assert result["totals"]["template_binding_fraction_within_template_scope"] is None
    assert result["by_table"] == {}
