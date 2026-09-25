"""The controlled fixture proves wiring only, not model semantic accuracy."""

import json
import sqlite3

import pytest

from ontology_r2.storage import read_yaml
from scripts.run_group_compilation_experiment import run


def test_controlled_packets_compile_business_types_and_evidence_bounded_relation(tmp_path):
    output = tmp_path / "group-compilation"
    summary = run(output)
    assert summary["fixture_rows"] == summary["semantic_cards"] == 6
    assert summary["checked_technical_rules"] == 1
    assert summary["concept_bundles"] == 6
    assert summary["relation_bundles"] == 1
    assert {item["parent"] for item in summary["accepted_business_types"]} == {
        "Metric", "Measure", "Dimension"}
    assert summary["step_statuses"] == ["accepted"] * 7
    assert summary["relation_level"] == "source_record_relation_and_one_exact_concept_pair"
    assert summary["evaluation_scope"].startswith("engineering_contract_only")

    ontology = read_yaml(output / "ontology.yaml")
    business = [item for item in ontology["object_types"]
                if item["category"] == "business_type"]
    assert len(business) == 6
    assert all(item["evidence_scope"] == "definition_record" for item in business)
    assert len(read_yaml(output / "business_concepts.yaml")) == 6
    assert len(read_yaml(output / "record_alignments.yaml")) == 6

    plan = read_yaml(output / "extraction_plan.yaml")
    assert len(plan["relations"]) == 1
    assert plan["relations"][0]["witness_snapshot_id"] == summary["snapshot_id"]
    assert len(plan["relations"][0]["witnessed_pairs"]) == 1
    assert len(plan["relation_types"]) == 2
    record_relation = next(item for item in plan["relation_types"]
                           if item["category"] is None)
    business_relation = next(item for item in plan["relation_types"]
                             if item["category"] == "business_relation_type")
    assert record_relation["parent"] == business_relation["parent"] == "depends_on"
    assert record_relation["domain"] == ["source_record_type:fruit.fruit_metric_definition"]
    assert record_relation["range"] == ["source_record_type:fruit.fruit_measure_definition"]
    assert record_relation["evidence_scope"] == plan["relations"][0]["evidence_scope"] == (
        "sample_semantic_with_full_technical_check")
    business_by_id = {item["id"]: item for item in business}
    assert len(business_relation["domain"]) == len(business_relation["range"]) == 1
    assert business_by_id[business_relation["domain"][0]]["parent"] == "Metric"
    assert business_by_id[business_relation["range"][0]]["parent"] == "Measure"
    assert business_relation["evidence_scope"] == (
        "one_positive_pair_with_exact_type_alignments")
    assert all(item["predicate"] != business_relation["id"] for item in plan["relations"])
    evidence = {item["id"]: item for item in read_yaml(output / "evidence.yaml")}
    assert set(record_relation["evidence_ids"] + business_relation["evidence_ids"]) <= set(evidence)
    assert any("水果销售收入" in evidence[item]["raw_fragment"]
               for item in business_relation["evidence_ids"] if item.startswith("record:"))
    validation = read_yaml(output / "validation.yaml")
    assert validation["passed"] is True
    assert all(value == 0 for value in validation["checks"].values())
    with sqlite3.connect(output / "work/results.sqlite") as db:
        edges = [json.loads(row[0]) for row in db.execute(
            "SELECT body FROM items WHERE kind='assertions' "
            "AND json_extract(body,'$.object') IS NOT NULL")]
        assert len(edges) == 2
        record_edge = next(edge for edge in edges if edge["predicate"] == record_relation["id"])
        concept_edge = next(edge for edge in edges if edge["predicate"] == business_relation["id"])
        assert record_edge["decision"]["evidence_scope"] == (
            "sample_semantic_with_full_technical_check")
        assert concept_edge["decision"]["evidence_scope"] == (
            "one_positive_pair_with_exact_type_alignments")
        assert concept_edge["subject"].startswith("concept:")
        assert concept_edge["object"].startswith("concept:")
        for edge in edges:
            for endpoint in (edge["subject"], edge["object"]):
                assert db.execute("SELECT 1 FROM items WHERE kind='objects' AND id=?",
                                  (endpoint,)).fetchone()
            for evidence_id in edge["evidence_ids"]:
                assert db.execute("SELECT 1 FROM items WHERE kind='evidence' AND id=?",
                                  (evidence_id,)).fetchone()

    html = (output / "viewer.html").read_text(encoding="utf-8")
    viewer = json.loads(html.split(
        '<script id="result-data" type="application/json">', 1)[1]
        .split('</script>', 1)[0])
    assert viewer["relation_count"] == 2
    assert len(viewer["relations"]) == 2
    assert viewer["validation"]["passed"] is True
    assert viewer["manifest"]["viewer"] == "viewer.html"
    assert viewer["manifest"]["llm"]["mode"] == "fixed_fixture_decisions"
    with pytest.raises(FileExistsError, match="already contains files"):
        run(output)
