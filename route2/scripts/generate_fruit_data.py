"""Deterministic, entirely synthetic 23-table fruit-market fixture.

Usage:
  python scripts/generate_fruit_data.py generate --output local_data/fruit_data
  python scripts/generate_fruit_data.py validate --input local_data/fruit_data
"""
from __future__ import annotations

import argparse
import ast
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path

import yaml


SCHEMA = "fruit_market"
AUDIT = "delete_flag created_by creation_date last_updated_by last_update_date".split()
FRUITS = (
    ("APPLE", "苹果", "apple", 8.2), ("BANANA", "香蕉", "banana", 4.1),
    ("ORANGE", "橙子", "orange", 6.4), ("GRAPE", "葡萄", "grape", 12.3),
    ("MANGO", "芒果", "mango", 11.2), ("STRAWBERRY", "草莓", "strawberry", 14.0),
    ("BLUEBERRY", "蓝莓", "blueberry", 15.0), ("DURIAN", "榴莲", "durian", 15.0),
    ("LYCHEE", "荔枝", "lychee", 13.0), ("KIWI", "猕猴桃", "kiwi", 10.4),
)
REGIONS = (("EAST", "华东"), ("SOUTH", "华南"), ("SEA", "东南亚"),
           ("SAM", "南美"), ("AFR", "非洲"))
CHANNELS = (("WHOLESALE", "批发"), ("RETAIL", "零售"),
            ("ECOM", "电商"), ("EXPORT", "出口"))
CUSTOMERS = (("SUPER", "超市"), ("PROCESS", "加工厂"),
             ("TRADER", "贸易商"), ("CONSUMER", "消费者"))
PORTS = (("SHANGHAI", "上海港"), ("SHENZHEN", "深圳港"),
         ("NINGBO", "宁波港"), ("GUANGZHOU", "广州港"))
DIMENSIONS = (
    ("DIM0001", "水果类别", "DIM_PROD", FRUITS),
    ("DIM0002", "产区", "DIM_REGION", REGIONS),
    ("DIM0003", "销售渠道", "DIM_CHANNEL", CHANNELS),
    ("DIM0004", "客户类型", "DIM_CUST", CUSTOMERS),
    ("DIM0005", "进出口口岸", "DIM_PORT", PORTS),
)
MEASURES = (
    ("均价", "元/kg"), ("总产量", "吨"), ("进口量", "吨"),
    ("出口量", "吨"), ("库存量", "吨"), ("损耗率", "%"),
    ("合格率", "%"), ("周转率", "次/年"),
)
METRICS = (
    ("水果销售总额", "元", "sales_total = sales_volume * avg_price"),
    ("出口量同比增长", "%", "export_yoy = (this_year_export - last_year_export) / last_year_export * 100"),
    ("进口均价波动", "%", "import_price_change = (current_import_price - previous_import_price) / previous_import_price * 100"),
    ("库存周转率", "次/年", "turnover = sales_volume / avg_stock"),
    ("损耗率趋势", "%", "loss_rate = damaged_volume / inbound_volume * 100"),
    ("区域均价差异", "元/kg", "regional_price_gap = max_price - min_price"),
)
SOURCE_TYPES = ("DIM0001", "DIM0002", "DIM0003", "DIM0004", "DIM0005",
                "measure", "metric", "fixedValue", "period", "currency")


@dataclass(frozen=True)
class Table:
    name: str
    columns: str
    count: int
    pk: str | None
    comment: str
    audit: bool = True
    expected_columns: int = 0

    @property
    def fields(self) -> list[str]:
        return self.columns.split() + (AUDIT if self.audit else [])


