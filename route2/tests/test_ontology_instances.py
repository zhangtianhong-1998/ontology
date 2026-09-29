"""Definition instances have executable source mappings, not type-edge products."""
import asyncio
from copy import deepcopy

from ontology_r2.calculation_stage import compile_calculation_stage
from ontology_r2.models import BuildPlan, DerivedType
from ontology_r2.ontology_instances import materialize_ontology_instances
from ontology_r2.semantic_bindings import compile_template_relations
from ontology_r2.storage import Dataset, digest, qi
from ontology_r2.template_projection import bind_projection
from test_semantic_bindings import PROFILE, _configuration_case, _PurposeModel
from test_semantic_cards import _table
from ontology_r2.configuration_relation_stage import adjudicate_configuration_relations


def test_generated_definition_memberships_materialize_with_template_replay(tmp_path):
    from test_definition_memberships import _fixture
    from ontology_r2.definition_memberships import build_definition_memberships
    from ontology_r2.group_incremental import _compiled_object_type

    data, index, group = _fixture(tmp_path)
    try:
        compiled = build_definition_memberships(data, index, group)
        plan = BuildPlan(object_types=[_compiled_object_type(item) for item in group["concepts"]])
        result = materialize_ontology_instances(data, plan, concepts=group["concepts"],
            record_alignments=group["record_alignments"], memberships=compiled["memberships"],
            definition_templates=compiled["templates"])
        assert result["coverage"]["definition_record_instances"] == 3
        assert result["coverage"]["partial"] is False
        assert result["assertions"] == []
        assert {node["source_ref"]["row"] for node in result["objects"]} == {1, 2, 3}
    finally:
        index.close()
        data.close()


def test_configuration_edges_execute_only_the_witness_paths(tmp_path):
    data, plan, concepts, alignments, candidate, rules = _configuration_case(tmp_path, "度量提供指标的量定义")
    try:
        accepted = asyncio.run(adjudicate_configuration_relations(data, PROFILE, plan, [candidate],
            concepts, alignments, _PurposeModel(), checked_rules=rules))
        result = materialize_ontology_instances(data, accepted["plan"], concepts=concepts,
            record_alignments=alignments, ontology_bindings=accepted["bindings"])
        assert len(result["objects"]) == 2 and len(result["assertions"]) == 1
        assert result["coverage"]["partial"] is False
        assert result["coverage"]["source_records_read"] == 2
        assert result["coverage"]["cartesian_products_created"] == 0
        assert {node["source_ref"]["record_id"] for node in result["objects"]} == {
            alignment["source_record_id"] for alignment in alignments}
        assert all(node["instance_kind"] == "definition_record_instance" and
                   node["properties"] and not node["business_observation_claim"] for node in result["objects"])
        limited = materialize_ontology_instances(data, accepted["plan"], concepts=concepts,
            record_alignments=alignments, ontology_bindings=accepted["bindings"], max_records=1)
        assert limited["assertions"] == [] and limited["coverage"]["partial"]
        assert limited["coverage"]["unmaterialized_type_ids"]
        forged = deepcopy(accepted["bindings"])
        forged[0]["contract"]["endpoints"]["target"]["record_id"] = candidate["configuration_record_id"]
        replay = materialize_ontology_instances(data, accepted["plan"], concepts=concepts,
            record_alignments=alignments, ontology_bindings=forged)
        assert replay["assertions"] == []
        assert replay["pending"][0]["reason"] == "semantic_binding_proof_failed_replay"
        missing = deepcopy(alignments)
        missing[0]["evidence_ids"] = ["not-observed"]
        replay = materialize_ontology_instances(data, accepted["plan"], concepts=concepts,
            record_alignments=missing, ontology_bindings=accepted["bindings"])
        assert replay["assertions"] == []
        assert any(item["reason"] == "mapping_has_no_source_evidence" for item in replay["pending"])
    finally:
        data.close()


