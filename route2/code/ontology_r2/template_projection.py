"""Evidence checked definition templates and observed bindings.

Projection is deliberately not record identity. A reusable template can describe
many source records, but their identifiers, references and dimension values stay
on separate bindings. Literal templates are executable contracts, not regex or
Python supplied by the model; changed formulas, units and unlisted fields fail
closed and return to semantic review.
"""
from __future__ import annotations

import re
import json
from typing import Literal

from pydantic import Field

from .models import BuildPlan, DerivedType, Strict
from .semantic_validation import require_complete_definition
from .storage import digest


class ProjectionQuote(Strict):
    record_id: str
    column: str
    quote: str


class ProjectionSlot(Strict):
    name: str
    role: Literal["business_object", "measure", "dimension", "reference", "record_identity"]
    label: str
    target_type_id: str | None = None
    target_component: str | None = None
    # If supplied, a placeholder must equal this observed cell, not any string.
    source_column: str | None = None
    evidence: ProjectionQuote


class ProjectionField(Strict):
    column: str
    template: str


class ProjectionComponent(Strict):
    name: str
    role: Literal["business_object", "measure", "dimension"]
    label: str
    definition: str
    root_type: Literal["GeneralObject", "Measure", "Dimension"]
    label_evidence: ProjectionQuote
    definition_evidence: ProjectionQuote


class TemplateProjectionDecision(Strict):
    label: str
    root_type: Literal["GeneralObject", "Measure", "Metric", "Dimension", "Term"]
    definition: str
    label_evidence: list[ProjectionQuote]
    slots: list[ProjectionSlot] = Field(default_factory=list)
    components: list[ProjectionComponent] = Field(default_factory=list)
    field_templates: list[ProjectionField] = Field(default_factory=list)
    witness_record_ids: list[str]
    existing_type_id: str | None = None
    class_definition: ProjectionQuote | None = None
    # A row may declare its name, definition and grain in separate cells.
    # Quotes are retained individually; composition cannot invent prose.
    class_definition_evidence: list[ProjectionQuote] = Field(default_factory=list)
    definition_parameters: dict[str, str] = Field(default_factory=dict)
    applicability_scope: dict[str, str] = Field(default_factory=dict)


_SLOT = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_SEMANTIC = {"name", "alias", "description", "formula", "unit", "scope", "identity", "unknown"}


def _source_target_names(data, target):
    """Only complete, same-snapshot name/alias evidence authorizes captures."""
    values, evidence_ids = set(), set()
    for prop in target.source_properties:
        if prop.role not in {"name", "alias"}:
            continue
        for eid in prop.evidence_ids:
            evidence = data.evidence.get(eid) or {}
            ref = evidence.get("source_ref") or {}
            raw = evidence.get("raw_fragment")
            if (eid not in target.evidence_ids or evidence.get("origin") != "observed_record"
                    or evidence.get("raw_fragment_truncated") or not isinstance(raw, str)
                    or ref.get("snapshot_id") != data.snapshot_id
                    or ref.get("table") != prop.source_table or ref.get("column") != prop.source_column):
                continue
            names = [raw]
            if prop.role == "alias":
                try:
                    parsed = json.loads(raw)
                except (ValueError, TypeError):
                    parsed = None
                names = (parsed if isinstance(parsed, list) and all(isinstance(v, str) for v in parsed)
                         else re.split(r"[;,；，、|]", raw))
            values.update(name.strip() for name in names if name.strip())
            evidence_ids.add(eid)
    return {"type_id": target.id, "accepted_values": sorted(values),
            "evidence_ids": sorted(evidence_ids), "match": "literal_source_name_or_alias"}


def _slot_target_mismatches(slots, captures):
    """Fixed and variable measure bindings require the same endpoint proof."""
    return [slot["name"] for slot in slots if slot["role"] == "measure"
            and slot.get("target_type_id")
            and (slot.get("target_value_contract", {}).get("type_id") != slot["target_type_id"]
                 or not slot.get("target_value_contract", {}).get("evidence_ids")
                 or captures.get(slot["name"]) not in slot.get("target_value_contract", {}).get("accepted_values", []))]


