"""Predicate names come from the four user-defined roots, not model prose."""

import pytest

from ontology_r2.relation_contract import (
    canonical_relation_id, canonical_relation_label,
    validate_proposed_relation_label,
)


@pytest.mark.parametrize("parent", ["contains", "depends_on", "related_to", "points_to"])
def test_canonical_relation_names_are_stable_english_predicates(parent):
    first = canonical_relation_id(parent, "type:profit", "type:revenue")
    assert first == canonical_relation_id(parent, "type:profit", "type:revenue")
    assert first.startswith("business_relation:" + parent + ":")
    assert first != canonical_relation_id(parent, "type:revenue", "type:profit")
    assert canonical_relation_label(parent) == parent


def test_source_rule_qualifier_only_changes_record_relation_identity():
    left = canonical_relation_id(
        "points_to", "source:a", "source:b", namespace="relation",
        qualifier=["a", "code", "b", "id"])
    right = canonical_relation_id(
        "points_to", "source:a", "source:b", namespace="relation",
        qualifier=["a", "alias", "b", "id"])
    assert left != right
    with pytest.raises(ValueError, match="cannot depend on a source rule"):
        canonical_relation_id("points_to", "type:a", "type:b", qualifier=["rule-1"])


def test_model_label_must_be_a_directed_predicate_not_a_new_ontology_name():
    validate_proposed_relation_label(
        "经营利润依赖收入", "depends_on", subject_label="经营利润", object_label="收入")
    validate_proposed_relation_label(
        "depends_on", "depends_on", subject_label="经营利润", object_label="收入")
    for invalid in ("收入依赖经营利润", "经营利润依赖香蕉", "经营利润包含收入",
                    "经营利润显著贡献于收入", "经营利润依赖收入。这里是自由解释"):
        with pytest.raises(ValueError):
            validate_proposed_relation_label(
                invalid, "depends_on", subject_label="经营利润", object_label="收入")
    with pytest.raises(ValueError, match="Unknown object relation root"):
        canonical_relation_label("drives")


def test_registered_derived_predicate_and_evidenced_roles_have_distinct_identity():
    from ontology_r2.relation_contract import validate_calculation_parameter_evidence
    base = canonical_relation_id("depends_on", "profit", "revenue")
    left = canonical_relation_id("depends_on", "profit", "revenue",
                                 predicate_name="calculation_dependency",
                                 semantic_parameters={"operand_role": "minuend"})
    right = canonical_relation_id("depends_on", "profit", "revenue",
                                  predicate_name="calculation_dependency",
                                  semantic_parameters={"operand_role": "subtrahend"})
    assert len({base, left, right}) == 3
    assert canonical_relation_label("depends_on", "calculation_dependency") == "calculation_dependency"
    validate_proposed_relation_label("计算依赖", "depends_on", predicate_name="calculation_dependency")
    validate_calculation_parameter_evidence({"operand_role": "minuend"}, ["利润 = 收入 - 成本"], ["收入"])
    with pytest.raises(ValueError, match="parsed source formula"):
        validate_calculation_parameter_evidence({"operand_role": "subtrahend"}, ["利润 = 收入 - 成本"], ["收入"])
    with pytest.raises(ValueError, match="parsed source formula"):
        validate_calculation_parameter_evidence({"operand_role": "minuend"}, ["收入不是利润的被减数"], ["收入"])
    with pytest.raises(ValueError, match="Unsupported predicate semantic parameters"):
        canonical_relation_id("depends_on", "a", "b", semantic_parameters={"arbitrary": "claim"})
    with pytest.raises(ValueError, match="incompatible"):
        canonical_relation_label("contains", "calculation_dependency")


@pytest.mark.parametrize("parent,predicate,cue", [
    ("points_to", "definition_reference", "points to"),
    ("related_to", "scope_constraint", "related to"),
    ("depends_on", "calculation_dependency", "依赖"),
    ("contains", "has_member", "包含"),
])
def test_registered_predicate_accepts_own_root_display_cue(parent, predicate, cue):
    validate_proposed_relation_label(cue, parent, predicate_name=predicate)
    assert canonical_relation_label(parent, predicate) == predicate
    validate_proposed_relation_label("甲" + cue + "乙", parent, predicate_name=predicate,
                                     subject_label="甲", object_label="乙")
    for label in ("乙" + cue + "甲", "uses", "caused by", "甲随意关联乙"):
        with pytest.raises(ValueError):
            validate_proposed_relation_label(label, parent, predicate_name=predicate,
                                             subject_label="甲", object_label="乙")


def test_derived_predicate_rejects_wrong_root_cues_or_unregistered_name():
    with pytest.raises(ValueError):
        validate_proposed_relation_label("related to", "points_to", predicate_name="definition_reference")
    with pytest.raises(ValueError):
        validate_proposed_relation_label("points to", "related_to", predicate_name="scope_constraint")
    with pytest.raises(ValueError):
        validate_proposed_relation_label("points to", "points_to", predicate_name="uses_target")
