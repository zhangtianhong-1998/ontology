"""Run the local field-link stages without spending LLM calls."""

import argparse
import asyncio
import time
from pathlib import Path

from ontology_r2.association_rules import build_association_rules
from ontology_r2.discovery import discover_and_check
from ontology_r2.pipeline import load_config
from ontology_r2.storage import Dataset, digest, write_yaml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--value-index-mode", choices=("sample", "full_distinct"),
                        default="full_distinct")
    parser.add_argument("--max-indexed-fields", type=int, default=0)
    parser.add_argument("--candidate-validations", type=int, default=50)
    args = parser.parse_args()
    config = load_config(args.config)
    config["dataset"] = str(Path(args.dataset).resolve())
    discovery_config = {**config.get("discovery", {}),
                        "value_index_mode": args.value_index_mode,
                        "max_indexed_fields": args.max_indexed_fields,
                        "max_candidate_validations": args.candidate_validations}
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "work").mkdir()
    started = time.monotonic()
    data = Dataset(config["dataset"], output / "work",
                   config.get("memory_limit", "1GB"), config.get("profiling"),
                   privacy_config=config.get("privacy"))
    try:
        discovery = discover_and_check(data, discovery_config)
        rule_config = {**config.get("association_rules", {}), "agent_enabled": False}
        association = asyncio.run(build_association_rules(data, discovery, rule_config))
        manifest = {
            "status": "component_complete", "component_only": True,
            "snapshot_id": data.snapshot_id, "input_tables": len(data.tables),
            "input_records": sum(table["rows"] for table in data.tables.values()),
            "config_hash": digest([config, discovery_config, rule_config]),
            "discovery": {"candidate_count": len(discovery["candidates"]),
                          "checked_count": discovery["coverage"]["candidates_checked"],
                          "value_index_fields": discovery["coverage"]["value_index_fields"],
                          "partial": discovery["coverage"]["partial"]},
            "association_rule_statuses": association["coverage"]["statuses"],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "llm_calls": 0,
        }
        for name, value in (("field_candidates", discovery["candidates"]),
                            ("association_checks", discovery["checks"]),
                            ("discovery_coverage", discovery["coverage"]),
                            ("association_rules", association),
                            ("manifest", manifest)):
            write_yaml(output / (name + ".yaml"), value)
        print(manifest)
    finally:
        data.close()


if __name__ == "__main__":
    main()
