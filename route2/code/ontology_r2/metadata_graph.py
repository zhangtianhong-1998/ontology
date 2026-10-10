"""Local source metadata and checked field links, with offline DataHub identities.

The graph is a queryable snapshot, not a DataHub server or a lineage inference.
Physical declarations and checked associations retain distinct edge types. Query
packets can feed an evidence bundle without treating a technical match as a
business predicate.
"""

from collections import defaultdict, deque
from copy import deepcopy
from pathlib import Path

from .datahub_adapter import dataset_urn, schema_field_urn
from .record_types import source_record_type
from .storage import digest, write_yaml


CHECKED_LINK_STATUSES = frozenset(("checked_technical", "observed_subset"))
FIELD_EDGE_TYPES = frozenset(("declared_fk", "technical_link"))


def technical_link_id(rule):
    """Conditions and transforms are part of a field link's local identity."""
    return "technical_link:" + digest([
        rule.get("rule_id"), rule.get("source"), rule.get("target"),
        rule.get("selector") or {}, rule.get("scope_bindings") or {},
        rule.get("transform") or {},
    ])[:24]


def build_metadata_graph(data, association=None, *, platform="postgres", environment="PROD"):
    """Build a deterministic graph from Dataset plus already checked rules.

    ``association`` is a ``{"rules": [...]}`` result or a rule list. This
    function does not perform a join or judge meaning. Rejected/candidate rules
    and rules from another snapshot are recorded in coverage, never as edges.
    Existing local table/column IDs are preserved for extraction compatibility;
    their ``datahub_urn`` values supply stable interchange identities.
    """
    # Validate the platform before constructing a partial graph.
    dataset_urn("metadata_identity_check", platform, environment)
    nodes, edges = {}, []

    def add_node(item):
        if item["id"] in nodes and nodes[item["id"]] != item:
            raise ValueError("Conflicting metadata node identity: " + item["id"])
        nodes[item["id"]] = item

    def source_edge(subject, kind, path):
        if path in data.files:
            edges.append({"source": subject, "type": kind, "target": "source:" + path})

    snapshot_node = "snapshot:" + data.snapshot_id
    add_node({"id": snapshot_node, "kind": "DatasetSnapshot", "snapshot_id": data.snapshot_id})
    for path, sha in sorted(data.files.items()):
        add_node({"id": "source:" + path, "kind": "Source", "path": path, "sha256": sha})
        edges.append({"source": snapshot_node, "type": "includes_source", "target": "source:" + path})
    for name, table in sorted(data.tables.items()):
        metadata_file = data.evidence["schema:" + name]["source_ref"]["file"]
        record_type, classification = source_record_type(name, table)
        try:
            urn = dataset_urn(name, platform, environment)
        except ValueError:
            urn = None
        add_node({"id": name, "kind": "Table",
                  **{key: table.get(key) for key in ("schema", "table_name", "table_comment", "relkind",
                                                     "estimated_rows", "total_size", "data_size")},
                  "observed_rows": table["rows"], "declared_primary_key": list(table["pk"]),
                  "source_record_type": record_type.id, "datahub_urn": urn,
                  "datahub_urn_scope": "offline_interchange" if urn else "unsupported_local_identifier",
                  "evidence_ids": ["schema:" + name]})
        add_node({"id": record_type.id, "kind": "SourceRecordType", "category": record_type.category,
                  "label": record_type.label, "definition": record_type.definition,
                  "parent": record_type.parent, "evidence_ids": record_type.evidence_ids,
                  "classification_basis": classification["basis"], "business_concept_inferred": False,
                  "observed_rows": table["rows"], "mapping_origin": "local_deterministic_source_structure"})
        edges.append({"source": name, "type": "mapped_as_source_record_type", "target": record_type.id,
                      "semantic_status": "source_structure_only"})
        source_edge(name, "documented_by", metadata_file)
        source_edge(name, "sample_from", str(Path(table["csv_path"]).relative_to(data.root)))
        for constraint in table["constraints"]:
            cid = name + ":constraint:" + digest([constraint.get("constraint_name"),
                                                    constraint.get("constraint_type"),
                                                    constraint.get("definition")])[:16]
            add_node({"id": cid, "kind": "Constraint", **deepcopy(constraint)})
            edges.append({"source": name, "type": "has_declared_constraint", "target": cid})
            source_edge(cid, "documented_by", metadata_file.replace("schema/tables/", "schema/constraints/", 1))
            if (constraint.get("constraint_type") == "p" or
                    str(constraint.get("constraint_type_name", "")).upper() == "PRIMARY KEY"):
                for position, column in enumerate(table["pk"], start=1):
                    edges.append({"source": cid, "type": "declared_key_column", "target": name + "." + column,
                                  "key_position": position})
        for column in table["columns"]:
            field = column["column_name"]
            try:
                field_urn = schema_field_urn(name, field, platform, environment)
            except ValueError:
                field_urn = None
            cid = name + "." + field
            add_node({"id": cid, "kind": "Column", **deepcopy(column), "table": name,
                      "datahub_urn": field_urn, "datahub_field_path": field,
                      "is_declared_primary_key": field in table["pk"],
                      "evidence_ids": ["schema:" + name + ":" + field]})
            edges.append({"source": name, "type": "table_has_column", "target": cid})
        for fk in table["foreign_keys"]:
            source = name + "." + fk["column_name"]
            target = fk["referenced_schema"] + "." + fk["referenced_table"] + "." + fk["referenced_column"]
            edges.append({"id": "declared_fk:" + digest([source, target, fk])[:24],
                          "source": source, "type": "declared_fk", "target": target,
                          "raw": deepcopy(fk), "status": "declared",
                          "meaning": "declared_foreign_key_only", "lineage_inferred": False,
                          "source_ref": {"file": metadata_file.replace("schema/tables/", "schema/foreign_keys/", 1)}})

    rules = association.get("rules", []) if isinstance(association, dict) else association or []
    omitted = defaultdict(int)
    link_ids = set()
    for rule in rules:
        if rule.get("status") not in CHECKED_LINK_STATUSES:
            omitted["not_checked"] += 1
            continue
        if rule.get("snapshot_id") not in (None, data.snapshot_id):
            omitted["different_snapshot"] += 1
            continue
        source, target = rule.get("source") or {}, rule.get("target") or {}
        endpoints = [str(value.get("table", "")) + "." + str(value.get("field", ""))
                     for value in (source, target)]
        if any(nodes.get(endpoint, {}).get("kind") != "Column" for endpoint in endpoints):
            omitted["unknown_field"] += 1
            continue
        link_id = technical_link_id(rule)
        if link_id in link_ids:
            omitted["duplicate_link"] += 1
            continue
        link_ids.add(link_id)
        evidence_ids = list(dict.fromkeys(rule.get("evidence_ids", []) + [
            "schema:" + value["table"] + ":" + value["field"] for value in (source, target)]))
        edges.append({"id": link_id, "type": "technical_link", "source": endpoints[0], "target": endpoints[1],
                      "rule_id": rule.get("rule_id"), "candidate_id": rule.get("candidate_id"),
                      "status": rule["status"], "snapshot_id": data.snapshot_id,
                      **{key: deepcopy(rule.get(key) or {}) for key in
                         ("selector", "scope_bindings", "transform", "verification")},
                      "evidence_ids": evidence_ids,
                      "numeric_overlap_only": bool(rule.get("numeric_overlap_only", False)),
                      "risk_flags": deepcopy(rule.get("risk_flags") or []),
                      "semantic_relation": rule.get("semantic_relation", "unresolved"),
                      "meaning": "checked_field_association_only", "lineage_inferred": False})
    for edge in edges:
        for endpoint in (edge["source"], edge["target"]):
            if endpoint not in nodes:
                add_node({"id": endpoint, "kind": "ExternalColumnReference", "availability": "not_in_input"})
    evidence_ids = {eid for item in [*nodes.values(), *edges] for eid in item.get("evidence_ids", [])}
    return {"schema_version": "0.2", "snapshot_id": data.snapshot_id,
            "nodes": list(nodes.values()), "edges": edges,
            "evidence": {eid: deepcopy(data.evidence[eid]) for eid in sorted(evidence_ids) if eid in data.evidence},
            "graph_source": "local_schema_and_csv_snapshot", "lineage_source_available": False,
            "association_coverage": {"input_rules": len(rules), "included_links": len(link_ids),
                                     "omitted": dict(omitted)},
            "datahub_interchange": {"format": "metadata-file", "default_platform": platform,
                                    "default_environment": environment, "service_backed": False}}


