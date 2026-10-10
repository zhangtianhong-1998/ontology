"""Lexical references need a code, a name/alias and a scoped member declaration."""
import copy
import json

import pytest

from ontology_r2.discovery import association_match_sql, discover_json_reference_candidates, validate_candidate
from ontology_r2.models import BuildPlan, DerivedType
from ontology_r2.semantic_references import (build_semantic_references, verify_semantic_reference_assertion,
                                            verify_semantic_reference_relation)
from ontology_r2.storage import Dataset, digest
from test_semantic_cards import _table


def fixture(tmp_path, *, duplicate=False, wrong_name=False, identity_conflict=False):
    root = tmp_path / "input"
    _table(root, "lexicon", {"id": "row identifier", "lexeme": "term name",
        "payload": "structured lexical reference", "namespace": "namespace"}, [
        {"id": "1", "lexeme": "contractor", "payload": json.dumps({"link": {"personnel_key": "C7"}}), "namespace": "hr"},
    ])
    members = [{"id": "2", "local_key": "C7", "axis_ref": "A3", "caption": "External worker",
                "other_words": "contractor; vendor worker" if not wrong_name else "employee; staff",
                "meaning": "Employment status member: External worker", "namespace": "other" if identity_conflict else "hr"}]
    if duplicate:
        members.append({**members[0], "id": "3", "axis_ref": "A4"})
    _table(root, "enumeration_rows", {"id": "row identifier",
        "local_key": "member code scoped within axis_ref", "axis_ref": "dimension code",
        "caption": "member name", "other_words": "member aliases",
        "meaning": "membership statement", "namespace": "namespace"}, members)
    _table(root, "classification", {"id": "row identifier", "axis_key": "dimension code",
        "title": "dimension name", "definition": "dimension definition", "namespace": "namespace"}, [
        {"id": "4", "axis_key": "A3", "title": "Employment status",
         "definition": "Employment status distinguishes employment arrangements", "namespace": "hr"}])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    plan, concepts, alignments = BuildPlan(), [], []
    for table, root_type, name_column in (("fruit.lexicon", "Term", "lexeme"),
                                          ("fruit.classification", "Dimension", "title")):
        row = next(data.rows(table))
        rid = data.record_id(table, row)
        tid, cid = "type:" + root_type, "concept:" + root_type
        eid = "record:" + digest([data.snapshot_id, rid, name_column])[:24]
        data.evidence[eid] = {"id": eid, "origin": "observed_record", "raw_fragment": row[name_column],
            "raw_fragment_truncated": False, "source_ref": {"table": table, "column": name_column,
                "row": row["__r2_row"], "record_id": rid, "snapshot_id": data.snapshot_id}}
        plan.object_types.append(DerivedType(id=tid, parent=root_type, label=row[name_column],
            definition=row.get("definition", row[name_column]), evidence_ids=[eid], category="business_type"))
        concepts.append({"id": cid, "ontology_level": "type", "ontology_type_id": tid})
        alignments.append({"id": "alignment:" + root_type, "mapping_kind": "exact", "source_record_id": rid,
                           "concept_id": cid, "evidence_ids": [eid]})
    return data, plan, concepts, alignments


def test_nested_json_with_different_key_names_forms_replayable_lexical_member_path(tmp_path):
    data, plan, concepts, alignments = fixture(tmp_path)
    try:
        result = build_semantic_references(data, plan, concepts, alignments)
        assert len(result["paths"]) == 1
        assert result["coverage"]["unique_member_instances"] == 1
        assert result["coverage"]["llm_calls"] == 0
        assert not result["pending"]
        assert not plan.relation_types  # The accepted caller core is immutable.
        relation = result["plan"].relation_types[0]
        assert (relation.domain, relation.range, relation.predicate_name) == (["type:Term"], ["type:Dimension"], "term_dimension_context")
        nodes = {node["id"]: node for node in result["instances"]}
        assert {edge["predicate_name"] for edge in result["assertions"]} == {"denotes", "member_of"}
        lexical = next(node for node in nodes.values() if node["instance_kind"] == "lexical_term")
        member = next(node for node in nodes.values() if node["instance_kind"] == "dimension_member")
        assert lexical["ontology_type_id"] == "type:Term"
        assert member["ontology_type_id"] == "type:Dimension"
        assert member["type_role"] == "member_of_dimension"
        candidate = result["json_reference_candidates"][0]
        assert candidate["source"]["source_path"] == ["link", "personnel_key"]
        assert candidate["target"]["field"] == "local_key"
        sql, params = association_match_sql(data, candidate)
        assert data.db.execute(sql, params).fetchall() == [(1, 1)]
        assert result["checked_reference_rules"][0]["checks"]["unique_matches"] == 1
        assert result["reference_plans"][0]["source_path"] == ["link", "personnel_key"]
        # Reloaded evidence, rather than an ephemeral stage result, validates
        # the Term→Dimension summary without erasing the member intermediary.
        data.evidence = json.loads(json.dumps(data.evidence))
        verify_semantic_reference_relation(data, relation, result["plan"])
        for edge in result["assertions"]:
            verify_semantic_reference_assertion(data, edge, nodes, result["plan"])
    finally:
        data.close()


