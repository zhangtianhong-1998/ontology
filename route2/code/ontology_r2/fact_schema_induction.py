"""Bounded, reusable field contracts for fact-only inputs.

Numeric samples establish the observed shape, never a business identity. New
Metric types require separate source-declared business-object and quantity
anchors. Missing calculation rules and units remain explicitly unknown.
"""

import json
import re
from contextlib import nullcontext
from copy import deepcopy
from typing import Literal

from pydantic import Field

from .column_roles import classify_columns, declared_context_role
from .fact_type_binding import (FactFieldBindingDecision, _norm, _recall_types,
                                _source_definition_evidence, _unit_from_comment,
                                field_unit_evidence)
from .models import BuildPlan, DerivedType, Strict
from .llm import BudgetExceeded, InputBudgetExceeded
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
    # A constant definition namespace/tenant can qualify this type only with
    # a literal field declaration and a complete coordinate check. Varying
    # tenant values remain observation coordinates, never inferred type IDs.
    identity_qualifiers: dict[str, str] = Field(default_factory=dict)
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
            # These are recall leads, not type identity evidence. Keep their
            # risks and full verification in both the prompt and reusable
            # contract fingerprint; a changed risk must invalidate reuse.
            links.append({key: deepcopy(edge[key]) for key in (
                "source", "target", "status",
                "selector", "scope_bindings", "transform", "semantic_relation",
                "numeric_overlap_only", "risk_flags", "verification", "meaning",
                "lineage_inferred",
            ) if key in edge})
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
                           "identity_qualifiers": item.identity_qualifiers,
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
                          "Missing formulas and units stay unknown; scope keys must map to actual coordinate columns. "
                          "Do not turn row-varying tenant values into types. identity_qualifiers require a literal "
                          "key=value (or key: value) clause in the complete field declaration, an explicit "
                          "scope_bindings coordinate, and the same nonblank value across the full input. "
                          "Preserve every explicit identity clause; omitting one or returning an empty object "
                          "cannot remove the source restriction. Unsupported identity clauses remain unresolved."}
    # Current record evidence IDs are excluded from the reusable contract, but
    # the complete semantic statements and unit completeness still participate.
    context_signature = [{key: value for key, value in item.items() if key != "source_definitions"} |
                         {"source_definitions": [(value["role"], value["value"]) for value in item["source_definitions"]]}
                         for item in context]
    unit_signature = [{key: value for key, value in item.items() if key != "evidence_id"} for item in units]
    packet["contract_fingerprint"] = digest([field_contract_fingerprint(table, name, coordinates),
                                             context_signature, unit_signature, links])
    return packet


def _declared_identity_qualifiers(table, raw):
    """Read explicit identity clauses independently of the proposed decision.

    Only recognized identity keys or source-checked identity columns qualify.
    A business clause such as limit=100 is not automatically type identity.
    Unsupported or conflicting identity clauses stay unresolved.
    """
    identity_fields = {item["column_name"] for item in table["columns"]
                       if declared_context_role(item) == "identity"}
    identity_fields.update(item["column"] for item in table.get("inferred_semantic_roles", [])
                           if item.get("status") == "source_verified_role_candidate"
                           and item.get("role") == "identity")
    names = {_norm(name) for name in identity_fields}
    names.update(_norm(re.sub(r"_(?:id|code|name|number)$", "", name, flags=re.I))
                 for name in identity_fields)
    clauses = re.finditer(r"(?<!\w)(?P<key>[^\W\d]\w*)\s*(?P<operator>[:：=]|[<>!]=?)"
                          r"\s*(?P<value>[^;；,，\n)）]*)", raw)
    declared = {}
    for clause in clauses:
        key, value = clause["key"], clause["value"].strip()
        identity_key = (_norm(key) in names or declared_context_role({"column_name": key,
            "column_comment": key}) == "identity" or key in {"租户", "命名空间", "定义版本", "定义修订"})
        if not identity_key:
            continue
        if (clause["operator"] not in {"=", ":", "："}
                or not re.fullmatch(r"[^\s=<>!:：()（）]+", value)
                or key in declared and declared[key] != value):
            raise ValueError("Identity qualifier declaration is unsupported or conflicting")
        declared[key] = value
    return declared


