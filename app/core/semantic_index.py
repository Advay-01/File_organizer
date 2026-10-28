"""Optional local semantic candidates; vectors never authorize file operations.

Ollama supplies the model runtime, so the desktop bundle needs no ML dependency
or automatic model download. Persistent entries contain only keys and vectors;
sensitive or insufficient evidence never enters this index.
"""
from __future__ import annotations

import hashlib
import inspect
import ipaddress
import json
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from app.core.evidence import EVIDENCE_VERSION, EvidenceBundle
from app.core.privacy import detect_sensitive

_INPUT_VERSION = 1
_MAX_VECTOR_DIMENSIONS = 4096
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_QUERY_INSTRUCTION = "Retrieve files matching the user's organization instruction."


class SemanticError(ValueError):
    """The optional local semantic backend cannot be used safely."""


@dataclass(frozen=True)
class SemanticConfig:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3-embedding:0.6b"
    min_files: int = 75
    batch_size: int = 16
    max_input_chars: int = 12000
    timeout: float = 10.0

    def __post_init__(self) -> None:
        if not 0 <= self.min_files <= 100000:
            raise ValueError("Embedding minimum file count is outside its supported range.")
        if not 1 <= self.batch_size <= 64 or not 64 <= self.max_input_chars <= 32000:
            raise ValueError("Embedding input budgets are outside their supported ranges.")
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 60:
            raise ValueError("Embedding timeout must be between zero and 60 seconds.")
        if not self.model or len(self.model) > 200 or any(character.isspace() for character in self.model):
            raise ValueError("Invalid embedding model name.")

    @classmethod
    def from_env(cls) -> "SemanticConfig":
        return cls(
            enabled=os.getenv("ORGANIZER_SEMANTIC_ENABLED", "0").lower() in {"1", "true", "yes"},
            base_url=os.getenv("ORGANIZER_EMBEDDING_BASE_URL", "http://127.0.0.1:11434"),
            model=os.getenv("ORGANIZER_EMBEDDING_MODEL", "qwen3-embedding:0.6b"),
            min_files=int(os.getenv("ORGANIZER_EMBEDDING_MIN_FILES", "75")),
            batch_size=int(os.getenv("ORGANIZER_EMBEDDING_BATCH_SIZE", "16")),
            max_input_chars=int(os.getenv("ORGANIZER_EMBEDDING_MAX_CHARS", "12000")),
            timeout=float(os.getenv("ORGANIZER_EMBEDDING_TIMEOUT", "10")),
        )


def _local_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        hostname, port = parsed.hostname, parsed.port
        if parsed.scheme not in {"http", "https"} or not hostname:
            raise ValueError
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError
        if parsed.path.rstrip("/") not in {"", "/api", "/v1"}:
            raise ValueError
        # Pin localhost to a numeric loopback address, bypassing DNS and proxies.
        address = ipaddress.ip_address("127.0.0.1" if hostname.lower() == "localhost" else hostname)
        if not address.is_loopback:
            raise ValueError
        host = f"[{address}]" if address.version == 6 else str(address)
        netloc = f"{host}:{port}" if port is not None else host
        return urlunsplit((parsed.scheme, netloc, "", "", ""))
    except (TypeError, ValueError) as error:
        raise SemanticError("Embedding endpoints must use a numeric loopback address or localhost.") from error


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise SemanticError("Embedding endpoint redirects are not permitted.")


