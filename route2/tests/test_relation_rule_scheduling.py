"""Packet budgets cover distinct joins before enumerating selector variants."""
from copy import deepcopy
from random import Random
from types import SimpleNamespace

import pytest

import ontology_r2.instance_bundles as packets


def rule(identifier, *, source="s.a", target="s.b", field="label", target_field="alias",
         selector=None, numeric=False, transform="identity", scope=None):
    conditional = bool(selector)
    return {"rule_id": identifier, "candidate_id": identifier,
            "status": "checked_technical", "semantic_relation": "unresolved",
            "source": {"table": source, "field": field},
            "target": {"table": target, "field": target_field},
            "selector": selector or {}, "scope_bindings": scope or {},
            "transform": {"operator": transform}, "numeric_overlap_only": numeric,
            "risk_flags": ["numeric_value_coincidence"] if numeric else [],
            "verification": {"scan_scope": "full_input", "checks": {
                "source_rows": 30, "selector_true": 10 if conditional else 30,
                "selector_false": 20 if conditional else 0, "selector_unknown": 0,
                "eligible_references": 10 if conditional else 30,
                "unique_matches": 10 if conditional else 30,
                "target_rows_with_complete_key": 10, "target_max_multiplicity": 1,
                "ambiguous_matches": 0, "missing_in_input": 0}}}


def test_selector_enumeration_cannot_displace_a_different_numeric_field_pair():
    variants = [rule(f"v{i:03}", selector={"category": str(i)}) for i in range(100)]
    numeric = rule("numeric", field="v17", target_field="x2", numeric=True,
                   selector={"kind": "接口"})
    original = deepcopy([*variants, numeric])
    chosen = packets._round_robin_rules([*variants, numeric], 2)
    assert len(chosen) == 2
    assert {item["source"]["field"] for item in chosen} == {"label", "v17"}
    assert chosen[1] is numeric
    assert chosen[1]["risk_flags"] == ["numeric_value_coincidence"]
    assert chosen[1]["semantic_relation"] == "unresolved"
    assert [*variants, numeric] == original


def test_target_table_and_source_table_receive_slots_before_repetition():
    dominant = [rule(f"a{i:03}", field=f"field_{i}", target="s.frequent") for i in range(50)]
    other_target = rule("z", target="s.rare")
    other_source = rule("zz", source="s.other", target="s.frequent")
    chosen = packets._round_robin_rules([*dominant, other_target, other_source], 3)
    assert len({item["source"]["table"] for item in chosen[:2]}) == 2
    assert {(item["source"]["table"], item["target"]["table"]) for item in chosen} == {
        ("s.a", "s.frequent"), ("s.a", "s.rare"), ("s.other", "s.frequent")}


def test_scope_or_transform_change_is_preserved_as_a_separate_join_family():
    variants = [rule(f"a{i:03}", selector={"category": str(i)}) for i in range(20)]
    normalized = rule("b", transform="strip_casefold")
    scoped = rule("c", scope={"tenant": "organization"})
    chosen = packets._round_robin_rules([*variants, normalized, scoped], 3)
    assert {item["rule_id"] for item in chosen} >= {"b", "c"}
    assert len(chosen) == 3


@pytest.mark.parametrize("value", ["接口", "CARD", 17])
def test_condition_priority_uses_exercised_branch_not_discriminator_spelling(value):
    conditional = rule("conditional", field="v17", target_field="x2",
                       selector={"opaque": value}, numeric=True)
    whole_column = rule("whole", field="v17", target_field="x2", numeric=True)
    assert packets._round_robin_rules([whole_column, conditional], 1) == [conditional]
    unobserved = deepcopy(conditional)
    unobserved["verification"]["checks"]["selector_false"] = 0
    assert packets._reference_priority(unobserved) == packets._reference_priority(whole_column)


def test_role_names_do_not_change_verification_priority():
    opaque = rule("opaque", field="x", target_field="y", target="s.z")
    advertised = deepcopy(opaque)
    advertised["source"]["field"] = "metric_code"
    advertised["target"] = {"table": "s.metric_dimension_measure", "field": "dimension_id"}
    assert packets._reference_priority(advertised) == packets._reference_priority(opaque)


def test_order_is_deterministic_complete_and_excludes_unverified_rules():
    rules = [rule(str(i), source=f"s.source{i % 3}", target=f"s.target{i % 4}",
                  selector={"类型": str(i % 7)}, numeric=i % 2 == 0) for i in range(80)]
    unresolved = rule("unresolved")
    unresolved["status"] = "unresolved"
    rules.append(unresolved)
    expected = packets._round_robin_rules(rules, 1000)
    Random(7).shuffle(rules)
    assert packets._round_robin_rules(rules, 1000) == expected
    assert {item["rule_id"] for item in expected} == {str(i) for i in range(80)}
    assert packets._round_robin_rules(rules, 0) == []


