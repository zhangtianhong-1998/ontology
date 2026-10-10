"""Versioned synthetic data with explicit quantity definitions and consistent references.

The legacy v2 generator and archived inputs remain unchanged. This fixture uses
ordinary table contents, never a hidden ontology answer file in the extractor.
"""
from __future__ import annotations

import argparse
import ast
import csv
import json
from pathlib import Path

import generate_fruit_data as base

# symbol, reusable name, unit, calculation/aggregation, complete definition
QUANTITIES = (
    ("sales_volume", "销量", "吨", "SUM", "销量是成交数量的求和，计量单位为吨。"),
    ("avg_price", "均价", "元/kg", "AVG", "均价是单位质量成交价格的算术平均值，计量单位为元/kg。"),
    ("this_year_export", "当年出口量", "吨", "SUM", "当年出口量是当年出口数量的求和，计量单位为吨。"),
    ("last_year_export", "上年出口量", "吨", "SUM", "上年出口量是上一年度出口数量的求和，计量单位为吨。"),
    ("current_import_price", "本期进口均价", "元/kg", "AVG", "本期进口均价是本期进口单位质量价格的算术平均值。"),
    ("previous_import_price", "上期进口均价", "元/kg", "AVG", "上期进口均价是前一可比期间进口单位质量价格的算术平均值。"),
    ("avg_stock", "平均库存量", "吨", "AVG", "平均库存量是本期各日库存数量的算术平均值，计量单位为吨。"),
    ("damaged_volume", "损耗量", "吨", "SUM", "损耗量是本期损坏数量的求和，计量单位为吨。"),
    ("inbound_volume", "入库量", "吨", "SUM", "入库量是本期入库数量的求和，计量单位为吨。"),
    ("max_price", "最高均价", "元/kg", "MAX", "最高均价是同一期间同一对象各地区均价的最大值。"),
    ("min_price", "最低均价", "元/kg", "MIN", "最低均价是同一期间同一对象各地区均价的最小值。"),
    ("qualified_quantity", "合格数量", "件", "SUM", "合格数量是按指定检验标准判定合格的受检件数之和。"),
    ("tested_quantity", "受检数量", "件", "SUM", "受检数量是按同一检验标准接受检验的全部件数之和。"),
    ("qualified_rate", "合格率", "%", "RATIO", "合格率=qualified_quantity / tested_quantity * 100；受检数量为零时结果缺失，分子分母使用同一检验标准。"),
    ("revenue", "收入", "元", "SUM", "收入是同一核算口径内已确认收入金额的求和，采用人民币元。"),
    ("cost", "成本", "元", "SUM", "成本是同一核算口径内已确认成本金额的求和，采用人民币元。"),
    ("production_volume", "总产量", "吨", "SUM", "总产量是同一统计期间产出数量的求和，计量单位为吨。"),
    ("import_volume", "进口量", "吨", "SUM", "进口量是同一统计期间进口数量的求和，计量单位为吨。"),
    ("export_volume", "出口量", "吨", "SUM", "出口量是同一统计期间出口数量的求和，计量单位为吨。"),
    ("stock_volume", "库存量", "吨", "SUM", "库存量是同一统计时点现存数量的求和，计量单位为吨。"),
    ("loss_rate", "损耗率", "%", "RATIO", "损耗率=damaged_volume / inbound_volume * 100；入库量为零时结果缺失。"),
    ("turnover_rate", "周转率", "次/期", "RATIO", "周转率=sales_volume / avg_stock；平均库存量为零时结果缺失。"),
    ("sales_amount", "销售额", "元", "MULTIPLY", "销售额=sales_volume * avg_price * 1000；吨先换算为千克。"),
    ("annual_budget", "当年预算", "元", "SUM", "当年预算是当前年度预算金额的求和。"),
    ("october_budget", "10月年预算", "元", "FILTER", "10月年预算是当前年度预算版本中会计月为10月的预算金额之和。"),
    ("annual_budget_rank", "当年预算排名", "名次", "RANK", "当年预算排名是同一比较范围内当年预算金额降序的稠密排名。"),
)
# title, unit, formula, underlying quantity name, business purpose
INDICATORS = (
    ("水果销售总额", "元", "fruit_sales_total = sales_volume * avg_price * 1000", "销售额", "销售规模分析"),
    ("水果出口量同比增长", "%", "fruit_export_yoy = (this_year_export - last_year_export) / last_year_export * 100", "当年出口量", "出口增长分析"),
    ("水果进口均价波动", "%", "fruit_import_change = (current_import_price - previous_import_price) / previous_import_price * 100", "本期进口均价", "进口价格分析"),
    ("水果库存周转率", "次/期", "fruit_turnover = sales_volume / avg_stock", "周转率", "库存效率分析"),
    ("水果损耗率", "%", "fruit_loss_rate = damaged_volume / inbound_volume * 100", "损耗率", "损耗管理"),
    ("水果区域均价差异", "元/kg", "fruit_price_gap = max_price - min_price", "均价", "区域价格分析"),
    ("水果合格率", "%", "fruit_qualified_rate = qualified_rate", "合格率", "质量经营分析"),
    ("水果经营利润", "元", "fruit_profit = revenue - cost", "收入", "经营盈利分析"),
)
QUANTITY_INDEX = {q[0]: i for i, q in enumerate(QUANTITIES)}
NAME_INDEX = {q[1]: i for i, q in enumerate(QUANTITIES)}

