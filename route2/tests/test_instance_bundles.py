"""Retrieval packets expose candidates and coverage without asserting identity."""

import pytest
from types import SimpleNamespace

import ontology_r2.instance_bundles as packets_module
from ontology_r2.instance_bundles import _relation_bundle, _round_robin_rules, _select_seeds, build_instance_bundles
from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from test_semantic_cards import _dataset


def test_checked_business_keys_precede_numeric_only_collisions():
    rules = [
        {"rule_id": "a", "status": "checked_technical", "numeric_overlap_only": True,
         "source": {"table": "a"}},
        {"rule_id": "b", "status": "checked_technical", "numeric_overlap_only": False,
         "source": {"table": "b"}},
    ]
    assert [item["rule_id"] for item in _round_robin_rules(rules, 1)] == ["b"]


def test_seed_budget_spans_business_roots_before_repeating_one_table():
    pool = [{"card_id": f"card-{index}", "table": f"table-{root}",
             "root_hint": root, "fields": {"description": [{"value": "定义"}]}}
            for index, root in enumerate(("GeneralObject", "Metric", "Measure",
                                          "Dimension", "Term"))]
    pool.append({"card_id": "another-metric", "table": "table-Metric",
                 "root_hint": "Metric", "fields": {}})
    chosen = _select_seeds(pool, 5)
    assert {item["root_hint"] for item in chosen} == {
        "GeneralObject", "Metric", "Measure", "Dimension", "Term"}


def test_shared_metric_definition_key_is_not_compiled_as_business_relation():
    tables = ("fruit.metric_attr", "fruit.metric_detail", "fruit.metric_anchor")
    data = SimpleNamespace(tables={name: {"table_name": name, "table_comment": "指标定义"}
                                   for name in tables})
    for source in ("fruit.metric_attr", "fruit.metric_anchor"):
        rule = {"status": "checked_technical", "transform": {"operator": "identity"},
                "source": {"table": source, "field": "metric_code"},
                "target": {"table": "fruit.metric_detail", "field": "metric_code"}}
        assert _relation_bundle(data, rule, {}) == (None, "same_concept_key_requires_alignment")


def test_skipped_alignment_lead_does_not_consume_relation_packet_slot(monkeypatch):
    rules = [
        {"rule_id": "a", "status": "checked_technical", "numeric_overlap_only": False,
         "source": {"table": "fruit.metric", "field": "metric_code"},
         "target": {"table": "fruit.metric_common", "field": "metric_code"}},
        {"rule_id": "b", "status": "checked_technical", "numeric_overlap_only": False,
         "source": {"table": "fruit.metric", "field": "measure_code"},
         "target": {"table": "fruit.measure", "field": "measure_code"}},
    ]
    monkeypatch.setattr(packets_module, "_relation_bundle", lambda data, rule, limits: (
        (None, "same_concept_key_requires_alignment") if rule["rule_id"] == "a"
        else ({"task_kind": "relation_meaning", "bundle_id": "b"}, None)))
    index = SimpleNamespace(db=SimpleNamespace(execute=lambda sql: SimpleNamespace(
        fetchone=lambda: (0,))))
    result = build_instance_bundles(SimpleNamespace(snapshot_id="snapshot"), index,
                                    {"rules": rules}, {"max_concept_bundles": 0,
                                                       "max_relation_bundles": 1})
    assert result["coverage"]["rules_selected"] == 2
    assert result["coverage"]["bundles_by_task"]["relation_meaning"] == 1
    assert result["coverage"]["skipped"][0]["reason"] == "same_concept_key_requires_alignment"


def test_lexical_packets_keep_conflicting_scopes_as_unjudged_candidates(tmp_path):
    data = _dataset(tmp_path)
    try:
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            result = build_instance_bundles(data, index, {"rules": []}, {
                "max_concept_bundles": 2, "max_relation_bundles": 0,
                "max_seeds_per_table": 2, "max_candidates_per_bundle": 1,
            })
            packets = result["bundles"]
            assert len(packets) == 2
            assert all(packet["task_kind"] == "concept_induction" for packet in packets)
            assert all(packet["examples"]["validated_edges"] == [] for packet in packets)
            assert all(packet["retrieval"][0]["candidate_status"] == "unjudged"
                       for packet in packets)
            assert all({record["scope"]["scope"] for record in packet["records"]}
                       == {"华东", "华南"} for packet in packets)
            assert all(packet["size"]["bundle_bytes"] <= packet["size"]["limit_bytes"]
                       for packet in packets)
            assert result["coverage"]["definition_cards_not_seeded"] == 0
            assert result["coverage"]["vector"]["status"] == "disabled"
        finally:
            index.close()
    finally:
        data.close()


def test_vector_unavailable_and_card_budget_are_reported(tmp_path):
    data = _dataset(tmp_path)
    try:
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            result = build_instance_bundles(data, index, {"rules": []}, {
                "max_concept_bundles": 1, "max_relation_bundles": 0,
                "vector_enabled": True, "max_vector_cards": 1,
            })
            assert result["coverage"]["vector"]["status"] == "model_unavailable"
            assert result["coverage"]["partial"] is True
            assert result["coverage"]["definition_cards_not_seeded"] == 1
        finally:
            index.close()
    finally:
        data.close()


def test_bounded_vector_and_bm25_are_fused_without_accepting_identity(tmp_path):
    np = pytest.importorskip("numpy")

    class FixedEmbedding:
        model_sha256 = "fixed-test-vector"

        def documents(self, texts):
            return np.asarray([[1.0, 0.0] for _ in texts], dtype=np.float32)

        def query(self, text):
            return np.asarray([1.0, 0.0], dtype=np.float32)

    data = _dataset(tmp_path)
    try:
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            result = build_instance_bundles(data, index, {"rules": []}, {
                "max_concept_bundles": 1, "max_relation_bundles": 0,
                "max_vector_cards": 2, "vector_enabled": True,
            }, embedding=FixedEmbedding())
            assert result["coverage"]["vector"]["status"] == "complete"
            assert result["coverage"]["vector"]["cards_encoded"] == 2
            assert "vector" in result["bundles"][0]["retrieval"][0]["channels"]
            assert result["bundles"][0]["retrieval"][0]["candidate_status"] == "unjudged"
        finally:
            index.close()
    finally:
        data.close()
