"""Local semantic candidates, cache boundaries, and optional-provider regressions."""
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import sqlite3
import threading
import time
from unittest.mock import patch

import pytest

from app.core.evidence import EvidenceBundle
from app.core import semantic_index as semantic
from app.core.semantic_index import LocalEmbeddingClient, SemanticConfig, SemanticError, SemanticIndex


def evidence(item_id=0, content="Physics lecture notes about heat transfer and thermodynamics.", **changes):
    base = EvidenceBundle(path=f"/selected/private-{item_id}.txt", fingerprint=f"{item_id + 1:064x}",
                          filename=f"private-{item_id}.txt", extension=".txt", parent_directory="selected",
                          content=content, extraction_status="sufficient")
    return replace(base, **changes)


class FakeEmbedder:
    model_identity = "fixture:model-v1"

    def __init__(self):
        self.calls = []

    def embed(self, texts, *, task):
        self.calls.append((task, list(texts)))
        return [[1, 0, 0] if "physics" in text.lower() else [0, 1, 0] for text in texts]


def build(tmp_path, items=None, embedder=None, **config):
    return SemanticIndex.build(items or {0: evidence()}, config=SemanticConfig(enabled=True, min_files=0, **config),
                               embedder=embedder or FakeEmbedder(), cache_path=tmp_path / "embeddings.sqlite")


