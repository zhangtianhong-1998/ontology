"""Build a local, bounded result viewer from the disk-backed extraction output."""
import json
import sqlite3
from pathlib import Path

from .storage import read_yaml


def render_viewer(run, max_nodes=200):
    run = Path(run).resolve()
    if not 10 <= max_nodes <= 1000:
        raise ValueError("max_nodes must be between 10 and 1000")

    def read(name, fallback):
        file = run / name
        return read_yaml(file) if file.exists() else fallback

    payload = {"run_name": run.name, "manifest": read("manifest.yaml", {}), "ontology": read("ontology.yaml", {}),
               "coverage": read("coverage.yaml", {}), "construction": read("construction.yaml", {}),
               "knowledge": read("knowledge.yaml", []), "validation": read("validation.yaml", {}), "limit": max_nodes,
               "objects": [], "relations": [], "unresolved": [], "evidence": {}, "counts": {}}
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
            payload["unresolved"] = [json.loads(r[0]) for r in db.execute("SELECT body FROM items WHERE kind='unresolved' ORDER BY id LIMIT ?", (max_nodes,))]
            refs = set()
            for item in payload["objects"] + payload["relations"] + payload["unresolved"] + payload["ontology"].get("object_types", []) + payload["ontology"].get("relation_types", []):
                refs.update(item.get("evidence_ids", []))
            for result in payload["knowledge"]:
                for claim in result.get("claims", []):
                    refs.add(claim["id"])
                    refs.update(claim.get("source_evidence_ids", []))
            for key in sorted(refs):
                row = db.execute("SELECT body FROM items WHERE kind='evidence' AND id=?", (key,)).fetchone()
                if row:
                    payload["evidence"][key] = json.loads(row[0])
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
