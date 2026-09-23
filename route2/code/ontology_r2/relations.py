"""Generic record relations: indexed references, safe formula parsing and text candidates."""
import ast
import json
import re
from functools import lru_cache

from .llm import BudgetExceeded
from .models import LinkDecision, evaluate
from .storage import digest


@lru_cache(maxsize=4096)
def formula_symbols(expression):
    tree = ast.parse(expression, mode="eval")
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd, ast.Name, ast.Load, ast.Constant, ast.Call)
    functions = {"sum", "avg", "min", "max", "abs"}
    symbols = []
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError("unsupported_formula_syntax")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id.casefold() not in functions or node.keywords:
                raise ValueError("unsupported_formula_function")
        if isinstance(node, ast.Constant) and (isinstance(node.value, bool) or not isinstance(node.value, (int, float))):
            raise ValueError("unsupported_formula_literal")
    function_nodes = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and id(node) not in function_nodes:
            symbols.append(node.id)
    return tuple(dict.fromkeys(symbols))


@lru_cache(maxsize=4096)
def formula_occurrences(expression, symbol):
    tree = ast.parse(expression, mode="eval")
    function_nodes = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    return [{"line": node.lineno, "end_line": node.end_lineno, "start_utf8_byte": node.col_offset, "end_utf8_byte": node.end_col_offset}
            for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == symbol and id(node) not in function_nodes]


def condition_fields(condition):
    if condition is None:
        return set()
    return ({condition.field} if condition.field else set()) | set().union(*(condition_fields(child) for child in condition.children))


def terms(value):
    text = str(value).casefold()
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text)
    return sorted(set(words))


