"""Compile source formulas into explicit, evidence-bound calculation contracts.

This stage reads accepted definition evidence and its local definition index.
It never evaluates expressions, visits business observations, or calls a model.
"""
from __future__ import annotations

import ast
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict

from .models import BuildPlan
from .storage import digest


_OPERATORS = {ast.Add: "add", ast.Sub: "subtract", ast.Mult: "multiply",
              ast.Div: "divide", ast.Pow: "power", ast.Mod: "modulo"}
_ROLES = {ast.Add: ("addend", "addend"), ast.Sub: ("minuend", "subtrahend"),
          ast.Mult: ("factor", "factor"), ast.Div: ("numerator", "denominator")}
_FUNCTIONS = frozenset(("sum", "avg", "mean", "count", "min", "max", "abs"))


def _norm(value):
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def _aliases(value):
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError):
        decoded = None
    if isinstance(decoded, list) and all(isinstance(item, str) for item in decoded):
        return [item.strip() for item in decoded if item.strip()]
    return [part.strip() for part in re.split(r"[;,；，|]", str(value)) if part.strip()]


def parse_calculation(expression):
    """Parse a bounded arithmetic DSL and retain symbol positions and roles."""
    if not isinstance(expression, str) or not expression.strip():
        return {"formula_type": "empty", "status": "unresolved", "reason": "empty_formula"}
    if len(expression) > 32768:
        return {"formula_type": "unparsed", "status": "unresolved", "reason": "formula_size_limit"}
    normalized = unicodedata.normalize("NFKC", expression).strip()
    assignment = None
    if "=" in normalized:
        if normalized.count("=") != 1:
            return {"formula_type": "unsupported", "status": "unresolved", "reason": "unsupported_comparison"}
        assignment, normalized = (part.strip() for part in normalized.split("=", 1))
    normalized = re.sub(r"(?<![\w.])(\d+(?:\.\d+)?)\s*%", r"(\1 / 100)", normalized)
    try:
        tree = ast.parse(normalized, mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        return {"formula_type": "natural_language", "status": "unresolved", "reason": "not_parseable_arithmetic"}
    if sum(1 for _ in ast.walk(tree)) > 1024:
        return {"formula_type": "unsupported", "status": "unresolved", "reason": "expression_node_limit"}
    symbols = []

    def encode(node, path="root", role="operand"):
        position = {"line": node.lineno, "start_utf8_byte": node.col_offset,
                    "end_line": node.end_lineno, "end_utf8_byte": node.end_col_offset}
        if isinstance(node, ast.Name):
            occurrence = {"symbol": node.id, "path": path, "operand_role": role,
                          "position_in_normalized_expression": position}
            symbols.append(occurrence)
            return {"kind": "symbol", **occurrence}
        if (isinstance(node, ast.Constant) and type(node.value) in (int, float)
                and (isinstance(node.value, int) or math.isfinite(node.value))):
            return {"kind": "constant", "value": node.value}
        if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            roles = _ROLES.get(type(node.op), ("operand", "operand"))
            return {"kind": "operation", "operator": _OPERATORS[type(node.op)], "arguments": [
                encode(node.left, path + ".0", roles[0]), encode(node.right, path + ".1", roles[1])]}
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return {"kind": "operation", "operator": "negate" if isinstance(node.op, ast.USub) else "positive",
                    "arguments": [encode(node.operand, path + ".0", role)]}
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id.casefold() in _FUNCTIONS and not node.keywords and node.args):
            return {"kind": "function", "operator": node.func.id.casefold(),
                    "arguments": [encode(arg, path + f".{i}", "argument") for i, arg in enumerate(node.args)]}
        raise ValueError("unsupported_formula_syntax_or_function")

    try:
        encoded = encode(tree.body)
    except (ValueError, RecursionError) as exc:
        return {"formula_type": "unsupported", "status": "unresolved", "reason": str(exc)}
    return {"formula_type": "symbol_reference" if isinstance(tree.body, ast.Name) else "mathematical",
            "status": "parsed", "normalized_expression": normalized, "assignment": assignment,
            "expression_signature": ast.dump(tree, include_attributes=False),
            "expression_tree": encoded, "symbols": symbols}