def write_metadata_graph(data, output, association=None, *, platform="postgres", environment="PROD"):
    """Persist the local graph and automatically export offline DataHub MCPs."""
    from .datahub_adapter import export_datahub_metadata

    output = Path(output)
    graph = build_metadata_graph(data, association, platform=platform, environment=environment)
    write_yaml(output / "meta_graph.yaml", graph)
    report = export_datahub_metadata(graph, output / "datahub-metadata.json", platform, environment)
    return graph, report


class MetadataGraph:
    """Read-only query index for source neighborhoods; returns detached values."""

    def __init__(self, graph):
        self.graph = deepcopy(graph)
        self.nodes = {node["id"]: node for node in self.graph.get("nodes", [])}
        self.edges = self.graph.get("edges", [])
        self.owners = {edge["target"]: edge["source"] for edge in self.edges
                       if edge.get("type") == "table_has_column"}
        self._field_edges = [edge for edge in self.edges if edge.get("type") in FIELD_EDGE_TYPES]
        self._field_edge_positions = defaultdict(list)
        for position, edge in enumerate(self._field_edges):
            for owner in {self.owners.get(edge["source"]), self.owners.get(edge["target"])} - {None}:
                self._field_edge_positions[owner].append(position)

    def table(self, name):
        item = self.nodes.get(name)
        return deepcopy(item) if item and item.get("kind") == "Table" else None

    def columns(self, table):
        return deepcopy(sorted((self.nodes[column] for column, owner in self.owners.items() if owner == table),
                               key=lambda item: (item.get("ordinal_position") or 0, item["id"])))

    def field_links(self, table=None, column=None, *, statuses=None, edge_types=None):
        """Return directed field edges, retaining every selector/transform branch."""
        kinds = FIELD_EDGE_TYPES if edge_types is None else set(edge_types)
        ids = ({table + "." + column} if table and column else
               {key for key, owner in self.owners.items() if owner == table} if table else None)
        return deepcopy([edge for edge in self.edges if edge.get("type") in kinds
                         and (ids is None or edge["source"] in ids or edge["target"] in ids)
                         and (statuses is None or edge.get("status") in statuses)])

    def field_link_context(self, tables, fields, *, statuses=None, rule_id=None,
                           with_coverage=False):
        """Project selected links before copying; keep audit references and counts.

        Table adjacency is indexed once. A prompt never copies unrelated links
        or potentially large counterexample arrays from full validation reports.
        Link/rule IDs and snapshot IDs locate those complete reports on disk.
        """
        positions = {position for table in tables for position in self._field_edge_positions.get(table, ())}
        selected, available = [], 0
        keys = ("id", "rule_id", "candidate_id", "snapshot_id", "source", "target", "type", "status",
                "selector", "scope_bindings", "transform", "evidence_ids", "source_ref",
                "numeric_overlap_only", "risk_flags", "semantic_relation", "meaning", "lineage_inferred")
        for position in sorted(positions):
            edge = self._field_edges[position]
            if (edge["source"] not in fields or edge["target"] not in fields
                    or (statuses is not None and edge.get("status") not in statuses)):
                continue
            available += 1
            # A relation packet judges one rule, not every conditional branch
            # sharing its fields. Filter before copying validation evidence.
            if rule_id is not None and edge.get("rule_id") != rule_id:
                continue
            item = {key: deepcopy(edge[key]) for key in keys if key in edge}
            verification = edge.get("verification") or {}
            item["verification"] = {key: deepcopy(verification[key]) for key in
                                    ("scan_scope", "checks", "error") if key in verification}
            item["verification"]["counterexample_count"] = len(verification.get("counterexamples") or [])
            item["evidence_sources"] = {eid: {key: deepcopy(evidence[key]) for key in ("origin", "source_ref")
                                              if key in evidence}
                                        for eid in item.get("evidence_ids", [])
                                        if (evidence := self.graph.get("evidence", {}).get(eid)) is not None}
            selected.append(item)
        if with_coverage:
            return selected, {"available_links": available, "included_links": len(selected),
                              "omitted_links": available - len(selected)}
        return selected

    def neighbors(self, table, *, edge_types=None, max_hops=1, statuses=None):
        """Find adjacent input table IDs; declarations and checked links are separate filters."""
        if type(max_hops) is not int or max_hops < 0:
            raise ValueError("max_hops must be a nonnegative integer")
        if self.table(table) is None:
            raise ValueError("Unknown metadata table: " + table)
        adjacent = defaultdict(set)
        for edge in self.field_links(statuses=statuses, edge_types=edge_types):
            source, target = self.owners.get(edge["source"]), self.owners.get(edge["target"])
            if source and target:
                adjacent[source].add(target)
                adjacent[target].add(source)
        seen, queue = {table}, deque([(table, 0)])
        while queue:
            current, depth = queue.popleft()
            if depth == max_hops:
                continue
            for neighbor in sorted(adjacent[current] - seen):
                seen.add(neighbor)
                queue.append((neighbor, depth + 1))
        return sorted(seen - {table})

    def packet(self, table_names, *, include_sources=False):
        """Return the complete selected-table schema/links/evidence, without CSV values.

        No internal truncation is applied. The bundle builder must budget or
        split this packet explicitly. Source files stay in provenance unless
        ``include_sources`` is requested, avoiding a graph dominated by files.
        """
        if isinstance(table_names, str):
            table_names = [table_names]
        selected = set(table_names)
        unknown = sorted(name for name in selected if self.table(name) is None)
        if unknown:
            raise ValueError("Unknown metadata tables: " + ", ".join(unknown))
        ids = set(selected)
        for edge in self.edges:
            if edge["source"] in selected and edge.get("type") in (
                    "table_has_column", "has_declared_constraint", "mapped_as_source_record_type"):
                ids.add(edge["target"])
        provenance = [deepcopy(edge) for edge in self.edges if edge["source"] in ids
                      and edge.get("type") in ("documented_by", "sample_from")]
        if include_sources:
            ids.update(edge["target"] for edge in provenance)
        edges = [deepcopy(edge) for edge in self.edges if edge["source"] in ids and edge["target"] in ids]
        nodes = [deepcopy(self.nodes[node_id]) for node_id in sorted(ids)]
        evidence_ids = {eid for item in nodes + edges for eid in item.get("evidence_ids", [])}
        return {"snapshot_id": self.graph.get("snapshot_id"), "tables": sorted(selected),
                "nodes": nodes, "edges": edges, "provenance": provenance,
                "evidence": {eid: deepcopy(self.graph["evidence"][eid]) for eid in sorted(evidence_ids)
                             if eid in self.graph.get("evidence", {})},
                "scope": "selected_source_metadata_and_checked_field_links", "truncated": False}
