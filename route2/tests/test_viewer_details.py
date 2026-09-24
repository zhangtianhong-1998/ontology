import asyncio
import json
import sqlite3

from ontology_r2.pipeline import build
from ontology_r2.visualization import (
    MAX_ATTRIBUTES_PER_OBJECT,
    MAX_ATTRIBUTE_CHARS,
    MAX_SOURCE_REFS_PER_CONCEPT,
    render_viewer,
)
from ontology_r2.storage import read_yaml, write_yaml
from test_pipeline import setup


def payload(html):
    return json.loads(html.split('<script id="result-data" type="application/json">', 1)[1]
                      .split('</script>', 1)[0])


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
