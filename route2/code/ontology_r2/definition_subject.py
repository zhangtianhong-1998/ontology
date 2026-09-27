"""Conservative source-subject checks; retrieval topics are not quantities.

This guard can reject a service-purpose quote, or identify a separately quoted
quantity definition. It never certifies the identity of an entire source row.
Unknown wording stays unknown and still needs the caller's semantic checks.
"""

import re

from .column_roles import is_sensitive_column
from .storage import qi


_SERVICE_NAME = re.compile(
    r"(?:接口|服务|卡片|图表|仪表盘|展示)(?:名称|标题)|"
    r"\b(?:api|interface|service|endpoint|card|dashboard|chart|display)[_\s]+(?:name|title|label)\b", re.I)
_SERVICE_DESCRIPTION = re.compile(
    r"(?:接口|服务|卡片|展示)(?:说明|描述|用途|功能)|"
    r"\b(?:api|interface|service|endpoint|card|dashboard)[_\s]+(?:description|purpose|function)\b", re.I)
_PURPOSE = re.compile(
    r"^(?:(?:此|该|本)?(?:接口|服务|卡片|图表|页面|API)[，,:：\s]*)?"
    r"(?:(?:提供|用于|用来|支持)[，,:：\s]*)?(?:查询|检索|展示|显示|返回|获取|调用|渲染)|"
    r"^(?:(?:this\s+)?(?:api|interface|service|endpoint|card|dashboard|chart)\s+)?"
    r"(?:(?:is\s+)?(?:used\s+to|designed\s+to)\s+)?"
    r"(?:queries|query|retrieves|retrieve|fetches|fetch|displays|display|shows|show|"
    r"returns|return|provides\s+access\s+to)\b|"
    r"^(?:an?\s+)?(?:api|endpoint|dashboard|chart)\s+(?:for|to)\b", re.I)
_URL = re.compile(r"^(?:https?|grpc|wss?)://[^\s]+$", re.I)
_PATH = re.compile(r"^/[A-Za-z0-9_{}:./-]+$")
_ADDRESS_DECLARATION = re.compile(r"接口地址|服务地址|端点地址|\b(?:api[_\s]*url|endpoint|request[_\s]*path)\b", re.I)
_AUTH_DECLARATION = re.compile(r"(?:^|[_\s])(?:auth|authentication)(?:[_\s]|$)|认证|鉴权", re.I)
_HTTP_METHODS = frozenset(("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"))
_PROTOCOLS = frozenset(("HTTP", "HTTPS", "HTTP/1.1", "HTTP/2", "GRPC", "REST", "WEBSOCKET"))


def _independent_definition(value, label):
    """Recognize a literal named definition, without requiring arithmetic."""
    if not label.strip():
        return False
    # A definition must start with its own quantity name. A topic appearing
    # inside a service-purpose sentence is insufficient.
    return bool(re.match(
        r"^\s*[‘“\"']?" + re.escape(label.strip()) + r"[’”\"']?\s*"
        r"(?:(?:的)?(?:定义为|是指|指的是|表示|等于|为|是|指)|[:：=]|"
        r"\b(?:is(?:\s+defined\s+as)?|means|refers\s+to|represents|equals|counts|measures)\b)"
        r"\s*\S.{3,}$", value, re.I))


