"""Read selected RDF/CDM models into local reference cards, never enterprise facts."""
import json
import sqlite3
from pathlib import Path

from rdflib import Graph, OWL, RDF, RDFS, SKOS, URIRef

from .embedding import top_cosine
from .relations import terms
from .storage import file_hash


class EmbeddingCardLimitExceeded(ValueError):
    pass


def rdf_cards(path):
    graph = Graph().parse(path, format="turtle" if path.suffix.lower() == ".ttl" else "xml")
    resources = set()
    for kind in (OWL.Class, RDFS.Class, OWL.ObjectProperty, OWL.DatatypeProperty, RDF.Property):
        resources.update(graph.subjects(RDF.type, kind))
    for node in sorted(resources, key=str):
        if not str(node).startswith(("http://", "https://")):
            continue
        labels = list(graph.objects(node, RDFS.label)) + list(graph.objects(node, SKOS.prefLabel))
        definitions = list(graph.objects(node, RDFS.comment)) + list(graph.objects(node, SKOS.definition))
        yield {"uri": str(node), "label": str(labels[0]) if labels else str(node).split("#")[-1].split("/")[-1], "aliases": [str(x) for x in labels[1:]] + [str(x) for x in graph.objects(node, SKOS.altLabel)], "definition": "\n".join(map(str, definitions)), "source_languages": sorted({str(x.language) for x in labels + definitions if getattr(x, "language", None)}), "parents": [str(x) for x in graph.objects(node, RDFS.subClassOf) if isinstance(x, URIRef)], "supported_semantics": "labels_definitions_named_parents", "unsupported_semantics": "OWL axioms are retained only in the original source"}


def cdm_cards(path, base):
    raw = path.read_bytes()
    encoding = "utf-8-sig"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        encoding = "cp1252"
        text = raw.decode(encoding)
    content = json.loads(text)
    for definition in content.get("definitions", []):
        name = definition.get("entityName")
        if name:
            yield {"uri": "cdm:" + str(path.relative_to(base)) + "#" + name, "label": name, "definition": definition.get("description", ""), "parents": [definition.get("extendsEntity")] if definition.get("extendsEntity") else [], "source_encoding": encoding, "supported_semantics": "entity_name_description_parent_reference", "unsupported_semantics": "traits, attribute groups and imported constraints are not interpreted"}


class ExternalIndex:
    def __init__(self, config, work, embedding=None):
        self.db = sqlite3.connect(Path(work) / "external.sqlite")
        self.db.execute("CREATE VIRTUAL TABLE cards USING fts5(words, body UNINDEXED)")
        self.embedding, self.cards, self.vectors, self.vector_norms = embedding, [], [], None
        self.report = {"files": 0, "cards": 0, "errors": [], "complete": True, "sources": []}
        for source in config.get("sources", []):
            path = Path(source["path"])
            files = sorted(path.rglob("*.cdm.json")) if path.is_dir() else [path]
            maximum = source.get("max_files")
            if maximum is not None and len(files) > maximum:
                self.report["complete"] = False
                files = files[:maximum]
            for f in files:
                try:
                    source_cards = cdm_cards(f, path if path.is_dir() else path.parent) if source["format"] == "cdm_json" else rdf_cards(f)
                    cards = source_cards
                    if embedding is not None:
                        cards = []
                        for card in source_cards:
                            cards.append(card)
                            if len(self.cards) + len(cards) > embedding.config["max_cards"]:
                                raise EmbeddingCardLimitExceeded("External ontology cards exceed embedding.max_cards")
                    sha = file_hash(f)
                    self.report["sources"].append({"path": str(f), "sha256": sha})
                    for card in cards:
                        card.update(source=source["id"], file=str(f), file_sha256=sha)
                        self.db.execute("INSERT INTO cards VALUES (?,?)", (" ".join(terms(card["label"] + " " + card["definition"] + " " + " ".join(card.get("aliases", [])))), json.dumps(card, ensure_ascii=False)))
                        if embedding is not None:
                            self.cards.append(card)
                        self.report["cards"] += 1
                    self.report["files"] += 1
                except EmbeddingCardLimitExceeded:
                    self.db.close()
                    raise
                except Exception as exc:
                    self.report["complete"] = False
                    self.report["errors"].append({"path": str(f), "error": type(exc).__name__})
        self.db.commit()
        if embedding is not None:
            texts = [card["label"] + "\n" + card["definition"] for card in self.cards]
            self.vectors = embedding.documents(texts, cache=False)
            if len(self.vectors):
                import numpy as np
                self.vector_norms = np.linalg.norm(self.vectors, axis=1)
            self.report["embedding"] = {"mode": "hybrid_fts_cosine", "cards_indexed": len(self.cards),
                                        "model_sha256": embedding.model_sha256,
                                        "min_cosine_similarity": embedding.config["min_cosine_similarity"]}

    def search(self, text, limit=5):
        query_terms = terms(text)[:20]
        if not query_terms:
            return []
        query = " OR ".join('"' + t.replace('"', '""') + '"' for t in query_terms)
        return [json.loads(row[0]) for row in self.db.execute("SELECT body FROM cards WHERE words MATCH ? ORDER BY rank LIMIT ?", (query, limit))]

    def search_many(self, queries, limit=5):
        scores, cards, channels, similarities = {}, {}, {}, {}
        for query in dict.fromkeys(queries):
            for rank, card in enumerate(self.search(query, limit), 1):
                key = card["uri"]
                cards[key] = card
                scores[key] = scores.get(key, 0) + 1 / (60 + rank)
                channels.setdefault(key, set()).add("fts5")
            if self.embedding is not None and query.strip() and len(self.vectors):
                for rank, (index, similarity) in enumerate(
                        top_cosine(self.vectors, self.embedding.query(query), limit, self.vector_norms), 1):
                    if similarity < self.embedding.config["min_cosine_similarity"]:
                        continue
                    card = self.cards[index]
                    key = card["uri"]
                    cards[key] = card
                    scores[key] = scores.get(key, 0) + 1 / (60 + rank)
                    channels.setdefault(key, set()).add("cosine")
                    similarities[key] = max(similarity, similarities.get(key, -1.0))
        chosen = sorted(cards, key=lambda key: (-scores[key], key))[:limit]
        if self.embedding is None:
            return [cards[key] for key in chosen]
        return [{**cards[key], "retrieval": {"channels": sorted(channels[key]),
                 "rrf_score": scores[key], "cosine_similarity": similarities.get(key),
                 "model_sha256": self.embedding.model_sha256}} for key in chosen]

    def close(self):
        self.db.close()
