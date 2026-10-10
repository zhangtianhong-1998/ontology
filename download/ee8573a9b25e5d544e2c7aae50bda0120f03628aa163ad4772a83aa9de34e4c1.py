import pytest
import sys
from types import SimpleNamespace

from ontology_r2.embedding import LocalEmbedder, settings, top_cosine
from ontology_r2.external import ExternalIndex
from ontology_r2.incremental import semantic_core_hits
from ontology_r2.knowledge import rerank_hits
from ontology_r2.models import BuildPlan, TablePlan


class FakeEmbedding:
    config = {"max_cards": 10, "min_cosine_similarity": 0.35}
    model_sha256 = "synthetic-model"

    def documents(self, texts, *, cache=True):
        return [[1.0, 0.0] if "Gross margin" in text or "profit" in text else [0.0, 1.0]
                for text in texts]

    def query(self, text):
        return [1.0, 0.0]


def test_embedding_settings_require_explicit_local_model(tmp_path, monkeypatch):
    monkeypatch.setenv("ONTOLOGY_EMBEDDING_ENABLED", "true")
    monkeypatch.setenv("ONTOLOGY_EMBEDDING_MODEL_PATH", str(tmp_path))
    assert settings({"enabled": False})["model_path"] == str(tmp_path)
    monkeypatch.setenv("ONTOLOGY_EMBEDDING_MODEL_PATH", str(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError):
        settings({"enabled": False})


def test_dtype_and_progress_settings_are_explicit_and_environment_overridable(monkeypatch):
    monkeypatch.delenv("ONTOLOGY_EMBEDDING_ENABLED", raising=False)
    monkeypatch.delenv("ONTOLOGY_EMBEDDING_DTYPE", raising=False)
    monkeypatch.delenv("ONTOLOGY_EMBEDDING_SHOW_PROGRESS", raising=False)
    assert settings({})["dtype"] == "float32"
    monkeypatch.setenv("ONTOLOGY_EMBEDDING_DTYPE", "bfloat16")
    monkeypatch.setenv("ONTOLOGY_EMBEDDING_SHOW_PROGRESS", "true")
    config = settings({"dtype": "auto", "show_progress": False})
    assert config["dtype"] == "bfloat16" and config["show_progress"] is True
    monkeypatch.setenv("ONTOLOGY_EMBEDDING_DTYPE", "float16")
    with pytest.raises(ValueError, match="embedding.dtype"):
        settings({})
    monkeypatch.setenv("ONTOLOGY_EMBEDDING_DTYPE", "auto")
    monkeypatch.setenv("ONTOLOGY_EMBEDDING_SHOW_PROGRESS", "maybe")
    with pytest.raises(ValueError, match="SHOW_PROGRESS"):
        settings({})
    monkeypatch.delenv("ONTOLOGY_EMBEDDING_SHOW_PROGRESS")
    with pytest.raises(ValueError, match="show_progress"):
        settings({"show_progress": "false"})


def _local_embedder(tmp_path, monkeypatch, **options):
    import numpy as np
    model_path = tmp_path / "model"
    model_path.mkdir(exist_ok=True)
    (model_path / "model.safetensors").write_bytes(b"synthetic weights")
    class Model:
        prompts = {"query": "query instruction", "document": ""}
        default_prompt_name = None
        def __init__(self, path, **kwargs):
            self.load_kwargs = kwargs
            self.calls = []
            requested = kwargs["model_kwargs"]["dtype"]
            self.dtype = "bfloat16" if requested == "auto" else requested
        def parameters(self):
            return iter([SimpleNamespace(dtype="torch." + self.dtype, device="cpu")])
        def encode(self, texts, **kwargs):
            self.calls.append((list(texts), kwargs))
            return np.array([[len(text), 1.0] for text in texts], dtype=np.float32)
        def get_embedding_dimension(self):
            return 2
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=Model))
    return LocalEmbedder({"enabled": True, "model_path": str(model_path), "batch_size": 2,
                          "min_cosine_similarity": 0.35, "query_prompt": None, **options})


@pytest.mark.parametrize("dtype,actual", [("float32", "float32"), ("bfloat16", "bfloat16"), ("auto", "bfloat16")])
def test_model_load_dtype_and_encoding_contract_are_reported(tmp_path, monkeypatch, dtype, actual):
    embedding = _local_embedder(tmp_path, monkeypatch, dtype=dtype)
    assert embedding.model.load_kwargs == {"device": "cpu", "local_files_only": True,
                                            "model_kwargs": {"dtype": dtype}}
    report = embedding.report()
    assert report["configured_dtype"] == dtype and report["actual_dtype"] == actual
    assert report["device"] == "cpu"
    assert report["encoding_contract"]["actual_dtype"] == actual
    assert len(report["encoding_contract_sha256"]) == 64


