import asyncio
import json
import sqlite3
import sys

from ontology_r2.cli import main as cli_main
from ontology_r2.pipeline import build
from ontology_r2.visualization import (
    MAX_ATTRIBUTES_PER_OBJECT,
    MAX_ATTRIBUTE_CHARS,
    MAX_SOURCE_REFS_PER_CONCEPT,
    _metadata_preview,
    render_viewer,
)
from ontology_r2.storage import read_yaml, write_yaml
from test_pipeline import setup


def payload(html):
    return json.loads(html.split('<script id="result-data" type="application/json">', 1)[1]
                      .split('</script>', 1)[0])


def test_metadata_view_prioritizes_tables_and_separates_link_statuses():
    graph = {"nodes": [
        {"id": "source:schema/a.yaml", "kind": "Source", "path": "schema/a.yaml"},
        {"id": "demo.a.code", "kind": "Column", "column_name": "code", "ordinal_position": 1},
        {"id": "demo.b.code", "kind": "Column", "column_name": "code", "ordinal_position": 1},
        {"id": "demo.a", "kind": "Table", "table_name": "a"},
        {"id": "demo.b", "kind": "Table", "table_name": "b"}],
        "edges": [
            {"source": "demo.a", "target": "demo.a.code", "type": "table_has_column"},
            {"source": "demo.a", "target": "source:schema/a.yaml", "type": "documented_by"},
            {"source": "demo.a.code", "target": "demo.b.code", "type": "declared_fk"}],
    }
    candidates = [{"candidate_id": "c1", "source": {"table": "demo.a", "field": "alias"},
                   "target": {"table": "demo.b", "field": "name"},
                   "retrieval_channels": ["value_overlap"]},
                  {"candidate_id": "c2", "source": {"table": "demo.a", "field": "code"},
                   "target": {"table": "demo.b", "field": "code"},
                   "retrieval_channels": ["metadata"]}]
    rules = {"rules": [{"rule_id": "r1", "source": {"table": "demo.a", "field": "code"},
                        "target": {"table": "demo.b", "field": "code"},
                        "status": "checked_technical", "semantic_relation": "unresolved"}]}
    metadata = _metadata_preview(graph, candidates, rules, 10)
    assert [table["id"] for table in metadata["tables"]] == ["demo.a", "demo.b"]
    assert metadata["details"]["demo.a"]["columns"][0]["id"] == "demo.a.code"
    assert metadata["details"]["demo.a"]["sources"][0]["path"] == "schema/a.yaml"
    assert metadata["link_counts"] == {"declared": 1, "verified_technical": 1, "candidate": 1}
    assert {link["status"] for link in metadata["links"]} == {
        "declared", "verified_technical", "candidate"}
    assert metadata["links"][1]["semantic_relation"] == "unresolved"


def test_metadata_column_preview_budget_is_shared_across_wide_tables():
    nodes = [{"id": "demo.a", "kind": "Table"}, {"id": "demo.b", "kind": "Table"}]
    edges = []
    for table in ("demo.a", "demo.b"):
        for index in range(100):
            column = f"{table}.column_{index:03}"
            nodes.append({"id": column, "kind": "Column", "column_name": f"column_{index:03}",
                          "ordinal_position": index + 1})
            edges.append({"source": table, "target": column, "type": "table_has_column"})
    metadata = _metadata_preview({"nodes": nodes, "edges": edges}, [], {"rules": []}, 10)
    assert [len(metadata["details"][table]["columns"]) for table in ("demo.a", "demo.b")] == [40, 40]
    assert [metadata["details"][table]["total_columns"] for table in ("demo.a", "demo.b")] == [100, 100]


def test_viewer_counts_business_and_source_record_types_separately(tmp_path):
    run = tmp_path / "type-preview"
    run.mkdir()
    write_yaml(run / "manifest.yaml", {"status": "complete", "input_tables": 2})
    write_yaml(run / "ontology.yaml", {
        "object_roots": [{"id": "GeneralObject"}, {"id": "Measure"}],
        "object_types": [
            {"id": "source_record_type:demo.api", "parent": "GeneralObject",
             "category": "source_record_type"},
            {"id": "source_record_type:demo.measure", "parent": "Measure",
             "category": "source_record_type"},
            {"id": "FruitRevenue", "parent": "Measure", "category": "business_type"},
            {"id": "legacy", "parent": "Measure"}],
        "relation_roots": [{"id": "depends_on"}],
        "relation_types": [{"id": "calculation_depends_on", "parent": "depends_on"}],
    })
    html = render_viewer(run, 10).read_text()
    data = payload(html)
    metrics = {item["label"]: item["value"] for item in data["preview"]["metrics"]}
    assert metrics["业务派生类型"] == 1
    assert metrics["源记录类型"] == 2
    assert data["preview"]["type_counts"] == {
        "business": 1, "source_record": 2, "unclassified": 1, "relation": 1,
        "business_relation": 0, "record_relation": 1}
    assert 'data-type-filter="business"' in html
    assert 'data-type-filter="source_record"' in html


