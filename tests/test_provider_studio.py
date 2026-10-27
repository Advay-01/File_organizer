"""Provider Studio persistence, discovery, and protocol tests."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from app.core import llm_http, provider_catalog, provider_store


KEY_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "HUGGINGFACE_API_KEY",
    "HF_TOKEN",
    "ORGANIZER_ANTHROPIC_BASE_URL",
    "ORGANIZER_GEMINI_BASE_URL",
    "ORGANIZER_OPENAI_BASE_URL",
    "ORGANIZER_DEEPSEEK_BASE_URL",
    "ORGANIZER_HUGGINGFACE_BASE_URL",
    "ORGANIZER_OLLAMA_BASE_URL",
)


class ProviderStoreTests(unittest.TestCase):
    def setUp(self):
        if sys.platform != 'win32':
            from unittest.mock import MagicMock
            secrets = {}
            backend = MagicMock()
            backend.get_password.side_effect = lambda service, account: secrets.get((service, account))
            backend.set_password.side_effect = lambda service, account, value: secrets.__setitem__((service, account), value)
            replacement = patch.object(provider_store, '_native_keyring', return_value=backend)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.saved_environment = {
            name: os.environ.get(name) for name in (*KEY_ENV_VARS, "ORGANIZER_HOME")
        }
        for name in KEY_ENV_VARS:
            os.environ.pop(name, None)
        os.environ["ORGANIZER_HOME"] = self.temp.name

    def tearDown(self):
        for name, value in self.saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temp.cleanup()

    def test_secret_persists_encrypted_and_never_appears_in_public_config(self):
        secret = "dpapi-test-secret-value"
        public = provider_store.save_config(
            "deepseek",
            [],
            {
                "deepseek": {
                    "model": "deepseek-chat",
                    "base_url": "",
                    "api_key": secret,
                }
            },
        )

        stored_text = (Path(self.temp.name) / "providers.json").read_text(encoding="utf-8")
        self.assertNotIn(secret, stored_text)
        self.assertNotIn("api_key", public["connections"]["deepseek"])
        self.assertTrue(public["connections"]["deepseek"]["has_api_key"])
        self.assertEqual(public["connections"]["deepseek"]["api_key_source"], "stored")

        reloaded = provider_store.public_config()
        self.assertEqual(reloaded["primary"], "deepseek")
        self.assertEqual(
            provider_store.active_models(provider_store._read_settings()),
            ["deepseek/deepseek-chat"],
        )
        key, source = provider_store.resolve_api_key(provider_store._read_settings(), "deepseek")
        self.assertEqual((key, source), (secret, "stored"))

    def test_primary_and_explicit_fallback_order_are_exact(self):
        provider_store.save_config(
            "deepseek",
            ["openai"],
            {
                "deepseek": {"model": "deepseek-chat", "api_key": "deepseek-test"},
                "openai": {"model": "user-selected-model", "api_key": "openai-test"},
            },
        )
        self.assertEqual(
            provider_store.active_models(),
            ["deepseek/deepseek-chat", "openai/user-selected-model"],
        )
        self.assertNotIn("gemini/gemini-2.5-flash", provider_store.active_models())
        self.assertNotIn("ollama/llama3.2", provider_store.active_models())

    def test_duplicate_provider_configuration_is_rejected(self):
        with self.assertRaises(provider_store.ProviderConfigurationError):
            provider_store.save_config(
                "deepseek",
                ["deepseek"],
                {"deepseek": {"model": "deepseek-chat", "api_key": "test"}},
            )

    def test_missing_model_is_rejected(self):
        with self.assertRaises(provider_store.ProviderConfigurationError):
            provider_store.save_config(
                "ollama",
                [],
                {"ollama": {"model": "", "base_url": ""}},
            )

    @patch("app.core.provider_catalog.llm_http.direct_call_sync")
    def test_active_connection_test_uses_primary_then_fallbacks(self, call):
        call.return_value = "OK"
        provider_store.save_config(
            "deepseek",
            ["groq"],
            {
                "deepseek": {"model": "deepseek-chat", "api_key": "deepseek-test"},
                "groq": {"model": "user-model", "api_key": "groq-test"},
            },
        )

        results = provider_catalog.test_active_config()

        self.assertEqual([result["ok"] for result in results], [True, True])
        self.assertEqual(
            [args.args[0] for args in call.call_args_list],
            ["deepseek/deepseek-chat", "groq/user-model"],
        )

    def test_fastapi_validation_errors_do_not_echo_credentials(self):
        from fastapi.testclient import TestClient
        from app.api import app

        secret = "must-never-be-echoed"
        response = TestClient(app).post(
            "/api/providers/config",
            json={
                "primary": "deepseek",
                "fallbacks": [],
                "connections": {
                    "deepseek": {
                        "model": "deepseek-chat",
                        "api_key": {"secret": secret},
                    }
                },
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved_environment = {
            name: os.environ.get(name) for name in (*KEY_ENV_VARS, "ORGANIZER_HOME")
        }
        for name in KEY_ENV_VARS:
            os.environ.pop(name, None)
        os.environ["ORGANIZER_HOME"] = self.temp.name

    def tearDown(self):
        for name, value in self.saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temp.cleanup()

    @patch("app.core.provider_catalog._request_json")
    def test_openai_compatible_discovery(self, request):
        request.return_value = {"data": [{"id": "model-a"}, {"id": "model-b"}]}
        result = provider_catalog.discover_models(
            "openai",
            {"model": "x", "base_url": "https://example.test/v1", "api_key": "test-key"},
        )
        self.assertEqual(result["models"], ["model-a", "model-b"])
        self.assertFalse(result["verified"])
        self.assertEqual(request.call_args.args[0], "https://example.test/v1/models")

    @patch("app.core.provider_catalog._request_json")
    def test_gemini_discovery(self, request):
        request.return_value = {"models": [{"name": "models/gemini-test"}]}
        result = provider_catalog.discover_models(
            "gemini",
            {
                "model": "gemini-test",
                "base_url": "https://example.test/v1beta/openai",
                "api_key": "test-key",
            },
        )
        self.assertEqual(result["models"], ["gemini-test"])
        self.assertEqual(request.call_args.args[0], "https://example.test/v1beta/models")

    @patch("app.core.provider_catalog._request_json")
    def test_anthropic_discovery(self, request):
        request.return_value = {"data": [{"id": "claude-test"}]}
        result = provider_catalog.discover_models(
            "anthropic",
            {
                "model": "claude-test",
                "base_url": "https://example.test",
                "api_key": "test-key",
            },
        )
        self.assertEqual(result["models"], ["claude-test"])
        self.assertEqual(request.call_args.args[0], "https://example.test/v1/models")
        self.assertEqual(request.call_args.args[1]["anthropic-version"], "2023-06-01")

    @patch("app.core.provider_catalog._request_json")
    def test_ollama_discovery(self, request):
        request.return_value = {"models": [{"model": "local-model:latest"}]}
        result = provider_catalog.discover_models(
            "ollama",
            {"model": "local-model:latest", "base_url": "http://localhost:11434/v1"},
        )
        self.assertEqual(result["models"], ["local-model:latest"])
        self.assertEqual(request.call_args.args[0], "http://localhost:11434/api/tags")

    def test_cloud_discovery_without_key_is_actionable(self):
        with self.assertRaises(provider_store.ProviderConfigurationError) as context:
            provider_catalog.discover_models("deepseek")
        self.assertIn("DEEPSEEK_API_KEY", str(context.exception))


class ProtocolServer:
    def __init__(self, response_payload, status=200):
        self.response_payload = response_payload
        self.status = status
        self.seen = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.seen.append((self.path, dict(self.headers), body))
                data = json.dumps(owner.response_payload).encode()
                self.send_response(owner.status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()


class DirectProtocolTests(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.get(name) for name in KEY_ENV_VARS}
        for name in KEY_ENV_VARS:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_anthropic_native_request(self):
        server = ProtocolServer(
            {"content": [{"type": "text", "text": "anthropic-ok"}]}
        )
        try:
            os.environ["ANTHROPIC_API_KEY"] = "anthropic-test-key"
            os.environ["ORGANIZER_ANTHROPIC_BASE_URL"] = server.url
            output = llm_http.direct_call_sync(
                "anthropic/test-model",
                [
                    {"role": "system", "content": "system instruction"},
                    {"role": "user", "content": "hello"},
                ],
                extra_body={"max_tokens": 16},
            )
            path, headers, body = server.seen[-1]
            self.assertEqual(output, "anthropic-ok")
            self.assertEqual(path, "/v1/messages")
            self.assertEqual(
                headers.get("x-api-key") or headers.get("X-Api-Key"),
                "anthropic-test-key",
            )
            self.assertEqual(
                headers.get("anthropic-version") or headers.get("Anthropic-Version"),
                "2023-06-01",
            )
            self.assertEqual(body["system"], "system instruction")
            self.assertEqual(body["max_tokens"], 16)
            self.assertNotIn("response_format", body)
        finally:
            server.close()

    def test_openai_compatible_provider_credentials_and_endpoints(self):
        providers = {
            "openai": "OPENAI_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "groq": "GROQ_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }
        server = ProtocolServer(
            {"choices": [{"message": {"content": "compatible-ok"}}]}
        )
        try:
            for provider, variable in providers.items():
                with self.subTest(provider=provider):
                    os.environ[variable] = f"{provider}-test-key"
                    os.environ[f"ORGANIZER_{provider.upper()}_BASE_URL"] = server.url
                    output = llm_http.direct_call_sync(
                        f"{provider}/user-selected-model",
                        [{"role": "user", "content": "hello"}],
                    )
                    path, headers, body = server.seen[-1]
                    self.assertEqual(output, "compatible-ok")
                    self.assertEqual(path, "/chat/completions")
                    self.assertEqual(
                        headers.get("Authorization"),
                        f"Bearer {provider}-test-key",
                    )
                    self.assertEqual(body["model"], "user-selected-model")
        finally:
            server.close()

    def test_ollama_request_needs_no_cloud_key(self):
        server = ProtocolServer(
            {"choices": [{"message": {"content": "ollama-ok"}}]}
        )
        try:
            os.environ["ORGANIZER_OLLAMA_BASE_URL"] = server.url
            output = llm_http.direct_call_sync(
                "ollama/local-model:latest",
                [{"role": "user", "content": "hello"}],
            )
            path, headers, body = server.seen[-1]
            self.assertEqual(output, "ollama-ok")
            self.assertEqual(path, "/chat/completions")
            self.assertNotIn("Authorization", headers)
            self.assertEqual(body["model"], "local-model:latest")
        finally:
            server.close()

    def test_huggingface_router_request(self):
        server = ProtocolServer(
            {"choices": [{"message": {"content": "router-ok"}}]}
        )
        try:
            os.environ["HUGGINGFACE_API_KEY"] = "hf-test-key"
            os.environ["ORGANIZER_HUGGINGFACE_BASE_URL"] = server.url
            output = llm_http.direct_call_sync(
                "huggingface/org/model",
                [{"role": "user", "content": "hello"}],
            )
            path, headers, body = server.seen[-1]
            self.assertEqual(output, "router-ok")
            self.assertEqual(path, "/chat/completions")
            self.assertEqual(headers["Authorization"], "Bearer hf-test-key")
            self.assertEqual(body["model"], "org/model")
        finally:
            server.close()

    def test_huggingface_legacy_text_generation_request(self):
        server = ProtocolServer([{"generated_text": "legacy-ok"}])
        try:
            os.environ["HUGGINGFACE_API_KEY"] = "hf-test-key"
            os.environ["ORGANIZER_HUGGINGFACE_BASE_URL"] = (
                f"{server.url}/models/org/text-model"
            )
            output = llm_http.direct_call_sync(
                "huggingface/org/text-model",
                [{"role": "user", "content": "hello"}],
                extra_body={"max_tokens": 12},
            )
            path, headers, body = server.seen[-1]
            self.assertEqual(output, "legacy-ok")
            self.assertEqual(path, "/models/org/text-model")
            self.assertEqual(headers["Authorization"], "Bearer hf-test-key")
            self.assertEqual(body["parameters"]["max_new_tokens"], 12)
            self.assertIn("user: hello", body["inputs"])
        finally:
            server.close()

    def test_unsupported_huggingface_response_is_clear(self):
        server = ProtocolServer({"error": "Unsupported task"})
        try:
            os.environ["HUGGINGFACE_API_KEY"] = "hf-test-key"
            os.environ["ORGANIZER_HUGGINGFACE_BASE_URL"] = (
                f"{server.url}/models/org/unsupported"
            )
            with self.assertRaises(ValueError) as context:
                llm_http.direct_call_sync(
                    "huggingface/org/unsupported",
                    [{"role": "user", "content": "hello"}],
                )
            self.assertIn("no compatible text-generation result", str(context.exception))
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