@contextmanager
def local_server(**changes):
    state = {"requests": [], "digest": "a" * 64, "vectors": None, "models": None, **changes}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["requests"].append(("GET", self.path, None))
            if state.get("delay"):
                time.sleep(state["delay"])
            if state.get("redirect"):
                self.send_response(302)
                self.send_header("Location", "http://example.invalid/collect")
                self.end_headers()
                return
            self.send_json({"models": state["models"] if state["models"] is not None else [
                {"name": "all-minilm:22m", "digest": state["digest"]},
            ]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(("POST", self.path, body))
            if state.get("retag"):
                state["digest"] = "b" * 64
            self.send_json({"embeddings": state["vectors"] if state["vectors"] is not None else
                            [[3.0, 4.0] for _ in body["input"]],
                            "total_duration": 20_000_000, "load_duration": 5_000_000})

        def send_json(self, value):
            body = json.dumps(value).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_default_and_below_threshold_never_construct_probe_or_cache(tmp_path):
    with patch.object(semantic, "LocalEmbeddingClient") as client:
        disabled = SemanticIndex.build({0: evidence()}, cache_path=tmp_path / "disabled.sqlite")
        small = SemanticIndex.build({i: evidence(i) for i in range(74)},
                                    config=SemanticConfig(enabled=True), cache_path=tmp_path / "small.sqlite")
    assert disabled.stats["embedding_status"] == "disabled"
    assert small.stats["embedding_status"] == "below_threshold"
    assert not disabled.vectors and not small.vectors
    client.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_threshold_is_configurable_from_environment(monkeypatch):
    monkeypatch.setenv("ORGANIZER_SEMANTIC_ENABLED", "true")
    monkeypatch.setenv("ORGANIZER_EMBEDDING_MIN_FILES", "100")
    monkeypatch.setenv("ORGANIZER_EMBEDDING_MODEL", "another-local-model")
    config = SemanticConfig.from_env()
    assert config.enabled and config.min_files == 100 and config.model == "another-local-model"


def test_sensitive_weak_missing_and_empty_evidence_never_reaches_provider(tmp_path):
    items = {0: evidence(0, sensitive_findings=[{"kind": "secret"}]),
             1: evidence(1, extraction_status="weak"), 2: evidence(2, extraction_status="unavailable"),
             3: evidence(3, fingerprint=""), 4: evidence(4, content="")}
    with patch.object(semantic, "LocalEmbeddingClient") as client:
        index = SemanticIndex.build(items, config=SemanticConfig(enabled=True, min_files=0),
                                    cache_path=tmp_path / "unused.sqlite")
    assert index.stats["embedding_status"] == "empty"
    assert index.stats["embedding_excluded_sensitive"] == 1
    client.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_sensitive_files_are_excluded_even_from_a_mixed_batch(tmp_path):
    fake = FakeEmbedder()
    index = build(tmp_path, {0: evidence(), 1: evidence(1, "DO NOT SEND", sensitive_findings=[{"kind": "secret"}])}, fake)
    assert set(index.vectors) == {0}
    assert "DO NOT SEND" not in json.dumps(fake.calls)


def test_standalone_index_rechecks_privacy_before_cache_or_provider(tmp_path):
    fake = FakeEmbedder()
    item = evidence(content="API_KEY=fixture-not-a-real-secret-value")
    assert not item.sensitive  # A caller forgot to run the upstream privacy extractor.
    index = build(tmp_path, {0: item}, fake)
    assert index.stats["embedding_status"] == "empty"
    assert index.stats["embedding_excluded_sensitive"] == 1
    assert not fake.calls and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("url", ["https://example.com", "http://192.168.1.1:11434", "http://localhost.example.com",
                                "http://127.0.0.1@evil.example", "http://user:password@localhost:11434",
                                "http://localhost:11434?token=secret", "http://localhost:11434/#fragment",
                                "ftp://127.0.0.1:11434", "http://127.0.0.1:11434/arbitrary-path"])
def test_nonlocal_or_credentialed_endpoints_are_rejected_before_network(url):
    with patch.object(semantic, "build_opener") as opener:
        with pytest.raises(SemanticError):
            LocalEmbeddingClient(SemanticConfig(base_url=url))
    opener.assert_not_called()


def test_local_transport_uses_installed_digest_and_never_downloads(tmp_path, monkeypatch):
    # Explicit proxy disabling ensures local evidence cannot be sent to a proxy.
    monkeypatch.setenv("HTTP_PROXY", "http://example.invalid:8080")
    with local_server() as (state, url):
        index = SemanticIndex.build({0: evidence()}, config=SemanticConfig(enabled=True, min_files=0, base_url=url),
                                    cache_path=tmp_path / "embeddings.sqlite")
        assert index.stats["embedding_status"] == "ready"
        assert index.vectors[0] == pytest.approx((0.6, 0.8))
        assert "a" * 64 in index.stats["embedding_model_identity"]
        assert index.stats["embedding_model_load_seconds"] == pytest.approx(0.005)
        assert index.stats["embedding_model_compute_seconds"] == pytest.approx(0.015)
        assert {request[1] for request in state["requests"]} == {"/api/tags", "/api/embed"}
        assert state["requests"][1][2]["truncate"] is False


def test_missing_model_never_triggers_download_or_embedding_request(tmp_path):
    with local_server(models=[]) as (state, url):
        index = SemanticIndex.build({0: evidence()}, config=SemanticConfig(enabled=True, min_files=0, base_url=url),
                                    cache_path=tmp_path / "cache.sqlite")
    assert index.stats["embedding_status"] == "unavailable" and index.warnings
    assert state["requests"] == [("GET", "/api/tags", None)]


@pytest.mark.parametrize("extra", [{"remote_host": "https://cloud.example"}, {"remote_model": "hosted-model"},
                                  {"digest": "not-a-model-digest"}])
def test_cloud_or_unversioned_model_is_not_used(extra):
    with local_server(models=[{"name": "all-minilm:22m", "digest": "a" * 64, **extra}]) as (state, url):
        with pytest.raises(SemanticError):
            LocalEmbeddingClient(SemanticConfig(base_url=url))
    assert all(request[0] == "GET" for request in state["requests"])


def test_redirect_is_not_followed_even_from_local_service():
    with local_server(redirect=True) as (state, url):
        with pytest.raises(SemanticError):
            LocalEmbeddingClient(SemanticConfig(base_url=url))
    assert len(state["requests"]) == 1


def test_service_timeout_falls_back_without_exposing_evidence(tmp_path):
    with local_server(delay=0.1) as (_state, url):
        index = SemanticIndex.build({0: evidence(content="PRIVATE DOCUMENT CONTENT")},
                                    config=SemanticConfig(enabled=True, min_files=0, base_url=url, timeout=0.01),
                                    cache_path=tmp_path / "cache.sqlite")
    assert index.stats["embedding_status"] == "unavailable"
    assert "PRIVATE DOCUMENT CONTENT" not in " ".join(index.warnings)
    assert index.stats["embedding_provider_setup_seconds"] > 0


def test_response_size_limit_is_enforced():
    with local_server() as (_state, url):
        with patch.object(semantic, "_MAX_RESPONSE_BYTES", 10):
            with pytest.raises(SemanticError):
                LocalEmbeddingClient(SemanticConfig(base_url=url))


def test_model_retagging_during_inference_invalidates_the_result(tmp_path):
    with local_server(retag=True) as (_state, url):
        index = SemanticIndex.build({0: evidence()}, config=SemanticConfig(enabled=True, min_files=0, base_url=url),
                                    cache_path=tmp_path / "cache.sqlite")
    assert index.stats["embedding_status"] == "unavailable"
    assert not index.vectors


def test_exact_duplicate_content_is_encoded_once_but_keeps_file_ids(tmp_path):
    fake = FakeEmbedder()
    item = evidence()
    index = build(tmp_path, {0: item, 1: replace(item, path="/selected/copy.txt", filename="copy.txt")}, fake)
    assert index.stats["embedding_unique_inputs"] == 1
    assert set(index.vectors) == {0, 1}
    assert fake.calls[0][1] == [item.content]
    assert index.candidates(0) == [(1, 1.0)]


def test_file_names_and_paths_are_not_embedded_or_cache_dependencies(tmp_path):
    fake = FakeEmbedder()
    item = evidence(metadata={"title": "Heat Transfer", "author": "PRIVATE AUTHOR"})
    first = build(tmp_path, {0: item}, fake)
    second_fake = FakeEmbedder()
    second = build(tmp_path, {0: replace(item, filename="new.txt", path="/new/root/new.txt", parent_directory="new")}, second_fake)
    assert first.vectors == second.vectors
    assert second.stats["embedding_cache_hits"] == 1 and second_fake.calls == []
    serialized = json.dumps(fake.calls)
    assert "PRIVATE AUTHOR" not in serialized and item.filename not in serialized and item.path not in serialized


@pytest.mark.parametrize("change", ["fingerprint", "content", "metadata", "model"])
def test_cache_invalidation_covers_content_evidence_and_model_version(tmp_path, change):
    item = evidence()
    build(tmp_path, {0: item})
    fake = FakeEmbedder()
    if change == "fingerprint":
        item = replace(item, fingerprint="b" * 64)
    elif change == "content":
        item = replace(item, content=item.content + " Updated extraction.")
    elif change == "metadata":
        item = replace(item, metadata={"title": "Changed title"})
    else:
        fake.model_identity = "fixture:model-v2"
    index = build(tmp_path, {0: item}, fake)
    assert index.stats["embedding_cache_hits"] == 0
    assert len(fake.calls) == 1


def test_invalid_cache_rows_are_recomputed(tmp_path):
    build(tmp_path)
    with sqlite3.connect(tmp_path / "embeddings.sqlite") as connection:
        connection.execute("UPDATE embeddings SET vector = ?", ('[NaN, 1]',))
    fake = FakeEmbedder()
    index = build(tmp_path, embedder=fake)
    assert index.stats["embedding_status"] == "ready"
    assert index.stats["embedding_cache_hits"] == 0 and fake.calls


def test_unavailable_cache_does_not_disable_semantic_analysis(tmp_path):
    directory = tmp_path / "directory"
    directory.mkdir()
    index = SemanticIndex.build({0: evidence()}, config=SemanticConfig(enabled=True, min_files=0),
                                embedder=FakeEmbedder(), cache_path=directory)
    assert index.stats["embedding_status"] == "ready" and index.warnings
    assert index.vectors


@pytest.mark.parametrize("vectors", [[[float("nan"), 1]], [[float("inf"), 1]], [[0, 0]], [[True, 1]], [[]], [], [["text", 1]]])
def test_invalid_vectors_fail_closed_to_ordinary_planning(tmp_path, vectors):
    index = build(tmp_path, embedder=lambda _texts: vectors)
    assert index.stats["embedding_status"] == "unavailable"
    assert not index.vectors and index.warnings


def test_inconsistent_dimensions_never_create_a_partial_index(tmp_path):
    index = build(tmp_path, {0: evidence(), 1: evidence(1)}, lambda texts: [[1, 0], [1, 0, 0]])
    assert index.stats["embedding_status"] == "unavailable" and not index.vectors


def test_backend_exception_is_visible_without_raw_provider_data(tmp_path):
    def failure(_texts):
        raise RuntimeError("echoed PRIVATE DOCUMENT CONTENT")
    index = build(tmp_path, embedder=failure)
    assert index.stats["embedding_status"] == "unavailable"
    assert index.warnings and "PRIVATE" not in " ".join(index.warnings)


def test_complete_semantic_search_never_silently_limits_global_chat(tmp_path):
    fake = FakeEmbedder()
    items = {i: evidence(i, content=f"Physics lesson {i} with sufficient content.") for i in range(101)}
    index = build(tmp_path, items, fake)
    ranked = index.search("all physics files")
    assert len(ranked) == 101 and {item_id for item_id, _score in ranked} == set(items)
    assert len(index.search("all physics files", limit=3)) == 3
    assert [item_id for item_id, _score in index.search("all physics files", allowed_ids={4, 100, 1000})] == [4, 100]
    assert index.search("all physics files", allowed_ids=set()) == []
    assert len([call for call in fake.calls if call[0] == "query"]) == 1


def test_query_embeddings_are_task_separated_and_persistently_cached(tmp_path):
    fake = FakeEmbedder()
    first = build(tmp_path, embedder=fake)
    first.search(evidence().content)
    assert [call[0] for call in fake.calls] == ["document", "query"]
    second_fake = FakeEmbedder()
    second = build(tmp_path, embedder=second_fake)
    assert second.search(evidence().content)
    assert second.stats["embedding_query_cache_hits"] == 1 and not second_fake.calls


def test_sensitive_queries_are_never_persisted_or_reused(tmp_path):
    fake = FakeEmbedder()
    index = build(tmp_path, embedder=fake)
    query = "Find credentials sk-" + "a" * 24
    index.search(query)
    index.search(query)
    assert len([call for call in fake.calls if call[0] == "query"]) == 2
    assert not index._queries
    with sqlite3.connect(tmp_path / "embeddings.sqlite") as connection:
        assert connection.execute("SELECT count(*) FROM embeddings").fetchone()[0] == 1
    assert query.encode() not in (tmp_path / "embeddings.sqlite").read_bytes()


def test_query_dimension_change_falls_back_instead_of_returning_wrong_matches(tmp_path):
    class ChangedDimensions(FakeEmbedder):
        def embed(self, texts, *, task):
            return [[1, 0] if task == "query" else [1, 0, 0] for _ in texts]
    index = build(tmp_path, embedder=ChangedDimensions())
    assert index.search("physics") == [] and index.warnings


def test_candidate_ranking_is_normalized_deterministic_and_excludes_self(tmp_path):
    index = build(tmp_path, {0: evidence(), 1: evidence(1), 2: evidence(2, content="Invoice financial purchase")})
    assert index.candidates(0) == [(1, 1.0), (2, 0.0)]
    assert index.candidates(0, limit=1) == [(1, 1.0)]
    assert index.candidates(100) == [] and index.candidates(0, limit=0) == []
    assert all(math.isclose(math.hypot(*vector), 1) for vector in index.vectors.values())


def test_input_and_query_budgets_are_bounded(tmp_path):
    fake = FakeEmbedder()
    index = build(tmp_path, {0: evidence(content="Physics " * 200)}, fake, max_input_chars=128)
    assert len(fake.calls[0][1][0]) == 128
    assert index.search("x" * 129) == []
    assert index.warnings and len(fake.calls) == 1


def test_qwen_queries_receive_instruction_prefix_without_changing_documents():
    with local_server(models=[{"name": "qwen3-embedding:0.6b", "digest": "a" * 64}]) as (state, url):
        client = LocalEmbeddingClient(SemanticConfig(base_url=url, model="qwen3-embedding:0.6b"))
        client.embed(["Physics lecture"], task="document")
        client.embed(["physics exercises"], task="query")
    inputs = [request[2]["input"][0] for request in state["requests"] if request[0] == "POST"]
    assert inputs[0] == "Physics lecture"
    assert inputs[1].startswith("Instruct: ") and inputs[1].endswith("Query: physics exercises")
