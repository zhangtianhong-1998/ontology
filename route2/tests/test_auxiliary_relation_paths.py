"""Source-declared machine/quantity paths, never domain-name heuristics."""
import asyncio
import csv
from copy import deepcopy

import pytest

from ontology_r2.auxiliary_relation_paths import build_auxiliary_path_projections, replay_auxiliary_path
from ontology_r2.configuration_relations import discover_configuration_relations, _relation_text_candidate
from ontology_r2.configuration_relation_stage import adjudicate_configuration_relations, _balanced_configuration_candidates
from ontology_r2.models import BuildPlan, DerivedType, RelationPlan, TablePlan
from ontology_r2.ontology_instances import materialize_ontology_instances
from ontology_r2.storage import Dataset, digest, qi, write_yaml
from test_semantic_bindings import PROFILE


def _case(tmp_path):
    root = tmp_path / "input"
    schemas = {
        "machine": ({"id": "record ID", "machine_code": "machine code", "name": "business name",
                     "definition": "definition", "tenant": "tenant", "unit": "unit"},
                    [{"id": "1", "machine_code": "EQ1", "name": "炉温监测", "definition": "炉温监测是一种观察炉体温度的服务",
                      "tenant": "north", "unit": "C"}]),
        "quantity": ({"id": "record ID", "quantity_code": "quantity code", "name": "business name",
                      "definition": "definition", "tenant": "tenant", "unit": "unit"},
                     [{"id": "2", "quantity_code": "Q1", "name": "温度", "definition": "温度是热状态的摄氏测量值",
                       "tenant": "north", "unit": "C"}]),
        "configuration": ({"id": "record ID", "machine_ref": "references factory.machine.machine_code",
                           "quantity_ref": "references factory.quantity.quantity_code", "tenant": "tenant",
                           "unit": "unit", "relation_text": "relationship description"},
                          [{"id": "3", "machine_ref": "EQ1", "quantity_ref": "Q1", "tenant": "north",
                            "unit": "C", "relation_text": "炉温监测引用温度"}]),
    }
    for name, (columns, rows) in schemas.items():
        base = {"schema": "factory", "table_name": name}
        write_yaml(root / "schema/tables" / (name + ".yaml"), {**base, "table_comment": "Source definitions",
            "columns": [{"column_name": col, "column_comment": comment, "ordinal_position": n,
                         "data_type": "text", "is_not_null": False} for n, (col, comment) in enumerate(columns.items(), 1)]})
        write_yaml(root / "schema/constraints" / (name + ".yaml"), {**base,
            "constraints": [{"constraint_name": "pk", "definition": "PRIMARY KEY (id)"}]})
        write_yaml(root / "schema/foreign_keys" / (name + ".yaml"), {**base, "foreign_keys": []})
        (root / "data").mkdir(exist_ok=True)
        with (root / "data" / (name + ".csv")).open("w") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader(); writer.writerows(rows)
    work = tmp_path / "work"; work.mkdir()
    data = Dataset(root, work)
    rows, ids, proofs = {}, {}, {}
    for name in schemas:
        table = "factory." + name
        row = next(data.rows(table)); rows[name] = row; ids[name] = data.record_id(table, row)
        field = "relation_text" if name == "configuration" else "definition"
        eid = "record:" + digest([data.snapshot_id, ids[name], field])[:24]; proofs[name] = eid
        data.evidence[eid] = {"id": eid, "origin": "observed_record", "raw_fragment": row[field],
            "raw_fragment_truncated": False, "source_ref": {"snapshot_id": data.snapshot_id,
                "table": table, "record_id": ids[name], "row": row["__r2_row"], "column": field}}
    types, concepts, alignments = [], [], []
    for name, parent in (("machine", "GeneralObject"), ("quantity", "Measure")):
        types.append(DerivedType(id="type:" + name, parent=parent, category="business_type",
            label=rows[name]["name"], definition=rows[name]["definition"], evidence_ids=[proofs[name]],
            source_properties=[{"role": "description", "source_table": "factory." + name,
                                "source_column": "definition", "evidence_ids": [proofs[name]]}]))
        concepts.append({"id": "concept:" + name, "ontology_level": "type", "ontology_type_id": "type:" + name})
        alignments.append({"id": "alignment:" + name, "source_record_id": ids[name],
            "mapping_kind": "exact", "concept_id": "concept:" + name, "evidence_ids": [proofs[name]]})
    source_plan = RelationPlan(id="record:configuration-machine", source_table="factory.configuration",
        target_table="factory.machine", mode="identifier", source_column="machine_ref", target_column="machine_code",
        predicate="record:reference", witness_snapshot_id=data.snapshot_id,
        evidence_scope="sample_semantic_with_full_technical_check",
        witnessed_pairs=[{"source_record_id": ids["configuration"], "target_record_id": ids["machine"]}],
        evidence_ids=["schema:factory.configuration:machine_ref", proofs["configuration"]])
    plan = BuildPlan(object_types=types, relations=[source_plan],
        relation_types=[DerivedType(id="record:reference", parent="points_to", definition="a source reference",
            evidence_scope="sample_semantic_with_full_technical_check", evidence_ids=source_plan.evidence_ids)],
        tables=[TablePlan(table="factory." + name, object_type="GeneralObject",
                          evidence_ids=["schema:factory." + name]) for name in schemas])
    rules = [{"rule_id": "checked:" + name, "status": "checked_technical", "snapshot_id": data.snapshot_id,
        "source": {"table": "factory.configuration", "field": name + "_ref"},
        "target": {"table": "factory." + name, "field": name + "_code"}, "transform": {"operator": "identity"},
        "selector": {}, "scope_bindings": {"tenant": "tenant", "unit": "unit"},
        "verification": {"scan_scope": "full_input", "checks": {"eligible_references": 1,
            "unique_matches": 1, "ambiguous_matches": 0, "missing_in_input": 0, "missing_scope": 0}}}
        for name in ("machine", "quantity")]
    return data, plan, concepts, alignments, rules