class LocalEmbeddingClient:
    """Use an already-installed local Ollama model, identified by its digest."""

    def __init__(self, config: SemanticConfig) -> None:
        self.config = config
        self.base_url = _local_base_url(config.base_url)
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self.probe_seconds = 0.0
        self.inference_seconds = 0.0
        self.model_load_seconds = 0.0
        self.model_compute_seconds = 0.0
        self.model_identity = self._installed_identity()

    def _request(self, path: str, data: dict | None = None) -> dict:
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        if body is not None and len(body) > 2 * 1024 * 1024:
            raise SemanticError("Embedding request exceeds its size budget.")
        request = Request(self.base_url + path, data=body, headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=self.config.timeout) as response:
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(payload) > _MAX_RESPONSE_BYTES:
                raise SemanticError("Embedding response exceeds its size budget.")
            result = json.loads(payload)
            if not isinstance(result, dict):
                raise SemanticError("Invalid embedding response.")
            return result
        except (HTTPError, URLError, OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
            # Provider response bodies can contain document text; never expose them.
            raise SemanticError("The local embedding service is unavailable or returned invalid data.") from error

    def _installed_identity(self) -> str:
        started = time.perf_counter()
        try:
            models = self._request("/api/tags").get("models")
            if not isinstance(models, list):
                raise SemanticError("The local embedding model list is invalid.")
            requested = self.config.model
            aliases = {requested, requested + ":latest"} if ":" not in requested else {requested}
            for model in models:
                if not isinstance(model, dict) or model.get("name", model.get("model")) not in aliases:
                    continue
                if (model.get("remote_host") or model.get("remote_model")
                        or requested.lower().endswith((":cloud", "-cloud"))):
                    raise SemanticError("Cloud embedding models are not permitted.")
                digest = str(model.get("digest", "")).removeprefix("sha256:")
                if not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
                    raise SemanticError("The local embedding model has no verifiable digest.")
                return f"ollama:{self.base_url}:{requested}:{digest.lower()}"
            raise SemanticError("The configured embedding model is not installed locally.")
        finally:
            self.probe_seconds += time.perf_counter() - started

    def embed(self, texts: list[str], *, task: str) -> list[list[float]]:
        inputs = texts
        if task == "query" and "qwen" in self.config.model.lower():
            inputs = [f"Instruct: {_QUERY_INSTRUCTION}\nQuery: {text}" for text in texts]
        started = time.perf_counter()
        result = self._request("/api/embed", {
            "model": self.config.model, "input": inputs, "truncate": False, "keep_alive": "5m",
        })
        self.inference_seconds += time.perf_counter() - started
        if self._installed_identity() != self.model_identity:
            raise SemanticError("The embedding model changed while computing vectors.")
        load = result.get("load_duration", 0)
        duration = result.get("total_duration", 0)
        load = load if isinstance(load, (int, float)) and math.isfinite(load) and load >= 0 else 0
        duration = duration if isinstance(duration, (int, float)) and math.isfinite(duration) and duration >= 0 else 0
        self.model_load_seconds += load / 1_000_000_000
        self.model_compute_seconds += max(0, duration - load) / 1_000_000_000
        return result.get("embeddings")


def _normalize(vector: Sequence[float]) -> tuple[float, ...]:
    if not isinstance(vector, (list, tuple)) or not 1 <= len(vector) <= _MAX_VECTOR_DIMENSIONS:
        raise SemanticError("Invalid embedding vector dimensions.")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
           for value in vector):
        raise SemanticError("Embedding vectors must contain finite numeric values.")
    length = math.hypot(*vector)
    if not math.isfinite(length) or length <= 0:
        raise SemanticError("Embedding vectors must have a nonzero finite magnitude.")
    return tuple(float(value) / length for value in vector)


def _embedding_key(identity: str, task: str, text: str, fingerprint: str = "") -> str:
    value = json.dumps([_INPUT_VERSION, EVIDENCE_VERSION, identity, task, fingerprint, text], ensure_ascii=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _document_text(evidence: EvidenceBundle, limit: int) -> str:
    # Filename/path changes do not invalidate content-derived embeddings. Names,
    # authors, timestamps and filesystem proximity are not semantic proof.
    metadata = "\n".join(f"{key}: {evidence.metadata[key]}" for key in
                         ("title", "subject", "keywords", "description", "headings")
                         if evidence.metadata.get(key))
    return f"{metadata}\n{evidence.content}".strip()[:limit]


class _EmbeddingCache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=2)
        try:
            self.connection.execute("CREATE TABLE IF NOT EXISTS embeddings (cache_key TEXT PRIMARY KEY, vector TEXT NOT NULL)")
            self.connection.commit()
        except sqlite3.Error:
            self.connection.close()
            raise

    def get(self, key: str) -> tuple[float, ...] | None:
        row = self.connection.execute(
            "SELECT vector FROM embeddings WHERE cache_key = ? AND length(vector) <= 200000", (key,),
        ).fetchone()
        if row is None:
            return None
        try:
            return _normalize(json.loads(row[0]))
        except (SemanticError, ValueError, TypeError):
            return None

    def put_many(self, entries: Mapping[str, tuple[float, ...]]) -> None:
        self.connection.executemany("INSERT OR REPLACE INTO embeddings(cache_key, vector) VALUES (?, ?)",
                                    [(key, json.dumps(vector)) for key, vector in entries.items()])
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


