"""Repair retrieval shares a source class quote, never an exact type identity."""
from copy import deepcopy

from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from ontology_r2.storage import Dataset, qi
from ontology_r2.template_reuse import augment_template_repair_witnesses
from test_semantic_cards import _table


def fixture(tmp_path):
    root, work = tmp_path / "input", tmp_path / "work"
    quote = "数据接口是一类提供区域查询的服务资源"
    _table(root, "resource_definition", {"id": "记录编号", "name": "名称", "definition": "定义说明",
           "tenant": "定义租户"}, [
        {"id": str(number), "name": name, "definition": f"{name}用于具体区域。{quote}", "tenant": tenant}
        for number, name, tenant in ((1, "东区查询", "A"), (2, "西区查询", "A"),
                                     (3, "北区查询", "A"), (4, "南区查询", "B"), (5, "待修订查询", "A"))])
    work.mkdir()
    data = Dataset(root, work)
    table = next(iter(data.tables.values()))
    table["inferred_semantic_roles"] = [{"column": "tenant", "role": "identity",
                                         "status": "source_verified_role_candidate"}]
    built = build_semantic_cards(data, work / "cards.sqlite")
    index = SemanticCardIndex(built["index_path"])
    cards = index.all_cards(10)["cards"]
    seed = next(card for card in cards if card["row_number"] == 1)
    # Stale index text must not defeat the same-class check against source rows.
    data.db.execute(f"UPDATE {qi(table['sql_name'])} SET definition=? WHERE __r2_row=5", ["另一类定义"])
    template = {"template_id": "projection:resource", "source_table": seed["table"],
                "identity_qualifiers": {"tenant": "A"}, "applicability_scope": {}, "slots": [],
                "class_definition": {"quote": quote, "column": "definition"}}
    packet = {"bundle_id": "repair:resource", "task_kind": "concept_induction", "records": [seed],
              "exact_alignment_record_ids": [seed["record_id"]],
              "template_repair_task": {"scope": "template_field_gaps", "known_template_matches": [{
                  "status": "partial_match", "unresolved_fields": [{"column": "definition"}],
                  "protected_fields_changed": [], "accepted_template": template}]}}
    return data, index, packet


def test_repair_witnesses_keep_exact_quote_identity_and_source_freshness(tmp_path):
    data, index, packet = fixture(tmp_path)
    try:
        result = augment_template_repair_witnesses(data, index, [packet])
        enriched = result["bundles"][0]
        assert {record["row_number"] for record in enriched["records"]} == {1, 2, 3}
        assert len(packet["records"]) == 1  # Callers retain the original packet.
        assert enriched["exact_alignment_record_ids"] == packet["exact_alignment_record_ids"]
        assert all(record["context_role"] == "repair_witness" and record["identity_claim"] == "none"
                   for record in enriched["records"][1:])
        assert result["coverage"]["witnesses_added"] == 2
        assert result["coverage"]["llm_calls"] == result["coverage"]["exact_alignments_added"] == 0
    finally:
        index.close()
        data.close()


def test_witness_lookup_only_enriches_bounded_definition_or_field_gap_repairs(tmp_path):
    data, index, packet = fixture(tmp_path)
    try:
        no_gap = deepcopy(packet)
        no_gap["template_repair_task"]["scope"] = "template_slot_endpoints"
        result = augment_template_repair_witnesses(data, index, [no_gap])
        assert result["bundles"] == [no_gap]
        assert result["coverage"].get("bundles_queried", 0) == 0
        result = augment_template_repair_witnesses(data, index, [packet], max_bundle_bytes=1000)
        assert len(result["bundles"][0]["records"]) == 1
        assert result["coverage"]["witnesses_omitted_by_byte_limit"] >= 1
    finally:
        index.close()
        data.close()