def _root(item, types):
    parent, seen = item.parent, set()
    while parent in types and parent not in seen:
        seen.add(parent)
        parent = types[parent].parent
    return parent


def _evidence(data, prop, accepted_records):
    for evidence_id in prop.evidence_ids:
        item = data.evidence.get(evidence_id, {})
        source = item.get("source_ref", {})
        if (item.get("origin") == "observed_record" and not item.get("raw_fragment_truncated")
                and source.get("snapshot_id") == data.snapshot_id
                and source.get("record_id") in accepted_records
                and source.get("table") == prop.source_table
                and source.get("column") == prop.source_column
                and isinstance(item.get("raw_fragment"), str) and item["raw_fragment"].strip()):
            yield evidence_id, item


def _declared_single_keys(table):
    keys = set(table.get("pk", [])) if len(table.get("pk", [])) == 1 else set()
    for constraint in table.get("constraints", []):
        match = re.fullmatch(r"UNIQUE\s*\(\s*\"?([\w]+)\"?\s*\)",
                             str(constraint.get("definition", "")), re.I)
        if match:
            keys.add(match[1])
    return keys


def _definition_code_names(data, index, record_id):
    """Only declared self keys are code aliases; foreign references are not."""
    if index is None:
        return []
    rows = index.db.execute(
        "SELECT c.table_name,c.fields_json,s.row_number FROM cards c JOIN card_sources s USING(card_id) "
        "WHERE s.record_id=? AND c.kind='definition'", (record_id,)).fetchall()
    result = []
    for row in rows:
        table_name = row["table_name"]
        table = data.tables.get(table_name, {})
        keys = _declared_single_keys(table)
        for entries in json.loads(row["fields_json"]).values():
            for entry in entries:
                if entry.get("column") in keys and not entry.get("truncated"):
                    matches = data.lookup(table_name, ((entry["column"], str(entry["value"])),))
                    if len(matches) != 1 or data.record_id(table_name, matches[0]) != record_id:
                        continue
                    evidence_id = "record:" + digest([data.snapshot_id, record_id, entry["column"]])[:24]
                    source_ref = {"record_id": record_id, "table": table_name, "column": entry["column"],
                                  "snapshot_id": data.snapshot_id, "row": row["row_number"]}
                    data.evidence[evidence_id] = {"id": evidence_id, "origin": "observed_record",
                        "raw_fragment": str(entry["value"]), "raw_fragment_truncated": False,
                        "source_ref": source_ref}
                    result.append((str(entry["value"]), {"evidence_id": evidence_id,
                        "source_ref": source_ref, "basis": "declared_unique_definition_key"}))
    return result