class _Judge:
    def __init__(self, reverse=False): self.calls = 0; self.reverse = reverse
    async def ask(self, stage, packet, model):
        self.calls += 1
        assert stage == "configuration_relation"
        return model.model_validate({"status": "proposed", "direction": "target_to_source" if self.reverse else "source_to_target",
            "parent_relation": "points_to", "label": "温度引用炉温监测" if self.reverse else "炉温监测引用温度",
            "definition": "服务引用测量定义", "configuration_quote": packet["configuration_text"],
            "source_definition_quote": "炉温监测是一种观察炉体温度的服务",
            "target_definition_quote": "温度是热状态的摄氏测量值"})


def test_auxiliary_witness_to_existing_compiler_and_concrete_instance_replay(tmp_path):
    data, plan, concepts, alignments, rules = _case(tmp_path)
    try:
        paths = build_auxiliary_path_projections(data, plan, rules, concepts, alignments)
        assert paths["pending"] == [] and len(paths["candidates"]) == 1
        candidate = paths["candidates"][0]
        assert candidate["configuration_record_id"] not in {candidate[side + "_definition"]["record_id"] for side in ("source", "target")}
        llm = _Judge()
        result = asyncio.run(adjudicate_configuration_relations(data, PROFILE, plan, paths["candidates"],
            concepts, alignments, llm, checked_rules=rules))
        assert llm.calls == 1 and result["coverage"]["accepted"] == 1
        assert len(result["plan"].object_types) == 2
        materialize = lambda cs: materialize_ontology_instances(data, result["plan"], concepts=concepts,
            record_alignments=alignments, record_relations=result["assertions"], configuration_candidates=cs)
        instances = materialize([candidate])
        assert len(instances["objects"]) == 2 and len(instances["assertions"]) == 1
        assert instances["pending"] == []
        missing = materialize([])
        assert missing["assertions"] == [] and missing["pending"][0]["reason"] == "record_relation_replay_failed"
        stripped = deepcopy(candidate); stripped.pop("auxiliary_path_proof")
        assert materialize([stripped])["assertions"] == []
        table = data.tables["factory.configuration"]
        data.db.execute(f"UPDATE {qi(table['sql_name'])} SET unit='F'")
        assert materialize([candidate])["assertions"] == []
        assert "scope" in materialize([candidate])["pending"][0]["detail"] or "lookup" in materialize([candidate])["pending"][0]["detail"]
    finally: data.close()


