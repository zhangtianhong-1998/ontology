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
