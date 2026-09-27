"""Formula contracts bind complete accepted definitions, never substring hits."""
from types import SimpleNamespace

from ontology_r2.calculation_contracts import enrich_calculation_contracts, parse_calculation
from ontology_r2.models import BuildPlan, DerivedType


def _case(formula="利润 = 收入 - 成本", *, duplicate=False, scope=None):
    evidence, types, concepts, alignments = {}, [], [], []
    definitions = [("profit", "利润", formula), ("revenue", "收入", None), ("cost", "成本", None)]
    if duplicate:
        definitions.append(("gross-revenue", "收入", None))
    for key, name, expression in definitions:
        record_id = "record:" + key
        props = []
        for role, value in (("name", name), ("formula", expression)):
            if value is None:
                continue
            evidence_id = key + ":" + role
            evidence[evidence_id] = {"origin": "observed_record", "raw_fragment": value,
                "raw_fragment_truncated": False, "source_ref": {
                    "snapshot_id": "snap", "record_id": record_id, "table": "definitions", "column": role}}
            props.append({"role": role, "source_table": "definitions", "source_column": role,
                          "evidence_ids": [evidence_id]})
        types.append(DerivedType(id=key, parent="Metric" if key == "profit" else "Measure",
            label=name, definition=name, evidence_ids=[key + ":name"], category="business_type",
            applicability_scope=(scope or {}) if key == "revenue" else {}, source_properties=props))
        concepts.append({"id": "concept:" + key, "ontology_type_id": key})
        alignments.append({"mapping_kind": "exact", "source_record_id": record_id, "concept_id": "concept:" + key})
    return SimpleNamespace(snapshot_id="snap", evidence=evidence, tables={}), BuildPlan(object_types=types), {
        "concepts": concepts, "record_alignments": alignments}


def test_profit_expression_binds_operand_types_and_retains_roles():
    result = enrich_calculation_contracts(*_case())
    calc = result["calculations"][0]
    assert calc["status"] == "accepted"
    assert calc["expression_tree"]["operator"] == "subtract"
    assert {(item["target_type_id"], item["operand_role"]) for item in result["dependencies"]} == {
        ("revenue", "minuend"), ("cost", "subtrahend")}
    assert result["coverage"]["llm_calls"] == 0
    assert result["coverage"]["business_observation_rows_read"] == 0


def test_ambiguous_names_narrower_scope_and_natural_prose_remain_unresolved():
    for args in (_case(duplicate=True), _case(scope={"region": "北京"}),
                 _case("利润并不依赖收入与成本"), _case("利润 = 收入总额 - 成本")):
        result = enrich_calculation_contracts(*args)
        assert result["calculations"][0]["status"] == "unresolved"
        assert result["dependencies"] == []
    assert enrich_calculation_contracts(*_case("利润用于说明经营结果"))["calculations"][0]["formula_type"] == "natural_language"


def test_no_eval_unsafe_functions_and_function_name_not_bound_as_operand():
    assert parse_calculation("open('file').read()")["status"] == "unresolved"
    result = parse_calculation("同比 = (本年出口 - 去年出口) / 去年出口 * 100%")
    assert result["status"] == "parsed"
    assert {item["symbol"] for item in result["symbols"]} == {"本年出口", "去年出口"}
    assert {item["symbol"] for item in parse_calculation("sum(收入)")["symbols"]} == {"收入"}


def test_same_name_alias_comes_only_from_exact_source_and_source_snapshot():
    data, plan, group = _case("利润 = revenue - 成本")
    data.evidence["alias-revenue"] = {"origin": "observed_record", "raw_fragment": '["revenue", "sales"]',
        "source_ref": {"snapshot_id": "snap", "record_id": "record:revenue",
                       "table": "definitions", "column": "aliases"}}
    from ontology_r2.models import SourceProperty
    plan.object_types[1].source_properties.append(SourceProperty(
        role="alias", source_table="definitions", source_column="aliases", evidence_ids=["alias-revenue"]))
    assert enrich_calculation_contracts(data, plan, group)["calculations"][0]["status"] == "accepted"
    data.evidence["alias-revenue"]["source_ref"]["snapshot_id"] = "old"
    assert enrich_calculation_contracts(data, plan, group)["calculations"][0]["status"] == "unresolved"


def test_self_reference_and_conflicting_formula_variants_are_not_accepted():
    data, plan, group = _case("利润 = 利润 + 收入")
    assert enrich_calculation_contracts(data, plan, group)["calculations"][0]["status"] == "unresolved"
    data, plan, group = _case()
    data.evidence["alternative"] = {**data.evidence["profit:formula"], "raw_fragment": "利润 = 收入 + 成本"}
    plan.object_types[0].source_properties[1].evidence_ids.append("alternative")
    result = enrich_calculation_contracts(data, plan, group)
    assert len(result["calculations"]) == 2
    assert all(item["reason"] == "conflicting_source_formula_variants" for item in result["calculations"])
    assert result["dependencies"] == []
