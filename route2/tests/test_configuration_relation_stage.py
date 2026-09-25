"""A config row can witness a relation, but never becomes its object endpoint."""

import asyncio
from pathlib import Path

import pytest

from ontology_r2.configuration_relation_stage import (
    ConfigurationRelationDecision, adjudicate_configuration_relations,
    compile_configuration_relation,
)
from ontology_r2.configuration_relations import discover_configuration_relations
from ontology_r2.storage import read_yaml
from ontology_r2.validation import validate_plan
from test_configuration_relations import _input


PROFILE = read_yaml(Path(__file__).resolve().parents[1] / "ontologies/internal_model.yaml")


def _candidate(data, plan, concepts, alignments, spec):
    result = discover_configuration_relations(data, plan, concepts, alignments, [spec])
    return next(item for item in result["candidates"]
                if item["status"] == "endpoint_verified_candidate")


def _decision(**updates):
    base = {
        "status": "proposed", "direction": "source_to_target",
        "parent_relation": "depends_on", "label": "经营利润依赖收入",
        "definition": "经营利润指标依赖收入度量",
        "configuration_quote": "经营利润依赖收入",
        "source_definition_quote": "经营利润用于考察收入减成本的经营成果",
        "target_definition_quote": "收入是销售额的数值聚合",
    }
    base.update(updates)
    return ConfigurationRelationDecision.model_validate(base)


def test_compile_uses_definition_types_and_config_witness_only(tmp_path):
    data, plan, concepts, alignments, spec = _input(tmp_path)
    try:
        candidate = _candidate(data, plan, concepts, alignments, spec)
        compiled = compile_configuration_relation(
            data, PROFILE, plan, candidate, _decision(), concepts, alignments)
        assert compiled is not None
        next_plan, assertion = compiled
        assert validate_plan(next_plan, data, PROFILE) == []
        relation = next_plan.relation_types[-1]
        assert relation.category == "business_relation_type"
        assert relation.parent == "depends_on"
        assert relation.domain == ["type:profit"]
        assert relation.range == ["type:revenue"]
        assert relation.endpoint_basis == "configuration_reference"
        assert relation.evidence_scope == (
            "one_configuration_witness_with_exact_type_alignments")
        assert assertion["subject"] == "concept:profit"
        assert assertion["object"] == "concept:revenue"
        assert assertion["configuration_witness_record_id"] == candidate["configuration_record_id"]
        assert candidate["configuration_record_id"] not in assertion["source_record_pair"].values()
        assert assertion["decision"]["method"] == (
            "explicit_configuration_phrase_and_exact_type_alignments")
        # A repeated observation contributes a second witness only if it is
        # explicitly selected; a count of two is not two invented assertions.
        assert candidate["configuration_rows_with_same_code_pair"] == 2
        assert len(plan.relation_types) == 0
    finally:
        data.close()


@pytest.mark.parametrize("decision,reason", [
    (_decision(direction="target_to_source"), "predicate and direction"),
    (_decision(parent_relation="contains"), "predicate and direction"),
    (_decision(configuration_quote="依赖"), "predicate and direction"),
    (_decision(source_definition_quote="利润来自云上服务"), "exact complete definition record"),
    (_decision(target_definition_quote="成本是利润"), "exact complete definition record"),
])
def test_rejects_invented_predicate_direction_or_definition_quote(tmp_path, decision, reason):
    data, plan, concepts, alignments, spec = _input(tmp_path)
    try:
        candidate = _candidate(data, plan, concepts, alignments, spec)
        with pytest.raises(ValueError, match=reason):
            compile_configuration_relation(
                data, PROFILE, plan, candidate, decision, concepts, alignments)
        assert plan.relation_types == []
    finally:
        data.close()


def test_no_relation_text_or_scope_conflict_remains_unresolved(tmp_path):
    data, plan, concepts, alignments, spec = _input(tmp_path)
    try:
        no_text_spec = {**spec, "relation_text_column": None}
        candidate = _candidate(data, plan, concepts, alignments, no_text_spec)
        with pytest.raises(ValueError, match="no explicit relationship text"):
            compile_configuration_relation(
                data, PROFILE, plan, candidate, _decision(), concepts, alignments)
        candidate = _candidate(data, plan, concepts, alignments, spec)
        conflicting = plan.model_copy(deep=True)
        conflicting.object_types[0].applicability_scope = {"market": "east"}
        conflicting.object_types[1].applicability_scope = {"market": "south"}
        with pytest.raises(ValueError, match="conflicting applicability scopes"):
            compile_configuration_relation(
                data, PROFILE, conflicting, candidate, _decision(), concepts, alignments)
    finally:
        data.close()


def test_candidate_snapshot_and_witness_text_are_rechecked(tmp_path):
    data, plan, concepts, alignments, spec = _input(tmp_path)
    try:
        candidate = _candidate(data, plan, concepts, alignments, spec)
        with pytest.raises(ValueError, match="snapshot differs"):
            compile_configuration_relation(
                data, PROFILE, plan, {**candidate, "snapshot_id": "other"},
                _decision(), concepts, alignments)
        with pytest.raises(ValueError, match="text is absent, changed"):
            compile_configuration_relation(
                data, PROFILE, plan,
                {**candidate, "predicate_literal": "经营利润依赖成本"},
                _decision(), concepts, alignments)
        with pytest.raises(ValueError, match="Definition code is missing or ambiguous"):
            compile_configuration_relation(
                data, PROFILE, plan, {**candidate, "source_code": "I_OTHER"},
                _decision(), concepts, alignments)
    finally:
        data.close()


class _FixedLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def ask(self, stage, payload, model):
        self.calls.append((stage, payload))
        return model.model_validate(self.response)


def test_stage_caps_model_calls_and_reports_failed_judgements(tmp_path):
    data, plan, concepts, alignments, spec = _input(tmp_path)
    try:
        candidate = _candidate(data, plan, concepts, alignments, spec)
        llm = _FixedLLM(_decision().model_dump())
        zero = asyncio.run(adjudicate_configuration_relations(
            data, PROFILE, plan, [candidate], concepts, alignments, llm,
            max_candidates=0))
        assert llm.calls == []
        assert zero["coverage"]["not_attempted"] == 1
        result = asyncio.run(adjudicate_configuration_relations(
            data, PROFILE, plan, [candidate], concepts, alignments, llm,
            max_candidates=1))
        assert [call[0] for call in llm.calls] == ["configuration_relation"]
        assert result["coverage"]["accepted"] == 1
        assert result["coverage"]["partial"] is False
        assert len(result["assertions"]) == 1
        assert llm.calls[0][1]["configuration_witness"] == (
            candidate["configuration_record_id"])

        bad_llm = _FixedLLM(_decision(parent_relation="contains").model_dump())
        failed = asyncio.run(adjudicate_configuration_relations(
            data, PROFILE, plan, [candidate], concepts, alignments, bad_llm,
            max_candidates=1))
        assert failed["coverage"]["unresolved"] == 1
        assert failed["coverage"]["partial"] is True
        assert failed["plan"].relation_types == []
    finally:
        data.close()
