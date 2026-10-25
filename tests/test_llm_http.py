"""Tests for the direct HTTP client against a real local server (no internet, no keys)."""
import asyncio
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from app.core import llm_http
from app.core.fast_planner import FastConfig, plan_files_fast
from app.models.schemas import FileInfo

SEEN = []          # (path, headers, body) of every request the fake server received
BEHAVIOUR = {"mode": "ok", "delay": 0.0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append((self.path, dict(self.headers), body))
        time.sleep(BEHAVIOUR["delay"])
        mode = BEHAVIOUR["mode"]
        if mode != "ok":
            code = {"rate": 429, "auth": 401, "bad": 400, "boom": 500}[mode]
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b'{"error":"nope"}')
            return
        lines = body["messages"][1]["content"].split("\n")
        rows = [[int(ln.split("|")[0]), 1] for ln in lines]
        out = json.dumps({"choices": [{"message": {"content": json.dumps({"r": rows})}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def start_server():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def files(n):
    return [FileInfo(path=f"/in/f{i}.txt", filename=f"f{i}.txt", extension=".txt", size_kb=1,
                     date_created="2026-01-01", snippet=f"text {i}") for i in range(n)]


MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "0|a.txt|hello"}]


class LLMHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.url = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        SEEN.clear()
        BEHAVIOUR.update(mode="ok", delay=0.0)
        os.environ["DEEPSEEK_API_KEY"] = "sk-test"
        os.environ["GEMINI_API_KEY"] = "g-test"
        os.environ["ORGANIZER_DEEPSEEK_BASE_URL"] = self.url
        os.environ["ORGANIZER_GEMINI_BASE_URL"] = self.url + "/g"

    def call(self, model, params=None):
        return asyncio.run(llm_http.direct_call(model, MSGS, params, timeout=5))

    def test_deepseek_request_shape_thinking_off(self):
        out = self.call("deepseek/deepseek-flash")
        self.assertIn('"r"', out)
        path, headers, body = SEEN[-1]
        self.assertEqual(path, "/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer sk-test")
        self.assertEqual(body["model"], "deepseek-flash")            # provider prefix stripped
        self.assertEqual(body["thinking"], {"type": "disabled"})     # the big latency fix
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_gemini_request_shape(self):
        self.call("gemini/gemini-3.8-flash")
        path, headers, body = SEEN[-1]
        self.assertEqual(path, "/g/chat/completions")
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertNotIn("response_format", body)

    def test_user_params_override_and_remove(self):
        self.call("deepseek/deepseek-flash", {"deepseek": {"thinking": {"type": "enabled"}}})
        self.assertEqual(SEEN[-1][2]["thinking"], {"type": "enabled"})
        self.call("gemini/gemini-3.8-flash", {"gemini": {"reasoning_effort": None}})
        self.assertNotIn("reasoning_effort", SEEN[-1][2])

    def test_error_mapping(self):
        from app.core.fast_planner import _error_kind
        for mode, kind in (("rate", "rate"), ("auth", "fatal"), ("bad", "fatal"), ("boom", "transient")):
            BEHAVIOUR["mode"] = mode
            with self.assertRaises(llm_http.LLMHTTPError) as cm:
                self.call("deepseek/deepseek-flash")
            self.assertEqual(_error_kind(cm.exception), kind, mode)

    def test_missing_key_is_fast_clear_fatal(self):
        del os.environ["DEEPSEEK_API_KEY"]
        from app.core.fast_planner import _error_kind
        with self.assertRaises(llm_http.LLMHTTPError) as cm:
            self.call("deepseek/deepseek-flash")
        self.assertIn("DEEPSEEK_API_KEY", str(cm.exception))
        self.assertEqual(_error_kind(cm.exception), "fatal")

    def test_connection_refused_is_fatal_not_slow(self):
        os.environ["ORGANIZER_DEEPSEEK_BASE_URL"] = "http://127.0.0.1:1"
        t = time.monotonic()
        with self.assertRaises(llm_http.LLMHTTPError) as cm:
            self.call("deepseek/deepseek-flash")
        self.assertLess(time.monotonic() - t, 5)
        self.assertEqual(cm.exception.status_code, 404)

    def test_end_to_end_small_run_through_real_http(self):
        cfg = FastConfig(backoff_floor=0.01, request_timeout=5)
        res = plan_files_fast(files(4), "/out", ["deepseek/deepseek-flash"], ["Docs"], cfg, extract=False)
        self.assertEqual({m.folder for m in res.moves}, {"Docs"})
        self.assertEqual(res.stats["calls"], 1)           # 4 files = exactly ONE request
        self.assertEqual(len(SEEN), 1)

    def test_hedging_returns_at_speed_of_faster_model(self):
        """Slow model (2s) + fast model: total time must track the fast one."""
        os.environ["ORGANIZER_GEMINI_BASE_URL"] = self.url + "/g"
        orig = BEHAVIOUR["delay"]
        real_post = llm_http._post

        def slow_for_gemini(url, headers, data, timeout, label):
            if label == "gemini":
                time.sleep(2.0)
            return real_post(url, headers, data, timeout, label)

        llm_http._post = slow_for_gemini
        try:
            t = time.monotonic()
            res = plan_files_fast(files(4), "/out", ["gemini/slow", "deepseek/fast"], ["Docs"],
                                  FastConfig(request_timeout=5), extract=False)
            elapsed = time.monotonic() - t
        finally:
            llm_http._post = real_post
            BEHAVIOUR["delay"] = orig
        self.assertEqual(len(res.moves), 4)
        self.assertLess(elapsed, 1.0, f"took {elapsed:.2f}s - hedge did not help")

    def test_small_run_all_models_dead_gives_clear_error(self):
        BEHAVIOUR["mode"] = "auth"
        with self.assertRaises(RuntimeError) as cm:
            plan_files_fast(files(4), "/out", ["deepseek/x", "gemini/y"], None,
                            FastConfig(backoff_floor=0.01, request_timeout=5), extract=False)
        self.assertIn("401", str(cm.exception))



class StatsContractTests(unittest.TestCase):
    """Regression: the CLI/API read these keys, so they must exist on every code path."""
    KEYS = {"files", "calls", "rate_limited", "profile", "units", "unclassified_units", "renamed",
            "seconds", "folders", "read_seconds", "llm_seconds"}

    def _run(self, n):
        async def llm(model, messages):
            lines = messages[1]["content"].split("\n")
            if "folder structure" in messages[0]["content"]:
                return json.dumps({"folders": ["Docs"]})
            return json.dumps({"r": [[int(l.split("|")[0]), 1 if "Docs" in messages[0]["content"] else "Docs"]
                                     for l in lines]})
        return plan_files_fast(files(n), "/out", ["m"], ["Docs"], FastConfig(backoff_floor=0.001),
                               acompletion_fn=llm, extract=False)

    def test_tiny_path_has_all_keys(self):
        res = self._run(4)
        self.assertEqual(res.stats["profile"], "tiny")
        self.assertTrue(self.KEYS <= set(res.stats), self.KEYS - set(res.stats))

    def test_balanced_and_fast_paths_have_all_keys(self):
        for n in (60, 400):
            res = self._run(n)
            self.assertTrue(self.KEYS <= set(res.stats), (n, self.KEYS - set(res.stats)))


class AnthropicRequestTests(unittest.TestCase):
    """Regression: claude-haiku-5-5 answers HTTP 400 when `temperature` is present."""

    MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]

    def setUp(self):
        self._old = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "test-key"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = self._old

    def _body(self, user_params=None):
        _url, _headers, data = llm_http.build_request(
            "anthropic/claude-haiku-5-5", self.MSGS, user_params
        )
        return json.loads(data)

    def test_no_temperature_by_default(self):
        body = self._body()
        self.assertNotIn("temperature", body)
        self.assertEqual(body["system"], "sys")
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])

    def test_temperature_can_be_opted_in_for_older_models(self):
        body = self._body({"anthropic": {"temperature": 0}})
        self.assertEqual(body["temperature"], 0)