@pytest.mark.parametrize("flag,reason", [("duplicate", "member_code_missing_or_ambiguous_in_namespace"),
    ("wrong_name", "code_match_without_lexical_evidence"),
    ("identity_conflict", "lexical_member_identity_scope_conflict")])
def test_overlap_never_bypasses_alias_namespace_or_unique_member_evidence(tmp_path, flag, reason):
    data, plan, concepts, alignments = fixture(tmp_path, **{flag: True})
    try:
        result = build_semantic_references(data, plan, concepts, alignments)
        assert not result["assertions"]
        assert result["pending"][0]["reason"] == reason
    finally:
        data.close()


def test_accepted_path_rejects_changed_cell_and_reversed_predicate(tmp_path):
    data, plan, concepts, alignments = fixture(tmp_path)
    try:
        result = build_semantic_references(data, plan, concepts, alignments)
        edge = copy.deepcopy(result["assertions"][0])
        edge["subject"], edge["object"] = edge["object"], edge["subject"]
        with pytest.raises(ValueError, match="direction"):
            verify_semantic_reference_assertion(data, edge, result["instances"], result["plan"])
        source = data.tables["fruit.lexicon"]["sql_name"]
        data.db.execute(f'UPDATE "{source}" SET payload=?', [json.dumps({"link": {"personnel_key": "WRONG"}})])
        with pytest.raises(ValueError, match="evidence changed"):
            verify_semantic_reference_assertion(data, result["assertions"][0], result["instances"], result["plan"])
    finally:
        data.close()


def test_json_namespace_can_resolve_shared_code_but_replay_cannot_invent_scope(tmp_path):
    data, plan, concepts, alignments = fixture(tmp_path, duplicate=True)
    try:
        sql_name = data.tables["fruit.lexicon"]["sql_name"]
        data.db.execute(f'UPDATE "{sql_name}" SET payload=?',
                        [json.dumps({"link": {"personnel_key": "C7", "axis_ref": "A3"}})])
        result = build_semantic_references(data, plan, concepts, alignments)
        assert len(result["paths"]) == 1
        assert not result["reference_plans"]  # A flat table plan cannot encode a JSON namespace.
        edge = copy.deepcopy(result["assertions"][0])
        edge["proof"]["member_lookup_bindings"] = [["local_key", "C7"], ["axis_ref", "invented"]]
        with pytest.raises(ValueError, match="scope binding"):
            verify_semantic_reference_assertion(data, edge, result["instances"], result["plan"])
    finally:
        data.close()


def test_replay_rejects_new_duplicate_member_even_after_prior_lookup(tmp_path):
    data, plan, concepts, alignments = fixture(tmp_path)
    try:
        result = build_semantic_references(data, plan, concepts, alignments)
        sql_name = data.tables["fruit.enumeration_rows"]["sql_name"]
        data.db.execute(f'INSERT INTO "{sql_name}" SELECT 2, \'99\', local_key, axis_ref, caption, other_words, meaning, namespace FROM "{sql_name}" WHERE __r2_row=1')
        with pytest.raises(ValueError, match="ambiguous"):
            verify_semantic_reference_assertion(data, result["assertions"][0], result["instances"], result["plan"])
    finally:
        data.close()


