"""Embedding preferences and explicit model downloads through local Ollama.

No weights or GPU runtimes are bundled. Only the two supported local models can
be downloaded, and no discovery/settings request ever starts a download.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import re
from threading import Lock, Thread
import time
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from app.core.semantic_index import SemanticConfig, _local_base_url, _NoRedirect

MODELS = (
    {"id": "all-minilm:22m", "label": "MiniLM", "download_size": "About 46 MB",
     "description": "Lightweight default for CPU laptops."},
    {"id": "qwen3-embedding:0.6b", "label": "Qwen 0.6B", "download_size": "About 639 MB",
     "description": "Higher-cost semantic model; benefits from supported GPU acceleration."},
)
SUPPORTED_MODELS = {model["id"] for model in MODELS}
_SETTINGS_LOCK = Lock()
_DOWNLOAD_LOCK = Lock()
_download: dict | None = None


class EmbeddingSettingsError(ValueError):
    pass


def _path() -> Path:
    return Path(os.getenv("ORGANIZER_HOME") or Path.home() / ".file_organizer") / "embeddings.json"


def _validate(data: dict) -> dict:
    if (not isinstance(data, dict) or type(data.get("enabled")) is not bool
            or not isinstance(data.get("model"), str) or data.get("model") not in SUPPORTED_MODELS
            or not isinstance(data.get("device"), str) or data.get("device") not in {"auto", "cpu"}):
        raise EmbeddingSettingsError("Choose MiniLM or Qwen and automatic acceleration or CPU only.")
    return {"enabled": data["enabled"], "model": data["model"], "device": data["device"]}


def runtime_config() -> SemanticConfig:
    base = SemanticConfig.from_env()
    if base.model not in SUPPORTED_MODELS:
        base = replace(base, model='all-minilm:22m')
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return base
    except (OSError, ValueError) as error:
        raise EmbeddingSettingsError("Embedding settings could not be read. Check the local settings file.") from error
    if not isinstance(data, dict) or data.get("version") != 1:
        raise EmbeddingSettingsError("The saved embedding settings version is unsupported.")
    return replace(base, **_validate(data))


def save_preferences(enabled: bool, model: str, device: str) -> dict:
    values = _validate({"enabled": enabled, "model": model, "device": device})
    path = _path()
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    with _SETTINGS_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump({"version": 1, **values}, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise EmbeddingSettingsError("Embedding settings could not be saved; previous settings are unchanged.") from error
    return values


def _transport(config: SemanticConfig):
    return _local_base_url(config.base_url), build_opener(ProxyHandler({}), _NoRedirect())


def _json_request(config: SemanticConfig, path: str) -> dict:
    base, opener = _transport(config)
    with opener.open(Request(base + path), timeout=2) as response:
        raw = response.read(512 * 1024 + 1)
    if len(raw) > 512 * 1024:
        raise EmbeddingSettingsError("The local model service returned an oversized response.")
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise EmbeddingSettingsError("The local model service returned invalid model information.")
    return data


def _local_model(row: dict) -> bool:
    return (isinstance(row, dict) and not row.get("remote_host") and not row.get("remote_model")
            and isinstance(row.get("digest"), str)
            and re.fullmatch(r'(?:sha256:)?[a-fA-F0-9]{64}', row['digest']) is not None)


def download_status() -> dict | None:
    with _DOWNLOAD_LOCK:
        return dict(_download) if _download else None


def public_state() -> dict:
    config = runtime_config()
    installed, running, available, message = {}, {}, False, (
        "Ollama is unavailable. Start or install it for local embeddings; Sortly can still use your configured LLM."
    )
    try:
        tags = _json_request(config, '/api/tags')
        installed = {row.get('name', row.get('model')): row for row in tags['models'] if _local_model(row)}
        available, message = True, "Ollama is available. Model files stay outside the Sortly installer."
        try:
            running = {row.get('name', row.get('model')): row for row in _json_request(config, '/api/ps')['models']
                       if isinstance(row, dict)}
        except Exception:
            message = "Ollama is available; acceleration status could not be read."
    except Exception:
        pass  # Report availability explicitly; never change the saved preference.
    catalog = []
    for model in MODELS:
        active = running.get(model['id'])
        vram = active.get('size_vram') if active else None
        acceleration = ('gpu' if isinstance(vram, (int, float)) and not isinstance(vram, bool) and vram > 0
                        else 'cpu' if vram == 0 else 'not_loaded')
        catalog.append({**model, "installed": model['id'] in installed, "acceleration": acceleration})
    return {"enabled": config.enabled, "model": config.model, "device": config.device,
            "min_files": config.min_files, "service_available": available, "service_message": message,
            "models": catalog, "download": download_status()}


def _update_download(job_id: str, **values) -> None:
    with _DOWNLOAD_LOCK:
        if _download and _download['id'] == job_id:
            _download.update(values)


def _pull_model(job_id: str, model: str, config: SemanticConfig) -> None:
    try:
        base, opener = _transport(config)
        request = Request(base + '/api/pull', data=json.dumps({"model": model, "stream": True}).encode(),
                          headers={"Content-Type": "application/json"})
        started, layers, success = time.monotonic(), {}, False
        with opener.open(request, timeout=30) as response:
            while True:
                if time.monotonic() - started > 30 * 60:
                    raise EmbeddingSettingsError("The model download exceeded its time budget.")
                line = response.readline(16385)
                if not line:
                    break
                if len(line) > 16384:
                    raise EmbeddingSettingsError("Invalid model-download progress.")
                event = json.loads(line)
                if not isinstance(event, dict) or event.get('error'):
                    raise EmbeddingSettingsError("The model download failed.")
                digest, total, completed = event.get('digest'), event.get('total'), event.get('completed')
                if (isinstance(digest, str) and type(total) is int and type(completed) is int
                        and 0 <= completed <= total <= 10 * 1024**3 and len(layers) < 64):
                    layers[digest] = (total, completed)
                _update_download(job_id, total=sum(v[0] for v in layers.values()),
                                 completed=sum(v[1] for v in layers.values()))
                if event.get('status') == 'success':
                    success = True
                    break
        tags = _json_request(config, '/api/tags') if success else {"models": []}
        if not any(_local_model(row) and row.get('name', row.get('model')) == model for row in tags['models']):
            raise EmbeddingSettingsError("Downloaded model could not be verified locally.")
        _update_download(job_id, state='completed', message='Download verified. The model is installed locally.')
    except Exception:
        _update_download(job_id, state='failed', message='Download failed. Check Ollama and your connection, then retry.')


def start_download(model: str) -> dict:
    if model not in SUPPORTED_MODELS:
        raise EmbeddingSettingsError("Only MiniLM and Qwen embedding downloads are supported.")
    config = runtime_config()
    _transport(config)  # Validate the loopback boundary before creating a worker.
    global _download
    with _DOWNLOAD_LOCK:
        if _download and _download['state'] == 'downloading':
            if _download['model'] != model:
                raise EmbeddingSettingsError("Another embedding model download is already running.")
            return dict(_download)
        _download = {"id": uuid4().hex, "model": model, "state": "downloading",
                     "total": 0, "completed": 0, "message": "Downloading through local Ollama…"}
        result = dict(_download)
    Thread(target=_pull_model, args=(result['id'], model, config), daemon=True).start()
    return result
