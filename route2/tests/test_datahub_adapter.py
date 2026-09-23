"""Offline DataHub interchange tests; no DataHub service or business CSV."""

import json

import pytest

from ontology_r2.datahub_adapter import export_datahub_metadata, metadata_change_proposals


def technical_graph():
    return {
        "nodes": [
            {"id": "snapshot:abc123", "kind": "DatasetSnapshot"},
            {"id": "source:schema/tables/items.yaml", "kind": "Source", "path": "schema/tables/items.yaml", "sha256": "schemahash"},
            {"id": "source:data/items.csv", "kind": "Source", "path": "data/items.csv", "sha256": "csvhash"},
            {"id": "demo.items", "kind": "Table", "schema": "demo", "table_name": "items", "table_comment": "条目定义", "relkind": "r", "observed_rows": 2},
            {"id": "demo.items.id", "kind": "Column", "column_name": "id", "ordinal_position": 1, "data_type": "bigint", "is_not_null": True, "column_comment": "标识"},
            {"id": "demo.items.title", "kind": "Column", "column_name": "title", "ordinal_position": 2, "data_type": "text", "is_not_null": False, "column_comment": "名称"},
            {"id": "demo.items:constraint:0", "kind": "Constraint", "constraint_type": "p", "definition": "PRIMARY KEY (id)"},
            {"id": "demo.other", "kind": "Table", "schema": "demo", "table_name": "other", "table_comment": "另一张表"},
            {"id": "demo.other.id", "kind": "Column", "column_name": "id", "ordinal_position": 1, "data_type": "boolean", "is_not_null": "false"},
        ],
        "edges": [
            {"source": "demo.items", "type": "documented_by", "target": "source:schema/tables/items.yaml"},
            {"source": "demo.items", "type": "sample_from", "target": "source:data/items.csv"},
            {"source": "demo.items", "type": "table_has_column", "target": "demo.items.title"},
            {"source": "demo.items", "type": "table_has_column", "target": "demo.items.id"},
            {"source": "demo.items", "type": "has_declared_constraint", "target": "demo.items:constraint:0"},
            {"source": "demo.other", "type": "table_has_column", "target": "demo.other.id"},
            # Even when present, FK edges are not imported as inferred lineage.
            {"source": "demo.items.id", "type": "declared_fk", "target": "demo.other.id"},
        ],
    }


def test_mcp_shape_types_provenance_and_no_invented_relations():
    proposals = metadata_change_proposals(technical_graph())
    assert len(proposals) == 4
    assert all(p["systemMetadata"] == {"lastObserved": 0, "runId": "ontology-r2-abc123", "lastRunId": "ontology-r2-abc123", "properties": {}} for p in proposals)
    assert [(p["entityUrn"], p["aspectName"]) for p in proposals] == [
        ("urn:li:dataset:(urn:li:dataPlatform:postgres,demo.items,PROD)", "datasetProperties"),
        ("urn:li:dataset:(urn:li:dataPlatform:postgres,demo.items,PROD)", "schemaMetadata"),
        ("urn:li:dataset:(urn:li:dataPlatform:postgres,demo.other,PROD)", "datasetProperties"),
        ("urn:li:dataset:(urn:li:dataPlatform:postgres,demo.other,PROD)", "schemaMetadata"),
    ]
    props = proposals[0]["aspect"]["json"]
    assert props["description"] == "条目定义"
    assert props["customProperties"] == {
        "source_snapshot_id": "abc123",
        "source_schema_file": "schema/tables/items.yaml",
        "source_schema_sha256": "schemahash",
        "source_csv_file": "data/items.csv",
        "source_csv_sha256": "csvhash",
        "source_relkind": "r",
        "source_observed_rows": "2",
    }
    schema = proposals[1]["aspect"]["json"]
    assert schema["platform"] == "urn:li:dataPlatform:postgres"
    assert schema["platformSchema"] == {"com.linkedin.schema.OtherSchema": {"rawSchema": ""}}
    assert schema["primaryKeys"] == ["id"]
    assert [f["fieldPath"] for f in schema["fields"]] == ["id", "title"]
    assert schema["fields"][0]["isPartOfKey"] and not schema["fields"][0]["nullable"]
    assert schema["fields"][0]["type"] == {"type": {"com.linkedin.schema.NumberType": {}}}
    assert schema["fields"][1]["nullable"] and schema["fields"][1]["description"] == "名称"
    assert proposals[3]["aspect"]["json"]["fields"][0]["nullable"]
    assert not any("foreignKeys" in p["aspect"]["json"] or p["aspectName"] == "upstreamLineage" for p in proposals)


def test_file_export_is_deterministic_and_requires_no_sdk(tmp_path):
    graph = technical_graph()
    output = tmp_path / "datahub-metadata.json"
    report = export_datahub_metadata(graph, output, platform="postgres", environment="DEV")
    first = output.read_bytes()
    assert report == {"path": str(output), "datasets": 2, "aspects": 4}
    assert json.loads(first)[0]["entityUrn"].endswith(",DEV)")
    export_datahub_metadata(graph, output, platform="postgres", environment="DEV")
    assert output.read_bytes() == first


def test_invalid_urn_or_nullability_fails_closed():
    graph = technical_graph()
    with pytest.raises(ValueError, match="platform"):
        metadata_change_proposals(graph, platform="postgres,evil")
    graph["nodes"][4]["is_not_null"] = "perhaps"
    with pytest.raises(ValueError, match="is_not_null"):
        metadata_change_proposals(graph)