def _template_case(tmp_path):
    root = tmp_path / "input"
    _table(root, "result_definition", {"id": "ID", "name": "名称", "definition": "定义",
        "formula": "完整公式", "measure_ref": "量名称引用"}, [
        {"id": str(i), "name": region + "利润", "definition": "利润由收入减成本，地区为" + region,
         "formula": "收入 - 成本", "measure_ref": "收入"} for i, region in ((1, "东"), (2, "西"))])
    _table(root, "amount_definition", {"id": "ID", "name": "名称", "definition": "定义"}, [
        {"id": "11", "name": "收入", "definition": "收入的通用量定义"},
        {"id": "12", "name": "成本", "definition": "成本的通用量定义"}])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    types, concepts, alignments, records = [], [], [], []
    for table in data.tables:
        for row in data.rows(table):
            rid = data.record_id(table, row)
            key = "profit" if row.get("formula") else row["name"]
            fields, props = {}, []
            for role, column in (("name", "name"), ("description", "definition"),
                                  ("formula", "formula"), ("reference", "measure_ref")):
                if not row.get(column):
                    continue
                eid = "record:" + digest([data.snapshot_id, rid, column])[:24]
                data.evidence[eid] = {"id": eid, "origin": "observed_record", "raw_fragment": row[column],
                    "source_ref": {"table": table, "row": row["__r2_row"], "record_id": rid,
                                   "column": column, "snapshot_id": data.snapshot_id}}
                fields.setdefault(role, []).append({"column": column, "value": row[column]})
                if role != "reference":
                    props.append({"role": role, "source_table": table, "source_column": column, "evidence_ids": [eid]})
            if key not in {item.id for item in types}:
                types.append(DerivedType(id=key, parent="Metric" if key == "profit" else "Measure",
                    label="利润" if key == "profit" else key, definition="可复用定义", category="business_type",
                    derivation_kind="template_projection" if key == "profit" else None,
                    source_properties=props, evidence_ids=[eid for prop in props for eid in prop["evidence_ids"]]))
            if key == "profit":
                records.append({"table": table, "record_id": rid, "row_number": row["__r2_row"], "fields": fields})
            else:
                concepts.append({"id": key, "ontology_type_id": key, "ontology_level": "type"})
                alignments.append({"id": "alignment:" + key, "source_record_id": rid,
                    "concept_id": key, "mapping_kind": "exact", "evidence_ids": types[-1].evidence_ids})
    slot_evidence = "record:" + digest([data.snapshot_id, records[0]["record_id"], "measure_ref"])[:24]
    template = {"template_id": "projection:p", "contract_hash": "p", "object_type_id": "profit",
        "source_table": records[0]["table"], "snapshot_id": data.snapshot_id, "status": "accepted", "root_type": "Metric",
        "semantic_columns": ["name", "definition", "formula"], "invariants": {"formula": "收入 - 成本"},
        "slots": [{"name": "region", "role": "dimension", "evidence_ids": [slot_evidence]},
                  {"name": "amount", "role": "measure", "target_type_id": "收入", "fixed_value": "收入", "evidence_ids": [slot_evidence]}],
        "field_templates": [{"column": "name", "template": "{region}利润"},
            {"column": "definition", "template": "利润由收入减成本，地区为{region}"},
            {"column": "formula", "template": "收入 - 成本"}, {"column": "measure_ref", "template": "{amount}"}]}
    bound = [bind_projection(data, template, record) for record in records]
    semantic = compile_template_relations(data, PROFILE, BuildPlan(object_types=types), [template], bound)
    calculation = compile_calculation_stage(data, PROFILE, semantic["plan"], {
        "concepts": concepts, "record_alignments": alignments, "template_projections": [template]})
    return data, calculation, {"template_projections": [template], "template_bindings": bound,
        "ontology_bindings": semantic["bindings"], "calculation_contracts": calculation,
        "concepts": concepts, "record_alignments": alignments}


def test_template_slots_and_formula_instances_keep_actual_coordinates(tmp_path):
    data, calculation, inputs = _template_case(tmp_path)
    try:
        result = materialize_ontology_instances(data, calculation["plan"], **inputs)
        assert result["coverage"]["definition_record_instances"] == 4
        assert result["coverage"]["slot_occurrences"] == 2
        assert len(result["assertions"]) == 6  # 2 source slots + 2 exact formula operands per source.
        primary = [node for node in result["objects"] if node["type"] == "profit"]
        assert {node["slot_values"]["region"] for node in primary} == {"东", "西"}
        assert len({node["id"] for node in result["objects"]}) == 6
        assert {edge["subject"] for edge in result["assertions"]} == {node["id"] for node in primary}
        assert all(edge["object"] in {node["id"] for node in result["objects"]} for edge in result["assertions"])
        assert {item["reason"] for item in result["pending"]} == {"template_slot_has_no_accepted_relation"}
        assert result["coverage"]["llm_calls"] == result["coverage"]["cartesian_products_created"] == 0
        # Changing a saved coordinate without changing the source must fail replay.
        forged = deepcopy(inputs)
        forged["template_bindings"][0]["slot_values"]["region"] = "北"
        rejected = materialize_ontology_instances(data, calculation["plan"], **forged)
        assert rejected["coverage"]["definition_record_instances"] == 3
        assert any(item["reason"] == "template_replay_differs_from_saved_binding" for item in rejected["pending"])
        limited = materialize_ontology_instances(data, calculation["plan"], max_assertions=0, **inputs)
        assert limited["assertions"] == []
    finally:
        data.close()


def test_future_variable_measure_value_cannot_inherit_initial_target(tmp_path):
    data, calculation, inputs = _template_case(tmp_path)
    try:
        template = inputs["template_projections"][0]
        template["template_id"], template["contract_hash"] = "projection:variable", "variable"
        template["slots"][1].pop("fixed_value")
        for binding in inputs["template_bindings"]:
            binding["template_id"] = template["template_id"]
        # The initial reviewed witness binds only the source value 收入.
        accepted = compile_template_relations(data, PROFILE, calculation["plan"], [template],
                                               inputs["template_bindings"][:1])
        inputs["ontology_bindings"] = accepted["bindings"]
        table = data.tables[template["source_table"]]
        data.db.execute(f"UPDATE {qi(table['sql_name'])} SET measure_ref=? WHERE __r2_row=2", ["成本"])
        inputs["template_bindings"][1]["slot_values"]["amount"] = "成本"
        result = materialize_ontology_instances(data, accepted["plan"], **inputs)
        assert result["coverage"]["definition_record_instances"] == 4
        assert result["coverage"]["slot_occurrences"] == 1
        assert len(result["assertions"]) == 5
        assert any(item["reason"] == "measure_slot_value_has_no_verified_target" for item in result["pending"])
        # A fresh compile also cannot publish a universal target over both values.
        retried = compile_template_relations(data, PROFILE, calculation["plan"], [template],
                                              inputs["template_bindings"])
        assert retried["bindings"] == []
        assert any("Varying measure" in item["reason"] for item in retried["pending"])
    finally:
        data.close()
