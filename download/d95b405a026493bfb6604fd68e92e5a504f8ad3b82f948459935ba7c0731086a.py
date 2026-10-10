"""Exercise second-pass endpoint repair through exported, source-backed instances."""
import asyncio
from copy import deepcopy
from pathlib import Path

from ontology_r2.llm import StructuredLLM
from ontology_r2.pipeline import build
from ontology_r2.storage import read_yaml, write_yaml
from test_pipeline import items
from test_semantic_cards import _table
from test_template_projection import decision, record


def test_targeted_repair_exports_real_instance_edges_and_mapping_coverage(tmp_path, monkeypatch):
    root, output = tmp_path / "input", tmp_path / "run"
    records = [record("row1"), record("row2", "橙子", "华南")]
    for item in records:
        item["table"] = "fruit.quality_definition"
        item["fields"]["description"].append({
            "column": "x13", "value": "地区是一类地域划分维度。"})
    rows = [{field["column"]: field["value"]
             for fields in item["fields"].values() for field in fields}
            for item in records]
    columns = {field["column"]: {"name": "名称", "description": "定义描述",
        "scope": "作用域", "formula": "计算公式", "unit": "单位", "reference": "记录 ID"}[role]
        for role, fields in records[0]["fields"].items() for field in fields}
    _table(root, "quality_definition", columns, rows, pk="x12")
    responses = tmp_path / "responses.yaml"
    write_yaml(responses, {})
    observed_calls = []
    source_ids = {}

    # Only candidate scheduling is fixed. Source import, semantic index, template
    # checks, second-pass planning, relation compilation and materialization run.
    def packets(data, *_args, **_kwargs):
        for row, item in zip(data.rows("fruit.quality_definition"), records):
            old_id = item["record_id"]
            item["record_id"] = data.record_id(item["table"], row)
            source_ids[old_id] = item["record_id"]
        return {"bundles": [{"bundle_id": "source:quality", "task_kind": "concept_induction",
            "records": deepcopy(records), "exact_alignment_record_ids": [records[0]["record_id"]]}],
            "coverage": {"status": "complete", "partial": False, "bundles_built": 1}}

    class RecordedModel(StructuredLLM):
        async def ask(self, task, payload, schema):
            assert self.mode == "mock" and task == "concept_bundle"
            proposal = decision().model_dump()
            proposal["witness_record_ids"] = list(source_ids.values())
            for quote in [*proposal["label_evidence"],
                          *(slot["evidence"] for slot in proposal["slots"]),
                          *(component[key] for component in proposal["components"]
                            for key in ("label_evidence", "definition_evidence"))]:
                quote["record_id"] = source_ids[quote["record_id"]]
            repair = payload.get("targeted_repair")
            observed_calls.append("repair" if repair else "initial")
            if repair:
                assert repair["preserve_type_id"]
                proposal["existing_type_id"] = repair["preserve_type_id"]
                proposal["slots"][1]["target_component"] = "region_dimension"
                quote = {"record_id": records[0]["record_id"], "column": "x13"}
                proposal["components"].append({"name": "region_dimension", "role": "dimension",
                    "root_type": "Dimension", "label": "地区", "definition": "地区是一类地域划分维度。",
                    "label_evidence": {**quote, "quote": "地区"},
                    "definition_evidence": {**quote, "quote": "地区是一类地域划分维度。"}})
            self.responses[task] = {"status": "proposed", "action": "project_template",
                                    "projection": proposal}
            # Keep normal mock validation, admission, trace and call accounting.
            return await super().ask(task, payload, schema)

    monkeypatch.setattr("ontology_r2.pipeline.build_instance_bundles", packets)
    monkeypatch.setattr("ontology_r2.pipeline.StructuredLLM", RecordedModel)
    config = {
        "dataset": str(root), "synthetic": True, "env_file": str(tmp_path / ".env"),
        "model_profile": str(Path(__file__).resolve().parents[1] / "ontologies/internal_model.yaml"),
        "llm": {"mode": "mock", "responses": str(responses), "max_calls": 8,
                "max_input_bytes": 200000, "max_reserved_tokens": 2000000},
        "incremental": {"mode": "source_mapping_only"}, "discovery": {"enabled": False},
        "mcp": {"enabled": False}, "external": {"enabled": False},
        "processing": {"materialize_all_objects": False}, "progress": {"enabled": False},
        "instance_bundles": {"enabled": True, "template_projection_enabled": True,
            "review": False, "concept_batch_size": 1, "max_llm_bundles": 1,
            "max_concept_bundles": 1, "max_relation_bundles": 0},
        "targeted_repair": {"enabled": True, "max_rounds": 2,
            "max_tasks_per_round": 1, "max_examples_per_task": 2, "max_calls_per_round": 2},
        "ontology_instantiation": {"enabled": True, "max_records": 10, "max_assertions": 20},
    }
    manifest = asyncio.run(build(config, output))
    assert manifest["status"] != "failed", manifest
    assert observed_calls == ["initial", "repair"]
    assert manifest["llm"]["calls"] == 2
    rounds = read_yaml(output / "targeted_repair_rounds.yaml")
    assert len(rounds) == 1 and rounds[0]["round"] == 2 and rounds[0]["model_calls"] == 1
    assert manifest["targeted_repair"]["rounds_executed"] == 1
    tasks = read_yaml(output / "targeted_repair_tasks.yaml")
    assert tasks[0]["scope"] == "template_slot_endpoints"

    objects = items(output, "objects")
    edges = [item for item in items(output, "assertions") if "object" in item]
    instances = [item for item in objects if item.get("instance_kind") == "definition_record_instance"]
    assert len(instances) == 2
    assert {item["source_ref"]["record_id"] for item in instances} == set(source_ids.values())
    assert {(item["slot_values"]["product"], item["slot_values"]["region"])
            for item in instances} == {("苹果", "华东"), ("橙子", "华南")}
    assert edges
    node_by_id = {item["id"]: item for item in objects}
    assert all(edge["subject"] in node_by_id and edge["object"] in node_by_id for edge in edges)
    # In particular the repaired Dimension endpoint must survive all the way
    # into actual same-source instances, not merely a type or a saved plan.
    region_type = next(item["id"] for item in read_yaml(output / "ontology.yaml")["object_types"]
                       if item.get("label") == "地区")
    region_edges = [edge for edge in edges if node_by_id[edge["object"]]["type"] == region_type]
    assert len(region_edges) == 2
    assert all(node_by_id[edge["subject"]]["source_ref"]["record_id"] ==
               node_by_id[edge["object"]]["source_ref"]["record_id"] for edge in region_edges)
    mappings = read_yaml(output / "ontology_instance_mappings.yaml")
    assert mappings and {item["instance_id"] for item in mappings} <= node_by_id.keys()
    coverage = read_yaml(output / "ontology_instance_coverage.yaml")
    assert coverage == manifest["ontology_instantiation"]
    assert coverage["definition_record_instances"] == 2
    assert coverage["cartesian_products_created"] == coverage["llm_calls"] == 0
    assert read_yaml(output / "validation.yaml")["passed"]
