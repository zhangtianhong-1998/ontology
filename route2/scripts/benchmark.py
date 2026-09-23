"""Reproducible synthetic scale checks; capped runs must remain partial."""
import argparse
import asyncio
import json
import resource
import sys
import time
from pathlib import Path

from ontology_r2.demo import make_demo
from ontology_r2.pipeline import build, load_config
from ontology_r2.storage import Dataset, read_yaml, write_yaml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, help="New directory")
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--record-cap", type=int, default=10000)
    parser.add_argument("--kind", choices=["identifiers", "text", "import"], default="identifiers")
    args = parser.parse_args()
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    dataset = root / "input"
    make_demo(dataset, args.rows, "linked")
    project = Path(__file__).resolve().parents[1]
    config = load_config(project / "config/runtime.mock.yaml")
    config.update(dataset=str(dataset), memory_limit="512MB", data_scope="complete_export")
    config["llm"].update(responses=str(dataset / "mock_llm.yaml"), max_calls=30)
    config["mcp"].update(command=sys.executable, args=["-m", "ontology_r2.mock_mcp", "--documents", str(dataset / "mock_documents.yaml")])
    config["processing"].update(materialize_all_objects=args.rows <= args.record_cap, max_relation_records=args.record_cap)
    if args.kind == "text":
        responses = read_yaml(config["llm"]["responses"])
        for task in ("plan", "final_plan"):
            responses[task]["relations"][0].update(mode="text", source_column="description", evidence_ids=["schema:demo.records:description"])
        write_yaml(config["llm"]["responses"], responses)
    start = time.monotonic()
    if args.kind == "import":
        work = root / "import"
        work.mkdir()
        data = Dataset(dataset, work, "512MB")
        result = {"input_records": sum(t["rows"] for t in data.tables.values()), "llm_calls": 0, "stage": "import_and_profiles_only"}
        data.close()
    else:
        result = asyncio.run(build(config, root / "run"))
        result["extraction"] = read_yaml(root / "run/metrics.yaml").get("extraction") if (root / "run/metrics.yaml").exists() else None
    report = {"synthetic": True, "scenario": args.kind, "requested_records": args.rows, "record_cap": args.record_cap, "elapsed_seconds": round(time.monotonic() - start, 3), "process_peak_rss_raw": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, "rss_unit": "bytes" if sys.platform == "darwin" else "KiB", "platform": sys.platform, "result": result, "semantic_validation": "mock responses only; not a real LLM quality measurement"}
    write_yaml(root / "benchmark.yaml", report)
    print(json.dumps({k: v for k, v in report.items() if k != "result"} | {"status": result.get("status", "import_complete"), "llm": result.get("llm"), "extraction": result.get("extraction")}, ensure_ascii=False))
    if result.get("status") == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
