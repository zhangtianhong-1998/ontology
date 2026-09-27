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
