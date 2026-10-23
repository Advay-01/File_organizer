"""Provider model discovery and active-configuration connection tests."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import List

from app.core import llm_http, provider_store


def _request_json(url: str, headers: dict, timeout: float = 30.0) -> dict | list:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500]
        raise llm_http.LLMHTTPError(
            f"Provider model discovery returned HTTP {error.code}: {detail}",
            error.code,
        ) from None
    except urllib.error.URLError as error:
        raise llm_http.LLMHTTPError(
            f"Could not reach the provider model endpoint: {error.reason}",
            404,
        ) from None


def _base_or_default(config: dict) -> str:
    provider = config["provider"]
    return (
        config.get("base_url")
        or provider_store.PROVIDER_SPECS[provider]["default_base_url"]
    ).rstrip("/")


def _require_key(config: dict) -> str:
    provider = config["provider"]
    key = config.get("api_key") or ""
    if provider_store.PROVIDER_SPECS[provider]["requires_key"] and not key:
        variable = provider_store.PROVIDER_SPECS[provider]["key_variable"]
        raise provider_store.ProviderConfigurationError(
            f"Add an API key for {provider_store.PROVIDER_SPECS[provider]['label']} "
            f"(expected variable: {variable})."
        )
    return key


def _openai_models(config: dict) -> List[str]:
    key = _require_key(config)
    url = f"{_base_or_default(config)}/models"
    payload = _request_json(url, {"Authorization": f"Bearer {key}"})
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    return [
        str(row["id"])
        for row in rows
        if isinstance(row, dict) and row.get("id")
    ]


def _gemini_models(config: dict) -> List[str]:
    key = _require_key(config)
    base = _base_or_default(config)
    if base.endswith("/openai"):
        base = base[: -len("/openai")]
    payload = _request_json(f"{base}/models", {"x-goog-api-key": key})
    rows = payload.get("models", []) if isinstance(payload, dict) else []
    return [
        str(row["name"]).removeprefix("models/")
        for row in rows
        if isinstance(row, dict) and row.get("name")
    ]


def _anthropic_models(config: dict) -> List[str]:
    key = _require_key(config)
    payload = _request_json(
        f"{_base_or_default(config)}/v1/models",
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    return [
        str(row["id"])
        for row in rows
        if isinstance(row, dict) and row.get("id")
    ]


def _huggingface_models(config: dict) -> List[str]:
    key = _require_key(config)
    query = urllib.parse.urlencode(
        {
            "filter": "text-generation",
            "inference": "warm",
            "sort": "downloads",
            "direction": "-1",
            "limit": "100",
        }
    )
    payload = _request_json(
        f"https://huggingface.co/api/models?{query}",
        {"Authorization": f"Bearer {key}"},
    )
    rows = payload if isinstance(payload, list) else []
    return [
        str(row["id"])
        for row in rows
        if isinstance(row, dict) and row.get("id")
    ]


def _ollama_models(config: dict) -> List[str]:
    base = _base_or_default(config)
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    try:
        payload = _request_json(f"{base}/api/tags", {})
    except llm_http.LLMHTTPError as error:
        raise llm_http.LLMHTTPError(
            "Ollama is unavailable. Start it with `ollama serve` and try again. "
            f"Details: {error}",
            error.status_code,
        ) from None
    rows = payload.get("models", []) if isinstance(payload, dict) else []
    return [
        str(row.get("model") or row.get("name"))
        for row in rows
        if isinstance(row, dict) and (row.get("model") or row.get("name"))
    ]


def discover_models(provider: str, draft: dict | None = None) -> dict:
    config = provider_store.test_connection_config(provider, draft)
    if provider == "gemini":
        models = _gemini_models(config)
    elif provider == "anthropic":
        models = _anthropic_models(config)
    elif provider == "huggingface":
        models = _huggingface_models(config)
    elif provider == "ollama":
        models = _ollama_models(config)
    else:
        models = _openai_models(config)
    return {
        "provider": provider,
        "models": list(dict.fromkeys(model for model in models if model))[:250],
        "verified": False,
        "source": "provider_api",
    }


def test_active_config() -> list:
    settings = provider_store._read_settings()
    models = provider_store.active_models(settings)
    if not models:
        raise provider_store.ProviderConfigurationError(
            "No active Provider Studio configuration has been saved."
        )
    provider_store.activate_runtime(settings)
    messages = [
        {
            "role": "user",
            "content": 'Return exactly {"ok":true} as JSON and nothing else.',
        }
    ]
    results = []
    for model in models:
        provider, model_id = llm_http.split_model(model)
        try:
            response = llm_http.direct_call_sync(
                model,
                messages,
                timeout=30,
                extra_body={"max_tokens": 256},
            )
            results.append(
                {
                    "provider": provider,
                    "model": model_id,
                    "ok": True,
                    "response": response.strip()[:80],
                }
            )
        except Exception as error:
            results.append(
                {
                    "provider": provider,
                    "model": model_id,
                    "ok": False,
                    "error": str(error)[:600],
                }
            )
    return results