@pytest.mark.parametrize("failure", ["absent_declaration", "ambiguous", "scope", "unit", "direction", "limit"])
def test_auxiliary_paths_reject_unsupported_composition_and_changes(tmp_path, failure):
    data, plan, concepts, alignments, rules = _case(tmp_path)
    try:
        table = data.tables["factory.configuration"]
        if failure == "absent_declaration":
            for column in table["columns"]: column["column_comment"] = "code"
        elif failure == "ambiguous":
            remote = data.tables["factory.quantity"]
            data.db.execute(f"INSERT INTO {qi(remote['sql_name'])} SELECT * FROM {qi(remote['sql_name'])}")
        elif failure in ("scope", "unit"):
            field, value = ("tenant", "south") if failure == "scope" else ("unit", "F")
            data.db.execute(f"UPDATE {qi(table['sql_name'])} SET {qi(field)}=?", [value])
        elif failure == "direction":
            rules[0]["source"], rules[0]["target"] = rules[0]["target"], rules[0]["source"]
        paths = build_auxiliary_path_projections(data, plan, rules, concepts, alignments,
            max_witnesses=0 if failure == "limit" else 10)
        assert paths["candidates"] == [] and paths["pending"]
    finally: data.close()


def test_auxiliary_path_does_not_transfer_or_reverse_original_predicate(tmp_path):
    data, plan, concepts, alignments, rules = _case(tmp_path)
    try:
        paths = build_auxiliary_path_projections(data, plan, rules, concepts, alignments)
        llm = _Judge(reverse=True)
        result = asyncio.run(adjudicate_configuration_relations(data, PROFILE, plan, paths["candidates"],
            concepts, alignments, llm, checked_rules=rules))
        assert result["coverage"]["accepted"] == 0
        assert "predicate and direction" in result["steps"][0]["reason"]
        forged = deepcopy(paths["candidates"][0])
        forged["auxiliary_path_proof"]["paths"][0]["rule"]["scope_bindings"] = {}
        with pytest.raises(ValueError, match="hash"):
            replay_auxiliary_path(data, forged, plan=plan)
    finally: data.close()


def test_self_pair_and_unrelated_json_text_are_not_semantic_candidates(tmp_path):
    data, plan, concepts, alignments, rules = _case(tmp_path)
    try:
        table = data.tables["factory.configuration"]
        spec = {"spec_id": "self", "configuration_table": "factory.configuration", "source_code_column": "machine_ref",
            "target_code_column": "quantity_ref", "source_definition_table": "factory.machine",
            "target_definition_table": "factory.machine", "source_definition_code_column": "machine_code",
            "target_definition_code_column": "machine_code", "relation_text_column": "relation_text"}
        data.db.execute(f"UPDATE {qi(table['sql_name'])} SET quantity_ref='EQ1'")
        found = discover_configuration_relations(data, plan, concepts, alignments, [spec])
        assert found["candidates"][0]["status"] == "unresolved"
        assert found["candidates"][0]["reason"] == "configuration_references_resolve_to_the_same_record"
        data.db.execute(f"UPDATE {qi(table['sql_name'])} SET relation_text=?", ['{"main":"EQ1","region":"OTHER"}'])
        assert _relation_text_candidate(data, "factory.configuration", {"machine_ref", "quantity_ref"}) is None
    finally: data.close()


def test_alternative_joins_to_one_configuration_pair_share_a_judgement_slot():
    base = {"configuration_record_id": "witness", "predicate_literal": "A references B",
        "source_definition": {"record_id": "a"}, "target_definition": {"record_id": "b"}}
    chosen, duplicates = _balanced_configuration_candidates([{**base, "id": "a", "spec_id": "s1"},
        {**base, "id": "b", "spec_id": "s2", "auxiliary_path_proof": {"id": "proof"}}], 2)
    assert len(chosen) == 1 and duplicates == 1
    assert chosen[0]["id"] == "b"


def test_witness_dedup_retains_direction_predicate_and_source_definition_differences():
    base = {"configuration_record_id": "witness", "predicate_literal": "A references B",
        "relation_text_column": "statement", "source_type_id": "type:a", "target_type_id": "type:b",
        "source_definition": {"record_id": "a"}, "target_definition": {"record_id": "b"}}
    variants = [base, {**base, "source_definition": {"record_id": "b"}, "target_definition": {"record_id": "a"}},
        {**base, "predicate_literal": "A contains B"}, {**base, "relation_text_column": "other_statement"},
        {**base, "source_type_id": "type:other"}, {**base, "direction_status": "target_to_source"},
        {**base, "predicate_name": "definition_reference"}]
    candidates = [{**value, "id": str(i), "spec_id": "s"} for i, value in enumerate(variants)]
    selected, duplicates = _balanced_configuration_candidates(candidates, 20)
    assert len(selected) == len(variants) and duplicates == 0


@pytest.mark.parametrize("declaration", ["引用 machine_code", "引用 machine.machine_code",
                                        "north引用machine_code，south引用quantity_code"])
