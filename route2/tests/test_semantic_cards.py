import csv
import sqlite3

from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from ontology_r2.column_roles import classify_columns
from ontology_r2.storage import Dataset, write_yaml
import pytest


def _physical_resource_dataset(tmp_path, declaration="physical table name，合成业务字段"):
    root = tmp_path / "input"
    _table(root, "resource_definition", {
        "id": "记录 ID", "physical_table_name": declaration,
        "display_name": "业务对象名称", "definition": "定义描述", "reference_code": "引用编码",
    }, [
        {"id": "1", "physical_table_name": "ledger_001", "display_name": "经营汇总表",
         "definition": "按经营对象汇总经营数据", "reference_code": "A"},
        {"id": "2", "physical_table_name": "ledger_002", "display_name": "经营汇总表",
         "definition": "按经营对象汇总经营数据", "reference_code": "B"},
    ])
    work = tmp_path / "work"
    work.mkdir()
    return Dataset(root, work)


def test_declared_physical_names_only_share_scheduling_pattern(tmp_path):
    data = _physical_resource_dataset(tmp_path)
    try:
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        report = built["coverage"]["by_table"]["fruit.resource_definition"]
        assert built["coverage"]["cards_indexed"] == 2
        assert built["coverage"]["definition_patterns_indexed"] == 1
        assert report["definition_pattern_exclusions"] == [{
            "column": "physical_table_name", "role": "name",
            "reason": "declared_physical_resource_name", "declaration": "physical table name，合成业务字段",
            "schema_evidence_id": "schema:fruit.resource_definition:physical_table_name",
            "scope": "definition_pattern_scheduling_only", "entity_identity_claim": False,
            "template_membership_still_compares_original_value": True}]
        assert "physical_table_name" not in report["binding_columns_excluded_from_semantic_pattern"]
        index = SemanticCardIndex(built["index_path"])
        try:
            cards = index.all_cards(2)["cards"]
            assert len({card["record_id"] for card in cards}) == 2
            assert len({card["card_id"] for card in cards}) == 2
            assert {entry["value"] for card in cards for entry in card["fields"]["name"]
                    if entry["column"] == "physical_table_name"} == {"ledger_001", "ledger_002"}
            assert {entry["value"] for card in cards for entry in card["fields"]["reference"]
                    if entry["column"] == "reference_code"} == {"A", "B"}
            assert index.search("ledger_001", limit=2)[0]["row_number"] == 1
        finally:
            index.close()
    finally:
        data.close()


@pytest.mark.parametrize("declaration", ["", "名称", "业务对象名称", "不是物理表名", "物理表名或业务对象名称",
                                         "物理对象名称", "物理资源名称", "physical object name", "physical resource name"])
def test_column_name_alone_or_ambiguous_declaration_cannot_hide_name_variants(tmp_path, declaration):
    data = _physical_resource_dataset(tmp_path, declaration)
    try:
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        assert built["coverage"]["definition_patterns_indexed"] == 2
        assert not built["coverage"]["by_table"]["fruit.resource_definition"]["definition_pattern_exclusions"]
    finally:
        data.close()


@pytest.mark.parametrize("changed", ["business_name", "alias", "definition", "formula", "unit", "period"])
def test_physical_name_exclusion_preserves_other_semantic_differences(changed):
    from ontology_r2.semantic_cards import _definition_pattern_exclusions, _row_card

    roles = {"name": ["storage_name", "business_name"], "alias": ["alias"],
             "description": ["definition"], "formula": ["formula"], "unit": ["unit"], "scope": ["period"]}
    table = {"name": "demo.resources", "columns": [
        {"column_name": "storage_name", "column_comment": "物理文件名称"}]}
    excluded = [item["column"] for item in _definition_pattern_exclusions(table, roles)]
    assert excluded == ["storage_name"]
    row = {"storage_name": "resource_1", "business_name": "经营汇总", "alias": "汇总数据",
           "definition": "按经营对象汇总", "formula": "sum(amount)", "unit": "元", "period": "Y"}
    make = lambda value: _row_card("demo.resources", value, roles, [], [], 512, 2048,
                                   "definition_data", pattern_name_exclusions=excluded)
    original = make(row)
    identity_variant = make({**row, "storage_name": "resource_2"})
    assert identity_variant["signature"] != original["signature"]
    assert identity_variant["pattern_signature"] == original["pattern_signature"]
    semantic_variant = make({**row, "storage_name": "resource_2", changed: row[changed] + " changed"})
    assert semantic_variant["pattern_signature"] != original["pattern_signature"]


def test_shared_physical_name_pattern_does_not_propagate_exact_type_membership(tmp_path):
    from ontology_r2.definition_memberships import build_definition_memberships
    from ontology_r2.group_incremental import ConceptBundleDecision, compile_concept
    from ontology_r2.storage import read_yaml
    from pathlib import Path

    data = _physical_resource_dataset(tmp_path)
    index = None
    try:
        built = build_semantic_cards(data, tmp_path / "cards.sqlite")
        index = SemanticCardIndex(built["index_path"])
        cards = index.all_cards(2)["cards"]
        card = next(item for item in cards if item["row_number"] == 1)
        assert len({item["pattern_id"] for item in cards}) == 1
        profile = read_yaml(Path(__file__).resolve().parents[1] / "ontologies/internal_model.yaml")
        decision = ConceptBundleDecision(status="proposed", label="经营汇总表", root_type="GeneralObject",
            ontology_level="type", definition="按经营对象汇总经营数据", alignments=[{
                "record_id": card["record_id"], "mapping_kind": "exact", "quote": "按经营对象汇总经营数据"}])
        concept, alignments = compile_concept(data, profile, {"records": [card]}, decision, {})
        group = {"snapshot_id": data.snapshot_id, "concepts": [concept], "record_alignments": alignments}
        result = build_definition_memberships(data, index, group)
        assert {member["row_number"] for member in result["memberships"]} == {1}
        assert result["rejections"] == [{"record_id": next(c["record_id"] for c in cards if c["row_number"] == 2),
                                         "row_number": 2, "card_id": next(c["card_id"] for c in cards if c["row_number"] == 2),
                                         "reason": "full_semantic_values_differ"}]
        assert [item["source_record_id"] for item in group["record_alignments"]] == [card["record_id"]]
        assert result["coverage"]["partial"]
    finally:
        if index:
            index.close()
        data.close()


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