def test_packet_coverage_keeps_unexamined_variants_and_numeric_risk_visible(monkeypatch):
    rules = [rule(f"v{i:03}", selector={"category": str(i)}) for i in range(20)]
    rules.append(rule("numeric", field="v17", target_field="x2", numeric=True))
    monkeypatch.setattr(packets, "_relation_bundle", lambda data, item, limits: (
        {"task_kind": "relation_meaning", "bundle_id": item["rule_id"], "rule": item}, None))
    index = SimpleNamespace(db=SimpleNamespace(execute=lambda sql: SimpleNamespace(fetchone=lambda: (0,))))
    result = packets.build_instance_bundles(SimpleNamespace(snapshot_id="snapshot"), index,
        {"rules": rules}, {"max_concept_bundles": 0, "max_relation_bundles": 2})
    coverage = result["coverage"]["rule_selection"]
    assert coverage["eligible_rules"] == 21
    assert coverage["inspected_rules"] == coverage["packaged_rules"] == 2
    assert coverage["uninspected_rules"] == 19
    assert coverage["families_available"] == coverage["families_packaged"] == 2
    assert coverage["families_not_inspected"] == coverage["families_not_packaged"] == 0
    assert coverage["numeric_overlap_rules"] == {"available": 1, "inspected": 1, "packaged": 1}
    assert coverage["semantic_acceptance"] == "not_assessed_by_packet_selection"
    assert result["coverage"]["partial"] is True


def test_structural_numeric_reference_does_not_spend_collision_exploration_budget():
    data = SimpleNamespace(tables={
        "s.source": {"pk": ["record"], "columns": [
            {"column_name": "opaque", "column_comment": "引用目标对象，按类别解析目标表"},
            {"column_name": "record", "column_comment": "主键"},
            {"column_name": "position", "column_comment": "显示排序序号"}]},
        "s.target": {"pk": ["pk"], "columns": [{"column_name": "pk"}]},
    })
    reference = rule("reference", source="s.source", target="s.target", field="opaque",
                     target_field="pk", selector={"kind": 23}, numeric=True)
    collision = rule("collision", source="s.source", target="s.target", field="record",
                     target_field="pk", selector={"kind": 23}, numeric=True)
    ordinal = rule("ordinal", source="s.source", target="s.target", field="position",
                   target_field="pk", numeric=True)
    assert packets._rule_quality_tier(reference, data) == "structural_reference"
    assert packets._rule_quality_tier(collision, data) == "risk_exploration"
    assert packets._rule_quality_tier(ordinal, data) == "risk_exploration"
    assert packets._round_robin_rules([collision, ordinal, reference], 100, data=data,
                                     max_exploration_rules=0) == [reference]
    assert reference["numeric_overlap_only"] is True
    assert reference["semantic_relation"] == "unresolved"


def test_unknown_numeric_rules_receive_bounded_exploration_without_metadata():
    candidates = [rule(str(i), field=f"v{i}", target_field=f"x{i}", numeric=True)
                  for i in range(100)]
    chosen = packets._round_robin_rules(candidates, 100, max_exploration_rules=3)
    assert len(chosen) == 3
    assert all(item["numeric_overlap_only"] for item in chosen)
    assert packets._round_robin_rules(candidates, 100, max_exploration_rules=0) == []


@pytest.mark.parametrize("invalid", [-1, True, 1.5, "2"])
def test_exploration_budget_is_a_nonnegative_integer(invalid):
    with pytest.raises(ValueError, match="nonnegative integers"):
        packets.validate_bundle_options({"max_exploration_rules": invalid})


def test_default_exploration_allowance_uses_packet_capacity():
    assert packets.validate_bundle_options({"max_relation_bundles": 7})["max_exploration_rules"] == 1
    assert packets.validate_bundle_options({"max_relation_bundles": 0})["max_exploration_rules"] == 0
    assert packets.validate_bundle_options({"max_relation_bundles": 7,
                                            "max_exploration_rules": 0})["max_exploration_rules"] == 0


def test_exploration_cutoff_is_reported_even_when_packet_capacity_remains(monkeypatch):
    candidates = [rule(str(i), field=f"v{i}", target_field=f"x{i}", numeric=True)
                  for i in range(20)]
    monkeypatch.setattr(packets, "_relation_bundle", lambda data, item, limits: (
        {"task_kind": "relation_meaning", "bundle_id": item["rule_id"], "rule": item}, None))
    index = SimpleNamespace(db=SimpleNamespace(execute=lambda sql: SimpleNamespace(fetchone=lambda: (0,))))
    result = packets.build_instance_bundles(SimpleNamespace(snapshot_id="snapshot"), index,
        {"rules": candidates}, {"max_concept_bundles": 0, "max_relation_bundles": 7})
    coverage = result["coverage"]["rule_selection"]
    assert coverage["max_exploration_rules"] == 1
    assert coverage["by_stratum"]["risk_exploration"] == {
        "eligible": 20, "explored": 1, "packaged": 1, "deferred": 19}
    assert coverage["families_not_inspected"] == coverage["families_not_packaged"] == 19
    assert result["coverage"]["checked_rules_not_selected"] == 19
    assert result["coverage"]["partial"] is True


def test_failed_risk_packet_does_not_silently_expand_exploration(monkeypatch):
    candidates = [rule(str(i), field=f"v{i}", numeric=True) for i in range(5)]
    monkeypatch.setattr(packets, "_relation_bundle", lambda *_: (None, "relation_over_budget"))
    index = SimpleNamespace(db=SimpleNamespace(execute=lambda sql: SimpleNamespace(fetchone=lambda: (0,))))
    result = packets.build_instance_bundles(SimpleNamespace(snapshot_id="snapshot"), index,
        {"rules": candidates}, {"max_concept_bundles": 0, "max_relation_bundles": 7,
                               "max_exploration_rules": 1})
    coverage = result["coverage"]["rule_selection"]
    assert coverage["by_stratum"]["risk_exploration"] == {
        "eligible": 5, "explored": 1, "packaged": 0, "deferred": 4}
    assert result["coverage"]["skipped"][0]["reason"] == "relation_over_budget"
