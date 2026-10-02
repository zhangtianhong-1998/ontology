"""Independent power-meter examples for source names, units and scoped binding."""
import asyncio
import csv
from copy import deepcopy

import pytest

from ontology_r2.fact_observations import build_fact_observation_candidates
from ontology_r2.fact_schema_induction import induce_fact_schema
from ontology_r2.fact_type_binding import (
    _recall_types, _unit_from_comment, _unit_key,
    bind_fact_observations, field_scope_constants, field_scope_mapping_candidates,
    field_unit_evidence,
)
from ontology_r2.models import BuildPlan, DerivedType, SourceProperty
from ontology_r2.storage import Dataset, digest, write_yaml


def _meter_data(tmp_path, *, field="load_kwh_total", comment="读数，kWh", scope=True,
                scope_values=("POWER", "POWER"), parameter="load_kwh", aliases="电耗；用电量"):
    root, work = tmp_path / "input", tmp_path / "work"
    root.mkdir(); work.mkdir()
    tables = {
        "meter_fact": ({"id": "记录 ID", "site_code": "地区编码", "period": "业务期间",
                        field: comment, "portfolio": "适用行业编码"}, [
            {"id": str(i), "site_code": site, "period": "2026Q1", field: str(10 * i),
             "portfolio": scope_values[i - 1]} for i, site in enumerate(["EAST", "WEST"], 1)]),
        "quantity_definition": ({"id": "记录 ID", "title": "度量名称", "parameter_name": "参数名称",
            "aliases": "别名", "definition": "完整定义", "sector_code": "适用行业编码"}, [
            {"id": "1", "title": "耗电量", "parameter_name": parameter, "aliases": aliases,
             "definition": "耗电量是计量点在观测期间实际消耗的有功电能。", "sector_code": "POWER"}]),
    }
    for name, (columns, rows) in tables.items():
        base = {"schema": "energy", "table_name": name}
        write_yaml(root / "schema/tables" / f"{name}.yaml", {**base,
            "table_comment": "能源计量事实" if name == "meter_fact" else "能源量定义",
            "columns": [{"column_name": col, "column_comment": label, "data_type": "text",
                         "ordinal_position": i + 1} for i, (col, label) in enumerate(columns.items())]})
        write_yaml(root / "schema/constraints" / f"{name}.yaml", {**base,
            "constraints": [{"constraint_type": "p", "definition": "PRIMARY KEY (id)"}]})
        write_yaml(root / "schema/foreign_keys" / f"{name}.yaml", {**base, "foreign_keys": []})
        (root / "data").mkdir(exist_ok=True)
        with (root / "data" / f"{name}.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader(); writer.writerows(rows)
    data = Dataset(root, work)
    table = "energy.quantity_definition"
    row = next(data.rows(table)); rid = data.record_id(table, row)
    properties, ids = [], []
    for column, role in [("title", "name"), ("parameter_name", "name"), ("aliases", "alias"),
                         ("definition", "description"), ("sector_code", "scope")]:
        eid = "record:" + digest([data.snapshot_id, rid, column])[:24]
        data.evidence[eid] = {"id": eid, "origin": "observed_record", "raw_fragment": row[column],
            "raw_fragment_truncated": False, "source_ref": {"table": table, "column": column,
                "row": row["__r2_row"], "record_id": rid, "snapshot_id": data.snapshot_id}}
        ids.append(eid)
        properties.append(SourceProperty(role=role, source_table=table, source_column=column, evidence_ids=[eid]))
    item = DerivedType(id="type:active_energy", parent="Measure", category="business_type",
        label=row["title"], definition=row["definition"], evidence_ids=ids, source_properties=properties,
        unit="kWh", applicability_scope={"sector_code": "POWER"} if scope else {})
    return data, BuildPlan(object_types=[item]), field


class _BindingProposal:
    def __init__(self, *, fixed=True):
        self.requests = []
        self.fixed = fixed

    async def ask(self, task, payload, schema):
        assert task == "fact_type_binding"
        self.requests.append(payload)
        item = payload["type_candidates"][0]
        source = item["full_source_definitions"][0]
        return schema.model_validate({"status": "bind", "type_id": item["id"],
            "source_column_quote": payload["source_declaration"],
            "type_definition_quote": item["definition"],
            "source_definition_evidence_id": source["evidence_id"], "type_source_quote": source["value"],
            "constant_scope_bindings": {"sector_code": "portfolio"} if self.fixed else {}})


@pytest.mark.parametrize("comment,expected", [
    ("出口量，吨", {"吨"}), ("电能, kWh", {"kWh"}), ("功率【kW】", {"kW"}),
    ("流量，m³/s", {"m3/s"}), ("power; unit: kW", {"kW"}),
    ("成本，元每千克", {"元/kg"}), ("运输费，元每公斤", {"元/kg"}),
    ("批次（最终版）", set()), ("组织单位（总部）", set()),
    ("tonnage inventory", set()), ("sales_amount_cny", set()),
    ("电能，kWh；单位：MWh", {"kWh", "MWh"}),
])
def test_unit_tokens_require_whole_literal_measurements(comment, expected):
    assert _unit_from_comment(comment) == expected


def test_unit_normalization_never_erases_si_scale_or_currency_scale():
    assert _unit_key("MW") != _unit_key("mW")
    assert _unit_key("元") != _unit_key("万元")
    assert _unit_key("千克") == _unit_key("kg")
    assert _unit_key("吨") != _unit_key("kg")


@pytest.mark.parametrize("field,comment", [("load_kwh_total", "读数，kWh"),
    ("reading_value", "用电量，kWh"), ("load_kwh_total", "读数，千瓦时")])
def test_parameter_and_separate_alias_names_recall_and_bind_with_scope_proof(tmp_path, field, comment):
    data, core, field = _meter_data(tmp_path, field=field, comment=comment)
    try:
        observations = build_fact_observation_candidates(data)
        proposal = _BindingProposal()
        result = asyncio.run(bind_fact_observations(data, observations, core, proposal))
        assert result["coverage"]["instances_created"] == 2, (result, observations)
        assert len(proposal.requests) == 1
        candidate = proposal.requests[0]["type_candidates"][0]
        assert candidate["source_name_matches"]
        assert candidate["scope_mapping_candidates"][0]["basis"] == "same_source_scope_declaration"
        for node in result["instances"]:
            constants = node["binding_decision"]["source_checked_scope_constants"]
            assert constants[0]["scope"] == "full_input" and constants[0]["rows"] == 2
            assert set(constants[0]["evidence_ids"]) <= set(node["evidence_ids"])
            assert node["binding_decision"]["source_scope_mapping_evidence"]
    finally:
        data.close()


def test_alias_evidence_must_be_cited_complete_and_from_current_source(tmp_path):
    data, core, field = _meter_data(tmp_path, scope=False)
    try:
        item = core.object_types[0]
        parameter = next(s for s in item.source_properties if s.source_column == "parameter_name")
        evidence = data.evidence[parameter.evidence_ids[0]]
        table = data.tables["energy.meter_fact"]
        column = next(c for c in table["columns"] if c["column_name"] == field)
        for change in ({"raw_fragment_truncated": True}, {"source_ref": {**evidence["source_ref"], "snapshot_id": "old"}}):
            saved = deepcopy(evidence); evidence.update(change)
            choices, _, _ = _recall_types(data, core, table, column, max_candidates=8, max_definition_chars=2048)
            assert choices == []
            evidence.clear(); evidence.update(saved)
        item.evidence_ids.remove(parameter.evidence_ids[0])
        assert _recall_types(data, core, table, column, max_candidates=8, max_definition_chars=2048)[0] == []
    finally:
        data.close()


def test_equal_scope_values_without_matching_source_declaration_do_not_bind(tmp_path):
    data, core, _ = _meter_data(tmp_path)
    try:
        table = data.tables["energy.meter_fact"]
        next(c for c in table["columns"] if c["column_name"] == "portfolio")["column_comment"] = "任意标签"
        assert field_scope_mapping_candidates(data, table, core.object_types[0]) == []
        result = asyncio.run(bind_fact_observations(data, build_fact_observation_candidates(data), core, _BindingProposal()))
        assert not result["instances"]
        assert result["coverage"]["skipped_reasons"] == {"scope_constant_missing_or_conflicting": 2}
    finally:
        data.close()


@pytest.mark.parametrize("values", [("POWER", "OTHER"), ("POWER", "")])
def test_mixed_or_blank_scope_is_not_a_full_column_constant(tmp_path, values):
    data, core, _ = _meter_data(tmp_path, scope_values=values)
    try:
        table = data.tables["energy.meter_fact"]
        assert field_scope_constants(data, table, {"sector_code"}, candidate_columns={"portfolio"}) == []
    finally:
        data.close()


def test_scope_codes_are_not_casefolded_or_invented_from_table_topic(tmp_path):
    data, core, _ = _meter_data(tmp_path, scope_values=("power", "power"))
    try:
        observed = build_fact_observation_candidates(data)
        result = asyncio.run(bind_fact_observations(data, observed, core, _BindingProposal()))
        assert not result["instances"]
        assert result["coverage"]["skipped_reasons"] == {"scope_constant_missing_or_conflicting": 2}
        core.object_types[0].applicability_scope = {"missing_domain": "power"}
        assert field_scope_mapping_candidates(data, data.tables["energy.meter_fact"], core.object_types[0]) == []
    finally:
        data.close()


def test_complete_source_name_is_not_capped_by_unbindable_word_fragments(tmp_path):
    data, core, field = _meter_data(tmp_path, scope=False)
    try:
        for i in range(12):
            peer = core.object_types[0].model_copy(deep=True)
            peer.id, peer.label = f"type:partial_{i}", "other load"
            peer.source_properties = [p for p in peer.source_properties if p.role == "description"]
            core.object_types.append(peer)
        table = data.tables["energy.meter_fact"]
        column = next(c for c in table["columns"] if c["column_name"] == field)
        candidates, omitted, excluded = _recall_types(data, core, table, column,
                                                     max_candidates=1, max_definition_chars=2048)
        assert [item.id for _, item, _ in candidates] == ["type:active_energy"]
        assert omitted == 0 and not excluded
    finally:
        data.close()


def test_source_alias_conflict_is_not_resolved_by_different_type_labels(tmp_path):
    data, core, _ = _meter_data(tmp_path, scope=False)
    try:
        competing = core.object_types[0].model_copy(deep=True)
        competing.id, competing.label = "type:other_energy", "上网电量"
        core.object_types.append(competing)
        result = asyncio.run(bind_fact_observations(data, build_fact_observation_candidates(data), core,
                                                  _BindingProposal(fixed=False)))
        assert not result["instances"]
        assert result["coverage"]["skipped_reasons"] == {"multiple_compatible_types_share_source_name": 2}
    finally:
        data.close()


def test_fact_schema_hands_source_parameter_candidate_to_binding_without_model(tmp_path):
    data, core, _ = _meter_data(tmp_path)
    class NoModel:
        async def ask(self, *_):
            raise AssertionError("Source-grounded existing type must be handled by field binding")
    try:
        result = asyncio.run(induce_fact_schema(data, build_fact_observation_candidates(data), core, NoModel()))
        assert result["coverage"]["model_calls"] == 0
        step = result["steps"][0]
        assert step["status"] == "existing_type_candidates"
        assert any(n["source_column"] == "parameter_name" for n in
                   step["existing_type_candidates"][0]["source_name_matches"])
    finally:
        data.close()


def test_unit_cost_column_does_not_supply_measurement_unit(tmp_path):
    data, _, _ = _meter_data(tmp_path)
    try:
        table = data.tables["energy.meter_fact"]
        next(c for c in table["columns"] if c["column_name"] == "portfolio")["column_comment"] = "单位成本"
        assert field_unit_evidence(data, table, "portfolio") is None
    finally:
        data.close()
