"""Local HTTP bridge for the React desktop interface.

The preview endpoint builds a plan exactly once and stores it in memory.  Apply
uses that stored plan after revalidating its file paths; it never calls the LLM
again.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from typing import Dict, List
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core import history, provider_catalog, provider_store
from app.core.content_intelligence import (
    ContentSuggestion,
    plan_content_intelligence,
    sanitize_windows_filename,
)
from app.core.executor import execute_plan
from app.core.planner import Move, sanitize_target_path
from app.core.plan_sanity import evaluate_plan
from app.core.safety import (
    ScanPolicy,
    UnsafePathError,
    assert_safe_root,
    should_skip_directory,
    validate_move_paths,
)
from app.core.scanner import scan_directory_with_report
from app.main import resolve_models
from app.history_api import router as history_router

load_dotenv()

app = FastAPI(title="Sortly Local API")


@app.exception_handler(RequestValidationError)
async def validation_error(_request, error: RequestValidationError) -> JSONResponse:
    """Never echo request input, which may contain a provider credential."""
    detail = [
        {
            "loc": item.get("loc", ()),
            "msg": item.get("msg", "Invalid request"),
            "type": item.get("type", "validation_error"),
        }
        for item in error.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://tauri.localhost",
        "https://tauri.localhost",
        "tauri://localhost",
    ],
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
    allow_sensitive_cloud: bool = False


class ApplyFile(BaseModel):
    id: str
    accepted: bool = True
    target_path: str = ""
    destination_name: str = ""


class ApplyRequest(BaseModel):
    plan_id: str
    selected_targets: List[str] = Field(default_factory=list)
    files: List[ApplyFile] | None = None
    risk_acknowledged: bool = False


class UndoRequest(BaseModel):
    source: str


class ProviderConnectionRequest(BaseModel):
    model: str = ""
    base_url: str = ""
    api_key: str = Field(default="", repr=False)
    clear_api_key: bool = False


class ProviderConfigRequest(BaseModel):
    primary: str
    fallbacks: List[str] = Field(default_factory=list)
    connections: Dict[str, ProviderConnectionRequest] = Field(default_factory=dict)


class ModelDiscoveryRequest(BaseModel):
    provider: str
    model: str = ""
    base_url: str = ""
    api_key: str = Field(default="", repr=False)


class PlanItem(BaseModel):
    id: str
    name: str
    current_path: str
    destination_name: str
    target_path: str
    reasoning: str
    renamed: bool = False
    selected: bool = True
    category: str = ""
    confidence: str = "insufficient"
    evidence_excerpt: str = ""
    evidence_sources: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    filename_conflict: str = "none"
    evidence_sufficient: bool = False
    detected_type: str = "unknown"
    extraction_status: str = "unavailable"
    rename_reason: str = ""
    sensitive: bool = False


class PreviewResponse(BaseModel):
    plan_id: str
    source: str
    files: List[PlanItem]
    analysis_seconds: float
    already_organized: int = 0
    plan_warnings: List[str] = Field(default_factory=list)
    requires_confirmation: bool = False
    scan_summary: dict = Field(default_factory=dict)
    analysis_stats: dict = Field(default_factory=dict)


@dataclass
class StoredPlan:
    source: Path
    moves: List[Move]
    suggestions: List[ContentSuggestion] = field(default_factory=list)
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
        try:
            validate_move_paths(source, destination, plan.source)
        except UnsafePathError as error:
            raise HTTPException(400, f"Unsafe file operation: {error}") from error
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


def _unique_destination(destination: Path, taken: set[str]) -> Path:
    candidate = destination
    index = 1
    while str(candidate).casefold() in taken or candidate.exists():
        candidate = destination.with_name(f"{destination.stem}-{index}{destination.suffix}")
        index += 1
    taken.add(str(candidate).casefold())
    return candidate


def _content_moves(
    suggestions: List[ContentSuggestion],
    source: Path,
) -> List[Move]:
    moves = []
    taken: set[str] = set()
    for suggestion in suggestions:
        destination_folder = source.joinpath(
            suggestion.category,
            suggestion.subcategory,
        )
        destination = _unique_destination(destination_folder / suggestion.suggested_name, taken)
        moves.append(
            Move(
                suggestion.path,
                str(destination),
                suggestion.category,
                suggestion.reasoning,
                suggestion.subcategory,
                suggestion.selected,
            )
        )
    return moves


def _apply_user_edits(plan: StoredPlan, request: ApplyRequest) -> List[Move]:
    if request.files is None:
        available_targets = {_target_path(move) for move in plan.moves}
        selected_targets = set(request.selected_targets)
        if not selected_targets.issubset(available_targets):
            raise HTTPException(400, "The selected targets do not match this preview.")
        for move in plan.moves:
            move.selected = _target_path(move) in selected_targets
        return plan.moves

    edits = {edit.id: edit for edit in request.files}
    known_ids = {str(index) for index in range(len(plan.moves))}
    if not set(edits).issubset(known_ids):
        raise HTTPException(400, "The file decisions do not match this preview.")
    moves = []
    taken: set[str] = set()
    for index, original in enumerate(plan.moves):
        edit = edits.get(str(index))
        if edit is None or not edit.accepted:
            moves.append(
                Move(
                    original.src,
                    original.dest,
                    original.folder,
                    original.reason,
                    original.subfolder,
                    False,
                )
            )
            continue
        target_path = edit.target_path or "/".join(
            part for part in (original.folder, original.subfolder) if part
        )
        category, subcategory = sanitize_target_path(target_path)
        destination_name = sanitize_windows_filename(
            edit.destination_name or Path(original.src).name,
            Path(original.src).name,
        )
        destination = _unique_destination(
            plan.source.joinpath(category, subcategory, destination_name),
            taken,
        )
        moves.append(
            Move(
                original.src,
                str(destination),
                category,
                original.reason,
                subcategory,
                True,
            )
        )
    return moves


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/providers/config")
def provider_config() -> dict:
    return provider_store.public_config()


@app.post("/api/providers/config")
def save_provider_config(request: ProviderConfigRequest) -> dict:
    try:
        connections = {
            provider: connection.model_dump()
            for provider, connection in request.connections.items()
        }
        return provider_store.save_config(
            request.primary,
            request.fallbacks,
            connections,
        )
    except provider_store.ProviderConfigurationError as error:
        raise HTTPException(400, str(error)) from error


@app.post("/api/providers/models")
def provider_models(request: ModelDiscoveryRequest) -> dict:
    try:
        return provider_catalog.discover_models(
            request.provider,
            {
                "model": request.model,
                "base_url": request.base_url,
                "api_key": request.api_key,
            },
        )
    except provider_store.ProviderConfigurationError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:
        raise HTTPException(502, str(error)) from error


@app.post("/api/providers/test")
def test_provider_config() -> dict:
    try:
        results = provider_catalog.test_active_config()
    except provider_store.ProviderConfigurationError as error:
        raise HTTPException(400, str(error)) from error
    if not any(result["ok"] for result in results):
        return {"ok": False, "results": results}
    return {
        "ok": all(result["ok"] for result in results),
        "results": results,
    }


@app.post("/api/preview", response_model=PreviewResponse)
def preview(request: PreviewRequest) -> PreviewResponse:
    started = perf_counter()
    try:
        source = assert_safe_root(Path(request.source).expanduser())
    except (OSError, UnsafePathError) as error:
        raise HTTPException(400, str(error)) from error
    if not source.is_dir():
        raise HTTPException(400, "Choose an existing folder.")

    files, scan_summary = scan_directory_with_report(
        str(source),
        recursive=request.recursive,
        policy=ScanPolicy.from_env(),
    )
    plan_warnings = []
    requires_confirmation = False
    analysis_stats = {}
    if files:
        existing = sorted(
            item.name
            for item in source.iterdir()
            if item.is_dir()
            and not item.name.startswith(".")
            and not should_skip_directory(item)
        )
        try:
            active_models = provider_store.activate_runtime()
            result = plan_content_intelligence(
                files,
                str(source),
                active_models or resolve_models(request.models),
                known_folders=existing,
                use_cache=not request.reorganize,
                allow_rename=request.rename,
                allow_sensitive_cloud=request.allow_sensitive_cloud,
            )
            suggestions = result.suggestions
            moves = _content_moves(suggestions, source)
            sanity = evaluate_plan(suggestions)
            plan_warnings = list(sanity.warnings)
            requires_confirmation = sanity.requires_confirmation
            analysis_stats = dict(result.stats)
        except RuntimeError as error:
            raise HTTPException(502, str(error)) from error
    else:
        suggestions = []
        moves = []

    plan_id = str(uuid4())
    plans[plan_id] = StoredPlan(source=source, moves=moves, suggestions=suggestions)
    return PreviewResponse(
        plan_id=plan_id,
        source=str(source),
        analysis_seconds=round(perf_counter() - started, 2),
        already_organized=0,
        plan_warnings=plan_warnings,
        requires_confirmation=requires_confirmation,
        scan_summary={
            "files_seen": scan_summary.files_seen,
            "skipped": scan_summary.skipped,
            "reasons": scan_summary.reasons,
            "truncated": scan_summary.truncated,
        },
        analysis_stats=analysis_stats,
        files=[
            PlanItem(
                id=str(index),
                name=Path(move.src).name,
                current_path=str(Path(move.src).parent),
                destination_name=Path(move.dest).name,
                target_path=_target_path(move),
                reasoning=move.reason or "No reason provided",
                renamed=Path(move.src).name != Path(move.dest).name,
                selected=move.selected,
                category=suggestions[index].target_path,
                confidence=suggestions[index].confidence,
                evidence_excerpt=suggestions[index].evidence_excerpt,
                evidence_sources=suggestions[index].evidence_sources,
                warnings=suggestions[index].warnings,
                filename_conflict=suggestions[index].filename_conflict,
                evidence_sufficient=suggestions[index].sufficient_for_change,
                detected_type=suggestions[index].detected_type,
                extraction_status=suggestions[index].extraction_status,
                rename_reason=suggestions[index].rename_reason,
                sensitive=suggestions[index].sensitive,
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
    sanity = evaluate_plan(plan.suggestions)
    if sanity.requires_confirmation and not request.risk_acknowledged:
        raise HTTPException(409, "Review and acknowledge the unusual plan warnings before organizing.")

    plan.moves = _apply_user_edits(plan, request)

    _validate_for_apply(plan)
    done = execute_plan(
        plan.moves,
        str(plan.source),
        dry_run=False,
        source_root=str(plan.source),
    )
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
