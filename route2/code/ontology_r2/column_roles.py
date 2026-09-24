"""Conservative column roles for semantic prompts and direct attribute bindings.

Roles describe only the imported CSV snapshot and declared metadata.  In
particular, an empty sample does not prove that the source database column is
always empty, and a code or a non-PK ID may be a meaningful join key.
"""

from __future__ import annotations

import re
from typing import Any


_AUDIT_NAMES = {
    "created_at", "created_time", "create_time", "create_date",
    "creation_date", "creation_time", "updated_at", "updated_time",
    "update_time", "update_date", "last_updated_at", "last_update_time",
    "last_update_date", "modified_at", "modified_time", "modify_time",
    "last_modified_at", "last_modified_time", "deleted_at", "delete_time",
    "deleted_time", "inserted_at", "insert_time", "etl_time",
    "load_time", "loaded_at", "ingest_time", "ingested_at", "sync_time",
}
_BUSINESS_TIME_CUES = re.compile(
    r"业务|交易|订单|合同|发生|生效|失效|统计|报告|结算|账期|预测|销售|"
    r"business|transaction|order|contract|event|effective|report|settlement|"
    r"accounting|forecast|sales|metric|measure|dimension",
    re.IGNORECASE,
)
_SEMANTIC_NAME_CUES = re.compile(
    r"(?:^|_)(?:name|alias|title|definition|description|meaning|formula|"
    r"expression|expr|rule|scope|unit|label|category|type|value|date|"
    r"time|period|code|id|metric|measure|dim|dimension|term)(?:_|$)"
)
_SEMANTIC_COMMENT_CUES = re.compile(
    r"名称|别名|定义|含义|描述|公式|口径|规则|维度|指标|度量|范围|单位|"
    r"编码|业务|区域|地区|来源|关联|指向|类型|取值|值域|周期|日期|时间"
)
_TECHNICAL_ID_NAMES = {"id", "pk", "pk_id", "row_id", "record_id", "uuid", "guid"}


def _table_name(table: dict[str, Any]) -> str:
    if table.get("name"):
        return str(table["name"])
    if table.get("schema") and table.get("table_name"):
        return f"{table['schema']}.{table['table_name']}"
    return str(table.get("table_name", "unknown_table"))


def _empty_in_input(profile: dict[str, Any] | None) -> bool:
    if not profile or profile.get("scan_scope") != "full_input":
        return False
    rows, usable = profile.get("row_count"), profile.get("usable_count")
    stat = profile.get("statistics", {}).get("usable_count", {})
    return (
        isinstance(rows, int) and not isinstance(rows, bool) and rows > 0
        and usable == 0 and stat.get("value") == 0
        and stat.get("exact") is True and stat.get("status") == "complete"
        and stat.get("rows_scanned") == rows
    )


def _literal_type(data_type: str) -> str:
    sql_type = data_type.lower().strip()
    if "timestamp" in sql_type or sql_type.startswith("datetime"):
        return "datetime"
    if sql_type == "date":
        return "date"
    if sql_type in ("bool", "boolean"):
        return "boolean"
    if re.match(r"^(smallint|integer|bigint|int\d*|serial\d*)$", sql_type):
        return "integer"
    if re.match(r"^(numeric|decimal|real|double|float)", sql_type):
        return "decimal"
    return "string"


def classify_columns(table: dict[str, Any]) -> list[dict[str, Any]]:
    """Classify each declared column without dropping it from source mappings.

    ``include_in_semantic_prompt=False`` applies only to prompt construction;
    every field remains in the deterministic direct mapping.  A binding is
    provided for columns whose meaning is settled by schema/profile evidence.
    Unknown and semantic columns stay visible so uncertain business fields are
    never silently discarded.
    """
    table_name = _table_name(table)
    pk = set(table.get("pk") or ())
    profiles = {p["column"]: p for p in table.get("profiles", ()) if p.get("column")}
    result = []
    for column in table.get("columns", ()):
        name = str(column["column_name"])
        normalized = name.lower()
        comment = str(column.get("column_comment") or "")
        profile = profiles.get(name)
        join_eligible = normalized in {"uuid", "guid", "pk"} or bool(
            re.search(r"(?:^|_)(?:id|code|key)$", normalized)
        )

        if _empty_in_input(profile):
            role, reason = "empty", "no usable values in the full imported CSV snapshot"
        elif normalized in _AUDIT_NAMES and not _BUSINESS_TIME_CUES.search(comment):
            role, reason = "audit_time", "explicit audit timestamp name without business-time evidence"
        elif (name in pk and (normalized in _TECHNICAL_ID_NAMES or normalized.endswith("_id"))
              and not _BUSINESS_TIME_CUES.search(comment)):
            role, reason = "technical_identifier", "declared primary key with a technical ID name"
        elif (_SEMANTIC_NAME_CUES.search(normalized) or _SEMANTIC_COMMENT_CUES.search(comment)
              or join_eligible):
            role, reason = "semantic", "name or comment indicates business meaning or a reference key"
        else:
            role, reason = "unknown", "insufficient evidence to remove from semantic analysis"

        deterministic = role in ("empty", "audit_time", "technical_identifier")
        result.append({
            "column": name,
            "role": role,
            "reason": reason,
            "include_in_semantic_prompt": not deterministic,
            "deterministic_binding": ({
                "relation_type": "has",
                "source_table": table_name,
                "source_column": name,
                "declared_data_type": column.get("data_type"),
                "literal_type": _literal_type(str(column.get("data_type") or "")),
            } if deterministic else None),
            "join_eligible": join_eligible,
            "evidence": {
                "schema_id": f"schema:{table_name}:{name}",
                "profile_field_id": profile.get("field_id") if profile else None,
                "row_count": profile.get("row_count") if profile else None,
                "usable_count": profile.get("usable_count") if profile else None,
                "scan_scope": profile.get("scan_scope") if profile else None,
                "input_scope": profile.get("input_scope") if profile else None,
            },
        })
    return result
