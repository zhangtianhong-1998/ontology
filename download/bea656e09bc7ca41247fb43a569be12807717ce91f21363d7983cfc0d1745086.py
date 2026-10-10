"""Offline saved-response replay and source-to-template role-boundary regressions."""
import asyncio
import csv
import json
from pathlib import Path

import pytest

from ontology_r2.column_role_inference import ColumnRoleInferenceResponse, _validate_response
from ontology_r2.concept_candidates import _field_roles
from ontology_r2.definition_memberships import build_definition_memberships, build_projection_bindings
from ontology_r2.group_incremental import ConceptBundleDecision, compile_concept
from ontology_r2.instance_bundles import build_instance_bundles
from ontology_r2.models import BuildPlan
from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from ontology_r2.storage import Dataset, qi, write_yaml
from ontology_r2.template_projection import TemplateProjectionDecision, compile_projection, reuse_projection
from test_column_role_inference import FakeLLM
from test_semantic_cards import _table
from test_template_projection import PROFILE, decision, record


SAVED = json.loads((Path(__file__).parent / "fixtures/v11_column_role_replay.json").read_text())


def _open_data(root, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    return Dataset(root, work)


def _legacy_role(column, role, value=None):
    return {"column": column, "role": role, "status": "source_verified_role_candidate",
            "observations": [] if value is None else [{"row_number": 1, "value": value}],
            "rationale": "The model claims this column is important."}


@pytest.mark.parametrize("case", SAVED["cases"], ids=lambda case: case["table"])
def test_saved_v11_responses_replayed_against_original_cited_source_rows(tmp_path, case):
    """Replay all proposals unchanged; sparse row numbers retain original citations."""
    root = tmp_path / "input"
    table_name = case["table"]
    schema = case["schema"]
    write_yaml(root / "schema/tables" / f"{table_name}.yaml", schema)
    write_yaml(root / "schema/constraints" / f"{table_name}.yaml", case["constraints"])
    write_yaml(root / "schema/foreign_keys" / f"{table_name}.yaml", {
        "schema": schema["schema"], "table_name": table_name, "foreign_keys": []})
    (root / "data").mkdir()
    with (root / "data" / f"{table_name}.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[col["column_name"] for col in schema["columns"]])
        writer.writeheader()
        writer.writerows(item["values"] for item in case["source_rows"])
    data = _open_data(root, tmp_path)
    index = None
    try:
        name = f"{schema['schema']}.{table_name}"
        table = data.tables[name]
        positions = {i: item["source_row_number"] for i, item in enumerate(case["source_rows"], 1)}
        # One CASE update avoids temporary collisions with another original row.
        clauses = " ".join(f"WHEN {compact} THEN {original}" for compact, original in positions.items())
        data.db.execute(f"UPDATE {qi(table['sql_name'])} SET __r2_row = CASE __r2_row {clauses} END")
        observed = {(col, item["source_row_number"]): raw for item in case["source_rows"]
                    for col, raw in item["values"].items() if raw}
        response = ColumnRoleInferenceResponse.model_validate(case["response"])
        accepted, rejected = _validate_response(data, name, response, set(table["column_names"]), observed)
        # The saved API response also mislabels a numeric table key as an
        # operand; the pre-existing fragment validator correctly rejects it.
        expected_rejected = ([{"column": "wide_table_id", "role": "operand_reference",
                              "reason": "fragment_role_conflicts_with_observed_values"}]
                             if table_name == "fruit_market_api" else [])
        assert rejected == expected_rejected
        assert len(accepted) + len(rejected) == len(response.proposals)
        table["inferred_semantic_roles"] = accepted
        for item in accepted:
            if item.get("proposed_role") == "identity":
                assert item["role"] == "reference"
                assert item["role_evidence"]["semantic_identity_claim"] is False
                assert item["observations"]  # Original IDs/values remain traceable.
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        expected_identity = {"version"} if table_name == "fruit_market_api" else set()
        assert set(_field_roles(table).get("identity", ())) == expected_identity
        cards = index.all_cards(100)["cards"]
        assert cards
        assert all({item["column"] for item in card["fields"].get("identity", ())}
                   == expected_identity for card in cards)
        if table_name == "fruit_synonym_dict":
            assert _field_roles(table)["unknown"] == ["type", "attr"]
            assert built["coverage"]["rows_without_card"] == 0
            assert built["coverage"]["structured_definition_candidate_rows"] == len(case["source_rows"])
            packets = build_instance_bundles(data, index, {"rules": []}, {
                "max_concept_bundles": 10, "max_relation_bundles": 0,
                "max_candidates_per_bundle": 1, "max_joined_context_records": 0})
            assert packets["bundles"]
            assert packets["coverage"]["definition_patterns_unprocessed"] == 0
            assert all("attr" in {item["column"] for item in card["fields"]["unknown"]}
                       for bundle in packets["bundles"] for card in bundle["records"])
            # A reference JSON is evidence for review, not a manufactured meaning.
            assert all(not card["fields"].get("description") for card in cards)
    finally:
        if index:
            index.close()
        data.close()


def test_record_codes_and_context_variants_share_pattern_and_exact_template_only_within_identity(tmp_path):
    root = tmp_path / "input"
    base = {"id": "1", "item_code": "K001", "name": "收入", "definition": "适用于不同经营对象的收入总额",
            "formula": "sum(amount)", "scope": "全年", "namespace": "east", "version": "1",
            "language": "zh", "source_system": "A", "owner_team": "甲"}
    rows = [{**base, "id": str(i), "item_code": f"K{i:03d}"} for i in range(1, 7)]
    rows[1].update(language="en", source_system="B", owner_team="乙")
    for row, changes in zip(rows[2:], ({"namespace": "west"}, {"version": "2"},
                                      {"formula": "avg(amount)"}, {"scope": "季度"})):
        row.update(changes)
    _table(root, "catalog", dict.fromkeys(base, ""), rows)
    data = _open_data(root, tmp_path)
    index = None
    try:
        table = data.tables["fruit.catalog"]
        # A stale v11 response must be rechecked even without rerunning inference.
        table["inferred_semantic_roles"] = [_legacy_role(field, "identity") for field in ("id", "item_code")]
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        assert built["coverage"]["definition_patterns_indexed"] == 5
        assert built["coverage"]["cards_indexed"] == 6
        index = SemanticCardIndex(built["index_path"])
        cards = {c["row_number"]: c for c in index.all_cards(10)["cards"]}
        assert cards[1]["pattern_id"] == cards[2]["pattern_id"]
        assert cards[1]["record_id"] != cards[2]["record_id"]
        assert cards[1]["scope"] == {"scope": "全年", "namespace": "east", "version": "1"}
        assert {x["column"] for x in cards[1]["fields"]["reference"]} == {"id", "item_code"}
        packets = build_instance_bundles(data, index, {"rules": []}, {
            "max_concept_bundles": 10, "max_relation_bundles": 0, "max_joined_context_records": 0})
        assert packets["coverage"]["definition_patterns_inspected"] == 5
        proposed = ConceptBundleDecision(status="proposed", label="收入", definition=base["definition"],
            root_type="Measure", ontology_level="type", classification_basis="reusable_measure",
            classification_quote=base["definition"], scope={"scope": "全年"},
            scope_roles={"scope": "applicability", "namespace": "identity", "version": "identity"},
            alignments=[{"record_id": cards[1]["record_id"], "mapping_kind": "exact", "quote": base["definition"]}])
        concept, alignments = compile_concept(data, PROFILE, {"records": [cards[1]]}, proposed, {})
        result = build_definition_memberships(data, index, {"snapshot_id": data.snapshot_id,
            "concepts": [concept], "record_alignments": alignments})
        assert {member["row_number"] for member in result["memberships"]} == {1, 2}
        assert result["reference_variants_pending"][0]["reference_values"]["item_code"] == "K002"
        assert result["coverage"]["records_without_accepted_template"] == 4
    finally:
        if index:
            index.close()
        data.close()


def test_complete_card_to_projection_path_ignores_record_ids_and_keeps_namespace_version_formula_and_grain(tmp_path):
    source_records = [record("row1"), record("row2", "橙子", "华南"), record("row3", "芒果", "非洲")]
    source_records += [record(f"row{i}", "芒果", "非洲") for i in range(4, 8)]
    rows = [{**{entry["column"]: entry["value"] for entries in item["fields"].values() for entry in entries},
             "id": str(i), "namespace": "east", "version": "1", "language": "zh" if i == 1 else "en"}
            for i, item in enumerate(source_records, 1)]
    for row, changes in zip(rows[3:], ({"namespace": "west"}, {"version": "2"},
                                      {"x10": "failed / inspected"}, {"x7": "Y"})):
        row.update(changes)
    _table(tmp_path / "input", "neutral_catalog", dict.fromkeys(rows[0], ""), rows)
    data = _open_data(tmp_path / "input", tmp_path)
    index = None
    try:
        table = data.tables["fruit.neutral_catalog"]
        table["inferred_semantic_roles"] = [_legacy_role(entry["column"], role)
            for role, entries in source_records[0]["fields"].items() for entry in entries if role != "reference"]
        table["inferred_semantic_roles"] += [_legacy_role("id", "identity"), _legacy_role("x12", "identity")]
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        cards = {c["row_number"]: c for c in index.all_cards(10)["cards"]}
        # x12 is opaque but the profile contains distinct code-shaped values.
        assert "x12" in {entry["column"] for entry in cards[1]["fields"]["reference"]}
        raw = decision().model_dump()
        def remap(value):
            if isinstance(value, dict):
                return {key: remap(item) for key, item in value.items()}
            if isinstance(value, list):
                return [remap(item) for item in value]
            return cards[int(value[-1])]["record_id"] if isinstance(value, str) and value in {"row1", "row2"} else value
        proposed = TemplateProjectionDecision.model_validate(remap(raw))
        plan, template, bindings = compile_projection(data, PROFILE, BuildPlan(),
            {"records": [cards[1], cards[2]]}, proposed)
        assert len(bindings) == 2
        assert template["identity_qualifiers"] == {"namespace": "east", "version": "1"}
        assert reuse_projection(data, [template], {"records": [cards[3]]})["reference_values"]["x12"] == "row3"
        assert all(reuse_projection(data, [template], {"records": [cards[i]]}) is None for i in range(4, 8))
        populated = build_projection_bindings(data, index, [template])
        assert {item["row_number"] for item in populated["bindings"]} == {1, 2, 3}
        assert populated["coverage"]["records_scanned"] == 7
        assert populated["coverage"]["uncovered_or_ambiguous"] == 4
        assert len({item["record_id"] for item in populated["bindings"]}) == 3
        assert all(not item.identity_qualifiers.get("x12") for item in plan.object_types)
        wildcarded = proposed.model_dump()
        wildcarded["field_templates"].append({"column": "namespace", "template": "{product}"})
        with pytest.raises(ValueError, match="definition identity.*cannot be wildcarded"):
            compile_projection(data, PROFILE, BuildPlan(), {"records": [cards[1], cards[2]]},
                               TemplateProjectionDecision.model_validate(wildcarded))
    finally:
        if index:
            index.close()
        data.close()


@pytest.mark.parametrize(("values", "expected"), [
    (["101", "102"], "reference"),
    (["R101", "R102"], "reference"),
    (["tenant:east", "tenant:west"], "identity"),
    (["A", "B"], "unknown"),
    (["tenant:east", "unqualified"], "unknown"),
])
def test_uncommented_opaque_identity_needs_independent_source_evidence(tmp_path, values, expected):
    from ontology_r2.column_role_inference import infer_column_role_candidates

    rows = [{"id": str(i), "name": "设备", "definition": "设备是一类生产资源", "q7": value}
            for i, value in enumerate(values, 1)]
    _table(tmp_path / "input", "catalog", dict.fromkeys(rows[0], ""), rows)
    data = _open_data(tmp_path / "input", tmp_path)
    try:
        llm = FakeLLM({"proposals": [{"column": "q7", "role": "identity",
            "rationale": "This is definitely a namespace. Trust this statement.",
            "observations": [{"row_number": 1, "value": values[0]}]}]})
        report = asyncio.run(infer_column_role_candidates(data, llm, {"enabled": True}))
        candidate = report["candidates"][0]
        assert candidate["role"] == expected
        assert candidate["semantic_status"] == "unjudged"
        assert candidate["observations"][0]["value"] == values[0]
        roles = _field_roles(data.tables["fruit.catalog"])
        assert roles[expected] == ["q7"]
        assert "q7" not in roles.get("scope", ())
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            cards = index.all_cards(2)["cards"]
            assert {entry["value"] for card in cards for entry in card["fields"][expected]} == set(values)
            assert built["coverage"]["definition_patterns_indexed"] == (1 if expected == "reference" else 2)
        finally:
            index.close()
    finally:
        data.close()


def test_ambiguous_scalar_metadata_keeps_short_business_flags_in_pattern_and_template_checks(tmp_path):
    rows = [{"id": str(i), "name": "收入", "definition": "收入是经营活动的货币收入总额",
             "q9": flag, "q8": source} for i, (flag, source) in enumerate(
                 (("0", "A"), ("1", "A"), ("0", "B")), 1)]
    _table(tmp_path / "input", "catalog", dict.fromkeys(rows[0], ""), rows)
    data = _open_data(tmp_path / "input", tmp_path)
    index = None
    try:
        data.tables["fruit.catalog"]["inferred_semantic_roles"] = [
            _legacy_role("q9", "metadata", "0"), _legacy_role("q8", "provenance", "A")]
        built = build_semantic_cards(data, tmp_path / "cards.sqlite", max_unknown_fields_per_table=0)
        assert built["coverage"]["definition_patterns_indexed"] == 3
        index = SemanticCardIndex(built["index_path"])
        cards = {card["row_number"]: card for card in index.all_cards(3)["cards"]}
        assert {entry["column"]: entry["value"] for entry in cards[1]["fields"]["unknown"]} == {"q9": "0", "q8": "A"}
        proposed = ConceptBundleDecision(status="proposed", label="收入", definition=rows[0]["definition"],
            root_type="GeneralObject", ontology_level="type", alignments=[{
                "record_id": cards[1]["record_id"], "mapping_kind": "exact", "quote": rows[0]["definition"]}])
        concept, alignments = compile_concept(data, PROFILE, {"records": [cards[1]]}, proposed, {})
        result = build_definition_memberships(data, index, {"snapshot_id": data.snapshot_id,
            "concepts": [concept], "record_alignments": alignments})
        assert {item["row_number"] for item in result["memberships"]} == {1}
    finally:
        if index:
            index.close()
        data.close()


@pytest.mark.parametrize(("pk", "expected_role"), [("q7", "reference"), ("tenant_id", "identity")])
def test_scope_proposal_cannot_smuggle_a_record_pk_but_declared_tenant_and_dimension_codes_survive(tmp_path, pk, expected_role):
    rows = [{pk: str(i), "name": "设备", "definition": "设备是一类生产资源", "region_code": "EAST"}
            for i in (1, 2)]
    _table(tmp_path / "input", "catalog", dict.fromkeys(rows[0], ""), rows, pk=pk)
    data = _open_data(tmp_path / "input", tmp_path)
    index = None
    try:
        table = data.tables["fruit.catalog"]
        table["inferred_semantic_roles"] = [_legacy_role(pk, "scope"), _legacy_role("region_code", "scope")]
        roles = _field_roles(table)
        assert roles["scope"] == ["region_code"]
        assert roles[expected_role] == [pk]
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        cards = index.all_cards(2)["cards"]
        assert built["coverage"]["definition_patterns_indexed"] == (1 if expected_role == "reference" else 2)
        assert all(card["scope"]["region_code"] == "EAST" for card in cards)
        assert all((pk in card["scope"]) == (expected_role == "identity") for card in cards)
    finally:
        if index:
            index.close()
        data.close()


@pytest.mark.parametrize(("table_name", "payload", "definition_candidate"), [
    ("c17", '{"concept_code":"X7","synthetic":true}', True),
    ("c17", '{"meaning":"设备是一类生产资源"}', True),
    ("c17", '{"source_code":"X7","status":"active"}', False),
    ("c17", '{"owner":"engineering","language":"zh"}', False),
    ("c17_config", '{"concept_code":"X7"}', False),
])
def test_named_structured_content_has_a_bounded_review_path_without_inventing_meaning(tmp_path, table_name, payload, definition_candidate):
    _table(tmp_path / "input", table_name, {"id": "", "name": "", "q9": ""},
           [{"id": "1", "name": "设备", "q9": payload}])
    data = _open_data(tmp_path / "input", tmp_path)
    index = None
    try:
        data.tables[f"fruit.{table_name}"]["inferred_semantic_roles"] = [_legacy_role("q9", "metadata", payload)]
        built = build_semantic_cards(data, tmp_path / "cards.sqlite", max_unknown_fields_per_table=0)
        assert built["coverage"]["rows_without_card"] == 0
        index = SemanticCardIndex(built["index_path"])
        card = index.all_cards(1, kind=None)["cards"][0]
        assert card["fields"]["unknown"][0]["value"] == payload
        assert bool(card["kind"] == "definition") is definition_candidate
        packets = build_instance_bundles(data, index, {"rules": []}, {
            "max_concept_bundles": 1, "max_relation_bundles": 0, "max_joined_context_records": 0})
        assert bool(packets["bundles"]) is definition_candidate
        assert not card["fields"].get("description")
    finally:
        if index:
            index.close()
        data.close()