def test_viewer_separates_witnessed_record_and_business_type_relations(tmp_path):
    run = tmp_path / "two-layer-relations"
    (run / "work").mkdir(parents=True)
    write_yaml(run / "manifest.yaml", {"status": "complete"})
    write_yaml(run / "ontology.yaml", {"object_roots": [], "object_types": [],
                                        "relation_roots": [{"id": "depends_on"}],
                                        "relation_types": [
                                            {"id": "record_relation", "parent": "depends_on"},
                                            {"id": "business_relation", "parent": "depends_on",
                                             "category": "business_relation_type"}]})
    objects = [
        {"id": "record:metric", "label": "m1"}, {"id": "record:measure", "label": "v1"},
        {"id": "concept:metric", "label": "利润", "ontology_level": "type"},
        {"id": "concept:measure", "label": "收入", "ontology_level": "type"},
    ]
    relations = [
        {"id": "edge:record", "subject": "record:metric", "object": "record:measure",
         "predicate": "record_relation", "evidence_ids": ["evidence:pair"]},
        {"id": "edge:business", "subject": "concept:metric", "object": "concept:measure",
         "predicate": "business_relation", "source_record_pair": {
             "source": "record:metric", "target": "record:measure"},
         "evidence_ids": ["evidence:pair"]},
    ]
    with sqlite3.connect(run / "work/results.sqlite") as db:
        db.execute("CREATE TABLE items (kind TEXT, id TEXT, body TEXT)")
        db.executemany("INSERT INTO items VALUES (?,?,?)", [
            *(('objects', item['id'], json.dumps(item)) for item in objects),
            *(('assertions', item['id'], json.dumps(item)) for item in relations),
            ('evidence', 'evidence:pair', json.dumps({'id': 'evidence:pair', 'raw_fragment': '同一见证记录对'})),
        ])
    html = render_viewer(run, 10).read_text()
    data = payload(html)
    assert data["relation_count"] == 2
    assert data["record_relation_count"] == 1
    assert data["business_relation_count"] == 1
    assert {item["viewer_layer"] for item in data["relations"]} == {"record", "business_type"}
    assert {item["viewer_layer"]: item["viewer_endpoint_labels"] for item in data["relations"]} == {
        "record": ["源记录", "目标记录"], "business_type": ["源概念", "目标概念"]}
    assert data["preview"]["type_counts"]["business_relation"] == 1
    assert 'data-mode="business_relations"' in html
    assert 'data-type-filter="business_relation"' in html


def test_viewer_shows_type_definition_and_bounded_record_attributes(tmp_path):
    config = setup(tmp_path, scenario="formula", mcp=False, rows=2)
    output = tmp_path / "run"
    assert asyncio.run(build(config, output))["status"] == "complete"
    first = payload(render_viewer(output, 10).read_text())
    object_id = first["objects"][0]["id"]
    long_description = "记录说明" + "x" * 2000 + "</script><script>window.BAD=1</script>"
    with sqlite3.connect(output / "work/results.sqlite") as db:
        for index in range(30):
            column = {0: "business_code", 1: "created_time", 2: "long_description"}.get(index, f"other_{index:02}")
            value = {0: "M001", 1: "2026-01-02 03:04:05", 2: long_description}.get(index, str(index))
            assertion = {"id": f"viewer-{index}", "subject": object_id, "predicate": "has",
                         "attribute": f"column:demo.records.{column}",
                         "literal": {"type": "string", "value": value}}
            db.execute("INSERT INTO items VALUES (?,?,?)", ("assertions", assertion["id"], json.dumps(assertion)))
    html = render_viewer(output, 10).read_text()
    data = payload(html)
    node = next(item for item in data["objects"] if item["id"] == object_id)
    attrs = {item["column"]: item for item in node["preview_attributes"]}
    assert node["preview_attribute_count"] > MAX_ATTRIBUTES_PER_OBJECT
    assert len(attrs) == MAX_ATTRIBUTES_PER_OBJECT
    assert attrs["business_code"]["value"] == "M001"
    assert attrs["created_time"]["value"] == "2026-01-02 03:04:05"
    assert attrs["long_description"]["truncated"] is True
    assert len(attrs["long_description"]["value"]) == MAX_ATTRIBUTE_CHARS
    assert long_description not in html
    assert "window.BAD=1" not in html
    relation_type = next(item for item in data["ontology"]["relation_types"]
                         if item["id"] == "calculation_depends_on")
    assert relation_type["definition"] == "由源公式明确引用目标定义"
    assert relation_type["evidence_ids"]
    assert "['标识','id']" in html and "['定义','definition']" in html
    assert "记录属性" in html