COMMENTS = {
    ("fruit_measure_def", "standard_name"): "不绑定具体经营对象的可复用度量标准名称",
    ("fruit_measure_def", "parameter_name"): "度量英文别名，也是计算公式中的变量名；不同版本可重复使用",
    ("fruit_measure_def", "source_field"): "量的计算来源；表达式表示该度量的计算公式，空值表示基础聚合量",
    ("fruit_measure_def", "inference_type"): "计算方法，例如 SUM、AVG、RATIO；是计算属性，不是单独本体类型",
    ("fruit_measure_def", "period"): "口径明确限定的期间粒度；空值表示由指标的期间绑定决定",
    ("fruit_metric_detail", "metric_definition"): "指标定义或配置说明；含经营对象、度量口径；实例限定不能当作新类",
    ("fruit_metric_detail", "metric_code"): "此行指标定义或指标配置的唯一编码",
    ("fruit_metric_attr", "metric_code"): "引用 fruit_metric_detail.metric_code，公共属性不独立拥有指标定义",
    ("fruit_dashboard_card", "card_name"): "具体看板实例的名称，不是本体类型名",
    ("fruit_dashboard_card", "card_description"): "看板类型说明及本条具体实例的展示用途",
    ("fruit_dashboard_card", "card_config_json"): "已配置实例的指标引用及维度选择器，不枚举未配置组合",
    ("fruit_param_ref_rule", "source_type"): "引用类型：维度编码指定维度定义；measure指度量，metric指指标；其余为字面值规则",
    ("fruit_param_ref_rule", "source_field"): "measure引用measure_code，metric引用metric_code，维度类型引用member_code",
    ("fruit_param_ref_rule", "field_rule"): "在source_type指定维度内允许的成员列表，或固定值列表；不表示全量观测成员",
}


