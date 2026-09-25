"""Retrieval packets expose candidates and coverage without asserting identity."""

import pytest
from types import SimpleNamespace

import ontology_r2.instance_bundles as packets_module
from ontology_r2.instance_bundles import _compatible, _relation_bundle, _round_robin_rules, _select_seeds, build_instance_bundles


def test_metric_and_measure_hints_may_be_compared_without_asserting_identity():
    metric = {"card_id": "m1", "root_hint": "Metric"}
    measure = {"card_id": "d1", "root_hint": "Measure"}
    dimension = {"card_id": "x1", "root_hint": "Dimension"}
    assert _compatible(metric, measure)
    assert _compatible(measure, metric)
    assert not _compatible(metric, dimension)
    assert not _compatible(metric, metric)
from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from ontology_r2.storage import Dataset
from test_semantic_cards import _dataset, _table


def test_semantic_pattern_pages_reference_variants_without_merging_identity(tmp_path):
    root = tmp_path / "input"
    _table(root, "fruit_metric_definition", {
        "id": "记录 ID", "metric_code": "指标编码", "metric_name": "指标名称",
        "definition": "指标定义",
    }, [
        {"id": "1", "metric_code": "M1", "metric_name": "经营利润", "definition": "收入减去成本"},
        {"id": "2", "metric_code": "M2", "metric_name": "经营利润", "definition": "收入减去成本"},
        {"id": "3", "metric_code": "M3", "metric_name": "经营利润", "definition": "收入减去成本"},
    ])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        assert built["coverage"]["cards_indexed"] == 3
        assert built["coverage"]["definition_patterns_indexed"] == 1
        index = SemanticCardIndex(built["index_path"])
        try:
            page = index.pattern_window(1)
            representative = page["patterns"][0]
            assert page["total_patterns"] == 1
            assert representative["pattern_card_count"] == 3
            assert representative["reference_variant_count"] == 3
            assert representative["pattern_nonrepresentative_cards"] == 2
            assert index.search("经营利润", kind="definition",
                                exclude_pattern_id=representative["pattern_id"]) == []
            assert index.db.execute("SELECT count(DISTINCT pattern_id) FROM cards").fetchone()[0] == 1
            assert index.db.execute("SELECT count(DISTINCT signature) FROM cards").fetchone()[0] == 3
            packets = build_instance_bundles(data, index, {"rules": []}, {
                "max_concept_bundles": 1, "max_relation_bundles": 0,
                "max_pattern_seed_pool": 1})
            bundle = packets["bundles"][0]
            assert bundle["input_mode"] == "single_definition"
            assert bundle["exact_alignment_record_ids"] == [representative["record_id"]]
            assert bundle["pattern"]["nonrepresentative_cards_candidate_only"] == 2
            assert packets["coverage"]["definition_cards_not_submitted_as_exact_candidates"] == 2
        finally:
            index.close()
    finally:
        data.close()


def test_truncated_semantic_previews_do_not_group_reference_variants(tmp_path):
    root = tmp_path / "input"
    _table(root, "fruit_metric_definition", {
        "id": "记录 ID", "metric_code": "指标编码", "metric_name": "指标名称",
        "definition": "指标定义",
    }, [
        {"id": "1", "metric_code": "M1", "metric_name": "经营利润", "definition": "收入减去经营成本的长期口径"},
        {"id": "2", "metric_code": "M2", "metric_name": "经营利润", "definition": "收入减去经营成本的长期口径"},
    ])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        built = build_semantic_cards(data, tmp_path / "cards.sqlite", max_field_chars=5)
        assert built["coverage"]["cards_indexed"] == 2
        assert built["coverage"]["definition_patterns_indexed"] == 2
    finally:
        data.close()


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