def _instance_columns(data, record, slots=()):
    """Separate declared examples and technical bindings, not arbitrary JSON.

    Unknown JSON may contain a formula or business rule. Only an explicit
    example/sample declaration or an already checked binding role permits it
    to vary without changing the type. Its actual value stays on the binding.
    """
    values, roles = _fields(record)
    schema = getattr(data, "tables", {}).get(record["table"], {})
    columns = {item["column_name"]: item for item in schema.get("columns", [])}
    binding = {item["column"] for item in record.get("role_conflicts", []) if item.get("binding_only")}
    coordinates = {item["column"] for item in schema.get("inferred_semantic_roles", [])
                   if item.get("status") == "source_verified_role_candidate"
                   and item.get("role") == "dimension_coordinate"}
    direct_slots = {slot.get("source_column") or slot.get("value_evidence", {}).get("column"): slot
                    for slot in slots if slot.get("role") in {"record_identity", "dimension"}}
    # Reuse the independent declaration check already used by card scheduling.
    # A storage locator is instance data even when no model supplied its slot.
    from .semantic_cards import _definition_pattern_exclusions
    role_columns = {role: [column for column in values if role in roles[column]]
                    for role in {role for column_roles in roles.values() for role in column_roles}}
    physical_names = {item["column"] for item in _definition_pattern_exclusions(
        {**schema, "name": record["table"], "columns": schema.get("columns", [])}, role_columns)}
    result = {}
    for column, value in values.items():
        if roles[column] & {"formula", "unit", "identity"}:
            continue
        if column in binding or "reference" in roles[column]:
            result[column] = "source_checked_record_binding"
        elif column in physical_names:
            result[column] = "declared_physical_resource_name"
        elif (column in direct_slots and direct_slots[column].get("value_evidence", {}).get("quote") == value
              and (direct_slots[column]["role"] == "record_identity"
                   or column in coordinates
                   or re.search(r"(?:^|_)member_(?:code|name|cn_name|en_name|value)$", column, re.I)
                   or re.search(r"(?:维度|成员).*(?:取值|编码|名称)",
                                str(columns.get(column, {}).get("column_comment") or "")))):
            result[column] = "source_checked_observed_slot_value"
        elif (re.search(r"(?:^|_)(?:request|response)_(?:example|sample)(?:_|$)", column, re.I)
              or re.search(r"(?:请求|响应|返回)(?:报文|数据)?(?:示例|样例)",
                           str(columns.get(column, {}).get("column_comment") or ""))):
            try:
                if isinstance(json.loads(value), (dict, list)):
                    result[column] = "declared_structured_example"
            except (ValueError, TypeError):
                pass
    return result


def _fields(record):
    values, roles = {}, {}
    for role, entries in record.get("fields", {}).items():
        for entry in entries:
            if entry.get("truncated"):
                raise ValueError("Template projection requires complete source fields")
            column, value = entry["column"], str(entry["value"])
            if column in values and values[column] != value:
                raise ValueError("One source field has inconsistent values")
            values[column] = value
            roles.setdefault(column, set()).add(role)
    return values, roles


def _evidence(data, record, column, quote):
    values, _ = _fields(record)
    if not quote or column not in values or quote not in values[column]:
        raise ValueError("Projection quote is absent from the complete source field")
    evidence_id = "record:" + digest([data.snapshot_id, record["record_id"], column])[:24]
    previous = data.evidence.get(evidence_id)
    if previous and previous.get("raw_fragment") != values[column]:
        raise ValueError("Projection source evidence changed within the same snapshot")
    data.evidence[evidence_id] = {
        "id": evidence_id, "origin": "observed_record", "raw_fragment": values[column],
        "raw_fragment_truncated": False,
        "source_ref": {"table": record["table"], "record_id": record["record_id"],
                       "row": record.get("row_number"), "column": column,
                       "snapshot_id": data.snapshot_id},
    }
    return evidence_id


def _match_text(pattern, value, slots, values, captures):
    """Match escaped literals and bounded cells; never execute model regex."""
    pieces, offset = [], 0
    local = set()
    for match in _SLOT.finditer(pattern):
        name = match.group(1)
        if name not in slots:
            raise ValueError("Field template names an undeclared slot")
        pieces.append(re.escape(pattern[offset:match.start()]))
        slot = slots[name]
        bound = captures.get(name)
        if slot.get("source_column"):
            bound = values.get(slot["source_column"])
            if not bound:
                return None
            if name in captures and captures[name] != bound:
                return None
        if bound is not None:
            pieces.append(re.escape(bound))
        elif name in local:
            pieces.append(f"(?P={name})")
        else:
            if offset and not pattern[offset:match.start()] and not slot.get("source_column"):
                raise ValueError("Adjacent unbound slots are ambiguous")
            pieces.append(f"(?P<{name}>.{{1,256}}?)")
            local.add(name)
        offset = match.end()
    pieces.append(re.escape(pattern[offset:]))
    matched = re.fullmatch("".join(pieces), value, flags=re.DOTALL)
    if not matched:
        return None
    found = {**captures, **{key: value for key, value in matched.groupdict().items() if value is not None}}
    for name, slot in slots.items():
        if slot.get("source_column") and slot["source_column"] in values:
            if name in found and found[name] != values[slot["source_column"]]:
                return None
            found[name] = values[slot["source_column"]]
    return found