def _checked_identity_qualifiers(data, table, raw, decision, coordinates):
    """Qualify a source-field type only by declared, constant source identity."""
    evidence_ids = []
    qualifiers = dict(sorted(decision.identity_qualifiers.items()))
    declared = _declared_identity_qualifiers(table, raw)
    if qualifiers != declared:
        raise ValueError("Identity qualifiers must preserve every explicit source identity declaration")
    for key, value in qualifiers.items():
        column = decision.scope_bindings.get(key)
        clause = (re.escape(key) + r"\s*[=:：]\s*" + re.escape(value)
                  + r"(?=$|[\s,，。;；)）])")
        if (not key.strip() or not value.strip() or not re.search(clause, raw)
                or column not in coordinates or column not in table["column_names"]):
            raise ValueError("Identity qualifier requires a literal field declaration and an explicit coordinate binding")
        # Inspect complete input, never infer constancy from packet samples.
        values = data.db.execute(f"SELECT DISTINCT {qi(column)} FROM {qi(table['sql_name'])} LIMIT 2").fetchall()
        if values != [(value,)]:
            raise ValueError("Identity qualifier coordinate is not one declared constant across the full input")
        evidence_id = "field_identity:" + digest([data.snapshot_id, table["name"], column, key, value])[:24]
        data.evidence[evidence_id] = {"id": evidence_id, "origin": "observed_identity_column",
            "raw_fragment": value, "raw_fragment_truncated": False,
            "source_ref": {"table": table["name"], "column": column, "snapshot_id": data.snapshot_id},
            "verification": {"scan_scope": "full_input", "distinct_nonblank_values": 1,
                             "identity_key": key, "source_field_declaration": raw}}
        evidence_ids.append(evidence_id)
    return qualifiers, evidence_ids


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
    qualifiers, identity_evidence_ids = _checked_identity_qualifiers(data, table, raw, decision, coordinates)
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
    identity_signature = [table["name"], name, raw, label, object_, quantity, unit]
    if qualifiers:
        identity_signature.append(qualifiers)
    type_id = "type:fact_field:" + digest(identity_signature)[:24]
    evidence_ids = [declaration["evidence_id"], *unit_evidence_ids, *identity_evidence_ids]
    item = DerivedType(id=type_id, parent="Metric", label=label, definition=definition,
                       category="business_type", evidence_scope="source_schema", unit=unit,
                       identity_qualifiers=qualifiers,
                       evidence_ids=evidence_ids,
                       semantic_parameters={"business_object_quote": object_, "quantity_quote": quantity,
                                            "calculation_status": "unknown", "identity_basis": "source_field_declaration"},
                       source_properties=[{"role": "name", "source_table": table["name"],
                                           "source_column": name, "evidence_ids": [declaration["evidence_id"]]}])
    binding = FactFieldBindingDecision(status="bind", type_id=item.id, source_column_quote=raw,
                                       type_definition_quote=definition,
                                       source_definition_evidence_id=declaration["evidence_id"], type_source_quote=raw,
                                       scope_bindings=decision.scope_bindings, unit_column=decision.unit_column)
    template = {"id": "field_binding_template:" + digest([table["name"], name, packet["contract_fingerprint"], qualifiers])[:24],
                "status": "accepted_field_template", "table": table["name"], "value_column": name,
                "source_snapshot_id": data.snapshot_id, "type_id": item.id,
                "type_signature": digest(item.model_dump()),
                "schema_fingerprint": field_contract_fingerprint(table, name, coordinates),
                "contract_fingerprint": packet["contract_fingerprint"],
                "scope_bindings": decision.scope_bindings, "unit_status": "source_verified" if unit else "unknown",
                "identity_qualifiers": qualifiers, "identity_evidence_ids": identity_evidence_ids,
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


def _fact_schema_packets(packet, llm, max_packet_bytes):
    """Partition complete context units; every page retains identity/unit rules.

    All pages must agree before the original complete contract can compile.
    A single oversized declaration, row or link is left unresolved, never cut.
    """
    input_limit = getattr(llm, "config", {}).get("max_input_bytes", 100000)

    def sizes(value):
        payload_bytes = len(json.dumps(value, ensure_ascii=False).encode())
        request_bytes = (llm.request_bytes("fact_schema_induction", value, FactSchemaDecision)
                         if hasattr(llm, "request_bytes") else payload_bytes)
        return payload_bytes, request_bytes

    def fits(value):
        payload_bytes, request_bytes = sizes(value)
        return payload_bytes <= max_packet_bytes and request_bytes <= input_limit

    def oversized(value):
        payload_bytes, request_bytes = sizes(value)
        error = (InputBudgetExceeded(payload_bytes, max_packet_bytes, budget_name="max_packet_bytes")
                 if payload_bytes > max_packet_bytes else InputBudgetExceeded(request_bytes, input_limit))
        error.packet_bytes = payload_bytes
        error.structured_request_bytes = request_bytes
        return error

    if fits(packet):
        return [packet]
    keys = ("checked_association_conditions", "associated_definitions", "sample_rows")
    base = {key: deepcopy(value) for key, value in packet.items() if key not in keys}
    units, counts = [], {}
    for key in keys:
        # Identical JSON evidence is losslessly interned; distinct conditions,
        # directions, source records and complete verification stay separate.
        unique = {digest(value): value for value in packet.get(key, [])}
        counts[key] = {"original": len(packet.get(key, [])), "distinct": len(unique)}
        units.extend((key, value) for value in unique.values())
    maximum_pages = max(1, len(units))

    def page(values, number, total=maximum_pages):
        return {**base, **{key: [deepcopy(value) for kind, value in values if kind == key]
                          for key in keys},
                "context_partition": {"page_index": number, "page_count": total,
                    "complete_units": counts,
                    "contract": "This is one complete-unit context page for the same field declaration. "
                                "No page alone accepts a type: every page must propose the same identity, "
                                "scope and unit before compilation. Do not invent or truncate evidence."}}

    grouped, current = [], []
    for unit in units:
        if fits(page([*current, unit], maximum_pages)):
            current.append(unit)
            continue
        if current:
            grouped.append(current)
            current = []
        if not fits(page([unit], maximum_pages)):
            raise oversized(page([unit], maximum_pages))
        current.append(unit)
    if current or not grouped:
        grouped.append(current)
    pages = [page(values, number, len(grouped)) for number, values in enumerate(grouped, 1)]
    if not all(fits(value) for value in pages):
        raise oversized(next(value for value in pages if not fits(value)))
    return pages


async def induce_fact_schema(data, fact_observations, core, llm, *, association_context=None,
                             column_role_candidates=None, max_calls=50, max_samples_per_field=5,
                             max_packet_bytes=16000, reusable_templates=()):
    """Bounded requests per unbound field; replay contracts across snapshots.

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
                    pages = _fact_schema_packets(packet, llm, max_packet_bytes)
                    step["context_pages"] = len(pages)
                    step["context_pages_reviewed"] = 0
                    if calls + len(pages) > max_calls:
                        raise ValueError("fact_schema_call_budget_exhausted")
                    decisions = []
                    for page in pages:
                        if calls >= max_calls:
                            raise ValueError("fact_schema_call_budget_exhausted")
                        before_calls = getattr(llm, "calls", None)
                        reservation = nullcontext()
                        if type(before_calls) is int and hasattr(llm, "reserve_calls"):
                            total = llm.config.get("max_calls", 100)
                            ceiling = min(total, before_calls + max_calls - calls)
                            reservation = llm.reserve_calls(total - ceiling, stage="fact_schema_induction")
                        try:
                            # Retries and structured-response repairs share the
                            # stage ceiling, not only the initial page request.
                            with reservation:
                                decision = await llm.ask("fact_schema_induction", page, FactSchemaDecision)
                        finally:
                            after_calls = getattr(llm, "calls", None)
                            if type(before_calls) is int and type(after_calls) is int:
                                calls += after_calls - before_calls
                        if type(before_calls) is not int:
                            calls += 1
                        decisions.append(decision)
                        step["context_pages_reviewed"] = len(decisions)
                    if len({digest(value.model_dump(exclude={"reason"})) for value in decisions}) != 1:
                        raise ValueError("fact_schema_context_pages_disagree")
                    decision = decisions[0]
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
            except InputBudgetExceeded as exc:
                step.update(error_type=type(exc).__name__, budget_kind="input_bytes",
                            reason="fact_schema_packet_exceeds_byte_budget",
                            request_bytes=getattr(exc, "structured_request_bytes", exc.request_bytes),
                            max_input_bytes=getattr(llm, "config", {}).get("max_input_bytes", 100000),
                            exceeded_limit=exc.budget_name,
                            packet_bytes=getattr(exc, "packet_bytes", None),
                            max_packet_bytes=max_packet_bytes)
            except BudgetExceeded as exc:
                step.update(error_type=type(exc).__name__, budget_kind="shared_calls_or_tokens",
                            reason=str(exc))
            except Exception as exc:
                step.update(error_type=type(exc).__name__, reason=str(exc) if isinstance(exc, ValueError) else "fact_schema_call_failed")
            steps.append(step)
    return {"plan": plan, "field_templates": templates, "steps": steps,
            "coverage": {"fields_considered": len(steps), "model_calls": calls,
                         "accepted_templates": len(templates), "reused_templates": reused,
                         "unresolved_fields": sum(item["status"] == "unresolved" for item in steps),
                         "per_row_llm_calls": 0,
                         "partial": any(item["status"] == "unresolved" for item in steps)}}
