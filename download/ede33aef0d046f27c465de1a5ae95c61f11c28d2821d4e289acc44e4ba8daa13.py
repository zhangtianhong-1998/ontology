"""Exercise attribution and three contracts using small parsed-artifact fixtures.

No local run/input directory or model is needed. Source verification and evidence
lookup are stubbed as valid here; these tests exercise the auditor's decisions,
not the extraction pipeline's semantic quality.
"""

import importlib.util
from collections import defaultdict
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/audit_semantic_samples.py"
SPEC = importlib.util.spec_from_file_location("audit_semantic_samples", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def _results(audit):
    audit.outcomes = []
    module.evaluate(audit)
    return {item["sample_id"]: item for item in audit.outcomes}


@pytest.fixture
def audit(tmp_path):
    a = module.Audit.__new__(module.Audit)
    a.run, a.db, a.snapshot = tmp_path, None, "snapshot"
    a.warnings, a.sources, a.mapping_proofs = [], {}, {}
    a.members, a.bundle_steps = defaultdict(list), defaultdict(list)
    a.templates, a.types, a.by_concept = {}, {}, {}
    a.concepts, a.alignments, a.outcomes = [], [], []
    a.rules, a.bindings, a.calculations, a.dependencies, a.rejections = [], [], [], [], []
    a.plan = {"relations": []}
    a.prepare = lambda: None
    a.evidence = lambda ids: [{"id": eid, "found": True} for eid in ids]

    def source(table, row, expected):
        result = {"table": table, "row_number": row, "expected_values": expected,
                  "applicable": table.startswith("semantic_control."),
                  "snapshot_file_verified": True, "snapshot_schema_verified": True,
                  "sample_values_match": True}
        if result["applicable"]:
            result["record_id"] = f"{table}:{row}"
            a.sources[result["record_id"]] = result
        return result

    a.source = source
    specs = [
        ("profit", "business_metric_definition", 1, "Metric", ["revenue - cost"]),
        ("charged", "business_metric_definition", 2, "Metric", ["(revenue - cost) * 0.9"]),
        ("revenue", "general_measure_definition", 1, "Measure", ["sum(revenue_amount)"]),
        ("cost", "general_measure_definition", 2, "Measure", ["sum(cost_amount)"]),
        ("year", "dimension_definition", 2, "Dimension", []),
    ]
    for name, table, row, root, formulas in specs:
        table, type_id, cid = "semantic_control." + table, "type:" + name, "concept:" + name
        rid, tid, fingerprint = f"{table}:{row}", "template:" + name, "fingerprint:" + name
        a.types[type_id] = {"id": type_id, "parent": root, "category": "business_type",
                            "definition_parameters": {"period_scope": "公历年"} if root == "Metric" else {},
                            "applicability_scope": {}, "evidence_ids": ["evidence:" + name]}
        concept = {"id": cid, "ontology_type_id": type_id, "ontology_level": "type",
                   "decision": "accepted_by_automatic_checks", "source_formulas": formulas,
                   "source_refs": [{"record_id": rid}]}
        a.concepts.append(concept)
        a.by_concept[cid] = concept
        a.alignments.append({"id": "alignment:" + name, "concept_id": cid, "source_record_id": rid,
                             "mapping_kind": "exact", "decision": "accepted_by_automatic_checks",
                             "evidence_ids": ["evidence:" + name]})
        a.templates[tid] = {"id": tid, "table": table, "type_id": type_id, "concept_id": cid,
                            "semantic_fingerprint": fingerprint}
        a.members[rid].append({"id": "membership:" + name, "record_id": rid,
                               "table": table, "row_number": row, "snapshot_id": a.snapshot,
                               "type_id": type_id, "concept_id": cid, "template_id": tid,
                               "kind": "definition_type_membership", "status": "definition_template_match",
                               "mapping_kind": "shares_definition_type_template", "entity_identity_claim": False,
                               "verification": {"method": "complete_original_source_field_equality",
                                                "semantic_fingerprint": fingerprint},
                               "evidence_ids": ["evidence:" + name]})
    # These genuine context links caused the original false positives: a source
    # may be related to another type without defining that type itself.
    for name in ("revenue", "charged"):
        rid = "semantic_control.business_metric_definition:1"
        a.by_concept["concept:" + name]["source_refs"].append({"record_id": rid})
        a.alignments.append({"id": "related:" + name, "concept_id": "concept:" + name,
                             "source_record_id": rid, "mapping_kind": "related",
                             "decision": "accepted_by_automatic_checks", "evidence_ids": ["evidence:profit"]})
    results = _results(a)
    assert [results[key]["status"] for key in ("C1", "C2", "C3")] == ["found"] * 3
    assert all(not results[f"F{i}"]["applicable"] for i in range(1, 10))
    return a


def test_c1_detects_shared_canonical_for_distinct_formulas(audit):
    audit.types["type:charged"]["canonical_type_id"] = "type:profit"
    assert _results(audit)["C1"]["status"] == "conflict"


def test_c2_detects_measure_misclassified_as_metric(audit):
    audit.types["type:revenue"]["parent"] = "Metric"
    result = _results(audit)["C2"]
    assert result["status"] == "conflict"
    assert result["details"]["wrong_root_type_ids"] == ["type:revenue"]


def test_c3_detects_parameter_moved_into_applicability(audit):
    item = audit.types["type:profit"]
    item["definition_parameters"].pop("period_scope")
    item["applicability_scope"]["period_scope"] = "公历年"
    result = _results(audit)["C3"]
    assert result["status"] == "conflict"
    assert result["details"]["wrong_contract_type_ids"] == ["type:profit"]


def test_checked_template_supports_type_membership_without_exact_alignment(audit):
    audit.alignments = []
    result = _results(audit)["C1"]
    assert result["status"] == "found"
    proofs = [proof for source in result["source_type_mappings"] for proof in source["mappings"]]
    assert len(proofs) == 2
    assert all(proof["kind"] == "checked_definition_template" for proof in proofs)
    assert all(proof["entity_identity_claim"] is False for proof in proofs)


def test_unchecked_template_and_source_refs_do_not_establish_type_membership(audit):
    audit.alignments = []
    for members in audit.members.values():
        for member in members:
            member["verification"]["method"] = "unchecked"
    result = _results(audit)["C1"]
    assert result["status"] == "unresolved"
    assert result["matched_output_count"] == 0
    assert any(w.startswith("unchecked_definition_membership_ignored:") for w in audit.warnings)


def test_only_exact_alignment_establishes_identity_not_related_context(audit):
    audit.members.clear()
    result = _results(audit)["C1"]
    assert result["status"] == "found"
    assert result["matched_output_count"] == 2
    audit.alignments = [item for item in audit.alignments if item["mapping_kind"] != "exact"]
    result = _results(audit)["C1"]
    assert result["status"] == "not_found"
    assert result["matched_output_count"] == 0
