"""Non-fruit, opaque-schema regression cases for source-bound instance contracts."""
import asyncio
import csv
from copy import deepcopy

import pytest

from ontology_r2.fact_observations import build_fact_observation_candidates
from ontology_r2.fact_type_binding import bind_fact_observations
from ontology_r2.configuration_relations import infer_configuration_specs, discover_configuration_relations
from ontology_r2.configuration_relation_stage import adjudicate_configuration_relations
from ontology_r2.models import BuildPlan, DerivedType, RelationPlan
from ontology_r2.ontology_instances import materialize_ontology_instances
from ontology_r2.relation_contract import canonical_relation_id
from ontology_r2.storage import Dataset, digest, qi, write_yaml
from test_semantic_bindings import PROFILE


def _table(root, name, columns, rows):
    """Use a distinct domain and blank metadata; names are not ontology rules."""
    base = {"schema": "operations", "table_name": name}
    write_yaml(root / "schema/tables" / f"{name}.yaml", {**base, "table_comment": "",
        "columns": [{"column_name": column, "ordinal_position": i + 1, "data_type": "text",
                     "column_comment": comment, "is_not_null": False, "default_value": None}
                    for i, (column, comment) in enumerate(columns.items())]})
    write_yaml(root / "schema/constraints" / f"{name}.yaml", {**base, "constraints": [
        {"constraint_name": "pk", "constraint_type": "p", "definition": "PRIMARY KEY (id)"}]})
    write_yaml(root / "schema/foreign_keys" / f"{name}.yaml", {**base, "foreign_keys": []})
    (root / "data").mkdir(exist_ok=True)
    with (root / "data" / f"{name}.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _proof(data, table, row, column):
    record_id = data.record_id(table, row)
    key = "record:" + digest([data.snapshot_id, record_id, column])[:24]
    data.evidence[key] = {"id": key, "origin": "observed_record", "raw_fragment": row[column],
        "raw_fragment_truncated": False, "source_ref": {"snapshot_id": data.snapshot_id,
            "table": table, "row": row["__r2_row"], "record_id": record_id, "column": column}}
    return key


def _membership_case(tmp_path):
    root = tmp_path / "input"
    _table(root, "x17", dict.fromkeys(["id", "c1", "c2", "c3"], ""), [
        {"id": "R1", "c1": "组件", "c2": "组件是独立装配的可复用部件", "c3": "R2"},
        {"id": "R2", "c1": "组件", "c2": "组件是独立装配的可复用部件", "c3": "R1"},
        {"id": "R3", "c1": "组件", "c2": "组件是独立装配的可复用部件", "c3": "R2"},
    ])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    table = "operations.x17"
    rows = list(data.rows(table))
    proof = _proof(data, table, rows[0], "c2")
    item = DerivedType(id="type:component", parent="GeneralObject", category="business_type",
        label="组件", definition=rows[0]["c2"], evidence_ids=[proof], source_properties=[{
            "role": "description", "source_table": table, "source_column": "c2", "evidence_ids": [proof]}])
    semantics = [["description", "c2", rows[0]["c2"]], ["name", "c1", rows[0]["c1"]]]
    template = {"id": "definition_template:" + digest([data.snapshot_id, table, item.id, semantics])[:24],
        "type_id": item.id, "table": table, "representative_record_id": data.record_id(table, rows[0]),
        "representative_row_number": 1, "semantic_fields": [
            {"role": role, "column": field, "value": value} for role, field, value in semantics],
        "semantic_fingerprint": digest(semantics), "evidence_ids": [proof]}
    members = [{"id": "member:" + row["id"], "table": table, "row_number": row["__r2_row"],
        "record_id": data.record_id(table, row), "type_id": item.id, "template_id": template["id"],
        "snapshot_id": data.snapshot_id, "status": "definition_template_match", "evidence_ids": [proof],
        "entity_identity_claim": False, "relationship_inheritance": False,
        "verification": {"method": "complete_original_source_field_equality",
            "semantic_fingerprint": template["semantic_fingerprint"], "columns": ["c2", "c1"]}}
        for row in rows]
    return data, BuildPlan(object_types=[item]), {"memberships": members, "definition_templates": [template]}


def test_definition_members_replay_own_rows_and_keep_reference_variants_separate(tmp_path):
    data, plan, inputs = _membership_case(tmp_path)
    try:
        result = materialize_ontology_instances(data, plan, **inputs)
        assert result["coverage"]["definition_record_instances"] == 3
        assert result["coverage"]["partial"] is False
        assert result["assertions"] == []
        assert len({item["id"] for item in result["objects"]}) == 3
        for node in result["objects"]:
            assert not node["business_observation_claim"]
            assert any(data.evidence[key]["source_ref"].get("record_id") == node["source_ref"]["record_id"]
                       for key in node["evidence_ids"])
        # The primary key still matches, but a changed definition must fail.
        data.db.execute(f"UPDATE {qi(data.tables['operations.x17']['sql_name'])} SET c2=? WHERE id='R2'",
                        ["组件已经改变定义"])
        rejected = materialize_ontology_instances(data, plan, **inputs)
        assert rejected["coverage"]["definition_record_instances"] == 2
        assert rejected["pending"][0]["reason"] == "definition_membership_replay_failed"
    finally:
        data.close()


@pytest.mark.parametrize("change", ["missing_template", "fingerprint", "identity_claim", "representative"])
def test_definition_membership_never_trusts_an_accepted_flag_alone(tmp_path, change):
    data, plan, inputs = _membership_case(tmp_path)
    try:
        inputs["memberships"] = inputs["memberships"][1:2]
        if change == "missing_template":
            inputs["definition_templates"] = []
        elif change == "fingerprint":
            inputs["memberships"][0]["verification"]["semantic_fingerprint"] = "forged"
        elif change == "identity_claim":
            inputs["memberships"][0]["entity_identity_claim"] = True
        else:
            data.db.execute(f"UPDATE {qi(data.tables['operations.x17']['sql_name'])} SET c2=? WHERE id='R1'",
                            ["不同部件定义"])
        result = materialize_ontology_instances(data, plan, **inputs)
        assert result["objects"] == result["assertions"] == []
        assert result["pending"][0]["reason"] == "definition_membership_replay_failed"
    finally:
        data.close()


def test_one_witnessed_record_relation_materializes_without_table_wide_inheritance(tmp_path):
    data, plan, inputs = _membership_case(tmp_path)
    try:
        proof = plan.object_types[0].evidence_ids[0]
        base = DerivedType(id="record_reference", parent="points_to", label="points_to",
            definition="来源记录引用", domain=["type:component"],
            range=["type:component"], evidence_ids=[proof], evidence_scope="sample_semantic_with_full_technical_check")
        relation_id = canonical_relation_id("points_to", "type:component", "type:component")
        promoted = base.model_copy(update={"id": relation_id, "category": "business_relation_type"})
        pair = {"source": inputs["memberships"][0]["record_id"], "target": inputs["memberships"][1]["record_id"]}
        execution = RelationPlan(id="plan:reference", source_table="operations.x17", target_table="operations.x17",
            source_column="c3", target_column="id", mode="identifier", predicate=base.id,
            evidence_ids=[proof], evidence_scope="sample_semantic_with_full_technical_check",
            witness_snapshot_id=data.snapshot_id, witnessed_pairs=[{
                "source_record_id": pair["source"], "target_record_id": pair["target"]}])
        plan.relation_types = [base, promoted]
        plan.relations = [execution]
        assertion = {"id": "record:relation", "predicate": relation_id, "source_record_pair": pair,
            "source_relation_plan_id": execution.id, "subject_type": "type:component", "object_type": "type:component",
            "identity_scope": "input_snapshot", "evidence_ids": [proof], "decision": {"status": "accepted"}}
        inputs["record_relations"] = [assertion]
        result = materialize_ontology_instances(data, plan, **inputs)
        assert len(result["assertions"]) == 1
        nodes = {node["id"]: node for node in result["objects"]}
        edge = result["assertions"][0]
        assert nodes[edge["subject"]]["source_ref"]["record_id"] == pair["source"]
        assert nodes[edge["object"]]["source_ref"]["record_id"] == pair["target"]
        assert result["coverage"]["cartesian_products_created"] == 0
        # R3 also refers to R2 but has no semantic witness, so it gets no edge.
        inputs["record_relations"] = [{**assertion, "source_record_pair": {
            **pair, "source": inputs["memberships"][2]["record_id"]}}]
        assert materialize_ontology_instances(data, plan, **inputs)["assertions"] == []
        inputs["record_relations"] = [assertion]
        data.db.execute(f"UPDATE {qi(data.tables['operations.x17']['sql_name'])} SET c3='R3' WHERE id='R1'")
        changed = materialize_ontology_instances(data, plan, **inputs)
        assert changed["assertions"] == []
        assert changed["pending"][0]["reason"] == "record_relation_replay_failed"
    finally:
        data.close()


def _quantity_case(tmp_path, *, comments=True, root_type="Measure", unit="kWh", ambiguous=False):
    root = tmp_path / "input"
    _table(root, "t42", {"id": "", "site_code": "", "period": "", "energy_value": "Energy (kWh)" if comments else "", "unit": ""}, [
        {"id": "1", "site_code": "SITE_A", "period": "2025Q1", "energy_value": "12", "unit": unit},
        {"id": "2", "site_code": "SITE_A" if ambiguous else "SITE_B", "period": "2025Q1", "energy_value": "13", "unit": unit}])
    _table(root, "dictionary_x", {"id": "", "x1": "", "x2": ""}, [
        {"id": "d1", "x1": "Energy", "x2": "Energy is the quantity consumed during one observation interval."}])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    row = next(data.rows("operations.dictionary_x"))
    proof = _proof(data, "operations.dictionary_x", row, "x2")
    item = DerivedType(id="type:energy", parent=root_type, label="Energy", definition=row["x2"],
        category="business_type", unit="kWh", evidence_ids=[proof], source_properties=[{
            "role": "description", "source_table": "operations.dictionary_x", "source_column": "x2", "evidence_ids": [proof]}])
    return data, BuildPlan(object_types=[item])


class _QuantityModel:
    def __init__(self):
        self.calls = 0

    async def ask(self, task, payload, schema):
        self.calls += 1
        assert task == "fact_type_binding"
        candidate = payload["type_candidates"][0]
        proof = candidate["full_source_definitions"][0]
        return schema.model_validate({"status": "bind", "type_id": candidate["id"],
            "source_column_quote": payload["source_declaration"], "type_definition_quote": candidate["definition"],
            "source_definition_evidence_id": proof["evidence_id"], "type_source_quote": proof["value"], "unit_column": "unit"})


@pytest.mark.parametrize("comments", [True, False])
def test_numeric_measure_binds_opaque_tables_with_or_without_column_comments(tmp_path, comments):
    data, plan = _quantity_case(tmp_path, comments=comments)
    try:
        observed = build_fact_observation_candidates(data)
        model = _QuantityModel()
        result = asyncio.run(bind_fact_observations(data, observed, plan, model))
        assert model.calls == 1
        assert result["coverage"]["instances_created"] == 2
        assert {item["observed_value"] for item in result["instances"]} == {"12", "13"}
        assert {item["type"] for item in result["instances"]} == {"type:energy"}
    finally:
        data.close()


@pytest.mark.parametrize("case", ["dimension", "unit", "ambiguous", "tenant"])
def test_measure_expansion_preserves_unit_coordinate_and_identity_gates(tmp_path, case):
    data, plan = _quantity_case(tmp_path, root_type="Dimension" if case == "dimension" else "Measure",
                                unit="kW" if case == "unit" else "kWh", ambiguous=case == "ambiguous")
    try:
        if case == "tenant":
            plan.object_types[0].identity_qualifiers = {"tenant": "TENANT_A"}
        model = _QuantityModel()
        observed = build_fact_observation_candidates(data)
        result = asyncio.run(bind_fact_observations(data, observed, plan, model))
        assert result["instances"] == []
        if case == "dimension":
            assert model.calls == 0
        else:
            expected = {"unit": "unit_missing_or_conflicting", "ambiguous": "ambiguous_or_incomplete_coordinates",
                        "tenant": "applicability_scope_unverified_or_conflicting"}[case]
            assert expected in result["coverage"]["skipped_reasons"]
    finally:
        data.close()


class _RelationModel:
    async def ask(self, task, payload, schema):
        assert task == "configuration_relation"
        return schema.model_validate({"status": "proposed", "direction": "source_to_target",
            "parent_relation": "contains", "label": "contains", "definition": "装配线包含组件",
            "configuration_quote": payload["configuration_text"],
            "source_definition_quote": payload["endpoint_a"]["definition_evidence"][0]["quote"],
            "target_definition_quote": payload["endpoint_b"]["definition_evidence"][0]["quote"]})


@pytest.mark.parametrize("statement,expected", [("装配线包含组件", 1), ("装配线不包含组件", 0), ("S1 T1", 0)])
def test_generic_same_root_configuration_requires_full_positive_source_statement(tmp_path, statement, expected):
    root = tmp_path / "input"
    for name, code, definition in (("x1", "S1", "装配线是按生产流程组织的装配设施"),
                                    ("x2", "T1", "组件是可独立更换的装配部件")):
        _table(root, name, {"id": "", "c1": "", "c2": ""}, [{"id": code, "c1": code, "c2": definition}])
    _table(root, "x3", {"id": "", "a1": "", "a2": "", "a3": ""}, [
        {"id": "C1", "a1": "S1", "a2": "T1", "a3": statement}])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        types, concepts, alignments, rules = [], [], [], []
        for index, label in ((1, "装配线"), (2, "组件")):
            table = f"operations.x{index}"
            row = next(data.rows(table))
            key = _proof(data, table, row, "c2")
            type_id, concept_id = f"type:t{index}", f"concept:c{index}"
            types.append(DerivedType(id=type_id, label=label, parent="GeneralObject", definition=row["c2"],
                category="business_type", evidence_ids=[key], source_properties=[{
                    "role": "description", "source_table": table, "source_column": "c2", "evidence_ids": [key]}]))
            concepts.append({"id": concept_id, "ontology_type_id": type_id, "ontology_level": "type"})
            alignments.append({"id": f"alignment:{index}", "mapping_kind": "exact", "concept_id": concept_id,
                "source_record_id": data.record_id(table, row), "evidence_ids": [key]})
            rules.append({"rule_id": f"rule:{index}", "status": "checked_technical", "snapshot_id": data.snapshot_id,
                "source": {"table": "operations.x3", "field": f"a{index}"}, "target": {"table": table, "field": "c1"},
                "transform": {"operator": "identity"}, "verification": {"scan_scope": "full_input", "checks": {
                    "eligible_references": 1, "unique_matches": 1, "ambiguous_matches": 0, "missing_in_input": 0}}})
        plan = BuildPlan(object_types=types)
        inferred = infer_configuration_specs(data, plan, concepts, alignments, rules)
        if statement == "S1 T1":
            assert inferred["spec_candidates"] == []
            return
        assert len(inferred["spec_candidates"]) == 1
        assert inferred["spec_candidates"][0]["spec"]["relation_text_column"] == "a3"
        candidates = discover_configuration_relations(data, plan, concepts, alignments,
            [entry["spec"] for entry in inferred["spec_candidates"]])
        result = asyncio.run(adjudicate_configuration_relations(data, PROFILE, plan, candidates["candidates"],
            concepts, alignments, _RelationModel(), checked_rules=rules))
        assert result["coverage"]["accepted"] == expected
        assert len(result["assertions"]) == expected
        if not expected:
            assert "does not prove" in result["steps"][0]["reason"]
    finally:
        data.close()


def _schema_identity_case(tmp_path, *, declaration="Device energy (kWh); tenant=TENANT_A", tenants=("TENANT_A", "TENANT_A")):
    from ontology_r2.fact_schema_induction import _packet

    root = tmp_path / "input"
    _table(root, "x9", {"id": "", "site_code": "", "tenant_id": "", "period": "", "energy_value": declaration}, [
        {"id": str(i), "site_code": "SITE_A", "tenant_id": tenant, "period": "2025Q1", "energy_value": "12"}
        for i, tenant in enumerate(tenants, 1)])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    observed = build_fact_observation_candidates(data)
    report = observed["tables"][0]
    packet = _packet(data, report, report["value_fields"][0], BuildPlan(), None, 8)
    decision = {"status": "proposed", "label": "Device energy", "business_object_quote": "Device",
        "quantity_quote": "energy", "identity_evidence_id": packet["source_declaration"]["evidence_id"],
        "identity_quote": declaration, "scope_bindings": {"tenant": "tenant_id"},
        "identity_qualifiers": {"tenant": "TENANT_A"}, "unit": "kWh", "unit_quote": declaration}
    return data, observed, report, packet, decision


def test_schema_identity_needs_declaration_coordinate_and_full_input_constant(tmp_path):
    from ontology_r2.fact_schema_induction import compile_fact_schema, template_binding_decision

    data, observed, report, packet, decision = _schema_identity_case(tmp_path)
    try:
        item, template = compile_fact_schema(data, BuildPlan(), report, packet, decision)
        assert item.identity_qualifiers == template["identity_qualifiers"] == {"tenant": "TENANT_A"}
        assert template["identity_evidence_ids"]
        assert all(data.evidence[key]["verification"]["scan_scope"] == "full_input"
                   for key in template["identity_evidence_ids"])
        plan = BuildPlan(object_types=[item])
        result = asyncio.run(bind_fact_observations(data, observed, plan, _QuantityModel(), field_templates=[template]))
        assert result["coverage"]["instances_created"] == 1
        assert result["instances"][0]["observation_coordinates"]["tenant_id"] == "TENANT_A"
        # A canonical/type-template substitution cannot erase namespace identity.
        plan.object_types[0].identity_qualifiers = {}
        with pytest.raises(ValueError, match="stale or differs"):
            template_binding_decision(template, data, data.tables[report["table"]], "energy_value",
                                      report["coordinate_columns"], plan)
    finally:
        data.close()


@pytest.mark.parametrize("change", ["unquoted", "missing_binding", "varying", "blank"])
def test_schema_identity_cannot_come_from_unverified_samples_or_free_model_text(tmp_path, change):
    from ontology_r2.fact_schema_induction import compile_fact_schema

    data, _, report, packet, decision = _schema_identity_case(tmp_path,
        declaration="Device energy (kWh)" if change == "unquoted" else "Device energy (kWh); tenant=TENANT_A",
        tenants=("TENANT_A", "TENANT_B") if change == "varying" else ("TENANT_A", "")
        if change == "blank" else ("TENANT_A", "TENANT_A"))
    try:
        if change == "missing_binding":
            decision["scope_bindings"] = {}
        with pytest.raises(ValueError, match="Identity qualifier"):
            compile_fact_schema(data, BuildPlan(), report, packet, decision)
    finally:
        data.close()


def test_varying_tenants_remain_observation_coordinates_without_inventing_types(tmp_path):
    from ontology_r2.fact_schema_induction import compile_fact_schema

    data, observed, report, packet, decision = _schema_identity_case(tmp_path,
        declaration="Device energy (kWh)", tenants=("TENANT_A", "TENANT_B"))
    try:
        decision["identity_qualifiers"] = {}
        item, template = compile_fact_schema(data, BuildPlan(), report, packet, decision)
        assert item.identity_qualifiers == {}
        result = asyncio.run(bind_fact_observations(data, observed, BuildPlan(object_types=[item]),
                            _QuantityModel(), field_templates=[template]))
        assert result["coverage"]["instances_created"] == 2
        assert len({node["id"] for node in result["instances"]}) == 2
        assert {node["observation_coordinates"]["tenant_id"] for node in result["instances"]} == {"TENANT_A", "TENANT_B"}
    finally:
        data.close()


@pytest.mark.parametrize("tenants", [("TENANT_A", "TENANT_A"), ("TENANT_A", "TENANT_B")])
def test_empty_qualifiers_cannot_erase_an_explicit_identity_declaration(tmp_path, tenants):
    from ontology_r2.fact_schema_induction import compile_fact_schema

    data, _, report, packet, decision = _schema_identity_case(tmp_path, tenants=tenants)
    try:
        decision["identity_qualifiers"] = {}
        with pytest.raises(ValueError, match="preserve every explicit source identity"):
            compile_fact_schema(data, BuildPlan(), report, packet, decision)
    finally:
        data.close()


@pytest.mark.parametrize("suffix", ["tenant=", "tenant>=TENANT_A", "tenant=TENANT_A or TENANT_B",
                                   "tenant=TENANT_A; tenant=TENANT_B"])
def test_unsupported_explicit_identity_declaration_stays_unresolved(tmp_path, suffix):
    from ontology_r2.fact_schema_induction import compile_fact_schema

    data, _, report, packet, decision = _schema_identity_case(tmp_path, declaration="Device energy (kWh); " + suffix)
    try:
        decision["identity_qualifiers"] = {}
        with pytest.raises(ValueError, match="Identity qualifier declaration"):
            compile_fact_schema(data, BuildPlan(), report, packet, decision)
    finally:
        data.close()


def test_ordinary_key_value_clause_is_not_automatically_an_identity_qualifier(tmp_path):
    from ontology_r2.fact_schema_induction import compile_fact_schema

    data, observed, report, packet, decision = _schema_identity_case(tmp_path,
        declaration="Device energy (kWh); limit=100", tenants=("TENANT_A", "TENANT_B"))
    try:
        decision["identity_qualifiers"] = {}
        item, template = compile_fact_schema(data, BuildPlan(), report, packet, decision)
        assert item.identity_qualifiers == {}
        result = asyncio.run(bind_fact_observations(data, observed, BuildPlan(object_types=[item]),
            _QuantityModel(), field_templates=[template]))
        assert len(result["instances"]) == 2
    finally:
        data.close()
