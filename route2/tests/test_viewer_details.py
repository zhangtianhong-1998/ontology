import asyncio
import json
import sqlite3

from ontology_r2.pipeline import build
from ontology_r2.visualization import (
    MAX_ATTRIBUTES_PER_OBJECT,
    MAX_ATTRIBUTE_CHARS,
    render_viewer,
)
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
