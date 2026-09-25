"""Program-only probe of row-purpose, joint-distinct and definition packet coverage."""

import argparse
import json
import time
from pathlib import Path

from ontology_r2.fact_observations import build_fact_observation_candidates
from ontology_r2.instance_bundles import build_instance_bundles
from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from ontology_r2.storage import Dataset, write_yaml


def run(dataset, output, *, max_cards=100000, max_concept_bundles=80,
        max_pattern_seed_pool=1000, seed_window_index=0):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "work").mkdir()
    start = time.monotonic()
    data = Dataset(Path(dataset), output / "work", "2GB")
    try:
        facts = build_fact_observation_candidates(data)
        cards = build_semantic_cards(
            data, output / "work" / "semantic_cards.sqlite", max_cards=max_cards)
        index = SemanticCardIndex(cards["index_path"])
        try:
            packets = build_instance_bundles(
                data, index, {"rules": []},
                {"max_concept_bundles": max_concept_bundles,
                 "max_relation_bundles": 0,
                 "max_pattern_seed_pool": max_pattern_seed_pool,
                 "seed_window_index": seed_window_index})
        finally:
            index.close()
        card_coverage, bundle_coverage = cards["coverage"], packets["coverage"]
        summary = {
            "input_scope": "imported_csv_snapshot; synthetic status is caller-supplied",
            "tables": len(data.tables),
            "rows": sum(table["rows"] for table in data.tables.values()),
            "definition_cards": bundle_coverage["definition_cards_indexed"],
            "definition_patterns": bundle_coverage["definition_patterns_indexed"],
            "concept_bundles": bundle_coverage["bundles_by_task"]["concept_induction"],
            "seed_window_index": seed_window_index,
            "seed_roots": bundle_coverage["seed_roots"],
            "definition_cards_not_seeded": bundle_coverage["definition_cards_not_seeded"],
            "definition_patterns_unprocessed": bundle_coverage["definition_patterns_unprocessed"],
            "business_fact_tables": facts["coverage"]["business_fact_tables"],
            "business_fact_candidates": facts["coverage"]["emitted_candidates"],
            "business_fact_instances_created": card_coverage["business_fact_instances_created"],
            "llm_calls": 0, "elapsed_seconds": round(time.monotonic() - start, 2),
            "semantic_quality": "unjudged; no ontology induction or relation judgment",
        }
        write_yaml(output / "semantic_card_coverage.yaml", card_coverage)
        write_yaml(output / "instance_bundle_coverage.yaml", bundle_coverage)
        write_yaml(output / "fact_observation_coverage.yaml", facts["coverage"])
        write_yaml(output / "fact_observation_candidates.yaml", facts)
        write_yaml(output / "summary.yaml", summary)
        return summary
    finally:
        data.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-cards", type=int, default=100000)
    parser.add_argument("--max-concept-bundles", type=int, default=80)
    parser.add_argument("--max-pattern-seed-pool", type=int, default=1000)
    parser.add_argument("--seed-window-index", type=int, default=0)
    args = parser.parse_args()
    print(json.dumps(run(args.dataset, args.output, max_cards=args.max_cards,
                         max_concept_bundles=args.max_concept_bundles,
                         max_pattern_seed_pool=args.max_pattern_seed_pool,
                         seed_window_index=args.seed_window_index), ensure_ascii=False))
