"""Export the existing technical graph as DataHub metadata-file MCPs.

This is an offline interchange adapter. It reads only table/column metadata from
``meta_graph.yaml``; it does not start DataHub, read CSV rows, or infer relations.
The JSON shape follows DataHub's simplified metadata-file sink (MCP + aspect.json).
"""

import hashlib
import json
import re
from pathlib import Path


def _urn_part(value, label):
    if not isinstance(value, str) or not value or any(c in value for c in ",()"):
        raise ValueError(f"Invalid DataHub {label}: {value!r}")
    return value


def _nullable(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "t", "yes", "1"}:
            return False
        if normalized in {"false", "f", "no", "0"}:
            return True
    if value is None:
        return True
    raise ValueError(f"Invalid is_not_null value: {value!r}")


def _datahub_type(native_type):
    name = native_type.lower().strip()
    if re.match(r"^(bool|boolean)\b", name):
        return "BooleanType"
    if re.match(r"^(smallint|integer|int|bigint|numeric|decimal|real|float|double|money|serial|bigserial)\b", name):
        return "NumberType"
    if re.match(r"^date\b", name):
        return "DateType"
    if re.match(r"^(time|timestamp|timestamptz|timetz|datetime)\b", name):
        return "TimeType"
    if re.match(r"^(bytea|blob|binary|varbinary)\b", name):
        return "BytesType"
    if name.endswith("[]") or name.startswith("array"):
        return "ArrayType"
    # DataHub requires a generic type. Keep the source's full type verbatim in
    # nativeDataType so this fallback cannot erase its original meaning.
    return "StringType"


def _mcp(urn, aspect_name, aspect, run_id):
    return {
        "entityType": "dataset",
        "entityUrn": urn,
        "changeType": "UPSERT",
        "aspectName": aspect_name,
        "aspect": {"json": aspect},
        # DataHub Lite's DuckDB writer expects this field to exist in the
        # serialized MCP. Zero means the source observation time is unknown.
        "systemMetadata": {"lastObserved": 0, "runId": run_id, "lastRunId": run_id, "properties": {}},
    }


def _declared_primary_keys(table_id, graph_nodes, edges, column_names):
    keys = []
    for edge in edges:
        if edge.get("source") != table_id or edge.get("type") != "has_declared_constraint":
            continue
        constraint = graph_nodes.get(edge.get("target"), {})
        if constraint.get("constraint_type") != "p" and str(constraint.get("constraint_type_name", "")).upper() != "PRIMARY KEY":
            continue
        match = re.search(r"PRIMARY\s+KEY\s*\(([^)]+)\)", str(constraint.get("definition", "")), re.I)
        if match:
            names = [item.strip().strip('"') for item in match.group(1).split(",")]
            if all(name in column_names for name in names):
                keys = names
                break
    return keys


def metadata_change_proposals(meta_graph, platform="postgres", environment="PROD"):
    """Return deterministic DataHub MCP dictionaries for table and column metadata.

    ``platform='postgres'`` is a practical default for GaussDB-compatible
    catalogs; callers may choose another registered DataHub platform key.
    ``meta_graph`` is the existing technical_graph() result or loaded YAML.
    """
    platform = _urn_part(platform, "platform")
    environment = _urn_part(environment, "environment")
    nodes = {node["id"]: node for node in meta_graph["nodes"]}
    edges = meta_graph["edges"]
    source_nodes = {node_id: node for node_id, node in nodes.items() if node.get("kind") == "Source"}
    snapshots = sorted(node_id for node_id, node in nodes.items() if node.get("kind") == "DatasetSnapshot")
    if len(snapshots) > 1:
        raise ValueError("Expected at most one DatasetSnapshot")
    snapshot_id = snapshots[0].removeprefix("snapshot:") if snapshots else None
    run_id = "ontology-r2-" + (snapshot_id or "metadata-export")
    proposals = []
    for table_id, table in sorted(nodes.items()):
        if table.get("kind") != "Table":
            continue
        qualified_name = _urn_part(table_id, "dataset name")
        table_name = table.get("table_name")
        if not isinstance(table_name, str) or not table_name:
            raise ValueError(f"Missing table_name for {table_id}")
        urn = f"urn:li:dataset:(urn:li:dataPlatform:{platform},{qualified_name},{environment})"
        table_edges = [edge for edge in edges if edge.get("source") == table_id]
        columns = [nodes[edge["target"]] for edge in table_edges if edge.get("type") == "table_has_column" and edge.get("target") in nodes]
        columns.sort(key=lambda col: (int(col.get("ordinal_position") or 0), col["column_name"]))
        column_names = {col["column_name"] for col in columns}
        primary_keys = _declared_primary_keys(table_id, nodes, table_edges, column_names)

        provenance = {}
        if snapshot_id:
            provenance["source_snapshot_id"] = snapshot_id
        for edge_type, label in (("documented_by", "source_schema"), ("sample_from", "source_csv")):
            source = next((source_nodes.get(edge["target"]) for edge in table_edges if edge.get("type") == edge_type and edge.get("target") in source_nodes), None)
            if source:
                provenance[label + "_file"] = str(source["path"])
                provenance[label + "_sha256"] = str(source["sha256"])
        if table.get("relkind") is not None:
            provenance["source_relkind"] = str(table["relkind"])
        if table.get("observed_rows") is not None:
            provenance["source_observed_rows"] = str(table["observed_rows"])

        properties = {"name": table_name, "qualifiedName": qualified_name, "customProperties": provenance}
        if table.get("table_comment"):
            properties["description"] = str(table["table_comment"])
        proposals.append(_mcp(urn, "datasetProperties", properties, run_id))

        fields = []
        for column in columns:
            native = str(column.get("data_type") or "unknown")
            generic = _datahub_type(native)
            field = {
                "fieldPath": column["column_name"],
                "nullable": _nullable(column.get("is_not_null")),
                "type": {"type": {f"com.linkedin.schema.{generic}": {}}},
                "nativeDataType": native,
                "recursive": False,
                "isPartOfKey": column["column_name"] in primary_keys,
            }
            if column.get("column_comment"):
                field["description"] = str(column["column_comment"])
            fields.append(field)
        schema_body = {
            "schemaName": qualified_name,
            "platform": f"urn:li:dataPlatform:{platform}",
            "version": 0,
            "hash": hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "platformSchema": {"com.linkedin.schema.OtherSchema": {"rawSchema": ""}},
            "fields": fields,
        }
        if primary_keys:
            schema_body["primaryKeys"] = primary_keys
        proposals.append(_mcp(urn, "schemaMetadata", schema_body, run_id))
    return proposals


def export_datahub_metadata(meta_graph, output, platform="postgres", environment="PROD"):
    """Write a DataHub metadata-file JSON array without a running server."""
    proposals = metadata_change_proposals(meta_graph, platform, environment)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(proposals, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"path": str(output), "datasets": len(proposals) // 2, "aspects": len(proposals)}
