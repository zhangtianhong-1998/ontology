"""Accepted stage results must survive a disabled or failing later stage."""
import asyncio
from pathlib import Path

from ontology_r2.pipeline import build
from ontology_r2.storage import digest, read_yaml
from test_pipeline import setup
from test_semantic_cards import _table


def _fact_config(tmp_path):
    config = setup(tmp_path, scenario="unrelated", mcp=False, rows=2)
    _table(Path(config["dataset"]), "sales_fact", {
        "id": "", "region_code": "", "period": "", "水果销售收入": ""}, [
        {"id": "1", "region_code": "EAST", "period": "2025Q1", "水果销售收入": "100"},
        {"id": "2", "region_code": "SOUTH", "period": "2025Q2", "水果销售收入": "120"}])
    config.update(incremental={"mode": "source_mapping_only"}, discovery={"enabled": False},
                  fact_observations={"enabled": True}, fact_schema_induction={"enabled": True},
                  fact_type_binding={"enabled": False}, visualization={"enabled": False},
                  progress={"enabled": False})
    return config


async def _field_decision(self, task, payload, schema):
    assert task == "fact_schema_induction"
    declaration = payload["source_declaration"]
    return schema.model_validate({"status": "proposed", "label": "水果销售收入",
                                  "identity_evidence_id": declaration["evidence_id"],
                                  "identity_quote": declaration["value"],
                                  "business_object_quote": "水果", "quantity_quote": "销售收入"})


def test_new_fact_type_is_published_even_with_binding_disabled(tmp_path, monkeypatch):
    config = _fact_config(tmp_path)
    monkeypatch.setattr("ontology_r2.llm.StructuredLLM.ask", _field_decision)
    output = tmp_path / "run"
    manifest = asyncio.run(build(config, output))
    assert manifest["status"] != "failed", manifest
    planned = {item["id"] for item in read_yaml(output / "extraction_plan.yaml")["object_types"]
               if item["id"].startswith("type:fact_field:")}
    assert len(planned) == 1
    ontology = read_yaml(output / "ontology.yaml")
    assert planned <= {item["id"] for item in ontology["object_types"]}
    assert manifest["ontology_hash"] == digest(ontology)


def test_later_binding_configuration_error_keeps_published_fact_type(tmp_path, monkeypatch):
    config = _fact_config(tmp_path)
    config["fact_type_binding"]["unsupported"] = True
    monkeypatch.setattr("ontology_r2.llm.StructuredLLM.ask", _field_decision)
    output = tmp_path / "run"
    manifest = asyncio.run(build(config, output))
    assert manifest["status"] == "failed"
    assert manifest["error"]["type"] == "ValueError"
    planned = {item["id"] for item in read_yaml(output / "extraction_plan.yaml")["object_types"]
               if item["id"].startswith("type:fact_field:")}
    assert len(planned) == 1
    assert planned <= {item["id"] for item in read_yaml(output / "ontology.yaml")["object_types"]}


def test_resume_does_not_turn_incomplete_column_roles_into_complete(tmp_path, monkeypatch):
    config = setup(tmp_path, scenario="unrelated", mcp=False, rows=2)
    config.update(incremental={"mode": "source_mapping_only"}, discovery={"enabled": False},
                  column_role_inference={"enabled": True}, visualization={"enabled": False},
                  instance_bundles={"enabled": True, "max_llm_bundles": 0},
                  progress={"enabled": False})

    async def incomplete(data, *_args, **_kwargs):
        return {"source_snapshot": data.snapshot_id, "candidates": [], "tables": [],
                "coverage": {"status": "candidate_only", "partial": True,
                             "model_calls_attempted": 0, "unresolved_tables": 1}}

    monkeypatch.setattr("ontology_r2.pipeline.infer_column_role_candidates", incomplete)
    first = tmp_path / "first"
    before = asyncio.run(build(config, first))
    assert before["column_role_inference"]["partial"] is True
    assert (first / "semantic_state.json").exists()
    config["resume_from"] = str(first)
    after = asyncio.run(build(config, tmp_path / "resumed"))
    assert after["status"] != "failed", after
    assert after["column_role_inference"]["partial"] is True
    assert "column_roles_not_fully_inferred" in after["partial_reasons"]


def test_new_operand_ambiguity_retracts_old_calculation_edges_without_aborting():
    from ontology_r2.calculation_stage import compile_calculation_stage
    from test_calculation_contracts import _case
    from test_calculation_stage import PROFILE

    original_data, original_plan, original_groups = _case()
    accepted = compile_calculation_stage(original_data, PROFILE, original_plan, original_groups)
    expanded_data, expanded_plan, expanded_groups = _case(duplicate=True)
    expanded_plan.relation_types = accepted["plan"].relation_types
    resumed = compile_calculation_stage(expanded_data, PROFILE, expanded_plan, expanded_groups)
    assert resumed["coverage"]["partial"] is True
    assert resumed["dependencies"] == []
    assert resumed["plan"].relation_types == []
