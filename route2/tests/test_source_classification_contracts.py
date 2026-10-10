"""Classification is source metadata, never guessed identity or row scope."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ontology_r2.classification_attributes import classification_basis
from ontology_r2.group_incremental import (
    ConceptBundleDecision, _compiled_object_type, bundle_request_payload, compile_concept,
)
from ontology_r2.models import BuildPlan
from ontology_r2.template_projection import TemplateProjectionDecision, compile_projection
from ontology_r2.type_equivalence import compile_equivalence, propose_equivalence_candidates
from ontology_r2.type_generalization import compile_generalization, propose_generalization_candidates
from test_template_projection import PROFILE


def classification_fixture(category="ASSET", declaration="业务类别，如设备或合同", proposed_role="identity"):
    data = SimpleNamespace(snapshot_id="s", evidence={}, tables={"ops.catalog": {
        "columns": [{"column_name": "c3", "column_comment": declaration}]}})
    record = {"record_id": "r1", "table": "ops.catalog", "row_number": 1, "kind": "definition",
              "scope": {"c3": category}, "unit": "", "fields": {
                  "name": [{"column": "c1", "value": "设备等级"}],
                  "description": [{"column": "c2", "value": "设备等级是一类按维护能力划分设备的分类维度。"}],
                  "scope": [{"column": "c3", "value": category}]}}
    decision = ConceptBundleDecision(status="proposed", root_type="Dimension", ontology_level="type",
        label="设备等级", definition=record["fields"]["description"][0]["value"], classification_basis="other",
        scope_roles={"c3": proposed_role},
        alignments=[{"record_id": "r1", "mapping_kind": "exact", "quote": "设备等级"}])
    return data, {"records": [record]}, decision


@pytest.mark.parametrize("role", ["identity", "parameter", "applicability", "classification"])
def test_source_category_repairs_scope_role_without_erasing_attribute(role):
    data, packet, proposed = classification_fixture(proposed_role=role)
    concept, _ = compile_concept(data, PROFILE, packet, proposed, {})
    item = _compiled_object_type(concept)
    assert item.classification_attributes == {"c3": "ASSET"}
    assert item.applicability_scope == item.definition_parameters == item.identity_qualifiers == {}
    assert concept["scope_roles"] == {"c3": "classification"}
    assert concept["proposed_scope_roles"] == {"c3": role}
    prop = next(p for p in item.source_properties if p.role == "classification")
    assert data.evidence[prop.evidence_ids[0]]["raw_fragment"] == "ASSET"
    payload = bundle_request_payload(data, PROFILE, BuildPlan(), packet)
    assert payload["scope_role_evidence"][0]["classification_basis"]["column"] == "c3"


@pytest.mark.parametrize("declaration", ["领域编码", "业务版本分类", "tenant category", "计算参数类别", "分类适用范围"])
def test_unknown_namespace_and_calculation_scopes_cannot_be_classified(declaration):
    data, packet, proposed = classification_fixture(declaration=declaration, proposed_role="classification")
    assert classification_basis(data, packet["records"][0], "c3", "ASSET") is None
    with pytest.raises(ValueError, match="Classification attribute"):
        compile_concept(data, PROFILE, packet, proposed, {})


def test_exact_and_projection_reuse_preserve_the_same_classification():
    data, packet, exact = classification_fixture()
    concept, _ = compile_concept(data, PROFILE, packet, exact, {})
    accepted = _compiled_object_type(concept)
    projection = TemplateProjectionDecision(root_type="Dimension", label=exact.label, definition=exact.definition,
        witness_record_ids=["r1"], label_evidence=[{"record_id": "r1", "column": "c1", "quote": exact.label}],
        applicability_scope={"c3": "ASSET"})
    plan, template, _ = compile_projection(data, PROFILE, BuildPlan(object_types=[accepted]), packet, projection)
    assert template["object_type_id"] == accepted.id
    assert template["classification_attributes"] == accepted.classification_attributes
    assert template["applicability_scope"] == {}
    assert len(plan.object_types) == 1
    other = deepcopy(packet["records"][0])
    other["record_id"] = "r2"
    other["scope"]["c3"] = other["fields"]["scope"][0]["value"] = "CONTRACT"
    packet["records"].append(other)
    projection.witness_record_ids.append("r2")
    with pytest.raises(ValueError, match="different source classification"):
        compile_projection(data, PROFILE, BuildPlan(), packet, projection)


def test_classification_changes_type_identity():
    identifiers = []
    for value in ("ASSET", "CONTRACT"):
        data, packet, decision = classification_fixture(value)
        concept, _ = compile_concept(data, PROFILE, packet, decision, {})
        identifiers.append(concept["ontology_type_id"])
    assert len(set(identifiers)) == 2


def test_equivalence_and_generalization_cannot_drop_classification():
    from test_type_equivalence import _fixture as equivalence_fixture, _decision as equivalence_decision
    from test_type_generalization import _fixture as generalization_fixture, _decision as generalization_decision
    data, core = equivalence_fixture()
    candidate = propose_equivalence_candidates(core, data, 1)[0]
    core.object_types[0].classification_attributes = {"category": "A"}
    core.object_types[1].classification_attributes = {"category": "B"}
    assert propose_equivalence_candidates(core, data, 1) == []
    with pytest.raises(ValueError, match="classification attributes differ"):
        compile_equivalence(data, core, candidate, equivalence_decision(data, candidate))
    data, core = generalization_fixture()
    core.object_types[0].classification_attributes = {"category": "A"}
    core.object_types[1].classification_attributes = {"category": "B"}
    assert propose_generalization_candidates(core, data, 1) == []
    with pytest.raises(ValueError, match="classification attributes differ"):
        compile_generalization(data, PROFILE, core, generalization_decision())
