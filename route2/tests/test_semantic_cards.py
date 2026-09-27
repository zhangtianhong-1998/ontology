import csv

from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from ontology_r2.column_roles import classify_columns
from ontology_r2.storage import Dataset, write_yaml
import pytest


def _table(root, name, columns, rows, *, pk="id"):
    base = {"schema": "fruit", "table_name": name}
    write_yaml(root / "schema/tables" / f"{name}.yaml", {
        **base, "table_comment": "合成水果定义" if "definition" in name else "合成辅助记录",
        "columns": [{"column_name": field, "ordinal_position": position + 1,
                     "data_type": "text", "is_not_null": False, "default_value": None,
                     "column_comment": comment}
                    for position, (field, comment) in enumerate(columns.items())],
    })
    write_yaml(root / "schema/constraints" / f"{name}.yaml", {
        **base, "constraints": ([{"constraint_name": "pk", "constraint_type": "p",
                                  "definition": f"PRIMARY KEY ({pk})"}] if pk else []),
    })
    write_yaml(root / "schema/foreign_keys" / f"{name}.yaml", {**base, "foreign_keys": []})
    (root / "data").mkdir(exist_ok=True)
    with (root / "data" / f"{name}.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _dataset(tmp_path):
    root = tmp_path / "input"
    _table(root, "fruit_definition", {
        "id": "记录 ID", "metric_name": "指标名称", "definition": "指标定义",
        "scope": "区域范围", "unit": "单位", "alias": "别名",
    }, [
        {"id": "1", "metric_name": "水果收入", "definition": "华东水果零售收入", "scope": "华东", "unit": "元", "alias": "销售额"},
        {"id": "2", "metric_name": "水果收入", "definition": "华南水果批发收入", "scope": "华南", "unit": "元", "alias": "营收"},
        {"id": "3", "metric_name": "水果收入", "definition": "华东水果零售收入", "scope": "华东", "unit": "元", "alias": "销售额"},
    ])
    _table(root, "opaque_table", {"id": "记录 ID", "payload": "", "empty": ""}, [
        {"id": "10", "payload": "苹果库存规则", "empty": ""},
        {"id": "11", "payload": "", "empty": ""},
    ])
    _table(root, "empty_table", {"id": "记录 ID", "creation_date": "创建时间"}, [
        {"id": "20", "creation_date": "2026-01-01"},
    ])
    work = tmp_path / "work"
    work.mkdir()
    return Dataset(root, work)


def test_full_scan_dedup_scope_short_chinese_and_unknown_fallback(tmp_path):
    data = _dataset(tmp_path)
    try:
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        coverage = built["coverage"]
        assert coverage["rows_scanned"] == 6
        assert coverage["rows_indexed"] == 4
        assert coverage["rows_without_card"] == 2
        assert coverage["cards_indexed"] == 3
        assert coverage["rows_omitted_cap"] == 0
        index = SemanticCardIndex(built["index_path"])
        try:
            east = index.search("收入", kind="definition", scope="华东")
            south = index.search("收入", kind="definition", scope="华南")
            assert len(east) == len(south) == 1
            assert east[0]["card_id"] != south[0]["card_id"]
            assert east[0]["occurrence_count"] == 2
            assert "bm25_zh_2_3gram" in east[0]["retrieval_channels"]
            assert east[0]["fields"]["description"][0]["value"] == "华东水果零售收入"
            assert len(index.sources(east[0]["card_id"])) == 2
            assert len(index.search("销售额", kind="definition", scope="华东")) == 1
            all_definitions = index.all_cards(2)
            assert all_definitions["complete"] is True and all_definitions["total"] == 2
            assert len(all_definitions["cards"]) == 2
            too_small = index.all_cards(1)
            assert too_small == {"cards": [], "total": 2, "complete": False,
                                 "reason": "card_count_exceeds_limit"}
            unknown = index.seeds(10, kind="uncertain")
            assert len(unknown) == 1 and unknown[0]["fields"]["unknown"][0]["value"] == "苹果库存规则"
            assert [x["card_id"] for x in index.seeds(3, per_table=3)] == [
                x["card_id"] for x in index.seeds(3, per_table=3)]
        finally:
            index.close()
    finally:
        data.close()


def test_explicit_semantic_column_exclusion_is_shared_by_context_and_cards(tmp_path):
    root = tmp_path / "input"
    _table(root, "fruit_definition", {"id": "记录 ID", "metric_name": "指标名称",
                                       "definition": "指标定义"}, [
        {"id": "1", "metric_name": "水果收入", "definition": "PRIVATE_MARKER"},
    ])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work, privacy_config={
        "exclude_columns": ["fruit.fruit_definition.definition"]})
    try:
        table = data.tables["fruit.fruit_definition"]
        role = {item["column"]: item for item in classify_columns(table)}
        assert role["definition"]["role"] == "sensitive"
        context = data.context()
        assert "PRIVATE_MARKER" not in str(context)
        assert "definition" not in [column["column_name"] for column in context["tables"][0]["columns"]]
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            assert "PRIVATE_MARKER" not in str(index.seeds(2))
        finally:
            index.close()
    finally:
        data.close()


