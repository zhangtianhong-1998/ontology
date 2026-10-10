from types import SimpleNamespace

from ontology_r2.incremental import core_context, direct_mapping
from ontology_r2.validation import validate_plan


PROFILE = {
    "object_roots": [{"id": name} for name in
                     ("GeneralObject", "Measure", "Metric", "Dimension", "Term")],
    "relation_roots": [{"id": name, "kind": "object"} for name in
                       ("contains", "depends_on", "related_to", "points_to")],
}


def table(name, comment):
    return {"table_name": name.split(".")[-1], "table_comment": comment,
            "pk": ["id"], "column_names": ["id", "name"],
            "columns": [{"column_name": "id", "data_type": "bigint"},
                        {"column_name": "name", "data_type": "text"}],
            "constraints": [], "foreign_keys": []}


def test_source_record_types_cover_tables_without_pretending_all_are_business_concepts():
    tables = {"demo.metrics": table("demo.metrics", "指标业务定义与计算公式"),
              "demo.measure": table("demo.measure", "度量定义"),
              "demo.references": table("demo.references", "参数引用指标和度量的规则")}
    data = SimpleNamespace(tables=tables,
                           evidence={"schema:" + name: {"raw_fragment": item["table_comment"]}
                                     for name, item in tables.items()})
    plan, mapping = direct_mapping(data)
    by_table = {item.table: item for item in plan.tables}
    by_type = {item.id: item for item in plan.object_types}

    assert len(by_type) == 3
    assert all(item.parent == "GeneralObject" for item in by_type.values())
    hints = {item["table"]: item["record_type_classification"]["described_business_root_hint"]
             for item in mapping["tables"]}
    assert hints == {"demo.metrics": "Metric", "demo.measure": "Measure",
                     "demo.references": None}
    assert all(item.category == "source_record_type" for item in by_type.values())
    assert all(item["status"] == "source_mapping_only" for item in mapping["tables"])
    assert mapping["business_semantics"] == "not_inferred"
    assert validate_plan(plan, data, PROFILE) == []


def test_source_record_type_definitions_stay_bounded_in_llm_core_context():
    tables = {f"demo.table_{n}": table(f"demo.table_{n}", "普通记录") for n in range(30)}
    data = SimpleNamespace(tables=tables)
    plan, _ = direct_mapping(data)
    context = core_context(plan, "demo.table_0", data)

    assert [item["id"] for item in context["object_types"]] == ["source_record_type:demo.table_0"]
    assert len(context["table_bindings"]) == 30
