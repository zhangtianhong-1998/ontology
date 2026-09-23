"""Synthetic fixtures only. Business names never enter the extraction engine."""
import csv
from pathlib import Path

from .storage import write_yaml


def make_demo(root: Path, rows=8, scenario="linked"):
    if root.exists():
        raise FileExistsError(root)
    if rows < 1:
        raise ValueError("rows must be positive")
    root.mkdir(parents=True)
    catalog = [("A", "north", "条目甲"), ("A", "south", "南区条目甲"), ("B", "north", "条目乙"), ("D", "north", "重复定义一"), ("D", "north", "重复定义二")]
    patterns = [("north", "linked", "A"), ("south", "linked", "A"), ("north", "unrelated", "B"), ("north", "linked", "Z"), ("north", "", "A"), ("north", "linked", "D"), ("north", "linked", ""), ("north", "linked", "B")]
    source_cols = {"rid": "本文件内记录编码", "namespace": "定义命名空间", "kind": "只有 linked 表示引用，其他记录不建立引用", "ref": "引用 catalog.code；namespace 必须一致；kind=linked 才适用", "description": "记录说明"}
    target_cols = {"code": "定义编码，不保证单独唯一", "namespace": "编码命名空间", "title": "定义名称"}
    source_rows = ((str(i + 1), *patterns[i % len(patterns)], "合成条目 " + str(i + 1)) for i in range(rows))
    predicate, mode, root_type = "points_to", "identifier", "GeneralObject"
    if scenario == "formula":
        catalog = [("X", "north", "输入甲"), ("Y", "north", "输入乙")]
        patterns = [("north", "linked", "X - Y"), ("north", "linked", "X + mystery(Y)")]
        source_rows = ((str(i + 1), *patterns[i % 2], "合成表达式 " + str(i + 1)) for i in range(rows))
        source_cols["ref"] = "公式表达式，符号引用同 namespace 的 catalog.code；未知函数语义不可推断"
        predicate, mode, root_type = "calculation_depends_on", "formula", "Metric"
    if scenario == "unrelated":
        source_cols["ref"] = "独立业务编码，与其他表无引用关系"
        source_cols["kind"] = "记录的内部状态，不指明表间关联"

    def table(name, columns, values, comment, pk=None):
        directory = root / "data"
        directory.mkdir(exist_ok=True)
        with (directory / (name + ".csv")).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(values)
        base = {"schema": "demo", "table_name": name}
        write_yaml(root / "schema/tables" / (name + ".yaml"), {**base, "table_comment": comment, "relkind": "r", "estimated_rows": None, "total_size": None, "data_size": None, "columns": [{"column_name": c, "ordinal_position": i + 1, "data_type": "text", "is_not_null": False, "default_value": None, "column_comment": text} for i, (c, text) in enumerate(columns.items())]})
        constraints = [{"constraint_name": name + "_pkey", "constraint_type": "p", "constraint_type_name": "PRIMARY KEY", "definition": f"PRIMARY KEY ({pk})"}] if pk else []
        write_yaml(root / "schema/constraints" / (name + ".yaml"), {**base, "constraints": constraints})
        write_yaml(root / "schema/foreign_keys" / (name + ".yaml"), {**base, "foreign_keys": []})

    table("catalog", target_cols, catalog, "合成定义条目；包含命名空间和可能重复的编码")
    table("records", source_cols, source_rows, "合成记录，仅用于测试", "rid")
    relation = {"id": "synthetic_reference", "source_table": "demo.records", "target_table": "demo.catalog", "mode": mode, "source_column": "ref", "target_column": "code", "scope_bindings": {"namespace": "namespace"}, "selector": {"op": "eq", "field": "kind", "value": "linked"}, "predicate": predicate, "evidence_ids": ["schema:demo.records:ref"]}
    plan = {"object_types": [], "relation_types": [], "tables": [{"table": "demo.catalog", "object_type": "Measure" if scenario == "formula" else "GeneralObject", "label_column": "title", "attributes": {"name": "title"}, "evidence_ids": ["schema:demo.catalog"]}, {"table": "demo.records", "object_type": root_type, "label_column": "description", "attributes": {"description": "description"}, "evidence_ids": ["schema:demo.records"]}], "relations": [] if scenario == "unrelated" else [relation], "knowledge_questions": ["ref 的命名空间关联规则"] if scenario == "linked" else []}
    if scenario == "formula":
        plan["relation_types"] = [{"id": predicate, "parent": "depends_on", "definition": "由源公式明确引用目标定义", "evidence_ids": ["schema:demo.records:ref"]}]
    quote = "ref 只引用相同 namespace 中的 code；重复编码不能自动选第一条。"
    claim = {"document_id": "join-rule", "quote": quote, "statement": quote, "scope": "本合成样本"}
    responses = {"plan": plan, "final_plan": plan, "review": {"accepted": True, "errors": []}, "knowledge": [{"queries": ["ref namespace"], "claims": [], "done": False}, {"queries": ["关联规则"], "claims": [claim], "done": False}, {"queries": [], "claims": [claim], "done": True}], "link": {"status": "unresolved", "explanation": "mock does not infer semantic identity"}, "alignment": {"mapping_kind": "unmapped", "explanation": "mock does not claim external equivalence"}}
    responses["external_queries"] = {"queries": []}
    write_yaml(root / "mock_llm.yaml", responses)
    write_yaml(root / "mock_documents.yaml", {"synthetic": True, "documents": [{"id": "join-rule", "title": "ref 关联规则", "text": quote, "scope": "本合成样本", "version": "1"}, {"id": "noise", "title": "午餐安排", "text": "食堂营业时间与记录关联无关。", "scope": "其他话题", "version": "1"}]})
    write_yaml(root / "fixture_manifest.yaml", {"synthetic": True, "scenario": scenario, "source_records": rows, "meaning": "Hand-authored fixtures and mock outputs; not user data or a learned result", "expected_links_first_eight": [[1, 1], [2, 2], [8, 3]] if scenario == "linked" else []})