class Extractor:
    def __init__(self, data, sink, plan, llm, config):
        self.data, self.sink, self.plan, self.llm, self.config = data, sink, plan, llm, config
        self.tables = {t.table: t for t in plan.tables}
        self.stats = {"records_examined": 0, "not_applicable": 0, "empty_reference": 0, "accepted": 0, "unresolved": 0, "text_groups": 0, "unprocessed_records": 0, "semantic_budget_exhausted": False, "plans": []}
        self.text_cache, self.indexed = {}, set()
        self.sink.db.execute("CREATE VIRTUAL TABLE search USING fts5(table_name UNINDEXED, words, body UNINDEXED)")

    def evidence(self, table, row, column):
        rid = self.data.record_id(table, row)
        eid = "record:" + digest([rid, column])[:24]
        self.sink.put("evidence", {"id": eid, "origin": "observed_record", "source_ref": {"table": table, "record_id": rid, "row": row["__r2_row"], "column": column, "snapshot_id": self.data.snapshot_id}, "raw_fragment": row.get(column)})
        return eid

    def object(self, table, row):
        p = self.tables[table]
        oid = self.data.record_id(table, row)
        col = p.label_column or self.data.tables[table]["column_names"][0]
        ev = self.evidence(table, row, col)
        self.sink.put("objects", {"id": oid, "type": p.object_type, "label": row.get(col), "identity_scope": "source_snapshot", "source_ref": {"table": table, "row": row["__r2_row"], "snapshot_id": self.data.snapshot_id}, "evidence_ids": [ev, *p.evidence_ids], "decision": {"status": "accepted", "method": "reviewed_model_type_plan"}})
        for attr, source in p.attributes.items():
            if row.get(source) in (None, ""):
                continue
            aid = digest([oid, "has", attr, row[source]])
            self.sink.put("assertions", {"id": aid, "subject": oid, "predicate": "has", "attribute": attr, "literal": {"type": "string", "value": row[source]}, "condition": {"status": "not_applicable"}, "evidence_ids": [self.evidence(table, row, source)], "decision": {"status": "accepted", "method": "source_copy"}})
        return oid

    def unresolved(self, plan, row, reason, detail=None):
        item = {"id": digest([plan.id, row["__r2_row"], reason, detail]), "plan_id": plan.id, "source_record": self.data.record_id(plan.source_table, row), "reason": reason, "detail": detail}
        self.sink.put("unresolved", item)
        self.stats["unresolved"] += 1

    def search(self, plan, row):
        table = plan.target_table
        if table not in self.indexed:
            for target in self.data.rows(table):
                text = " ".join(terms(" ".join(str(v or "") for v in target.values())))
                self.sink.db.execute("INSERT INTO search VALUES (?,?,?)", (table, text, json.dumps(target, ensure_ascii=False)))
            self.indexed.add(table)
        query_terms = terms(row[plan.source_column])[:20]
        if not query_terms:
            return []
        query = " OR ".join('"' + t.replace('"', '""') + '"' for t in query_terms)
        sql, args = "SELECT body FROM search WHERE words MATCH ? AND table_name=?", [query, table]
        for source, target in plan.scope_bindings.items():
            if row.get(source) in (None, ""):
                return []
            sql += " AND json_extract(body, ?) = ?"
            args.extend(['$."' + target.replace('"', '\\"') + '"', row[source]])
        sql += " ORDER BY rank LIMIT ?"
        args.append(self.config.get("candidate_top_k", 10))
        return [json.loads(r[0]) for r in self.sink.db.execute(sql, args).fetchall()]

    async def text_target(self, plan, row):
        candidates = self.search(plan, row)
        if not candidates:
            return None, "no_text_candidate"
        # Include all source values and scopes: no semantic conclusion leaks across records.
        identity_fields = set(self.data.tables[plan.source_table]["pk"]) | set(self.tables[plan.source_table].identity_columns)
        source = {k: v for k, v in row.items() if k != "__r2_row" and k not in identity_fields}
        cards = [{"record_id": self.data.record_id(plan.target_table, c), "fields": c} for c in candidates]
        key = digest([plan.model_dump(), source, cards])
        if key in self.text_cache:
            decision = self.text_cache[key]
        else:
            if self.stats["text_groups"] >= self.config.get("max_text_groups", 100):
                self.stats["semantic_budget_exhausted"] = True
                return None, "semantic_group_budget"
            self.stats["text_groups"] += 1
            decision = await self.llm.ask("link", {"source": source, "candidates": cards, "plan": plan.model_dump()}, LinkDecision)
            self.text_cache[key] = decision
        if decision.status != "accepted":
            return None, decision.status
        selected = next((c for c in candidates if self.data.record_id(plan.target_table, c) == decision.target_record_id), None)
        if selected is None or not decision.source_quote or not decision.target_quote:
            return None, "invalid_link_evidence"
        if not any(decision.source_quote in str(v) for v in source.values()) or not any(decision.target_quote in str(v) for v in selected.values()):
            return None, "invalid_link_quote"
        return selected, "model_semantic_match"

    async def execute(self):
        for p in self.plan.relations:
            processed = 0
            coverage = {"plan_id": p.id, "total_source_records": self.data.tables[p.source_table]["rows"], "examined": 0, "selector_true": 0, "selector_unknown": 0, "nonempty_applicable_records": 0, "matched_records": 0, "references": 0, "matched_references": 0}
            self.stats["plans"].append(coverage)
            for row in self.data.rows(p.source_table):
                if self.stats["records_examined"] >= self.config.get("max_relation_records", 1000000):
                    remaining = self.data.tables[p.source_table]["rows"] - processed
                    self.stats["unprocessed_records"] += remaining
                    self.sink.put("unresolved", {"id": digest([p.id, "remaining", processed]), "plan_id": p.id, "reason": "record_budget", "from_row": processed + 1, "count": remaining})
                    break
                processed += 1
                coverage["examined"] += 1
                self.stats["records_examined"] += 1
                applies = evaluate(p.selector, row)
                if applies is False:
                    self.stats["not_applicable"] += 1
                    continue
                if applies is None:
                    coverage["selector_unknown"] += 1
                    self.unresolved(p, row, "condition_unknown")
                    continue
                coverage["selector_true"] += 1
                value = row.get(p.source_column)
                if p.source_path and value not in (None, ""):
                    try:
                        value = json.loads(value)
                        for key in p.source_path:
                            value = value.get(key) if isinstance(value, dict) else None
                        if isinstance(value, list) and p.mode == "members":
                            value = json.dumps(value, ensure_ascii=False)
                        if value is not None and not isinstance(value, str):
                            raise ValueError("reference path must resolve to a string or member list")
                    except (ValueError, TypeError) as exc:
                        self.unresolved(p, row, "unsupported_value", str(exc))
                        continue
                if value in (None, ""):
                    self.stats["empty_reference"] += 1
                    continue
                coverage["nonempty_applicable_records"] += 1
                if any(row.get(s) in (None, "") for s in set(p.scope_bindings) | set(p.context_columns)):
                    self.unresolved(p, row, "scope_unknown")
                    continue
                method, targets = p.mode, []
                try:
                    if p.mode == "text":
                        coverage["references"] += 1
                        target, method = await self.text_target(p, row)
                        if target is None:
                            self.unresolved(p, row, method)
                            continue
                        targets = [target]
                    else:
                        if p.mode == "formula":
                            values = formula_symbols(value)
                        elif p.mode == "members":
                            values = json.loads(value) if value.lstrip().startswith("[") else value.split(p.delimiter) if p.delimiter else [value]
                            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                                raise ValueError("members must be strings")
                        else:
                            values = [value]
                        if len(values) > self.config.get("max_references_per_record", 100):
                            raise ValueError("too_many_references")
                        coverage["references"] += len(values)
                        for item in values:
                            binding = tuple(sorted([(p.target_column, item), *((t, row[s]) for s, t in p.scope_bindings.items())]))
                            matches = self.data.lookup(p.target_table, binding)
                            if len(matches) != 1:
                                self.unresolved(p, row, "missing_target" if not matches else "ambiguous_identity", item)
                            else:
                                targets.append(matches[0])
                    coverage["matched_records"] += bool(targets)
                    coverage["matched_references"] += len(targets)
                    rule_evidence = set()
                    for target in targets:
                        subject, obj = self.object(p.source_table, row), self.object(p.target_table, target)
                        condition = {"status": "known", "expression": p.selector.model_dump()} if p.selector else {"status": "not_applicable"}
                        scope = {s: row[s] for s in sorted(set(p.scope_bindings) | set(p.context_columns))}
                        edge = {"subject": subject, "predicate": p.predicate, "object": obj, "scope": scope, "condition": condition}
                        edge["semantics"] = p.semantics
                        source_fields = {p.source_column} | set(p.scope_bindings) | set(p.context_columns) | condition_fields(p.selector)
                        target_fields = {p.target_column} | set(p.scope_bindings.values())
                        evidence = [self.evidence(p.source_table, row, col) for col in sorted(source_fields)] + [self.evidence(p.target_table, target, col) for col in sorted(target_fields)] + p.evidence_ids
                        rule_evidence.update(evidence)
                        edge.update(id=digest(edge), evidence_ids=sorted(set(evidence)), decision={"status": "accepted", "method": method, "identity_scope": "input_snapshot"}, plan_id=p.id)
                        if p.mode == "formula":
                            edge["formula"] = value
                            edge["symbol"] = target[p.target_column]
                            edge["symbol_occurrences"] = formula_occurrences(value, target[p.target_column])
                            edge["dependency_completeness"] = "partial" if len(targets) != len(values) else "complete_within_supported_grammar"
                        self.sink.put("assertions", edge)
                        self.stats["accepted"] += 1
                    if p.semantics == "allowed_member" and (targets or not values):
                        if not targets:
                            subject = self.object(p.source_table, row)
                            scope = {s: row[s] for s in sorted(set(p.scope_bindings) | set(p.context_columns))}
                            condition = {"status": "known", "expression": p.selector.model_dump()} if p.selector else {"status": "not_applicable"}
                            rule_evidence.update(p.evidence_ids)
                            rule_evidence.update(self.evidence(p.source_table, row, col) for col in {p.source_column} | set(scope) | condition_fields(p.selector))
                        self.sink.put("rules", {"id": digest([p.id, self.data.record_id(p.source_table, row)]), "subject": subject, "rule": "allowed_members", "members": sorted({self.data.record_id(p.target_table, target) for target in targets}), "scope": scope, "condition": condition, "raw_rule": row[p.source_column], "source_path": p.source_path, "completeness": "complete_within_source_list" if len(targets) == len(values) else "partial", "evidence_ids": sorted(rule_evidence)})
                except BudgetExceeded:
                    self.stats["semantic_budget_exhausted"] = True
                    self.unresolved(p, row, "llm_budget_exhausted")
                except (ValueError, SyntaxError) as exc:
                    self.unresolved(p, row, "unsupported_value", str(exc))
            denominator = coverage["nonempty_applicable_records"]
            coverage["matched_record_ratio"] = coverage["matched_records"] / denominator if denominator else None
        return self.stats