def test_missing_member_declaration_is_reported_instead_of_inferred_from_same_values(tmp_path):
    data, plan, concepts, alignments = fixture(tmp_path)
    try:
        for column in data.tables["fruit.enumeration_rows"]["columns"]:
            if column["column_name"] == "local_key":
                column["column_comment"] = "opaque source identifier"
        result = build_semantic_references(data, plan, concepts, alignments)
        assert not result["paths"]
        assert result["coverage"]["partial"]
        assert result["coverage"]["member_tables_with_supported_declaration"] == 0
        assert any(item["table"] == "fruit.enumeration_rows" for item in
                   result["coverage"]["tables_without_supported_member_declaration"])
        assert result["pending"][0]["reason"] == "no_source_grounded_json_member_path"
    finally:
        data.close()


def test_json_path_validation_excludes_malformed_containers_booleans_and_reports_limits(tmp_path):
    data, *_ = fixture(tmp_path)
    try:
        source = data.tables["fruit.lexicon"]["sql_name"]
        data.db.execute(f'INSERT INTO "{source}" VALUES (2, ?, ?, ?, ?)',
                        ["2", "other", '{"link":{"personnel_key":true}}', "hr"])
        data.db.execute(f'INSERT INTO "{source}" VALUES (3, ?, ?, ?, ?)', ["3", "other", "{bad", "hr"])
        report = discover_json_reference_candidates(data, source_tables=["fruit.lexicon"],
            target_fields=[("fruit.enumeration_rows", "local_key")], max_rows_per_table=1)
        assert report["coverage"]["source_rows_not_examined"] == 2
        assert report["coverage"]["partial"]
        checked = validate_candidate(data, report["candidates"][0])
        assert checked["checks"]["source_rows"] == 3
        assert checked["checks"]["unique_matches"] == 1
        assert checked["checks"]["null_references"] == 2
        invalid = copy.deepcopy(report["candidates"][0])
        invalid["source"]["source_path"] = [0]
        with pytest.raises(ValueError, match="object keys"):
            validate_candidate(data, invalid)
    finally:
        data.close()


