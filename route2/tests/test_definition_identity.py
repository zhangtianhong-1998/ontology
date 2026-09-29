"""Definition metadata survives without becoming calculation or instance identity."""
from copy import deepcopy
from types import SimpleNamespace
import asyncio

import pytest

from ontology_r2.group_incremental import (ConceptBundleDecision, compile_concept,
                                           _compiled_object_type, construct_from_bundles)
from ontology_r2.models import BuildPlan
from ontology_r2.template_projection import compile_projection, reuse_projection
from test_group_incremental import PROFILE
from test_template_projection import setup, decision


def dimension(namespace):
    record = {"record_id": "catalog:1", "table": "catalog", "kind": "definition",
              "row_number": 1, "scope": {"c9": namespace}, "unit": "", "fields": {
                  "name": [{"column": "c1", "value": "设备等级"}],
                  "description": [{"column": "c2", "value": "设备等级按维护能力对设备分类。"}],
                  "metadata": [{"column": "c3", "value": "zh"}],
                  "provenance": [{"column": "c4", "value": "登记系统"}],
                  "identity": [{"column": "c9", "value": namespace}]}}
    proposed = ConceptBundleDecision(status="proposed", root_type="Dimension", ontology_level="type",
        label="设备等级", definition="设备等级按维护能力对设备分类。", classification_basis="other",
        scope_roles={"c9": "identity"},
        alignments=[{"record_id": "catalog:1", "mapping_kind": "exact", "quote": "设备等级"}])
    return {"records": [record]}, proposed


def test_uncommented_dimension_retains_metadata_and_namespace_without_parameter():
    types = []
    for namespace in ("tenant-a", "tenant-b"):
        data = SimpleNamespace(snapshot_id="snap", evidence={}, tables={})
        packet, proposed = dimension(namespace)
        concept, _ = compile_concept(data, PROFILE, packet, proposed, {})
        item = _compiled_object_type(concept)
        assert item.definition_parameters == item.applicability_scope == {}
        assert item.identity_qualifiers == {"c9": namespace}
        assert {p.role for p in item.source_properties} >= {"metadata", "provenance", "identity"}
        types.append(item.id)
    assert len(set(types)) == 2


@pytest.mark.parametrize("role", ["parameter", "unrestricted", "applicability"])
def test_namespace_cannot_be_reclassified_to_erase_identity(role):
    packet, proposed = dimension("tenant-a")
    proposed.scope_roles["c9"] = role
    with pytest.raises(ValueError):
        compile_concept(SimpleNamespace(snapshot_id="snap", evidence={}, tables={}),
                        PROFILE, packet, proposed, {})


def test_projection_never_reuses_across_namespace_or_wildcards_identity():
    data, bundle = setup()
    for record in bundle["records"]:
        record["fields"]["identity"] = [{"column": "ns", "value": "tenant-a"}]
    plan, template, _ = compile_projection(data, PROFILE, BuildPlan(), bundle, decision())
    target = next(item for item in plan.object_types if item.id == template["object_type_id"])
    assert target.identity_qualifiers == {"ns": "tenant-a"}
    changed = deepcopy(bundle["records"][0])
    changed["fields"]["identity"][0]["value"] = "tenant-b"
    assert reuse_projection(data, [template], {"records": [changed]}) is None


def test_projection_component_identity_comes_from_its_own_source_definition():
    core = BuildPlan()
    component_ids = []
    for namespace in ("tenant-a", "tenant-b"):
        data, bundle = setup()
        for row in bundle["records"]:
            row["fields"]["identity"] = [{"column": "ns", "value": namespace}]
        core, template, _ = compile_projection(data, PROFILE, core, bundle, decision())
        ids = {slot["target_type_id"] for slot in template["slots"] if slot.get("target_type_id")}
        assert all(item.identity_qualifiers == {"ns": namespace}
                   for item in core.object_types if item.id in ids)
        component_ids.append(ids)
    assert component_ids[0].isdisjoint(component_ids[1])


def test_later_type_upgrade_preserves_identity_in_concept_and_core():
    data = SimpleNamespace(snapshot_id="snap", evidence={}, tables={})
    packet, proposed = dimension("tenant-a")
    packet.update(bundle_id="first", task_kind="concept_induction")
    class Decisions:
        calls = 0
        async def ask(self, task, payload, schema):
            self.calls += 1
            if self.calls == 1:
                return proposed.model_copy(update={"ontology_level": "unresolved", "scope_roles": {}})
            return proposed
    packets = [packet, {**packet, "bundle_id": "second"}]
    result = asyncio.run(construct_from_bundles(data, PROFILE, BuildPlan(), packets,
                                              Decisions(), review=False))
    assert all(step["status"] == "accepted" for step in result["steps"])
    assert result["concepts"][0]["identity_qualifiers"] == {"c9": "tenant-a"}
    assert result["plan"].object_types[0].identity_qualifiers == {"c9": "tenant-a"}