@pytest.mark.parametrize("legacy_cache", [False, True])
def test_record_lookup_index_preserves_rows_and_legacy_first_card(tmp_path, legacy_cache):
    data = _dataset(tmp_path)
    path = tmp_path / "cards.sqlite"
    try:
        build_semantic_cards(data, path)
        with sqlite3.connect(path) as db:
            assert "card_sources_record" in {row[1] for row in db.execute("PRAGMA index_list(card_sources)")}
            if legacy_cache:
                db.execute("DROP INDEX card_sources_record")
            card_ids = [row[0] for row in db.execute("SELECT card_id FROM cards ORDER BY card_id")]
            record_id, row_number = db.execute("SELECT record_id,row_number FROM card_sources LIMIT 1").fetchone()
            # A record may have several card bindings. Retain the former
            # card-first covering index's first result after adding the index.
            db.executemany("INSERT OR IGNORE INTO card_sources VALUES (?,?,?)",
                           [(card_id, record_id, row_number) for card_id in reversed(card_ids)])
            expected = db.execute("SELECT card_id FROM card_sources "
                                  "INDEXED BY sqlite_autoindex_card_sources_1 WHERE record_id=?",
                                  (record_id,)).fetchone()[0]
            tables = ("cards", "aliases", "card_sources", "card_fts", "metadata")
            before = {table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                      for table in tables}
        sources = {p: p.read_bytes() for p in (tmp_path / "input").rglob("*") if p.is_file()}
        index = SemanticCardIndex(path)
        try:
            query = "SELECT card_id FROM card_sources WHERE record_id=? ORDER BY card_id LIMIT 1"
            assert index.db.execute(query, (record_id,)).fetchone()[0] == expected == min(card_ids)
            plan = " ".join(row[3] for row in index.db.execute("EXPLAIN QUERY PLAN " + query, (record_id,)))
            assert "SEARCH card_sources USING COVERING INDEX card_sources_record (record_id=?)" in plan
            assert "SCAN" not in plan and "TEMP B-TREE" not in plan
            assert {table: [tuple(row) for row in index.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in tables} == before
            assert index.coverage["rows_scanned"] == 6
        finally:
            index.close()
        with sqlite3.connect(path) as db:
            assert "card_sources_record" in {row[1] for row in db.execute("PRAGMA index_list(card_sources)")}
        assert all(p.read_bytes() == value for p, value in sources.items())
    finally:
        data.close()


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


def _wide_reference_dataset(tmp_path, *, first_reference=None):
    root = tmp_path / "input"
    references = [f"ref{i}_code" for i in range(10)]
    rows = []
    for number in (1, 2):
        rows.append({"id": str(number), "measure_name": "收入", "definition": "适用于不同经营对象的收入总额",
                     **{field: "reference_value_" + "A" * 80 for field in references},
                     "ref9_code": first_reference if number == 1 else "reference_value_" + "B" * 80})
    _table(root, "measure_definition", {
        "id": "记录主键", "measure_name": "度量名称", "definition": "度量定义",
        **{field: "引用编码" for field in references}}, rows)
    work = tmp_path / "work"
    work.mkdir()
    return Dataset(root, work), references


def test_all_ordinary_reference_columns_keep_full_signatures_and_explicit_packet_limits(tmp_path):
    from ontology_r2.instance_bundles import _card_text, build_instance_bundles

    data, references = _wide_reference_dataset(tmp_path, first_reference="reference_value_" + "A" * 80)
    try:
        built = build_semantic_cards(data, tmp_path / "cards.sqlite", max_field_chars=16)
        report = built["coverage"]["by_table"]["fruit.measure_definition"]
        assert report["binding_columns_excluded_from_semantic_pattern"] == []
        assert report["reference_columns_considered"] == references
        assert report["reference_columns_not_examined"] == []
        assert report["reference_column_selection"] == "all_eligible_reference_columns"
        assert built["coverage"]["cards_indexed"] == 2
        assert built["coverage"]["definition_patterns_indexed"] == 1
        index = SemanticCardIndex(built["index_path"])
        try:
            cards = index.all_cards(2)["cards"]
            assert all(len(card["fields"]["reference"]) == 10 for card in cards)
            assert all("reference_value" not in _card_text(card) for card in cards)
            # Complete values differ beyond both the old column cap and the
            # character preview; identical previews never erase that variant.
            assert cards[0]["fields"]["reference"] == cards[1]["fields"]["reference"]
            assert index.db.execute("SELECT count(DISTINCT signature) FROM cards").fetchone()[0] == 2
            assert index.db.execute("SELECT count(DISTINCT reference_signature) FROM cards").fetchone()[0] == 2
            assert index.pattern_window(1)["patterns"][0]["reference_variant_count"] == 2
            packets = build_instance_bundles(data, index, {"rules": []}, {
                "max_concept_bundles": 1, "max_relation_bundles": 0, "max_bundle_bytes": 1000})
            assert packets["bundles"] == []
            assert packets["coverage"]["partial"]
            assert [item["reason"] for item in packets["coverage"]["skipped"]] == ["seed_over_budget"]
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