class TemperatureFallbackTests(unittest.TestCase):
    def test_rejection_check_ignores_unrelated_provider_errors(self):
        from app.core.classifier import temperature_rejected

        self.assertTrue(
            temperature_rejected(Exception("`temperature` is deprecated for this model."))
        )
        self.assertTrue(
            temperature_rejected(Exception("Unsupported parameter: temperature"))
        )
        self.assertFalse(temperature_rejected(Exception("rate limit exceeded")))
        self.assertFalse(temperature_rejected(Exception("invalid api key")))

    def test_classifier_retries_once_without_temperature(self):
        from app.core.classifier import _litellm_completion

        calls = []

        def completion(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise Exception("Unsupported parameter: temperature")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
            )

        fake_litellm = SimpleNamespace(completion=completion)
        with patch.dict(sys.modules, {"litellm": fake_litellm}):
            output = _litellm_completion("provider/model", [{"role": "user", "content": "hi"}])

        self.assertEqual(output, '{"ok": true}')
        self.assertEqual(calls[0]["temperature"], 0.1)
        self.assertNotIn("temperature", calls[1])

    def test_fast_planner_retries_once_without_temperature(self):
        from app.core.fast_planner import _litellm_call

        calls = []

        async def acompletion(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise Exception("temperature is not supported")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
            )

        fake_litellm = SimpleNamespace(acompletion=acompletion)
        with patch.dict(sys.modules, {"litellm": fake_litellm}):
            output = asyncio.run(
                _litellm_call("provider/model", [{"role": "user", "content": "hi"}], {})
            )

        self.assertEqual(output, '{"ok": true}')
        self.assertEqual(calls[0]["temperature"], 0)
        self.assertNotIn("temperature", calls[1])


if __name__ == "__main__":
    unittest.main()