def match_projection(template, record):
    """Return an observed binding candidate, or None on any uncovered change."""
    if record.get("table") != template["source_table"]:
        return None
    values, roles = _fields(record)
    instance_columns = template.get("instance_columns", {})
    if any(roles.get(column, set()) & {"formula", "unit", "identity"} for column in instance_columns):
        return None  # A later role change cannot bypass protected semantics.
    semantic = {column: value for column, value in values.items()
                if roles[column] & _SEMANTIC and column not in instance_columns}
    if set(semantic) != set(template["semantic_columns"]):
        return None
    if any(semantic.get(column) != value for column, value in template["invariants"].items()):
        return None
    slots = {item["name"]: item for item in template["slots"]}
    captures = {name: slot["fixed_value"] for name, slot in slots.items() if "fixed_value" in slot}
    pending = list(template["field_templates"])
    while pending:
        progressed = False
        for item in pending[:]:
            if item["column"] not in values:
                return None
            try:
                matched = _match_text(item["template"], values[item["column"]], slots, values, captures)
            except ValueError as exc:
                if str(exc) == "Adjacent unbound slots are ambiguous":
                    continue  # An anchored field may bind these same slots first.
                raise
            if matched is None:
                return None
            captures = matched
            pending.remove(item)
            progressed = True
        if not progressed:
            raise ValueError("Adjacent unbound slots are ambiguous")
    if not set(slots) <= set(captures):
        return None
    if _slot_target_mismatches(template["slots"], captures):
        return None
    declarations = ([template["class_definition"]] if template.get("class_definition") else [])
    declarations += template.get("class_definition_evidence", [])
    instance_contracts = {(item["column"], item["quote"]): item["template"]
                          for item in template.get("definition_instance_contracts", [])}
    for citation in declarations:
        try:
            require_complete_definition(
                values.get(citation["column"], ""), citation["quote"],
                retained_values=(*template.get("definition_parameters", {}).values(),
                                 *template.get("applicability_scope", {}).values()),
                instance_template=instance_contracts.get((citation["column"], citation["quote"])),
                instance_values=captures)
        except ValueError:
            return None
    return {"record_id": record["record_id"], "source_table": record["table"],
            "row_number": record.get("row_number"), "source_card_id": record.get("card_id"),
            "slot_values": captures,
            "instance_values": {column: values[column] for column in instance_columns if column in values},
            "reference_values": {column: value for column, value in values.items()
                                 if "reference" in roles[column]},
            "mapping_kind": "template_instance", "identity_claim": "none"}


def bind_projection(data, template, record):
    candidate = match_projection(template, record)
    if candidate is None:
        return None
    values, roles = _fields(record)
    evidence_ids = [_evidence(data, record, column, value) for column, value in values.items()
                    if value and roles[column] & _SEMANTIC]
    return {**candidate,
            "id": "template_binding:" + digest([data.snapshot_id, template["template_id"], record["record_id"]])[:24],
            "snapshot_id": data.snapshot_id, "template_id": template["template_id"],
            "object_type_id": template["object_type_id"], "evidence_ids": sorted(evidence_ids),
            "status": "accepted", "method": "complete_template_contract_match"}


