"""A reviewed row relation may use template instances without claiming type equality."""
from copy import deepcopy

from ontology_r2.group_incremental import compile_business_relation, compile_relation
from ontology_r2.models import DerivedType
from ontology_r2.template_projection import _fields, _SEMANTIC, bind_projection
from test_relation_incremental_merge import checked_relations, decision_for, PROFILE


def typed_pair(data, core, bundle):
    decision = decision_for(bundle)
    core, relation_plan = compile_relation(data, PROFILE, core, bundle, decision)
    templates, bindings = [], []
    pair = bundle["examples"]["positive"][0]
    records = {row["record_id"]: row for row in bundle["records"]}
    for side in ("source", "target"):
        record = records[pair[side + "_record_id"]]
        values, roles = _fields(record)
        semantic = {k: v for k, v in values.items() if roles[k] & _SEMANTIC}
        type_id = "type:" + side
        core.object_types.append(DerivedType(id=type_id, parent="GeneralObject",
            definition="Accepted record class", evidence_ids=[f"schema:{record['table']}"],
            category="business_type", derivation_kind="template_projection"))
        template = {"template_id": "template:" + side, "snapshot_id": data.snapshot_id,
            "status": "accepted", "source_table": record["table"], "object_type_id": type_id,
            "semantic_columns": sorted(semantic), "invariants": semantic,
            "slots": [], "field_templates": []}
        templates.append(template)
        bindings.append(bind_projection(data, template, record))
    return core, relation_plan, decision, templates, bindings


def test_template_pair_emits_only_reviewed_record_witness(checked_relations):
    data, core, bundles = checked_relations
    bundle = next(iter(bundles.values()))
    core, plan, decision, templates, bindings = typed_pair(data, core, bundle)
    result, assertion, reason = compile_business_relation(
        data, PROFILE, core, bundle, decision, plan, [], [],
        template_bindings=bindings, template_projections=templates)
    assert reason is None
    pair = bundle["examples"]["positive"][0]
    assert assertion["subject"] == pair["source_record_id"]
    assert assertion["object"] == pair["target_record_id"]
    assert assertion["endpoint_scope"] == "typed_record_witness"
    assert assertion["endpoint_mapping_kinds"] == {
        "source": "template_instance", "target": "template_instance"}
    relation = next(t for t in result.relation_types if t.id == assertion["predicate"])
    assert relation.evidence_scope == "one_positive_pair_with_type_bindings"
    assert len(plan.witnessed_pairs) == 1


def test_stale_or_incompatible_binding_does_not_type_relation(checked_relations):
    data, core, bundles = checked_relations
    bundle = next(iter(bundles.values()))
    core, plan, decision, templates, bindings = typed_pair(data, core, bundle)
    for change in ("snapshot", "contract", "ambiguous"):
        bad_templates, bad_bindings = deepcopy(templates), deepcopy(bindings)
        if change == "snapshot":
            bad_bindings[0]["snapshot_id"] = "old"
        elif change == "contract":
            key = next(iter(bad_templates[0]["invariants"]))
            bad_templates[0]["invariants"][key] = "changed definition"
        else:
            other = deepcopy(bad_templates[0])
            other.update(template_id="other", object_type_id="type:target")
            bad_templates.append(other)
            duplicate = deepcopy(bad_bindings[0])
            duplicate.update(template_id="other", object_type_id="type:target")
            bad_bindings.append(duplicate)
        result, assertion, reason = compile_business_relation(
            data, PROFILE, core, bundle, decision, plan, [], [],
            template_bindings=bad_bindings, template_projections=bad_templates)
        assert result is assertion is None
        assert reason == "positive_record_requires_unambiguous_accepted_type_binding"
