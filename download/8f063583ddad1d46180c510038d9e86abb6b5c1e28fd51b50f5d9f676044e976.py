"""Replay definition membership against its complete source template and row.

Shared semantics establish a type membership, never equality of records or
inheritance of their reference relations. Callers must resolve edges separately.
"""
from .storage import digest, qi


def replay_definition_membership(data, member, template, row, business_type, cache):
    """Return proven member columns; reject stale or incomplete saved contracts."""
    if (not template or member.get("snapshot_id") != data.snapshot_id
            or member.get("status") != "definition_template_match"
            or template.get("type_id") != business_type.id
            or member.get("type_id", member.get("object_type_id")) != business_type.id
            or member.get("table") != template.get("table")
            or member.get("template_id") != template.get("id")
            or member.get("entity_identity_claim") is not False
            or member.get("relationship_inheritance") is not False):
        raise ValueError("Definition membership contract is missing, stale or claims record identity")
    table_name = template["table"]
    table = data.tables.get(table_name)
    fields = template.get("semantic_fields") or []
    if (table is None or not fields or any(
            field.get("column") not in table["column_names"] or not field.get("role")
            for field in fields)):
        raise ValueError("Definition template semantic columns are missing")
    semantics = [[field["role"], field["column"], field.get("value")] for field in fields]
    signature = digest(semantics)
    verification = member.get("verification") or {}
    columns = [field["column"] for field in fields]
    if (template.get("semantic_fingerprint") != signature
            or template["id"] != "definition_template:" + digest([
                data.snapshot_id, table_name, business_type.id, semantics])[:24]
            or verification.get("method") != "complete_original_source_field_equality"
            or verification.get("semantic_fingerprint") != signature
            or verification.get("columns") != columns):
        raise ValueError("Definition membership complete semantic fingerprint differs")
    if (row.get("__r2_row") != member.get("row_number")
            or data.record_id(table_name, row) != member.get("record_id")
            or [[field["role"], field["column"], row.get(field["column"])] for field in fields] != semantics):
        raise ValueError("Definition member source identity or complete semantics changed")
    cache_key = (template["id"], signature)
    if cache_key not in cache:
        number = template.get("representative_row_number")
        if type(number) is not int or number < 1:
            raise ValueError("Definition template representative row is missing")
        cursor = data.db.execute(f"SELECT * FROM {qi(table['sql_name'])} WHERE __r2_row=?", [number])
        values = cursor.fetchone()
        representative = dict(zip([part[0] for part in cursor.description], values)) if values else {}
        record_id = template.get("representative_record_id")
        if (not representative or data.record_id(table_name, representative) != record_id
                or [[field["role"], field["column"], representative.get(field["column"])]
                    for field in fields] != semantics):
            raise ValueError("Definition template representative changed")
        # The accepted type must still cite this representative's own complete
        # definition, not merely an arbitrary row with equal strings.
        supported = False
        for prop in business_type.source_properties:
            if prop.role not in {"description", "formula"} or prop.source_table != table_name:
                continue
            for evidence_id in prop.evidence_ids:
                evidence = data.evidence.get(evidence_id) or {}
                ref = evidence.get("source_ref") or {}
                if (evidence_id in template.get("evidence_ids", [])
                        and evidence.get("origin") == "observed_record"
                        and not evidence.get("raw_fragment_truncated")
                        and ref.get("snapshot_id") == data.snapshot_id
                        and ref.get("record_id") == record_id
                        and ref.get("table") == table_name
                        and ref.get("column") == prop.source_column
                        and representative.get(prop.source_column) == evidence.get("raw_fragment")):
                    supported = True
        if not supported:
            raise ValueError("Definition template has no accepted representative source proof")
        cache[cache_key] = True
    return list(dict.fromkeys(columns))
