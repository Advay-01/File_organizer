"""Local HTTP bridge for the React desktop interface.

The preview endpoint builds a plan exactly once and stores it in memory.  Apply
uses that stored plan after revalidating its file paths; it never calls the LLM
again.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from typing import Dict, List
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.core import history
from app.core.cached_planner import plan_with_cache
from app.core.executor import execute_plan
from app.core.fast_planner import config_from_env
from app.core.planner import Move
from app.core.scanner import scan_directory
from app.main import resolve_models
from app.history_api import router as history_router

load_dotenv()

app = FastAPI(title="Sortly Local API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
app.include_router(history_router)


class PreviewRequest(BaseModel):
    source: str
    recursive: bool = False
    rename: bool = False
    reorganize: bool = False
    models: List[str] | None = None


class ApplyRequest(BaseModel):
    plan_id: str
    selected_targets: List[str] = Field(default_factory=list)


class UndoRequest(BaseModel):
    source: str


class PlanItem(BaseModel):
    id: str
    name: str
    destination_name: str
    target_path: str
    reasoning: str
    renamed: bool = False
    selected: bool = True


class PreviewResponse(BaseModel):
    plan_id: str
    source: str
    files: List[PlanItem]
    analysis_seconds: float
    already_organized: int = 0


@dataclass
class StoredPlan:
    source: Path
    moves: List[Move]
    applied: bool = False


plans: Dict[str, StoredPlan] = {}


def _target_path(move: Move) -> str:
    return "/".join(part for part in (move.folder, move.subfolder) if part)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _validate_for_apply(plan: StoredPlan) -> None:
    for move in plan.moves:
        if not move.selected:
            continue
        source = Path(move.src)
        destination = Path(move.dest)
        if not _inside(source, plan.source) or not _inside(destination, plan.source):
            raise HTTPException(400, "Stored plan contains an unsafe file path.")
        if not source.is_file():
            raise HTTPException(409, f"Preview is stale because this file changed or disappeared: {source.name}")


def _hierarchy(root: Path, moves: List[dict]) -> dict:
    tree = {"name": root.name or str(root), "files": [], "folders": {}}
    for move in moves:
        destination = Path(move["dest"]).resolve()
        try:
            parts = destination.relative_to(root).parts
        except ValueError:
            continue
        current = tree
        for folder in parts[:-1]:
            current = current["folders"].setdefault(folder, {"name": folder, "files": [], "folders": {}})
        current["files"].append(parts[-1])

    def freeze(node: dict) -> dict:
        return {
            "name": node["name"],
            "files": sorted(node["files"]),
            "folders": [freeze(folder) for _, folder in sorted(node["folders"].items())],
        }

    return freeze(tree)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/preview", response_model=PreviewResponse)
def preview(request: PreviewRequest) -> PreviewResponse:
    started = perf_counter()
    source = Path(request.source).expanduser().resolve()
    if not source.is_dir():
        raise HTTPException(400, "Choose an existing folder.")

    files = scan_directory(str(source), recursive=request.recursive)
    if files:
        existing = sorted(item.name for item in source.iterdir() if item.is_dir() and not item.name.startswith("."))
        try:
            config = config_from_env()
            config.rename = request.rename or config.rename
            fast_result = plan_with_cache(
                files,
                str(source),
                resolve_models(request.models),
                known_folders=existing,
                config=config,
                use_cache=not request.reorganize,
            )
            moves = fast_result.moves
        except RuntimeError as error:
            raise HTTPException(502, str(error)) from error
    else:
        moves = []

    plan_id = str(uuid4())
    plans[plan_id] = StoredPlan(source=source, moves=moves)
    return PreviewResponse(
        plan_id=plan_id,
        source=str(source),
        analysis_seconds=round(perf_counter() - started, 2),
        already_organized=len(fast_result.already_organized) if files else 0,
        files=[
            PlanItem(
                id=str(index),
                name=Path(move.src).name,
                destination_name=Path(move.dest).name,
                target_path=_target_path(move),
                reasoning=move.reason or "No reason provided",
                renamed=Path(move.src).name != Path(move.dest).name,
                selected=move.selected,
            )
            for index, move in enumerate(moves)
        ],
    )


@app.post("/api/apply")
def apply(request: ApplyRequest) -> dict:
    plan = plans.get(request.plan_id)
    if plan is None:
        raise HTTPException(404, "Preview expired. Create a new preview before organizing.")
    if plan.applied:
        raise HTTPException(409, "This preview has already been applied. Create a new preview.")

    available_targets = {_target_path(move) for move in plan.moves}
    selected_targets = set(request.selected_targets)
    if not selected_targets.issubset(available_targets):
        raise HTTPException(400, "The selected targets do not match this preview.")
    for move in plan.moves:
        move.selected = _target_path(move) in selected_targets

    _validate_for_apply(plan)
    done = execute_plan(plan.moves, str(plan.source), dry_run=False)
    plan.applied = True
    if done:
        history.remember_folder(str(plan.source))
    return {
        "moved": len(done),
        "kept_in_root": sum(not move.selected for move in plan.moves),
        "hierarchy": _hierarchy(plan.source, done),
    }


@app.post("/api/undo")
def undo(request: UndoRequest) -> dict:
    source = Path(request.source).expanduser().resolve()
    if not source.is_dir():
        raise HTTPException(400, "Choose an existing folder.")
    result = history.undo_last_run(str(source))
    return {"restored": result["restored"] if result else 0}


@app.post("/api/open-folder")
def open_folder(request: UndoRequest) -> dict:
    source = Path(request.source).expanduser().resolve()
    if not source.is_dir():
        raise HTTPException(400, "Choose an existing folder.")
    try:
        subprocess.Popen(["explorer.exe", str(source)])
    except OSError as error:
        raise HTTPException(500, f"Could not open File Explorer: {error}") from error
    return {"opened": True}


@app.post("/api/choose-folder")
def choose_folder() -> dict:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.folder_picker"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception as error:
        raise HTTPException(500, f"Could not open the folder picker: {error}") from error
    if result.returncode != 0:
        raise HTTPException(500, "Could not open the folder picker.")
    selected = result.stdout.strip()
    if not selected:
        raise HTTPException(400, "No folder was selected.")
    return {"source": selected}
