#!/usr/bin/env python3
"""Read-only checks of 12 source-defined cases; never an accuracy/pass-rate estimate.

Usage: .venv/bin/python scripts/audit_semantic_samples.py RUN [--output FILE]
Only the optional report is written. SQLite is opened with mode=ro. No pipeline
module or model client is imported. Missing/partial evidence stays explicit.
C1-C3 apply to semantic_contract_control; F1-F9 apply to fruit_data_v2.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import yaml


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), default=str).encode()).hexdigest()


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Audit:
    def __init__(self, run):
        self.run = run.resolve()
        self.warnings, self.sources, self.used_files = [], {}, {}
        self.manifest = self.read("manifest.yaml", {})
        self.root = Path(self.manifest.get("input_root", "/nonexistent"))
        self.snapshot = self.manifest.get("snapshot_id")
        self.ontology = self.read("ontology.yaml", {})
        self.types = {x["id"]: x for x in self.ontology.get("object_types", [])}
        self.concepts = self.read("business_concepts.yaml", [])
        self.by_concept = {x["id"]: x for x in self.concepts}
        self.alignments = self.read("record_alignments.yaml", [])
        self.templates = {x["id"]: x for x in self.read("definition_templates.yaml", [])}
        self.calculations = self.read("calculation_contracts.yaml", {}).get("calculations", [])
        self.dependencies = self.read("calculation_contracts.yaml", {}).get("dependencies", [])
        self.rules = self.read("association_rules.yaml", {}).get("rules", [])
        self.plan = self.read("extraction_plan.yaml", {})
        self.bindings = self.read("business_fact_field_bindings.yaml", [])
        self.rejections = self.read("definition_rejections.yaml", [])
        self.steps = {x["bundle_id"]: x for x in self.read("group_steps.yaml", [])}
        self.db = None
        path = self.run / "work/results.sqlite"
        if path.exists():
            self.db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        else:
            self.warnings.append("results.sqlite unavailable: member/evidence/fact lookup may be incomplete")
        self.members = defaultdict(list)
        self.mapping_proofs = {}
        self.bundle_steps = defaultdict(list)
        self.outcomes = []

    def read(self, name, default):
        path = self.run / name
        if not path.exists():
            self.warnings.append("artifact_missing:" + name)
            return default
        # Huge instance files use the result store. Other oversized artifacts
        # fail explicitly rather than exhausting the auditing process's memory.
        if path.stat().st_size > 64 * 1024 * 1024:
            self.warnings.append("artifact_exceeds_64MiB_read_limit:" + name)
            return default
        self.used_files[name] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
        with path.open() as stream:
            return yaml.load(stream, Loader=yaml.CSafeLoader) or default

    def source(self, table, row_number, expected):
        schema_path = self.root / "schema/tables" / (table.split(".")[-1] + ".yaml")
        result = {"table": table, "row_number": row_number, "csv_line": row_number + 1,
                  "expected_values": expected, "applicable": schema_path.exists()}
        if not result["applicable"]:
            return result
        metadata = yaml.load(schema_path.read_text(), Loader=yaml.CSafeLoader)
        constraint_path = self.root / "schema/constraints" / schema_path.name
        result["snapshot_schema_verified"] = all(
            self.manifest.get("input_files", {}).get(str(p.relative_to(self.root))) == sha256(p)
            for p in (schema_path, constraint_path))
        path = self.root / "data" / (metadata["table_name"] + ".csv")
        scoped = self.root / "data" / metadata["schema"] / (metadata["table_name"] + ".csv")
        if scoped.exists():
            path = scoped
        result["file"] = str(path)
        rel = str(path.relative_to(self.root))
        file_hash = sha256(path)
        expected_hash = self.manifest.get("input_files", {}).get(rel)
        result["snapshot_file_verified"] = bool(expected_hash and file_hash == expected_hash)
        row = None
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            for position, item in enumerate(reader, 1):
                if position == row_number:
                    row = item
                    result["csv_line"] = reader.line_num
                    break
        result["sample_values_match"] = row is not None and all(row.get(k) == v for k, v in expected.items())
        if row is None:
            return result
        constraints = yaml.load(constraint_path.read_text(), Loader=yaml.CSafeLoader)
        pk = []
        for entry in constraints.get("constraints", []):
            match = re.search(r"PRIMARY KEY\s*\(([^)]+)\)", entry.get("definition", ""), re.I)
            if match:
                pk = [field.strip().strip('"') for field in match[1].split(",")]
        key = {k: row[k] for k in pk} if pk and all(row.get(k) not in (None, "") for k in pk) else {
            "file": file_hash, "row": row_number}
        result["record_id"] = table + ":" + digest([key, row_number, self.snapshot])[:24]
        result["values"] = {k: row.get(k) for k in expected}
        self.sources[result["record_id"]] = result
        return result

    def prepare(self):
        ids = list(self.sources)
        if self.db and ids:
            sql = "SELECT body FROM items WHERE kind='definition_memberships' AND json_extract(body,'$.record_id') IN (" + ",".join("?" for _ in ids) + ")"
            for body, in self.db.execute(sql, ids):
                item = json.loads(body)
                self.members[item["record_id"]].append(item)
        # C-backed YAML parsing is important for full-run packets. Oversized
        # files receive an explicit warning; absent decisions then stay unresolved.
        for bundle in self.read("evidence_bundles.yaml", []):
            step = self.steps.get(bundle.get("bundle_id"))
            if step:
                # Context-only neighbors do not justify attributing a concept
                # rejection to a source that was never exact-eligible.
                rids = bundle.get("exact_alignment_record_ids", [])
                if bundle.get("task_kind") == "relation_meaning":
                    rids = [r.get("record_id") for r in bundle.get("records", [])]
                for rid in set(rids) & self.sources.keys():
                    self.bundle_steps[rid].append({k: step[k] for k in
                        ("bundle_id", "task_kind", "status", "reason", "error_type") if k in step})

    def accepted(self, source):
        """Attribute a type only through an accepted exact or checked template map.

        Concept.source_refs and evidence_ids include related/dependency context.
        Neither is a source identity map. A template membership establishes only
        the shared definition type, never entity equality or inherited relations.
        """
        rid = source.get("record_id")
        proofs = []

        def accepted_concept(concept_id):
            concept = self.by_concept.get(concept_id, {})
            return concept if (concept.get("decision") == "accepted_by_automatic_checks"
                               and concept.get("ontology_level") == "type") else {}

        for alignment in self.alignments:
            if (alignment.get("source_record_id") != rid or alignment.get("mapping_kind") != "exact"
                    or alignment.get("decision") != "accepted_by_automatic_checks"):
                continue
            concept = accepted_concept(alignment.get("concept_id"))
            if concept.get("ontology_type_id"):
                proofs.append({"type_id": concept["ontology_type_id"], "kind": "exact_alignment",
                               "alignment_id": alignment.get("id"), "concept_id": concept["id"],
                               "evidence_ids": alignment.get("evidence_ids", [])})
        for member in self.members.get(rid, []):
            concept = accepted_concept(member.get("concept_id"))
            template = self.templates.get(member.get("template_id"), {})
            verification = member.get("verification") or {}
            valid = (
                member.get("kind") == "definition_type_membership"
                and member.get("status") == "definition_template_match"
                and member.get("mapping_kind") == "shares_definition_type_template"
                and member.get("snapshot_id") == self.snapshot
                and member.get("table") == source.get("table")
                and member.get("row_number") == source.get("row_number")
                and verification.get("method") == "complete_original_source_field_equality"
                and verification.get("semantic_fingerprint")
                and verification["semantic_fingerprint"] == template.get("semantic_fingerprint")
                and template.get("table") == source.get("table")
                and template.get("concept_id") == member.get("concept_id")
                and concept.get("ontology_type_id") == member.get("type_id") == template.get("type_id")
                and member.get("type_id") is not None)
            if not valid:
                self.warnings.append("unchecked_definition_membership_ignored:" + str(member.get("id")))
                continue
            proofs.append({"type_id": member["type_id"], "kind": "checked_definition_template",
                           "membership_id": member.get("id"), "template_id": member["template_id"],
                           "concept_id": member["concept_id"], "entity_identity_claim": False,
                           "reference_variants_pending": member.get("reference_variants_pending"),
                           "evidence_ids": member.get("evidence_ids", [])})
        self.mapping_proofs[rid] = proofs
        ids = {proof["type_id"] for proof in proofs}
        return [self.types[x] for x in sorted(ids) if x in self.types
                and self.types[x].get("category") == "business_type"]

    def root_type(self, item):
        seen = set()
        while item.get("parent") in self.types and item["parent"] not in seen:
            seen.add(item["parent"])
            item = self.types[item["parent"]]
        return item.get("parent")

    def canonicals(self, items):
        return {x.get("canonical_type_id") or x["id"] for x in items}

    def formulas(self, items):
        ids = {x["id"] for x in items}
        return {f for c in self.concepts if c.get("ontology_type_id") in ids for f in c.get("source_formulas", [])}

    def brief(self, item):
        keys = ("id", "ontology_type_id", "parent", "label", "status", "reason", "raw_formula",
                "definition_parameters", "applicability_scope", "rule_id", "source", "target",
                "selector", "scope_bindings", "numeric_overlap_only", "semantic_relation",
                "observed_value", "observation_coordinates", "value_column", "evidence_ids")
        return {k: item[k] for k in keys if k in item}

    def evidence(self, ids):
        result = []
        for eid in sorted(set(ids)):
            row = self.db.execute("SELECT body FROM items WHERE kind='evidence' AND id=?", (eid,)).fetchone() if self.db else None
            item = json.loads(row[0]) if row else {}
            result.append({"id": eid, "found": bool(item), "source_ref": item.get("source_ref"),
                           "locator": {"sqlite": str(self.run / "work/results.sqlite"), "kind": "evidence", "id": eid}})
        return result

    def result(self, sample_id, title, sources, status, reason, *, items=(), details=None):
        steps = [s for source in sources for s in self.bundle_steps.get(source.get("record_id"), [])]
        if not all(s.get("applicable") for s in sources):
            status, reason = "not_found", "sample_not_in_this_input; not counted as a failed expectation"
        elif not all(s.get("snapshot_file_verified") and s.get("snapshot_schema_verified")
                     and s.get("sample_values_match") for s in sources):
            status, reason = "unresolved", "source_file_or_sample_differs_from_frozen_expectation"
        elif status == "not_found":
            relevant = [s for s in steps if s.get("task_kind") == "concept_induction"]
            explicit = [s for s in relevant if s.get("status") == "rejected"
                        or str(s.get("reason", "")).startswith("Group review rejected:")]
            if explicit:
                status, reason = "rejected", "explicit_rejection_in_group_steps"
            elif relevant or self.warnings:
                status, reason = "unresolved", "no_accepted_match; see stage reasons and artifact warnings"
        items = list(items)
        ids = {eid for item in items for eid in item.get("evidence_ids", [])}
        proof = self.evidence(ids)
        if status == "found" and (not ids or any(not x["found"] for x in proof)):
            status, reason = "unresolved", "matching_output_found_but_evidence_not_fully_resolvable"
        value = {"sample_id": sample_id, "title": title, "status": status, "reason": reason,
                 "applicable": all(s.get("applicable") for s in sources), "sources": sources,
                 "source_type_mappings": [{"record_id": source.get("record_id"),
                                           "mappings": self.mapping_proofs.get(source.get("record_id"), [])}
                                          for source in sources if source.get("record_id")],
                 "matched_output_count": len(items), "matched_outputs": [self.brief(x) for x in items[:8]],
                 "outputs_not_shown": max(0, len(items)-8), "stage_outcomes": steps[:12],
                 "stage_outcomes_not_shown": max(0, len(steps)-12), "evidence": proof[:16],
                 "evidence_count": len(proof), "evidence_not_shown": max(0, len(proof)-16),
                 "details": details or {}}
        self.outcomes.append(value)

    def rules_between(self, a, af, b, bf):
        return [r for r in self.rules if r.get("source") == {"table": a, "field": af}
                and r.get("target") == {"table": b, "field": bf}]

    def plans_between(self, a, af, b, bf):
        return [r for r in self.plan.get("relations", []) if r.get("source_table") == a
                and r.get("source_column") == af and r.get("target_table") == b and r.get("target_column") == bf]

    def facts(self, table, column=None):
        if not self.db:
            return []
        sql = "SELECT body FROM items WHERE kind='objects' AND json_extract(body,'$.source_ref.table')=? AND json_type(body,'$.observed_value') IS NOT NULL"
        args = [table]
        if column:
            sql += " AND json_extract(body,'$.value_column')=?"
            args.append(column)
        return [json.loads(x[0]) for x in self.db.execute(sql, args)]


def evaluate(a):
    c, f = "semantic_control.", "fruit_market."
    s = {}
    specifications = {
        "profit": (c+"business_metric_definition", 1, {"metric_key": "apple_profit", "calculation_formula": "revenue - cost"}),
        "charged": (c+"business_metric_definition", 2, {"metric_key": "apple_profit_after_charge", "calculation_formula": "(revenue - cost) * 0.9"}),
        "revenue": (c+"general_measure_definition", 1, {"measure_key": "revenue", "measure_name": "收入"}),
        "cost": (c+"general_measure_definition", 2, {"measure_key": "cost", "measure_name": "成本"}),
        "year": (c+"dimension_definition", 2, {"dimension_key": "calendar_year", "dimension_name": "公历年"}),
        "observation": (c+"operating_fact", 1, {"year": "2025", "apple_profit_amount": "400"}),
        "metric": (f+"fruit_metric_detail", 1, {"metric_code": "MET000001", "calculation_formula": "sales_total = sales_volume * avg_price", "business_anchor_code": "MANCH001"}),
        "measure": (f+"fruit_measure_def", 1, {"measure_code": "MES000001", "standard_name": "苹果均价", "period": "Q", "budget_flag": "1"}),
        "measure2": (f+"fruit_measure_def", 41, {"measure_code": "MES000041", "standard_name": "苹果均价", "period": "M", "budget_flag": "0"}),
        "anchor": (f+"fruit_metric_anchor", 1, {"anchor_code": "MANCH001"}),
        "api_ref": (f+"fruit_param_ref_rule", 1, {"business_id": "1", "business_type": "API"}),
        "card_ref": (f+"fruit_param_ref_rule", 3, {"business_id": "1", "business_type": "CARD"}),
        "table_ref": (f+"fruit_param_ref_rule", 5, {"business_id": "1", "business_type": "TABLE"}),
        "dimension": (f+"fruit_dim_definition", 1, {"fruit_dim_def_id": "1", "dim_code": "DIM0001", "dim_cn_name": "水果类别"}),
        "member": (f+"fruit_dim_member", 1, {"dim_code": "DIM0001", "member_code": "APPLE", "member_cn_name": "苹果"}),
        "bad_unit": (f+"fruit_measure_def", 3, {"measure_code": "MES000003", "unit": "吨", "source_field": "avg_price_cny_per_kg"}),
        "fact1": (f+"fruit_import_staging_temp", 1, {"fruit_code": "APPLE", "origin_region_code": "EAST", "period": "2025Q1", "sales_amount_cny": "90000.0"}),
        "fact2": (f+"fruit_import_staging_temp", 21, {"fruit_code": "APPLE", "origin_region_code": "EAST", "period": "2025Q1", "sales_amount_cny": "250000.0"}),
    }
    for key, spec in specifications.items():
        s[key] = a.source(*spec)
    a.prepare()
    t = {key: a.accepted(source) for key, source in s.items()}
    def present(*keys):
        return all(t[key] for key in keys)
    def outputs(*keys):
        return [item for key in keys for item in t[key]]

    good = present("profit", "charged")
    conflict = bool(a.canonicals(t["profit"]) & a.canonicals(t["charged"]))
    formulas = all(s[key]["expected_values"]["calculation_formula"] in a.formulas(t[key]) for key in ("profit", "charged"))
    a.result("C1", "同名异公式不能 exact 合并", [s["profit"], s["charged"]],
             "conflict" if conflict else "found" if good and formulas else "unresolved" if good else "not_found",
             "distinct_canonical_types_and_source_formulas" if good and formulas and not conflict else "missing_or_conflicting_type_formula_contract", items=outputs("profit", "charged"))
    expectations = {"profit": "Metric", "revenue": "Measure", "cost": "Measure"}
    wrong = [x["id"] for key, root in expectations.items() for x in t[key] if a.root_type(x) != root]
    a.result("C2", "通用度量与经营指标分类", [s[x] for x in expectations],
             "conflict" if wrong else "found" if present(*expectations) else "not_found",
             "source_defined_root_classification", items=outputs(*expectations), details={"wrong_root_type_ids": wrong})
    wrong = [x["id"] for x in t["profit"] if x.get("applicability_scope", {}).get("period_scope") == "公历年"
             or x.get("definition_parameters", {}).get("period_scope") != "公历年"]
    wrong += [x["id"] for x in t["year"] if a.root_type(x) != "Dimension"]
    a.result("C3", "公历年参数区别于年份观测", [s["profit"], s["year"], s["observation"]],
             "conflict" if wrong else "found" if present("profit", "year") else "not_found",
             "parameter_and_dimension_only; does_not_certify_fact_binding", items=outputs("profit", "year"),
             details={"wrong_contract_type_ids": wrong, "fact_binding_outcomes": [x for x in a.bindings if x.get("table") == c+"operating_fact"]})

    ids = {x["id"] for x in t["metric"]}
    calculations = [x for x in a.calculations if x.get("source_type_id") in ids]
    wrong = any(a.root_type(x) != "Metric" for x in t["metric"])
    valid_formula = s["metric"]["expected_values"]["calculation_formula"] in a.formulas(t["metric"])
    accepted_calculations = [x for x in calculations if x.get("status") == "accepted"]
    a.result("F1", "水果销售指标保留原式；缺失符号不强行绑定", [s["metric"]],
             "conflict" if wrong else "unresolved" if accepted_calculations else "found" if t["metric"] and valid_formula and calculations else "not_found",
             "accepted_operand_binding_requires_independent_manual_review" if accepted_calculations else "type_and_formula_with_explicit_calculation_status",
             items=[*t["metric"], *calculations], details={"dependencies": [x for x in a.dependencies if x.get("source_type_id") in ids], "calculation_bindings": [x.get("bindings", []) for x in calculations]})
    wrong = [x["id"] for x in t["measure"] if a.root_type(x) == "Measure" and "苹果" in x.get("definition", "")]
    a.result("F2", "水果限定定义不能直接当无对象限制的通用度量", [s["measure"]],
             "conflict" if wrong else "found" if t["measure"] and all(a.root_type(x) == "Metric" for x in t["measure"]) else "unresolved" if t["measure"] else "not_found",
             "full_definition_classification_not_table_name", items=t["measure"], details={"conflicting_type_ids": wrong})
    shared = a.canonicals(t["measure"]) & a.canonicals(t["measure2"])
    retained = all(all({**x.get("applicability_scope", {}), **x.get("definition_parameters", {})}.get(k) == s[key]["expected_values"][k]
                       for k in ("period", "budget_flag")) for key in ("measure", "measure2") for x in t[key])
    a.result("F3", "同名同定义但期间和预算标记不同", [s["measure"], s["measure2"]],
             "conflict" if shared else "found" if present("measure", "measure2") and retained else "unresolved" if present("measure", "measure2") else "not_found",
             "separate_types_must_retain_source_parameters", items=outputs("measure", "measure2"), details={"shared_canonical_ids": sorted(shared), "parameters_retained": retained and present("measure", "measure2")})
    rules = a.rules_between(f+"fruit_metric_detail", "business_anchor_code", f+"fruit_metric_anchor", "anchor_code")
    checked = [x for x in rules if x.get("status") in ("checked_technical", "observed_subset") and x.get("snapshot_id") == a.snapshot
               and x.get("verification", {}).get("scan_scope") == "full_input" and not x.get("selector")]
    # Rule schema endpoint evidence is retained even when no business concept
    # has been accepted for either endpoint.
    for rule in rules:
        rule.setdefault("evidence_ids", ["schema:" + side["table"] + ":" + side["field"] for side in (rule["source"], rule["target"])])
    a.result("F4", "异名 business_anchor_code 到 anchor_code", [s["metric"], s["anchor"]],
             "found" if checked else "unresolved" if rules else "not_found", "technical_match_only_not_business_predicate", items=rules,
             details={"business_plans": a.plans_between(f+"fruit_metric_detail", "business_anchor_code", f+"fruit_metric_anchor", "anchor_code")})
    branches, plans = {}, []
    for label, table, field in (("API", "fruit_market_api", "fruit_api_id"), ("CARD", "fruit_dashboard_card", "fruit_card_id"), ("TABLE", "fruit_wide_table_def", "fruit_wide_table_id")):
        rows = a.rules_between(f+"fruit_param_ref_rule", "business_id", f+table, field)
        branches[label] = [a.brief(x) for x in rows if x.get("selector", {}).get("business_type") == label]
        plans += a.plans_between(f+"fruit_param_ref_rule", "business_id", f+table, field)
    unconditional = [x for x in plans if not x.get("selector") and not x.get("witnessed_pairs")]
    a.result("F5", "相同 business_id 按业务类型指向不同表", [s["api_ref"], s["card_ref"], s["table_ref"]],
             "conflict" if unconditional else "unresolved", "conditional_technical_candidates_do_not_alone_prove_business_semantics",
             items=plans, details={"conditional_rules": branches, "unconditional_unwitnessed_business_plans": [x["id"] for x in unconditional]})
    shared = a.canonicals(t["dimension"]) & a.canonicals(t["metric"])
    plans = a.plans_between(f+"fruit_dim_definition", "fruit_dim_def_id", f+"fruit_metric_detail", "fruit_metric_detail_id")
    plans += a.plans_between(f+"fruit_metric_detail", "fruit_metric_detail_id", f+"fruit_dim_definition", "fruit_dim_def_id")
    a.result("F6", "独立主键数字相等不证明对象相同", [s["dimension"], s["metric"]],
             "conflict" if shared else "unresolved" if plans else "found" if present("dimension", "metric") else "not_found",
             "distinct_types_only; other_business_relations_require_quote_review", items=outputs("dimension", "metric"),
             details={"shared_canonical_ids": sorted(shared), "numeric_endpoint_business_plans": plans})
    wrong = any(a.root_type(x) != "Dimension" for x in t["dimension"])
    rules = a.rules_between(f+"fruit_dim_member", "dim_code", f+"fruit_dim_definition", "dim_code")
    a.result("F7", "水果类别维度及 APPLE 成员来源", [s["dimension"], s["member"]],
             "conflict" if wrong else "unresolved" if t["dimension"] else "not_found",
             "dimension_type_found_does_not_certify_member_identity_or_hierarchy", items=t["dimension"],
             details={"technical_member_rules": [a.brief(x) for x in rules], "member_definition_memberships": a.members.get(s["member"].get("record_id"), [])})
    ids = {x["id"] for x in t["bad_unit"]}
    calculations = [x for x in a.calculations if x.get("source_type_id") in ids]
    a.result("F8", "进口量吨与价格来源字段的量纲冲突", [s["bad_unit"]],
             "unresolved" if t["bad_unit"] or calculations else "not_found", "requires_source_binding_and_unit_review; do_not_repair_input_silently",
             items=[*t["bad_unit"], *calculations], details={"calculation_bindings": [x.get("bindings", []) for x in calculations]})
    facts = [x for x in a.facts(f+"fruit_import_staging_temp", "sales_amount_cny")
             if all(x.get("observation_coordinates", {}).get(k) == v for k,v in
                    {"fruit_code": "APPLE", "origin_region_code": "EAST", "period": "2025Q1"}.items())]
    from decimal import Decimal, InvalidOperation
    witnessed, contradictions = set(), []
    for x in facts:
        ref = x.get("source_ref", {})
        for key in ("fact1", "fact2"):
            source = s[key]
            if (source.get("row_number") in ref.get("row_numbers", [])
                    or source.get("record_id") in ref.get("record_ids", [])):
                try:
                    same = Decimal(str(x.get("observed_value"))) == Decimal(source["expected_values"]["sales_amount_cny"])
                except InvalidOperation:
                    same = False
                if same:
                    witnessed.add(key)
                else:
                    contradictions.append({"instance_id": x["id"], "source_row": source["row_number"],
                                           "output_value": x.get("observed_value"),
                                           "source_value": source["expected_values"]["sales_amount_cny"]})
    a.result("F9", "同坐标的不同观测金额不能擅自取首值或聚合", [s["fact1"], s["fact2"]],
             "conflict" if contradictions else "found" if len(witnessed) == 2 else "unresolved",
             "source_observations_only; repeated_coordinates_do_not_prove_a_unique_business_grain",
             items=facts, details={"source_value_contradictions": contradictions, "source_rows_retained": sorted(witnessed),
                 "source_expectation": {"rows_at_three_coordinates": 745, "distinct_amounts": 44},
                 "field_binding_outcomes": [x for x in a.bindings if x.get("table") == f+"fruit_import_staging_temp" and x.get("value_column") == "sales_amount_cny"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, help="Optional JSON report; stdout otherwise")
    args = parser.parse_args()
    audit = Audit(args.run)
    try:
        evaluate(audit)
        report = {"audit_version": 2, "run": str(audit.run), "snapshot_id": audit.snapshot,
                  "run_status": audit.manifest.get("status", "manifest_missing"),
                  "scope": "12 targeted source-contract checks; not semantic accuracy or full acceptance",
                  "status_meanings": {"found": "specific observable contract found with resolvable evidence; only stated check is satisfied",
                                      "not_found": "no matching accepted output, or sample outside this input; not proof of rejection",
                                      "unresolved": "incomplete output/evidence or independent semantic review required",
                                      "rejected": "explicit rejection recorded for an exact-eligible sample",
                                      "conflict": "accepted content contradicts a checked source constraint"},
                  "counts": dict(Counter(x["status"] for x in audit.outcomes)),
                  "applicable_counts": dict(Counter(x["status"] for x in audit.outcomes if x["applicable"])),
                  "warnings": sorted(set(audit.warnings)), "artifacts": audit.used_files,
                  "samples": audit.outcomes}
        output = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
        if args.output:
            target = args.output.resolve()
            if target == Path(__file__).resolve() or target.is_relative_to(audit.root.resolve()) or target.is_relative_to(audit.run):
                parser.error("Report must be outside source input and run artifacts")
            target.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
    finally:
        if audit.db:
            audit.db.close()


if __name__ == "__main__":
    main()
