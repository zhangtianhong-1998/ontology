"""Bounded, reusable field contracts for fact-only inputs.

Numeric samples establish the observed shape, never a business identity. New
Metric types require separate source-declared business-object and quantity
anchors. Missing calculation rules and units remain explicitly unknown.
"""

import json
import re
from typing import Literal

from pydantic import Field

from .column_roles import classify_columns
from .fact_type_binding import (FactFieldBindingDecision, _norm, _recall_types,
                                _source_definition_evidence, _unit_from_comment,
                                field_unit_evidence)
from .models import BuildPlan, DerivedType, Strict
from .storage import digest, qi


class FactSchemaDecision(Strict):
    status: Literal["proposed", "unresolved"]
    label: str = ""
    # Both anchors must be literal substrings of the same source field
    # declaration. No vocabulary is inferred from a numeric distribution.
    identity_evidence_id: str = ""
    identity_quote: str = ""
    business_object_quote: str = ""
    quantity_quote: str = ""
    scope_bindings: dict[str, str] = Field(default_factory=dict)
    unit: str | None = None
    unit_column: str | None = None
    unit_quote: str = ""
    reason: str = ""


_ANONYMOUS = re.compile(r"^(?:(?:col(?:umn)?|field|value|val|amount|number|num|f|c|v|x|data)[_\d]*)$", re.I)
_GENERIC = {"amount", "value", "number", "numeric", "metric", "measure", "data", "field",
            "fact", "total", "指标", "度量", "数值", "金额", "数据", "字段", "合计"}


def _surface(value):
    return re.sub(r"[\W_]+", "", _norm(value))


def field_contract_fingerprint(table, value_column, coordinate_columns):
    """Exclude snapshots and observed values; retain source semantic contracts."""
    roles = sorted((item.get("column"), item.get("role"), item.get("status"), item.get("semantic_status", "unjudged"))
                   for item in table.get("inferred_semantic_roles", []))
    return digest({"table": table["name"], "table_comment": table.get("table_comment"),
                   "columns": table["columns"], "pk": table.get("pk", []),
                   "excluded": sorted(table.get("semantic_excluded_columns", [])),
                   "roles": roles, "value_column": value_column,
                   "coordinates": list(coordinate_columns)})


def _declaration(data, table, column):
    comment = str(column.get("column_comment") or "").strip()
    raw, kind = (comment, "column_comment") if comment else (column["column_name"], "column_name")
    evidence_id = "field_declaration:" + digest([table["name"], column["column_name"], kind, raw])[:24]
    data.evidence[evidence_id] = {
        "id": evidence_id, "origin": "declared_metadata", "declaration_kind": kind,
        "raw_fragment": raw, "raw_fragment_truncated": False,
        "source_ref": {"table": table["name"], "column": column["column_name"],
                       "snapshot_id": data.snapshot_id,
                       "file": data.evidence[f"schema:{table['name']}:{column['column_name']}"]["source_ref"].get("file")},
    }
    return {"evidence_id": evidence_id, "kind": kind, "value": raw}


def _checked_links(data, table_name, association_context):
    """Return current checked links, preserving their conditions and direction."""
    graph = getattr(association_context, "graph", association_context) or {}
    if not isinstance(graph, dict):
        return [], set()
    owners = {edge["target"]: edge["source"] for edge in graph.get("edges", [])
              if edge.get("type") == "table_has_column"}
    neighbors = set()
    links = []
    for edge in graph.get("edges", []):
        if (edge.get("type") != "technical_link" or edge.get("status") != "checked_technical"
                or edge.get("snapshot_id") != data.snapshot_id):
            continue
        a, b = owners.get(edge.get("source")), owners.get(edge.get("target"))
        if table_name in (a, b):
            neighbors.update(name for name in (a, b) if name and name != table_name)
            links.append({key: edge.get(key) for key in ("source", "target", "status", "selector",
                                                         "scope_bindings", "transform", "semantic_relation")})
    return sorted(links, key=digest), neighbors


def _definition_context(data, core, neighbors):
    """Retain accepted definitions reached through current checked field links."""
    result = []
    for item in core.object_types:
        if item.category != "business_type" or not any(source.source_table in neighbors for source in item.source_properties):
            continue
        evidence, error = _source_definition_evidence(item, data, max_chars=2048)
        if not error:
            result.append({"type_id": item.id, "label": item.label,
                           "definition": item.definition, "unit": item.unit,
                           "applicability_scope": item.applicability_scope,
                           "source_definitions": evidence})
    return result


