"""Persisted Provider Studio configuration with backend-only credentials."""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List


PROVIDER_ORDER = (
    "openai",
    "anthropic",
    "gemini",
    "deepseek",
    "groq",
    "openrouter",
    "huggingface",
    "ollama",
)

PROVIDER_SPECS: Dict[str, dict] = {
    "openai": {
        "label": "OpenAI",
        "key_variable": "OPENAI_API_KEY",
        "default_base_url": "https://api.openai.com/v1",
        "requires_key": True,
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "key_variable": "ANTHROPIC_API_KEY",
        "default_base_url": "https://api.anthropic.com",
        "requires_key": True,
    },
    "gemini": {
        "label": "Google Gemini",
        "key_variable": "GEMINI_API_KEY",
        "alternate_key_variables": ("GOOGLE_API_KEY",),
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "requires_key": True,
    },
    "deepseek": {
        "label": "DeepSeek",
        "key_variable": "DEEPSEEK_API_KEY",
        "default_base_url": "https://api.deepseek.com",
        "requires_key": True,
    },
    "groq": {
        "label": "Groq Cloud",
        "key_variable": "GROQ_API_KEY",
        "default_base_url": "https://api.groq.com/openai/v1",
        "requires_key": True,
    },
    "openrouter": {
        "label": "OpenRouter",
        "key_variable": "OPENROUTER_API_KEY",
        "default_base_url": "https://openrouter.ai/api/v1",
        "requires_key": True,
    },
    "huggingface": {
        "label": "Hugging Face Inference",
        "key_variable": "HUGGINGFACE_API_KEY",
        "alternate_key_variables": ("HF_TOKEN",),
        "default_base_url": "https://router.huggingface.co/v1",
        "requires_key": True,
    },
    "ollama": {
        "label": "Ollama (local)",
        "key_variable": None,
        "default_base_url": "http://localhost:11434/v1",
        "requires_key": False,
    },
}

_LOCK = threading.RLock()


