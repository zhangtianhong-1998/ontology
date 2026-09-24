"""Build a local, bounded result viewer from the disk-backed extraction output."""
import json
import sqlite3
from pathlib import Path

from .storage import read_yaml


MAX_ATTRIBUTES_PER_OBJECT = 16
MAX_ATTRIBUTE_CHARS = 320
MAX_SOURCE_REFS_PER_CONCEPT = 24
MAX_EVIDENCE_PER_CONCEPT = 24
SQLITE_PARAMETERS_PER_QUERY = 400


def _attribute_rank(column):
    name = column.casefold()
    if any(part in name for part in ("name", "title", "description", "definition", "comment", "meaning", "formula", "label", "名称", "说明", "定义", "口径")):
        return 0
    if name in ("id", "code", "key", "sn", "number", "uuid") or name.endswith(("_id", "_code", "_no", "_number")):
        return 1
    if any(part in name for part in ("time", "date", "created", "updated", "modified", "时间", "日期")):
        return 2
    return 3


def _attach_attribute_previews(db, nodes, ontology):
    """Scan literal assertions once; retain only a small, useful card per shown node."""
    if not nodes:
        return
    by_id = {node["id"]: node for node in nodes}
    catalog = {attr["id"]: attr for attr in ontology.get("attributes", [])}
    ids = list(by_id)
    for start in range(0, len(ids), SQLITE_PARAMETERS_PER_QUERY):
        batch = ids[start:start + SQLITE_PARAMETERS_PER_QUERY]
        placeholders = ",".join("?" for _ in batch)
        cursor = db.execute(
            "SELECT json_extract(body,'$.subject'), json_extract(body,'$.attribute'), "
            "json_extract(body,'$.literal.value') FROM items "
            "WHERE kind='assertions' AND json_type(body,'$.literal') IS NOT NULL "
            f"AND json_extract(body,'$.subject') IN ({placeholders})",
            batch,
        )
        for subject, attribute, raw_value in cursor:
            node = by_id[subject]
            node["preview_attribute_count"] = node.get("preview_attribute_count", 0) + 1
            meta = catalog.get(attribute, {})
            column = meta.get("source_column") or attribute.rsplit(":", 1)[-1].rsplit(".", 1)[-1]
            value = str(raw_value) if raw_value is not None else ""
            preview = {"column": column, "value": value[:MAX_ATTRIBUTE_CHARS],
                       "truncated": len(value) > MAX_ATTRIBUTE_CHARS}
            if meta.get("literal_type"):
                preview["literal_type"] = meta["literal_type"]
            if meta.get("declared_data_type"):
                preview["declared_data_type"] = meta["declared_data_type"]
            selected = node.setdefault("preview_attributes", [])
            if any(item["column"] == column and item["value"] == preview["value"] for item in selected):
                continue
            selected.append(preview)
            selected.sort(key=lambda item: (_attribute_rank(item["column"]), item["column"], item["value"]))
            del selected[MAX_ATTRIBUTES_PER_OBJECT:]


def _bound_concept_refs(item):
    refs = item.get("source_refs", [])
    item["preview_source_ref_count"] = len(refs)
    item["source_refs"] = refs[:MAX_SOURCE_REFS_PER_CONCEPT]
    evidence = item.get("evidence_ids", [])
    item["preview_evidence_count"] = len(evidence)
    item["evidence_ids"] = evidence[:MAX_EVIDENCE_PER_CONCEPT]


def _preview_summary(payload, has_results):
    """Keep unavailable stages distinct from measured zeroes in the viewer."""
    manifest = payload["manifest"]
    counts = payload["counts"]
    group_replay = manifest.get("experimental_scope") == "focused_group_replay_only"
    count = lambda kind: counts.get(kind, 0) if has_results else None
    metrics = [
        {"label": "输入快照记录", "value": manifest.get("input_records")},
        {"label": "已抽取对象", "value": count("objects")},
        {"label": "业务概念", "value": payload["concept_count"]},
        {"label": "记录对齐", "value": count("record_alignments")},
        {"label": "已接受对象关系", "value": payload["relation_count"]},
        {"label": "映射字段", "value": None if group_replay else payload["construction"].get("direct_mapping_columns")},
        {"label": "未决项", "value": count("unresolved")},
        {"label": "本次模型调用", "value": manifest.get("llm", {}).get("calls")},
    ]
    notice = None
    if group_replay:
        groups = len(payload["construction"].get("group_steps", []))
        hits = manifest.get("llm", {}).get("cache_hits")
        notice = (f"聚焦语义组重放：仅展示本次执行的 {groups} 个语义组；输入记录为快照规模。"
                  "映射阶段未运行，— 表示未执行或无统计，0 表示本次确为零。")
        if hits is not None:
            notice += f" 模型缓存命中 {hits} 次。"
    return {"metrics": metrics, "notice": notice}


