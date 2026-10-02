"""Cross-root repair retains directed, source-backed relation contracts."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ontology_r2.configuration_relation_stage import (
    _balanced_configuration_candidates, compile_configuration_relation,
    adjudicate_configuration_relations, verify_configuration_assertion,
)
from ontology_r2.configuration_relations import _relation_text_candidate, discover_configuration_relations
from ontology_r2.models import BuildPlan, DerivedType
from ontology_r2.ontology_instances import materialize_ontology_instances
from ontology_r2.relation_contract import relation_scope_conflicts, predicate_endpoint_errors
from ontology_r2.semantic_bindings import compile_template_relations
from ontology_r2.storage import qi
from ontology_r2.validation import validate_plan
from test_configuration_relations import _input
from test_configuration_relation_stage import _decision
from test_semantic_bindings import PROFILE, _configuration_case, _PurposeModel


def _resource_case():
    names = {"business_object": ("entity", "GeneralObject", "检查对象"),
             "measure": ("quantity", "Measure", "合格率"),
             "dimension": ("coordinate", "Dimension", "地区")}
    evidence, slots = {}, []
    types = [DerivedType(id="resource", parent="GeneralObject", label="查询服务",
                         definition="A configured query resource", category="business_type", evidence_ids=["measure"])]
    values = {}
    for role, (key, root, name) in names.items():
        evidence[role] = {"origin": "observed_record", "raw_fragment": name,
            "source_ref": {"table": "definitions", "record_id": "row1", "column": role, "snapshot_id": "snap"}}
        types.append(DerivedType(id=key, parent=root, label=name, definition=name,
                                category="business_type", evidence_ids=[role]))
        values[role] = name
        slots.append({"name": role, "role": role, "target_type_id": key,
                      "fixed_value": name, "evidence_ids": [role],
                      "value_evidence": {"record_id": "row1", "column": role, "quote": name}})
    projection = {"template_id": "resource_projection", "object_type_id": "resource",
                  "root_type": "GeneralObject", "status": "accepted", "snapshot_id": "snap",
                  "contract_hash": "proved-projection", "source_table": "definitions", "slots": slots}
    bindings = [{"id": "binding", "template_id": "resource_projection", "object_type_id": "resource",
                 "status": "accepted", "mapping_kind": "template_instance", "snapshot_id": "snap",
                 "record_id": "row1", "slot_values": values}]
    return SimpleNamespace(snapshot_id="snap", evidence=evidence, tables={}), BuildPlan(object_types=types), projection, bindings


def test_resource_slots_have_distinct_replayable_meaning_and_do_not_create_calculations():
    data, plan, projection, bindings = _resource_case()
    result = compile_template_relations(data, PROFILE, plan, [projection], bindings)
    assert result["pending"] == []
    assert {item.predicate_name for item in result["plan"].relation_types} == {
        "resource_object_binding", "resource_measure_binding", "resource_scope_binding"}
    assert all(item.parent == "related_to" for item in result["plan"].relation_types)
    assert validate_plan(result["plan"], data, PROFILE) == []
    data.evidence["business_object"]["raw_fragment"] = "检查对象运营"
    assert any("complete literal source field" in value for value in validate_plan(result["plan"], data, PROFILE))


def test_resource_topic_substring_or_unmapped_instance_cannot_authorize_a_binding():
    data, plan, projection, bindings = _resource_case()
    data.evidence["business_object"]["raw_fragment"] = "查询检查对象的结果"
    result = compile_template_relations(data, PROFILE, plan, [projection], bindings)
    assert len(result["bindings"]) == 2
    assert "topic substring" in result["pending"][0]["reason"]
    missing = compile_template_relations(data, PROFILE, plan, [projection], [])
    assert missing["bindings"] == []
    assert len(missing["pending"]) == 3


def test_resource_member_code_does_not_guess_a_dimension_from_its_slot_role():
    data, plan, projection, bindings = _resource_case()
    bindings[0]["slot_values"]["dimension"] = "EAST"
    result = compile_template_relations(data, PROFILE, plan, [projection], bindings)
    assert not any(item.predicate_name == "resource_scope_binding" for item in result["plan"].relation_types)
    assert "independently resolved reference" in result["pending"][0]["reason"]


def test_configuration_resource_purpose_uses_the_registered_directed_contract(tmp_path):
    case = _configuration_case(tmp_path, "This resource publishes the quantity identified by the quantity reference.")
    data, plan, concepts, alignments, candidate, rules = case
    try:
        plan.object_types[0].parent = "GeneralObject"
        result = asyncio.run(adjudicate_configuration_relations(
            data, PROFILE, plan, [candidate], concepts, alignments, _PurposeModel(), checked_rules=rules))
        assert result["coverage"]["accepted"] == 1
        relation = result["plan"].relation_types[0]
        assert relation.predicate_name == "resource_measure_binding"
        assert relation.domain == ["type:profit"] and relation.range == ["type:revenue"]
    finally:
        data.close()


def test_configuration_projection_reads_current_witness_definition_not_seed(tmp_path):
    data, plan, concepts, alignments, spec = _input(tmp_path)
    try:
        memberships = []
        for index, alignment in enumerate(alignments):
            item = plan.object_types[index]
            current_eid = item.source_properties[0].evidence_ids[0]
            old_eid = "other-seed:" + str(index)
            data.evidence[old_eid] = deepcopy(data.evidence[current_eid])
            data.evidence[old_eid]["source_ref"]["record_id"] = "different-seed-record"
            item.source_properties[0].evidence_ids = [old_eid]
            memberships.append({"id": "template:" + str(index), "object_type_id": item.id,
                "record_id": alignment["source_record_id"], "mapping_kind": "template_instance",
                "status": "accepted", "snapshot_id": data.snapshot_id, "evidence_ids": [current_eid]})
        candidate = next(item for item in discover_configuration_relations(data, plan, concepts, [], [spec],
            memberships=memberships)["candidates"] if item["status"] == "endpoint_verified_candidate")
        # Complete row values may exist in the snapshot before any caller has
        # registered their evidence IDs. Endpoint binding must read them here.
        for membership in memberships:
            data.evidence.pop(membership["evidence_ids"][0], None)
        compiled, assertion = compile_configuration_relation(data, PROFILE, plan, candidate, _decision(),
                                                              concepts, [], memberships=memberships)
        assert assertion["subject"] == candidate["source_definition"]["record_id"]
        assert assertion["endpoint_scope"] == "typed_record_witness"
        assert assertion["endpoint_mapping_kinds"] == {"source": "template_instance", "target": "template_instance"}
        assert compiled.relation_types[0].evidence_scope == "one_configuration_witness_with_type_bindings"
        assert validate_plan(compiled, data, PROFILE) == []
        data.evidence[memberships[0]["evidence_ids"][0]]["source_ref"]["snapshot_id"] = "stale"
        with pytest.raises(ValueError, match="complete snapshot record"):
            compile_configuration_relation(data, PROFILE, plan, candidate, _decision(), concepts, [], memberships=memberships)
    finally:
        data.close()


def test_relationship_text_recall_excludes_rule_primary_key_and_numeric_columns(tmp_path):
    data, plan, concepts, alignments, spec = _input(tmp_path)
    try:
        table = data.tables[spec["configuration_table"]]
        # An opaque PK with a relationship-sounding comment still is an ID.
        table["columns"][0]["column_comment"] = "relationship rule identifier"
        assert _relation_text_candidate(data, spec["configuration_table"], {"metric_ref", "measure_ref"}) == "relation_text"
        table["pk"] = []
        data.db.execute(f"UPDATE {qi(table['sql_name'])} SET relation_text='123'")
        assert _relation_text_candidate(data, spec["configuration_table"], {"metric_ref", "measure_ref"}) is None
    finally:
        data.close()


def test_configuration_selection_visits_other_specs_and_patterns_before_more_duplicates():
    base = {"source_type_id": "left", "target_type_id": "right", "source_code": "L1", "target_code": "R1"}
    candidates = [{**base, "id": "a" + str(i), "spec_id": "a", "predicate_literal": "L1 references R1"}
                  for i in range(100)]
    candidates += [{**base, "id": "a-other", "spec_id": "a", "predicate_literal": "L1 conditionally references R1"},
                   {**base, "id": "b", "spec_id": "b", "predicate_literal": "L1 references R1"}]
    selected, duplicated = _balanced_configuration_candidates(candidates + [candidates[0]], 3)
    assert {item["id"] for item in selected} == {"a0", "a-other", "b"}
    assert duplicated == 1


def test_only_source_declared_classification_attributes_are_exempt_from_scope_conflicts():
    data = SimpleNamespace(snapshot_id="snap", tables={"defs": {"columns": [
        {"column_name": "kind", "column_comment": "业务分类"}]}}, evidence={})
    types = []
    for label in ("region", "channel"):
        data.evidence[label] = {"origin": "observed_record", "raw_fragment": label,
            "source_ref": {"snapshot_id": "snap", "table": "defs", "column": "kind"}}
        types.append(DerivedType(id=label, parent="Dimension", definition=label, category="business_type",
            evidence_ids=[label], applicability_scope={"kind": label}, source_properties=[{
                "role": "scope", "source_table": "defs", "source_column": "kind", "evidence_ids": [label]}]))
    assert relation_scope_conflicts(*types, data) == []
    assert relation_scope_conflicts(*types) == ["kind"]
    types[0].identity_qualifiers = {"tenant": "A"}
    types[1].identity_qualifiers = {"tenant": "B"}
    assert relation_scope_conflicts(*types, data) == ["identity:tenant"]
    data.tables["defs"]["columns"][0]["column_comment"] = "tenant classification"
    assert relation_scope_conflicts(*types, data) == ["identity:tenant", "kind"]


def test_cross_root_predicate_name_cannot_bypass_its_endpoint_meaning():
    data, plan, projection, bindings = _resource_case()
    result = compile_template_relations(data, PROFILE, plan, [projection], bindings)
    relation = next(item for item in result["plan"].relation_types if item.predicate_name == "resource_measure_binding")
    assert predicate_endpoint_errors(relation, plan.object_types) == []
    relation.domain = ["quantity"]
    assert predicate_endpoint_errors(relation, plan.object_types)
    relation.predicate_name = "denotes"
    assert predicate_endpoint_errors(relation, plan.object_types)


def test_configuration_instance_replay_rejects_swapped_pair_and_changed_source(tmp_path):
    data, plan, concepts, alignments, spec = _input(tmp_path)
    try:
        candidate = next(item for item in discover_configuration_relations(data, plan, concepts, alignments, [spec])[
            "candidates"] if item["status"] == "endpoint_verified_candidate")
        compiled, assertion = compile_configuration_relation(data, PROFILE, plan, candidate, _decision(), concepts, alignments)
        relation = compiled.relation_types[0]
        verify_configuration_assertion(data, assertion, relation, candidate, concepts, alignments, plan=compiled)
        changed = deepcopy(assertion)
        changed["source_record_pair"]["target"] = changed["source_record_pair"]["source"]
        with pytest.raises(ValueError, match="endpoint pair"):
            verify_configuration_assertion(data, changed, relation, candidate, concepts, alignments, plan=compiled)
        table = data.tables[spec["configuration_table"]]
        data.db.execute(f"UPDATE {qi(table['sql_name'])} SET relation_text='经营利润不依赖收入'")
        with pytest.raises(ValueError, match="predicate and direction|text is absent, changed"):
            verify_configuration_assertion(data, assertion, relation, candidate, concepts, alignments, plan=compiled)
    finally:
        data.close()


def test_configuration_assertion_materializes_only_its_replayed_record_pair(tmp_path):
    data, plan, concepts, alignments, spec = _input(tmp_path)
    try:
        candidate = next(item for item in discover_configuration_relations(data, plan, concepts, alignments, [spec])[
            "candidates"] if item["status"] == "endpoint_verified_candidate")
        compiled, assertion = compile_configuration_relation(data, PROFILE, plan, candidate, _decision(), concepts, alignments)

        def materialize(saved_assertion, candidates):
            return materialize_ontology_instances(data, compiled, concepts=concepts,
                record_alignments=alignments, record_relations=[saved_assertion],
                configuration_candidates=candidates)

        # Explicit configuration assertions have no physical RelationPlan;
        # their source candidate and complete decision authorize this path.
        assert not assertion.get("source_relation_plan_id")
        accepted = materialize(assertion, [candidate])
        assert accepted["pending"] == []
        assert len(accepted["objects"]) == 2
        assert len(accepted["assertions"]) == 1
        edge = accepted["assertions"][0]
        nodes = {item["id"]: item for item in accepted["objects"]}
        assert nodes[edge["subject"]]["source_ref"]["record_id"] == assertion["source_record_pair"]["source"]
        assert nodes[edge["object"]]["source_ref"]["record_id"] == assertion["source_record_pair"]["target"]
        assert edge["configuration_candidate_id"] == candidate["id"]
        assert edge["instance_scope"] == "definition_graph_not_observed_business_values"
        assert accepted["coverage"]["cartesian_products_created"] == 0
        assert accepted["coverage"]["llm_calls"] == 0

        tampered = deepcopy(assertion)
        tampered["configuration_decision"]["direction"] = "target_to_source"
        for saved, candidates, reason in (
            (assertion, [], "source candidate is absent"),
            (tampered, [candidate], "predicate and direction"),
        ):
            rejected = materialize(saved, candidates)
            assert len(rejected["objects"]) == 2
            assert rejected["assertions"] == []
            assert len(rejected["pending"]) == 1
            assert rejected["pending"][0]["reason"] == "record_relation_replay_failed"
            assert reason in rejected["pending"][0]["detail"]
    finally:
        data.close()
