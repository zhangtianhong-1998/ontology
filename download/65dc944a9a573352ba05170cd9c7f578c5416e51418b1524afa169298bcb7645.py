"""Conservative source-record classes; these are not induced business concepts."""

from .models import DerivedType


def source_record_type(table_name, table):
    comment = (table.get("table_comment") or "").strip()
    # A table comment can identify a definition table. Mentioning a metric or
    # dimension as a reference is not enough to classify its rows as that type.
    described_root = None
    for phrase, root in (("指标定义", "Metric"), ("指标业务定义", "Metric"),
                         ("度量定义", "Measure"), ("维度定义", "Dimension"),
                         ("术语定义", "Term")):
        if phrase in comment:
            described_root = root
            break
    type_id = "source_record_type:" + table_name
    definition = "源表记录结构" + ("：" + comment if comment else "：" + table_name)
    return DerivedType(
        id=type_id, parent="GeneralObject", label=comment or table_name,
        definition=definition, evidence_ids=["schema:" + table_name],
        category="source_record_type",
    ), {"basis": "explicit_table_comment" if described_root else "source_table_only",
        "described_business_root_hint": described_root,
        "business_concept_inferred": False}
