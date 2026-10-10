"""Reference/role boundaries use source evidence, not the demonstration schema."""
import asyncio
import csv
import pytest

from ontology_r2.column_role_inference import infer_column_role_candidates
from ontology_r2.concept_candidates import _field_roles
from ontology_r2.discovery import discover_and_check, validate_candidate
from ontology_r2.fact_observations import build_fact_observation_candidates
from ontology_r2.instance_bundles import _relation_bundle
from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from ontology_r2.storage import Dataset, write_yaml
from test_column_role_inference import FakeLLM
from test_discovery import SmallDataset


def _write_table(root, name, columns, rows):
    base = {"schema": "lab", "table_name": name}
    write_yaml(root / "schema/tables" / f"{name}.yaml", {
        **base, "table_comment": "", "columns": [
            {"column_name": field, "ordinal_position": position + 1,
             "data_type": "text", "column_comment": comment}
            for position, (field, comment) in enumerate(columns.items())]})
    write_yaml(root / "schema/constraints" / f"{name}.yaml", {
        **base, "constraints": [{"constraint_name": "pk", "constraint_type": "p",
                                  "definition": "PRIMARY KEY (id)"}]})
    write_yaml(root / "schema/foreign_keys" / f"{name}.yaml", {**base, "foreign_keys": []})
    (root / "data").mkdir(exist_ok=True)
    with (root / "data" / f"{name}.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _dataset(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    return Dataset(tmp_path / "input", work)


def test_context_values_survive_without_contaminating_definition_scope(tmp_path):
    row = {"id": "1", "name": "泵站", "definition": "泵站是输送流体的设施。",
           "language": "zh-CN", "data_source_code": "SYSTEM_A", "owner_team": "engineering",
           "sync_type": "FULL", "namespace": "plant", "version": "2"}
    _write_table(tmp_path / "input", "catalog", dict.fromkeys(row, ""), [row])
    data = _dataset(tmp_path)
    try:
        table = data.tables["lab.catalog"]
        # A replayed legacy model suggestion must not restore the scope bug.
        table["inferred_semantic_roles"] = [{"column": field, "role": "scope",
            "status": "source_verified_role_candidate"} for field in
            ("language", "data_source_code", "owner_team", "sync_type", "namespace", "version")]
        roles = _field_roles(table)
        assert not roles.get("scope")
        assert roles["identity"] == ["namespace", "version"]
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            card = index.all_cards(1)["cards"][0]
            assert card["scope"] == {"namespace": "plant", "version": "2"}
            context = {entry["column"]: entry["value"] for role in ("provenance", "metadata", "identity")
                       for entry in card["fields"].get(role, ())}
            assert context == {field: row[field] for field in row if field not in ("id", "name", "definition")}
        finally:
            index.close()
    finally:
        data.close()


def test_opaque_no_comment_roles_preserve_identity_and_context(tmp_path):
    row = {"id": "1", "q1": "摩擦系数", "q2": "摩擦系数表示两接触面的阻力与正压力之比。",
           "q3": "de-DE", "q4": "INSTRUMENT_A", "q5": "tenant-east"}
    _write_table(tmp_path / "input", "c17", dict.fromkeys(row, ""), [row])
    data = _dataset(tmp_path)
    try:
        proposals = [{"column": field, "role": role,
            "observations": [{"row_number": 1, "value": row[field]}]} for field, role in
            (("q1", "name"), ("q2", "description"), ("q3", "metadata"),
             ("q4", "provenance"), ("q5", "identity"))]
        llm = FakeLLM({"proposals": proposals})
        report = asyncio.run(infer_column_role_candidates(data, llm, {"enabled": True}))
        assert len(report["candidates"]) == 5
        assert {"provenance", "metadata", "identity"} <= set(llm.packets[0]["allowed_roles"])
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            card = index.all_cards(1)["cards"][0]
            assert card["kind"] == "definition"
            assert card["scope"] == {"q5": "tenant-east"}
            assert card["fields"]["metadata"][0]["value"] == "de-DE"
            assert card["fields"]["provenance"][0]["value"] == "INSTRUMENT_A"
        finally:
            index.close()
    finally:
        data.close()


def test_reference_selector_mentioning_definition_reaches_relation_packet(tmp_path):
    root = tmp_path / "input"
    _write_table(root, "routing", {"id": "", "route_kind": "引用类型：编码指定目标定义",
        "note_description": ""}, [{"id": "1", "route_kind": "C17",
            "note_description": "规则引用设备类别定义，限制允许选择的设备。"}])
    _write_table(root, "catalog", {"id": "", "definition_key": "定义编码",
        "name": "", "definition": ""}, [{"id": "9", "definition_key": "C17",
            "name": "设备类别", "definition": "设备类别用于按工作原理区分设备。"}])
    data = _dataset(tmp_path)
    try:
        source = {"table": "lab.routing", "field": "route_kind"}
        target = {"table": "lab.catalog", "field": "definition_key"}
        candidate = {"candidate_id": "opaque_reference", "source": source, "target": target}
        check = validate_candidate(data, candidate)
        rule = {**candidate, "rule_id": "ref_rule", "status": "checked_technical",
                "selector": {}, "scope_bindings": {}, "transform": {"operator": "identity"},
                "verification": {"scan_scope": "full_input", "checks": check["checks"]}}
        assert "route_kind" not in _field_roles(data.tables["lab.routing"]).get("description", ())
        bundle, reason = _relation_bundle(data, rule, {
            "max_joint_pairs_per_rule": 2, "max_bundle_bytes": 16000})
        assert reason is None
        assert bundle["rule"]["source"] == source
        assert bundle["examples"]["positive"][0]["matching_raw_value"] == "C17"
        assert bundle["limits"]["technical_match_is_business_relation"] is False
    finally:
        data.close()


def test_composite_scope_discovered_after_renaming_without_comments_or_foreign_keys():
    data = SmallDataset({
        "plant.c17": (["q7", "q8"], [("east", "X"), ("east", "Y"), ("west", "X")], []),
        "plant.c29": (["p4", "p5"], [("east", "X"), ("east", "Y"), ("west", "X")], []),
    })
    try:
        found = discover_and_check(data, {"value_index_mode": "full_distinct",
            "max_indexed_fields": 0, "max_candidate_validations": 32})
        candidate = next(item for item in found["candidates"] if
            item["source"] == {"table": "plant.c17", "field": "q8"} and
            item["target"] == {"table": "plant.c29", "field": "p5"} and
            item.get("suggested_scope_bindings") == {"q7": "p4"} and
            item.get("suggested_selector") == {"q7": "east"})
        check = next(item for item in found["checks"] if item["candidate_id"] == candidate["candidate_id"])
        assert candidate["condition_discovery"] == "observed_composite_key_scope"
        assert check["checks"]["unique_matches"] == 2
        assert check["checks"]["ambiguous_matches"] == 0
        assert check["decision"]["semantic_relation"] == "unresolved"
    finally:
        data.close()


def test_opaque_numeric_collision_is_never_a_semantic_relation():
    data = SmallDataset({"plant.c17": (["q7"], [("1",), ("2",)], ["q7"]),
                         "plant.c29": (["p4"], [("1",), ("2",)], ["p4"])})
    try:
        found = discover_and_check(data, {"value_index_mode": "full_distinct",
            "max_indexed_fields": 0, "max_candidate_validations": 4})
        assert all(item["numeric_overlap_only"] for item in found["candidates"])
        assert all(item["decision"]["semantic_relation"] == "unresolved" for item in found["checks"])
    finally:
        data.close()


@pytest.mark.parametrize("namespace", ["zone-A", "7"])
def test_constant_namespace_can_recall_composite_scope_without_an_outside_branch(namespace):
    data = SmallDataset({
        "plant.c17": (["q7", "q8"], [(namespace, "X")], []),
        "plant.c29": (["p4", "p5"], [(namespace, "X"), ("other", "X")], []),
    })
    try:
        found = discover_and_check(data, {"value_index_mode": "full_distinct",
            "max_indexed_fields": 0, "max_candidate_validations": 32})
        candidate = next(item for item in found["candidates"] if
            item["source"] == {"table": "plant.c17", "field": "q8"} and
            item["target"] == {"table": "plant.c29", "field": "p5"} and
            item.get("suggested_scope_bindings") == {"q7": "p4"})
        check = next(item for item in found["checks"] if item["candidate_id"] == candidate["candidate_id"])
        assert candidate["condition_evidence"]["scoped_unique_gain"] == 1
        assert check["checks"]["unique_matches"] == 1
        assert check["checks"]["outside_eligible_references"] == 0
        assert check["decision"]["semantic_relation"] == "unresolved"
        assert "plant.c17.q7" in found["coverage"]["constant_scope_fields_considered"]
    finally:
        data.close()


@pytest.mark.parametrize("target_rows", [
    [("zone-A", "X"), ("zone-A", "X"), ("zone-B", "X")],
    [("zone-B", "X"), ("zone-C", "X")],
    [("zone-A", "X")],
])
def test_constant_scope_needs_actual_cooccurrence_and_unique_gain(target_rows):
    data = SmallDataset({
        "plant.c17": (["q7", "q8"], [("zone-A", "X")], []),
        "plant.c29": (["p4", "p5"], target_rows, []),
    })
    try:
        found = discover_and_check(data, {"value_index_mode": "full_distinct",
            "max_indexed_fields": 0, "max_candidate_validations": 32,
            "max_conditional_pair_checks": 2})
        assert not any(item["source"] == {"table": "plant.c17", "field": "q8"}
            and item["target"] == {"table": "plant.c29", "field": "p5"}
            and item.get("suggested_scope_bindings") == {"q7": "p4"}
            for item in found["candidates"])
        assert found["coverage"]["conditional_scope_checks"] <= 2
        if len(target_rows) == 1:
            assert found["coverage"]["conditional_scope_checks"] == 0
    finally:
        data.close()


def test_fact_coordinates_keep_tenant_identity_separate(tmp_path):
    rows = [{"id": str(index), "tenant_id": tenant, "region_code": "R1",
             "period": "2026Q1", "sales_amount": "100"}
            for index, tenant in enumerate(("a", "b"), 1)]
    _write_table(tmp_path / "input", "readings", dict.fromkeys(rows[0], ""), rows)
    data = _dataset(tmp_path)
    try:
        result = build_fact_observation_candidates(data)
        table = next(item for item in result["tables"] if item["table"] == "lab.readings")
        assert "tenant_id" in table["coordinate_columns"]
        assert len(table["candidates"]) == 2
        assert {item["coordinate_values"]["tenant_id"] for item in table["candidates"]} == {"a", "b"}
        assert all(item["source_row_count"] == 1 for item in table["candidates"])
        assert table["accepted_dimension_link_fields"] == []
        limited = build_fact_observation_candidates(data, max_dimension_columns=1)
        limited_table = next(item for item in limited["tables"] if item["table"] == "lab.readings")
        assert limited_table["coordinate_grain_status"] == "unresolved_omitted_coordinate_columns"
        assert limited["coverage"]["partial"] is True
    finally:
        data.close()
