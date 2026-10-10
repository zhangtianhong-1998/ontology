"""Bounded joint row examples remain candidates rather than ontology assertions."""

from copy import deepcopy

import pytest

from ontology_r2.discovery import validate_candidate
from ontology_r2.row_bundles import joint_examples
from test_discovery import SmallDataset


class RowDataset(SmallDataset):
    def record_id(self, table, row):
        return f"{table}:{self.snapshot_id}:{row['__r2_row']}"


@pytest.fixture
def checked_pair():
    data = RowDataset({
        "demo.source": (["id", "kind", "ref", "region", "display"], [
            ("s1", "linked", "K1", "north", "源甲"),
            ("s2", "linked", "K2", "south", "源乙"),
            ("s3", "linked", "X", "north", "缺失"),
            ("s4", "linked", "AMB", "north", "歧义"),
            ("s5", "other", "K1", "north", "不适用"),
            ("s6", "linked", " K1 ", "north", "未归一化"),
        ], ["id"]),
        "demo.target": (["id", "code", "scope", "name"], [
            ("t1", "K1", "north", "目标甲"),
            ("t2", "K2", "south", "目标乙"),
            ("t3", "AMB", "north", "目标丙一"),
            ("t4", "AMB", "north", "目标丙二"),
        ], ["id"]),
    })
    candidate = {"candidate_id": "pair-1",
                 "source": {"table": "demo.source", "field": "ref"},
                 "target": {"table": "demo.target", "field": "code"}}
    check = validate_candidate(data, candidate, selector={"kind": "linked"},
                               scope_bindings={"scope": "region"})
    yield data, candidate, check
    data.close()


def test_joint_examples_bind_matching_rows_and_one_counterexample(checked_pair):
    data, candidate, check = checked_pair
    first = joint_examples(data, candidate, check, limit=10,
                           source_fields=("display",), target_fields=("name",))
    second = joint_examples(data, candidate, check, limit=10,
                            source_fields=("display",), target_fields=("name",))
    assert first == second
    assert first["status"] == "examples"
    assert first["semantic_relation"] == "unresolved"
    assert first["validation_ref"]["unique_matches"] == 2
    assert first["validation_ref"]["selector"] == {"kind": "linked"}
    assert first["validation_ref"]["scope_bindings"] == {"scope": "region"}
    assert len(first["matched_pairs"]) == 2
    assert [(pair["source_record"]["row_number"], pair["target_record"]["row_number"],
             pair["matching_raw_value"]) for pair in first["matched_pairs"]] == [
                 (1, 1, "K1"), (2, 2, "K2")]
    assert first["matched_pairs"][0]["source_record"] == {
        "table": "demo.source", "row_number": 1,
        "record_id": "demo.source:synthetic-snapshot:1",
        "fields": {"ref": "K1", "display": "源甲"}}
    assert first["matched_pairs"][0]["target_record"]["fields"] == {
        "code": "K1", "name": "目标甲"}
    assert first["counterexamples"] == [{
        "reason": "missing_in_input",
        "source_record": {"table": "demo.source", "row_number": 3,
                          "record_id": "demo.source:synthetic-snapshot:3",
                          "fields": {"ref": "X", "display": "缺失"}}}]


def test_joint_examples_reject_unchecked_stale_and_unknown_fields(checked_pair):
    data, candidate, check = checked_pair
    unchecked = deepcopy(check)
    unchecked["decision"]["status"] = "proposed"
    assert joint_examples(data, candidate, unchecked)["matched_pairs"] == []
    assert joint_examples(data, candidate, unchecked)["counterexamples"] == []
    transformed = deepcopy(check)
    transformed["normalization"] = "casefold"
    assert joint_examples(data, candidate, transformed)["status"] == "not_applicable"
    stale = deepcopy(check)
    stale["snapshot_id"] = "other-snapshot"
    with pytest.raises(ValueError, match="same snapshot"):
        joint_examples(data, candidate, stale)
    with pytest.raises(ValueError, match="Unknown field"):
        joint_examples(data, candidate, check, source_fields=('ref"; DROP TABLE src_0; --',))
    assert joint_examples(data, candidate, check, limit=0)["status"] == "disabled_by_limit"
    assert data.db.execute('SELECT count(*) FROM "src_0"').fetchone()[0] == 6
