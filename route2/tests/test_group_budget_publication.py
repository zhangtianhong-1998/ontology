"""A mid-batch budget stop must publish accepted work and preserve paid proposals."""
import asyncio
import json
from pathlib import Path

from ontology_r2.llm import StructuredLLM
from ontology_r2.pipeline import build
from ontology_r2.storage import read_yaml
from test_pipeline import setup
from test_semantic_cards import _table


def test_group_budget_stop_keeps_accepted_ontology_checkpoint_and_html(tmp_path, monkeypatch):
    config = setup(tmp_path, scenario="unrelated", mcp=False, rows=2)
    names = ["苹果销售收入", "香蕉销售收入", "橙子销售收入", "葡萄销售收入"]
    _table(Path(config["dataset"]), "metric_definition", {
        "id": "记录 ID", "name": "指标名称", "definition": "业务指标定义", "unit": "单位",
    }, [{"id": str(i), "name": name, "definition": name + "表示该水果销售业务确认的收入。", "unit": "元"}
        for i, name in enumerate(names, 1)])
    config.update(incremental={"mode": "source_mapping_only"}, discovery={"enabled": False},
                  progress={"enabled": False}, visualization={"enabled": True},
                  instance_bundles={"enabled": True, "max_concept_bundles": 4,
                                    "max_relation_bundles": 0, "max_llm_bundles": 4,
                                    "concept_batch_size": 2, "review": True})
    # Batch one proposal/review accepts two types. Batch two's proposal is
    # paid for, but its review exceeds the same real StructuredLLM budget.
    config["llm"]["max_calls"] = 3
    original_ask = StructuredLLM.ask

    async def fixture_response(self, task, payload, schema):
        assert self.mode == "mock"
        if task == "concept_batch":
            decisions = []
            for packet in payload["packets"]:
                record = packet["bundle"]["records"][0]
                name = record["fields"]["name"][0]["value"]
                definition = record["fields"]["description"][0]["value"]
                decisions.append({"bundle_id": packet["bundle"]["bundle_id"], "decision": {
                    "status": "proposed", "label": name, "definition": definition,
                    "root_type": "Metric", "ontology_level": "type",
                    "classification_basis": "business_driven_metric", "classification_quote": definition,
                    "business_object_quote": name[:2], "aggregation_operator": None,
                    "alignments": [{"record_id": record["record_id"], "mapping_kind": "exact", "quote": name}],
                }})
            self.responses[task] = {"decisions": decisions}
        elif task == "group_review_batch":
            self.responses[task] = {"reviews": [
                {"bundle_id": packet["bundle"]["bundle_id"], "review": {"accepted": True}}
                for packet in payload["packets"]]}
        else:
            raise AssertionError(task)
        return await original_ask(self, task, payload, schema)

    monkeypatch.setattr(StructuredLLM, "ask", fixture_response)
    output = tmp_path / "run"
    manifest = asyncio.run(build(config, output))
    assert manifest["status"] == "partial", manifest
    assert manifest["llm"]["calls"] == 3
    assert "group_incremental_partial" in manifest["partial_reasons"]
    steps = read_yaml(output / "group_steps.yaml")
    assert [step["status"] for step in steps].count("accepted") == 2
    assert [step["status"] for step in steps].count("budget_exhausted") == 1
    accepted_ids = {step["object_type_id"] for step in steps if step["status"] == "accepted"}
    state = json.loads((output / "semantic_state.json").read_text())
    assert len(state["pending_concept_decisions"]) == 2
    assert accepted_ids <= {item["id"] for item in state["plan"]["object_types"]}
    for file in ("extraction_plan.yaml", "ontology.yaml"):
        business_ids = {item["id"] for item in read_yaml(output / file)["object_types"]
                        if item.get("category") == "business_type"}
        assert business_ids == accepted_ids
    assert manifest["viewer"] == "viewer.html" and "viewer_error" not in manifest
    page = json.loads((output / "viewer.html").read_text().split(
        '<script id="result-data" type="application/json">', 1)[1].split("</script>", 1)[0])
    assert page["manifest"]["status"] == "partial"
    assert accepted_ids <= {item["id"] for item in page["ontology"]["object_types"]}
    events = [json.loads(line) for line in (output / "trace.jsonl").read_text().splitlines()]
    assert [event["task"] for event in events if event["stage"] == "llm_request"] == [
        "concept_batch", "group_review_batch", "concept_batch"]