class ProviderConfigurationError(ValueError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _crypt32():
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    library.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    library.CryptProtectData.restype = ctypes.wintypes.BOOL
    library.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(ctypes.wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    library.CryptUnprotectData.restype = ctypes.wintypes.BOOL
    return library


def _local_free(pointer) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree(ctypes.cast(pointer, ctypes.c_void_p))


def _blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data)
    value = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return value, buffer


def _protect_secret(secret: str) -> str:
    if os.name != "nt":
        raise ProviderConfigurationError("Secure credential storage requires Windows DPAPI.")
    data_in, _buffer_in = _blob(secret.encode("utf-8"))
    data_out = _DataBlob()
    if not _crypt32().CryptProtectData(
        ctypes.byref(data_in),
        "Sortly provider credential",
        None,
        None,
        None,
        0x1,
        ctypes.byref(data_out),
    ):
        raise ProviderConfigurationError("Windows could not encrypt the provider credential.")
    try:
        encrypted = ctypes.string_at(data_out.pbData, data_out.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        _local_free(data_out.pbData)


def _unprotect_secret(encrypted: str) -> str:
    if os.name != "nt":
        raise ProviderConfigurationError("Secure credential storage requires Windows DPAPI.")
    data_in, _buffer_in = _blob(base64.b64decode(encrypted))
    data_out = _DataBlob()
    if not _crypt32().CryptUnprotectData(
        ctypes.byref(data_in),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(data_out),
    ):
        raise ProviderConfigurationError("Windows could not decrypt the saved provider credential.")
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData).decode("utf-8")
    finally:
        _local_free(data_out.pbData)


def provider_definitions() -> List[dict]:
    return [
        {
            "id": provider,
            "label": PROVIDER_SPECS[provider]["label"],
            "default_base_url": PROVIDER_SPECS[provider]["default_base_url"],
            "requires_key": PROVIDER_SPECS[provider]["requires_key"],
        }
        for provider in PROVIDER_ORDER
    ]


def _settings_path() -> Path:
    root = Path(os.getenv("ORGANIZER_HOME") or Path.home() / ".file_organizer")
    return root / "providers.json"


def _empty_settings() -> dict:
    return {"version": 1, "primary": None, "fallbacks": [], "providers": {}}


def _read_settings() -> dict:
    path = _settings_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_settings()
    if not isinstance(payload, dict):
        return _empty_settings()
    payload.setdefault("version", 1)
    payload.setdefault("primary", None)
    payload.setdefault("fallbacks", [])
    payload.setdefault("providers", {})
    return payload


def _write_settings(settings: dict) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _validate_provider(provider: str) -> None:
    if provider not in PROVIDER_SPECS:
        raise ProviderConfigurationError(f"Unsupported provider: {provider}")


def _key_from_environment(provider: str) -> str:
    spec = PROVIDER_SPECS[provider]
    names = (spec.get("key_variable"),) + tuple(spec.get("alternate_key_variables", ()))
    return next((os.getenv(name) for name in names if name and os.getenv(name)), "")


def _stored_key(settings: dict, provider: str) -> str:
    encrypted = settings.get("providers", {}).get(provider, {}).get("api_key_encrypted")
    return _unprotect_secret(encrypted) if encrypted else ""


def resolve_api_key(settings: dict, provider: str) -> tuple[str, str]:
    stored = _stored_key(settings, provider)
    if stored:
        return stored, "stored"
    environment = _key_from_environment(provider)
    if environment:
        return environment, "environment"
    return "", "missing"


def _public_connection(settings: dict, provider: str) -> dict:
    saved = settings.get("providers", {}).get(provider, {})
    key, source = resolve_api_key(settings, provider)
    return {
        "provider": provider,
        "model": saved.get("model", ""),
        "base_url": saved.get("base_url", ""),
        "has_api_key": bool(key),
        "api_key_source": source,
    }


def public_config(settings: dict | None = None) -> dict:
    settings = settings or _read_settings()
    configured = bool(settings.get("primary"))
    return {
        "configured": configured,
        "primary": settings.get("primary"),
        "fallbacks": list(settings.get("fallbacks", [])),
        "connections": {
            provider: _public_connection(settings, provider)
            for provider in PROVIDER_ORDER
        },
        "providers": provider_definitions(),
    }


def _clean_base_url(base_url: str) -> str:
    base_url = (base_url or "").strip().rstrip("/")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ProviderConfigurationError("Provider endpoint must start with http:// or https://.")
    return base_url


def save_config(primary: str, fallbacks: Iterable[str], connections: Dict[str, dict]) -> dict:
    with _LOCK:
        _validate_provider(primary)
        ordered_fallbacks = list(dict.fromkeys(fallbacks))
        for provider in ordered_fallbacks:
            _validate_provider(provider)
        selected = [primary, *ordered_fallbacks]
        if len(selected) != len(set(selected)):
            raise ProviderConfigurationError("Primary and fallback providers must be unique.")

        existing = _read_settings()
        saved = existing.get("providers", {})
        for provider in selected:
            draft = connections.get(provider, {})
            model = str(draft.get("model", "")).strip()
            if not model:
                raise ProviderConfigurationError(
                    f"Enter a model ID for {PROVIDER_SPECS[provider]['label']}."
                )
            base_url = _clean_base_url(str(draft.get("base_url", "")))
            old = dict(saved.get(provider, {}))
            old.update({"model": model, "base_url": base_url})
            api_key = str(draft.get("api_key", "") or "").strip()
            if api_key:
                old["api_key_encrypted"] = _protect_secret(api_key)
            elif draft.get("clear_api_key"):
                old.pop("api_key_encrypted", None)
            saved[provider] = old

        updated = {
            "version": 1,
            "primary": primary,
            "fallbacks": ordered_fallbacks,
            "providers": saved,
        }
        _write_settings(updated)
        activate_runtime(updated)
        return public_config(updated)


def active_models(settings: dict | None = None) -> List[str]:
    settings = settings or _read_settings()
    primary = settings.get("primary")
    if not primary:
        return []
    providers = [primary, *settings.get("fallbacks", [])]
    models = []
    for provider in providers:
        config = settings.get("providers", {}).get(provider, {})
        model = str(config.get("model", "")).strip()
        if model:
            models.append(f"{provider}/{model}")
    return models


def activate_runtime(settings: dict | None = None) -> List[str]:
    """Apply the selected endpoints and decrypted credentials to this sidecar process."""
    settings = settings or _read_settings()
    models = active_models(settings)
    for provider in [settings.get("primary"), *settings.get("fallbacks", [])]:
        if not provider:
            continue
        spec = PROVIDER_SPECS[provider]
        config = settings.get("providers", {}).get(provider, {})
        base_url = str(config.get("base_url", "")).strip()
        if base_url:
            os.environ[f"ORGANIZER_{provider.upper()}_BASE_URL"] = base_url
        key, source = resolve_api_key(settings, provider)
        if key and spec.get("key_variable"):
            os.environ[spec["key_variable"]] = key
    return models


def test_connection_config(provider: str, draft: dict | None = None) -> dict:
    """Resolve one provider endpoint and credential without mutating stored settings."""
    _validate_provider(provider)
    settings = _read_settings()
    config = dict(settings.get("providers", {}).get(provider, {}))
    draft = draft or {}
    if draft.get("model"):
        config["model"] = str(draft["model"]).strip()
    if "base_url" in draft:
        config["base_url"] = _clean_base_url(str(draft.get("base_url", "")))
    transient_key = str(draft.get("api_key", "") or "").strip()
    if transient_key:
        key = transient_key
    else:
        key, _source = resolve_api_key(settings, provider)
    return {
        "provider": provider,
        "model": str(config.get("model", "")).strip(),
        "base_url": str(config.get("base_url", "")).strip(),
        "api_key": key,
    }