def render_viewer(run, max_nodes=200, *, manifest_override=None):
    run = Path(run).resolve()
    if not 10 <= max_nodes <= 1000:
        raise ValueError("max_nodes must be between 10 and 1000")

    def read(name, fallback):
        file = run / name
        return read_yaml(file) if file.exists() else fallback

    payload = {"run_name": run.name, "manifest": manifest_override if manifest_override is not None else read("manifest.yaml", {}),
               "ontology": read("ontology.yaml", {}),
               "coverage": read("coverage.yaml", {}), "construction": read("construction.yaml", {}),
               "knowledge": read("knowledge.yaml", []), "validation": read("validation.yaml", {}), "limit": max_nodes,
               "objects": [], "concepts": [], "concept_count": None,
               "record_alignments": [], "relations": [], "relation_count": None,
               "unresolved": [], "evidence": {}, "counts": {}}
    database = run / "work/results.sqlite"
    if database.exists():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            payload["counts"] = dict(db.execute("SELECT kind,count(*) FROM items GROUP BY kind"))
            payload["relation_count"] = db.execute("SELECT count(*) FROM items WHERE kind='assertions' AND json_extract(body,'$.object') IS NOT NULL").fetchone()[0]
            nodes = {}
            for (raw,) in db.execute("SELECT body FROM items WHERE kind='assertions' AND json_extract(body,'$.object') IS NOT NULL ORDER BY id LIMIT ?", (max_nodes * 2,)):
                edge = json.loads(raw)
                needed = {edge["subject"], edge["object"]} - nodes.keys()
                if len(nodes) + len(needed) > max_nodes:
                    continue
                for key in needed:
                    row = db.execute("SELECT body FROM items WHERE kind='objects' AND id=?", (key,)).fetchone()
                    if row:
                        nodes[key] = json.loads(row[0])
                if edge["subject"] in nodes and edge["object"] in nodes:
                    payload["relations"].append(edge)
            for (raw,) in db.execute("SELECT body FROM items WHERE kind='objects' ORDER BY id LIMIT ?", (max_nodes,)):
                item = json.loads(raw)
                if len(nodes) < max_nodes:
                    nodes.setdefault(item["id"], item)
            payload["objects"] = list(nodes.values())
            _attach_attribute_previews(db, payload["objects"], payload["ontology"])
            for item in payload["objects"]:
                if item["id"].startswith("concept:"):
                    _bound_concept_refs(item)
            payload["concept_count"] = db.execute(
                "SELECT count(*) FROM items WHERE kind='objects' AND id LIKE 'concept:%'").fetchone()[0]
            payload["concepts"] = [json.loads(r[0]) for r in db.execute(
                "SELECT body FROM items WHERE kind='objects' AND id LIKE 'concept:%' "
                "ORDER BY id LIMIT ?", (max_nodes,))]
            for concept in payload["concepts"]:
                _bound_concept_refs(concept)
            payload["record_alignments"] = [json.loads(r[0]) for r in db.execute(
                "SELECT body FROM items WHERE kind='record_alignments' ORDER BY id LIMIT ?",
                (max_nodes,))]
            payload["unresolved"] = [json.loads(r[0]) for r in db.execute("SELECT body FROM items WHERE kind='unresolved' ORDER BY id LIMIT ?", (max_nodes,))]
            refs = set()
            for item in (payload["objects"] + payload["concepts"] + payload["record_alignments"]
                         + payload["relations"] + payload["unresolved"]
                         + payload["ontology"].get("object_types", [])
                         + payload["ontology"].get("relation_types", [])):
                refs.update(item.get("evidence_ids", []))
            for result in payload["knowledge"]:
                for claim in result.get("claims", []):
                    refs.add(claim["id"])
                    refs.update(claim.get("source_evidence_ids", []))
            for key in sorted(refs):
                row = db.execute("SELECT body FROM items WHERE kind='evidence' AND id=?", (key,)).fetchone()
                if row:
                    payload["evidence"][key] = json.loads(row[0])
    payload["preview"] = _preview_summary(payload, database.exists())
    graph = read("meta_graph.yaml", {"nodes": [], "edges": []})
    ids = {n["id"] for n in graph["nodes"][:max_nodes]}
    payload["metadata"] = {"nodes": graph["nodes"][:max_nodes], "edges": [e for e in graph["edges"] if e["source"] in ids and e["target"] in ids][:max_nodes * 2], "total_nodes": len(graph["nodes"])}
    # Audit logs remain on disk. The page contains only bounded graph data and summaries.
    payload["knowledge"] = [{k: v for k, v in item.items() if k != "documents"} for item in payload["knowledge"]]
    payload["construction"] = {**payload["construction"], "steps": [{k: v for k, v in s.items() if k != "attempts"} | {"attempts": [{k: v for k, v in a.items() if k not in ("delta", "accepted_delta")} for a in s["attempts"]]} for s in payload["construction"].get("steps", [])]}
    encoded = json.dumps(payload, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    page = Path(__file__).with_name("viewer.html").read_text(encoding="utf-8").replace("__RESULT_DATA__", encoded)
    target = run / "viewer.html"
    target.write_text(page, encoding="utf-8")
    return target
