"""Direct, dependency-free calls to OpenAI-compatible chat endpoints.

Why not litellm for everything?
  * `import litellm` can take seconds (worst on Windows) before the first request.
  * Defaults matter more than the wrapper: DeepSeek runs with thinking ON at HIGH effort
    unless told otherwise, and that alone makes a 4-file job take many seconds.

Standard library only (urllib + threads), so it also honours HTTP(S)_PROXY and works
without extra installs. Unknown providers (anthropic, bedrock, ...) still go through
litellm, imported lazily by fast_planner only when needed.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import urllib.error
import urllib.request
from typing import Dict, List, Optional


class LLMHTTPError(Exception):
    """Carries `status_code` so the planner can tell rate limits from dead models."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


# params = speed-oriented defaults. Override/disable per provider with ORGANIZER_LLM_PARAMS
# e.g. {"deepseek": {"thinking": {"type": "enabled"}}} or {"gemini": {"reasoning_effort": null}}
PROVIDERS: Dict[str, dict] = {
    "deepseek": {
        "base": "https://api.deepseek.com",
        "keys": ("DEEPSEEK_API_KEY",),
        "params": {"thinking": {"type": "disabled"}},   # default is enabled + high effort
        "json_mode": True,
    },
    "gemini": {
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "keys": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "params": {"reasoning_effort": "low"},          # Gemini 3 cannot fully disable thinking
        "json_mode": False,                             # prompt + lenient parser instead
    },
    "openai": {"base": "https://api.openai.com/v1", "keys": ("OPENAI_API_KEY",), "params": {}, "json_mode": True},
    "groq": {"base": "https://api.groq.com/openai/v1", "keys": ("GROQ_API_KEY",), "params": {}, "json_mode": True},
    "openrouter": {"base": "https://openrouter.ai/api/v1", "keys": ("OPENROUTER_API_KEY",), "params": {}, "json_mode": False},
    "ollama": {"base": "http://localhost:11434/v1", "keys": (), "params": {}, "json_mode": True},
}


def split_model(model: str):
    prov, _, name = model.partition("/")
    return prov.lower(), name


def is_supported(model: str) -> bool:
    prov, name = split_model(model)
    return bool(name) and prov in PROVIDERS


def _base_url(prov: str) -> str:
    override = os.getenv(f"ORGANIZER_{prov.upper()}_BASE_URL")
    if override:
        return override.rstrip("/")
    if prov == "ollama" and os.getenv("OLLAMA_API_BASE"):
        return os.environ["OLLAMA_API_BASE"].rstrip("/") + "/v1"
    return PROVIDERS[prov]["base"]


def build_params(prov: str, user_params: Optional[dict]) -> dict:
    """Provider defaults, then global user params, then per-provider user params.
    A value of None removes a key (lets you restore a provider's own default)."""
    user_params = user_params or {}
    p = dict(PROVIDERS[prov]["params"])
    p.update({k: v for k, v in user_params.items() if k not in PROVIDERS})
    nested = user_params.get(prov)
    if isinstance(nested, dict):
        p.update(nested)
    return {k: v for k, v in p.items() if v is not None}


def build_request(model: str, messages: List[dict], user_params: Optional[dict] = None):
    prov, name = split_model(model)
    spec = PROVIDERS[prov]
    headers = {"Content-Type": "application/json"}
    if spec["keys"]:
        key = next((os.getenv(k) for k in spec["keys"] if os.getenv(k)), None)
        if not key:
            raise LLMHTTPError(f"{spec['keys'][0]} is not set (needed for {model})", 401)
        headers["Authorization"] = f"Bearer {key.strip()}"
    body = {"model": name, "messages": messages, "temperature": 0, "stream": False}
    if spec["json_mode"]:
        body["response_format"] = {"type": "json_object"}
    body.update(build_params(prov, user_params))
    return f"{_base_url(prov)}/chat/completions", headers, json.dumps(body).encode()


def _post(url: str, headers: dict, data: bytes, timeout: float, label: str) -> str:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise LLMHTTPError(f"{label} HTTP {e.code}: {detail}", e.code) from None
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise LLMHTTPError(f"{label} timed out", None) from None
        raise LLMHTTPError(f"cannot connect to {label} ({reason})", 404) from None  # dead for now
    except (socket.timeout, TimeoutError):
        raise LLMHTTPError(f"{label} timed out", None) from None
    try:
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise ValueError(f"{label}: unexpected response shape: {str(payload)[:200]}") from None


def _set(fut: "asyncio.Future", result, error) -> None:
    if fut.done():
        return
    fut.set_exception(error) if error else fut.set_result(result)


async def _in_daemon_thread(fn, *args):
    """Run blocking `fn` in a daemon thread. Unlike run_in_executor, an abandoned request
    (the loser of a hedged race) never delays interpreter or event-loop shutdown."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def run():
        try:
            res, err = fn(*args), None
        except BaseException as e:  # noqa: BLE001 - forwarded to the awaiting coroutine
            res, err = None, e
        try:
            loop.call_soon_threadsafe(_set, fut, res, err)
        except RuntimeError:
            pass  # loop already closed: nobody is waiting any more

    threading.Thread(target=run, daemon=True).start()
    return await fut


async def direct_call(model: str, messages: List[dict], user_params: Optional[dict] = None,
                      timeout: float = 90.0) -> str:
    url, headers, data = build_request(model, messages, user_params)
    return await _in_daemon_thread(_post, url, headers, data, timeout, model.split("/")[0])