def quantity_subject_assessment(data, record, label, classification_quote):
    """Assess a quoted Metric/Measure definition using one privacy-safe row.

    ``blocks_exact_record_identity`` also blocks a mixed service record whose
    independent quantity field would require a separate projection contract.
    No table-name/root-hint classifier or model is invoked here.
    """
    table = getattr(data, "tables", {}).get(record.get("table"), {})
    excluded = set(table.get("semantic_excluded_columns") or ())
    columns = {c["column_name"]: c for c in table.get("columns", ())
               if c["column_name"] not in excluded and not is_sensitive_column(c)
               and not _AUTH_DECLARATION.search(c["column_name"] + " " + str(c.get("column_comment") or ""))}
    entries = []
    for role, values in record.get("fields", {}).items():
        for item in values:
            if item.get("column") in columns and item.get("value") is not None:
                entries.append({**item, "role": role, "value": str(item["value"]).strip()})
    quote = str(classification_quote or "").strip()
    selected = [e for e in entries if e["role"] in ("description", "formula")
                and not e.get("truncated") and quote and e["value"] == quote]
    result = {"status": "unknown", "blocks_quantity_definition": False,
              "blocks_exact_record_identity": False, "requires_definition_projection": False,
              "record_identity_supported": False, "reasons": [], "evidence": [],
              "selected_definition_columns": [e["column"] for e in selected]}
    if not selected:
        result["reasons"].append("complete_source_definition_quote_not_available")
        # The classification quote may belong to a different exact record.
        # Still inspect this record's own subject; another record cannot lend
        # its quantity identity to a service row with the same label.

    def declaration(column):
        meta = columns[column]
        return column.replace("_", " ") + " " + str(meta.get("column_comment") or "")

    def evidence(kind, column, *, value=None, shape=None):
        item = {"kind": kind, "table": record.get("table"), "record_id": record.get("record_id"),
                "row_number": record.get("row_number"), "column": column,
                "source_declaration": declaration(column)}
        if value is not None:
            item["quote"] = value
        if shape is not None:
            # URL credentials/query strings and unrelated payloads are never
            # added to semantic context merely to establish service shape.
            item["value_shape"] = shape
        result["evidence"].append(item)

    selected_service_name = False
    for entry in entries:
        if (entry["role"] in ("name", "alias") and not entry.get("truncated")
                and entry["value"] == str(label).strip()
                and _SERVICE_NAME.search(declaration(entry["column"]))):
            selected_service_name = True
            evidence("service_name_declaration", entry["column"], value=entry["value"])

    # Do not load excluded/authentication columns even transiently. A single
    # exact source row supplies shape clues omitted from the semantic card.
    raw = {entry["column"]: entry["value"] for entry in entries}
    row_number = record.get("row_number")
    if (hasattr(data, "db") and table.get("sql_name") and type(row_number) is int
            and row_number > 0 and columns):
        names = list(columns)
        row = data.db.execute(
            f"SELECT {', '.join(qi(name) for name in names)} FROM {qi(table['sql_name'])} "
            "WHERE __r2_row=? LIMIT 1", [row_number]).fetchone()
        if row is not None:
            raw.update({name: str(value).strip() for name, value in zip(names, row) if value is not None})

    shapes = set()
    for column, value in raw.items():
        shape = ("service_address" if _URL.fullmatch(value) or
                 (_PATH.fullmatch(value) and _ADDRESS_DECLARATION.search(declaration(column))) else
                 "http_method" if value.upper() in _HTTP_METHODS else
                 "service_protocol" if value.upper() in _PROTOCOLS else None)
        if shape:
            shapes.add(shape)
            evidence("service_structure", column, shape=shape)
    structural_service = "service_address" in shapes and bool(shapes & {"http_method", "service_protocol"})
    service_descriptions = []
    description_columns = {e["column"] for e in entries
                           if e["role"] == "description" and not e.get("truncated")}
    for column, value in raw.items():
        if (_PURPOSE.search(value) and not _independent_definition(value, str(label))
                and (column in description_columns or structural_service
                                       or _SERVICE_DESCRIPTION.search(declaration(column)))):
            service_descriptions.append({"column": column, "value": value})
            evidence("operation_purpose_text", column, value=value)
    selected_service_declaration = any(_SERVICE_DESCRIPTION.search(declaration(e["column"])) for e in selected)
    independent = bool(selected) and _independent_definition(quote, str(label))
    service_subject = selected_service_name or bool(service_descriptions) or (
        structural_service and selected_service_declaration)

    if independent:
        result["status"] = "independent_quantity_definition"
        result["reasons"].append("named_independent_definition_field")
        for entry in selected:
            evidence("quantity_definition_candidate", entry["column"], value=entry["value"])
        if service_subject:
            result.update(requires_definition_projection=True, blocks_exact_record_identity=True)
            result["reasons"].append("mixed_record_requires_definition_projection")
    elif service_subject:
        result.update(status="service_subject", blocks_quantity_definition=True,
                      blocks_exact_record_identity=True)
        result["reasons"].append("service_or_display_topic_is_not_quantity_definition")
    elif selected:
        result["reasons"].append("quantity_subject_not_established_by_this_guard")
    return result
