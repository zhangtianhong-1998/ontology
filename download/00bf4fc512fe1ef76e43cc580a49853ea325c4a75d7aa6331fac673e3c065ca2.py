"""Revalidate recorded role responses and count patterns without a model client.

This is a regression/cost preflight, not a fresh model evaluation. The source
snapshot must match the recorded requests; missing responses fail explicitly.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from ontology_r2.column_role_inference import infer_column_role_candidates
from ontology_r2.pipeline import load_config
from ontology_r2.progress import ProgressReporter
from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from ontology_r2.storage import Dataset, digest, write_yaml


class RecordedRoles:
    """Replay source-bound responses; intentionally has no provider or network."""

    def __init__(self, trace_path):
        self.responses = {}
        self.calls = 0
        pending = None
        for line in Path(trace_path).open(encoding="utf-8"):
            event = json.loads(line)
            if event.get("task") != "column_role_inference":
                continue
            if event.get("stage") == "llm_request":
                pending = event["input"]
            elif event.get("stage") == "llm_response" and pending:
                self.responses[(pending["source_snapshot"], pending["table"])] = event["result"]
                pending = None

    async def ask(self, task, packet, schema):
        if task != "column_role_inference":
            raise AssertionError("This preflight only replays column roles")
        self.calls += 1
        return schema.model_validate(self.responses[(packet["source_snapshot"], packet["table"])])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "work").mkdir()
    started = time.monotonic()
    progress = ProgressReporter(config.get("progress"))
    replay = RecordedRoles(args.trace)
    data = Dataset(config["dataset"], output / "work", config.get("memory_limit", "1GB"),
                   config.get("profiling"), progress=progress, privacy_config=config.get("privacy"))
    try:
        missing = sorted(table for table in data.tables
                         if (data.snapshot_id, table) not in replay.responses)
        if missing:
            raise ValueError("Missing recorded responses for the exact snapshot: " + ", ".join(missing))
        roles = asyncio.run(infer_column_role_candidates(data, replay, config.get("column_role_inference")))
        if any(table.get("error_type") for table in roles["tables"]):
            raise ValueError("Role replay failed; inspect responses against the new contract")
        data.column_role_report = roles
        write_yaml(output / "column_role_candidates.yaml", roles)
        options = config.get("instance_bundles", {})
        cards = build_semantic_cards(
            data, output / "work" / "semantic_cards.sqlite",
            max_cards=options.get("max_cards", 200000),
            max_field_chars=options.get("max_field_chars", 512),
            max_unknown_fields_per_table=options.get("max_unknown_fields_per_table", 4),
            progress=progress)
        write_yaml(output / "semantic_card_coverage.yaml", cards["coverage"])
        index = SemanticCardIndex(output / "work" / "semantic_cards.sqlite")
        try:
            counts = [dict(zip(("table", "kind", "cards", "patterns"), row)) for row in index.db.execute(
                "SELECT table_name, kind, count(*), count(DISTINCT pattern_id) "
                "FROM cards GROUP BY table_name, kind ORDER BY table_name, kind")]
        finally:
            index.close()
        patterns = sum(item["patterns"] for item in counts if item["kind"] == "definition")
        limit = min(options.get("max_concept_bundles", 0), options.get("max_pattern_seed_pool", 0))
        manifest = {
            "status": "offline_role_replay_complete", "llm_calls": 0,
            "recorded_responses_replayed": replay.calls, "source_snapshot": data.snapshot_id,
            "input_tables": len(data.tables), "input_records": sum(t["rows"] for t in data.tables.values()),
            "config_hash": digest(config), "definition_patterns": patterns,
            "concept_window_limit": limit, "patterns_outside_first_window": max(0, patterns - limit),
            "by_table_and_kind": counts, "elapsed_seconds": round(time.monotonic() - started, 3),
            "limits": ["Recorded responses, not fresh model predictions",
                       "Pattern count is a scheduling count, not a predicted LLM call count",
                       "Relations, semantic acceptance and template reuse are not measured here"],
        }
        write_yaml(output / "manifest.yaml", manifest)
        print(json.dumps({key: value for key, value in manifest.items()
                          if key != "by_table_and_kind"}, ensure_ascii=False, indent=2))
    finally:
        data.close()


if __name__ == "__main__":
    main()
