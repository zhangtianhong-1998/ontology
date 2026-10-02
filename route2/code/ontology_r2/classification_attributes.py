"""Keep explicitly declared classification attributes outside row scope."""
import re


def classification_basis(data, record, column, value):
    """Require complete source content and an unambiguous schema declaration.

    Column names, model labels and category-like values do not establish this
    role. Namespace/version, calculation and restrictive scope remain strict.
    """
    fields = record.get("fields", {})
    if any(entry.get("column") == column for role in ("identity", "formula", "unit")
           for entry in fields.get(role, [])):
        return None
    if not str(value).strip() or not any(
            entry.get("column") == column and not entry.get("truncated")
            and str(entry.get("value")) == str(value) for entry in fields.get("scope", [])):
        return None
    metadata = next((item for item in getattr(data, "tables", {}).get(record["table"], {}).get("columns", [])
                     if item.get("column_name") == column), {})
    declaration = str(metadata.get("column_comment") or "")
    if (not re.search(r"分类|类别|\b(?:category|classification)\b", declaration, re.I)
            or re.search(r"租户|命名空间|版本|粒度|计算|聚合|统计期间|适用范围|筛选|过滤|"
                         r"\b(?:tenant|namespace|version|granularity|calculation|aggregation|filter|restriction)\b",
                         declaration, re.I)):
        return None
    return {"basis": "source_column_classification_declaration", "record_id": record["record_id"],
            "table": record["table"], "column": column, "value": str(value),
            "declaration": declaration, "schema_evidence_id": f"schema:{record['table']}:{column}"}


def source_classifications(data, records):
    """Return only classification fields shared by every complete witness."""
    if not records:
        return {}, {}
    candidates = {entry["column"]: str(entry["value"])
                  for entry in records[0].get("fields", {}).get("scope", [])}
    values, evidence = {}, {}
    for column, value in candidates.items():
        bases = [classification_basis(data, record, column, value) for record in records]
        if all(bases):
            values[column], evidence[column] = value, bases
    return values, evidence
