"""Project auxiliary witnesses to two declared definition references.

This is candidate discovery, not transitivity. The original record predicate
is never inherited: only the auxiliary row's own complete configuration
statement can authorize a business relation in the existing compiler.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from itertools import combinations

from .configuration_relations import (
    ConfigurationRelationSpec, _check_column, _record_evidence, _relation_text_candidate,
    _type_alignments, declared_reference_evidence, verify_configuration_pair,
)
from .models import BuildPlan
from .storage import digest, qi


def _definition_tables(plan):
    types = {item.id: item for item in plan.object_types}
    result = defaultdict(set)
    for item in types.values():
        if item.category != "business_type":
            continue
        root, seen = item.parent, set()
        while root in types and root not in seen:
            seen.add(root); root = types[root].parent
        for prop in item.source_properties:
            if prop.role in ("name", "description", "formula"):
                result[prop.source_table].add(root)
    return dict(result)


def _checked_rule(data, rule, definition_tables=None):
    if (rule.get("status") != "checked_technical" or rule.get("snapshot_id") != data.snapshot_id
            or rule.get("transform", {}).get("operator") != "identity"
            or rule.get("source", {}).get("source_path") or rule.get("source_path")
            or rule.get("verification", {}).get("scan_scope") != "full_input"):
        raise ValueError("auxiliary_path_requires_current_checked_scalar_identity_reference")
    counts = rule["verification"].get("checks", {})
    if (not counts.get("eligible_references") or counts.get("unique_matches") != counts["eligible_references"]
            or any(counts.get(key, 0) for key in ("ambiguous_matches", "missing_in_input", "missing_scope"))):
        raise ValueError("auxiliary_path_reference_is_missing_or_ambiguous")
    source, target = rule["source"], rule["target"]
    _check_column(data, source["table"], source["field"])
    _check_column(data, target["table"], target["field"])
    for field in rule.get("selector", {}):
        _check_column(data, source["table"], field)
    for local, remote in rule.get("scope_bindings", {}).items():
        _check_column(data, source["table"], local)
        _check_column(data, target["table"], remote)
    declaration = declared_reference_evidence(data, rule, definition_tables=definition_tables)
    if declaration is None:
        raise ValueError("auxiliary_path_has_no_original_exact_reference_declaration")
    return declaration


def _row(data, table, number, record_id):
    if table not in data.tables or type(number) is not int:
        raise ValueError("auxiliary_witness_has_no_current_row_location")
    cursor = data.db.execute(f"SELECT * FROM {qi(data.tables[table]['sql_name'])} WHERE __r2_row=?", [number])
    names = [item[0] for item in cursor.description]
    values = cursor.fetchone()
    row = dict(zip(names, values)) if values else {}
    if not row or data.record_id(table, row) != record_id:
        raise ValueError("auxiliary_witness_identity_changed")
    return row


def _resolve(data, rule, witness):
    if any(witness.get(key) != value for key, value in rule.get("selector", {}).items()):
        raise ValueError("auxiliary_path_selector_does_not_apply")
    source, target = rule["source"], rule["target"]
    code = witness.get(source["field"])
    scope = {remote: witness.get(local) for local, remote in rule.get("scope_bindings", {}).items()}
    if code in (None, "") or any(value in (None, "") for value in scope.values()):
        raise ValueError("auxiliary_path_has_blank_reference_or_scope")
    rows = data.lookup(target["table"], ((target["field"], code), *sorted(scope.items())))
    if len(rows) != 1:
        raise ValueError("auxiliary_path_current_lookup_is_missing_or_ambiguous")
    return data.record_id(target["table"], rows[0]), scope


def replay_auxiliary_path(data, candidate, *, plan):
    """Recheck path provenance and actual witness lookups; return proof IDs."""
    proof = candidate.get("auxiliary_path_proof")
    if proof is None:
        return []
    saved = {key: value for key, value in proof.items() if key != "id"}
    if (proof.get("id") != "auxiliary_path:" + digest(saved)[:24]
            or proof.get("snapshot_id") != data.snapshot_id
            or proof.get("configuration_record_id") != candidate.get("configuration_record_id")):
        raise ValueError("auxiliary_path_proof_hash_or_witness_changed")
    plan = BuildPlan.model_validate(plan)
    accepted = {item.id: item for item in plan.relations}
    witness = _row(data, proof["configuration_table"], proof["row_number"], proof["configuration_record_id"])
    if proof["configuration_table"] != candidate.get("configuration_table"):
        raise ValueError("auxiliary_path_configuration_table_changed")
    for anchor in proof["record_witnesses"]:
        relation = accepted.get(anchor["relation_plan_id"])
        if (relation is None or relation.witness_snapshot_id != data.snapshot_id
                or anchor["pair"] not in [value.model_dump() for value in relation.witnessed_pairs]
                or proof["configuration_record_id"] not in anchor["pair"].values()):
            raise ValueError("auxiliary_path_original_record_witness_changed")
    if not proof["record_witnesses"] or len(proof["paths"]) != 2:
        raise ValueError("auxiliary_path_requires_witness_and_two_references")
    for side, entry in zip(("source", "target"), proof["paths"]):
        rule = entry["rule"]
        if _checked_rule(data, rule, _definition_tables(plan)) != entry["declaration"]:
            raise ValueError("auxiliary_path_reference_declaration_changed")
        endpoint = candidate[side + "_definition"]
        if (rule["source"] != {"table": candidate["configuration_table"], "field": candidate[side + "_code_column"]}
                or rule["target"] != {"table": endpoint["table"], "field": endpoint["code_column"]}
                or rule["rule_id"] != candidate.get(side + "_rule_id")):
            raise ValueError("auxiliary_path_direction_or_reference_column_changed")
        rid, scope = _resolve(data, rule, witness)
        if (rid != endpoint["record_id"] or scope != endpoint.get("lookup_scope", {})
                or witness[rule["source"]["field"]] != candidate[side + "_code"]):
            raise ValueError("auxiliary_path_endpoint_or_scope_changed")
    verify_configuration_pair(data, plan, candidate, witness)
    for eid in proof["evidence_ids"]:
        evidence = data.evidence.get(eid, {})
        ref = evidence.get("source_ref", {})
        if evidence.get("origin") == "observed_record" and (
                evidence.get("raw_fragment_truncated") or ref.get("snapshot_id") != data.snapshot_id
                or ref.get("record_id") != proof["configuration_record_id"]
                or str(witness.get(ref.get("column"), "")) != evidence.get("raw_fragment")):
            raise ValueError("auxiliary_path_complete_witness_evidence_changed")
        if not evidence:
            raise ValueError("auxiliary_path_source_evidence_missing")
    return proof["evidence_ids"]


def build_auxiliary_path_projections(data, plan, checked_rules, concepts, alignments, *,
                                     memberships=(), max_witnesses=200, max_paths_per_witness=16):
    """Return existing configuration candidates, pending and replayable proofs.

    Only original accepted record witnesses seed this bounded discovery. Each
    candidate uses two outward source-declared references from one unmapped
    auxiliary row. No reverse join, intermediate identity or transitive
    predicate is invented. Multiple rule routes resolving the same record
    pair collapse only as discovery alternatives, not as new assertions.
    """
    if any(type(value) is not int or value < 0 for value in (max_witnesses, max_paths_per_witness)):
        raise ValueError("Auxiliary path limits must be nonnegative integers")
    plan = BuildPlan.model_validate(plan)
    aligned, conflicting = _type_alignments(plan, concepts, alignments, memberships)
    anchors = defaultdict(list)
    for relation in plan.relations:
        if relation.witness_snapshot_id != data.snapshot_id:
            continue
        for pair in relation.witnessed_pairs:
            for rid in (pair.source_record_id, pair.target_record_id):
                if rid not in aligned:
                    anchors[rid].append({"relation_plan_id": relation.id, "pair": pair.model_dump()})
    refs = {}
    for evidence in data.evidence.values():
        ref = evidence.get("source_ref", {})
        if (evidence.get("origin") == "observed_record" and not evidence.get("raw_fragment_truncated")
                and ref.get("snapshot_id") == data.snapshot_id and ref.get("record_id") in anchors
                and type(ref.get("row")) is int):
            refs[ref["record_id"]] = ref
    rules_by_table = defaultdict(list)
    rejected_rules = Counter()
    definition_tables = _definition_tables(plan)
    for rule in checked_rules:
        try:
            declaration = _checked_rule(data, rule, definition_tables)
        except ValueError as exc:
            rejected_rules[str(exc)] += 1
            continue
        rules_by_table[rule["source"]["table"]].append((rule, declaration))
    candidates, proofs, pending = [], [], []
    resolved_paths, unbound_paths = 0, 0
    for position, rid in enumerate(sorted(anchors)):
        if position >= max_witnesses:
            pending.append({"record_id": rid, "reason": "auxiliary_witness_limit"})
            continue
        ref = refs.get(rid, {})
        try:
            witness = _row(data, ref.get("table"), ref.get("row"), rid)
        except ValueError as exc:
            pending.append({"record_id": rid, "reason": str(exc)})
            continue
        table = ref["table"]
        paths = []
        for rule, declaration in rules_by_table[table]:
            try:
                target_rid, scope = _resolve(data, rule, witness)
                if target_rid not in aligned or target_rid in conflicting:
                    unbound_paths += 1
                    pending.append({"record_id": rid, "rule_id": rule["rule_id"],
                        "reason": "auxiliary_path_target_without_accepted_type",
                        "target_record_id": target_rid, "lookup_scope": scope,
                        "conflicting_type_bindings": target_rid in conflicting})
                    continue
                resolved_paths += 1
                paths.append((rule, declaration, target_rid, scope))
            except ValueError as exc:
                pending.append({"record_id": rid, "rule_id": rule["rule_id"], "reason": str(exc)})
        seen, considered, emitted = set(), 0, 0
        for left, right in combinations(paths, 2):
            if left[0]["source"]["field"] == right[0]["source"]["field"] or left[2] == right[2]:
                continue
            key = tuple(sorted((left[2], right[2])))
            if key in seen:
                continue
            seen.add(key)
            considered += 1
            if considered > max_paths_per_witness:
                pending.append({"record_id": rid, "reason": "auxiliary_path_limit"})
                continue
            text = _relation_text_candidate(data, table, {left[0]["source"]["field"], right[0]["source"]["field"]})
            # No automatic path composition without this row's own statement.
            if text is None:
                pending.append({"record_id": rid, "reason": "auxiliary_pair_has_no_relationship_statement"})
                continue
            columns = {text}
            for rule, _, _, _ in (left, right):
                columns.update([rule["source"]["field"], *rule.get("selector", {}), *rule.get("scope_bindings", {})])
            spec = ConfigurationRelationSpec(spec_id="auxiliary_spec:" + digest([table, key])[:24],
                configuration_table=table, source_code_column=left[0]["source"]["field"],
                target_code_column=right[0]["source"]["field"], source_definition_table=left[0]["target"]["table"],
                source_definition_code_column=left[0]["target"]["field"], target_definition_table=right[0]["target"]["table"],
                target_definition_code_column=right[0]["target"]["field"], relation_text_column=text)
            _, evidence_ids = _record_evidence(data, spec, witness, sorted(columns))
            candidate = {"id": "configuration_relation_candidate:" + digest([data.snapshot_id, rid, key, text])[:24],
                "spec_id": spec.spec_id, "status": "endpoint_verified_candidate", "snapshot_id": data.snapshot_id,
                "predicate_status": "unjudged", "direction_status": "unjudged", "configuration_table": table,
                "configuration_record_id": rid, "configuration_witness_row_number": ref["row"],
                "configuration_rows_with_same_code_pair": 1, "relation_text_column": text,
                "predicate_literal": witness[text], "selector": {}, "evidence_ids": evidence_ids,
                "semantic_status": "auxiliary_paths_are_not_a_business_predicate"}
            for side, (rule, declaration, target_rid, scope) in zip(("source", "target"), (left, right)):
                candidate.update({side + "_code_column": rule["source"]["field"],
                    side + "_code": witness[rule["source"]["field"]], side + "_rule_id": rule["rule_id"],
                    side + "_scope_bindings": rule.get("scope_bindings", {}), side + "_type_id": aligned[target_rid]["type_id"],
                    side + "_definition": {"table": rule["target"]["table"], "code_column": rule["target"]["field"],
                        "record_id": target_rid, "lookup_scope": scope, "status": "accepted_type", **aligned[target_rid]}})
                candidate["selector"].update(rule.get("selector", {}))
                candidate["evidence_ids"] = sorted(set(candidate["evidence_ids"] + aligned[target_rid]["evidence_ids"]))
            proof = {"snapshot_id": data.snapshot_id, "configuration_table": table, "configuration_record_id": rid,
                "row_number": ref["row"], "record_witnesses": anchors[rid], "evidence_ids": evidence_ids,
                "paths": [{"rule": deepcopy(item[0]), "declaration": deepcopy(item[1])} for item in (left, right)]}
            proof["id"] = "auxiliary_path:" + digest(proof)[:24]
            candidate["auxiliary_path_proof"] = proof
            try:
                replay_auxiliary_path(data, candidate, plan=plan)
            except ValueError as exc:
                pending.append({"record_id": rid, "reason": str(exc)})
                continue
            candidates.append(candidate)
            proofs.append(proof)
            emitted += 1
        if not emitted:
            pending.append({"record_id": rid, "reason": "auxiliary_witness_has_no_supported_business_endpoint_pair",
                "declared_reference_rules": len(rules_by_table[table]), "bound_endpoint_paths": len(paths),
                "resolved_endpoints": [{"record_id": path[2], "type_id": aligned[path[2]]["type_id"],
                    "rule_id": path[0]["rule_id"], "reference_column": path[0]["source"]["field"]}
                    for path in paths[:max_paths_per_witness]],
                "resolved_endpoints_omitted_limit": max(0, len(paths) - max_paths_per_witness)})
    return {"candidates": candidates, "proofs": proofs, "pending": pending,
        "coverage": {"auxiliary_witnesses": len(anchors), "witnesses_examined": min(len(anchors), max_witnesses),
            "declared_reference_rules": sum(map(len, rules_by_table.values())),
            "rejected_rule_reasons": dict(rejected_rules), "resolved_endpoint_paths": resolved_paths,
            "paths_without_accepted_endpoint_binding": unbound_paths,
            "candidates": len(candidates), "pending": len(pending), "llm_calls": 0,
            "partial": bool(pending), "scope": "declared_dual_reference_witnesses_not_relation_transitivity"}}