def _packet(data, report, field, core, association_context, max_samples):
    table = data.tables[report["table"]]
    columns = {item["column_name"]: item for item in table["columns"]}
    name, coordinates = field["column"], list(report.get("coordinate_columns") or [])
    safe = {item["column"] for item in classify_columns(table)
            if item["role"] not in ("sensitive", "empty", "audit_time", "audit_metadata")}
    if name not in safe or not set(coordinates) <= safe:
        raise ValueError("Fact field or coordinate is excluded from semantic inspection")
    declaration = _declaration(data, table, columns[name])
    # DISTINCT is applied to complete observed rows, preserving co-occurrence.
    selected = list(dict.fromkeys([*coordinates, name]))
    fields = ", ".join(qi(value) for value in selected)
    rows = data.db.execute(f"SELECT {fields}, min(__r2_row) FROM {qi(table['sql_name'])} "
                           f"GROUP BY {fields} ORDER BY min(__r2_row) LIMIT ?", [max_samples + 1]).fetchall()
    samples = [{"row_number": row[-1], "values": dict(zip(selected, row[:-1]))}
               for row in rows[:max_samples]]
    units = [value for column in columns if column in safe
             if (value := field_unit_evidence(data, table, column)) is not None]
    links, neighbors = _checked_links(data, table["name"], association_context)
    context = _definition_context(data, core, neighbors)
    roles = [{"column": item["column"], "role": item["role"], "status": item["status"],
              "semantic_status": item.get("semantic_status", "unjudged")}
             for item in table.get("inferred_semantic_roles", [])
             if item.get("status") == "source_verified_role_candidate" and item.get("column") in safe]
    packet = {"table": table["name"], "table_comment": table.get("table_comment") or "",
              "value_column": name, "source_declaration": declaration,
              "coordinate_columns": coordinates,
              "coordinate_declarations": [columns[value] for value in coordinates],
              "source_checked_role_candidates": roles, "sample_rows": samples,
              "sample_scope": "bounded_actual_distinct_rows_only",
              "more_sample_rows_available": len(rows) > max_samples,
              "unit_columns": units, "checked_association_conditions": links,
              "declared_unit_quote_contract": {
                  "recognized_units": sorted(_unit_from_comment(declaration["value"])),
                  "required_unit_quote_if_declared": declaration["value"],
                  "rule": "When unit comes from this field declaration, unit_quote MUST equal the entire required_unit_quote_if_declared, not just a unit word or substring."
              },
              "associated_definitions": context,
              "contract": "Declare only a source-named business quantity. Samples do not name a Metric. "
                          "Missing formulas and units stay unknown; scope keys must map to actual coordinate columns."}
    # Current record evidence IDs are excluded from the reusable contract, but
    # the complete semantic statements and unit completeness still participate.
    context_signature = [{key: value for key, value in item.items() if key != "source_definitions"} |
                         {"source_definitions": [(value["role"], value["value"]) for value in item["source_definitions"]]}
                         for item in context]
    unit_signature = [{key: value for key, value in item.items() if key != "evidence_id"} for item in units]
    packet["contract_fingerprint"] = digest([field_contract_fingerprint(table, name, coordinates),
                                             context_signature, unit_signature, links])
    return packet


