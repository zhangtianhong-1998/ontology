"""Bounded review preserves joins, provenance, and unresolved business meaning."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ontology_r2 import instance_bundles as packets
from ontology_r2.association_rules import _ordered_base_candidates, build_association_rules
from ontology_r2.discovery import propose_candidates, validate_candidate
from ontology_r2.row_bundles import joint_examples
from test_discovery import SmallDataset
from test_relation_rule_scheduling import rule


def test_generic_reference_comment_cannot_promote_every_numeric_target():
    data = SimpleNamespace(tables={
        "u.feed": {"columns": [{"column_name": "q", "column_comment": "关联引用目标的ID"}]},
        "u.catalog": {"columns": [{"column_name": "z"}], "pk": ["z"]}})
    candidate = rule("a", source="u.feed", target="u.catalog", field="q", target_field="z",
                     numeric=True, selector={"category": "A"})
    assert packets._rule_quality_tier(candidate, data) == "risk_exploration"
    data.tables["u.feed"]["columns"][0]["column_comment"] = "引用 u.catalog.z"
    assert packets._rule_quality_tier(candidate, data) == "structural_reference"
    other = deepcopy(candidate)
    other["target"]["table"] = "u.other"
    assert packets._rule_quality_tier(other, data) == "risk_exploration"
    assert candidate["semantic_relation"] == "unresolved"


@pytest.mark.parametrize("names", [("q", "z", "b"), ("renamed", "opaque_key", "opaque_kind")])
def test_checked_partial_branch_receives_priority_without_comments_or_name_hints(names):
    source, target, branch = names
    data = SmallDataset({
        "u.feed": ([source, branch], [("7", "甲"), ("7", "甲"), ("9", "甲"),
                                      ("8", "乙"), ("8", "乙")], []),
        "u.catalog": ([target], [("7",)], []),
    })
    candidate = {"candidate_id": "a", "source": {"table": "u.feed", "field": source},
                 "target": {"table": "u.catalog", "field": target}, "numeric_overlap_only": True,
                 "suggested_selector": {branch: "甲"}}
    try:
        check = validate_candidate(data, candidate, selector={branch: "甲"})
        built = asyncio.run(build_association_rules(data, {"candidates": [candidate], "checks": [check]}))
        result = built["rules"][0]
        assert result["status"] == "observed_subset"
        assert result["verification"]["checks"]["unique_matches"] == 2
        assert result["verification"]["checks"]["missing_in_input"] == 1
        assert packets._rule_quality_tier(result, data) == "structural_reference"
        assert result["numeric_overlap_only"] and result["semantic_relation"] == "unresolved"
    finally:
        data.close()


def test_rule_budget_covers_another_field_family_before_selector_variants():
    candidates = []
    for i in range(20):
        candidates.append({"candidate_id": f"a{i:02}",
            "source": {"table": "u.feed", "field": "q"},
            "target": {"table": "u.catalog", "field": "z"},
            "suggested_selector": {"bucket": str(i)}})
    other = {"candidate_id": "z", "source": {"table": "u.feed", "field": "other"},
             "target": {"table": "u.catalog", "field": "other"}}
    candidates.append(other)
    indexed = {c["candidate_id"]: c for c in candidates}
    ordered = _ordered_base_candidates(indexed, {})
    assert other in ordered[:2]
    assert len(ordered) == len(candidates)
    result = asyncio.run(build_association_rules(SimpleNamespace(snapshot_id="s"),
        {"candidates": candidates}, {"max_rules": 2}))
    assert {r["source"]["field"] for r in result["rules"]} == {"q", "other"}
    assert result["coverage"]["rule_variants_not_emitted"] == 19
    assert all(r["semantic_relation"] == "unresolved" for r in result["rules"])


def test_global_candidate_cap_interleaves_sources_before_field_pair_repetitions():
    data = SmallDataset({
        "u.a": (["alpha", "beta", "gamma"], [("K1", "K1", "K1")], []),
        "u.b": (["alpha", "beta", "gamma"], [("K1", "K1", "K1")], []),
    })
    try:
        found = propose_candidates(data, max_candidates_total=2)
        assert {candidate["source"]["table"] for candidate in found["candidates"]} == {"u.a", "u.b"}
        assert found["coverage"]["candidates_queued_global"] > 0
    finally:
        data.close()


def test_deduplicated_rule_keeps_each_origin_and_candidate_id():
    candidates = [{"candidate_id": name, "source": {"table": "u.a", "field": "x"},
                   "target": {"table": "u.b", "field": "y"}, "retrieval_channels": [name]}
                  for name in ("metadata", "value_overlap")]
    result = asyncio.run(build_association_rules(SimpleNamespace(snapshot_id="s"),
        {"candidates": candidates}))
    assert len(result["rules"]) == 1
    assert {source["candidate_id"] for source in result["rules"][0]["evidence_sources"]} == {
        "metadata", "value_overlap"}


def test_exact_evidence_dedup_precedes_risk_budget_and_retains_source_references(monkeypatch):
    first = rule("a", field="q", target_field="z", numeric=True)
    duplicate = {**deepcopy(first), "rule_id": "b", "candidate_id": "different-channel"}
    distinct = rule("c", field="other", target_field="other", numeric=True)
    original = deepcopy([first, duplicate, distinct])
    monkeypatch.setattr(packets, "_relation_bundle", lambda data, item, limits: (
        {"task_kind": "relation_meaning", "bundle_id": item["rule_id"], "rule": item}, None))
    index = SimpleNamespace(db=SimpleNamespace(execute=lambda sql: SimpleNamespace(fetchone=lambda: (0,))))
    result = packets.build_instance_bundles(SimpleNamespace(snapshot_id="s"), index,
        {"rules": [first, duplicate, distinct]}, {"max_concept_bundles": 0,
        "max_relation_bundles": 2, "max_exploration_rules": 2})
    assert {b["rule"]["rule_id"] for b in result["bundles"]} == {"a", "c"}
    canonical = next(b["rule"] for b in result["bundles"] if b["rule"]["rule_id"] == "a")
    assert canonical["equivalent_rule_evidence"][0]["candidate_id"] == "different-channel"
    assert result["coverage"]["novelty"]["relation_exact_duplicate_rules_skipped"] == 1
    assert result["coverage"]["skipped"][0]["source_artifact"] == "association_rules.yaml"
    assert [first, duplicate, distinct] == original


@pytest.mark.parametrize("field, value", [
    ("selector", {"kind": "B"}), ("scope_bindings", {"left": "right"}),
    ("transform", {"operator": "source_alias_items"}), ("snapshot_id", "new-snapshot")])
def test_equal_counts_do_not_coalesce_different_join_semantics(field, value):
    first = rule("a", selector={"kind": "A"})
    changed = {**deepcopy(first), field: value}
    assert packets._exact_relation_signature(first) != packets._exact_relation_signature(changed)


def test_full_composite_scope_keeps_many_to_one_partial_and_ambiguous_rows():
    data = SmallDataset({
        "u.feed": (["q", "p", "r", "b"], [
            ("7", "x", "north", "active"), ("7", "x", "north", "active"),
            ("7", "x", "south", "active"), ("8", "x", "north", "active"),
            ("9", "x", "north", "active"), ("7", None, "north", "active"),
            ("7", "x", "north", "other")], []),
        "u.catalog": (["z", "c", "d"], [
            ("7", "x", "north"), ("7", "x", "south"), ("7", "y", "north"),
            ("8", "x", "north"), ("8", "x", "north")], []),
    })
    candidate = {"candidate_id": "a", "source": {"table": "u.feed", "field": "q"},
                 "target": {"table": "u.catalog", "field": "z"},
                 "suggested_selector": {"b": "active"},
                 "suggested_scope_bindings": {"p": "c", "r": "d"}}
    try:
        check = validate_candidate(data, candidate, selector={"b": "active"},
                                   scope_bindings={"c": "p", "d": "r"})
        counts = check["checks"]
        assert {k: counts[k] for k in ("unique_matches", "ambiguous_matches", "missing_in_input",
            "missing_scope", "outside_matched_references")} == {
            "unique_matches": 3, "ambiguous_matches": 1, "missing_in_input": 1,
            "missing_scope": 1, "outside_matched_references": 1}
        built = asyncio.run(build_association_rules(data, {"candidates": [candidate], "checks": [check]}))
        assert built["rules"][0]["status"] == "observed_subset"
        assert built["rules"][0]["scope_bindings"] == {"p": "c", "r": "d"}
        assert built["rules"][0]["semantic_relation"] == "unresolved"
        data.record_id = lambda table, row: f"{table}:{row['__r2_row']}"
        examples = joint_examples(data, candidate, check, source_fields=("p", "r", "b"),
                                  target_fields=("c", "d"))
        assert [pair["source_record"]["row_number"] for pair in examples["matched_pairs"]] == [1, 3]
        assert examples["sampling"] == {
            "method": "distinct_observed_target_round_robin", "unique_target_witnesses": 2,
            "sampled_unique_target_witnesses": 2, "unique_target_witnesses_not_sampled": 0,
            "ambiguous_targets_sampled_as_positive": False}
        assert examples["counterexamples"][0]["reason"] == "ambiguous_target"
        for info in data.tables.values():
            info["columns"] = [{"column_name": name, "column_comment": ""}
                               for name in info["column_names"]]
        bundle, reason = packets._relation_bundle(data, built["rules"][0], packets.validate_bundle_options({}))
        assert reason is None
        for record in bundle["records"]:
            fields = {entry["column"] for entries in record["fields"].values() for entry in entries}
            assert fields >= ({"q", "p", "r", "b"} if record["table"] == "u.feed" else {"z", "c", "d"})
        assert bundle["technical_coverage"]["ambiguous_matches"] == 1
        assert bundle["limits"]["technical_match_is_business_relation"] is False
    finally:
        data.close()