TABLES = (
    Table("fruit_market_api", "fruit_api_id api_code api_name api_description api_url http_method protocol auth_method request_example response_example business_domain version status fruit_category_code region_code market_name data_source_code wide_table_id owner_team", 2500, "fruit_api_id", "水果经营数据服务 API 定义", expected_columns=24),
    Table("fruit_api_tag", "fruit_tag_id business_id business_type tag_name tag_category tag_frequency domain_code tag_description", 95813, "fruit_tag_id", "API、卡片和宽表的业务标签", expected_columns=13),
    Table("fruit_op_log", "fruit_log_id business_id business_type operation_type parent_business_id parent_business_type operator_name operation_time operation_note before_state after_state domain_code", 5000, "fruit_log_id", "业务对象的操作日志", expected_columns=17),
    Table("fruit_api_input_param", "fruit_input_param_id business_id business_type param_name param_cn_name param_type param_position is_required default_value param_description", 151937, "fruit_input_param_id", "API 和卡片的入参定义", expected_columns=15),
    Table("fruit_api_output_param", "fruit_output_param_id business_id business_type param_name param_cn_name param_type unit output_order param_description", 274444, "fruit_output_param_id", "API 和卡片的出参定义", expected_columns=14),
    Table("fruit_dashboard_card", "fruit_card_id card_code card_name card_description business_domain api_id dim_code metric_code measure_name wide_table_id chart_type component_name component_version positive_description negative_description analysis_period region_code fruit_category_code channel_code customer_type refresh_cycle data_source_code owner_team status layout_row layout_col layout_width layout_height card_config_json", 1000, "fruit_card_id", "水果经营仪表板卡片", expected_columns=34),
    Table("fruit_dim_definition", "fruit_dim_def_id dim_code dim_cn_name dim_en_name dim_type physical_table_name data_source_code schema_name filter_sql sync_type dim_description dim_level_count primary_code_field primary_name_field hierarchy_json domain_code owner_team status synonym_text", 80, "fruit_dim_def_id", "水果经营分析维度定义", expected_columns=24),
    Table("fruit_dim_member", "fruit_member_id dim_code member_code member_cn_name member_en_name parent_member_code level_no code_path synonym_text bg_code region_code fruit_category_code is_active member_description display_order", 5000, "fruit_member_id", "维度成员及层级取值", expected_columns=20),
    Table("fruit_dim_field", "fruit_field_id dim_code physical_field_name field_cn_name field_en_name field_type level_no synonym_text output_name filter_sql is_key is_display domain_code field_description", 3000, "fruit_field_id", "维度物理字段及输出名", expected_columns=19),
    Table("fruit_dim_anchor", "fruit_anchor_id dim_code anchor_code anchor_name anchor_description applicable_scenario synonym_text domain_code owner_team status", 200, "fruit_anchor_id", "维度业务锚点", expected_columns=15),
    Table("fruit_dim_combo_rule", "fruit_combo_rule_id main_dim_code sub_dim_code region_dim_code customer_dim_code product_dim_code combo_name combo_description rule_json", 500, "fruit_combo_rule_id", "多维组合取数规则", expected_columns=14),
    Table("fruit_measure_def", "fruit_measure_id measure_code measure_name standard_name parameter_name measure_description unit budget_flag forecast_flag actual_flag target_flag remain_flag estimate_flag rank_flag period inference_type domain_code source_field metric_code", 5000, "fruit_measure_id", "水果经营度量定义与期间标记", expected_columns=24),
    Table("fruit_metric_detail", "fruit_metric_detail_id metric_code metric_name metric_definition calculation_formula setup_purpose business_anchor_code source_code unit domain_code metric_category", 16000, "fruit_metric_detail_id", "指标业务定义、来源与计算公式", expected_columns=16),
    Table("fruit_metric_attr", "fruit_metric_attr_id metric_code metric_formula metric_category metric_type synonym_text domain_code unit valid_period measure_code attr_description", 16000, "fruit_metric_attr_id", "指标公共属性及别名", expected_columns=16),
    Table("fruit_metric_anchor", "fruit_metric_anchor_id metric_code anchor_code anchor_name synonym_text domain_code anchor_description applicable_scenario owner_team", 9, "fruit_metric_anchor_id", "指标业务锚点", expected_columns=14),
    Table("fruit_param_ref_rule", "fruit_ref_rule_id business_id business_type param_id source_type source_field field_rule rule_description", 433645, "fruit_ref_rule_id", "参数引用维度、度量、指标或固定值的规则", expected_columns=13),
    Table("fruit_business_rule", "fruit_rule_id domain_code rule_name scenario_json rule_content_json rule_type rule_description", 2000, "fruit_rule_id", "水果经营分析场景规则", expected_columns=12),
    Table("fruit_db_connection", "fruit_db_conn_id connection_code connection_type connection_name connection_url account_name schema_name db_name port region_code ssl_mode status owner_team connection_description", 10, "fruit_db_conn_id", "合成数据源连接元数据，不含可用凭据", expected_columns=19),
    Table("fruit_wide_table_def", "fruit_wide_table_id physical_table_name data_source_code schema_name table_cn_name table_en_name table_description domain_code grain refresh_cycle owner_team status", 200, "fruit_wide_table_id", "水果销售、进出口宽表定义", expected_columns=17),
    Table("fruit_wide_table_column", "fruit_column_id fruit_wide_table_id physical_field_name field_cn_name field_en_name field_type field_example field_description unit", 50000, "fruit_column_id", "宽表字段定义", expected_columns=14),
    Table("fruit_synonym_dict", "sn word type attr lang scope", 5000, None, "中英文及业务别名词典", audit=False, expected_columns=6),
    Table("fruit_org_dim_data", "dim_code member_code member_cn_name member_en_name level_no code_path dim_type region_code fruit_category_code bg_code synonym_text source_system valid_from valid_to status", 20000, None, "维度取值快照，无声明主键", audit=False, expected_columns=15),
    # The prompt lists 22 tables despite saying 23. This explicitly synthetic table
    # is the 23rd; it is deliberately not asserted to exist in the real database.
    Table("fruit_import_staging_temp", "staging_batch staging_row_no fruit_code fruit_cn_name origin_region_code origin_region_name channel_code channel_name customer_type port_code period sales_volume_ton avg_price_cny_per_kg sales_amount_cny import_volume_ton export_volume_ton inventory_ton damaged_volume_ton quality_pass_rate turnover_rate " + " ".join(f"stage_attr_{i:02d}" for i in range(1, 46)), 14900, None, "合成导入暂存表；为满足23表要求额外假设", expected_columns=70),
)
TABLE_BY_NAME = {table.name: table for table in TABLES}
assert len(TABLES) == len(TABLE_BY_NAME) == 23
assert sum(table.audit for table in TABLES) == 21
assert all(len(table.fields) == table.expected_columns for table in TABLES)