def enrich_calculation_contracts(data, plan, group_result, index=None):
    """Return calculation trees and dependency bindings without changing the plan."""
    if (index is not None and index.coverage.get("snapshot_id") != data.snapshot_id):
        raise ValueError("Calculation definition index snapshot differs from current input")
    plan = BuildPlan.model_validate(plan)
    types = {item.id: item for item in plan.object_types}
    selected = {key: item for key, item in types.items()
                if item.category == "business_type" and _root(item, types) in ("Metric", "Measure")}
    concepts = {item["id"]: item for item in group_result.get("concepts", [])}
    records_by_type = defaultdict(set)
    for alignment in group_result.get("record_alignments", []):
        concept = concepts.get(alignment.get("concept_id"), {})
        type_id = concept.get("ontology_type_id")
        if alignment.get("mapping_kind") == "exact" and type_id in selected:
            records_by_type[type_id].add(alignment["source_record_id"])
    registry = defaultdict(lambda: defaultdict(list))
    formulas = defaultdict(list)
    for type_id, item in selected.items():
        records = records_by_type[type_id]
        for prop in item.source_properties:
            for evidence_id, evidence in _evidence(data, prop, records):
                raw = evidence["raw_fragment"]
                if prop.role in ("name", "alias"):
                    for name in (_aliases(raw) if prop.role == "alias" else [raw]):
                        registry[_norm(name)][type_id].append({"evidence_id": evidence_id,
                                                             "role": prop.role, "source_ref": evidence["source_ref"]})
                elif prop.role == "formula":
                    formulas[type_id].append((raw, evidence_id, evidence["source_ref"]))
        for record_id in records:
            for name, source in _definition_code_names(data, index, record_id):
                registry[_norm(name)][type_id].append(source)
    calculations, dependencies = [], []
    for type_id in sorted(formulas):
        item = selected[type_id]
        unique = {}
        for raw, evidence_id, source_ref in formulas[type_id]:
            unique.setdefault(raw, {"evidence_ids": [], "source_refs": []})
            unique[raw]["evidence_ids"].append(evidence_id)
            unique[raw]["source_refs"].append(source_ref)
        normalized_variants = {parse_calculation(raw).get("expression_signature", raw) for raw in unique}
        for raw, evidence in unique.items():
            parsed = parse_calculation(raw)
            calculation = {"id": "calculation:" + digest([type_id, raw])[:24],
                           "source_type_id": type_id, "source_label": item.label,
                           "raw_formula": raw, **evidence, **parsed,
                           "applicability_scope": item.applicability_scope,
                           "bindings": [], "identity_scope": "definition"}
            if parsed["status"] == "parsed":
                for occurrence in parsed["symbols"]:
                    candidates = registry.get(_norm(occurrence["symbol"]), {})
                    compatible = {candidate: proof for candidate, proof in candidates.items()
                                  if candidate != type_id and all(item.applicability_scope.get(key) == value
                                      for key, value in selected[candidate].applicability_scope.items())}
                    binding = {**occurrence, "candidate_type_ids": sorted(candidates),
                               "compatible_type_ids": sorted(compatible), "status": "unresolved"}
                    if len(compatible) == 1:
                        target_id = next(iter(compatible))
                        binding.update(status="bound", target_type_id=target_id,
                                       binding_evidence=compatible[target_id])
                    else:
                        binding["reason"] = ("ambiguous_definition" if len(compatible) > 1 else
                                             "self_reference" if type_id in candidates else
                                             "scope_not_proven" if candidates else "symbol_has_no_accepted_definition")
                    calculation["bindings"].append(binding)
                if len(normalized_variants) > 1:
                    calculation.update(status="unresolved", reason="conflicting_source_formula_variants")
                elif all(binding["status"] == "bound" for binding in calculation["bindings"]):
                    calculation["status"] = "accepted"
                else:
                    calculation.update(status="unresolved", reason="unresolved_formula_operands")
                    if parsed["formula_type"] == "symbol_reference" and not registry.get(_norm(raw)):
                        calculation["formula_type"] = "natural_language"
            calculations.append(calculation)
    # Dependency cycles require a domain rule (for example a time offset). The
    # arithmetic DSL does not yet express one, so do not accept circular bindings.
    graph = defaultdict(set)
    for calc in calculations:
        if calc["status"] == "accepted":
            graph[calc["source_type_id"]].update(binding["target_type_id"] for binding in calc["bindings"])
    def cyclic(start):
        stack, visited = list(graph[start]), set()
        while stack:
            current = stack.pop()
            if current == start:
                return True
            if current not in visited:
                visited.add(current)
                stack.extend(graph[current])
        return False
    cycle_types = {node for node in list(graph) if cyclic(node)}
    for calc in calculations:
        if calc["source_type_id"] in cycle_types and calc["status"] == "accepted":
            calc.update(status="unresolved", reason="cyclic_calculation_dependency")
        if calc["status"] != "accepted":
            continue
        for binding in calc["bindings"]:
            dependencies.append({"id": "calculation_dependency:" + digest([
                calc["id"], binding["path"], binding["target_type_id"]])[:24],
                "calculation_id": calc["id"], "source_type_id": calc["source_type_id"],
                "target_type_id": binding["target_type_id"], "parent_relation": "depends_on",
                "predicate_name": "calculation_dependency", "operand_role": binding["operand_role"],
                "expression_path": binding["path"], "symbol": binding["symbol"],
                "evidence_ids": sorted(set(calc["evidence_ids"] + [p["evidence_id"]
                    for p in binding["binding_evidence"] if "evidence_id" in p])),
                "binding_evidence": binding["binding_evidence"], "status": "accepted"})
    statuses = Counter(item["status"] for item in calculations)
    return {"calculations": calculations, "dependencies": dependencies,
            "coverage": {"accepted_quantitative_types": len(selected), "types_with_formula": len(formulas),
                "types_without_formula": len(selected) - len(formulas), "calculations": len(calculations),
                "accepted": statuses["accepted"], "unresolved": statuses["unresolved"],
                "dependencies": len(dependencies), "partial": bool(statuses["unresolved"]),
                "input_scope": "accepted_definition_evidence_and_definition_index",
                "business_observation_rows_read": 0, "llm_calls": 0,
                "numeric_evaluation": "not_performed"}}