def compile_fact_schema(data, core, report, packet, decision):
    """Compile a source-schema Metric and an auditable field binding template."""
    decision = FactSchemaDecision.model_validate(decision)
    if decision.status != "proposed":
        return None
    declaration = packet["source_declaration"]
    name = packet["value_column"]
    table = data.tables[packet["table"]]
    if (decision.identity_evidence_id != declaration["evidence_id"]
            or decision.identity_quote != declaration["value"]):
        raise ValueError("Metric identity requires the full original field declaration")
    object_, quantity, label = (decision.business_object_quote.strip(), decision.quantity_quote.strip(), decision.label.strip())
    raw = declaration["value"]
    if (not label or any(len(_surface(value)) < 2 or value not in raw
                         or _surface(value) in _GENERIC for value in (object_, quantity))
            or _surface(object_) == _surface(quantity)
            or object_ in quantity or quantity in object_):
        raise ValueError("Metric identity requires distinct source-declared business object and quantity")
    if (_surface(label) not in _surface(raw) or _surface(object_) not in _surface(label)
            or _surface(quantity) not in _surface(label)):
        raise ValueError("Metric label must retain both literal source identity anchors")
    coordinates = packet["coordinate_columns"]
    if (not set(decision.scope_bindings.values()) <= set(coordinates)
            or len(set(decision.scope_bindings.values())) != len(decision.scope_bindings)
            or any(not key.strip() for key in decision.scope_bindings)):
        raise ValueError("Scope binding must explicitly name unique observed coordinate columns")
    observed_units = _unit_from_comment(raw)
    unit_evidence_ids = []
    if decision.unit_column:
        observed = next((item for item in packet["unit_columns"] if item["column"] == decision.unit_column), None)
        if (observed is None or observed["status"] != "verified_single_unit"
                or observed["value"] != decision.unit_quote or _norm(observed["value"]) != _norm(decision.unit)):
            raise ValueError("Unit must quote a complete current-snapshot unit column")
        unit_evidence_ids.append(observed["evidence_id"])
        observed_units.add(_norm(observed["value"]))
    elif decision.unit and (decision.unit_quote != raw or observed_units != {_norm(decision.unit)}):
        raise ValueError("Unit must appear explicitly in the complete field declaration")
    if observed_units != ({_norm(decision.unit)} if decision.unit else set()):
        raise ValueError("Unit is missing or conflicts with source declarations")
    unit = decision.unit.strip() if decision.unit else None
    # The text is deterministic: a model cannot smuggle in an unquoted formula.
    definition = f"{raw}；来源事实字段 {table['name']}.{name} 的观测量，计算口径未提供。"
    type_id = "type:fact_field:" + digest([table["name"], name, raw, label, object_, quantity, unit])[:24]
    evidence_ids = [declaration["evidence_id"], *unit_evidence_ids]
    item = DerivedType(id=type_id, parent="Metric", label=label, definition=definition,
                       category="business_type", evidence_scope="source_schema", unit=unit,
                       evidence_ids=evidence_ids,
                       semantic_parameters={"business_object_quote": object_, "quantity_quote": quantity,
                                            "calculation_status": "unknown", "identity_basis": "source_field_declaration"},
                       source_properties=[{"role": "name", "source_table": table["name"],
                                           "source_column": name, "evidence_ids": [declaration["evidence_id"]]}])
    binding = FactFieldBindingDecision(status="bind", type_id=item.id, source_column_quote=raw,
                                       type_definition_quote=definition,
                                       source_definition_evidence_id=declaration["evidence_id"], type_source_quote=raw,
                                       scope_bindings=decision.scope_bindings, unit_column=decision.unit_column)
    template = {"id": "field_binding_template:" + digest([table["name"], name, packet["contract_fingerprint"]])[:24],
                "status": "accepted_field_template", "table": table["name"], "value_column": name,
                "source_snapshot_id": data.snapshot_id, "type_id": item.id,
                "type_signature": digest(item.model_dump()),
                "schema_fingerprint": field_contract_fingerprint(table, name, coordinates),
                "contract_fingerprint": packet["contract_fingerprint"],
                "scope_bindings": decision.scope_bindings, "unit_status": "source_verified" if unit else "unknown",
                "calculation_status": "unknown", "evidence_ids": evidence_ids,
                "decision": decision.model_dump(), "binding_decision": binding.model_dump()}
    return item, template


def template_binding_decision(template, data, table, name, coordinates, core):
    """Only freshly revalidated templates may materialize the current snapshot."""
    item = next((item for item in core.object_types if item.id == template.get("type_id")), None)
    if (template.get("status") != "accepted_field_template" or template.get("table") != table["name"]
            or template.get("value_column") != name or template.get("source_snapshot_id") != data.snapshot_id
            or template.get("schema_fingerprint") != field_contract_fingerprint(table, name, coordinates)
            or item is None or template.get("type_signature") != digest(item.model_dump())):
        raise ValueError("Field template is stale or differs from its accepted type")
    return FactFieldBindingDecision.model_validate(template["binding_decision"])