def test_original_unqualified_and_conditional_columns_close_unique_definition_paths(tmp_path, declaration):
    data, plan, concepts, alignments, rules = _case(tmp_path)
    try:
        table = data.tables["factory.configuration"]
        for column in table["columns"]:
            if column["column_name"] == "machine_ref": column["column_comment"] = declaration
            if column["column_name"] == "tenant": column["column_comment"] = "Resource type discriminator"
        if declaration.startswith("north"):
            rules[0]["selector"] = {"tenant": "north"}
        paths = build_auxiliary_path_projections(data, plan, rules, concepts, alignments)
        assert len(paths["candidates"]) == 1
        proof = paths["proofs"][0]["paths"][0]["declaration"]
        assert proof["kind"] == "declared_column_reference"
        assert proof["quote"] == declaration
        assert proof["target"] == {"table": "factory.machine", "field": "machine_code"}
        if declaration.startswith("north"):
            assert proof["condition"] == {"tenant": "north"}
            rules[0]["selector"] = {}
            assert build_auxiliary_path_projections(data, plan, rules, concepts, alignments)["candidates"] == []
    finally: data.close()


def test_bare_target_column_ambiguity_or_disjunction_cannot_choose_a_definition_table(tmp_path):
    data, plan, concepts, alignments, rules = _case(tmp_path)
    try:
        table = data.tables["factory.configuration"]
        field = next(x for x in table["columns"] if x["column_name"] == "machine_ref")
        field["column_comment"] = "引用 machine_code 或 quantity_code"
        assert build_auxiliary_path_projections(data, plan, rules, concepts, alignments)["candidates"] == []
        field["column_comment"] = "引用 machine_code"
        data.tables["factory.quantity"]["column_names"].append("machine_code")
        assert build_auxiliary_path_projections(data, plan, rules, concepts, alignments)["candidates"] == []
    finally: data.close()


def test_structured_resource_path_uses_existing_binding_instance_replay(tmp_path):
    data, plan, concepts, alignments, rules = _case(tmp_path)
    try:
        table = data.tables["factory.configuration"]
        data.db.execute(f"UPDATE {qi(table['sql_name'])} SET relation_text='This resource uses the configured quantity.'")
        paths = build_auxiliary_path_projections(data, plan, rules, concepts, alignments)
        class Judge:
            async def ask(self, stage, packet, model):
                assert stage == "configuration_purpose"
                evidence = next(x for x in packet["evidence"] if x.get("column") == "relation_text"
                                and x["kind"] == "configuration_value")
                return model.model_validate({"status": "proposed", "source_type_id": "type:machine", "role": "measure",
                    "evidence_id": evidence["evidence_id"], "purpose_quote": evidence["text"],
                    "reason": "The source resource explicitly uses the quantity referenced by this configuration."})
        result = asyncio.run(adjudicate_configuration_relations(data, PROFILE, plan, paths["candidates"],
            concepts, alignments, Judge(), checked_rules=rules))
        assert result["coverage"]["accepted"] == 1
        assert result["bindings"][0]["contract"]["auxiliary_path_candidate"] == paths["candidates"][0]
        materialize = lambda: materialize_ontology_instances(data, result["plan"], concepts=concepts,
            record_alignments=alignments, ontology_bindings=result["bindings"])
        assert len(materialize()["assertions"]) == 1
        for field in table["columns"]:
            if field["column_name"] == "quantity_ref": field["column_comment"] = "Opaque code"
        rejected = materialize()
        assert rejected["assertions"] == []
        assert rejected["pending"][0]["reason"] == "auxiliary_path_execution_failed"
    finally: data.close()


def test_declared_definition_root_resolves_bare_code_without_matching_resource_uses(tmp_path):
    from ontology_r2.configuration_relations import declared_reference_evidence
    data, plan, concepts, alignments, rules = _case(tmp_path)
    try:
        field = next(x for x in data.tables["factory.configuration"]["columns"] if x["column_name"] == "quantity_ref")
        field["column_comment"] = "对应度量定义的 quantity_code"
        data.tables["factory.machine"]["column_names"].append("quantity_code")
        roots = {"factory.machine": {"GeneralObject"}, "factory.quantity": {"Measure"}}
        proof = declared_reference_evidence(data, rules[1], definition_tables=roots)
        assert proof["target"]["table"] == "factory.quantity"
        assert proof["declared_target_roots"] == ["Measure"]
        assert declared_reference_evidence(data, rules[1]) is None
        roots["factory.machine"] = {"Measure"}
        assert declared_reference_evidence(data, rules[1], definition_tables=roots) is None
    finally: data.close()
