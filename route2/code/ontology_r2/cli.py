import argparse
import asyncio
import json
from pathlib import Path

from .pipeline import build, load_config


def main():
    parser = argparse.ArgumentParser(description="Route 2 YAML ontology prototype")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("build")
    run.add_argument("--config", required=True)
    run.add_argument("--output", required=True, help="A new directory, never overwritten")
    run.add_argument("--dataset")
    run.add_argument("--llm-mode", choices=["mock", "agentscope"])
    run.add_argument("--profile", choices=["E2L", "E2O", "E2K", "E2F"], help="Explicit knowledge-source switches")
    demo = sub.add_parser("make-demo")
    demo.add_argument("--output", required=True)
    demo.add_argument("--rows", type=int, default=8)
    demo.add_argument("--scenario", choices=["linked", "unrelated", "formula"], default="linked")
    view = sub.add_parser("visualize", help="Generate a local result viewer")
    view.add_argument("--run", required=True)
    view.add_argument("--max-nodes", type=int, default=200)
    export = sub.add_parser("export-datahub", help="Export local technical metadata as a DataHub metadata file")
    export.add_argument("--run", required=True, help="Existing build output containing meta_graph.yaml")
    export.add_argument("--output", required=True, help="Destination JSON file")
    export.add_argument("--platform", default="postgres", help="DataHub platform key")
    export.add_argument("--environment", default="PROD")
    args = parser.parse_args()
    if args.command == "export-datahub":
        from .datahub_adapter import export_datahub_metadata
        from .storage import read_yaml
        graph = read_yaml(Path(args.run) / "meta_graph.yaml")
        print(json.dumps(export_datahub_metadata(graph, args.output, args.platform, args.environment), ensure_ascii=False))
        return
    if args.command == "visualize":
        from .visualization import render_viewer
        print(render_viewer(args.run, args.max_nodes))
        return
    if args.command == "make-demo":
        from .demo import make_demo
        make_demo(Path(args.output), args.rows, args.scenario)
        print(json.dumps({"dataset": str(Path(args.output).resolve()), "synthetic": True}, ensure_ascii=False))
        return
    config = load_config(args.config)
    if args.dataset:
        config["dataset"] = str(Path(args.dataset).resolve())
    if args.llm_mode:
        config["llm"]["mode"] = args.llm_mode
    if args.profile:
        config["experiment_profile"] = args.profile
        config.setdefault("mcp", {})["enabled"] = args.profile in ("E2K", "E2F")
        config.setdefault("external", {})["enabled"] = args.profile in ("E2O", "E2F")
    result = asyncio.run(build(config, args.output))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