def test_mock_pipeline_exports_reference_path_and_renders_both_instance_hops(tmp_path, monkeypatch):
    """Exercise compilation, persisted proofs, Sink checks and the actual HTML payload."""
    import asyncio
    from pathlib import Path
    import re

    from ontology_r2.llm import StructuredLLM
    from ontology_r2.pipeline import build
    from ontology_r2.storage import read_yaml, write_yaml
    from ontology_r2.validation import validate_plan
    from test_pipeline import items

    data, *_ = fixture(tmp_path)
    data.close()
    root, output = tmp_path / "input", tmp_path / "pipeline-run"
    responses = tmp_path / "responses.yaml"
    write_yaml(responses, {})
    calls = []

    # Fix only source packet scheduling. The accepted types and alignments are
    # produced by the real compiler; no semantic-reference result is mocked.
    def packets(source, *_args, **_kwargs):
        bundles = []
        for table, root_type, name_column, description_column in (
                ("fruit.lexicon", "Term", "lexeme", None),
                ("fruit.classification", "Dimension", "title", "definition")):
            row = next(source.rows(table))
            field = lambda column: {"column": column, "value": row[column], "truncated": False}
            record = {"record_id": source.record_id(table, row), "table": table,
                "row_number": row["__r2_row"], "kind": "definition", "root_hint": root_type,
                "scope": {"namespace": row["namespace"]}, "unit": "",
                "fields": {"name": [field(name_column)], "identity": [field("namespace")],
                    "description": [field(description_column)] if description_column else []}}
            bundles.append({"bundle_id": "source:" + root_type, "task_kind": "concept_induction",
                "records": [record], "exact_alignment_record_ids": [record["record_id"]]})
        return {"bundles": bundles,
                "coverage": {"status": "complete", "partial": False, "bundles_built": 2}}

    class RecordedModel(StructuredLLM):
        async def ask(self, task, payload, schema):
            assert self.mode == "mock" and task == "concept_bundle"
            assert "compiler_error" not in payload, payload.get("compiler_error")
            record = payload["bundle"]["records"][0]
            name = record["fields"]["name"][0]["value"]
            definition = (record["fields"]["description"] or record["fields"]["name"])[0]["value"]
            self.responses[task] = {"status": "proposed", "action": "exact_definition",
                "root_type": record["root_hint"], "label": name, "definition": definition,
                "classification_basis": "other", "ontology_level": "type",
                "scope": record["scope"], "scope_roles": {"namespace": "identity"},
                "alignments": [{"record_id": record["record_id"], "mapping_kind": "exact",
                                "quote": definition}]}
            calls.append(record["root_hint"])
            return await super().ask(task, payload, schema)

    monkeypatch.setattr("ontology_r2.pipeline.build_instance_bundles", packets)
    monkeypatch.setattr("ontology_r2.pipeline.StructuredLLM", RecordedModel)
    config = {
        "dataset": str(root), "synthetic": True, "env_file": str(tmp_path / ".env"),
        "model_profile": str(Path(__file__).resolve().parents[1] / "ontologies/internal_model.yaml"),
        "llm": {"mode": "mock", "responses": str(responses), "max_calls": 2,
                "max_input_bytes": 200000, "max_reserved_tokens": 2000000},
        "incremental": {"mode": "source_mapping_only"}, "discovery": {"enabled": False},
        "mcp": {"enabled": False}, "external": {"enabled": False},
        "processing": {"materialize_all_objects": False}, "progress": {"enabled": False},
        "instance_bundles": {"enabled": True, "review": False, "concept_batch_size": 1,
            "max_llm_bundles": 2, "max_concept_bundles": 2, "max_relation_bundles": 0},
        "semantic_references": {"enabled": True},
        "ontology_instantiation": {"enabled": True, "max_records": 10, "max_assertions": 20},
    }
    manifest = asyncio.run(build(config, output))
    assert manifest["status"] != "failed", manifest
    assert sorted(calls) == ["Dimension", "Term"]
    assert manifest["llm"]["calls"] == 2
    assert manifest["semantic_references"]["llm_calls"] == 0
    paths = read_yaml(output / "semantic_reference_paths.yaml")
    assert len(paths) == 1 and not read_yaml(output / "semantic_reference_pending.yaml")
    assert manifest["ontology_instantiation"]["lexical_member_instances"] == 3
    assert manifest["ontology_instantiation"]["lexical_member_assertions"] == 2
    assert read_yaml(output / "validation.yaml")["passed"]

    # Verify exported endpoints, source proofs and predicate compatibility rather
    # than equating a successful stage return with usable graph output.
    objects = {item["id"]: item for item in items(output, "objects")}
    edges = [item for item in items(output, "assertions")
             if item.get("predicate_name") in {"denotes", "member_of"}]
    assert {(edge["predicate_name"], edge["predicate"]) for edge in edges} == {
        ("denotes", "points_to"), ("member_of", "related_to")}
    assert all(edge["subject"] in objects and edge["object"] in objects for edge in edges)
    member = next(item for item in objects.values() if item.get("instance_kind") == "dimension_member")
    assert member["semantic_role"] == "observed_dimension_member_not_dimension_definition"
    evidence = {item["id"]: item for item in items(output, "evidence")}
    assert all(set(edge["evidence_ids"]) <= evidence.keys() for edge in edges)
    plan = read_yaml(output / "extraction_plan.yaml")
    relation = next(item for item in plan["relation_types"]
                    if item.get("predicate_name") == "term_dimension_context")
    assert any(evidence[key].get("origin") == "semantic_reference_path"
               for key in relation["evidence_ids"])
    replay_work = tmp_path / "export-replay"
    replay_work.mkdir()
    replay_data = Dataset(root, replay_work)
    try:
        # Reload the exported evidence and plan into a fresh dataset. Validation
        # must not depend on an in-memory stage result or prior lookup cache.
        replay_data.evidence = evidence
        saved_plan = BuildPlan.model_validate(plan)
        assert validate_plan(saved_plan, replay_data, read_yaml(config["model_profile"])) == []
        for edge in edges:
            verify_semantic_reference_assertion(replay_data, edge, objects, saved_plan)
    finally:
        replay_data.close()

    assert manifest.get("viewer") == "viewer.html" and "viewer_error" not in manifest
    html = (output / "viewer.html").read_text(encoding="utf-8")
    embedded = re.search(r'<script id="result-data" type="application/json">(.*?)</script>', html, re.S)
    assert embedded, "Generated viewer must contain the persisted graph payload"
    payload = json.loads(embedded.group(1))
    assert relation["id"] in {item["id"] for item in payload["ontology_overview"]["relations"]}
    assert {item["predicate_name"] for item in payload["ontology"]["relation_types"]
            if item.get("predicate_name")} >= {"term_dimension_context"}
    assert {edge["id"] for edge in edges} <= {edge["id"] for edge in payload["instance_relations"]}
    assert member["id"] in {item["id"] for item in payload["instance_preview"]}
