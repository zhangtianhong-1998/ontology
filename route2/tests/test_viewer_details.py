import asyncio
import json
import sqlite3
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ontology_r2.cli import main as cli_main
from ontology_r2.pipeline import build
from ontology_r2.visualization import (
    MAX_ATTRIBUTES_PER_OBJECT,
    MAX_ATTRIBUTE_CHARS,
    MAX_SOURCE_REFS_PER_CONCEPT,
    _metadata_preview,
    _ontology_overview,
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
    assert metadata["link_counts"] == {"declared": 1, "verified_technical": 1,
                                       "observed_subset": 0, "candidate": 1}
    assert {link["status"] for link in metadata["links"]} == {
        "declared", "verified_technical", "candidate"}
    assert metadata["links"][1]["semantic_relation"] == "unresolved"
    assert metadata["node_kinds"]["Table"] == 2
    assert metadata["edge_kinds"]["table_has_column"] == 1


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


def test_metadata_link_preview_keeps_late_table_connection():
    graph = {"nodes": [{"id": name, "kind": "Table"}
                       for name in ("demo.a", "demo.b", "demo.z")], "edges": []}
    candidates = [
        {"candidate_id": f"dense-{index}",
         "source": {"table": "demo.a", "field": f"code_{index}"},
         "target": {"table": "demo.b", "field": f"code_{index}"}}
        for index in range(150)
    ] + [{"candidate_id": "late", "source": {"table": "demo.z", "field": "code"},
          "target": {"table": "demo.b", "field": "code"}}]
    preview = _metadata_preview(graph, candidates, {"rules": []}, 10)
    assert preview["total_links"] == 151 and preview["shown_links"] == 100
    assert any(link["id"] == "candidate:late" for link in preview["links"])


def test_metadata_graph_fallback_preserves_subset_without_duplicate_candidate():
    graph = {"nodes": [
        {"id": "demo.a", "kind": "Table"}, {"id": "demo.b", "kind": "Table"},
        {"id": "demo.a.code", "kind": "Column", "column_name": "code"},
        {"id": "demo.b.code", "kind": "Column", "column_name": "code"},
    ], "edges": [
        {"source": "demo.a", "target": "demo.a.code", "type": "table_has_column"},
        {"source": "demo.b", "target": "demo.b.code", "type": "table_has_column"},
        {"source": "demo.a.code", "target": "demo.b.code",
         "type": "inferred_technical_match", "status": "observed_subset", "rule_id": "r1"},
    ]}
    candidate = {"candidate_id": "c1", "source": {"table": "demo.a", "field": "code"},
                 "target": {"table": "demo.b", "field": "code"}}
    rule = {"rule_id": "r1", "candidate_id": "c1", "status": "observed_subset",
            "source": candidate["source"], "target": candidate["target"]}
    preview = _metadata_preview(graph, [candidate], {"rules": [rule]}, 10)
    assert preview["link_counts"]["observed_subset"] == 1
    assert preview["link_counts"]["candidate"] == 0
    assert preview["links"][0]["status"] == "observed_subset"
    fallback = _metadata_preview(graph, [], {"rules": []}, 10)
    assert fallback["links"][0]["status"] == "observed_subset"


def test_metadata_view_preserves_numeric_collision_risk_from_rule_or_graph():
    graph = {"nodes": [
        {"id": "s.a", "kind": "Table"}, {"id": "s.b", "kind": "Table"},
        {"id": "s.a.code", "kind": "Column", "column_name": "code"},
        {"id": "s.b.number", "kind": "Column", "column_name": "number"}],
        "edges": [
            {"source": "s.a", "target": "s.a.code", "type": "table_has_column"},
            {"source": "s.b", "target": "s.b.number", "type": "table_has_column"},
            {"id": "technical:test", "source": "s.a.code", "target": "s.b.number",
             "type": "technical_link", "status": "observed_subset", "rule_id": "r1",
             "numeric_overlap_only": True, "risk_flags": ["numeric_value_coincidence"]}]}
    rule = {"rule_id": "r1", "source": {"table": "s.a", "field": "code"},
            "target": {"table": "s.b", "field": "number"}, "status": "observed_subset",
            "numeric_overlap_only": True, "risk_flags": ["numeric_value_coincidence"]}
    for rules in ({"rules": [rule]}, {"rules": []}):
        preview = _metadata_preview(graph, [], rules, 10)
        assert preview['links']
        assert all(link['numeric_overlap_only'] is True for link in preview['links'])
        assert all(link['risk_flags'] == ['numeric_value_coincidence'] for link in preview['links'])
        assert all(link['status'] == 'observed_subset' for link in preview['links'])
    html = (Path(__file__).resolve().parents[1] / 'code/ontology_r2/viewer.html').read_text()
    assert "if(x.numeric_overlap_only)" in html
    assert "数值重合风险" in html and "风险标记" in html
    assert '.risk-warning{' in html


def test_metadata_graph_exposes_table_to_source_record_type_mapping():
    graph = {"nodes": [
        {"id": "demo.metric", "kind": "Table", "table_name": "metric"},
        {"id": "source_record_type:demo.metric", "kind": "SourceRecordType",
         "label": "指标定义记录", "business_concept_inferred": False},
    ], "edges": [{"source": "demo.metric", "target": "source_record_type:demo.metric",
                "type": "mapped_as_source_record_type"}]}
    preview = _metadata_preview(graph, [], {"rules": []}, 10)
    assert preview["node_kinds"]["SourceRecordType"] == 1
    assert preview["details"]["demo.metric"]["record_types"][0]["business_concept_inferred"] is False


def test_viewer_payload_keeps_local_match_candidates_outside_metadata_graph(tmp_path):
    run = tmp_path / "metadata-boundary"
    run.mkdir()
    write_yaml(run / "manifest.yaml", {"status": "complete"})
    write_yaml(run / "meta_graph.yaml", {"nodes": [
        {"id": "demo.a", "kind": "Table"}, {"id": "demo.b", "kind": "Table"},
        {"id": "demo.a.code", "kind": "Column", "column_name": "code"},
        {"id": "demo.b.code", "kind": "Column", "column_name": "code"},
    ], "edges": [
        {"source": "demo.a", "target": "demo.a.code", "type": "table_has_column"},
        {"source": "demo.b", "target": "demo.b.code", "type": "table_has_column"},
        {"source": "demo.a.code", "target": "demo.b.code", "type": "declared_fk"},
    ]})
    write_yaml(run / "association_rules.yaml", {"rules": [{
        "rule_id": "rule-1", "source": {"table": "demo.a", "field": "alias"},
        "target": {"table": "demo.b", "field": "name"},
        "status": "checked_technical",
    }]})
    write_yaml(run / "field_candidates.yaml", [{
        "candidate_id": "candidate-1", "source": {"table": "demo.a", "field": "word"},
        "target": {"table": "demo.b", "field": "word"},
    }])
    data = payload(render_viewer(run, 10).read_text())
    assert {item["status"] for item in data["metadata"]["links"]} == {"declared", "verified_technical"}
    assert {item["status"] for item in data["association_preview"]["links"]} == {"candidate"}
    assert data["metadata"]["link_counts"] == {"declared": 1, "verified_technical": 1, "observed_subset": 0}
    assert {item["edge_type"] for item in data["metadata"]["links"]} == {"declared_fk", "technical_link"}


def test_ontology_overview_keeps_business_structure_without_source_fields_or_cartesian_edges():
    overview = _ontology_overview({
        "object_roots": [{"id": "GeneralObject"}, {"id": "Metric"}, {"id": "Measure"}],
        "object_types": [
            {"id": "FruitRevenue", "parent": "Metric", "category": "business_type"},
            {"id": "FruitCost", "parent": "Metric", "category": "business_type"},
            {"id": "Income", "parent": "Measure", "category": "business_type"},
            {"id": "source_record_type:metric", "parent": "GeneralObject", "category": "source_record_type"},
        ],
        "attributes": [{"id": "source_column:metric.name", "domain": ["FruitRevenue"]}],
        "relation_types": [
            {"id": "depends", "label": "depends_on", "category": "business_relation_type",
             "domain": ["FruitRevenue"], "range": ["FruitCost"]},
            {"id": "ambiguous", "category": "business_relation_type",
             "domain": ["FruitRevenue", "FruitCost"], "range": ["Income"]},
            {"id": "record_link", "domain": ["source_record_type:metric"],
             "range": ["FruitRevenue"]},
        ],
    })
    assert {item["id"] for item in overview["nodes"]} == {
        "GeneralObject", "Metric", "Measure", "FruitRevenue", "FruitCost", "Income"}
    assert {tuple(item.values()) for item in overview["inheritance"]} == {
        ("FruitRevenue", "Metric"), ("FruitCost", "Metric"), ("Income", "Measure")}
    assert overview["relations"] == [{"id": "depends", "source": "FruitRevenue",
                                       "target": "FruitCost", "label": "depends_on", "parent": None}]
    assert overview["omitted_relation_type_ids"] == ["ambiguous", "record_link"]


def test_viewer_counts_business_and_source_record_types_separately(tmp_path):
    run = tmp_path / "type-preview"
    run.mkdir()
    write_yaml(run / "manifest.yaml", {"status": "complete", "input_tables": 2})
    write_yaml(run / "ontology.yaml", {
        "object_roots": [{"id": "GeneralObject"}, {"id": "Metric"}, {"id": "Measure"}],
        "object_types": [
            {"id": "source_record_type:demo.api", "parent": "GeneralObject",
             "category": "source_record_type"},
            {"id": "source_record_type:demo.measure", "parent": "GeneralObject",
             "category": "source_record_type"},
            {"id": "FruitRevenue", "parent": "Metric", "category": "business_type"},
            {"id": "Income", "parent": "Measure", "category": "business_type"},
            {"id": "legacy", "parent": "Measure"}],
        "relation_roots": [{"id": "depends_on"}],
        "relation_types": [{"id": "calculation_depends_on", "parent": "depends_on"}],
    })
    html = render_viewer(run, 10).read_text()
    data = payload(html)
    metrics = {item["label"]: item["value"] for item in data["preview"]["metrics"]}
    assert metrics["已接受本体对象"] == 2
    assert metrics["源记录类型"] == 2
    assert data["preview"]["type_counts"] == {
        "business": 2, "source_record": 2, "unclassified": 1, "relation": 1,
        "business_relation": 0, "record_relation": 1}
    assert 'data-mode="ontology"' in html
    assert 'data-mode="metadata"' in html
    assert 'data-type-filter=' not in html
    assert '<section class="metrics"' not in html
    assert {item["id"] for item in data["ontology_overview"]["nodes"]} == {
        "GeneralObject", "Metric", "Measure", "FruitRevenue", "Income"}
    assert {item["source"] for item in data["ontology_overview"]["inheritance"]} == {
        "FruitRevenue", "Income"}
    assert "search-panel\" class=\"search-panel\" hidden" in html
    assert "Measure:'度量'" in html and "整体本体结构" in html
    assert "聚合度量" not in html
    assert "cols.slice(0,6)" not in html
    assert "kind:link.status==='declared'?'declared':'technical_link'" in html
    assert "业务类型" not in html


def test_viewer_uses_fitted_pan_zoom_canvas(tmp_path):
    run = tmp_path / "fit-canvas"
    run.mkdir()
    write_yaml(run / "manifest.yaml", {"status": "complete"})
    write_yaml(run / "ontology.yaml", {
        "object_roots": [{"id": "Metric"}],
        "object_types": [{"id": "Revenue", "parent": "Metric", "category": "business_type"}],
    })
    html = render_viewer(run, 10).read_text()
    assert 'id="graph-viewport"' in html
    assert 'id="zoom-in"' in html and 'id="zoom-out"' in html
    assert 'id="zoom-fit"' in html and 'id="zoom-level"' in html
    assert "addEventListener('wheel'" in html
    assert "setPointerCapture(event.pointerId)" in html
    assert ".graph-viewport{overflow:hidden" in html
    assert "svg.style.width" not in html
    assert "svgEl('circle',{cx:n.x,cy:n.y,r:NODE_RADIUS,fill})" in html
    assert "svgEl('rect',{x:n.x" not in html
    assert "'data-edge-label-for':index" in html
    assert "edges.length<30" not in html


def test_viewer_shows_verified_equivalence_on_business_type_without_relation_edge(tmp_path):
    run = tmp_path / "equivalent-types"
    run.mkdir()
    write_yaml(run / "manifest.yaml", {"status": "complete"})
    write_yaml(run / "ontology.yaml", {
        "object_roots": [{"id": "Metric"}],
        "object_types": [
            {"id": "ProfitA", "label": "水果利润", "parent": "Metric",
             "category": "business_type", "canonical_type_id": "ProfitA",
             "equivalent_type_ids": ["ProfitB"]},
            {"id": "ProfitB", "label": "水果利润", "parent": "Metric",
             "category": "business_type", "canonical_type_id": "ProfitA",
             "equivalent_type_ids": ["ProfitA"]},
        ],
        "relation_types": [],
        "type_equivalences": [{
            "id": "type_equivalence:profit", "source_type_id": "ProfitA",
            "target_type_id": "ProfitB", "canonical_type_id": "ProfitA",
            "semantic_equivalence_explanation": "完整公式、单位与适用范围一致",
            "source_description_quote": "水果销售收入扣除成本",
            "target_description_quote": "水果销售收入减去成本",
            "source_formula_quote": "收入-成本",
            "target_formula_quote": "收入-成本",
            "evidence_ids": ["metric-description-a", "metric-description-b"],
        }],
    })
    html = render_viewer(run, 10).read_text()
    data = payload(html)
    assert data["ontology"]["object_types"][1]["canonical_type_id"] == "ProfitA"
    assert data["ontology"]["type_equivalences"][0]["source_type_id"] == "ProfitA"
    assert data["ontology"]["relation_types"] == []
    assert "已验证等价来源类型" in html
    assert "规范类型" in html and "等价判定依据" in html
    assert "source_description_quote" in html and "source_formula_quote" in html
    assert "verifiedEquivalentTypes(x)" in html
    assert "equivalents,y=>button(name(y),y.id,()=>select('object',y.id))" in html


def test_viewer_includes_data_attribute_evidence_and_definition_only_scope(tmp_path):
    run = tmp_path / "property-evidence"
    (run / "work").mkdir(parents=True)
    write_yaml(run / "manifest.yaml", {"status": "complete"})
    write_yaml(run / "ontology.yaml", {
        "object_roots": [{"id": "Metric"}],
        "object_types": [{"id": "Profit", "label": "利润", "parent": "Metric",
                          "category": "business_type"}],
        "attributes": [{"id": "business_property:formula", "label": "计算公式",
                        "relation_type": "has", "domain": ["Profit"],
                        "literal_type": "string", "evidence_scope": "definition_record",
                        "mapping_status": "definition_record_only",
                        "source_bindings": [{"table": "demo.metric", "column": "formula"}],
                        "evidence_ids": ["record:formula"]}],
    })
    with sqlite3.connect(run / "work/results.sqlite") as db:
        db.execute("CREATE TABLE items (kind TEXT, id TEXT, body TEXT)")
        evidence = {"id": "record:formula", "raw_fragment": "利润=收入-成本",
                    "source_ref": {"table": "demo.metric", "column": "formula", "row": 1}}
        db.execute("INSERT INTO items VALUES (?,?,?)", ("evidence", evidence["id"], json.dumps(evidence)))
    html = render_viewer(run, 10).read_text()
    data = payload(html)
    assert data["evidence"]["record:formula"]["raw_fragment"] == "利润=收入-成本"
    assert "definition_record_only" in html and "源字段绑定" in html


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
    assert 'data-mode="ontology"' in html
    assert "对象属性与连接" in html
    assert "本体对象关系实例" in html


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
    assert "field(p,'定义'" in html
    assert "数据值" in html


def test_viewer_literal_values_keep_their_own_source_evidence(tmp_path):
    config = setup(tmp_path, scenario="unrelated", mcp=False, rows=2)
    output = tmp_path / "run"
    assert asyncio.run(build(config, output))["status"] == "complete"
    result = payload(render_viewer(output, 10).read_text())
    attributes = [value for node in result["objects"] for value in node.get("preview_attributes", [])]
    assert attributes and all(value["id"] and value["attribute_id"] for value in attributes)
    assert all(value["evidence_ids"] for value in attributes)
    assert all(eid in result["evidence"] for value in attributes for eid in value["evidence_ids"])


def test_viewer_inherited_properties_execute_against_actual_script():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute viewer helpers")
    page = (Path(__file__).parents[1] / "code/ontology_r2/viewer.html").read_text()
    start = page.index("function typeAncestors(")
    end = page.index("function verifiedEquivalentTypes(", start)
    script = """
const T=new Map([{id:'Root'},{id:'Parent',parent:'Root'},{id:'Child',parent:'Parent'},
{id:'Other',parent:'Root'},{id:'Cycle',parent:'Cycle'}].map(item=>[item.id,item]));
const attrs=[{id:'shared',domain:['Parent']},{id:'own',domain:['Child']},{id:'unrelated',domain:['Other']}];
const O={relation_types:[{id:'rel',category:'business_relation_type',domain:['Parent'],range:['Other']}]};
const D={objects:[{id:'one',type:'Child'},{id:'two',type:'Other'}],concepts:[{id:'one',ontology_type_id:'Child'}]};
const name=item=>item.id;
""" + page[start:end] + """
const assert=require('node:assert/strict');
assert.deepEqual(objectAttrs('Child').map(item=>item.id),['shared','own']);
assert.deepEqual(objectAttrs('Other').map(item=>item.id),['unrelated']);
assert.deepEqual(objectRels('Child').map(item=>item.id),['rel']);
assert.deepEqual(objectInstances('Parent').map(item=>item.id),['one']);
assert.equal(inheritedFrom(attrs[0],'Child'),'Parent');
assert.deepEqual([...typeAncestors('Cycle')],['Cycle']);
"""
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


def test_viewer_fit_and_zoom_execute_against_actual_script():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute viewer helpers")
    page = (Path(__file__).parents[1] / "code/ontology_r2/viewer.html").read_text()
    helpers = page[page.index('function fittedView('):page.index('function zoomAtCenter(')]
    script = """
const assert=require('node:assert/strict');
let size={width:830,height:490};
const elements={graph:{setAttribute(key,value){this[key]=value},getBoundingClientRect(){return {left:0,top:0,...size}}},'zoom-level':{}};
const $=id=>elements[id],viewportSize=()=>size,graphState={};
""" + helpers + """
const bounds={x:-410,y:-330,width:940,height:760};
for(const dimensions of [{width:830,height:490},{width:400,height:700},{width:1800,height:520}]){
  size=dimensions;graphState.bounds=bounds;graphState.userViewChanged=true;fitGraph();
  const v=graphState.view;
  assert(v.x<=bounds.x && v.y<=bounds.y);
  assert(v.x+v.width>=bounds.x+bounds.width-1e-8);
  assert(v.y+v.height>=bounds.y+bounds.height-1e-8);
  assert.equal(elements['zoom-level'].textContent,'100%');
  const center=[v.x+v.width/2,v.y+v.height/2];
  zoomGraph(1.25,size.width/2,size.height/2);
  assert.equal(elements['zoom-level'].textContent,'125%');
  assert(Math.abs(graphState.view.x+graphState.view.width/2-center[0])<1e-8);
  assert(Math.abs(graphState.view.y+graphState.view.height/2-center[1])<1e-8);
  fitGraph();assert.equal(elements['zoom-level'].textContent,'100%');assert.equal(graphState.userViewChanged,false);
}
"""
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    assert '.graph-viewport svg{display:block;position:absolute;inset:0' in page
    assert 'graphState.key===key&&graphState.view&&graphState.userViewChanged' in page


def test_metadata_layout_aggregation_and_focus_execute_against_actual_script():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute viewer helpers")
    page = (Path(__file__).parents[1] / "code/ontology_r2/viewer.html").read_text()
    helpers = page[page.index('function tableLabelLines('):page.index('function renderGraph(')]
    script = """
const assert=require('node:assert/strict');
const M={tables:Array.from({length:23},(_,i)=>({id:`s.t${i}`,table_name:`fruit_config_table_${i}`,table_comment:`水果定义${i}`})),links:[],details:{}};
for(let i=0;i<23;i++)for(let j=1;j<=3;j++)M.links.push({id:`link:${i}:${j}`,source:`s.t${i}`,target:`s.t${(i+j)%23}`,source_field:'ref',target_field:'code',status:'verified_technical'});
M.links.push({id:'reverse',source:'s.t1',target:'s.t0',source_field:'back_ref',target_field:'other',status:'verified_technical'});
const TABLE=new Map(M.tables.map(t=>[t.id,t])),expandedTables=new Set(),pinnedPositions={ontology:new Map(),metadata:new Map()};
let selected=null,tableId=null,drawn=null,showNumericMatches=false;
const $=id=>({}),name=x=>x.table_comment||x.table_name,node=(id,label,kind,x,y,action,active,subtitle)=>({id,label,kind,x,y,action,active,subtitle}),select=()=>{};
const drawGraph=(nodes,edges)=>{drawn={nodes,edges}};
""" + helpers + """
metadataGraph();
assert.equal(drawn.nodes.length,23);
assert.equal([...metadataLinkGroups.values()].reduce((n,g)=>n+g.links.length,0),M.links.length);
assert([...metadataLinkGroups.values()].some(g=>g.bidirectional));
assert(drawn.edges.filter(e=>e.showLabel).length<=4);
assert(drawn.edges.every(e=>typeof e.action==='function'));
assert(drawn.nodes.every(n=>n.label.startsWith('水果定义')&&n.subtitle.startsWith('fruit_config')));
const first=metadataPositions(M.tables,M.links),again=metadataPositions([...M.tables].reverse(),M.links);
assert.deepEqual([...first],[...again]);
const coords=[...first.values()];
for(let i=0;i<coords.length;i++)for(let j=i+1;j<coords.length;j++)assert(Math.hypot(coords[i].x-coords[j].x,coords[i].y-coords[j].y)>100);
assert(Math.max(...coords.map(p=>p.x))-Math.min(...coords.map(p=>p.x))<1400);
// Risk filtering changes visibility, never deletes evidence or hides declared FKs.
const risk={id:'numeric',source:'s.t0',target:'s.t22',numeric_overlap_only:true,status:'verified_technical'};
const declared={...risk,id:'declared',status:'declared'};
M.links.push(risk,declared);
assert.equal(metadataVisibleLinks(M.links,null,false).length,M.links.length-1);
assert(metadataVisibleLinks(M.links,null,false).includes(declared));
assert(!metadataVisibleLinks(M.links,null,false).includes(risk));
assert(metadataVisibleLinks(M.links,'s.t0',true).includes(risk));
assert(!metadataVisibleLinks(M.links,'s.t1',true).includes(risk));
assert.equal(M.links.at(-2),risk);
M.links.splice(-2);
selected={kind:'table',id:'s.t0'};tableId='s.t0';metadataGraph();
assert(drawn.nodes.length<23);
assert(drawn.edges.every(e=>e.from==='s.t0'||e.to==='s.t0'));
assert(drawn.edges.every(e=>e.showLabel));
assert.deepEqual(tableLabelLines('水果接口入参定义'),['水果接口入参','定义']);
"""
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    assert "(OV.inheritance||[]).forEach" in page and "(OV.relations||[]).forEach" in page


def test_metadata_graph_edges_keep_conditional_branches_and_source_details():
    from ontology_r2.metadata_graph import technical_link_id

    source, target = {"table": "demo.a", "field": "code"}, {"table": "demo.b", "field": "code"}
    rules = [{"rule_id": "same", "source": source, "target": target,
              "status": "checked_technical", "selector": {"kind": kind},
              "transform": {"operator": operator}, "evidence_ids": ["quote:" + kind]}
             for kind, operator in (("one", "identity"), ("two", "source_alias_items"))]
    graph = {"nodes": [
        {"id": "demo.a", "kind": "Table"}, {"id": "demo.b", "kind": "Table"},
        {"id": "demo.a.code", "kind": "Column", "column_name": "code"},
        {"id": "demo.b.code", "kind": "Column", "column_name": "code"},
    ], "edges": [
        {"source": "demo.a", "target": "demo.a.code", "type": "table_has_column"},
        {"source": "demo.b", "target": "demo.b.code", "type": "table_has_column"},
        *[{**rule, "id": technical_link_id(rule), "type": "technical_link",
           "source": "demo.a.code", "target": "demo.b.code"} for rule in rules],
    ]}
    preview = _metadata_preview(graph, [], {"rules": rules}, 10)
    assert len(preview["links"]) == 2
    assert len({edge["id"] for edge in preview["links"]}) == 2
    assert {edge["transform"]["operator"] for edge in preview["links"]} == {"identity", "source_alias_items"}
    fallback = _metadata_preview(graph, [], {"rules": []}, 10)
    assert {edge["id"] for edge in fallback["links"]} == {edge["id"] for edge in preview["links"]}
    assert all(edge["evidence_ids"] for edge in fallback["links"])


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
    assert 'data-mode="ontology"' in html and 'data-mode="metadata"' in html
    assert "objectInstances" in html


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


def test_template_preview_bounds_large_yaml_and_keeps_instances_off_canvas(tmp_path):
    from ontology_r2.visualization import _sequence_preview

    run = tmp_path / "template-preview"
    run.mkdir()
    write_yaml(run / "manifest.yaml", {"status": "complete"})
    write_yaml(run / "ontology.yaml", {
        "object_roots": [{"id": "Metric"}, {"id": "Measure"}],
        "object_types": [{"id": "QualityRate", "label": "合格率", "parent": "Metric",
                          "category": "business_type"},
                         {"id": "PassCount", "label": "合格数量", "parent": "Measure",
                          "category": "business_type"}],
        "relation_types": [],
    })
    write_yaml(run / "template_projections.yaml", [{
        "template_id": "projection:quality", "object_type_id": "QualityRate",
        "source_table": "demo.metrics", "slots": [{"name": "fruit", "role": "dimension"}],
    }])
    write_yaml(run / "template_bindings.yaml", [{
        "id": f"binding:{i}", "template_id": "projection:quality", "object_type_id": "QualityRate",
        "source_table": "demo.metrics", "record_id": f"record:{i}", "slot_values": {"fruit": str(i)},
    } for i in range(500)])
    write_yaml(run / "ontology_bindings.yaml", {
        "bindings": [{"id": "semantic:quality", "source_type_id": "QualityRate",
                      "target_type_id": "PassCount", "slot": {"role": "measure"}}],
        "pending": [{"template_id": "projection:quality", "reason": "missing dimension definition"}],
        "coverage": {"accepted": 1, "pending": 1},
    })
    data = payload(render_viewer(run, 10).read_text())
    preview = data["templates"]
    assert preview["truncated"] is True
    assert len(preview["bindings"]) == 3
    assert preview["binding_prefix_counts"]["QualityRate"] == 150
    assert len(preview["projections"]) == len(preview["ontology_bindings"]) == len(preview["pending"]) == 1
    assert {item["id"] for item in data["ontology_overview"]["nodes"]} == {
        "Metric", "Measure", "QualityRate", "PassCount"}
    # Cutting an input byte prefix retains only fully parsed records, never a partial instance.
    rows, summary = _sequence_preview(run / "template_bindings.yaml", 500, byte_limit=900)
    assert summary["truncated"] and summary["read_bytes"] == 900
    assert 0 < len(rows) < 10
    assert all("slot_values" in row for row in rows)


def test_template_preview_collects_source_evidence_for_bindings(tmp_path):
    run = tmp_path / "template-evidence"
    (run / "work").mkdir(parents=True)
    write_yaml(run / "template_projections.yaml", [{
        "template_id": "p", "object_type_id": "QualityRate", "evidence_ids": ["proof:template"]}])
    write_yaml(run / "template_bindings.yaml", [{
        "id": "b", "object_type_id": "QualityRate", "evidence_ids": ["proof:binding"]}])
    with sqlite3.connect(run / "work/results.sqlite") as db:
        db.execute("CREATE TABLE items(kind TEXT,id TEXT,body TEXT)")
        for key in ("proof:template", "proof:binding"):
            db.execute("INSERT INTO items VALUES(?,?,?)", ("evidence", key, json.dumps({
                "id": key, "raw_fragment": "合格率", "source_ref": {"table": "demo.metrics"}})))
    data = payload(render_viewer(run, 10).read_text())
    assert {"proof:template", "proof:binding"} <= data["evidence"].keys()
    html = (Path(__file__).parents[1] / "code/ontology_r2/viewer.html").read_text()
    assert "templateDetails(p,x)" in html and "成分关系依据" in html