@dataclass
class SemanticIndex:
    vectors: dict[int, tuple[float, ...]] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    _config: SemanticConfig = field(default_factory=SemanticConfig, repr=False)
    _embedder: object | None = field(default=None, repr=False)
    _identity: str = field(default="", repr=False)
    _cache_path: Path | None = field(default=None, repr=False)
    _queries: dict[str, tuple[float, ...]] = field(default_factory=dict, repr=False)

    @classmethod
    def build(
        cls, evidence_by_id: Mapping[int, EvidenceBundle], *, config: SemanticConfig | None = None,
        embedder: object | None = None, cache_path: str | Path | None = None,
    ) -> "SemanticIndex":
        started = time.perf_counter()
        index = cls(stats={"embedding_status": "disabled", "embedding_cache_hits": 0,
                           "embedding_unique_inputs": 0, "embedding_batches": 0,
                           "embedding_query_calls": 0, "embedding_query_cache_hits": 0,
                           "embedding_cache_seconds": 0.0, "embedding_inference_seconds": 0.0,
                           "embedding_provider_setup_seconds": 0.0})
        cache = None
        try:
            index._config = config or SemanticConfig.from_env()
            if not index._config.enabled:
                return index
            if len(evidence_by_id) < index._config.min_files:
                index.stats["embedding_status"] = "below_threshold"
                return index
            cards = {}
            sensitive_ids = set()
            for item_id, evidence in evidence_by_id.items():
                sensitive = evidence.sensitive or bool(detect_sensitive(
                    evidence.content,
                    [evidence.filename, evidence.parent_directory, *map(str, evidence.metadata.values())],
                ))
                if sensitive:
                    sensitive_ids.add(item_id)
                if sensitive or evidence.extraction_status != "sufficient" or not evidence.fingerprint:
                    continue
                text = _document_text(evidence, index._config.max_input_chars)
                if text:
                    cards[item_id] = text
            index.stats["embedding_excluded_sensitive"] = len(sensitive_ids)
            index.stats["embedding_excluded_weak"] = len(evidence_by_id) - len(cards) - index.stats["embedding_excluded_sensitive"]
            if not cards:
                index.stats["embedding_status"] = "empty"
                return index
            probe_started = time.perf_counter()
            try:
                index._embedder = embedder if embedder is not None else LocalEmbeddingClient(index._config)
            finally:
                index.stats["embedding_provider_setup_seconds"] = time.perf_counter() - probe_started
            identity = getattr(index._embedder, "model_identity", None)
            index._identity = str(identity() if callable(identity) else identity or f"injected:{index._config.model}")
            default_cache = Path(os.getenv("ORGANIZER_HOME") or Path.home() / ".file_organizer") / "embeddings.sqlite"
            index._cache_path = Path(cache_path or os.getenv("ORGANIZER_EMBEDDING_CACHE_DB") or default_cache)
            try:
                cache = _EmbeddingCache(index._cache_path)
            except (OSError, sqlite3.Error):
                index._cache_path = None
                index.warnings.append("Embedding cache is unavailable; semantic vectors will remain in memory.")
            keys = {item_id: _embedding_key(index._identity, "document", text, evidence_by_id[item_id].fingerprint)
                    for item_id, text in cards.items()}
            unique = {keys[item_id]: text for item_id, text in cards.items()}
            index.stats["embedding_unique_inputs"] = len(unique)
            cache_started = time.perf_counter()
            try:
                vectors = {key: value for key in unique if cache is not None and (value := cache.get(key)) is not None}
            except sqlite3.Error:
                cache.close()
                cache = None
                index._cache_path = None
                vectors = {}
                index.warnings.append("Embedding cache could not be read; semantic vectors will remain in memory.")
            index.stats["embedding_cache_hits"] = sum(key in vectors for key in keys.values())
            index.stats["embedding_cache_seconds"] += time.perf_counter() - cache_started
            missing = [key for key in unique if key not in vectors]
            new_vectors = {}
            for start in range(0, len(missing), index._config.batch_size):
                batch = missing[start:start + index._config.batch_size]
                embedded = index._encode([unique[key] for key in batch], "document")
                new_vectors.update(zip(batch, embedded))
                index.stats["embedding_batches"] += 1
            vectors.update(new_vectors)
            if len({len(vector) for vector in vectors.values()}) != 1:
                raise SemanticError("Embedding vectors use inconsistent dimensions.")
            if cache is not None and new_vectors:
                cache_started = time.perf_counter()
                try:
                    cache.put_many(new_vectors)
                except sqlite3.Error:
                    index.warnings.append("Embedding cache could not be updated; semantic vectors remain in memory.")
                index.stats["embedding_cache_seconds"] += time.perf_counter() - cache_started
            index.vectors = {item_id: vectors[key] for item_id, key in keys.items()}
            index.stats.update(embedding_status="ready", embedding_model=index._config.model,
                               embedding_model_identity=index._identity,
                               embedding_documents=len(index.vectors),
                               embedding_dimensions=len(next(iter(index.vectors.values()))))
            return index
        except Exception:
            index.vectors.clear()
            index.stats["embedding_status"] = "unavailable"
            index.warnings.append("Local semantic analysis is unavailable; ordinary evidence-based planning will be used.")
            return index
        finally:
            if cache is not None:
                cache.close()
            index.stats["embedding_seconds"] = time.perf_counter() - started
            client = index._embedder
            if client is not None and hasattr(client, "probe_seconds"):
                index.stats["embedding_provider_probe_seconds"] = client.probe_seconds
                index.stats["embedding_inference_seconds"] = client.inference_seconds
                index.stats["embedding_model_load_seconds"] = client.model_load_seconds
                index.stats["embedding_model_compute_seconds"] = client.model_compute_seconds

    def _encode(self, texts: list[str], task: str) -> list[tuple[float, ...]]:
        method = getattr(self._embedder, "embed", self._embedder)
        if not callable(method):
            raise SemanticError("Invalid local embedding backend.")
        started = time.perf_counter()
        parameters = inspect.signature(method).parameters
        supports_task = "task" in parameters or any(p.kind == p.VAR_KEYWORD for p in parameters.values())
        raw = method(texts, task=task) if supports_task else method(texts)
        self.stats["embedding_inference_seconds"] += time.perf_counter() - started
        if not isinstance(raw, (list, tuple)) or len(raw) != len(texts):
            raise SemanticError("Embedding response did not cover every input.")
        return [_normalize(vector) for vector in raw]

    def candidates(self, file_id: int, limit: int = 8) -> list[tuple[int, float]]:
        if file_id not in self.vectors or limit <= 0:
            return []
        return self._rank(self.vectors[file_id], set(self.vectors) - {file_id}, limit)

    def _rank(self, vector: tuple[float, ...], ids: set[int], limit: int | None) -> list[tuple[int, float]]:
        ranked = [(item_id, max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(vector, self.vectors[item_id])))))
                  for item_id in ids if item_id in self.vectors]
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return ranked if limit is None else ranked[:max(0, limit)]

    def search(self, query: str, *, allowed_ids: set[int] | None = None, limit: int | None = None) -> list[tuple[int, float]]:
        """Rank the complete allowed universe unless an explicit limit is supplied."""
        if not self.vectors or not isinstance(query, str) or not query.strip():
            return []
        if len(query) > self._config.max_input_chars:
            self.warnings.append("Semantic query exceeds its input budget; ordinary rule resolution is required.")
            return []
        ids = set(self.vectors) if allowed_ids is None else set(allowed_ids) & set(self.vectors)
        if not ids:
            return []
        query = query.strip()
        key = _embedding_key(self._identity, "query", query)
        cache = None
        try:
            # Queries may contain personal details even when indexed documents do not.
            persistent = not detect_sensitive(query)
            vector = self._queries.get(key) if persistent else None
            if vector is None and persistent and self._cache_path is not None:
                try:
                    cache = _EmbeddingCache(self._cache_path)
                    vector = cache.get(key)
                except (OSError, sqlite3.Error):
                    if cache is not None:
                        cache.close()
                    cache = None
                    self.warnings.append("Embedding query cache is unavailable; lookup will use the local service.")
            if vector is not None:
                self.stats["embedding_query_cache_hits"] += 1
            else:
                vector = self._encode([query], "query")[0]
                self.stats["embedding_query_calls"] += 1
            if len(vector) != len(next(iter(self.vectors.values()))):
                raise SemanticError("Query embedding dimensions do not match the document index.")
            if persistent:
                self._queries[key] = vector
                if cache is not None:
                    try:
                        cache.put_many({key: vector})
                    except sqlite3.Error:
                        self.warnings.append("Embedding query cache could not be updated; lookup remains valid.")
            return self._rank(vector, ids, limit)
        except Exception:
            self.warnings.append("Local semantic lookup failed; ordinary rule resolution is required.")
            return []
        finally:
            if cache is not None:
                cache.close()
