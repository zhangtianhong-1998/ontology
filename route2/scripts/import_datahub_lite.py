"""Load an ontology-r2 DataHub metadata file into local DataHub Lite.

Optional demo, tested with ``acryl-datahub[datahub-lite]==1.7.0.12``:

    python scripts/import_datahub_lite.py run/datahub-metadata.json --catalog run/datahub-lite.duckdb

Only datasetProperties and schemaMetadata are accepted. This script provides
local storage and read-back verification; it does not expose graph traversal,
lineage discovery, or the full DataHub GMS/UI server.
"""

import argparse
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


DATAHUB_VERSION = "1.7.0.12"
ALLOWED_ASPECTS = {"datasetProperties", "schemaMetadata"}


def import_metadata_file(metadata_file: Path, catalog_file: Path) -> dict:
    try:
        installed_version = version("acryl-datahub")
    except PackageNotFoundError as exc:
        raise RuntimeError(f"Install acryl-datahub[datahub-lite]=={DATAHUB_VERSION} in this Python environment") from exc
    if installed_version != DATAHUB_VERSION:
        raise RuntimeError(f"Tested with acryl-datahub=={DATAHUB_VERSION}; found {installed_version}")

    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.lite.duckdb_lite import DuckDBLite
    from datahub.lite.duckdb_lite_config import DuckDBLiteConfig

    records = json.loads(metadata_file.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("Expected a DataHub metadata-file JSON array")

    mcps = []
    expected = {}
    for record in records:
        if not isinstance(record, dict) or record.get("entityType") != "dataset" or record.get("changeType") != "UPSERT" or record.get("aspectName") not in ALLOWED_ASPECTS:
            raise ValueError("Only dataset UPSERTs with datasetProperties/schemaMetadata are supported")
        mcp = MetadataChangeProposalWrapper.from_obj(record.copy())
        if not isinstance(mcp, MetadataChangeProposalWrapper) or not mcp.validate():
            raise ValueError("Invalid DataHub metadata proposal")
        key = (mcp.entityUrn, mcp.aspectName)
        if key in expected:
            raise ValueError(f"Duplicate dataset aspect: {key}")
        expected[key] = mcp.to_obj(simplified_structure=True)["aspect"]["json"]
        mcps.append(mcp)

    catalog_file.parent.mkdir(parents=True, exist_ok=True)
    # DataHub Lite's close() reindexes its DuckDB, so keep the default
    # read_only=False even for this read-back check.
    lite = DuckDBLite(DuckDBLiteConfig(file=str(catalog_file)))
    try:
        for mcp in mcps:
            lite.write(mcp)
        for (urn, aspect_name), body in expected.items():
            stored = lite.get(urn, [aspect_name])
            if not stored or stored.get(aspect_name) != body:
                raise RuntimeError(f"DataHub Lite read-back differs for {urn} / {aspect_name}")
    finally:
        lite.close()
    return {"catalog": str(catalog_file), "datasets": len({urn for urn, _ in expected}), "aspects_verified": len(expected)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Import table/column MCPs into local DataHub Lite and verify read-back")
    parser.add_argument("metadata_file", type=Path, help="DataHub metadata-file JSON exported by route 2")
    parser.add_argument("--catalog", type=Path, required=True, help="Local DuckDB catalog file")
    args = parser.parse_args()
    print(json.dumps(import_metadata_file(args.metadata_file, args.catalog), ensure_ascii=False))


if __name__ == "__main__":
    main()
