"""Generate a tiny independent semantic control; never edits the fruit experiment.

Run from route2 with `.venv/bin/python scripts/generate_semantic_control.py`.
The extraction input contains declarations and source records only. Expectations
live separately in docs/route2/SEMANTIC_CONTROL_ACCEPTANCE.md.
"""

import argparse
import csv
from pathlib import Path

import yaml


SCHEMA = "semantic_control"
TABLES = [
    ("general_measure_definition", "可复用度量定义，不限定经营对象", "measure_key", {
        "measure_key": "度量定义标识，用于公式操作数绑定",
        "measure_name": "通用量名称", "definition": "度量的完整定义和计算口径",
        "calculation_formula": "在事实表金额字段上执行的聚合表达式",
        "unit": "结果单位", "applicability_scope": "度量适用范围",
    }, [
        {"measure_key": "revenue", "measure_name": "收入",
         "definition": "收入是收入金额的合计，对事实记录的 revenue_amount 字段求和；不限定经营对象、地区或年份。",
         "calculation_formula": "sum(revenue_amount)", "unit": "元",
         "applicability_scope": "不限定经营对象、地区或年份"},
        {"measure_key": "cost", "measure_name": "成本",
         "definition": "成本是成本金额的合计，对事实记录的 cost_amount 字段求和；不限定经营对象、地区或年份。",
         "calculation_formula": "sum(cost_amount)", "unit": "元",
         "applicability_scope": "不限定经营对象、地区或年份"},
    ]),
    ("business_metric_definition", "指标定义，名称、公式、适用范围均在本行声明", "metric_key", {
        "metric_key": "指标定义标识", "metric_name": "指标业务名称",
        "definition": "指标自身的完整业务定义",
        "calculation_formula": "指标自身公式；revenue 与 cost 分别引用 measure_key 的同值定义",
        "unit": "指标结果单位", "business_scope": "适用经营对象",
        "region_scope": "适用地区范围", "period_scope": "适用期间粒度",
    }, [
        {"metric_key": "apple_profit", "metric_name": "苹果经营利润",
         "definition": "苹果经营利润是苹果经营业务的收入减去成本。revenue 表示收入，cost 表示成本；按地区和公历年分别计算。",
         "calculation_formula": "revenue - cost", "unit": "元", "business_scope": "苹果",
         "region_scope": "全国", "period_scope": "公历年"},
        {"metric_key": "apple_profit_after_charge", "metric_name": "苹果经营利润",
         "definition": "本定义的苹果经营利润在收入减去成本后，另扣除该差额的 10% 专项管理费；与未扣费定义不同。revenue 表示收入，cost 表示成本。",
         "calculation_formula": "(revenue - cost) * 0.9", "unit": "元", "business_scope": "苹果",
         "region_scope": "全国", "period_scope": "公历年"},
        {"metric_key": "apple_profit_east", "metric_name": "苹果经营利润",
         "definition": "苹果经营利润是苹果经营业务的收入减去成本。本定义仅适用于华东地区；revenue 表示收入，cost 表示成本。",
         "calculation_formula": "revenue - cost", "unit": "元", "business_scope": "苹果",
         "region_scope": "仅华东", "period_scope": "公历年"},
        {"metric_key": "banana_profit", "metric_name": "香蕉经营利润",
         "definition": "香蕉经营利润是香蕉经营业务的收入减去成本。revenue 表示收入，cost 表示成本；按地区和公历年分别计算。",
         "calculation_formula": "revenue - cost", "unit": "元", "business_scope": "香蕉",
         "region_scope": "全国", "period_scope": "公历年"},
    ]),
    ("dimension_definition", "地区与公历年维度定义", "dimension_key", {
        "dimension_key": "维度定义标识", "dimension_name": "维度名称",
        "definition": "维度定义", "scope": "适用范围",
    }, [
        {"dimension_key": "region", "dimension_name": "地区",
         "definition": "经营事实所属的中国省级行政区域；本数据只列出北京和上海，不声明区域上下钻层级。", "scope": "中国省级行政区域"},
        {"dimension_key": "calendar_year", "dimension_name": "公历年",
         "definition": "经营事实所属的公历年份，使用四位年份编码；本数据不提供季度或月份明细。", "scope": "公历年"},
    ]),
    ("dimension_member", "维度取值记录，分别指向地区或年份定义", "member_key", {
        "member_key": "维度取值记录标识", "catalog_pointer": "所属维度的 dimension_key 标识",
        "member_code": "维度取值编码", "member_name": "维度取值名称",
    }, [
        {"member_key": "region_beijing", "catalog_pointer": "region", "member_code": "BJ", "member_name": "北京"},
        {"member_key": "region_shanghai", "catalog_pointer": "region", "member_code": "SH", "member_name": "上海"},
        {"member_key": "year_2025", "catalog_pointer": "calendar_year", "member_code": "2025", "member_name": "2025年"},
        {"member_key": "year_2026", "catalog_pointer": "calendar_year", "member_code": "2026", "member_name": "2026年"},
    ]),
    ("operating_fact", "已观察到的苹果经营年度事实，每行是一组已记录的地区和年份", "observation_key", {
        "observation_key": "事实记录标识", "metric_ref_key": "采用的 business_metric_definition.metric_key 标识",
        "business_object": "经营对象维度取值", "location_key": "地区维度取值编码，对应 member_code",
        "year": "公历年份维度取值，对应 member_code", "revenue_amount": "收入金额，单位元",
        "cost_amount": "成本金额，单位元", "apple_profit_amount": "苹果经营利润金额，单位元",
    }, [
        {"observation_key": "obs_bj_2025", "metric_ref_key": "apple_profit", "business_object": "苹果",
         "location_key": "BJ", "year": "2025", "revenue_amount": "1000", "cost_amount": "600", "apple_profit_amount": "400"},
        {"observation_key": "obs_bj_2026", "metric_ref_key": "apple_profit", "business_object": "苹果",
         "location_key": "BJ", "year": "2026", "revenue_amount": "1200", "cost_amount": "800", "apple_profit_amount": "400"},
        {"observation_key": "obs_sh_2025", "metric_ref_key": "apple_profit", "business_object": "苹果",
         "location_key": "SH", "year": "2025", "revenue_amount": "700", "cost_amount": "550", "apple_profit_amount": "150"},
    ]),
]


def generate(output):
    """Create one new dataset with deterministic records and no declared FK."""
    output.mkdir(parents=True, exist_ok=False)
    for table, comment, key, columns, rows in TABLES:
        base = {"schema": SCHEMA, "table_name": table}
        metadata = {
            "tables": {**base, "table_comment": comment, "relkind": "r", "estimated_rows": len(rows),
                       "columns": [{"column_name": name, "ordinal_position": i + 1,
                                    "data_type": "numeric" if name.endswith("_amount") else "text",
                                    "is_not_null": True, "default_value": None, "column_comment": text}
                                   for i, (name, text) in enumerate(columns.items())]},
            "constraints": {**base, "constraints": [{"constraint_name": table + "_pk", "constraint_type": "p",
                "constraint_type_name": "PRIMARY KEY", "definition": f"PRIMARY KEY ({key})"}]},
            "foreign_keys": {**base, "foreign_keys": []},
        }
        for kind, document in metadata.items():
            path = output / "schema" / kind / (table + ".yaml")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
        path = output / "data" / (table + ".csv")
        path.parent.mkdir(exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
    return {"tables": len(TABLES), "records": sum(len(t[-1]) for t in TABLES), "output": str(output.resolve())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "local_data/semantic_contract_control")
    print(generate(parser.parse_args().output))
