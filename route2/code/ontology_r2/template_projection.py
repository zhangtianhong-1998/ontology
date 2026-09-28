"""Evidence checked definition templates and observed bindings.

Projection is deliberately not record identity. A reusable template can describe
many source records, but their identifiers, references and dimension values stay
on separate bindings. Literal templates are executable contracts, not regex or
Python supplied by the model; changed formulas, units and unlisted fields fail
closed and return to semantic review.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import Field

from .models import BuildPlan, DerivedType, Strict
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
    definition_parameters: dict[str, str] = Field(default_factory=dict)


_SLOT = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_SEMANTIC = {"name", "alias", "description", "formula", "unit", "scope", "unknown"}


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
    semantic = {column: value for column, value in values.items() if roles[column] & _SEMANTIC}
    if set(semantic) != set(template["semantic_columns"]):
        return None
    if any(semantic.get(column) != value for column, value in template["invariants"].items()):
        return None
    slots = {item["name"]: item for item in template["slots"]}
    captures = {name: slot["fixed_value"] for name, slot in slots.items() if "fixed_value" in slot}
    for item in template["field_templates"]:
        if item["column"] not in values:
            return None
        captures = _match_text(item["template"], values[item["column"]], slots, values, captures)
        if captures is None:
            return None
    if not set(slots) <= set(captures):
        return None
    return {"record_id": record["record_id"], "source_table": record["table"],
            "row_number": record.get("row_number"), "source_card_id": record.get("card_id"),
            "slot_values": captures,
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
    if not decision.label.strip() or not decision.definition.strip() or not decision.label_evidence:
        raise ValueError("Projection requires a label, definition and source-grounded label fragments")
    evidence_ids, fragments = [], []
    for item in decision.label_evidence:
        if item.record_id not in records:
            raise ValueError("Projection label evidence refers outside its bundle")
        evidence_ids.append(_evidence(data, records[item.record_id], item.column, item.quote))
        fragments.append(item.quote)
    # Abstract names can concatenate grounded fragments (fruit + quality rate),
    # but cannot erase a qualifier and pretend the result was an exact record.
    label_remaining = decision.label
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
        ceids, properties = [], []
        for citation, text in ((component.label_evidence, component.label),
                               (component.definition_evidence, component.definition)):
            if citation.record_id not in records or text != citation.quote:
                raise ValueError("Component label and definition must be quoted source fragments")
            rec = records[citation.record_id]
            eid = _evidence(data, rec, citation.column, citation.quote)
            ceids.append(eid)
            roles_for_column = _fields(rec)[1][citation.column]
            role = next((r for r in ("name", "alias", "description", "formula", "unit", "scope") if r in roles_for_column), None)
            if role:
                properties.append({"role": role, "source_table": rec["table"],
                                   "source_column": citation.column, "evidence_ids": [eid]})
        component_id = "type:" + digest(["projected_component", component.root_type,
                                         component.label, component.definition])[:24]
        prior = next((item for item in candidate.object_types if item.parent == component.root_type
                      and item.label == component.label and item.definition == component.definition), None)
        if prior:
            component_id = prior.id
        else:
            item = DerivedType(id=component_id, parent=component.root_type, label=component.label,
                               definition=component.definition, category="business_type",
                               evidence_ids=sorted(set(ceids)), source_properties=properties,
                               derivation_kind="template_projection", evidence_scope="projected_definition_template")
            candidate.object_types.append(item)
            known[item.id] = item
        components[component.name] = {"type_id": component_id, "role": component.role,
                                      "evidence_ids": sorted(set(ceids))}
    for slot in slots:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", slot["name"]):
            raise ValueError("Projection slot names must be snake_case")
        witness = slot.pop("evidence")
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
    field_templates = [item.model_dump() for item in decision.field_templates]
    if len({item["column"] for item in field_templates}) != len(field_templates):
        raise ValueError("Duplicate projected field")
    values, roles = _fields(witnesses[0])
    semantic = {column: value for column, value in values.items() if roles[column] & _SEMANTIC}
    protected = {column for column, rs in roles.items() if rs & {"formula", "unit"}}
    protected.update(item["column"] for item in witnesses[0].get("calculation_fragments", [])
                     if item.get("effective_role") == "calculation_operator")
    for item in field_templates:
        if item["column"] not in values:
            raise ValueError("Projected field is absent from the source record")
        if item["column"] in protected and _SLOT.search(item["template"]):
            raise ValueError("Formula, unit and calculation operators cannot be wildcarded")
        if not _SLOT.search(item["template"]) or re.sub(_SLOT, "", item["template"]).count("{"):
            raise ValueError("A projected field needs declared literal placeholders")
    changed = {item["column"] for item in field_templates}
    template = {"snapshot_id": data.snapshot_id,
                "source_table": witnesses[0]["table"], "root_type": decision.root_type,
                "label": decision.label, "definition": decision.definition,
                "semantic_columns": sorted(semantic), "invariants": {k: v for k, v in semantic.items() if k not in changed},
                "field_templates": field_templates, "slots": slots,
                "evidence_ids": sorted(set(evidence_ids)), "status": "accepted",
                "identity_claim": "projection_not_exact_source_identity"}
    for name, value in decision.definition_parameters.items():
        if not name or value not in template["invariants"].values():
            raise ValueError("Definition parameter must retain a complete invariant source value")
    template["definition_parameters"] = dict(decision.definition_parameters)
    source_properties = {}
    for record in witnesses:
        raw_values, raw_roles = _fields(record)
        for column, role_set in raw_roles.items():
            for role in role_set & {"name", "alias", "description", "formula", "unit", "scope"}:
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
        # A citation proves this captured value, not an implicit name/code
        # translation. Related records cannot stand in for a matching witness.
        citation = source_slot.evidence
        binding = bindings_by_record.get(citation.record_id)
        if binding is None or binding["slot_values"].get(source_slot.name) != citation.quote:
            raise ValueError("Variable slot evidence must equal its captured value in the cited witness")
    # A new generalized matcher must be witnessed by variation. A separately
    # accepted type can instead supply a structural instantiation contract.
    declared_class = False
    if decision.class_definition is not None:
        citation = decision.class_definition
        if (citation.record_id not in records or citation.quote != decision.definition
                or decision.label not in citation.quote):
            raise ValueError("Class definition must quote the source statement including its class label")
        evidence_ids.append(_evidence(data, records[citation.record_id], citation.column, citation.quote))
        template["evidence_ids"] = sorted(set(evidence_ids))
        template["class_definition"] = citation.model_dump()
        declared_class = True
    if any("fixed_value" not in slot for slot in slots) and not decision.existing_type_id and not declared_class:
        if len(witnesses) < 2 or not any(len({item["slot_values"][slot["name"]] for item in bindings}) > 1 for slot in slots):
            raise ValueError("A new template requires observed variation, not one invented generalization")
    if decision.root_type in {"Metric", "GeneralObject"} and not decision.existing_type_id and not declared_class:
        shared_definition = decision.label in decision.definition and all(
            any(decision.definition in str(entry["value"])
                for entry in record.get("fields", {}).get("description", []))
            for record in witnesses)
        varying = any(len({item["slot_values"][slot["name"]] for item in bindings}) > 1 for slot in slots)
        if len(witnesses) < 2 or not varying or not shared_definition:
            raise ValueError(
                "A new Metric or GeneralObject requires a quoted class_definition or multiple "
                "varying witnesses sharing the same source definition including its class label")
    if decision.root_type == "Metric":
        for slot in slots:
            if slot["role"] == "measure" and len({item["slot_values"][slot["name"]] for item in bindings}) > 1:
                raise ValueError("Different quantity meanings cannot be wildcarded into one Metric")
    if decision.existing_type_id:
        existing = known.get(decision.existing_type_id)
        existing_formulas = {str(data.evidence[eid].get("raw_fragment", "")).strip()
                             for prop in (existing.source_properties if existing else []) if prop.role == "formula"
                             for eid in prop.evidence_ids if eid in data.evidence}
        source_formulas = {values[column].strip() for column, rs in roles.items() if "formula" in rs}
        if (existing is None or existing.category != "business_type"
                or existing.parent != decision.root_type or existing.label != decision.label
                or existing.definition != decision.definition
                or (existing.unit or "") != (witnesses[0].get("unit") or "")
                or existing.definition_parameters != decision.definition_parameters
                or existing_formulas != source_formulas):
            raise ValueError("Template target must match an existing type definition, unit, formula and parameters")
        template["object_type_id"] = existing.id
    else:
        if decision.root_type not in roots:
            raise ValueError("Unknown template root")
        # A source matcher is not an ontology identity. Physical columns,
        # reference bindings and instance structure never mint another class.
        formulas = sorted({values[column] for column, rs in roles.items() if "formula" in rs})
        identity = [decision.root_type, decision.label, decision.definition,
                    witnesses[0].get("unit") or None, formulas, decision.definition_parameters]
        template["object_type_id"] = "type:" + digest(identity)[:24]
        if template["object_type_id"] not in known:
            candidate.object_types.append(DerivedType(
                id=template["object_type_id"], parent=decision.root_type, label=decision.label,
                definition=decision.definition, category="business_type", evidence_ids=template["evidence_ids"],
                derivation_kind="template_projection", evidence_scope="projected_definition_template",
                unit=witnesses[0].get("unit") or None,
                definition_parameters=decision.definition_parameters, source_properties=properties))
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
                                        template["semantic_columns"], template["invariants"], field_templates, slots])
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
    return bind_projection(data, sorted(matches, key=lambda item: item["template_id"])[0], seed)
