"""Materialize source-backed definition instances, never a product of type edges.

Definition/configuration records and bound slot occurrences are not measured
business observations. Existing fact instances remain a separate channel.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .models import BuildPlan, Condition, evaluate
from .storage import digest, qi
from .template_projection import match_projection


def materialize_ontology_instances(data, plan, *, template_projections=(), template_bindings=(),
                                  ontology_bindings=(), calculation_contracts=None,
                                  concepts=(), record_alignments=(), memberships=(),
                                  definition_templates=(),
                                  record_relations=(),
                                  fact_instances=(), max_records=1200000,
                                  max_assertions=1200000):
    """Return Sink-compatible objects/assertions plus mapping coverage and pending.

    Source rows are read by fixed row positions in batches. Templates are
    replayed on those actual rows; saved accepted flags alone are insufficient.
    Each slot edge stays within its witness. Formula/configuration edges use
    independently proved target definition records, never every instance of a type.
    """
    if any(type(value) is not int or value < 0 for value in (max_records, max_assertions)):
        raise ValueError("Instance limits must be nonnegative integers")
    plan = BuildPlan.model_validate(plan)
    types = {item.id: item for item in plan.object_types if item.category == "business_type"}
    relations = {item.id: item for item in plan.relation_types}
    templates = {item["template_id"]: item for item in template_projections}
    definition_templates_by_id = {item["id"]: item for item in definition_templates}
    replayed_definition_templates = {}
    concepts_by_id = {item["id"]: item for item in concepts}
    pending, objects, assertions, mappings = [], {}, {}, []
    by_record_type, template_nodes, rows = {}, defaultdict(list), {}
    evidence_refs = {}
    for proof in data.evidence.values():
        ref = proof.get("source_ref", {})
        if (proof.get("origin") == "observed_record" and not proof.get("raw_fragment_truncated")
                and ref.get("snapshot_id") == data.snapshot_id and ref.get("record_id")
                and ref.get("table") in data.tables and type(ref.get("row")) is int):
            evidence_refs[ref["record_id"]] = ref

    def unresolved(reason, **details):
        pending.append({"status": "unresolved", "reason": reason, **details})

    def source_proofs(ids):
        return [eid for eid in ids if eid in data.evidence and not
                data.evidence[eid].get("raw_fragment_truncated") and
                data.evidence[eid].get("source_ref", {}).get("snapshot_id", data.snapshot_id) == data.snapshot_id]

    def register_record_proof(table, row, record_id, column):
        eid = "record:" + digest([data.snapshot_id, record_id, column])[:24]
        data.evidence[eid] = {"id": eid, "origin": "observed_record", "raw_fragment": str(row[column]),
            "raw_fragment_truncated": False, "source_ref": {"table": table, "row": row["__r2_row"],
                "record_id": record_id, "column": column, "snapshot_id": data.snapshot_id}}
        return eid

    candidates = []
    for binding in template_bindings:
        if binding.get("mapping_kind") == "template_instance" and binding.get("status") == "accepted":
            candidates.append({**binding, "mapping_channel": "template"})
    for alignment in record_alignments:
        concept = concepts_by_id.get(alignment.get("concept_id"), {})
        if alignment.get("mapping_kind") == "exact" and concept.get("ontology_level") == "type":
            candidates.append({**alignment, "record_id": alignment.get("source_record_id"),
                "object_type_id": concept.get("ontology_type_id"), "mapping_channel": "exact_definition"})
    for member in memberships:
        if member.get("status") == "definition_template_match":
            candidates.append({**member, "object_type_id": member.get("type_id"),
                               "mapping_channel": "definition_membership"})
    grouped = defaultdict(list)
    for candidate in candidates:
        rid, type_id = candidate.get("record_id"), candidate.get("object_type_id")
        ref = evidence_refs.get(rid, {})
        table = candidate.get("source_table", candidate.get("table", ref.get("table")))
        number = candidate.get("row_number", ref.get("row"))
        if (type_id not in types or table not in data.tables or type(number) is not int
                or candidate.get("snapshot_id", data.snapshot_id) != data.snapshot_id):
            unresolved("source_mapping_missing_or_stale", record_id=rid, type_id=type_id)
            continue
        grouped[(table, number)].append(candidate)
    selected_keys = sorted(grouped)[:max_records]
    for key in sorted(grouped)[max_records:]:
        unresolved("source_record_limit", source_table=key[0], row_number=key[1])
    by_table = defaultdict(list)
    for table, number in selected_keys:
        by_table[table].append(number)
    for table, numbers in by_table.items():
        for start in range(0, len(numbers), 256):
            batch = numbers[start:start + 256]
            cursor = data.db.execute(f"SELECT * FROM {qi(data.tables[table]['sql_name'])} "
                f"WHERE __r2_row IN ({','.join('?' for _ in batch)})", batch)
            fields = [column[0] for column in cursor.description]
            for values in cursor.fetchall():
                row = dict(zip(fields, values))
                rows[(table, row["__r2_row"])] = row
    for key in selected_keys:
        row = rows.get(key)
        for binding in grouped[key]:
            rid, type_id = binding["record_id"], binding["object_type_id"]
            channel = binding["mapping_channel"]
            if row is None or data.record_id(key[0], row) != rid:
                unresolved("source_record_identity_changed", record_id=rid, type_id=type_id)
                continue
            proofs = source_proofs(binding.get("evidence_ids", []))
            if not proofs:
                unresolved("mapping_has_no_source_evidence", record_id=rid, type_id=type_id)
                continue
            if channel == "definition_membership":
                from .definition_instance_bindings import replay_definition_membership
                try:
                    fields = replay_definition_membership(data, binding,
                        definition_templates_by_id.get(binding.get("template_id")), row,
                        types[type_id], replayed_definition_templates)
                except ValueError as exc:
                    unresolved("definition_membership_replay_failed", record_id=rid,
                               type_id=type_id, detail=str(exc))
                    continue
                # Membership evidence cites the representative. Register the
                # replayed member's own complete values, keeping its identity.
                proofs.extend(register_record_proof(key[0], row, rid, field) for field in fields)
            elif channel != "template":
                source_values = [data.evidence[eid] for prop in types[type_id].source_properties
                                 for eid in prop.evidence_ids if eid in data.evidence
                                 and data.evidence[eid].get("source_ref", {}).get("record_id") == rid]
                source_values.extend(data.evidence[eid] for eid in proofs
                    if data.evidence[eid].get("source_ref", {}).get("record_id") == rid)
                if not any(proof.get("origin") == "observed_record"
                           and proof.get("source_ref", {}).get("table") == key[0]
                           and proof.get("source_ref", {}).get("snapshot_id") == data.snapshot_id
                           and not proof.get("raw_fragment_truncated")
                           and row.get(proof.get("source_ref", {}).get("column")) == proof.get("raw_fragment")
                           for proof in source_values):
                    unresolved("definition_mapping_has_no_current_record_value_proof", record_id=rid, type_id=type_id)
                    continue
            template = None
            if channel == "template":
                template = templates.get(binding.get("template_id"))
                if (template is None or template.get("status") != "accepted"
                        or template.get("snapshot_id") != data.snapshot_id
                        or template.get("object_type_id") != type_id):
                    unresolved("template_contract_missing_or_stale", record_id=rid, type_id=type_id)
                    continue
                semantic_columns = set(template["semantic_columns"])
                selected_columns = semantic_columns | {item["column"] for item in template["field_templates"]}
                if not selected_columns <= row.keys():
                    unresolved("template_source_columns_missing", record_id=rid, type_id=type_id)
                    continue
                record = {"table": key[0], "record_id": rid, "row_number": key[1], "fields": {
                    "description": [{"column": column, "value": str(row[column]), "truncated": False}
                                    for column in sorted(semantic_columns) if row.get(column) not in (None, "")],
                    "reference": [{"column": column, "value": str(row[column]), "truncated": False}
                                  for column in sorted(selected_columns - semantic_columns)
                                  if row.get(column) not in (None, "")]}}
                try:
                    matched = match_projection(template, record)
                except ValueError:
                    matched = None
                if matched is None or matched["slot_values"] != binding.get("slot_values"):
                    unresolved("template_replay_differs_from_saved_binding", record_id=rid, type_id=type_id)
                    continue
            properties = []
            for prop in types[type_id].source_properties:
                if prop.source_table != key[0] or row.get(prop.source_column) in (None, ""):
                    continue
                eid = register_record_proof(key[0], row, rid, prop.source_column)
                properties.append({"role": prop.role, "column": prop.source_column,
                    "value": str(row[prop.source_column]), "evidence_ids": [eid]})
                proofs.append(eid)
            object_id = "ontology_instance:" + digest([data.snapshot_id, type_id, rid])[:24]
            node = {"id": object_id, "type": type_id, "label": types[type_id].label,
                "instance_kind": "definition_record_instance", "mapping_channel": channel,
                "source_ref": {"snapshot_id": data.snapshot_id, "table": key[0], "row": key[1], "record_id": rid},
                "properties": properties, "evidence_ids": sorted(set(proofs)),
                "business_observation_claim": False}
            if template:
                node["slot_values"] = binding["slot_values"]
                identity_slots = [slot["name"] for slot in template["slots"] if slot["role"] == "record_identity"]
                node["label"] = next((binding["slot_values"][name] for name in identity_slots
                                      if binding["slot_values"].get(name)), node["label"])
            if object_id in objects and objects[object_id]["source_ref"] != node["source_ref"]:
                raise ValueError("Conflicting ontology instance identity")
            objects.setdefault(object_id, node)
            by_record_type[(rid, type_id)] = object_id
            mappings.append({"record_id": rid, "type_id": type_id, "instance_id": object_id,
                             "mapping_id": binding.get("id"), "mapping_channel": channel})
            if template:
                template_nodes[template["template_id"]].append((object_id, binding, row))

    def add_edge(subject, object_id, relation_id, evidence_ids, **details):
        relation = relations.get(relation_id)
        if (subject not in objects or object_id not in objects or relation is None
                or objects[subject]["type"] not in relation.domain
                or objects[object_id]["type"] not in relation.range):
            unresolved("instance_edge_endpoint_missing_or_type_mismatch", relation_type_id=relation_id, **details)
            return
        proofs = source_proofs(evidence_ids)
        if not proofs or len(proofs) != len(set(evidence_ids)):
            unresolved("instance_edge_evidence_missing", relation_type_id=relation_id, **details)
            return
        edge_id = "ontology_instance_edge:" + digest([data.snapshot_id, subject, object_id, relation_id, details])[:24]
        if len(assertions) >= max_assertions and edge_id not in assertions:
            unresolved("instance_assertion_limit", relation_type_id=relation_id)
            return
        assertions[edge_id] = {"id": edge_id, "subject": subject, "predicate": relation_id,
            "object": object_id, "evidence_ids": sorted(set(proofs)),
            "decision": {"status": "accepted", "method": "source_bound_instance_contract"},
            "instance_scope": "definition_graph_not_observed_business_values", **details}

    bindings = ontology_bindings.get("bindings", []) if isinstance(ontology_bindings, dict) else ontology_bindings
    bound_slots = {(item.get("contract", {}).get("template_id"), item.get("slot", {}).get("name"))
                   for item in bindings if item.get("status") == "accepted"}
    for template_id in template_nodes:
        for slot in templates[template_id].get("slots", []):
            if slot.get("role") in ("business_object", "measure", "dimension") and (template_id, slot["name"]) not in bound_slots:
                unresolved("template_slot_has_no_accepted_relation", template_id=template_id, slot_name=slot["name"])
    for binding in bindings:
        if binding.get("status") != "accepted" or binding.get("snapshot_id") != data.snapshot_id:
            unresolved("semantic_binding_missing_or_stale", binding_id=binding.get("id"))
            continue
        from .semantic_bindings import binding_relation_errors
        relation = relations.get(binding.get("relation_type_id"))
        proof_id = str(binding.get("id", "")).replace("semantic_binding:", "semantic_binding_proof:", 1)
        body = data.evidence.get(proof_id, {}).get("binding_proof", {})
        expected_body = {"snapshot_id": data.snapshot_id, "source_type_id": binding.get("source_type_id"),
                         "slot": binding.get("slot"), "contract": binding.get("contract")}
        if (relation is None or body != expected_body or
                binding_relation_errors(data, relation, list(types.values()))):
            unresolved("semantic_binding_proof_failed_replay", binding_id=binding.get("id"))
            continue
        contract = binding.get("contract", {})
        if contract.get("kind") == "template_projection":
            template = templates.get(contract.get("template_id"), {})
            slot = binding.get("slot", {})
            for subject, observed, row in template_nodes.get(contract.get("template_id"), []):
                value = observed["slot_values"].get(slot.get("name"))
                target_id = binding.get("target_type_id")
                if (target_id not in types or not isinstance(value, str) or not value.strip()
                        or template.get("contract_hash") != contract.get("contract_hash")):
                    unresolved("slot_instance_contract_incomplete", binding_id=binding["id"], subject=subject)
                    continue
                from .semantic_bindings import measure_slot_value_supported
                declared_slot = next((item for item in template.get("slots", []) if item["name"] == slot["name"]), {})
                if (slot.get("role") == "measure" and "fixed_value" not in declared_slot
                        and not measure_slot_value_supported(data, types[target_id], value)):
                    unresolved("measure_slot_value_has_no_verified_target", binding_id=binding["id"],
                               subject=subject, observed_value=value, target_type_id=target_id)
                    continue
                if slot.get("selector") and evaluate(Condition.model_validate(slot["selector"]), row) is not True:
                    unresolved("slot_selector_not_proven_on_source", binding_id=binding["id"], subject=subject)
                    continue
                target = "ontology_slot_instance:" + digest([subject, target_id, slot["name"], value])[:24]
                proofs = sorted(set(objects[subject]["evidence_ids"] + binding["evidence_ids"]))
                objects[target] = {"id": target, "type": target_id, "label": value,
                    "instance_kind": "definition_slot_occurrence", "source_ref": objects[subject]["source_ref"],
                    "bound_value": value, "slot": slot["name"], "evidence_ids": proofs,
                    "business_observation_claim": False}
                add_edge(subject, target, binding["relation_type_id"], proofs,
                    binding_id=binding["id"], slot_name=slot["name"], selector=slot.get("selector"))
        elif contract.get("kind") == "configuration_references":
            endpoints = contract.get("endpoints", {})
            table, number = contract.get("configuration_table"), contract.get("configuration_row_number")
            valid = table in data.tables and type(number) is int
            if valid:
                cursor = data.db.execute(f"SELECT * FROM {qi(data.tables[table]['sql_name'])} WHERE __r2_row=?", [number])
                fields = [column[0] for column in cursor.description]
                values = cursor.fetchone()
                witness = dict(zip(fields, values)) if values else {}
                valid = bool(witness) and data.record_id(table, witness) == contract.get("configuration_record_id")
                resolved_records = set()
                for path in contract.get("reference_paths", []):
                    if (path.get("source", {}).get("table") != table
                            or any(witness.get(key) != value for key, value in path.get("selector", {}).items())):
                        valid = False
                        break
                    remote = path.get("target", {})
                    lookup = [(remote.get("field"), witness.get(path["source"]["field"]))]
                    lookup.extend((field, witness.get(local)) for local, field in path.get("scope_bindings", {}).items())
                    matches = data.lookup(remote["table"], tuple(lookup))
                    if len(matches) != 1 or data.record_id(remote["table"], matches[0]) not in {
                            endpoint.get("record_id") for endpoint in endpoints.values()}:
                        valid = False
                        break
                    resolved_records.add(data.record_id(remote["table"], matches[0]))
                valid = valid and len(resolved_records) == 2 and resolved_records == {
                    endpoint.get("record_id") for endpoint in endpoints.values()}
            if not valid:
                unresolved("configuration_reference_execution_failed", binding_id=binding["id"])
                continue
            resolved = [by_record_type.get((endpoints.get(side, {}).get("record_id"),
                endpoints.get(side, {}).get("type_id"))) for side in ("source", "target")]
            add_edge(*resolved, binding["relation_type_id"], binding["evidence_ids"],
                binding_id=binding["id"], configuration_record_id=contract.get("configuration_record_id"),
                reference_paths=contract.get("reference_paths"), selector=contract.get("selector"))

    calculation_contracts = calculation_contracts or {}
    calculations = {item["id"]: item for item in calculation_contracts.get("calculations", [])}
    for dependency in calculation_contracts.get("dependencies", []):
        calculation = calculations.get(dependency.get("calculation_id"), {})
        if calculation.get("status") != "accepted":
            continue
        from .calculation_contracts import calculation_relation_errors
        relation = relations.get(dependency.get("relation_type_id"))
        if relation is None or calculation_relation_errors(data, relation, list(types.values())):
            unresolved("calculation_proof_failed_replay", dependency_id=dependency.get("id"))
            continue
        source_records = {ref.get("record_id") for ref in calculation.get("source_refs", [])}
        target_records = {proof.get("source_ref", {}).get("record_id") for proof in dependency.get("binding_evidence", [])}
        subjects = {by_record_type[(rid, dependency["source_type_id"])] for rid in source_records
                    if (rid, dependency["source_type_id"]) in by_record_type}
        for template_id, instances in template_nodes.items():
            projection = templates[template_id]
            if projection.get("object_type_id") != dependency["source_type_id"]:
                continue
            formula_columns = [prop.source_column for prop in types[dependency["source_type_id"]].source_properties
                               if prop.role == "formula" and prop.source_table == projection.get("source_table")]
            if any(projection.get("invariants", {}).get(column) == calculation.get("raw_formula")
                   for column in formula_columns):
                subjects.update(subject for subject, _, _ in instances)
        targets = {by_record_type[(rid, dependency["target_type_id"])] for rid in target_records
                   if (rid, dependency["target_type_id"]) in by_record_type}
        if len(targets) != 1 or not subjects:
            unresolved("formula_definition_instance_missing_or_ambiguous", dependency_id=dependency["id"],
                       source_instance_count=len(subjects), target_instance_count=len(targets))
            continue
        # Every extra source above replayed the same complete formula invariant.
        for subject in sorted(subjects):
            add_edge(subject, next(iter(targets)), dependency.get("relation_type_id"), dependency["evidence_ids"],
                calculation_id=calculation["id"], expression_path=dependency["expression_path"],
                operand_role=dependency["operand_role"], symbol=dependency["symbol"],
                raw_formula=calculation["raw_formula"])
    relation_plans = {item.id: item for item in plan.relations}
    for assertion in record_relations:
        # Only a reviewed source pair may connect the newly materialized
        # records. A shared definition never authorizes table-wide promotion.
        pair = assertion.get("source_record_pair") or {}
        if not assertion.get("source_relation_plan_id") or not pair:
            continue
        subject = by_record_type.get((pair.get("source"), assertion.get("subject_type")))
        target = by_record_type.get((pair.get("target"), assertion.get("object_type")))
        if subject is None or target is None:
            unresolved("record_relation_definition_instance_missing", assertion_id=assertion.get("id"))
            continue
        from .record_relation_instances import verify_record_relation
        try:
            verify_record_relation(data, relation_plans.get(assertion["source_relation_plan_id"]),
                relations.get(assertion.get("predicate")), assertion, objects[subject], objects[target], rows, relations)
        except ValueError as exc:
            unresolved("record_relation_replay_failed", assertion_id=assertion.get("id"), detail=str(exc))
            continue
        add_edge(subject, target, assertion.get("predicate"), assertion.get("evidence_ids", []),
                 source_relation_assertion_id=assertion.get("id"),
                 source_relation_plan_id=assertion["source_relation_plan_id"], source_record_pair=pair)
    materialized = {node["type"] for node in objects.values()} | {node.get("type") for node in fact_instances}
    missing_types = sorted(set(types) - materialized)
    missing_relations = sorted({item.id for item in relations.values() if item.category == "business_relation_type"}
                               - {edge["predicate"] for edge in assertions.values()})
    counts = Counter(node["instance_kind"] for node in objects.values())
    return {"objects": list(objects.values()), "assertions": list(assertions.values()),
            "source_mappings": mappings, "pending": pending,
            "coverage": {"source_mapping_candidates": len(candidates), "source_records_read": len(rows),
                "source_records_omitted": max(0, len(grouped) - len(selected_keys)),
                "definition_record_instances": counts["definition_record_instance"],
                "slot_occurrences": counts["definition_slot_occurrence"],
                "observed_fact_instances_existing_channel": len(fact_instances),
                "instance_assertions": len(assertions), "pending": len(pending),
                "unmaterialized_type_ids": missing_types, "cartesian_products_created": 0,
                "uninstantiated_relation_type_ids": missing_relations,
                "llm_calls": 0, "partial": bool(pending or missing_types or missing_relations),
                "semantic_scope": "source-backed definition/configuration instances; not business measurements"}}
