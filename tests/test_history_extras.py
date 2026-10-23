"""Folder registry, CLI history commands, and the API router (against a stub FastAPI)."""
import contextlib
import importlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

from app.core import history
from app.core.executor import execute_plan
from app.core.planner import Move


def apply(root: Path, spec):
    moves = []
    for name, folder in spec:
        (root / name).write_text(name)
        moves.append(Move(str(root / name), str(root / folder / name), folder, "t"))
    with contextlib.redirect_stdout(io.StringIO()):
        execute_plan(moves, str(root), dry_run=False)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        os.environ["ORGANIZER_HOME"] = str(self.home)
        self.a, self.b = Path(self.tmp.name) / "a", Path(self.tmp.name) / "b"
        self.a.mkdir(), self.b.mkdir()

    def tearDown(self):
        os.environ.pop("ORGANIZER_HOME", None)
        self.tmp.cleanup()

    def test_remember_dedupes_orders_and_drops_missing(self):
        history.remember_folder(str(self.a))
        history.remember_folder(str(self.b))
        history.remember_folder(str(self.a))                      # moves to the front, no duplicate
        self.assertEqual(history.known_folders(), [str(self.a.resolve()), str(self.b.resolve())])
        self.b.rmdir()
        self.assertEqual(history.known_folders(), [str(self.a.resolve())])

    def test_all_runs_spans_folders_newest_first(self):
        apply(self.a, [("x.txt", "AI")])
        apply(self.b, [("y.txt", "DSA")])
        history.remember_folder(str(self.a))
        history.remember_folder(str(self.b))
        runs = history.all_runs()
        self.assertEqual({r["folder"] for r in runs}, {str(self.a.resolve()), str(self.b.resolve())})
        self.assertEqual([r["time"] for r in runs], sorted((r["time"] for r in runs), reverse=True))

    def test_corrupt_registry_is_ignored(self):
        self.home.mkdir(parents=True)
        (self.home / "folders.json").write_text("{nope")
        self.assertEqual(history.known_folders(), [])
        history.remember_folder(str(self.a))                      # and it self-heals
        self.assertEqual(len(history.known_folders()), 1)


class CliTests(unittest.TestCase):
    def test_history_and_undo_commands(self):
        from app import fast_run
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            apply(root, [("a.txt", "AI"), ("b.txt", "DSA")])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(fast_run.main([d, "--history"]), 0)
            self.assertIn("applied", buf.getvalue())
            self.assertIn("AI (1)", buf.getvalue())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fast_run.main([d, "--undo-run", "0"]), 0)
            self.assertTrue((root / "a.txt").exists() and (root / "b.txt").exists())
            with contextlib.redirect_stderr(io.StringIO()):       # second undo and unknown id fail cleanly
                self.assertEqual(fast_run.main([d, "--undo-run", "0"]), 1)
                self.assertEqual(fast_run.main([d, "--undo-run", "9"]), 1)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(fast_run.main([d, "--undo-last"]), 0)
            self.assertIn("Nothing left", out.getvalue())


def _install_stub_fastapi():
    """Minimal stand-in so the router's own logic can be exercised without FastAPI installed."""
    fa = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code, detail=""):
            self.status_code, self.detail = status_code, detail

    class APIRouter:
        def __init__(self, **kw):
            self.routes = {}

        def _reg(self, method, path):
            def deco(fn):
                self.routes[(method, path)] = fn
                return fn
            return deco

        def get(self, path):
            return self._reg("GET", path)

        def post(self, path):
            return self._reg("POST", path)

    fa.APIRouter, fa.HTTPException = APIRouter, HTTPException
    sys.modules["fastapi"] = fa
    return fa


class RouterLogicTests(unittest.TestCase):
    """Runs the handlers directly. Real FastAPI wiring (body parsing etc.) is covered by its own
    well-tested machinery; here we check OUR logic: validation and error mapping."""

    @classmethod
    def setUpClass(cls):
        cls._had = sys.modules.get("fastapi")
        cls.fa = _install_stub_fastapi()
        sys.modules.pop("app.history_api", None)
        cls.api = importlib.import_module("app.history_api")

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("app.history_api", None)
        if cls._had is not None:
            sys.modules["fastapi"] = cls._had
        else:
            sys.modules.pop("fastapi", None)

    def test_endpoints(self):
        api, HTTPException = self.api, self.fa.HTTPException
        with tempfile.TemporaryDirectory() as d:
            apply(Path(d), [("a.txt", "AI")])
            r = api.router.routes
            self.assertEqual(len(r[("GET", "")](d)), 1)
            self.assertEqual(r[("GET", "/{run_id}")](0, d)["moves"][0]["to_name"], "a.txt")
            res = r[("POST", "/{run_id}/undo")](0, api.UndoRequest(folder=d))
            self.assertEqual((res["restored"], res["status"]), (1, "undone"))
            with self.assertRaises(HTTPException) as c:
                r[("POST", "/{run_id}/undo")](0, api.UndoRequest(folder=d))
            self.assertEqual(c.exception.status_code, 409)
            with self.assertRaises(HTTPException) as c:
                r[("GET", "/{run_id}")](7, d)
            self.assertEqual(c.exception.status_code, 404)
        with tempfile.TemporaryDirectory() as empty:              # a folder with no history is refused
            with self.assertRaises(HTTPException) as c:
                api.router.routes[("GET", "")](empty)
            self.assertEqual(c.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()

