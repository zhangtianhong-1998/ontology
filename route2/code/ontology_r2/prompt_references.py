"""Request-local, source-addressed aliases for already assembled evidence.

This is an encoding boundary, not a semantic normalizer. Free text, formulae,
units and scope are never rewritten. Source values are eligible only in explicit
reference/record_identity fields; raw source citations are restored before any
of the existing source-checking compilers see a response.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import json
import re

VERSION = 1
PROMPT = """当前请求使用临时短标识（@r记录、@e证据、@t类型、@b包、@v引用值等）。
短标识只在当前请求有效，只能复制输入中已经出现的标识，禁止编造或推算标识；不同短标识不可因名称相同而合并。
reference/record_identity 独立列中的 @v 值保留引用位置及已核验的技术匹配；编码相等不证明业务语义或对象同一。
逐字引文若引用这类独立列，完整复制其 @v 值，并保留 record_id/column；程序会按该来源位置还原。
名称、完整说明、公式、单位、范围、身份限定和否定条件保持原文，其中的数字/编码不是短标识，不可改写。
"""

# Values under these maps are business/source data even when a key is "id".
_LITERAL_MAPS = {"scope", "applicability_scope", "identity_qualifiers", "definition_parameters",
                 "scope_bindings", "selector", "invariants", "slot_values", "values", "attributes",
                 "semantic_parameters", "field_values", "record_identity", "reference"}
_ROOTS = {"GeneralObject", "Measure", "Metric", "Dimension", "Term", "contains", "depends_on",
          "related_to", "points_to", "has"}
_ALIAS = re.compile(r"@[retbcpksvdu]\d+")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_@][A-Za-z0-9_.:/@+\-]*\Z")
_KINDS = {"record": "r", "evidence": "e", "type": "t", "bundle": "b", "plan": "p",
          "card": "c", "rule": "k", "snapshot": "s", "template": "d", "candidate": "u",
          "concept": "t", "field": "e", "pattern": "c", "binding": "p", "document": "e"}
_ID_CONTEXTS = {"object_types": "type", "relation_types": "type", "current_types": "type",
                "current_relations": "type", "object_roots": "type", "relation_roots": "type",
                "component_type_candidates": "type", "candidates": "type", "types": "type",
                "relations": "plan", "plans": "plan", "evidence": "evidence", "claims": "evidence"}
_TYPE_KEYS = {"parent", "object_type", "predicate", "domain", "range", "range_type"}


def _map(value):
    return value if isinstance(value, dict) else {}


def _raw(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _nodes(value, path=()):
    if isinstance(value, dict):
        yield path, value
        for key, item in value.items():
            # Business maps can coincidentally contain record_id/column/value;
            # never let a generic reference-copy scan enter those literals.
            # Selector/invariant/slot maps are handled explicitly by their parent.
            if key in _LITERAL_MAPS and isinstance(item, dict):
                continue
            yield from _nodes(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _nodes(item, (*path, index))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _kind(key, node, path):
    """Recognize metadata positions, never arbitrary strings that look like IDs."""
    if key in _TYPE_KEYS:
        return "type"
    if key == "configuration_witness":
        return "record"
    if key in {"seed_ids", "related_ids"}:
        return "rule" if node.get("task_kind") == "relation_meaning" else "card"
    if key == "id":
        if node.get("origin") or "raw_fragment" in node:
            return "evidence"
        if "parent" in node or "root_type" in node:
            return "type"
        if "source_table" in node and "target_table" in node:
            return "plan"
        container = next((part for part in reversed(path) if isinstance(part, str)), "")
        return _ID_CONTEXTS.get(container)
    suffix = key[:-4] if key.endswith("_ids") else key[:-3] if key.endswith("_id") else None
    if suffix:
        return next((kind for kind in _KINDS if suffix == kind or suffix.endswith("_" + kind)), None)
    return None


def _eligible(value):
    # Source quotes are strings. Numeric business cells and malformed adapters
    # keep their original JSON type instead of creating aliases that decode to int.
    return (isinstance(value, str) and len(value) <= 256 and bool(_IDENTIFIER.fullmatch(value))
            and (any(character.isdigit() for character in value)
                 or bool(re.fullmatch(r"[a-fA-F]{24,}", value))))


def _selector_literals(selector, path=(), *, condition=False):
    """Address equality/membership literals; range bounds retain numeric meaning."""
    if not isinstance(selector, dict):
        return
    operator = selector.get("op")
    # Rule selectors are raw column->value maps; columns named op/field/value
    # are legal. Only the enclosing RelationPlan declares a Condition tree.
    if not condition:
        for column, raw in selector.items():
            yield (*path, column), column, raw
    elif operator == "eq" and "value" in selector and isinstance(selector.get("field"), str):
        yield (*path, "value"), selector["field"], selector.get("value")
    elif operator == "in" and isinstance(selector.get("values"), list) and isinstance(selector.get("field"), str):
        for index, raw in enumerate(selector.get("values", [])):
            yield (*path, "values", index), selector["field"], raw
    elif isinstance(operator, str) and operator in {"and", "or"} and isinstance(selector.get("children"), list):
        for index, child in enumerate(selector.get("children", [])):
            yield from _selector_literals(child, (*path, "children", index), condition=True)


def _put(value, path, replacement):
    target = value
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = replacement


class PromptReferences:
    """Keep the reversible map local; send only aliases and source structure."""

    def __init__(self, task, payload, *, enabled=True):
        self.enabled = enabled and task != "column_role_inference"
        self.task = task
        self.original = payload
        self.counts = Counter()
        self.retained = []
        self.entries = []
        self._reserved_original = {match.group() for text in _strings(payload) for match in _ALIAS.finditer(text)}
        self._reserved = set(self._reserved_original)
        self._counters = Counter()
        self._ids = {}
        self._aliases = {}
        self._cells = {}
        self._by_record = defaultdict(list)
        self._by_column = defaultdict(list)
        self._overrides = {}
        self._evidence_cells = {}
        self._scoped_values = {}
        if not self.enabled:
            self.payload = deepcopy(payload)
            return
        self._collect_cells()
        self._join_checked_pairs()
        for cell in self._cells.values():
            root = self._root(cell)
            if "alias" not in root:
                root["alias"] = self._allocate("v")
            cell["alias"] = root["alias"]
            self._aliases.setdefault(cell["alias"], {"kind": "reference", "cells": []})["cells"].append(cell)
            self.entries.append({"kind": "reference", "alias": cell["alias"],
                                 **{key: cell[key] for key in ("table", "record_id", "column", "original", "scope")}})
            for path in cell["paths"]:
                self._overrides[path] = cell["alias"]
        self._reference_copies()
        self.payload = self._transform(payload)

    def _allocate(self, prefix):
        while True:
            self._counters[prefix] += 1
            alias = f"@{prefix}{self._counters[prefix]}"
            if alias not in self._reserved:
                self._reserved.add(alias)
                return alias

    def _cell(self, table, record_id, column, value, path, *, scope=None):
        identity = (table, record_id, column, _raw(value))
        if identity not in self._cells:
            cell = {"table": table, "record_id": record_id, "column": column,
                    "original": deepcopy(value), "paths": [], "scope": deepcopy(scope or {})}
            # Raw equality is shared only within the same source column and
            # complete visible scope; it never asserts record/entity identity.
            scoped = (table, column, _raw(value), _raw(scope or {}))
            prior = self._scoped_values.get(scoped)
            if prior is not None:
                cell["parent"] = prior
            else:
                self._scoped_values[scoped] = cell
            self._cells[identity] = cell
            self._by_record[record_id].append(cell)
            self._by_column[table, column].append(cell)
        cell = self._cells[identity]
        cell["paths"].append(path)
        return cell

    def _retain(self, path, reason):
        self.counts[reason] += 1
        self.retained.append({"path": list(path), "reason": reason})

    def _collect_cells(self):
        for path, node in _nodes(self.original):
            fields = node.get("fields")
            if not isinstance(fields, dict) or not node.get("table") or not node.get("record_id"):
                continue
            protected = {entry.get("column") for role, entries in fields.items()
                         if role not in {"reference", "record_identity"} and isinstance(entries, list)
                         for entry in entries if isinstance(entry, dict)} | set(node.get("scope") or {})
            for role in ("reference", "record_identity"):
                for index, entry in enumerate(fields.get(role, [])):
                    value, column = entry.get("value"), entry.get("column")
                    where = (*path, "fields", role, index, "value")
                    if column in protected:
                        self._retain(where, "protected_semantic_column")
                    elif not isinstance(value, str):
                        self._retain(where, "non_string_reference_preserved")
                    elif entry.get("truncated") or not column or not _IDENTIFIER.fullmatch(value) or len(value) > 256:
                        self._retain(where, "non_atomic_or_incomplete_reference")
                    elif not _eligible(value):
                        self._retain(where, "readable_reference_literal_preserved")
                    else:
                        self._cell(node["table"], node["record_id"], column, value, where,
                                   scope={"scope": node.get("scope", {}), "identity": fields.get("identity", []),
                                          "scope_fields": fields.get("scope", [])})
            # Embedded IDs remain in their entire source context, including negation.
            raw_refs = [str(cell["original"]) for cell in self._by_record[node["record_id"]]]
            for role, entries in fields.items():
                if role not in {"reference", "record_identity"} and isinstance(entries, list):
                    for index, entry in enumerate(entries):
                        if any(raw in str(entry.get("value", "")) for raw in raw_refs):
                            self._retain((*path, "fields", role, index, "value"), "embedded_reference_preserved")
        # Configuration purpose packets identify reference columns by checked paths.
        for path, node in _nodes(self.original):
            if not node.get("configuration_table") or not isinstance(node.get("reference_paths"), list):
                continue
            table = node["configuration_table"]
            columns = {_map(item.get("source")).get("field") for item in node["reference_paths"]
                       if _map(item.get("source")).get("table") == table}
            for index, entry in enumerate(node.get("evidence", [])):
                if entry.get("kind") == "configuration_value" and entry.get("column") in columns:
                    where = (*path, "evidence", index, "text")
                    if not isinstance(entry.get("text"), str):
                        self._retain(where, "non_string_reference_preserved")
                        continue
                    if not _eligible(entry["text"]):
                        reason = ("readable_reference_literal_preserved"
                                  if len(entry["text"]) <= 256 and _IDENTIFIER.fullmatch(entry["text"])
                                  else "non_atomic_or_incomplete_reference")
                        self._retain(where, reason)
                        continue
                    cell = self._cell(table, node.get("configuration_record_id", node.get("candidate_id")),
                                      entry["column"], entry["text"], where)
                    if entry.get("evidence_id"):
                        self._evidence_cells[entry["evidence_id"]] = cell

    @staticmethod
    def _root(cell):
        while "parent" in cell:
            cell = cell["parent"]
        return cell

    def _find(self, record_id=None, column=None, value=None, table=None):
        cells = self._by_record.get(record_id, []) if record_id else self._by_column.get((table, column), [])
        return [cell for cell in cells if (not column or cell["column"] == column)
                and (not table or cell["table"] == table)
                and (value is None or _raw(cell["original"]) == _raw(value))]

    def _join(self, left, right):
        # Equal bytes are insufficient: callers must have a checked witnessed link.
        if len(left) == len(right) == 1 and _raw(left[0]["original"]) == _raw(right[0]["original"]):
            a, b = self._root(left[0]), self._root(right[0])
            if a is not b:
                b["parent"] = a
                self.counts["checked_reference_unions"] += 1

    def _join_checked_pairs(self):
        for _, node in _nodes(self.original):
            rule = _map(node.get("rule"))
            if rule.get("status") not in {"checked_technical", "observed_subset"}:
                continue
            if _map(rule.get("transform")).get("operator", "identity") != "identity":
                continue
            source, target = _map(rule.get("source")), _map(rule.get("target"))
            for pair in _map(node.get("examples")).get("positive", []):
                self._join(self._find(pair.get("source_record_id"), source.get("field"), table=source.get("table")),
                           self._find(pair.get("target_record_id"), target.get("field"), table=target.get("table")))
        # Joined context is emitted only after checked, scoped, unique matching.
        for _, bundle in _nodes(self.original):
            records = bundle.get("records")
            if not isinstance(records, list):
                continue
            seeds = [record for record in records if record.get("context_role") != "related_context"
                     and (not bundle.get("exact_alignment_record_ids") or
                          record.get("record_id") in bundle["exact_alignment_record_ids"])]
            for record in records:
                connection = _map(record.get("connection"))
                if (record.get("context_role") != "related_context" or not connection.get("rule_id")
                        or _map(connection.get("transform")).get("operator", "identity") != "identity"):
                    continue
                source, target = _map(connection.get("source")), _map(connection.get("target"))
                side, other = (target, source) if connection.get("direction") == "forward" else (source, target)
                candidates = [seed for seed in seeds if seed.get("table") == other.get("table")]
                # Multiple seed witnesses require explicit pairs, not guessed equality.
                if len(candidates) == 1 and record.get("table") == side.get("table"):
                    self._join(self._find(record.get("record_id"), side.get("field"), table=side.get("table")),
                               self._find(candidates[0].get("record_id"), other.get("field"), table=other.get("table")))

    def _context_cells(self, record_ids, column, table=None):
        if not column:
            return []
        if record_ids:
            return [cell for record_id in record_ids
                    for cell in self._find(record_id, column, table=table)]
        return self._find(column=column, table=table)

    def _set_copy(self, path, cells, value):
        matching = [cell for cell in cells if _raw(cell["original"]) == _raw(value)]
        aliases = {cell["alias"] for cell in matching}
        if len(aliases) == 1:
            self._overrides[path] = next(iter(aliases))
            self.counts["reference_copies_aliased"] += 1
        elif matching:
            self._retain(path, "ambiguous_reference_copy_preserved")

    def _reference_copies(self):
        for path, node in _nodes(self.original):
            record_id = node.get("record_id", node.get("source_record_id"))
            column = node.get("column", node.get("source_column"))
            if record_id and column:
                cells = self._find(record_id, column, table=node.get("table"))
                for key in ("quote", "value", "raw_fragment", "text"):
                    if key in node:
                        self._set_copy((*path, key), cells, node[key])
            evidence = self._evidence_cells.get(node.get("evidence_id"))
            if evidence:
                for key in ("quote", "purpose_quote", "text"):
                    if key in node:
                        self._set_copy((*path, key), [evidence], node[key])
            rule = _map(node.get("rule"))
            source, target = _map(rule.get("source")), _map(rule.get("target"))
            for index, pair in enumerate(_map(node.get("examples")).get("positive", [])):
                left = self._find(pair.get("source_record_id"), source.get("field"), table=source.get("table"))
                right = self._find(pair.get("target_record_id"), target.get("field"), table=target.get("table"))
                for key, cells in (("source_raw_value", left), ("target_raw_value", right),
                                   ("matching_raw_value", [*left, *right])):
                    if key in pair:
                        self._set_copy((*path, "examples", "positive", index, key), cells, pair[key])
            if isinstance(node.get("selector"), dict):
                table = _map(node.get("source")).get("table", node.get("source_table"))
                for location, column, value in _selector_literals(
                        node["selector"], condition=isinstance(node.get("source_table"), str)
                        and not isinstance(node.get("source"), dict)):
                    self._set_copy((*path, "selector", *location),
                                   self._find(column=column, table=table), value)
            template = _map(node.get("accepted_template", node))
            template_path = (*path, "accepted_template") if "accepted_template" in node else path
            record_ids = template.get("witness_record_ids") or node.get("witness_record_ids") or ([record_id] if record_id else [])
            for index, field in enumerate(template.get("field_templates", [])):
                if isinstance(field, dict) and "template" in field:
                    self._set_copy((*template_path, "field_templates", index, "template"),
                                   self._context_cells(record_ids, field.get("column"), template.get("source_table")),
                                   field["template"])
            for column, value in (template.get("invariants") or {}).items():
                self._set_copy((*template_path, "invariants", column),
                               self._context_cells(record_ids, column, template.get("source_table")), value)
            slots = template.get("slots", [])
            if not isinstance(slots, list):
                continue
            for index, slot in enumerate(slots):
                if not isinstance(slot, dict) or slot.get("role") not in {"reference", "record_identity"}:
                    continue
                column = slot.get("source_column")
                cells = self._find(record_id, column, table=template.get("source_table")) if column else []
                if "slot_values" in node and slot.get("name") in node["slot_values"]:
                    self._set_copy((*path, "slot_values", slot["name"]), cells, node["slot_values"][slot["name"]])
                if "fixed_value" in slot:
                    slot_path = (*path, "accepted_template") if "accepted_template" in node else path
                    self._set_copy((*slot_path, "slots", index, "fixed_value"), cells, slot["fixed_value"])

    def _id(self, kind, value, *, restore=False):
        if isinstance(value, list):
            return [self._id(kind, item, restore=restore) for item in value]
        if not isinstance(value, str) or value in _ROOTS:
            return deepcopy(value)
        if restore:
            entry = self._aliases.get(value)
            if entry:
                if entry["kind"] != kind:
                    raise ValueError(f"Prompt alias kind mismatch for {kind}: {value}")
                return entry["original"]
            if _ALIAS.fullmatch(value) and value not in {raw for _, raw in self._ids}:
                raise ValueError(f"Unknown prompt alias: {value}")
            return value  # Non-alias unknown IDs remain subject to the strict compiler.
        identity = kind, value
        if identity not in self._ids:
            alias = self._allocate(_KINDS[kind])
            self._ids[identity] = alias
            entry = {"kind": kind, "alias": alias, "original": value}
            self._aliases[alias] = entry
            self.entries.append(entry)
        return self._ids[identity]

    def _transform(self, value, path=(), *, literal=False):
        if path in self._overrides:
            return self._overrides[path]
        if isinstance(value, list):
            return [self._transform(item, (*path, index), literal=literal) for index, item in enumerate(value)]
        if not isinstance(value, dict):
            return deepcopy(value)
        result = {}
        for key, item in value.items():
            kind = None if literal else _kind(key, value, path)
            if kind and isinstance(item, (str, list)):
                result[key] = self._id(kind, item)
            else:
                # Field dictionaries contain schema_evidence_id metadata, not source-key maps.
                result[key] = self._transform(item, (*path, key), literal=literal or key in _LITERAL_MAPS and isinstance(item, dict))
        return result

    def response(self, value, *, encode=False):
        """Decode only typed IDs and addressed source quotes, never free prose."""
        if not self.enabled:
            return deepcopy(value)

        def reference(raw, cells):
            if encode:
                aliases = {cell["alias"] for cell in cells if _raw(cell["original"]) == _raw(raw)}
                return next(iter(aliases)) if len(aliases) == 1 else deepcopy(raw)
            if not isinstance(raw, str) or not re.fullmatch(r"@v\d+", raw):
                return deepcopy(raw)
            if raw in self._reserved_original:
                return raw
            entry = self._aliases.get(raw)
            if not entry or entry["kind"] != "reference":
                raise ValueError(f"Unknown reference alias: {raw}")
            matched = [cell for cell in cells if cell["alias"] == raw]
            if len({_raw(cell["original"]) for cell in matched}) != 1:
                raise ValueError(f"Reference alias lacks its source record/column: {raw}")
            return deepcopy(matched[0]["original"])

        def original_id(raw):
            return raw if encode else self._id("record", raw, restore=True)

        def walk(node, path=(), literal=False, context=None):
            context = context or {}
            if isinstance(node, list):
                return [walk(item, (*path, index), literal, context) for index, item in enumerate(node)]
            if not isinstance(node, dict):
                return deepcopy(node)
            local = dict(context)
            if not literal:
                if "witness_record_ids" in node:
                    local["records"] = [original_id(raw) for raw in node["witness_record_ids"]]
                if node.get("record_id") or node.get("source_record_id"):
                    local["records"] = [original_id(node.get("record_id", node.get("source_record_id")))]
                if node.get("table") or node.get("source_table"):
                    local["table"] = node.get("table", node.get("source_table"))
                template = _map(node.get("accepted_template", node))
                if isinstance(template.get("slots"), list):
                    local["slots"] = template["slots"]
                if template.get("source_table"):
                    local["table"] = template["source_table"]
            result = {}
            for key, item in node.items():
                kind = None if literal else _kind(key, node, path)
                # Hand-authored fixtures may propose new IDs; they are not aliases.
                if kind and isinstance(item, (str, list)):
                    def mapped(raw):
                        if isinstance(raw, list):
                            return [mapped(part) for part in raw]
                        return self._ids.get((kind, raw), raw) if isinstance(raw, str) else raw
                    result[key] = mapped(item) if encode else self._id(kind, item, restore=True)
                else:
                    result[key] = walk(item, (*path, key), literal or key in _LITERAL_MAPS
                                       and isinstance(item, dict), local)
            if literal:
                return result
            source = node if encode else result
            column = source.get("column", source.get("source_column"))
            cells = self._context_cells(local.get("records", []), column, local.get("table"))
            evidence_id = source.get("evidence_id")
            if evidence_id in self._evidence_cells:
                cells.append(self._evidence_cells[evidence_id])
            for key in ("quote", "value", "raw_fragment", "text", "purpose_quote", "template",
                        "fixed_value", "observed_value"):
                if key in node and isinstance(node[key], (str, int)):
                    result[key] = reference(node[key], cells)
            if isinstance(node.get("selector"), dict):
                table = _map(node.get("source")).get("table", node.get("source_table"))
                for location, field, raw in _selector_literals(
                        node["selector"], condition=isinstance(node.get("source_table"), str)
                        and not isinstance(node.get("source"), dict)):
                    cells = self._context_cells(local.get("records", []), field, table)
                    _put(result["selector"], location, reference(raw, cells))
            for column, raw in (node.get("invariants") or {}).items():
                cells = self._context_cells(local.get("records", []), column, local.get("table"))
                result["invariants"][column] = reference(raw, cells)
            for slot in local.get("slots", []):
                if slot.get("role") not in {"reference", "record_identity"}:
                    continue
                name = slot.get("name")
                if name in (node.get("slot_values") or {}):
                    cells = self._context_cells(local.get("records", []), slot.get("source_column"), local.get("table"))
                    result["slot_values"][name] = reference(node["slot_values"][name], cells)
            return result

        result = walk(value)
        if encode:
            return result

        def finish(node, *, explanation=False, literal=False):
            if isinstance(node, list):
                return [finish(item, explanation=explanation, literal=literal) for item in node]
            if isinstance(node, dict):
                return {finish(key, literal=True): finish(item,
                                    explanation=explanation or not literal and key in {"reason", "errors", "explanation"},
                                    literal=literal or key in _LITERAL_MAPS and isinstance(item, dict))
                        for key, item in node.items()}
            if not isinstance(node, str):
                return node
            def replace(match):
                alias = match.group()
                if alias in self._reserved_original:
                    return alias
                entry = self._aliases.get(alias)
                if not entry:
                    raise ValueError(f"Unknown prompt alias: {alias}")
                # Model explanations may name a known system ID. This narrow
                # exception does not turn a business literal/quote into evidence.
                if explanation and not literal and entry["kind"] != "reference":
                    return str(entry["original"])
                raise ValueError(f"Unaddressed {entry['kind']} alias in model output: {alias}")
            return _ALIAS.sub(replace, node)

        # Alignment quotes intentionally need semantic source fields: their
        # compiler forbids reference columns, so record-only @v quotes fail.
        return finish(result)

    def audit(self):
        return {"version": VERSION, "enabled": self.enabled, "mappings": deepcopy(self.entries),
                "coverage": {**dict(self.counts), "source_reference_cells": len(self._cells),
                             "aliased_value_occurrences": len(self._overrides),
                             "system_ids": len(self._ids), "retained": deepcopy(self.retained),
                             "free_text_policy": "preserve_complete_text_including_embedded_identifiers",
                             "unrecognized_structures": "preserved_without_value_guessing"}}