def metric_row(i):
    title, unit, formula, quantity, purpose = INDICATORS[i % len(INDICATORS)]
    fruit = base.FRUITS[(i // len(INDICATORS)) % len(base.FRUITS)]
    region = base.REGIONS[(i // (len(INDICATORS) * len(base.FRUITS))) % len(base.REGIONS)]
    definition = (f"{title}是用于{purpose}的指标；经营对象为水果，所用度量为{quantity}。"
                  "水果是一类可供经营的可食用果实，苹果、香蕉等是其成员。"
                  "统计口径由计算公式给出，可按水果类别、产区和会计期观察。")
    label = title
    if i >= len(INDICATORS):
        label = f"{region[1]}{fruit[1]}{title.removeprefix('水果')}"
        definition += f" 本记录是{title}的配置对象：水果类别={fruit[1]}，产区={region[1]}；不另定义计算口径。"
    return label, definition, formula, unit, quantity, fruit, region


def coherent_row(table, i, counts):
    row = base.make_row(table, i, counts)
    name = table.name
    metric_index = i % counts["fruit_metric_detail"]
    label, definition, formula, unit, quantity, fruit, region = metric_row(metric_index)
    if "domain_code" in row:
        row["domain_code"] = "FRUIT"
    if "business_domain" in row:
        row["business_domain"] = "水果经营"
    if name == "fruit_measure_def":
        symbol, title, quantity_unit, operation, description = QUANTITIES[i % len(QUANTITIES)]
        formula_text = description.split("；", 1)[0] if "=" in description else ""
        row.update(measure_name=title, standard_name=title, parameter_name=symbol,
                   measure_description=description + " 这是可复用度量定义，不限定具体经营对象、产品、地区或实际期间。",
                   unit=quantity_unit, inference_type=operation, source_field=formula_text,
                   metric_code="", period="Y" if symbol == "annual_budget" else
                   "M" if symbol == "october_budget" else "Y" if symbol == "annual_budget_rank" else "")
        for flag in ("budget", "forecast", "actual", "target", "remain", "estimate", "rank"):
            row[flag + "_flag"] = str(int(flag == ("rank" if operation == "RANK" else "budget" if "budget" in symbol else "actual")))
    elif name == "fruit_metric_detail":
        row.update(metric_name=label, metric_definition=definition, calculation_formula=formula,
                   setup_purpose=INDICATORS[metric_index % len(INDICATORS)][4],
                   metric_category="FRUIT", source_code="FRUIT_DEMO")
        row["unit"] = unit
    elif name == "fruit_metric_attr":
        row.update(metric_formula=formula, metric_category="FRUIT", metric_type="Y",
                   synonym_text=label, unit=unit, valid_period="",
                   measure_code=base._measure_code(NAME_INDEX[quantity]),
                   attr_description=f"本配置引用指标{base._metric_code(metric_index)}和度量{base._measure_code(NAME_INDEX[quantity])}；度量提供指标的量定义，实例取值不改变定义。")
    elif name == "fruit_dashboard_card":
        row.update(card_name=label + "看板", card_description=(
            "看板是一类展示指标及维度选择条件的分析界面。"
            f"本记录是一个具体看板实例，展示{label}；不是新的看板类型。"),
            metric_code=base._metric_code(metric_index), measure_name=quantity,
            dim_code="DIM0001", region_code=region[0], fruit_category_code=fruit[0],
            analysis_period="2025Q1", positive_description="变化方向需结合指标含义判断",
            negative_description="变化方向需结合指标含义判断",
            card_config_json=base._json({"metric_code": base._metric_code(metric_index),
                "selectors": {"DIM0001": [fruit[0]], "DIM0002": [region[0]]}, "period": "2025Q1"}))
    elif name == "fruit_dim_definition":
        dimension = base.DIMENSIONS[i % 5]
        row.update(dim_cn_name=dimension[1], dim_en_name=("fruit_category", "region", "channel", "customer", "port")[i % 5],
                   dim_description=f"{dimension[1]}是用于区分经营观测坐标的维度定义；具体成员不改变维度类型。",
                   synonym_text=dimension[1], filter_sql="")
    elif name in ("fruit_dim_member", "fruit_org_dim_data"):
        # A member's own region/fruit must agree with its code; unrelated metadata
        # is empty, not a randomly assigned conflicting member from another row.
        row["region_code"] = row["member_code"] if row["dim_code"] == "DIM0002" else ""
        row["fruit_category_code"] = row["member_code"] if row["dim_code"] == "DIM0001" else ""
    elif name == "fruit_param_ref_rule":
        if row["source_type"] in ("measure", "metric"):
            row["field_rule"] = "[]"
    elif name == "fruit_business_rule":
        row.update(rule_name="水果库存预警", rule_description="库存预警规则实例，库存量低于配置阈值时提示风险。")
    elif name == "fruit_market_api":
        row["api_description"] += " API是一类对外提供数据查询的服务资源；本记录是已配置的接口实例。"
    return row


def validate_semantics(root):
    """Check relationships independently from the expected ontology output."""
    root = Path(root)
    def read(table):
        with (root / "data" / f"{table}.csv").open(newline="", encoding="utf-8") as f:
            yield from csv.DictReader(f)
    measures = list(read("fruit_measure_def"))
    symbols = {r["parameter_name"] for r in measures}
    names = {r["measure_name"] for r in measures}
    by_code = {r["measure_code"]: r for r in measures}
    formulas = 0
    for table, field in (("fruit_metric_detail", "calculation_formula"), ("fruit_metric_attr", "metric_formula"), ("fruit_measure_def", "source_field")):
        for row in read(table):
            expr = row[field]
            if not expr:
                continue
            rhs = expr.split("=", 1)[-1].strip()
            tree = ast.parse(rhs, mode="eval")
            unknown = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - symbols
            if unknown:
                raise ValueError(f"Unbound formula symbols in {table}: {sorted(unknown)}")
            formulas += 1
    metrics = {r["metric_code"]: r for r in read("fruit_metric_detail")}
    for row in read("fruit_metric_attr"):
        source = metrics.get(row["metric_code"])
        if source is None or source["calculation_formula"] != row["metric_formula"] or source["unit"] != row["unit"]:
            raise ValueError("Metric attributes conflict with their definition")
        if row["measure_code"] not in by_code:
            raise ValueError("Metric attribute measure reference has no definition")
    for row in read("fruit_dashboard_card"):
        if row["metric_code"] not in metrics or row["measure_name"] not in names:
            raise ValueError("Dashboard references have no definitions")
    for row in measures:
        if row["measure_name"] == "合格率" and (row["parameter_name"] != "qualified_rate" or "tested_quantity" not in row["source_field"]):
            raise ValueError("Qualification rate has an inconsistent calculation")
    report = {"semantic_fixture_version": 3, "formulas_with_resolved_symbols": formulas,
              "distinct_quantity_symbols": len(symbols), "metric_definition_rows": len(metrics),
              "checks": ["formula_symbols", "metric_attribute_formula_unit", "quantity_references", "qualification_formula"],
              "boundary": "Input consistency only; not extraction accuracy or an ontology gold standard"}
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report


def generate(root, scale=1.0):
    result = base.generate(root, scale, row_factory=coherent_row, column_comments=COMMENTS,
        minimum_counts={"fruit_measure_def": len(QUANTITIES), "fruit_metric_detail": len(INDICATORS)},
        manifest_extra={"semantic_fixture_version": 3,
            "semantic_changes": ["reusable_quantity_definitions", "formula_symbol_aliases", "metric_configuration_bindings", "resource_instance_declarations", "consistent_dimension_member_codes"],
            "comparison_boundary": "Semantic input changed from v2; do not attribute all output differences to algorithm changes"})
    return {**result, **validate_semantics(root)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generate", "validate"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args()
    if args.command == "generate":
        if args.output is None:
            parser.error("generate requires --output")
        generate(args.output, args.scale)
    else:
        if args.input is None:
            parser.error("validate requires --input")
        base.validate(args.input)
        validate_semantics(args.input)


if __name__ == "__main__":
    main()