def test_viewer_previews_concepts_and_record_alignments_with_source_evidence(tmp_path):
    config = setup(tmp_path, scenario="unrelated", mcp=False, rows=2)
    output = tmp_path / "run"
    assert asyncio.run(build(config, output))["status"] == "complete"
    source_refs = [{"record_id": f"record:{index}"} for index in range(30)]
    concept = {"id": "concept:fruit_price", "type": "Measure", "label": "水果均价",
               "definition": "每千克水果的平均销售价格", "scope": {"currency": "CNY"},
               "source_refs": source_refs, "evidence_ids": ["evidence:fruit-price"],
               "identity_scope": "snapshot_only"}
    alignments = [{"id": f"alignment:{index:02}", "source_record_id": f"record:{index}",
                   "concept_id": concept["id"], "mapping_kind": "exact",
                   "evidence_ids": ["evidence:fruit-price"]} for index in range(11)]
    evidence = {"id": "evidence:fruit-price", "origin": "observed_record",
                "raw_fragment": "水果均价", "source_ref": {"table": "fruit.measure", "row": 1,
                                                    "column": "measure_name"}}
    with sqlite3.connect(output / "work/results.sqlite") as db:
        db.execute("INSERT INTO items VALUES (?,?,?)", ("objects", concept["id"], json.dumps(concept)))
        db.execute("INSERT INTO items VALUES (?,?,?)", ("evidence", evidence["id"], json.dumps(evidence)))
        db.executemany("INSERT INTO items VALUES (?,?,?)",
                       [("record_alignments", item["id"], json.dumps(item)) for item in alignments])
    construction = read_yaml(output / "construction.yaml")
    construction["group_steps"] = [{"bundle_id": "bundle:1", "task_kind": "concept_induction",
                                    "status": "accepted", "concept_id": concept["id"]}]
    write_yaml(output / "construction.yaml", construction)

    html = render_viewer(output, 10).read_text()
    data = payload(html)
    assert data["concept_count"] == 1
    assert data["counts"]["record_alignments"] == 11
    assert len(data["record_alignments"]) == 10
    assert data["record_alignments"][0]["source_record_id"] == "record:0"
    assert data["concepts"][0]["definition"] == concept["definition"]
    assert data["concepts"][0]["preview_source_ref_count"] == 30
    assert len(data["concepts"][0]["source_refs"]) == MAX_SOURCE_REFS_PER_CONCEPT
    assert data["evidence"][evidence["id"]]["raw_fragment"] == "水果均价"
    assert data["construction"]["group_steps"][0]["status"] == "accepted"
    assert 'data-mode="concepts"' in html and 'data-mode="alignments"' in html


def test_group_replay_viewer_distinguishes_unrun_stage_and_registers_cli_output(tmp_path, monkeypatch):
    run = tmp_path / "group-replay"
    (run / "work").mkdir(parents=True)
    write_yaml(run / "manifest.yaml", {
        "status": "partial", "input_records": 1213,
        "experimental_scope": "focused_group_replay_only",
        "llm": {"calls": 0, "cache_hits": 9},
    })
    write_yaml(run / "construction.yaml", {
        "steps": [], "group_steps": [{"bundle_id": "bundle:1", "status": "accepted"}],
        "direct_mapping_columns": 0,
    })
    with sqlite3.connect(run / "work/results.sqlite") as db:
        db.execute("CREATE TABLE items (kind TEXT, id TEXT, body TEXT)")

    monkeypatch.setattr(sys, "argv", ["ontology-r2", "visualize", "--run", str(run), "--max-nodes", "10"])
    cli_main()

    data = payload((run / "viewer.html").read_text())
    metrics = {item["label"]: item["value"] for item in data["preview"]["metrics"]}
    assert metrics["输入快照记录"] == 1213
    assert metrics["已接受对象关系"] == 0
    assert metrics["映射字段"] is None
    assert "仅展示本次执行的 1 个语义组" in data["preview"]["notice"]
    assert "模型缓存命中 9 次" in data["preview"]["notice"]
    assert data["manifest"]["viewer"] == "viewer.html"
    assert data["manifest"] == read_yaml(run / "manifest.yaml")

    (run / "work/results.sqlite").unlink()
    data = payload(render_viewer(run, 10).read_text())
    metrics = {item["label"]: item["value"] for item in data["preview"]["metrics"]}
    assert metrics["已抽取对象"] is None
    assert metrics["已接受对象关系"] is None