def test_unknown_semantic_column_exclusion_fails_closed(tmp_path):
    root = tmp_path / "input"
    _table(root, "fruit_definition", {"id": "记录 ID"}, [{"id": "1"}])
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ValueError, match="Unknown privacy.exclude_columns"):
        Dataset(root, work, privacy_config={
            "exclude_columns": ["fruit.fruit_definition.missing"]})


def test_card_cap_reports_omitted_rows_and_still_scans_all_input(tmp_path):
    data = _dataset(tmp_path)
    try:
        coverage = build_semantic_cards(data, tmp_path / "cap.sqlite", max_cards=1)["coverage"]
        assert coverage["rows_scanned"] == 6
        assert coverage["cards_indexed"] == 1
        assert coverage["rows_indexed"] == 2
        assert coverage["rows_omitted_cap"] == 2
        assert coverage["rows_without_card"] == 2
        assert coverage["partial"] is True
        assert coverage["omitted_examples"]
        assert coverage["by_table"]["fruit.fruit_definition"]["reserved_card_quota"] == 1
    finally:
        data.close()


def test_code_reference_card_preserves_source_field(tmp_path):
    root = tmp_path / "ref_input"
    _table(root, "dimension_references", {"id": "记录 ID", "dim_code": "维度编码",
                                           "source_field": "引用字段"}, [
        {"id": "1", "dim_code": "DIM0001", "source_field": "fruit_type"},
    ])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        built = build_semantic_cards(data, tmp_path / "refs.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            card = index.seeds(1)[0]
            assert card["kind"] == "reference"
            assert {value["column"] for value in card["fields"]["reference"]} == {
                "dim_code", "source_field"}
            assert "record_evidence_id" not in card["fields"]["reference"][0]
            assert built["coverage"]["kind_rows"]["reference"] == 1
            assert index.search("DIM0001", kind="reference")[0]["card_id"] == card["card_id"]
        finally:
            index.close()
    finally:
        data.close()


def test_model_code_aliases_remain_bindings_without_multiplying_definition_patterns(tmp_path):
    root = tmp_path / "input"
    code_fields = ["measure_code", "metric_code", *[f"external_{i}_code" for i in range(8)]]
    columns = {"id": "记录ID", "measure_name": "名称", "definition": "定义",
               "alias": "别名", "budget_flag": "预算适用范围", "period": "周期",
               **{field: "引用编码" for field in code_fields}}
    _table(root, "measure_definition", columns, [
        {"id": str(i), "measure_name": "收入", "definition": "对收入金额求和",
         "alias": "营收", "budget_flag": str(i == 3), "period": "Y",
         **{field: f"{field}_{i}" for field in code_fields}}
        for i in (1, 2, 3)
    ])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        table = data.tables["fruit.measure_definition"]
        table["inferred_semantic_roles"] = [
            {"column": field, "role": "alias", "status": "source_verified_role_candidate"}
            for field in code_fields]
        built = build_semantic_cards(data, tmp_path / "bindings.sqlite")
        assert built["coverage"]["definition_patterns_indexed"] == 2
        assert built["coverage"]["cards_indexed"] == 3
        report = built["coverage"]["by_table"]["fruit.measure_definition"]
        assert set(report["binding_columns_excluded_from_semantic_pattern"]) == set(code_fields)
        assert all(item["semantic_identity_claim"] is False and item["effective_role"] == "reference"
                   for item in report["role_conflicts"])
        index = SemanticCardIndex(built["index_path"])
        try:
            cards = index.all_cards(3)["cards"]
            assert len({card["record_id"] for card in cards}) == 3
            for card in cards:
                assert [entry["value"] for entry in card["fields"]["alias"]] == ["营收"]
                assert set(entry["column"] for entry in card["fields"]["reference"]) == set(code_fields)
                assert len(card["role_conflicts"]) == len(code_fields)
                assert card["scope"]["period"] == "Y"
                assert "budget_flag" in card["scope"]
            assert {row[0] for row in index.db.execute("SELECT name_norm FROM aliases")} == {"营收"}
        finally:
            index.close()
    finally:
        data.close()


def test_model_code_names_do_not_become_business_names_but_scope_stays_semantic(tmp_path):
    root = tmp_path / "input"
    _table(root, "metric_definition", {"id": "ID", "metric_code": "指标编码",
                                       "metric_name": "指标名称", "definition": "指标定义",
                                       "region_code": "地区编码"}, [
        {"id": "1", "metric_code": "M001", "metric_name": "水果收入",
         "definition": "水果收入金额合计", "region_code": "EAST"},
        {"id": "2", "metric_code": "M002", "metric_name": "水果收入",
         "definition": "水果收入金额合计", "region_code": "EAST"},
        {"id": "3", "metric_code": "M003", "metric_name": "水果收入",
         "definition": "水果收入金额合计", "region_code": "SOUTH"},
    ])
    _table(root, "metric_attr", {"id": "ID", "metric_code": "指标编码",
                                "definition": "属性定义"}, [
        {"id": "1", "metric_code": "M001", "definition": "按月记录属性"}])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        data.tables["fruit.metric_definition"]["inferred_semantic_roles"] = [
            {"column": column, "role": role, "status": "source_verified_role_candidate"}
            for column, role in (("metric_code", "name"), ("region_code", "name"),
                                 ("region_code", "scope"))]
        data.tables["fruit.metric_attr"]["inferred_semantic_roles"] = [
            {"column": "metric_code", "role": "name", "status": "source_verified_role_candidate"}]
        built = build_semantic_cards(data, tmp_path / "code_names.sqlite")
        assert built["coverage"]["definition_patterns_indexed"] == 2
        index = SemanticCardIndex(built["index_path"])
        try:
            cards = index.all_cards(3)["cards"]
            assert {card["name"] for card in cards} == {"水果收入"}
            assert {card["scope"]["region_code"] for card in cards} == {"EAST", "SOUTH"}
            assert all({entry["column"] for entry in card["fields"]["reference"]} == {
                "metric_code", "region_code"} for card in cards)
            assert all({entry["column"] for entry in card["fields"]["name"]} == {
                "metric_name"} for card in cards)
            attr = index.seeds(3, kind="reference")[0]
            assert attr["table"] == "fruit.metric_attr" and attr["name"] == ""
            assert attr["fields"]["reference"][0]["value"] == "M001"
        finally:
            index.close()
    finally:
        data.close()


def test_calculation_fragments_are_parameters_not_complete_formulas(tmp_path):
    root = tmp_path / "input"
    _table(root, "measure_definition", {"id": "ID", "measure_name": "名称",
        "definition": "定义", "inference_type": "聚合操作类型", "source_field": "被引用的字段",
        "calculation_formula": "计算公式"}, [
        {"id": "1", "measure_name": "收入", "definition": "收入的通用计算表达",
         "inference_type": "SUM", "source_field": "revenue", "calculation_formula": "收入"},
        {"id": "2", "measure_name": "收入", "definition": "收入的通用计算表达",
         "inference_type": "AVG", "source_field": "revenue", "calculation_formula": "收入"},
        {"id": "3", "measure_name": "收入", "definition": "收入的通用计算表达",
         "inference_type": "SUM", "source_field": "net_revenue", "calculation_formula": "收入"},
    ])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        data.tables["fruit.measure_definition"]["inferred_semantic_roles"] = [
            {"column": column, "role": "formula", "status": "source_verified_role_candidate",
             "schema_evidence_id": f"schema:fruit.measure_definition:{column}",
             "observations": [{"row_number": 1, "column": column, "value": value}]}
            for column, value in (("inference_type", "SUM"), ("source_field", "revenue"),
                                  ("calculation_formula", "收入"))]
        built = build_semantic_cards(data, tmp_path / "fragments.sqlite")
        assert built["coverage"]["definition_patterns_indexed"] == 3
        index = SemanticCardIndex(built["index_path"])
        try:
            for card in index.all_cards(3)["cards"]:
                assert [entry["column"] for entry in card["fields"]["formula"]] == ["calculation_formula"]
                assert {"inference_type", "source_field"} <= set(card["scope"])
                fragments = {item["column"]: item for item in card["calculation_fragments"]}
                assert fragments["inference_type"]["effective_role"] == "calculation_operator"
                assert fragments["source_field"]["effective_role"] == "operand_reference"
                assert all(item["formula_status"] == "fragment" and not item["expression_constructed"]
                           and not item["operand_target_verified"] for item in fragments.values())
        finally:
            index.close()
    finally:
        data.close()


def test_reference_rule_name_does_not_turn_rule_row_into_business_definition(tmp_path):
    root = tmp_path / "rule_input"
    _table(root, "param_ref_rule", {"id": "记录 ID", "rule_name": "规则名称",
                                    "source_field": "引用字段", "dim_code": "维度编码"}, [
        {"id": "1", "rule_name": "地区取值规则",
         "source_field": "region_code", "dim_code": "DIM0002"},
    ])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        built = build_semantic_cards(data, tmp_path / "rule_cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            assert index.seeds(1, kind="definition") == []
            card = index.seeds(1, kind="reference")[0]
            assert card["root_hint"] == "GeneralObject"
            assert card["name"] == "地区取值规则"
        finally:
            index.close()
    finally:
        data.close()


def test_dimension_combination_rule_stays_context_not_dimension_definition(tmp_path):
    root = tmp_path / "combo_input"
    _table(root, "fruit_dim_combo_rule", {
        "id": "记录 ID", "combo_name": "组合规则名称",
        "combo_description": "取数规则说明", "dim_code": "维度编码",
    }, [{"id": "1", "combo_name": "客户类型组合取数",
         "combo_description": "按水果和客户类型组合", "dim_code": "DIM0004"}])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        built = build_semantic_cards(data, tmp_path / "combo.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            assert index.seeds(1, kind="definition") == []
            assert index.seeds(1, kind="reference")[0]["root_hint"] == "GeneralObject"
        finally:
            index.close()
    finally:
        data.close()


def test_credentials_do_not_enter_semantic_cards(tmp_path):
    root = tmp_path / "secret_input"
    _table(root, "fruit_db_connection", {
        "id": "记录 ID", "connection_name": "连接名称",
        "password": "密码", "api_key": "访问密钥",
        "connection_url": "连接地址", "connection_description": "用途说明",
    }, [{"id": "1", "connection_name": "测试连接", "password": "sensitive-password-value",
         "api_key": "sensitive-api-key-value", "connection_url": "secret://credential",
         "connection_description": "水果数据源"}])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        context = str(data.context())
        assert "sensitive-password-value" not in context
        assert "sensitive-api-key-value" not in context
        assert "secret://credential" not in context
        built = build_semantic_cards(data, tmp_path / "secret.sqlite")
        index = SemanticCardIndex(built["index_path"])
        try:
            cards = index.seeds(10)
            assert all("sensitive-" not in str(card) and "secret://" not in str(card)
                       for card in cards)
        finally:
            index.close()
    finally:
        data.close()


def test_no_semantic_definition_finishes_with_empty_index(tmp_path):
    root = tmp_path / "only_input"
    _table(root, "empty_table", {"id": "记录 ID", "creation_date": "创建时间"}, [
        {"id": "1", "creation_date": "2026-01-01"},
    ])
    _table(root, "technical_link", {"id": "记录 ID", "business_id": "业务 ID",
                                      "business_type": "业务类型", "last_update_date": "更新时间"}, [
        {"id": "2", "business_id": "100", "business_type": "API",
         "last_update_date": "2026-01-01"},
    ])
    work = tmp_path / "work"
    work.mkdir()
    data = Dataset(root, work)
    try:
        built = build_semantic_cards(data, tmp_path / "empty.sqlite")
        assert built["coverage"]["rows_without_card"] == 2
        index = SemanticCardIndex(built["index_path"])
        try:
            assert index.search("收入") == []
            assert index.seeds(10) == []
        finally:
            index.close()
    finally:
        data.close()
