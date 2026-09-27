"""Local graph contracts: no service or model is used for these checks."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ontology_r2.datahub_adapter import dataset_urn, schema_field_urn
from ontology_r2.metadata_graph import MetadataGraph, build_metadata_graph, write_metadata_graph
from ontology_r2.storage import Dataset
from test_semantic_cards import _table


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "input"
    _table(root, "records", {"id": "记录编码", "ref": "关联编码", "kind": "引用类别"},
           [{"id": "1", "ref": "A", "kind": "metric"}], pk="id")
    _table(root, "definitions", {"code": "定义编码", "name": "定义名称"},
           [{"code": "A", "name": "收入"}], pk="code")
    work = tmp_path / "work"
    work.mkdir()
    dataset = Dataset(root, work)
    try:
        yield dataset
    finally:
        dataset.close()


def checked_rule(data, **updates):
    return {"rule_id": "same-upstream-id", "status": "checked_technical",
            "snapshot_id": data.snapshot_id,
            "source": {"table": "fruit.records", "field": "ref"},
            "target": {"table": "fruit.definitions", "field": "code"},
            "selector": {"kind": "metric"}, "transform": {"operator": "identity"},
            "scope_bindings": {}, "verification": {"scan_scope": "full_input"},
            "semantic_relation": "unresolved", **updates}


def test_graph_keeps_conditional_transforms_and_declarations_separate(data):
    data.tables["fruit.records"]["foreign_keys"] = [{
        "column_name": "ref", "referenced_schema": "fruit",
        "referenced_table": "definitions", "referenced_column": "code",
        "constraint_name": "records_ref_fkey"}]
    rules = [checked_rule(data), checked_rule(data, selector={"kind": "measure"},
                                             transform={"operator": "source_alias_items"})]
    graph = build_metadata_graph(data, {"rules": rules})
    index = MetadataGraph(graph)
    assert index.table("fruit.records")["datahub_urn"] == dataset_urn("fruit.records")
    assert index.columns("fruit.records")[1]["datahub_urn"] == schema_field_urn("fruit.records", "ref")
    links = index.field_links("fruit.records", "ref")
    assert {item["type"] for item in links} == {"declared_fk", "technical_link"}
    checked = [item for item in links if item["type"] == "technical_link"]
    assert len({item["id"] for item in checked}) == 2
    assert {item["selector"]["kind"] for item in checked} == {"metric", "measure"}
    assert {item["transform"]["operator"] for item in checked} == {"identity", "source_alias_items"}
    assert all(item["source"] == "fruit.records.ref" and item["target"] == "fruit.definitions.code"
               and item["lineage_inferred"] is False for item in checked)
    assert all(item["evidence_ids"] for item in checked)
    assert not any(edge["type"] == "lineage" for edge in graph["edges"])


def test_only_current_checked_rules_enter_query_neighborhood(data):
    rules = [checked_rule(data), checked_rule(data, status="candidate"),
             checked_rule(data, snapshot_id="old"),
             checked_rule(data, target={"table": "fruit.definitions", "field": "missing"})]
    graph = build_metadata_graph(data, rules)
    assert graph["association_coverage"] == {"input_rules": 4, "included_links": 1,
                                            "omitted": {"not_checked": 1, "different_snapshot": 1,
                                                        "unknown_field": 1}}
    index = MetadataGraph(graph)
    assert index.neighbors("fruit.records") == ["fruit.definitions"]
    assert index.neighbors("fruit.records", edge_types={"declared_fk"}) == []
    assert index.neighbors("fruit.records", max_hops=0) == []
    packet = index.packet(["fruit.records", "fruit.definitions"])
    assert packet["truncated"] is False
    assert not any(node["kind"] == "Source" for node in packet["nodes"])
    assert packet["evidence"] and packet["provenance"]
    assert any(edge["type"] == "technical_link" for edge in packet["edges"])
    packet["nodes"].clear()
    assert index.packet("fruit.records")["nodes"]
    assert any(node["kind"] == "Source" for node in index.packet("fruit.records", include_sources=True)["nodes"])
    with pytest.raises(ValueError, match="Unknown metadata"):
        index.packet("missing")


def test_write_exports_datahub_automatically_and_deterministically(data, tmp_path):
    output = tmp_path / "graph"
    graph, report = write_metadata_graph(data, output, [checked_rule(data)])
    assert (output / "meta_graph.yaml").is_file()
    first = (output / "datahub-metadata.json").read_bytes()
    assert report["datasets"] == 2
    proposals = json.loads(first)
    assert {item["aspectName"] for item in proposals} == {"datasetProperties", "schemaMetadata"}
    assert all("upstreamLineage" not in item for item in proposals)
    write_metadata_graph(data, output, [checked_rule(data)])
    assert (output / "datahub-metadata.json").read_bytes() == first
    assert build_metadata_graph(data, [checked_rule(data)]) == graph


def test_packet_field_projection_uses_local_index_and_keeps_provenance():
    from ontology_r2.instance_bundles import _metadata_context

    nodes, edges = [], []
    for number in range(3000):
        left, right = f's.a{number}', f's.b{number}'
        nodes.extend({'id': table, 'kind': 'Table', 'table_comment': table} for table in (left, right))
        edges.extend({'source': table, 'target': table + '.key', 'type': 'table_has_column'}
                     for table in (left, right))
        edges.append({'id': f'link:{number}', 'source': left + '.key', 'target': right + '.key',
                      'type': 'technical_link', 'status': 'checked_technical', 'snapshot_id': 'snapshot',
                      'rule_id': f'rule:{number}', 'candidate_id': f'candidate:{number}',
                      'selector': {'kind': 'API'}, 'scope_bindings': {'scope': 'region'},
                      'transform': {'operator': 'target_alias_items'}, 'evidence_ids': ['evidence:1'],
                      'verification': {'scan_scope': 'full_input', 'checks': {'unique_matches': 1},
                                       'counterexamples': []}})
    graph = MetadataGraph({'nodes': nodes, 'edges': edges, 'evidence': {'evidence:1': {
        'origin': 'declared_metadata', 'source_ref': {'file': 'schema/tables/a0.yaml', 'column': 'key'}}}})

    class IndexedEdges(list):
        lookups = 0

        def __getitem__(self, item):
            self.lookups += 1
            return super().__getitem__(item)

    class NeverCopy:
        def __deepcopy__(self, memo):
            raise AssertionError('Validation examples must not be copied into every prompt')

    class NeverScan(list):
        def __iter__(self):
            raise AssertionError('Per-packet metadata lookup must not scan the complete graph')

    graph._field_edges = IndexedEdges(graph._field_edges)
    graph._field_edges[0]['verification']['counterexamples'] = [NeverCopy(), NeverCopy()]
    graph._field_edges.lookups = 0
    graph.edges = NeverScan(graph.edges)
    records = [{'table': table, 'fields': {'reference': [{'column': 'key', 'value': 'A'}]}}
               for table in ('s.a0', 's.b0')]
    # An operation-count assertion is stable across machines, unlike a tight
    # wall-clock threshold: 100 packets inspect 100 edges, not 300,000 edges.
    for _ in range(100):
        packet = _metadata_context(SimpleNamespace(metadata_graph=graph), records)
        assert len(packet['field_links']) == 1
    assert graph._field_edges.lookups == 100
    edge = packet['field_links'][0]
    assert edge['rule_id'] == 'rule:0' and edge['candidate_id'] == 'candidate:0'
    assert edge['snapshot_id'] == 'snapshot'
    assert edge['verification'] == {'scan_scope': 'full_input', 'checks': {'unique_matches': 1},
                                    'counterexample_count': 2}
    assert edge['evidence_sources']['evidence:1']['source_ref']['file'] == 'schema/tables/a0.yaml'
    edge['selector']['kind'] = 'MUTATED'
    assert graph._field_edges[0]['selector']['kind'] == 'API'
    only_source = _metadata_context(SimpleNamespace(metadata_graph=graph), records[:1])
    assert only_source['field_links'] == []
