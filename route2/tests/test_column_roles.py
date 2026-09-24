from copy import deepcopy

from ontology_r2.column_roles import classify_columns


def _column(name, data_type="text", comment=""):
    return {"column_name": name, "data_type": data_type, "column_comment": comment}


def _profile(name, usable, *, rows=3, input_scope="sample", exact=True):
    return {
        "column": name,
        "field_id": f"demo.items.{name}",
        "row_count": rows,
        "input_scope": input_scope,
        "scan_scope": "full_input",
        "usable_count": usable,
        "statistics": {"usable_count": {
            "value": usable, "exact": exact, "status": "complete",
            "rows_scanned": rows,
        }},
    }


def _table(columns, profiles=(), pk=()):
    return {
        "name": "demo.items", "columns": columns,
        "profiles": list(profiles), "pk": list(pk),
    }


def test_empty_role_is_bounded_to_imported_rows_and_keeps_direct_binding():
    table = _table([_column("note")], [_profile("note", 0)])
    before = deepcopy(table)
    role = classify_columns(table)[0]
    assert role["role"] == "empty"
    assert role["include_in_semantic_prompt"] is False
    assert role["deterministic_binding"]["source_column"] == "note"
    assert role["evidence"]["input_scope"] == "sample"
    assert "imported CSV snapshot" in role["reason"]
    assert table == before


def test_zero_rows_or_inexact_profile_does_not_prove_a_field_empty():
    columns = [_column("opaque"), _column("other")]
    profiles = [_profile("opaque", 0, rows=0), _profile("other", 0, exact=False)]
    roles = classify_columns(_table(columns, profiles))
    assert [r["role"] for r in roles] == ["unknown", "unknown"]
    assert all(r["include_in_semantic_prompt"] for r in roles)


def test_only_explicit_audit_names_without_business_counterevidence_are_removed():
    columns = [
        _column("creation_date", "date", "记录创建时间"),
        _column("last_update_date", "timestamp", "系统最后更新时间"),
        _column("transaction_date", "date", "交易发生日期"),
        _column("created_at", "timestamp", "订单创建时间"),
    ]
    roles = classify_columns(_table(columns))
    assert [r["role"] for r in roles] == [
        "audit_time", "audit_time", "semantic", "semantic",
    ]
    assert roles[0]["deterministic_binding"]["literal_type"] == "date"
    assert roles[1]["deterministic_binding"]["literal_type"] == "datetime"
    assert roles[2]["include_in_semantic_prompt"]


def test_declared_pk_id_is_technical_but_business_codes_and_reference_ids_remain():
    columns = [
        _column("api_info_id", "bigint", "主键 ID"),
        _column("metric_code", "text", "指标编码"),
        _column("business_id", "text", "关联业务 ID"),
        _column("metric_name", "text", "指标名称"),
    ]
    roles = classify_columns(_table(columns, pk=("api_info_id", "metric_code")))
    assert [r["role"] for r in roles] == [
        "technical_identifier", "semantic", "semantic", "semantic",
    ]
    assert roles[0]["join_eligible"]
    assert roles[0]["deterministic_binding"]["literal_type"] == "integer"
    assert all(r["join_eligible"] for r in roles[:3])
    assert all(r["include_in_semantic_prompt"] for r in roles[1:])


def test_unknown_or_missing_profiles_do_not_hide_possible_business_fields():
    roles = classify_columns(_table([_column("opaque"), _column("calculation_formula")]))
    assert [r["role"] for r in roles] == ["unknown", "semantic"]
    assert all(r["include_in_semantic_prompt"] for r in roles)
    assert all(r["deterministic_binding"] is None for r in roles)
