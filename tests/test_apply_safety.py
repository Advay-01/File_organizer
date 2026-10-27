"""Failure-safety regressions for the Apply and Undo transaction boundary."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import api
from app.core import executor, history
from app.core.executor import UndoLogError
from app.core.planner import Move


class ApplySafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.first = self.root / "first.txt"
        self.second = self.root / "second.txt"
        self.first.write_text("first", encoding="utf-8")
        self.second.write_text("second", encoding="utf-8")
        api.plans.clear()

    def tearDown(self):
        api.plans.clear()
        self.tmp.cleanup()

    def plan(self, plan_id: str = "plan") -> None:
        api.plans[plan_id] = api.StoredPlan(
            self.root,
            [
                Move(
                    str(self.first),
                    str(self.root / "Docs" / "first.txt"),
                    "Docs",
                    "notes",
                ),
                Move(
                    str(self.second),
                    str(self.root / "Docs" / "second.txt"),
                    "Docs",
                    "notes",
                ),
            ],
        )

    @staticmethod
    def request(plan_id: str = "plan") -> api.ApplyRequest:
        return api.ApplyRequest(
            plan_id=plan_id,
            files=[
                api.ApplyFile(id="0", accepted=True, target_path="Docs/Notes"),
                api.ApplyFile(id="1", accepted=True, target_path="Docs/Notes"),
            ],
        )

    @staticmethod
    def response_json(response) -> dict:
        return json.loads(response.body)

    def test_successful_apply_and_undo(self):
        self.plan()

        result = api.apply(self.request())

        self.assertEqual(
            (result["status"], result["moved"], result["requested"], result["failed"]),
            ("success", 2, 2, 0),
        )
        self.assertTrue(result["undo_available"])
        self.assertTrue((self.root / "Docs" / "Notes" / "first.txt").is_file())
        self.assertTrue((self.root / "Docs" / "Notes" / "second.txt").is_file())

        undone = history.undo_last_run(str(self.root))

        self.assertEqual((undone["restored"], undone["status"]), (2, "undone"))
        self.assertTrue(self.first.is_file())
        self.assertTrue(self.second.is_file())
        self.assertFalse((self.root / "Docs").exists())

    def test_overlapping_apply_runs_both_remain_in_the_journal(self):
        first = self.root / "overlap-first.txt"
        second = self.root / "overlap-second.txt"
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        first_loaded = threading.Event()
        allow_first_write = threading.Event()
        real_load = history.load_history_for_update
        load_count = 0
        load_count_lock = threading.Lock()

        def delayed_load(output_root: str) -> list[dict]:
            nonlocal load_count
            entries = real_load(output_root)
            with load_count_lock:
                load_count += 1
                current = load_count
            if current == 1:
                first_loaded.set()
                allow_first_write.wait(timeout=5)
            return entries

        errors: list[BaseException] = []

        def apply(move: Move) -> None:
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    executor.execute_plan([move], str(self.root), dry_run=False)
            except BaseException as error:
                errors.append(error)

        threads = [
            threading.Thread(
                target=apply,
                args=(Move(str(first), str(self.root / "One" / first.name), "One", "x"),),
            ),
            threading.Thread(
                target=apply,
                args=(Move(str(second), str(self.root / "Two" / second.name), "Two", "x"),),
            ),
        ]
        with patch("app.core.history.load_history_for_update", side_effect=delayed_load):
            with patch(
                "app.core.executor.load_history_for_update",
                side_effect=delayed_load,
                create=True,
            ):
                threads[0].start()
                self.assertTrue(first_loaded.wait(timeout=5))
                threads[1].start()
                time.sleep(0.2)
                allow_first_write.set()
                for thread in threads:
                    thread.join(timeout=10)

        self.assertFalse(errors)
        self.assertTrue((self.root / "One" / first.name).is_file())
        self.assertTrue((self.root / "Two" / second.name).is_file())
        runs = history.list_runs(str(self.root))
        self.assertEqual(len(runs), 2)
        self.assertEqual({tuple(run["sample"][0][key] for key in ("from", "to")) for run in runs}, {
            ("overlap-first.txt", "overlap-first.txt"),
            ("overlap-second.txt", "overlap-second.txt"),
        })

    def test_unicode_filename_survives_legacy_console_encoding(self):
        unicode_file = self.root / "résumé_café_测试.txt"
        unicode_file.write_text("Disposable Unicode apply regression.", encoding="utf-8")
        destination = self.root / "Unicode" / "General" / unicode_file.name
        real_print = print

        class LegacyStdout:
            encoding = "cp1252"

            def __init__(self):
                self.buffer = io.StringIO()

            def write(self, value):
                return self.buffer.write(value)

            def flush(self):
                return self.buffer.flush()

        legacy_stdout = LegacyStdout()

        def cp1252_print(*args, **kwargs):
            message = " ".join(str(value) for value in args)
            message.encode("cp1252")
            real_print(*args, **kwargs)

        with contextlib.redirect_stdout(legacy_stdout):
            with patch("builtins.print", side_effect=cp1252_print):
                result = executor.execute_plan(
                    [Move(str(unicode_file), str(destination), "Unicode", "test")],
                    str(self.root),
                    dry_run=False,
                )

        self.assertEqual((result.state, result.moved, result.failed), ("success", 1, 0))
        self.assertFalse(unicode_file.exists())
        self.assertTrue(destination.is_file())
        self.assertEqual(history.list_runs(str(self.root))[0]["files"], 1)

    def test_concurrent_apply_request_is_rejected(self):
        self.plan()
        started = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def blocked_apply(_plan, _request):
            started.set()
            release.wait(timeout=5)
            return {"status": "success"}

        def run_apply() -> None:
            try:
                api.apply(self.request())
            except BaseException as error:
                errors.append(error)

        with patch("app.api._apply_plan", side_effect=blocked_apply):
            thread = threading.Thread(target=run_apply)
            thread.start()
            self.assertTrue(started.wait(timeout=5))
            try:
                with self.assertRaises(api.HTTPException) as caught:
                    api.apply(self.request())
            finally:
                release.set()
            thread.join(timeout=5)

        self.assertFalse(errors)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("Another Apply operation", str(caught.exception.detail))

    def test_undo_log_failure_before_first_move_moves_nothing(self):
        self.plan()
        (self.root / history.UNDO_LOG).write_text("{invalid json", encoding="utf-8")

        with self.assertLogs("app.core.executor", level="ERROR"):
            response = api.apply(self.request())
        payload = self.response_json(response)

        self.assertEqual(response.status_code, 500)
        self.assertEqual((payload["status"], payload["moved"]), ("failed", 0))
        self.assertTrue(self.first.is_file())
        self.assertTrue(self.second.is_file())
        self.assertFalse((self.root / "Docs").exists())
        self.assertFalse(api.plans["plan"].applied)
        self.assertTrue(api.plans["plan"].attempted)

    def test_failure_after_first_move_reports_and_records_only_actual_move(self):
        self.plan()
        real_move = executor.move_file_no_overwrite

        def fail_second(source: Path, destination: Path, **kwargs) -> None:
            if source.name == "second.txt":
                raise PermissionError("locked by another process")
            real_move(source, destination, **kwargs)

        with self.assertLogs("app.core.executor", level="ERROR") as logs:
            with patch("app.core.executor.move_file_no_overwrite", side_effect=fail_second):
                response = api.apply(self.request())

        payload = self.response_json(response)
        self.assertEqual(response.status_code, 207)
        self.assertEqual(
            (payload["status"], payload["moved"], payload["requested"], payload["failed"]),
            ("partial", 1, 2, 1),
        )
        self.assertEqual(payload["failures"][0]["file"], "second.txt")
        self.assertNotIn(str(self.root), payload["detail"])
        self.assertIn("Traceback", "\n".join(logs.output))
        self.assertTrue((self.root / "Docs" / "Notes" / "first.txt").is_file())
        self.assertTrue(self.second.is_file())
        self.assertFalse((self.root / "Docs" / "Notes" / "second.txt").exists())

        runs = history.list_runs(str(self.root))
        self.assertEqual((len(runs), runs[0]["files"], runs[0]["status"]), (1, 1, "partial"))
        self.assertEqual(history.undo_last_run(str(self.root))["restored"], 1)
        self.assertTrue(self.first.is_file())

    def test_undo_log_failure_after_move_keeps_a_recoverable_record(self):
        self.plan()
        real_write = history.write_history
        calls = 0

        def fail_completion_write(output_root: str, entries: list[dict]) -> None:
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise UndoLogError("simulated locked journal")
            real_write(output_root, entries)

        with self.assertLogs("app.core.executor", level="ERROR") as logs:
            with patch("app.core.history.write_history", side_effect=fail_completion_write):
                response = api.apply(self.request())

        payload = self.response_json(response)
        self.assertEqual(response.status_code, 207)
        self.assertEqual((payload["status"], payload["moved"]), ("partial", 1))
        self.assertTrue(payload["undo_available"])
        self.assertIn("Traceback", "\n".join(logs.output))
        self.assertTrue((self.root / "Docs" / "Notes" / "first.txt").is_file())
        self.assertTrue(self.second.is_file())

        # The durable in-progress record plus the filesystem proves the first
        # move completed even though its completion update failed.
        runs = history.list_runs(str(self.root))
        self.assertEqual((runs[0]["files"], runs[0]["status"]), (1, "partial"))
        undone = history.undo_last_run(str(self.root))
        self.assertEqual((undone["restored"], undone["status"]), (1, "undone"))
        self.assertTrue(self.first.is_file())

    def test_already_applied_plan_cannot_be_retried(self):
        self.plan()
        api.apply(self.request())

        with self.assertRaises(api.HTTPException) as caught:
            api.apply(self.request())

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(history.list_runs(str(self.root))[0]["files"], 2)

    def test_partially_applied_plan_cannot_be_retried(self):
        self.plan()
        real_move = executor.move_file_no_overwrite

        def fail_first(source: Path, destination: Path, **kwargs) -> None:
            if source.name == "first.txt":
                raise PermissionError("locked")
            real_move(source, destination, **kwargs)

        with self.assertLogs("app.core.executor", level="ERROR"):
            with patch("app.core.executor.move_file_no_overwrite", side_effect=fail_first):
                first = api.apply(self.request())

        self.assertEqual(self.response_json(first)["status"], "partial")
        with self.assertRaises(api.HTTPException) as caught:
            api.apply(self.request())
        self.assertEqual(caught.exception.status_code, 409)

    def test_stale_operation_is_skipped_and_unchanged_file_moves(self):
        self.plan()
        self.second.unlink()

        response = api.apply(self.request())
        with self.assertRaises(api.HTTPException) as retried:
            api.apply(self.request())

        payload = self.response_json(response)
        self.assertEqual(response.status_code, 207)
        self.assertEqual((payload["moved"], payload["failed"]), (1, 1))
        self.assertIn("reanalysis", payload["failures"][0]["reason"])
        self.assertEqual(retried.exception.status_code, 409)
        self.assertTrue((self.root / "Docs" / "Notes" / "first.txt").is_file())


class ApiErrorBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        api.plans.clear()

    def tearDown(self):
        api.plans.clear()
        self.tmp.cleanup()

    def test_tauri_preflight_allows_private_network_and_json_headers(self):
        client = TestClient(api.app)
        response = client.options(
            "/api/apply",
            headers={
                "Origin": "http://tauri.localhost",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
                "Access-Control-Request-Private-Network": "true",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://tauri.localhost")
        self.assertEqual(response.headers["access-control-allow-private-network"], "true")

    def test_unexpected_error_is_json_and_does_not_expose_details(self):
        source = self.root / "file.txt"
        source.write_text("x", encoding="utf-8")
        api.plans["explode"] = api.StoredPlan(
            self.root,
            [Move(str(source), str(self.root / "Docs" / "file.txt"), "Docs", "x")],
        )
        client = TestClient(api.app, raise_server_exceptions=False)

        with self.assertLogs("app.api", level="ERROR") as logs:
            with patch("app.api.evaluate_plan", side_effect=RuntimeError(f"secret {self.root}")):
                response = client.post(
                    "/api/apply",
                    json={"plan_id": "explode"},
                    headers={"Origin": "http://tauri.localhost"},
                )

        payload = response.json()
        self.assertEqual(response.status_code, 500)
        self.assertIn("unexpected error", payload["detail"])
        self.assertNotIn(str(self.root), response.text)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://tauri.localhost")
        self.assertIn("Traceback", "\n".join(logs.output))
        self.assertTrue(source.is_file())

    def test_undo_reports_source_and_destination_conflict(self):
        source = self.root / "both.txt"
        destination = self.root / "Docs" / "both.txt"
        destination.parent.mkdir()
        source.write_text("source", encoding="utf-8")
        destination.write_text("destination", encoding="utf-8")
        history.write_history(str(self.root), [{
            "state": "completed",
            "moves": [{"src": str(source), "dest": str(destination), "completed": True}],
        }])

        result = api.undo(api.UndoRequest(source=str(self.root)))

        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["restored"], 0)
        self.assertEqual(result["skipped"], [{
            "file": "both.txt",
            "reason": "original location is already occupied",
        }])
        self.assertEqual(source.read_text(encoding="utf-8"), "source")
        self.assertEqual(destination.read_text(encoding="utf-8"), "destination")

    def test_undo_reports_when_source_and_destination_are_both_missing(self):
        source = self.root / "gone.txt"
        destination = self.root / "Docs" / "gone.txt"
        history.write_history(str(self.root), [{
            "state": "completed",
            "moves": [{"src": str(source), "dest": str(destination), "completed": True}],
        }])

        result = api.undo(api.UndoRequest(source=str(self.root)))

        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["restored"], 0)
        self.assertEqual(result["skipped"], [{
            "file": "gone.txt",
            "reason": "file is no longer there",
        }])


if __name__ == "__main__":
    unittest.main()
