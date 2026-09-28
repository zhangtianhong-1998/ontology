"""Formula contracts bind complete accepted definitions, never substring hits."""
from types import SimpleNamespace
import pytest

from ontology_r2.calculation_contracts import (calculation_relation_errors,
    enrich_calculation_contracts, parse_calculation)
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


def _profit_revenue_relation():
    return DerivedType(id='calc:profit-revenue', parent='depends_on',
        predicate_name='calculation_dependency', label='calculation_dependency',
        definition='利润的被减数是收入', domain=['profit'], range=['revenue'],
        semantic_parameters={'operand_role': 'minuend'},
        evidence_scope='complete_calculation_definition',
        evidence_ids=['profit:formula', 'revenue:name'])


@pytest.mark.parametrize('source_parameters', [{}, {'period_scope': '公历月'}])
def test_calculation_rejects_missing_or_different_target_definition_parameters(source_parameters):
    data, plan, group = _case()
    plan.object_types[0].definition_parameters = source_parameters
    plan.object_types[1].definition_parameters = {'period_scope': '公历年'}
    result = enrich_calculation_contracts(data, plan, group)
    calc = result['calculations'][0]
    assert calc['status'] == 'unresolved'
    assert result['dependencies'] == []
    binding = next(b for b in calc['bindings'] if b['symbol'] == '收入')
    assert binding['reason'] == 'definition_parameters_not_proven'
    assert calculation_relation_errors(data, _profit_revenue_relation(), plan.object_types) == [
        'calculation relation target definition parameters are not proved']


@pytest.mark.parametrize('target_parameters', [{}, {'period_scope': '公历年'}])
def test_calculation_accepts_supported_parameters_and_unparameterized_measure(target_parameters):
    data, plan, group = _case()
    plan.object_types[0].definition_parameters = {'period_scope': '公历年'}
    plan.object_types[1].definition_parameters = target_parameters
    assert enrich_calculation_contracts(data, plan, group)['calculations'][0]['status'] == 'accepted'
    assert calculation_relation_errors(data, _profit_revenue_relation(), plan.object_types) == []


def test_calculation_parameter_guard_applies_to_every_target_parameter():
    data, plan, group = _case()
    plan.object_types[0].definition_parameters = {'period_scope': '公历年'}
    plan.object_types[1].definition_parameters = {'period_scope': '公历年', 'aggregation': '预算'}
    assert enrich_calculation_contracts(data, plan, group)['calculations'][0]['status'] == 'unresolved'
    assert calculation_relation_errors(data, _profit_revenue_relation(), plan.object_types)


def test_ambiguity_uses_the_same_parameter_guard_as_binding_and_replay():
    data, plan, group = _case(duplicate=True)
    plan.object_types[0].definition_parameters = {'period_scope': '公历月'}
    plan.object_types[1].definition_parameters = {'period_scope': '公历月'}
    plan.object_types[3].definition_parameters = {'period_scope': '公历年'}
    result = enrich_calculation_contracts(data, plan, group)
    assert result['calculations'][0]['status'] == 'accepted'
    assert calculation_relation_errors(data, _profit_revenue_relation(), plan.object_types) == []
    plan.object_types[3].definition_parameters = {'period_scope': '公历月'}
    assert enrich_calculation_contracts(data, plan, group)['calculations'][0]['status'] == 'unresolved'
    assert calculation_relation_errors(data, _profit_revenue_relation(), plan.object_types) == [
        'calculation relation symbol has ambiguous accepted definitions']


def test_missing_operands_schedule_targeted_definition_tasks_without_creating_relations():
    data, plan, group = _case("利润 = (新收入 - 成本) / 新收入")
    class Index:
        coverage = {"snapshot_id": "snap"}
        def search(self, query, **options):
            assert query == "新收入" and options["kind"] == "definition"
            return [{"card_id": "possible", "name": "收入", "retrieval_channels": ["bm25"]}]
        # This fixture has no declared keys to look up; test query scoping below.
    from ontology_r2.calculation_contracts import _formula_binding_tasks
    result = enrich_calculation_contracts(data, plan, group)
    tasks = _formula_binding_tasks(result["calculations"], Index())
    assert len(tasks) == 1 and len(tasks[0]["occurrences"]) == 2
    assert tasks[0]["definition_candidates"][0]["card_id"] == "possible"
    assert tasks[0]["automatic_relation_created"] is False
    assert result["dependencies"] == []


def test_projected_components_bind_formulas_without_claiming_whole_record_identity():
    data, plan, group = _case()
    for item in plan.object_types:
        item.derivation_kind = "template_projection"
    group["record_alignments"] = []
    assert enrich_calculation_contracts(data, plan, group)["calculations"][0]["status"] == "accepted"
    group["template_projections"] = [{"object_type_id": "revenue", "field_templates": [
        {"column": "name", "template": "{region}收入"}]}]
    assert enrich_calculation_contracts(data, plan, group)["calculations"][0]["status"] == "unresolved"


def test_declared_formula_variable_alias_works_without_primary_key_on_alias(tmp_path):
    from ontology_r2.storage import Dataset, digest
    from ontology_r2.semantic_cards import build_semantic_cards, SemanticCardIndex
    from ontology_r2.calculation_stage import compile_calculation_stage
    from test_semantic_cards import _table
    from test_semantic_bindings import PROFILE
    root = tmp_path / "input"
    _table(root, "amount_definition", {"id": "记录ID", "name": "度量名称", "definition": "度量定义",
           "parameter_name": "英文别名，公式变量"}, [
        {"id": "1", "name": "收入", "definition": "通用收入量", "parameter_name": "revenue"},
        {"id": "2", "name": "成本", "definition": "通用成本量", "parameter_name": "cost"}])
    _table(root, "metric_definition", {"id": "记录ID", "name": "指标名称", "definition": "指标定义",
           "formula": "计算公式"}, [{"id": "3", "name": "经营利润", "definition": "经营所得",
                                   "formula": "profit = revenue - cost"}])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        groups = {"concepts": [], "record_alignments": []}
        types = []
        for table in sorted(data.tables):
            for row in data.rows(table):
                key = "type:" + row["id"]
                rid = data.record_id(table, row)
                props = []
                for role, column in (("name", "name"), ("description", "definition"), ("formula", "formula")):
                    if not row.get(column):
                        continue
                    eid = "record:" + digest([data.snapshot_id, rid, column])[:24]
                    data.evidence[eid] = {"origin": "observed_record", "raw_fragment": row[column],
                        "source_ref": {"snapshot_id": data.snapshot_id, "record_id": rid,
                                       "table": table, "column": column, "row": row["__r2_row"]}}
                    props.append({"role": role, "source_table": table, "source_column": column, "evidence_ids": [eid]})
                types.append(DerivedType(id=key, parent="Metric" if row.get("formula") else "Measure",
                    label=row["name"], definition=row["definition"], category="business_type",
                    evidence_ids=[e for p in props for e in p["evidence_ids"]], source_properties=props))
                groups["concepts"].append({"id": key, "ontology_type_id": key})
                groups["record_alignments"].append({"source_record_id": rid, "concept_id": key, "mapping_kind": "exact"})
        indexed = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(indexed["index_path"])
        try:
            result = compile_calculation_stage(data, PROFILE, BuildPlan(object_types=types), groups, index)
            assert result["calculations"][0]["status"] == "accepted"
            assert {d["symbol"] for d in result["dependencies"]} == {"revenue", "cost"}
            assert len(result["plan"].relation_types) == 2
        finally:
            index.close()
    finally:
        data.close()
