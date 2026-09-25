"""Deterministic engineering check for group packets and ontology compilation.

The canned model responses are an oracle for this tiny synthetic fixture. This
checks wiring and evidence guards, not semantic accuracy of a live model.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from pathlib import Path

from ontology_r2.association_rules import build_association_rules
from ontology_r2.discovery import validate_candidate
from ontology_r2.group_incremental import (
    BundleReview, ConceptBundleDecision, RecordAlignmentDecision,
    RelationBundleDecision, construct_from_bundles,
)
from ontology_r2.incremental import direct_mapping, ontology_from_plan
from ontology_r2.instance_bundles import build_instance_bundles
from ontology_r2.pipeline import add_inferred_matches, check_output, technical_graph
from ontology_r2.relations import Extractor
from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from ontology_r2.storage import Dataset, Sink, read_yaml, write_yaml
from ontology_r2.visualization import render_viewer


def _table(root, name, comment, columns, rows):
    base = {"schema": "fruit", "table_name": name}
    fields = list(columns)
    write_yaml(root / "schema/tables" / f"{name}.yaml", {
        **base, "table_comment": comment,
        "columns": [{"column_name": field, "ordinal_position": position + 1,
                     "data_type": "text", "is_not_null": position == 0,
                     "default_value": None, "column_comment": columns[field]}
                    for position, field in enumerate(fields)],
    })
    write_yaml(root / "schema/constraints" / f"{name}.yaml", {
        **base, "constraints": [{"constraint_name": f"{name}_pk", "constraint_type": "p",
                                 "definition": f"PRIMARY KEY ({fields[0]})"}],
    })
    write_yaml(root / "schema/foreign_keys" / f"{name}.yaml", {
        **base, "foreign_keys": [],
    })
    (root / "data").mkdir(parents=True, exist_ok=True)
    with (root / "data" / f"{name}.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def make_fixture(root):
    """Six definition rows, one explicit metric-to-measure code reference."""
    root = Path(root)
    _table(root, "fruit_metric_definition", "水果经营指标定义", {
        "metric_id": "记录编号", "metric_code": "指标编码", "metric_name": "指标名称",
        "definition": "指标业务定义", "calculation_formula": "计算公式",
        "measure_code": "被引用的度量编码", "unit": "金额单位",
    }, [{"metric_id": "m1", "metric_code": "PROFIT", "metric_name": "水果销售利润",
         "definition": "水果销售收入扣除销售成本的金额", "calculation_formula":
         "水果销售利润 = 水果销售收入 - 水果销售成本",
         "measure_code": "REV", "unit": "元"}])
    _table(root, "fruit_metric_common", "水果经营指标公共定义", {
        "id": "记录编号", "metric_name": "指标名称", "definition": "指标说明", "unit": "单位",
    }, [{"id": "mc1", "metric_name": "水果销售利润",
         "definition": "销售收入减去销售成本后的利润金额", "unit": "元"}])
    _table(root, "fruit_measure_definition", "水果经营度量定义", {
        "measure_id": "记录编号", "measure_code": "度量编码", "measure_name": "度量名称",
        "definition": "度量业务定义", "unit": "金额单位",
    }, [{"measure_id": "v1", "measure_code": "REV", "measure_name": "水果销售收入",
         "definition": "水果销售形成的收入金额", "unit": "元"}])
    _table(root, "fruit_measure_common", "水果经营度量公共定义", {
        "id": "记录编号", "measure_name": "度量名称", "definition": "度量说明", "unit": "单位",
    }, [{"id": "vc1", "measure_name": "水果销售收入",
         "definition": "水果经营销售所得收入", "unit": "元"}])
    _table(root, "fruit_dim_definition", "水果经营地区维度定义", {
        "dim_id": "记录编号", "dim_code": "维度编码", "dim_name": "维度名称",
        "definition": "维度业务定义",
    }, [{"dim_id": "d1", "dim_code": "REGION", "dim_name": "销售地区",
         "definition": "按水果销售所处地区划分"}])
    _table(root, "fruit_dim_common", "水果经营地区维度公共定义", {
        "id": "记录编号", "dim_name": "维度名称", "definition": "维度说明",
    }, [{"id": "dc1", "dim_name": "销售地区",
         "definition": "水果销售的地区归属"}])


class FixtureDecisions:
    """Explicit expected decisions; never presented as measured model quality."""

    async def ask(self, task, payload, schema):
        if task == "concept_bundle":
            seed = payload["bundle"]["records"][0]
            name = seed["fields"]["name"][0]["value"]
            definition = seed["fields"]["description"][0]["value"]
            alignments = []
            exact_allowed = set(payload["bundle"].get(
                "exact_alignment_record_ids", [seed["record_id"]]))
            for record in payload["bundle"]["records"]:
                if (record["record_id"] in exact_allowed
                        and record.get("kind") == "definition"
                        and record.get("root_hint") == seed.get("root_hint")
                        and record.get("fields", {}).get("name", [{}])[0].get("value") == name
                        and record.get("unit", "") == seed.get("unit", "")):
                    alignments.append(RecordAlignmentDecision(
                        record_id=record["record_id"], mapping_kind="exact", quote=name))
            return ConceptBundleDecision(
                status="proposed", label=name, definition=definition,
                root_type=seed["root_hint"], ontology_level="type",
                classification_basis=("business_driven_metric" if seed["root_hint"] == "Metric"
                                      else "aggregation_or_filter_measure" if seed["root_hint"] == "Measure"
                                      else "other"),
                classification_quote=definition,
                alignments=alignments,
            )
        if task == "relation_bundle":
            bundle = payload["bundle"]
            pair = bundle["examples"]["positive"][0]
            records = {item["record_id"]: item for item in bundle["records"]}
            source = records[pair["source_record_id"]]
            target = records[pair["target_record_id"]]
            return RelationBundleDecision(
                status="proposed", parent_relation="depends_on",
                label="指标计算依赖收入度量",
                definition="利润指标的公式引用销售收入度量",
                source_quote=source["fields"]["name"][0]["value"],
                target_quote=target["fields"]["name"][0]["value"],
            )
        if task == "group_review":
            return BundleReview(accepted=True)
        raise AssertionError(task)


def run(output):
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Experiment output already contains files: " + str(output))
    output.mkdir(parents=True, exist_ok=True)
    root, work = output / "input", output / "work"
    make_fixture(root)
    work.mkdir()
    profile = read_yaml(Path(__file__).resolve().parents[1] / "ontologies/internal_model.yaml")
    data = Dataset(root, work)
    try:
        cards = build_semantic_cards(data, work / "cards.sqlite", max_cards=100)
        index = SemanticCardIndex(cards["index_path"])
        candidate = {
            "candidate_id": "fixture:metric_measure_code",
            "source": {"table": "fruit.fruit_metric_definition", "field": "measure_code"},
            "target": {"table": "fruit.fruit_measure_definition", "field": "measure_code"},
            "retrieval_channels": ["fixture_declared_candidate"],
            "suggested_selector": {}, "suggested_scope_bindings": {},
            "numeric_overlap_only": False,
        }
        checked = validate_candidate(data, candidate)
        association = asyncio.run(build_association_rules(
            data, {"candidates": [candidate], "checks": [checked]},
            {"agent_enabled": False, "max_rules": 1}))
        try:
            packets = build_instance_bundles(data, index, association, {
                "max_concept_bundles": 6, "max_relation_bundles": 1,
                "max_candidates_per_bundle": 2, "max_joint_pairs_per_rule": 1,
            })
        finally:
            index.close()
        core, mapping = direct_mapping(data)
        result = asyncio.run(construct_from_bundles(
            data, profile, core, packets["bundles"], FixtureDecisions(),
            max_bundles=7, review=True))
        business_types = [item for item in result["plan"].object_types
                          if item.category == "business_type"]
        relation_types = result["plan"].relation_types
        if len(result["concept_relations"]) != 1:
            raise AssertionError("Controlled positive pair did not lift to a business relation: "
                                 + repr({"steps": result["steps"],
                                         "derivations": result["concept_relation_derivations"]}))
        sink = Sink(output)
        try:
            for item in data.evidence.values():
                sink.put("evidence", item)
            for item in result["concepts"]:
                sink.put("objects", item)
            for item in result["record_alignments"]:
                sink.put("record_alignments", item)
            for item in result["concept_relations"]:
                sink.put("assertions", item)
            extractor = Extractor(data, sink, result["plan"], FixtureDecisions(),
                                  {"max_relation_records": 100})
            materialized = 0
            for table in result["plan"].tables:
                for row in data.rows(table.table):
                    extractor.object(table.table, row)
                    materialized += 1
            extraction = asyncio.run(extractor.execute())
            counts = sink.export()
            validation = check_output(sink)
        finally:
            sink.close()
        if extraction["accepted"] != 1 or not validation["passed"]:
            raise AssertionError("Controlled relation materialization failed structural checks")
        manifest = {
            "status": "complete", "experimental_scope": "controlled_synthetic_fixture_only",
            "input_tables": len(data.tables), "input_records": 6,
            "viewer": "viewer.html",
            "llm": {"mode": "fixed_fixture_decisions", "calls": 0},
            "semantic_quality": "unjudged; no live LLM or independent business Gold",
        }
        summary = {
            "experiment": "controlled_synthetic_group_compilation",
            "snapshot_id": data.snapshot_id,
            "fixture_rows": sum(table["rows"] for table in data.tables.values()),
            "semantic_cards": cards["coverage"]["cards_indexed"],
            "checked_technical_rules": sum(rule["status"] == "checked_technical"
                                           for rule in association["rules"]),
            "concept_bundles": packets["coverage"]["bundles_by_task"]["concept_induction"],
            "relation_bundles": packets["coverage"]["bundles_by_task"]["relation_meaning"],
            "accepted_business_types": [{"id": item.id, "parent": item.parent,
                                         "label": item.label} for item in business_types],
            "accepted_relation_types": [{"id": item.id, "parent": item.parent,
                                          "category": item.category,
                                          "domain": item.domain, "range": item.range,
                                          "evidence_scope": item.evidence_scope}
                                         for item in relation_types],
            "relation_level": "source_record_relation_and_one_exact_concept_pair",
            "step_statuses": [item["status"] for item in result["steps"]],
            "materialized_source_records": materialized,
            "source_record_relation_assertions": extraction["accepted"],
            "concept_relation_assertions": len(result["concept_relations"]),
            "structural_validation": validation,
            "viewer": "viewer.html",
            "evaluation_scope": "engineering_contract_only; canned decisions; no live LLM or semantic Gold",
        }
        write_yaml(output / "semantic_card_coverage.yaml", cards["coverage"])
        write_yaml(output / "association_rules.yaml", association)
        write_yaml(output / "evidence_bundles.yaml", packets["bundles"])
        write_yaml(output / "group_steps.yaml", result["steps"])
        write_yaml(output / "evidence.yaml", list(data.evidence.values()))
        write_yaml(output / "business_concepts.yaml", result["concepts"])
        write_yaml(output / "record_alignments.yaml", result["record_alignments"])
        write_yaml(output / "concept_relations.yaml", result["concept_relations"])
        write_yaml(output / "concept_relation_derivations.yaml",
                   result["concept_relation_derivations"])
        write_yaml(output / "extraction_plan.yaml", result["plan"].model_dump())
        write_yaml(output / "ontology.yaml", ontology_from_plan(
            result["plan"], profile, data, mapping, []))
        write_yaml(output / "meta_graph.yaml", add_inferred_matches(technical_graph(data), association))
        write_yaml(output / "construction.yaml", {
            "steps": [], "group_steps": result["steps"],
            "direct_mapping_columns": sum(len(table["columns"]) for table in data.tables.values()),
        })
        write_yaml(output / "coverage.yaml", {"relation_plans": extraction["plans"],
                                              "input_scope": "six_synthetic_definition_rows"})
        write_yaml(output / "metrics.yaml", {"counts": counts, "extraction": extraction,
                                             "materialized_records": materialized,
                                             "semantic_quality": "unjudged"})
        write_yaml(output / "validation.yaml", validation)
        write_yaml(output / "manifest.yaml", manifest)
        write_yaml(output / "summary.yaml", summary)
        render_viewer(output, 50)
        return summary
    finally:
        data.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="New directory, preferably below route2/runs/")
    args = parser.parse_args()
    print(run(args.output))