def calculation_relation_errors(data, relation, object_types):
    """Recheck calculation proof when loading a plan; scope labels are not proof."""
    types = {item.id: item for item in object_types}
    if len(relation.domain) != 1 or len(relation.range) != 1:
        return ["calculation relation requires one source and target"]
    source, target = types.get(relation.domain[0]), types.get(relation.range[0])
    if (source is None or target is None or source.id == target.id
            or _root(source, types) not in ("Metric", "Measure")
            or _root(target, types) not in ("Metric", "Measure")):
        return ["calculation relation lacks distinct quantitative definition types"]
    if any(source.applicability_scope.get(key) != value
           for key, value in target.applicability_scope.items()):
        return ["calculation relation target applicability is not proved"]

    def property_evidence(item, roles):
        record_ids = {data.evidence.get(evidence_id, {}).get("source_ref", {}).get("record_id")
                      for prop in item.source_properties for evidence_id in prop.evidence_ids}
        return [(prop.role, evidence_id, proof) for prop in item.source_properties if prop.role in roles
                for evidence_id, proof in _evidence(data, prop, record_ids)]

    names = defaultdict(set)
    for role, evidence_id, proof in property_evidence(target, ("name", "alias")):
        if evidence_id not in relation.evidence_ids:
            continue
        for name in (_aliases(proof["raw_fragment"]) if role == "alias" else [proof["raw_fragment"]]):
            names[_norm(name)].add(evidence_id)
    target_records = {proof["source_ref"]["record_id"] for _, _, proof in
                      property_evidence(target, ("name", "alias", "description", "formula"))}
    for evidence_id in relation.evidence_ids:
        proof = data.evidence.get(evidence_id, {})
        ref = proof.get("source_ref", {})
        table = data.tables.get(ref.get("table"), {})
        if (proof.get("origin") == "observed_record" and not proof.get("raw_fragment_truncated")
                and ref.get("snapshot_id") == data.snapshot_id and ref.get("record_id") in target_records
                and ref.get("column") in _declared_single_keys(table)):
            matches = data.lookup(ref["table"], ((ref["column"], proof.get("raw_fragment")),))
            if len(matches) == 1 and data.record_id(ref["table"], matches[0]) == ref["record_id"]:
                names[_norm(proof["raw_fragment"])].add(evidence_id)
    formulas = [(evidence_id, parse_calculation(proof["raw_fragment"]))
                for _, evidence_id, proof in property_evidence(source, ("formula",))
                if evidence_id in relation.evidence_ids]
    if not formulas or not names:
        return ["calculation relation requires independent complete source formula and target name evidence"]
    if any(parsed["status"] != "parsed" for _, parsed in formulas):
        return ["calculation relation formula is not a supported arithmetic expression"]
    if len({parsed["expression_signature"] for _, parsed in formulas}) != 1:
        return ["calculation relation has conflicting formula evidence"]
    role = relation.semantic_parameters.get("operand_role")
    matched = [symbol for _, parsed in formulas for symbol in parsed["symbols"]
               if _norm(symbol["symbol"]) in names
               and (role is None or role == symbol["operand_role"])]
    if not matched:
        return ["calculation relation target or operand role is absent from parsed source formula"]
    # Even complete name evidence is ambiguous when another compatible accepted
    # type has the same original name. A caller cannot choose one by ID alone.
    for other in object_types:
        if other.id in (source.id, target.id) or other.category != "business_type":
            continue
        if any(source.applicability_scope.get(key) != value
               for key, value in other.applicability_scope.items()):
            continue
        for other_role, _, proof in property_evidence(other, ("name", "alias")):
            values = _aliases(proof["raw_fragment"]) if other_role == "alias" else [proof["raw_fragment"]]
            if any(_norm(value) == _norm(symbol["symbol"]) for value in values for symbol in matched):
                return ["calculation relation symbol has ambiguous accepted definitions"]
    return []