COMMENTS = {
    "api_name": "对外数据服务接口名称",
    "api_description": "接口提供的水果经营数据说明",
    "api_url": "接口访问路径，仅合成示例",
    "request_example": "请求参数 JSON 示例",
    "response_example": "响应结果 JSON 示例",
    "param_name": "API 参数英文名称",
    "param_cn_name": "API 参数中文业务名称",
    "param_description": "API 参数的含义及适用范围",
    "business_id": "业务对象编号，需结合 business_type 解析目标表",
    "business_type": "业务对象类型：API、CARD 或 TABLE",
    "dim_code": "维度头编码，对应维度定义表的 dim_code",
    "dim_cn_name": "维度中文名称",
    "dim_en_name": "维度英文名称",
    "dim_type": "维度业务类别，如产品、地区或客户",
    "dim_description": "维度的业务含义和使用范围",
    "member_code": "维度成员编码；在所属 dim_code 内解释",
    "member_cn_name": "维度成员中文名称",
    "member_en_name": "维度成员英文名称",
    "code_path": "从维度头到成员的层级编码路径",
    "synonym_text": "同义词和别名，用于候选召回",
    "metric_code": "指标编码，对应指标详情和公共属性",
    "metric_name": "指标中文业务名称",
    "metric_definition": "指标定义及统计口径",
    "metric_formula": "指标公共属性中的计算表达式",
    "metric_type": "指标类型标记：Y、N 或 O",
    "measure_name": "度量中文业务名称",
    "standard_name": "带水果业务限定的度量标准名称",
    "measure_description": "度量的业务含义、聚合口径和适用范围",
    "unit": "计量单位；不同单位不可直接合并",
    "measure_code": "度量编码，对应度量定义",
    "source_type": "引用类别：维度编码、measure、metric、fixedValue、period 或 currency",
    "source_field": "被引用的编码、名称或参数字段，解释取决于 source_type",
    "field_rule": "允许取值或过滤规则 JSON；空值表示未声明",
    "calculation_formula": "指标计算表达式或自然语言计算口径",
    "delete_flag": "逻辑删除标记，0 为有效",
    "created_by": "创建人账号，审计属性",
    "creation_date": "创建时间，审计属性",
    "last_updated_by": "最后修改人账号，审计属性",
    "last_update_date": "最后修改时间，审计属性",
    "avg_price_cny_per_kg": "水果均价，元每千克",
    "sales_volume_ton": "销售量，吨",
    "import_volume_ton": "进口量，吨",
    "export_volume_ton": "出口量，吨",
}


def _value(values, i):
    return values[i % len(values)]


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _metric_code(i):
    return f"MET{i + 1:06d}"


def _measure_code(i):
    return f"MES{i + 1:06d}"


def _member(dim_index, i):
    values = DIMENSIONS[dim_index][3]
    item = _value(values, i)
    extension = i // len(values)
    code = item[0] if extension == 0 else f"{item[0]}_{extension:04d}"
    name = item[1] if extension == 0 else f"{item[1]}细分{extension}"
    english = item[2] if len(item) > 2 else item[0].lower()
    return code, name, english