def test_seed_schedule_uses_full_population_not_equal_truncated_pool():
    roots = {"Metric": ("metric_main", 16000),
             "Measure": ("measure_main", 5000),
             "Dimension": ("dimension_main", 80),
             "GeneralObject": ("general_main", 3700)}
    pool = [{"card_id": f"{root}-{i}", "table": table, "root_hint": root,
             "fields": {"description": [{"value": "definition"}]}}
            for root, (table, _) in roots.items() for i in range(50)]
    # Every root has exactly 50 pool cards, but the indexed corpus is skewed.
    chosen = _select_seeds(pool, 80,
                           population_by_root={root: count for root, (_, count) in roots.items()},
                           population_by_table={table: count for table, count in roots.values()})
    counts = {root: sum(item["root_hint"] == root for item in chosen) for root in roots}
    assert counts["Metric"] > counts["Measure"] > counts["Dimension"] >= 1
    assert counts["GeneralObject"] > counts["Dimension"]
    assert sum(counts.values()) == 80


def test_seed_schedule_gives_each_available_table_a_floor():
    pool = [{"card_id": f"{table}-{i}", "table": table, "root_hint": root,
             "fields": {}}
            for root, table in (("Metric", "metric_large"), ("Metric", "metric_small"),
                                ("Measure", "measure"), ("Dimension", "dimension"))
            for i in range(8)]
    chosen = _select_seeds(pool, 4,
                           population_by_root={"Metric": 10010, "Measure": 5000, "Dimension": 80},
                           population_by_table={"metric_large": 10000, "metric_small": 10,
                                                "measure": 5000, "dimension": 80})
    assert {item["table"] for item in chosen} == {
        "metric_large", "metric_small", "measure", "dimension"}


def test_seed_windows_reach_later_definitions_without_repeating_cards(tmp_path):
    data = _dataset(tmp_path)
    try:
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            first = index.pattern_window(1, kind="definition", window_index=0)["patterns"]
            second = index.pattern_window(1, kind="definition", window_index=1)["patterns"]
            assert len(first) == len(second) == 1
            assert {item["card_id"] for item in first}.isdisjoint(
                {item["card_id"] for item in second})
            packets = build_instance_bundles(data, index, {"rules": []}, {
                "max_concept_bundles": 1, "max_relation_bundles": 0,
                "max_pattern_seed_pool": 1, "seed_window_index": 1,
            })
            assert packets["coverage"]["seed_window_index"] == 1
            assert packets["coverage"]["definition_cards_before_requested_window"] == 1
            assert packets["coverage"]["seed_windows_to_visit_indexed_cards"] == 2
            assert packets["coverage"]["definition_patterns_before_requested_window"] == 1
            assert packets["bundles"][0]["seed_ids"] == [second[0]["card_id"]]
            with pytest.raises(ValueError, match="window_index"):
                index.pattern_window(1, kind="definition", window_index=-1)
        finally:
            index.close()
    finally:
        data.close()


def test_oversized_requested_seed_pool_cannot_strand_patterns_in_one_page(tmp_path):
    data = _dataset(tmp_path)
    try:
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            results = [build_instance_bundles(data, index, {"rules": []}, {
                "max_concept_bundles": 1, "max_relation_bundles": 0,
                "max_pattern_seed_pool": 1000, "seed_window_index": page,
            }) for page in (0, 1)]
            assert [result["coverage"]["seed_window_pattern_limit"] for result in results] == [1, 1]
            assert all(result["coverage"]["seed_window_pattern_limit_requested"] == 1000
                       for result in results)
            assert all(result["coverage"]["seed_window_limited_by_concept_budget"]
                       for result in results)
            assert all(result["coverage"]["seed_windows_to_visit_indexed_cards"] == 2
                       for result in results)
            assert [result["coverage"]["next_seed_window_index"] for result in results] == [1, None]
            assert len(results[0]["bundles"]) == len(results[1]["bundles"]) == 1
            assert {results[0]["bundles"][0]["seed_ids"][0]} != {
                results[1]["bundles"][0]["seed_ids"][0]}
        finally:
            index.close()
    finally:
        data.close()


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