def compile_projection(data, profile, core, bundle, decision):
    """Compile a reviewed semantic projection without asserting exact identity."""
    records = {record["record_id"]: record for record in bundle.get("records", [])}
    if (not decision.witness_record_ids or len(set(decision.witness_record_ids)) != len(decision.witness_record_ids)
            or not set(decision.witness_record_ids) <= set(records)):
        raise ValueError("Projection requires distinct observed witness records from this bundle")
    witnesses = [records[key] for key in decision.witness_record_ids]
    if len({record["table"] for record in witnesses}) != 1:
        raise ValueError("A projection matcher belongs to one source schema")
    if any(record.get("context_role") == "related_context" for record in witnesses):
        raise ValueError("Joined context cannot silently become a template witness")
    from .classification_attributes import source_classifications
    classifications, classification_evidence = source_classifications(data, witnesses)
    if any(source_classifications(data, [record])[0] != classifications for record in witnesses):
        raise ValueError("Projection witnesses have different source classification attributes")
    decision = decision.model_copy(update={
        "applicability_scope": {key: value for key, value in decision.applicability_scope.items()
                                if key not in classifications},
        "definition_parameters": {key: value for key, value in decision.definition_parameters.items()
                                  if key not in classifications}})
    if not decision.label.strip() or not decision.definition.strip() or not decision.label_evidence:
        raise ValueError("Projection requires a label, definition and source-grounded label fragments")
    evidence_ids, fragments = [], []
    for item in decision.label_evidence:
        if item.record_id not in records:
            raise ValueError("Projection label evidence refers outside its bundle")
        evidence_ids.append(_evidence(data, records[item.record_id], item.column, item.quote))
        fragments.append(item.quote)
    # Abstract names can concatenate source-grounded semantic fragments,
    # but cannot erase a qualifier and pretend the result was an exact record.
    label_remaining = "" if any(decision.label in fragment for fragment in fragments) else decision.label
    for fragment in sorted(fragments, key=len, reverse=True):
        label_remaining = label_remaining.replace(fragment, "")
    if label_remaining.strip(" -_·/（）()"):
        raise ValueError("Every projected label fragment requires source evidence")
    slots = [item.model_dump() for item in decision.slots]
    if len({item["name"] for item in slots}) != len(slots):
        raise ValueError("Duplicate projection slot")
    roots = {item["id"] for item in profile["object_roots"]}
    known = {item.id: item for item in core.object_types}
    candidate = core.model_copy(deep=True)
    components = {}
    for component in decision.components:
        if component.name in components:
            raise ValueError("Duplicate projected component")
        expected = {"business_object": "GeneralObject", "measure": "Measure", "dimension": "Dimension"}
        if component.root_type != expected[component.role]:
            raise ValueError("Projected component role conflicts with its root")
        ceids, properties, component_qualifiers = [], [], {}
        if component.definition_evidence.record_id in records:
            source_values, _ = _fields(records[component.definition_evidence.record_id])
            require_complete_definition(source_values.get(component.definition_evidence.column, ""),
                                        component.definition_evidence.quote)
        for citation, text in ((component.label_evidence, component.label),
                               (component.definition_evidence, component.definition)):
            if citation.record_id not in records or text != citation.quote:
                raise ValueError("Component label and definition must be quoted source fragments")
            rec = records[citation.record_id]
            for qualifier in rec.get("fields", {}).get("identity", []):
                column, value = qualifier["column"], str(qualifier["value"])
                if column in component_qualifiers and component_qualifiers[column] != value:
                    raise ValueError("Component citations have conflicting definition identity qualifiers")
                component_qualifiers[column] = value
                qeid = _evidence(data, rec, column, value)
                ceids.append(qeid)
                properties.append({"role": "identity", "source_table": rec["table"],
                                   "source_column": column, "evidence_ids": [qeid]})
            eid = _evidence(data, rec, citation.column, citation.quote)
            ceids.append(eid)
            roles_for_column = _fields(rec)[1][citation.column]
            role = next((r for r in ("name", "alias", "description", "formula", "unit", "scope") if r in roles_for_column), None)
            if role:
                properties.append({"role": role, "source_table": rec["table"],
                                   "source_column": citation.column, "evidence_ids": [eid]})
        component_identity = ["projected_component", component.root_type,
                              component.label, component.definition]
        if component_qualifiers:
            component_identity.append({"identity_qualifiers": component_qualifiers})
        component_id = "type:" + digest(component_identity)[:24]
        prior = next((item for item in candidate.object_types if item.parent == component.root_type
                      and item.label == component.label and item.definition == component.definition
                      and item.identity_qualifiers == component_qualifiers), None)
        if prior:
            component_id = prior.id
        else:
            item = DerivedType(id=component_id, parent=component.root_type, label=component.label,
                               definition=component.definition, category="business_type",
                               evidence_ids=sorted(set(ceids)), source_properties=properties,
                               identity_qualifiers=component_qualifiers,
                               derivation_kind="template_projection", evidence_scope="projected_definition_template")
            candidate.object_types.append(item)
            known[item.id] = item
        components[component.name] = {"type_id": component_id, "role": component.role,
                                      "evidence_ids": sorted(set(ceids))}
    for slot in slots:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", slot["name"]):
            raise ValueError("Projection slot names must be snake_case")
        witness = slot.pop("evidence")
        slot["value_evidence"] = witness
        if witness["record_id"] not in records:
            raise ValueError("Slot evidence refers outside its bundle")
        slot["evidence_ids"] = [_evidence(data, records[witness["record_id"]], witness["column"], witness["quote"])]
        if not any(slot["name"] in _SLOT.findall(item.template) for item in decision.field_templates):
            slot["fixed_value"] = witness["quote"]
        evidence_ids.extend(slot["evidence_ids"])
        if slot["target_component"]:
            component = components.get(slot["target_component"])
            if component is None or component["role"] != slot["role"]:
                raise ValueError("Slot component is absent or has a different semantic role")
            if slot["target_type_id"] and slot["target_type_id"] != component["type_id"]:
                raise ValueError("Slot type and component targets disagree")
            slot["target_type_id"] = component["type_id"]
            slot["evidence_ids"] = sorted(set(slot["evidence_ids"] + component["evidence_ids"]))
        if slot["target_type_id"] and slot["target_type_id"] not in known:
            raise ValueError("Slot target must be an existing accepted object type")
        if slot["role"] == "measure" and slot.get("target_type_id"):
            target = known[slot["target_type_id"]]
            parent, seen = target.parent, {target.id}
            while parent in known and parent not in seen:
                seen.add(parent)
                parent = known[parent].parent
            if parent != "Measure":
                raise ValueError("A measure slot target must derive from Measure")
            slot["target_value_contract"] = _source_target_names(data, target)
            if not slot["target_value_contract"]["accepted_values"]:
                kind = "fixed" if "fixed_value" in slot else "variable"
                raise ValueError(f"A {kind} measure target requires complete source name or alias evidence")
    orphan_components = sorted(set(components) - {slot["target_component"] for slot in slots
                                                   if slot["target_component"]})
    if orphan_components:
        raise ValueError(
            "Orphan projected components: " + ", ".join(orphan_components)
            + "; set a same-role slot.target_component to each exact component.name, "
            "or remove the component if its connection lacks source evidence")
    field_templates = [item.model_dump() for item in decision.field_templates]
    if len({item["column"] for item in field_templates}) != len(field_templates):
        raise ValueError("Duplicate projected field")
    values, roles = _fields(witnesses[0])
    instance_columns = _instance_columns(data, witnesses[0], slots)
    # Every witness must independently support the same instance-field roles.
    instance_columns = {column: basis for column, basis in instance_columns.items()
                        if all(_instance_columns(data, record, slots).get(column) == basis
                               or (basis == "source_checked_observed_slot_value" and column in _fields(record)[0])
                               for record in witnesses)}
    # A reviewed instance/member slot is captured from each row; retaining its
    # original witness as a fixed value would silently replay the wrong member.
    for slot in slots:
        column = slot.get("source_column") or slot.get("value_evidence", {}).get("column")
        if (column in instance_columns and slot.get("value_evidence", {}).get("quote") == values.get(column)
                and slot["role"] in {"record_identity", "reference", "dimension"}):
            slot.pop("fixed_value", None)
            slot["source_column"] = column
            existing_field = next((item for item in field_templates if item["column"] == column), None)
            if existing_field is not None and not _SLOT.search(existing_field["template"]):
                existing_field["template"] = "{" + slot["name"] + "}"
            elif existing_field is None:
                field_templates.append({"column": column, "template": "{" + slot["name"] + "}"})
    field_templates = [item for item in field_templates
                       if item["column"] not in instance_columns or _SLOT.search(item["template"])]
    semantic = {column: value for column, value in values.items()
                if roles[column] & _SEMANTIC and column not in instance_columns}
    protected = {column for column, rs in roles.items() if rs & {"formula", "unit", "identity"}}
    protected.update(classifications)
    protected.update(item["column"] for item in witnesses[0].get("calculation_fragments", [])
                     if item.get("effective_role") == "calculation_operator")
    for item in field_templates:
        if item["column"] not in values:
            raise ValueError("Projected field is absent from the source record")
        if item["column"] in protected and _SLOT.search(item["template"]):
            raise ValueError("Formula, unit, definition identity, classification and calculation operators cannot be wildcarded")
        if _SLOT.search(item["template"]) and re.sub(_SLOT, "", item["template"]).count("{"):
            raise ValueError("A projected field needs declared literal placeholders")
    # Explicit literal fields remain invariants, including formula/unit parameters.
    changed = {item["column"] for item in field_templates if _SLOT.search(item["template"])}
    template = {"snapshot_id": data.snapshot_id,
                "source_table": witnesses[0]["table"], "root_type": decision.root_type,
                "label": decision.label, "definition": decision.definition,
                "semantic_columns": sorted(semantic), "invariants": {k: v for k, v in semantic.items() if k not in changed},
                "instance_columns": instance_columns,
                "field_templates": field_templates, "slots": slots,
                "evidence_ids": sorted(set(evidence_ids)), "status": "accepted",
                "identity_claim": "projection_not_exact_source_identity"}
    for name, value in decision.definition_parameters.items():
        if not name or value not in template["invariants"].values():
            raise ValueError("Definition parameter must retain a complete invariant source value")
    template["definition_parameters"] = dict(decision.definition_parameters)
    template["classification_attributes"] = classifications
    template["classification_attribute_evidence"] = classification_evidence
    # Definition namespace/version is preserved independently of business scope.
    identity_qualifiers = {column: value for column, value in values.items()
                           if "identity" in roles[column]}
    template["identity_qualifiers"] = identity_qualifiers
    for column, value in decision.applicability_scope.items():
        if not value or column in changed or column in instance_columns:
            raise ValueError("Projection applicability scope must use complete invariant scope fields")
        for record in witnesses:
            raw_values, raw_roles = _fields(record)
            if "scope" not in raw_roles.get(column, set()) or raw_values.get(column) != value:
                raise ValueError("Projection applicability scope is absent from a witness scope field")
        if any(slot["role"] == "dimension" and (
                slot.get("source_column") == column
                or slot.get("value_evidence", {}).get("column") == column) for slot in slots):
            raise ValueError("An observed dimension slot cannot become class applicability scope")
    template["applicability_scope"] = dict(decision.applicability_scope)
    source_properties = {}
    for record in witnesses:
        raw_values, raw_roles = _fields(record)
        for column, role_set in raw_roles.items():
            for role in role_set & {"name", "alias", "description", "formula", "unit", "scope",
                                   "metadata", "provenance", "identity"}:
                if role == "scope" and column in classifications:
                    role = "classification"
                eid = _evidence(data, record, column, raw_values[column])
                source_properties.setdefault((role, record["table"], column), set()).add(eid)
                evidence_ids.append(eid)
    properties = [{"role": role, "source_table": table, "source_column": column,
                   "evidence_ids": sorted(eids)}
                  for (role, table, column), eids in sorted(source_properties.items())]
    template["evidence_ids"] = sorted(set(evidence_ids))
    template["source_properties"] = properties
    bindings = [match_projection(template, record) for record in witnesses]
    if any(item is None for item in bindings):
        raise ValueError("Every witness must match all literals, invariants and shared slot bindings")
    bindings_by_record = {item["record_id"]: item for item in bindings}
    for source_slot, compiled_slot in zip(decision.slots, slots):
        if "fixed_value" in compiled_slot:
            continue
        # Context is allowed, but the literal captured value must be supported
        # on this witness's actual matching path; never infer a name/code map.
        citation = source_slot.evidence
        binding = bindings_by_record.get(citation.record_id)
        captured = binding["slot_values"].get(source_slot.name) if binding else None
        field_paths = {item["column"] for item in field_templates
                       if source_slot.name in _SLOT.findall(item["template"])}
        if source_slot.source_column:
            field_paths.add(source_slot.source_column)
        if not captured or captured not in citation.quote or citation.column not in field_paths:
            raise ValueError(
                "Variable slot evidence must support its captured value in the cited witness and matching field path")
        compiled_slot["witness_value"] = captured
    # A new generalized matcher must be witnessed by variation. A separately
    # accepted type can instead supply a structural instantiation contract.
    declared_class = False
    retained_definitions = (*decision.definition_parameters.values(), *decision.applicability_scope.values())

    def check_definition_cell(citation):
        """Check all witnesses so one instance section cannot hide a limit."""
        pattern = next((item["template"] for item in field_templates if item["column"] == citation.column), None)
        names = _SLOT.findall(pattern or "")
        eligible_slots = {slot["name"]: slot for slot in slots
                          if slot["role"] in {"record_identity", "dimension"}
                          and slot.get("source_column") in instance_columns}
        if (len(witnesses) < 2 or not set(names) <= set(eligible_slots)
                or not any(len({binding["slot_values"].get(name) for binding in bindings}) > 1
                           for name in names)):
            pattern = None
        for record in witnesses:
            raw_values, _ = _fields(record)
            if citation.column not in raw_values:
                raise ValueError("Definition source column is absent from a witness")
            require_complete_definition(
                raw_values[citation.column], citation.quote, retained_values=retained_definitions,
                instance_template=pattern,
                instance_values=bindings_by_record[record["record_id"]]["slot_values"] if pattern else None)
        return pattern

    if decision.class_definition is not None:
        citation = decision.class_definition
        same_row_name = any(item.record_id == citation.record_id
                            and decision.label in item.quote
                            and _fields(records[item.record_id])[1].get(item.column, set()) & {"name", "alias"}
                            for item in decision.label_evidence)
        if (citation.record_id not in {record["record_id"] for record in witnesses}
                or citation.quote != decision.definition
                or "description" not in _fields(records[citation.record_id])[1].get(citation.column, set())
                or not (decision.label in citation.quote or same_row_name)):
            raise ValueError("Class definition requires a source description and its same-record class label")
        pattern = check_definition_cell(citation)
        if pattern:
            template.setdefault("definition_instance_contracts", []).append({
                "column": citation.column, "quote": citation.quote, "template": pattern})
        evidence_ids.append(_evidence(data, records[citation.record_id], citation.column, citation.quote))
        template["evidence_ids"] = sorted(set(evidence_ids))
        template["class_definition"] = citation.model_dump()
        declared_class = True
    if decision.class_definition_evidence:
        citations = decision.class_definition_evidence
        record_ids = {item.record_id for item in citations}
        if len(record_ids) != 1 or not record_ids <= {record["record_id"] for record in witnesses}:
            raise ValueError("Composed class definition must use one complete witness record")
        rec = records[next(iter(record_ids))]
        raw_values, raw_roles = _fields(rec)
        if (decision.definition != "\n".join(item.quote for item in citations)
                or not any("description" in raw_roles.get(item.column, set()) for item in citations)
                or not any(item.record_id in record_ids and decision.label in item.quote
                           and raw_roles.get(item.column, set()) & {"name", "alias"}
                           for item in decision.label_evidence)):
            raise ValueError("Composed definition must retain quoted name, description and source constraint cells")
        for item in citations:
            if (not raw_roles.get(item.column, set()) & {"name", "alias", "description", "formula", "scope", "identity", "unit"}
                    or item.column in instance_columns):
                raise ValueError("Composed definition cannot use partial or instance-only source cells")
            pattern = check_definition_cell(item)
            if pattern:
                template.setdefault("definition_instance_contracts", []).append({
                    "column": item.column, "quote": item.quote, "template": pattern})
            evidence_ids.append(_evidence(data, rec, item.column, item.quote))
        template["class_definition_evidence"] = [item.model_dump() for item in citations]
        template["evidence_ids"] = sorted(set(evidence_ids))
        declared_class = True
    if not declared_class and decision.root_type not in {"Metric", "GeneralObject"}:
        # Measure/Dimension/Term have the same source completeness contract;
        # omitting the explicit class citation must not open a weaker path.
        for record in witnesses:
            descriptions = [str(entry["value"]) for entry in record.get("fields", {}).get("description", [])]
            matches = [raw for raw in descriptions if decision.definition in raw]
            if not matches:
                raise ValueError("Projected definition requires a complete source description")
            if not any(raw == decision.definition or raw in retained_definitions for raw in matches):
                require_complete_definition(matches[0], decision.definition,
                                            retained_values=retained_definitions)
    if any("fixed_value" not in slot for slot in slots) and not decision.existing_type_id and not declared_class:
        if len(witnesses) < 2 or not any(len({item["slot_values"][slot["name"]] for item in bindings}) > 1 for slot in slots):
            raise ValueError("A new template requires observed variation, not one invented generalization")
    if decision.root_type in {"Metric", "GeneralObject"} and not decision.existing_type_id and not declared_class:
        shared_definition = decision.label in decision.definition and all(
            any(decision.definition == str(entry["value"])
                for entry in record.get("fields", {}).get("description", []))
            for record in witnesses)
        varying = any(len({item["slot_values"][slot["name"]] for item in bindings}) > 1 for slot in slots)
        if len(witnesses) < 2 or not varying or not shared_definition:
            raise ValueError(
                "A new Metric or GeneralObject requires a quoted class_definition or multiple "
                "varying witnesses sharing the same source definition including its class label")
    if decision.root_type == "Metric":
        for slot in slots:
            if (slot["role"] == "measure" and not slot.get("target_value_contract")
                    and len({item["slot_values"][slot["name"]] for item in bindings}) > 1):
                raise ValueError("Different quantity meanings cannot be wildcarded into one Metric")
    # Different witnessed spellings are safe only when every capture passed
    # the target's source-name/alias contract in match_projection above.
    source_formulas = {values[column].strip() for column, rs in roles.items() if "formula" in rs}
    def compatible_type(existing):
        existing_formulas = {str(data.evidence[eid].get("raw_fragment", "")).strip()
                             for prop in existing.source_properties if prop.role == "formula"
                             for eid in prop.evidence_ids if eid in data.evidence}
        return (existing.category == "business_type" and existing.parent == decision.root_type
                and existing.label == decision.label and existing.definition == decision.definition
                and (existing.unit or "") == (witnesses[0].get("unit") or "")
                and existing.definition_parameters == decision.definition_parameters
                and existing.identity_qualifiers == identity_qualifiers
                and existing.classification_attributes == classifications
                and existing.applicability_scope == decision.applicability_scope
                and existing_formulas == source_formulas and existing.aggregation_operator is None)
    compatible = [item for item in core.object_types if compatible_type(item)]
    existing_type_id = decision.existing_type_id or (compatible[0].id if len(compatible) == 1 else None)
    if existing_type_id:
        existing = known.get(existing_type_id)
        existing_formulas = {str(data.evidence[eid].get("raw_fragment", "")).strip()
                             for prop in (existing.source_properties if existing else []) if prop.role == "formula"
                             for eid in prop.evidence_ids if eid in data.evidence}
        source_formulas = {values[column].strip() for column, rs in roles.items() if "formula" in rs}
        if (existing is None or existing.category != "business_type"
                or existing.parent != decision.root_type or existing.label != decision.label
                or existing.definition != decision.definition
                or (existing.unit or "") != (witnesses[0].get("unit") or "")
                or existing.definition_parameters != decision.definition_parameters
                or existing.identity_qualifiers != identity_qualifiers
                or existing.classification_attributes != classifications
                or existing.applicability_scope != decision.applicability_scope
                or existing_formulas != source_formulas):
            raise ValueError("Template target must match an existing type definition, unit, formula, parameters and applicability scope")
        template["object_type_id"] = existing.id
    else:
        if decision.root_type not in roots:
            raise ValueError("Unknown template root")
        # A source matcher is not an ontology identity. Physical columns,
        # reference bindings and instance structure never mint another class.
        formulas = sorted({values[column] for column, rs in roles.items() if "formula" in rs})
        identity = [decision.root_type, decision.label, decision.definition,
                    witnesses[0].get("unit") or None, formulas, decision.definition_parameters]
        if decision.applicability_scope:
            identity.append({"applicability_scope": decision.applicability_scope})
        if identity_qualifiers:
            identity.append({"identity_qualifiers": identity_qualifiers})
        if classifications:
            identity.append({"classification_attributes": classifications})
        template["object_type_id"] = "type:" + digest(identity)[:24]
        if template["object_type_id"] not in known:
            candidate.object_types.append(DerivedType(
                id=template["object_type_id"], parent=decision.root_type, label=decision.label,
                definition=decision.definition, category="business_type", evidence_ids=template["evidence_ids"],
                derivation_kind="template_projection", evidence_scope="projected_definition_template",
                unit=witnesses[0].get("unit") or None,
                definition_parameters=decision.definition_parameters,
                identity_qualifiers=identity_qualifiers,
                classification_attributes=classifications,
                applicability_scope=decision.applicability_scope, source_properties=properties))
    # Add projection mappings without inventing exact record-to-type identity.
    current = next(item for item in candidate.object_types if item.id == template["object_type_id"])
    known_properties = {(item.role, item.source_table, item.source_column): item.model_dump()
                        for item in current.source_properties}
    for item in properties:
        key = (item["role"], item["source_table"], item["source_column"])
        if key in known_properties:
            known_properties[key]["evidence_ids"] = sorted(set(known_properties[key]["evidence_ids"]) | set(item["evidence_ids"]))
        else:
            known_properties[key] = item
    updated = current.model_copy(update={"evidence_ids": sorted(set(current.evidence_ids) | set(template["evidence_ids"]))})
    updated = DerivedType.model_validate({**updated.model_dump(), "source_properties": list(known_properties.values())})
    candidate.object_types[candidate.object_types.index(current)] = updated
    template["contract_hash"] = digest([template["source_table"], template["object_type_id"],
                                        template["semantic_columns"], template["invariants"], field_templates, slots,
                                        instance_columns, template.get("definition_instance_contracts", [])])
    template["template_id"] = "projection:" + template["contract_hash"][:24]
    return candidate, template, [bind_projection(data, template, record) for record in witnesses]


def reuse_projection(data, templates, bundle):
    """Resolve one representative only; ambiguity must return to the model."""
    allowed = set(bundle.get("exact_alignment_record_ids") or ())
    seed = next((record for record in bundle.get("records", [])
                 if record.get("context_role") != "related_context"
                 and (not allowed or record["record_id"] in allowed)), None)
    if seed is None:
        return None
    matches = [template for template in templates if match_projection(template, seed) is not None]
    if len({item["object_type_id"] for item in matches}) != 1:
        return None
    endpoints = {}
    for template in matches:
        for slot in template["slots"]:
            if slot.get("target_type_id"):
                endpoints.setdefault((slot["role"], slot["name"]), set()).add(slot["target_type_id"])
    if any(len(targets) > 1 for targets in endpoints.values()):
        return None  # One source row cannot silently choose conflicting accepted endpoints.
    chosen = min(matches, key=lambda item: (
        sum(slot["role"] in {"business_object", "measure", "dimension"}
            and not slot.get("target_type_id") for slot in item["slots"]),
        -sum(bool(slot.get("target_type_id")) for slot in item["slots"]), item["template_id"]))
    return bind_projection(data, chosen, seed)