def _referenced_member(dim_index, i, counts):
    # Dimension membership is interleaved across five dim codes; select only
    # existing rows in the same dimension even at arbitrary fractional scale.
    total = counts["fruit_dim_member"]
    available = 1 + (total - 1 - dim_index) // len(DIMENSIONS)
    return _member(dim_index, (i // len(DIMENSIONS)) % available)


def _business(i, counts):
    kind = ("API", "CARD", "TABLE")[i % 3]
    target = {"API": "fruit_market_api", "CARD": "fruit_dashboard_card",
              "TABLE": "fruit_wide_table_def"}[kind]
    return kind, str(1 + (i // 3) % counts[target])


def _default(field, i):
    fruit = _value(FRUITS, i)
    if field == "delete_flag":
        return "0"
    if field in ("created_by", "last_updated_by"):
        return "fixture_generator"
    if field == "creation_date":
        return f"2025-01-{1 + i % 28:02d}T08:00:00"
    if field == "last_update_date":
        return f"2025-02-{1 + i % 28:02d}T08:00:00"
    if field.endswith("_description") or field.endswith("_definition"):
        return f"用于全球水果经营分析的{fruit[1]}业务说明；按区域和会计期统计。"
    if field.endswith("_name"):
        return fruit[1] + "经营分析"
    if field.endswith("_code"):
        return fruit[0]
    if field.endswith("_json"):
        return _json({"synthetic": True, "fruit": fruit[0]})
    if field.endswith("_flag"):
        return "0"
    if field.endswith("_id"):
        return str(i + 1)
    if field.endswith("_type"):
        return "FRUIT"
    if field.endswith("_sql"):
        return ""
    if field.endswith("_time") or field.endswith("_date"):
        return "2025-02-01T08:00:00"
    if field.startswith("stage_attr_"):
        return "" if (i + int(field[-2:])) % 8 else f"批次附注{i % 13}"
    return f"{fruit[1]}-{i % 37 + 1}"


def make_row(table: Table, i: int, counts: dict[str, int]) -> dict[str, str]:
    name = table.name
    r = {field: _default(field, i) for field in table.fields}
    fruit = _value(FRUITS, i)
    region = _value(REGIONS, i)
    dim_index = i % len(DIMENSIONS)
    dim_code, dim_name, dim_type, _ = DIMENSIONS[dim_index]
    member_code, member_name, member_en = (
        _referenced_member(dim_index, i, counts) if name == "fruit_org_dim_data"
        else _member(dim_index, i // len(DIMENSIONS)))
    metric_ix = i % counts["fruit_metric_detail"]
    metric_name, metric_unit, formula = _value(METRICS, metric_ix)
    measure_name, measure_unit = _value(MEASURES, i)
    business_type, business_id = _business(i, counts)
    if table.pk:
        r[table.pk] = str(i + 1)
    if "business_type" in r:
        r.update(business_type=business_type, business_id=business_id)
    if "domain_code" in r:
        r["domain_code"] = ("SALES", "IMPORT_EXPORT", "INVENTORY")[i % 3]
    if "business_domain" in r:
        r["business_domain"] = ("销售", "进出口", "库存")[i % 3]
    if "region_code" in r:
        r["region_code"] = region[0]
    if "fruit_category_code" in r:
        r["fruit_category_code"] = fruit[0]
    if "data_source_code" in r:
        r["data_source_code"] = "FRUIT_DEMO"
    if "schema_name" in r:
        r["schema_name"] = SCHEMA
    if "owner_team" in r:
        r["owner_team"] = "fruit_analytics"
    if "status" in r:
        r["status"] = "ACTIVE"
    if "dim_code" in r:
        r["dim_code"] = dim_code
    if "metric_code" in r:
        r["metric_code"] = _metric_code(metric_ix)
    if "measure_code" in r:
        r["measure_code"] = _measure_code(i % counts["fruit_measure_def"])
    if "wide_table_id" in r:
        r["wide_table_id"] = str(1 + i % counts["fruit_wide_table_def"])
    if "fruit_wide_table_id" in r:
        r["fruit_wide_table_id"] = str(1 + i % counts["fruit_wide_table_def"])
    if name == "fruit_market_api":
        subject = ("市场价格查询", "进出口通关查询", "产量统计", "库存查询", "质量检测")[i % 5]
        r.update(api_code=f"API{i + 1:06d}", api_name=f"{fruit[1]}{subject}",
                 api_description=f"查询{fruit[1]}在{region[1]}的{subject}结果。",
                 api_url=f"/synthetic/fruit/v1/{fruit[2]}/{i % 5}",
                 http_method="GET", protocol="HTTPS", auth_method="API_KEY_DEMO",
                 request_example=_json({"fruit_code": fruit[0], "region_code": region[0]}),
                 response_example=_json({"avg_price_cny_per_kg": fruit[3],
                                         "unit": "元/kg", "synthetic": True}),
                 version="v1", market_name="全球水果市场")
    elif name == "fruit_api_tag":
        tag = ("高频调用", "经营分析", "水果价格", "进出口", "区域看板")[i % 5]
        r.update(tag_name=tag, tag_category="业务", tag_frequency=str(i % 30 + 1),
                 tag_description=f"{business_type} 业务标签：{tag}")
    elif name == "fruit_op_log":
        r.update(operation_type=("ADD", "EDIT", "DELETE")[i % 3],
                 parent_business_id=business_id, parent_business_type=business_type,
                 operator_name="fixture_generator", operation_time="2025-02-01T10:00:00",
                 operation_note="合成操作日志", before_state="{}", after_state="{}")
    elif name in ("fruit_api_input_param", "fruit_api_output_param"):
        candidates = (("fruit_code", "水果编码", "文本", ""),
                      ("region_code", "产区编码", "文本", ""),
                      ("period", "会计期", "文本", ""),
                      ("avg_price", "均价", "小数", "元/kg"),
                      ("export_volume", "出口量", "小数", "吨"),
                      ("metric_code", "指标编码", "文本", ""),
                      ("dim_code", "维度编码", "文本", ""))
        col, cn, typ, unit = _value(candidates, i)
        r.update(param_name=col, param_cn_name=cn, param_type=typ,
                 param_description=f"{cn}，用于{business_type}水果经营分析。")
        if name == "fruit_api_input_param":
            r.update(param_position="query", is_required=str(int(i % 3 != 0)),
                     default_value="",)
        else:
            # A rule has one param_id but no direction column. Keep input and
            # output parameter IDs disjoint so a value identifies one row.
            r["fruit_output_param_id"] = str(counts["fruit_api_input_param"] + i + 1)
            r.update(unit=unit, output_order=str(i % 10 + 1))
    elif name == "fruit_dashboard_card":
        r.update(card_code=f"CARD{i + 1:06d}", card_name=f"{region[1]}{fruit[1]}{metric_name}看板",
                 api_id=str(1 + i % counts["fruit_market_api"]),
                 dim_code=dim_code, metric_code=_metric_code(metric_ix),
                 measure_name=measure_name,
                 chart_type=("line", "bar", "table")[i % 3],
                 component_name="fruit_metric_view", component_version="1.0",
                 positive_description="上涨表示经营规模扩大", negative_description="下降需检查需求和库存",
                 analysis_period=("M", "Q", "Y")[i % 3],
                 channel_code=_value(CHANNELS, i)[0],
                 customer_type=_value(CUSTOMERS, i)[0],
                 refresh_cycle="DAY", layout_row=str(i % 10), layout_col=str(i % 4),
                 layout_width="4", layout_height="3",
                 card_config_json=_json({"fruit": fruit[0], "metric": _metric_code(metric_ix)}))
    elif name == "fruit_dim_definition":
        # Keep the first five canonical dimension codes used by source_type;
        # further definitions get their own codes instead of false key collisions.
        unique_dim_code = f"DIM{i + 1:04d}"
        unique_dim_name = dim_name if i < 5 else f"{dim_name}专题维{i + 1}"
        r.update(dim_code=unique_dim_code, dim_cn_name=unique_dim_name,
                 dim_en_name=f"fruit_dimension_{i + 1}",
                 dim_type=dim_type, physical_table_name="fruit_org_dim_data",
                 filter_sql="delete_flag = 0" if i % 6 == 0 else "",
                 sync_type="FULL", dim_description=f"{unique_dim_name}用于水果经营分析。",
                 dim_level_count="2", primary_code_field="member_code",
                 primary_name_field="member_cn_name",
                 hierarchy_json=_json({"levels": ["分类", "成员"]}),
                 synonym_text=unique_dim_name + "," + ("水果品类" if dim_index == 0 else "业务维度"))
    elif name in ("fruit_dim_member", "fruit_org_dim_data"):
        r.update(dim_code=dim_code, member_code=member_code, member_cn_name=member_name,
                 member_en_name=member_en, level_no="2", code_path=f"{dim_code}/{member_code}",
                 dim_type=dim_type, synonym_text=f"{member_name},{member_en}",
                 bg_code=f"BG{i % 8 + 1:02d}")
        if name == "fruit_dim_member":
            r.update(parent_member_code=dim_code, is_active="1",
                     display_order=str(i % 20 + 1), member_description=f"{dim_name}成员：{member_name}")
        else:
            r.update(source_system="FRUIT_DEMO", valid_from="2025-01-01",
                     valid_to="2099-12-31")
    elif name == "fruit_dim_field":
        variant = i // len(DIMENSIONS)
        base_field = ("member_code", "member_cn_name", "code_path")[variant % 3]
        physical_field = base_field if variant < 3 else f"{base_field}_{variant:03d}"
        r.update(physical_field_name=physical_field,
                 field_cn_name=("成员编码", "成员名称", "编码路径")[variant % 3] +
                 ("" if variant < 3 else f"扩展{variant}"),
                 field_en_name=("member code", "member name", "code path")[variant % 3],
                 field_type="varchar", level_no="2", synonym_text="维度字段",
                 output_name=physical_field,
                 filter_sql="", is_key=str(int(variant == 0)),
                 is_display=str(int(variant == 1)), field_description=f"{dim_name}的字段定义")
    elif name == "fruit_dim_anchor":
        r.update(anchor_code=f"DANCH{i + 1:05d}", anchor_name=f"{dim_name}分析锚点",
                 anchor_description=f"限定{dim_name}分析的业务语境。",
                 applicable_scenario="水果经营分析", synonym_text=f"{dim_name},业务维度")
    elif name == "fruit_dim_combo_rule":
        r.update(main_dim_code=dim_code, sub_dim_code=DIMENSIONS[(dim_index + 1) % 5][0],
                 region_dim_code="DIM0002", customer_dim_code="DIM0004",
                 product_dim_code="DIM0001", combo_name=f"{dim_name}组合取数",
                 combo_description="按水果、地区和客户类型组合取数",
                 rule_json=_json({"main": dim_code, "region": "DIM0002"}))
    elif name == "fruit_measure_def":
        r.update(measure_code=_measure_code(i), measure_name=measure_name,
                 standard_name=f"{fruit[1]}{measure_name}",
                 parameter_name=f"{fruit[2]}_{i % 8}_value",
                 measure_description=f"{fruit[1]}{measure_name}，按{region[1]}和会计期汇总。",
                 unit=measure_unit,
                 budget_flag=str(int(i % 7 == 0)), forecast_flag=str(int(i % 7 == 1)),
                 actual_flag=str(int(i % 7 == 2)), target_flag=str(int(i % 7 == 3)),
                 remain_flag=str(int(i % 7 == 4)), estimate_flag=str(int(i % 7 == 5)),
                 rank_flag=str(int(i % 7 == 6)), period=("Q", "M", "Y")[i % 3],
                 inference_type="SUM" if measure_unit == "吨" else "RATIO",
                 source_field=("avg_price_cny_per_kg", "sales_volume_ton")[i % 2])
    elif name == "fruit_metric_detail":
        r.update(metric_code=_metric_code(i),
                 metric_name=f"{region[1]}{fruit[1]}{metric_name}",
                 metric_definition=f"{region[1]}{fruit[1]}的{metric_name}；单位{metric_unit}，按会计期统计。",
                 calculation_formula=formula if i % 5 == 0 else
                 ("按本期与上期差额统计" if i % 5 == 1 else ""),
                 setup_purpose="监测水果经营变化",
                 business_anchor_code=f"MANCH{i % counts['fruit_metric_anchor'] + 1:03d}",
                 source_code="FRUIT_DEMO", unit=metric_unit,
                 metric_category=("SALES", "IMPORT_EXPORT", "INVENTORY")[i % 3])
    elif name == "fruit_metric_attr":
        r.update(metric_code=_metric_code(i),
                 metric_formula=formula if i % 5 == 0 else "",
                 metric_category=("SALES", "IMPORT_EXPORT", "INVENTORY")[i % 3],
                 metric_type=("Y", "N", "O")[i % 3],
                 synonym_text=f"{fruit[1]}{metric_name},{metric_name}",
                 unit=metric_unit, valid_period=("M", "Q", "Y")[i % 3],
                 measure_code=_measure_code(i % counts["fruit_measure_def"]),
                 attr_description=f"{metric_name}的公共属性")
    elif name == "fruit_metric_anchor":
        r.update(metric_code=_metric_code(i), anchor_code=f"MANCH{i + 1:03d}",
                 anchor_name=f"{metric_name}业务锚点", synonym_text=metric_name,
                 anchor_description="水果经营指标的业务适用语境",
                 applicable_scenario="全球水果经营分析")
    elif name == "fruit_param_ref_rule":
        kind = SOURCE_TYPES[i % len(SOURCE_TYPES)]
        param_target = "fruit_api_output_param" if i % 2 else "fruit_api_input_param"
        target_index = (i // 2) % counts[param_target]
        param_business_type, param_business_id = _business(target_index, counts)
        param_id = target_index + 1 + (
            counts["fruit_api_input_param"] if i % 2 else 0)
        source_field = {
            "measure": _measure_code(i % counts["fruit_measure_def"]),
            "metric": _metric_code(i % counts["fruit_metric_detail"]),
            "fixedValue": "fruit_state",
            "period": "accounting_period",
            "currency": "currency_code",
        }.get(kind, _referenced_member(dim_index, i, counts)[0])
        values = {
            "fixedValue": ["fresh", "frozen"],
            "period": ["2025Q1", "2025Q2"],
            "currency": ["CNY", "USD"],
        }.get(kind, [_referenced_member(dim_index, i + len(DIMENSIONS), counts)[0]
                     if kind.startswith("DIM") else member_code])
        # Explicitly preserve a small unbound tail for negative examples.
        if i % 101 == 100 and kind in ("measure", "metric"):
            source_field = "UNKNOWN_SYNTHETIC_REFERENCE"
        r.update(business_id=param_business_id, business_type=param_business_type,
                 param_id=str(param_id), source_type=kind,
                 source_field=source_field, field_rule=_json(values),
                 rule_description=f"{param_business_type} 参数引用 {kind} 定义；按业务类型解释。")
    elif name == "fruit_business_rule":
        r.update(rule_name=f"{fruit[1]}库存预警规则", scenario_json=_json({"region": region[0]}),
                 rule_content_json=_json({"inventory_lt_ton": 100 + i % 100}),
                 rule_type="THRESHOLD", rule_description="低于阈值时标注库存风险")
    elif name == "fruit_db_connection":
        r.update(connection_code=f"DEMO{i + 1:03d}", connection_type="GaussDB",
                 connection_name="本地合成水果库", connection_url="offline://synthetic/fruit",
                 account_name="synthetic_no_login", db_name="fruit_market_demo",
                 port="0", ssl_mode="OFF", connection_description="仅生成演示元数据；不是实际连接")
    elif name == "fruit_wide_table_def":
        table_base = ("fruit_sales_summary", "fruit_import_export")[i % 2]
        r.update(physical_table_name=f"{table_base}_{i + 1:03d}",
                 table_cn_name="水果销售汇总表" if i % 2 == 0 else "水果进出口宽表",
                 table_en_name=table_base, table_description="按水果、产区与会计期汇总",
                 grain="fruit_code,region_code,period", refresh_cycle="DAY")
    elif name == "fruit_wide_table_column":
        fields = (
            ("fruit_code", "水果编码", "varchar", fruit[0], ""),
            ("region_code", "产区编码", "varchar", region[0], ""),
            ("period", "会计期", "varchar", "2025Q1", ""),
            ("sales_volume_ton", "销量", "decimal", "120.5", "吨"),
            ("avg_price_cny_per_kg", "均价", "decimal", str(fruit[3]), "元/kg"),
            ("export_yoy", "出口量同比增长", "decimal", "8.2", "%"),
        )
        variant = i // counts["fruit_wide_table_def"]
        field, cn, typ, example, unit = _value(fields, variant)
        physical_field = field if variant < len(fields) else f"{field}_{variant:03d}"
        r.update(physical_field_name=physical_field,
                 field_cn_name=cn if variant < len(fields) else f"{cn}扩展{variant}",
                 field_en_name=field.replace("_", " "), field_type=typ,
                 field_example=example, field_description=f"水果宽表中的{cn}字段", unit=unit)
    elif name == "fruit_synonym_dict":
        synonym = (fruit[1], fruit[2], fruit[2].title())[i % 3]
        if fruit[0] == "APPLE" and i % 40 == 20:
            synonym = "红富士"
        r.update(sn=str(i + 1),
                 word=synonym,
                 type="FRUIT_ALIAS" if synonym != fruit[1] else "FRUIT_NAME",
                 attr=_json({"fruit_code": fruit[0], "synthetic": True}),
                 lang="zh" if synonym in (fruit[1], "红富士") else "en",
                 scope="global_fruit_market")
    elif name == "fruit_import_staging_temp":
        price = fruit[3]
        if fruit[0] == "APPLE":
            price = 3 + (i % 120) / 10  # 3.0-14.9 元/kg
        elif fruit[0] == "BANANA":
            price = 2 + (i % 40) / 10   # 2.0-5.9 元/kg
        r.update(staging_batch=f"BATCH{i // 1000 + 1:05d}", staging_row_no=str(i % 1000 + 1),
                 fruit_code=fruit[0], fruit_cn_name=fruit[1],
                 origin_region_code=region[0], origin_region_name=region[1],
                 channel_code=_value(CHANNELS, i)[0],
                 channel_name=_value(CHANNELS, i)[1],
                 customer_type=_value(CUSTOMERS, i)[0],
                 port_code=_value(PORTS, i)[0], period=f"2025Q{i % 4 + 1}",
                 sales_volume_ton=f"{30 + i % 400:.1f}",
                 avg_price_cny_per_kg=f"{price:.1f}",
                 sales_amount_cny=f"{(30 + i % 400) * 1000 * price:.1f}",
                 import_volume_ton=f"{4 + i % 100:.1f}",
                 export_volume_ton=f"{3 + i % 80:.1f}",
                 inventory_ton=f"{20 + i % 300:.1f}",
                 damaged_volume_ton=f"{i % 7:.1f}",
                 quality_pass_rate=f"{92 + i % 8:.1f}",
                 turnover_rate=f"{1 + (i % 30) / 10:.1f}")
    return {field: r[field] for field in table.fields}


def _type(field):
    if field.endswith("_id") or field in ("sn", "staging_row_no", "port"):
        return "bigint"
    if field in ("avg_price_cny_per_kg", "sales_volume_ton", "sales_amount_cny",
                 "import_volume_ton", "export_volume_ton", "inventory_ton",
                 "damaged_volume_ton", "quality_pass_rate", "turnover_rate"):
        return "numeric(18,2)"
    if field.endswith("_date") or field.endswith("_time"):
        return "timestamp"
    if field.endswith("_json") or field == "field_rule" or field == "attr":
        return "text"
    return "character varying(255)"


def scaled_counts(scale):
    if scale <= 0 or not math.isfinite(scale):
        raise ValueError("scale must be positive and finite")
    return {t.name: max(min(10, t.count), math.ceil(t.count * scale)) for t in TABLES}


def generate(root: Path, scale: float = 1.0):
    root = Path(root)
    if root.exists():
        raise FileExistsError(f"Output already exists: {root}")
    counts = scaled_counts(scale)
    for folder in ("data", "schema/tables", "schema/constraints", "schema/foreign_keys"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    for table in TABLES:
        base = {"schema": SCHEMA, "table_name": table.name}
        columns = [
            {"column_name": field, "ordinal_position": order, "data_type": _type(field),
             "is_not_null": field == table.pk, "default_value": None,
             "column_comment": COMMENTS.get(field, field.replace("_", " ") + "，合成业务字段")}
            for order, field in enumerate(table.fields, 1)
        ]
        metadata = {**base, "table_comment": table.comment, "relkind": "r",
                    "estimated_rows": counts[table.name], "total_size": None,
                    "data_size": None, "columns": columns}
        constraint = {**base, "constraints": ([{
            "constraint_name": table.name + "_pkey", "constraint_type": "p",
            "constraint_type_name": "PRIMARY KEY",
            "definition": f"PRIMARY KEY ({table.pk})"}] if table.pk else [])}
        for folder, content in (("tables", metadata), ("constraints", constraint),
                                ("foreign_keys", {**base, "foreign_keys": []})):
            path = root / "schema" / folder / f"{table.name}.yaml"
            path.write_text(yaml.safe_dump(content, allow_unicode=True, sort_keys=False),
                            encoding="utf-8")
        with (root / "data" / f"{table.name}.csv").open("w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fieldnames=table.fields)
            writer.writeheader()
            for i in range(counts[table.name]):
                writer.writerow(make_row(table, i, counts))
        print(f"{table.name}: {counts[table.name]:,} rows", flush=True)
    manifest = {
        "synthetic": True,
        "schema": SCHEMA,
        "source": "Hand-coded deterministic fixture from user-supplied fruit-domain prompt; no real data",
        "table_count": len(TABLES),
        "row_count": sum(counts.values()),
        "counts": counts,
        "assumption": "Prompt lists 22 tables; fruit_import_staging_temp is the synthetic 23rd table",
        "semantic_status": "Test fixture only; not ground truth for ontology quality",
    }
    (root / "synthetic_manifest.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return validate(root)


def _formula_parseable(text):
    if "=" not in text:
        return False
    try:
        node = ast.parse(text.split("=", 1)[1].strip(), mode="eval")
    except SyntaxError:
        return False
    return all(isinstance(item, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name,
                                 ast.Load, ast.Constant, ast.Add, ast.Sub, ast.Mult,
                                 ast.Div, ast.UAdd, ast.USub)) for item in ast.walk(node))


def validate(root: Path):
    root = Path(root)
    expected = {t.name for t in TABLES}
    for folder in ("tables", "constraints", "foreign_keys"):
        actual = {p.stem for p in (root / "schema" / folder).glob("*.yaml")}
        if actual != expected:
            raise ValueError(f"schema/{folder}: expected {len(expected)} tables, got {sorted(actual)}")
    actual_csv = {p.stem for p in (root / "data").glob("*.csv")}
    if actual_csv != expected:
        raise ValueError("data CSV tables differ from schema tables")
    rows, source_types, business_types, dim_types, formulas = {}, set(), set(), set(), [0, 0]
    sample_refs = {"dim_code": set(), "metric_code": set(), "measure_code": set()}
    target_keys = {name: set() for name in (
        "fruit_market_api", "fruit_dashboard_card", "fruit_wide_table_def",
        "fruit_dim_definition", "fruit_metric_detail", "fruit_measure_def")}
    member_keys = set()
    param_targets = {}
    parameter_references_checked = 0
    dim_rule_values_checked = 0
    observed_business = set()
    source_fields = {kind: set() for kind in SOURCE_TYPES}
    for table in TABLES:
        base = root / "schema"
        metadata = yaml.safe_load((base / "tables" / f"{table.name}.yaml").read_text(encoding="utf-8"))
        constraints = yaml.safe_load((base / "constraints" / f"{table.name}.yaml").read_text(encoding="utf-8"))
        fk = yaml.safe_load((base / "foreign_keys" / f"{table.name}.yaml").read_text(encoding="utf-8"))
        fields = [col["column_name"] for col in metadata["columns"]]
        if fields != table.fields or len(fields) != table.expected_columns:
            raise ValueError(f"Column mismatch: {table.name}")
        if fk["foreign_keys"] != []:
            raise ValueError(f"Declared FK in {table.name}")
        actual_pk = constraints["constraints"]
        if bool(actual_pk) != bool(table.pk):
            raise ValueError(f"PK mismatch: {table.name}")
        if table.pk and actual_pk[0]["definition"] != f"PRIMARY KEY ({table.pk})":
            raise ValueError(f"PK definition mismatch: {table.name}")
        count = 0
        seen_pk = set()
        with (root / "data" / f"{table.name}.csv").open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != fields:
                raise ValueError(f"CSV header mismatch: {table.name}")
            for row in reader:
                count += 1
                if table.pk:
                    pk = row[table.pk]
                    if not pk or pk in seen_pk:
                        raise ValueError(f"Duplicate/empty PK in {table.name}: {pk}")
                    seen_pk.add(pk)
                    if table.name in ("fruit_market_api", "fruit_dashboard_card", "fruit_wide_table_def"):
                        target_keys[table.name].add(pk)
                    if table.name in ("fruit_api_input_param", "fruit_api_output_param"):
                        if pk in param_targets:
                            raise ValueError(f"Ambiguous parameter ID across input/output: {pk}")
                        param_targets[pk] = (row["business_type"], row["business_id"])
                if "business_type" in row:
                    business_types.add(row["business_type"])
                    observed_business.add((row["business_type"], row["business_id"]))
                if "source_type" in row:
                    kind = row["source_type"]
                    if kind not in source_fields:
                        raise ValueError(f"Unexpected source_type: {kind}")
                    source_types.add(kind)
                    source_fields[kind].add(row["source_field"])
                    scope = param_targets.get(row["param_id"])
                    if scope is None or scope != (row["business_type"], row["business_id"]):
                        raise ValueError(f"Parameter reference missing or scope mismatch: {row['fruit_ref_rule_id']}")
                    parameter_references_checked += 1
                    if kind.startswith("DIM"):
                        try:
                            allowed = json.loads(row["field_rule"])
                        except (TypeError, json.JSONDecodeError) as exc:
                            raise ValueError(f"Dimension field_rule is not JSON: {row['fruit_ref_rule_id']}") from exc
                        if (not isinstance(allowed, list) or not allowed or
                                any(not isinstance(value, str) or not value for value in allowed)):
                            raise ValueError(f"Dimension field_rule must be a nonempty string list: {row['fruit_ref_rule_id']}")
                        if any((kind, value) not in member_keys for value in allowed):
                            raise ValueError(f"Dimension field_rule contains unknown member: {row['fruit_ref_rule_id']}")
                        dim_rule_values_checked += len(allowed)
                if "dim_type" in row:
                    dim_types.add(row["dim_type"])
                if table.name == "fruit_dim_definition":
                    target_keys[table.name].add(row["dim_code"])
                elif table.name == "fruit_dim_member":
                    member_keys.add((row["dim_code"], row["member_code"]))
                elif table.name == "fruit_metric_detail":
                    target_keys[table.name].add(row["metric_code"])
                elif table.name == "fruit_measure_def":
                    target_keys[table.name].add(row["measure_code"])
                if table.name == "fruit_metric_detail":
                    formulas[1] += 1
                    formulas[0] += _formula_parseable(row["calculation_formula"])
                for key in sample_refs:
                    if key in row and row[key] and len(sample_refs[key]) < 20:
                        sample_refs[key].add(row[key])
                if table.name == "fruit_import_staging_temp":
                    price = float(row["avg_price_cny_per_kg"])
                    if row["fruit_code"] == "APPLE" and not 3 <= price <= 15:
                        raise ValueError("Apple price outside 3-15 元/kg")
                    if row["fruit_code"] == "BANANA" and not 2 <= price <= 6:
                        raise ValueError("Banana price outside 2-6 元/kg")
        rows[table.name] = count
    if not set(SOURCE_TYPES) <= source_types:
        raise ValueError(f"Missing source_type values: {set(SOURCE_TYPES) - source_types}")
    if not {"API", "CARD", "TABLE"} <= business_types:
        raise ValueError("Missing business_type values")
    if not {"DIM_PROD", "DIM_REGION", "DIM_CUST"} <= dim_types:
        raise ValueError("Missing dim_type values")
    business_targets = {"API": "fruit_market_api", "CARD": "fruit_dashboard_card",
                        "TABLE": "fruit_wide_table_def"}
    invalid_business = {(kind, identifier) for kind, identifier in observed_business
                        if identifier not in target_keys[business_targets[kind]]}
    if invalid_business:
        raise ValueError(f"Polymorphic business_id has missing targets: {sorted(invalid_business)[:5]}")
    if not {f"DIM{i:04d}" for i in range(1, 6)} <= target_keys["fruit_dim_definition"]:
        raise ValueError("Canonical dimension definitions missing")
    for kind in SOURCE_TYPES:
        if not kind.startswith("DIM"):
            continue
        member_codes = {member for dim, member in member_keys if dim == kind}
        if not source_fields[kind] or not source_fields[kind] <= member_codes:
            raise ValueError(f"Dimension references missing targets: {kind}")
    for kind, table_name in (("measure", "fruit_measure_def"),
                             ("metric", "fruit_metric_detail")):
        valid = source_fields[kind] & target_keys[table_name]
        if not valid:
            raise ValueError(f"No valid {kind} references")
        # The fixture includes explicit unresolved references; they must not
        # be silently treated as valid joins.
        invalid = source_fields[kind] - target_keys[table_name]
        if invalid - {"UNKNOWN_SYNTHETIC_REFERENCE"}:
            raise ValueError(f"Unexpected unresolved {kind} references: {sorted(invalid)[:5]}")
    if formulas[0] < math.ceil(formulas[1] * 0.1):
        raise ValueError("Fewer than 10% parseable metric formulas")
    manifest = yaml.safe_load((root / "synthetic_manifest.yaml").read_text(encoding="utf-8"))
    if rows != manifest["counts"] or sum(rows.values()) != manifest["row_count"]:
        raise ValueError("CSV row counts differ from manifest")
    result = {"table_count": len(TABLES), "row_count": sum(rows.values()),
              "parseable_formulas": formulas[0], "metric_detail_rows": formulas[1],
              "source_types": sorted(source_types), "business_types": sorted(business_types),
              "polymorphic_business_targets_checked": len(observed_business),
              "parameter_references_checked": parameter_references_checked,
              "dimension_rule_values_checked": dim_rule_values_checked,
              "valid_metric_reference_codes": len(source_fields["metric"] & target_keys["fruit_metric_detail"]),
              "valid_measure_reference_codes": len(source_fields["measure"] & target_keys["fruit_measure_def"]),
              "sampled_codes": {k: sorted(v) for k, v in sample_refs.items()}}
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("generate")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--scale", type=float, default=1.0,
                        help="1.0 writes 1,102,238 rows; 0.001 is a small smoke dataset")
    check = subparsers.add_parser("validate")
    check.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "generate":
        generate(args.output, args.scale)
    else:
        validate(args.input)


if __name__ == "__main__":
    main()
