"""Tests for the PyInstaller-compatible local API entry point."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI

from app import desktop_server


class ArgumentsTests(unittest.TestCase):
    def test_defaults_bind_only_to_loopback(self):
        args = desktop_server.parse_args([])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8765)
        self.assertEqual(args.log_level, "info")
        self.assertIsNone(args.env_file)

    def test_non_loopback_host_is_rejected(self):
        with self.assertRaises(SystemExit):
            desktop_server.parse_args(["--host", "0.0.0.0"])

    def test_explicit_loopback_and_port_are_accepted(self):
        args = desktop_server.parse_args(["--host", "::1", "--port", "9000"])
        self.assertEqual(args.host, "::1")
        self.assertEqual(args.port, 9000)

    def test_stdio_uses_utf8_replacement_when_available(self):
        class Stream:
            def __init__(self):
                self.options = None

            def reconfigure(self, **options):
                self.options = options

        stdout = Stream()
        stderr = Stream()
        with patch.object(desktop_server.sys, "stdout", stdout):
            with patch.object(desktop_server.sys, "stderr", stderr):
                desktop_server._configure_stdio()

        self.assertEqual(stdout.options, {"encoding": "utf-8", "errors": "replace"})
        self.assertEqual(stderr.options, {"encoding": "utf-8", "errors": "replace"})


class ConfigurationTests(unittest.TestCase):
    def test_env_file_loads_without_overriding_process_values(self):
        key = "SORTLY_DESKTOP_SERVER_TEST_VALUE"
        os.environ.pop(key, None)
        try:
            with tempfile.TemporaryDirectory() as directory:
                env_file = Path(directory) / "sidecar.env"
                env_file.write_text(f"{key}=from-file\n", encoding="utf-8")
                desktop_server._load_env_file(env_file)
                self.assertEqual(os.environ[key], "from-file")
        finally:
            os.environ.pop(key, None)

    def test_missing_env_file_fails_clearly(self):
        with self.assertRaises(FileNotFoundError):
            desktop_server._load_env_file(Path("does-not-exist.env"))

    def test_shutdown_route_requires_its_token(self):
        class Server:
            should_exit = False

        application = FastAPI()
        server = Server()
        desktop_server._install_shutdown_route(application, server, "test-token")
        route = next(route for route in application.routes if route.path == "/api/shutdown")

        with self.assertRaises(Exception):
            route.endpoint(None)
        with self.assertRaises(Exception):
            route.endpoint("wrong-token")
        self.assertEqual(route.endpoint("test-token"), {"stopping": True})
        self.assertTrue(server.should_exit)


class EntryPointTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows SIGBREAK handling")
    @patch("app.desktop_server.signal.signal")
    def test_windows_break_requests_graceful_shutdown(self, signal_handler):
        class Server:
            should_exit = False

        server = Server()
        desktop_server._install_windows_break_handler(server)
        callback = signal_handler.call_args.args[1]
        callback(desktop_server.signal.SIGBREAK, None)

        self.assertTrue(server.should_exit)
        signal_handler.assert_called_once()

    @patch("app.desktop_server.multiprocessing.freeze_support")
    @patch("app.desktop_server.build_server")
    @patch("app.desktop_server._install_windows_break_handler")
    def test_main_runs_server_with_parsed_options(
        self, install_break_handler, build_server, freeze_support
    ):
        server = build_server.return_value
        server.started = True

        result = desktop_server.main(["--port", "9001", "--log-level", "warning"])

        self.assertEqual(result, 0)
        freeze_support.assert_called_once_with()
        build_server.assert_called_once_with("127.0.0.1", 9001, "warning")
        install_break_handler.assert_called_once_with(server)
        server.run.assert_called_once_with()

    @patch("app.desktop_server.multiprocessing.freeze_support")
    @patch("app.desktop_server.build_server")
    @patch("app.desktop_server._install_windows_break_handler")
    def test_startup_failure_returns_nonzero(
        self, _install_break_handler, build_server, _freeze_support
    ):
        server = build_server.return_value
        server.started = False

        result = desktop_server.main([])

        self.assertEqual(result, 1)
        server.run.assert_called_once_with()

    @patch("app.desktop_server.multiprocessing.freeze_support")
    @patch("app.folder_picker.main")
    def test_frozen_folder_picker_module_call_is_supported(self, picker_main, freeze_support):
        result = desktop_server.main(["-m", "app.folder_picker"])

        self.assertEqual(result, 0)
        freeze_support.assert_called_once_with()
        picker_main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
