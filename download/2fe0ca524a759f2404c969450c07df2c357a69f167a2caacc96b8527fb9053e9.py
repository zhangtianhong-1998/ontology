"""Case-sensitive symbols stay distinct throughout type construction."""
from types import SimpleNamespace

import pytest

from ontology_r2.concept_candidates import _unit_values
from ontology_r2.group_incremental import ConceptBundleDecision, compile_concept
from ontology_r2.type_equivalence import compile_equivalence, propose_equivalence_candidates
from ontology_r2.type_generalization import compile_generalization, propose_generalization_candidates
from test_template_projection import PROFILE


def power_record(record_id, unit):
    return {"record_id": record_id, "table": "ops.power", "kind": "definition", "unit": unit,
            "fields": {"name": [{"column": "name", "value": "设备功率"}],
                       "description": [{"column": "definition", "value": "设备功率表示设备消耗电能的速率。"}],
                       "unit": [{"column": "unit", "value": unit}]}}


def power_decision(records):
    return ConceptBundleDecision(status="proposed", root_type="Metric", ontology_level="type",
        label="设备功率", definition="设备功率表示设备消耗电能的速率。",
        classification_basis="business_driven_metric", classification_quote="设备功率表示设备消耗电能的速率。",
        business_object_quote="设备", alignments=[
            {"record_id": record["record_id"], "mapping_kind": "exact", "quote": "设备功率"} for record in records])


def test_exact_compilation_retains_unit_case_and_rejects_mixed_units():
    records = [power_record("r1", "MW"), power_record("r2", "mW")]
    data = SimpleNamespace(snapshot_id="s", tables={}, evidence={})
    concepts = [compile_concept(data, PROFILE, {"records": [record]}, power_decision([record]), {})[0]
                for record in records]
    assert {item["unit"] for item in concepts} == {"MW", "mW"}
    assert len({item["ontology_type_id"] for item in concepts}) == 2
    assert _unit_values(records[0]).isdisjoint(_unit_values(records[1]))
    with pytest.raises(ValueError, match="Conflicting units"):
        compile_concept(data, PROFILE, {"records": records}, power_decision(records), {})


def test_equivalence_and_generalization_reject_case_distinct_units():
    from test_type_equivalence import _fixture as ef, _decision as ed
    from test_type_generalization import _fixture as gf, _decision as gd
    data, core = ef(units=("MW", "MW"))
    candidate = propose_equivalence_candidates(core, data, 1)[0]
    core.object_types[1].unit = "mW"
    assert propose_equivalence_candidates(core, data, 1) == []
    with pytest.raises(ValueError, match="same-unit"):
        compile_equivalence(data, core, candidate, ed(data, candidate))
    data, core = gf(units=("MW", "mW"))
    assert propose_generalization_candidates(core, data, 1) == []
    decision = gd()
    decision["unit"] = "MW"
    with pytest.raises(ValueError, match="units conflict"):
        compile_generalization(data, PROFILE, core, decision)