async def induce_fact_schema(data, fact_observations, core, llm, *, association_context=None,
                             column_role_candidates=None, max_calls=50, max_samples_per_field=5,
                             max_packet_bytes=16000, reusable_templates=()):
    """At most one request per unbound field; replay contracts across snapshots.

    Returned templates are JSON/YAML serializable. Reuse rebuilds declarations
    and unit witnesses from current input and reruns the compiler. Numeric
    values can change without triggering another model request.
    """
    if any(type(value) is not int or value < 1 for value in (max_samples_per_field, max_packet_bytes)):
        raise ValueError("Fact schema sample and byte limits must be positive integers")
    if type(max_calls) is not int or max_calls < 0:
        raise ValueError("max_calls must be a nonnegative integer")
    if fact_observations.get("scope") != "imported_csv_snapshot_only":
        raise ValueError("Unsupported fact observation scope")
    if column_role_candidates:
        if column_role_candidates.get("source_snapshot") != data.snapshot_id:
            raise ValueError("Column-role candidates belong to another snapshot")
        # The role stage stores its checked candidates on Dataset tables. Do
        # not consume detached role claims without that source-checked state.
        for candidate in column_role_candidates.get("candidates", []):
            roles = data.tables.get(candidate.get("table"), {}).get("inferred_semantic_roles", [])
            if not any(all(item.get(key) == candidate.get(key) for key in ("column", "role", "status"))
                       for item in roles):
                raise ValueError("Column-role candidate is absent from source-checked table metadata")
    plan = BuildPlan.model_validate(core).model_copy(deep=True)
    templates, steps = [], []
    calls = reused = 0
    prior = {(item.get("table"), item.get("value_column")): item for item in reusable_templates or ()}
    for report in fact_observations.get("tables", []):
        if report.get("row_purpose") != "business_fact":
            continue
        if report.get("source_snapshot_id") != data.snapshot_id:
            raise ValueError("Fact report belongs to another snapshot")
        table = data.tables[report["table"]]
        for field in report.get("value_fields", []):
            name = field["column"]
            step = {"table": table["name"], "value_column": name, "status": "unresolved"}
            try:
                packet = _packet(data, report, field, plan, association_context, max_samples_per_field)
                saved = prior.get((table["name"], name))
                reusable = saved and saved.get("contract_fingerprint") == packet["contract_fingerprint"]
                if reusable:
                    decision = FactSchemaDecision.model_validate(saved["decision"])
                    step["reused_template"] = True
                else:
                    column = next(value for value in table["columns"] if value["column_name"] == name)
                    choices, omitted, excluded = _recall_types(data, plan, table, column,
                                                              max_candidates=8, max_definition_chars=2048)
                    if omitted or excluded:
                        raise ValueError("existing_type_evidence_incomplete_or_ambiguous")
                    if choices and not omitted and not excluded:
                        step.update(status="existing_type_candidates", reason="use_existing_type_binding")
                        steps.append(step)
                        continue
                    if not str(column.get("column_comment") or "").strip() and _ANONYMOUS.fullmatch(name):
                        raise ValueError("anonymous_numeric_field_has_no_business_identity")
                    if len(json.dumps(packet, ensure_ascii=False).encode()) > max_packet_bytes:
                        raise ValueError("fact_schema_packet_exceeds_byte_budget")
                    if calls >= max_calls:
                        raise ValueError("fact_schema_call_budget_exhausted")
                    calls += 1
                    decision = await llm.ask("fact_schema_induction", packet, FactSchemaDecision)
                compiled = compile_fact_schema(data, plan, report, packet, decision)
                if compiled is None:
                    step["reason"] = decision.reason or "model_unresolved"
                else:
                    item, template = compiled
                    prior_item = next((value for value in plan.object_types if value.id == item.id), None)
                    if prior_item:
                        plan.object_types[plan.object_types.index(prior_item)] = item
                    else:
                        plan.object_types.append(item)
                    templates.append(template)
                    reused += bool(reusable)
                    step.update(status="accepted_field_template", type_id=item.id,
                                template_id=template["id"], calculation_status="unknown")
            except Exception as exc:
                step.update(error_type=type(exc).__name__, reason=str(exc) if isinstance(exc, ValueError) else "fact_schema_call_failed")
            steps.append(step)
    return {"plan": plan, "field_templates": templates, "steps": steps,
            "coverage": {"fields_considered": len(steps), "model_calls": calls,
                         "accepted_templates": len(templates), "reused_templates": reused,
                         "unresolved_fields": sum(item["status"] == "unresolved" for item in steps),
                         "per_row_llm_calls": 0,
                         "partial": any(item["status"] == "unresolved" for item in steps)}}