def test_progress_reports_batches_and_memory_cache_is_contract_scoped(tmp_path, monkeypatch, capsys):
    import numpy as np
    embedding = _local_embedder(tmp_path, monkeypatch, show_progress=True)
    vectors = embedding.documents(["income", "cost", "income", "profit"])
    assert vectors.shape == (4, 2) and vectors.dtype == np.float32
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1)
    assert embedding.documents_encoded == 3
    assert len(embedding.model.calls) == 1
    assert embedding.model.calls[0][1]["show_progress_bar"] is True
    assert "3 texts, 2 batches, cpu/float32" in capsys.readouterr().err
    assert {key[0] for key in embedding.cache} == {embedding.encoding_contract_sha256}
    embedding.documents(["income"])
    assert len(embedding.model.calls) == 1
    embedding.queries(["income", "cost"])
    assert embedding.model.calls[-1][1]["prompt_name"] == "query"
    assert "[embedding:query]" in capsys.readouterr().err
    assert embedding.queries([]).shape == (0, 2)
    other = _local_embedder(tmp_path, monkeypatch, dtype="bfloat16")
    assert other.encoding_contract_sha256 != embedding.encoding_contract_sha256
    other.documents(["income"])
    assert other.model.calls[0][1]["show_progress_bar"] is False
    assert capsys.readouterr().err == ""


def test_exact_cosine_normalizes_and_preserves_tie_order():
    assert top_cosine([[2.0, 0.0], [1.0, 0.0], [0.0, 3.0]], [1.0, 0.0], 3) == [
        (0, 1.0), (1, 1.0), (2, 0.0)]


def test_external_vector_channel_recalls_missing_lexical_match(tmp_path):
    model = tmp_path / "mini.ttl"
    model.write_text('''@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<https://example.org/GrossMargin> a owl:Class ; rdfs:label "Gross margin" .
    <https://example.org/City> a owl:Class ; rdfs:label "City" .
''')
    config = {"sources": [{"id": "test", "format": "rdf", "path": str(model)}]}
    lexical_dir, hybrid_dir = tmp_path / "lexical", tmp_path / "hybrid"
    lexical_dir.mkdir()
    hybrid_dir.mkdir()
    lexical = ExternalIndex(config, lexical_dir)
    assert lexical.search_many(["利润率"], 1) == []
    lexical.close()
    hybrid = ExternalIndex(config, hybrid_dir, embedding=FakeEmbedding())
    result = hybrid.search_many(["利润率"], 1)
    assert result[0]["uri"] == "https://example.org/GrossMargin"
    assert result[0]["retrieval"]["channels"] == ["cosine"]
    assert result[0]["retrieval"]["model_sha256"] == "synthetic-model"
    hybrid.close()


def test_external_vector_threshold_does_not_force_unrelated_alignment(tmp_path):
    model = tmp_path / "mini.ttl"
    model.write_text('''@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<https://example.org/City> a owl:Class ; rdfs:label "City" .
''')
    work = tmp_path / "work"
    work.mkdir()
    index = ExternalIndex({"sources": [{"id": "test", "format": "rdf", "path": str(model)}]},
                          work, embedding=FakeEmbedding())
    assert index.search_many(["利润率"], 1) == []
    index.close()


def test_external_embedding_card_limit_fails_before_indexing(tmp_path):
    model = tmp_path / "mini.ttl"
    model.write_text('''@prefix owl: <http://www.w3.org/2002/07/owl#> .
<https://example.org/A> a owl:Class .
<https://example.org/B> a owl:Class .
''')
    class TinyEmbedding(FakeEmbedding):
        config = {"max_cards": 1, "min_cosine_similarity": 0.35}

    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ValueError, match="max_cards"):
        ExternalIndex({"sources": [{"id": "test", "format": "rdf", "path": str(model)}]},
                      work, embedding=TinyEmbedding())


def test_core_vector_context_only_uses_accepted_units():
    class Data:
        tables = {
            "past": {"table_comment": "profit data", "columns": [{"column_name": "value", "column_comment": "Gross margin"}]},
            "unaccepted": {"table_comment": "profit data", "columns": [{"column_name": "value", "column_comment": "Gross margin"}]},
            "current": {"table_comment": "营业利润率", "columns": [{"column_name": "value", "column_comment": "利润率"}]},
        }

    core = BuildPlan(tables=[TablePlan(table=name, object_type="GeneralObject", evidence_ids=[])
                             for name in Data.tables])
    hits = semantic_core_hits(core, Data(), "current", ["past"], FakeEmbedding(), 5)
    assert [hit["table"] for hit in hits] == ["past"]
    assert hits[0]["cosine_similarity"] == 1.0


def test_mcp_vector_rerank_preserves_candidate_provenance():
    hits = [{"id": "unrelated", "title": "City", "snippet": "weather"},
            {"id": "relevant", "title": "Gross margin", "snippet": "profit formula"}]
    ranked = rerank_hits("利润率", hits, FakeEmbedding())
    assert [hit["id"] for hit in ranked] == ["relevant", "unrelated"]
    assert ranked[0]["client_rerank"]["original_rank"] == 2
    assert ranked[0]["client_rerank"]["method"] == "local_cosine_on_mcp_snippet"
