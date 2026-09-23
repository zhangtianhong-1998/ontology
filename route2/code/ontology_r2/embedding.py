"""Optional, offline-only sentence embeddings for bounded reference retrieval."""

import hashlib
import os
from pathlib import Path


def settings(config):
    raw = os.getenv("ONTOLOGY_EMBEDDING_ENABLED", "").strip().lower()
    if raw and raw not in ("true", "false", "1", "0"):
        raise ValueError("ONTOLOGY_EMBEDDING_ENABLED must be true or false")
    enabled = raw in ("true", "1") if raw else config.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("embedding.enabled must be a boolean")
    result = {"enabled": enabled,
              "model_path": os.getenv("ONTOLOGY_EMBEDDING_MODEL_PATH", "").strip() or config.get("model_path"),
              "batch_size": config.get("batch_size", 16),
              "max_cards": config.get("max_cards", 20000),
              "core_top_k": config.get("core_top_k", 5),
              "min_cosine_similarity": config.get("min_cosine_similarity", 0.35),
              "query_prompt": config.get("query_prompt")}
    for key in ("batch_size", "max_cards", "core_top_k"):
        if not isinstance(result[key], int) or result[key] < 1:
            raise ValueError(f"embedding.{key} must be a positive integer")
    threshold = result["min_cosine_similarity"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not -1 <= threshold <= 1:
        raise ValueError("embedding.min_cosine_similarity must be between -1 and 1")
    if enabled:
        if not result["model_path"]:
            raise ValueError("Embedding enabled but ONTOLOGY_EMBEDDING_MODEL_PATH is empty")
        result["model_path"] = str(Path(result["model_path"]).expanduser().resolve())
        if not Path(result["model_path"]).is_dir():
            raise FileNotFoundError("Embedding model directory not found: " + result["model_path"])
    return result


def model_digest(directory):
    """Hash local weights, tokenizer and model configuration without loading them in RAM."""
    root = Path(directory)
    files = sorted(p for p in root.rglob("*") if p.is_file() and
                   not any(part.startswith(".") for part in p.relative_to(root).parts) and
                   p.suffix in (".safetensors", ".bin", ".model", ".json", ".txt"))
    if not files or not any(p.suffix in (".safetensors", ".bin") for p in files):
        raise ValueError("Embedding model has no local weight files")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def top_cosine(vectors, query, limit, vector_norms=None):
    """Exact cosine top-k with stable tie order; no FAISS dependency for small corpora."""
    import numpy as np

    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.size == 0:
        return []
    needle = np.asarray(query, dtype=np.float32)
    if matrix.ndim != 2 or needle.ndim != 1 or matrix.shape[1] != needle.shape[0]:
        raise ValueError("Embedding dimensions do not match")
    norms = (np.asarray(vector_norms, dtype=np.float32) if vector_norms is not None
             else np.linalg.norm(matrix, axis=1)) * np.linalg.norm(needle)
    if norms.shape != (len(matrix),):
        raise ValueError("Embedding norm count does not match vectors")
    scores = np.divide(matrix @ needle, norms, out=np.zeros(len(matrix), dtype=np.float32), where=norms > 0)
    return [(int(i), float(scores[i])) for i in np.argsort(-scores, kind="stable")[:limit]]


class LocalEmbedder:
    def __init__(self, config):
        if not config["enabled"]:
            raise ValueError("LocalEmbedder requires embedding.enabled=true")
        # Hub offline mode is read during import by some dependency versions.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ValueError("Install the optional embedding dependencies: uv sync --extra embedding") from exc
        self.config = config
        self.model_sha256 = model_digest(config["model_path"])
        self.model = SentenceTransformer(config["model_path"], device="cpu", local_files_only=True)
        self.cache = {}
        self.documents_encoded = 0
        self.queries_encoded = 0

    def _encode(self, texts, *, query=False):
        import numpy as np

        options = {"batch_size": self.config["batch_size"], "normalize_embeddings": True,
                   "convert_to_numpy": True, "show_progress_bar": False}
        if query:
            if self.config.get("query_prompt"):
                options["prompt"] = self.config["query_prompt"]
            elif "query" in (getattr(self.model, "prompts", None) or {}):
                options["prompt_name"] = "query"
        values = np.asarray(self.model.encode(texts, **options), dtype=np.float32)
        if values.ndim == 1:
            values = values.reshape(1, -1)
        norms = np.linalg.norm(values, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise ValueError("Embedding model returned a zero vector")
        return values / norms

    def documents(self, texts, *, cache=True):
        import numpy as np

        if not texts:
            return np.empty((0, self.report()["dimensions"]), dtype=np.float32)
        if not cache:
            self.documents_encoded += len(texts)
            return self._encode(texts)
        missing = list(dict.fromkeys(text for text in texts if text not in self.cache))
        if missing:
            for text, vector in zip(missing, self._encode(missing)):
                self.cache[text] = vector
            self.documents_encoded += len(missing)
        return np.stack([self.cache[text] for text in texts])

    def query(self, text):
        self.queries_encoded += 1
        return self._encode([text], query=True)[0]

    def report(self):
        dimension = (self.model.get_embedding_dimension() if hasattr(self.model, "get_embedding_dimension")
                     else self.model.get_sentence_embedding_dimension())
        return {"enabled": True, "model": Path(self.config["model_path"]).name,
                "model_sha256": self.model_sha256,
                "query_prompt": self.config.get("query_prompt") or (
                    "model:query" if "query" in (getattr(self.model, "prompts", None) or {}) else "none"),
                "min_cosine_similarity": self.config["min_cosine_similarity"],
                "dimensions": dimension,
                "documents_encoded": self.documents_encoded,
                "queries_encoded": self.queries_encoded,
                "retrieval": "external_ontology_and_accepted_core; mcp_snippets_reranked_when_available",
                "mcp_document_recall": "provided_by_mcp_server"}
